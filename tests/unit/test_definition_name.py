"""definition_name resolution tests — Greedy-Anchor / suffix match + inference.

Covers the decode_tb scenarios from the design doc:
  TC1 exact top match
  TC2 leaf/suffix match into a DUT-rooted partial netlist
  TC3 SV interface must NOT be inferred as a module
  TC4 pure name-inference fallback (no netlist)
  TC5 same-leaf under different parents -> disambiguate by longest suffix

Run: python tests/test_definition_name.py   (plain asserts, no pytest needed)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wave_mcp.netlist.trace_engine import TraceEngine
from wave_mcp.netlist.name_infer import infer_definition, make_name_resolver


def _engine(instance_tree):
    return TraceEngine({"modules": {m: {} for m in set(instance_tree.values())},
                        "instance_tree": instance_tree}, fst=None)


def test_tc1_exact_top():
    eng = _engine({"top_tb": "top_tb", "decode": "decode"})
    assert eng.resolve_module("top_tb") == "top_tb"


def test_tc2_leaf_suffix_into_dut_rooted_netlist():
    # netlist rooted at the DUT (decode.*), FST rooted at sim top (top_tb.U_DECODE.*)
    tree = {
        "decode": "decode",
        "decode.u_decode_unit": "decode_unit",
        "decode.u_dffr_dec_warp_sche_vld_p1_o": "DFFR",
    }
    eng = _engine(tree)
    assert eng.resolve_module("top_tb.U_DECODE.u_decode_unit") == "decode_unit"
    assert eng.resolve_module(
        "top_tb.U_DECODE.u_dffr_dec_warp_sche_vld_p1_o") == "DFFR"


def test_tc3_interface_not_inferred():
    known = {"decode": "decode", "decode_unit": "decode_unit"}
    # u_decode_tb__clk_rst_if is an SV interface: boundary-prefix must decline
    assert infer_definition("u_decode_tb__clk_rst_if", known, allow_prefix=True) is None
    assert infer_definition("tc_if", known, allow_prefix=True) is None
    assert infer_definition("u_decode_tb__csr_if", known, allow_prefix=True) is None


def test_tc4_name_inference_fallback():
    r = make_name_resolver(["my_custom"], allow_prefix=False)
    assert r("top_tb.u_my_custom") == "my_custom"


def test_tc5_same_leaf_disambiguated_by_suffix():
    tree = {
        "mod_a": "mod_a",
        "mod_a.u_sub": "sub_type_A",
        "mod_b": "mod_b",
        "mod_b.u_sub": "sub_type_B",
    }
    eng = _engine(tree)
    # leaf u_sub is ambiguous; parent context (mod_a) picks the right one
    assert eng.resolve_module("top.mod_a.u_sub") == "sub_type_A"
    assert eng.resolve_module("top.mod_b.u_sub") == "sub_type_B"
    # no parent context at all -> ambiguous -> honest None (no wrong guess)
    assert eng.resolve_module("u_sub") is None


def test_anchor_recovers_dut_root_when_name_differs():
    # DUT instance named u_dut (NOT matching module 'decode'); the netlist is
    # rooted at 'decode' and top_tb did not elaborate. Anchor propagation must
    # recover top_tb.u_dut -> decode from its resolved children, via netlist.
    tree = {
        "decode": "decode",
        "decode.u_decode_unit": "decode_unit",
        "decode.u_dffr_x": "DFFR",
    }
    eng = _engine(tree)
    scopes = ["top_tb", "top_tb.u_dut", "top_tb.u_dut.u_decode_unit",
              "top_tb.u_dut.u_dffr_x"]
    res = eng.resolve_definitions(scopes)
    assert res.get("top_tb.u_dut.u_decode_unit") == "decode_unit"  # direct
    assert res.get("top_tb.u_dut.u_dffr_x") == "DFFR"              # direct
    assert res.get("top_tb.u_dut") == "decode"                    # anchored!


def test_anchor_propagates_multiple_levels():
    tree = {"a": "a", "a.b": "modB", "a.b.c": "modC"}
    eng = _engine(tree)
    scopes = ["sim.wrap.a_inst", "sim.wrap.a_inst.b", "sim.wrap.a_inst.b.c"]
    res = eng.resolve_definitions(scopes)
    assert res.get("sim.wrap.a_inst.b.c") == "modC"
    assert res.get("sim.wrap.a_inst.b") == "modB"
    assert res.get("sim.wrap.a_inst") == "a"  # propagated up two levels


def test_dffr_prefix_still_works_without_netlist():
    # when there IS no netlist, boundary-prefix still recovers leaf cells
    known = ["dffr", "dffre", "decode"]
    r = make_name_resolver(known, allow_prefix=True)
    assert r("top.u_dffr_dec_warp_sche_vld_p1_o") == "dffr"
    assert r("top.u_dffre_dec_scorb_wid_p1_o") == "dffre"


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
        passed += 1
    print(f"\nOK: {passed}/{len(fns)} definition_name tests passed.")


if __name__ == "__main__":
    _run_all()
