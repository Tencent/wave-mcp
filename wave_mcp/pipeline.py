"""End-to-end debug pipeline: waveform file -> FST -> session.

This wires the team's standard workflow into one entry point so an LLM client can
go from "I want to analyze the waveform" to a ready session in a single call:

    waveform file (.fst / .fsdb / .vcd)  ->  [convert to FST]  ->
    build session.json           ->  open session          ->  ready to query

The entry point takes a *waveform file your simulator already produced*:
  * ``.fst`` — read directly (no conversion).
  * ``.fsdb`` — auto-converted to FST (bundled fsdb2fst; docs/FSDB_GUIDE.md).
  * ``.vcd`` — auto-converted to FST (GTKWave vcd2fst).

A converted FST is kept beside its source as ``<name>.fst`` and reused from
there (see ``convert.cached_fst``), so building several sessions from one
waveform converts once; partial conversions and unwritable source directories
use the derived-cache layer.

It never invokes a simulator. Run your sim (xrun / Verilator / whatever) with
your own flow, then point this at the resulting waveform.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import convert
from .netlist import build_netlist
from .runtime import storage
from .runtime.identity import cache_key, dataset_identity, file_version
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
                     files: List[str],
                     declared: Optional[Dict[str, Any]] = None) -> str:
    """Identity of the inputs a session is about to be built from.

    ``dataset_identity`` over the manifest as it will be written, minus what
    the build itself produces (``maps_path``, converted FST): the caller's
    waveform (None for a static session), the top and the expanded source
    list. Names the default session directory, so the same inputs asked for
    from anywhere land in one place and two designs never share one.

    ``declared`` is what the filelist *asked for* (see
    :func:`declared_inputs`), not just what resolved. Without it a filelist
    whose ``$VAR`` entries all dropped out resolved to the same file set as a
    genuinely smaller design and silently reused that design's netlist.
    Omitted (inline ``filelist``) when nothing was dropped, so identities of
    complete inputs do not change.
    """
    pre: Dict[str, Any] = {
        "fst_path": os.path.abspath(wave_path) if wave_path else None,
        "top": top or "", "filelist": [os.path.abspath(f) for f in files]}
    if declared:
        pre["declared_inputs"] = declared
    return dataset_identity(pre, os.getcwd())


def declared_inputs(parsed: Optional["ParsedFilelist"]) -> Optional[Dict[str, Any]]:
    """What a filelist declared beyond the files that resolved, or None.

    Only the parts that change what gets elaborated: dropped entries (with the
    variable names that were undefined) and library inputs. A complete filelist
    without libraries yields None and keeps its previous identity.
    """
    if parsed is None:
        return None
    out: Dict[str, Any] = {}
    if parsed.dropped:
        out["dropped"] = [d.get("entry") for d in parsed.dropped]
    if parsed.libdirs or parsed.libfiles:
        out["libdirs"] = list(parsed.libdirs)
        out["libfiles"] = list(parsed.libfiles)
        out["libexts"] = list(parsed.libexts)
    return out or None


def resolve_out_dir(out_dir: Optional[str], *, wave_path: Optional[str] = None,
                    top: str = "", files: Optional[List[str]] = None,
                    declared: Optional[Dict[str, Any]] = None) -> str:
    """Where a session lands.

    ``out_dir`` given: used as given. Omitted: ``<session root>/<identity>``
    with ``identity`` from :func:`session_identity`; the root is
    ``$WAVE_MCP_SESSION_ROOT`` or ``~/.wave-mcp/sessions`` (``storage.py``). Nothing
    is remapped: a caller that names a place gets that place, and whether it
    may write there is the operating system's call.
    """
    identity = "" if out_dir else session_identity(wave_path, top, files or [],
                                                   declared)
    return storage.policy().session_dir(out_dir, identity)


def netlist_home(out_dir: str, explicit: bool, top: str, files: List[str],
                 declared: Optional[Dict[str, Any]] = None) -> str:
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
    return storage.policy().session_dir(
        None, session_identity(None, top, files, declared))


def _read_filelist(path: str) -> List[str]:
    """Back-compat: return just the source files from a ``.f`` filelist."""
    files, _incdirs, _defines = _parse_filelist(path)
    return files


#: ``$VAR`` / ``${VAR}`` left over after ``os.path.expandvars``: the variable was
#: not defined in the server's environment.
_UNEXPANDED_VAR = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")

#: Default library-file extensions when a filelist uses ``-y`` without
#: ``+libext+`` (the Verilog-XL convention most simulators follow).
_DEFAULT_LIBEXTS = (".v", ".sv")

#: How many dropped filelist entries to list verbatim in replies.
_DROPPED_SAMPLE = 10


@dataclass
class ParsedFilelist:
    """Everything a ``.f`` filelist declared, including what could not be used.

    ``files`` are the sources that exist. ``dropped`` records every entry that
    was declared but not taken: an undefined ``$VAR`` in the path, or a path
    that does not exist. A partial filelist must never read as a complete one,
    so callers report these and lower the netlist trust.
    """
    files: List[str] = field(default_factory=list)
    incdirs: List[str] = field(default_factory=list)
    defines: List[str] = field(default_factory=list)
    libdirs: List[str] = field(default_factory=list)
    libfiles: List[str] = field(default_factory=list)
    libexts: List[str] = field(default_factory=list)
    declared: List[str] = field(default_factory=list)
    dropped: List[Dict[str, Any]] = field(default_factory=list)

    def undefined_vars(self) -> List[str]:
        names: List[str] = []
        for d in self.dropped:
            for v in d.get("undefined_vars", []):
                if v not in names:
                    names.append(v)
        return names

    def accounting(self) -> Dict[str, Any]:
        """Declared / resolved / dropped filelist entries plus samples.

        An *entry* is anything the filelist names by path: a source, a nested
        ``-f`` filelist, a ``-y`` directory or a ``-v`` file. ``dropped`` is
        the subset that could not be used; the sources of a nested filelist
        that resolved are counted as its own entries.
        """
        out: Dict[str, Any] = {
            "declared_entries": len(self.declared),
            "resolved_entries": max(len(self.declared) - len(self.dropped), 0),
            "dropped_entries": len(self.dropped),
        }
        if self.dropped:
            out["dropped_samples"] = self.dropped[:_DROPPED_SAMPLE]
            names = self.undefined_vars()
            if names:
                out["undefined_env_vars"] = names
        return out

    def warnings(self) -> List[str]:
        """One line per cause, naming the variable or the count of missing paths."""
        out: List[str] = []
        by_var: Dict[str, int] = {}
        missing = 0
        for d in self.dropped:
            names = d.get("undefined_vars") or []
            for v in names:
                by_var[v] = by_var.get(v, 0) + 1
            if not names:
                missing += 1
        for v, n in by_var.items():
            out.append(f"undefined environment variable ${v}: skipped {n} "
                       "filelist entr" + ("y" if n == 1 else "ies")
                       + "; set it in the MCP server env block")
        if missing:
            out.append(f"{missing} filelist entr"
                       + ("y does" if missing == 1 else "ies do")
                       + " not exist on disk and were skipped")
        return out


class _FilelistParser:
    """One recursive parse of a ``.f`` filelist into :class:`ParsedFilelist`."""

    def __init__(self) -> None:
        self.out = ParsedFilelist()
        self._seen_f: set = set()

    def _resolve(self, raw: str, base: str) -> Tuple[str, List[str]]:
        """Expand ``$VAR`` and anchor on ``base``; return (path, undefined vars)."""
        p = os.path.expandvars(raw)
        undefined = _UNEXPANDED_VAR.findall(p)
        if not os.path.isabs(p):
            p = os.path.normpath(os.path.join(base, p))
        return p, undefined

    def _drop(self, raw: str, path: str, undefined: List[str], where: str,
              kind: str) -> None:
        entry: Dict[str, Any] = {"entry": raw, "kind": kind, "at": where}
        if undefined:
            entry["undefined_vars"] = undefined
        else:
            entry["path"] = path
        self.out.dropped.append(entry)

    def _take_path(self, raw: str, base: str, where: str, kind: str,
                   want_dir: bool = False) -> Optional[str]:
        """Resolve a path argument; record it as dropped when unusable."""
        path, undefined = self._resolve(raw, base)
        self.out.declared.append(path)
        ok = os.path.isdir(path) if want_dir else os.path.isfile(path)
        if undefined or not ok:
            self._drop(raw, path, undefined, where, kind)
            return None
        return path

    def parse(self, path: str, where: str = "") -> None:
        path = os.path.abspath(path)
        if path in self._seen_f:
            return  # a -f cycle or a filelist included twice
        self._seen_f.add(path)
        base = os.path.dirname(path)
        try:
            with open(path) as fh:
                lines = fh.readlines()
        except OSError:
            return
        tokens: List[Tuple[str, int]] = []
        for lineno, line in enumerate(lines, 1):
            s = line.split("//", 1)[0].strip()
            if not s or s.startswith("#"):
                continue
            tokens.extend((t, lineno) for t in s.split())
        name = os.path.basename(path)
        i = 0
        while i < len(tokens):
            tok, lineno = tokens[i]
            nxt = tokens[i + 1][0] if i + 1 < len(tokens) else None
            i += 1 + self._directive(tok, nxt, base, f"{name}:{lineno}")

    def _directive(self, tok: str, nxt: Optional[str], base: str,
                   where: str) -> int:
        """Handle one token; return how many *extra* tokens it consumed."""
        out = self.out
        if tok.startswith("+incdir+"):
            for d in tok[len("+incdir+"):].split("+"):
                if d:
                    out.incdirs.append(self._resolve(d, base)[0])
            return 0
        if tok.startswith("+define+"):
            out.defines.extend(d for d in tok[len("+define+"):].split("+") if d)
            return 0
        if tok.startswith("+libext+"):
            for e in tok[len("+libext+"):].split("+"):
                if e:
                    out.libexts.append(e if e.startswith(".") else "." + e)
            return 0
        if tok.startswith("-I") and len(tok) > 2:
            out.incdirs.append(self._resolve(tok[2:], base)[0])
            return 0
        if nxt is None and tok in ("-incdir", "-y", "-v", "-define", "-d",
                                   "-f", "-F", "-sv_lib"):
            return 0
        if tok == "-incdir":
            out.incdirs.append(self._resolve(nxt, base)[0])
            return 1
        if tok == "-y":
            # library directory: a module that is instantiated but not defined
            # is looked up as <dir>/<module><libext>, never `include-searched
            d = self._take_path(nxt, base, where, "libdir", want_dir=True)
            if d:
                out.libdirs.append(d)
            return 1
        if tok == "-v":
            # library file: parsed, but its modules are not tops by themselves
            f = self._take_path(nxt, base, where, "libfile")
            if f:
                out.libfiles.append(f)
            return 1
        if tok == "-sv_lib":
            return 1  # DPI shared object: nothing to elaborate
        if tok in ("-define", "-d"):
            out.defines.append(nxt)
            return 1
        if tok in ("-f", "-F"):
            sub = self._take_path(nxt, base, where, "filelist")
            if sub:
                self.parse(sub, where)
            return 1
        if tok.startswith(("-", "+")):
            return 0  # unknown simulator option: not a file
        path, undefined = self._resolve(tok, base)
        out.declared.append(path)
        if undefined or not os.path.isfile(path):
            self._drop(tok, path, undefined, where, "source")
        else:
            out.files.append(path)
        return 0


def _uniq(xs: List[str]) -> List[str]:
    seen, out = set(), []
    for x in xs:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def parse_filelist(path: str) -> ParsedFilelist:
    """Parse a Verilog/UVM ``.f`` filelist, keeping account of what was dropped.

    Recognized directives:
      * ``+incdir+<dir>`` (``+``-joined multiples), ``-incdir <dir>``, ``-I<dir>``
      * ``+define+NAME[=VAL]``, ``-define NAME``
      * ``-y <dir>`` library directory and ``-v <file>`` library file; with
        ``+libext+.v+.sv`` selecting the extensions searched in ``-y`` dirs
      * ``-f`` / ``-F <other.f>`` recursion
      * ``$VAR`` / ``${VAR}`` environment expansion. An undefined variable is
        reported (``dropped`` / ``undefined_vars``), never silently skipped.
    Plain tokens are source files (relative to the ``.f`` location). Unknown
    ``-``/``+`` options are ignored.
    """
    p = _FilelistParser()
    p.parse(path)
    out = p.out
    out.files = _uniq(out.files)
    out.incdirs = _uniq(out.incdirs)
    out.defines = _uniq(out.defines)
    out.libdirs = _uniq(out.libdirs)
    out.libfiles = _uniq(out.libfiles)
    out.libexts = _uniq(out.libexts)
    out.declared = _uniq(out.declared)
    return out


def _parse_filelist(path: str) -> Tuple[List[str], List[str], List[str]]:
    """Back-compat triple ``(files, incdirs, defines)``; see :func:`parse_filelist`."""
    parsed = parse_filelist(path)
    return parsed.files, parsed.incdirs, parsed.defines


@dataclass
class StepResult:
    name: str
    ok: bool
    elapsed_sec: float
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"step": self.name, "ok": self.ok,
                "elapsed_sec": round(self.elapsed_sec, 3), **self.detail}


def _conversion_detail(got: Dict[str, Any]) -> Dict[str, Any]:
    """Step detail for a conversion: what ran plus where the FST ended up."""
    detail = {**got.get("detail", {}), "fst_path": got["fst_path"],
              "cached": got["cached"], "placement": got.get("placement")}
    if got.get("cache_dir"):
        detail["cache_dir"] = got["cache_dir"]
    if got.get("reused_by_hand"):
        detail["reused_by_hand"] = True
    return detail


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
                   caches: Optional[List[Dict[str, Any]]] = None,
                   declared: Optional[Dict[str, Any]] = None,
                   filelist_report: Optional[Dict[str, Any]] = None) -> str:
    """Write session.json binding all data sources + versions. Returns path.

    ``fst_path=None`` produces a *static* (netlist-only) session manifest.
    ``wave_path`` is the waveform the caller named when it differs from the
    FST actually opened (a converted VCD/FSDB); ``caches`` lists the derived
    files this session reads from (see ``cache_record``). ``declared`` joins
    the dataset identity (see :func:`declared_inputs`); ``filelist_report`` is
    the declared/resolved/dropped accounting surfaced by ``netlist_health``.
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
    if declared:
        manifest["declared_inputs"] = declared
    if filelist_report:
        manifest["filelist_report"] = filelist_report
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
                       defines: Optional[List[str]] = None,
                       libdirs: Optional[List[str]] = None,
                       libexts: Optional[List[str]] = None,
                       libfiles: Optional[List[str]] = None,
                       inputs_key: Optional[str] = None) -> Optional[str]:
    """Build the pyslang netlist into out_dir/netlist/maps.json. Returns path or
    None on failure (caller degrades gracefully).

    ``incdirs`` (``+incdir+`` dirs) and ``defines`` are essential for real UVM /
    IP designs that use `` `include `` and macro guards — without them
    elaboration fails and connectivity/trace silently degrade to unavailable.
    When ``incdirs`` is not given, the directories of the source files are used
    as a best-effort fallback so sibling `` `include `` files resolve.
    ``libdirs``/``libexts``/``libfiles`` are the ``-y``/``+libext+``/``-v``
    library inputs. ``inputs_key`` is recorded so reuse can check it.
    """
    files = [f for f in (files or []) if f and os.path.exists(f)]
    if not files:
        return None
    return _build_maps(out_dir, files, top, incdirs, defines, libdirs,
                       libexts, libfiles, inputs_key)[0]


