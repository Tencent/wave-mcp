#!/usr/bin/env python3
"""四开源模块功能正确性验证 (结构 netlist + 真实 Verilator FST 上的 trace).

先运行 ``build_fst.sh`` 生成 FST (默认 /tmp/wave_verify), 再运行本脚本.
环境变量 FST_DIR 指定 FST 目录 (默认 /tmp/wave_verify).

验证内容 (带 PASS/FAIL 断言):
  1. netlist 提取成功 + 字段级 driver / instance_port driver 出现
  2. 真实 FST 信号值可读
  3. trace_value 跨模块穿透 (crosses_into) 且节点带真实波形值
  4. active_drivers 能定位到 RTL 源 (file/line/snippet)
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from wave_mcp.netlist import slang_netlist          # noqa: E402
from wave_mcp.netlist.trace_engine import TraceEngine  # noqa: E402
from wave_mcp.sources.fst_source import FstSource    # noqa: E402

OT = os.environ.get("OT", "/data/home/wukongxin/opentitan")
FST_DIR = os.environ.get("FST_DIR", "/tmp/wave_verify")
PRIM = f"{OT}/hw/ip/prim/rtl"
TLUL = f"{OT}/hw/ip/tlul/rtl"
TOP = f"{OT}/hw/top_earlgrey/rtl"
IBEX = f"{OT}/hw/vendor/lowrisc_ibex/rtl"
DVU = f"{OT}/hw/dv/sv/dv_utils"

CASES = {
    "tlul_adapter_host": {
        "files": [f"{TOP}/top_pkg.sv", f"{PRIM}/prim_secded_pkg.sv",
                  f"{PRIM}/prim_mubi_pkg.sv", f"{TLUL}/tlul_pkg.sv",
                  f"{PRIM}/prim_util_pkg.sv", f"{TLUL}/tlul_data_integ_dec.sv",
                  f"{TLUL}/tlul_cmd_intg_gen.sv", f"{TLUL}/tlul_rsp_intg_chk.sv",
                  f"{TLUL}/tlul_adapter_host.sv"],
        "incdirs": [PRIM, TLUL],
        "fst": "adapter_host.fst",
        "trace_sig": "TOP.tlul_adapter_host.tl_o",
        "time": "100ns",
        "expect_cross": True,
    },
    "tlul_socket_1n": {
        "files": [f"{TOP}/top_pkg.sv", f"{PRIM}/prim_secded_pkg.sv",
                  f"{PRIM}/prim_mubi_pkg.sv", f"{PRIM}/prim_count_pkg.sv",
                  f"{TLUL}/tlul_pkg.sv", f"{PRIM}/prim_util_pkg.sv",
                  f"{PRIM}/prim_fifo_sync_cnt.sv", f"{PRIM}/prim_fifo_sync.sv",
                  f"{TLUL}/tlul_fifo_sync.sv", f"{TLUL}/tlul_socket_1n.sv"],
        "incdirs": [PRIM, TLUL],
        "fst": "socket_1n.fst",
        "trace_sig": "TOP.tlul_socket_1n.tl_h_o",
        "time": "100ns",
        "expect_cross": True,
        "expect_field_level": True,
    },
    "ibex_core": {
        "files": [f"{PRIM}/prim_util_pkg.sv", f"{PRIM}/prim_cipher_pkg.sv",
                  f"{IBEX}/ibex_pkg.sv", f"{IBEX}/ibex_alu.sv",
                  f"{IBEX}/ibex_branch_predict.sv", f"{IBEX}/ibex_compressed_decoder.sv",
                  f"{IBEX}/ibex_controller.sv", f"{IBEX}/ibex_counter.sv",
                  f"{IBEX}/ibex_cs_registers.sv", f"{IBEX}/ibex_csr.sv",
                  f"{IBEX}/ibex_decoder.sv", f"{IBEX}/ibex_dummy_instr.sv",
                  f"{IBEX}/ibex_ex_block.sv", f"{IBEX}/ibex_fetch_fifo.sv",
                  f"{IBEX}/ibex_id_stage.sv", f"{IBEX}/ibex_if_stage.sv",
                  f"{IBEX}/ibex_load_store_unit.sv", f"{IBEX}/ibex_multdiv_fast.sv",
                  f"{IBEX}/ibex_pmp.sv", f"{IBEX}/ibex_prefetch_buffer.sv",
                  f"{IBEX}/ibex_register_file_ff.sv", f"{IBEX}/ibex_wb_stage.sv",
                  f"{IBEX}/ibex_core.sv"],
        "incdirs": [PRIM, IBEX, DVU],
        "fst": "ibex_core.fst",
        "trace_sig": "TOP.ibex_core.instr_req_o",
        "time": "300ns",
        "expect_cross": True,
    },
}


def _count_tree(node, stats):
    stats["nodes"] += 1
    if node.get("crosses_into"):
        stats["crosses"] += 1
    if node.get("value") not in (None, ""):
        stats["valued"] += 1
    stats["max_depth"] = max(stats["max_depth"], node.get("_d", 0))
    for c in node.get("contributors", []):
        c["_d"] = node.get("_d", 0) + 1
        _count_tree(c, stats)


def run_case(top, cfg):
    res = {"top": top, "checks": []}

    def chk(name, cond, detail=""):
        res["checks"].append((name, bool(cond), detail))

    # --- netlist ---
    files = [f for f in cfg["files"] if os.path.exists(f)]
    maps = slang_netlist.build_netlist(files, top=top, incdirs=cfg["incdirs"])
    mod = maps["modules"].get(top, {})
    drivers = mod.get("drivers", {})
    n_drv = sum(len(v) for v in drivers.values())
    field_keys = [k for k in drivers if "." in k]
    inst_port = sum(1 for recs in drivers.values() for r in recs
                    if r.get("kind") == "instance_port")
    chk("netlist:extract", top in maps["modules"], f"{len(maps['modules'])} modules")
    chk("netlist:drivers>0", n_drv > 0, f"{n_drv} drivers")
    if cfg.get("expect_field_level"):
        chk("netlist:field_level", len(field_keys) > 0,
            f"{len(field_keys)} field-level lhs")

    # --- FST ---
    fst_path = os.path.join(FST_DIR, cfg["fst"])
    if not os.path.exists(fst_path):
        chk("fst:exists", False, fst_path + " (run build_fst.sh first)")
        return res
    fs = FstSource(fst_path)
    chk("fst:signals>0", len(fs.signals) > 0, f"{len(fs.signals)} signals")
    te = TraceEngine(maps, fs)

    # --- active_drivers locates RTL source ---
    ad = te.active_drivers(cfg["trace_sig"], cfg["time"])
    drv_list = ad.get("active_drivers", [])
    has_src = any(d.get("line") and d.get("snippet") for d in drv_list) \
        or ad.get("note", "").startswith("no RTL")
    chk("active_drivers:resolved", ad.get("available"), ad.get("note", ""))
    chk("active_drivers:src_or_boundary", has_src or not drv_list,
        f"{len(drv_list)} drivers")

    # --- trace_value cross-module + valued ---
    tv = te.trace_value(cfg["trace_sig"], cfg["time"], max_depth=10)
    stats = {"nodes": 0, "crosses": 0, "valued": 0, "max_depth": 0}
    tv["tree"]["_d"] = 0
    _count_tree(tv["tree"], stats)
    chk("trace:nodes>1", stats["nodes"] > 1, f"{stats['nodes']} nodes")
    if cfg.get("expect_cross"):
        chk("trace:cross_module", stats["crosses"] > 0,
            f"{stats['crosses']} crosses_into, depth={stats['max_depth']}")
    chk("trace:has_values", stats["valued"] > 0, f"{stats['valued']} valued nodes")
    res["stats"] = stats
    return res


def main():
    print(f"OT={OT}\nFST_DIR={FST_DIR}\n" + "=" * 72)
    all_ok = True
    for top, cfg in CASES.items():
        print(f"\n### {top}")
        try:
            r = run_case(top, cfg)
        except Exception as e:
            import traceback
            print(f"  [EXCEPTION] {type(e).__name__}: {e}")
            traceback.print_exc(limit=2)
            all_ok = False
            continue
        for name, ok, detail in r["checks"]:
            mark = "PASS" if ok else "FAIL"
            if not ok:
                all_ok = False
            print(f"  [{mark}] {name:<32} {detail}")
    print("\n" + "=" * 72)
    print("总判定:", "ALL PASS" if all_ok else "HAS FAILURES")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
