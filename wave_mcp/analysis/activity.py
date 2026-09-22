"""Per-signal activity statistics over a time window.

Motivation: to learn "which of these 200 signals actually moved in this window,
and did any of them spend time unknown", an agent previously had to pull every
value timeline and count locally, which blows up the context window. This
answers it in one pass over the file.

Ratios are *time weighted* (how long a value was held), not change-count
weighted: a signal that goes x once and stays x reports the large x_ratio a
debugger means by "this signal is mostly unknown here".

Every backing signal of every requested path is read in ONE file pass
(``_iter_values_multi``); never one pass per signal.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import timeutil

#: Ceiling on collected changes per backing signal for one activity query.
#: Past this the statistics would silently describe a prefix of the window, so
#: the row is marked ``truncated`` instead of reporting a wrong ratio.
_MAX_CHANGES = 1_000_000


def _unknown(bits: Optional[str], ch: str) -> bool:
    return ch in (bits or "").lower()


def _merge_aggregated(buckets: List[List[Any]],
                      init_parts: List[str]) -> List[Any]:
    """Combine per-element timelines into one MSB-first value timeline.

    Timestamps are the union of every element's change times; each element keeps
    its previous value between its own changes (carry-forward), seeded from the
    value it held at the window start.
    """
    times = set()
    for b in buckets:
        times.update(t for t, _ in b)
    if not times:
        return []
    cur = list(init_parts)
    ptr = [0] * len(buckets)
    out: List[Any] = []
    for t in sorted(times):
        for i, b in enumerate(buckets):
            p = ptr[i]
            while p < len(b) and b[p][0] <= t:
                cur[i] = b[p][1]
                p += 1
            ptr[i] = p
        out.append((t, "".join(cur)))
    return out


def _stats(init: Optional[str], timeline: List[Any], start: int, end: int,
           exp: int, truncated: bool) -> Dict[str, Any]:
    cur = init
    prev_t = start
    x_units = z_units = 0
    toggles = 0
    first_t: Optional[int] = None
    last_t: Optional[int] = None

    for t, v in timeline:
        if t > prev_t:
            # `cur` was held over [prev_t, t)
            if _unknown(cur, "x"):
                x_units += t - prev_t
            if _unknown(cur, "z"):
                z_units += t - prev_t
            prev_t = t
        if v != cur:
            toggles += 1
            if first_t is None:
                first_t = t
            last_t = t
        cur = v

    if end > prev_t:
        # tail: `cur` was held over [prev_t, end)
        if _unknown(cur, "x"):
            x_units += end - prev_t
        if _unknown(cur, "z"):
            z_units += end - prev_t

    total = end - start
    return {
        "toggles": toggles,
        "x_ratio": round(x_units / total, 6) if total > 0 else 0.0,
        "z_ratio": round(z_units / total, 6) if total > 0 else 0.0,
        "is_constant": toggles == 0,
        "first_change": (timeutil.format_fst_time(first_t, exp)
                         if first_t is not None else None),
        "last_change": (timeutil.format_fst_time(last_t, exp)
                        if last_t is not None else None),
        "value_first": init if init is not None else "",
        "value_last": cur if cur is not None else "",
        "truncated": truncated,
    }


def signal_activity(fst, paths, start: int, end: int) -> List[Dict[str, Any]]:
    """Activity statistics for each path over ``[start, end]`` (FST time units).

    ``paths`` is one signal path or a list of them. Returns one row per
    requested path in request order. A path that is not in the waveform comes
    back as an ``error`` row instead of raising, so one bad name does not lose
    the rest of the batch.
    """
    if isinstance(paths, str):
        paths = [paths]

    # (path, backing Signals or None, aggregated?)
    plan: List[Any] = []
    for p in paths:
        sig = fst.signals.get(p)
        if sig is not None:
            plan.append((p, [sig], False))
            continue
        elems = fst._element_signals(p)
        if elems:
            # an aggregated bus / unpacked-array root as listed by list_signals
            plan.append((p, elems, True))
            continue
        plan.append((p, None, False))

    uniq: Dict[int, Any] = {}
    for _p, sigs, _agg in plan:
        for s in (sigs or []):
            uniq.setdefault(s.handle, s)
    multi = (fst._iter_values_multi(list(uniq.values()), start, end,
                                    _MAX_CHANGES) if uniq else {})

    exp = fst.timescale_exp
    rows: List[Dict[str, Any]] = []
    for p, sigs, is_agg in plan:
        if sigs is None:
            rows.append({
                "path": p,
                "error": f"signal not found: {p}",
                "reason": "the path is not in this waveform's hierarchy",
                "hint": "use list_signals for exact full paths; an aggregated "
                        "bus must be spelled the way list_signals returns it",
            })
            continue
        buckets = [sorted(multi.get(s.handle, [])) for s in sigs]
        truncated = any(len(b) >= _MAX_CHANGES for b in buckets)
        if is_agg:
            init_parts = []
            for s in sigs:
                v = fst.value_at(s.full_path, start)
                init_parts.append((v or {}).get("value", ""))
            init = "".join(init_parts)
            timeline = _merge_aggregated(buckets, init_parts)
        else:
            v = fst.value_at(p, start)
            init = (v or {}).get("value", "")
            timeline = buckets[0]
        row: Dict[str, Any] = {"path": p}
        row.update(_stats(init, timeline, start, end, exp, truncated))
        rows.append(row)
    return rows
