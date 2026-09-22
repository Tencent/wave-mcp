#!/usr/bin/env python3
"""Viewer browser E2E suite (multi-scenario).

Needs viewer assets (WAVE_MCP_VIEWER_ASSETS / pip package / cache) and
playwright + chromium. Auto-skips (exit 0 with SKIP) when unavailable,
so the regression entry can always invoke it.

Scenarios:
  A. single waveform: signals/cursor/marker/annotation render, log popup
  B. update_wave_view: annotation live-append + cursor change reboots view
  C. dual waveform diff view: auto-select fail side, divergence marker
  D. delivery awareness: actual write-back + get_view_state
  E. log popup collapse/expand + unread badge
  F. two concurrent views stay isolated
  G. surver reuse for the same file set
  H. wave-view CLI smoke (subprocess)
  I. flicker-free navigation + no viewer-state readback
  J. entry page stays bounded when service workers are blocked
  K. gateway with a rewritten Server header still renders
  L. injected cursor/viewport/markers land on the requested values
  M. no user-interaction readback (license isolation from the EUPL viewer)
  N. IDE-sized viewports: alive at 430px, renders at 700px
  O. sandboxed webview (no storage, no workers) settles on the error card
  P. a dropped backend is surfaced, and the stream reconnects on return
  Q. two tabs on one view both render
"""
from __future__ import annotations

import http.server
import json
import os
import re
import socket
import socketserver
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests", "unit"))

from wave_mcp.viewer import find_assets                      # noqa: E402

if find_assets() is None:
    print("[SKIP] viewer assets not found; e2e suite skipped")
    sys.exit(0)
try:
    from playwright.sync_api import sync_playwright          # noqa: E402
except ImportError:
    print("[SKIP] playwright not installed; e2e suite skipped")
    sys.exit(0)

from fstgen import clocked_pair                              # noqa: E402
from wave_mcp.viewer.manager import ViewManager              # noqa: E402

PASSED, FAILED = [], []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  ({detail})" if detail and not cond else ""))


