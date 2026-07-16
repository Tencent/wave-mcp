#!/usr/bin/env python3
"""Verilator quickstart for wave-mcp — the open-box, anyone-can-run demo.

End to end, with NO commercial simulator (xrun) and NO vcd2fst:

    1. compile a tiny counter design with Verilator (`--binary --trace-fst`)
    2. run the binary -> it dumps a real `counter.fst` waveform
    3. open a wave-mcp session straight on that .fst (prepare_session)
    4. run a few structural / value queries and print the results

Run:
    python examples/verilator_quickstart/run.py

Requires:
    * verilator >= 5.006 (for `--binary`; any 5.x works)
    * wave-mcp installed (this repo)

Verilator writes FST directly, so `prepare_session` reads it with zero
conversion — the same entry point you'd use on an xrun-produced .fst/.vcd.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

BUILD = os.path.join(HERE, "build")
OBJ_DIR = os.path.join(BUILD, "obj_dir")
BIN = os.path.join(OBJ_DIR, "Vcounter")
FST = os.path.join(BUILD, "counter.fst")

COUNTER = os.path.join(HERE, "counter.sv")
TB = os.path.join(HERE, "tb_counter.sv")


def _run(cmd, cwd=None):
    print("+ " + " ".join(cmd))
    subprocess.run(cmd, cwd=cwd, check=True)


def build_fst() -> str:
    """Compile + simulate with Verilator, returning the FST path."""
    if shutil.which("verilator") is None:
        sys.exit("verilator not found on PATH. Install Verilator >= 5.006 and retry.")
    os.makedirs(BUILD, exist_ok=True)

    # --binary = --main --exe --build ; --trace-fst enables $dumpfile/$dumpvars -> FST
    _run([
        "verilator", "--binary", "--trace-fst", "-j", "0",
        "-Wno-fatal",
        "-Mdir", OBJ_DIR, "-o", "Vcounter",
        "--top-module", "top_tb",
        COUNTER, TB,
    ])
    # the binary's $dumpfile path is relative to its CWD -> run inside build/
    _run([BIN], cwd=BUILD)
    if not os.path.exists(FST):
        sys.exit(f"expected FST not produced: {FST}")
    print(f"[ok] real FST dumped by Verilator: {FST}")
    return FST


def show(label, obj):
    print(f"\n### {label}")
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def main():
    fst = build_fst()

    from wave_mcp import pipeline
    from wave_mcp.session import open_session

    # open the waveform via the standard entry point. filelist enables the
    # file/declaration tools and the pyslang netlist (categories 5/6).
    result = pipeline.prepare_session(
        os.path.join(BUILD, "session"), fst,
        top="top_tb", filelist=[COUNTER, TB])
    show("prepare_session steps", result["steps"])

    s = open_session(result["session_path"])
    show("session summary", s.summary())
    show("child instances (top_tb, 2 levels)", s.fst.child_instances("top_tb", 2))
    show("signals of u_counter", s.fst.signals_of_instance("top_tb.u_counter"))
    show("all values of count", s.fst.all_values("top_tb.u_counter.count"))
    s.close()

    print("\nOK: Verilator quickstart completed — session ready on a real FST.")


if __name__ == "__main__":
    main()
