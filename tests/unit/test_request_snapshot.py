#!/usr/bin/env python3
"""Request snapshot unit suite (v0.3.0 S3).

One tool call resolves its session, copies the query defaults and normalizes
its arguments exactly once, and everything in the reply describes that one
resolution. This suite pins the consequences:

- interleaved queries on two sessions with different default windows never
  report each other's window (the snapshot is per call, not per thread);
- an explicit call and a defaults-driven call that ask the same question carry
  the same ``_fp.query`` (F01), and a different question carries a different one;
- ``_query`` reports the *effective* mode / signals / window, in readable form,
  and names which fields came from the defaults;
- ``defaults_revision`` is a consistency check: a stale one refuses before any
  data is read, and the refusal has no fingerprint;
- a default set that changes *after* a request started does not change that
  request (the copy is taken up front);
- a session closed while a query runs still yields a fingerprinted reply;
- a diff's identity is the runs in order: swapping them changes the digest;
- point reads and window reads are different modes, and a default window is
  never smuggled into an explicit point read;
- a role parameter (``clock``, ``path`` of ``scope_info``) is recorded but never
  taken from the signal defaults;
- no tool body or wrapper re-resolves the default session while a snapshot is
  active (checked at source level).

Run directly or via tests/run_regression.py.
"""
from __future__ import annotations

import contextvars
import os
import re
import shutil
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)

from wave_mcp import server as srv                          # noqa: E402
from wave_mcp.runtime import request as rq                  # noqa: E402
from wave_mcp.sources.fst_source import FstSource           # noqa: E402

PASSED, FAILED = [], []

EXAMPLES = os.path.join(HERE, "..", "..", "examples")
SAMPLE = os.path.join(EXAMPLES, "sample", "session")
SAMPLE_DUMP = os.path.join(EXAMPLES, "sample", "dump.fst")
CLK = "top_tb.clk"
RST = "top_tb.rst_n"
CNT = "top_tb.u_counter.count"


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSED if cond else FAILED).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  ({detail})" if detail and not cond else ""))


def _open() -> str:
    return srv.open_session(SAMPLE)["session_id"]


def _stage_f01() -> None:
    print("== F01: same question, same digest ==")
    sid = _open()
    explicit = srv.signal_values(paths=[CLK], start="10ns", end="30ns",
                                 session_id=sid)
    srv.query_defaults_set(paths=[CLK], start="10ns", end="30ns", session_id=sid)
    implicit = srv.signal_values(session_id=sid)
    check("explicit and defaults-driven calls share one digest",
          explicit["_fp"]["query"] == implicit["_fp"]["query"],
          f"{explicit['_fp']['query']} vs {implicit['_fp']['query']}")
    check("they share the data identity too",
          explicit["_fp"]["dataset"] == implicit["_fp"]["dataset"])
    check("the defaults-driven one says which fields it inherited",
          implicit["_query"].get("from_defaults") == ["end", "paths", "start"],
          str(implicit["_query"]))
    check("the explicit one inherited nothing",
          "from_defaults" not in explicit["_query"], str(explicit["_query"]))
    check("both report the same effective window in readable form",
          explicit["_query"]["start"] == implicit["_query"]["start"] == "10ns"
          and explicit["_query"]["end"] == implicit["_query"]["end"] == "30ns",
          str((explicit["_query"], implicit["_query"])))
    check("the defaults revision is reported, but not hashed",
          explicit["_query"]["defaults_revision"]
          != implicit["_query"]["defaults_revision"]
          and explicit["_fp"]["query"] == implicit["_fp"]["query"])

    # spelling differences that mean the same instant hash the same
    spelled = srv.signal_values(paths=CLK, start="10000ps", end="30000ps",
                                session_id=sid)
    check("a different spelling of the same window is the same question",
          spelled["_fp"]["query"] == explicit["_fp"]["query"],
          str(spelled["_query"]))
    one = srv.signal_values(paths=CLK, start="10ns", end="30ns", session_id=sid)
    check("a bare string path equals a one-element list",
          one["_fp"]["query"] == explicit["_fp"]["query"])

    print("== a different question is a different digest ==")
    other_win = srv.signal_values(paths=[CLK], start="10ns", end="40ns",
                                  session_id=sid)
    other_sig = srv.signal_values(paths=[RST], start="10ns", end="30ns",
                                  session_id=sid)
    other_lim = srv.signal_values(paths=[CLK], start="10ns", end="30ns",
                                  limit=3, session_id=sid)
    digests = {explicit["_fp"]["query"], other_win["_fp"]["query"],
               other_sig["_fp"]["query"], other_lim["_fp"]["query"]}
    check("window, signal and limit each change the digest", len(digests) == 4,
          str(digests))
    check("the effective limit is reported even when defaulted",
          explicit["_query"].get("limit") == 1000
          and other_lim["_query"].get("limit") == 3,
          str((explicit["_query"].get("limit"), other_lim["_query"].get("limit"))))
    act = srv.signal_activity(paths=[CLK], start="10ns", end="30ns",
                              session_id=sid)
    check("the same arguments to a different tool are a different question",
          act["_fp"]["query"] != explicit["_fp"]["query"])
    srv.close_session(session_id=sid)