def _build_maps(out_dir: str, files: List[str], top: str,
                incdirs: Optional[List[str]], defines: Optional[List[str]],
                libdirs: Optional[List[str]], libexts: Optional[List[str]],
                libfiles: Optional[List[str]],
                inputs_key: Optional[str]) -> Tuple[str, Dict[str, Any]]:
    """Elaborate into ``out_dir/netlist/maps.json``; return (path, stats).

    The stats come from the in-memory result, so the caller never re-parses a
    maps.json that can be GB-scale just to count its modules.
    """
    if not incdirs:
        incdirs = sorted({os.path.dirname(os.path.abspath(f)) for f in files})
    maps_path = os.path.join(out_dir, "netlist", "maps.json")
    result = build_netlist(files, top=top or None, incdirs=incdirs,
                           defines=defines, out_path=maps_path,
                           libdirs=libdirs, libexts=libexts, libfiles=libfiles,
                           build_inputs={"key": inputs_key} if inputs_key else None)
    return maps_path, {
        "modules": len(result.get("modules", {})),
        "diagnostics": result.get("diagnostics", 0),
        "library_files": len((result.get("library") or {}).get("files", [])),
    }


def _build_step(netlist_dir: str, inputs: "_Inputs", top: str,
                zero_note: str) -> Tuple[Optional[str], "StepResult"]:
    """Reuse or build the netlist for ``inputs``; return (maps_path, step)."""
    key = inputs.key(top)
    reused = _netlist_fresh(netlist_dir, inputs.files, key)
    if reused:
        return reused, StepResult("build_netlist", True, 0.0, {
            "maps_path": reused, "reused": True,
            "note": "existing netlist was built from the same inputs and is "
                    "newer than every source; skipped re-elaboration"})
    t0 = time.time()
    if len(inputs.files) >= _PROGRESS_FILE_COUNT:
        _log(f"[wave-mcp] building RTL netlist from {len(inputs.files)} source "
             f"files (this can take minutes on large designs)...")
    maps_path, stats = _build_maps(
        netlist_dir, inputs.files, top, inputs.incdirs or None,
        inputs.defines or None, inputs.libdirs or None,
        inputs.libexts or None, inputs.libfiles or None, key)
    modules = int(stats["modules"])
    detail: Dict[str, Any] = {
        "maps_path": maps_path, "modules": modules,
        "diagnostics": stats["diagnostics"],
        "incdirs": len(inputs.incdirs), "defines": len(inputs.defines),
        "note": "" if modules > 0 else zero_note}
    if inputs.libdirs or inputs.libfiles:
        detail["library_files"] = stats["library_files"]
    if os.path.exists(maps_path):
        size = os.path.getsize(maps_path)
        detail["maps_mb"] = round(size / 1e6, 1)
        if size >= _LARGE_MAPS_BYTES:
            detail["storage_hint"] = (
                f"netlist maps take {size / 1e6:.0f} MB under "
                f"{os.path.dirname(os.path.dirname(maps_path))} (plus a lazy "
                "index of similar size in the cache root). Point "
                "WAVE_MCP_SESSION_ROOT / WAVE_MCP_CACHE_ROOT at a large local "
                "disk if HOME is quota-limited; reclaim with 'wave-mcp gc'.")
    return maps_path, StepResult("build_netlist", modules > 0,
                                 time.time() - t0, detail)


