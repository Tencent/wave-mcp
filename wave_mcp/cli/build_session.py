"""Build a self-contained debug session directory + manifest.

This is the 'no-pain wrapper' from the requirements (stage 5): from the same
filelist used by the simulator, assemble a ``session.json`` that binds the FST
waveform and (optionally) the RTL netlist, recording fingerprints so the server
can detect stale data.

Usage::

    wave-session --fst sim/dump.fst --top top_tb \
                 --filelist rtl.f --out sessions/my_module

The pyslang netlist (connectivity / driver / trace, categories 5/6) is built by
default when a filelist is given; if elaboration fails the session still works
for categories 1,2,3,4,7,8,9,10 (graceful degradation).
"""
from __future__ import annotations

import argparse
import json
import os
from typing import List, Optional

from ..runtime.identity import file_version


def _read_filelist(path: str) -> List[str]:
    base = os.path.dirname(os.path.abspath(path))
    out: List[str] = []
    with open(path) as fh:
        for line in fh:
            s = line.strip()
            if not s or s.startswith(("#", "//", "-")):
                continue
            out.append(s if os.path.isabs(s) else os.path.normpath(os.path.join(base, s)))
    return out


def _run_static(args, p) -> int:
    if args.no_netlist:
        p.error("--static requires the netlist (drop --no-netlist)")
    if not args.filelist:
        p.error("--static requires --filelist")
    from .. import pipeline
    try:
        result = pipeline.prepare_static_session(
            args.out or None, top=args.top, filelist_path=args.filelist)
    except (FileNotFoundError, ValueError) as exc:
        p.error(str(exc))
    for step in result["steps"]:
        tag = "ok" if step.get("ok") else "warn"
        print(f"[{tag}] {step['step']}: {step.get('note') or step.get('maps_path')}")
    print(f"[ok] static session written: {result['manifest']}")
    print("     waveform=no (value/trace tools disabled; connectivity/"
          "drivers/loads/fanin enabled)")
    return 0


def _resolve_vcd(args, p) -> None:
    """Convert --vcd via resolve_waveform so the FST is shared; sets args.fst."""
    from .. import convert
    print(f"[info] resolving VCD -> FST (pack={args.pack or 'fastlz'}) ...")
    try:
        got = convert.resolve_waveform(args.vcd, pack=args.pack)
    except (convert.UnsupportedWaveformError, FileNotFoundError,
            convert.ConversionError) as exc:
        p.error(str(exc))
    detail = got.get("detail") or {}
    if got.get("cached"):
        print(f"[ok] reused cached FST -> {got['fst_path']}")
    else:
        elapsed = detail.get("elapsed_sec")
        ratio = detail.get("compression_ratio")
        extra = ""
        if elapsed is not None:
            extra = f"{elapsed:.3f}s"
            if ratio:
                extra += f", x{ratio} smaller"
            extra += " "
        print(f"[ok] {extra}-> {got['fst_path']}")
    args.fst = got["fst_path"]


def _build_netlist(args, files: List[str]) -> Optional[str]:
    """Build the pyslang netlist into --out; returns maps_path or None."""
    from .. import netlist
    from .. import pipeline
    _f, incdirs, defines = pipeline._parse_filelist(args.filelist)  # pylint: disable=protected-access
    maps_out = os.path.join(args.out, "netlist", "maps.json")
    print("[info] building pyslang netlist (categories 5/6) ...")
    try:
        res = netlist.build_netlist(files, top=args.top or None,
                                    incdirs=incdirs or None, defines=defines or None,
                                    out_path=maps_out)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[warn] netlist build failed ({exc}); connectivity/trace disabled")
        return None
    print(f"[ok] netlist: {len(res.get('modules', {}))} modules, "
          f"{res.get('diagnostics', 0)} diagnostics -> {maps_out}")
    return os.path.abspath(maps_out)


def _run_wave(args, p) -> int:
    # auto-convert VCD -> FST if requested (xrun produces VCD).
    # Goes through resolve_waveform so the artifact is shared with
    # prepare_session and open_wave_view: converting here used to write a
    # private copy under --out, so the same waveform got converted again by
    # every other entry point (and vice versa).
    if args.vcd and not args.fst:
        _resolve_vcd(args, p)
    if not args.fst:
        p.error("either --fst or --vcd is required")
    fst = os.path.abspath(args.fst)
    if not os.path.exists(fst):
        p.error(f"FST not found: {fst}")

    filelist_files: List[str] = []
    if args.filelist and os.path.exists(args.filelist):
        filelist_files = _read_filelist(args.filelist)

    from .. import pipeline
    args.out = pipeline.resolve_out_dir(args.out or None, wave_path=fst,
                                        top=args.top, files=filelist_files)
    os.makedirs(args.out, exist_ok=True)

    maps_path = None
    if not args.no_netlist and filelist_files:
        maps_path = _build_netlist(args, filelist_files)

    manifest = {
        "top": args.top,
        "fst_path": fst,
        "maps_path": maps_path,
        "filelist": filelist_files,
        "fst_version": file_version(fst) or None,
    }
    out_manifest = os.path.join(args.out, "session.json")
    with open(out_manifest, "w") as fh:
        json.dump(manifest, fh, indent=2)
    print(f"[ok] session written: {out_manifest}")
    print(f"     fst={fst}")
    print(f"     netlist={'yes' if maps_path else 'no (categories 5/6 disabled)'}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="Build a wave-mcp session directory")
    p.add_argument("--fst", help="FST waveform path (or use --vcd to convert)")
    p.add_argument("--vcd", help="waveform to convert to FST (.vcd / .fsdb); "
                                "conversion is cached and shared with "
                                "prepare_session and the viewer")
    p.add_argument("--pack", choices=["fastlz", "lz4", "zlib"], default=None,
                   help="FST compressor for the VCD->FST conversion: fastlz "
                        "(fastest, default), lz4, zlib (smallest)")
    p.add_argument("--top", default="", help="top instance name")
    p.add_argument("--filelist", help="xrun filelist (.f) — same one used for sim")
    p.add_argument("--out", default=None,
                   help="session directory; default: under the wave-mcp session "
                        "root ($WAVE_MCP_SESSION_ROOT or ~/.wave-mcp/"
                        "sessions), named after the inputs")
    p.add_argument("--static", action="store_true",
                   help="build a static (netlist-only) session — no waveform "
                        "needed; value/trace tools stay off until an FST is added")
    p.add_argument("--no-netlist", action="store_true",
                   help="skip building the pyslang netlist (disables categories 5/6)")
    args = p.parse_args(argv)

    if args.static:
        return _run_static(args, p)
    return _run_wave(args, p)


if __name__ == "__main__":
    raise SystemExit(main())