def _stage_modes() -> None:
    print("== point vs window, and role parameters ==")
    sid = _open()
    srv.query_defaults_set(paths=[CNT], start="10ns", end="30ns", session_id=sid)

    point = srv.signal_values(paths=CNT, time="25ns", session_id=sid)
    check("a point read reports mode=point and a time",
          point["_query"].get("mode") == "point"
          and point["_query"].get("time") == "25ns", str(point["_query"]))
    check("a default window is never smuggled into an explicit point read",
          "start" not in point["_query"] and "end" not in point["_query"],
          str(point["_query"]))
    check("the point read did not count as using defaults",
          "_defaults_used" not in point and "from_defaults" not in point["_query"])

    win = srv.signal_values(session_id=sid)
    check("a window read reports mode=window",
          win["_query"].get("mode") == "window", str(win["_query"]))
    check("point and window on the same signal are different questions",
          point["_fp"]["query"] != win["_fp"]["query"])

    tv = srv.trace_value(session_id=sid)
    check("trace_value takes the single default signal and the window start",
          tv["_query"].get("path") == CNT and tv["_query"].get("time") == "10ns"
          and set(tv["_query"].get("from_defaults", [])) == {"path", "time"},
          str(tv.get("_query")))
    tv_explicit = srv.trace_value(path=CNT, time="10ns", session_id=sid)
    check("F01 holds for a single-signal point tool too",
          tv["_fp"]["query"] == tv_explicit["_fp"]["query"])

    scope = srv.scope_info(path="top_tb.u_counter", session_id=sid)
    check("scope_info records its path but never inherits signal defaults",
          scope["_query"].get("path") == "top_tb.u_counter"
          and "from_defaults" not in scope["_query"], str(scope.get("_query")))
    import inspect
    scope_param = inspect.signature(srv.scope_info).parameters["path"]
    check("scope_info's path is required: it can never borrow a signal default",
          scope_param.default is inspect.Parameter.empty)

    clk = srv.sample_at_clock(paths=[CNT], clock=CLK, session_id=sid)
    check("clock is a role parameter: recorded, not defaulted",
          clk["_query"].get("paths") == [CNT]
          and "clock" not in clk["_query"].get("from_defaults", []),
          str(clk.get("_query")))
    srv.close_session(session_id=sid)


