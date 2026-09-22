#!/usr/bin/env python3
"""Viewer stability soak: repeated open/close cycles against a real surver.

Not part of run_regression.py (it takes minutes); run it by hand before a
release or when investigating viewer flakiness:

    python3 tests/viewer_soak.py --loops 300 --token mixed
    python3 tests/viewer_soak.py --minutes 30 --token mixed

The worker rotates waveform file sets, opens and closes one view per cycle,
and reports failures plus per-cycle timing. Token mode selects what kind of
surver tokens the cycle sees:

  natural - whatever secrets.token_urlsafe() produces
  dash    - first character forced to '-', the case that failed ~1 in 64
            before the token was passed attached (--token=<value>)
  mixed   - alternates the two per surver start

--minutes runs until that wall-clock budget is spent (--loops then acts as
no cap). A monitor thread samples this process's RSS and fd count plus the
number of surver processes every --sample-every seconds, so a slow leak over
a long soak shows up in the JSON result instead of only in an external probe.

Evidence this catches real regressions: with the old separate-argv token a
dash run fails immediately; with the fix, 750 cycles across modes passed,
and the 30-minute mixed soak passed with flat RSS and fds.
"""
from __future__ import annotations

import argparse
import glob
import itertools
import json
import os
import secrets
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from wave_mcp.viewer import find_assets            # noqa: E402
from wave_mcp.viewer.manager import ViewManager    # noqa: E402

WAVES = os.path.join(ROOT, "examples", "viewer_demos", "waves")
SETS = {
    "xprop": [os.path.join(WAVES, "xprop.fst")],
    "cdc": [os.path.join(WAVES, "cdc.fst")],
    "dual": [os.path.join(WAVES, "cdc.fst"),
             os.path.join(WAVES, "xprop.fst")],
}


def _sample() -> dict:
    """One resource sample: RSS (MB), open fds, live surver processes."""
    rss_mb = 0
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss_mb = int(line.split()[1]) // 1024
                    break
    except OSError:
        pass
    fds = 0
    try:
        fds = len(os.listdir("/proc/self/fd"))
    except OSError:
        pass
    survers = 0
    for comm in glob.glob("/proc/[0-9]*/comm"):
        try:
            with open(comm, encoding="ascii") as f:
                if f.read().strip() == "surver":
                    survers += 1
        except OSError:
            continue
    return {"rss_mb": rss_mb, "fds": fds, "survers": survers}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--loops", type=int, default=300,
                    help="cycle count (only when --minutes is 0)")
    ap.add_argument("--minutes", type=float, default=0.0,
                    help="run until this wall-clock budget is spent")
    ap.add_argument("--token", choices=("natural", "dash", "mixed"),
                    default="mixed")
    ap.add_argument("--sets", default="xprop,cdc,dual",
                    help="file sets to rotate, from: xprop, cdc, dual")
    ap.add_argument("--label", default="soak")
    ap.add_argument("--json", default="", help="write the result JSON here")
    ap.add_argument("--progress-every", type=int, default=100)
    ap.add_argument("--sample-every", type=float, default=60.0,
                    help="resource sample interval in seconds (0 disables)")
    args = ap.parse_args()

    if not find_assets():
        print("viewer assets not found; set WAVE_MCP_VIEWER_ASSETS or "
              "build them locally (docs/SELF_BUILD.md)")
        return 2
    names = [s for s in args.sets.split(",") if s in SETS]
    if not names:
        print(f"no usable file sets in {args.sets!r}")
        return 2
    missing = [p for s in names for p in SETS[s] if not os.path.isfile(p)]
    if missing:
        print(f"demo waveforms missing: {missing}")
        return 2

    mgr = ViewManager()
    real = secrets.token_urlsafe
    if args.token != "natural":
        counter = itertools.count()

        def patched(n: int = 32) -> str:
            s = real(n)
            if args.token == "dash":
                return "-" + s[1:]
            return ("-" + s[1:]) if next(counter) % 2 == 0 else s

        secrets.token_urlsafe = patched

    fails, times = [], []
    samples = [dict(_sample(), t=0.0)]
    t0 = time.time()
    stop_evt = threading.Event()

    def monitor() -> None:
        while not stop_evt.wait(args.sample_every):
            samples.append(dict(_sample(), t=round(time.time() - t0, 1)))

    mon = None
    if args.sample_every > 0:
        mon = threading.Thread(target=monitor, daemon=True)
        mon.start()

    deadline = t0 + args.minutes * 60 if args.minutes > 0 else None
    i = 0
    try:
        while True:
            if deadline is not None:
                if time.time() >= deadline:
                    break
            elif i >= args.loops:
                break
            paths = SETS[names[i % len(names)]]
            t1 = time.time()
            r = mgr.open_view(paths)
            if not r.get("available"):
                fails.append({"i": i, "step": "open", "resp": r})
            else:
                c = mgr.close_view(r["view_id"])
                if not c.get("closed"):
                    fails.append({"i": i, "step": "close", "resp": c})
                times.append(time.time() - t1)
            i += 1
            if args.progress_every and i % args.progress_every == 0:
                s = samples[-1]
                print(f"  [{args.label}] {i} cycles fails={len(fails)} "
                      f"rss={s['rss_mb']}MB fds={s['fds']} "
                      f"survers={s['survers']} {time.time() - t0:.0f}s",
                      flush=True)
    finally:
        stop_evt.set()
        if mon is not None:
            mon.join(timeout=2)
        secrets.token_urlsafe = real
        mgr.close_all()
        time.sleep(1.0)                      # let any dying child be reaped
        samples.append(dict(_sample(), t=round(time.time() - t0, 1)))

    final = samples[-1]
    out = {
        "label": args.label, "cycles": i, "token": args.token,
        "minutes": args.minutes,
        "fail_count": len(fails), "fails": fails[:10],
        "elapsed_s": round(time.time() - t0, 1),
        "cycle_ms": {
            "min": round(min(times, default=0) * 1000, 1),
            "avg": round((sum(times) / len(times) if times else 0) * 1000, 1),
            "max": round(max(times, default=0) * 1000, 1),
        },
        "resources": {
            "samples": samples,
            "max_survers": max(s["survers"] for s in samples),
            "rss_mb": {"start": samples[0]["rss_mb"], "end": final["rss_mb"],
                       "max": max(s["rss_mb"] for s in samples)},
            "fds": {"start": samples[0]["fds"], "end": final["fds"],
                    "max": max(s["fds"] for s in samples)},
            "leftover_survers_at_end": final["survers"],
        },
    }
    print("SOAK " + json.dumps(out), flush=True)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(out, f, indent=1)
    bad = bool(fails) or final["survers"] > 0
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
