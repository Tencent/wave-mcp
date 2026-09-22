"""Value reads for one or many signals, in a single pass over the file.

``signal_values`` used to be three tools (whole dump / window / single point).
They are one query with the window degenerating to everything or to a point, so
they are one function here, and asking for several signals at once costs one
file pass instead of N.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import timeutil
from . import sampling


def _rows_from(fst, handlemap, sig, start: int, end: int) -> List[dict]:
    """Format one signal's collected changes, mirroring values_between()."""
    out = []
    for t, v in sorted(handlemap.get(sig.handle, [])):
        out.append({"time": timeutil.format_fst_time(t, fst.timescale_exp),
                    "time_units": t,
                    "value": v,
                    "hex": fst._to_hex(v)})
    return out


def read_values(fst, paths: List[str], start: int, end: int,
                limit: Optional[int]) -> Dict[str, Any]:
    """Value changes for each path within ``[start, end]`` (FST time units).

    Signals backed by a single FST handle are read together in one pass.
    Aggregated buses (per-element VARs merged into one name) keep using the
    engine's own merge path, which already scans their elements in one pass.

    Returns ``{"signals": [ {path, count, values, ...} ]}`` with one entry per
    requested path, in request order; a path that is not in the waveform yields
    an error entry rather than aborting the batch.
    """
    n = sampling.resolve_limit(limit)
    scan = min(sampling.MAX_SCAN, max(n * 50, 100_000))

    plain: List[Any] = []
    plan: List[Any] = []
    for p in paths:
        sig = fst.signals.get(p)
        if sig is not None:
            plan.append((p, sig))
            plain.append(sig)
        else:
            plan.append((p, None))

    multi = fst._iter_values_multi(plain, start, end, scan) if plain else {}

    out: List[Dict[str, Any]] = []
    for p, sig in plan:
        if sig is not None:
            rows = _rows_from(fst, multi, sig, start, end)
        else:
            # aggregated bus / array root: the engine merges its elements
            rows = fst.values_between(p, start, end, scan)
            if rows is None:
                out.append({
                    "path": p,
                    "error": f"signal not found: {p}",
                    "hint": "use list_signals for exact full paths",
                })
                continue
        entry: Dict[str, Any] = {"path": p}
        entry.update(sampling.downsample(rows, n))
        out.append(entry)
    return {"signals": out}


def read_point(fst, paths: List[str], time_units: int,
               time_text: str) -> Dict[str, Any]:
    """The value each path holds at one instant (last change at or before it)."""
    out: List[Dict[str, Any]] = []
    for p in paths:
        val = fst.value_at(p, time_units)
        if val is None:
            out.append({"path": p,
                        "error": f"signal not found: {p}",
                        "hint": "use list_signals for exact full paths"})
            continue
        out.append({"path": p, **val})
    return {"time": time_text, "signals": out}
