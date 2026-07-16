#!/usr/bin/env python3
"""socket_1n 跨模块字段级 trace 验证 (真实 FST)。"""
import sys, json
sys.path.insert(0, "/data/home/wukongxin/workspace/wave_mcp")
from wave_mcp.netlist import slang_netlist
from wave_mcp.netlist.trace_engine import TraceEngine
from wave_mcp.sources.fst_source import FstSource

OT = "/data/home/wukongxin/opentitan"
PRIM = f"{OT}/hw/ip/prim/rtl"; TLUL = f"{OT}/hw/ip/tlul/rtl"; TOP = f"{OT}/hw/top_earlgrey/rtl"
FILES = [f"{TOP}/top_pkg.sv", f"{PRIM}/prim_secded_pkg.sv", f"{PRIM}/prim_mubi_pkg.sv",
         f"{PRIM}/prim_count_pkg.sv", f"{TLUL}/tlul_pkg.sv", f"{PRIM}/prim_util_pkg.sv",
         f"{PRIM}/prim_fifo_sync_cnt.sv", f"{PRIM}/prim_fifo_sync.sv",
         f"{TLUL}/tlul_fifo_sync.sv", f"{TLUL}/tlul_socket_1n.sv"]
FST = "/tmp/wave_verify/socket_1n.fst"


def walk(node, depth=0):
    cross = " [CROSS]" if node.get("crosses_into") else ""
    val = node.get("value")
    vs = f" = {val}" if val not in (None, "") else ""
    print("   " + "  " * depth + node["signal"].replace("TOP.tlul_socket_1n.", "") + vs + cross)
    for c in node.get("contributors", []):
        walk(c, depth + 1)


def main():
    maps = slang_netlist.build_netlist(FILES, top="tlul_socket_1n", incdirs=[PRIM, TLUL])
    fs = FstSource(FST)
    te = TraceEngine(maps, fs)
    print("modules extracted:", len(maps["modules"]))
    print("instance_tree depth sample:")
    for k, v in list(maps["instance_tree"].items())[:10]:
        print("   ", k, "->", v)
    print()

    # field-level driver query on a struct port
    sig = "TOP.tlul_socket_1n.tl_h_o"
    print(f"===== active_drivers (struct root): {sig} =====")
    r = te.active_drivers(sig, "100ns")
    print("module:", r.get("module"), "#drivers:", len(r.get("active_drivers", [])))
    for d in r.get("active_drivers", [])[:6]:
        dbi = d.get("driven_by_instance_port", {})
        print(f"   kind={d['kind']} port_ref={dbi.get('instance')}.{dbi.get('port')}")
    print()

    print(f"===== trace_value: {sig} (cross-module, field-level) =====")
    r = te.trace_value(sig, "100ns", max_depth=8)
    walk(r["tree"])


if __name__ == "__main__":
    main()
