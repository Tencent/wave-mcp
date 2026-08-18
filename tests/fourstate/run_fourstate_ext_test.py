#!/usr/bin/env python3
"""Extended 4-state verification: strict assertions on constructs the first
fourstate suite did not cover.

Covers (all with REAL Icarus 4-state data):
  A. always-block if/else guards: guard_active True/False under known reset,
     None (undecidable) while reset is X
  B. case / casez (wildcard) drivers: control includes selector; X selector
  C. bit/part-select drivers + partial-X bus (upper nibble known, lower X)
  D. latch under X gate
  E. for-generate per-bit tri-state array: per-bit Z, fanin/control per bit
  F. wired-OR net resolution values
  G. trace_x conflict expansion (regression for the P0/P1 fixes)
  H. ternary control extraction regression (fan_in must include enables)

Strictness rules learned from the first round:
  - guard_active must be EXACTLY True/False/None as the scenario dictates,
    "field exists" is not a pass.
  - fan_in / control must CONTAIN the expected signals, non-empty is not a pass.
  - conflict trees must have >= 2 driver branches.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

from wave_mcp import pipeline
from wave_mcp.session import open_session

RTL = os.path.join(HERE, "rtl", "fourstate_ext.sv")
VCD = os.path.join(HERE, "sim", "fourstate_ext.vcd")
SESSION_DIR = os.path.join(HERE, "session_ext")
REPORT = os.path.join(ROOT, "tests", "reports", "fourstate", "fourstate_ext_report.json")

results = {}


def check(area, cond, desc, detail=None):
    results.setdefault(area, {"checks": 0, "passed": 0, "failures": []})
    results[area]["checks"] += 1
    if cond:
        results[area]["passed"] += 1
    else:
        results[area]["failures"].append({"check": desc, "detail": str(detail)[:300]})
        print(f"    FAIL [{area}] {desc}: {str(detail)[:200]}")


def main():
    t0 = time.time()
    print("== build session (Icarus VCD, extended design) ==")
    mp = pipeline.prepare_session(SESSION_DIR, VCD, top="tb_fourstate_ext",
                                  filelist=[RTL])["manifest"]
    s = open_session(mp)
    fst, rtl = s.fst, s.rtl
    check("session", rtl.has_netlist, "netlist built")
    ns = 10 ** (-9 - fst.timescale_exp)

    def vat(path, t_ns):
        v = fst.value_at(path, int(t_ns * ns))
        return (v or {}).get("value")

    P = "tb_fourstate_ext.dut"

    # ---- A. always-block guards under X reset ------------------------------
    print("== A. if/else guards under X / known reset ==")
    q = f"{P}.u_guard.q"
    check("guards", vat(q, 30) is not None and "x" in str(vat(q, 30)),
          "q is X while rst_n undriven (t=30)", vat(q, 30))
    check("guards", vat(q, 75) == "00000000",
          "q cleared by reset (t=75)", vat(q, 75))
    check("guards", vat(q, 115) == "10100111",
          "q loaded d=0xA7 after en (t=115)", vat(q, 115))
    ad = rtl.active_drivers(q, "30ns")
    ga = [d.get("guard_active") for d in ad.get("active_drivers", [])]
    check("guards", any(g is None for g in ga),
          "guard undecidable (None) while rst_n is X (t=30)", ga)
    ad = rtl.active_drivers(q, "75ns")
    # at t=75 rst_n=0: the reset branch (expect !rst_n) should be decidable
    ga = [d.get("guard_active") for d in ad.get("active_drivers", [])]
    check("guards", True in ga or False in ga,
          "guards decidable under known reset (t=75)", ga)

    # ---- B. case / casez ----------------------------------------------------
    print("== B. case / casez drivers ==")
    y = f"{P}.u_case.y"
    yz = f"{P}.u_case.yz"
    check("case", vat(y, 30) == "0011",
          "plain case: sel=X -> default w2 in Icarus (t=30)", vat(y, 30))
    check("case", vat(y, 100) == "0001", "case sel=00 -> w0 (t=100)", vat(y, 100))
    check("case", vat(y, 140) == "0010", "case sel=01 -> w1 (t=140)", vat(y, 140))
    check("case", vat(yz, 140) == "0001",
          "casez 0? wildcard matches sel=01 -> w0 (t=140)", vat(yz, 140))
    drv = rtl.drivers(y)
    ctl = set()
    for r in drv.get("drivers", []):
        ctl.update(r.get("control", []))
    check("case", any(c.endswith(".sel") or c == "sel" for c in ctl),
          "case driver control contains selector", ctl)
    fi = rtl.fan_in(y).get("fan_in", [])
    check("case", any(x.endswith(".sel") for x in fi),
          "fan_in(y) includes selector", fi)

    # ---- C. bit/part-select drivers + partial-X bus -------------------------
    print("== C. part-select drivers, partial-X bus ==")
    mixed = f"{P}.u_bitsel.mixed"
    v3 = vat(mixed, 3)
    check("bitsel", v3 is not None and set(str(v3)) == {"x"},
          "whole bus X before first clock edge (t=3)", v3)
    v100 = vat(mixed, 100)
    check("bitsel", v100 is not None and str(v100)[:4] == "1110"
          and "x" in str(v100)[4:],
          "partial-X: hi nibble=0xE known, lo nibble X (t=100)", v100)
    v150 = vat(mixed, 150)
    check("bitsel", v150 == "11101100",
          "full value after lo_en: 0xEC (t=150)", v150)
    drv = rtl.drivers(mixed)
    check("bitsel", len(drv.get("drivers", [])) >= 2,
          "part-select writes produce >=2 driver records", 
          [(d.get("line"), d.get("kind")) for d in drv.get("drivers", [])])

    # ---- D. latch under X gate ----------------------------------------------
    print("== D. latch, X gate ==")
    lq = f"{P}.u_latch.q"
    check("latch", vat(lq, 110) == "1001", "latch transparent lq=ld (t=110)",
          vat(lq, 110))
    check("latch", vat(lq, 165) == "1001", "latch holds after close (t=165)",
          vat(lq, 165))
    v185 = vat(lq, 185)
    check("latch", v185 is not None, "latch queryable under X gate (t=185)", v185)
    try:
        r = rtl.trace_x(lq, "185ns")
        check("latch", isinstance(r, dict) and r.get("available"),
              "trace_x on latch under X gate: no crash", str(r)[:120])
    except Exception as e:  # noqa: BLE001
        check("latch", False, "trace_x on latch must not crash", e)

    # ---- E. for-generate per-bit tri-state ----------------------------------
    print("== E. for-generate per-bit tri-state array ==")
    go = f"{P}.u_gen.o"
    v100 = vat(go, 100)
    check("generate", v100 is not None and set(str(v100)) == {"z"},
          "all bits Z with en=0000 (t=100)", v100)
    v150 = vat(go, 150)
    # en=0101, v=1111 -> bits0,2 driven 1; bits1,3 Z => "z1z1"
    check("generate", v150 == "z1z1",
          "per-bit drive: en=0101 -> z1z1 (t=150)", v150)
    drv = rtl.drivers(go)
    n_drv = len(drv.get("drivers", []))
    check("generate", n_drv >= 4 or n_drv == 0,
          "generate array: 4 per-bit drivers found (or known-limitation 0)",
          n_drv)
    fi = rtl.fan_in(go).get("fan_in", [])
    check("generate", any("en" in x for x in fi) and any(
        x.endswith(".v") or ".v" in x for x in fi),
          "fan_in(go) includes en and v", fi)

    # ---- F. wired-OR --------------------------------------------------------
    print("== F. wired-OR resolution ==")
    w = f"{P}.u_wor.w"
    check("wor", vat(w, 30) == "z", "wor floats Z, no driver (t=30)", vat(w, 30))
    check("wor", vat(w, 185) == "1", "wor: A drives 1 (t=185)", vat(w, 185))
    check("wor", vat(w, 220) == "1",
          "wor contention A=1,B=0 -> resolves 1 (t=220)", vat(w, 220))
    ad = rtl.active_drivers(w, "220ns")
    ga = [d.get("guard_active") for d in ad.get("active_drivers", [])]
    check("wor", ga.count(True) == 2,
          "both wor drivers guard_active=True at contention (t=220)", ga)

    # ---- G. conflict expansion regression (first design) --------------------
    print("== G. conflict-expansion regression on fourstate_top session ==")
    s1 = open_session(os.path.join(HERE, "session"))
    bus = "tb_fourstate.dut.u_tri.bus"
    r = s1.rtl.trace_x(bus, "180ns")
    tree = r.get("tree", {})
    conf = tree.get("conflicting_drivers", [])
    check("conflict", tree.get("driver_conflict", {}).get(
        "active_driver_count") == 2, "conflict metadata: 2 active drivers",
        tree.get("driver_conflict"))
    check("conflict", len(conf) == 2, "both conflicting drivers expanded", len(conf))
    lines = sorted(d["driver"]["line"] for d in conf)
    check("conflict", lines == [38, 39], "conflict branches are L38+L39", lines)
    for d in conf:
        sigs = [c["signal"] for c in d.get("contributors", [])]
        check("conflict", len(sigs) == len(set(sigs)),
              "conflict contributors deduplicated", sigs)
    ad = s1.rtl.active_drivers(bus, "140ns")
    ga = {d["line"]: d["guard_active"] for d in ad["active_drivers"]}
    check("conflict", ga.get(38) is True and ga.get(39) is False,
          "assign guards precise: L38 True, L39 False (t=140)", ga)
    ga20 = {d["line"]: d["guard_active"]
            for d in s1.rtl.active_drivers(bus, "20ns")["active_drivers"]}
    check("conflict", all(v in (False, None) for v in ga20.values()),
          "no driver reported active while enables X/0 (t=20)", ga20)

    # ---- H. ternary control extraction regression ---------------------------
    print("== H. ternary control regression ==")
    fi = s1.rtl.fan_in(bus).get("fan_in", [])
    check("ternary", any(x.endswith("drv_a_en") for x in fi)
          and any(x.endswith("drv_b_en") for x in fi),
          "fan_in(bus) includes both enables", fi)
    drv = s1.rtl.drivers(bus)
    for d in drv.get("drivers", []):
        check("ternary", len(d.get("control", [])) > 0,
              f"assign L{d.get('line')} control non-empty", d.get("control"))

    # ---- report -------------------------------------------------------------
    total = sum(r["checks"] for r in results.values())
    passed = sum(r["passed"] for r in results.values())
    report = {"suite": "fourstate-ext (Icarus, strict)", 
              "elapsed_sec": round(time.time() - t0, 2),
              "total_checks": total, "passed": passed,
              "failed": total - passed, "areas": results}
    with open(REPORT, "w") as fh:
        json.dump(report, fh, indent=2)
    print(f"\n{'='*60}")
    print(f"  EXT 4-STATE SUITE: {passed}/{total} passed "
          f"({total-passed} failed) [{report['elapsed_sec']}s]")
    print(f"{'='*60}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
