"""wave-mcp MCP server.

Exposes 31 concise tools for waveform debug, static RTL analysis, waveform
diff and the browser wave viewer, backed
entirely by open-source sources (FST + RTL static analysis). No license
required; any number of sessions can run concurrently. Static-only sessions
(open_static_session) work from RTL sources alone — no waveform needed.

Deployment modes:
  * stdio (default, recommended): ``wave-mcp`` — one server per user/module.
  * streamable HTTP multi-session: ``wave-mcp --transport http``.

Tools accept an optional ``session_id`` so a single HTTP server can host many
isolated sessions; in stdio mode it defaults to the one open session.
"""
from __future__ import annotations

import argparse
from typing import Any, List, Optional

from mcp.server.mcpserver import MCPServer

from . import convert, pipeline, timeutil
from .session import SessionManager


_TEXT_MAX_LINES = 400  # cap the human text; structuredContent always has it all


def _render_text(obj, indent: int = 0, lines: Optional[List[str]] = None) -> List[str]:
    """Render a dict/list into human-readable, quote-free plain text (YAML-ish).

    Goal: a ``content[].text`` that reads naturally in a client's raw view — no
    JSON braces, no escaped ``\\"``. Keys/values are printed bare. The full,
    machine-readable data always remains in ``structuredContent``.
    """
    if lines is None:
        lines = []
    pad = "  " * indent

    def _scalar(v) -> str:
        if v is None:
            return "-"
        if isinstance(v, bool):
            return "true" if v else "false"
        return str(v)

    if isinstance(obj, dict):
        for k, v in obj.items():
            if len(lines) >= _TEXT_MAX_LINES:
                lines.append(f"{pad}… (truncated; see structuredContent)")
                return lines
            if isinstance(v, dict) and v:
                lines.append(f"{pad}{k}:")
                _render_text(v, indent + 1, lines)
            elif isinstance(v, list) and v:
                if all(not isinstance(x, (dict, list)) for x in v):
                    # short scalar list -> inline
                    lines.append(f"{pad}{k}: " + ", ".join(_scalar(x) for x in v))
                else:
                    lines.append(f"{pad}{k}:")
                    _render_text(v, indent + 1, lines)
            else:
                val = "(none)" if isinstance(v, (dict, list)) else _scalar(v)
                lines.append(f"{pad}{k}: {val}")
    elif isinstance(obj, list):
        for item in obj:
            if len(lines) >= _TEXT_MAX_LINES:
                lines.append(f"{pad}… (truncated; see structuredContent)")
                return lines
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}-")
                _render_text(item, indent + 1, lines)
            else:
                lines.append(f"{pad}- {_scalar(item)}")
    else:
        lines.append(f"{pad}{_scalar(obj)}")
    return lines


def _install_readable_text_patch() -> None:
    """Make ``content[].text`` a readable plain-text summary instead of JSON.

    FastMCP always emits an unstructured text block in ``content[].text``
    *alongside* ``structuredContent``, and the SDK serializes the tool's dict
    return as pretty JSON (``func_metadata._convert_to_content``, ``indent=2``).
    Clients that show the raw ``content[].text`` then display escaped ``\\"`` /
    ``\\n`` (a string that itself contains JSON — double-encoded).

    This patch renders that dict/list into quote-free YAML-ish text so the raw
    view reads naturally; ``structuredContent`` remains the full machine-readable
    truth. Fully guarded — any SDK change silently no-ops.
    """
    try:
        import pydantic_core
        from mcp.server.mcpserver.utilities import func_metadata as _fm
        from mcp.types import TextContent
    except (ImportError, AttributeError):
        return
    _orig = getattr(_fm, "_convert_to_content", None)
    if _orig is None or getattr(_orig, "_wave_readable", False):
        return

    def _readable(result, **kwargs):
        blocks = _orig(result, **kwargs)
        out = []
        for b in blocks:
            # only rewrite the JSON-object/array fallback text; plain string
            # results / code snippets / images pass through untouched.
            if isinstance(b, TextContent) and b.text and b.text.lstrip()[:1] in "{[":
                try:
                    obj = pydantic_core.from_json(b.text)
                    if isinstance(obj, (dict, list)):
                        b = TextContent(type="text",
                                        text="\n".join(_render_text(obj)))
                except (ValueError, TypeError):
                    pass
            out.append(b)
        return out

    _readable._wave_readable = True
    _fm._convert_to_content = _readable


_install_readable_text_patch()

mcp = MCPServer("wave-mcp")
SESSIONS = SessionManager()


def _sess(session_id: Optional[str]):
    return SESSIONS.get(session_id)