#: maps.json size above which the build step tells where it went and how to
#: reclaim it
_LARGE_MAPS_BYTES = 100 * 1000 * 1000


def netlist_inputs_key(files: List[str], top: str, incdirs: List[str],
                       defines: List[str], libdirs: List[str],
                       libexts: List[str], libfiles: List[str]) -> str:
    """Digest of everything that shapes an elaboration.

    A netlist is reused only when it was built from exactly these inputs;
    mtime freshness alone let a differently configured build (other top, other
    defines, another filelist that happened to resolve to the same files) be
    served as up to date.
    """
    blob = json.dumps({"files": [os.path.abspath(f) for f in files],
                       "top": top or "", "incdirs": list(incdirs),
                       "defines": list(defines), "libdirs": list(libdirs),
                       "libexts": list(libexts), "libfiles": list(libfiles)},
                      sort_keys=True)
    return cache_key(blob)


#: bytes read from the head of maps.json to find ``build_inputs`` without
#: parsing the whole (possibly GB-scale) file
_MAPS_HEAD = 1 << 16


def _maps_build_key(maps_path: str) -> Optional[str]:
    """The ``build_inputs.key`` a maps.json was written with, or None.

    Reads the small summary fields by scanning the file tail, where
    ``build_inputs`` is serialized after ``instance_tree``; falls back to a
    full parse only for files written before this field existed.
    """
    try:
        size = os.path.getsize(maps_path)
        with open(maps_path, "rb") as fh:
            fh.seek(max(0, size - _MAPS_HEAD))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    m = re.search(r'"build_inputs":\{"key":"([0-9a-f]+)"\}', tail)
    return m.group(1) if m else None


