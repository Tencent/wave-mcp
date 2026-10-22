"""Bit and part selects on vectors (GitHub issue #4).

An RCA agent that finds one wrong bit in ``cnt`` asks for ``cnt[3]``. The
waveform carries ``cnt`` as one 8-bit VAR and the netlist records drivers per
signal, so before this fix the select was "not found" in the waveform and
"not declared" in the netlist: signal_values errored, trace_value returned a
null value at a fake boundary and signal_drivers called it a constant.

The waveform is written with pylibfst, so no simulator is needed. Ranges are
embedded in VAR names the way vcd2fst and Verilator write them
(``cnt [7:0]``), including a non-zero-based and an ascending range, because
index-to-bit mapping must follow the declaration, not assume ``[w-1:0]``.

Run: python3 -m pytest tests/unit/test_bit_select.py
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pylibfst import ffi, lib  # noqa: E402

from wave_mcp.sources.fst_source import FstSource  # noqa: E402

RTL = """\
module dut (
    input  logic       clk,
    input  logic [7:0] a,
    output logic [7:0] cnt,
    output logic       b3
);
    always_ff @(posedge clk) cnt <= a;
    assign b3 = cnt[3];
endmodule

module top;
    logic       clk;
    logic [7:0] a;
    logic [7:0] cnt;
    logic       b3;
    dut u_dut (.clk(clk), .a(a), .cnt(cnt), .b3(b3));
