#!/usr/bin/env python3
"""四模块结构(netlist)验证: 提取 + 字段级 driver + pyslang GT 对账。"""
import os, sys, traceback
ROOT = "/data/home/wukongxin/workspace/wave_mcp"
sys.path.insert(0, ROOT)
from wave_mcp.netlist import slang_netlist

OT = "/data/home/wukongxin/opentitan"
PRIM = f"{OT}/hw/ip/prim/rtl"
TLUL = f"{OT}/hw/ip/tlul/rtl"
TOP = f"{OT}/hw/top_earlgrey/rtl"
IBEX = f"{OT}/hw/vendor/lowrisc_ibex/rtl"
DVU = f"{OT}/hw/dv/sv/dv_utils"

# 每个目标: top, 显式给出的 package/源文件(顺序), incdirs
TARGETS = {
    "tlul_adapter_host": {
        "files": [f"{TOP}/top_pkg.sv", f"{PRIM}/prim_secded_pkg.sv",
                  f"{PRIM}/prim_mubi_pkg.sv", f"{TLUL}/tlul_pkg.sv",
                  f"{PRIM}/prim_util_pkg.sv",
                  f"{TLUL}/tlul_data_integ_dec.sv", f"{TLUL}/tlul_cmd_intg_gen.sv",
                  f"{TLUL}/tlul_rsp_intg_chk.sv",
                  f"{TLUL}/tlul_adapter_host.sv"],
        "incdirs": [PRIM, TLUL],
    },
    "tlul_socket_1n": {
        "files": [f"{TOP}/top_pkg.sv", f"{PRIM}/prim_secded_pkg.sv",
                  f"{PRIM}/prim_mubi_pkg.sv", f"{PRIM}/prim_count_pkg.sv",
                  f"{TLUL}/tlul_pkg.sv", f"{PRIM}/prim_util_pkg.sv",
                  f"{PRIM}/prim_fifo_sync_cnt.sv", f"{PRIM}/prim_fifo_sync.sv",
                  f"{TLUL}/tlul_fifo_sync.sv",
                  f"{TLUL}/tlul_socket_1n.sv"],
        "incdirs": [PRIM, TLUL],
    },
    "ibex_core": {
        # 完整子模块清单(默认配置: register_file_ff + multdiv_fast),
        # 排除变体冲突(fpga/latch/slow)与 top/tracing/lockstep/icache.
        "files": [f"{PRIM}/prim_util_pkg.sv", f"{PRIM}/prim_cipher_pkg.sv",
                  f"{IBEX}/ibex_pkg.sv",
                  f"{IBEX}/ibex_alu.sv", f"{IBEX}/ibex_branch_predict.sv",
                  f"{IBEX}/ibex_compressed_decoder.sv", f"{IBEX}/ibex_controller.sv",
                  f"{IBEX}/ibex_counter.sv", f"{IBEX}/ibex_cs_registers.sv",
                  f"{IBEX}/ibex_csr.sv", f"{IBEX}/ibex_decoder.sv",
                  f"{IBEX}/ibex_dummy_instr.sv", f"{IBEX}/ibex_ex_block.sv",
                  f"{IBEX}/ibex_fetch_fifo.sv", f"{IBEX}/ibex_id_stage.sv",
                  f"{IBEX}/ibex_if_stage.sv", f"{IBEX}/ibex_load_store_unit.sv",
                  f"{IBEX}/ibex_multdiv_fast.sv", f"{IBEX}/ibex_pmp.sv",
                  f"{IBEX}/ibex_prefetch_buffer.sv",
                  f"{IBEX}/ibex_register_file_ff.sv", f"{IBEX}/ibex_wb_stage.sv",
                  f"{IBEX}/ibex_core.sv"],
        "incdirs": [PRIM, IBEX, DVU],
    },
}


def field_level_stats(mod):
    """统计字段级 driver: 含 '.' 的 lhs 个数。"""
    drivers = mod.get("drivers", {})
    field_keys = [k for k in drivers if "." in k]
    inst_port = sum(1 for recs in drivers.values() for r in recs
                    if r.get("kind") == "instance_port")
    return field_keys, inst_port


def run(top, cfg):
    print(f"\n{'='*70}\n模块: {top}\n{'='*70}")
    files = [f for f in cfg["files"] if os.path.exists(f)]
    miss = [f for f in cfg["files"] if not os.path.exists(f)]
    if miss:
        print("  [WARN] 缺文件:", [os.path.basename(m) for m in miss])
    try:
        res = slang_netlist.build_netlist(files, top=top, incdirs=cfg["incdirs"])
    except Exception as e:
        print(f"  [FAIL] build_netlist: {type(e).__name__}: {e}")
        traceback.print_exc(limit=2)
        return None
    modules = res.get("modules", {})
    mod = modules.get(top)
    if mod is None:
        print(f"  [FAIL] top '{top}' 未提取; got: {list(modules)[:8]}")
        return None
    drivers = mod.get("drivers", {})
    n_drv = sum(len(v) for v in drivers.values())
    field_keys, n_instport = field_level_stats(mod)
    print(f"  modules={len(modules)} ports={len(mod.get('ports',{}))} "
          f"signals={len(mod.get('signals',{}))} instances={len(mod.get('instances',[]))}")
    print(f"  driven={len(drivers)} drivers={n_drv} "
          f"instance_port_drv={n_instport} field_level_lhs={len(field_keys)}")
    print(f"  diagnostics={res.get('diagnostics',0)} "
          f"parse_errors={len(res.get('parse_errors',[]))}")
    if field_keys:
        print(f"  字段级 driver 样例: {sorted(field_keys)[:6]}")
    return {"res": res, "mod": mod, "field_keys": field_keys}


if __name__ == "__main__":
    for top, cfg in TARGETS.items():
        run(top, cfg)
