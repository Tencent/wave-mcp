"""Waveform diff: first-divergence localization between two FST files.

The highest-leverage regression-debug primitive: given a pass waveform and
a fail waveform of the same design, find the first time the two runs
diverge and which signals diverge first. Those earliest divergers are the
prime suspects; everything later is usually downstream contagion.

The diverging signals feed directly into the netlist tools (signal_fanin /
active_drivers / signal_drivers) for causal backtracking, and into
open_wave_view for a dual-waveform diff view with an auto marker.

Pure data tool: no viewer assets required.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .sources.fst_source import FstSource
from . import timeutil

_MAX_VALUES_PER_SIGNAL = 200_000
_MAX_REPORTED = 50


def _common_signals(a: FstSource, b: FstSource,
                    scope: Optional[str],
                    signals: Optional[List[str]]) -> Tuple[List[str], List[str]]:
    """Return (comparable, missing) signal path lists."""
    if signals:
        wanted = list(signals)
    elif scope:
        prefix = scope.rstrip(".") + "."
        wanted = [p for p in a.signals if p.startswith(prefix)]
    else:
        wanted = list(a.signals.keys())
    comparable, missing = [], []
    for path in wanted:
        if path in a.signals and path in b.signals:
            comparable.append(path)
        else:
            missing.append(path)
    return comparable, missing


def _first_mismatch(rows_a: List[dict], rows_b: List[dict],
                    after_units: int) -> Optional[Tuple[int, str, str]]:
    """Merge two change lists; return (time, value_a, value_b) of the first
    differing sampled value after ``after_units``, or None if identical."""
    ia = ib = 0
    va = vb = None
    # seed with the last value at/before `after_units`
    while ia < len(rows_a) and rows_a[ia]["time_units"] <= after_units:
        va = rows_a[ia]["value"]; ia += 1
    while ib < len(rows_b) and rows_b[ib]["time_units"] <= after_units:
        vb = rows_b[ib]["value"]; ib += 1
    if va is not None and vb is not None and va != vb:
        return (after_units, va, vb)

    while ia < len(rows_a) or ib < len(rows_b):
        ta = rows_a[ia]["time_units"] if ia < len(rows_a) else None
        tb = rows_b[ib]["time_units"] if ib < len(rows_b) else None
        if tb is None or (ta is not None and ta <= tb):
            va = rows_a[ia]["value"]; t = ta; ia += 1
            if tb is not None and ta == tb:
                vb = rows_b[ib]["value"]; ib += 1
        else:
            vb = rows_b[ib]["value"]; t = tb; ib += 1
        if va is not None and vb is not None and va != vb:
            return (t, va, vb)
    return None


def _clock_edges(src: FstSource, clock: str, after_units: int) -> List[int]:
    rows = src.values_between(clock, after_units, src.end_time,
                              _MAX_VALUES_PER_SIGNAL) or []
    edges, prev = [], None
    for r in rows:
        v = r["value"]
        if prev is not None and prev in "0lL" and v in "1hH":
            edges.append(r["time_units"])
        prev = v
    return edges


def _sample_at(rows: List[dict], times: List[int]) -> List[Optional[str]]:
    """Sample a change list at the given ascending times (value at/before t)."""
    out: List[Optional[str]] = []
    i, cur = 0, None
    for t in times:
        while i < len(rows) and rows[i]["time_units"] <= t:
            cur = rows[i]["value"]; i += 1
        out.append(cur)
    return out


def diff_waveforms(fst_a: str, fst_b: str,
                   scope: Optional[str] = None,
                   signals: Optional[List[str]] = None,
                   clock: Optional[str] = None,
                   after: Optional[str] = None) -> Dict[str, Any]:
    """Compare two FST waveforms and locate the first divergence.

    Args:
        fst_a / fst_b: the two FST paths (conventionally pass / fail).
        scope: restrict comparison to signals under this instance path.
        signals: explicit signal list (overrides scope).
        clock: sample values on this clock's rising edges (filters phase
            jitter / combinational glitch false positives).
        after: skip differences before this time (e.g. "200ns" to ignore
            reset); default compares from time 0.
    """
    src_a = FstSource(fst_a)
    try:
        src_b = FstSource(fst_b)
    except Exception:
        src_a.close()
        raise
    try:
        after_units = 0
        if after:
            after_units = timeutil.time_to_fst_units(after, src_a.timescale_exp)

        comparable, missing = _common_signals(src_a, src_b, scope, signals)
        if not comparable:
            return {"status": "error",
                    "error": "no comparable signals between the two waveforms",
                    "missing_examples": missing[:10]}

        edges: Optional[List[int]] = None
        if clock:
            if clock not in src_a.signals or clock not in src_b.signals:
                return {"status": "error",
                        "error": f"clock signal not in both waveforms: {clock}"}
            edges = _clock_edges(src_a, clock, after_units)

        divergers: List[Dict[str, Any]] = []
        truncated = False
        for path in comparable:
            rows_a = src_a.values_between(path, after_units, src_a.end_time,
                                          _MAX_VALUES_PER_SIGNAL) or []
            rows_b = src_b.values_between(path, after_units, src_b.end_time,
                                          _MAX_VALUES_PER_SIGNAL) or []
            if (len(rows_a) >= _MAX_VALUES_PER_SIGNAL
                    or len(rows_b) >= _MAX_VALUES_PER_SIGNAL):
                truncated = True

            if edges is not None:
                sa = _sample_at(rows_a, edges)
                sb = _sample_at(rows_b, edges)
                hit = next(((edges[i], x, y) for i, (x, y)
                            in enumerate(zip(sa, sb))
                            if x is not None and y is not None and x != y),
                           None)
            else:
                hit = _first_mismatch(rows_a, rows_b, after_units)

            if hit:
                t, va, vb = hit
                divergers.append({
                    "path": path,
                    "time_units": t,
                    "time": timeutil.format_fst_time(t, src_a.timescale_exp),
                    "value_a": va, "value_b": vb,
                })

        divergers.sort(key=lambda d: d["time_units"])
        result: Dict[str, Any] = {
            "status": "ok",
            "compared": {"signals": len(comparable),
                         "identical": len(comparable) - len(divergers),
                         "diverging": len(divergers),
                         "missing_in_one": len(missing)},
            "coverage": "truncated" if truncated else "complete",
            "sampling": ("clock-aligned" if edges is not None
                         else "event-based"),
        }
        if divergers:
            first = divergers[0]
            result["first_divergence"] = {"time": first["time"],
                                          "time_units": first["time_units"]}
            shown = divergers[:_MAX_REPORTED]
            for d in shown:
                d["hint"] = ("earliest diverger — backtrack with signal_fanin"
                             "/active_drivers, then open_wave_view both FSTs "
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
        src_a.close()
        src_b.close()
