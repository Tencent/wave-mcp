"""End-to-end debug pipeline: waveform file -> FST -> session.

This wires the team's standard workflow into one entry point so an LLM client can
go from "I want to analyze the waveform" to a ready session in a single call:

    waveform file (.fst / .vcd)  ->  [convert VCD to FST]  ->  parse xrun.log  ->
    build session.json           ->  open session          ->  ready to query

The entry point takes a *waveform file your simulator already produced*:
  * ``.fst`` — read directly (no conversion).
  * ``.vcd`` — auto-converted to FST (GTKWave vcd2fst).

It never invokes a simulator. Run your sim (xrun / Verilator / whatever) with
your own flow, then point this at the resulting waveform.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from . import convert
from .netlist import build_netlist


def _sha1(path: str, limit: int = 1 << 20) -> Optional[str]:
    if not path or not os.path.exists(path):
        return None
    h = hashlib.sha1()
    read = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            read += len(chunk)
            if limit and read >= limit:
                break
    return h.hexdigest()


def _read_filelist(path: str) -> List[str]:
    """Back-compat: return just the source files from a ``.f`` filelist."""
    files, _incdirs, _defines = _parse_filelist(path)
    return files


def _parse_filelist(path: str) -> Tuple[List[str], List[str], List[str]]:
    """Parse a Verilog/UVM ``.f`` filelist into (files, incdirs, defines).

    Recognizes the common directives so the netlist gets what it needs to
    elaborate real designs:
      * ``+incdir+<dir>`` (may be ``+``-joined multiples)
      * ``-incdir <dir>`` / ``-y <dir>`` / ``-I<dir>``
      * ``+define+NAME[=VAL]`` and ``-define NAME``
      * ``-f <other.f>`` recursion
    Plain tokens are treated as source files (relative to the .f location).
    Unknown ``-``/``+`` options are ignored (not treated as files).
    """
    base = os.path.dirname(os.path.abspath(path))

    def _abs(p: str) -> str:
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))

    files: List[str] = []
    incdirs: List[str] = []
    defines: List[str] = []
    try:
        with open(path) as fh:
            lines = fh.readlines()
    except OSError:
        return files, incdirs, defines

    i = 0
    tokens: List[str] = []
    for line in lines:
        s = line.split("//", 1)[0].strip()
        if not s or s.startswith("#"):
            continue
        tokens.extend(s.split())
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("+incdir+"):
            for d in tok[len("+incdir+"):].split("+"):
                if d:
                    incdirs.append(_abs(d))
        elif tok.startswith("+define+"):
            for d in tok[len("+define+"):].split("+"):
                if d:
                    defines.append(d)
        elif tok.startswith("-I") and len(tok) > 2:
            incdirs.append(_abs(tok[2:]))
        elif tok in ("-incdir", "-y", "-sv_lib", "+libext"):
            if i + 1 < len(tokens):
                incdirs.append(_abs(tokens[i + 1]))
                i += 1
        elif tok in ("-define", "-d"):
            if i + 1 < len(tokens):
                defines.append(tokens[i + 1])
                i += 1
        elif tok == "-f" or tok == "-F":
            if i + 1 < len(tokens):
                sub = _abs(tokens[i + 1])
                sf, si, sd = _parse_filelist(sub)
                files.extend(sf)
                incdirs.extend(si)
                defines.extend(sd)
                i += 1
        elif tok.startswith(("-", "+")):
            pass  # unknown option, ignore
        else:
            files.append(_abs(tok))
        i += 1
    # de-dup preserving order
    def _uniq(xs: List[str]) -> List[str]:
        seen, out = set(), []
        for x in xs:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out
    return _uniq(files), _uniq(incdirs), _uniq(defines)


@dataclass
class StepResult:
    name: str
    ok: bool
    elapsed_sec: float
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"step": self.name, "ok": self.ok,
                "elapsed_sec": round(self.elapsed_sec, 3), **self.detail}


def build_manifest(out_dir: str, fst_path: str, *, top: str = "",
                   filelist: Optional[List[str]] = None,
                   filelist_path: Optional[str] = None,
                   uhdm_db: Optional[str] = None, maps_path: Optional[str] = None) -> str:
    """Write session.json binding all data sources + fingerprints. Returns path."""
    os.makedirs(out_dir, exist_ok=True)
    files = list(filelist or [])
    filelist_hash = None
    if filelist_path and os.path.exists(filelist_path):
        files = files or _read_filelist(filelist_path)
        filelist_hash = _sha1(filelist_path, limit=0)
    manifest = {
        "top": top,
        "fst_path": os.path.abspath(fst_path),
        "uhdm_db": os.path.abspath(uhdm_db) if uhdm_db else None,
        "maps_path": os.path.abspath(maps_path) if maps_path else None,
        "filelist": [os.path.abspath(f) for f in files],
        "fst_hash": _sha1(fst_path),
        "filelist_hash": filelist_hash,
    }
    out_manifest = os.path.join(out_dir, "session.json")
    with open(out_manifest, "w") as fh:
        json.dump(manifest, fh, indent=2)
    return out_manifest


def build_netlist_maps(out_dir: str, files: List[str],
                       top: str = "",
                       incdirs: Optional[List[str]] = None,
                       defines: Optional[List[str]] = None) -> Optional[str]:
    """Build the pyslang netlist into out_dir/netlist/maps.json. Returns path or
    None on failure (caller degrades gracefully).

    ``incdirs`` (``+incdir+`` dirs) and ``defines`` are essential for real UVM /
    IP designs that use `` `include `` and macro guards — without them
    elaboration fails and connectivity/trace silently degrade to unavailable.
    When ``incdirs`` is not given, the directories of the source files are used
    as a best-effort fallback so sibling `` `include `` files resolve.
    """
    files = [f for f in (files or []) if f and os.path.exists(f)]
    if not files:
        return None
    if not incdirs:
        incdirs = sorted({os.path.dirname(os.path.abspath(f)) for f in files})
    maps_path = os.path.join(out_dir, "netlist", "maps.json")
    build_netlist(files, top=top or None, incdirs=incdirs, defines=defines,
                  out_path=maps_path)
    return maps_path


def prepare_session(out_dir: str, wave_path: str, *,
                    fst_path: Optional[str] = None,
                    top: str = "", filelist: Optional[List[str]] = None,
                    filelist_path: Optional[str] = None,
                    incdirs: Optional[List[str]] = None,
                    defines: Optional[List[str]] = None,
                    mode: str = "speed",
                    build_netlist_flag: bool = True) -> dict:
    """Orchestrate waveform file -> FST -> session and return the manifest path
    plus per-step timing.

    ``wave_path`` is a waveform file your simulator already produced:
      * ``.fst`` — read directly (no conversion).
      * ``.vcd`` (anything else) — auto-converted to FST via GTKWave vcd2fst.

    Never runs a simulator. Does NOT open the session (the server does that so the
    session is registered in its SessionManager)."""
    steps: List[StepResult] = []
    os.makedirs(out_dir, exist_ok=True)

    if wave_path.lower().endswith(".fst"):
        # already an FST: read it in place, no conversion step.
        if not os.path.exists(wave_path):
            raise FileNotFoundError(f"FST not found: {wave_path}")
        fst_path = os.path.abspath(wave_path)
    else:
        # treat as VCD -> convert to FST inside the session dir.
        if not os.path.exists(wave_path):
            raise FileNotFoundError(f"VCD not found: {wave_path}")
        if fst_path is None:
            base = os.path.splitext(os.path.basename(wave_path))[0]
            fst_path = os.path.join(out_dir, base + ".fst")
        t0 = time.time()
        res = convert.convert(wave_path, fst_path, mode=mode)
        fst_path = res.fst_path
        steps.append(StepResult("convert_vcd_to_fst", True, time.time() - t0,
                                res.to_dict()))

    # resolve the source file list + include dirs + defines (inline or from .f).
    # A .f filelist is parsed for +incdir+/+define+/-y so the netlist can
    # elaborate real UVM/IP designs (missing incdirs is the #1 cause of the
    # netlist silently degrading to "unavailable").
    files = list(filelist or [])
    inc = list(incdirs or [])
    defs = list(defines or [])
    if filelist_path and os.path.exists(filelist_path):
        f_files, f_inc, f_defs = _parse_filelist(filelist_path)
        if not files:
            files = f_files
        inc = inc + [d for d in f_inc if d not in inc]
        defs = defs + [d for d in f_defs if d not in defs]

    # build the pyslang netlist (categories 5/6); degrade gracefully on failure
    maps_path = None
    if build_netlist_flag and files:
        t0 = time.time()
        try:
            maps_path = build_netlist_maps(out_dir, files, top=top,
                                           incdirs=inc or None, defines=defs or None)
            modules = 0
            diagnostics = 0
            if maps_path and os.path.exists(maps_path):
                with open(maps_path) as fh:
                    _m = json.load(fh)
                    modules = len(_m.get("modules", {}))
                    diagnostics = _m.get("diagnostics", 0)
            steps.append(StepResult("build_netlist", modules > 0, time.time() - t0,
                                    {"maps_path": maps_path, "modules": modules,
                                     "diagnostics": diagnostics,
                                     "incdirs": len(inc), "defines": len(defs),
                                     "note": ("" if modules > 0 else
                                              "0 modules extracted — check incdirs/"
                                              "defines/top; trace/connectivity limited")}))
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-except
            # any netlist-build failure degrades gracefully: trace/connectivity
            # off, all other tools keep working (never abort session prep).
            steps.append(StepResult("build_netlist", False, time.time() - t0,
                                    {"error": str(exc),
                                     "incdirs": len(inc), "defines": len(defs),
                                     "note": "trace/connectivity disabled; other tools still work"}))

    manifest_path = build_manifest(
        out_dir, fst_path, top=top,
        filelist=files, maps_path=maps_path)
    return {
        "session_path": out_dir,
        "manifest": manifest_path,
        "fst_path": fst_path,
        "maps_path": maps_path,
        "steps": [s.to_dict() for s in steps],
    }
