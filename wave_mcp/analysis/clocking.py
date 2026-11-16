"""Clock-edge extraction and edge-aligned sampling.

A waveform records value *changes*; most questions about synchronous logic are
about values *per clock cycle*. Turning one into the other is the same operation
whether it serves a diff (compare two runs cycle by cycle) or a direct
per-cycle table, so it lives here once and both callers import it.

Sampling on edges is not cosmetic: comparing raw change lists reports every
combinational glitch and every picosecond of phase jitter as a difference, which
buries the one difference that matters. Sampling at the edge asks what the logic
actually captured.
"""
from __future__ import annotations

from typing import Callable, List, Optional, Sequence, Tuple

from .. import timeutil

#: Levels treated as a settled low / high. A four-state waveform also carries
#: x and z, and an edge is only claimed for a clean 0->1 transition: treating
#: x->1 as a rising edge would invent cycles that the hardware never clocked.
_LOW = {"0", "l", "L"}
_HIGH = {"1", "h", "H"}


def _scan_edges(src, clock: str, start_units: int, end_units: int,
                edge: str, consume: Optional[Callable[[int, int], None]] = None
                ) -> int:
    """Count all clean edges in ``(start, end]``, optionally consuming each.

    The consumer receives the zero-based edge ordinal and its FST time. Both
    passes of a cycle table and the diff helper use this same detector.
    """
    b = src.backing(clock)
    if b is None:
        return 0
    sig, sel = b
    prev = (src.value_at(clock, start_units) or {}).get("value")
    want_rise = edge in ("rising", "both")
    want_fall = edge in ("falling", "both")
    count = 0

    def visit(t, _handle, raw):
        nonlocal prev, count
        if t <= start_units:
            return
        v = src._decode(raw)
        if sel is not None:
            v = src._slice(v, sig.length, sel[0], sel[1])
        if prev is not None and (
                (want_rise and prev in _LOW and v in _HIGH) or
                (want_fall and prev in _HIGH and v in _LOW)):
            if consume is not None:
                consume(count, t)
            count += 1
        prev = v

    src._scan_values([sig], start_units, end_units, visit)
    return count


def clock_edges(src, clock: str, start_units: int, end_units: Optional[int] = None,
                edge: str = "rising") -> List[int]:
    """All clean clock edges in ``(start, end]``, in FST units.

    A clock can be a vector bit. X/Z transitions do not count as clean edges.
    This list is complete or the scan raises; it has no change-count cap.
    """
    edges: List[int] = []
    _scan_edges(src, clock, start_units,
                src.end_time if end_units is None else end_units, edge,
                lambda _i, t: edges.append(t))
    return edges


def sample_at(rows: Sequence[Tuple[int, str]],
              times: Sequence[int]) -> List[Optional[str]]:
    """Sample a change list at ascending ``times`` (the value held at/before each).

    ``rows`` are ``(time_units, value)`` pairs in ascending time, the native
    shape from ``FstSource._iter_values_multi``. Returns one entry per requested
    time; None means the signal had no value yet at that point, which is
    distinct from a real x and must not be collapsed into one.

    Single pass over both sequences, so sampling N signals at M edges stays
    O(N*(changes+M)) rather than doing a search per edge.
    """
    out: List[Optional[str]] = []
    i, cur = 0, None
    for t in times:
        while i < len(rows) and rows[i][0] <= t:
            cur = rows[i][1]
            i += 1
        out.append(cur)
    return out


def sample_table(fst, paths: Sequence[str], clock: str,
                 start_units: int, end_units: int, edge: str = "rising",
                 limit: Optional[int] = None) -> dict:
    """A per-cycle table: one row per clock edge, one column per signal.

    Count the entire clock window, then scan again retaining only the chosen
    edge times. Read signal values at those times after iteration completes.
    Python memory is O(limit * signals), independent of waveform history.

    Signals not present in the waveform are reported in ``unknown_paths`` and
    left out of the columns, rather than filling a column with nulls that would
    read as "recorded but unknown".
    """
    from . import sampling

    n = sampling.resolve_limit(limit)
    total = _scan_edges(fst, clock, start_units, end_units, edge)
    wanted = iter(sampling.sample_indices(total, n))
    next_index = next(wanted, None)
    edges: List[int] = []

    def keep(i, t):
        nonlocal next_index
        if i == next_index:
            edges.append(t)
            next_index = next(wanted, None)

    if total:
        scanned = _scan_edges(fst, clock, start_units, end_units, edge, keep)
        if scanned != total or next_index is not None:
            raise RuntimeError("clock changed between sampling passes")
    known = [p for p in paths if fst.has_signal(p)]
    unknown = [p for p in paths if not fst.has_signal(p)]
    cycles = [{"time": timeutil.format_fst_time(t, fst.timescale_exp),
               "time_units": t, "values": {}} for t in edges]
    # fstapi caches one signal's random-access chain. Read ascending times for
    # each column to reuse it instead of decompressing it again for every row.
    for p in known:
        for row in cycles:
            value = fst.value_at(p, row["time_units"])
            row["values"][p] = (value or {}).get("value")

    reduced = sampling.sampled_result(cycles, total)
    out = {"clock": clock, "edge": edge,
           "signals": list(known),
           "cycles": reduced.pop("values"),
           "count": reduced.pop("count")}
    out.update(reduced)          # sampled / sample_rate / total_available / note
    if unknown:
        out["unknown_paths"] = unknown
    return out
