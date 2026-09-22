#!/usr/bin/env python3
"""find_time_windows unit suite: interval correctness + independent re-check.

Every reported interval is re-verified against ``value_at`` at its own
boundaries, so a window is only accepted if the raw values actually satisfy the
condition at the start and fail at the close.

Run directly or via tests/run_regression.py.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)

from wave_mcp import timeutil                        # noqa: E402
from wave_mcp.session import open_session            # noqa: E402
from wave_mcp.analysis import find_time_windows      # noqa: E402
from wave_mcp import server as srv                   # noqa: E402
from session_client import bound             # noqa: E402

PASSED, FAILED = [], []

SAMPLE = os.path.join(HERE, "..", "..", "examples", "sample", "session")
RST = "top_tb.rst_n"
CNT = "top_tb.u_counter.count"


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSED if cond else FAILED).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  ({detail})" if detail and not cond else ""))


def sig(name: str) -> dict:
    return {"k": "sig", "name": name}


def lit(text: str) -> dict:
    return {"k": "const", "lit": text}


def binop(op: str, left: dict, right: dict) -> dict:
    return {"k": "bin", "op": op, "l": left, "r": right}


# rst_n == 1'b1
RST_HIGH = binop("Equality", sig(RST), lit("1'b1"))
# count > 8'd1
CNT_GT1 = binop("GreaterThan", sig(CNT), lit("8'd1"))
# rst_n && count > 1
BOTH = binop("LogicalAnd", RST_HIGH, CNT_GT1)
# (count > 1) || (rst_n == 0)
TWO_WINDOWS = binop("LogicalOr", CNT_GT1,
                    binop("Equality", sig(RST), lit("1'b0")))


def _int_or_none(bits):
    if bits is None or any(c in "xz" for c in bits.lower()):
        return None
    return int(bits, 2)


def both_true(f, t: int) -> bool:
    """Semantics of ``BOTH``, read straight from value_at (independent)."""
    rst = _int_or_none(f.value_at(RST, t)["value"])
    cnt = _int_or_none(f.value_at(CNT, t)["value"])
    if rst is None or cnt is None:
        return False            # undecidable counts as not-true
    return rst == 1 and cnt > 1


def _units(f, text: str) -> int:
    return timeutil.time_to_fst_units(text, f.timescale_exp)


def _stage_basic_windows() -> None:
    print("== single window, re-verified at its boundaries ==")
    f = open_session(SAMPLE).fst
    res = find_time_windows(f, BOTH, 0, 50)
    check("one window", res["total_hits"] == 1, str(res))
    w = res["windows"][0]
    check("window is 30ns..50ns", w["start"] == "30ns" and w["end"] == "50ns",
          str(w))
    check("duration 20ns", w["duration"] == "20ns", str(w))
    check("closed by a value change, not open ended",
          w["open_ended"] is False, str(w))
    check("signals reported", res["signals"] == [RST, CNT], str(res["signals"]))

    a, b = _units(f, w["start"]), _units(f, w["end"])
    check("raw values satisfy the predicate at the window start",
          both_true(f, a), f"at {w['start']}")
    check("raw values fail the predicate at the window close",
          not both_true(f, b), f"at {w['end']}")
    check("raw values still satisfy it just before the close",
          both_true(f, b - 1), f"at {b - 1}")
    check("nothing reported before the start",
          not any(both_true(f, t) for t in range(0, a)), "t < start")


def _stage_min_duration() -> None:
    print("== min_duration filter ==")
    f = open_session(SAMPLE).fst
    keep = find_time_windows(f, BOTH, 0, 50, min_duration_units=15)
    check("20ns window kept by a 15ns threshold", keep["total_hits"] == 1,
          str(keep))
    drop = find_time_windows(f, BOTH, 0, 50, min_duration_units=25)
    check("20ns window dropped by a 25ns threshold", drop["total_hits"] == 0,
          str(drop))
    edge = find_time_windows(f, BOTH, 0, 50, min_duration_units=20)
    check("threshold equal to the duration keeps it (inclusive)",
          edge["total_hits"] == 1, str(edge))


def _stage_open_ended() -> None:
    print("== open-ended windows ==")
    f = open_session(SAMPLE).fst
    res = find_time_windows(f, RST_HIGH, 0, 40)
    w = res["windows"][0]
    check("still true at the window end -> open_ended",
          w["open_ended"] is True and w["start"] == "10ns"
          and w["end"] == "40ns", str(w))
    check("re-verified true at the reported end",
          _int_or_none(f.value_at(RST, _units(f, w["end"]))["value"]) == 1)

    # becomes true only at the very last instant: a zero-length interval is
    # not a window
    res = find_time_windows(f, binop("Equality",
                                     sig("top_tb.u_counter.overflow"),
                                     lit("1'b1")), 0, 50)
    check("zero-length interval at the dump end is not reported",
          res["total_hits"] == 0, str(res))


def _stage_multiple_windows() -> None:
    print("== multiple windows ==")
    f = open_session(SAMPLE).fst
    res = find_time_windows(f, TWO_WINDOWS, 0, 50)
    check("two windows found", res["total_hits"] == 2, str(res))
    got = [(w["start"], w["end"], w["duration"]) for w in res["windows"]]
    check("windows are 0..10 and 30..50",
          got == [("0ns", "10ns", "10ns"), ("30ns", "50ns", "20ns")], str(got))
    check("windows are ordered in time",
          res["windows"][0]["start"] == "0ns", str(got))


def _stage_undecidable() -> None:
    print("== undecidable (x) accounting ==")
    f = open_session(SAMPLE).fst
    # count is x for the whole 0..10 stretch, so the comparison cannot be decided
    res = find_time_windows(f, binop("Equality", sig(CNT), lit("8'd0")), 0, 10)
    check("undecidable time is reported, not silently a miss",
          res["undecidable_units"] == 10, str(res))
    check("undecidable time is also formatted",
          res["undecidable_time"] == "10ns", str(res))
    check("no window claimed while undecidable", res["total_hits"] == 0,
          str(res))


def _stage_truncation() -> None:
    print("== hit cap ==")
    f = open_session(SAMPLE).fst
    res = find_time_windows(f, TWO_WINDOWS, 0, 50, max_hits=1)
    check("capped list keeps the first window", len(res["windows"]) == 1
          and res["windows"][0]["start"] == "0ns", str(res))
    check("total_hits stays exact under the cap", res["total_hits"] == 2,
          str(res))
    check("truncated flag set", res["truncated"] is True, str(res))
    full = find_time_windows(f, TWO_WINDOWS, 0, 50)
    check("no truncated flag when everything fits",
          full["truncated"] is False, str(full))


def _stage_errors_and_tool() -> None:
    print("== errors and the MCP tool layer ==")
    f = open_session(SAMPLE).fst

    res = find_time_windows(f, sig("top_tb.nope"), 0, 50)
    check("unknown signal in the predicate -> structured error",
          res.get("error_type") == "signal_not_found"
          and res.get("missing") == ["top_tb.nope"], str(res))

    res = find_time_windows(f, {"k": "const", "lit": "1'b1"}, 0, 50)
    check("predicate with no signal -> structured error",
          res.get("error_type") == "empty_predicate", str(res))

    srv = bound(SAMPLE)
    out = srv.find_time_windows(BOTH, "min", "max")
    check("tool: mirrors the analysis result",
          out["total_hits"] == 1 and out["windows"][0]["start"] == "30ns",
          str(out))
    check("tool: fingerprint attached", "_fp" in out, str(out.keys()))

    out = srv.find_time_windows(RST_HIGH, "0ns", "40ns", min_duration="5ns")
    check("tool: min_duration as a time string",
          out["total_hits"] == 1 and out["windows"][0]["open_ended"] is True,
          str(out))

    bad = srv.find_time_windows(BOTH, "min", "max", min_duration="nonsense")
    check("tool: bad min_duration -> invalid_argument naming the parameter",
          bad.get("error_type") == "invalid_argument"
          and bad.get("parameter") == "min_duration", str(bad))

    bad = srv.find_time_windows("not-an-expression", "min", "max")
    check("tool: non-dict predicate -> invalid_argument with a hint",
          bad.get("error_type") == "invalid_argument"
          and bad.get("parameter") == "predicate"
          and "hint" in bad, str(bad))


def main() -> int:
    print("== find_time_windows unit suite ==")
    _stage_basic_windows()
    _stage_min_duration()
    _stage_open_ended()
    _stage_multiple_windows()
    _stage_undecidable()
    _stage_truncation()
    _stage_errors_and_tool()
    print(f"\n  predicate suite: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("  failed:", FAILED)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
