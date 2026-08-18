#!/usr/bin/env python3
"""wave-mcp unified regression entry.

One command runs every suite that can run in the current environment:

    python3 tests/run_regression.py            # run everything available
    python3 tests/run_regression.py --quick    # skip slow project regressions

Suites (auto-skipped when their prerequisites are missing):
  unit       - smoke_test + test_definition_name (examples/sample session)
  fourstate  - 4-state X/Z suites; needs iverilog (rebuilds VCDs on the fly)
  projects   - OpenTitan/XiangShan functional verify; needs /tmp/ot_build
               or /tmp/xs_build artifacts (built by tests/projects/ scripts)

Exit code: 0 if every executed suite passed, 1 otherwise.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable


def run(name, cmd, cwd=ROOT):
    print(f"\n{'='*66}\n  [{name}] {' '.join(cmd)}\n{'='*66}")
    t0 = time.time()
    p = subprocess.run(cmd, cwd=cwd)
    return {"suite": name, "ok": p.returncode == 0,
            "elapsed": round(time.time() - t0, 1)}


def main():
    ap = argparse.ArgumentParser(description="wave-mcp regression runner")
    ap.add_argument("--quick", action="store_true",
                    help="skip slow project-level regressions")
    args = ap.parse_args()

    results = []
    skipped = []

    # ---- unit tests (always runnable: sample session ships in-repo) --------
    results.append(run("unit/smoke",
                       [PY, os.path.join(HERE, "unit", "smoke_test.py")]))
    results.append(run("unit/definition_name",
                       [PY, os.path.join(HERE, "unit", "test_definition_name.py")]))

    # ---- 4-state suites (need iverilog; regenerate waves for a fresh run) --
    if shutil.which("iverilog") and shutil.which("vvp"):
        fs = os.path.join(HERE, "fourstate")
        sim = os.path.join(fs, "sim")
        os.makedirs(sim, exist_ok=True)
        for rtl, tb, vvp in [("fourstate_top.sv", "tb_fourstate.sv", "fourstate.vvp"),
                             ("fourstate_ext.sv", "tb_fourstate_ext.sv", "fourstate_ext.vvp")]:
            subprocess.run(["iverilog", "-g2005-sv", "-o", os.path.join(sim, vvp),
                            os.path.join(fs, "rtl", rtl), os.path.join(fs, "tb", tb)],
                           check=True)
            subprocess.run(["vvp", vvp], cwd=sim, check=True)
        # rebuild sessions from scratch so netlist changes are picked up
        for d in ("session", "session_ext"):
            shutil.rmtree(os.path.join(fs, d), ignore_errors=True)
        results.append(run("fourstate/base",
                           [PY, os.path.join(fs, "run_fourstate_test.py")]))
        results.append(run("fourstate/ext",
                           [PY, os.path.join(fs, "run_fourstate_ext_test.py")]))
    else:
        skipped.append(("fourstate", "iverilog/vvp not in PATH"))

    # ---- project-level functional verification -----------------------------
    if args.quick:
        skipped.append(("projects", "--quick"))
    elif os.path.isdir("/tmp/ot_build") or os.path.exists("/tmp/xs_build/xiangshan_tb.fst"):
        results.append(run("projects/functional_verify",
                           [PY, os.path.join(HERE, "functional_verify.py")]))
    else:
        skipped.append(("projects", "no /tmp/ot_build or /tmp/xs_build artifacts"))

    # ---- summary ------------------------------------------------------------
    print(f"\n{'='*66}\n  REGRESSION SUMMARY\n{'='*66}")
    ok = True
    for r in results:
        mark = "PASS" if r["ok"] else "FAIL"
        ok = ok and r["ok"]
        print(f"  [{mark}] {r['suite']:32s} {r['elapsed']:>7.1f}s")
    for name, why in skipped:
        print(f"  [SKIP] {name:32s} ({why})")
    print(f"{'='*66}")
    print(f"  overall: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
