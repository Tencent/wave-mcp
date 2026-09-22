"""Sessions and the data resources behind them.

Two different things are deliberately kept apart here:

* a **work session** is what a client talks to: one opaque random id with its
  own query defaults and its own lifetime. Two agents, or two conversations,
  each get one even when they are looking at the same design;
* a **dataset resource** is what costs memory: the parsed waveform plus the
  elaborated netlist, described by a ``session.json`` manifest that binds every
  data source together::

      {
        "top": "top_tb",
        "fst_path": "sim/dump.fst",
        "uhdm_db": "netlist/design.uhdm",        # optional
        "maps_path": "netlist/maps.json",         # optional (pyslang netlist)
        "filelist": ["rtl/a.sv", "rtl/b.sv"],     # or "filelist_path"
        "fst_version": "..."                      # file_version at build time
      }

Several work sessions can point at one resource, so a second open of the same
design does not re-parse it, while the two never share defaults and closing one
never pulls the reader out from under the other. A consistency check runs on
open and refuses to silently serve stale data.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from . import timeutil
from .runtime import LOCAL_OWNER, ResourceLease, ResourceRegistry
from .runtime.executor import ResourceLimit
from .runtime.identity import dataset_identity, dataset_version, file_version
from .runtime.manifest import manifest_filelist, manifest_inputs
from .runtime.manifest import manifest_path as _manifest_path
from .runtime.manifest import resolve as _resolve
from .sources.fst_source import FstSource
from .sources.rtl_source import RtlSource


class ConsistencyWarning(Dict[str, Any]):
    pass


class QueryDefaults:
    """Default signals and time window for the queries that follow.

    An agent debugging one failure asks a dozen questions about the same handful
    of signals in the same time window, and restating them on every call is
    where paths get mistyped and windows silently drift apart. These defaults
    hold that context once so later calls can leave it out.

    Strictly a *data-plane* pointer: which signals, which time span. It must
    never hold process state, an intermediate conclusion, or what to do next.
    wave-mcp is a base layer that reports facts about a waveform; storing "the
    current hypothesis" here would make it an agent with opinions, and two
    clients sharing a session would then inherit each other's reasoning.

    Named for what it stores rather than for a tool or a client. Several
    different coding agents call this server, and a name tied to one editor
    describes the caller instead of the data.

    Times are kept in FST units (integers) because that is what the engine
    compares against; the formatted spelling is derived only when reporting.

    Concurrency: an update publishes one complete snapshot while holding
    ``_lock``, so a rejected or superseded update can never leave the fields
    half applied. ``revision`` advances on every successful publish, including
    one that changes nothing, which is what makes "the revision is unchanged"
    a reliable statement that nobody else wrote in between.
    """

    __slots__ = ("_lock", "_paths", "_start", "_end", "_revision")

    #: Fields a caller may treat as the data-plane coordinates.
    FIELDS = ("paths", "start", "end")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._paths: Optional[List[str]] = None
        self._start: Optional[int] = None
        self._end: Optional[int] = None
        self._revision = 0

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    @property
    def is_set(self) -> bool:
        """True when these defaults would supply anything at all."""
        with self._lock:
            return (bool(self._paths) or self._start is not None
                    or self._end is not None)

    def read(self) -> Dict[str, Any]:
        """A copy of the current state; the list is copied, never aliased."""
        with self._lock:
            return {"paths": list(self._paths) if self._paths else [],
                    "start": self._start, "end": self._end,
                    "revision": self._revision}

    def replace(self, paths: Optional[List[str]], start: Optional[int],
                end: Optional[int],
                defaults_revision: Optional[int] = None) -> bool:
        """Atomically publish a complete new state.

        The caller passes the *whole* candidate state, already validated, so a
        partial write cannot happen by construction. Returns False and changes
        nothing when ``defaults_revision`` does not match the current revision,
        which is how a caller detects that someone else published first.
        """
        with self._lock:
            if (defaults_revision is not None
                    and defaults_revision != self._revision):
                return False
            self._paths = list(paths) if paths else None
            self._start = start
            self._end = end
            self._revision += 1
            return True

    def clear(self, defaults_revision: Optional[int] = None) -> bool:
        """Drop everything. Same atomicity and revision rule as ``replace``."""
        return self.replace([], None, None, defaults_revision)

    def as_dict(self, timescale_exp: Optional[int] = None) -> Dict[str, Any]:
        """Report the defaults, adding formatted times when a timescale is known."""
        state = self.read()
        out: Dict[str, Any] = {
            "paths": state["paths"], "start": state["start"],
            "end": state["end"], "revision": state["revision"],
            "is_set": bool(state["paths"]) or state["start"] is not None
                      or state["end"] is not None,
        }
        if timescale_exp is not None:
            out["start_time"] = (timeutil.format_fst_time(state["start"], timescale_exp)
                                 if state["start"] is not None else None)
            out["end_time"] = (timeutil.format_fst_time(state["end"], timescale_exp)
                               if state["end"] is not None else None)
        return out


class SessionError(Exception):
    """A session failure that a tool renders as a structured reply.

    Raised in the manager (or in the tool-side session lookup) and converted at
    the tool boundary, so every session-scoped tool reports a missing, ambiguous
    or stale session the same way instead of leaking a traceback with different
    wording in each one.
    """

    error_type = "session_error"

    def __init__(self, message: str, hint: str = "", **extra: Any) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.extra = extra

    def payload(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"status": "error",
                               "error_type": self.error_type,
                               "error": self.message}
        if self.hint:
            out["hint"] = self.hint
        out.update(self.extra)
        return out


class NoActiveSession(SessionError):
    """Nothing to answer from: the owner has no session open."""

    error_type = "no_active_session"


class AmbiguousSession(SessionError):
    """Several sessions are open and the call did not name one."""

    error_type = "ambiguous_session"


class SessionNotFound(SessionError):
    """No such session for this owner (whether it never existed or is not ours)."""

    error_type = "session_not_found"


class SessionInputMismatch(SessionError):
    """A resume named inputs that differ from what the session was built from."""

    error_type = "session_input_mismatch"


class InputChanged(SessionError):
    """The files behind a session changed on disk since it was opened."""

    error_type = "input_changed"


def _manifest_signature(session_path: str) -> str:
    """What ``open_session`` was asked for: the manifest it named."""
    return "open:" + os.path.abspath(_manifest_path(session_path))


def _read_manifest(session_path: str) -> Tuple[bytes, Dict[str, Any], str]:
    """(raw bytes, parsed manifest, base dir) of the manifest a path names."""
    path = _manifest_path(session_path)
    with open(path, "rb") as fh:
        raw = fh.read()
    return raw, json.loads(raw), os.path.dirname(os.path.abspath(path))


def _identify(session_path: str) -> Tuple[str, str]:
    """(dataset identity, dataset version) of the data a manifest binds."""
    path = _manifest_path(session_path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"session manifest not found: {path}")
    return input_identity(session_path), input_version(session_path)


def input_identity(session_path: str) -> str:
    """``dataset_identity`` of the manifest at ``session_path``."""
    _raw, manifest, base = _read_manifest(session_path)
    return dataset_identity(manifest, base)


def input_version(session_path: str) -> str:
    """``dataset_version`` of the manifest at ``session_path``; "" if unreadable."""
    try:
        raw, manifest, base = _read_manifest(session_path)
    except (OSError, ValueError):
        return ""
    return dataset_version(raw, manifest, base)


#: Registry key of a loaded resource: who may see it, which design, which revision.
ResourceKey = Tuple[str, str, str]


class DatasetResource:
    """One loaded set of inputs: the waveform reader plus the netlist.

    This is the expensive object, and it is defined by what it was built from
    rather than by who asked for it, which is what lets two work sessions share
    one instance. Lifetime is owned by the registry, never by a session.
    """

    def __init__(self, manifest: Dict[str, Any], base_dir: str,
                 manifest_path: Optional[str] = None):
        self.base_dir = base_dir
        self.manifest = manifest
        self.manifest_path = manifest_path
        self.top: str = manifest.get("top", "")
        inputs = manifest_inputs(base_dir, manifest)
        self.fst_path = inputs["fst"]
        self.uhdm_db = inputs["uhdm"]
        self.maps_path = inputs["maps"]
        self.warnings: List[str] = []
        self._fp: Optional[Dict[str, str]] = None

        # filelist may be inline or in a file ("filelist_path")
        self.filelist = manifest_filelist(base_dir, manifest)

        # --- open sources ---
        # FST is optional: a *static session* (netlist-only, no waveform) opens
        # with fst=None; waveform-dependent tools degrade gracefully while all
        # structural tools (connectivity/drivers/loads/fanin/files) work.
        if self.fst_path:
            if not os.path.exists(self.fst_path):
                raise FileNotFoundError(f"FST not found: {self.fst_path}")
            self.fst = FstSource(self.fst_path)
        else:
            self.fst = None
        self.rtl = RtlSource(self.filelist, self.maps_path, fst=self.fst)

        # Resolve each FST scope's module *definition* name so module_type reports
        # the real module (e.g. "decode") instead of the generic scope kind
        # ("module"). FST/VCD carries only the *instance* name, so we layer three
        # sources by confidence, each filling what the higher layer left empty:
        #   L1 netlist  : pyslang elaboration (accurate; may be partial)
        #   L2 inferred : instance-name -> module-def naming-convention match
        #                 (netlist-independent; works even when elaboration fails)
        #   L3 manual   : session.json "scope_map" override (authoritative)
        if self.fst is not None and self.rtl.has_netlist and getattr(self.rtl, "engine", None):
            try:
                # batch resolve so anchor propagation ("向上推导") can recover the
                # DUT-root scope from its already-matched children via the netlist.
                netmap = self.rtl.engine.resolve_definitions(
                    list(self.fst.scopes.keys()))
                self.fst.apply_definition_map(netmap, source="netlist")
            except Exception:  # pylint: disable=broad-except
                pass  # best-effort L1: never let definition resolution fail open
            # let FST bus-aggregation validate merged widths against RTL decls
            self.fst.width_hint = self.rtl.signal_width

        # L2: naming-convention inference over the scopes L1 didn't resolve. The
        # known module-def names come from the netlist (if any) plus a cheap
        # regex scan of the source files, so this fills gaps even when the
        # netlist is missing/partial. Only module-kind scopes; never overrides L1.
        # Run in two confidence tiers so weak (heuristic) matches are separable:
        #   inferred        : exact / strip-prefix equality against real names
        #   inferred_prefix : longest boundary-prefix heuristic (irregular names)
        try:
            from .netlist.name_infer import extract_module_names, make_name_resolver
            known = set(extract_module_names(self.filelist))
            if self.rtl.has_netlist:
                known |= set((self.rtl.maps.get("modules") or {}).keys())
            if known and self.fst is not None:
                noise = {"begin", "fork", "clocking"}
                self.fst.annotate_definitions(
                    make_name_resolver(known, allow_prefix=False),
                    source="inferred", only_empty=True, skip_kinds=noise)
                self.fst.annotate_definitions(
                    make_name_resolver(known, allow_prefix=True),
                    source="inferred_prefix", only_empty=True, skip_kinds=noise)
        except Exception:  # pylint: disable=broad-except
            pass  # best-effort L2: name inference is optional, never crash open

        # L3: manual authoritative override from the manifest.
        try:
            if self.fst is not None:
                self.fst.apply_scope_map(manifest.get("scope_map") or {})
        except Exception:  # pylint: disable=broad-except
            pass  # best-effort L3: manual override is optional, never crash open

        self._check_consistency()

    def _check_consistency(self):
        # ``fst_version`` is a ``file_version``; manifests written before S4
        # carry a whole-file ``fst_hash`` instead, which is simply not checked.
        recorded = self.manifest.get("fst_version")
        if recorded and self.fst_path:
            actual = file_version(self.fst_path)
            if actual and actual != recorded:
                self.warnings.append(
                    "FST fingerprint mismatch: waveform was re-dumped after the "
                    "session manifest was built. Rebuild the session to avoid stale data.")
        # netlist vs source staleness (stage 4)
        if self.rtl.has_netlist and self.filelist:
            newest_src = max((os.path.getmtime(f) for f in self.filelist
                              if os.path.exists(f)), default=0)
            if self.maps_path and os.path.exists(self.maps_path):
                if newest_src > os.path.getmtime(self.maps_path):
                    self.warnings.append(
                        "RTL source newer than netlist maps: connectivity/trace "
                        "results may be stale. Rebuild the netlist.")

    def cache_status(self) -> List[Dict[str, Any]]:
        """The derived caches this session reads, each with ``valid``: whether
        the file is still the version the manifest was built against."""
        out: List[Dict[str, Any]] = []
        for rec in self.manifest.get("caches") or []:
            path = rec.get("path")
            entry = dict(rec)
            entry["valid"] = bool(path) and file_version(path) == rec.get("version")
            out.append(entry)
        return out

    def input_versions(self) -> Dict[str, str]:
        """``file_version`` of each primary input, "" where absent.

        Cached for the resource lifetime: the inputs cannot change under a
        loaded resource (any edit changes the dataset version and forces a
        reload), so re-stat'ing them on every query would be pure overhead.
        """
        if self._fp is None:
            self._fp = {"wave": file_version(self.fst_path),
                        "netlist": file_version(self.maps_path)}
        return dict(self._fp)

    # -- info ---------------------------------------------------------------
    def summary(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "top": self.top,
            # tell LLM clients up front which tool families apply, so a static
            # session doesn't need trial-and-error against value/trace tools.
            "mode": "full" if self.fst is not None else "static",
            "fst_path": self.fst_path,
            "netlist_available": self.rtl.has_netlist,
            "netlist_health": self.rtl.netlist_health(),
            "verible_available": self.rtl.verible,
            "warnings": self.warnings,
        }
        caches = self.cache_status()
        if caches:
            out["caches"] = caches
        if self.fst is not None:
            out.update({
                "timescale_exp": self.fst.timescale_exp,
                "start_time": self.fst.start_time,
                "end_time": self.fst.end_time,
                "num_scopes": len(self.fst.scopes),
                "num_signals": len(self.fst.signals),
                # how many module scopes have a resolved definition_name (and via
                # which source: netlist / inferred / manual). Tells the client
                # whether module_type is trustworthy across the hierarchy.
                "definition_coverage": self.fst.definition_coverage(),
            })
        else:
            mods = self.rtl.maps.get("modules", {}) if self.rtl.has_netlist else {}
            tree = self.rtl.maps.get("instance_tree", {}) if self.rtl.has_netlist else {}
            out.update({
                "num_modules": len(mods),
                "num_instances": len(tree),
                "available_tools": [
                    "signal_connectivity", "signal_drivers", "signal_loads",
                    "signal_fanin", "driver_contributors", "list_modules",
                    "find_instances", "files", "scope_info", "signal_info"],
                "unavailable_tools_hint":
                    "value/trace tools (signal_values, signal_activity, "
                    "find_time_windows, trace_*, active_drivers) "
                    "need a waveform; use prepare_session once your sim has "
                    "dumped an FST/VCD.",
            })
        return out

    def close(self):
        if self.fst is not None:
            self.fst.close()


def open_session(session_path: str) -> DatasetResource:
    """Load the data a session manifest describes.

    Returns the heavy object itself rather than a work session: the manager wraps
    this, and offline callers (tests, the field kit, scripts that just want the
    readers) can use it directly without a server-side session.
    """
    manifest_path = _manifest_path(session_path)
    if not os.path.exists(manifest_path):
        raise FileNotFoundError(f"session manifest not found: {manifest_path}")
    with open(manifest_path) as fh:
        manifest = json.load(fh)
    return DatasetResource(manifest,
                           os.path.dirname(os.path.abspath(manifest_path)),
                           manifest_path=os.path.abspath(manifest_path))


class FstBorrow:
    """A borrowed waveform reader plus the lease that keeps it alive."""

    __slots__ = ("fst", "_lease")

    def __init__(self, fst: FstSource, lease: ResourceLease) -> None:
        self.fst = fst
        self._lease = lease

    def release(self) -> bool:
        return self._lease.release()


class WorkSession:
    """One client-visible session, backed by a possibly shared resource.

    The id is random and opaque, never derived from a path: knowing where the
    data lives must not be enough to address somebody's session. Everything a
    client accumulates hangs off this object (its query defaults, its own
    lifetime), while the expensive readers hang off the resource.
    """

    __slots__ = ("session_id", "owner_id", "resource", "resource_key",
                 "input_identity", "input_version", "request_signature",
                 "query_defaults", "created_at", "last_used", "last_used_mono",
                 "closed", "resource_reused", "in_flight", "_lock")

    def __init__(self, session_id: str, owner_id: str,
                 resource: DatasetResource, resource_key: ResourceKey,
                 identity: str, version: str, signature: str) -> None:
        self.session_id = session_id
        self.owner_id = owner_id
        self.resource = resource
        self.resource_key = resource_key
        self.input_identity = identity
        self.input_version = version
        self.request_signature = signature
        self.query_defaults = QueryDefaults()
        self.created_at = time.time()
        self.last_used = self.created_at
        # Idle reaping compares against a monotonic clock: a wall-clock jump
        # must not make a busy session look idle (or an idle one look busy).
        self.last_used_mono = time.monotonic()
        self.closed = False
        self.resource_reused = False
        self.in_flight = 0
        self._lock = threading.RLock()

    # -- the resource, delegated (tools read through the session) ------------
    @property
    def fst(self):
        return self.resource.fst

    @property
    def rtl(self):
        return self.resource.rtl

    @property
    def manifest(self) -> Dict[str, Any]:
        return self.resource.manifest

    @property
    def manifest_path(self) -> Optional[str]:
        return self.resource.manifest_path

    @property
    def top(self) -> str:
        return self.resource.top

    @property
    def filelist(self) -> List[str]:
        return self.resource.filelist

    @property
    def warnings(self) -> List[str]:
        return self.resource.warnings

    @property
    def resource_id(self) -> str:
        """Short label for the loaded data, for logs and replies: its version."""
        return self.input_version

    def touch(self) -> None:
        self.last_used = time.time()
        self.last_used_mono = time.monotonic()

    def mark_closed(self) -> None:
        self.closed = True

    def attach(self, resource: DatasetResource, resource_key: ResourceKey,
               identity: str, version: str, reused: bool) -> None:
        """Re-point this session at freshly loaded data, keeping its defaults.

        Used when a caller re-prepares a session and the data changed underneath
        it: the id and the query defaults survive, and the answers come from the
        new data. The caller releases the previous reference afterwards.
        """
        with self._lock:
            self.resource = resource
            self.resource_key = resource_key
            self.input_identity = identity
            self.input_version = version
            self.resource_reused = reused
            self.touch()

    def fingerprint(self) -> Dict[str, Any]:
        """``_fp.dataset`` for replies: which design, which revision, and the
        version of each primary input so a static session reads as ``wave == ""``."""
        return {"identity": self.input_identity, "version": self.input_version,
                **self.resource.input_versions()}

    def summary(self) -> Dict[str, Any]:
        out = dict(self.resource.summary())
        out["session_id"] = self.session_id
        out["resource_id"] = self.resource_id
        out["resource_shared"] = self.resource_reused
        out["dataset"] = {"identity": self.input_identity,
                          "version": self.input_version}
        out["query_defaults"] = self.query_defaults.as_dict(
            out.get("timescale_exp"))
        return out

    def describe(self) -> Dict[str, Any]:
        """A listing entry: identity and state, without the data itself."""
        return {"session_id": self.session_id,
                "session_path": self.manifest_path,
                "mode": "full" if self.resource.fst is not None else "static",
                "resource_id": self.resource_id,
                "created_at": self.created_at,
                "last_used": self.last_used,
                "in_flight": self.in_flight,
                "query_defaults": {"is_set": self.query_defaults.is_set,
                                   "revision": self.query_defaults.revision}}


class SessionManager:
    """Work sessions, and the dataset resources they share.

    ``open()`` without an id always creates a *new* work session: a fresh opaque
    id, empty query defaults, and a reference to whatever resource those inputs
    identify (which is loaded only if this is the first session to ask for it).
    Passing an id resumes that session instead, refusing when the caller's inputs
    no longer match what the session was built from: a resumption continues the
    same analysis, it never swaps the data underneath it or takes over somebody
    else's session.
    """

    def __init__(self, owner_id: str = LOCAL_OWNER, *,
                 per_owner_sessions: int = 0,
                 idle_ttl: float = 0.0) -> None:
        self._owner_id = owner_id
        self._sessions: Dict[str, WorkSession] = {}
        self._resources = ResourceRegistry()
        self._lock = threading.RLock()
        #: Max open sessions per owner; 0 means unlimited.
        self.per_owner_sessions = per_owner_sessions
        #: Seconds a session may sit unused before ``reap_idle`` closes it; 0 disables.
        self.idle_ttl = idle_ttl
        self._reaped = 0

    # -- opening / resuming -------------------------------------------------
    def open(self, session_path: str, session_id: Optional[str] = None, *,
             signature: Optional[str] = None, refresh: bool = False,
             owner_id: Optional[str] = None) -> str:
        """Create a work session, or resume ``session_id``. Returns its id.

        ``signature`` is what the caller asked for (a manifest path for
        ``open_session``, the resolved arguments for ``prepare_session``), so a
        resume can tell "the same request" from "a different one" before anything
        is written. ``refresh=True`` lets a re-prepare re-point the session at the
        data it just rebuilt instead of refusing with ``input_changed``.
        """
        owner = owner_id or self._owner_id
        sig = signature or _manifest_signature(session_path)
        if session_id:
            return self._resume(session_id, session_path, owner, sig, refresh)
        return self._create(session_path, owner, sig)

    def precheck(self, session_id: Optional[str], signature: str,
                 owner_id: Optional[str] = None) -> None:
        """Refuse a resume before the caller writes anything.

        ``prepare_session`` rewrites the session manifest, so a caller who named
        a session must learn that its inputs differ *before* that write, not
        after the previous manifest has been overwritten.
        """
        if not session_id:
            return
        self._lookup(session_id, owner_id or self._owner_id, signature)

    def _create(self, session_path: str, owner: str, signature: str) -> str:
        # The quota is checked before the load, which is the expensive part: an
        # owner at the limit must not cost the server a parse it then discards.
        self._check_quota(owner)
        identity, version = _identify(session_path)
        resource, key, reused = self._load(owner, session_path, identity, version)
        ws = WorkSession(uuid.uuid4().hex[:16], owner, resource, key,
                         identity, version, signature)
        ws.resource_reused = reused
        with self._lock:
            self._sessions[ws.session_id] = ws
        return ws.session_id

    def _resume(self, session_id: str, session_path: str, owner: str,
                signature: str, refresh: bool) -> str:
        ws = self._lookup(session_id, owner, signature)
        identity, version = _identify(session_path)
        if identity != ws.input_identity or version != ws.input_version:
            if not refresh:
                raise InputChanged(
                    f"the inputs of session {session_id} changed on disk",
                    hint="omit session_id to open a new session on the new data, "
                         "or re-prepare it to pick the change up")
            resource, key, reused = self._load(owner, session_path, identity,
                                               version)
            old_resource, old_key = ws.resource, ws.resource_key
            ws.attach(resource, key, identity, version, reused)
            # Drop the previous reference only after the new one is in place, so
            # the session is never briefly pointing at nothing.
            self._resources.release(old_key, old_resource)
        ws.touch()
        return session_id

    def _lookup(self, session_id: str, owner: str,
                signature: Optional[str] = None) -> WorkSession:
        with self._lock:
            ws = self._sessions.get(session_id)
        if ws is None or ws.owner_id != owner or ws.closed:
            # An id belonging to somebody else is reported exactly like one that
            # never existed: existence is not something a caller may probe for.
            raise SessionNotFound(
                f"no active session with id {session_id!r}",
                hint="omit session_id to start a new session, or list yours "
                     "with session_info(list_sessions=True)")
        if signature is not None and ws.request_signature != signature:
            raise SessionInputMismatch(
                f"session {session_id} was opened for different inputs",
                hint="a session can only be resumed with the inputs it was built "
                     "from; omit session_id to start a fresh session")
        return ws

    def _load(self, owner: str, session_path: str, identity: str,
              version: str) -> Tuple[DatasetResource, ResourceKey, bool]:
        key: ResourceKey = (owner, identity, version)

        def loader() -> DatasetResource:
            # The inputs must not move while they are being read: a re-dump racing
            # with the load would otherwise be published under the previous digest
            # and then reported as that older revision for the session's whole
            # lifetime.
            if input_version(session_path) != version:
                raise InputChanged(
                    "the inputs changed while they were being read",
                    hint="retry: the load will pick up the new version")
            resource = open_session(session_path)
            if input_version(session_path) != version:
                resource.close()
                raise InputChanged(
                    "the inputs changed while they were being read",
                    hint="retry: the load will pick up the new version")
            return resource

        resource = self._resources.acquire(key, loader)
        reused = self._resources.refcount(key) > 1
        return resource, key, reused

    def _check_quota(self, owner: str) -> None:
        limit = self.per_owner_sessions
        if not limit:
            return
        with self._lock:
            mine = sum(1 for w in self._sessions.values()
                       if w.owner_id == owner and not w.closed)
        if mine >= limit:
            raise ResourceLimit(
                f"this owner already has {mine} open sessions (limit {limit})",
                hint="close_session on one you no longer need, or list them "
                     "with session_info(list_sessions=True)",
                open_sessions=mine, limit=limit)

    # -- in-flight tracking and idle reaping ----------------------------------
    def begin_call(self, ws: WorkSession) -> None:
        """Mark a request as running against ``ws`` so the reaper skips it."""
        with self._lock:
            ws.in_flight += 1

    def end_call(self, ws: WorkSession) -> None:
        with self._lock:
            ws.in_flight = max(0, ws.in_flight - 1)
            ws.touch()

    def reap_idle(self, now_mono: Optional[float] = None) -> List[str]:
        """Close sessions idle longer than ``idle_ttl`` with no request in flight.

        Returns the ids closed. Only *sessions* are reaped; the underlying
        resource goes only when its last reference does, so a resource another
        session or a view still uses survives. Safe to call from any thread;
        the server calls it from a background ticker.
        """
        if not self.idle_ttl:
            return []
        now = time.monotonic() if now_mono is None else now_mono
        with self._lock:
            victims = [w for w in self._sessions.values()
                       if not w.closed and w.in_flight == 0
                       and now - w.last_used_mono >= self.idle_ttl]
        closed: List[str] = []
        for ws in victims:
            with self._lock:
                cur = self._sessions.get(ws.session_id)
                # Re-check under the lock: a call may have started meanwhile.
                if cur is not ws or ws.in_flight or ws.closed:
                    continue
                del self._sessions[ws.session_id]
                self._reaped += 1
            ws.mark_closed()
            self._resources.release(ws.resource_key, ws.resource)
            closed.append(ws.session_id)
        return closed

    # -- lookup -------------------------------------------------------------
    def get(self, session_id: Optional[str] = None,
            owner_id: Optional[str] = None) -> WorkSession:
        """The session a call runs against.

        Omitting the id is allowed only when the owner has exactly one session
        open. With none there is nothing to answer from, and with several the
        choice would be a guess that answers about the wrong data while looking
        perfectly authoritative.
        """
        owner = owner_id or self._owner_id
        if session_id:
            return self._lookup(session_id, owner)
        with self._lock:
            mine = [w for w in self._sessions.values()
                    if w.owner_id == owner and not w.closed]
        if not mine:
            raise NoActiveSession(
                "no session is open",
                hint="call open_session / prepare_session first")
        if len(mine) > 1:
            raise AmbiguousSession(
                f"{len(mine)} sessions are open, so session_id is required",
                hint="pass session_id, or list yours with "
                     "session_info(list_sessions=True)")
        return mine[0]

    def peek(self, session_id: Optional[str] = None,
             owner_id: Optional[str] = None) -> Optional[WorkSession]:
        """``get`` without the failure: None when no single session resolves."""
        try:
            return self.get(session_id, owner_id)
        except SessionError:
            return None

    def lease(self, session_id: Optional[str] = None,
              owner_id: Optional[str] = None) -> Optional[ResourceLease]:
        """Keep the session's resource alive for the duration of one call.

        The lease is what makes a concurrent ``close`` safe: the resource is
        destroyed when its last reference goes, so an in-flight scan finishes on
        the reader it started with.
        """
        ws = self.peek(session_id, owner_id)
        if ws is None:
            return None
        return self._resources.retain(ws.resource_key, ws.resource)

    def borrow_fst(self, path: str,
                   owner_id: Optional[str] = None) -> Optional[FstBorrow]:
        """Lend an already-loaded waveform reader, under a lease.

        Only this owner's sessions are considered: a diff must not reach into
        somebody else's data to save a parse. None means "not open here", which
        tells the caller to open the file itself.
        """
        target = os.path.abspath(path)
        owner = owner_id or self._owner_id
        with self._lock:
            candidates = [w for w in self._sessions.values()
                          if w.owner_id == owner and not w.closed]
        for ws in candidates:
            fst = ws.resource.fst
            reader_path = getattr(fst, "path", None) if fst is not None else None
            if not reader_path or os.path.abspath(reader_path) != target:
                continue
            lease = self._resources.retain(ws.resource_key, ws.resource)
            if lease is not None:
                return FstBorrow(fst, lease)
        return None

    # -- closing ------------------------------------------------------------
    def close(self, session_id: Optional[str] = None,
              owner_id: Optional[str] = None) -> bool:
        """Drop this session's reference to its resource.

        The data survives while any other session or in-flight request holds a
        reference, so closing one session cannot disturb another. An unknown id,
        an id belonging to somebody else and an already-closed id all report
        False: the reply is not a way to probe for sessions.
        """
        owner = owner_id or self._owner_id
        if session_id:
            with self._lock:
                ws = self._sessions.get(session_id)
                # Ownership is checked *before* anything is removed: a refusal
                # must not be a way to delete somebody else's session.
                if ws is None or ws.owner_id != owner:
                    return False
                del self._sessions[session_id]
        else:
            ws = self.get(None, owner)       # raises when none / ambiguous
            with self._lock:
                if self._sessions.pop(ws.session_id, None) is None:
                    return False             # somebody closed it first
        ws.mark_closed()
        self._resources.release(ws.resource_key, ws.resource)
        return True

    # -- listing ------------------------------------------------------------
    def list_sessions(self, owner_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """This owner's open sessions, oldest first. Never anybody else's."""
        owner = owner_id or self._owner_id
        with self._lock:
            mine = [w for w in self._sessions.values()
                    if w.owner_id == owner and not w.closed]
        return [w.describe() for w in sorted(mine, key=lambda w: w.created_at)]

    def stats(self) -> Dict[str, Any]:
        """Counters for tests and diagnostics; not a client-facing contract."""
        with self._lock:
            sessions = len(self._sessions)
            in_flight = sum(w.in_flight for w in self._sessions.values())
        return {"sessions": sessions, "in_flight": in_flight,
                "reaped": self._reaped, "resources": self._resources.stats()}
