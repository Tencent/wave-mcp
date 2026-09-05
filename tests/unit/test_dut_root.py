"""DUT-rooted netlist regression — the case that made signal_drivers 0/30.

Background (why this file exists)
---------------------------------
`_resolve_key` resolves an FST scope to a netlist module in three levels:
exact key, leaf+suffix, then FST scope metadata. Level three used to read
only `module_name` (the FST "component" field), which is empty for every
waveform we produce, so it never resolved anything. It was dead code.

Nobody noticed for a long time, and the reason matters, because it is also
the reason this test must be shaped the way it is. Historical sessions
elaborated from the testbench top, so the netlist root key equalled the FST
root scope and level one absorbed every lookup. `tests/fourstate` looks like
it should have caught this, since its netlist root (`fourstate_top`) differs
from its FST root (`tb_fourstate.dut`), but its DUT instance name `dut` is
unique in the leaf index, so level two matched first and level three was
again never reached.

Reaching level three needs all three of:
  1. netlist rooted at the DUT, not the testbench
  2. FST root scope named differently from the netlist root key
  3. DUT instance name that level two cannot resolve

Condition 3 has two shapes, and both are covered here because they fail
through the same dead branch but are reached differently:

  Shape A  The DUT instance name is absent from the netlist entirely. This
           is what the real project hit: the netlist keys are `decode` and
           `decode.u_core`, so `U_DECODE` is not in the leaf index at all.

  Shape B  The DUT instance name is in the leaf index but genuinely
           ambiguous, two candidates with equal suffix length, so level two
           declines the tie rather than guess. Note that a *near* miss is not
           enough: adding a `other.U_DECODE` key to shape A lets level two
           pick a winner by longer suffix, which silently resolves the root
           to the wrong module. Ambiguity must be an exact tie.

Everything runs on a synthetic FST written by pylibfst, so no simulator is
needed and the failure cannot be masked by a stale checked-in waveform.

Run: python3 tests/unit/test_dut_root.py   (plain asserts, no pytest needed)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from pylibfst import lib, ffi

from wave_mcp.netlist.trace_engine import TraceEngine
from wave_mcp.sources.fst_source import FstSource

# Mirrors what slang_netlist.build_netlist produces for a module: the keys
# resolve_path() reads. 'valid' is driven so the driver lookup is meaningful.
DECODE_MOD = {
    "ports": {"clk": "input"},
    "signals": {"valid": "logic"},
    "drivers": {"valid": [{"driver": "u_core.ready", "type": "assign"}]},
}
CORE_MOD = {"ports": {"ready": "output"}, "signals": {}, "drivers": {}}
ALPHA_MOD = {"ports": {"ready": "output"}, "signals": {}, "drivers": {}}

# Shape A: DUT-rooted. Root key 'decode' vs FST root 'top_tb.U_DECODE'.
# 'U_DECODE' is absent from the leaf index, so level two cannot match.
NETLIST_A = {
    "modules": {"decode": DECODE_MOD, "core": CORE_MOD},
    "instance_tree": {"decode": "decode", "decode.u_core": "core"},
}

# Shape B: 'U_DECODE' present twice, equal suffix length -> exact tie -> level
# two declines. The tie is broken by definition_name, which the anchor pass
# derives from the unique child leaf 'u_alpha' under blk_a. The child is what
# makes this reachable: with no resolvable child there is no anchor, so no
# definition_name gets written back and level three still has nothing to read.
NETLIST_B = {
    "modules": {"decode": DECODE_MOD, "alpha": ALPHA_MOD, "shadow": CORE_MOD},
    "instance_tree": {
        "blk_a.U_DECODE": "decode",
        "blk_a.U_DECODE.u_alpha": "alpha",
        "blk_b.U_DECODE": "shadow",
    },
}

FST_ROOT = "top_tb.U_DECODE"


def _write_fst(path, child="u_core"):
    """top_tb.clk / top_tb.U_DECODE.valid / top_tb.U_DECODE.<child>.ready."""
    ctx = lib.fstWriterCreate(path.encode(), 1)
    assert ctx, f"cannot create {path}"
    lib.fstWriterSetTimescale(ctx, -12)
    lib.fstWriterSetScope(ctx, 0, b"top_tb", ffi.NULL)
    h_clk = lib.fstWriterCreateVar(ctx, lib.FST_VT_VCD_WIRE, 0, 1, b"clk", 0)
    lib.fstWriterSetScope(ctx, 0, b"U_DECODE", ffi.NULL)
    h_valid = lib.fstWriterCreateVar(ctx, lib.FST_VT_VCD_WIRE, 0, 1, b"valid", 0)
    lib.fstWriterSetScope(ctx, 0, child.encode(), ffi.NULL)
    h_ready = lib.fstWriterCreateVar(ctx, lib.FST_VT_VCD_WIRE, 0, 1, b"ready", 0)
    for _ in range(3):
        lib.fstWriterSetUpscope(ctx)
    lib.fstWriterEmitTimeChange(ctx, 0)
    for h, v in ((h_clk, b"0"), (h_valid, b"1"), (h_ready, b"0")):
        lib.fstWriterEmitValueChange(ctx, h, v)
    lib.fstWriterEmitTimeChange(ctx, 100)
    lib.fstWriterEmitValueChange(ctx, h_clk, b"1")
    lib.fstWriterClose(ctx)
    return path


def _session(netlist, child="u_core"):
    """FST + engine wired up the way session.py does it.

    Order matters: resolve_definitions() runs first and its result is written
    back onto the scopes, which is what gives level three something to read.
    A test that skipped the write-back would not exercise the fix.
    """
    d = tempfile.mkdtemp(prefix="wave_mcp_dutroot_")
    fst = FstSource(_write_fst(os.path.join(d, "dutroot.fst"), child=child))
    eng = TraceEngine(netlist, fst=fst)
    netmap = eng.resolve_definitions(list(fst.scopes.keys()))
    fst.apply_definition_map(netmap, source="netlist")
    return fst, eng


# -- shape A: DUT instance name absent from the netlist --------------------

def test_fst_component_is_empty_so_level_three_cannot_use_it():
    """Root cause: module_name is empty for our FSTs, hence the dead branch.

    If a future FST writer starts emitting components this test fails, and
    level three becomes reachable through module_name again. That would be
    fine, but the comment in trace_engine.py would then be stale.
    """
    fst, _ = _session(NETLIST_A)
    sc = fst.scopes[FST_ROOT]
    assert sc.module_name == "", (
        f"expected empty FST component, got {sc.module_name!r}")


def test_shape_a_dut_instance_absent_from_leaf_index():
    """Guards the test's premise: level two really has nothing to match."""
    _, eng = _session(NETLIST_A)
    assert "U_DECODE" not in eng._leaf_index, (
        "U_DECODE is in the leaf index; level two would absorb the lookup "
        "and this file would stop testing level three")


