"""Transaction folding: turn signal activity into request/response records.

A bus trace is thousands of value changes; the question is almost always "which
transactions happened, and what did each carry". Folding raw changes into records
is mechanical once someone says what a transaction *is*, so this takes the start
condition, the end condition and the fields to capture as arguments.

**No protocol library.** There is no built-in AXI, APB, AHB or anything else.
Protocol knowledge belongs to the caller: an agent can describe a handshake in a
prompt, but a hard-coded table of protocols would be wrong for every design that
deviates from the spec, and silently wrong at that. This module only evaluates
the conditions it is handed.

Edge versus level: a start condition like ``valid && ready`` is a *level* that
can hold for many cycles, while a transaction starts once. Conditions are
therefore evaluated as edges, on the rising transition of the condition
(not-true -> true), which is what "when this happens" means. ``expr_eval`` is
untouched: it evaluates a level, and the edge is derived here by comparing
consecutive evaluations.

An undecidable condition (x/z reaching the expression) never opens or closes a
transaction. Inventing a boundary from unknown data would fabricate records, so
the undecidable time is reported instead and an empty result is not proof that
nothing happened.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .. import timeutil
from ..netlist import expr_eval
from . import sampling

#: Node keys that may hold a child expression (mirrors expr_eval.evaluate).
_CHILD_KEYS = ("base", "cond", "t", "f", "a", "l", "r")

#: Ceiling on collected changes per watched signal.
_MAX_CHANGES = 1_000_000


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


def fold_transactions(fst, start_cond: Dict[str, Any],
                      end_cond: Dict[str, Any],
                      id_field: Optional[str] = None,
                      fields: Optional[Sequence[str]] = None,
                      start: int = 0, end: Optional[int] = None,
                      limit: Optional[int] = None) -> Dict[str, Any]:
    """Fold value changes into transaction records.

    Args:
        fst: an open FstSource.
        start_cond / end_cond: expr_eval expression objects. A transaction opens
            on the rising edge of ``start_cond`` and closes on the next rising
            edge of ``end_cond``.
        id_field: signal path whose value tags a transaction, letting an end
            match the start carrying the same tag (out-of-order completion).
            Without it, an end pairs with the oldest open transaction (FIFO).
        fields: extra signal paths to capture at open and close time.
        start / end: search window in FST time units.
        limit: target number of records in the reply.

    A transaction still open at the window end is reported with
    ``incomplete: true`` and no end time. It is neither dropped nor given a
    guessed end: "we stopped looking" and "it finished here" are different facts.
    """
    stop = fst.end_time if end is None else end
    fields = list(fields or [])

    names: List[str] = []
    _collect_names(start_cond, names)
    _collect_names(end_cond, names)
    if not names:
        return {"status": "error", "error_type": "empty_predicate",
                "error": "the conditions reference no signals",
                "parameter": "start_cond",
                "hint": 'a signal leaf looks like {"k":"sig","name":"<full path>"}'}
    watched = list(dict.fromkeys(
        names + ([id_field] if id_field else []) + fields))

    missing = [n for n in watched if n not in fst.signals]
    if missing:
        return {"status": "error", "error_type": "signal_not_found",
                "error": "signals not in the waveform: " + ", ".join(missing[:5]),
                "missing": missing,
                "hint": "use list_signals for exact full paths"}

    # One pass covering every watched signal, then replay the merged timeline.
    multi = fst._iter_values_multi([fst.signals[n] for n in watched],
                                   start, stop, _MAX_CHANGES)
    timelines = {n: sorted(multi.get(fst.signals[n].handle, []))
                 for n in watched}
    exp = fst.timescale_exp

    cur: Dict[str, Optional[str]] = {}
    for n in watched:
        v = fst.value_at(n, start)
        cur[n] = (v or {}).get("value")

    def state(node) -> str:
        return expr_eval.truth(expr_eval.evaluate(node, lambda nm: cur.get(nm)))

    def snapshot() -> Dict[str, Optional[str]]:
        return {f: cur.get(f) for f in fields}

    events = sorted({t for tl in timelines.values() for t, _ in tl if t > start})
    ptr = {n: 0 for n in watched}

    open_txns: List[Dict[str, Any]] = []
    done: List[Dict[str, Any]] = []
    undecidable = 0
    prev_s, prev_e = state(start_cond), state(end_cond)
    prev_t = start

    # A condition already true at the window start is not a rising edge: we did
    # not observe it become true, so no transaction is opened for it.
    for t in events:
        if t > prev_t and (prev_s == "x" or prev_e == "x"):
            undecidable += t - prev_t
        prev_t = t
        for n in watched:
            tl = timelines[n]
            p = ptr[n]
            while p < len(tl) and tl[p][0] <= t:
                cur[n] = tl[p][1]
                p += 1
            ptr[n] = p

        now_s, now_e = state(start_cond), state(end_cond)

        # Close before opening: one instant can both end a transaction and start
        # the next (back-to-back handshake), and the close must not consume the
        # transaction that is opening at the same time. An edge is "false the
        # instant before, true now": a condition coming out of x/z was never
        # observed false, so it opens or closes nothing (the x time is already
        # counted as undecidable above).
        if now_e == "1" and prev_e == "0":
            tag = cur.get(id_field) if id_field else None
            idx: Optional[int] = None
            if id_field:
                for k, txn in enumerate(open_txns):
                    if txn.get("id") == tag:
                        idx = k
                        break
            elif open_txns:
                idx = 0
            if idx is not None:
                txn = open_txns.pop(idx)
                txn["end"] = timeutil.format_fst_time(t, exp)
                txn["end_units"] = t
                txn["duration"] = timeutil.format_fst_time(
                    t - txn["start_units"], exp)
                txn["duration_units"] = t - txn["start_units"]
                if fields:
                    txn["fields_at_end"] = snapshot()
                done.append(txn)
            # An end with no matching open transaction is itself a fact worth
            # keeping: it means the trace shows a completion we never saw start.
            # With an id it is "no open transaction with this id"; without one,
            # "nothing was open at all". Neither is dropped.
            else:
                orphan = {"end": timeutil.format_fst_time(t, exp),
                          "end_units": t, "unmatched_end": True,
                          "note": "an end matched no open transaction"
                                  + (" with this id" if id_field else "")}
                if id_field:
                    orphan["id"] = tag
                done.append(orphan)

        if now_s == "1" and prev_s == "0":
            txn = {"start": timeutil.format_fst_time(t, exp),
                   "start_units": t}
            if id_field:
                txn["id"] = cur.get(id_field)
            if fields:
                txn["fields_at_start"] = snapshot()
            open_txns.append(txn)

        prev_s, prev_e = now_s, now_e

    if stop > prev_t and (prev_s == "x" or prev_e == "x"):
        undecidable += stop - prev_t

    for txn in open_txns:
        txn["incomplete"] = True
        txn["note"] = ("still open at the end of the search window; no end time "
                       "is reported because none was observed")
        done.append(txn)
    done.sort(key=lambda d: d.get("start_units", d.get("end_units", 0)))

    n_target = sampling.resolve_limit(limit)
    reduced = sampling.downsample(done, n_target)
    out: Dict[str, Any] = {
        "status": "ok",
        "count": reduced.pop("count"),
        "transactions": reduced.pop("values"),
        "complete": sum(1 for d in done
                        if not d.get("incomplete") and not d.get("unmatched_end")),
        "incomplete": sum(1 for d in done if d.get("incomplete")),
        "unmatched_ends": sum(1 for d in done if d.get("unmatched_end")),
        "pairing": "id_field" if id_field else "fifo",
        "watched_signals": watched,
    }
    out.update(reduced)          # sampled / sample_rate / total_available / ...
    if undecidable:
        out["undecidable_units"] = undecidable
        out["undecidable_time"] = timeutil.format_fst_time(undecidable, exp)
        out["undecidable_note"] = (
            "time where a condition could not be decided (x/z reached the "
            "expression); no boundary is claimed there, so an empty result is "
            "not proof that nothing happened")
    return out
