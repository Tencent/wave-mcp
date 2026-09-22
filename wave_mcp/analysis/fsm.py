"""State-register transitions and branch reachability, observed in one run.

Two questions about a state machine that a waveform can answer directly: which
state transitions actually occurred, and which of the RTL branches that assign
the state register were ever taken.

**This is not coverage.** Coverage needs a denominator (the set of legal states
and transitions), which comes from the design intent, not from one dump. So this
reports only what was observed and never says a missing transition is a bug or a
gap: a state absent from one run may be unreachable by design, excluded by the
stimulus, or a genuine hole, and a single waveform cannot tell those apart. The
caller decides.

Branch reachability is evaluated by replaying each driver's guard condition
against the waveform at the transition times, so a branch is reported as taken
only when its guard actually held at a moment the register changed.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import timeutil
from ..netlist import expr_eval

_MAX_CHANGES = 1_000_000


def _guard_text(guard: List[Dict[str, Any]]) -> str:
    """Readable form of a guard chain, for naming a branch in the reply."""
    parts = []
    for g in guard or []:
        node = g.get("cond")
        expect = g.get("expect", 1)
        text = _expr_text(node)
        parts.append(text if expect else f"!({text})")
    return " && ".join(parts) if parts else "(unconditional)"


def _expr_text(node: Any) -> str:
    """Best-effort source-like rendering of an expression node."""
    if not isinstance(node, dict):
        return str(node)
    kind = node.get("k")
    if kind == "sig":
        return str(node.get("name", "?"))
    if kind == "const":
        return str(node.get("lit", "?"))
    if kind == "un":
        return f"{node.get('op')}({_expr_text(node.get('a'))})"
    if kind == "bin":
        return (f"({_expr_text(node.get('l'))} {node.get('op')} "
                f"{_expr_text(node.get('r'))})")
    if kind == "bitselect":
        return f"{_expr_text(node.get('base'))}[{node.get('idx')}]"
    if kind == "cond":
        return (f"({_expr_text(node.get('cond'))} ? "
                f"{_expr_text(node.get('t'))} : {_expr_text(node.get('f'))})")
    return kind or "?"


def _collect_names(node: Any, out: List[str]) -> None:
    if not isinstance(node, dict):
        return
    if node.get("k") == "sig":
        name = node.get("name")
        if isinstance(name, str) and name:
            out.append(name)
        return
    for key in ("base", "cond", "t", "f", "a", "l", "r"):
        _collect_names(node.get(key), out)


def fsm_transitions(fst, rtl, path: str, start: int, end: int,
                    max_states: int = 200) -> Dict[str, Any]:
    """Observed state transitions of ``path`` plus branch reachability.

    Args:
        fst: an open FstSource.
        rtl: the session's RtlSource (for the driver guards; optional).
        path: full path of the state register.
        start / end: window in FST time units.
        max_states: cap on distinct states reported.

    Returns states seen, transition pairs with counts and first occurrence, and
    (when a netlist is available) one entry per assigning branch saying whether
    its guard ever held at a transition instant.
    """
    sig = fst.signals.get(path)
    if sig is None:
        return {"status": "error", "error_type": "signal_not_found",
                "error": f"signal not found: {path}", "parameter": "path",
                "hint": "use list_signals for exact full paths"}

    exp = fst.timescale_exp
    rows = sorted(fst._iter_values(sig, start, end, _MAX_CHANGES))
    held = fst.value_at(path, start)
    prev = (held or {}).get("value")

    states: Dict[str, int] = {}
    if prev is not None:
        states[prev] = 0
    trans: Dict[tuple, Dict[str, Any]] = {}
    change_times: List[int] = []

    for t, val in rows:
        if val == prev:
            continue
        change_times.append(t)
        states[val] = states.get(val, 0) + 1
        if prev is not None:
            key = (prev, val)
            rec = trans.get(key)
            if rec is None:
                trans[key] = {"from": prev, "to": val, "count": 1,
                              "first_time": timeutil.format_fst_time(t, exp),
                              "first_time_units": t}
            else:
                rec["count"] += 1
        prev = val

    out: Dict[str, Any] = {
        "status": "ok",
        "signal": path,
        "states_seen": sorted(states)[:max_states],
        "state_count": len(states),
        "transitions": sorted(trans.values(),
                              key=lambda d: d["first_time_units"]),
        "changes": len(change_times),
        "note": ("observed in this run only; this is not coverage. A state or "
                 "transition not listed may be unreachable by design, not "
                 "exercised by this stimulus, or a real gap, and one waveform "
                 "cannot distinguish those."),
    }

    branches = _branches(fst, rtl, path, change_times)
    if branches is not None:
        out["branches"] = branches
    else:
        out["branches_note"] = ("no netlist for this signal, so RTL branches "
                                "were not evaluated")
    return out


def _branches(fst, rtl, path: str,
              change_times: List[int]) -> Optional[List[Dict[str, Any]]]:
    """Per-driver guard reachability, evaluated just *before* each change.

    The guard must be sampled before the change, not at it. A register updates
    from the condition that held going into the edge, and a reset released on
    the same edge that clears the register already reads as deasserted at the
    change timestamp. Evaluating at the change time therefore reports the reset
    branch as never taken, which is wrong. Sampling one time unit earlier asks
    what the logic actually saw.
    """
    if rtl is None or not getattr(rtl, "has_netlist", False):
        return None
    info = rtl.drivers(path)
    if not info.get("available"):
        return None
    drivers = info.get("drivers") or []
    if not drivers:
        return None

    # Guards reference module-local names; the waveform needs full paths, so
    # resolve against the register's own instance scope.
    inst = path.rsplit(".", 1)[0]
    #: sample point per change: the instant before it, floored at the dump start
    probes = [(t, max(fst.start_time, t - 1)) for t in change_times]

    rows: List[Dict[str, Any]] = []
    for drv in drivers:
        guard = drv.get("guard") or []
        names: List[str] = []
        for g in guard:
            _collect_names(g.get("cond"), names)
        names = list(dict.fromkeys(names))

        taken = 0
        undecided = 0
        first: Optional[int] = None
        for t, probe in probes:
            vals: Dict[str, Optional[str]] = {}
            for n in names:
                full = n if n in fst.signals else f"{inst}.{n}"
                v = fst.value_at(full, probe)
                vals[n] = (v or {}).get("value")
            verdict = _guard_holds(guard, vals)
            if verdict is True:
                taken += 1
                if first is None:
                    first = t
            elif verdict is None:
                undecided += 1

        row: Dict[str, Any] = {
            "guard": _guard_text(guard),
            "file": drv.get("file"),
            "line": drv.get("line"),
            "snippet": (drv.get("snippet") or "").strip(),
            "taken": taken > 0,
            "count": taken,
        }
        if first is not None:
            row["first_time"] = timeutil.format_fst_time(first,
                                                         fst.timescale_exp)
        if undecided:
            row["undecided_at"] = undecided
            row["undecided_note"] = ("the guard could not be decided at this "
                                     "many transition instants (x/z), so "
                                     "'taken: false' is not proof it never ran")
        rows.append(row)
    return rows


def _guard_holds(guard: List[Dict[str, Any]],
                 values: Dict[str, Optional[str]]) -> Optional[bool]:
    """Whether every clause of a guard chain holds; None when undecidable."""
    if not guard:
        return True
    unknown = False
    for g in guard:
        res = expr_eval.truth(
            expr_eval.evaluate(g.get("cond"), lambda nm: values.get(nm)))
        expect = "1" if g.get("expect", 1) else "0"
        if res == "x":
            unknown = True
        elif res != expect:
            return False
    return None if unknown else True
