"""End-to-end debug pipeline: xrun -> VCD -> FST -> session.

This wires the team's standard workflow into one entry point so an LLM client can
go from "I want to analyze the waveform" to a ready session in a single call:

    run xrun (dump VCD)  ->  convert VCD to FST  ->  parse xrun.log  ->
    build session.json   ->  open session       ->  ready to query

Two strategies:
  * post-process (default): run sim to completion, then convert the VCD.
  * streaming (``stream=True``): dump straight into a FIFO and convert during
    simulation, so the FST is ready almost as soon as the sim ends.

The actual xrun invocation differs per project, so it is passed in as
``sim_command`` (a shell command run in ``cwd``); when omitted, an already-dumped
VCD is assumed to exist.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from . import convert
from .netlist import build_netlist, NetlistError


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


def run_simulation(command: str, cwd: Optional[str] = None,
                   log_path: Optional[str] = None,
                   timeout: Optional[float] = None,
                   env: Optional[dict] = None) -> StepResult:
    """Run the xrun simulation command, teeing combined output to ``log_path``.

    ``command`` is run through the shell (so module loads / make targets work).
    The captured output is written to ``log_path`` (becomes the session's
    xrun.log) and the tail is returned.
    """
    t0 = time.time()
    full_env = {**os.environ, **(env or {})}
    proc = subprocess.run(command, shell=True, cwd=cwd, env=full_env,
                          capture_output=True, text=True, timeout=timeout)
    output = (proc.stdout or "") + (proc.stderr or "")
    if log_path:
        os.makedirs(os.path.dirname(os.path.abspath(log_path)) or ".", exist_ok=True)
        with open(log_path, "w", errors="replace") as fh:
            fh.write(output)
    tail = "\n".join(output.splitlines()[-30:])
    return StepResult(
        "run_simulation", proc.returncode == 0, time.time() - t0,
        {"returncode": proc.returncode, "log_path": log_path, "output_tail": tail,
         "command": command, "cwd": cwd or os.getcwd()})


def build_manifest(out_dir: str, fst_path: str, *, top: str = "",
                   log_path: Optional[str] = None,
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
        "log_path": os.path.abspath(log_path) if log_path else None,
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


def prepare_session(out_dir: str, vcd_path: str, *,
                    sim_command: Optional[str] = None, cwd: Optional[str] = None,
                    fst_path: Optional[str] = None, log_path: Optional[str] = None,
                    top: str = "", filelist: Optional[List[str]] = None,
                    filelist_path: Optional[str] = None,
                    incdirs: Optional[List[str]] = None,
                    defines: Optional[List[str]] = None,
                    mode: str = "speed", stream: bool = False,
                    build_netlist_flag: bool = True,
                    timeout: Optional[float] = None) -> dict:
    """Orchestrate xrun -> VCD -> FST -> session and return the manifest path
    plus per-step timing. Does NOT open the session (the server does that so the
    session is registered in its SessionManager)."""
    steps: List[StepResult] = []
    os.makedirs(out_dir, exist_ok=True)
    if fst_path is None:
        base = os.path.splitext(os.path.basename(vcd_path))[0]
        fst_path = os.path.join(out_dir, base + ".fst")
    if log_path is None and sim_command:
        log_path = os.path.join(out_dir, "xrun.log")

    if stream:
        # converter consumes the FIFO in the background while xrun writes it
        t0 = time.time()
        conv = convert.start_streaming(vcd_path, fst_path, mode=mode)
        steps.append(StepResult("start_streaming", True, time.time() - t0,
                                {"pid": conv.pid, "fifo": vcd_path, "fst_path": fst_path}))
        if not sim_command:
            raise ValueError("stream=True requires sim_command (xrun must write the FIFO)")
        steps.append(run_simulation(sim_command, cwd, log_path, timeout))
        t0 = time.time()
        try:
            os.waitpid(conv.pid, 0)
            ok = os.path.exists(fst_path)
        except ChildProcessError:
            ok = os.path.exists(fst_path)
        steps.append(StepResult("finish_streaming_convert", ok, time.time() - t0,
                                {"fst_path": fst_path,
                                 "fst_bytes": os.path.getsize(fst_path) if ok else None}))
    else:
        if sim_command:
            steps.append(run_simulation(sim_command, cwd, log_path, timeout))
        if not os.path.exists(vcd_path):
            raise FileNotFoundError(f"VCD not produced: {vcd_path}")
        t0 = time.time()
        res = convert.convert(vcd_path, fst_path, mode=mode)
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
        except (NetlistError, Exception) as exc:  # noqa: BLE001
            steps.append(StepResult("build_netlist", False, time.time() - t0,
                                    {"error": str(exc),
                                     "incdirs": len(inc), "defines": len(defs),
                                     "note": "trace/connectivity disabled; other tools still work"}))

    manifest_path = build_manifest(
        out_dir, fst_path, top=top, log_path=log_path,
        filelist=files, maps_path=maps_path)
    return {
        "session_path": out_dir,
        "manifest": manifest_path,
        "fst_path": fst_path,
        "maps_path": maps_path,
        "steps": [s.to_dict() for s in steps],
    }
