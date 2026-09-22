"""wave-vcd2fst — fast VCD -> FST conversion CLI.

Post-process an existing VCD::

    wave-vcd2fst --vcd sim/dump.vcd --fst sim/dump.fst --pack fastlz

Batch (one FST per input, converted concurrently; shell-quoted globs work)::

    wave-vcd2fst --batch --vcd 'sim/*.vcd' --jobs 8

Streaming (hide conversion in simulation time — fastest end-to-end)::

    # 1) set up the FIFO + background converter
    wave-vcd2fst --stream --vcd sim/dump.vcd --fst sim/dump.fst
    # 2) in the TB:  $dumpfile("sim/dump.vcd");  then run xrun normally.
    #    When the sim finishes, sim/dump.fst is ready (vcd2fst exits on EOF).
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import convert


def _batch(args) -> int:
    """Convert several VCDs concurrently, one FST per input.

    Globs are expanded here (scripts often quote them) and duplicates are
    dropped, so an overlapping pattern cannot convert one file twice and race
    on its output path. Each conversion is the usual heartbeat-monitored
    subprocess, so a single stuck file fails its own slot without taking the
    batch down with it.
    """
    if args.fst:
        print("[error] --batch writes <input>.fst next to each input; "
              "--fst does not apply", file=sys.stderr)
        return 1
    files = []
    for pattern in args.vcd:
        if any(ch in pattern for ch in "*?["):
            files.extend(sorted(glob.glob(pattern)))
        else:
            files.append(pattern)
    seen, uniq = set(), []
    for f in files:
        ap = os.path.abspath(f)
        if ap not in seen:
            seen.add(ap)
            uniq.append(f)
    if not uniq:
        print("[error] --batch: no input files matched", file=sys.stderr)
        return 1
    jobs = args.jobs if args.jobs > 0 else max(1, min(8, (os.cpu_count() or 2) // 2))
    print(f"[ok] batch converting {len(uniq)} file(s) with up to {jobs} slot(s)")

    def _one(path: str):
        out = os.path.splitext(path)[0] + ".fst"
        res = convert.convert(path, out, pack=args.pack,
                              parallel=not args.no_parallel,
                              compress=args.compress, timeout=args.timeout)
        return res.to_dict()

    failures = 0
    done = 0
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(_one, f): f for f in uniq}
        for fut in as_completed(futures):
            f = futures[fut]
            done += 1
            try:
                d = fut.result()
                ratio = (f" (x{d['compression_ratio']} smaller)"
                         if d.get("compression_ratio") else "")
                print(f"[{done}/{len(uniq)}] {d['vcd_path']} -> {d['fst_path']} "
                      f"{d['elapsed_sec']}s{ratio}")
            except Exception as exc:  # pylint: disable=broad-except
                failures += 1
                print(f"[{done}/{len(uniq)}] [error] {f}: {exc}", file=sys.stderr)
    if failures:
        print(f"[error] {failures} of {len(uniq)} conversions failed",
              file=sys.stderr)
    return 1 if failures else 0


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Fast VCD -> FST converter (vcd2fst wrapper)")
    p.add_argument("--vcd", required=True, nargs="+",
                   help="input VCD file(s); with --batch also accepts several "
                        "files or shell-quoted globs")
    p.add_argument("--fst", help="output FST file (default: <vcd>.fst)")
    p.add_argument("--pack", choices=list(convert.PACKS), default="fastlz",
                   help="FST compressor: fastlz (fastest, default), lz4, zlib (smallest)")
    p.add_argument("--no-parallel", action="store_true", help="disable parallel packing")
    p.add_argument("--compress", action="store_true", help="extra zlib compress on close")
    p.add_argument("--timeout", type=float, default=None,
                   help="conversion timeout in seconds (default: auto-estimated "
                        "from the file size; a stalled converter fails fast)")
    p.add_argument("--batch", action="store_true",
                   help="convert several inputs concurrently (one FST each)")
    p.add_argument("--jobs", type=int, default=0,
                   help="--batch concurrency (default: half the CPUs, between "
                        "1 and 8)")
    p.add_argument("--stream", action="store_true",
                   help="streaming mode: create FIFO + launch background converter")
    p.add_argument("--log", help="streaming: vcd2fst log file")
    args = p.parse_args(argv)

    if len(args.vcd) > 1 and not args.batch:
        print("[error] several --vcd inputs require --batch", file=sys.stderr)
        return 1
    if args.batch:
        if args.stream:
            print("[error] --batch cannot be combined with --stream",
                  file=sys.stderr)
            return 1
        return _batch(args)

    vcd = args.vcd[0]
    try:
        if args.stream:
            res = convert.start_streaming(vcd, args.fst, pack=args.pack,
                                          parallel=not args.no_parallel,
                                          log_path=args.log)
            print(f"[ok] streaming converter started (pid={res.pid})")
            print(f"     FIFO : {res.vcd_path}")
            print(f"     FST  : {res.fst_path}")
            print(f"     now point $dumpfile at the FIFO and run xrun; "
                  f"FST completes when sim ends.")
        else:
            res = convert.convert(vcd, args.fst, pack=args.pack,
                                  parallel=not args.no_parallel,
                                  compress=args.compress, timeout=args.timeout)
            d = res.to_dict()
            print(f"[ok] {d['vcd_path']} -> {d['fst_path']}")
            print(f"     pack={d['pack']} parallel={d['parallel']} "
                  f"elapsed={d['elapsed_sec']}s")
            if d["compression_ratio"]:
                print(f"     {d['vcd_bytes']} -> {d['fst_bytes']} bytes "
                      f"(x{d['compression_ratio']} smaller)")
    except convert.ConversionError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