def _no_waveform(feature: str) -> dict[str, Any]:
    """Uniform graceful-degradation reply for waveform tools in a static session."""
    return {"available": False, "feature": feature,
            "reason": "static-only session (no waveform opened)",
            "hint": "Run your simulation, then call prepare_session with the "
                    "dumped .fst/.vcd (same out_dir reuses the netlist) to "
                    "enable value & trace queries."}


# =============================================================================
# 1. Session management
# =============================================================================
@mcp.tool()
def open_session(session_path: str, session_id: Optional[str] = None) -> dict[str, Any]:
    """Open a debug session from a session directory or session.json manifest.

    Opens the FST waveform, the sim log and the RTL netlist maps (if built).
    Does NOT consume any license and supports unlimited concurrent sessions.
    """
    sid = SESSIONS.open(session_path, session_id)
    sess = SESSIONS.get(sid)
    return {"status": "connected", "session_id": sid, **sess.summary()}


@mcp.tool()
def close_session(session_id: Optional[str] = None) -> dict[str, Any]:
    """Close a session and release its resources (FST handle)."""
    ok = SESSIONS.close(session_id)
    return {"status": "disconnected" if ok else "no-such-session"}


@mcp.tool()
def session_info(session_id: Optional[str] = None) -> dict[str, Any]:
    """Return a summary of the active session (top, time range, counts, warnings)."""
    return _sess(session_id).summary()


# =============================================================================
# 0. Waveform preparation (waveform file -> FST -> session)
# =============================================================================
@mcp.tool()
def prepare_session(out_dir: str, wave_path: str,
                    fst_path: Optional[str] = None,
                    top: str = "", filelist: Optional[List[str]] = None,
                    filelist_path: Optional[str] = None,
                    incdirs: Optional[List[str]] = None,
                    defines: Optional[List[str]] = None,
                    mode: str = "speed",
                    fsdb_scopes: Optional[List[str]] = None,
                    fsdb_signals_file: Optional[str] = None,
                    session_id: Optional[str] = None) -> dict[str, Any]:
    """One-shot waveform analysis entry point — the standard team workflow.

    Takes a waveform file your simulator already produced and leaves an OPEN
    session ready to query:
        waveform (.fst read directly / .fsdb or .vcd auto-converted) ->
        build session.json -> open session.

    This never runs a simulator. Run your sim (xrun / Verilator / etc.) with your
    own flow first, then point this at the resulting ``.fst``, ``.fsdb`` or ``.vcd``.

    Conversions are cached next to the source waveform, so repeated sessions on
    the same waveform convert only once.

    Call this first whenever you want to start analyzing a waveform; afterwards
    use the query tools (signal_values, list_child_instances, ...).

    Args:
        out_dir: directory to hold the session (session.json, fst).
        wave_path: waveform file to analyze — ``.fst`` (read directly), ``.fsdb``
            (auto-converted via bundled fsdb2fst) or ``.vcd`` (auto-converted via
            vcd2fst). This is a file the sim already dumped.
        fst_path: explicit output FST when converting (default: cached artifact
            next to the source waveform, falling back to out_dir).
        top: top instance name.
        filelist / filelist_path: RTL source list (enables file/declaration tools).
            A .f filelist is parsed for +incdir+/+define+/-y automatically.
        incdirs: extra `+incdir+` directories for netlist elaboration. CRITICAL
            for real UVM/IP designs using `include; without them the netlist
            (connectivity/drivers/trace) silently degrades to unavailable.
        defines: extra `+define+NAME[=VAL]` macros for elaboration.
        mode: VCD->FST packing — "speed" (fastlz, default) / "balanced" / "size".
        fsdb_scopes: for .fsdb only — convert just the signals whose full path
            contains any of these substrings (fsdb2fst -l). Use on huge designs;
            the loader refuses batches above 5M signals.
        fsdb_signals_file: for .fsdb only — file listing exact signal paths to
            convert, one per line (fsdb2fst -L).

    Returns the session summary plus per-step timing.
    """
    try:
        result = pipeline.prepare_session(
            out_dir, wave_path, fst_path=fst_path,
            top=top, filelist=filelist, filelist_path=filelist_path,
            incdirs=incdirs, defines=defines, mode=mode,
            fsdb_scopes=fsdb_scopes, fsdb_signals_file=fsdb_signals_file)
    except (FileNotFoundError, ValueError, convert.ConversionError) as exc:
        return {"status": "error", "error": str(exc)}
    sid = SESSIONS.open(result["session_path"], session_id)
    sess = SESSIONS.get(sid)
    return {"status": "ready", "session_id": sid, "steps": result["steps"],
            **sess.summary()}


