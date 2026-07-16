"""End-to-end smoke test against the generated sample session.

Run:  python tests/smoke_test.py
Exercises every category that does not require a GUI / netlist.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wave_mcp.session import open_session  # noqa: E402

SESSION = os.path.join(ROOT, "examples", "sample", "session")


def show(label, obj):
    print(f"\n### {label}")
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def main():
    s = open_session(SESSION)
    show("session summary", s.summary())

    show("child instances (top, 2 levels)", s.fst.child_instances("top_tb", 2))
    show("all module names", s.fst.all_module_names())
    show("instances by module 'counter'", s.fst.instances_by_module("counter"))
    show("scope info u_counter", s.fst.scope_info("top_tb.u_counter"))

    show("signals of u_counter", s.fst.signals_of_instance("top_tb.u_counter"))
    show("signal info count", s.fst.signal_info("top_tb.u_counter.count"))
    show("declaration of count (RTL)", s.rtl.signal_declaration("count"))

    show("all values of count", s.fst.all_values("top_tb.u_counter.count"))
    show("count between 10ns-40ns",
         s.fst.values_between("top_tb.u_counter.count",
                              s.fst.start_time, 40, 100))
    exp = s.fst.timescale_exp
    from wave_mcp import timeutil
    t = timeutil.time_to_fst_units("40ns", exp)
    show("count value at 40ns", s.fst.value_at("top_tb.u_counter.count", t))

    show("errors", s.log.errors())
    show("warnings", s.log.warnings())
    show("messages containing 'mismatch'", s.log.containing("mismatch"))

    show("all files", s.rtl.all_files())
    show("modules in counter.sv", s.rtl.modules_in_file(s.rtl.files[0]))

    # categories 5/6 (pyslang netlist + FST)
    show("netlist available", s.rtl.has_netlist)
    show("drivers(count)", s.rtl.drivers("top_tb.u_counter.count"))
    show("fan_in(count)", s.rtl.fan_in("top_tb.u_counter.count"))
    show("connectivity(count)", s.rtl.connectivity("top_tb.u_counter.count"))
    show("active_drivers(count@40ns)",
         s.rtl.active_drivers("top_tb.u_counter.count", "40ns"))
    show("trace_value(count@40ns)",
         s.rtl.trace_value("top_tb.u_counter.count", "40ns"))

    s.close()
    print("\nOK: smoke test completed.")


if __name__ == "__main__":
    main()