def _stage_revision() -> None:
    print("== defaults_revision is a consistency check ==")
    sid = _open()
    srv.query_defaults_set(paths=[CLK], session_id=sid)
    rev = srv.query_defaults_get(session_id=sid)["query_defaults"]["revision"]

    ok = srv.signal_values(session_id=sid, defaults_revision=rev)
    check("the current revision is accepted", ok.get("status") != "error"
          and ok["_query"]["defaults_revision"] == rev, str(ok)[:120])
    stale = srv.signal_values(session_id=sid, defaults_revision=rev - 1)
    check("a stale revision is refused", stale.get("error_type")
          == "defaults_conflict", str(stale))
    check("the refusal reports both revisions",
          stale.get("defaults_revision") == rev - 1
          and stale.get("current_revision") == rev, str(stale))
    check("a refusal carries no fingerprint and no query report",
          "_fp" not in stale and "_query" not in stale, str(stale.keys()))
    future = srv.signal_values(session_id=sid, defaults_revision=rev + 1)
    check("a revision from the future is refused the same way",
          future.get("error_type") == "defaults_conflict")
    explicit = srv.signal_values(paths=[RST], session_id=sid,
                                 defaults_revision=rev - 1)
    check("the check applies even when no default is actually needed",
          explicit.get("error_type") == "defaults_conflict", str(explicit)[:120])
    check("a refused query did not touch the defaults",
          srv.query_defaults_get(session_id=sid)["query_defaults"]["revision"]
          == rev)
    srv.close_session(session_id=sid)


def _stage_interleaved() -> None:
    print("== interleaved queries on two sessions never mix windows ==")
    a, b = _open(), _open()
    srv.query_defaults_set(paths=[CLK], start="5ns", end="20ns", session_id=a)
    srv.query_defaults_set(paths=[RST], start="30ns", end="50ns", session_id=b)
    mixed = False
    for _ in range(6):
        ra = srv.signal_values(session_id=a)
        rb = srv.signal_values(session_id=b)
        if (ra["_query"]["paths"], ra["_query"]["start"], ra["_query"]["end"]) \
                != ([CLK], "5ns", "20ns"):
            mixed = True
        if (rb["_query"]["paths"], rb["_query"]["start"], rb["_query"]["end"]) \
                != ([RST], "30ns", "50ns"):
            mixed = True
    check("twelve alternating calls each report their own window", not mixed)
    check("the two sessions have different digests",
          ra["_fp"]["query"] != rb["_fp"]["query"])
    check("but the same data identity",
          ra["_fp"]["dataset"] == rb["_fp"]["dataset"])

    print("== concurrent queries from threads keep their own snapshot ==")
    results = {}
    gate = threading.Barrier(2)

    def run(tag, sid):
        gate.wait(10)
        results[tag] = srv.signal_values(session_id=sid)

    ta = threading.Thread(target=run, args=("a", a))
    tb = threading.Thread(target=run, args=("b", b))
    ta.start(); tb.start(); ta.join(20); tb.join(20)
    check("thread A saw session A's window",
          results["a"]["_query"]["paths"] == [CLK]
          and results["a"]["_query"]["end"] == "20ns", str(results["a"]["_query"]))
    check("thread B saw session B's window",
          results["b"]["_query"]["paths"] == [RST]
          and results["b"]["_query"]["start"] == "30ns", str(results["b"]["_query"]))
    check("no snapshot leaks out of a finished call", rq.current() is None)
    srv.close_session(session_id=a)
    srv.close_session(session_id=b)


def _stage_snapshot_is_fixed() -> None:
    print("== defaults changed mid-request do not affect that request ==")
    sid = _open()
    srv.query_defaults_set(paths=[CLK], start="0ns", end="20ns", session_id=sid)
    resource = srv.SESSIONS.get(sid).resource
    inside, proceed = threading.Event(), threading.Event()
    original = FstSource._iter_values_multi

    def parked(self, *args, **kwargs):
        if self is resource.fst:
            inside.set()
            proceed.wait(20)
        return original(self, *args, **kwargs)

    out: dict = {}
    FstSource._iter_values_multi = parked
    t = threading.Thread(target=lambda: out.update(r=srv.signal_values(
        session_id=sid)), daemon=True)
    try:
        t.start()
        check("the query reached the reader", inside.wait(20))
        moved = srv.query_defaults_set(paths=[RST], start="30ns", end="50ns",
                                       session_id=sid)
        check("the defaults could be changed while the query ran",
              moved.get("status") == "ok")
        proceed.set()
        t.join(20)
        r = out.get("r", {})
        check("the in-flight query still reports the window it started with",
              r["_query"]["paths"] == [CLK] and r["_query"]["end"] == "20ns",
              str(r.get("_query")))
        check("its data really is the old window",
              all(v["time_units"] <= 20 for v in r["signals"][0]["values"]),
              str([v["time_units"] for v in r["signals"][0]["values"]][:6]))
        check("and it names the revision it was answered under",
              r["_query"]["defaults_revision"]
              == moved["query_defaults"]["revision"] - 1, str(r["_query"]))
        after = srv.signal_values(session_id=sid)
        check("the next query sees the new defaults",
              after["_query"]["paths"] == [RST], str(after["_query"]))
    finally:
        FstSource._iter_values_multi = original
        proceed.set()
        t.join(20)
    srv.close_session(session_id=sid)