@mcp.tool()
def open_static_session(out_dir: str,
                        top: str = "", filelist: Optional[List[str]] = None,
                        filelist_path: Optional[str] = None,
                        incdirs: Optional[List[str]] = None,
                        defines: Optional[List[str]] = None,
                        session_id: Optional[str] = None) -> dict[str, Any]:
    """Open a pure static-analysis session from RTL sources — NO waveform needed.

    Builds the RTL netlist (pyslang elaboration) and opens the session in one
    call, so you can explore a design before any simulation exists:
    connectivity, drivers, loads, fan-in, hierarchy, module/file/declaration
    queries all work from source code alone.

    Use this to understand design structure, review driver/fan-in relations,
    or check interfaces before running a sim. Waveform tools (signal_values*,
    trace_value, trace_x, active_drivers) return a clear "needs waveform" hint;
    later, call prepare_session with the SAME out_dir and your dumped .fst/.vcd
    to upgrade — the already-built netlist is reused, not re-elaborated.

    Args:
        out_dir: directory to hold the session (session.json, netlist maps).
        top: top module name for elaboration.
        filelist / filelist_path: RTL source list. A .f filelist is parsed for
            +incdir+/+define+/-y automatically.
        incdirs: extra `+incdir+` directories for netlist elaboration.
        defines: extra `+define+NAME[=VAL]` macros for elaboration.

    Returns the session summary (mode: "static") plus per-step timing.
    """
    try:
        result = pipeline.prepare_static_session(
            out_dir, top=top, filelist=filelist, filelist_path=filelist_path,
            incdirs=incdirs, defines=defines)
    except (FileNotFoundError, ValueError) as exc:
        return {"status": "error", "error": str(exc)}
    sid = SESSIONS.open(result["session_path"], session_id)
    sess = SESSIONS.get(sid)
    return {"status": "ready", "session_id": sid, "steps": result["steps"],
            **sess.summary()}


@mcp.tool()
def convert_vcd_to_fst(vcd_path: str, fst_path: Optional[str] = None,
                       mode: str = "speed", parallel: bool = True) -> dict[str, Any]:
    """Convert an xrun-produced VCD to FST (fast).

    xrun's open/parseable dump is VCD; FST is ~1/50 the size and supports fast
    random access, which is what this server reads. Uses GTKWave ``vcd2fst`` with
    the fastest options.

    Args:
        vcd_path: input .vcd path.
        fst_path: output .fst path (default: same name with .fst).
        mode: "speed" (fastlz, fastest), "balanced" (lz4), or "size" (zlib).
        parallel: use multi-core parallel packing (recommended).

    Returns timing, sizes and compression ratio. For *zero* extra wall-clock
    cost, dump straight into a FIFO and stream-convert during simulation
    (see the ``wave-vcd2fst --stream`` CLI / README).
    """
    try:
        res = convert.convert(vcd_path, fst_path, mode=mode, parallel=parallel)
        return {"status": "ok", **res.to_dict()}
    except convert.ConversionError as exc:
        return {"status": "error", "error": str(exc)}


@mcp.tool()
def convert_fsdb_to_fst(fsdb_path: str, fst_path: Optional[str] = None,
                        scopes: Optional[List[str]] = None,
                        signals_file: Optional[str] = None,
                        pack: str = "lz4",
                        info_only: bool = False) -> dict[str, Any]:
    """Convert a Synopsys FSDB to FST via the bundled fsdb2fst (single pass).

    ``prepare_session`` already converts ``.fsdb`` automatically, so reach for
    this tool when you want to inspect or control the conversion first: check the
    scale and signal census of a huge file (``info_only=True``), or convert one
    subtree at a time. No VCD intermediate; requires Verdi's FsdbReader runtime
    on this machine (checks out no license). See docs/FSDB_GUIDE.md.

    Args:
        fsdb_path: input .fsdb path.
        fst_path: output .fst path (default: same name with .fst). The companion
            ``<fst>.hier`` sidecar is written alongside and BOTH files are needed
            to open the FST.
        scopes: convert only signals whose full path contains any of these
            substrings (fsdb2fst -l). Required for very large designs: the loader
            refuses batches above 5M signals and can crash beyond that regardless.
        signals_file: file with exact signal paths, one per line (fsdb2fst -L).
        pack: FST packing — "lz4" (default) / "fastlz" / "zlib".
        info_only: only report scale + signal census, convert nothing.

    Returns the output path, timing, sizes and the signal census (real /
    strength-skipped / unsupported-type counts).
    """
    binary = convert.resolve_fsdb2fst()
    if binary is None:
        return {"status": "error", "error": str(convert.fsdb2fst_missing_error())}
    try:
        if info_only:
            return {"status": "ok", "info_only": True,
                    **convert.fsdb_info(fsdb_path)}
        res = convert.convert_fsdb(fsdb_path, fst_path, scopes=scopes,
                                   signals_file=signals_file, pack=pack)
        return {"status": "ok", **res.to_dict()}
    except convert.ConversionError as exc:
        return {"status": "error", "error": str(exc)}