def test_shape_a_dut_root_resolves():
    """The regression: DUT root resolves so its own signals become traceable."""
    _, eng = _session(NETLIST_A)
    assert eng.resolve_module(FST_ROOT) == "decode", (
        "DUT root did not resolve; every signal declared directly on the DUT "
        "top reports unresolved_path even though the netlist has its drivers")


def test_shape_a_dut_root_signal_traces():
    """User-visible symptom: a signal on the DUT top resolves with drivers."""
    _, eng = _session(NETLIST_A)
    mod, inst, leaf, drivers = eng.resolve_path(f"{FST_ROOT}.valid")
    assert mod == "decode", f"module {mod!r}, expected 'decode'"
    assert inst == FST_ROOT, f"instance {inst!r}, expected {FST_ROOT!r}"
    assert leaf == "valid", f"leaf {leaf!r}, expected 'valid'"
    assert drivers, "valid has drivers in the netlist but none came back"


def test_shape_a_children_still_resolve():
    """Children resolved before the fix; they must keep resolving after it."""
    _, eng = _session(NETLIST_A)
    assert eng.resolve_module(f"{FST_ROOT}.u_core") == "core"


# -- shape B: ambiguous leaf, level two declines the tie -------------------

def test_shape_b_leaf_index_is_a_genuine_tie():
    """Guards the premise: an exact tie, not one level two could break."""
    _, eng = _session(NETLIST_B, child="u_alpha")
    assert len(eng._leaf_index["U_DECODE"]) == 2
    # no parent context to break the tie -> declines rather than guesses
    assert eng.resolve_module("nowhere.U_DECODE") is None


def test_shape_b_definition_name_picks_the_right_module():
    """Tie broken by definition_name, not by a longer-suffix guess."""
    _, eng = _session(NETLIST_B, child="u_alpha")
    assert eng.resolve_module(FST_ROOT) == "decode"


# -- guard rails ------------------------------------------------------------

def test_unrelated_scope_still_unresolved():
    """The fix must not make unresolvable scopes resolve to something wrong."""
    _, eng = _session(NETLIST_A)
    assert eng.resolve_module("top_tb") is None


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
        passed += 1
    print(f"\nOK: {passed}/{len(fns)} dut_root tests passed.")


if __name__ == "__main__":
    _run_all()
