#!/usr/bin/env python3
"""Capture viewer screenshots for the demo documentation.

Opens each demo session in the browser viewer (surver + surfer wasm) via a
headless chromium and writes a PNG per scenario into docs/images/viewer/.

Usage:
    python3 examples/viewer_demos/capture_screenshots.py
    python3 examples/viewer_demos/capture_screenshots.py xprop cdc

Requires: playwright + chromium, viewer assets available (WAVE_MCP_VIEWER_ASSETS
or the wave-mcp-viewer-assets package). Exits 0 with SKIP when unavailable so it
never breaks a CI run.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "docs" / "images" / "viewer"
WAVES = HERE / "waves"

# Per-scenario view spec: the signals, cursor, marker and annotation that make
# the screenshot tell the story of that bug.
SCENARIOS = {
    "xprop": {
        "session": "session_xprop",
        "fst": "xprop.fst",
        "title": "X propagation",
        "signals": [
            {"path": "xprop_tb.dut.data_out", "color": "red", "group": "symptom"},
            {"path": "xprop_tb.dut.done", "group": "symptom"},
            {"path": "xprop_tb.dut.shreg", "group": "cause", "format": "hex"},
            {"path": "xprop_tb.dut.fsm_state", "group": "cause"},
            {"path": "xprop_tb.dut.rst_n", "group": "cause"},
        ],
        "annotation": "## X propagation\n`data_out` carries X: `shreg` is never fully "
                      "initialised before the FLUSH state drives the output.",
    },
    "fsm_stuck": {
        "session": "session_fsm_stuck",
        "fst": "fsm_stuck.fst",
        "title": "FSM deadlock",
        "signals": [
            {"path": "fsm_stuck_tb.dut.state", "color": "red", "group": "fsm"},
            {"path": "fsm_stuck_tb.dut.ack", "group": "fsm"},
            {"path": "fsm_stuck_tb.dut.req", "group": "fsm"},
            {"path": "fsm_stuck_tb.ack_wanted", "group": "testbench"},
            {"path": "fsm_stuck_tb.dut.rd_count", "group": "testbench"},
        ],
        "annotation": "## FSM deadlock\nThe read FSM parks in `WAIT_ACK`: "
                      "`ack` never returns, so `rd_count` stops advancing.",
    },
    "cdc": {
        "session": "session_cdc",
        "fst": "cdc.fst",
        "title": "CDC pulse loss",
        "signals": [
            {"path": "cdc_tb.dut.pulse_fast", "group": "fast domain"},
            {"path": "cdc_tb.dut.clk_fast", "group": "fast domain"},
            {"path": "cdc_tb.dut.pulse_seen", "color": "red", "group": "slow domain"},
            {"path": "cdc_tb.dut.clk_slow", "group": "slow domain"},
            {"path": "cdc_tb.dut.pulse_count", "group": "slow domain"},
        ],
        "annotation": "## CDC pulse loss\n`pulse_fast` is narrower than one `clk_slow` "
                      "period, so the slow domain never samples it and `pulse_count` stalls.",
    },
    "crc_diff": {
        "session": "session_crc_diff",
        "fst": "crc_pass.fst",
        "fst_b": "crc_fail.fst",
        "title": "Pass/fail divergence",
        "signals": [
            {"path": "crc_diff_tb.dut.crc", "color": "red", "group": "crc",
             "format": "hex"},
            {"path": "crc_diff_tb.dut.crc_err", "group": "crc"},
            {"path": "crc_diff_tb.dut.data", "group": "stimulus"},
            {"path": "crc_diff_tb.dut.valid", "group": "stimulus"},
        ],
        "annotation": "## First divergence\nSame stimulus, one CRC tap typo. "
                      "The overlay shows where the pass and fail runs first differ.",
    },
}


def main(argv: list[str]) -> int:
    from wave_mcp.viewer import find_assets

    if find_assets() is None:
        print("[SKIP] viewer assets not found; screenshot capture skipped")
        return 0
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[SKIP] playwright not installed; screenshot capture skipped")
        return 0

    from wave_mcp.viewer.manager import ViewManager

    wanted = argv or list(SCENARIOS)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # relative waveform paths below are resolved against the repo root
    os.chdir(ROOT)
    mgr = ViewManager.instance()
    failures: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=["--no-sandbox", "--enable-features=SharedArrayBuffer"])
        for name in wanted:
            spec = SCENARIOS.get(name)
            if spec is None:
                print(f"[WARN] unknown scenario: {name}")
                continue
            fst = WAVES / spec["fst"]
            if not fst.exists():
                print(f"[SKIP] {name}: {fst.name} missing, run ./make_all.sh first")
                continue

            # surfer echoes the resolved waveform path in its title bar and
            # status bar. Stage the FSTs under a short neutral directory so the
            # published screenshots never show a local home path.
            stage = Path(tempfile.gettempdir()) / "wave-demos"
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
            stage.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fst, stage / spec["fst"])
            paths = [str(stage / spec["fst"])]
            if spec.get("fst_b") and (WAVES / spec["fst_b"]).exists():
                shutil.copy2(WAVES / spec["fst_b"], stage / spec["fst_b"])
                paths.append(str(stage / spec["fst_b"]))

            res = mgr.open_view(
                paths,
                signals=spec["signals"],
                annotations=[{"markdown": spec["annotation"],
                              "confidence": "high"}],
            )
            if not res.get("available"):
                print(f"[FAIL] {name}: open_view unavailable: {res}")
                failures.append(name)
                continue

            page = browser.new_page(viewport={"width": 1600, "height": 900})
            page.goto(res["url"], wait_until="domcontentloaded", timeout=45000)
            # view.html is a bootstrap page that redirects to shell.html, which
            # hosts the Surfer WASM app inside an <iframe id="surfer">. Wait for
            # that iframe's canvas to actually paint before capturing.
            try:
                page.wait_for_url("**/shell.html*", timeout=30000)
            except Exception:
                pass
            try:
                page.wait_for_selector("#surfer", timeout=30000)
                frame = page.frame_locator("#surfer")
                frame.locator("canvas").first.wait_for(timeout=45000)
            except Exception as exc:
                print(f"     [WARN] {name}: canvas wait failed: {str(exc)[:120]}")
            # let surfer stream values and settle the layout
            page.wait_for_timeout(12000)
            out = OUT_DIR / f"{name}.png"
            # surfer's bottom status bar echoes the absolute waveform path that
            # surver resolved, so crop it out of the published screenshot.
            vp = page.viewport_size or {"width": 1600, "height": 900}
            page.screenshot(path=str(out), clip={
                "x": 0, "y": 0,
                "width": vp["width"], "height": vp["height"] - 22})
            page.close()
            size = out.stat().st_size
            print(f"[OK] {name:10} -> {out.relative_to(ROOT)}  ({size // 1024} KB)")
            if size < 20000:
                print(f"     [WARN] {name}: image looks blank ({size} bytes)")
                failures.append(name)
        browser.close()

    if failures:
        print(f"\n{len(failures)} scenario(s) need attention: {failures}")
        return 1
    print(f"\nAll screenshots written to {OUT_DIR.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main([a for a in sys.argv[1:] if not a.startswith("-")]))