# =============================================================================
# 2. Design hierarchy exploration
# =============================================================================
@mcp.tool()
def list_child_instances(instance_full_path: str = "",
                                    number_of_levels: int = 1,
                                    max_scopes: int = 2000,
                                    filter_noise: bool = False,
                                    session_id: Optional[str] = None) -> dict[str, Any]:
    """Get child instances of an instance (empty path = top), up to N levels.

    Set ``filter_noise=True`` to drop anonymous begin/fork procedural blocks
    (testbench noise) and show only real design hierarchy. Each row includes
    ``scope_kind`` (module/interface/generate/...) for further client filtering.
    """
    s = _sess(session_id)
    levels = max(1, min(int(number_of_levels), 10))
    if s.fst is None:
        # static mode: answer from the netlist instance_tree (paths are rooted
        # at the elaborated top — no testbench prefix, unlike FST paths).
        if not s.rtl.has_netlist:
            return _no_waveform("list_child_instances")
        tree = s.rtl.maps.get("instance_tree", {})
        prefix = instance_full_path.strip(".")
        base_depth = len(prefix.split(".")) if prefix else 0
        rows = []
        for key, mod in sorted(tree.items()):
            if prefix and not key.startswith(prefix + "."):
                continue
            depth = len(key.split(".")) - base_depth
            if 1 <= depth <= levels:
                rows.append({"path": key, "module_type": mod,
                             "scope_kind": "module", "source": "netlist"})
            if len(rows) >= min(max_scopes, 10000):
                break
        return {"count": len(rows), "instances": rows, "mode": "static",
                "note": "paths are netlist-rooted (no testbench prefix)"}
    rows = s.fst.child_instances(instance_full_path, levels,
                                 min(max_scopes, 10000), filter_noise=filter_noise)
    return {"count": len(rows), "instances": rows}


@mcp.tool()
def list_modules(name_contains: Optional[str] = None,
                         session_id: Optional[str] = None) -> dict[str, Any]:
    """Get all module definition names in the design (optionally filtered)."""
    s = _sess(session_id)
    if s.fst is None:
        if not s.rtl.has_netlist:
            return _no_waveform("list_modules")
        names = sorted(s.rtl.maps.get("modules", {}).keys())
        if name_contains:
            sub = name_contains.lower()
            names = [n for n in names if sub in n.lower()]
        return {"count": len(names), "modules": names[:3000], "mode": "static"}
    names = s.fst.all_module_names(name_contains)
    return {"count": len(names), "modules": names[:3000]}


def _static_instances(s, module: str, name_filter: Optional[str] = None):
    """Instance paths of a module from the netlist instance_tree (static mode)."""
    tree = s.rtl.maps.get("instance_tree", {})
    paths = sorted(k for k, m in tree.items() if m == module)
    if name_filter:
        sub = name_filter.lower()
        paths = [p for p in paths if sub in p.rsplit(".", 1)[-1].lower()]
    return paths


def _static_resolve_module(s, instance_path: str) -> Optional[str]:
    """Resolve an instance path (or bare module name) to a module definition.

    Mirrors TraceEngine leaf anchoring: exact instance_tree key first, then
    unique leaf-suffix match, finally a bare module-definition name.
    """
    tree = s.rtl.maps.get("instance_tree", {})
    path = instance_path.strip(".")
    if path in tree:
        return tree[path]
    if path:
        leaf = path.rsplit(".", 1)[-1]
        cands = {m for k, m in tree.items()
                 if k == leaf or k.endswith("." + leaf)}
        if len(cands) == 1:
            return cands.pop()
        if path in s.rtl.maps.get("modules", {}):
            return path  # bare module-definition name
    return None


@mcp.tool()
def instances_of_module(module: str, session_id: Optional[str] = None) -> dict[str, Any]:
    """Get all instantiation paths of a given module."""
    s = _sess(session_id)
    if s.fst is None:
        if not s.rtl.has_netlist:
            return _no_waveform("instances_of_module")
        paths = _static_instances(s, module)
        return {"count": len(paths), "instances": paths, "mode": "static",
                "note": "paths are netlist-rooted (no testbench prefix)"}
    paths = s.fst.instances_by_module(module)
    return {"count": len(paths), "instances": paths}


@mcp.tool()
def instances_of_module_matching(module: str, string_in_instance_name: str,
                                     session_id: Optional[str] = None) -> dict[str, Any]:
    """Get instantiation paths of a module filtered by instance-name substring."""
    s = _sess(session_id)
    if s.fst is None:
        if not s.rtl.has_netlist:
            return _no_waveform("instances_of_module_matching")
        paths = _static_instances(s, module, string_in_instance_name)
        return {"count": len(paths), "instances": paths, "mode": "static",
                "note": "paths are netlist-rooted (no testbench prefix)"}
    paths = s.fst.instances_by_module(module, string_in_instance_name)
    return {"count": len(paths), "instances": paths}