def wait_boot_done(page, timeout_s=30):
    """Wait for the boot overlay to settle: True on 'done', else False.

    'stuck' (the explicit error card) also counts as settled: what must
    never happen is settling nowhere.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            cls = page.eval_on_selector("#wv-boot",
                                        "el => el.className") or ""
        except Exception:  # pylint: disable=broad-except
            cls = ""
        if "done" in cls:
            return True
        if "stuck" in cls:
            return False
        time.sleep(0.5)
    return False


_STATE_JS = """(sel) => new Promise((resolve) => {
    const w = document.querySelector(sel).contentWindow;
    if (!w) { resolve(null); return; }
    if (!w.__wv_get_state) {
        try { w.eval("import('./surfer.js').then(function(m){window.__wv_get_state = m.get_state;}).catch(function(){})"); }
        catch (e) { resolve(null); return; }
    }
    const t0 = Date.now();
    const tryGet = () => {
        if (w.__wv_get_state) {
            Promise.resolve(w.__wv_get_state()).then(s => resolve(String(s)))
                .catch(() => resolve(null));
        } else if (Date.now() - t0 > 8000) { resolve(null); }
        else { setTimeout(tryGet, 400); }
    };
    tryGet();
})"""


def frame_state(page, sel="#surfer"):
    """What one pane really shows, read from Surfer's own get_state().

    Returns {cursor, left, right, markers}; left/right are 0..1 fractions
    of the source end time. None when the pane cannot answer yet."""
    s = page.evaluate(_STATE_JS, sel)
    if not isinstance(s, str):
        return None
    out = {"cursor": None, "left": None, "right": None, "markers": []}
    m = re.search(r"cursor: Some\(\((-?1), \[\s*([0-9,\s]*?)\s*\]", s)
    if m:
        digits = [d.strip() for d in m.group(2).split(",") if d.strip()]
        val = 0
        for d in reversed(digits):
            val = (val << 32) + int(d)
        out["cursor"] = val
    m = re.search(r"curr_left: \(([-0-9.e]+)\)", s)
    if m:
        out["left"] = float(m.group(1))
    m = re.search(r"curr_right: \(([-0-9.e]+)\)", s)
    if m:
        out["right"] = float(m.group(1))
    i = s.find("markers: {")
    if i >= 0:
        for mm in re.finditer(r"\d+:\s*\(\s*-?1,\s*\[\s*(\d+)", s[i:i + 400]):
            out["markers"].append(int(mm.group(1)))
    return out


class _GatewayServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._conns = set()

    def get_request(self):
        sock, addr = super().get_request()
        self._conns.add(sock)
        return sock, addr

    def close_connections(self):
        """Force every accepted connection shut.

        A real tunnel teardown (killed ssh -L, a rebuilt IDE forward) drops
        all TCP connections; without this, browser keep-alive reuse would
        slip new requests through handler threads that are still alive,
        and the page would never notice the gateway is gone."""
        for s in list(self._conns):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass
        self._conns.clear()


class Gateway:
    """Local reverse proxy that rewrites the `Server` header, the way
    openresty/nginx gateways (and some IDE proxies) do.

    Can be stopped and restarted on the same port, which is how the suite
    simulates a dropped and rebuilt port forward without touching the
    viewer itself."""

    def __init__(self, target):
        self.target = target
        self.server = None
        self.port = None

    def _handler_class(self):
        target = self.target

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def version_string(self):
                return "openresty/1.21.4.1"

            def log_message(self, *a):
                pass

            def _proxy(self, method):
                body = None
                if method in ("PUT", "POST"):
                    n = int(self.headers.get("Content-Length") or 0)
                    body = self.rfile.read(n) if n else b""
                req = urllib.request.Request(target + self.path,
                                             method=method, data=body)
                ct = self.headers.get("Content-Type")
                if ct:
                    req.add_header("Content-Type", ct)
                try:
                    with urllib.request.urlopen(req, timeout=90) as r:
                        data = r.read()
                        self.send_response(r.status)
                        for k, v in r.headers.items():
                            if k.lower() in ("server", "transfer-encoding",
                                             "connection", "content-length",
                                             "date"):
                                continue
                            self.send_header(k, v)
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        if method != "HEAD":
                            self.wfile.write(data)
                except urllib.error.HTTPError as e:
                    data = e.read()
                    self.send_response(e.code)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    if method != "HEAD":
                        self.wfile.write(data)
                except Exception:  # pylint: disable=broad-except
                    try:
                        self.send_error(502)
                    except Exception:  # pylint: disable=broad-except
                        pass

            def do_GET(self):  # pylint: disable=invalid-name
                self._proxy("GET")

            def do_HEAD(self):  # pylint: disable=invalid-name
                self._proxy("HEAD")

            def do_PUT(self):  # pylint: disable=invalid-name
                self._proxy("PUT")

            def do_POST(self):  # pylint: disable=invalid-name
                self._proxy("POST")

        return Handler

    def start(self, port=0):
        self.server = _GatewayServer(("127.0.0.1", port),
                                     self._handler_class())
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()
        return self.port

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.close_connections()
            self.server.server_close()
            self.server = None

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"


def green_pixels(page, shot):
    page.screenshot(path=shot)
    from PIL import Image
    im = Image.open(shot).convert("RGB")
    w, h = im.size
    px = im.load()
    return sum(1 for y in range(0, h, 8) for x in range(0, w, 8)
               if px[x, y][1] > 120 and px[x, y][1] > px[x, y][0] + 30
               and px[x, y][1] > px[x, y][2] + 30)


def _stage_a(mgr, browser, tmp, fst_fail, div_t):
    """A. single waveform full render; returns (res, page)."""
    print("\n-- A. single waveform --")
    console = []
    res = mgr.open_view(
        [fst_fail],
        signals=[{"path": "top.err", "color": "red", "group": "suspects"},
                 {"path": "top.cnt", "group": "suspects",
                  "format": "hex"}],
        cursor={"time": str(div_t), "unit": "ps"},
        markers=[{"time": str(div_t), "unit": "ps",
                  "label": "fail point", "color": "red"}],
        annotations=[{"markdown": "## Analysis\n`err` rises at "
                                  f"[{div_t}ps](#t={div_t}ps)",
                      "confidence": "high",
                      "evidence": ["e1", "e2"]}])
    check("A: view opened", res.get("available"), str(res))
    bad = mgr.open_view([fst_fail], cursor={"time_units": 1})
    check("A: bad cursor rejected with guidance",
          bad.get("error_type") == "invalid_argument"
          and "time_units" in bad.get("error", ""), str(bad))
    page = browser.new_page(viewport={"width": 1500, "height": 800})
    page.on("console", lambda m: console.append(m.text[:200]))
    page.goto(res["url"], wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(9000)
    check("A: waveform rendered",
          green_pixels(page, f"{tmp}/a.png") > 15)
    log_text = page.eval_on_selector("#log-body", "el => el.innerText")
    check("A: annotation in log popup", "Analysis" in log_text)
    check("A: evidence rendered", "e1" in log_text)
    errs = [c for c in console if "ERROR" in c]
    check("A: zero surfer errors", not errs, str(errs[:2]))
    return res, page


def _stage_bed(mgr, page, vid, tmp):
    """B (live updates) + E (log popup) + D (bidirectional awareness)."""
    # ---- B. live update: annotation append + cursor reboot --------
    print("\n-- B. live updates --")
    up = mgr.update_view(vid, annotations=[
        {"id": "u1", "markdown": "second finding",
         "confidence": "medium"}])
    check("B: update accepted", up.get("available"), str(up))
    page.wait_for_timeout(2500)
    log_text = page.eval_on_selector("#log-body", "el => el.innerText")
    check("B: annotation live-appended", "second finding" in log_text)

    up = mgr.update_view(vid, cursor={"time": "150000", "unit": "ps"},
                         signals=[{"path": "top.clk"},
                                  {"path": "top.err", "color": "red"}])
    check("B: cursor+signals update accepted", up.get("available"))
    page.wait_for_timeout(6000)   # shell reboots the iframe
    src = page.eval_on_selector("#surfer", "el => el.src")
    check("B: iframe rebooted with new commands",
          "cursor_set%20150000" in src or "cursor_set 150000" in src,
          src[-120:])
    check("B: waveform still rendered after reboot",
          green_pixels(page, f"{tmp}/b.png") > 15)

    # ---- E. log popup collapse/expand + unread badge ---------------
    print("\n-- E. log popup behavior --")
    page.click("#log-collapse")
    check("E: collapses to capsule",
          page.eval_on_selector("#log-panel",
                                "el => el.classList.contains('collapsed')")
          and page.eval_on_selector("#log-capsule",
                                    "el => el.classList.contains('visible')"))
    mgr.update_view(vid, annotations=[
        {"id": "u2", "markdown": "while collapsed"}])
    page.wait_for_timeout(2500)
    check("E: unread badge on new annotation",
          page.eval_on_selector("#log-capsule",
                                "el => el.classList.contains('unread')"))
    page.click("#log-capsule")
    check("E: expands again",
          not page.eval_on_selector(
              "#log-panel", "el => el.classList.contains('collapsed')"))
    log_text = page.eval_on_selector("#log-body", "el => el.innerText")
    check("E: collapsed-time annotation present",
          "while collapsed" in log_text)

    # ---- D. delivery awareness --------------------------------------
    print("\n-- D. actual write-back --")
    page.wait_for_timeout(1500)
    gs = mgr.get_state(vid)
    check("D: get_state available", gs.get("available"), str(gs))
    check("D: browser reported liveness",
          gs["actual"]["updated_at"] is not None)
    check("D: applied revision tracked",
          gs["actual"]["applied_revision"] >= 1,
          str(gs["actual"]))
    check("D: desired summary coherent",
          gs["desired_summary"]["annotations"] == 3
          and "top.clk" in gs["desired_summary"]["signals"],
          str(gs["desired_summary"]))
    page.close()


def _stage_c(mgr, browser, tmp, fst_pass, fst_fail, div_t):
    """C. dual waveform diff view; returns the second view's res dict."""
    print("\n-- C. dual waveform diff view --")
    from wave_mcp.diff import diff_waveforms
    d = diff_waveforms([fst_pass, fst_fail])
    check("C: diff finds divergence",
          d["first_divergence"]["time_units"] == div_t, str(d))
    res2 = mgr.open_view(
        [fst_pass, fst_fail],
        signals=[{"path": p["path"], "color": "red"}
                 for p in d["diverging_signals"][:2]],
        cursor={"time": str(div_t), "unit": "ps"},
        diff={"source_a": "a", "source_b": "b",
              "first_divergence": {"time": str(div_t), "unit": "ps"}},
        labels=["pass", "fail"])
    check("C: dual view opened", res2.get("available"), str(res2))
    console2 = []
    page2 = browser.new_page(viewport={"width": 1500, "height": 900})
    page2.on("console", lambda m: console2.append(m.text[:200]))
    page2.goto(res2["url"], wait_until="domcontentloaded", timeout=45000)
    page2.wait_for_timeout(12000)
    check("C: compare layout active (pane B visible)",
          page2.eval_on_selector(
              "#pane-b", "el => el.style.display !== 'none'"))
    check("C: pane labels show pass/fail",
          page2.eval_on_selector("#label-a", "el => el.textContent")
          == "pass"
          and page2.eval_on_selector("#label-b", "el => el.textContent")
          == "fail")
    check("C: both panes render waveforms",
          green_pixels(page2, f"{tmp}/c.png") > 30)
    check("C: no file picker stall",
          not any("no waveform loaded" in c for c in console2),
          str([c for c in console2 if "ERROR" in c][:2]))
    # compare sync: agent-set navigation is injected into BOTH panes
    # (the shell no longer follows a manual pane-A zoom: reading the
    # viewer's state from the shell crossed the license isolation boundary)
    mgr.update_view(res2["view_id"],
                    viewport={"from": "60000", "to": "90000",
                              "unit": "ps"})
    page2.wait_for_timeout(3000)
    vb = page2.evaluate("""async () => {
        const w = document.getElementById('surfer-b').contentWindow;
        if (!w.__wv_get_state) {
            w.eval("import('./surfer.js').then(m=>{" +
                   "window.__wv_get_state=m.get_state;})");
            await new Promise(r => setTimeout(r, 1500));
        }
        const s = String(await w.__wv_get_state());
        const l = s.match(/curr_left: \\(([-0-9.e]+)\\)/);
        const r2 = s.match(/curr_right: \\(([-0-9.e]+)\\)/);
        return l && r2 ? [parseFloat(l[1]), parseFloat(r2[1])] : null;
    }""")
    end_t = 100 * 2000
    ok_sync = (vb is not None
               and abs(vb[0] * end_t - 60000) < 5000
               and abs(vb[1] * end_t - 90000) < 5000)
    check("C: agent viewport update reaches pane B", ok_sync, str(vb))
    page2.close()
    return res2


