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

from typing import List, Optional, Sequence, Tuple

from .. import timeutil

#: Levels treated as a settled low / high. A four-state waveform also carries
#: x and z, and an edge is only claimed for a clean 0->1 transition: treating
#: x->1 as a rising edge would invent cycles that the hardware never clocked.
_LOW = "0lL"
_HIGH = "1hH"

#: Cap on changes pulled per signal when building a cycle table.
_DEFAULT_MAX_VALUES = 200_000


def clock_edges(src, clock: str, start_units: int, end_units: Optional[int] = None,
                edge: str = "rising", max_values: int = 200_000) -> List[int]:
    """Times of the clock's edges within a window, in FST units.

    Args:
        src: an open FstSource.
        clock: full path of the clock signal.
        start_units: window start; edges at or before it are not reported
            (an edge needs a previous level to be an edge at all).
        end_units: window end; defaults to the end of the dump.
        edge: "rising", "falling", or "both".
        max_values: cap on changes pulled for the clock itself.

    Only clean 0->1 / 1->0 transitions count. A transition through x or z is
    not an edge here, because the point of edge sampling is to ask what the
    logic captured, and an unknown clock captured nothing definite.
    """
    sig = src.signals.get(clock)
    if sig is None:
        return []
    stop = src.end_time if end_units is None else end_units
    pairs = src._iter_values(sig, start_units, stop, max_values)
    want_rise = edge in ("rising", "both")
    want_fall = edge in ("falling", "both")
    edges: List[int] = []
    prev: Optional[str] = None
    for t, v in pairs:
        if prev is not None:
            if want_rise and prev in _LOW and v in _HIGH:
                edges.append(t)
            elif want_fall and prev in _HIGH and v in _LOW:
                edges.append(t)
        prev = v
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

    All requested signals are read in a single pass over the file, so a wide
    table costs one sweep rather than one per signal.

    Signals not present in the waveform are reported in ``unknown_paths`` and
    left out of the columns, rather than filling a column with nulls that would
    read as "recorded but unknown".
    """
    from . import sampling

    edges = clock_edges(fst, clock, start_units, end_units, edge)
    known = [p for p in paths if p in fst.signals]
    unknown = [p for p in paths if p not in fst.signals]

    cols: dict = {}
    if edges and known:
        data = fst._iter_values_multi(
            [fst.signals[p] for p in known], fst.start_time, end_units,
            _DEFAULT_MAX_VALUES)
        for p in known:
            rows = sorted(data.get(fst.signals[p].handle, []))
            cols[p] = sample_at(rows, edges)

    cycles = []
    for i, t in enumerate(edges):
        cycles.append({"time": timeutil.format_fst_time(t, fst.timescale_exp),
                       "time_units": t,
                       "values": {p: cols[p][i] for p in known}})

    n = sampling.resolve_limit(limit)
    reduced = sampling.downsample(cycles, n)
    out = {"clock": clock, "edge": edge,
           "signals": list(known),
           "cycles": reduced.pop("values"),
           "count": reduced.pop("count")}
    out.update(reduced)          # sampled / sample_rate / total_available / note
    if unknown:
        out["unknown_paths"] = unknown
    return out
