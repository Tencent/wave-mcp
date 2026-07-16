"""wave-mcp MCP server.

Exposes ~35 concise tools for waveform debug, backed entirely by open-source
sources (FST + xrun.log + RTL static analysis). No license required;
any number of sessions can run concurrently.

Deployment modes:
  * stdio (default, recommended): ``wave-mcp`` — one server per user/module.
  * streamable HTTP multi-session: ``wave-mcp --transport http``.

Tools accept an optional ``session_id`` so a single HTTP server can host many
isolated sessions; in stdio mode it defaults to the one open session.
"""
from __future__ import annotations

import argparse
import os
from typing import Any, List, Optional

from mcp.server.fastmcp import FastMCP

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
        from mcp.server.fastmcp.utilities import func_metadata as _fm
        from mcp.types import TextContent
    except Exception:
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
                except Exception:
                    pass
            out.append(b)
        return out

    _readable._wave_readable = True
    _fm._convert_to_content = _readable


_install_readable_text_patch()

mcp = FastMCP("wave-mcp")
SESSIONS = SessionManager()


def _sess(session_id: Optional[str]):
    return SESSIONS.get(session_id)


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
                    fst_path: Optional[str] = None, log_path: Optional[str] = None,
                    top: str = "", filelist: Optional[List[str]] = None,
                    filelist_path: Optional[str] = None,
                    incdirs: Optional[List[str]] = None,
                    defines: Optional[List[str]] = None,
                    mode: str = "speed",
                    session_id: Optional[str] = None) -> dict[str, Any]:
    """One-shot waveform analysis entry point — the standard team workflow.

    Takes a waveform file your simulator already produced and leaves an OPEN
    session ready to query:
        waveform (.fst read directly / .vcd auto-converted) -> parse xrun.log ->
        build session.json -> open session.

    This never runs a simulator. Run your sim (xrun / Verilator / etc.) with your
    own flow first, then point this at the resulting ``.fst`` or ``.vcd``.

    Call this first whenever you want to start analyzing a waveform; afterwards
    use the query tools (signal_values, list_child_instances, ...).

    Args:
        out_dir: directory to hold the session (session.json, fst).
        wave_path: waveform file to analyze — ``.fst`` (read directly) or ``.vcd``
            (auto-converted to FST). This is a file the sim already dumped.
        fst_path: output FST when converting a VCD (default: out_dir/<vcd>.fst).
        log_path: optional simulator log (xrun.log) to attach for message tools.
        top: top instance name.
        filelist / filelist_path: RTL source list (enables file/declaration tools).
            A .f filelist is parsed for +incdir+/+define+/-y automatically.
        incdirs: extra `+incdir+` directories for netlist elaboration. CRITICAL
            for real UVM/IP designs using `include; without them the netlist
            (connectivity/drivers/trace) silently degrades to unavailable.
        defines: extra `+define+NAME[=VAL]` macros for elaboration.
        mode: VCD->FST packing — "speed" (fastlz, default) / "balanced" / "size".

    Returns the session summary plus per-step timing.
    """
    try:
        result = pipeline.prepare_session(
            out_dir, wave_path, fst_path=fst_path,
            log_path=log_path, top=top, filelist=filelist, filelist_path=filelist_path,
            incdirs=incdirs, defines=defines, mode=mode)
    except (FileNotFoundError, ValueError, convert.ConversionError) as exc:
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
    rows = s.fst.child_instances(instance_full_path, levels,
                                 min(max_scopes, 10000), filter_noise=filter_noise)
    return {"count": len(rows), "instances": rows}


@mcp.tool()
def list_modules(name_contains: Optional[str] = None,
                         session_id: Optional[str] = None) -> dict[str, Any]:
    """Get all module definition names in the design (optionally filtered)."""
    names = _sess(session_id).fst.all_module_names(name_contains)
    return {"count": len(names), "modules": names[:3000]}


