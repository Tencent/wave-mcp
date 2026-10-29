"""Complete clock sampling on real FSTs, including long histories and failures.

The long counter changes at 2*k and rises at 2*k+1: its golden value is k,
independent of both the sampler and point queries. No simulator is required.
"""
from __future__ import annotations

import asyncio
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import pylibfst
import pytest
from pylibfst import lib

from fstgen import write_fst
from wave_mcp.analysis.clocking import clock_edges, sample_table
from wave_mcp.analysis.sampling import downsample, sample_indices
from wave_mcp.diff import diff_waveforms
from wave_mcp.sources.fst_source import FstSource

N = 220_003


@pytest.fixture(scope="module")
def long_wave(tmp_path_factory):
    def changes():
        yield 0, "clk", "0"
        yield 0, "q", "0" * 32
        yield 0, "held", "1010"
        yield 0, "sparse", "0" * 32
        for k in range(1, N + 1):
            yield 2 * k, "clk", "0"
            yield 2 * k, "q", f"{k:032b}"
            if k % 100_000 == 0:
                yield 2 * k, "sparse", f"{k:032b}"
            yield 2 * k + 1, "clk", "1"

    return write_fst(str(tmp_path_factory.mktemp("clock") / "long.fst"),
                     {"clk": 1, "q": 32, "held": 4, "sparse": 32},
                     changes(), timescale_exp=-9)


@pytest.fixture
def long_fst(long_wave):
    src = FstSource(long_wave)
    try:
        yield src
    finally:
        src.close()


def pairs(result, path="top.q"):
    return [(r["time_units"], int(r["values"][path], 2))
            for r in result["cycles"]]


def test_long_core_queries(long_fst):
    narrow = sample_table(long_fst, ["top.q"], "top.clk",
                          440002, 440006, limit=20)
    assert pairs(narrow) == [(440003, 220001), (440005, 220002)]
    assert narrow["count"] == 2 and "sampled" not in narrow
    wide = sample_table(long_fst, ["top.q"], "top.clk", 0, 440007, limit=3)
    assert pairs(wide) == [(3, 1), (220005, 110002), (440007, 220003)]
    assert wide["count"] == 3 and wide["total_available"] == N
    assert wide["sampled"] is True
    assert wide["sample_rate"] == round(3 / N, 6)
    assert wide["resume_from"] == wide["cycles"][1]["time"]


@pytest.mark.parametrize("k", [199998, 199999, 200000, 200001, 220002])
def test_history_near_old_data_cap(long_fst, k):
    result = sample_table(long_fst, ["top.q"], "top.clk",
                          2 * k, 2 * k + 1, limit=1)
    assert pairs(result) == [(2 * k + 1, k)]


@pytest.mark.parametrize("n", [99999, 100000, 100001, 199999,
                               200000, 200001, N])
def test_full_window_near_old_clock_and_data_caps(long_fst, n):
    result = sample_table(long_fst, ["top.q"], "top.clk",
                          0, 2 * n + 1, limit=2)
    assert pairs(result) == [(3, 1), (2 * n + 1, n)]
    assert result["total_available"] == n


def test_default_limit_held_sparse_selects_and_common_times(long_fst):
    paths = ["top.q", "top.held", "top.sparse", "top.q[7:0]", "top.missing"]
    result = sample_table(long_fst, paths, "top.clk", 0, 440007)
    assert result["count"] == 1000 and result["total_available"] == N
    assert result["unknown_paths"] == ["top.missing"]
    for row in result["cycles"]:
        k = (row["time_units"] - 1) // 2
        assert int(row["values"]["top.q"], 2) == k
        assert row["values"]["top.held"] == "1010"
        assert int(row["values"]["top.sparse"], 2) == k // 100000 * 100000
        assert int(row["values"]["top.q[7:0]"], 2) == k % 256
    # Different output limits must agree on every time they have in common.
    by_time = {}
    for limit in (1, 2, 3, 5, 11):
        out = sample_table(long_fst, paths, "top.clk", 0, 440007, limit=limit)
        for row in out["cycles"]:
            t = row["time_units"]
            if t in by_time:
                assert row["values"] == by_time[t]
            by_time[t] = row["values"]
        assert out["count"] == limit and out["total_available"] == N