def _stage_f(mgr, res, res2, vid):
    print("\n-- F. view isolation --")
    gs1 = mgr.get_state(vid)
    gs2 = mgr.get_state(res2["view_id"])
    check("F: separate URLs", res["url"] != res2["url"])
    check("F: separate states",
          gs1["desired_summary"]["annotations"] == 3
          and gs2["desired_summary"]["annotations"] == 0,
          f"{gs1['desired_summary']} vs {gs2['desired_summary']}")
    mgr.update_view(res2["view_id"], annotations=[
        {"markdown": "only view2"}])
    gs1b = mgr.get_state(vid)
    check("F: update targets one view only",
          gs1b["desired_summary"]["annotations"] == 3)


def _stage_g(mgr, res2, fst_pass, fst_fail):
    print("\n-- G. surver reuse --")
    res3 = mgr.open_view([fst_pass, fst_fail])
    tok2 = res2["url"].split("token=")[1]
    tok3 = res3["url"].split("token=")[1]
    check("G: same file set reuses surver (same token)", tok2 == tok3)
    res4 = mgr.open_view([fst_fail])
    tok4 = res4["url"].split("token=")[1]
    check("G: different file set gets its own surver", tok4 != tok2)


def _stage_i(mgr, browser, tmp, fst_fail):
    print("\n-- I. flicker-free updates --")
    res5 = mgr.open_view([fst_fail],
                         signals=[{"path": "top.cnt"},
                                  {"path": "top.err"}])
    page5 = browser.new_page(viewport={"width": 1400, "height": 700})
    page5.goto(res5["url"], wait_until="domcontentloaded", timeout=45000)
    page5.wait_for_timeout(9000)
    src_before = page5.eval_on_selector("#surfer", "el => el.src")
    up = mgr.update_view(res5["view_id"],
                         cursor={"time": "150000", "unit": "ps"},
                         markers=[{"time": "150000", "unit": "ps",
                                   "label": "nav", "color": "red"}])
    check("I: nav update accepted", up.get("available"))
    page5.wait_for_timeout(3000)
    src_after = page5.eval_on_selector("#surfer", "el => el.src")
    check("I: cursor-only update does NOT reboot iframe",
          src_before == src_after,
          f"{src_before[-60:]} -> {src_after[-60:]}")
    # license isolation: the shell no longer reads viewer state, so a user
    # click must NOT surface in actual (cursor stays unset, user_dirty False)
    box5 = page5.query_selector("#surfer").bounding_box()
    page5.mouse.click(box5["x"] + box5["width"] * 0.75,
                      box5["y"] + box5["height"] * 0.25)
    page5.wait_for_timeout(3500)
    gs5 = mgr.get_state(res5["view_id"])
    check("I: no viewer-state readback (cursor not written back)",
          gs5["actual"].get("cursor") is None,
          str(gs5["actual"].get("cursor")))
    check("I: user_dirty stays False", not gs5["actual"].get("user_dirty"),
          str(gs5["actual"]))
    check("I: page still ready after click",
          gs5["actual"].get("page_ready") is True, str(gs5["actual"]))
    page5.close()


