#!/usr/bin/env python3
"""diff_waveforms unit suite: correctness + edge cases + robustness.

Self-contained: generates all FST inputs with pylibfst (no simulator).
Run directly or via tests/run_regression.py.
"""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)

from fstgen import write_fst, clocked_pair          # noqa: E402
from wave_mcp.diff import diff_waveforms            # noqa: E402
from wave_mcp import server as srv                  # noqa: E402

PASSED, FAILED = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSED if cond else FAILED).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  ({detail})" if detail and not cond else ""))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="wave_diff_test_")
    print(f"== diff_waveforms unit suite (workdir {tmp}) ==")

    # ---------------- correctness on a classic pass/fail pair ------------
    a, b = clocked_pair(tmp, tmp, n_cycles=100, period=2000, diverge_cycle=42)
    expect_t = 42 * 2000 + 1000                     # 85000 ps

    r = diff_waveforms(a, b)
    check("basic: status ok", r["status"] == "ok", str(r))
    check("basic: finds divergence", r["first_divergence"] is not None)
    check("basic: divergence time exact",
          r["first_divergence"]["time_units"] == expect_t,
          str(r["first_divergence"]))
    firsts = [d["path"] for d in r["diverging_signals"]
              if d["time_units"] == expect_t]
    check("basic: err+cnt both at first point",
          set(firsts) == {"top.err", "top.cnt"}, str(firsts))
    check("basic: clk identical",
          all(d["path"] != "top.clk" for d in r["diverging_signals"]))
    check("basic: coverage complete", r["coverage"] == "complete")

    # identity: file vs itself
    r = diff_waveforms(a, a)
    check("identity: no divergence", r["first_divergence"] is None
          and r["compared"]["diverging"] == 0, str(r["compared"]))

    # symmetry: swapped args, same divergence time, values swapped
    r1 = diff_waveforms(a, b)
    r2 = diff_waveforms(b, a)
    check("symmetry: same first time",
          r1["first_divergence"]["time_units"]
          == r2["first_divergence"]["time_units"])
    d1 = {d["path"]: d for d in r1["diverging_signals"]}
    d2 = {d["path"]: d for d in r2["diverging_signals"]}
    check("symmetry: values swap",
          all(d1[p]["value_a"] == d2[p]["value_b"]
              and d1[p]["value_b"] == d2[p]["value_a"] for p in d1))

    # ---------------- parameter matrix -----------------------------------
    r = diff_waveforms(a, b, clock="top.clk")
    check("clock: still exact",
          r["sampling"] == "clock-aligned"
          and r["first_divergence"]["time_units"] == expect_t, str(r))

    r = diff_waveforms(a, b, signals=["top.cnt"])
    check("signals filter: only cnt",
          r["compared"]["signals"] == 1
          and r["diverging_signals"][0]["path"] == "top.cnt")

    r = diff_waveforms(a, b, scope="top")
    check("scope filter: 3 signals under top", r["compared"]["signals"] == 3,
          str(r["compared"]))

    r = diff_waveforms(a, b, scope="top.nonexistent")
    check("scope miss: error not crash", r["status"] == "error", str(r))

    r = diff_waveforms(a, b, after="90000ps")
    check("after skips first divergence",
          r["first_divergence"]["time_units"] > 90000,
          str(r["first_divergence"]))

    r = diff_waveforms(a, b, after="1ms")
    check("after beyond end: clean result", r["first_divergence"] is None
          or r["status"] == "ok")

    r = diff_waveforms(a, b, clock="top.nosuchclk")
    check("bad clock: error not crash", r["status"] == "error")

    r = diff_waveforms(a, b, signals=["top.ghost"])
    check("all signals missing: error with examples",
          r["status"] == "error" and "missing_examples" in r, str(r))

    r = diff_waveforms(a, b, signals=["top.cnt", "top.ghost"])
    check("partial missing: compares the rest, reports missing",
          r["status"] == "ok" and r["compared"]["missing_in_one"] == 1)

    # ---------------- edge cases ------------------------------------------
    # missing file
    r_err = None
    try:
        r_err = diff_waveforms("/nonexistent/x.fst", b)
        crashed = False
    except Exception as e:                          # noqa: BLE001
        crashed = True
        r_err = str(e)
    check("missing file: raises clean error (tool layer catches)",
          crashed or (isinstance(r_err, dict) and r_err.get("status") == "error"),
          str(r_err))

    # X/Z values diverging
    xa = write_fst(f"{tmp}/x_a.fst", {"sig": 4},
                   [(0, "sig", "0000"), (5000, "sig", "1010")])
    xb = write_fst(f"{tmp}/x_b.fst", {"sig": 4},
                   [(0, "sig", "0000"), (5000, "sig", "1xz0")])
    r = diff_waveforms(xa, xb)
    check("x/z divergence detected",
          r["first_divergence"] is not None
          and r["first_divergence"]["time_units"] == 5000
          and "x" in r["diverging_signals"][0]["value_b"], str(r))

    # divergence at t=0 (initial values differ)
    za = write_fst(f"{tmp}/z_a.fst", {"s": 1}, [(0, "s", "0")])
    zb = write_fst(f"{tmp}/z_b.fst", {"s": 1}, [(0, "s", "1")])
    r = diff_waveforms(za, zb)
    check("t=0 divergence", r["first_divergence"] is not None
          and r["first_divergence"]["time_units"] == 0, str(r))

    # one waveform longer: change after the other's end is a real diff
    la = write_fst(f"{tmp}/l_a.fst", {"s": 1},
                   [(0, "s", "0"), (9000, "s", "0")])
    lb = write_fst(f"{tmp}/l_b.fst", {"s": 1},
                   [(0, "s", "0"), (9000, "s", "0"), (20000, "s", "1")])
    r = diff_waveforms(la, lb)
    check("tail-only change detected", r["first_divergence"] is not None
          and r["first_divergence"]["time_units"] == 20000, str(r))

    # value revisits: A toggles 0->1->0, B stays 0; event lists differ but
    # sampled values converge again — first diff must be the 1 pulse
    pa = write_fst(f"{tmp}/p_a.fst", {"s": 1},
                   [(0, "s", "0"), (3000, "s", "1"), (4000, "s", "0")])
    pb = write_fst(f"{tmp}/p_b.fst", {"s": 1}, [(0, "s", "0")])
    r = diff_waveforms(pa, pb)
    check("pulse divergence at pulse start",
          r["first_divergence"] is not None
          and r["first_divergence"]["time_units"] == 3000, str(r))

    # glitch filtering: pulse between clock edges disappears when sampled
    ga = write_fst(f"{tmp}/g_a.fst", {"clk": 1, "s": 1},
                   [(0, "clk", "0"), (0, "s", "0"),
                    (1000, "clk", "1"), (2000, "clk", "0"),
                    (2300, "s", "1"), (2700, "s", "0"),   # glitch mid-cycle
                    (3000, "clk", "1"), (4000, "clk", "0")])
    gb = write_fst(f"{tmp}/g_b.fst", {"clk": 1, "s": 1},
                   [(0, "clk", "0"), (0, "s", "0"),
                    (1000, "clk", "1"), (2000, "clk", "0"),
                    (3000, "clk", "1"), (4000, "clk", "0")])
    r_ev = diff_waveforms(ga, gb, signals=["top.s"])
    r_ck = diff_waveforms(ga, gb, signals=["top.s"], clock="top.clk")
    check("glitch: seen event-based", r_ev["first_divergence"] is not None)
    check("glitch: filtered clock-aligned", r_ck["first_divergence"] is None,
          str(r_ck["first_divergence"]))

    # wide bus + hex-ish values
    wa = write_fst(f"{tmp}/w_a.fst", {"bus": 64},
                   [(0, "bus", "0" * 64), (7000, "bus", "1" * 64)])
    wb = write_fst(f"{tmp}/w_b.fst", {"bus": 64},
                   [(0, "bus", "0" * 64), (7000, "bus", "1" * 63 + "0")])
    r = diff_waveforms(wa, wb)
    check("64-bit bus lsb diff", r["first_divergence"] is not None
          and r["first_divergence"]["time_units"] == 7000)

    # many diverging signals: report capped at 50, count preserved
    sigs = {f"s{i}": 1 for i in range(80)}
    ca = write_fst(f"{tmp}/c_a.fst", sigs,
                   [(0, f"s{i}", "0") for i in range(80)])
    cb = write_fst(f"{tmp}/c_b.fst", sigs,
                   [(0, f"s{i}", "0") for i in range(80)]
                   + [(1000 + i * 10, f"s{i}", "1") for i in range(80)])
    r = diff_waveforms(ca, cb)
    check("cap: 50 reported", len(r["diverging_signals"]) == 50,
          str(len(r["diverging_signals"])))
    check("cap: total count kept",
          r["compared"]["diverging"] == 80
          and r.get("diverging_truncated") == 30, str(r["compared"]))
    check("cap: earliest first",
          r["diverging_signals"][0]["path"] == "top.s0")

    # ---------------- MCP tool wrapper ------------------------------------
    tool = srv.diff_waveforms
    r = tool.fn(a, b) if hasattr(tool, "fn") else None
    if r is None:                                   # plain function fallback
        r = srv.diff_waveforms(a, b)
    check("mcp wrapper: ok path", r["status"] == "ok")
    r = (tool.fn if hasattr(tool, "fn") else srv.diff_waveforms)(
        "/nonexistent/a.fst", "/nonexistent/b.fst")
    check("mcp wrapper: missing files -> error dict, no exception",
          r.get("status") == "error", str(r))

    # ---------------- summary ---------------------------------------------
    print(f"\n  diff suite: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("  failed:", FAILED)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
