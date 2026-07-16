#!/usr/bin/env python3
"""ibex_core 大型模块 trace 验证 (真实 FST)。"""
import sys
sys.path.insert(0, "/data/home/wukongxin/workspace/wave_mcp")
from wave_mcp.netlist import slang_netlist
from wave_mcp.netlist.trace_engine import TraceEngine
from wave_mcp.sources.fst_source import FstSource

OT = "/data/home/wukongxin/opentitan"
PRIM = f"{OT}/hw/ip/prim/rtl"; IBEX = f"{OT}/hw/vendor/lowrisc_ibex/rtl"; DVU = f"{OT}/hw/dv/sv/dv_utils"
FILES = [f"{PRIM}/prim_util_pkg.sv", f"{PRIM}/prim_cipher_pkg.sv", f"{IBEX}/ibex_pkg.sv",
         f"{IBEX}/ibex_alu.sv", f"{IBEX}/ibex_branch_predict.sv",
         f"{IBEX}/ibex_compressed_decoder.sv", f"{IBEX}/ibex_controller.sv",
         f"{IBEX}/ibex_counter.sv", f"{IBEX}/ibex_cs_registers.sv", f"{IBEX}/ibex_csr.sv",
         f"{IBEX}/ibex_decoder.sv", f"{IBEX}/ibex_dummy_instr.sv", f"{IBEX}/ibex_ex_block.sv",
         f"{IBEX}/ibex_fetch_fifo.sv", f"{IBEX}/ibex_id_stage.sv", f"{IBEX}/ibex_if_stage.sv",
         f"{IBEX}/ibex_load_store_unit.sv", f"{IBEX}/ibex_multdiv_fast.sv", f"{IBEX}/ibex_pmp.sv",
         f"{IBEX}/ibex_prefetch_buffer.sv", f"{IBEX}/ibex_register_file_ff.sv",
         f"{IBEX}/ibex_wb_stage.sv", f"{IBEX}/ibex_core.sv"]
FST = "/tmp/wave_verify/ibex_core.fst"


def walk(node, depth=0, maxn=[0]):
    if maxn[0] > 40:
        return
    maxn[0] += 1
    cross = " [CROSS]" if node.get("crosses_into") else ""
    trunc = " ..." if node.get("truncated") else ""
    val = node.get("value")
    vs = f" = {val}" if val not in (None, "") else ""
    name = node["signal"].replace("TOP.ibex_core.", "")
    print("   " + "  " * depth + name + vs + cross + trunc)
    for c in node.get("contributors", []):
        walk(c, depth + 1, maxn)


def main():
    maps = slang_netlist.build_netlist(FILES, top="ibex_core", incdirs=[PRIM, IBEX, DVU])
    fs = FstSource(FST)
    te = TraceEngine(maps, fs)
    print("modules extracted:", len(maps["modules"]),
          "| instance_tree size:", len(maps["instance_tree"]))
    print()

    for sig in ["TOP.ibex_core.instr_req_o",
                "TOP.ibex_core.data_req_o",
                "TOP.ibex_core.id_stage_i.instr_valid_i"]:
        print(f"===== active_drivers: {sig} =====")
        r = te.active_drivers(sig, "300ns")
        if not r.get("available"):
            print("  ", r.get("reason")); print(); continue
        ad = r.get("active_drivers", [])
        print(f"  module={r.get('module')} #drivers={len(ad)} note={r.get('note','')}")
        for d in ad[:3]:
            print(f"   kind={d['kind']} line={d.get('line')} :: {d.get('snippet','')[:70]}")
        print()

    sig = "TOP.ibex_core.instr_req_o"
    print(f"===== trace_value: {sig} @300ns (deep cross-module) =====")
    r = te.trace_value(sig, "300ns", max_depth=10)
    walk(r["tree"])


if __name__ == "__main__":
    main()
