"""Waveform diff: first-divergence localization across N FST files.

The highest-leverage regression-debug primitive: given several runs of the same
design, find the first time they stop agreeing and which signals split first.
Those earliest divergers are the prime suspects; everything later is usually
downstream contagion.

Two runs is the classic pass/fail case. More than two is what a regression sweep
actually produces, and comparing them together answers a question pairwise diffs
cannot: *how* the runs partition. Three runs agreeing and one differing points at
that one run's stimulus; a two-two split points at a configuration difference.
That grouping is the ``groups`` field.

The diverging signals feed directly into the netlist tools (signal_fanin /
active_drivers / signal_drivers) for causal backtracking, and into
open_wave_view for a dual-waveform diff view with an auto marker.

Attribution: this code was written independently and reuses nothing from
TraceWeave. First-divergence localization was already on our development
roadmap. TraceWeave's diff_first_divergence came earlier, and we referred
to it when prioritising this feature (MIT, Copyright (c) 2025
gokeshenzhen, https://github.com/gokeshenzhen/TraceWeave). See
docs/THIRD_PARTY.md for the full notice.

Pure data tool: no viewer assets required.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .sources.fst_source import FstSource
from . import timeutil
from .analysis.clocking import clock_edges, sample_at

_MAX_VALUES_PER_SIGNAL = 200_000
_MAX_REPORTED = 50
#: signals collected per file pass in diff: one whole batch shares a single
#: IterBlocks2 sweep, so this trades a little memory for far fewer scans
#: (256 signals per pass instead of one pass per signal).
_DIFF_BATCH = 256


def _borrow_fst(path: str, sessions, owner_id: Optional[str] = None
                ) -> Tuple[FstSource, Callable[[], None]]:
    """Return ``(source, release)`` for ``path``.

    When the caller's own sessions already hold this waveform, that reader is
    reused under a lease: repeated diffs on the same pair then skip re-parsing
    the hierarchy, and the lease keeps the reader alive even if a session is
    closed while the comparison runs. Only the caller's owner is considered, so a
    diff can never borrow somebody else's handle to save a parse. ``release``
    belongs in the caller's ``finally``.
    """
    if sessions is not None:
        try:
            borrowed = sessions.borrow_fst(path, owner_id=owner_id)
        except Exception:  # pylint: disable=broad-except
            borrowed = None  # borrowing is best-effort; fall back to a fresh open
        if borrowed is not None:
            return borrowed.fst, borrowed.release
    src = FstSource(path)
    return src, src.close


def _common_signals(srcs: Sequence[FstSource],
                    scope: Optional[str],
                    signals: Optional[List[str]]) -> Tuple[List[str], List[str]]:
    """Signals present in *every* run, plus those missing from at least one.

    A signal only one run recorded cannot be compared, and quietly dropping it
    would let a renamed or removed signal look like agreement.
    """
    first = srcs[0]
    if signals:
        wanted = list(signals)
    elif scope:
        prefix = scope.rstrip(".") + "."
        wanted = [p for p in first.signals if p.startswith(prefix)]
    else:
        wanted = list(first.signals.keys())
    comparable, missing = [], []
    for path in wanted:
        if all(path in s.signals for s in srcs):
            comparable.append(path)
        else:
            missing.append(path)
    return comparable, missing


def _groups_of(values: Sequence[Optional[str]]) -> Optional[Dict[str, List[int]]]:
    """Partition run indices by value; None when everyone who has a value agrees.

    A run whose value is None (nothing recorded yet at this point) takes no part:
    "not yet known" is not evidence of a difference.
    """
    groups: Dict[str, List[int]] = {}
    for idx, val in enumerate(values):
        if val is None:
            continue
        groups.setdefault(val, []).append(idx)
    return groups if len(groups) > 1 else None


def _seed_values(rows_per_run: Sequence[Sequence[Tuple[int, str]]],
                 after_units: int) -> Tuple[List[Optional[str]], List[int]]:
    """Values held at ``after_units``, plus each run's read position past it."""
    held: List[Optional[str]] = []
    pos: List[int] = []
    for rows in rows_per_run:
        i, cur = 0, None
        while i < len(rows) and rows[i][0] <= after_units:
            cur = rows[i][1]
            i += 1
        held.append(cur)
        pos.append(i)
    return held, pos


def _first_divergence(rows_per_run: Sequence[Sequence[Tuple[int, str]]],
                      after_units: int
                      ) -> Optional[Tuple[int, Dict[str, List[int]]]]:
    """First time the runs disagree on one signal, with the value grouping.

    Walks all N change lists in one merged sweep, advancing whichever runs share
    the next earliest timestamp, then re-checking agreement. Event-based, so a
    difference is caught at the exact change that caused it.
    """
    held, pos = _seed_values(rows_per_run, after_units)
    groups = _groups_of(held)
    if groups:
        return after_units, groups

    n = len(rows_per_run)
    while True:
        nxt = None
        for r in range(n):
            rows = rows_per_run[r]
            if pos[r] < len(rows):
                t = rows[pos[r]][0]
                if nxt is None or t < nxt:
                    nxt = t
        if nxt is None:
            return None
        for r in range(n):
            rows = rows_per_run[r]
            while pos[r] < len(rows) and rows[pos[r]][0] == nxt:
                held[r] = rows[pos[r]][1]
                pos[r] += 1
        groups = _groups_of(held)
        if groups:
            return nxt, groups


