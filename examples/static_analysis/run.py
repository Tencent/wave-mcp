"""Static-analysis example: analyze a UART design with NO waveform and NO simulator.

Demonstrates wave-mcp's unique waveform-free static analysis
(`open_static_session`): explore hierarchy, signals, drivers, fan-in and
declarations from RTL source code alone — usable before any simulation exists.

Run:
    python examples/static_analysis/run.py
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from wave_mcp.server import (
    open_static_session,
    list_child_instances,
    list_modules,
    list_signals,
    signal_drivers,
    signal_fanin,
    signal_connectivity,
    signal_info,
    scope_info,
    modules_in_file,
    signal_values,
)

SV = os.path.join(HERE, "uart_top.sv")
OUT = os.path.join(HERE, "session")


def section(title: str) -> None:
    print("\n" + "=" * 62)
    print(" " + title)
    print("=" * 62)


def show(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def main() -> None:
    # 1) Open a static session: RTL only, no waveform, no simulator.
    print("opening static session (no waveform, no simulator) ...")
    r = open_static_session(out_dir=OUT, top="uart_top", filelist=[SV])
    if r.get("status") != "ready":
        print("ERROR:", json.dumps(r, indent=2))
        raise SystemExit(1)
    print("session ready:", r["session_id"], "| mode:", r.get("mode", "static"))
    print("netlist health:", r.get("netlist_health", {}).get("status"))

    # 2) Hierarchy (2 levels: u_tx and u_tx.u_baud_gen).
    section("hierarchy — list_child_instances")
    show(list_child_instances(instance_full_path="uart_top", number_of_levels=2))

    # 3) Modules in the design.
    section("modules — list_modules")
    show(list_modules())

    # 4) Signals of the TX core.
    section("signals of u_tx — list_signals")
    show(list_signals(instance_full_path="uart_top.u_tx"))

    # 5) All drivers of tx_serial (RHS of every assignment that can write it).
    section("drivers of u_tx.tx_serial — signal_drivers")
    show(signal_drivers("uart_top.u_tx.tx_serial"))

    # 6) Fan-in of the state register.
    section("fan-in of u_tx.state — signal_fanin")
    show(signal_fanin("uart_top.u_tx.state"))

    # 7) Connectivity of the baud tick.
    section("connectivity of u_tx.u_baud_gen.tick — signal_connectivity")
    show(signal_connectivity("uart_top.u_tx.u_baud_gen.tick"))

    # 8) Declaration location of a signal.
    section("declaration of u_tx.bit_cnt — signal_info")
    show(signal_info("uart_top.u_tx.bit_cnt"))

    # 9) Scope metadata.
    section("scope u_tx.u_baud_gen — scope_info")
    show(scope_info("uart_top.u_tx.u_baud_gen"))

    # 10) Modules declared in the file.
    section("modules in uart_top.sv — modules_in_file")
    show(modules_in_file(SV))

    # 11) Value tools correctly refuse without a waveform.
    section("signal_values without a waveform (expect 'needs waveform')")
    show(signal_values("uart_top.u_tx.tx_serial"))

    print("\ndone. session dir:", OUT)


if __name__ == "__main__":
    main()