def _netlist_fresh(out_dir: str, files: List[str],
                   inputs_key: Optional[str] = None) -> Optional[str]:
    """Return the existing ``out_dir/netlist/maps.json`` when it can be reused.

    Reusable = maps.json exists, was built from the same inputs
    (``inputs_key``), and is newer than every source file. Saves the
    (expensive) re-elaboration when upgrading a static session to a full one,
    or re-running prepare on an unchanged design.
    """
    maps_path = os.path.join(out_dir, "netlist", "maps.json")
    if not os.path.exists(maps_path) or os.path.getsize(maps_path) == 0:
        return None
    if inputs_key is not None and _maps_build_key(maps_path) != inputs_key:
        return None
    maps_mtime = os.path.getmtime(maps_path)
    for f in files:
        if os.path.exists(f) and os.path.getmtime(f) > maps_mtime:
            return None
    return maps_path


@dataclass
class _Inputs:
    """RTL inputs resolved from inline arguments plus an optional ``.f``."""
    files: List[str]
    incdirs: List[str]
    defines: List[str]
    libdirs: List[str]
    libexts: List[str]
    libfiles: List[str]
    parsed: Optional[ParsedFilelist]

    @property
    def declared(self) -> Optional[Dict[str, Any]]:
        return declared_inputs(self.parsed)

    def key(self, top: str) -> str:
        return netlist_inputs_key(self.files, top, self.incdirs, self.defines,
                                  self.libdirs, self.libexts, self.libfiles)

    def report(self) -> Optional[Dict[str, Any]]:
        return self.parsed.accounting() if self.parsed else None

    def warnings(self) -> List[str]:
        return self.parsed.warnings() if self.parsed else []


