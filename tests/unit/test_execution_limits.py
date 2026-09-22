#!/usr/bin/env python3
"""S5 execution limits: the admission gate, quotas, idle reaping, shutdown.

Concurrency is driven with Barrier/Event so every interleaving is forced, not
hoped for; timeouts exist only to turn a deadlock into a failure.

Stages
  * executor: bounded slots, per-owner cap, fairness between owners, queue
    full -> server_busy, wait too long -> queue_timeout, slot returned on
    exception, shutdown refuses waiters and drains runners;
  * limits: environment parsing keeps defaults on junk, never lets
    per_owner_running exceed workers;
  * sessions: per-owner quota -> resource_limit before the load, idle TTL
    reaps only sessions with nothing in flight, shared resource survives;
  * server: a gated tool reports server_busy as a structured reply, in-flight
    tracking is visible, session_info(list_sessions=True) carries server
    status, renamed S5 parameters are gone from the schema;
  * converter: a stalled child in its own process group is reaped with its
    children, nothing else.

Run directly or via tests/run_regression.py.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

from wave_mcp.runtime.executor import (BoundedExecutor, ExecutionLimits,  # noqa: E402
                                       QueueTimeout, ServerBusy,
                                       ServerShuttingDown, ResourceLimit)
from wave_mcp.session import SessionManager                                # noqa: E402

PASSED, FAILED = [], []
SAMPLE = os.path.join(ROOT, "examples", "sample", "session")


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSED if cond else FAILED).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  ({detail})" if detail and not cond else ""))


def _spawn(fn, *a):
    t = threading.Thread(target=fn, args=a, daemon=True)
    t.start()
    return t


# -- 1. executor -----------------------------------------------------------------
def _stage_executor() -> None:
    print("== executor: bounded slots ==")
    ex = BoundedExecutor(ExecutionLimits(workers=2, per_owner_running=2,
                                         queue_capacity=3, queue_wait_timeout=5))
    inside = threading.Barrier(3, timeout=5)     # 2 runners + main
    release = threading.Event()
    results = {}

    def body(tag, sync=True):
        if sync:
            inside.wait()
        release.wait(5)
        return tag

    def run(tag, owner="a", sync=True):
        try:
            results[tag] = ex.run(owner, lambda: body(tag, sync))
        except Exception as exc:  # pylint: disable=broad-except
            results[tag] = exc

    t1, t2 = _spawn(run, "r1"), _spawn(run, "r2")
    inside.wait()
    st = ex.stats()
    check("two calls occupy both slots", st["running"] == 2, str(st))

    # third caller waits; fourth, fifth fill the queue; sixth is refused
    waiters = [_spawn(run, f"w{i}", "a", False) for i in range(3)]
    for _ in range(50):
        if ex.stats()["waiting"] == 3:
            break
        time.sleep(0.02)
    check("three callers are queued", ex.stats()["waiting"] == 3, str(ex.stats()))
    run("overflow", "a", False)
    check("queue full -> server_busy", isinstance(results["overflow"], ServerBusy),
          str(results.get("overflow")))
    check("busy payload names the counts",
          results["overflow"].payload().get("waiting") == 3
          and results["overflow"].payload()["error_type"] == "server_busy")

    release.set()
    for t in (t1, t2, *waiters):
        t.join(5)
    check("all admitted calls completed",
          all(results.get(k) == k for k in ("r1", "r2", "w0", "w1", "w2")),
          str(results))
    st = ex.stats()
    check("slots all returned", st["running"] == 0 and st["waiting"] == 0, str(st))
    check("counters add up", st["admitted"] == 5 and st["busy"] == 1
          and st["completed"] == 5, str(st))

    print("== executor: per-owner cap and fairness ==")
    ex = BoundedExecutor(ExecutionLimits(workers=3, per_owner_running=1,
                                         queue_capacity=10, queue_wait_timeout=5))
    gate = threading.Event()
    order = []
    lock = threading.Lock()

    def slow(owner, tag):
        def body():
            with lock:
                order.append(tag)
            gate.wait(5)
            return tag
        return ex.run(owner, body)

    a1 = _spawn(slow, "A", "A1")
    for _ in range(50):
        if ex.stats()["running"] == 1:
            break
        time.sleep(0.02)
    a2 = _spawn(slow, "A", "A2")          # same owner: must wait although slots free
    for _ in range(50):
        if ex.stats()["waiting"] == 1:
            break
        time.sleep(0.02)
    st = ex.stats()
    check("per-owner cap holds a second call back with free slots",
          st["running"] == 1 and st["waiting"] == 1, str(st))
    b1 = _spawn(slow, "B", "B1")          # other owner: goes straight in
    for _ in range(50):
        if ex.stats()["running"] == 2:
            break
        time.sleep(0.02)
    check("another owner is not blocked by A's cap", ex.stats()["running"] == 2)
    gate.set()
    for t in (a1, a2, b1):
        t.join(5)
    check("A1 first, A2 only after A1 released, B1 not held behind A2",
          order[0] == "A1" and order.index("B1") < order.index("A2"), str(order))

    print("== executor: fairness between owners ==")
    ex = BoundedExecutor(ExecutionLimits(workers=1, per_owner_running=1,
                                         queue_capacity=10, queue_wait_timeout=5))
    hold = threading.Event()
    grants = []

    def held(owner, tag):
        def body():
            grants.append(tag)
            hold.wait(5)
        ex.run(owner, body)

    first = _spawn(held, "A", "A0")
    for _ in range(50):
        if ex.stats()["running"] == 1:
            break
        time.sleep(0.02)
    # A floods the queue, then B asks once
    flood = [_spawn(held, "A", f"A{i}") for i in range(1, 4)]
    for _ in range(50):
        if ex.stats()["waiting"] == 3:
            break
        time.sleep(0.02)
    bq = _spawn(held, "B", "B0")
    for _ in range(50):
        if ex.stats()["waiting"] == 4:
            break
        time.sleep(0.02)
    hold.set()   # everything finishes in dispatch order; B must not be last
    for t in (first, *flood, bq):
        t.join(5)
    check("a never-served owner is dispatched before the flooder's backlog",
          grants.index("B0") == 1, str(grants))

    print("== executor: timeout, exception, shutdown ==")
    ex = BoundedExecutor(ExecutionLimits(workers=1, per_owner_running=1,
                                         queue_capacity=5, queue_wait_timeout=0.3))
    block = threading.Event()
    runner = _spawn(lambda: ex.run("A", lambda: block.wait(5)))
    for _ in range(50):
        if ex.stats()["running"] == 1:
            break
        time.sleep(0.02)
    t0 = time.monotonic()
    try:
        ex.run("B", lambda: None)
        timed_out = None
    except QueueTimeout as exc:
        timed_out = exc
    check("waiting past queue_wait_timeout -> queue_timeout",
          isinstance(timed_out, QueueTimeout) and 0.25 < time.monotonic() - t0 < 2,
          str(timed_out))
    check("timed-out waiter left the queue", ex.stats()["waiting"] == 0)

    def boom():
        raise RuntimeError("body failed")
    block.set()
    runner.join(5)
    try:
        ex.run("A", boom)
    except RuntimeError:
        pass
    st = ex.stats()
    check("exception returns the slot and counts as failed",
          st["running"] == 0 and st["failed"] == 1, str(st))

    block = threading.Event()
    runner = _spawn(lambda: ex.run("A", lambda: block.wait(5)))
    for _ in range(50):
        if ex.stats()["running"] == 1:
            break
        time.sleep(0.02)
    waiter_result = {}

    def waiter():
        try:
            ex.run("B", lambda: None)
        except Exception as exc:  # pylint: disable=broad-except
            waiter_result["exc"] = exc
    w = _spawn(waiter)
    for _ in range(50):
        if ex.stats()["waiting"] == 1:
            break
        time.sleep(0.02)
    left = ex.shutdown(grace=0.2)
    w.join(5)
    check("shutdown refuses the waiter", isinstance(waiter_result.get("exc"),
                                                    ServerShuttingDown))
    check("shutdown reports the still-running body, does not kill it",
          left == {"refused": 1, "still_running": 1}, str(left))
    try:
        ex.run("C", lambda: None)
        after = None
    except ServerShuttingDown as exc:
        after = exc
    check("nothing is admitted after shutdown", after is not None)
    block.set()
    runner.join(5)
    check("running body still completed and released", ex.stats()["running"] == 0)


# -- 2. limits from env -------------------------------------------------------------
def _stage_limits() -> None:
    print("== limits from environment ==")
    keys = list(ExecutionLimits.ENV.values())
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        d = ExecutionLimits.from_env()
        check("defaults when unset", d == ExecutionLimits(), str(d))
        os.environ["WAVE_MCP_WORKERS"] = "8"
        os.environ["WAVE_MCP_QUEUE_CAPACITY"] = "junk"
        os.environ["WAVE_MCP_PER_OWNER_RUNNING"] = "0"
        os.environ["WAVE_MCP_SESSION_TTL"] = "0"
        os.environ["WAVE_MCP_QUEUE_TIMEOUT"] = "-5"
        lim = ExecutionLimits.from_env()
        check("valid value applied", lim.workers == 8)
        check("junk keeps default", lim.queue_capacity == 32)
        check("zero rejected where positive required", lim.per_owner_running == 2)
        check("ttl=0 accepted (disables reaping)", lim.idle_session_ttl == 0)
        check("negative rejected", lim.queue_wait_timeout == 30.0)
        os.environ["WAVE_MCP_WORKERS"] = "1"
        os.environ["WAVE_MCP_PER_OWNER_RUNNING"] = "5"
        lim = ExecutionLimits.from_env()
        check("per_owner_running clamped to workers", lim.per_owner_running == 1, str(lim))
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# -- 3. session quota and TTL ----------------------------------------------------------
def _stage_sessions() -> None:
    print("== session quota ==")
    sm = SessionManager("owner-a", per_owner_sessions=2, idle_ttl=0)
    s1 = sm.open(SAMPLE)
    s2 = sm.open(SAMPLE)
    loads_before = sm.stats()["resources"]["loads"]
    try:
        sm.open(SAMPLE)
        third = None
    except ResourceLimit as exc:
        third = exc
    check("third session refused with resource_limit",
          third is not None and third.payload()["error_type"] == "resource_limit"
          and third.payload()["limit"] == 2, str(third))
    check("refusal happened before any load",
          sm.stats()["resources"]["loads"] == loads_before)
    check("another owner is not affected", bool(sm.open(SAMPLE, owner_id="owner-b")))
    sm.close(s1)
    check("closing one frees the quota", bool(sm.open(SAMPLE)))
    sm.close(s2)

    print("== idle TTL reaping ==")
    sm = SessionManager("o", per_owner_sessions=0, idle_ttl=10.0)
    idle = sm.open(SAMPLE)
    busy = sm.open(SAMPLE)
    ws_busy = sm.get(busy)
    ws_idle = sm.get(idle)
    sm.begin_call(ws_busy)
    now = time.monotonic()
    ws_idle.last_used_mono = now - 20
    ws_busy.last_used_mono = now - 20
    reaped = sm.reap_idle(now)
    check("idle session reaped, in-flight one kept",
          reaped == [idle] and sm.peek(busy) is not None, str(reaped))
    check("shared resource survives while the other session holds it",
          sm.stats()["resources"]["keys"] == 1, str(sm.stats()))
    sm.end_call(ws_busy)
    check("end_call touches the session",
          time.monotonic() - ws_busy.last_used_mono < 1)
    check("a fresh session is not reaped", sm.reap_idle() == [])
    ws_busy.last_used_mono = now - 20
    reaped = sm.reap_idle(now)
    check("once nothing is in flight it goes too", reaped == [busy]
          and sm.stats()["resources"]["keys"] == 0, str((reaped, sm.stats())))
    check("ttl=0 never reaps", SessionManager("o", idle_ttl=0).reap_idle() == [])


# -- 4. server integration -----------------------------------------------------------
def _stage_server() -> None:
    print("== server: gate and diagnostics ==")
    from wave_mcp import server as srv
    from wave_mcp.runtime.executor import BoundedExecutor as _BE
    sid = srv.open_session(SAMPLE)["session_id"]
    info = srv.session_info(list_sessions=True)
    check("listing carries server status",
          isinstance(info.get("server"), dict) and "limits" in info["server"],
          str(info.get("server")))
    check("status counts, never ids or paths",
          not any(isinstance(v, str) and "/" in v for v in info["server"].values()))

    tight = _BE(ExecutionLimits(workers=1, per_owner_running=1, queue_capacity=0,
                                queue_wait_timeout=1))
    orig = srv.EXECUTOR
    srv.EXECUTOR = tight
    try:
        started = threading.Event()
        hold = threading.Event()

        def occupy():
            tight.run(srv.PRINCIPAL.owner_id, lambda: (started.set(), hold.wait(5)))
        t = _spawn(occupy)
        started.wait(5)
        out = srv.signal_values("top_tb.clk", 5, session_id=sid)
        check("saturated gate -> structured server_busy reply",
              out.get("error_type") == "server_busy" and out.get("status") == "error",
              str(out)[:160])
        check("refused reply has no fingerprint", "_fp" not in out)
        hold.set()
        t.join(5)
        out = srv.signal_values("top_tb.clk", 5, session_id=sid)
        check("after release the same call succeeds", "_fp" in out, str(out)[:120])
    finally:
        srv.EXECUTOR = orig

    print("== server: in-flight tracking ==")
    ws = srv.SESSIONS.get(sid)
    seen = {}
    entered = threading.Event()
    go = threading.Event()
    real = srv.SESSIONS.end_call

    def spy_end(w):
        seen["in_flight_during"] = w.in_flight
        entered.set()
        go.wait(5)
        real(w)
    srv.SESSIONS.end_call = spy_end
    try:
        t = _spawn(lambda: srv.signal_values("top_tb.clk", 5, session_id=sid))
        entered.wait(5)
        check("a running query counts as in flight", seen.get("in_flight_during") == 1
              and srv.SESSIONS.reap_idle(time.monotonic() + 10 ** 6) == [],
              str(seen))
        go.set()
        t.join(5)
    finally:
        srv.SESSIONS.end_call = real
    check("in-flight back to zero", ws.in_flight == 0)
    srv.close_session(session_id=sid)

    print("== server: S5 parameter names ==")
    tools = {t.name: t for t in asyncio.run(srv.mcp.list_tools())}
    gone = {"max_signals", "max_scopes", "levels", "transitive",
            "filter_by_name", "filter_by_type", "parallel"}
    bad = {n: sorted(set(t.input_schema["properties"]) & gone)
           for n, t in tools.items()}
    bad = {k: v for k, v in bad.items() if v}
    check("no retired S5 parameter in any schema", not bad, str(bad))
    check("limit / max_depth / name_contains where expected",
          {"limit", "name_contains", "signal_type"} <= set(tools["list_signals"].input_schema["properties"])
          and {"max_depth", "limit"} <= set(tools["find_instances"].input_schema["properties"])
          and {"max_depth", "limit"} <= set(tools["signal_fanin"].input_schema["properties"])
          and {"max_depth", "limit"} <= set(tools["signal_downstream"].input_schema["properties"]))
    check("tool count still 37", len(tools) == 37, str(len(tools)))
    hint = srv.param_rename_hint(TypeError("got an unexpected keyword argument 'transitive'"),
                                 "signal_fanin")
    check("transitive gets a rename hint pointing at max_depth",
          hint is not None and "max_depth" in hint["use_instead"], str(hint))

    print("== server: max_depth semantics on fan-in/out ==")
    sid = srv.open_session(SAMPLE)["session_id"]
    d1 = srv.signal_downstream(path="top_tb.rst_n", max_depth=1, session_id=sid)
    d8 = srv.signal_downstream(path="top_tb.rst_n", max_depth=8, session_id=sid)
    check("max_depth=1 is a subset of max_depth=8",
          set(d1.get("fan_out", [])) <= set(d8.get("fan_out", []))
          and len(d8.get("fan_out", [])) >= len(d1.get("fan_out", [])),
          f"{len(d1.get('fan_out', []))} vs {len(d8.get('fan_out', []))}")
    lim = srv.signal_downstream(path="top_tb.rst_n", max_depth=8, limit=1, session_id=sid)
    check("limit caps the transitive answer", len(lim.get("fan_out", [])) <= 1, str(lim)[:120])
    srv.close_session(session_id=sid)


# -- 5. converter process-group reaping ---------------------------------------------
def _stage_converter() -> None:
    print("== converter: own process group reaped ==")
    from wave_mcp import convert
    tmp = tempfile.mkdtemp()
    marker = os.path.join(tmp, "child.pid")
    # A fake converter that forks a grandchild and then hangs without output.
    script = (f"import os,subprocess,time;"
              f"p=subprocess.Popen(['sleep','30']);"
              f"open({marker!r},'w').write(str(p.pid));"
              f"time.sleep(30)")
    fst = os.path.join(tmp, "out.fst")
    saved = convert._STALL_LIMIT
    t0 = time.monotonic()
    try:
        rc, _out = convert._run_with_heartbeat([sys.executable, "-c", script], fst,
                                               timeout=1.0, kind="test")
        raised = None
    except convert.ConversionError as exc:
        raised = exc
        rc = None
    finally:
        convert._STALL_LIMIT = saved
    check("timeout raises ConversionError within a few seconds",
          raised is not None and time.monotonic() - t0 < 8, str(raised)[:100])
    time.sleep(0.3)
    try:
        gpid = int(open(marker).read())
        alive = subprocess.run(["kill", "-0", str(gpid)], capture_output=True).returncode == 0
    except (OSError, ValueError):
        alive = False
    check("grandchild in the converter's group was reaped too", not alive)


def main() -> int:
    _stage_executor()
    _stage_limits()
    _stage_sessions()
    _stage_server()
    _stage_converter()
    print(f"\n  execution-limits suite: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("  failed:", ", ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
