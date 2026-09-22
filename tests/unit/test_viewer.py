#!/usr/bin/env python3
"""Viewer unit suite: state machine, sucl translation, asset discovery,
view-state HTTP API, degradation, concurrency. No browser, no surver
binary, no viewer assets required — everything is exercised at the
Python/HTTP layer with a stub upstream.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))

from wave_mcp.viewer.state import ViewState, ViewStateError   # noqa: E402
from wave_mcp.viewer.translate import desired_to_sucl         # noqa: E402
from wave_mcp.viewer import find_assets, unavailable_hint     # noqa: E402
from wave_mcp.viewer.server import ViewerServer               # noqa: E402

PASSED, FAILED = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSED if cond else FAILED).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  ({detail})" if detail and not cond else ""))


def expect_raises(name: str, fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
        check(name, False, "no exception raised")
    except ViewStateError:
        check(name, True)
    except Exception as e:  # pylint: disable=broad-except
        check(name, False, f"wrong exception {type(e).__name__}: {e}")


# ---------------------------------------------------------------- state --
def test_state() -> None:
    print("\n-- ViewState --")
    st = ViewState()
    check("initial revision 0", st.revision == 0)

    rev = st.update_desired(signals=[{"path": "a.b", "color": "red"}])
    check("update bumps revision", rev == 1)
    check("sucl cache maintained",
          "variable_add a.b" in st.desired["startup_commands_cache"])

    # replace semantics for lists
    st.update_desired(signals=[{"path": "c.d"}])
    check("signals replace not append",
          [s["path"] for s in st.desired["signals"]] == ["c.d"])

    # annotations append + dedupe by id
    st.update_desired(annotations=[{"id": "x", "markdown": "one"}])
    st.update_desired(annotations=[{"id": "x", "markdown": "dup"},
                                   {"markdown": "two"}])
    anns = st.desired["annotations"]
    check("annotations append-only, id dedupe",
          len(anns) == 2 and anns[0]["markdown"] == "one", str(anns))
    check("auto id assigned", anns[1]["id"], str(anns[1]))

    # cursor normalization + defaults
    st.update_desired(cursor={"time": 123})
    check("cursor unit defaults ps",
          st.desired["cursor"] == {"time": "123", "unit": "ps"})

    # diff auto-marker, idempotent
    st.update_desired(diff={"source_a": "a", "source_b": "b",
                            "first_divergence": {"time": "500"}})
    st.update_desired(diff={"source_a": "a", "source_b": "b",
                            "first_divergence": {"time": "500"}})
    marks = [m for m in st.desired["markers"]
             if m.get("label") == "first divergence"]
    check("diff auto-marker added once", len(marks) == 1, str(marks))

    # strict time schema: typos and bad values are loud, suffixes normalized
    st2 = ViewState()
    st2.update_desired(cursor={"time": "100ns", "unit": "ns"})
    check("suffixed time normalized",
          st2.desired["cursor"] == {"time": "100", "unit": "ns"},
          str(st2.desired["cursor"]))
    st2.update_desired(cursor={"time": "85ns"})
    check("suffix derives the unit",
          st2.desired["cursor"] == {"time": "85", "unit": "ns"},
          str(st2.desired["cursor"]))
    try:
        st2.update_desired(cursor={"time": "85ns", "unit": "ps"})
        check("suffix/unit conflict rejected", False, "no exception")
    except ViewStateError as e:
        check("suffix/unit conflict rejected",
              "85ns" in str(e) and e.parameter == "cursor", str(e))
    for bad in ({"time_units": 100}, {"time": None}, {"time": "100.5"},
                {"time": "100nanoseconds"}, {"time": ""},
                {"time": "100", "unit": "furlong"}):
        try:
            st2.update_desired(cursor=bad)
            check(f"cursor rejected: {bad!r}", False, "accepted")
        except ViewStateError:
            check(f"cursor rejected: {bad!r}", True)
    try:
        st2.update_desired(cursor={"time": 100, "time_units": "ns"})
        check("unknown field not silently ignored", False, "accepted")
    except ViewStateError as e:
        check("unknown field not silently ignored",
              "time_units" in str(e)
              and getattr(e, "did_you_mean", None) in ("time", "unit"),
              str(e))
    for kw in ({"viewport": 5}, {"diff": "abc"}, {"markers": "x"},
               {"signals": "x"}):
        try:
            st2.update_desired(**kw)
            check(f"type guard {kw}", False, "accepted")
        except ViewStateError:
            check(f"type guard {kw}", True)
    try:
        st2.update_desired(markers=[{"time": "0"}, {"label": "x"}])
        check("marker index in error", False, "accepted")
    except ViewStateError as e:
        check("marker index in error", "markers[1]" in str(e), str(e))

    # validation rejects garbage
    expect_raises("bad color rejected", st.update_desired,
                  signals=[{"path": "x", "color": "pink"}])
    expect_raises("bad format rejected", st.update_desired,
                  signals=[{"path": "x", "format": "octal"}])
    expect_raises("signal without path rejected", st.update_desired,
                  signals=[{"color": "red"}])
    expect_raises("cursor without time rejected", st.update_desired,
                  cursor={"unit": "ps"})
    expect_raises("viewport without to rejected", st.update_desired,
                  viewport={"from": "0"})
    expect_raises("annotation without markdown rejected", st.update_desired,
                  annotations=[{"confidence": "high"}])
    expect_raises("bad confidence rejected", st.update_desired,
                  annotations=[{"markdown": "m", "confidence": "sure"}])

    # failed validation must not corrupt committed state
    before = json.dumps(st.snapshot()["desired"], sort_keys=True)
    try:
        st.update_desired(signals=[{"path": "ok"},
                                   {"path": "bad", "color": "pink"}])
    except ViewStateError:
        pass
    after = json.dumps(st.snapshot()["desired"], sort_keys=True)
    check("failed update leaves no partial signals",
          [s["path"] for s in st.desired["signals"]] != ["ok"],
          after if before != after else "")

    # actual write-back merges only known keys
    st.write_actual({"cursor": {"time": "9", "unit": "ps"},
                     "user_dirty": True, "evil_key": 1})
    check("actual merge + unknown key ignored",
          st.actual["cursor"]["time"] == "9"
          and st.actual["user_dirty"] is True
          and "evil_key" not in st.actual)
    check("updated_at stamped", bool(st.actual["updated_at"]))

    # long-poll: wakes on change, times out clean
    got = {}

    def waiter():
        got["snap"] = st.wait_change(st.revision, timeout=5)

    th = threading.Thread(target=waiter)
    th.start()
    time.sleep(0.2)
    st.update_desired(cursor={"time": "777"})
    th.join(timeout=5)
    check("long-poll wakes on change",
          got.get("snap", {}).get("desired", {}).get("cursor", {})
          .get("time") == "777")
    t0 = time.time()
    st.wait_change(st.revision, timeout=0.3)
    check("long-poll timeout honored", 0.2 < time.time() - t0 < 2)


# ------------------------------------------------------------ translate --
def test_translate() -> None:
    print("\n-- translate (desired -> sucl) --")
    d = {
        "waveform": {"sources": [{"id": "a", "path": "/w/a.fst"}]},
        "signals": [
            {"path": "t.g1_s1", "group": "G1", "color": "red"},
            {"path": "t.g1_s2", "group": "G1", "format": "hex"},
            {"path": "t.g2_s1", "group": "G2"},
            {"path": "t.plain"},
        ],
        "cursor": {"time": "85000", "unit": "ps"},
        "viewport": {"from": "80000", "to": "90000", "unit": "ps"},
        "markers": [{"time": "85000", "unit": "ps"},
                    {"time": "86000", "unit": "ps"}],
    }
    # schema times are ps; pass the matching FST timescale exponent so the
    # raw-number conversion is exercised the way state.py drives it.
    s = desired_to_sucl(d, -12)
    check("groups emit dividers once each",
          s.count('divider_add "G1"') == 1
          and s.count('divider_add "G2"') == 1, s)
    # A divider name containing whitespace is silently dropped by the sucl
    # parser (probed), which loses the whole heading. Names must be folded.
    ws = desired_to_sucl({
        "waveform": {"sources": [{"id": "a", "path": "/tmp/a.fst"}]},
        "signals": [{"path": "t.a", "group": "fast domain"},
                    {"path": "t.b", "group": "slow  domain  x"}],
    }, -12)
    check("multi-word group folded to underscores",
          'divider_add "fast_domain"' in ws
          and 'divider_add "slow_domain_x"' in ws
          and '"fast domain"' not in ws, ws)
    # Non-ASCII is passed through: the divider is created correctly, it just
    # renders as boxes. Dropping it would lose the grouping entirely.
    cjk = desired_to_sucl({
        "waveform": {"sources": [{"id": "a", "path": "/tmp/a.fst"}]},
        "signals": [{"path": "t.a", "group": "快时钟域"}],
    }, -12)
    check("non-ascii group preserved",
          'divider_add "快时钟域"' in cjk, cjk)
    check("color follows its variable",
          "variable_add t.g1_s1;item_set_color red" in s, s)
    check("format mapped", "item_set_format Hexadecimal" in s, s)
    check("raw-number times (no unit suffix)",
          "85000ps" not in s and "cursor_set 85000" in s, s)
    check("viewport uses zoom_to", "zoom_to 80000 90000" in s, s)
    check("markers numbered from 1",
          "marker_set_at 85000 1" in s and "marker_set_at 86000 2" in s, s)
    check("no GUI-only marker_add", "marker_add" not in s, s)

    # single source: no surver_select_file
    check("single source: no select_file", "surver_select_file" not in s, s)

    # multi source + diff: selects fail side by full path
    d2 = {
        "waveform": {"sources": [{"id": "a", "path": "/w/pass.fst"},
                                 {"id": "b", "path": "/w/fail.fst"}]},
        "signals": [{"path": "t.s"}],
        "diff": {"source_a": "a", "source_b": "b"},
        "markers": [],
    }
    s2 = desired_to_sucl(d2)
    check("diff view selects source_b full path",
          s2.startswith("surver_select_file /w/fail.fst"), s2)

    # multi source, signal-source focus
    d3 = {
        "waveform": {"sources": [{"id": "a", "path": "/w/a.fst"},
                                 {"id": "b", "path": "/w/b.fst"}]},
        "signals": [{"path": "t.s", "source": "a"}],
        "markers": [],
    }
    s3 = desired_to_sucl(d3)
    check("signal source drives selection",
          s3.startswith("surver_select_file /w/a.fst"), s3)

    # zoom_fit fallback when signals but no viewport
    d4 = {"waveform": {"sources": []},
          "signals": [{"path": "t.s"}], "markers": []}
    check("zoom_fit fallback", "zoom_fit" in desired_to_sucl(d4))

    # empty desired -> empty command string
    d5 = {"waveform": {"sources": []}, "signals": [], "markers": []}
    check("empty desired -> empty sucl", desired_to_sucl(d5) == "")

    # dropped commands are reported, never silent (defense in depth: the
    # validated tool path cannot produce these, direct callers can)
    rep: list = []
    s_bad = desired_to_sucl({
        "waveform": {"sources": []},
        "cursor": {"time": "100", "unit": "furlong"},
        "markers": [{"time": "100", "unit": "bogus"}],
    }, -12, report=rep)
    check("bad unit dropped but reported",
          "cursor_set" not in s_bad and "marker_set_at" not in s_bad
          and len(rep) == 2 and any("cursor" in r for r in rep), str(rep))


# ----------------------------------------------------- assets/degradation --
def test_assets() -> None:
    print("\n-- asset discovery / degradation --")
    saved = os.environ.pop("WAVE_MCP_VIEWER_ASSETS", None)
    try:
        os.environ["WAVE_MCP_VIEWER_ASSETS"] = "/nonexistent/assets"
        check("bogus env dir ignored (falls through)",
              find_assets() is None or find_assets()["origin"] != "env")

        hint = unavailable_hint()
        check("degradation payload shape",
              hint["available"] is False
              and hint.get("error_type") == "viewer_unavailable"
              and "SELF_BUILD" in hint["hint"], str(hint))

        # tool-level degradation via a manager with no assets
        from wave_mcp.viewer.manager import ViewManager
        mgr = ViewManager()          # fresh, not the singleton
        if not mgr.available:
            r = mgr.open_view(["/tmp/x.fst"])
            check("open_view degrades without assets",
                  r["available"] is False and "hint" in r, str(r))
        else:
            check("open_view degrades without assets", True,
                  "assets installed in env; skipped")
        r = mgr.update_view("nope")
        if mgr.available:
            check("update unknown view: error dict",
                  r["status"] == "error" and r["error_type"] == "unknown_view",
                  str(r))
        else:
            # without assets the manager degrades before looking any view up
            check("update degrades without assets",
                  r["status"] == "error" and "hint" in r, str(r))
        r = mgr.get_state("nope")
        if mgr.available:
            check("get_state unknown view: error dict",
                  r["status"] == "error" and r["error_type"] == "unknown_view",
                  str(r))
        else:
            check("get_state degrades without assets",
                  r["status"] == "error" and "hint" in r, str(r))
    finally:
        if saved is not None:
            os.environ["WAVE_MCP_VIEWER_ASSETS"] = saved
        else:
            os.environ.pop("WAVE_MCP_VIEWER_ASSETS", None)


# ---------------------------------------------------- error contract --
def test_error_contract() -> None:
    print("\n-- error contract / argument validation --")
    from wave_mcp.viewer import invalid_argument_payload
    from wave_mcp.viewer.manager import ViewManager, validate_open_args
    from wave_mcp.viewer.state import validate_view_inputs

    # pure validators: no viewer process, no assets involved
    validate_view_inputs(cursor={"time": "100", "unit": "ps"})
    check("validate_view_inputs accepts a good fragment", True)
    try:
        validate_view_inputs(cursor={"time_units": 1})
        check("validate_view_inputs rejects a typo", False, "accepted")
    except ViewStateError as e:
        payload = invalid_argument_payload(e)
        check("invalid_argument payload shape",
              payload["status"] == "error"
              and payload["error_type"] == "invalid_argument"
              and payload.get("parameter") == "cursor"
              and "time_units" in payload["error"]
              and "available" not in payload, str(payload))

    try:
        validate_open_args(["a.fst", "b.fst", "c.fst"])
        check("three waveforms rejected", False, "accepted")
    except ViewStateError as e:
        check("three waveforms rejected", "at most two" in str(e), str(e))
    try:
        validate_open_args(["a.fst"], labels=["x", "y"])
        check("label count mismatch rejected", False, "accepted")
    except ViewStateError as e:
        check("label count mismatch rejected",
              e.parameter == "labels", str(e))
    try:
        validate_open_args([])
        check("empty fst_paths rejected", False, "accepted")
    except ViewStateError:
        check("empty fst_paths rejected", True)

    # open_view answers a bad argument with invalid_argument before anything
    # is started (no surver, no server); only reachable when assets exist,
    # because a missing viewer short-circuits with viewer_unavailable.
    mgr = ViewManager()          # fresh instance, not the singleton
    if mgr.available:
        r = mgr.open_view(["/tmp/x.fst"], cursor={"time_units": 1})
        check("open_view bad cursor -> invalid_argument",
              r["status"] == "error"
              and r["error_type"] == "invalid_argument"
              and r.get("parameter") == "cursor", str(r))
        r = mgr.open_view(["/tmp/x.fst"], labels=["a", "b"])
        check("open_view label mismatch -> invalid_argument",
              r["status"] == "error"
              and r.get("parameter") == "labels", str(r))
    else:
        check("open_view arg-error paths (skipped: no assets)", True)

    from collections import deque
    from wave_mcp.viewer.surver import _tail_suffix
    check("surver stderr tail formatting",
          "boom" in _tail_suffix(deque(["error: boom"]))
          and _tail_suffix(deque()) == ""
          and _tail_suffix(deque([""])) == "")


# ---------------------------------------------------------- http server --
def test_http() -> None:
    print("\n-- ViewerServer HTTP API --")
    import tempfile
    web = tempfile.mkdtemp(prefix="wv_web_")
    wasm = tempfile.mkdtemp(prefix="wv_wasm_")
    with open(os.path.join(web, "shell.html"), "w") as f:
        f.write("<html>shell</html>")
    # A same-named page in shell_dir must NOT shadow the Surfer WASM entry:
    # the viewer iframe loads /index.html and would otherwise never boot.
    with open(os.path.join(web, "index.html"), "w") as f:
        f.write("<html>shell-landing</html>")
    with open(os.path.join(wasm, "index.html"), "w") as f:
        f.write("<html>wasm</html>")

    st = ViewState()
    server = ViewerServer(wasm_dir=wasm, shell_dir=web,
                          surver_base="http://127.0.0.1:1",  # dead upstream
                          state=st)
    server.start()
    base = server.base_url

    def get(path):
        with urllib.request.urlopen(base + path, timeout=5) as r:
            return r.status, r.read(), dict(r.headers)

    def put(path, obj):
        req = urllib.request.Request(base + path, method="PUT",
                                     data=json.dumps(obj).encode(),
                                     headers={"Content-Type":
                                              "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    try:
        code, body, hdrs = get("/")
        check("/ serves shell with COOP/COEP",
              code == 200 and b"shell" in body
              and hdrs.get("Cross-Origin-Embedder-Policy") == "require-corp"
              and hdrs.get("Server") == "Surfer", str(hdrs))

        code, body, _ = get("/index.html")
        check("wasm dir fallback", code == 200 and b"wasm" in body)

        try:
            code, _, _ = get("/../etc/passwd")
        except urllib.error.HTTPError as e:
            code = e.code
        check("path traversal blocked", code in (403, 404), str(code))

        try:
            code, _, _ = get("/no/such/file.js")
        except urllib.error.HTTPError as e:
            code = e.code
        check("404 for missing static", code == 404)

        # view-state API
        code, obj = put("/api/view-state",
                        {"cursor": {"time": "42", "unit": "ps"}})
        check("PUT desired ok", code == 200 and obj["ok"]
              and obj["revision"] == 1, str(obj))

        code, obj = put("/api/view-state",
                        {"signals": [{"path": "x", "color": "pink"}]})
        check("PUT invalid -> 400 with error",
              code == 400 and not obj["ok"] and "color" in obj["error"],
              str(obj))

        code, body, _ = get("/api/view-state")
        snap = json.loads(body)
        check("GET snapshot reflects desired",
              snap["desired"]["cursor"]["time"] == "42"
              and snap["revision"] == 1)

        # long-poll returns quickly once revision advances
        results = {}

        def poller():
            t0 = time.time()
            _, b2, _ = get("/api/view-state?since=1")
            results["dt"] = time.time() - t0
            results["snap"] = json.loads(b2)

        th = threading.Thread(target=poller)
        th.start()
        time.sleep(0.3)
        put("/api/view-state", {"cursor": {"time": "43"}})
        th.join(timeout=10)
        check("long-poll wakes within 2s",
              results.get("dt", 99) < 2
              and results["snap"]["desired"]["cursor"]["time"] == "43",
              str(results.get("dt")))

        # actual write-back
        req = urllib.request.Request(
            base + "/api/view-state/actual", method="POST",
            data=json.dumps({"applied_revision": 2,
                             "user_dirty": True}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            check("POST actual ok", r.status == 200)
        check("actual visible in snapshot",
              st.actual["applied_revision"] == 2
              and st.actual["user_dirty"] is True)

        # dead upstream proxy -> 502, not a hang/crash
        try:
            code, _, _ = get("/surver/tok/get_status")
        except urllib.error.HTTPError as e:
            code = e.code
        check("dead surver upstream -> 502", code == 502, str(code))

        # concurrent PUT storm: revisions strictly increase, no corruption
        errs = []

        def writer(i):
            try:
                for j in range(20):
                    put("/api/view-state",
                        {"annotations": [{"id": f"w{i}-{j}",
                                          "markdown": f"m{i}-{j}"}]})
            except Exception as e:  # pylint: disable=broad-except
                errs.append(e)

        threads = [threading.Thread(target=writer, args=(i,))
                   for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        snap = st.snapshot()
        check("concurrent PUTs: no errors", not errs, str(errs[:2]))
        check("concurrent PUTs: all 100 annotations kept",
              len(snap["desired"]["annotations"]) == 100,
              str(len(snap["desired"]["annotations"])))
        check("concurrent PUTs: revision consistent",
              snap["revision"] >= 100, str(snap["revision"]))
    finally:
        server.stop()


def test_lifecycle():
    """close_view / list_views: socket release, surver refcount, LRU cap."""
    import socket
    import tempfile
    from pathlib import Path
    from wave_mcp.viewer.manager import ViewManager

    # socket must be released, and stop() must be idempotent
    server = ViewerServer(wasm_dir=tempfile.mkdtemp(prefix="wv_lc_wasm_"),
                          shell_dir=tempfile.mkdtemp(prefix="wv_lc_web_"),
                          surver_base="http://127.0.0.1:1",
                          state=ViewState())
    port = server.port
    server.start()
    server.stop()
    time.sleep(0.3)
    released = True
    try:
        probe = socket.socket()
        probe.bind(("127.0.0.1", port))
        probe.close()
    except OSError:
        released = False
    check("stop() releases the listening socket", released,
          "port still bound after stop()")
    server.stop()
    check("stop() is idempotent", True)

    if ViewManager.instance().available is not True:
        check("viewer assets present (lifecycle tests skipped)", True)
        return

    waves = Path(__file__).resolve().parents[1].parent / \
        "examples/viewer_demos/waves"
    cdc, xprop = str(waves / "cdc.fst"), str(waves / "xprop.fst")
    if not Path(cdc).is_file():
        check("demo waveforms present (lifecycle tests skipped)", True)
        return

    mgr = ViewManager()          # isolated instance, not the singleton
    mgr.max_views = 2
    a = mgr.open_view([cdc], title="a")["view_id"]
    b = mgr.open_view([cdc], title="b")["view_id"]   # same set -> reuse
    check("list_views reports both views",
          mgr.list_views()["count"] == 2, str(mgr.list_views()["count"]))

    r = mgr.close_view(a)
    check("closing one sharer keeps surver alive",
          r["surver_stopped"] is False, str(r))
    r = mgr.close_view(b)
    check("closing last sharer stops surver",
          r["surver_stopped"] is True, str(r))
    check("close_view rejects unknown id",
          mgr.close_view("nope").get("error_type") == "unknown_view")

    # LRU: opening past max_views evicts the oldest and says which
    r1 = mgr.open_view([cdc], title="1")
    mgr.open_view([xprop], title="2")
    r3 = mgr.open_view([cdc], title="3")
    keep = r3["view_id"]
    lv = mgr.list_views()
    check("max_views caps open views", lv["count"] == 2, str(lv["count"]))
    check("newest view survives eviction",
          keep in [v["view_id"] for v in lv["views"]])
    check("evicted view id reported",
          r3.get("evicted_view_id") == r1["view_id"], str(r3))
    check("no phantom eviction on the first opens",
          "evicted_view_id" not in r1, str(r1))
    check("evicted view is really gone",
          mgr.get_state(r1["view_id"]).get("error_type") == "unknown_view")
    mgr.close_all()
    check("close_all empties the registry",
          mgr.list_views()["count"] == 0)

    # A token whose first character is "-" used to be parsed by clap as an
    # option (exit 2), failing roughly 1 in 64 opens; with the token attached
    # as --token=<value> every open must start. The patch makes the race a
    # deterministic case instead of a probabilistic one.
    import secrets as _secrets
    real_token_urlsafe = _secrets.token_urlsafe
    _secrets.token_urlsafe = lambda n=32: "-" + real_token_urlsafe(n)[1:]
    try:
        for i in range(3):       # each cycle starts a fresh surver -> new token
            r = mgr.open_view([xprop], title=f"dash-{i}")
            check(f"dash-leading token open #{i + 1}",
                  r.get("available") is True, str(r))
            if r.get("available"):
                mgr.close_view(r["view_id"])
    finally:
        _secrets.token_urlsafe = real_token_urlsafe


# --------------------------------------------------------- signal check --
def test_signal_check() -> None:
    """Advisory signal-name check: a name the waveform does not contain is
    reported as a warning instead of being silently dropped by Surfer, and
    the page-health fields plus the warnings list travel with the state."""
    from pathlib import Path
    import wave_mcp.viewer.manager as m

    waves = Path(__file__).resolve().parents[1].parent / \
        "examples/viewer_demos/waves"
    fst = str(waves / "xprop.fst")
    if not Path(fst).is_file():
        check("xprop.fst present (signal check skipped)", True)
        return

    names = m._fst_signal_names(fst)
    check("signal names parsed from the fst", bool(names))
    check("existing signal not flagged",
          m._missing_signals([fst], [{"path": "xprop_tb.din"}]) == [])
    check("missing signal flagged",
          m._missing_signals([fst], [{"path": "xprop_tb.nosuch"}])
          == ["xprop_tb.nosuch"])
    check("wildcard skipped",
          m._missing_signals([fst], [{"path": "xprop_tb.*"}]) == [])
    check("aggregated spelling accepted",
          m._missing_signals([fst], [{"path": "xprop_tb.din[7:0]"}]) == [])
    check("case-insensitive match",
          m._missing_signals([fst], [{"path": "XPROP_TB.DIN"}]) == [])
    check("cache returns the same set", m._fst_signal_names(fst) is names)
    check("unreadable file stays quiet",
          m._missing_signals(["/no/such.fst"], [{"path": "x"}]) == [])

    # actual state carries the page health fields; snapshot carries warnings
    st = ViewState()
    st.write_actual({"page_ready": True, "page_error": "boom", "evil": 1})
    check("page fields accepted in actual",
          st.actual.get("page_ready") is True
          and st.actual.get("page_error") == "boom"
          and "evil" not in st.actual)
    st.warnings.append("w1")
    check("snapshot carries warnings", st.snapshot().get("warnings") == ["w1"])

    # a missing signal must not block the open; it is reported instead
    from wave_mcp.viewer.manager import ViewManager
    mgr = ViewManager()          # isolated instance, not the singleton
    if not mgr.available:
        check("open_view missing-signal warning (skipped: no assets)", True)
        return
    r = mgr.open_view([fst], signals=[{"path": "xprop_tb.nosuch"}])
    check("open_view still opens", r.get("available") is True, str(r))
    check("missing signal reported as warning",
          any("xprop_tb.nosuch" in w for w in r.get("warnings", [])),
          str(r.get("warnings")))
    if r.get("available"):
        mgr.close_view(r["view_id"])
    mgr.close_all()


# --------------------------------------------------------- surver spawn --
def test_surver_argv() -> None:
    """The token must be passed attached (--token=<value>), never as a
    separate argv item: token_urlsafe() may start with "-" (1 in 64) and
    clap then reads the value as an option, so surver exits 2 without ever
    starting. That was the intermittent open failure this pins down.
    No assets or surver binary needed: Popen is a recorder here."""
    import io
    from wave_mcp.viewer import surver as sv

    captured = {}

    class _FakeProc:
        def __init__(self):
            self.stderr = io.StringIO("")
            self.returncode = None

        def poll(self):
            return None

    def _fake_popen(argv, **kwargs):
        captured["argv"] = argv
        return _FakeProc()

    inst = sv.SurverInstance.__new__(sv.SurverInstance)   # no __init__ side effects
    inst.token = "-starting-with-dash"
    inst.fst_paths = ["/tmp/a.fst", "/tmp/b.fst"]

    real_popen = sv.subprocess.Popen
    sv.subprocess.Popen = _fake_popen
    try:
        inst._spawn("/bin/surver", 43210)
    finally:
        sv.subprocess.Popen = real_popen

    argv = captured.get("argv", [])
    check("token attached as --token=<value>",
          "--token=-starting-with-dash" in argv, str(argv))
    check("no bare --token / dash value pair",
          "--token" not in argv and "-starting-with-dash" not in argv,
          str(argv))
    check("argv layout intact",
          argv[:5] == ["/bin/surver", "--port", "43210", "--bind-address",
                       "127.0.0.1"]
          and argv[-2:] == ["/tmp/a.fst", "/tmp/b.fst"], str(argv))


def test_stability_loop():
    """Bounded open/close soak against a real surver: every open must
    succeed and every close must stop its backend. 1 in 64 opens used to
    fail on a dash-leading token, so even this small loop is a regression
    net; set WAVE_MCP_VIEWER_LOOPS (test-only) to run a longer soak by
    hand. Skips without viewer assets, like test_lifecycle."""
    from pathlib import Path
    from wave_mcp.viewer.manager import ViewManager

    if ViewManager.instance().available is not True:
        check("viewer assets present (stability loop skipped)", True)
        return
    waves = Path(__file__).resolve().parents[1].parent / \
        "examples/viewer_demos/waves"
    cdc, xprop = str(waves / "cdc.fst"), str(waves / "xprop.fst")
    if not Path(cdc).is_file():
        check("demo waveforms present (stability loop skipped)", True)
        return

    loops = int(os.environ.get("WAVE_MCP_VIEWER_LOOPS", "10"))
    mgr = ViewManager()          # isolated instance, not the singleton
    fails = []
    t0 = time.time()
    for i in range(loops):
        r = mgr.open_view([cdc if i % 2 else xprop])
        if not r.get("available"):
            fails.append({"i": i, "step": "open", "resp": r})
            continue
        c = mgr.close_view(r["view_id"])
        if not c.get("closed"):
            fails.append({"i": i, "step": "close", "resp": c})
    check(f"{loops} open/close cycles: no failures", not fails,
          str(fails[:2]))
    check("cycles leave no backend running",
          mgr._surver_mgr is None
          or not any(inst.alive()
                     for inst in mgr._surver_mgr._instances.values()),
          "surver still alive after close")
    check("registry empty after the loop", mgr.list_views()["count"] == 0)
    if loops > 10:
        print(f"  (soak: {loops} cycles in {time.time() - t0:.1f}s)")


def test_ports():
    """alloc_port: ephemeral by default, windowed when a base is configured."""
    import importlib
    import wave_mcp.viewer as vw

    saved = os.environ.get("WAVE_MCP_VIEWER_PORT_BASE")
    try:
        os.environ.pop("WAVE_MCP_VIEWER_PORT_BASE", None)
        importlib.reload(vw)
        check("no base configured -> ephemeral ports",
              vw.port_base() is None, str(vw.port_base()))
        check("ephemeral port is usable", vw.alloc_port() > 1024)

        os.environ["WAVE_MCP_VIEWER_PORT_BASE"] = "45400"
        importlib.reload(vw)
        check("base is honoured", vw.port_base() == 45400,
              str(vw.port_base()))
        p = vw.alloc_port()
        check("allocated port falls inside the window",
              45400 <= p < 45400 + vw.PORT_WINDOW, str(p))

        # live sockets in the window must not collide
        import socket as _s
        held, ports = [], []
        for _ in range(3):
            sk = _s.socket()
            sk.bind(("127.0.0.1", vw.alloc_port()))
            sk.listen(1)
            held.append(sk)
            ports.append(sk.getsockname()[1])
        check("concurrent allocations do not collide",
              len(set(ports)) == 3, str(ports))
        for sk in held:
            sk.close()

        for bad in ("abc", "80", "70000", ""):
            os.environ["WAVE_MCP_VIEWER_PORT_BASE"] = bad
            importlib.reload(vw)
            if vw.port_base() is not None or vw.alloc_port() <= 1024:
                check(f"invalid base {bad!r} degrades to ephemeral", False,
                      str(vw.port_base()))
                break
        else:
            check("invalid bases degrade to ephemeral ports", True)
    finally:
        if saved is None:
            os.environ.pop("WAVE_MCP_VIEWER_PORT_BASE", None)
        else:
            os.environ["WAVE_MCP_VIEWER_PORT_BASE"] = saved
        importlib.reload(vw)


def main() -> int:
    print("== viewer unit suite ==")
    test_state()
    test_translate()
    test_assets()
    test_error_contract()
    test_signal_check()
    test_surver_argv()
    test_http()
    test_lifecycle()
    test_stability_loop()
    test_ports()
    print(f"\n  viewer suite: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("  failed:", FAILED)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
