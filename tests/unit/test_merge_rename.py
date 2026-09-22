#!/usr/bin/env python3
"""v0.3.0 tool-merge and rename unit suite.

Covers the three merged tools (``signal_values`` / ``find_instances`` /
``files``), the ``limit`` downsampling contract, and the rename hint tables that
replace an alias shim.

Merged behaviour is cross-checked against the underlying engine
(``value_at`` / ``values_between`` / ``instances_by_module``), not against the
merged code itself.

Run directly or via tests/run_regression.py.
"""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)

from fstgen import write_fst                          # noqa: E402
from wave_mcp.session import open_session             # noqa: E402
from wave_mcp.analysis import sampling                # noqa: E402
from wave_mcp import server as srv                    # noqa: E402
from session_client import bound             # noqa: E402

PASSED, FAILED = [], []

SAMPLE = os.path.join(HERE, "..", "..", "examples", "sample", "session")
CLK = "top_tb.clk"
RST = "top_tb.rst_n"
CNT = "top_tb.u_counter.count"


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSED if cond else FAILED).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  ({detail})" if detail and not cond else ""))


def _entry(out: dict, path: str) -> dict:
    for e in out.get("signals", []):
        if e.get("path") == path:
            return e
    return {}


def _stage_retired_gone() -> None:
    print("== retired names are really gone ==")
    for name in ("signal_values_in_range", "signal_value_at",
                 "instances_of_module", "instances_of_module_matching",
                 "list_child_instances", "list_files", "find_files",
                 "modules_in_file"):
        check(f"{name} removed", not hasattr(srv, name))
    for name in ("signal_values", "find_instances", "files"):
        check(f"{name} present", hasattr(srv, name))


def _stage_value_shapes() -> None:
    print("== signal_values: three shapes vs the engine ==")
    srv = bound(SAMPLE)
    f = open_session(SAMPLE).fst

    # point read
    out = srv.signal_values(CLK, time="25ns")
    e = _entry(out, CLK)
    check("point: matches value_at", e.get("value") == f.value_at(CLK, 25)["value"],
          str(e))
    check("point: echoes the requested time", out.get("time") == "25ns", str(out))

    # window read
    out = srv.signal_values([CLK], start="10ns", end="30ns")
    got = [v["time_units"] for v in _entry(out, CLK)["values"]]
    want = [r["time_units"] for r in f.values_between(CLK, 10, 30, 10000)]
    check("window: identical to values_between", got == want, f"{got} vs {want}")
    check("window: echoed back", out["window"] == {"start": "10ns", "end": "30ns"},
          str(out.get("window")))

    # whole dump
    out = srv.signal_values(CLK)
    n_all = _entry(out, CLK)["count"]
    check("whole dump: matches all_values",
          n_all == len(f.all_values(CLK, 10000)), str(n_all))

    # a bare string and a one-element list agree
    a = srv.signal_values(CLK)
    b = srv.signal_values([CLK])
    check("string path == single-element list",
          _entry(a, CLK)["count"] == _entry(b, CLK)["count"])


def _stage_value_batch_and_errors() -> None:
    print("== signal_values: batching and bad input ==")
    srv = bound(SAMPLE)
    f = open_session(SAMPLE).fst

    out = srv.signal_values([CLK, RST, CNT])
    check("batch returns one entry per path in order",
          [e["path"] for e in out["signals"]] == [CLK, RST, CNT],
          str([e["path"] for e in out["signals"]]))
    for p in (CLK, RST, CNT):
        want = len(f.values_between(p, f.start_time, f.end_time, 10000))
        check(f"batch: {p} count matches engine",
              _entry(out, p)["count"] == want,
              f"{_entry(out, p)['count']} vs {want}")

    out = srv.signal_values([CNT, "top_tb.ghost"])
    check("one bad path does not lose the batch",
          _entry(out, CNT).get("count") and
          "error" in _entry(out, "top_tb.ghost"), str(out))

    bad = srv.signal_values([])
    check("empty paths -> invalid_argument naming paths",
          bad.get("error_type") == "invalid_argument"
          and bad.get("parameter") == "paths", str(bad))

    bad = srv.signal_values(CLK, time="nonsense")
    check("bad time -> invalid_argument naming time",
          bad.get("error_type") == "invalid_argument"
          and bad.get("parameter") == "time", str(bad))

    bad = srv.signal_values(CLK, start="30ns", end="10ns")
    check("reversed window -> invalid_argument naming end",
          bad.get("error_type") == "invalid_argument"
          and bad.get("parameter") == "end", str(bad))