def test_large_limit_returns_every_long_wave_edge(long_fst):
    result = sample_table(long_fst, ["top.q"], "top.clk", 0, 440007, limit=N + 1)
    assert result["count"] == N and "sampled" not in result
    assert all(t == 2 * k + 1 and value == k
               for k, (t, value) in enumerate(pairs(result), 1))


def test_million_cycles_exceed_both_old_scan_budgets(tmp_path):
    n = 1_000_003

    def changes():
        yield 0, "clk", "0"
        yield 0, "q", "0" * 32
        for k in range(1, n + 1):
            yield 2 * k, "clk", "0"
            yield 2 * k, "q", f"{k:032b}"
            yield 2 * k + 1, "clk", "1"

    path = write_fst(str(tmp_path / "million.fst"), {"clk": 1, "q": 32},
                     changes(), timescale_exp=-9)
    src = FstSource(path)
    try:
        result = sample_table(src, ["top.q"], "top.clk", 0, 2 * n + 1, limit=3)
        assert pairs(result) == [(3, 1), (1000005, 500002), (2000007, 1000003)]
        assert result["total_available"] == n
    finally:
        src.close()


@pytest.fixture
def small_fst(tmp_path):
    path = write_fst(str(tmp_path / "small.fst"),
        {"clk [3:0]": 4, "q": 8, "held": 4}, [
            (0, "clk [3:0]", "0000"), (0, "q", "00000000"),
            (0, "held", "1010"),
            (2, "q", "00000001"),
            (3, "clk [3:0]", "0010"), (3, "q", "00000010"),
            (4, "clk [3:0]", "1010"),  # another bit changes
            (5, "clk [3:0]", "1000"),
            (6, "clk [3:0]", "10x0"),
            (7, "clk [3:0]", "1010"),  # x->1 is not a clean edge
            (8, "clk [3:0]", "10z0"),
            (9, "clk [3:0]", "1010"),  # z->1 is not a clean edge
            (10, "clk [3:0]", "1000"),
            (12, "clk [3:0]", "1010"), (12, "q", "x1z0x1z0"),
            (14, "clk [3:0]", "1000"),
            (16, "clk [3:0]", "1010"), (16, "q", "00000111"),
            (18, "clk [3:0]", "1000"), (20, "clk [3:0]", "1010"),
        ], timescale_exp=-9)
    src = FstSource(path)
    try:
        yield src
    finally:
        src.close()


@pytest.mark.parametrize("start,end,edge,expected", [
    (0, 20, "rising", [3, 12, 16, 20]),
    (2, 12, "rising", [3, 12]),  # start between clock changes
    (3, 12, "rising", [12]),     # start excluded, end included
    (3, 11, "rising", []),
    (12, 12, "rising", []),
    (0, 20, "falling", [5, 10, 14, 18]),
    (4, 10, "falling", [5, 10]),
    (0, 20, "both", [3, 5, 10, 12, 14, 16, 18, 20]),
    (21, 30, "both", []),
])
def test_boundaries_and_four_state_clock(small_fst, start, end, edge, expected):
    assert clock_edges(small_fst, "top.clk[1]", start, end, edge) == expected
    result = sample_table(small_fst, ["top.q"], "top.clk[1]",
                          start, end, edge, limit=100)
    assert [r["time_units"] for r in result["cycles"]] == expected
    assert result["count"] == len(expected) and "sampled" not in result


@pytest.mark.parametrize("limit,expected", [
    (1, [3]), (2, [3, 20]), (3, [3, 16, 20]), (4, [3, 12, 16, 20]),
    (100, [3, 12, 16, 20]),
])
def test_limits_same_timestamp_and_four_state_values(small_fst, limit, expected):
    result = sample_table(small_fst, ["top.q", "top.q[3:0]", "top.held"],
                          "top.clk[1]", 0, 20, limit=limit)
    golden = {3: "00000010", 12: "x1z0x1z0", 16: "00000111", 20: "00000111"}
    assert [r["time_units"] for r in result["cycles"]] == expected
    for row in result["cycles"]:
        t, vals = row["time_units"], row["values"]
        assert vals == {"top.q": golden[t], "top.q[3:0]": golden[t][-4:],
                        "top.held": "1010"}
        assert vals["top.q"] == small_fst.value_at("top.q", t)["value"]


