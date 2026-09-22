#!/usr/bin/env python3
"""signal_activity unit suite: statistics correctness + cross-validation.

Every expectation is derived from an *independent* source (``values_between``
and ``value_at``), never recomputed with the activity code under test.

Run directly or via tests/run_regression.py.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)

from fstgen import write_fst                        # noqa: E402
from wave_mcp.session import open_session           # noqa: E402
from wave_mcp.sources.fst_source import FstSource    # noqa: E402
from wave_mcp.analysis import signal_activity       # noqa: E402
from wave_mcp import server as srv                  # noqa: E402
from session_client import bound             # noqa: E402

PASSED, FAILED = [], []

SAMPLE = os.path.join(HERE, "..", "..", "examples", "sample", "session")
PATHS = ["top_tb.clk", "top_tb.rst_n", "top_tb.u_counter.count",
         "top_tb.u_counter.overflow"]


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSED if cond else FAILED).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  ({detail})" if detail and not cond else ""))


def _expected_toggles(fst, path: str, start: int, end: int) -> int:
    """Independent expectation: the value held at ``start``, then every change
    in ``(start, end]``; count adjacent pairs whose value differs."""
    base = fst.value_at(path, start)["value"]
    rows = fst.values_between(path, start, end, 1 << 20) or []
    seq = [base] + [r["value"] for r in rows if r["time_units"] > start]
    return sum(1 for a, b in zip(seq, seq[1:]) if a != b)


def _expected_x_units(fst, path: str, start: int, end: int) -> int:
    """Independent time-weighted count of the units spent holding an x."""
    base = fst.value_at(path, start)["value"]
    rows = [r for r in (fst.values_between(path, start, end, 1 << 20) or [])
            if r["time_units"] > start]
    prev_t, cur, x = start, base, 0
    for r in rows:
        if r["time_units"] > prev_t and "x" in (cur or "").lower():
            x += r["time_units"] - prev_t
        prev_t = r["time_units"]
        cur = r["value"]
    if end > prev_t and "x" in (cur or "").lower():
        x += end - prev_t
    return x


def _stage_full_window() -> None:
    print("== full window: statistics vs independent recomputation ==")
    f = open_session(SAMPLE).fst
    start, end = f.start_time, f.end_time
    rows = signal_activity(f, PATHS, start, end)
    by = {r["path"]: r for r in rows}

    check("order preserved", [r["path"] for r in rows] == PATHS)
    for p in PATHS:
        r = by[p]
        check(f"{p}: toggles == values_between transitions",
              r["toggles"] == _expected_toggles(f, p, start, end),
              f"{r['toggles']} vs {_expected_toggles(f, p, start, end)}")
        check(f"{p}: value_first == value_at(start)",
              r["value_first"] == f.value_at(p, start)["value"],
              f"{r['value_first']} vs {f.value_at(p, start)['value']}")
        check(f"{p}: value_last == value_at(end)",
              r["value_last"] == f.value_at(p, end)["value"],
              f"{r['value_last']} vs {f.value_at(p, end)['value']}")
        xu = _expected_x_units(f, p, start, end)
        check(f"{p}: x_ratio == independent time-weighted x share",
              abs(r["x_ratio"] - xu / (end - start)) < 1e-9,
              f"{r['x_ratio']} vs {xu / (end - start)}")

    check("clk: 10 toggles", by["top_tb.clk"]["toggles"] == 10)
    check("clk: first change 5ns", by["top_tb.clk"]["first_change"] == "5ns")
    check("rst_n: single toggle at 10ns",
          by["top_tb.rst_n"]["toggles"] == 1
          and by["top_tb.rst_n"]["first_change"] == "10ns"
          and by["top_tb.rst_n"]["last_change"] == "10ns")
    check("count: x_ratio 0.2 (x held 0..10 of 0..50)",
          abs(by["top_tb.u_counter.count"]["x_ratio"] - 0.2) < 1e-9)
    check("overflow: x_ratio 0.2 and 2 toggles",
          abs(by["top_tb.u_counter.overflow"]["x_ratio"] - 0.2) < 1e-9
          and by["top_tb.u_counter.overflow"]["toggles"] == 2)
    check("z_ratio zero everywhere (dump has no z)",
          all(r["z_ratio"] == 0.0 for r in rows))


def _stage_narrow_windows() -> None:
    print("== narrowed windows ==")
    f = open_session(SAMPLE).fst

    # a window with no change at all: constant, and no first/last change
    rows = signal_activity(f, ["top_tb.rst_n"], 11, 19)
    r = rows[0]
    check("quiet window: is_constant, toggles 0", r["is_constant"]
          and r["toggles"] == 0, str(r))
    check("quiet window: first/last change are null (not 0)",
          r["first_change"] is None and r["last_change"] is None, str(r))
    check("quiet window: values held at both ends",
          r["value_first"] == "1" and r["value_last"] == "1", str(r))

    # a window that starts mid-way through a held value
    rows = signal_activity(f, ["top_tb.clk"], 11, 31)
    check("mid-window start: 3 toggles (15/20/25/30 minus the held initial)",
          rows[0]["toggles"] == _expected_toggles(f, "top_tb.clk", 11, 31),
          str(rows[0]))
    check("mid-window start: first change is 15ns",
          rows[0]["first_change"] == "15ns", str(rows[0]))


def _stage_batch_and_errors() -> None:
    print("== batch behaviour ==")
    f = open_session(SAMPLE).fst
    rows = signal_activity(f, ["top_tb.ghost", "top_tb.clk"], 0, 50)
    check("bad path returns an error row, good path still answered",
          "error" in rows[0] and "hint" in rows[0]
          and rows[1]["toggles"] == 10, str(rows))

    rows = signal_activity(f, "top_tb.clk", 0, 50)
    check("a bare string path is accepted", len(rows) == 1
          and rows[0]["toggles"] == 10)


def _stage_aggregated(tmp: str) -> None:
    print("== aggregated split bus ==")
    sigs = {"bus[0]": 1, "bus[1]": 1}
    path = os.path.join(tmp, "split.fst")
    write_fst(path, sigs, [
        (0, "bus[1]", "0"), (0, "bus[0]", "0"),
        (100, "bus[0]", "1"),          # 00 -> 01
        (200, "bus[1]", "1"),          # 01 -> 11
        (300, "bus[0]", "0"),          # 11 -> 10
    ], timescale_exp=-12)
    f = FstSource(path)

    rows = signal_activity(f, ["top.bus"], 0, 300)
    r = rows[0]
    check("aggregated path resolves (not an error row)", "error" not in r,
          str(r))
    check("aggregated toggles == 3 (00/01/11/10)",
          r["toggles"] == 3, str(r))
    check("aggregated value_first matches value_at(start)",
          r["value_first"] == f.value_at("top.bus", 0)["value"],
          f"{r['value_first']} vs {f.value_at('top.bus', 0)['value']}")
    check("aggregated value_last matches value_at(end)",
          r["value_last"] == f.value_at("top.bus", 300)["value"],
          f"{r['value_last']} vs {f.value_at('top.bus', 300)['value']}")
    check("aggregated msb-first ordering (10 at the end)",
          r["value_last"] == "10", str(r))

    # the ranged spelling of the same bus must resolve to the same thing
    r2 = signal_activity(f, ["top.bus[1:0]"], 0, 300)[0]
    check("ranged spelling matches the plain one",
          r2.get("toggles") == 3 and r2["value_last"] == "10", str(r2))

    # mid-window on the aggregate: 01 is held from 100 to 200
    r3 = signal_activity(f, ["top.bus"], 150, 250)[0]
    check("aggregate mid-window: value_first 01, value_last 11",
          r3["value_first"] == "01" and r3["value_last"] == "11", str(r3))


def _stage_tool_layer(tmp: str) -> None:
    print("== MCP tool layer ==")
    srv = bound(SAMPLE)
    out = srv.signal_activity(["top_tb.clk"], "min", "max")
    check("tool: window echoed", out["window"]["end"] == "50ns", str(out))
    check("tool: count and rows agree", out["count"] == 1
          and len(out["signals"]) == 1)
    check("tool: fingerprint attached", "_fp" in out, str(out.keys()))

    bad = srv.signal_activity(["top_tb.clk"], "not-a-time", "max")
    check("tool: bad start -> structured invalid_argument",
          bad.get("error_type") == "invalid_argument"
          and bad.get("parameter") == "start", str(bad))

    bad = srv.signal_activity(["top_tb.clk"], "20ns", "10ns")
    check("tool: reversed window -> structured invalid_argument",
          bad.get("error_type") == "invalid_argument"
          and bad.get("parameter") == "end", str(bad))

    # a netlist-only session has no waveform: degrade, never crash
    sdir = os.path.join(tmp, "static_session")
    os.makedirs(sdir, exist_ok=True)
    with open(os.path.join(sdir, "session.json"), "w") as fh:
        json.dump({"top": "top_tb",
                   "maps_path": os.path.join(SAMPLE, "netlist", "maps.json")},
                  fh)
    sid = srv.new_session(sdir)["session_id"]
    deg = srv.signal_activity(["top_tb.clk"], "min", "max", session_id=sid)
    check("tool: static session degrades gracefully",
          deg.get("available") is False and "hint" in deg, str(deg))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="wave_activity_test_")
    print(f"== signal_activity unit suite (workdir {tmp}) ==")
    _stage_full_window()
    _stage_narrow_windows()
    _stage_batch_and_errors()
    _stage_aggregated(tmp)
    _stage_tool_layer(tmp)
    print(f"\n  activity suite: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("  failed:", FAILED)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
