"""Session model — aggregates the 4 data sources behind one manifest.

A *session* is one isolated debug context (one module / one user), described by a
``session.json`` manifest that binds every data source together::

    {
      "top": "top_tb",
      "fst_path": "sim/dump.fst",
      "log_path": "sim/xrun.log",
      "uhdm_db": "netlist/design.uhdm",        # optional (stage 4)
      "maps_path": "netlist/maps.json",         # optional (stage 4)
      "filelist": ["rtl/a.sv", "rtl/b.sv"],     # or "filelist_path"
      "wcp": {"host": "127.0.0.1", "port": 54321},
      "fst_hash": "...", "filelist_hash": "..."  # consistency fingerprints
    }

``open_session(path)`` loads everything at once, mirroring Indago ``launch`` —
the user only ever sees one handle. Per the requirements, a consistency check
runs on open and refuses to silently serve stale data.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Optional

from .sources.assertion_source import AssertionSource
from .sources.coverage_source import CoverageSource
from .sources.fst_source import FstSource
from .sources.log_source import LogSource
from .sources.rtl_source import RtlSource
from .sources.wcp_client import WcpClient


def _sha1_file(path: str, limit: int = 0) -> Optional[str]:
    if not path or not os.path.exists(path):
        return None
    h = hashlib.sha1()
    read = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
            read += len(chunk)
            if limit and read >= limit:
                break
    return h.hexdigest()


def _resolve(base: str, path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(base, path))


class ConsistencyWarning(Dict[str, Any]):
    pass


class Session:
    def __init__(self, manifest: Dict[str, Any], base_dir: str):
        self.base_dir = base_dir
        self.manifest = manifest
        self.top: str = manifest.get("top", "")
        self.fst_path = _resolve(base_dir, manifest.get("fst_path"))
        self.log_path = _resolve(base_dir, manifest.get("log_path"))
        self.uhdm_db = _resolve(base_dir, manifest.get("uhdm_db"))
        self.maps_path = _resolve(base_dir, manifest.get("maps_path"))
        self.warnings: List[str] = []

        # filelist may be inline or in a file
        filelist = manifest.get("filelist")
        if not filelist and manifest.get("filelist_path"):
            filelist = self._read_filelist(_resolve(base_dir, manifest["filelist_path"]))
        self.filelist = [
            _resolve(base_dir, f) for f in (filelist or [])
        ]

        # --- open sources ---
        if not self.fst_path or not os.path.exists(self.fst_path):
            raise FileNotFoundError(f"FST not found: {self.fst_path}")
        self.fst = FstSource(self.fst_path)
        self.log = LogSource(self.log_path) if self.log_path else LogSource("")
        self.rtl = RtlSource(self.filelist, self.maps_path, fst=self.fst)

        # Resolve each FST scope's module *definition* name so module_type reports
        # the real module (e.g. "decode") instead of the generic scope kind
        # ("module"). FST/VCD carries only the *instance* name, so we layer three
        # sources by confidence, each filling what the higher layer left empty:
        #   L1 netlist  : pyslang elaboration (accurate; may be partial)
        #   L2 inferred : instance-name -> module-def naming-convention match
        #                 (netlist-independent; works even when elaboration fails)
        #   L3 manual   : session.json "scope_map" override (authoritative)
        if self.rtl.has_netlist and getattr(self.rtl, "engine", None):
            try:
                # batch resolve so anchor propagation ("向上推导") can recover the
                # DUT-root scope from its already-matched children via the netlist.
                netmap = self.rtl.engine.resolve_definitions(
                    list(self.fst.scopes.keys()))
                self.fst.apply_definition_map(netmap, source="netlist")
            except Exception:
                pass
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
            if known:
                noise = {"begin", "fork", "clocking"}
                self.fst.annotate_definitions(
                    make_name_resolver(known, allow_prefix=False),
                    source="inferred", only_empty=True, skip_kinds=noise)
                self.fst.annotate_definitions(
                    make_name_resolver(known, allow_prefix=True),
                    source="inferred_prefix", only_empty=True, skip_kinds=noise)
        except Exception:
            pass

        # L3: manual authoritative override from the manifest.
        try:
            self.fst.apply_scope_map(manifest.get("scope_map") or {})
        except Exception:
            pass

        # coverage / assertion sources (optional; auto-discovered if not given)
        cov_report = _resolve(base_dir, manifest.get("coverage_report"))
        cov_csv = _resolve(base_dir, manifest.get("coverage_csv"))
        if not cov_report and not cov_csv:
            cov_report, cov_csv = self._discover_coverage()
        self.coverage = CoverageSource(cov_report, cov_csv)
        # assertions: reuse the sim log for failures + the coverage csv for status
        asrt_csv = _resolve(base_dir, manifest.get("assertion_csv")) or cov_csv
        self.assertions = AssertionSource(self.log_path, asrt_csv)
        wcp_cfg = manifest.get("wcp") or {}
        self.wcp = WcpClient(wcp_cfg.get("host", "127.0.0.1"),
                             int(wcp_cfg.get("port", 54321)))

        self._check_consistency()

    def _discover_coverage(self):
        """Best-effort locate a urg text report and all_bins.csv near the session.

        Searches the session dir and its ancestors (up to 3 levels) plus a
        sibling ``cov/`` directory, matching common urg output names. Returns
        ``(report_path, csv_path)`` with either possibly None.
        """
        report = csv_path = None
        seeds = [self.base_dir]
        if self.log_path:
            seeds.append(os.path.dirname(self.log_path))
        if self.fst_path:
            seeds.append(os.path.dirname(self.fst_path))
        seen = set()
        cand_dirs: List[str] = []
        for s in seeds:
            d = s
            for _ in range(4):
                if d and d not in seen:
                    seen.add(d)
                    cand_dirs.append(d)
                    cand_dirs.append(os.path.join(d, "cov"))
                    cand_dirs.append(os.path.join(d, "cov_report"))
                nd = os.path.dirname(d)
                if nd == d:
                    break
                d = nd
        for d in cand_dirs:
            if not os.path.isdir(d):
                continue
            if report is None:
                for nm in ("cov_report.txt", "urg_report.txt", "coverage.txt"):
                    p = os.path.join(d, nm)
                    if os.path.exists(p):
                        report = p
                        break
            if csv_path is None:
                for nm in ("all_bins.csv", "cov.csv"):
                    p = os.path.join(d, nm)
                    if os.path.exists(p):
                        csv_path = p
                        break
            if report and csv_path:
                break
        return report, csv_path

    @staticmethod
    def _read_filelist(path: Optional[str]) -> List[str]:
        if not path or not os.path.exists(path):
            return []
        base = os.path.dirname(path)
        out: List[str] = []
        with open(path) as fh:
            for line in fh:
                s = line.strip()
                if not s or s.startswith(("#", "//", "-")):
                    continue
                out.append(_resolve(base, s))
        return out

    def _check_consistency(self):
        recorded = self.manifest.get("fst_hash")
        if recorded:
            actual = _sha1_file(self.fst_path, limit=1 << 20)
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

    # -- info ---------------------------------------------------------------
    def summary(self) -> Dict[str, Any]:
        return {
            "top": self.top,
            "fst_path": self.fst_path,
            "log_path": self.log_path,
            "timescale_exp": self.fst.timescale_exp,
            "start_time": self.fst.start_time,
            "end_time": self.fst.end_time,
            "num_scopes": len(self.fst.scopes),
            "num_signals": len(self.fst.signals),
            "num_log_messages": len(self.log.messages),
            "netlist_available": self.rtl.has_netlist,
            "netlist_health": self.rtl.netlist_health(),
            # how many module scopes have a resolved definition_name (and via
            # which source: netlist / inferred / manual). Tells the client
            # whether module_type is trustworthy across the hierarchy.
            "definition_coverage": self.fst.definition_coverage(),
            "verible_available": self.rtl.verible,
            "coverage_available": self.coverage.available,
            "assertions_available": self.assertions.available,
            "warnings": self.warnings,
        }

    def close(self):
        try:
            self.fst.close()
        finally:
            self.wcp.disconnect()


def open_session(session_path: str) -> Session:
    """Open a session from a directory (containing session.json) or json file."""
    if os.path.isdir(session_path):
        manifest_path = os.path.join(session_path, "session.json")
    else:
        manifest_path = session_path
    if not os.path.exists(manifest_path):
        raise FileNotFoundError(f"session manifest not found: {manifest_path}")
    with open(manifest_path) as fh:
        manifest = json.load(fh)
    return Session(manifest, os.path.dirname(os.path.abspath(manifest_path)))


class SessionManager:
    """Holds active sessions keyed by id (supports stdio single-session and the
    HTTP multi-session deployment mode)."""

    def __init__(self):
        self._sessions: Dict[str, Session] = {}
        self._default: Optional[str] = None

    def open(self, session_path: str, session_id: Optional[str] = None) -> str:
        sess = open_session(session_path)
        sid = session_id or os.path.abspath(
            session_path if os.path.isdir(session_path) else os.path.dirname(session_path))
        # replace existing
        if sid in self._sessions:
            self._sessions[sid].close()
        self._sessions[sid] = sess
        self._default = sid
        return sid

    def get(self, session_id: Optional[str] = None) -> Session:
        sid = session_id or self._default
        if not sid or sid not in self._sessions:
            raise KeyError("no active session; call launch(session_path) first")
        return self._sessions[sid]

    def close(self, session_id: Optional[str] = None) -> bool:
        sid = session_id or self._default
        if sid and sid in self._sessions:
            self._sessions.pop(sid).close()
            if self._default == sid:
                self._default = next(iter(self._sessions), None)
            return True
        return False

    def list_ids(self) -> List[str]:
        return list(self._sessions)