@pytest.mark.parametrize("total,limit,expected", [
    (0, 3, []), (3, 10, [0, 1, 2]), (4, 1, [0]),
    (4, 3, [0, 2, 3]), (6, 3, [0, 2, 5]), (N, 3, [0, 110001, 220002]),
])
def test_shared_downsample_rule(total, limit, expected):
    assert list(sample_indices(total, limit)) == expected
    assert downsample(list(range(total)), limit)["values"] == expected


@pytest.mark.parametrize("failure", [RuntimeError("callback failed"),
    MemoryError("allocation failed"), TimeoutError("deadline"),
    asyncio.CancelledError("cancelled")])
def test_callback_failure_propagates_and_reader_recovers(small_fst, failure):
    seen = []

    def fail(t, handle, raw):
        seen.append(t)
        raise failure

    with pytest.raises(type(failure)) as caught:
        small_fst._scan_values([small_fst.signals["top.q"]], 2, 12, fail)
    assert caught.value is failure and len(seen) == 1
    assert all(not lib.fstReaderGetFacProcessMask(small_fst._ctx, h)
               for h in small_fst._by_handle)
    # A different thread verifies lock release as well as a usable reader.
    with ThreadPoolExecutor(1) as pool:
        assert pool.submit(clock_edges, small_fst, "top.clk[1]", 0).result(5) == [3, 12, 16, 20]
    rows = small_fst._iter_values(small_fst.signals["top.q"], 0, 20, 100)
    assert rows[0][0] == 0 and rows[-1] == (16, "00000111")
    assert small_fst.value_at("top.q", 20)["value"] == "00000111"


@pytest.mark.parametrize("mode", ["status", "exception"])
def test_native_failure_propagates_and_reader_recovers(small_fst, monkeypatch, mode):
    def fail(*args):
        if mode == "exception":
            raise OSError("native read failed")
        return 0

    with monkeypatch.context() as patch:
        patch.setattr(pylibfst, "fstReaderIterBlocks2", fail)
        with pytest.raises(OSError):
            sample_table(small_fst, ["top.q"], "top.clk[1]", 2, 12)
    assert clock_edges(small_fst, "top.clk[1]", 0) == [3, 12, 16, 20]


@pytest.mark.parametrize("phase", ["selection", "point"])
def test_no_successful_prefix_on_failure(small_fst, monkeypatch, phase):
    scan, point = small_fst._scan_values, small_fst.value_at
    passes, in_scan, reads = 0, False, 0

    def scanning(signals, start, end, consume):
        nonlocal passes, in_scan
        passes += 1
        in_scan = True

        def visit(t, h, raw):
            consume(t, h, raw)
            if phase == "selection" and passes == 2 and t >= 12:
                raise MemoryError("selection failed after a retained edge")
        try:
            return scan(signals, start, end, visit)
        finally:
            in_scan = False

    def reading(path, time):
        nonlocal reads
        assert not in_scan  # no random queries from native iteration callbacks
        if path == "top.q":
            reads += 1
            if phase == "point" and reads == 2:
                raise MemoryError("point read failed after an output row")
        return point(path, time)

    with monkeypatch.context() as patch:
        patch.setattr(small_fst, "_scan_values", scanning)
        patch.setattr(small_fst, "value_at", reading)
        with pytest.raises(MemoryError):
            sample_table(small_fst, ["top.q"], "top.clk[1]", 0, 20)
    assert clock_edges(small_fst, "top.clk[1]", 0) == [3, 12, 16, 20]