endmodule
"""

# VAR name -> width. Names carry their range as simulators write them.
VARS = {
    "clk": 1,
    "a [7:0]": 8,
    "cnt [7:0]": 8,
    "b3": 1,
    "asc [8:1]": 8,      # non-zero-based: asc[1] is the LSB
    "up [0:3]": 4,       # ascending: up[0] is the MSB
    "sb [3]": 1,         # a bus split into one VAR per bit
    "sb [2]": 1,
    "sb [1]": 1,
    "sb [0]": 1,
    "mem[2]": 8,         # an unpacked-array element, queryable verbatim
}

CHANGES = [
    (0, "clk", "0"), (0, "a [7:0]", "00000000"), (0, "cnt [7:0]", "00000000"),
    (0, "b3", "0"), (0, "asc [8:1]", "10000001"), (0, "up [0:3]", "1000"),
    (0, "sb [3]", "1"), (0, "sb [2]", "0"), (0, "sb [1]", "1"),
    (0, "sb [0]", "0"), (0, "mem[2]", "00100000"),
    (5, "a [7:0]", "00001000"),
    (10, "clk", "1"), (10, "cnt [7:0]", "00001000"), (10, "b3", "1"),
    (15, "a [7:0]", "00001001"),
    (20, "clk", "0"), (20, "cnt [7:0]", "00001001"),   # bit 3 unchanged
    (25, "a [7:0]", "00000001"),
    (30, "clk", "1"), (30, "cnt [7:0]", "00000001"), (30, "b3", "0"),
]


def _write_fst(path: str, changes=None) -> str:
    ctx = lib.fstWriterCreate(path.encode(), 1)
    lib.fstWriterSetTimescale(ctx, -9)
    lib.fstWriterSetScope(ctx, 0, b"top", ffi.NULL)
    lib.fstWriterSetScope(ctx, 0, b"u_dut", ffi.NULL)
    handles = {name: lib.fstWriterCreateVar(
        ctx, lib.FST_VT_VCD_WIRE, 0, w, name.encode(), 0)
        for name, w in VARS.items()}
    lib.fstWriterSetUpscope(ctx)
    lib.fstWriterSetUpscope(ctx)
    last = None
    for t, name, v in (CHANGES if changes is None else changes):
        if t != last:
            lib.fstWriterEmitTimeChange(ctx, t)
            last = t
        lib.fstWriterEmitValueChange(ctx, handles[name], v.encode())
    lib.fstWriterClose(ctx)
    return path


P = "top.u_dut."


@pytest.fixture(scope="module")
def wave(tmp_path_factory):
    d = tmp_path_factory.mktemp("bitsel")
    fst_path = _write_fst(str(d / "bitsel.fst"))
    rtl = d / "bitsel.sv"
    rtl.write_text(RTL)
    return str(d), fst_path, str(rtl)


@pytest.fixture(scope="module")
def fst(wave):
    src = FstSource(wave[1])
    yield src
    src.close()


def _val(fst, path, t):
    row = fst.value_at(P + path, t)
    return None if row is None else row["value"]


def _changes(fst, path, start, end):
    rows = fst.values_between(P + path, start, end)
    return None if rows is None else [(r["time_units"], r["value"])
                                      for r in rows]


class TestWaveformSelects:
    def test_bit_select_point_value(self, fst):
        row = fst.value_at(P + "cnt[3]", 15)
        assert row["value"] == "1" and row["hex"] == "1"
        assert row["select_of"] == P + "cnt"
        assert _val(fst, "cnt[3]", 30) == "0"
        assert _val(fst, "cnt[0]", 25) == "1"

    def test_part_selects(self, fst):
        # cnt = 00001001 at t=25
        assert _val(fst, "cnt[7:4]", 25) == "0000"
        assert _val(fst, "cnt[3:0]", 25) == "1001"
        assert _val(fst, "cnt[3+:2]", 25) == "01"   # bits 4..3
        assert _val(fst, "cnt[4-:2]", 25) == "01"   # bits 4..3
        assert _val(fst, "cnt[7:0]", 25) == "00001001"

    def test_index_follows_declared_range(self, fst):
        # asc [8:1] = 10000001: asc[8] is the MSB, asc[1] the LSB
        assert _val(fst, "asc[8]", 0) == "1"
        assert _val(fst, "asc[2]", 0) == "0"
        assert _val(fst, "asc[1]", 0) == "1"
        assert _val(fst, "asc[0]", 0) is None
        # up [0:3] = 1000: ascending, up[0] is the MSB
        assert _val(fst, "up[0]", 0) == "1"
        assert _val(fst, "up[3]", 0) == "0"
        assert _val(fst, "up[0:1]", 0) == "10"

    def test_out_of_range_is_not_found(self, fst):
        assert fst.value_at(P + "cnt[8]", 0) is None
        assert fst.values_between(P + "cnt[9:8]", 0, 40) is None
        assert not fst.has_signal(P + "cnt[8]")
        assert fst.has_signal(P + "cnt[7]")

    def test_window_reports_changes_of_selected_bits_only(self, fst):
        # the base changes at 0, 10, 20, 30; bit 3 only at 0, 10, 30
        assert _changes(fst, "cnt[3]", 0, 40) == [(0, "0"), (10, "1"),
                                                   (30, "0")]
        # starting mid-run: bit 3 is already 1 before 15, so its first
        # change inside the window is the fall at 30
        assert _changes(fst, "cnt[3]", 15, 40) == [(30, "0")]

    def test_signal_info_for_select(self, fst):
        info = fst.signal_info(P + "cnt[7:4]")
        assert info["width"] == 4 and info["select_of"] == P + "cnt"

    def test_split_bus_part_select_takes_only_its_bits(self, fst):
        # sb is four 1-bit VARs: 3=1 2=0 1=1 0=0
        assert _val(fst, "sb[3:0]", 0) == "1010"
        assert _val(fst, "sb[2:1]", 0) == "01"

    def test_select_on_array_element(self, fst):
        assert _val(fst, "mem[2]", 0) == "00100000"
        assert _val(fst, "mem[2][5]", 0) == "1"

    def test_exact_signals_unchanged(self, fst):
        row = fst.value_at(P + "cnt", 25)
        assert row["value"] == "00001001" and "select_of" not in row


@pytest.fixture(scope="module")
def client(wave):
    from session_client import bound
    from wave_mcp.pipeline import prepare_session
    out, fst_path, rtl = wave
    res = prepare_session(os.path.join(out, "session"), fst_path, top="top",
                          filelist=[rtl])
    assert res["steps"][0]["ok"], res["steps"]
    c = bound(res["session_path"])
    yield c
    c.close()


def _strip(res):
    res.pop("_fp", None)
    res.pop("_query", None)
    return res


class TestToolsOnSelects:
    def test_signal_values(self, client):
        res = _strip(client.signal_values(paths=[P + "cnt[3]"], time="15ns"))
        row = res["signals"][0]
        assert row["value"] == "1" and row["select_of"] == P + "cnt"

    def test_trace_value_reaches_the_vector_driver(self, client):
        res = _strip(client.trace_value(path=P + "cnt[3]", time="15ns"))
        tree = res["tree"]
        assert tree["value"] == "1"
        assert tree.get("boundary") is None
        assert tree["driver"]["snippet"] == "always_ff @(posedge clk) cnt <= a;"
        assert tree["select_of"] == P + "cnt"
        assert "whole vector" in tree["select_note"]
        assert any(c["signal"] == P + "a" for c in tree.get("contributors", []))

    def test_signal_drivers(self, client):
        res = _strip(client.signal_drivers(path=P + "cnt[3]"))
        assert res["drivers"], res
        assert res["select_of"] == P + "cnt"
        exact = _strip(client.signal_drivers(path=P + "cnt"))
        assert exact["drivers"] == res["drivers"]
        assert "select_of" not in exact

    def test_active_drivers(self, client):
        res = _strip(client.active_drivers(path=P + "cnt[3]", time="15ns"))
        assert res["active_drivers"]
        assert res["active_drivers"][0]["driver_unique_id"] == "dut.cnt#0"
        assert res["select_of"] == P + "cnt"

    def test_signal_loads(self, client):
        res = _strip(client.signal_loads(path=P + "cnt[3]"))
        assert P + "b3" in res["loads"]
        assert res["select_of"] == P + "cnt"


def _sig(path):
    return {"k": "sig", "name": path}


class TestAnalysisToolsOnSelects:
    """The analysis tools read timelines through FstSource.timelines, so a
    select works the same in every one of them as in signal_values.

    cnt over the window: 0 -> 00001000 @10 -> 00001001 @20 -> 00000001 @30,
    so cnt[3] rises at 10 and falls at 30 (the change at 20 leaves it alone),
    and cnt[0] rises at 20.
    """

    def test_signal_activity_counts_only_selected_bit_toggles(self, client):
        res = _strip(client.signal_activity(
            paths=[P + "cnt[3]", P + "cnt[0]", P + "cnt"],
            start="0ns", end="30ns"))
        rows = {r["path"]: r for r in res["signals"]}
        assert rows[P + "cnt[3]"]["toggles"] == 2
        assert rows[P + "cnt[3]"]["first_change"] == "10ns"
        assert rows[P + "cnt[3]"]["last_change"] == "30ns"
        assert rows[P + "cnt[0]"]["toggles"] == 1
        assert rows[P + "cnt"]["toggles"] == 3

    def test_find_time_windows(self, client):
        res = _strip(client.find_time_windows(
            predicate=_sig(P + "cnt[3]"), start="0ns", end="30ns"))
        assert [(w["start"], w["end"]) for w in res["windows"]] == [
            ("10ns", "30ns")]

    def test_sample_at_clock_with_select_column(self, client):
        res = _strip(client.sample_at_clock(
            paths=[P + "cnt[3:0]"], clock=P + "clk", start="0ns", end="30ns"))
        assert [c["values"][P + "cnt[3:0]"] for c in res["cycles"]] == [
            "1000", "0001"]
        assert "unknown_paths" not in res

    def test_sample_at_clock_with_select_clock(self, client):
        res = _strip(client.sample_at_clock(
            paths=[P + "a"], clock=P + "cnt[0]", start="0ns", end="30ns"))
        assert [c["time"] for c in res["cycles"]] == ["20ns"]
        assert res["cycles"][0]["values"][P + "a"] == "00001001"

    def test_fsm_transitions_on_select(self, client):
        res = _strip(client.fsm_transitions(
            path=P + "cnt[3]", start="0ns", end="30ns"))
        assert res["status"] == "ok"
        assert res["states_seen"] == ["0", "1"]
        assert [(t["from"], t["to"]) for t in res["transitions"]] == [
            ("0", "1"), ("1", "0")]
        assert res["select_of"] == P + "cnt"

    def test_fold_transactions_with_select_conditions(self, client):
        res = _strip(client.fold_transactions(
            start_cond=_sig(P + "cnt[3]"),
            end_cond={"k": "un", "op": "LogicalNot", "a": _sig(P + "cnt[3]")},
            fields=[P + "a[3:0]"], start="0ns", end="30ns"))
        assert res["status"] == "ok" and res["count"] == 1
        txn = res["transactions"][0]
        assert (txn["start"], txn["end"]) == ("10ns", "30ns")
        assert txn["fields_at_start"][P + "a[3:0]"] == "1000"
        assert txn["fields_at_end"][P + "a[3:0]"] == "0001"

    def test_unknown_select_is_reported_missing(self, client):
        res = _strip(client.find_time_windows(
            predicate=_sig(P + "cnt[8]"), start="0ns", end="30ns"))
        assert res["error_type"] == "signal_not_found"
        assert res["missing"] == [P + "cnt[8]"]

    def test_diff_waveforms_on_selects(self, wave):
        from wave_mcp.diff import diff_waveforms
        out, fst_a, _rtl = wave
        # run b differs from run a only in cnt[1], at 20
        changes_b = [(t, n, "00001011" if (t, n) == (20, "cnt [7:0]") else v)
                     for t, n, v in CHANGES]
        fst_b = _write_fst(os.path.join(out, "bitsel_b.fst"), changes_b)
        res = diff_waveforms([fst_a, fst_b],
                             signals=[P + "cnt[3]", P + "cnt[1]"])
        assert res["compared"]["identical"] == 1
        assert [d["path"] for d in res["diverging_signals"]] == [P + "cnt[1]"]
        assert res["first_divergence"]["time"] == "20ns"


class TestBatchedValueReads:
    """signal_values reads selects in the same file pass as whole signals."""

    def test_window_mixes_signals_and_selects(self, fst):
        from wave_mcp.analysis import values
        res = values.read_values(
            fst, [P + "cnt", P + "cnt[3]", P + "cnt[0]", P + "sb[3:0]",
                  P + "cnt[8]"], 0, 40, None)
        rows = {r["path"]: r for r in res["signals"]}
        pick = lambda p: [(v["time_units"], v["value"]) for v in rows[p]["values"]]
        assert pick(P + "cnt[3]") == [(0, "0"), (10, "1"), (30, "0")]
        assert pick(P + "cnt[0]") == [(0, "0"), (20, "1")]
        assert len(pick(P + "cnt")) == 4
        assert pick(P + "sb[3:0]")[0] == (0, "1010")      # split bus path
        assert "error" in rows[P + "cnt[8]"]
        assert rows[P + "cnt[3]"]["values"][0]["select_of"] == P + "cnt"

    def test_mid_window_select_starts_at_a_real_change(self, fst):
        from wave_mcp.analysis import values
        res = values.read_values(fst, [P + "cnt[3]", P + "cnt"], 15, 40, None)
        rows = {r["path"]: r for r in res["signals"]}
        sel = [(v["time_units"], v["value"]) for v in rows[P + "cnt[3]"]["values"]]
        # cnt changes at 20 too, but bit 3 holds 1 across it
        assert sel == [(30, "0")]
        assert sel == _changes(fst, "cnt[3]", 15, 40)

    def test_unknown_name_does_not_rescan(self, fst):
        fst.value_at(P + "nosuch", 0)
        idx = fst._elem_idx
        assert idx is not None and P + "sb" in idx
        fst.value_at(P + "nosuch2", 0)
        assert fst._elem_idx is idx                         # built once
