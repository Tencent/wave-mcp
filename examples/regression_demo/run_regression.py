#!/usr/bin/env python3
"""Stage 1 of the demo: run a small regression and record what happened.

This is the part a real project already has. It compiles the DUT once per
seed, runs it, and writes a machine-readable manifest of the outcome. It
does no analysis at all: it only reports pass / fail / error, the log and
the waveform path, exactly like a dvsim / Makefile regression would.

The pass/fail split is a real simulation outcome. The bug in the RTL is
payload-dependent and the payload comes from the seed, so which seeds fail
is decided by the simulator, not by this script.

Usage:
    ./run_regression.py                # default: seeds 1..8
    ./run_regression.py --seeds 1 2 3
    ./run_regression.py --keep-vcd     # keep the intermediate VCD
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RTL = HERE / "rtl" / "crc_regress_top.sv"
RUNS = HERE / "runs"
MANIFEST = RUNS / "regression.json"

TOP = "crc_regress_tb"


def need(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        sys.exit(f"error: {tool} not found on PATH (needed to build the demo)")
    return path


def run_one(seed: int, keep_vcd: bool) -> dict:
    """Compile + simulate one seed. Returns the case record."""
    case_dir = RUNS / f"seed_{seed:03d}"
    case_dir.mkdir(parents=True, exist_ok=True)
    vvp = case_dir / "sim.vvp"
    log = case_dir / "sim.log"
    vcd = case_dir / "dump.vcd"
    fst = case_dir / "wave.fst"

    rec: dict = {"case_id": f"crc_pkt_seed{seed}", "seed": seed,
                 "log": str(log.relative_to(HERE)), "waveform": None,
                 "status": "error", "reason": None,
                 "elapsed_sec": None}

    t0 = time.time()
    comp = subprocess.run(
        ["iverilog", "-g2012", f"-DSEED={seed}", "-o", str(vvp), str(RTL)],
        capture_output=True, text=True)
    if comp.returncode != 0:
        rec["reason"] = "compile failed"
        log.write_text(comp.stderr or comp.stdout)
        rec["elapsed_sec"] = round(time.time() - t0, 3)
        return rec

    sim = subprocess.run(["vvp", str(vvp)], capture_output=True, text=True,
                         cwd=case_dir)
    out = (sim.stdout or "") + (sim.stderr or "")
    log.write_text(out)
    rec["elapsed_sec"] = round(time.time() - t0, 3)

    if sim.returncode != 0:
        rec["reason"] = f"simulator exited {sim.returncode}"
        return rec

    # convert the dump so the triage stage has a waveform to read.
    # NOTE: passing the VCD straight to wave-mcp would also work now, but
    # a real regression usually converts once at collection time.
    if vcd.exists():
        conv = subprocess.run(["vcd2fst", str(vcd), str(fst)],
                              capture_output=True, text=True)
        if conv.returncode == 0 and fst.exists():
            rec["waveform"] = str(fst.relative_to(HERE))
            if not keep_vcd:
                vcd.unlink()
        else:
            rec["reason"] = "vcd2fst failed"
    else:
        rec["reason"] = "no dump produced"

    # outcome comes from the testbench's own self-check
    if "TEST PASSED" in out:
        rec["status"] = "pass"
        rec["reason"] = None
    elif "TEST FAILED" in out:
        rec["status"] = "fail"
        m = re.search(r"FAIL seed=\d+ residue=(\w+) expected=(\w+)", out)
        if m:
            rec["reason"] = f"residue {m.group(1)} != expected {m.group(2)}"
            rec["observed_residue"] = m.group(1)
            rec["expected_residue"] = m.group(2)
        else:
            rec["reason"] = "self-check failed"
    else:
        rec["reason"] = rec["reason"] or "no verdict in log"

    vvp.unlink(missing_ok=True)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description="run the demo regression")
    ap.add_argument("--seeds", nargs="*", type=int,
                    default=list(range(1, 9)),
                    help="seeds to run (default 1..8)")
    ap.add_argument("--keep-vcd", action="store_true",
                    help="keep intermediate VCD files")
    args = ap.parse_args()

    need("iverilog")
    need("vvp")
    need("vcd2fst")

    RUNS.mkdir(parents=True, exist_ok=True)
    print(f"== running {len(args.seeds)} cases ==")
    cases = []
    for seed in args.seeds:
        rec = run_one(seed, args.keep_vcd)
        cases.append(rec)
        mark = {"pass": "PASS", "fail": "FAIL", "error": "ERR "}[rec["status"]]
        detail = f"  {rec['reason']}" if rec["reason"] else ""
        print(f"  [{mark}] {rec['case_id']:<22} {rec['elapsed_sec']:>6}s{detail}")

    summary = {
        "total": len(cases),
        "pass": sum(1 for c in cases if c["status"] == "pass"),
        "fail": sum(1 for c in cases if c["status"] == "fail"),
        "error": sum(1 for c in cases if c["status"] == "error"),
    }
    MANIFEST.write_text(json.dumps(
        {"suite": "crc_pkt_regression", "top": TOP,
         "rtl": str(RTL.relative_to(HERE)),
         "filelist": "rtl/crc_regress.f",
         "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
         "summary": summary, "cases": cases}, indent=2))

    print(f"\n  {summary['pass']} passed, {summary['fail']} failed, "
          f"{summary['error']} error")
    print(f"  manifest: {MANIFEST.relative_to(HERE)}")
    if summary["fail"] == 0:
        print("\n  note: no failures to triage; the demo needs at least one "
              "failing and one passing case.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