@mcp.tool()
def instances_of_module(module: str, session_id: Optional[str] = None) -> dict[str, Any]:
    """Get all instantiation paths of a given module."""
    paths = _sess(session_id).fst.instances_by_module(module)
    return {"count": len(paths), "instances": paths}


@mcp.tool()
def instances_of_module_matching(module: str, string_in_instance_name: str,
                                     session_id: Optional[str] = None) -> dict[str, Any]:
    """Get instantiation paths of a module filtered by instance-name substring."""
    paths = _sess(session_id).fst.instances_by_module(module, string_in_instance_name)
    return {"count": len(paths), "instances": paths}


@mcp.tool()
def scope_info(scope_full_path: str,
                                 session_id: Optional[str] = None) -> dict[str, Any]:
    """Get module info for a scope: module type, declaration & instantiation code.

    Module type comes from the FST; declaration location is resolved (best-effort)
    from the RTL source via Verible-tier scanning.
    """
    s = _sess(session_id)
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
    rows = _sess(session_id).fst.signals_of_instance(
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
    exp = s.fst.timescale_exp
    start = (s.fst.start_time if start_time_as_string in ("min", "")
             else timeutil.time_to_fst_units(start_time_as_string, exp))
    end = (s.fst.end_time if end_time_as_string in ("max", "")
           else timeutil.time_to_fst_units(end_time_as_string, exp))
    rows = s.fst.values_between(full_path, start, end, max_number_of_values)
    if rows is None:
        return {"error": f"signal not found: {full_path}"}
    return {"count": len(rows), "values": rows}


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
def signal_fanin(signal_path: str, session_id: Optional[str] = None) -> dict[str, Any]:
    """Get all signals that can affect the given signal (fan-in; needs UHDM)."""
    return _sess(session_id).rtl.fan_in(signal_path)


@mcp.tool()
def active_drivers(signal_full_path: str, time_as_string: str,
                                 session_id: Optional[str] = None) -> dict[str, Any]:
    """Get the active driver(s) of a signal at a time point (dynamic; needs UHDM+FST)."""
    return _sess(session_id).rtl.active_drivers(signal_full_path, time_as_string)


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
                session_id: Optional[str] = None) -> dict[str, Any]:
    """Trace how a signal's value at a time point was produced (needs UHDM+FST)."""
    return _sess(session_id).rtl.trace_value(signal_path, time_point)


@mcp.tool()
def trace_x(signal_path: str, time_point: str,
            session_id: Optional[str] = None) -> dict[str, Any]:
    """Trace the root cause of an X value on a signal (approximate; needs UHDM+FST)."""
    return _sess(session_id).rtl.trace_x(signal_path, time_point)


# =============================================================================
# 7. Simulation log
# =============================================================================
@mcp.tool()
def log_errors(session_id: Optional[str] = None) -> dict[str, Any]:
    """Get all error/fatal messages from xrun.log."""
    rows = _sess(session_id).log.errors()
    return {"count": len(rows), "messages": rows}


@mcp.tool()
def log_warnings(session_id: Optional[str] = None) -> dict[str, Any]:
    """Get all warning messages from xrun.log."""
    rows = _sess(session_id).log.warnings()
    return {"count": len(rows), "messages": rows}


@mcp.tool()
def search_log(search_string: str,
                                   session_id: Optional[str] = None) -> dict[str, Any]:
    """Search xrun.log messages containing a substring."""
    rows = _sess(session_id).log.containing(search_string)
    return {"count": len(rows), "messages": rows}


@mcp.tool()
def log_messages_by_index(indices: List[int],
                          session_id: Optional[str] = None) -> dict[str, Any]:
    """Get full details for log messages by their indices."""
    rows = _sess(session_id).log.by_indices(indices)
    return {"count": len(rows), "messages": rows}