def _resolve_inputs(filelist: Optional[List[str]],
                    filelist_path: Optional[str],
                    incdirs: Optional[List[str]],
                    defines: Optional[List[str]]) -> _Inputs:
    files = list(filelist or [])
    inc = list(incdirs or [])
    defs = list(defines or [])
    parsed = None
    if filelist_path and os.path.exists(filelist_path):
        parsed = parse_filelist(filelist_path)
        if not files:
            files = list(parsed.files) + [f for f in parsed.libfiles
                                          if f not in parsed.files]
        inc = inc + [d for d in parsed.incdirs if d not in inc]
        defs = defs + [d for d in parsed.defines if d not in defs]
    files = [f for f in files if f and os.path.exists(f)]
    return _Inputs(files, inc, defs,
                   list(parsed.libdirs) if parsed else [],
                   list(parsed.libexts) if parsed else [],
                   list(parsed.libfiles) if parsed else [], parsed)


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

    inputs = _resolve_inputs(filelist, filelist_path, incdirs, defines)
    files = inputs.files
    if not files:
        detail = ""
        if inputs.parsed and inputs.parsed.dropped:
            detail = "; " + "; ".join(inputs.warnings())
        raise ValueError("static session needs RTL sources: pass filelist or "
                         "filelist_path (no valid source file found)" + detail)

    out_dir = resolve_out_dir(out_dir, top=top, files=files,
                              declared=inputs.declared)
    os.makedirs(out_dir, exist_ok=True)

    maps_path, step = _build_step(out_dir, inputs, top,
                                  "0 modules extracted — check incdirs/"
                                  "defines/top")
    steps.append(step)

    manifest_path = build_manifest(out_dir, None, top=top,
                                   filelist=files, maps_path=maps_path,
                                   declared=inputs.declared,
                                   filelist_report=inputs.report())
    return {
        "session_path": out_dir,
        "manifest": manifest_path,
        "fst_path": None,
        "maps_path": maps_path,
        "steps": [s.to_dict() for s in steps],
        "warnings": inputs.warnings(),
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

    A full conversion is kept beside the source as ``<name>.fst`` and reused
    from there, including one the user converted by hand; a stale one is
    overwritten. Partial conversions and unwritable source directories go to
    the derived-cache layer, and ``hints`` says so. ``scopes``
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
        detail = _conversion_detail(got)
        if got.get("notice"):
            hints.append(got["notice"])
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
        detail = _conversion_detail(got)
        if got.get("notice"):
            hints.append(got["notice"])
        steps.append(StepResult("convert_vcd_to_fst", True, time.time() - t0,
                                detail))
        hints.extend(_heavy_waveform_hints(wave_path, detail, None, None))

    # resolve the source file list + include dirs + defines (inline or from .f).
    # A .f filelist is parsed for +incdir+/+define+/-y/-v so the netlist can
    # elaborate real UVM/IP designs; entries that could not be resolved are
    # accounted for and reported, never silently dropped.
    inputs = _resolve_inputs(filelist, filelist_path, incdirs, defines)
    files = inputs.files

    out_dir = resolve_out_dir(out_dir, wave_path=wave_path, top=top, files=files,
                              declared=inputs.declared)
    os.makedirs(out_dir, exist_ok=True)

    # build the pyslang netlist (categories 5/6); degrade gracefully on failure.
    # An existing netlist built from the same inputs (by prepare_static_session
    # on the same out_dir, or at the defaulted static location of these
    # sources) is reused instead of re-elaborated.
    maps_path = None
    if build_netlist_flag and files:
        netlist_dir = netlist_home(out_dir, explicit_out, top, files,
                                   inputs.declared)
        t0 = time.time()
        try:
            maps_path, step = _build_step(
                netlist_dir, inputs, top,
                "0 modules extracted — check incdirs/defines/top; "
                "trace/connectivity limited")
            steps.append(step)
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-except
            # any netlist-build failure degrades gracefully: trace/
            # connectivity off, all other tools keep working (never abort
            # session prep).
            steps.append(StepResult(
                "build_netlist", False, time.time() - t0,
                {"error": str(exc),
                 "incdirs": len(inputs.incdirs), "defines": len(inputs.defines),
                 "note": "trace/connectivity disabled; other tools still work"}))

    caches = []
    if fst_path and os.path.abspath(fst_path) != os.path.abspath(wave_path):
        caches.append(cache_record("fst", fst_path))
    manifest_path = build_manifest(
        out_dir, fst_path, top=top,
        filelist=files, maps_path=maps_path, wave_path=wave_path,
        caches=caches, declared=inputs.declared,
        filelist_report=inputs.report())
    return {
        "session_path": out_dir,
        "manifest": manifest_path,
        "fst_path": fst_path,
        "maps_path": maps_path,
        "steps": [s.to_dict() for s in steps],
        "hints": hints,
        "warnings": inputs.warnings(),
    }
