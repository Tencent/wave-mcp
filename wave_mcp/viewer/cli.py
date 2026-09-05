"""wave-view CLI: open a waveform in the browser-based viewer.

Examples:
  wave-view dump.fst
  wave-view dump.fst --signals top.clk top.u_dma.req --cursor 1523400ps
  wave-view pass.fst fail.fst --labels pass fail
"""
from __future__ import annotations

import argparse
import re
import signal
import sys
import time

from . import try_open_browser
from .manager import ViewManager
from ..timeutil import VALID_UNITS

_TIME_RE = re.compile(r"^(\d+)([a-z]+)$")


def _parse_time(text: str):
    m = _TIME_RE.match(text)
    if not m:
        raise argparse.ArgumentTypeError(
            f"bad time {text!r}; expected e.g. 1523400ps / 12ns")
    unit = m.group(2)
    # Without this the regex happily accepts "12nanoseconds" or "100furlong",
    # which used to reach the viewer and silently land the cursor on a raw
    # number in the waveform's own timescale.
    if unit not in VALID_UNITS:
        raise argparse.ArgumentTypeError(
            f"unknown time unit {unit!r} in {text!r}; "
            f"expected one of {list(VALID_UNITS)} (e.g. 1523400ps / 12ns)")
    return {"time": m.group(1), "unit": unit}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="wave-view",
        description="Open FST waveform(s) in the wave-mcp browser viewer "
                    "(streamed via surver; tens-of-GB files open instantly).")
    ap.add_argument("fst", nargs="+", help="FST waveform path(s); two paths "
                    "open a comparison view")
    ap.add_argument("--signals", nargs="*", default=None,
                    help="signal paths to add initially")
    ap.add_argument("--cursor", type=_parse_time, default=None,
                    help="initial cursor time, e.g. 1523400ps")
    ap.add_argument("--labels", nargs="*", default=None,
                    help="labels for each waveform (e.g. pass fail)")
    ap.add_argument("--no-browser", action="store_true",
                    help="do not try to open a browser")
    args = ap.parse_args(argv)

    mgr = ViewManager.instance()
    signals = ([{"path": p} for p in args.signals]
               if args.signals else None)
    result = mgr.open_view(args.fst, signals=signals, cursor=args.cursor,
                           labels=args.labels)

    if not result.get("available"):
        print("error:", result.get("hint") or result.get("error"),
              file=sys.stderr)
        return 1

    print(f"Viewer running at {result['url']}", flush=True)
    print(f"  native client : {result['native_hint']}", flush=True)
    print(f"  remote/SSH    : {result['ssh_hint']}", flush=True)

    if not args.no_browser and try_open_browser(result["url"]):
        print("  (opened in browser)", flush=True)
    else:
        print("  (headless: open the URL from your workstation; IDE "
              "terminals auto-forward localhost ports)", flush=True)

    print("Press Ctrl-C to stop.", flush=True)

    def _shutdown(signum, _frame):
        # SIGTERM does not run atexit hooks, so surver children would leak
        # (a stale process holding a port). Clean up explicitly, then exit.
        try:
            mgr.close_all()
        finally:
            sys.exit(128 + signum)

    for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(_sig, _shutdown)
        except (ValueError, AttributeError, OSError):
            pass

    try:
        while True:
            time.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        mgr.close_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