def _stage_inflight_close_has_fp() -> None:
    print("== a session closed mid-query still yields a fingerprinted reply ==")
    sid = _open()
    resource = srv.SESSIONS.get(sid).resource
    inside, proceed = threading.Event(), threading.Event()
    original = FstSource._iter_values_multi

    def parked(self, *args, **kwargs):
        if self is resource.fst:
            inside.set()
            proceed.wait(20)
        return original(self, *args, **kwargs)

    out: dict = {}
    FstSource._iter_values_multi = parked
    t = threading.Thread(target=lambda: out.update(r=srv.signal_values(
        CLK, session_id=sid)), daemon=True)
    try:
        t.start()
        check("the query reached the reader", inside.wait(20))
        check("close succeeds while the query runs",
              srv.close_session(session_id=sid).get("status") == "disconnected")
        proceed.set()
        t.join(20)
        r = out.get("r", {})
        check("the reply carries _fp although the session is gone",
              isinstance(r.get("_fp"), dict) and r["_fp"].get("query"),
              str(r.keys()))
        check("and _query", isinstance(r.get("_query"), dict), str(r.keys()))
        check("with real data",
              r.get("signals", [{}])[0].get("count", 0) > 0)
    finally:
        FstSource._iter_values_multi = original
        proceed.set()
        t.join(20)


def _stage_diff_identity(tmp: str) -> None:
    print("== a diff's identity is the runs, in order ==")
    a = os.path.join(tmp, "a.fst")
    b = os.path.join(tmp, "b.fst")
    shutil.copy2(SAMPLE_DUMP, a)
    shutil.copy2(SAMPLE_DUMP, b)
    st = os.stat(b)
    os.utime(b, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))  # distinct id

    ab = srv.diff_waveforms([a, b], signals=[CLK])
    ba = srv.diff_waveforms([b, a], signals=[CLK])
    check("the diff reply carries _fp.runs in input order",
          [r["index"] for r in ab["_fp"]["runs"]] == [0, 1], str(ab["_fp"]))
    check("each run has its own wave identity",
          ab["_fp"]["runs"][0]["wave"] != ab["_fp"]["runs"][1]["wave"])
    check("swapping the runs swaps the identities",
          ab["_fp"]["runs"][0]["wave"] == ba["_fp"]["runs"][1]["wave"])
    check("swapping the runs changes the query digest",
          ab["_fp"]["query"] != ba["_fp"]["query"])
    check("a diff borrows no session: no wave/netlist keys from a default",
          not ({"wave", "netlist", "resource"} & set(ab["_fp"])), str(ab["_fp"]))
    check("the diff's _query says how many runs and how it sampled",
          ab["_query"].get("runs") == 2 and ab["_query"].get("mode"),
          str(ab["_query"]))

    # a session open on some *other* design must not leak into the diff
    sid = _open()
    with_session = srv.diff_waveforms([a, b], signals=[CLK])
    check("an open session does not change the diff's identity",
          with_session["_fp"] == ab["_fp"])
    srv.close_session(session_id=sid)

    after = srv.diff_waveforms([a, b], signals=[CLK], after="10ns")
    check("`after` is part of the question", after["_fp"]["query"]
          != ab["_fp"]["query"])
    bad = srv.diff_waveforms([a])
    check("a refused diff carries no fingerprint",
          bad.get("status") == "error" and "_fp" not in bad)


