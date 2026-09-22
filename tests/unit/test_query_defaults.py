#!/usr/bin/env python3
"""Query-defaults unit suite (v0.3.0 S1).

Covers the renamed default-values feature plus the boundaries that decide
whether a caller can rely on it:

- the three tools are named ``query_defaults_*`` and the old ``cursor_*`` names
  are gone, with a hint that says "renamed" rather than "merged";
- ``_defaults_used`` appears exactly when a default was drawn, never otherwise,
  so an agent can always tell what range the data covers;
- ``"min"`` / ``"max"`` stay an explicit request and are not mistaken for
  "absent", or a caller could not widen back out while defaults are set;
- a multi-signal default set supplies nothing to single-signal tools, rather
  than picking one arbitrarily;
- an update that fails validation changes *nothing*, and ``revision`` advances
  only on a successful publish;
- ``defaults_revision`` refuses an update built on a state someone else changed;
- a static session accepts default signals but not a default time window;
- the stored state is data-plane coordinates only.

Run directly or via tests/run_regression.py.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))

from wave_mcp import server as srv                    # noqa: E402
from session_client import bound                       # noqa: E402
from wave_mcp.session import QueryDefaults, open_session  # noqa: E402

PASSED, FAILED = [], []

SAMPLE = os.path.join(HERE, "..", "..", "examples", "sample", "session")
CLK = "top_tb.clk"
RST = "top_tb.rst_n"
CNT = "top_tb.u_counter.count"
STATIC_TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "..", "tmp_s1_static")


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSED if cond else FAILED).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  ({detail})" if detail and not cond else ""))


def _paths(out: dict) -> list:
    return [e.get("path") for e in out.get("signals", [])]


#: The session-bound client the helpers work through, set by ``_bind`` so a
#: stage never depends on "the only open session".
CUR = None


def _bind():
    """Open a fresh session for a stage and point the helpers at it.

    A new session per stage is deliberate: it is the same thing a second agent
    would get, so anything that leaked between sessions would show up here.
    """
    global CUR
    CUR = bound(SAMPLE)
    return CUR


def _state() -> dict:
    return CUR.query_defaults_get()["query_defaults"]


def _stage_naming() -> None:
    print("== the tools are query_defaults_*, and only that ==")
    tools = {t.name: t for t in asyncio.run(srv.mcp.list_tools())}
    for name in ("query_defaults_set", "query_defaults_get",
                 "query_defaults_clear"):
        check(f"{name} is registered", name in tools)
    for name in ("cursor_set", "cursor_get", "cursor_clear"):
        check(f"{name} is gone", name not in tools)
        check(f"{name} has no attribute either", not hasattr(srv, name))
    check("tool count is unchanged at 37", len(tools) == 37, str(len(tools)))

    # cursor_* never shipped, so there is nothing to explain: the names are
    # simply unknown, exactly like any other typo.
    check("pre-release names get no retirement hint",
          all(srv.rename_hint(n) is None for n in
              ("cursor_set", "cursor_get", "cursor_clear")))
    check("a live name is not reported as retired",
          srv.rename_hint("query_defaults_set") is None)
    check("merged tools that did ship still say merged",
          "removed" in srv.rename_hint("list_files")["error"])

    props = set(tools["query_defaults_set"].input_schema.get("properties", {}))
    check("query_defaults_set exposes coordinates, session and revision",
          props == {"paths", "start", "end", "session_id",
                    "defaults_revision"}, str(sorted(props)))
    check("query_defaults_clear exposes the revision check",
          set(tools["query_defaults_clear"].input_schema
              .get("properties", {})) == {"defaults_revision", "session_id"})


def _stage_tools() -> None:
    print("== storing and reading defaults ==")
    srv = _bind()

    out = srv.query_defaults_get()
    check("a fresh state is not set",
          out["query_defaults"]["is_set"] is False, str(out))

    out = srv.query_defaults_set(paths=[CLK, RST])
    check("paths are stored",
          out["query_defaults"]["paths"] == [CLK, RST], str(out))
    check("a paths-only state has no window",
          out["query_defaults"]["start"] is None
          and out["query_defaults"]["end"] is None)

    out = srv.query_defaults_set(start="10ns", end="30ns")
    check("a window is stored in FST units",
          out["query_defaults"]["start"] == 10
          and out["query_defaults"]["end"] == 30, str(out))
    check("the window is echoed in readable form",
          out["query_defaults"]["start_time"] == "10ns"
          and out["query_defaults"]["end_time"] == "30ns", str(out))
    check("setting the window leaves the paths alone",
          out["query_defaults"]["paths"] == [CLK, RST], str(out))

    check("query_defaults_get reports the same state",
          _state() == out["query_defaults"])

    out = srv.query_defaults_set(paths=[])
    check("an empty list drops the paths",
          out["query_defaults"]["paths"] == []
          and out["query_defaults"]["start"] == 10, str(out))
    out = srv.query_defaults_set(paths=[RST])
    out = srv.query_defaults_set(paths="")
    check("a blank string drops the paths too, rather than storing an empty name",
          out["query_defaults"]["paths"] == [], str(out))
    out = srv.query_defaults_set(start="")
    check("an empty string drops one bound",
          out["query_defaults"]["start"] is None
          and out["query_defaults"]["end"] == 30, str(out))

    out = srv.query_defaults_set(paths=[CLK], start="min", end="max")
    f = open_session(SAMPLE).fst
    check("min/max pin to the dump limits",
          out["query_defaults"]["start"] == f.start_time
          and out["query_defaults"]["end"] == f.end_time, str(out))

    out = srv.query_defaults_set(paths=["top_tb.ghost"])
    check("an unknown signal warns but still sets",
          out["status"] == "ok" and out.get("warnings"), str(out))

    err = srv.query_defaults_set(start="40ns", end="10ns")
    check("a reversed window is refused",
          err.get("error_type") == "invalid_argument"
          and err.get("parameter") == "end", str(err))
    err = srv.query_defaults_set(start="nonsense")
    check("a malformed time is refused naming the field",
          err.get("error_type") == "invalid_argument"
          and err.get("parameter") == "start", str(err))

    out = srv.query_defaults_clear()
    check("clear empties everything",
          out["query_defaults"]["is_set"] is False, str(out))


def _stage_atomic() -> None:
    print("== a rejected update changes nothing ==")
    srv = _bind()
    srv.query_defaults_set(paths=[CLK], start="10ns", end="30ns")
    before = _state()

    # A valid new signal list submitted together with a bad time must not be
    # half-applied: the whole request is refused, so the caller never has to
    # wonder which part landed.
    err = srv.query_defaults_set(paths=[RST], start="nonsense")
    after = _state()
    check("paths + bad time: refused",
          err.get("error_type") == "invalid_argument", str(err))
    check("paths + bad time: nothing changed", after == before,
          f"{before} vs {after}")
    check("paths + bad time: revision did not move",
          after["revision"] == before["revision"], str(after))

    err = srv.query_defaults_set(paths=[RST], end="5ns")
    after = _state()
    check("paths + reversed window: refused and unchanged",
          err.get("error_type") == "invalid_argument" and after == before,
          str(after))

    err = srv.query_defaults_set(paths=[RST], start="not-a-time",
                                 defaults_revision=before["revision"] + 99)
    after = _state()
    check("a stale revision is checked before validation",
          err.get("error_type") == "defaults_conflict", str(err))
    check("a stale revision changes nothing", after == before, str(after))

    # A later valid call still works, i.e. the refused ones left no residue.
    ok = srv.query_defaults_set(paths=[RST])
    check("a valid update after refusals still applies",
          ok["query_defaults"]["paths"] == [RST], str(ok))


def _stage_revision() -> None:
    print("== revision and defaults_revision ==")
    srv = _bind()
    r0 = _state()["revision"]

    out = srv.query_defaults_set(paths=[CLK])
    check("a successful update advances the revision",
          out["query_defaults"]["revision"] == r0 + 1, str(out))

    r1 = _state()["revision"]
    out = srv.query_defaults_set(paths=[CLK])
    check("a no-op update still advances it",
          out["query_defaults"]["revision"] == r1 + 1, str(out))
    check("revision is reported by get", _state()["revision"] == r1 + 1)

    r2 = _state()["revision"]
    ok = srv.query_defaults_set(paths=[RST], defaults_revision=r2)
    check("a matching revision is accepted",
          ok.get("status") == "ok", str(ok))

    stale = srv.query_defaults_set(paths=[CLK], defaults_revision=r2)
    check("a stale revision is refused",
          stale.get("error_type") == "defaults_conflict", str(stale))
    check("the conflict reports both revisions",
          stale.get("defaults_revision") == r2
          and stale.get("current_revision") == r2 + 1, str(stale))
    check("the refused update did not apply",
          _state()["paths"] == [RST], str(_state()))

    stale = srv.query_defaults_clear(defaults_revision=r2)
    check("clear honours defaults_revision too",
          stale.get("error_type") == "defaults_conflict", str(stale))
    ok = srv.query_defaults_clear(defaults_revision=_state()["revision"])
    check("clear with the current revision empties it",
          ok["status"] == "ok" and ok["query_defaults"]["is_set"] is False,
          str(ok))
    check("clear advances the revision",
          ok["query_defaults"]["revision"] > r2, str(ok))


def _stage_fallback() -> None:
    print("== queries fall back to the defaults ==")
    srv = _bind()

    err = srv.signal_values()
    check("no paths and no defaults -> invalid_argument",
          err.get("error_type") == "invalid_argument", str(err))

    srv.query_defaults_set(paths=[CLK, RST])
    out = srv.signal_values()
    check("paths come from the defaults",
          _paths(out) == [CLK, RST], str(_paths(out)))
    check("_defaults_used is set when a default was used",
          out.get("_defaults_used") is True, str(out.keys()))

    srv.query_defaults_set(start="10ns", end="30ns")
    out = srv.signal_values()
    check("window comes from the defaults",
          out["window"] == {"start": "10ns", "end": "30ns"}, str(out["window"]))

    act = srv.signal_activity()
    check("signal_activity falls back too",
          _paths(act) == [CLK, RST]
          and act["window"] == {"start": "10ns", "end": "30ns"}, str(act))
    check("signal_activity flags the defaults",
          act.get("_defaults_used") is True)

    pred = {"k": "bin", "op": "Equality", "l": {"k": "sig", "name": CLK},
            "r": {"k": "const", "lit": "1'b1"}}
    win = srv.find_time_windows(predicate=pred)
    check("find_time_windows falls back to the default window",
          win["window"] == {"start": "10ns", "end": "30ns"}, str(win["window"]))
    check("find_time_windows flags the defaults",
          win.get("_defaults_used") is True)


def _stage_explicit_wins() -> None:
    print("== explicit arguments override the defaults ==")
    srv = _bind()
    srv.query_defaults_set(paths=[CLK, RST], start="10ns", end="30ns")

    out = srv.signal_values(paths=CNT, start="0ns", end="50ns")
    check("explicit paths win", _paths(out) == [CNT], str(_paths(out)))
    check("explicit window wins",
          out["window"]["end"] == "50ns", str(out["window"]))
    check("a fully explicit call is not flagged",
          "_defaults_used" not in out, str(out.keys()))

    out = srv.signal_values(paths=CLK, start="min", end="max")
    f = open_session(SAMPLE).fst
    check('"min"/"max" are explicit, not absent',
          out["signals"][0]["count"] == len(f.all_values(CLK, 10000))
          and "_defaults_used" not in out, str(out.get("window")))

    out = srv.signal_values(paths=CLK, end="20ns")
    check("each bound falls back independently",
          out["window"] == {"start": "10ns", "end": "20ns"}, str(out["window"]))
    check("a partially defaulted call is still flagged",
          out.get("_defaults_used") is True)

    act = srv.signal_activity(paths=CNT)
    check("signal_activity: explicit paths win, window still defaults",
          _paths(act) == [CNT]
          and act["window"] == {"start": "10ns", "end": "30ns"}, str(act))


def _stage_single_signal() -> None:
    print("== single-signal tools ==")
    srv = _bind()
    srv.query_defaults_set(paths=[CNT], start="30ns", end="50ns")

    for name in ("signal_drivers", "signal_loads", "signal_connectivity",
                 "signal_fanin", "signal_info"):
        out = getattr(srv, name)()
        check(f"{name} uses a one-signal default set",
              out.get("_defaults_used") is True, str(out)[:160])

    tv = srv.trace_value()
    check("trace_value uses the default signal and window start",
          tv.get("_defaults_used") is True and tv.get("time") == "30ns",
          str(tv)[:160])
    ad = srv.active_drivers()
    check("active_drivers uses the default signal and window start",
          ad.get("_defaults_used") is True and ad.get("time") == "30ns",
          str(ad)[:160])
    tx = srv.trace_x()
    check("trace_x uses the defaults", tx.get("_defaults_used") is True,
          str(tx)[:160])

    out = srv.signal_drivers(path=CNT)
    check("an explicit path is not flagged as default-driven",
          "_defaults_used" not in out, str(out.keys()))

    print("== an ambiguous default set supplies nothing ==")
    srv.query_defaults_set(paths=[CLK, RST])
    err = srv.signal_drivers()
    check("a multi-signal default refuses instead of guessing",
          err.get("error_type") == "invalid_argument"
          and err.get("default_paths") == [CLK, RST], str(err))
    check("the refusal names the offending parameter",
          err.get("parameter") == "path", str(err))
    ok = srv.signal_drivers(path=CNT)
    check("naming one signal resolves the ambiguity",
          ok.get("available") is True and "_defaults_used" not in ok)

    err = srv.trace_value()
    check("trace_value refuses on an ambiguous default set",
          err.get("error_type") == "invalid_argument", str(err))


def _stage_cleared() -> None:
    print("== a cleared state falls back to the built-in defaults ==")
    srv = _bind()
    f = open_session(SAMPLE).fst
    srv.query_defaults_set(paths=[CLK], start="10ns", end="20ns")
    srv.query_defaults_clear()

    out = srv.signal_values(CLK)
    check("the window widens back to the whole dump",
          out["signals"][0]["count"] == len(f.all_values(CLK, 10000)),
          str(out["window"]))
    check("nothing is flagged as default-driven after clear",
          "_defaults_used" not in out, str(out.keys()))

    err = srv.signal_values()
    check("paths are required again after clear",
          err.get("error_type") == "invalid_argument", str(err))
    err = srv.signal_drivers()
    check("single-signal tools require a path again after clear",
          err.get("error_type") == "invalid_argument", str(err))

    act = srv.signal_activity(paths=[CLK])
    full = srv.signal_values(CLK)["window"]
    check("signal_activity spans the whole dump after clear",
          act["window"] == full, f"{act['window']} vs {full}")
    check("signal_activity is not flagged after clear",
          "_defaults_used" not in act, str(act.keys()))


def _stage_static() -> None:
    print("== a static session takes signals but not a window ==")
    if os.path.isdir(STATIC_TMP):
        shutil.rmtree(STATIC_TMP, ignore_errors=True)
    try:
        opened = srv.open_static_session(
            out_dir=STATIC_TMP, top="counter",
            filelist=[os.path.join(HERE, "..", "..", "examples", "sample",
                                   "counter.sv")])
        sid = opened["session_id"]
        check("the static session opened",
              opened.get("mode") == "static", str(opened.get("mode")))

        ok = srv.query_defaults_set(paths=[CNT], session_id=sid)
        check("default signals are accepted without a waveform",
              ok.get("status") == "ok"
              and ok["query_defaults"]["paths"] == [CNT], str(ok))

        err = srv.query_defaults_set(paths=[CLK], start="10ns", session_id=sid)
        check("a default window is refused without a waveform",
              err.get("error_type") == "unavailable"
              and err.get("parameter") == "start", str(err))
        after = srv.query_defaults_get(session_id=sid)["query_defaults"]
        check("the refused window left the signals alone",
              after["paths"] == [CNT] and after["start"] is None, str(after))
    finally:
        try:
            srv.close_session(session_id=locals().get("sid"))
        except Exception:  # pylint: disable=broad-except
            pass
        shutil.rmtree(STATIC_TMP, ignore_errors=True)


def _stage_scope() -> None:
    print("== the stored state is data-plane only ==")
    srv = _bind()
    check("QueryDefaults.FIELDS is exactly paths/start/end",
          set(QueryDefaults.FIELDS) == {"paths", "start", "end"},
          str(sorted(QueryDefaults.FIELDS)))
    # A field that could hold a hypothesis or a step counter would make this a
    # stateful agent rather than a fact source, and would leak one client's
    # reasoning into another sharing the session.
    banned = {"hypothesis", "note", "notes", "step", "state", "next",
              "next_step", "conclusion", "finding", "history", "plan"}
    slots = set(QueryDefaults.__slots__)
    semantic = {s.lstrip("_") for s in slots if not s.startswith("__")}
    check("no process/analysis state is stored",
          not (semantic & banned), str(sorted(semantic & banned)))
    check("the lock and revision are private",
          "_lock" in slots and "_revision" in slots, str(sorted(slots)))

    tools = {t.name: t for t in asyncio.run(srv.mcp.list_tools())}
    props = set(tools["query_defaults_set"].input_schema.get("properties", {}))
    check("no analysis field is exposed",
          not (props & banned), str(sorted(props & banned)))

    srv.query_defaults_clear()
    srv.query_defaults_set(paths=[CLK])
    # The property that matters is not just "a fresh session is empty" but that a
    # second session cannot inherit the first one's window, which is what makes
    # two agents on one design safe.
    other = srv.new_session(SAMPLE)["session_id"]
    fresh = srv.query_defaults_get(session_id=other)["query_defaults"]
    check("a new session does not inherit another session's defaults",
          fresh["is_set"] is False, str(fresh))
    check("the first session keeps its own defaults",
          _state()["paths"] == [CLK], str(_state()))
    srv.close_session(session_id=other)


def _stage_read_is_a_copy() -> None:
    print("== reads hand back copies, not the live list ==")
    srv = _bind()
    srv.query_defaults_set(paths=[CLK, RST])
    got = srv.query_defaults_get()["query_defaults"]
    got["paths"].append("top_tb.ghost")
    check("mutating a reply does not change the store",
          _state()["paths"] == [CLK, RST], str(_state()))

    snap = CUR.defaults.read()
    snap["paths"].append("top_tb.ghost")
    check("mutating a read() copy does not change the store",
          _state()["paths"] == [CLK, RST], str(_state()))


def main() -> int:
    print("== query defaults suite ==")
    _stage_naming()
    _stage_tools()
    _stage_atomic()
    _stage_revision()
    _stage_fallback()
    _stage_explicit_wins()
    _stage_single_signal()
    _stage_cleared()
    _stage_static()
    _stage_scope()
    _stage_read_is_a_copy()
    print(f"\n  query-defaults suite: {len(PASSED)} passed, "
          f"{len(FAILED)} failed")
    if FAILED:
        print("  failed:", FAILED)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