def _stage_limit(tmp: str) -> None:
    print("== limit: downsample, never truncate ==")
    path = os.path.join(tmp, "many.fst")
    write_fst(path, {"s": 8},
              [(i * 10, "s", format(i % 256, "08b")) for i in range(500)],
              timescale_exp=-12)
    srv = bound(SAMPLE)
    f = open_session(SAMPLE).fst

    # unit-level contract on the sampler itself
    rows = [{"time_units": i} for i in range(500)]
    r = sampling.downsample(rows, 10)
    check("sampler: honours the target size", r["count"] <= 10, str(r["count"]))
    check("sampler: keeps the first row",
          r["values"][0]["time_units"] == 0)
    check("sampler: keeps the last row",
          r["values"][-1]["time_units"] == 499)
    check("sampler: reports total_available",
          r["total_available"] == 500 and r["sampled"] is True, str(r))
    check("sampler: strictly increasing (no duplicates)",
          all(a["time_units"] < b["time_units"]
              for a, b in zip(r["values"], r["values"][1:])))
    clean = sampling.downsample(rows[:5], 10)
    check("sampler: no bookkeeping when everything fits",
          "sampled" not in clean and clean["count"] == 5, str(clean))
    check("sampler: limit=1 yields one row",
          sampling.downsample(rows, 1)["count"] == 1)

    # through the tool
    out = srv.signal_values(CLK, limit=3)
    e = _entry(out, CLK)
    total = len(f.all_values(CLK, 10000))
    check("tool: sampled flag set when over limit",
          e.get("sampled") is True and e["count"] == 3, str(e))
    check("tool: total_available is the true count",
          e.get("total_available") == total, f"{e.get('total_available')} vs {total}")
    check("tool: endpoints preserved under sampling",
          e["values"][0]["time_units"] == 0
          and e["values"][-1]["time_units"] == 50, str(e["values"]))
    check("tool: sample_rate reported",
          0 < e.get("sample_rate", 0) <= 1, str(e.get("sample_rate")))

    out = srv.signal_values(CLK, limit=10_000)
    check("tool: no sampling when under limit",
          "sampled" not in _entry(out, CLK), str(_entry(out, CLK).keys()))


def _stage_find_instances() -> None:
    print("== find_instances ==")
    srv = bound(SAMPLE)
    f = open_session(SAMPLE).fst

    out = srv.find_instances(module="counter")
    check("by module: matches instances_by_module",
          out["instances"] == f.instances_by_module("counter"), str(out))

    out = srv.find_instances(module="counter", name_contains="u_c")
    check("leaf filter keeps a real match", out["count"] == 1, str(out))

    out = srv.find_instances(module="counter", name_contains="top")
    check("leaf filter ignores the parent scope name (was a full-path match)",
          out["count"] == 0, str(out))

    out = srv.find_instances(under="")
    check("under='' walks from the top",
          [r["full_path"] for r in out["instances"]] == ["top_tb"], str(out))

    out = srv.find_instances(under="top_tb")
    check("under=<scope> lists its children",
          [r["full_path"] for r in out["instances"]] == ["top_tb.u_counter"],
          str(out))

    deep = srv.find_instances(under="", max_depth=2)
    check("levels descends further", deep["count"] >= 2, str(deep["count"]))

    out = srv.find_instances(under="", max_depth=2, name_contains="u_counter")
    check("name_contains narrows a tree walk", out["count"] == 1, str(out))

    bad = srv.find_instances()
    check("bare call refuses instead of dumping everything",
          bad.get("error_type") == "invalid_argument", str(bad))


def _stage_files() -> None:
    print("== files ==")
    srv = bound(SAMPLE)
    s = open_session(SAMPLE)

    out = srv.files()
    check("no args lists every source file",
          out["count"] == len(s.rtl.all_files()), str(out["count"]))

    out = srv.files(name="counter.sv")
    check("name finds the file",
          out["count"] == 1 and out["files"][0].endswith("counter.sv"), str(out))

    out = srv.files(name="counter.sv", exact=True)
    check("exact still matches the exact short name", out["count"] == 1, str(out))

    target = srv.files(name="counter.sv")["files"][0]
    out = srv.files(modules_of=target)
    check("modules_of reads the file's modules",
          out["modules"] == ["counter"], str(out))
    check("modules_of echoes source_file, not path",
          out.get("source_file") == target and "path" not in out, str(out.keys()))