@mcp.tool()
def scope_info(scope_full_path: str,
                                 session_id: Optional[str] = None) -> dict[str, Any]:
    """Get module info for a scope: module type, declaration & instantiation code.

    Module type comes from the FST; declaration location is resolved (best-effort)
    from the RTL source via Verible-tier scanning.
    """
    s = _sess(session_id)
    if s.fst is None:
        if not s.rtl.has_netlist:
            return _no_waveform("scope_info")
        mod = _static_resolve_module(s, scope_full_path)
        if not mod:
            return {"error": f"scope not found in netlist: {scope_full_path}",
                    "hint": "static-mode paths are netlist-rooted (no "
                            "testbench prefix); try list_child_instances"}
        return {"path": scope_full_path, "module_type": mod, "mode": "static",
                "declaration": s.rtl.module_declaration(mod)}
    info = s.fst.scope_info(scope_full_path)
    if not info:
        return {"error": f"scope not found: {scope_full_path}"}
    decl = s.rtl.module_declaration(info["module_type"])
    info["declaration"] = decl
    return info


# =============================================================================
# 3. Signal query
# =============================================================================
@mcp.tool()
def list_signals(instance_full_path: str,
                            filter_by_name: Optional[str] = None,
                            filter_by_type: Optional[str] = None,
                            max_signals: int = 2000,
                            aggregate_buses: bool = True,
                            underscore_style: bool = False,
                            session_id: Optional[str] = None) -> dict[str, Any]:
    """Get the signals of an instance (ports + internal), with width/dir/type.

    ``filter_by_type`` accepts Port/Input/Output/Inout/Internal-wire/
    Internal-register/Parameter (case-insensitive).

    ``aggregate_buses`` (default True) merges per-element/per-bit VARs a writer
    split apart (``bus [31] ... bus [0]``) into one ``bus[hi:lo]`` entry with an
    ``element_count`` field; per-element signals stay individually queryable by
    full path. Results are ordered ports -> registers -> wires -> parameters so
    a small ``max_signals`` still surfaces meaningful logic signals.

    ``underscore_style`` (default False) also coalesces underscore bit-split
    names (``data_7 ... data_0``); off by default since a real signal may end in
    ``_<n>``. When a netlist is present, merged widths are validated against RTL
    declarations (``width_matches_rtl`` / ``rtl_width`` fields on bus entries).
    """
    s = _sess(session_id)
    if s.fst is None:
        if not s.rtl.has_netlist:
            return _no_waveform("list_signals")
        mod = _static_resolve_module(s, instance_full_path)
        if not mod or mod not in s.rtl.maps.get("modules", {}):
            return {"error": f"instance not found in netlist: {instance_full_path}",
                    "hint": "static-mode paths are netlist-rooted (no "
                            "testbench prefix); try list_child_instances"}
        m = s.rtl.maps["modules"][mod]
        sub = (filter_by_name or "").lower()
        want = (filter_by_type or "").lower()
        rows = []
        ports = m.get("ports", {})
        for name, p in ports.items():
            if sub and sub not in name.lower():
                continue
            direction = p.get("direction", "")
            if want and want not in ("port", direction):
                continue
            rows.append({"name": name, "width": p.get("width"),
                         "direction": direction, "type": "Port",
                         "file": p.get("file"), "line": p.get("line")})
        if not want or want.startswith("internal"):
            for name, sig in m.get("signals", {}).items():
                if name in ports or (sub and sub not in name.lower()):
                    continue
                rows.append({"name": name, "width": sig.get("width"),
                             "type": "Internal", "kind": sig.get("kind"),
                             "file": sig.get("file"), "line": sig.get("line")})
        rows = rows[:min(max_signals, 10000)]
        return {"count": len(rows), "signals": rows, "mode": "static",
                "module": mod,
                "note": "from RTL netlist (declared signals); no runtime values"}
    rows = s.fst.signals_of_instance(
        instance_full_path, filter_by_name, filter_by_type,
        min(max_signals, 10000), aggregate_buses=aggregate_buses,
        underscore_style=underscore_style)
    return {"count": len(rows), "signals": rows}


