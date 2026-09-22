"""wave-mcp MCP server.

Exposes a concise tool set for waveform debug, static RTL analysis, waveform
diff and the browser wave viewer, backed
entirely by open-source sources (FST + RTL static analysis). No license
required; any number of sessions can run concurrently. Static-only sessions
(open_static_session) work from RTL sources alone — no waveform needed.

Deployment modes:
  * stdio (default, recommended): ``wave-mcp`` — one server per user/module.
  * streamable HTTP multi-session: ``wave-mcp --transport http``.

Tools accept an optional ``session_id``. Every open creates a new work session
with a random id; the id may be omitted only while exactly one session is open
(the common stdio case), otherwise the call is refused as ambiguous rather than
guessed. Several sessions on the same design share one loaded copy of the data.
"""
from __future__ import annotations

import argparse
import functools
import inspect
import json
import os
import signal
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Union

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import convert, pipeline, timeutil
from .__init__ import __version__
from .analysis import activity as _activity
from .analysis import clocking as _clocking
from .analysis import fsm as _fsm
from .analysis import predicate as _predicate
from .analysis import sampling as _sampling
from .analysis import transactions as _transactions
from .analysis import values as _values
from .runtime import local_principal
from .runtime import request as _request
from .runtime import auth as _auth
from .runtime import audit as _audit
from .runtime.executor import BoundedExecutor, ExecutionError, ExecutionLimits
from .runtime import storage
from .runtime.identity import file_version, request_signature
from .session import SessionError, SessionManager, SessionNotFound


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

class _WaveServer(MCPServer):
    """MCPServer that explains retired names before the SDK sees the call.

    ``call_tool`` is the SDK's public entry for one tool invocation. Checking
    here, against the public ``list_tools()`` and each tool's public
    ``input_schema``, keeps the whole 1.0 rename table out of SDK internals:
    an unknown tool that is a retired one, or a declared tool called with an
    undeclared key, is refused with a ``ToolError`` naming the replacement,
    which the SDK turns into an ``isError`` reply the client can read. Any
    other unknown key is refused too, because the SDK's argument model would
    otherwise drop it and answer a different question.
    """

    _schema_keys: Dict[str, frozenset] = {}

    async def _declared_keys(self, name: str) -> Optional[frozenset]:
        keys = self._schema_keys.get(name)
        if keys is None:
            for tool in await self.list_tools():
                props = (tool.input_schema or {}).get("properties") or {}
                self._schema_keys[tool.name] = frozenset(props)
            keys = self._schema_keys.get(name)
        return keys

    async def call_tool(self, name, arguments, context=None):
        declared = await self._declared_keys(name)
        if declared is None:
            hint = rename_hint(name)
            if hint is not None:
                raise ToolError(f"{hint['error']}; use {hint['use_instead']}")
            return await super().call_tool(name, arguments, context)
        extra = sorted(set(arguments or {}) - declared)
        if extra:
            parts = []
            for key in extra:
                repl = _renamed_param_for(key, name)
                parts.append(f"'{key}' (renamed in wave-mcp 1.0: use {repl})"
                             if repl else f"'{key}'")
            raise ToolError(f"unknown parameter(s) for {name}: " + ", ".join(parts))
        return await super().call_tool(name, arguments, context)


mcp = _WaveServer("wave-mcp")

#: The identity every request runs as: the OS account that started the server.
#: Sessions and resources are keyed by it so the session layer has one notion
#: of ownership; there is no second identity behind it.
PRINCIPAL = local_principal()
#: Service-side limits (WAVE_MCP_WORKERS etc.); read once at import.
LIMITS = ExecutionLimits.from_env()
SESSIONS = SessionManager(PRINCIPAL.owner_id,
                          per_owner_sessions=LIMITS.per_owner_sessions,
                          idle_ttl=LIMITS.idle_session_ttl)


def _principal():
    """The ``Principal`` of the current request."""
    return PRINCIPAL
#: The admission gate every tool call passes through (see runtime/executor.py).
EXECUTOR = BoundedExecutor(LIMITS)


def _sess(session_id: Optional[str]):
    """The work session a tool call runs against.

    Inside a tool call this is the session the request snapshot already resolved
    and leased, so the body and the fingerprint describe the same state. Outside
    one (a direct Python call into a helper) it resolves on the spot. Raises
    :class:`SessionError`, which the registration wrapper turns into a structured
    reply, so a missing or ambiguous session is reported the same way by every
    tool.
    """
    snap = _request.current()
    if snap is not None and snap.session is not None:
        if session_id and session_id != snap.session.session_id:
            # A body asking for a different session than the request resolved
            # would be a programming error; refuse rather than silently answer
            # from data the caller did not lease.
            raise SessionNotFound(
                f"no active session with id {session_id!r}",
                hint="the session named on the call is the one it runs against")
        return snap.session
    return SESSIONS.get(session_id, PRINCIPAL.owner_id)


#: Tools that create, close or describe sessions rather than read data from one.
#: They must not take the per-call lease: open creates the session, close
#: destroys it, and session_info may not name one at all.
_SESSION_LIFECYCLE_TOOLS = frozenset({
    "open_session", "close_session", "session_info",
    "prepare_session", "open_static_session",
})


def _bind_call(fn, args, kwargs) -> Dict[str, Any]:
    """Arguments by parameter name, whichever way the caller passed them."""
    if not args:
        return dict(kwargs)
    names = list(inspect.signature(fn).parameters)
    return {**dict(zip(names, args)), **kwargs}


def _session_errors(fn):
    """Turn a session or admission failure into a structured reply.

    Outermost wrapper, so it also writes the audit line: it sees refusals at
    the gate and the reply's ``_fp`` alike, and its clock covers the wait.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        t0 = time.monotonic()
        try:
            out = fn(*args, **kwargs)
        except (SessionError, ExecutionError) as exc:
            out = exc.payload()
            status = "refused" if isinstance(exc, ExecutionError) else "error"
            _audit.record(fn.__name__, status, time.monotonic() - t0,
                          error_type=out.get("error_type"))
            return out
        except BaseException:
            _audit.record(fn.__name__, "error", time.monotonic() - t0,
                          error_type="exception")
            raise
        status, error_type, dataset = _audit.outcome_of(out)
        _audit.record(fn.__name__, status, time.monotonic() - t0,
                      error_type=error_type, dataset=dataset)
        return out
    return wrapper


def _gated(fn):
    """Run the tool body inside one execution slot of the calling owner.

    Outermost of the functional wrappers so the slot covers session resolution
    and leasing too: a saturated server refuses at the gate before touching any
    session state, and the slot is returned only when the body really returns.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return EXECUTOR.run(_principal().owner_id,
                            functools.partial(fn, *args, **kwargs),
                            tool=fn.__name__)
    return wrapper