def test_diff_sees_late_divergence_with_complete_clock(long_wave, tmp_path):
    # Few data changes, but far more than 200k clock changes. The old clock
    # prefix made this diff falsely declare complete agreement.
    def changes(bug):
        yield 0, "clk", "0"
        yield 0, "held", "0"
        for k in range(1, N + 1):
            yield 2 * k, "clk", "0"
            if bug and k == 220001:
                yield 2 * k, "held", "1"
            yield 2 * k + 1, "clk", "1"

    paths = [write_fst(str(tmp_path / f"{bug}.fst"), {"clk": 1, "held": 1},
                        changes(bug), timescale_exp=-9) for bug in (False, True)]
    result = diff_waveforms(paths, signals=["top.held"], clock="top.clk")
    assert result["coverage"] == "complete"
    assert result["first_divergence"]["time_units"] == 440003
    assert result["diverging_signals"][0]["value_a"] == "0"
    assert result["diverging_signals"][0]["value_b"] == "1"
    identical = diff_waveforms([long_wave, long_wave], signals=["top.q"], clock="top.clk")
    assert identical["coverage"] == "truncated"  # data budget still belongs to diff
    assert identical["first_divergence"] is None


def test_diff_does_not_extrapolate_truncated_data(tmp_path, monkeypatch):
    import importlib
    diff = importlib.import_module("wave_mcp.diff")
    monkeypatch.setattr(diff, "_MAX_VALUES_PER_SIGNAL", 2)
    clocks = [(0, "clk", "0"), (1, "clk", "1"), (3, "clk", "0"),
              (5, "clk", "1"), (7, "clk", "0"), (9, "clk", "1")]
    paths = []
    for glitch in (False, True):
        changes = clocks + [(0, "q", "0")]
        if glitch:
            changes += [(2, "q", "1"), (3, "q", "0"),
                        (6, "q", "1"), (7, "q", "0")]
        paths.append(write_fst(str(tmp_path / f"{glitch}.fst"),
            {"clk": 1, "q": 1}, sorted(changes, key=lambda r: r[0]),
            timescale_exp=-9))
    result = diff_waveforms(paths, signals=["top.q"], clock="top.clk")
    assert result["coverage"] == "truncated"
    assert result["first_divergence"] is None


def test_diff_start_between_clock_changes(tmp_path):
    paths = [write_fst(str(tmp_path / f"{v}.fst"), {"clk": 1, "q": 1},
        [(0, "clk", "0"), (0, "q", "0"), (3, "clk", "1"), (3, "q", v)],
        timescale_exp=-9) for v in ("0", "1")]
    result = diff_waveforms(paths, signals=["top.q"], clock="top.clk", after="2ns")
    assert result["coverage"] == "complete"
    assert result["first_divergence"]["time_units"] == 3


def test_real_mcp_prepare_sample_and_point_queries(long_wave, tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        params = StdioServerParameters(command=sys.executable,
            args=["-m", "wave_mcp.server"],
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            env={"WAVE_MCP_CACHE_ROOT": str(tmp_path / "cache")})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()

                async def call(name, args):
                    result = await client.call_tool(name, args)
                    assert not result.is_error, result
                    return result.structured_content

                prep = await call("prepare_session", {"wave_path": long_wave,
                    "out_dir": str(tmp_path / "session")})
                sid = prep["session_id"]
                common = {"paths": ["top.q"], "clock": "top.clk", "session_id": sid}
                narrow = await call("sample_at_clock", {**common,
                    "start": "440002ns", "end": "440006ns", "limit": 20})
                assert pairs(narrow) == [(440003, 220001), (440005, 220002)]
                wide = await call("sample_at_clock", {**common,
                    "start": "0ns", "end": "440007ns", "limit": 3})
                assert pairs(wide) == [(3, 1), (220005, 110002), (440007, 220003)]
                assert wide["total_available"] == N
                for t, k in pairs(narrow):
                    point = await call("signal_values", {"paths": ["top.q"],
                        "time": f"{t}ns", "session_id": sid})
                    assert point["signals"][0]["time_units"] == t
                    assert int(point["signals"][0]["value"], 2) == k
                await call("close_session", {"session_id": sid})

    asyncio.run(run())