@mcp.tool()
def signal_info(full_path: str, session_id: Optional[str] = None) -> dict[str, Any]:
    """Get signal metadata: width, type, direction, and declaration file/line.

    Width/type/direction come from the FST; declaration file+line is resolved
    (best-effort) from the RTL source.
    """
    s = _sess(session_id)
    if s.fst is None:
        if not s.rtl.has_netlist:
            return _no_waveform("signal_info")
        inst, _sep, leaf = full_path.strip(".").rpartition(".")
        mod = _static_resolve_module(s, inst) if inst else None
        if mod and mod in s.rtl.maps.get("modules", {}):
            m = s.rtl.maps["modules"][mod]
            p = m.get("ports", {}).get(leaf)
            sig = m.get("signals", {}).get(leaf)
            src = p or sig
            if src:
                return {"name": leaf, "path": full_path, "module": mod,
                        "width": src.get("width"),
                        "direction": (p or {}).get("direction"),
                        "type": "Port" if p else "Internal",
                        "mode": "static",
                        "declaration": {"file": src.get("file"),
                                        "line": src.get("line")}}
        decl = s.rtl.signal_declaration(full_path.rsplit(".", 1)[-1].split("[")[0])
        if decl:
            return {"name": full_path.rsplit(".", 1)[-1], "path": full_path,
                    "mode": "static", "declaration": decl}
        return {"error": f"signal not found in netlist: {full_path}"}
    info = s.fst.signal_info(full_path)
    if not info:
        return {"error": f"signal not found: {full_path}"}
    leaf = info["name"].split("[")[0]
    decl = s.rtl.signal_declaration(leaf)
    info["declaration"] = decl
    return info


# =============================================================================
# 4. Signal value query
# =============================================================================
@mcp.tool()
def signal_values(full_path: str, max_number_of_values: int = 1000,
                       session_id: Optional[str] = None) -> dict[str, Any]:
    """Get all value changes of a signal across the whole simulation."""
    s = _sess(session_id)
    if s.fst is None:
        return _no_waveform("signal_values")
    rows = s.fst.all_values(full_path, max_number_of_values)
    if rows is None:
        return {"error": f"signal not found: {full_path}"}
    return {"count": len(rows), "values": rows}


@mcp.tool()
def signal_values_in_range(full_path: str, start_time_as_string: str,
                                    end_time_as_string: str,
                                    max_number_of_values: int = 5000,
                                    session_id: Optional[str] = None) -> dict[str, Any]:
    """Get value changes of a signal within [start, end] (e.g. "100ns".."500ns")."""
    s = _sess(session_id)
    if s.fst is None:
        return _no_waveform("signal_values_in_range")
    exp = s.fst.timescale_exp
    start = (s.fst.start_time if start_time_as_string in ("min", "")
             else timeutil.time_to_fst_units(start_time_as_string, exp))
    end = (s.fst.end_time if end_time_as_string in ("max", "")
           else timeutil.time_to_fst_units(end_time_as_string, exp))
    rows = s.fst.values_between(full_path, start, end, max_number_of_values)
    if rows is None:
        return {"error": f"signal not found: {full_path}"}
    return {"count": len(rows), "values": rows}


@mcp.tool()
def signal_value_at(full_path: str, time_as_string: str,
                    session_id: Optional[str] = None) -> dict[str, Any]:
    """Get the value of a signal at a specific time point (e.g. "5000ns").

    Unlike ``signal_values_in_range`` which returns all changes, this returns a
    single value: the signal's value held at exactly that time (last change at
    or before the time point).
    """
    s = _sess(session_id)
    if s.fst is None:
        return _no_waveform("signal_value_at")
    exp = s.fst.timescale_exp
    t = timeutil.time_to_fst_units(time_as_string, exp)
    val = s.fst.value_at(full_path, t)
    if val is None:
        return {"error": f"signal not found: {full_path}"}
    return {"signal": full_path, "time": time_as_string, **val}


# =============================================================================
# 5. Connectivity & driver analysis (RTL / UHDM — stage 3/4, graceful degrade)
# =============================================================================
@mcp.tool()
def signal_connectivity(full_path: str, session_id: Optional[str] = None) -> dict[str, Any]:
    """Get signals directly wire-connected to the given signal (needs UHDM)."""
    return _sess(session_id).rtl.connectivity(full_path)


@mcp.tool()
def signal_drivers(full_path: str, session_id: Optional[str] = None) -> dict[str, Any]:
    """Get all code locations that can drive the given signal (static; needs UHDM)."""
    return _sess(session_id).rtl.drivers(full_path)


@mcp.tool()
def signal_loads(full_path: str, session_id: Optional[str] = None) -> dict[str, Any]:
    """Get all signals affected by the given signal (fan-out; needs UHDM)."""
    return _sess(session_id).rtl.loads(full_path)


@mcp.tool()
def signal_fanin(signal_path: str, transitive: bool = False,
                 session_id: Optional[str] = None) -> dict[str, Any]:
    """Get all signals that can affect the given signal (fan-in; needs UHDM).

    Args:
        transitive: if True, recursively expand fan-in across hierarchy levels
            (cross-module fan-in). Default False (direct fan-in only).
    """
    return _sess(session_id).rtl.fan_in(signal_path, transitive=transitive)