def _stage_j(browser, res, tmp):
    """J. bounded entry when service workers are blocked."""
    print("\n-- J. bounded entry with blocked service workers --")
    ctx_j = browser.new_context(viewport={"width": 1200, "height": 700},
                                service_workers="block")
    page_j = ctx_j.new_page()
    t0_j = time.time()
    page_j.goto(res["url"], wait_until="domcontentloaded", timeout=45000)
    reached = False
    try:
        page_j.wait_for_selector("#surfer", timeout=15000)
        reached = True
    except Exception:  # pylint: disable=broad-except
        pass
    check("J: shell reached with blocked workers", reached,
          f"took {time.time() - t0_j:.1f}s")
    # the direct path must still come up without a worker: poll until
    # the overlay clears (backend probe succeeded) or the budget ends
    ok_render, overlay_done = False, False
    deadline_j = time.time() + 20
    while time.time() < deadline_j:
        try:
            overlay_done = "done" in (page_j.eval_on_selector(
                "#wv-boot", "el => el.className") or "")
        except Exception:  # pylint: disable=broad-except
            overlay_done = False
        ok_render = green_pixels(page_j, f"{tmp}/j.png") > 15
        if ok_render and overlay_done:
            break
        time.sleep(1.5)
    check("J: direct path still renders", ok_render)
    check("J: boot overlay cleared", overlay_done)
    ctx_j.close()


