#!/usr/bin/env python3
"""Refresh the two committed screenshots for docs/VIEWER_SCREENSHOTS.md.

The demo already writes its own screenshots into report/shots/ on every
run. This script promotes them into docs/images/viewer/ and additionally
shoots the HTML report itself, so the images in the docs can be
regenerated from a clean checkout instead of being hand-copied.

Run the demo first (it produces the waveform shot and the report):

    ./run_demo.sh
    python3 capture_docs_screenshots.py

Exits 0 with a message if prerequisites are missing, so it is safe to call
from a docs pipeline.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
DOCS_IMG = REPO / "docs" / "images" / "viewer"
REPORT = HERE / "report" / "index.html"
SHOTS = HERE / "report" / "shots"

# the waveform shot to promote: first failing case, whichever it is
WAVE_TARGET = DOCS_IMG / "regression_triage.png"
REPORT_TARGET = DOCS_IMG / "regression_report.png"


def promote_waveform_shot() -> bool:
    if not SHOTS.is_dir():
        print("no report/shots/ yet: run ./run_demo.sh (with a browser) first")
        return False
    shots = sorted(SHOTS.glob("*.png"))
    if not shots:
        print("report/shots/ is empty: the demo ran with --no-shots?")
        return False
    DOCS_IMG.mkdir(parents=True, exist_ok=True)
    shutil.copy2(shots[0], WAVE_TARGET)
    print(f"waveform: {shots[0].name} -> "
          f"{WAVE_TARGET.relative_to(REPO)}")
    return True


def shoot_report() -> bool:
    if not REPORT.is_file():
        print("no report/index.html yet: run ./run_demo.sh first")
        return False
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed: skipping the report screenshot")
        return False
    DOCS_IMG.mkdir(parents=True, exist_ok=True)
    url = REPORT.resolve().as_uri()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1200, "height": 1000},
                                    device_scale_factor=2)
            page.goto(url, wait_until="load", timeout=30000)
            page.wait_for_timeout(2000)
            page.screenshot(path=str(REPORT_TARGET), clip={
                "x": 0, "y": 0, "width": 1200, "height": 1000})
            browser.close()
    except Exception as exc:
        print(f"report screenshot failed: {str(exc)[:140]}")
        return False
    print(f"report:   index.html -> {REPORT_TARGET.relative_to(REPO)}")
    return True


def main() -> int:
    ok_wave = promote_waveform_shot()
    ok_report = shoot_report()
    if not (ok_wave or ok_report):
        print("nothing refreshed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