@mcp.tool()
def active_drivers(signal_full_path: str, time_as_string: str,
                                 session_id: Optional[str] = None) -> dict[str, Any]:
    """Get the active driver(s) of a signal at a time point (dynamic; needs UHDM+FST)."""
    s = _sess(session_id)
    if s.fst is None:
        return _no_waveform("active_drivers")
    return s.rtl.active_drivers(signal_full_path, time_as_string)


@mcp.tool()
def driver_contributors(driver_unique_id: str,
                            session_id: Optional[str] = None) -> dict[str, Any]:
    """Get the contributing signals (RHS / control) of a driver (needs UHDM)."""
    return _sess(session_id).rtl.driver_contributors(driver_unique_id)


# =============================================================================
# 6. Value tracing (stage 4)
# =============================================================================
@mcp.tool()
def trace_value(signal_path: str, time_point: str,
                max_depth: int = 12,
                session_id: Optional[str] = None) -> dict[str, Any]:
    """Trace how a signal's value at a time point was produced (needs UHDM+FST).

    Args:
        max_depth: maximum recursion depth for the trace tree (default 12, range 1-50).
            Increase for deep designs; decrease for faster, shallower traces.
    """
    s = _sess(session_id)
    if s.fst is None:
        return _no_waveform("trace_value")
    depth = max(1, min(int(max_depth), 50))
    return s.rtl.trace_value(signal_path, time_point, max_depth=depth)


@mcp.tool()
def trace_x(signal_path: str, time_point: str,
            max_depth: int = 12,
            session_id: Optional[str] = None) -> dict[str, Any]:
    """Trace the root cause of an X value on a signal (approximate; needs UHDM+FST).

    Args:
        max_depth: maximum recursion depth for the X-trace tree (default 12, range 1-50).
    """
    s = _sess(session_id)
    if s.fst is None:
        return _no_waveform("trace_x")
    depth = max(1, min(int(max_depth), 50))
    return s.rtl.trace_x(signal_path, time_point, max_depth=depth)


# =============================================================================
# 8. File query
# =============================================================================
@mcp.tool()
def list_files(session_id: Optional[str] = None) -> dict[str, Any]:
    """Get all source files participating in the design (from the filelist)."""
    files = _sess(session_id).rtl.all_files()
    return {"count": len(files), "files": files}


@mcp.tool()
def find_files(file_short_name: str, return_exact_names: bool = False,
                            session_id: Optional[str] = None) -> dict[str, Any]:
    """Find full file paths by (partial) short name."""
    files = _sess(session_id).rtl.files_by_short_name(file_short_name, return_exact_names)
    return {"count": len(files), "files": files}


@mcp.tool()
def modules_in_file(full_file_path: str, session_id: Optional[str] = None) -> dict[str, Any]:
    """Get all modules declared in a given file."""
    mods = _sess(session_id).rtl.modules_in_file(full_file_path)
    return {"count": len(mods), "modules": mods}


# =============================================================================
# 9. Waveform diff (pass/fail first-divergence localization)
# =============================================================================
@mcp.tool()
def diff_waveforms(fst_a: str, fst_b: str,
                   scope: Optional[str] = None,
                   signals: Optional[List[str]] = None,
                   clock: Optional[str] = None,
                   after: Optional[str] = None) -> dict[str, Any]:
    """Compare two FST waveforms (e.g. pass vs fail) and find the first divergence.

    Turns "what changed between these runs" into one deterministic call:
    returns the first divergence time, the earliest-diverging signals (prime
    suspects; later ones are usually downstream contagion) and an honest
    coverage flag. Follow up with signal_fanin/active_drivers on the earliest
    diverger, then open_wave_view with both FSTs and a marker there.

    Args:
        fst_a: first FST path (conventionally the passing run).
        fst_b: second FST path (conventionally the failing run).
        scope: restrict comparison to signals under this instance path —
            strongly recommended on full-chip waveforms.
        signals: explicit signal paths to compare (overrides scope).
        clock: sample on this clock's rising edges instead of raw events;
            filters phase-jitter/glitch false divergences.
        after: ignore differences before this time (e.g. "200ns" to skip
            reset); default compares from time 0.
    """
    from . import diff as _diff
    try:
        return _diff.diff_waveforms(fst_a, fst_b, scope=scope,
                                    signals=signals, clock=clock, after=after)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        return {"status": "error", "error": str(exc)}


# =============================================================================
# 10. Wave viewer (browser-based, surver-streamed; assets optional)
# =============================================================================
def _viewer():
    from .viewer.manager import ViewManager
    return ViewManager.instance()


