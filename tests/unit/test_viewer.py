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
    except Exception as e:                                     # noqa: BLE001
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
              hint["available"] is False and "pip install" in hint["hint"])

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
        check("update unknown view: error dict",
              r["available"] is False)
        r = mgr.get_state("nope")
        check("get_state unknown view: error dict",
              r["available"] is False)
    finally:
        if saved is not None:
            os.environ["WAVE_MCP_VIEWER_ASSETS"] = saved
        else:
            os.environ.pop("WAVE_MCP_VIEWER_ASSETS", None)


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
            except Exception as e:                             # noqa: BLE001
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


def main() -> int:
    print("== viewer unit suite ==")
    test_state()
    test_translate()
    test_assets()
    test_http()
    print(f"\n  viewer suite: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("  failed:", FAILED)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