def _stage_renames() -> None:
    print("== renamed parameters accepted ==")
    srv = bound(SAMPLE)
    check("scope_info(path=)", srv.scope_info(path="top_tb.u_counter")
          .get("module_type") == "counter")
    check("signal_info(path=)", srv.signal_info(path=CNT).get("width") == 8)
    check("list_signals(path=)",
          srv.list_signals(path="top_tb.u_counter").get("count") == 4)
    check("signal_drivers(path=)",
          srv.signal_drivers(path=CNT).get("available") is True)
    check("signal_fanin(path=)",
          srv.signal_fanin(path=CNT).get("available") is not None)
    check("trace_value(path=, time=)",
          srv.trace_value(path=CNT, time="30ns").get("available") is True)
    check("trace_x(path=, time=)",
          srv.trace_x(path=CNT, time="30ns").get("available") is not None)
    check("active_drivers(path=, time=)",
          srv.active_drivers(path=CNT, time="30ns").get("available") is True)

    err = srv.active_drivers(path=CNT, time="bogus")
    check("active_drivers bad time names the NEW parameter",
          err.get("parameter") == "time", str(err))
    err = srv.trace_value(path=CNT, time="bogus")
    check("trace_value bad time names the NEW parameter",
          err.get("parameter") == "time", str(err))

    print("== old names are rejected loudly ==")
    for call, label in (
        (lambda: srv.signal_values(full_path=CLK), "signal_values(full_path=)"),
        (lambda: srv.scope_info(scope_full_path="top_tb"), "scope_info(scope_full_path=)"),
        (lambda: srv.trace_value(signal_path=CNT, time_point="30ns"),
         "trace_value(signal_path=, time_point=)"),
    ):
        try:
            call()
            check(f"{label} raises TypeError", False, "no exception")
        except TypeError:
            check(f"{label} raises TypeError", True)


def _stage_hints() -> None:
    print("== rename hint tables (error path only) ==")
    srv = bound(SAMPLE)
    h = srv.rename_hint("signal_value_at")
    check("retired tool hint names the replacement",
          h and h["error_type"] == "retired_tool"
          and "signal_values" in h["use_instead"], str(h))
    check("hint covers every retired tool",
          all(srv.rename_hint(n) for n in
              ("signal_values_in_range", "signal_value_at",
               "instances_of_module", "instances_of_module_matching",
               "list_child_instances", "list_files", "find_files",
               "modules_in_file")))
    check("a live tool is not reported as retired",
          srv.rename_hint("signal_values") is None)
    check("an unknown name is not reported as retired",
          srv.rename_hint("no_such_tool") is None)

    # Each of these must *raise*: a silent dict reply would skip the assertion
    # and the stage would look green without testing anything.
    def expect_type_error(label, call, verify):
        try:
            call()
        except TypeError as exc:
            check(label, verify(exc), str(exc))
        else:
            check(label, False, "no TypeError raised")

    expect_type_error(
        "param hint maps full_path -> paths on a batch tool",
        lambda: srv.signal_values(full_path=CLK),
        lambda exc: (srv.param_rename_hint(exc, "signal_values") or {})
        .get("use_instead") == "paths")
    expect_type_error(
        "param hint maps full_path -> path on a single-signal tool",
        lambda: srv.signal_drivers(full_path=CNT),
        lambda exc: (srv.param_rename_hint(exc, "signal_drivers") or {})
        .get("use_instead") == "path")
    expect_type_error(
        "an unrelated TypeError yields no bogus hint",
        lambda: srv.signal_values(paths=CLK, nonsense=1),
        lambda exc: srv.param_rename_hint(exc, "signal_values") is None)

    check("renamed_param lookup exposes the table",
          srv.renamed_param("time_point") == "time"
          and srv.renamed_param("path") is None)


def _stage_schema() -> None:
    print("== MCP schema accepts both a string and a list ==")
    import asyncio
    tools = {t.name: t for t in asyncio.run(srv.mcp.list_tools())}
    check("tool count is 37", len(tools) == 37, str(len(tools)))
    for name in ("signal_values", "signal_activity"):
        prop = tools[name].input_schema["properties"]["paths"]
        # A plain List[str] annotation makes the SDK reject a bare string over
        # the wire even though the function handles it. The schema must offer
        # both forms, or the documented "one path or a list" is a lie on the
        # protocol layer (in-process calls would still pass).
        variants = prop.get("anyOf") or prop.get("oneOf") or [prop]
        kinds = {v.get("type") for v in variants}
        check(f"{name}: paths accepts a string and an array",
              {"string", "array"} <= kinds, str(prop))
    for name in ("signal_drivers", "trace_value", "scope_info",
                 "signal_info", "list_signals"):
        props = set(tools[name].input_schema.get("properties", {}))
        check(f"{name}: exposes path (not a retired name)",
              "path" in props, str(sorted(props)))
    check("signal_values exposes time/start/end/limit",
          {"time", "start", "end", "limit"}
          <= set(tools["signal_values"].input_schema["properties"]),
          str(sorted(tools["signal_values"].input_schema["properties"])))
    check("files exposes name/exact/modules_of",
          {"name", "exact", "modules_of"}
          <= set(tools["files"].input_schema["properties"]))
    check("find_instances exposes module/under/name_contains",
          {"module", "under", "name_contains"}
          <= set(tools["find_instances"].input_schema["properties"]))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="wave_merge_test_")
    print(f"== v0.3.0 merge & rename suite (workdir {tmp}) ==")
    _stage_retired_gone()
    _stage_value_shapes()
    _stage_value_batch_and_errors()
    _stage_limit(tmp)
    _stage_find_instances()
    _stage_files()
    _stage_renames()
    _stage_hints()
    _stage_schema()
    print(f"\n  merge suite: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("  failed:", FAILED)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