def _stage_k(browser, res, tmp):
    """K. gateway with a rewritten Server header."""
    print("\n-- K. gateway with rewritten Server header --")
    import http.server as _hs
    import socketserver as _ss
    import threading as _th
    import urllib.error as _ue
    import urllib.request as _ur

    _target = res["url"].split("/view.html")[0]
    _token = res["url"].split("token=")[1]

    class _RewriteProxy(_hs.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def version_string(self):
            return "openresty/1.21.4.1"        # the rewrite in question

        def log_message(self, *a):
            pass

        def _p(self, method):
            body = None
            if method in ("PUT", "POST"):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
            req = _ur.Request(_target + self.path, method=method,
                              data=body)
            ct = self.headers.get("Content-Type")
            if ct:
                req.add_header("Content-Type", ct)
            try:
                with _ur.urlopen(req, timeout=90) as r:
                    data = r.read()
                    self.send_response(r.status)
                    for k, v in r.headers.items():
                        if k.lower() in ("server", "transfer-encoding",
                                         "connection", "content-length",
                                         "date"):
                            continue
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    if method != "HEAD":
                        self.wfile.write(data)
            except _ue.HTTPError as e:
                data = e.read()
                self.send_response(e.code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if method != "HEAD":
                    self.wfile.write(data)
            except Exception:  # pylint: disable=broad-except
                try:
                    self.send_error(502)
                except Exception:  # pylint: disable=broad-except
                    pass

        def do_GET(self):  # pylint: disable=invalid-name
            self._p("GET")

        def do_HEAD(self):  # pylint: disable=invalid-name
            self._p("HEAD")

        def do_PUT(self):  # pylint: disable=invalid-name
            self._p("PUT")

        def do_POST(self):  # pylint: disable=invalid-name
            self._p("POST")

    proxy_k = _ss.ThreadingTCPServer(("127.0.0.1", 0), _RewriteProxy)
    proxy_k.daemon_threads = True
    _th.Thread(target=proxy_k.serve_forever, daemon=True).start()
    url_k = (f"http://127.0.0.1:{proxy_k.server_address[1]}"
             f"/view.html?token={_token}")
    ctx_k = browser.new_context(viewport={"width": 1200, "height": 700})
    page_k = ctx_k.new_page()
    try:
        page_k.goto(url_k, wait_until="domcontentloaded", timeout=45000)
        ok_k, done_k = False, False
        deadline_k = time.time() + 30
        while time.time() < deadline_k:
            ok_k = green_pixels(page_k, f"{tmp}/k.png") > 15
            try:
                done_k = "done" in (page_k.eval_on_selector(
                    "#wv-boot", "el => el.className") or "")
            except Exception:  # pylint: disable=broad-except
                done_k = False
            if ok_k and done_k:
                break
            time.sleep(1.5)
        check("K: gateway path renders", ok_k)
        check("K: gateway overlay cleared", done_k)
    finally:
        ctx_k.close()
        proxy_k.shutdown()
        proxy_k.server_close()


def _stage_l(mgr, browser, fst_fail):
    """L. injected navigation actually lands."""
    print("\n-- L. injected navigation lands --")
    res_l = mgr.open_view([fst_fail],
                          signals=[{"path": "top.cnt"},
                                   {"path": "top.err"}],
                          cursor={"time": "150000", "unit": "ps"})
    page_l = browser.new_page(viewport={"width": 1200, "height": 700})
    page_l.goto(res_l["url"], wait_until="domcontentloaded",
                timeout=45000)
    check("L: boot settles", wait_boot_done(page_l, 30))
    time.sleep(3)
    st = frame_state(page_l)
    check("L: boot cursor lands", st is not None and st["cursor"] == 150000,
          str(st))

    mgr.update_view(res_l["view_id"],
                    viewport={"from": "0", "to": "100000",
                              "unit": "ps"})
    time.sleep(3)
    st = frame_state(page_l)
    win = (round(st["left"] * 200000), round(st["right"] * 200000)) \
        if st is not None and st["left"] is not None else None
    check("L: viewport-only update lands on the requested window",
          win is not None and abs(win[0]) < 5000
          and abs(win[1] - 100000) < 5000,
          f"window={win}, cursor 150000 sat outside it")

    mgr.update_view(res_l["view_id"],
                    cursor={"time": "50000", "unit": "ps"})
    time.sleep(3)
    st = frame_state(page_l)
    win2 = (round(st["left"] * 200000), round(st["right"] * 200000)) \
        if st is not None and st["left"] is not None else None
    check("L: cursor-only update lands", st is not None
          and st["cursor"] == 50000, str(st))
    check("L: cursor-only update keeps the window",
          win2 is not None and abs(win2[0]) < 5000
          and abs(win2[1] - 100000) < 5000,
          f"window={win2}")

    mgr.update_view(res_l["view_id"],
                    markers=[{"time": "50000", "unit": "ps",
                              "label": "m1", "color": "red"}])
    time.sleep(3)
    st = frame_state(page_l)
    check("L: marker update lands",
          st is not None and 50000 in st["markers"], str(st))

    mgr.update_view(res_l["view_id"],
                    viewport={"from": "100000", "to": "200000",
                              "unit": "ps"})
    time.sleep(3)
    st = frame_state(page_l)
    win3 = (round(st["left"] * 200000), round(st["right"] * 200000)) \
        if st is not None and st["left"] is not None else None
    check("L: outside-cursor viewport lands (regression)",
          win3 is not None and abs(win3[0] - 100000) < 5000
          and abs(win3[1] - 200000) < 5000,
          f"window={win3}, cursor 50000 sat outside")
    page_l.close()


def _stage_m(mgr, browser, fst_fail):
    """M. no user-interaction readback (license isolation)."""
    print("\n-- M. no viewer-state readback --")
    res_m = mgr.open_view([fst_fail], signals=[{"path": "top.cnt"}],
                          cursor={"time": "1000", "unit": "ps"})
    page_m = browser.new_page(viewport={"width": 1200, "height": 700})
    page_m.goto(res_m["url"], wait_until="domcontentloaded",
                timeout=45000)
    check("M: boot settles", wait_boot_done(page_m, 30))
    time.sleep(9)
    gs = mgr.get_state(res_m["view_id"])
    check("M: starts clean", gs["actual"].get("user_dirty") is False,
          str(gs["actual"].get("user_dirty")))

    mgr.update_view(res_m["view_id"],
                    cursor={"time": "80000", "unit": "ps"})
    time.sleep(4)
    gs = mgr.get_state(res_m["view_id"])
    check("M: agent cursor move stays clean",
          gs["actual"].get("user_dirty") is False,
          str(gs["actual"].get("user_dirty")))
    st_m = frame_state(page_m)
    check("M: injected cursor reached the pane",
          st_m is not None and st_m["cursor"] == 80000, str(st_m))

    box_m = page_m.query_selector("#surfer").bounding_box()
    page_m.mouse.click(box_m["x"] + box_m["width"] * 0.8,
                       box_m["y"] + box_m["height"] * 0.4)
    time.sleep(4)
    gs = mgr.get_state(res_m["view_id"])
    check("M: a user click is NOT read back (stays clean)",
          gs["actual"].get("user_dirty") is False
          and gs["actual"].get("cursor") is None,
          str(gs["actual"]))
    page_m.close()


def _stage_n(mgr, browser, tmp, fst_fail):
    """N. IDE-sized viewports."""
    print("\n-- N. narrow viewport (IDE split pane) --")
    res_n = mgr.open_view([fst_fail],
                          signals=[{"path": "top.err"},
                                   {"path": "top.cnt"}])
    ctx_n = browser.new_context(viewport={"width": 430, "height": 760})
    page_n = ctx_n.new_page()
    page_n.goto(res_n["url"], wait_until="domcontentloaded",
                timeout=45000)
    check("N: boot settles at 430px", wait_boot_done(page_n, 30))
    no_overflow = page_n.evaluate(
        "() => document.documentElement.scrollWidth"
        " <= window.innerWidth + 1")
    check("N: no horizontal overflow at 430px", no_overflow)
    st_n = frame_state(page_n)
    check("N: pane alive at 430px (signals loaded)",
          st_n is not None, str(st_n))
    page_n.set_viewport_size({"width": 700, "height": 760})
    ok_n = False
    deadline_n = time.time() + 25
    while time.time() < deadline_n:
        if green_pixels(page_n, f"{tmp}/n700.png") > 5:
            ok_n = True
            break
        time.sleep(1.5)
    check("N: waveform renders at 700px", ok_n)
    ctx_n.close()


def _stage_o(browser, res):
    """O. sandboxed webview: no storage, no workers, gateway."""
    print("\n-- O. sandboxed webview (no storage, no workers) --")
    gw_o = Gateway(res["url"].split("/view.html")[0])
    gw_o.start()
    token_o = res["url"].split("token=")[1]
    ctx_o = browser.new_context(viewport={"width": 900, "height": 700},
                                service_workers="block")
    ctx_o.add_init_script(
        "for (const k of ['sessionStorage', 'localStorage']) {"
        "  Object.defineProperty(window, k, {"
        "    configurable: true,"
        "    get() { throw new DOMException('denied', 'SecurityError'); }"
        "  });"
        "}")
    page_o = ctx_o.new_page()
    page_o.goto(gw_o.url(f"/view.html?token={token_o}"),
                wait_until="domcontentloaded", timeout=45000)
    denied = page_o.evaluate(
        "() => { try { window.sessionStorage; return false; }"
        " catch (e) { return true; } }")
    check("O: storage really denied", denied)
    settled_o, txt_o = False, ""
    deadline_o = time.time() + 30
    while time.time() < deadline_o:
        try:
            cls_o = page_o.eval_on_selector(
                "#wv-boot", "el => el.className") or ""
            txt_o = page_o.eval_on_selector(
                "#wv-boot-text", "el => el.textContent") or ""
        except Exception:  # pylint: disable=broad-except
            cls_o = ""
        if "stuck" in cls_o:
            settled_o = True
            break
        time.sleep(1)
    check("O: settles on an explicit error (never a stuck spinner)",
          settled_o, f"overlay text: {txt_o[:80]!r}")
    ctx_o.close()
    gw_o.stop()


def _stage_p(mgr, browser, res, tmp):
    """P. backend drop and recovery (sleep / VPN rebuild)."""
    print("\n-- P. backend lost and recovered --")
    gw_p = Gateway(res["url"].split("/view.html")[0])
    gw_p.start()
    port_p = gw_p.port
    ctx_p = browser.new_context(viewport={"width": 1100, "height": 700})
    page_p = ctx_p.new_page()
    logs_p = []
    page_p.on("console", lambda m: logs_p.append(m.text))
    token_p = res["url"].split("token=")[1]
    page_p.goto(gw_p.url(f"/view.html?token={token_p}"),
                wait_until="domcontentloaded", timeout=45000)
    base_ok = wait_boot_done(page_p, 30)
    render_p = False
    deadline_p = time.time() + 20
    while time.time() < deadline_p:
        if green_pixels(page_p, f"{tmp}/p1.png") > 15:
            render_p = True
            break
        time.sleep(1.5)
    check("P: baseline renders before the drop", base_ok and render_p)

    gw_p.stop()                       # the tunnel goes away
    try:
        # wake the in-flight long-poll so the failure is noticed fast
        mgr.update_view(res["view_id"],
                        annotations=[{"id": "recovery-probe",
                                      "markdown": "recovery probe"}])
    except Exception:  # pylint: disable=broad-except
        pass
    err_seen = False
    deadline_p = time.time() + 20
    while time.time() < deadline_p:
        try:
            err_seen = page_p.eval_on_selector(
                "#wv-error", "el => el.classList.contains('visible')")
        except Exception:  # pylint: disable=broad-except
            err_seen = False
        if err_seen:
            break
        time.sleep(1.5)
    check("P: the disconnect is surfaced on the page", err_seen)
    time.sleep(7)                     # let the failure streak build

    gw_p.start(port_p)                # the tunnel is rebuilt
    recovered, err_gone = False, False
    deadline_p = time.time() + 40
    while time.time() < deadline_p:
        recovered = green_pixels(page_p, f"{tmp}/p2.png") > 15
        try:
            err_gone = not page_p.eval_on_selector(
                "#wv-error", "el => el.classList.contains('visible')")
        except Exception:  # pylint: disable=broad-except
            err_gone = False
        if recovered and err_gone:
            break
        time.sleep(2)
    check("P: stream reconnect was triggered",
          any("reconnecting the waveform stream" in l for l in logs_p))
    check("P: page healthy after the backend returns",
          recovered and err_gone,
          f"rendered={recovered} error_cleared={err_gone}")
    st_p = frame_state(page_p)
    check("P: pane state readable after recovery", st_p is not None,
          str(st_p))
    ctx_p.close()
    gw_p.stop()


def _stage_q(mgr, browser, tmp, fst_fail):
    """Q. two tabs on the same view."""
    print("\n-- Q. two tabs on one view --")
    res_q = mgr.open_view([fst_fail], signals=[{"path": "top.cnt"}])
    ctx_q = browser.new_context(viewport={"width": 1100, "height": 700})
    tab1 = ctx_q.new_page()
    tab2 = ctx_q.new_page()
    tab1.goto(res_q["url"], wait_until="domcontentloaded", timeout=45000)
    tab2.goto(res_q["url"], wait_until="domcontentloaded", timeout=45000)
    ok1 = wait_boot_done(tab1, 30)
    ok2 = wait_boot_done(tab2, 30)
    r1 = r2 = False
    deadline_q = time.time() + 25
    while time.time() < deadline_q:
        r1 = green_pixels(tab1, f"{tmp}/q1.png") > 15
        r2 = green_pixels(tab2, f"{tmp}/q2.png") > 15
        if r1 and r2:
            break
        time.sleep(1.5)
    check("Q: both tabs render the same view",
          ok1 and ok2 and r1 and r2,
          f"boot={ok1},{ok2} render={r1},{r2}")
    # closing one tab and reopening the URL (the everyday IDE gesture)
    tab2.close()
    tab3 = ctx_q.new_page()
    tab3.goto(res_q["url"], wait_until="domcontentloaded", timeout=45000)
    ok3 = wait_boot_done(tab3, 30)
    r3 = False
    deadline_q2 = time.time() + 20
    while time.time() < deadline_q2:
        r3 = green_pixels(tab3, f"{tmp}/q3.png") > 15
        if r3:
            break
        time.sleep(1.5)
    check("Q: a reopened tab still renders", ok3 and r3,
          f"boot={ok3} render={r3}")
    ctx_q.close()


def _stage_h(fst_fail):
    """H. wave-view CLI smoke (subprocess)."""
    print("\n-- H. wave-view CLI --")
    import signal
    import subprocess
    import urllib.request
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "wave_mcp.viewer.cli", fst_fail,
         "--signals", "top.err", "--cursor", "85000ps", "--no-browser"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        cwd=ROOT, env={**os.environ})
    out_lines = []
    try:
        import threading as _th
        url_box = {}

        def _reader():
            for line in proc.stdout:
                out_lines.append(line)
                if "Viewer running at" in line:
                    url_box["url"] = line.split("at", 1)[1].strip()
                    return

        rd = _th.Thread(target=_reader, daemon=True)
        rd.start()
        rd.join(timeout=25)
        url = url_box.get("url")
        check("H: CLI prints viewer URL", url is not None,
              "".join(out_lines)[:200])
        if url:
            with urllib.request.urlopen(url.replace("/view.html?token=",
                                                    "/api/view-state?x="),
                                        timeout=5) as r:
                snap = json.loads(r.read())
            check("H: CLI view-state has cursor from --cursor",
                  snap["desired"]["cursor"]["time"] == "85000",
                  str(snap["desired"]["cursor"]))
            check("H: CLI signal registered",
                  snap["desired"]["signals"][0]["path"] == "top.err")
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    check("H: CLI exits clean on Ctrl-C", proc.returncode in (0, -2),
          str(proc.returncode))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="wave_e2e_")
    fst_pass, fst_fail = clocked_pair(tmp, tmp, diverge_cycle=42)
    div_t = 42 * 2000 + 1000                                  # 85000 ps
    mgr = ViewManager.instance()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=["--enable-features=SharedArrayBuffer"])

        res, page = _stage_a(mgr, browser, tmp, fst_fail, div_t)
        vid = res["view_id"]
        _stage_bed(mgr, page, vid, tmp)
        res2 = _stage_c(mgr, browser, tmp, fst_pass, fst_fail, div_t)
        _stage_f(mgr, res, res2, vid)
        _stage_g(mgr, res2, fst_pass, fst_fail)
        _stage_i(mgr, browser, tmp, fst_fail)
        _stage_j(browser, res, tmp)
        _stage_k(browser, res, tmp)
        _stage_l(mgr, browser, fst_fail)
        _stage_m(mgr, browser, fst_fail)
        _stage_n(mgr, browser, tmp, fst_fail)
        _stage_o(browser, res)
        _stage_p(mgr, browser, res, tmp)
        _stage_q(mgr, browser, tmp, fst_fail)

        browser.close()

    _stage_h(fst_fail)

    print(f"\n  e2e suite: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("  failed:", FAILED)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
