#!/usr/bin/env python3
"""Viewer browser E2E suite (multi-scenario).

Needs viewer assets (WAVE_MCP_VIEWER_ASSETS / pip package / cache) and
playwright + chromium. Auto-skips (exit 0 with SKIP) when unavailable,
so the regression entry can always invoke it.

Scenarios:
  A. single waveform: signals/cursor/marker/annotation render, log popup
  B. update_wave_view: annotation live-append + cursor change reboots view
  C. dual waveform diff view: auto-select fail side, divergence marker
  D. bidirectional awareness: actual write-back + get_view_state
  E. log popup collapse/expand + unread badge
  F. two concurrent views stay isolated
  G. surver reuse for the same file set
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

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


def green_pixels(page, shot):
    page.screenshot(path=shot)
    from PIL import Image
    im = Image.open(shot).convert("RGB")
    w, h = im.size
    px = im.load()
    return sum(1 for y in range(0, h, 8) for x in range(0, w, 8)
               if px[x, y][1] > 120 and px[x, y][1] > px[x, y][0] + 30
               and px[x, y][1] > px[x, y][2] + 30)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="wave_e2e_")
    fst_pass, fst_fail = clocked_pair(tmp, tmp, diverge_cycle=42)
    div_t = 42 * 2000 + 1000                                  # 85000 ps
    mgr = ViewManager.instance()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=["--enable-features=SharedArrayBuffer"])

        # ---- A. single waveform full render ---------------------------
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

        # ---- B. live update: annotation append + cursor reboot --------
        print("\n-- B. live updates --")
        vid = res["view_id"]
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

        # ---- D. bidirectional awareness --------------------------------
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

        # ---- C. dual waveform diff view ---------------------------------
        print("\n-- C. dual waveform diff view --")
        from wave_mcp.diff import diff_waveforms
        d = diff_waveforms(fst_pass, fst_fail)
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
        # lockstep: zoom pane A via injection, pane B viewport should follow
        page2.evaluate("""() => {
            document.getElementById('surfer').contentWindow.postMessage(
                {command: 'InjectMessage',
                 message: JSON.stringify({ZoomToRange: {
                     start: [1, [60000]], end: [1, [90000]],
                     viewport_idx: 0}})}, '*');
        }""")
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
        check("C: pane B viewport lockstep-follows pane A", ok_sync, str(vb))

        # ---- F. concurrent views isolated -------------------------------
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
        page2.close()

        # ---- G. surver reuse ---------------------------------------------
        print("\n-- G. surver reuse --")
        res3 = mgr.open_view([fst_pass, fst_fail])
        tok2 = res2["url"].split("token=")[1]
        tok3 = res3["url"].split("token=")[1]
        check("G: same file set reuses surver (same token)", tok2 == tok3)
        res4 = mgr.open_view([fst_fail])
        tok4 = res4["url"].split("token=")[1]
        check("G: different file set gets its own surver", tok4 != tok2)

        # ---- I. flicker-free navigation + user cursor readback ----------
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
        # user click -> actual.cursor picked up via get_state polling
        box5 = page5.query_selector("#surfer").bounding_box()
        page5.mouse.click(box5["x"] + box5["width"] * 0.75,
                          box5["y"] + box5["height"] * 0.25)
        page5.wait_for_timeout(3500)
        gs5 = mgr.get_state(res5["view_id"])
        check("I: user cursor written back",
              gs5["actual"].get("cursor") is not None
              and gs5["actual"]["cursor"].get("time") not in (None, "150000"),
              str(gs5["actual"].get("cursor")))
        check("I: user_dirty flagged", gs5["actual"].get("user_dirty") is True,
              str(gs5["actual"]))
        page5.close()

        browser.close()

    # ---- H. wave-view CLI smoke (subprocess) ---------------------------
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
                  snap["desired"]["cursor"]["time"] == "85000", str(snap["desired"]["cursor"]))
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

    print(f"\n  e2e suite: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("  failed:", FAILED)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
