"""End-to-end debug pipeline: waveform file -> FST -> session.

This wires the team's standard workflow into one entry point so an LLM client can
go from "I want to analyze the waveform" to a ready session in a single call:

    waveform file (.fst / .fsdb / .vcd)  ->  [convert to FST]  ->
    build session.json           ->  open session          ->  ready to query

The entry point takes a *waveform file your simulator already produced*:
  * ``.fst`` — read directly (no conversion).
  * ``.fsdb`` — auto-converted to FST (bundled fsdb2fst; docs/FSDB_GUIDE.md).
  * ``.vcd`` — auto-converted to FST (GTKWave vcd2fst).

Conversions are cached in the derived-cache layer (never next to the source),
keyed on the waveform's version plus the slicing options, so building several
sessions from one waveform converts once.

It never invokes a simulator. Run your sim (xrun / Verilator / whatever) with
your own flow, then point this at the resulting waveform.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import convert
from .netlist import build_netlist
from .runtime import storage
from .runtime.identity import dataset_identity, file_version
from .runtime.storage import StoragePolicy

#: log a progress line for netlist builds once the filelist is this large
#: (small designs elaborate in seconds; a line would just be noise)
_PROGRESS_FILE_COUNT = 100

def _log(msg: str) -> None:
    """One-line progress on stderr.

    MCP clients capture the server's stderr as logs, so a long-running step
    (conversion, netlist elaboration) stays visible instead of reading as a
    black box. Purely informational: never affects the result.
    """
    try:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()
    except Exception:  # pylint: disable=broad-except
        pass

#: source size (MB) above which waveform hints are worth a model's attention
#: (below it, conversion is fast enough that strategy does not matter)
_HEAVY_WAVE_MB = 512

def _heavy_waveform_hints(wave_path: str, detail: dict,
                          scopes: Optional[List[str]],
                          signals_file: Optional[str]) -> List[str]:
    """Post-conversion advice for a large waveform, surfaced in the reply.

    Conversion is the slow part and cannot be preceded by a reply, so the
    value is twofold: the model learns the real size/timing for planning the
    session, and — when a file was converted whole without slicing — gets the
    concrete lever (scopes / signals_file, or dumping FST straight
    from the simulator) to shorten the next conversion.
    """
    try:
        size = os.path.getsize(wave_path)
    except OSError:
        return []
    if size < _HEAVY_WAVE_MB * 1024 * 1024:
        return []
    kind = "FSDB" if wave_path.lower().endswith(".fsdb") else "VCD"
    cached = bool(detail.get("cached"))
    elapsed = detail.get("elapsed_sec")
    line = f"large waveform: {size / (1024 ** 3):.2f} GB {kind}"
    if cached:
        line += " (FST served from the conversion cache)"
    elif elapsed:
        line += f", conversion took {elapsed:.0f}s"
    hints = [line]
    if not cached and not (scopes or signals_file):
        hints.append(
            "for faster repeat runs, narrow the conversion with scopes / "
            "signals_file, or dump FST directly from the simulator "
            "(zero-conversion workflow)")
    return hints


def session_identity(wave_path: Optional[str], top: str,
                     files: List[str]) -> str:
    """Identity of the inputs a session is about to be built from.

    ``dataset_identity`` over the manifest as it will be written, minus what
    the build itself produces (``maps_path``, converted FST): the caller's
    waveform (None for a static session), the top and the expanded source
    list. Names the default session directory, so the same inputs asked for
    from anywhere land in one place and two designs never share one.
    """
    pre = {"fst_path": os.path.abspath(wave_path) if wave_path else None,
           "top": top or "", "filelist": [os.path.abspath(f) for f in files]}
    return dataset_identity(pre, os.getcwd())


def resolve_out_dir(out_dir: Optional[str], *, wave_path: Optional[str] = None,
                    top: str = "", files: Optional[List[str]] = None) -> str:
    """Where a session lands.

    ``out_dir`` given: used as given. Omitted: ``<session root>/<identity>``
    with ``identity`` from :func:`session_identity`; the root is
    ``$WAVE_MCP_SESSION_ROOT`` or ``~/.wave-mcp/sessions`` (``storage.py``). Nothing
    is remapped: a caller that names a place gets that place, and whether it
    may write there is the operating system's call.
    """
    identity = "" if out_dir else session_identity(wave_path, top, files or [])
    return storage.policy().session_dir(out_dir, identity)


def netlist_home(out_dir: str, explicit: bool, top: str, files: List[str]) -> str:
    """The directory whose ``netlist/maps.json`` belongs to this build.

    With an explicit ``out_dir`` the netlist sits inside it, next to
    ``session.json``. At a defaulted location it sits at the identity of the
    *sources* (the static session's directory), whatever waveform is attached:
    every waveform dumped from the same design then reuses one elaboration,
    and ``open_static_session`` followed by ``prepare_session`` needs no
    shared ``out_dir`` to find it.
    """
    if explicit:
        return out_dir
    return storage.policy().session_dir(None, session_identity(None, top, files))


def _read_filelist(path: str) -> List[str]:
    """Back-compat: return just the source files from a ``.f`` filelist."""
    files, _incdirs, _defines = _parse_filelist(path)
    return files


def _apply_directive(tok: str, tokens: List[str], i: int, _abs,
                     files: List[str], incdirs: List[str],
                     defines: List[str]) -> int:
    """Handle one filelist token; returns the index of the next token."""
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
            return i + 1
    elif tok in ("-define", "-d"):
        if i + 1 < len(tokens):
            defines.append(tokens[i + 1])
            return i + 1
    elif tok in ("-f", "-F"):
        if i + 1 < len(tokens):
            sub = _abs(tokens[i + 1])
            sf, si, sd = _parse_filelist(sub)
            files.extend(sf)
            incdirs.extend(si)
            defines.extend(sd)
            return i + 1
    elif not tok.startswith(("-", "+")):
        files.append(_abs(tok))
    return i


def _parse_filelist(path: str) -> Tuple[List[str], List[str], List[str]]:
    """Parse a Verilog/UVM ``.f`` filelist into (files, incdirs, defines).

    Recognizes the common directives so the netlist gets what it needs to
    elaborate real designs:
      * ``+incdir+<dir>`` (may be ``+``-joined multiples)
      * ``-incdir <dir>`` / ``-y <dir>`` / ``-I<dir>``
      * ``+define+NAME[=VAL]`` and ``-define NAME``
      * ``-f <other.f>`` recursion
      * ``$VAR`` / ``${VAR}`` environment expansion. Real filelists are full
        of these (``-F $PROJ_FE/rtl/foo.f``). An undefined variable leaves the
        token untouched, so it simply drops out as a missing file; export the
        variable (or replace it with a literal path) to pick those files up.
    Plain tokens are treated as source files (relative to the .f location).
    Unknown ``-``/``+`` options are ignored (not treated as files).
    """
    base = os.path.dirname(os.path.abspath(path))

    def _abs(p: str) -> str:
        p = os.path.expandvars(p)
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))

    files: List[str] = []
    incdirs: List[str] = []
    defines: List[str] = []
    try:
        with open(path) as fh:
            lines = fh.readlines()
    except OSError:
        return files, incdirs, defines

    tokens: List[str] = []
    for line in lines:
        s = line.split("//", 1)[0].strip()
        if not s or s.startswith("#"):
            continue
        tokens.extend(s.split())
    i = 0
    while i < len(tokens):
        i = _apply_directive(tokens[i], tokens, i, _abs,
                             files, incdirs, defines) + 1

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


def cache_record(kind: str, path: Optional[str]) -> Optional[Dict[str, Any]]:
    """One ``caches[]`` entry: a derived file the session depends on, and its
    version at build time, so ``session_info`` can later tell whether it moved."""
    if not path or not os.path.exists(path):
        return None
    return {"kind": kind, "path": os.path.abspath(path),
            "version": file_version(path)}


def build_manifest(out_dir: str, fst_path: Optional[str], *, top: str = "",
                   filelist: Optional[List[str]] = None,
                   filelist_path: Optional[str] = None,
                   uhdm_db: Optional[str] = None, maps_path: Optional[str] = None,
                   wave_path: Optional[str] = None,
                   caches: Optional[List[Dict[str, Any]]] = None) -> str:
    """Write session.json binding all data sources + versions. Returns path.

    ``fst_path=None`` produces a *static* (netlist-only) session manifest.
    ``wave_path`` is the waveform the caller named when it differs from the
    FST actually opened (a converted VCD/FSDB); ``caches`` lists the derived
    files this session reads from (see ``cache_record``).
    """
    os.makedirs(out_dir, exist_ok=True)
    files = list(filelist or [])
    if filelist_path and os.path.exists(filelist_path):
        files = files or _read_filelist(filelist_path)
    manifest: Dict[str, Any] = {
        "top": top,
        "fst_path": os.path.abspath(fst_path) if fst_path else None,
        "uhdm_db": os.path.abspath(uhdm_db) if uhdm_db else None,
        "maps_path": os.path.abspath(maps_path) if maps_path else None,
        "filelist": [os.path.abspath(f) for f in files],
        "fst_version": file_version(fst_path) or None,
    }
    if wave_path and os.path.abspath(wave_path) != manifest["fst_path"]:
        manifest["wave_path"] = os.path.abspath(wave_path)
    if caches:
        manifest["caches"] = [c for c in caches if c]
    out_manifest = os.path.join(out_dir, "session.json")
    StoragePolicy.atomic_write_bytes(
        out_manifest, json.dumps(manifest, indent=2).encode())
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


def _netlist_fresh(out_dir: str, files: List[str]) -> Optional[str]:
    """Return the existing ``out_dir/netlist/maps.json`` when it can be reused.

    Reusable = maps.json exists, parses, has modules, and is newer than every
    source file. Saves the (expensive) re-elaboration when upgrading a static
    session to a full one, or re-running prepare on an unchanged design.
    """
    maps_path = os.path.join(out_dir, "netlist", "maps.json")
    if not os.path.exists(maps_path):
        return None
    try:
        with open(maps_path) as fh:
            if not json.load(fh).get("modules"):
                return None
    except (OSError, ValueError):
        return None
    maps_mtime = os.path.getmtime(maps_path)
    for f in files:
        if os.path.exists(f) and os.path.getmtime(f) > maps_mtime:
            return None
    return maps_path


def prepare_static_session(out_dir: Optional[str] = None, *,
                           top: str = "", filelist: Optional[List[str]] = None,
                           filelist_path: Optional[str] = None,
                           incdirs: Optional[List[str]] = None,
                           defines: Optional[List[str]] = None) -> dict:
    """Build a *static* (netlist-only) session — no waveform required.

    RTL sources -> pyslang netlist -> session.json with ``fst_path: null``.
    Connectivity / drivers / loads / fan-in / files / declaration tools all
    work; value & trace tools stay off until a waveform session is prepared.
    Reuses an existing fresh ``netlist/maps.json`` instead of re-elaborating.
    """
    steps: List[StepResult] = []

    files = list(filelist or [])
    inc = list(incdirs or [])
    defs = list(defines or [])
    if filelist_path and os.path.exists(filelist_path):
        f_files, f_inc, f_defs = _parse_filelist(filelist_path)
        if not files:
            files = f_files
        inc = inc + [d for d in f_inc if d not in inc]
        defs = defs + [d for d in f_defs if d not in defs]
    files = [f for f in files if f and os.path.exists(f)]
    if not files:
        raise ValueError("static session needs RTL sources: pass filelist or "
                         "filelist_path (no valid source file found)")

    out_dir = resolve_out_dir(out_dir, top=top, files=files)
    os.makedirs(out_dir, exist_ok=True)

    reused = _netlist_fresh(out_dir, files)
    if reused:
        maps_path = reused
        steps.append(StepResult("build_netlist", True, 0.0,
                                {"maps_path": maps_path, "reused": True,
                                 "note": "existing netlist is up to date; "
                                         "skipped re-elaboration"}))
    else:
        t0 = time.time()
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
                                          "defines/top")}))

    manifest_path = build_manifest(out_dir, None, top=top,
                                   filelist=files, maps_path=maps_path)
    return {
        "session_path": out_dir,
        "manifest": manifest_path,
        "fst_path": None,
        "maps_path": maps_path,
        "steps": [s.to_dict() for s in steps],
    }


def prepare_session(out_dir: Optional[str], wave_path: str, *,
                    top: str = "", filelist: Optional[List[str]] = None,
                    filelist_path: Optional[str] = None,
                    incdirs: Optional[List[str]] = None,
                    defines: Optional[List[str]] = None,
                    pack: Optional[str] = None,
                    scopes: Optional[List[str]] = None,
                    signals_file: Optional[str] = None,
                    timeout: Optional[float] = None,
                    build_netlist_flag: bool = True) -> dict:
    """Orchestrate waveform file -> FST -> session and return the manifest path
    plus per-step timing.

    ``wave_path`` is a waveform file your simulator already produced:
      * ``.fst`` — read directly (no conversion).
      * ``.fsdb`` — auto-converted via the bundled fsdb2fst (docs/FSDB_GUIDE.md).
      * ``.vcd`` (anything else) — auto-converted to FST via GTKWave vcd2fst.

    Both conversions are cached in the derived-cache layer (never next to the
    source), so repeated sessions on the same waveform convert once. ``scopes``
    / ``signals_file`` slice a huge FSDB down to a subset (fsdb2fst -l / -L);
    ``pack`` picks the compressor (fastlz / lz4 / zlib, default per converter).
    All three take part in the cache key.

    ``timeout=None`` (default) auto-estimates the conversion cap from the file
    size; the run is heartbeat-monitored, so a stuck converter fails fast
    instead of hanging. Large inputs also log their size and expected duration
    to stderr before conversion starts.

    Never runs a simulator. Does NOT open the session (the server does that so the
    session is registered in its SessionManager)."""
    steps: List[StepResult] = []
    hints: List[str] = []
    explicit_out = bool(out_dir)

    # Reject unsupported formats up front by name: falling through to the VCD
    # converter makes a .ghw/.vpd fail as "VCD not found" or inside vcd2fst,
    # which points at the wrong problem entirely.
    convert.waveform_kind(wave_path)

    lowered = wave_path.lower()
    if lowered.endswith(".fst"):
        # already an FST: read it in place, no conversion step.
        if not os.path.exists(wave_path):
            raise FileNotFoundError(f"FST not found: {wave_path}")
        fst_path = os.path.abspath(wave_path)
    elif lowered.endswith(".fsdb"):
        # FSDB -> FST through the bundled single-pass converter. Cached, since
        # these are routinely GB-scale and cost minutes per run.
        if not os.path.exists(wave_path):
            raise FileNotFoundError(f"FSDB not found: {wave_path}")
        t0 = time.time()
        got = convert.cached_fst(
            wave_path, kind="fsdb", scopes=scopes, signals_file=signals_file,
            pack=pack, timeout=timeout)
        fst_path = got["fst_path"]
        detail = {**got["detail"], "cached": got["cached"],
                  "cache_dir": got["cache_dir"]}
        steps.append(StepResult("convert_fsdb_to_fst", True, time.time() - t0,
                                detail))
        hints.extend(_heavy_waveform_hints(wave_path, detail, scopes, signals_file))
    else:
        # VCD (anything else is rejected by name in convert.waveform_kind)
        if not os.path.exists(wave_path):
            raise FileNotFoundError(f"VCD not found: {wave_path}")
        t0 = time.time()
        got = convert.cached_fst(wave_path, kind="vcd", pack=pack, timeout=timeout)
        fst_path = got["fst_path"]
        detail = {**got["detail"], "cached": got["cached"],
                  "cache_dir": got["cache_dir"]}
        steps.append(StepResult("convert_vcd_to_fst", True, time.time() - t0,
                                detail))
        hints.extend(_heavy_waveform_hints(wave_path, detail, None, None))

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
    files = [f for f in files if f]

    out_dir = resolve_out_dir(out_dir, wave_path=wave_path, top=top, files=files)
    os.makedirs(out_dir, exist_ok=True)

    # build the pyslang netlist (categories 5/6); degrade gracefully on failure.
    # An existing fresh netlist (built by prepare_static_session on the same
    # out_dir, or at the defaulted static location of these sources) is
    # reused instead of re-elaborated.
    maps_path = None
    if build_netlist_flag and files:
        netlist_dir = netlist_home(out_dir, explicit_out, top, files)
        reused = _netlist_fresh(netlist_dir, files)
        if reused:
            maps_path = reused
            steps.append(StepResult("build_netlist", True, 0.0,
                                    {"maps_path": maps_path, "reused": True,
                                     "note": "existing netlist is up to date; "
                                             "skipped re-elaboration"}))
        else:
            t0 = time.time()
            if len(files) >= _PROGRESS_FILE_COUNT:
                _log(f"[wave-mcp] building RTL netlist from {len(files)} source "
                     f"files (this can take minutes on large designs)...")
            try:
                maps_path = build_netlist_maps(
                    netlist_dir, files, top=top,
                    incdirs=inc or None, defines=defs or None)
                modules = 0
                diagnostics = 0
                if maps_path and os.path.exists(maps_path):
                    with open(maps_path) as fh:
                        _m = json.load(fh)
                        modules = len(_m.get("modules", {}))
                        diagnostics = _m.get("diagnostics", 0)
                steps.append(StepResult(
                    "build_netlist", modules > 0, time.time() - t0,
                    {"maps_path": maps_path, "modules": modules,
                     "diagnostics": diagnostics,
                     "incdirs": len(inc), "defines": len(defs),
                     "note": ("" if modules > 0 else
                              "0 modules extracted — check incdirs/"
                              "defines/top; trace/connectivity limited")}))
            except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-except
                # any netlist-build failure degrades gracefully: trace/
                # connectivity off, all other tools keep working (never abort
                # session prep).
                steps.append(StepResult(
                    "build_netlist", False, time.time() - t0,
                    {"error": str(exc),
                     "incdirs": len(inc), "defines": len(defs),
                     "note": "trace/connectivity disabled; other tools still work"}))

    caches = []
    if fst_path and os.path.abspath(fst_path) != os.path.abspath(wave_path):
        caches.append(cache_record("fst", fst_path))
    manifest_path = build_manifest(
        out_dir, fst_path, top=top,
        filelist=files, maps_path=maps_path, wave_path=wave_path,
        caches=caches)
    return {
        "session_path": out_dir,
        "manifest": manifest_path,
        "fst_path": fst_path,
        "maps_path": maps_path,
        "steps": [s.to_dict() for s in steps],
        "hints": hints,
    }
