"""wave-vcd2fst — fast VCD -> FST conversion CLI.

Post-process an existing VCD::

    wave-vcd2fst --vcd sim/dump.vcd --fst sim/dump.fst --mode speed

Streaming (hide conversion in simulation time — fastest end-to-end)::

    # 1) set up the FIFO + background converter
    wave-vcd2fst --stream --vcd sim/dump.vcd --fst sim/dump.fst
    # 2) in the TB:  $dumpfile("sim/dump.vcd");  then run xrun normally.
    #    When the sim finishes, sim/dump.fst is ready (vcd2fst exits on EOF).
"""
from __future__ import annotations

import argparse
import sys

from .. import convert


def main(argv=None):
    p = argparse.ArgumentParser(description="Fast VCD -> FST converter (vcd2fst wrapper)")
    p.add_argument("--vcd", required=True, help="input VCD file (or FIFO path in --stream)")
    p.add_argument("--fst", help="output FST file (default: <vcd>.fst)")
    p.add_argument("--mode", choices=["speed", "balanced", "size"], default="speed",
                   help="speed=fastlz (default), balanced=lz4, size=zlib")
    p.add_argument("--no-parallel", action="store_true", help="disable parallel packing")
    p.add_argument("--compress", action="store_true", help="extra zlib compress on close")
    p.add_argument("--stream", action="store_true",
                   help="streaming mode: create FIFO + launch background converter")
    p.add_argument("--log", help="streaming: vcd2fst log file")
    args = p.parse_args(argv)

    try:
        if args.stream:
            res = convert.start_streaming(args.vcd, args.fst, mode=args.mode,
                                          parallel=not args.no_parallel, log_path=args.log)
            print(f"[ok] streaming converter started (pid={res.pid})")
            print(f"     FIFO : {res.vcd_path}")
            print(f"     FST  : {res.fst_path}")
            print(f"     now point $dumpfile at the FIFO and run xrun; "
                  f"FST completes when sim ends.")
        else:
            res = convert.convert(args.vcd, args.fst, mode=args.mode,
                                  parallel=not args.no_parallel, compress=args.compress)
            d = res.to_dict()
            print(f"[ok] {d['vcd_path']} -> {d['fst_path']}")
            print(f"     mode={d['mode']} parallel={d['parallel']} "
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
