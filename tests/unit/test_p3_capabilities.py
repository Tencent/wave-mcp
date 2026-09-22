#!/usr/bin/env python3
"""P3 capability suite: clock sampling, N-ary diff, downstream, FSM, transactions.

Each capability is cross-checked against an independent source rather than
against itself:

- ``sample_at_clock`` values are re-derived with ``signal_values(time=...)``.
- ``fsm_transitions`` branch verdicts are checked against the sample design's
  known reset/increment semantics, including the case that first exposed a real
  bug (a reset released on the same edge that clears the register).
- ``fold_transactions`` records are checked against the actual value timeline.
- N-ary diff is checked on identical inputs (must find nothing) and on a
  deliberately altered copy (must find the change, and group the runs).

Run directly or via tests/run_regression.py.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)

from fstgen import write_fst                          # noqa: E402
from wave_mcp import server as srv                    # noqa: E402
from session_client import bound             # noqa: E402
from wave_mcp.analysis import clocking                # noqa: E402

PASSED, FAILED = [], []

SAMPLE = os.path.join(HERE, "..", "..", "examples", "sample", "session")
FST = os.path.join(HERE, "..", "..", "examples", "sample", "dump.fst")
CLK = "top_tb.clk"
RST = "top_tb.rst_n"
CNT = "top_tb.u_counter.count"


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSED if cond else FAILED).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  ({detail})" if detail and not cond else ""))


def _lit(v: str, sig: str) -> dict:
    return {"k": "bin", "op": "Equality",
            "l": {"k": "sig", "name": sig},
            "r": {"k": "const", "lit": v}}


def _stage_clock() -> None:
    print("== sample_at_clock ==")
    srv = bound(SAMPLE)
    srv.query_defaults_clear()

    out = srv.sample_at_clock(paths=[CNT, RST], clock=CLK)
    check("returns a cycle table", out["count"] > 0, str(out.get("count")))
    check("columns are the requested signals",
          out["signals"] == [CNT, RST], str(out["signals"]))

    # independent re-derivation: each cell must equal value_at that instant
    bad = []
    for row in out["cycles"]:
        pt = srv.signal_values(paths=[CNT, RST], time=row["time"])
        got = {e["path"]: e.get("value") for e in pt["signals"]}
        for sig, val in row["values"].items():
            if got.get(sig) != val:
                bad.append((row["time"], sig, val, got.get(sig)))
    check("every sampled value matches signal_values(time=...)",
          not bad, str(bad[:3]))

    rise = srv.sample_at_clock(paths=[CNT], clock=CLK, edge="rising")["count"]
    fall = srv.sample_at_clock(paths=[CNT], clock=CLK, edge="falling")["count"]
    both = srv.sample_at_clock(paths=[CNT], clock=CLK, edge="both")["count"]
    check("both == rising + falling", both == rise + fall,
          f"{both} vs {rise}+{fall}")

    edges = clocking.clock_edges(srv.fst, CLK, 0)
    check("tool edges match the shared clock_edges helper",
          [c["time_units"] for c in
           srv.sample_at_clock(paths=[CNT], clock=CLK,
                               limit=10_000)["cycles"]] == edges, str(edges))

    check("an unknown signal is listed, not filled with nulls",
          srv.sample_at_clock(paths=[CNT, "top_tb.ghost"],
                              clock=CLK).get("unknown_paths") == ["top_tb.ghost"])
    for args, label in (
        ({"paths": [CNT]}, "missing clock"),
        ({"paths": [CNT], "clock": "nope"}, "clock not in waveform"),
        ({"paths": [CNT], "clock": CLK, "edge": "sideways"}, "bad edge"),
        ({"clock": CLK}, "missing paths"),
    ):
        check(f"{label} -> invalid_argument",
              srv.sample_at_clock(**args).get("error_type") == "invalid_argument")

    small = srv.sample_at_clock(paths=[CNT], clock=CLK, limit=2)
    check("limit downsamples the cycle table",
          small["count"] == 2 and small.get("sampled") is True, str(small))


def _stage_clock_xz() -> None:
    print("== clock edges ignore x/z transitions ==")
    tmp = tempfile.mkdtemp(prefix="wave_p3_")
    path = os.path.join(tmp, "xclk.fst")
    # clk: x -> 1 (not an edge), then 0 -> 1 twice (two real edges)
    write_fst(path, {"clk": 1, "d": 1},
              [(0, "clk", "x"), (0, "d", "0"),
               (10, "clk", "1"), (20, "clk", "0"), (25, "d", "1"),
               (30, "clk", "1"), (40, "clk", "0"), (50, "clk", "1")],
              timescale_exp=-9)
    from wave_mcp.sources.fst_source import FstSource
    src = FstSource(path)
    try:
        edges = clocking.clock_edges(src, "top.clk", 0)
        check("x->1 is not counted as a rising edge",
              10 not in edges, str(edges))
        check("clean 0->1 edges are found", edges == [30, 50], str(edges))
        rows = sorted(src._iter_values(src.signals["top.d"], 0,
                                       src.end_time, 100))
        check("sample_at reports the held value at each edge",
              clocking.sample_at(rows, edges) == ["1", "1"],
              str(clocking.sample_at(rows, edges)))
        check("sample_at returns None before a signal has any value",
              clocking.sample_at([(30, "1")], [10, 30]) == [None, "1"])
    finally:
        src.close()
        shutil.rmtree(tmp, ignore_errors=True)


def _stage_fsm() -> None:
    print("== fsm_transitions ==")
    srv = bound(SAMPLE)
    srv.query_defaults_clear()
    out = srv.fsm_transitions(path=CNT)

    check("transitions are ordered by first occurrence",
          [t["first_time_units"] for t in out["transitions"]]
          == sorted(t["first_time_units"] for t in out["transitions"]))
    pairs = [(t["from"], t["to"]) for t in out["transitions"]]
    check("the observed increment chain is reported",
          ("00000000", "00000001") in pairs
          and ("00000001", "00000010") in pairs, str(pairs))
    check("states_seen covers the transition endpoints",
          all(a in out["states_seen"] and b in out["states_seen"]
              for a, b in pairs), str(out["states_seen"]))

    branches = {b["line"]: b for b in out["branches"]}
    # counter.sv: line 11 is the reset assignment, line 13 the increment.
    # The reset is released on the very edge that clears the register, so a
    # guard sampled *at* the change time reads rst_n=1 and the reset branch
    # looks untaken. This assertion is what caught that.
    check("the reset branch is reported as taken",
          branches[11]["taken"] is True and branches[11]["count"] == 1,
          str(branches[11]))
    check("the increment branch is taken for the remaining changes",
          branches[13]["taken"] is True and branches[13]["count"] == 4,
          str(branches[13]))
    check("branch counts add up to the observed changes",
          sum(b["count"] for b in out["branches"]) == out["changes"],
          f"{[b['count'] for b in out['branches']]} vs {out['changes']}")
    check("each branch carries its source location",
          all(b.get("file") and b.get("line") for b in out["branches"]))

    check("the reply refuses to claim coverage",
          "coverage" not in out and "not coverage" in out["note"].lower(),
          str(out.get("note"))[:80])
    check("an unknown signal is refused",
          srv.fsm_transitions(path="top_tb.ghost").get("error_type")
          == "signal_not_found")


def _stage_downstream() -> None:
    print("== signal_downstream ==")
    srv = bound(SAMPLE)
    srv.query_defaults_clear()

    out = srv.signal_downstream(path=RST)
    check("one hop reaches the sub-module port",
          "top_tb.u_counter.rst_n" in (out.get("fan_out") or []), str(out))

    deep = srv.signal_downstream(path=RST, max_depth=8)
    check("transitive reaches further than one hop",
          len(deep.get("fan_out") or []) > len(out.get("fan_out") or []),
          f"{deep.get('fan_out')} vs {out.get('fan_out')}")
    check("transitive is a superset of the direct answer",
          set(out.get("fan_out") or []) <= set(deep.get("fan_out") or []))

    timed = srv.signal_downstream(path=RST, time="10ns")
    check("with time, each downstream signal reports its next change",
          all("first_change_after" in r for r in timed.get("downstream", [])),
          str(timed.get("downstream")))
    check("the reply does not claim causation",
          "correlation" in (timed.get("note") or ""), str(timed.get("note")))

    # fan_out must agree with the one-hop answer loads() already gives, which is
    # an independent code path. Strict fan_in symmetry does NOT hold at a
    # hierarchy boundary: fan_in on a sub-module input port deliberately returns
    # empty with reason="primary_input" (it has no *internal* RTL driver), so
    # asserting the mirror there would encode a wrong expectation.
    direct = set(srv.signal_downstream(path=RST).get("fan_out") or [])
    loads = set(srv.signal_loads(path=RST).get("loads") or [])
    check("one-hop fan_out agrees with signal_loads",
          direct == loads, f"{sorted(direct)} vs {sorted(loads)}")

    port = srv.signal_fanin(path="top_tb.u_counter.rst_n")
    check("a sub-module input port explains its empty fan_in",
          port.get("fan_in") == [] and port.get("reason") == "primary_input",
          str(port)[:160])

    # where an internal signal is reached, the mirror does hold
    internal = [s for s in (srv.signal_downstream(path=RST, max_depth=8)
                            .get("fan_out") or []) if s.endswith(".count")]
    if internal:
        fi = srv.signal_fanin(path=internal[0]).get("fan_in") or []
        check("an internal downstream signal's fan_in is non-empty",
              bool(fi), f"{internal[0]} -> {fi}")


def _stage_transactions() -> None:
    print("== fold_transactions ==")
    srv = bound(SAMPLE)
    srv.query_defaults_clear()
    start = _lit("1'b1", RST)

    # count reaches 2 at 30ns in this dump, so this transaction closes
    out = srv.fold_transactions(start_cond=start, end_cond=_lit("8'd2", CNT),
                                fields=[CNT])
    check("a transaction that closes is complete",
          out["complete"] == 1 and out["incomplete"] == 0, str(out))
    txn = out["transactions"][0]
    check("open time is the rising edge of the start condition",
          txn["start"] == "10ns", str(txn))
    check("close time is the rising edge of the end condition",
          txn["end"] == "30ns", str(txn))
    check("duration is end - start",
          txn["duration_units"] == 20, str(txn))
    check("fields are captured at both ends",
          txn["fields_at_start"][CNT] == "00000000"
          and txn["fields_at_end"][CNT] == "00000010", str(txn))

    # count never reaches 200 -> must be reported, not dropped, not guessed
    out = srv.fold_transactions(start_cond=start, end_cond=_lit("8'd200", CNT))
    check("an unclosed transaction is kept and flagged",
          out["incomplete"] == 1 and out["count"] == 1, str(out))
    rec = out["transactions"][0]
    check("no end time is invented for it",
          "end" not in rec and rec.get("incomplete") is True, str(rec))

    late = srv.fold_transactions(start_cond=start, end_cond=_lit("8'd2", CNT),
                                 start="20ns")
    check("a level that is already true at the window start opens nothing",
          late["complete"] == 0 and late["incomplete"] == 0, str(late)[:200])
    check("the end edge seen with nothing open is still reported",
          late.get("unmatched_ends") == 1, str(late)[:200])

    print("== fold_transactions: edges out of x and orphan ends ==")
    from wave_mcp.analysis import transactions as _tx
    from wave_mcp.sources.fst_source import FstSource
    tmp = tempfile.mkdtemp(prefix="wave_p3_tx_")
    path = os.path.join(tmp, "tx.fst")
    # req: x -> 1 at 10 (not an edge), 0 at 20, 1 at 40 (real edge)
    # ack: 1 at 15 with nothing open (orphan end), 1 at 50 closes the 40 txn
    write_fst(path, {"req": 1, "ack": 1},
              [(0, "req", "x"), (0, "ack", "0"),
               (10, "req", "1"), (15, "ack", "1"), (18, "ack", "0"),
               (20, "req", "0"), (40, "req", "1"), (50, "ack", "1")],
              timescale_exp=-9)
    src = FstSource(path)
    try:
        out = _tx.fold_transactions(src, _lit("1'b1", "top.req"),
                                    _lit("1'b1", "top.ack"))
        recs = out["transactions"]
        starts = [r.get("start") for r in recs if "start" in r]
        check("a start coming out of x opens nothing", "10ns" not in starts, str(recs))
        check("the clean 0->1 start opens a transaction", "40ns" in starts, str(recs))
        orphans = [r for r in recs if r.get("unmatched_end")]
        check("an end with nothing open is recorded, not dropped",
              len(orphans) == 1 and orphans[0]["end"] == "15ns", str(recs))
        closed = [r for r in recs if r.get("start") == "40ns"]
        check("the real transaction closes at the next end edge",
              bool(closed) and closed[0].get("end") == "50ns", str(closed))
        check("undecidable time covers the x stretch",
              out.get("undecidable_units", 0) == 10, str(out.get("undecidable_units")))
    finally:
        src.close()
        shutil.rmtree(tmp, ignore_errors=True)

    print("== fold_transactions: no protocol is assumed ==")
    import wave_mcp.analysis.transactions as _tx_mod
    src = open(_tx_mod.__file__, encoding="utf-8").read()
    lowered = src.lower()
    # A built-in protocol table would be wrong for any design that deviates
    # from the spec, and wrong silently. The caller supplies the handshake.
    check("no built-in protocol tables",
          not any(p in lowered for p in ("awvalid", "arvalid", "wstrb",
                                         "pselx", "hready", "tvalid")),
          "a protocol signal name appears in the module")

    print("== fold_transactions: bad input ==")
    check("a non-object condition is refused",
          srv.fold_transactions(start_cond="x",
                                end_cond=_lit("8'd2", CNT))
          .get("error_type") == "invalid_argument")
    check("an unknown signal is refused",
          srv.fold_transactions(start_cond={"k": "sig", "name": "top_tb.ghost"},
                                end_cond=_lit("8'd2", CNT))
          .get("error_type") == "signal_not_found")
    check("a condition with no signals is refused",
          srv.fold_transactions(start_cond={"k": "const", "lit": "1'b1"},
                                end_cond={"k": "const", "lit": "1'b1"})
          .get("error_type") == "empty_predicate")


def _stage_diff_nary() -> None:
    print("== diff_waveforms is N-ary ==")
    srv = bound(SAMPLE)
    check("fewer than two runs is refused",
          srv.diff_waveforms(fst_paths=[FST]).get("error_type")
          == "invalid_argument")
    check("an empty list is refused",
          srv.diff_waveforms(fst_paths=[]).get("error_type")
          == "invalid_argument")

    same = srv.diff_waveforms(fst_paths=[FST, FST])
    check("identical runs report no divergence",
          same["first_divergence"] is None
          and same["compared"]["diverging"] == 0, str(same.get("compared")))
    check("runs are echoed with their indices",
          [r["index"] for r in same["runs"]] == [0, 1], str(same.get("runs")))

    three = srv.diff_waveforms(fst_paths=[FST, FST, FST])
    check("three identical runs also report nothing",
          three["first_divergence"] is None
          and [r["index"] for r in three["runs"]] == [0, 1, 2])

    tmp = tempfile.mkdtemp(prefix="wave_p3_diff_")
    try:
        a = os.path.join(tmp, "a.fst")
        b = os.path.join(tmp, "b.fst")
        c = os.path.join(tmp, "c.fst")
        base = [(0, "clk", "0"), (0, "d", "0"), (10, "clk", "1"),
                (20, "clk", "0"), (30, "clk", "1"), (40, "d", "1")]
        write_fst(a, {"clk": 1, "d": 1}, base, timescale_exp=-9)
        write_fst(c, {"clk": 1, "d": 1}, base, timescale_exp=-9)
        write_fst(b, {"clk": 1, "d": 1},
                  base[:-1] + [(40, "d", "0")], timescale_exp=-9)

        two = srv.diff_waveforms(fst_paths=[a, b], signals=["top.d"])
        check("a real difference is found",
              two["first_divergence"] is not None
              and two["compared"]["diverging"] == 1, str(two.get("compared")))
        row = two["diverging_signals"][0]
        check("the two-run case keeps readable value_a/value_b",
              row.get("value_a") == "1" and row.get("value_b") == "0", str(row))
        check("the divergence time is the changed instant",
              row["time_units"] == 40, str(row))

        tri = srv.diff_waveforms(fst_paths=[a, b, c], signals=["top.d"])
        row = tri["diverging_signals"][0]
        groups = row["groups"]
        check("three runs partition into value groups",
              sorted(groups.keys()) == ["0", "1"], str(groups))
        check("the odd run out is identified by index",
              groups["0"] == [1] and groups["1"] == [0, 2], str(groups))
        check("run indices follow the argument order",
              [r["fst"] for r in tri["runs"]] == [a, b, c])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _stage_retired_diff_params() -> None:
    print("== diff's old a/b parameters are gone ==")
    try:
        srv.diff_waveforms(fst_a=FST, fst_b=FST)
        check("fst_a raises TypeError", False, "no exception")
    except TypeError as exc:
        check("fst_a raises TypeError", True)
        hint = srv.param_rename_hint(exc, "diff_waveforms")
        check("the hint points at fst_paths",
              hint and "fst_paths" in hint["use_instead"], str(hint))
        check("the hint warns the reply shape changed too",
              hint and "groups" in hint["use_instead"], str(hint))


def main() -> int:
    print("== P3 capability suite ==")
    _stage_clock()
    _stage_clock_xz()
    _stage_fsm()
    _stage_downstream()
    _stage_transactions()
    _stage_diff_nary()
    _stage_retired_diff_params()
    print(f"\n  P3 suite: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("  failed:", FAILED)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
