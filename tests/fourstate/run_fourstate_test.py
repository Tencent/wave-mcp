#!/usr/bin/env python3
"""Four-state (0/1/X/Z) functional verification for wave-mcp.

Fills the two biggest blind spots before xrun/real-code testing:
  1. All prior waves came from Verilator (2-state): trace_x, X propagation,
     X/Z in signal_values / value_at were never exercised on REAL 4-state data.
  2. All prior VCDs were Verilator-dialect: this run converts an
     Icarus Verilog VCD (different dialect) through convert_vcd_to_fst.

Also covers:
  3. Partial dump: u_hidden.u_inner exists in RTL but is NOT in the waveform
     ($dumpvars depth limit) -> tools must degrade gracefully, not crash.
  4. Negative paths: corrupted FST, nonexistent signal, out-of-range time.

Scenario timeline produced by tb_fourstate (timescale 1ns, dump in ps):
  phase 1 (0-42ns)    : din=X, regs=X, bus=Z (nothing driven)
  phase 2 (42-80ns)   : rst_n=1, din still X -> X propagates through XOR
  phase 3 (80-120ns)  : din=0x3C -> X clears through the 2-stage pipeline
  phase 4 (120-160ns) : tri-state driver A only -> bus = a_val (0xA)
  phase 5 (160-200ns) : both drivers, conflicting values -> bus = X
  phase 6 (200-240ns) : both drivers off -> bus = Z
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

RTL = os.path.join(HERE, "rtl", "fourstate_top.sv")
VCD = os.path.join(HERE, "sim", "fourstate.vcd")
SESSION_DIR = os.path.join(HERE, "session")
REPORT = os.path.join(ROOT, "tests", "reports", "fourstate", "fourstate_report.json")

results = {}


def init(tool):
    results.setdefault(tool, {"checks": 0, "passed": 0, "failures": []})


def check(tool, cond, desc, detail=None):
    init(tool)
    results[tool]["checks"] += 1
    if cond:
        results[tool]["passed"] += 1
    else:
        results[tool]["failures"].append({"check": desc, "detail": detail})
        print(f"    FAIL [{tool}] {desc}: {detail}")


def has_x(v):
    return v is not None and ("x" in str(v).lower())


def has_z(v):
    return v is not None and ("z" in str(v).lower())


def value_near(fst, path, t_ns):
    """value_at with ns->native-unit conversion."""
    exp = fst.timescale_exp          # e.g. -12 for ps
    scale = 10 ** (-9 - exp)         # ns -> native units
    return fst.value_at(path, int(t_ns * scale))


def main():
    t0 = time.time()

    # ---- 1. Icarus VCD -> FST -> session (dialect + pipeline check) -------
    print("== prepare_session from Icarus VCD ==")
    manifest_path = pipeline.prepare_session(
        SESSION_DIR, VCD, top="tb_fourstate", filelist=[RTL])["manifest"]
    s = open_session(manifest_path)
    fst, rtl = s.fst, s.rtl

    check("convert_vcd_to_fst", fst is not None, "Icarus VCD converted and opened")
    check("convert_vcd_to_fst", fst.timescale_exp == -12,
          "1ps timescale preserved", f"exp={fst.timescale_exp}")
    check("prepare_session", rtl.has_netlist, "netlist built from 4-state RTL")

    sigs = list(fst.signals.keys())
    scopes = list(fst.scopes.keys())
    check("prepare_session", len(sigs) > 0, "signals discovered", len(sigs))
    print(f"    {len(sigs)} signals, {len(scopes)} scopes")

    def find(leaf):
        m = [p for p in sigs if p.endswith(leaf)]
        return m[0] if m else None

    din = find("u_xprop.din[7:0]") or find("u_xprop.din")
    dout = find("u_xprop.dout[7:0]") or find("u_xprop.dout")
    stage = find("u_xprop.stage[7:0]") or find("u_xprop.stage")
    bus = find("u_tri.bus[3:0]") or find("u_tri.bus")
    for name, p in [("din", din), ("dout", dout), ("stage", stage), ("bus", bus)]:
        check("prepare_session", p is not None, f"signal {name} found in FST", p)
    if not all([din, dout, stage, bus]):
        print("FATAL: key signals missing, aborting")
        _write_report(t0)
        return 1

    # ---- 2. X values through value_at / values_between --------------------
    print("== 4-state values: X ==")
    v = value_near(fst, din, 20)
    check("signal_value_at", has_x(v.get("value") if v else None),
          "din is X before being driven (t=20ns)", v)
    v = value_near(fst, stage, 60)
    check("signal_value_at", has_x(v.get("value") if v else None),
          "stage register X before din driven (t=60ns)", v)
    v = value_near(fst, dout, 70)
    check("signal_value_at", has_x(v.get("value") if v else None),
          "X propagates through XOR into dout (t=70ns)", v)
    v = value_near(fst, dout, 115)
    check("signal_value_at", v and not has_x(v.get("value")),
          "X cleared after din driven (t=115ns)", v)
    if v and not has_x(v.get("value")):
        got = int(str(v["value"]), 2)
        check("signal_value_at", got == (0x3C ^ 0x5A),
              "post-X value correct: 0x3C^0x5A", hex(got))

    # ---- 3. Z and contention on the tri-state bus --------------------------
    print("== 4-state values: Z / contention ==")
    v = value_near(fst, bus, 20)
    check("signal_value_at", has_z(v.get("value") if v else None),
          "bus floats Z with no driver (t=20ns)", v)
    v = value_near(fst, bus, 140)
    if v and not (has_x(v.get("value")) or has_z(v.get("value"))):
        check("signal_value_at", int(str(v["value"]), 2) == 0xA,
              "single driver: bus = a_val (t=140ns)", v)
    else:
        check("signal_value_at", False, "single driver: bus = a_val (t=140ns)", v)
    v = value_near(fst, bus, 180)
    check("signal_value_at", has_x(v.get("value") if v else None),
          "contention -> bus X (t=180ns)", v)
    v = value_near(fst, bus, 220)
    check("signal_value_at", has_z(v.get("value") if v else None),
          "drivers off -> bus back to Z (t=220ns)", v)

    # full timeline must contain x and z states without crashing
    exp = fst.timescale_exp
    rows = fst.values_between(bus, 0, 240 * 10 ** (-9 - exp), max_values=500)
    check("signal_values_in_range", rows is not None and len(rows) >= 4,
          "bus timeline retrieved", len(rows) if rows else None)
    if rows:
        seen = "".join(str(r.get("value", "")) for r in rows).lower()
        check("signal_values_in_range", "x" in seen, "timeline contains X states")
        check("signal_values_in_range", "z" in seen, "timeline contains Z states")

    # ---- 4. trace_x on real X data -----------------------------------------
    print("== trace_x on real 4-state data ==")
    for t_ns, sig, expect_x, why in [
            (70, dout, True, "dout X at 70ns (from un-driven din)"),
            (180, bus, True, "bus X at 180ns (contention)"),
            (115, dout, False, "dout valid at 115ns (no X to trace)")]:
        try:
            r = rtl.trace_x(sig, f"{t_ns}ns")
            init("trace_x")
            if expect_x:
                ok = isinstance(r, dict) and (
                    r.get("tree") or r.get("root") or r.get("is_x")
                    or "x" in json.dumps(r).lower())
                check("trace_x", ok, why, str(r)[:200])
            else:
                txt = json.dumps(r).lower()
                ok = isinstance(r, dict) and ("no-x" in txt or "not" in txt
                                              or r.get("is_x") is False)
                check("trace_x", ok, why, str(r)[:200])
        except Exception as e:  # noqa: BLE001 - crash IS the failure signal here
            check("trace_x", False, why, f"CRASH: {e}")

    # ---- 5. active_drivers with 4-state guards ------------------------------
    print("== active_drivers under X/Z ==")
    for t_ns, why in [(20, "all-X time"), (180, "contention time")]:
        try:
            r = rtl.active_drivers(bus, f"{t_ns}ns")
            check("active_drivers", isinstance(r, dict),
                  f"no crash at {why}", str(r)[:150])
        except Exception as e:  # noqa: BLE001
            check("active_drivers", False, f"no crash at {why}", f"CRASH: {e}")
    try:
        r = rtl.drivers(bus)
        n = len(r.get("drivers", [])) if isinstance(r, dict) else 0
        check("signal_drivers", n >= 2,
              "both tri-state drivers found on bus", n)
    except Exception as e:  # noqa: BLE001
        check("signal_drivers", False, "both tri-state drivers found", str(e))

    # ---- 6. partial dump: RTL-only signal ----------------------------------
    print("== partial dump (u_inner not in waveform) ==")
    inner = [p for p in sigs if "u_inner" in p]
    check("partial_dump", len(inner) == 0,
          "u_inner correctly absent from FST", inner)
    # netlist must still know the module
    mods = rtl.engine.modules if rtl.has_netlist else {}
    check("partial_dump", "hidden_inner" in mods,
          "hidden_inner present in netlist", list(mods.keys()))
    # value query on a not-dumped signal: None/empty, never a crash
    ghost = "tb_fourstate.dut.u_hidden.u_inner.shadow[3:0]"
    try:
        v = value_near(fst, ghost, 100)
        check("partial_dump", v is None or v == {} or not v.get("value"),
              "not-dumped signal returns empty, no crash", v)
    except Exception as e:  # noqa: BLE001
        check("partial_dump", False, "not-dumped signal must not crash", str(e))
    # drivers (static) still work for the not-dumped scope
    try:
        r = rtl.drivers("tb_fourstate.dut.u_hidden.u_inner.shadow")
        ok = isinstance(r, dict)
        check("partial_dump", ok, "static drivers for not-dumped signal",
              str(r)[:150])
    except Exception as e:  # noqa: BLE001
        check("partial_dump", False, "static drivers for not-dumped signal",
              f"CRASH: {e}")

    # ---- 7. negative paths ---------------------------------------------------
    print("== negative paths ==")
    try:
        v = fst.value_at("no.such.signal[1:0]", 100)
        check("negative", v is None or v == {},
              "nonexistent signal -> None, no crash", v)
    except Exception as e:  # noqa: BLE001
        check("negative", False, "nonexistent signal must not crash", str(e))
    try:
        v = value_near(fst, bus, 10_000)  # way past end_time
        check("negative", True, "out-of-range time: no crash", v)
    except Exception as e:  # noqa: BLE001
        check("negative", False, "out-of-range time must not crash", str(e))

    corrupt = os.path.join(HERE, "sim", "corrupt.fst")
    with open(corrupt, "wb") as fh:
        fh.write(b"definitely not an fst file" * 20)
    try:
        from wave_mcp.sources.fst_source import FstSource
        FstSource(corrupt)
        check("negative", False, "corrupted FST must raise a clean error")
    except Exception as e:  # noqa: BLE001 - a typed error is the PASS here
        check("negative", "fst" in str(e).lower() or "open" in str(e).lower()
              or "read" in str(e).lower(),
              "corrupted FST raises informative error", str(e)[:120])
    finally:
        os.remove(corrupt)

    return _write_report(t0)


def _write_report(t0):
    total = sum(r["checks"] for r in results.values())
    passed = sum(r["passed"] for r in results.values())
    report = {
        "suite": "fourstate (Icarus Verilog, 4-state)",
        "elapsed_sec": round(time.time() - t0, 2),
        "total_checks": total,
        "passed": passed,
        "failed": total - passed,
        "tools": results,
    }
    with open(REPORT, "w") as fh:
        json.dump(report, fh, indent=2)
    print(f"\n{'='*60}")
    print(f"  4-STATE SUITE: {passed}/{total} passed "
          f"({total - passed} failed)  [{report['elapsed_sec']}s]")
    print(f"  report: {REPORT}")
    print(f"{'='*60}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