@mcp.tool()
def open_wave_view(fst_paths: List[str],
                   signals: Optional[List[dict]] = None,
                   cursor: Optional[dict] = None,
                   viewport: Optional[dict] = None,
                   markers: Optional[List[dict]] = None,
                   diff: Optional[dict] = None,
                   annotation: Optional[dict] = None,
                   labels: Optional[List[str]] = None) -> dict[str, Any]:
    """Open waveform(s) in the browser viewer and return the URL for the user.

    Streams via a local surver process, so tens-of-GB FSTs open in
    milliseconds. Give the returned URL to the user; IDE terminals
    auto-forward localhost ports. Two fst_paths open a comparison view.

    Args:
        fst_paths: one FST (normal view) or two (diff view, e.g. [pass, fail]).
        signals: initial signals, each {path, color?, group?, format?, source?}.
            Prefer a short ASCII word for ``group``: the heading is drawn in the
            waveform canvas, whose font has no CJK glyphs, so non-ASCII names
            show as boxes (the grouping itself still works). Spaces are turned
            into underscores automatically. Put prose in ``annotation``, which
            renders any language correctly.
        cursor: {time, unit} to pin the cursor (e.g. the failure time).
        viewport: {from, to, unit} visible time window.
        markers: [{time, unit, label?, color?}] annotations on the timeline.
        diff: diff_waveforms result reference {source_a, source_b,
            first_divergence} — auto-adds a red marker at the divergence.
        annotation: {markdown, confidence?, evidence?} analysis note shown in
            the log popup next to the waveform. Any language: written in your
            own words, rendered as-is.
        labels: display labels per waveform (e.g. ["pass", "fail"]).
    """
    try:
        return _viewer().open_view(
            fst_paths, signals=signals, cursor=cursor, viewport=viewport,
            markers=markers, diff=diff,
            annotations=[annotation] if annotation else None,
            labels=labels)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        return {"available": False, "error": str(exc)}


@mcp.tool()
def update_wave_view(view_id: str,
                     signals: Optional[List[dict]] = None,
                     cursor: Optional[dict] = None,
                     viewport: Optional[dict] = None,
                     markers: Optional[List[dict]] = None,
                     annotation: Optional[dict] = None) -> dict[str, Any]:
    """Update an open wave view in place (same URL, no reload for the user).

    Omitted args keep their current value; lists replace entirely except
    annotations, which append to the analysis log popup. Same ``group`` and
    ``annotation`` conventions as open_wave_view.
    """
    try:
        return _viewer().update_view(
            view_id, signals=signals, cursor=cursor, viewport=viewport,
            markers=markers,
            annotations=[annotation] if annotation else None)
    except (ValueError, RuntimeError) as exc:
        return {"available": False, "error": str(exc)}


@mcp.tool()
def get_view_state(view_id: str) -> dict[str, Any]:
    """Read what the user currently sees in an open wave view.

    Returns the actual state (cursor position, selected/displayed signals,
    viewport, user_dirty) written back by the browser, plus a desired-state
    summary. Use this to continue the conversation from where the user is
    looking, e.g. analyze the signal at their cursor.
    """
    try:
        return _viewer().get_state(view_id)
    except (ValueError, RuntimeError) as exc:
        return {"available": False, "error": str(exc)}


@mcp.tool()
def list_wave_views() -> dict[str, Any]:
    """List the wave views currently open in this server.

    Returns each view's view_id, url, title, waveform paths, revision and
    whether its streaming backend is still alive, newest first. Use it to
    find a view again after losing track of a view_id, or to check what is
    still holding resources during a long batch run.
    """
    try:
        return _viewer().list_views()
    except (ValueError, RuntimeError) as exc:
        return {"available": False, "error": str(exc)}


@mcp.tool()
def close_wave_view(view_id: Optional[str] = None,
                    all_views: bool = False) -> dict[str, Any]:
    """Close a wave view and release its server and streaming backend.

    Views stay open until closed, so a batch that opens many of them should
    close each one when done. The streaming backend is shared between views
    on the same waveform set and is only stopped once no view still uses it.

    Args:
        view_id: the view to close. Required unless all_views is set.
        all_views: close every open view instead of a single one.
    """
    try:
        if all_views:
            return _viewer().close_all()
        if not view_id:
            return {"available": False,
                    "error": "provide view_id, or set all_views=true"}
        return _viewer().close_view(view_id)
    except (ValueError, RuntimeError) as exc:
        return {"available": False, "error": str(exc)}


# =============================================================================
# entrypoint
# =============================================================================
def main():
    parser = argparse.ArgumentParser(description="wave-mcp: open-source xrun waveform debug MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--session", help="optional session dir/json to auto-open at startup")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.session:
        SESSIONS.open(args.session)

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        # mcp SDK v2: transport options moved from settings to run() kwargs.
        mcp.run(transport="streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