# =============================================================================
# 7b. Coverage (urg text report / all_bins.csv)
# =============================================================================
@mcp.tool()
def coverage_summary(instance_full_path: Optional[str] = None,
                         max_depth: int = 2,
                         session_id: Optional[str] = None) -> dict[str, Any]:
    """Coverage summary tree (Overall/Block/Expression/Toggle/Fsm/Functional).

    Parses the urg text report. Optionally root the tree at ``instance_full_path``
    and prune to ``max_depth`` levels. Returns ``available: false`` when no
    coverage report was found/attached to the session.
    """
    cov = _sess(session_id).coverage
    if not cov.available:
        return {"available": False,
                "reason": "no coverage report attached; run urg and set "
                          "coverage_report/coverage_csv in the session, or place "
                          "cov_report.txt / all_bins.csv near the session dir"}
    return cov.summary(instance_full_path, max_depth=max_depth)


@mcp.tool()
def coverage_detail(instance_full_path: str,
                        session_id: Optional[str] = None) -> dict[str, Any]:
    """Full coverage metrics + children for one instance node."""
    cov = _sess(session_id).coverage
    if not cov.available:
        return {"available": False, "reason": "no coverage report attached"}
    return cov.detail(instance_full_path)


@mcp.tool()
def coverage_holes(threshold: float = 90.0, metric: str = "overall",
                       max_results: int = 100,
                       session_id: Optional[str] = None) -> dict[str, Any]:
    """List design nodes whose coverage is below ``threshold`` for ``metric``.

    ``metric`` is one of overall/block/expression/toggle/fsm/functional. Results
    are sorted ascending (worst first) — the coverage-hole worklist.
    """
    cov = _sess(session_id).coverage
    if not cov.available:
        return {"available": False, "reason": "no coverage report attached"}
    rows = cov.low_coverage(threshold, metric, max_results)
    return {"available": True, "count": len(rows), "holes": rows}


# =============================================================================
# 7c. Assertions (xrun.log failures + all_bins.csv status)
# =============================================================================
@mcp.tool()
def assertion_failures(max_results: int = 300,
                           session_id: Optional[str] = None) -> dict[str, Any]:
    """Assertion failures parsed from xrun.log (*E/*F,ASRT* lines).

    Each entry carries the assertion name (if present), time, file:line, and the
    $error text. Empty list means no assertion fired (design passed SVA checks).
    """
    asr = _sess(session_id).assertions
    rows = asr.all_failures(max_results)
    return {"available": True, "count": len(rows), "failures": rows}


@mcp.tool()
def assertion_status(name_contains: Optional[str] = None,
                         only_failing: bool = False,
                         max_results: int = 500,
                         session_id: Optional[str] = None) -> dict[str, Any]:
    """Per-assertion pass status from urg all_bins.csv (Assertion Status Grade).

    pass_grade 100 => never failed; <100 => failed at least once. Cover
    properties (c_/p_) are listed with kind="cover". Set ``only_failing`` to show
    just the assertions that failed. Returns available:false without a csv.
    """
    asr = _sess(session_id).assertions
    if not asr.statuses:
        return {"available": False,
                "reason": "no assertion status csv attached (urg all_bins.csv); "
                          "assertion FAILURES from the log are still available via "
                          "assertion_failures"}
    rows = asr.status(name_contains, only_failing, max_results)
    return {"available": True, "count": len(rows), "assertions": rows}


@mcp.tool()
def assertion_summary(session_id: Optional[str] = None) -> dict[str, Any]:
    """Assertion overview: #failures in log, #assertions, #failing, cover count."""
    return _sess(session_id).assertions.summary()


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
# entrypoint
# =============================================================================
def main():
    parser = argparse.ArgumentParser(description="wave-mcp: open-source xrun waveform debug MCP server")
    parser.add_argument("--transport", choices=["stdio", "http", "sse"], default="stdio")
    parser.add_argument("--session", help="optional session dir/json to auto-open at startup")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.session:
        SESSIONS.open(args.session)

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    elif args.transport == "http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="sse")


if __name__ == "__main__":
    main()