def _stage_no_reresolution() -> None:
    print("== nothing re-resolves the default session inside a request ==")
    import wave_mcp.server as _srv_mod
    src = open(_srv_mod.__file__, encoding="utf-8").read()
    hits = [m.start() for m in re.finditer(r"SESSIONS\.get\(None", src)]
    check("no `SESSIONS.get(None` anywhere in server.py", not hits, str(hits))
    # Direct reads of the live defaults store are allowed in exactly two places:
    # where the snapshot copies them, and the fallback for calls made outside
    # any snapshot. Every tool body must go through _defaults_of.
    reads = [m.start() for m in re.finditer(r"query_defaults\.read\(\)", src)]
    check("the live defaults store is read in at most two places",
          len(reads) <= 2, str(len(reads)))
    check("the query-defaults tools themselves are not snapshotted",
          all(n not in srv._QUERY_TOOLS for n in
              ("query_defaults_set", "query_defaults_get",
               "query_defaults_clear")))
    check("the schema version is part of the digest",
          rq.API_SCHEMA_VERSION and rq.digest_of("x", {}) != "")

    # the resolvers read the snapshot copy, not the store, when one is active
    sid = _open()
    srv.query_defaults_set(paths=[CLK], session_id=sid)
    sess = srv.SESSIONS.get(sid)
    snap = rq.RequestSnapshot("probe", srv.PRINCIPAL, sess, None,
                              {"paths": [RST], "start": None, "end": None,
                               "revision": 99}, {})
    token = rq.CURRENT.set(snap)
    try:
        paths, used = srv._resolve_paths(sess, None)
        check("_resolve_paths reads the snapshot copy while one is active",
              paths == [RST] and used is True, str(paths))
    finally:
        rq.CURRENT.reset(token)
    paths, _ = srv._resolve_paths(sess, None)
    check("and the live store when none is", paths == [CLK], str(paths))
    srv.close_session(session_id=sid)


def _stage_thread_propagation() -> None:
    print("== a snapshot does not bleed into a worker thread by default ==")
    # contextvars are per-thread unless copied; a helper that hands work to a
    # pool must pass the context on purpose. This pins the behaviour so a future
    # pool cannot silently inherit or drop the snapshot.
    seen = {}
    snap = rq.RequestSnapshot("probe", srv.PRINCIPAL, None, None, {}, {})
    token = rq.CURRENT.set(snap)
    try:
        t = threading.Thread(target=lambda: seen.update(plain=rq.current()))
        t.start(); t.join(5)
        check("a plain thread starts without the caller's snapshot",
              seen.get("plain") is None)
        ctx = contextvars.copy_context()
        t2 = threading.Thread(target=lambda: seen.update(
            copied=ctx.run(rq.current)))
        t2.start(); t2.join(5)
        check("copy_context().run carries it explicitly",
              seen.get("copied") is snap)
    finally:
        rq.CURRENT.reset(token)
    check("no snapshot remains on the main thread", rq.current() is None)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="wave_snap_test_")
    print(f"== request snapshot suite (workdir {tmp}) ==")
    try:
        check("the suite starts with no sessions open",
              srv.SESSIONS.stats()["sessions"] == 0)
        _stage_f01()
        _stage_modes()
        _stage_revision()
        _stage_interleaved()
        _stage_snapshot_is_fixed()
        _stage_inflight_close_has_fp()
        _stage_diff_identity(tmp)
        _stage_no_reresolution()
        _stage_thread_propagation()
    finally:
        for entry in srv.SESSIONS.list_sessions():
            srv.close_session(session_id=entry["session_id"])
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n  request snapshot suite: {len(PASSED)} passed, "
          f"{len(FAILED)} failed")
    if FAILED:
        print("  failed:", FAILED)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
