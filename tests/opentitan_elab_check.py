#!/usr/bin/env python3
"""OpenTitan elaboration 体检 (步骤一, 四批迭代版)。

不依赖 Indago：用 pyslang 自身的 elaboration 作为结构类 ground truth。
对每个目标模块跑 wave_mcp.netlist.build_netlist，统计能否成功 + 各项计数，
并与 pyslang 自身 elaboration 的顶层端口/实例数做 ground-truth 对账。

用法:
  python3 tests/opentitan_elab_check.py [--ot /path] [--batch 1,2,3,4]
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from wave_mcp.netlist import slang_netlist  # noqa: E402

PRIM = "hw/ip/prim/rtl"
TLUL = "hw/ip/tlul/rtl"
AES = "hw/ip/aes/rtl"
HMAC = "hw/ip/hmac/rtl"
RVDM = "hw/ip/rv_dm/rtl"
TOP = "hw/top_earlgrey/rtl"
VENDOR_DM = "hw/vendor/pulp_riscv_dbg/src"

# 每项: (batch, top, [src files...]). 依赖 package 在前, 顶层在后.
TARGETS = [
    # ---- 第 1 批: 基础 prim ----
    (1, "prim_fifo_sync", [f"{PRIM}/prim_util_pkg.sv",
                           f"{PRIM}/prim_fifo_sync_cnt.sv",
                           f"{PRIM}/prim_fifo_sync.sv"]),
    (1, "prim_lfsr", [f"{PRIM}/prim_util_pkg.sv",
                      f"{PRIM}/prim_cipher_pkg.sv", f"{PRIM}/prim_lfsr.sv"]),
    (1, "prim_arbiter_tree", [f"{PRIM}/prim_util_pkg.sv",
                              f"{PRIM}/prim_arbiter_tree.sv"]),
    (1, "prim_arbiter_fixed", [f"{PRIM}/prim_util_pkg.sv",
                               f"{PRIM}/prim_arbiter_fixed.sv"]),
    (1, "prim_packer", [f"{PRIM}/prim_util_pkg.sv", f"{PRIM}/prim_packer.sv"]),
    # ---- 第 2 批: tlul (generate / struct / 跨模块) ----
    (2, "tlul_fifo_sync", [f"{TOP}/top_pkg.sv", f"{PRIM}/prim_secded_pkg.sv",
                           f"{PRIM}/prim_mubi_pkg.sv", f"{TLUL}/tlul_pkg.sv",
                           f"{PRIM}/prim_util_pkg.sv",
                           f"{PRIM}/prim_fifo_sync_cnt.sv",
                           f"{PRIM}/prim_fifo_sync.sv",
                           f"{TLUL}/tlul_fifo_sync.sv"]),
    (2, "tlul_adapter_reg", [f"{TOP}/top_pkg.sv", f"{PRIM}/prim_secded_pkg.sv",
                             f"{PRIM}/prim_mubi_pkg.sv", f"{TLUL}/tlul_pkg.sv",
                             f"{PRIM}/prim_util_pkg.sv",
                             f"{TLUL}/tlul_rsp_intg_gen.sv",
                             f"{TLUL}/tlul_cmd_intg_chk.sv",
                             f"{TLUL}/tlul_err.sv",
                             f"{TLUL}/tlul_adapter_reg.sv"]),
    # ---- 第 3 批: 大型状态机 / 密码核 ----
    (3, "hmac_core", [f"{PRIM}/prim_sha2_pkg.sv", f"{HMAC}/hmac_core.sv"]),
    (3, "aes_cipher_core", [f"{TOP}/top_pkg.sv", f"{PRIM}/prim_util_pkg.sv",
                            f"{AES}/aes_reg_pkg.sv",
                            f"{PRIM}/prim_cipher_pkg.sv",
                            "hw/ip/entropy_src/rtl/entropy_src_pkg.sv",
                            "hw/ip/csrng/rtl/csrng_reg_pkg.sv",
                            "hw/ip/csrng/rtl/csrng_pkg.sv",
                            "hw/ip/edn/rtl/edn_pkg.sv",
                            f"{AES}/aes_pkg.sv",
                            f"{AES}/aes_cipher_core.sv"]),
    # ---- 第 4 批: interface/package/深层次 ----
    (4, "rv_dm_dmi_gate", [f"{TOP}/top_pkg.sv", f"{PRIM}/prim_util_pkg.sv",
                           f"{PRIM}/prim_secded_pkg.sv", f"{PRIM}/prim_mubi_pkg.sv",
                           f"{TLUL}/tlul_pkg.sv",
                           "hw/ip/lc_ctrl/rtl/lc_ctrl_reg_pkg.sv",
                           "hw/ip/lc_ctrl/rtl/lc_ctrl_state_pkg.sv",
                           "hw/ip/lc_ctrl/rtl/lc_ctrl_pkg.sv",
                           f"{VENDOR_DM}/dm_pkg.sv",
                           f"{RVDM}/rv_dm_dmi_gate.sv"]),
]

INCDIRS_REL = [PRIM, VENDOR_DM]


def _abs_exist(ot, files):
    out, missing = [], []
    for f in files:
        p = os.path.join(ot, f)
        (out if os.path.exists(p) else missing).append(p)
    # 去重保持顺序
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq, missing


def _count_instances(members):
    """递归统计 InstanceSymbol, 包含 generate block 内的子实例 (与提取口径对齐)。"""
    n = 0
    for m in members:
        tn = type(m).__name__
        if tn == "InstanceSymbol":
            n += 1
        elif tn == "GenerateBlockSymbol":
            if getattr(m, "isUninstantiated", False):
                continue
            try:
                n += _count_instances(list(m))
            except TypeError:
                pass
        elif tn == "GenerateBlockArraySymbol":
            entries = getattr(m, "entries", None)
            if entries is None:
                try:
                    entries = list(m)
                except TypeError:
                    entries = []
            for blk in entries:
                if getattr(blk, "isUninstantiated", False):
                    continue
                try:
                    n += _count_instances(list(blk))
                except TypeError:
                    pass
    return n


def _ground_truth(abs_files, incdirs, top):
    """用 pyslang 自身 elaboration 拿顶层端口/实例数做对账基准。"""
    try:
        import pyslang as ps
        from pyslang.syntax import SyntaxTree
        from pyslang.ast import Compilation
        sm = ps.SourceManager()
        for d in incdirs:
            try:
                sm.addUserDirectories(d)
            except Exception:
                pass
        comp = Compilation()
        for f in abs_files:
            comp.addSyntaxTree(SyntaxTree.fromFile(f, sm))
        for ti in comp.getRoot().topInstances:
            dn = getattr(getattr(ti, "definition", None), "name", None) or \
                 getattr(getattr(ti, "body", None), "name", None)
            if dn == top:
                body = ti.body
                ports = sum(1 for m in body if type(m).__name__ == "PortSymbol")
                insts = _count_instances(body)
                return {"gt_ports": ports, "gt_top_insts": insts}
    except Exception as exc:
        return {"gt_error": f"{type(exc).__name__}: {exc}"}
    return {"gt_note": "top not a root instance (param/needs explicit top)"}


def check_one(ot, top, files, incdirs):
    abs_files, missing = _abs_exist(ot, files)
    if missing:
        return {"top": top, "ok": False, "stage": "locate",
                "reason": "missing: " + ", ".join(os.path.basename(m) for m in missing)}
    try:
        res = slang_netlist.build_netlist(abs_files, top=top, incdirs=incdirs)
    except Exception as exc:
        return {"top": top, "ok": False, "stage": "build_netlist",
                "reason": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc(limit=2)}
    modules = res.get("modules", {})
    mod = modules.get(top)
    if mod is None:
        return {"top": top, "ok": False, "stage": "extract",
                "reason": f"top '{top}' not in extracted: {list(modules)[:8]}",
                "diagnostics": res.get("diagnostics")}
    drivers = mod.get("drivers", {})
    n_drivers = sum(len(v) for v in drivers.values())
    out = {"top": top, "ok": True, "modules": len(modules),
           "ports": len(mod.get("ports", {})),
           "signals": len(mod.get("signals", {})),
           "driven": len(drivers), "drivers": n_drivers,
           "instances": len(mod.get("instances", [])),
           "diagnostics": res.get("diagnostics", 0),
           "parse_errors": len(res.get("parse_errors", []))}
    out.update(_ground_truth(abs_files, incdirs, top))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ot", default="/data/home/wukongxin/opentitan")
    ap.add_argument("--batch", default="1,2,3,4")
    args = ap.parse_args()
    ot = os.path.abspath(args.ot)
    want = {int(x) for x in args.batch.split(",") if x.strip()}
    incdirs = [os.path.join(ot, d) for d in INCDIRS_REL]

    print(f"OpenTitan : {ot}")
    print(f"pyslang   : {getattr(__import__('pyslang'), '__version__', '?')}")
    print(f"batches   : {sorted(want)}")
    print("=" * 92)

    results = []
    for batch, top, files in TARGETS:
        if batch not in want:
            continue
        r = check_one(ot, top, files, incdirs)
        r["batch"] = batch
        results.append(r)

    cur = None
    n_ok = 0
    for r in results:
        if r["batch"] != cur:
            cur = r["batch"]
            print(f"\n--- 第 {cur} 批 ---")
        if r["ok"]:
            n_ok += 1
            gt = ""
            if "gt_ports" in r:
                pmark = "=" if r["gt_ports"] == r["ports"] else "!"
                imark = "=" if r.get("gt_top_insts") == r["instances"] else "!"
                gt = f" | GT ports{pmark}{r['gt_ports']} inst{imark}{r.get('gt_top_insts')}"
            elif "gt_error" in r:
                gt = f" | GT_ERR {r['gt_error'][:40]}"
            print(f"[ OK ] {r['top']:<20} ports={r['ports']:>3} sig={r['signals']:>4} "
                  f"driven={r['driven']:>4} drv={r['drivers']:>5} inst={r['instances']:>3} "
                  f"mods={r['modules']:>3} diag={r['diagnostics']:>3}{gt}")
        else:
            print(f"[FAIL] {r['top']:<20} stage={r['stage']}  {r['reason']}")
    print("\n" + "=" * 92)
    print(f"结构提取成功率: {n_ok}/{len(results)} = {n_ok/max(len(results),1)*100:.0f}%")
    print("说明: GT 为 pyslang 自身 elaboration 的顶层端口/实例数; "
          "'=' 表示与我们提取一致, '!' 表示不一致(需排查)")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
