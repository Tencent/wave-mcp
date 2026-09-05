#!/usr/bin/env python3
"""wave-mcp unified regression entry.

One command runs every suite that can run in the current environment:

    python3 tests/run_regression.py            # run everything available
    python3 tests/run_regression.py --quick    # skip slow project regressions

Suites (auto-skipped when their prerequisites are missing):
  unit       - smoke_test + test_definition_name (examples/sample session)
  fourstate  - 4-state X/Z suites; needs iverilog (rebuilds VCDs on the fly)
  projects   - project-level functional verification over prebuilt assets
               (auto-skipped when assets are not configured)

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


# Project-level verification needs prebuilt assets, located via the
# WAVE_MCP_PROJECT_ASSETS env var (colon-separated) or an optional
# tests/project_assets.txt file. Without either, the suite is skipped.
ASSETS_ENV = "WAVE_MCP_PROJECT_ASSETS"
ASSETS_FILE = os.path.join(HERE, "project_assets.txt")


def project_assets_ready():
    raw = os.environ.get(ASSETS_ENV, "")
    if not raw and os.path.exists(ASSETS_FILE):
        with open(ASSETS_FILE, encoding="utf-8") as f:
            raw = f.read()
    assets = [p.strip() for p in raw.replace("\n", os.pathsep).split(os.pathsep)
              if p.strip()]
    return any(os.path.exists(p) for p in assets)


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
    results.append(run("unit/dut_root",
                       [PY, os.path.join(HERE, "unit", "test_dut_root.py")]))
    results.append(run("unit/diff",
                       [PY, os.path.join(HERE, "unit", "test_diff.py")]))
    results.append(run("unit/viewer",
                       [PY, os.path.join(HERE, "unit", "test_viewer.py")]))

    # ---- viewer browser e2e (self-skips without assets/playwright) ---------
    results.append(run("viewer/e2e",
                       [PY, os.path.join(HERE, "viewer_e2e.py")]))

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
    harness = os.path.join(HERE, "functional_verify.py")
    if args.quick:
        skipped.append(("projects", "--quick"))
    elif not os.path.exists(harness):
        skipped.append(("projects", "verification harness not present"))
    elif project_assets_ready():
        results.append(run("projects/functional_verify", [PY, harness]))
    else:
        skipped.append(("projects", "project assets not configured"))

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