def diff_waveforms(fst_paths: List[str],
                   scope: Optional[str] = None,
                   signals: Optional[List[str]] = None,
                   clock: Optional[str] = None,
                   after: Optional[str] = None,
                   sessions=None,
                   owner_id: Optional[str] = None) -> Dict[str, Any]:
    """Compare N FST waveforms and locate the first divergence.

    Args:
        fst_paths: two or more FST paths. Run indices in the reply refer to
            this list's order, so index 0 is conventionally the passing run.
        scope: restrict comparison to signals under this instance path.
        signals: explicit signal list (overrides scope).
        clock: sample values on this clock's rising edges (filters phase
            jitter / combinational glitch false positives).
        after: skip differences before this time (e.g. "200ns" to ignore
            reset); default compares from time 0.
        sessions: optional SessionManager; readers already open for this owner
            are reused under a lease, which keeps a concurrent close from
            pulling a handle out of the comparison.
        owner_id: whose sessions may be borrowed. Nothing else is ever reached
            into: a diff must not read another owner's handle to save a parse.
    """
    paths = [p for p in (fst_paths or []) if p]
    if len(paths) < 2:
        return {"status": "error", "error_type": "invalid_argument",
                "error": "fst_paths needs at least two waveforms",
                "parameter": "fst_paths",
                "hint": "pass the runs to compare, e.g. [pass.fst, fail.fst]"}

    srcs: List[FstSource] = []
    releases: List[Callable[[], None]] = []
    try:
        for p in paths:
            src, release = _borrow_fst(p, sessions, owner_id)
            srcs.append(src)
            releases.append(release)

        exp = srcs[0].timescale_exp
        after_units = 0
        if after:
            try:
                after_units = timeutil.time_to_fst_units(after, exp)
            except ValueError as exc:
                return {"status": "error", "error_type": "invalid_argument",
                        "error": str(exc), "parameter": "after"}

        comparable, missing = _common_signals(srcs, scope, signals)
        if not comparable:
            return {"status": "error",
                    "error": "no comparable signals across the waveforms",
                    "missing_examples": missing[:10]}

        edges: Optional[List[int]] = None
        if clock:
            absent = [paths[i] for i, s in enumerate(srcs)
                      if clock not in s.signals]
            if absent:
                return {"status": "error", "error_type": "invalid_argument",
                        "error": f"clock signal not in every waveform: {clock}",
                        "parameter": "clock",
                        "missing_in": absent}
            edges = clock_edges(srcs[0], clock, after_units,
                                max_values=_MAX_VALUES_PER_SIGNAL)

        divergers: List[Dict[str, Any]] = []
        truncated = False
        # Compare in batches: every batch of signals shares ONE file pass per
        # waveform, so an N-signal, R-run diff pays R passes per batch rather
        # than one per signal per run.
        for start in range(0, len(comparable), _DIFF_BATCH):
            batch = comparable[start:start + _DIFF_BATCH]
            per_run = []
            for src in srcs:
                per_run.append(src._iter_values_multi(
                    [src.signals[p] for p in batch], after_units,
                    src.end_time, _MAX_VALUES_PER_SIGNAL))
            for path in batch:
                rows_per_run = [
                    data.get(src.signals[path].handle, [])
                    for src, data in zip(srcs, per_run)]
                if any(len(r) >= _MAX_VALUES_PER_SIGNAL for r in rows_per_run):
                    truncated = True

                if edges is not None:
                    sampled = [sample_at(rows, edges) for rows in rows_per_run]
                    hit = None
                    for i, t in enumerate(edges):
                        groups = _groups_of([s[i] for s in sampled])
                        if groups:
                            hit = (t, groups)
                            break
                else:
                    hit = _first_divergence(rows_per_run, after_units)

                if hit:
                    t, groups = hit
                    row: Dict[str, Any] = {
                        "path": path,
                        "time_units": t,
                        "time": timeutil.format_fst_time(t, exp),
                        "groups": groups,
                    }
                    if len(paths) == 2:
                        # keep the two-run case directly readable
                        vals = {i: v for v, idxs in groups.items() for i in idxs}
                        row["value_a"] = vals.get(0)
                        row["value_b"] = vals.get(1)
                    divergers.append(row)

        divergers.sort(key=lambda d: d["time_units"])
        result: Dict[str, Any] = {
            "status": "ok",
            "runs": [{"index": i, "fst": p} for i, p in enumerate(paths)],
            "compared": {"signals": len(comparable),
                         "identical": len(comparable) - len(divergers),
                         "diverging": len(divergers),
                         "missing_in_one": len(missing)},
            "coverage": "truncated" if truncated else "complete",
            "sampling": ("clock-aligned" if edges is not None
                         else "event-based"),
            # normalized form of `after`, so callers (and the request digest)
            # see the instant actually used rather than the spelling given
            "after_units": after_units,
        }
        if divergers:
            first = divergers[0]
            result["first_divergence"] = {"time": first["time"],
                                          "time_units": first["time_units"]}
            shown = divergers[:_MAX_REPORTED]
            for d in shown:
                d["hint"] = ("earliest diverger — backtrack with signal_fanin"
                             "/active_drivers, then open_wave_view the runs "
                             "with a marker here"
                             if d is shown[0] else
                             "likely downstream of earlier divergers")
            result["diverging_signals"] = shown
            if len(divergers) > _MAX_REPORTED:
                result["diverging_truncated"] = len(divergers) - _MAX_REPORTED
        else:
            result["first_divergence"] = None
            result["note"] = ("waveforms are identical over the compared "
                              "signals" + (" (coverage truncated — narrow "
                              "scope or time window before concluding clean)"
                              if truncated else ""))
        if truncated:
            result["hint"] = ("some signals exceeded the per-signal change "
                              "budget; re-run with a narrower scope, an "
                              "explicit signal list, or a later `after` time")
        return result
    finally:
        for release in releases:
            release()
