"""Time-window search: when did a boolean condition over signals hold?

The condition is a plain ``expr_eval`` expression node, the same structure the
netlist guard conditions already use, so there is no second expression language
to learn. A ``{"k":"sig","name":"..."}`` leaf names an FST signal path.

A predicate can only change value when one of its own signals changes, so the
search walks the union of those change times and evaluates there. No sampling
grid, no missed glitches, and the answer is *intervals* rather than a value
stream the caller would have to post-process.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import timeutil
from ..netlist import expr_eval

#: Ceiling on collected changes per signal; past this the window list would
#: describe a prefix of the request, which the caller is told about.
_MAX_CHANGES = 1_000_000

#: Node keys that may hold a child expression (see expr_eval.evaluate).
_CHILD_KEYS = ("base", "cond", "t", "f", "a", "l", "r")


def _collect_names(node: Any, out: List[str]) -> None:
    """Collect every signal path referenced by an expression node."""
    if not isinstance(node, dict):
        return
    if node.get("k") == "sig":
        name = node.get("name")
        if isinstance(name, str) and name:
            out.append(name)
        return
    for key in _CHILD_KEYS:
        _collect_names(node.get(key), out)


def find_time_windows(fst, predicate: Dict[str, Any], start: int, end: int,
                      min_duration_units: int = 0,
                      max_hits: int = 200) -> Dict[str, Any]:
    """Intervals inside ``[start, end]`` where ``predicate`` holds true.

    ``min_duration_units`` drops hits shorter than the threshold (the "held for
    at least N" query). ``max_hits`` caps how many intervals are reported; the
    sweep keeps counting so ``total_hits`` stays exact and ``truncated`` says
    whether the list was cut.

    A predicate that evaluates to x (unknown signal values) counts as *not
    true*: an unprovable window is never claimed as a hit. The time spent
    undecidable is reported separately so that an empty result is
    distinguishable from "the values were unknown throughout".
    """
    names: List[str] = []
    _collect_names(predicate, names)
    names = list(dict.fromkeys(names))
    if not names:
        return {"status": "error", "error_type": "empty_predicate",
                "error": "predicate references no signals",
                "hint": 'a signal leaf looks like {"k":"sig","name":"<full path>"}'}

    missing = [n for n in names if n not in fst.signals]
    if missing:
        return {"status": "error", "error_type": "signal_not_found",
                "error": "predicate references signals not in the waveform: "
                         + ", ".join(missing[:5]),
                "missing": missing,
                "hint": "use list_signals for exact full paths"}

    multi = fst._iter_values_multi([fst.signals[n] for n in names],
                                   start, end, _MAX_CHANGES)
    timelines = {n: sorted(multi.get(fst.signals[n].handle, []))
                 for n in names}
    exp = fst.timescale_exp

    cur: Dict[str, Optional[str]] = {}
    for n in names:
        v = fst.value_at(n, start)
        cur[n] = (v or {}).get("value")

    def state() -> str:
        return expr_eval.truth(
            expr_eval.evaluate(predicate, lambda nm: cur.get(nm)))

    events = sorted({t for tl in timelines.values() for t, _ in tl
                     if t > start})
    ptr = {n: 0 for n in names}
    hits: List[Dict[str, Any]] = []
    total = 0
    undecidable = 0

    def record(a: int, b: int, open_ended: bool) -> None:
        nonlocal total
        dur = b - a
        if dur <= 0 or dur < min_duration_units:
            return
        total += 1
        if len(hits) < max_hits:
            hits.append({"start": timeutil.format_fst_time(a, exp),
                         "end": timeutil.format_fst_time(b, exp),
                         "duration": timeutil.format_fst_time(dur, exp),
                         "open_ended": open_ended})

    sv = state()
    prev_t = start
    open_at: Optional[int] = start if sv == "1" else None

    for t in events:
        if t > prev_t and sv == "x":
            undecidable += t - prev_t
        prev_t = t
        for n in names:
            tl = timelines[n]
            p = ptr[n]
            while p < len(tl) and tl[p][0] <= t:
                cur[n] = tl[p][1]
                p += 1
            ptr[n] = p
        nv = state()
        if nv != sv:
            if sv == "1" and open_at is not None:
                record(open_at, t, False)
                open_at = None
            if nv == "1":
                open_at = t
        sv = nv

    # the final state holds from the last event to the window end
    if end > prev_t and sv == "x":
        undecidable += end - prev_t
    if sv == "1" and open_at is not None:
        record(open_at, end, True)

    return {"windows": hits,
            "total_hits": total,
            "truncated": total > len(hits),
            "undecidable_units": undecidable,
            "undecidable_time": timeutil.format_fst_time(undecidable, exp),
            "signals": names}