def _snapshotted(fn):
    """Resolve the request once, then run the tool inside that fixed view.

    In order: bind the caller's identity, resolve and lease the session, copy
    its query defaults, check ``defaults_revision`` if the caller pinned one,
    then run the body with the snapshot in a context variable so ``_sess`` and
    the resolvers read from it rather than looking the session up again. The
    lease is released in ``finally``, so a concurrent ``close_session`` cannot
    destroy the reader mid-scan, and the fingerprint is taken from the snapshot
    *before* the lease goes, which is why it is still there when the session
    was closed while the query ran.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        bound = _bind_call(fn, args, kwargs)
        principal = _principal()
        session = SESSIONS.get(bound.get("session_id"), principal.owner_id)
        lease = SESSIONS.lease(session.session_id, principal.owner_id)
        if lease is None:
            # Closed between the lookup and the lease: report it as gone rather
            # than answering from a reader that may already be closing.
            raise SessionNotFound(
                f"no active session with id {session.session_id!r}",
                hint="the session was closed; open a new one")
        try:
            defaults = session.query_defaults.read()
            pinned = bound.get("defaults_revision")
            if pinned is not None and pinned != defaults["revision"]:
                return _defaults_conflict(pinned, defaults["revision"])
            snap = _request.RequestSnapshot(fn.__name__, principal, session,
                                            lease, defaults, bound)
            token = _request.CURRENT.set(snap)
            # Counted as in flight for the reaper: an idle-TTL sweep must not
            # close a session whose query is still scanning.
            SESSIONS.begin_call(session)
            try:
                out = fn(*args, **kwargs)
            finally:
                SESSIONS.end_call(session)
                _request.CURRENT.reset(token)
            if fn.__name__ in _QUERY_TOOLS:
                _annotate(out, snap)
            return out
        finally:
            lease.release()
    return wrapper


def _annotate(out, snap) -> None:
    """Attach ``_query`` and ``_fp`` to a successful query reply.

    Refusals carry neither: a fingerprint on an error would claim that a
    question was answered when it was not. Bookkeeping must never break a
    query, so any failure here leaves the reply untouched.
    """
    if not isinstance(out, dict) or out.get("status") == "error":
        return
    try:
        out["_query"] = snap.query_report(timeutil.format_fst_time)
        out["_fp"] = snap.fingerprint()
    except Exception:      # pylint: disable=broad-except
        pass


def _abspath(path: str) -> str:
    """Normalize a caller-supplied path the way the request signature needs.

    Same file named through ``~``, a relative path or an absolute path must be
    the same request; delegated to the storage policy's path normalizer.
    """
    return storage.user_path(path)


def _no_waveform(feature: str) -> dict[str, Any]:
    """Uniform graceful-degradation reply for waveform tools in a static session."""
    return {"available": False, "feature": feature,
            "reason": "static-only session (no waveform opened)",
            "hint": "Run your simulation, then call prepare_session with the "
                    "dumped .fst/.vcd (same RTL sources reuse the netlist) to "
                    "enable value & trace queries."}


#: Tools whose replies carry ``_query`` and ``_fp``. Query tools answer *about
#: the waveform*, so reproducing an answer requires knowing which waveform and
#: netlist produced it and what was actually asked. Session lifecycle and
#: query-defaults tools report server-side state rather than waveform facts, so
#: an envelope there would be noise.
_QUERY_TOOLS = frozenset({
    "find_instances", "list_modules", "scope_info", "list_signals",
    "signal_info", "signal_values", "signal_connectivity", "signal_drivers",
    "signal_loads", "signal_fanin", "active_drivers", "driver_contributors",
    "trace_value", "trace_x", "files", "diff_waveforms",
    "signal_activity", "find_time_windows",
    "sample_at_clock", "fold_transactions", "signal_downstream",
    "fsm_transitions",
})


#: Every registered tool and its data-access class. Registration refuses a
#: tool missing here: the class says what a tool touches, it drives the audit
#: line and any future front door, and "forgot to classify" must be a startup
#: failure, not a silent hole.
#:   session-scoped : answers from a leased work session
#:   path-scoped    : takes file paths directly and may read or write them
#:   view-scoped    : browser viewer
#:   server-scoped  : reports server state only, no data access
TOOL_SCOPES: Dict[str, str] = {
    "open_session": "path-scoped", "close_session": "session-scoped",
    "session_info": "session-scoped",
    "query_defaults_set": "session-scoped", "query_defaults_get": "session-scoped",
    "query_defaults_clear": "session-scoped",
    "prepare_session": "path-scoped", "open_static_session": "path-scoped",
    "convert_vcd_to_fst": "path-scoped", "convert_fsdb_to_fst": "path-scoped",
    "find_instances": "session-scoped", "list_modules": "session-scoped",
    "scope_info": "session-scoped", "list_signals": "session-scoped",
    "signal_info": "session-scoped", "signal_values": "session-scoped",
    "signal_activity": "session-scoped", "find_time_windows": "session-scoped",
    "signal_connectivity": "session-scoped", "signal_drivers": "session-scoped",
    "signal_loads": "session-scoped", "signal_fanin": "session-scoped",
    "active_drivers": "session-scoped", "driver_contributors": "session-scoped",
    "trace_value": "session-scoped", "trace_x": "session-scoped",
    "files": "session-scoped", "sample_at_clock": "session-scoped",
    "fsm_transitions": "session-scoped", "signal_downstream": "session-scoped",
    "fold_transactions": "session-scoped", "diff_waveforms": "path-scoped",
    "open_wave_view": "view-scoped", "update_wave_view": "view-scoped",
    "get_view_state": "view-scoped", "list_wave_views": "view-scoped",
    "close_wave_view": "view-scoped",
}


def _tool():
    """Register a tool with the wrappers its shape requires.

    A session-scoped tool (anything with a ``session_id`` parameter that is not
    a lifecycle tool) runs inside one request snapshot: session resolved once,
    resource leased for the call, defaults copied, ``_query`` / ``_fp`` taken
    from that same snapshot. The error conversion is outermost so a refusal is a
    reply rather than an exception.
    """
    def decorate(fn):
        scope = TOOL_SCOPES.get(fn.__name__)
        if scope is None:
            raise RuntimeError(
                f"tool {fn.__name__} has no entry in TOOL_SCOPES; classify it "
                f"before registering (session-, path-, view- or server-scoped)")
        wrapped = fn
        if ("session_id" in inspect.signature(fn).parameters
                and fn.__name__ not in _SESSION_LIFECYCLE_TOOLS):
            wrapped = _snapshotted(wrapped)
        wrapped = _gated(wrapped)
        wrapped = _session_errors(wrapped)
        return mcp.tool()(wrapped)
    return decorate


# --- 1.0 rename map (error path only) -------------------------------------
# 1.0 merged six tools away and unified the parameter names. There is no
# alias shim: an MCP client re-reads the schema each session and the CLI derives
# its flags from the signature, so both follow a rename by themselves, and an
# alias would tax every successful reply forever to serve a rare mistake.
#
# What a stale *hardcoded* caller would otherwise get is an opaque
# "unknown tool" / TypeError, so these two tables turn that into an actionable
# message. They are consulted only when a call has already failed.

#: retired tool name -> what replaces it
_RETIRED_TOOLS = {
    "signal_values_in_range": "signal_values(paths, start=..., end=...)",
    "signal_value_at": "signal_values(paths, time=...)",
    "instances_of_module": "find_instances(module=...)",
    "instances_of_module_matching":
        "find_instances(module=..., name_contains=...)",
    "list_child_instances": "find_instances(under=...)",
    "list_files": "files()",
    "find_files": "files(name=...)",
    "modules_in_file": "files(modules_of=...)",
}
# The pre-release names cursor_set / cursor_get / cursor_clear are deliberately
# absent: they never shipped, so nobody can hold a reference to them.

#: retired parameter name -> current name
_RENAMED_PARAMS = {
    "full_path": "path",
    "signal_path": "path",
    "signal_full_path": "path",
    "instance_full_path": "path",
    "scope_full_path": "path",
    "full_file_path": "source_file (on files(modules_of=...))",
    "time_as_string": "time",
    "start_time_as_string": "start",
    "end_time_as_string": "end",
    "time_point": "time",
    "max_number_of_values": "limit",
    "number_of_levels": "max_depth",
    "string_in_instance_name": "name_contains",
    "file_short_name": "name",
    "return_exact_names": "exact",
    # diff went N-ary in 1.0: the reply also changed shape (a `groups` field
    # partitioning run indices), so this is not a rename an alias could have
    # papered over — the caller has to read the new reply either way.
    "fst_a": "fst_paths=[a, b] (reply now carries per-signal `groups`)",
    "fst_b": "fst_paths=[a, b] (reply now carries per-signal `groups`)",
    # S4 (development standard, section 3.2): one concept, one name.
    "expected_revision": "defaults_revision",
    "fsdb_scopes": "scopes",
    "fsdb_signals_file": "signals_file",
    # S5: one name per concept across every tool.
    "max_signals": "limit",
    "max_scopes": "limit",
    "levels": "max_depth",
    "transitive": "max_depth (1 = direct only, larger = follow the chain)",
    "filter_by_name": "name_contains",
    "filter_by_type": "signal_type",
    "parallel": "(removed: the converter picks parallel packing itself)",
    "mode": "pack (fastlz / lz4 / zlib; the old speed/balanced/size map to those)",
    "fst_path": "out_path on the convert tools; prepare_session no longer takes "
                "an output path (conversions live in the user cache)",
}


def rename_hint(name: str) -> Optional[dict[str, Any]]:
    """Explain a retired tool name, or None if the name is not a retired one."""
    if name in _RETIRED_TOOLS:
        return {"status": "error", "error_type": "retired_tool",
                "error": f"{name} was removed in wave-mcp 1.0",
                "use_instead": _RETIRED_TOOLS[name],
                "hint": "the three value / instance / file query tools were "
                        "merged; call list_tools to see the current set"}
    return None


def renamed_param(name: str) -> Optional[str]:
    """Current name of a retired parameter, or None if it is not a retired one."""
    return _RENAMED_PARAMS.get(name)


def _renamed_param_for(old: str, tool: str) -> Optional[str]:
    """``renamed_param`` resolved for one tool: ``path`` vs ``paths``."""
    new = _RENAMED_PARAMS.get(old)
    fn = globals().get(tool)
    if new == "path" and fn is not None:
        try:
            params = set(inspect.signature(fn).parameters)
        except (TypeError, ValueError):
            params = set()
        # signal_values takes a batch (``paths``); the single-signal tools
        # take ``path``. Name whichever this tool really declares.
        if "path" not in params and "paths" in params:
            new = "paths"
    return new


def param_rename_hint(exc: TypeError, tool: str) -> Optional[dict[str, Any]]:
    """Map an unexpected-keyword TypeError onto the 1.0 rename table."""
    msg = str(exc)
    for old in _RENAMED_PARAMS:
        if f"'{old}'" not in msg:
            continue
        new = _renamed_param_for(old, tool)
        return {"status": "error", "error_type": "renamed_parameter",
                "error": f"{tool}: parameter '{old}' was renamed in "
                         f"wave-mcp 1.0",
                "use_instead": new,
                "hint": "parameter names were unified: path/paths, time, "
                        "start, end, limit"}
    return None


# =============================================================================
# 1. Session management
# =============================================================================
@_tool()
def open_session(session_path: str, session_id: Optional[str] = None) -> dict[str, Any]:
    """Open a debug session from a session directory or session.json manifest.

    Opens the FST waveform and the RTL netlist maps (if built).
    Does NOT consume any license and supports unlimited concurrent sessions.

    Without ``session_id`` this creates a *new* session: a fresh opaque id with
    its own query defaults. Opening the same design twice therefore gives two
    independent sessions, which is what lets two agents (or two conversations)
    work on one design without inheriting each other's window; the expensive part
    (parsed waveform, elaborated netlist) is shared, so the second open does not
    pay for it again.

    Passing ``session_id`` resumes that session instead. The inputs must be the
    same ones it was built from, otherwise the call is refused rather than
    silently re-pointing the session at different data. If the files changed on
    disk since, this reports ``input_changed`` and leaves the session on the data
    it already has: an open is "continue what I had", so it never swaps the data
    underneath a running analysis. ``prepare_session`` is the call that says
    "I rebuilt the data, follow it".
    """
    try:
        sid = SESSIONS.open(_abspath(session_path), session_id,
                            owner_id=PRINCIPAL.owner_id)
    except FileNotFoundError as exc:
        return {"status": "error", "error_type": "not_found",
                "error": str(exc), "parameter": "session_path",
                "hint": "pass a session directory containing session.json, or "
                        "the manifest file itself; build one with "
                        "prepare_session / open_static_session"}
    except ValueError as exc:
        return {"status": "error", "error_type": "invalid_argument",
                "error": f"session manifest is not valid JSON: {exc}",
                "parameter": "session_path"}
    sess = SESSIONS.get(sid, PRINCIPAL.owner_id)
    return {"status": "connected", "session_id": sid, **sess.summary()}

@_tool()
def close_session(session_id: Optional[str] = None) -> dict[str, Any]:
    """Close a session and release its reference to the loaded data.

    The data itself stays loaded while any other session still uses it, so
    closing one session never disturbs another. An unknown, somebody else's or
    already-closed session all report ``no-such-session``, which keeps the reply
    from confirming whether an id exists.
    """
    ok = SESSIONS.close(session_id, PRINCIPAL.owner_id)
    return {"status": "disconnected" if ok else "no-such-session"}

@_tool()
def session_info(session_id: Optional[str] = None,
                 list_sessions: bool = False) -> dict[str, Any]:
    """Summarise the active session, or list the sessions this caller has open.

    With ``list_sessions=True`` it returns every session of the calling owner
    (id, manifest path, mode, resource id, when it was opened and last used) and
    refuses a ``session_id`` in the same call: it is a listing, not a lookup. Use
    it to find a session again after losing an id; it never shows another owner's
    sessions.

    Without it, returns a summary of the named session (or of the only one open):
    top, time range, counts, warnings and the current query defaults.
    """
    if list_sessions:
        if session_id:
            return {"status": "error", "error_type": "invalid_argument",
                    "error": "list_sessions and session_id cannot be combined",
                    "parameter": "session_id",
                    "hint": "call session_info(list_sessions=True) without a "
                            "session_id, or drop list_sessions to read one"}
        sessions = SESSIONS.list_sessions(PRINCIPAL.owner_id)
        return {"status": "ok", "count": len(sessions), "sessions": sessions,
                "server": server_status()}
    return _sess(session_id).summary()


def server_status() -> dict[str, Any]:
    """Occupancy and limits of this server process, for diagnostics.

    Counts only; never another owner's session ids, paths or arguments.
    """
    ex = EXECUTOR.stats()
    sm = SESSIONS.stats()
    return {"running": ex["running"], "waiting": ex["waiting"],
            "admitted": ex["admitted"], "busy_refusals": ex["busy"],
            "queue_timeouts": ex["timeout"],
            "avg_wait_s": ex["avg_wait_s"], "avg_run_s": ex["avg_run_s"],
            "sessions_open": sm["sessions"], "sessions_in_flight": sm["in_flight"],
            "sessions_reaped": sm["reaped"],
            "resources_loaded": sm["resources"].get("keys", 0),
            "limits": {**ex["limits"],
                       "per_owner_sessions": LIMITS.per_owner_sessions,
                       "idle_session_ttl_s": LIMITS.idle_session_ttl}}



def _point_units(fst, val, parameter: str):
    """Parse one time value into FST units; returns ``(units, error)``.

    Accepts the dump limits by name so a default can be pinned to the start or
    end of the waveform without the caller looking them up.
    """
    if isinstance(val, int):
        return val, None
    text = str(val)
    if text == "min":
        return fst.start_time, None
    if text == "max":
        return fst.end_time, None
    try:
        return timeutil.time_to_fst_units(text, fst.timescale_exp), None
    except ValueError as exc:
        return 0, {"status": "error", "error_type": "invalid_argument",
                   "error": str(exc), "parameter": parameter}


# =============================================================================
# 1b. Query defaults (default signals + time window for later calls)
# =============================================================================
# Debugging one failure means asking many questions about the same few signals
# in the same window. Stating them once as defaults removes the retyping where
# paths get mistyped and windows drift apart, and lets a later call say only
# what it is actually varying.
#
# Deliberately a *data-plane* pointer only: which signals, which time span. It
# never holds a hypothesis, a step counter, or a conclusion. wave-mcp reports
# facts about a waveform; storing "what to do next" would turn a base layer
# into an agent with opinions, and two clients on one session would then
# inherit each other's reasoning.
#
# Named query defaults rather than a cursor because several different coding
# agents call this server: the name has to describe the stored data, not one
# editor's pointing device.
def _defaults_conflict(expected: Optional[int], current: int) -> dict[str, Any]:
    """Refusal payload for a revision mismatch.

    Reported instead of applying the change: an update based on a revision that
    has moved was built from a state the caller never saw, and silently rebasing
    it onto the newer one would apply an intent nobody expressed.
    """
    out: Dict[str, Any] = {
        "status": "error", "error_type": "defaults_conflict",
        "error": "query defaults changed since the revision you based this on",
        "current_revision": current,
        "hint": "read them with query_defaults_get, then retry",
    }
    if expected is not None:
        out["defaults_revision"] = expected
    return out


def _publish_defaults(s, paths, start, end,
                      defaults_revision: Optional[int]) -> dict[str, Any]:
    """Validate a complete candidate state, then publish it in one atomic step.

    Validation runs on the candidate before anything is stored, so a bad time
    or a reversed window leaves the existing defaults untouched. The read,
    validate, publish sequence is retried only when the revision moved under us
    and the caller did not pin one; a pinned revision gets a conflict instead.
    """
    cur = s.query_defaults
    exp = s.fst.timescale_exp if s.fst is not None else None
    for _attempt in range(4):
        base = cur.read()
        if (defaults_revision is not None
                and defaults_revision != base["revision"]):
            return _defaults_conflict(defaults_revision, base["revision"])

        cand_paths = _as_paths(paths) if paths is not None else base["paths"]
        cand_start, cand_end = base["start"], base["end"]
        for field, val in (("start", start), ("end", end)):
            if val is None:
                continue
            if val == "":                  # explicit "drop this bound"
                if field == "start":
                    cand_start = None
                else:
                    cand_end = None
                continue
            if s.fst is None:
                return {"status": "error", "error_type": "unavailable",
                        "error": "query defaults with a time window need "
                                 "a waveform",
                        "parameter": field,
                        "hint": "open a waveform session, or set only paths"}
            units, err = _point_units(s.fst, val, field)
            if err:
                return err
            if field == "start":
                cand_start = units
            else:
                cand_end = units

        if (cand_start is not None and cand_end is not None
                and cand_end < cand_start):
            return {"status": "error", "error_type": "invalid_argument",
                    "error": f"end ({timeutil.format_fst_time(cand_end, exp)}) "
                             f"is before start "
                             f"({timeutil.format_fst_time(cand_start, exp)})",
                    "parameter": "end"}

        unknown = []
        if cand_paths and s.fst is not None:
            # Advisory only: a name the waveform lacks is worth saying now rather
            # than once per later call, but it must not block the update
            # (aggregated buses and static-mode paths legitimately miss the map).
            # Probed while no lock is held, because this scans the waveform.
            unknown = [p for p in cand_paths
                       if p not in s.fst.signals
                       and s.fst.value_at(p, s.fst.start_time) is None]

        if cur.replace(cand_paths, cand_start, cand_end, defaults_revision):
            out: Dict[str, Any] = {"status": "ok",
                                   "query_defaults": cur.as_dict(exp)}
            if unknown:
                out["warnings"] = [
                    f"not found in the waveform: {', '.join(unknown)}"]
            return out
        if defaults_revision is not None:
            return _defaults_conflict(defaults_revision, cur.revision)
        # Unpinned and the revision moved: rebuild the candidate on whatever is
        # current now, so the delta keeps meaning the same thing.
    return _defaults_conflict(None, cur.revision)


@_tool()
def query_defaults_set(paths: Union[str, List[str], None] = None,
                       start: Optional[str] = None,
                       end: Optional[str] = None,
                       defaults_revision: Optional[int] = None,
                       session_id: Optional[str] = None) -> dict[str, Any]:
    """Set the default signals and/or time window for the queries that follow.

    Once set, query tools may omit ``paths`` / ``start`` / ``end`` and will use
    these values, replying with ``_defaults_used: true`` so the data's range is
    never ambiguous. An explicit argument always overrides a default.

    Only the fields you pass are changed, so the window can be moved without
    restating the signals. Pass an empty list or an empty string to drop one
    field; use ``query_defaults_clear`` to drop everything. The whole update is
    validated before it is stored, so a rejected call changes nothing.

    Holds waveform *coordinates* only. There is deliberately no place to store a
    hypothesis or a next step: this is a base layer that reports facts, and a
    second client sharing the session must not inherit someone else's reasoning.

    Args:
        paths: signal path or list of paths to make the default.
        start: window start, e.g. "100ns".
        end: window end, e.g. "500ns".
        defaults_revision: refuse the update unless the stored defaults are
            still at this revision, so a caller cannot silently overwrite a
            concurrent change. Read it from ``query_defaults_get``; the same
            name pins a revision on the query tools.
    """
    return _publish_defaults(_sess(session_id), paths, start, end,
                             defaults_revision)


@_tool()
def query_defaults_get(session_id: Optional[str] = None) -> dict[str, Any]:
    """Return the current query defaults (default signals and time window)."""
    s = _sess(session_id)
    exp = s.fst.timescale_exp if s.fst is not None else None
    return {"status": "ok", "query_defaults": s.query_defaults.as_dict(exp)}


@_tool()
def query_defaults_clear(defaults_revision: Optional[int] = None,
                         session_id: Optional[str] = None) -> dict[str, Any]:
    """Clear the query defaults; later calls fall back to the built-in defaults.

    Args:
        defaults_revision: refuse the update unless the stored defaults are
            still at this revision (see ``query_defaults_set``).
    """
    s = _sess(session_id)
    cur = s.query_defaults
    exp = s.fst.timescale_exp if s.fst is not None else None
    if not cur.clear(defaults_revision):
        return _defaults_conflict(defaults_revision, cur.revision)
    return {"status": "ok", "query_defaults": cur.as_dict(exp)}



# =============================================================================
# 0. Waveform preparation (waveform file -> FST -> session)
# =============================================================================
@_tool()
def prepare_session(wave_path: str, out_dir: Optional[str] = None,
                    top: str = "", filelist: Optional[List[str]] = None,
                    filelist_path: Optional[str] = None,
                    incdirs: Optional[List[str]] = None,
                    defines: Optional[List[str]] = None,
                    pack: Optional[str] = None,
                    scopes: Optional[List[str]] = None,
                    signals_file: Optional[str] = None,
                    timeout: Optional[float] = None,
                    session_id: Optional[str] = None) -> dict[str, Any]:
    """One-shot waveform analysis entry point — the standard team workflow.

    Takes a waveform file your simulator already produced and leaves an OPEN
    session ready to query:
        waveform (.fst read directly / .fsdb or .vcd auto-converted) ->
        build session.json -> open session.

    This never runs a simulator. Run your sim (xrun / Verilator / etc.) with your
    own flow first, then point this at the resulting ``.fst``, ``.fsdb`` or ``.vcd``.

    Conversions are cached under the user cache dir (~/.wave-mcp/cache),
    never next to the source waveform, so repeated sessions on the same
    waveform convert only once and read-only regression areas stay untouched.

    Call this first whenever you want to start analyzing a waveform; afterwards
    use the query tools (signal_values, find_instances, signal_activity, ...).

    Args:
        wave_path: waveform file to analyze — ``.fst`` (read directly), ``.fsdb``
            (auto-converted via bundled fsdb2fst) or ``.vcd`` (auto-converted via
            vcd2fst). This is a file the sim already dumped.
        out_dir: where to keep the session (session.json, netlist maps).
            Usually omit it: the session then lands under the wave-mcp session
            root (``$WAVE_MCP_SESSION_ROOT``, default ``~/.wave-mcp/
            sessions``) in a directory named after the inputs, and the netlist
            is shared with every session built from the same RTL sources,
            including a static one. Give it only when the session directory
            has to live at a specific place (e.g. checked in next to a
            testbench); it is then used exactly as given. The reply's
            ``session_path`` is the actual location either way.
        top: top instance name.
        filelist / filelist_path: RTL source list (enables file/declaration tools).
            A .f filelist is parsed for +incdir+/+define+/-y automatically.
        incdirs: extra `+incdir+` directories for netlist elaboration. CRITICAL
            for real UVM/IP designs using `include; without them the netlist
            (connectivity/drivers/trace) silently degrades to unavailable.
        defines: extra `+define+NAME[=VAL]` macros for elaboration.
        pack: FST compressor when converting: "fastlz" (fastest), "lz4"
            (balanced) or "zlib" (smallest). Default is the converter's own
            (fastlz for VCD, lz4 for FSDB). Part of the cache key.
        scopes: for .fsdb only — convert just the signals whose full path
            contains any of these substrings (fsdb2fst -l). Use on huge designs;
            the loader refuses batches above 5M signals.
        signals_file: for .fsdb only — file listing exact signal paths to
            convert, one per line (fsdb2fst -L).
        timeout: optional conversion timeout in seconds. Default (None)
            auto-estimates from the file size (about 3x the expected
            conversion time, capped at 4 hours). Conversions are heartbeat-
            monitored, so a stuck converter fails fast instead of hanging.

    Returns the session summary plus per-step timing. A large waveform adds
    ``hints`` (its size, conversion cost, and how to narrow the next run).
    """
    # A resume names the inputs the session was built from. That check has to run
    # before the pipeline writes anything: the manifest is rewritten during the
    # prepare, so a mismatch found afterwards would have destroyed the evidence.
    signature = request_signature("prepare", {
        "out_dir": _abspath(out_dir) if out_dir else None,
        "wave_path": _abspath(wave_path),
        "top": top, "filelist": list(filelist or []),
        "filelist_path": _abspath(filelist_path) if filelist_path else None,
        "incdirs": list(incdirs or []), "defines": list(defines or []),
        "pack": pack, "scopes": list(scopes or []),
        "signals_file": _abspath(signals_file) if signals_file else None,
    })
    SESSIONS.precheck(session_id, signature, PRINCIPAL.owner_id)

    try:
        result = pipeline.prepare_session(
            out_dir, wave_path,
            top=top, filelist=filelist, filelist_path=filelist_path,
            incdirs=incdirs, defines=defines, pack=pack,
            scopes=scopes, signals_file=signals_file,
            timeout=timeout)
    except (FileNotFoundError, ValueError, convert.ConversionError) as exc:
        return {"status": "error", "error": str(exc)}
    sid = SESSIONS.open(result["session_path"], session_id, signature=signature,
                        refresh=True, owner_id=PRINCIPAL.owner_id)
    sess = SESSIONS.get(sid, PRINCIPAL.owner_id)
    out = {"status": "ready", "session_id": sid, "steps": result["steps"],
           "session_path": result["session_path"], **sess.summary()}
    if result.get("hints"):
        out["hints"] = result["hints"]
    return out


@_tool()
def open_static_session(out_dir: Optional[str] = None,
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
    later, call prepare_session with the same RTL sources and your dumped
    .fst/.vcd to upgrade — the netlist built here is reused, not re-elaborated.

    Args:
        out_dir: where to keep the session (session.json, netlist maps).
            Usually omit it: the session lands under the wave-mcp session root
            in a directory named after the RTL sources, which is exactly where
            a later prepare_session on the same sources looks for the netlist.
            Give it only when the directory has to live at a specific place;
            it is then used as given (and the later prepare_session must name
            the same out_dir to reuse the netlist).
        top: top module name for elaboration.
        filelist / filelist_path: RTL source list. A .f filelist is parsed for
            +incdir+/+define+/-y automatically.
        incdirs: extra `+incdir+` directories for netlist elaboration.
        defines: extra `+define+NAME[=VAL]` macros for elaboration.

    Returns the session summary (mode: "static") plus per-step timing.
    """
    signature = request_signature("static", {
        "out_dir": _abspath(out_dir) if out_dir else None, "top": top,
        "filelist": list(filelist or []),
        "filelist_path": _abspath(filelist_path) if filelist_path else None,
        "incdirs": list(incdirs or []), "defines": list(defines or []),
    })
    SESSIONS.precheck(session_id, signature, PRINCIPAL.owner_id)

    try:
        result = pipeline.prepare_static_session(
            out_dir, top=top, filelist=filelist, filelist_path=filelist_path,
            incdirs=incdirs, defines=defines)
    except (FileNotFoundError, ValueError) as exc:
        return {"status": "error", "error": str(exc)}
    sid = SESSIONS.open(result["session_path"], session_id, signature=signature,
                        refresh=True, owner_id=PRINCIPAL.owner_id)
    sess = SESSIONS.get(sid, PRINCIPAL.owner_id)
    return {"status": "ready", "session_id": sid, "steps": result["steps"],
            "session_path": result["session_path"], **sess.summary()}


@_tool()
def convert_vcd_to_fst(vcd_path: str, out_path: Optional[str] = None,
                       pack: str = "fastlz",
                       timeout: Optional[float] = None) -> dict[str, Any]:
    """Convert an xrun-produced VCD to FST (fast).

    xrun's open/parseable dump is VCD; FST is ~1/50 the size and supports fast
    random access, which is what this server reads. Uses GTKWave ``vcd2fst`` with
    the fastest options.

    Args:
        vcd_path: input .vcd path.
        out_path: output .fst path (default: the derived cache under
            ~/.wave-mcp/cache/fst/, never the input's directory; pass an
            explicit path to place it elsewhere. This tool converts on
            request, it is not the cache).
        pack: FST compressor: "fastlz" (fastest, default), "lz4" or "zlib" (smallest).
        timeout: optional conversion timeout in seconds; default auto-estimates
            from the file size. A converter that stops writing output fails
            fast with its byte count instead of hanging indefinitely.

    Returns timing, sizes and compression ratio. For *zero* extra wall-clock
    cost, dump straight into a FIFO and stream-convert during simulation
    (see the ``wave-vcd2fst --stream`` CLI / README).
    """
    try:
        res = convert.convert(vcd_path, out_path, pack=pack, timeout=timeout)
        return {"status": "ok", **res.to_dict()}
    except convert.ConversionError as exc:
        return {"status": "error", "error": str(exc)}


@_tool()
def convert_fsdb_to_fst(fsdb_path: str, out_path: Optional[str] = None,
                        scopes: Optional[List[str]] = None,
                        signals_file: Optional[str] = None,
                        pack: str = "lz4",
                        timeout: Optional[float] = None,
                        info_only: bool = False) -> dict[str, Any]:
    """Convert a Synopsys FSDB to FST via the bundled fsdb2fst (single pass).

    ``prepare_session`` already converts ``.fsdb`` automatically, so reach for
    this tool when you want to inspect or control the conversion first: check
    the scale and signal census of a huge file (``info_only=True``), or convert one
    subtree at a time. No VCD intermediate; requires Verdi's FsdbReader runtime
    on this machine (checks out no license). See docs/FSDB_GUIDE.md.

    Args:
        fsdb_path: input .fsdb path.
        out_path: output .fst path (default: the derived cache under
            ~/.wave-mcp/cache/fst/, never the input's directory). The companion
            ``<fst>.hier`` sidecar is written alongside and BOTH files are needed
            to open the FST.
        scopes: convert only signals whose full path contains any of these
            substrings (fsdb2fst -l). Required for very large designs: the loader
            refuses batches above 5M signals and can crash beyond that regardless.
        signals_file: file with exact signal paths, one per line (fsdb2fst -L).
        pack: FST compressor: "lz4" (default), "fastlz" (fastest) or "zlib" (smallest).
        timeout: optional conversion timeout in seconds; default auto-estimates
            from the file size. A converter that stops writing output fails
            fast with its byte count instead of hanging indefinitely.
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
        res = convert.convert_fsdb(fsdb_path, out_path, scopes=scopes,
                                   signals_file=signals_file, pack=pack,
                                   timeout=timeout)
        return {"status": "ok", **res.to_dict()}
    except convert.ConversionError as exc:
        return {"status": "error", "error": str(exc)}


# =============================================================================
# 2. Design hierarchy exploration
# =============================================================================
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


def _children_of(s, under: str, max_depth: int, limit: int,
                 filter_noise: bool) -> dict[str, Any]:
    """Instances below a scope (``under=""`` means the top)."""
    if s.fst is None:
        # static mode: answer from the netlist instance_tree (paths are rooted
        # at the elaborated top — no testbench prefix, unlike FST paths).
        if not s.rtl.has_netlist:
            return _no_waveform("find_instances")
        tree = s.rtl.maps.get("instance_tree", {})
        prefix = under.strip(".")
        base_depth = len(prefix.split(".")) if prefix else 0
        rows = []
        for key, mod in sorted(tree.items()):
            if prefix and not key.startswith(prefix + "."):
                continue
            depth = len(key.split(".")) - base_depth
            if 1 <= depth <= max_depth:
                rows.append({"path": key, "module_type": mod,
                             "scope_kind": "module", "source": "netlist"})
            if len(rows) >= min(limit, 10000):
                break
        return {"count": len(rows), "instances": rows, "mode": "static",
                "note": "paths are netlist-rooted (no testbench prefix)"}
    rows = s.fst.child_instances(under, max_depth, min(limit, 10000),
                                 filter_noise=filter_noise)
    return {"count": len(rows), "instances": rows}


def _leaf_of(row) -> str:
    """Leaf (instance) name of an instance row, whatever shape it has.

    The waveform path returns rows keyed ``full_path`` (with a ``name`` field),
    static mode returns rows keyed ``path``, and module lookups return plain
    strings. One accessor keeps the ``name_contains`` filter identical across
    all three instead of silently matching nothing on one of them.
    """
    if isinstance(row, str):
        return row.rsplit(".", 1)[-1]
    if row.get("name"):
        return str(row["name"])
    p = row.get("full_path") or row.get("path") or ""
    return str(p).rsplit(".", 1)[-1]


def _filter_leaf(out: dict[str, Any], name_contains: str) -> dict[str, Any]:
    """Narrow an instance listing to rows whose leaf name contains a substring."""
    if "instances" not in out:
        return out
    sub = name_contains.lower()
    rows = [r for r in out["instances"] if sub in _leaf_of(r).lower()]
    out["instances"] = rows
    out["count"] = len(rows)
    return out


@_tool()
def find_instances(module: Optional[str] = None, under: Optional[str] = None,
                   name_contains: Optional[str] = None, max_depth: int = 1,
                   limit: int = 2000, filter_noise: bool = False,
                   defaults_revision: Optional[int] = None,
                   session_id: Optional[str] = None) -> dict[str, Any]:
    """Locate instances, either by module definition or by position in the tree.

    Two ways in, same instance tree:

      - ``module``  -> every instantiation path of that module definition,
                       optionally narrowed by ``name_contains`` (a substring of
                       the instance's own leaf name).
      - ``under``   -> the instances below that scope; ``under=""`` starts at the
                       top of the design. ``max_depth`` controls the depth.

    At least one of ``module`` / ``under`` / ``name_contains`` must be given, so
    a bare call cannot accidentally dump the whole hierarchy. To walk from the
    top explicitly, pass ``under=""``.

    Args:
        module: module definition name, e.g. "uart_core".
        under: parent scope path; "" means the top of the design.
        name_contains: substring filter on the instance leaf name.
        max_depth: with ``under``, how many hierarchy levels to descend (1-10).
        limit: cap on returned rows when walking the tree.
        filter_noise: with ``under``, drop anonymous begin/fork blocks
            (testbench noise) and keep only real design hierarchy.
    """
    s = _sess(session_id)
    if module is None and under is None and name_contains is None:
        return {"status": "error", "error_type": "invalid_argument",
                "error": "give module, under, or name_contains",
                "parameter": "module",
                "hint": 'pass under="" to list the top-level instances, or '
                        'module="<name>" to find a module\'s instantiations'}

    if module is not None:
        if s.fst is None:
            if not s.rtl.has_netlist:
                return _no_waveform("find_instances")
            paths = _static_instances(s, module, name_contains)
            return {"count": len(paths), "instances": paths, "mode": "static",
                    "note": "paths are netlist-rooted (no testbench prefix)"}
        paths = s.fst.instances_by_module(module)
        if name_contains:
            # filter on the instance's own leaf name, not the whole path, so
            # that a parent scope's name cannot make every child match. Static
            # mode has always behaved this way; this keeps the two consistent.
            sub = name_contains.lower()
            paths = [p for p in paths if sub in p.rsplit(".", 1)[-1].lower()]
        return {"count": len(paths), "instances": paths}

    if under is not None:
        lv = max(1, min(int(max_depth), 10))
        out = _children_of(s, under, lv, limit, filter_noise)
        return _filter_leaf(out, name_contains) if name_contains else out

    # name_contains alone: search the whole tree from the top
    out = _children_of(s, "", 10, limit, filter_noise)
    return _filter_leaf(out, name_contains)


@_tool()
def list_modules(name_contains: Optional[str] = None,
                         defaults_revision: Optional[int] = None,
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


@_tool()
def scope_info(path: str,
               defaults_revision: Optional[int] = None,
               session_id: Optional[str] = None) -> dict[str, Any]:
    """Get module info for a scope: module type, declaration & instantiation code.

    Module type comes from the FST; declaration location is resolved (best-effort)
    from the RTL source via Verible-tier scanning.
    """
    s = _sess(session_id)
    if s.fst is None:
        if not s.rtl.has_netlist:
            return _no_waveform("scope_info")
        mod = _static_resolve_module(s, path)
        if not mod:
            return {"error": f"scope not found in netlist: {path}",
                    "hint": "static-mode paths are netlist-rooted (no "
                            "testbench prefix); try find_instances"}
        return {"path": path, "module_type": mod, "mode": "static",
                "declaration": s.rtl.module_declaration(mod)}
    info = s.fst.scope_info(path)
    if not info:
        return {"error": f"scope not found: {path}"}
    decl = s.rtl.module_declaration(info["module_type"])
    info["declaration"] = decl
    return info


# =============================================================================
# 3. Signal query
# =============================================================================
@_tool()
def list_signals(path: str,
                            name_contains: Optional[str] = None,
                            signal_type: Optional[str] = None,
                            limit: int = 2000,
                            aggregate_buses: bool = True,
                            underscore_style: bool = False,
                            defaults_revision: Optional[int] = None,
                            session_id: Optional[str] = None) -> dict[str, Any]:
    """Get the signals of an instance (ports + internal), with width/dir/type.

    ``name_contains`` keeps signals whose name contains the substring;
    ``signal_type`` accepts Port/Input/Output/Inout/Internal-wire/
    Internal-register/Parameter (case-insensitive). ``limit`` caps the rows.

    ``aggregate_buses`` (default True) merges per-element/per-bit VARs a writer
    split apart (``bus [31] ... bus [0]``) into one ``bus[hi:lo]`` entry with an
    ``element_count`` field; per-element signals stay individually queryable by
    full path. Results are ordered ports -> registers -> wires -> parameters so
    a small ``limit`` still surfaces meaningful logic signals.

    ``underscore_style`` (default False) also coalesces underscore bit-split
    names (``data_7 ... data_0``); off by default since a real signal may end in
    ``_<n>``. When a netlist is present, merged widths are validated against RTL
    declarations (``width_matches_rtl`` / ``rtl_width`` fields on bus entries).
    """
    s = _sess(session_id)
    if s.fst is None:
        if not s.rtl.has_netlist:
            return _no_waveform("list_signals")
        mod = _static_resolve_module(s, path)
        if not mod or mod not in s.rtl.maps.get("modules", {}):
            return {"error": f"instance not found in netlist: {path}",
                    "hint": "static-mode paths are netlist-rooted (no "
                            "testbench prefix); try find_instances"}
        m = s.rtl.maps["modules"][mod]
        sub = (name_contains or "").lower()
        want = (signal_type or "").lower()
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
        rows = rows[:min(limit, 10000)]
        return {"count": len(rows), "signals": rows, "mode": "static",
                "module": mod,
                "note": "from RTL netlist (declared signals); no runtime values"}
    rows = s.fst.signals_of_instance(
        path, name_contains, signal_type,
        min(limit, 10000), aggregate_buses=aggregate_buses,
        underscore_style=underscore_style)
    return {"count": len(rows), "signals": rows}


@_tool()
def signal_info(path: Optional[str] = None,
                defaults_revision: Optional[int] = None,
                session_id: Optional[str] = None) -> dict[str, Any]:
    """Get signal metadata: width, type, direction, and declaration file/line.

    Width/type/direction come from the FST; declaration file+line is resolved
    (best-effort) from the RTL source.

    ``path`` may be omitted when the defaults hold exactly one signal.
    """
    s = _sess(session_id)
    path, cur, err = _resolve_one_path(s, path)
    if err:
        return err
    if s.fst is None:
        if not s.rtl.has_netlist:
            return _no_waveform("signal_info")
        inst, _sep, leaf = path.strip(".").rpartition(".")
        mod = _static_resolve_module(s, inst) if inst else None
        if mod and mod in s.rtl.maps.get("modules", {}):
            m = s.rtl.maps["modules"][mod]
            p = m.get("ports", {}).get(leaf)
            sig = m.get("signals", {}).get(leaf)
            src = p or sig
            if src:
                return _mark_defaults(
                    {"name": leaf, "path": path, "module": mod,
                     "width": src.get("width"),
                     "direction": (p or {}).get("direction"),
                     "type": "Port" if p else "Internal",
                     "mode": "static",
                     "declaration": {"file": src.get("file"),
                                     "line": src.get("line")}}, cur)
        decl = s.rtl.signal_declaration(path.rsplit(".", 1)[-1].split("[")[0])
        if decl:
            return _mark_defaults({"name": path.rsplit(".", 1)[-1], "path": path,
                                 "mode": "static", "declaration": decl}, cur)
        return {"error": f"signal not found in netlist: {path}"}
    info = s.fst.signal_info(path)
    if not info:
        return {"error": f"signal not found: {path}"}
    leaf = info["name"].split("[")[0]
    decl = s.rtl.signal_declaration(leaf)
    info["declaration"] = decl
    return _mark_defaults(info, cur)


# =============================================================================
# 4. Signal value query
# =============================================================================
def _mark_defaults(out: dict[str, Any], used: bool) -> dict[str, Any]:
    """Flag a reply that drew any default from the query defaults.

    Only set when it is true: a reply without the key means every bound came
    from the call itself, which is the common case and should stay uncluttered.
    """
    if used and isinstance(out, dict):
        out["_defaults_used"] = True
    return out


def _defaults_of(s) -> Dict[str, Any]:
    """The query defaults this call runs against.

    Inside a tool call this is the copy the request snapshot took at the start,
    so a concurrent ``query_defaults_set`` cannot change the window half way
    through one request. Outside a snapshot it reads the live store.
    """
    snap = _request.current()
    if snap is not None and snap.defaults is not None:
        return snap.defaults
    return s.query_defaults.read()


def _record(name: str, value: Any, *, from_default: bool = False) -> None:
    """Note a normalized effective argument on the current snapshot, if any."""
    snap = _request.current()
    if snap is not None:
        snap.set(name, value, from_default=from_default)


def _record_mode(mode: str) -> None:
    snap = _request.current()
    if snap is not None:
        snap.mode = mode


def _resolve_paths(s, paths) -> tuple[List[str], bool]:
    """Requested paths, or the stored defaults' when the caller passed none.

    Returns ``(paths, used_defaults)``. An explicit argument always wins, so a
    default can never silently redirect a call that named its own signals.
    """
    if paths is not None and paths != []:
        plist = _as_paths(paths)
        _record("paths", list(plist))
        return plist, False
    stored = _defaults_of(s)["paths"]
    if stored:
        _record("paths", list(stored), from_default=True)
        return list(stored), True
    return [], False


def _resolve_window(s, start, end) -> tuple[Any, Any, bool]:
    """Window bounds, falling back to the defaults for whichever side is absent.

    ``None`` means "not specified by the caller"; the literal strings ``"min"`` /
    ``"max"`` remain an explicit request for the dump limits and are *not*
    treated as absent, otherwise a caller could not deliberately widen back to
    the whole dump while defaults are set. Each side falls back independently.
    The normalized integer bounds are recorded later by ``_window_units``; here
    only the *source* of each bound is noted.
    """
    used = False
    state = _defaults_of(s)
    if start is None:
        if state["start"] is not None:
            start, used = state["start"], True
            _record("start", state["start"], from_default=True)
        else:
            start = "min"
    if end is None:
        if state["end"] is not None:
            end, used = state["end"], True
            _record("end", state["end"], from_default=True)
        else:
            end = "max"
    return start, end, used


def _window_units(fst, start, end):
    """Parse a start/end time pair into FST time units.

    Returns ``(t0, t1, error)``; ``error`` is a structured reply (or None) and
    names the parameter that failed, so a bad time string never reaches the
    engine and never raises out of a tool. Integers pass through as FST units
    already (that is how the defaults store them).
    """
    exp = fst.timescale_exp
    try:
        t0 = (fst.start_time if str(start) in ("min", "")
              else start if isinstance(start, int)
              else timeutil.time_to_fst_units(start, exp))
    except ValueError as exc:
        return 0, 0, {"status": "error", "error_type": "invalid_argument",
                      "error": str(exc), "parameter": "start"}
    try:
        t1 = (fst.end_time if str(end) in ("max", "")
              else end if isinstance(end, int)
              else timeutil.time_to_fst_units(end, exp))
    except ValueError as exc:
        return 0, 0, {"status": "error", "error_type": "invalid_argument",
                      "error": str(exc), "parameter": "end"}
    if t1 < t0:
        return 0, 0, {"status": "error", "error_type": "invalid_argument",
                      "error": f"end ({end}) is before start ({start})",
                      "parameter": "end"}
    # The effective window is the *normalized* pair, whichever spelling or
    # default produced it; "from_default" was already noted by the resolver.
    snap = _request.current()
    if snap is not None:
        snap.effective["start"] = t0
        snap.effective["end"] = t1
        if snap.mode is None:
            snap.mode = "window"
    return t0, t1, None


def _as_paths(paths) -> List[str]:
    """Accept a single path or a list of them; normalize to a list.

    A blank string normalizes to the empty list, so ``paths=""`` drops the set
    rather than registering a signal whose name is empty.
    """
    if isinstance(paths, str):
        return [paths] if paths.strip() else []
    return list(paths or [])


@_tool()
def signal_values(paths: Union[str, List[str], None] = None,
                  time: Optional[str] = None,
                  start: Optional[str] = None, end: Optional[str] = None,
                  limit: Optional[int] = None,
                  defaults_revision: Optional[int] = None,
                  session_id: Optional[str] = None) -> dict[str, Any]:
    """Read signal values: a whole dump, a time window, or one instant.

    One tool for all three shapes, because they are the same query with the
    window degenerating. Several paths are read in a single pass over the file,
    so batching is cheaper than looping.

      - pass ``time``            -> the value each signal holds at that instant
      - pass ``start`` / ``end`` -> every change inside that window
      - pass neither             -> every change in the whole dump

    Omitted arguments fall back to the session query defaults (see
    ``query_defaults_set``), and the reply carries ``_defaults_used: true`` when
    any did, so the time range of the data is never ambiguous. An explicit
    argument always wins; pass ``start="min"`` / ``end="max"`` to widen back to
    the whole dump despite a default window.

    ``limit`` is a target reply size (default 1000). When more changes match,
    the timeline is *evenly downsampled* instead of cut short, keeping the first
    and last change, and the reply says so via ``sampled`` / ``sample_rate`` /
    ``total_available``. Narrow ``start``/``end`` to get full resolution.

    Args:
        paths: one signal path or a list of them, e.g. ["top.u.din", "top.u.q"].
        time: read a single instant, e.g. "5000ns". Overrides start/end.
        start: window start, e.g. "100ns"; "min" = start of the dump.
        end: window end, e.g. "500ns"; "max" = end of the dump.
        limit: target number of changes per signal in the reply.
    """
    s = _sess(session_id)
    if s.fst is None:
        return _no_waveform("signal_values")
    plist, def_p = _resolve_paths(s, paths)
    if not plist:
        return {"status": "error", "error_type": "invalid_argument",
                "error": "paths must name at least one signal",
                "parameter": "paths",
                "hint": "pass paths, or set a default with query_defaults_set(paths=[...])"}

    if time not in (None, ""):
        try:
            t = timeutil.time_to_fst_units(time, s.fst.timescale_exp)
        except ValueError as exc:
            return {"status": "error", "error_type": "invalid_argument",
                    "error": str(exc), "parameter": "time"}
        _record_point(t)
        out = _values.read_point(s.fst, plist, t, time)
        return _mark_defaults(out, def_p)

    start, end, def_w = _resolve_window(s, start, end)
    t0, t1, err = _window_units(s.fst, start, end)
    if err:
        return err
    _record("limit", _sampling.resolve_limit(limit))
    out = _values.read_values(s.fst, plist, t0, t1, limit)
    out["window"] = {"start": timeutil.format_fst_time(t0, s.fst.timescale_exp),
                     "end": timeutil.format_fst_time(t1, s.fst.timescale_exp)}
    return _mark_defaults(out, def_p or def_w)


@_tool()
def signal_activity(paths: Union[str, List[str], None] = None,
                    start: Optional[str] = None,
                    end: Optional[str] = None,
                    defaults_revision: Optional[int] = None,
                    session_id: Optional[str] = None) -> dict[str, Any]:
    """Per-signal activity summary over a time window, in one pass over the file.

    Omitted arguments fall back to the session query defaults (see ``query_defaults_set``); the
    reply carries ``_defaults_used: true`` when any did.

    Answers "which of these signals actually moved here, and did any spend time
    unknown" without pulling raw value timelines first. One row per requested
    path, in request order:

      toggles        value changes inside the window
      x_ratio        share of the window the signal *held* an x value
      z_ratio        same for z (both are time weighted, not change weighted)
      is_constant    no change inside the window
      first_change   time of the first change, null if it never changed
      last_change    time of the last change, null if it never changed
      value_first    value held at the window start
      value_last     value held at the window end

    A path that is not in the waveform returns an error row for that path only,
    so one bad name does not lose the rest of the batch.

    Args:
        paths: one signal path or a list of them, e.g. ["top.u.din", "top.u.state"].
        start: window start such as "100ns"; "min" means simulation start.
        end: window end such as "500ns"; "max" means simulation end.
    """
    s = _sess(session_id)
    if s.fst is None:
        return _no_waveform("signal_activity")
    plist, def_p = _resolve_paths(s, paths)
    if not plist:
        return {"status": "error", "error_type": "invalid_argument",
                "error": "paths must name at least one signal",
                "parameter": "paths",
                "hint": "pass paths, or set a default with query_defaults_set(paths=[...])"}
    start, end, def_w = _resolve_window(s, start, end)
    t0, t1, err = _window_units(s.fst, start, end)
    if err:
        return err
    rows = _activity.signal_activity(s.fst, plist, t0, t1)
    exp = s.fst.timescale_exp
    ok = [r for r in rows if "error" not in r]
    return _mark_defaults(
        {"window": {"start": timeutil.format_fst_time(t0, exp),
                    "end": timeutil.format_fst_time(t1, exp)},
         "count": len(ok),
         "signals": rows},
        def_p or def_w)


@_tool()
def find_time_windows(predicate: Dict[str, Any], start: Optional[str] = None,
                      end: Optional[str] = None,
                      min_duration: Optional[str] = None,
                      defaults_revision: Optional[int] = None,
                      session_id: Optional[str] = None) -> dict[str, Any]:
    """Find the time intervals where a boolean condition over signals holds.

    Returns intervals, not values, so "when was req high while cnt stayed above
    1" is one call instead of a value dump plus local post-processing.

    An omitted ``start`` / ``end`` falls back to the session query defaults (see
    ``query_defaults_set``); the reply carries ``_defaults_used: true``
    when it did.

    The predicate is an expression object. Leaves:
      {"k":"sig","name":"top.u.req"}     signal value (paths from list_signals)
      {"k":"const","lit":"8'd3"}         SystemVerilog integer literal
    Combinators:
      {"k":"bin","op":<op>,"l":<node>,"r":<node>}
      {"k":"un","op":"LogicalNot","a":<node>}
      {"k":"bitselect","base":<node>,"idx":<n>}
      <op> is one of LogicalAnd, LogicalOr, Equality, Inequality,
      CaseEquality, CaseInequality, BinaryAnd, BinaryOr, BinaryXor,
      LessThan, GreaterThan, LessThanEqual, GreaterThanEqual.
    Example:
      {"k":"bin","op":"LogicalAnd",
       "l":{"k":"bin","op":"Equality","l":{"k":"sig","name":"top.u.req"},
            "r":{"k":"const","lit":"1'b1"}},
       "r":{"k":"bin","op":"GreaterThan","l":{"k":"sig","name":"top.u.cnt"},
            "r":{"k":"const","lit":"8'd1"}}}

    A predicate that cannot be decided (an x/z value reaching the expression) is
    reported as not true, never as a hit. The undecidable time is returned
    separately so an empty result is not misread as "it never happened". An
    interval still true at the window end is marked open_ended.

    Args:
        predicate: expression object as described above.
        start: search window start such as "100ns"; "min" = dump start.
        end: search window end; "max" = dump end.
        min_duration: report only intervals at least this long, e.g. "20ns".
    """
    s = _sess(session_id)
    if s.fst is None:
        return _no_waveform("find_time_windows")
    start, end, def_w = _resolve_window(s, start, end)
    t0, t1, err = _window_units(s.fst, start, end)
    if err:
        return err
    md = 0
    if min_duration not in (None, ""):
        try:
            md = timeutil.time_to_fst_units(min_duration,
                                            s.fst.timescale_exp)
        except ValueError as exc:
            return {"status": "error", "error_type": "invalid_argument",
                    "error": str(exc), "parameter": "min_duration"}
    if not isinstance(predicate, dict):
        return {"status": "error", "error_type": "invalid_argument",
                "error": "predicate must be an expression object",
                "parameter": "predicate",
                "hint": 'a signal leaf looks like '
                        '{"k":"sig","name":"top.u.req"}'}
    res = _predicate.find_time_windows(s.fst, predicate, t0, t1, md)
    if res.get("status") == "error":
        return res
    res["window"] = {"start": timeutil.format_fst_time(t0, s.fst.timescale_exp),
                     "end": timeutil.format_fst_time(t1, s.fst.timescale_exp)}
    return _mark_defaults(res, def_w)


def _resolve_one_path(s, path) -> tuple[Optional[str], bool, Optional[dict]]:
    """A single signal path, falling back to the defaults when unambiguous.

    Returns ``(path, used_defaults, error)``. A default set holding several
    signals does *not* supply one here: picking one would be an arbitrary choice
    the caller never made, and the resulting answer would look authoritative
    while describing a signal they did not ask about. Say so instead.
    """
    if path:
        _record("path", path)
        return path, False, None
    cur = _defaults_of(s)["paths"]
    if len(cur) == 1:
        _record("path", cur[0], from_default=True)
        return cur[0], True, None
    if len(cur) > 1:
        return None, False, {
            "status": "error", "error_type": "invalid_argument",
            "error": f"path is required: the query defaults hold {len(cur)} "
                     f"signals, so there is no single default",
            "parameter": "path",
            "default_paths": list(cur),
            "hint": "name one of their signals explicitly"}
    return None, False, {
        "status": "error", "error_type": "invalid_argument",
        "error": "path is required", "parameter": "path",
        "hint": "pass path, or set a one-signal default with "
                "query_defaults_set(paths=[...])"}


def _resolve_time(s, time) -> tuple[Optional[Any], bool, Optional[dict]]:
    """A single time point, falling back to the defaults' window start.

    The window start is the meaningful default for a point query: it is where
    the region of interest begins. The end is deliberately not used, since
    "either edge of the window" would be ambiguous.
    """
    if time:
        return time, False, None
    start = _defaults_of(s)["start"]
    if start is not None:
        _record("time", start, from_default=True)
        return start, True, None
    return None, False, {
        "status": "error", "error_type": "invalid_argument",
        "error": "time is required", "parameter": "time",
        "hint": "pass time, or set a default with "
                "query_defaults_set(start=...)"}


def _time_text(s, t) -> str:
    """Render a resolved time for engines that parse a time *string*.

    The defaults store FST units (integers) since that is what comparisons need,
    while the trace/driver engines take the textual form. Converting here keeps
    that mismatch out of every call site. The integer form is what the request
    records as the effective ``time``, so a "30ns" and its default-supplied
    equivalent digest identically.
    """
    if isinstance(t, int):
        _record_point(t)
        return timeutil.format_fst_time(t, s.fst.timescale_exp)
    try:
        _record_point(timeutil.time_to_fst_units(str(t), s.fst.timescale_exp))
    except ValueError:
        pass                     # the engine reports the bad string itself
    return str(t)


def _record_point(units: int) -> None:
    """Record a point query's effective instant (integer units) and mode."""
    snap = _request.current()
    if snap is not None:
        snap.effective["time"] = units
        snap.mode = snap.mode or "point"


# =============================================================================
# 5. Connectivity & driver analysis (RTL / UHDM — stage 3/4, graceful degrade)
# =============================================================================


@_tool()
def signal_connectivity(path: Optional[str] = None,
                        defaults_revision: Optional[int] = None,
                        session_id: Optional[str] = None) -> dict[str, Any]:
    """Get signals directly wire-connected to the given signal (needs UHDM).

    ``path`` may be omitted when the defaults hold exactly one signal.
    """
    s = _sess(session_id)
    p, cur, err = _resolve_one_path(s, path)
    if err:
        return err
    return _mark_defaults(s.rtl.connectivity(p), cur)


@_tool()
def signal_drivers(path: Optional[str] = None,
                   defaults_revision: Optional[int] = None,
                   session_id: Optional[str] = None) -> dict[str, Any]:
    """Get all code locations that can drive the given signal (static; needs UHDM).

    ``path`` may be omitted when the defaults hold exactly one signal.
    """
    s = _sess(session_id)
    p, cur, err = _resolve_one_path(s, path)
    if err:
        return err
    return _mark_defaults(s.rtl.drivers(p), cur)


@_tool()
def signal_loads(path: Optional[str] = None,
                 defaults_revision: Optional[int] = None,
                 session_id: Optional[str] = None) -> dict[str, Any]:
    """Get all signals affected by the given signal (fan-out; needs UHDM).

    ``path`` may be omitted when the defaults hold exactly one signal.
    """
    s = _sess(session_id)
    p, cur, err = _resolve_one_path(s, path)
    if err:
        return err
    return _mark_defaults(s.rtl.loads(p), cur)


@_tool()
def signal_fanin(path: Optional[str] = None, max_depth: int = 1,
                 limit: int = 500,
                 defaults_revision: Optional[int] = None,
                 session_id: Optional[str] = None) -> dict[str, Any]:
    """Get all signals that can affect the given signal (fan-in; needs UHDM).

    Boundary nets (struct ports such as ``reg2hw``, aggregated buses,
    sub-module outputs) resolve to the peer ports one hop away; ``max_depth=1``
    returns every direct peer, a larger value the cone behind each, following
    boundary hops up to that many levels. For source locations rather than
    signal names use signal_drivers.

    ``path`` may be omitted when the defaults hold exactly one signal.

    Args:
        max_depth: hierarchy levels to follow (1 = direct fan-in only, the
            default; up to 8 for the cross-module cone).
        limit: cap on returned signals.
    """
    s = _sess(session_id)
    p, cur, err = _resolve_one_path(s, path)
    if err:
        return err
    return _mark_defaults(s.rtl.fan_in(p, max_depth=max_depth, limit=limit), cur)


@_tool()
def active_drivers(path: Optional[str] = None, time: Optional[str] = None,
                   defaults_revision: Optional[int] = None,
                   session_id: Optional[str] = None) -> dict[str, Any]:
    """Get the active driver(s) of a signal at a time point (dynamic; needs UHDM+FST).

    ``path`` may be omitted when the defaults hold exactly one signal, and
    ``time`` falls back to the query defaults's window start.

    A malformed time string is answered with a structured invalid_argument
    error instead of raising.
    """
    s = _sess(session_id)
    if s.fst is None:
        return _no_waveform("active_drivers")
    p, def_p, err = _resolve_one_path(s, path)
    if err:
        return err
    t, def_t, err = _resolve_time(s, time)
    if err:
        return err
    try:
        return _mark_defaults(s.rtl.active_drivers(p, _time_text(s, t)),
                            def_p or def_t)
    except ValueError as exc:
        return {"status": "error", "error_type": "invalid_argument",
                "error": str(exc), "parameter": "time"}


@_tool()
def driver_contributors(driver_unique_id: str,
                            defaults_revision: Optional[int] = None,
                            session_id: Optional[str] = None) -> dict[str, Any]:
    """Get the contributing signals (RHS / control) of a driver (needs UHDM)."""
    return _sess(session_id).rtl.driver_contributors(driver_unique_id)


# =============================================================================
# 6. Value tracing (stage 4)
# =============================================================================
@_tool()
def trace_value(path: Optional[str] = None, time: Optional[str] = None,
                max_depth: int = 12,
                defaults_revision: Optional[int] = None,
                session_id: Optional[str] = None) -> dict[str, Any]:
    """Trace how a signal's value at a time point was produced (needs UHDM+FST).

    ``path`` may be omitted when the defaults hold exactly one signal, and
    ``time`` falls back to the query defaults's window start.

    A malformed time string is answered with a structured invalid_argument
    error instead of raising.

    Args:
        max_depth: maximum recursion depth for the trace tree (default 12, range 1-50).
            Increase for deep designs; decrease for faster, shallower traces.
    """
    s = _sess(session_id)
    if s.fst is None:
        return _no_waveform("trace_value")
    p, def_p, err = _resolve_one_path(s, path)
    if err:
        return err
    t, def_t, err = _resolve_time(s, time)
    if err:
        return err
    depth = max(1, min(int(max_depth), 50))
    try:
        return _mark_defaults(
            s.rtl.trace_value(p, _time_text(s, t), max_depth=depth),
            def_p or def_t)
    except ValueError as exc:
        return {"status": "error", "error_type": "invalid_argument",
                "error": str(exc), "parameter": "time"}


@_tool()
def trace_x(path: Optional[str] = None, time: Optional[str] = None,
            max_depth: int = 12,
            defaults_revision: Optional[int] = None,
            session_id: Optional[str] = None) -> dict[str, Any]:
    """Trace the root cause of an X value on a signal (approximate; needs UHDM+FST).

    ``path`` may be omitted when the defaults hold exactly one signal, and
    ``time`` falls back to the query defaults's window start.

    A malformed time string is answered with a structured invalid_argument
    error instead of raising.

    Args:
        max_depth: maximum recursion depth for the X-trace tree (default 12, range 1-50).
    """
    s = _sess(session_id)
    if s.fst is None:
        return _no_waveform("trace_x")
    p, def_p, err = _resolve_one_path(s, path)
    if err:
        return err
    t, def_t, err = _resolve_time(s, time)
    if err:
        return err
    depth = max(1, min(int(max_depth), 50))
    try:
        return _mark_defaults(
            s.rtl.trace_x(p, _time_text(s, t), max_depth=depth),
            def_p or def_t)
    except ValueError as exc:
        return {"status": "error", "error_type": "invalid_argument",
                "error": str(exc), "parameter": "time"}


# =============================================================================
# 8. File query
# =============================================================================
@_tool()
def files(name: Optional[str] = None, exact: bool = False,
          modules_of: Optional[str] = None,
          defaults_revision: Optional[int] = None,
          session_id: Optional[str] = None) -> dict[str, Any]:
    """List the design's source files, find one by name, or read a file's modules.

    Three shapes of the same file index:

      - no arguments      -> every source file participating in the design
      - ``name``          -> full paths whose short name matches (substring, or
                             exact with ``exact=True``)
      - ``modules_of``    -> the module definitions declared in that file

    Note ``modules_of`` takes a *filesystem* path, not a design hierarchy path.

    Args:
        name: (partial) file short name, e.g. "uart_core.sv".
        exact: require the short name to match exactly rather than as a substring.
        modules_of: full path of a source file whose modules you want.
    """
    rtl = _sess(session_id).rtl
    if modules_of:
        mods = rtl.modules_in_file(modules_of)
        return {"source_file": modules_of, "count": len(mods),
                "modules": mods}
    if name:
        found = rtl.files_by_short_name(name, exact)
        return {"count": len(found), "files": found}
    found = rtl.all_files()
    return {"count": len(found), "files": found}


@_tool()
def sample_at_clock(paths: Union[str, List[str], None] = None,
                    clock: str = "", edge: str = "rising",
                    start: Optional[str] = None, end: Optional[str] = None,
                    limit: Optional[int] = None,
                    defaults_revision: Optional[int] = None,
                    session_id: Optional[str] = None) -> dict[str, Any]:
    """Values of signals sampled on a clock's edges: one row per clock cycle.

    A waveform stores changes, but synchronous logic is reasoned about per
    cycle. Reading raw changes makes every combinational glitch and every
    picosecond of skew look like an event; sampling on the edge asks what the
    logic actually captured.

    Returns a cycle table: each row is one edge time plus the value each
    requested signal held at that instant. ``null`` means the signal had no
    recorded value yet, which is not the same as x and is not collapsed into it.

    Only clean 0->1 (or 1->0) clock transitions count as edges. A clock passing
    through x or z yields no edge, since an unknown clock captured nothing
    definite.

    ``paths`` / ``start`` / ``end`` fall back to the session query defaults.

    Args:
        paths: signal path or list of paths to sample.
        clock: full path of the clock signal, e.g. "top.clk".
        edge: "rising" (default), "falling", or "both".
        start: window start, e.g. "100ns"; "min" = start of the dump.
        end: window end; "max" = end of the dump.
        limit: target number of cycles in the reply; more are downsampled
            evenly (first and last kept) and the reply says so.
    """
    s = _sess(session_id)
    if s.fst is None:
        return _no_waveform("sample_at_clock")
    plist, def_p = _resolve_paths(s, paths)
    if not plist:
        return {"status": "error", "error_type": "invalid_argument",
                "error": "paths must name at least one signal",
                "parameter": "paths",
                "hint": "pass paths, or set a default with query_defaults_set(paths=[...])"}
    if not clock:
        return {"status": "error", "error_type": "invalid_argument",
                "error": "clock is required", "parameter": "clock",
                "hint": "pass the clock's full path, e.g. clock='top.clk'"}
    if clock not in s.fst.signals:
        return {"status": "error", "error_type": "invalid_argument",
                "error": f"clock signal not in the waveform: {clock}",
                "parameter": "clock",
                "hint": "use list_signals to find the clock's full path"}
    if edge not in ("rising", "falling", "both"):
        return {"status": "error", "error_type": "invalid_argument",
                "error": f"edge must be rising, falling or both (got {edge!r})",
                "parameter": "edge"}

    start, end, def_w = _resolve_window(s, start, end)
    t0, t1, err = _window_units(s.fst, start, end)
    if err:
        return err
    _record("limit", _sampling.resolve_limit(limit))
    out = _clocking.sample_table(s.fst, plist, clock, t0, t1, edge, limit)
    out["window"] = {"start": timeutil.format_fst_time(t0, s.fst.timescale_exp),
                     "end": timeutil.format_fst_time(t1, s.fst.timescale_exp)}
    return _mark_defaults(out, def_p or def_w)


@_tool()
def fsm_transitions(path: Optional[str] = None, start: Optional[str] = None,
                    end: Optional[str] = None,
                    defaults_revision: Optional[int] = None,
                    session_id: Optional[str] = None) -> dict[str, Any]:
    """State transitions a register actually made, plus which RTL branches ran.

    Two facts a waveform can settle about a state machine: which transitions
    occurred (with counts and first occurrence), and which of the RTL branches
    assigning this register ever had its guard hold at a transition instant.

    **This is not coverage.** Coverage needs a denominator — the states and
    transitions the design is *supposed* to have — and that comes from intent,
    not from one dump. A state or transition not listed may be unreachable by
    design, simply not exercised by this stimulus, or a real hole; one waveform
    cannot tell those apart, so nothing here is reported as a gap or a bug.

    A branch whose guard was undecidable (x/z) at some transitions reports
    ``undecided_at``, because "taken: false" would otherwise overstate the case.

    ``path`` may be omitted when the defaults hold exactly one signal; the window
    falls back to the query defaults.

    Args:
        path: full path of the state register, e.g. "top.u_ctrl.state".
        start: window start, e.g. "100ns"; "min" = start of the dump.
        end: window end; "max" = end of the dump.
    """
    s = _sess(session_id)
    if s.fst is None:
        return _no_waveform("fsm_transitions")
    p, def_p, err = _resolve_one_path(s, path)
    if err:
        return err
    start, end, def_w = _resolve_window(s, start, end)
    t0, t1, err = _window_units(s.fst, start, end)
    if err:
        return err
    out = _fsm.fsm_transitions(s.fst, s.rtl, p, t0, t1)
    if out.get("status") == "error":
        return out
    out["window"] = {"start": timeutil.format_fst_time(t0, s.fst.timescale_exp),
                     "end": timeutil.format_fst_time(t1, s.fst.timescale_exp)}
    return _mark_defaults(out, def_p or def_w)


@_tool()
def signal_downstream(path: Optional[str] = None, time: Optional[str] = None,
                      max_depth: int = 1, limit: int = 500,
                      defaults_revision: Optional[int] = None,
                      session_id: Optional[str] = None) -> dict[str, Any]:
    """Signals this one can affect, optionally with when each next changed.

    The forward mirror of ``signal_fanin``. ``signal_loads`` gives one hop;
    this follows the chain, including across hierarchy boundaries.

    Pass ``time`` to join the netlist answer with the waveform: each downstream
    signal also reports the first time it changed *after* that instant. That
    turns "what does this reach" into "what actually moved as a result", which
    is the question worth asking when a signal went wrong at a known moment.

    Reachability comes from the netlist and a change after ``time`` is only a
    correlation: a signal may change for an unrelated reason, and this does not
    claim causation. ``first_change_after`` being null means it never changed
    again, which is itself evidence the path was not exercised.

    ``path`` may be omitted when the defaults hold exactly one signal; ``time``
    falls back to the defaults' window start when a window is set.

    Args:
        path: signal path to walk forward from.
        time: instant to measure downstream changes after, e.g. "300ns".
            Omit to get reachability only.
        max_depth: hierarchy levels to follow (1 = one hop, the default; up
            to 8 to follow the chain across module boundaries).
        limit: cap on returned signals.
    """
    s = _sess(session_id)
    p, def_p, err = _resolve_one_path(s, path)
    if err:
        return err
    res = s.rtl.fan_out(p, max_depth=max_depth, limit=limit)
    if not res.get("available"):
        return _mark_defaults(res, def_p)

    downstream = res.get("fan_out") or []
    def_t = False
    default_start = _defaults_of(s)["start"]
    if time is None and default_start is not None:
        time, def_t = default_start, True
        _record("time", default_start, from_default=True)
    if time is not None and downstream:
        if s.fst is None:
            res["timing_note"] = ("no waveform in this session, so only "
                                  "reachability is reported")
            return _mark_defaults(res, def_p or def_t)
        t0, err = _point_units(s.fst, time, "time")
        if err:
            return err
        _record_point(t0)
        rows = []
        for name in downstream:
            entry: dict[str, Any] = {"path": name}
            sig = s.fst.signals.get(name)
            if sig is None:
                entry["in_waveform"] = False
            else:
                nxt = next((t for t, _ in s.fst._iter_values(
                    sig, t0, s.fst.end_time, 2) if t > t0), None)
                entry["in_waveform"] = True
                entry["first_change_after"] = (
                    timeutil.format_fst_time(nxt, s.fst.timescale_exp)
                    if nxt is not None else None)
            rows.append(entry)
        res["after"] = timeutil.format_fst_time(t0, s.fst.timescale_exp)
        res["downstream"] = rows
        res["note"] = ("reachability is from the netlist; a change after this "
                       "time is correlation, not proven causation")
    return _mark_defaults(res, def_p or def_t)


@_tool()
def fold_transactions(start_cond: Dict[str, Any], end_cond: Dict[str, Any],
                      id_field: Optional[str] = None,
                      fields: Optional[List[str]] = None,
                      start: Optional[str] = None, end: Optional[str] = None,
                      limit: Optional[int] = None,
                      defaults_revision: Optional[int] = None,
                      session_id: Optional[str] = None) -> dict[str, Any]:
    """Fold signal activity into transaction records you define.

    A bus trace is thousands of value changes; the question is usually "which
    transactions happened and what did each carry". You say what a transaction
    is, this returns the records.

    **No protocol library is built in** — no AXI, APB or AHB tables. Describe the
    handshake yourself with the same expression objects ``find_time_windows``
    uses. A built-in protocol table would be wrong for every design that
    deviates from the spec, and wrong silently.

    Conditions are treated as *edges*: a transaction opens when ``start_cond``
    becomes true (not while it stays true) and closes when ``end_cond`` next
    becomes true. A condition already true at the window start opens nothing,
    since its transition was never observed.

    A transaction still open at the window end is reported with
    ``incomplete: true`` and no end time: "we stopped looking" and "it finished
    here" are different facts, so no end is guessed. An end edge with nothing
    open (or, with ``id_field``, nothing open under that id) is reported as
    ``unmatched_end`` rather than dropped. An edge is "false before, true now":
    a condition coming out of x/z opens or closes nothing.
    Time where a condition is undecidable (x/z) claims no boundary and is
    reported separately, so an empty result is not proof nothing happened.

    Args:
        start_cond: expression object that opens a transaction on its rising edge.
        end_cond: expression object that closes one on its rising edge.
        id_field: signal path tagging each transaction, enabling out-of-order
            matching. Without it, ends pair FIFO with the oldest open one.
        fields: extra signal paths to capture at open and at close.
        start: window start, e.g. "100ns"; "min" = start of the dump.
        end: window end; "max" = end of the dump.
        limit: target number of records in the reply.
    """
    s = _sess(session_id)
    if s.fst is None:
        return _no_waveform("fold_transactions")
    for name, node in (("start_cond", start_cond), ("end_cond", end_cond)):
        if not isinstance(node, dict):
            return {"status": "error", "error_type": "invalid_argument",
                    "error": f"{name} must be an expression object",
                    "parameter": name,
                    "hint": 'a signal leaf looks like '
                            '{"k":"sig","name":"top.u.valid"}'}
    start, end, def_w = _resolve_window(s, start, end)
    t0, t1, err = _window_units(s.fst, start, end)
    if err:
        return err
    out = _transactions.fold_transactions(
        s.fst, start_cond, end_cond, id_field=id_field, fields=fields,
        start=t0, end=t1, limit=limit)
    _record("limit", _sampling.resolve_limit(limit))
    if out.get("status") == "error":
        return out
    out["window"] = {"start": timeutil.format_fst_time(t0, s.fst.timescale_exp),
                     "end": timeutil.format_fst_time(t1, s.fst.timescale_exp)}
    return _mark_defaults(out, def_w)


# =============================================================================
# 9. Waveform diff (N-run first-divergence localization)
# =============================================================================
@_tool()
def diff_waveforms(fst_paths: List[str],
                   scope: Optional[str] = None,
                   signals: Optional[List[str]] = None,
                   clock: Optional[str] = None,
                   after: Optional[str] = None) -> dict[str, Any]:
    """Compare two or more FST waveforms and find the first divergence.

    Turns "what changed between these runs" into one deterministic call:
    returns the first divergence time, the earliest-diverging signals (prime
    suspects; later ones are usually downstream contagion) and an honest
    coverage flag. Follow up with signal_fanin/active_drivers on the earliest
    diverger, then open_wave_view with the runs and a marker there.

    With more than two runs each diverging signal also reports ``groups``: the
    run indices partitioned by the value they hold. How the runs split is itself
    the evidence — three agreeing against one points at that run's stimulus,
    while a two-two split points at a configuration difference. A run that has
    no value yet at that point joins no group rather than counting as different.

    Args:
        fst_paths: two or more FST paths. Reply indices follow this order, so
            index 0 is conventionally the passing run.
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
        out = _diff.diff_waveforms(fst_paths, scope=scope,
                                   signals=signals, clock=clock, after=after,
                                   sessions=SESSIONS,
                                   owner_id=PRINCIPAL.owner_id)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        return {"status": "error", "error": str(exc)}
    if out.get("status") == "error":
        return out
    # A diff has no session, so its identity is the runs themselves, in the
    # order given: run 0 is "the reference" by convention, and swapping two
    # runs asks a different question even though the same files are involved.
    paths = [p for p in (fst_paths or []) if p]
    runs = [{"index": i, "wave": file_version(_abspath(p))}
            for i, p in enumerate(paths)]
    effective = {"runs": [r["wave"] for r in runs], "scope": scope,
                 "signals": list(signals) if signals else None,
                 "clock": clock, "after_units": out.get("after_units")}
    out["_query"] = {"mode": out.get("sampling"), "runs": len(paths),
                     **{k: v for k, v in (("scope", scope), ("clock", clock),
                                          ("after", after)) if v}}
    out["_fp"] = {"runs": runs,
                  "query": _request.digest_of("diff_waveforms", effective)}
    return out


# =============================================================================
# 10. Wave viewer (browser-based, surver-streamed; assets optional)
# =============================================================================
def _viewer():
    from .viewer.manager import ViewManager
    return ViewManager.instance()


def _viewer_fail(exc: BaseException) -> dict[str, Any]:
    """Turn any viewer-layer exception into a structured reply.

    Parameter mistakes become ``invalid_argument`` payloads carrying the
    failing parameter and a fix; a missing viewer install degrades to the
    standard unavailable shape; anything else reports as ``internal_error``
    with the exception type kept in the message, so a bug is loud instead of
    surfacing as a bare ``available: false``.
    """
    from .viewer import invalid_argument_payload, unavailable_hint
    from .viewer.state import ViewStateError
    if isinstance(exc, ViewStateError):
        return invalid_argument_payload(exc)
    if isinstance(exc, ImportError):
        return unavailable_hint()
    return {"status": "error", "error_type": "internal_error",
            "error": f"{type(exc).__name__}: {exc}"}


@_tool()
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

    Time fields are strict objects: {"time": <digits>, "unit": "ps"} (unit
    one of s / ms / us / ns / ps / fs, default ps). A suffixed value like
    "1523400ps" is accepted and normalized. Unknown fields, unknown units,
    conflicting suffixes and malformed values are rejected with a structured
    error naming the parameter, never silently ignored or defaulted.

    Args:
        fst_paths: one waveform (normal view) or two (diff view, e.g.
            [pass, fail]); at most two. FST is opened directly; VCD and FSDB
            are converted to FST first and cached, so the same waveform
            converted during analysis is reused here instead of being
            converted again.
        signals: initial signals, each {path, color?, group?, format?, source?}.
            Prefer a short ASCII word for ``group``: the heading is drawn in the
            waveform canvas, whose font has no CJK glyphs, so non-ASCII names
            show as boxes (the grouping itself still works). Spaces are turned
            into underscores automatically. Put prose in ``annotation``, which
            renders any language correctly.
        cursor: {time, unit} to pin the cursor (e.g. the failure time).
        viewport: {from, to, unit} visible time window.
        markers: [{time, unit, label?, color?}] annotations on the timeline.
        diff: diff_waveforms result reference exactly {source_a, source_b,
            first_divergence} — auto-adds a red marker at the divergence.
            first_divergence may be passed back verbatim ("85ns" is accepted
            and normalized).
        annotation: {markdown, confidence?, evidence?} analysis note shown in
            the log popup next to the waveform. Any language: written in your
            own words, rendered as-is.
        labels: display labels per waveform, one per waveform (e.g.
            ["pass", "fail"]).

    Returns available:true plus view_id and url on success, reports an
    ``evicted_view_id`` when the view cap had to close an older view, and
    lists ``warnings`` for anything worth saying: a command that had to be
    dropped, or a requested signal the waveform does not contain (Surfer
    drops such a name silently, so the reply says so; the page shows it
    too). Failures return
    {"status": "error", "error_type": ...}: invalid_argument for a malformed
    parameter, file_not_found / unsupported_format / conversion_failed for
    waveform-file problems, surver_error when the viewer backend cannot
    start, and viewer_unavailable (with available:false) when the viewer
    feature is not installed at all.
    """
    from . import convert as _convert
    if isinstance(fst_paths, str):
        return {"status": "error", "error_type": "invalid_argument",
                "error": "fst_paths must be a list of waveform paths, e.g. "
                         '["sim/fail.fst"]; a bare string would be read as '
                         "separate characters",
                "parameter": "fst_paths"}
    resolved: List[str] = []
    for p in fst_paths:
        try:
            got = _convert.resolve_waveform(p)
        except _convert.UnsupportedWaveformError as exc:
            return {"status": "error", "error_type": "unsupported_format",
                    "error": str(exc),
                    "hint": "convert this file to .fst / .vcd / .fsdb first"}
        except _convert.ConversionError as exc:
            return {"status": "error", "error_type": "conversion_failed",
                    "error": str(exc),
                    "hint": "conversion to FST failed; check the waveform "
                            "file and converter dependencies (vcd2fst / "
                            "fsdb2fst)"}
        except FileNotFoundError as exc:
            return {"status": "error", "error_type": "file_not_found",
                    "error": str(exc),
                    "hint": "check the waveform path"}
        resolved.append(got["fst_path"])

    try:
        return _viewer().open_view(
            resolved, signals=signals, cursor=cursor, viewport=viewport,
            markers=markers, diff=diff,
            annotations=[annotation] if annotation else None,
            labels=labels, owner=PRINCIPAL.owner_id)
    except Exception as exc:  # pylint: disable=broad-except
        return _viewer_fail(exc)


@_tool()
def update_wave_view(view_id: str,
                     signals: Optional[List[dict]] = None,
                     cursor: Optional[dict] = None,
                     viewport: Optional[dict] = None,
                     markers: Optional[List[dict]] = None,
                     annotation: Optional[dict] = None) -> dict[str, Any]:
    """Update an open wave view in place (same URL, no reload for the user).

    Omitted args keep their current value; lists replace entirely except
    annotations, which append to the analysis log popup. Same ``group``,
    ``annotation`` and time-schema conventions as open_wave_view; returns
    ``warnings`` for a dropped command or a requested signal that is not in
    the waveform, and structured errors on bad input.
    """
    try:
        return _viewer().update_view(
            view_id, signals=signals, cursor=cursor, viewport=viewport,
            markers=markers,
            annotations=[annotation] if annotation else None)
    except Exception as exc:  # pylint: disable=broad-except
        return _viewer_fail(exc)


@_tool()
def get_view_state(view_id: str) -> dict[str, Any]:
    """Read the delivery status of an open wave view.

    Returns the actual state (applied revision, page_ready / page_error)
    written back by the browser, plus a desired-state summary.
    ``page_ready`` says whether the page actually reached the streaming
    backend (a blank or unreachable page reports false with the reason in
    ``page_error``). User-interaction readback (cursor position,
    user_dirty) is not populated: the shell no longer reads state out of
    the viewer app, so those fields stay at their defaults.
    """
    try:
        return _viewer().get_state(view_id)
    except Exception as exc:  # pylint: disable=broad-except
        return _viewer_fail(exc)


@_tool()
def list_wave_views() -> dict[str, Any]:
    """List the wave views currently open in this server.

    Returns each view's view_id, url, title, waveform paths, revision and
    whether its streaming backend is still alive, newest first. Use it to
    find a view again after losing track of a view_id, or to check what is
    still holding resources during a long batch run.
    """
    try:
        return _viewer().list_views(owner=PRINCIPAL.owner_id)
    except Exception as exc:  # pylint: disable=broad-except
        return _viewer_fail(exc)


@_tool()
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
            return _viewer().close_all(owner=PRINCIPAL.owner_id)
        if not view_id:
            return {"status": "error", "error_type": "invalid_argument",
                    "error": "provide view_id, or set all_views=true",
                    "parameter": "view_id"}
        return _viewer().close_view(view_id)
    except Exception as exc:  # pylint: disable=broad-except
        return _viewer_fail(exc)


# =============================================================================
# entrypoint
# =============================================================================
def _reap_viewers() -> None:
    """Stop any surver children before exit.

    The viewer is an optional extra, so this must not import it: touching
    ViewManager here would spin up machinery just to tear it down (and raise
    on installs without the assets). Only clean up if a view was ever opened.
    """
    mod = sys.modules.get("wave_mcp.viewer.manager")
    if mod is None:
        return
    try:
        mgr = mod.ViewManager.instance()
        if getattr(mgr, "available", False):
            mgr.close_all()
    except Exception:  # pylint: disable=broad-except
        pass


def _serve_http(host: str, port: int, token: Optional[str]) -> None:
    """Run the streamable-HTTP app, behind the bearer guard when a token is set.

    Same app and same uvicorn setup the SDK's ``run(transport=...)`` uses; the
    only addition is the middleware in front of it, so nothing under this
    port answers without the token.
    """
    import uvicorn
    app = mcp.streamable_http_app(host=host)
    if token is not None:
        app = _auth.BearerTokenMiddleware(app, token)
    uvicorn.run(app, host=host, port=port,
                log_level=mcp.settings.log_level.lower())


def main():
    parser = argparse.ArgumentParser(description="wave-mcp: open-source xrun waveform debug MCP server")
    parser.add_argument(
        "--version",
        action="version",
        version=f"wave-mcp {__version__}",
        help="print the wave-mcp version and exit",
    )
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--session", help="optional session dir/json to auto-open at startup")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    audit_dest = _audit.configure_from_env()
    if audit_dest:
        sys.stderr.write(f"wave-mcp: audit log -> {audit_dest}\n")

    token = None
    if args.transport == "http":
        try:
            token = _auth.configured_token()
        except _auth.TokenConfigError as exc:
            parser.error(str(exc))
        if token is None and not _auth.is_loopback(args.host):
            parser.error(
                f"--host {args.host} is reachable from other machines, so the "
                f"server needs {_auth.TOKEN_ENV}: set it to a random string "
                f"(openssl rand -hex 32) and have every client send "
                f"'Authorization: Bearer <that string>'. Without it only "
                f"--host 127.0.0.1 is allowed.")
        if token is not None:
            sys.stderr.write(f"wave-mcp: HTTP requests must carry "
                             f"Authorization: Bearer <{_auth.TOKEN_ENV}>\n")

    if args.session:
        sid = SESSIONS.open(args.session, owner_id=PRINCIPAL.owner_id)
        # The id is random, so print it: a client that wants to address this
        # session explicitly (rather than relying on "the only one open") has no
        # other way to learn it.
        sys.stderr.write(f"wave-mcp: opened session {sid} from {args.session}\n")
        sys.stderr.flush()

    stop = threading.Event()

    def _ticker():
        # Idle-session sweep. Only sessions with no request in flight go, and
        # the shared resource behind one goes only with its last reference.
        period = min(60.0, max(1.0, LIMITS.idle_session_ttl / 4))
        while not stop.wait(period):
            try:
                closed = SESSIONS.reap_idle()
                if closed:
                    sys.stderr.write(
                        f"wave-mcp: reaped {len(closed)} idle session(s)\n")
            except Exception:  # pylint: disable=broad-except
                pass

    if LIMITS.idle_session_ttl:
        threading.Thread(target=_ticker, name="wave-mcp-reaper",
                         daemon=True).start()

    def _drain():
        # Stop admitting, refuse waiters, give running bodies their grace, then
        # reap our own viewer children. Running C scans cannot be interrupted;
        # they are reported, not killed.
        stop.set()
        try:
            left = EXECUTOR.shutdown()
            if left["still_running"]:
                sys.stderr.write(
                    f"wave-mcp: {left['still_running']} call(s) still running "
                    f"after {LIMITS.shutdown_grace:.0f}s grace\n")
        finally:
            _reap_viewers()

    def _shutdown(signum, _frame):
        # SIGTERM/SIGHUP skip atexit hooks, so surver children would outlive
        # the server (stale processes holding ports). Reap them explicitly.
        try:
            _drain()
        finally:
            sys.exit(128 + signum)

    for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(_sig, _shutdown)
        except (ValueError, AttributeError, OSError):
            pass

    try:
        if args.transport == "stdio":
            mcp.run(transport="stdio")
        else:
            _serve_http(args.host, args.port, token)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        _drain()


if __name__ == "__main__":
    main()
