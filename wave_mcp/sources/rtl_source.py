"""RTL static-analysis data source.

Single backend: a pyslang-elaborated netlist (built offline into maps.json) plus
the FST for temporal decisions. No Surelog/UHDM, no Verible.

  * categories 5 (connectivity / drivers / loads / fan-in) and 6 (trace) use the
    elaborated DriverMap/FanInMap/LoadMap + the FST trace engine.
  * categories 8 (files) and signal/module declaration lines come from the
    netlist (file/line per symbol).

When the netlist hasn't been built (e.g. elaboration failed), every netlist-
dependent call degrades gracefully to a structured ``available: false`` result —
never a silently wrong answer.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import List, Optional

from ..netlist.trace_engine import TraceEngine


@dataclass
class Unavailable:
    feature: str
    reason: str

    def to_dict(self) -> dict:
        return {"available": False, "feature": self.feature, "reason": self.reason,
                "hint": "Build the pyslang netlist for this session to enable "
                        "connectivity / driver / trace analysis."}


class RtlSource:
    def __init__(self, filelist: Optional[List[str]] = None,
                 maps_path: Optional[str] = None, fst=None):
        self.files: List[str] = [f for f in (filelist or []) if f]
        self.maps: dict = {}
        self.engine: Optional[TraceEngine] = None
        self.fst = fst
        self._maps_dir: Optional[str] = None
        if maps_path and os.path.exists(maps_path):
            self._load_maps(maps_path)

    def _load_maps(self, maps_path: str):
        try:
            with open(maps_path) as fh:
                self.maps = json.load(fh)
        except (OSError, ValueError):
            self.maps = {}
            return
        self._maps_dir = os.path.dirname(os.path.abspath(maps_path))
        self._normalize_map_paths()
        if self.maps.get("modules"):
            self.engine = TraceEngine(self.maps, self.fst)
            # backfill files from netlist if filelist empty
            if not self.files:
                fs = {self.resolve_file(m.get("file"))
                      for m in self.maps["modules"].values() if m.get("file")}
                self.files = sorted(f for f in fs if f)

    # -- path resolution ------------------------------------------------

    def _path_bases(self) -> List[str]:
        """Candidate base dirs for the relative paths stored in a netlist.

        A netlist records whatever path the elaborator saw: absolute in some
        builds, relative to the build cwd in others (``examples/sample/x.sv``).
        The build cwd is not recoverable from the file alone, so try the
        recorded ``build_root``, then the maps.json directory and its
        ancestors, since netlists are usually built from a project root that
        sits a few levels above the maps file.
        """
        bases: List[str] = []
        root = self.maps.get("build_root")
        if root:
            bases.append(root)
        if self._maps_dir:
            bases.append(self._maps_dir)
            cur = self._maps_dir
            for _ in range(8):
                parent = os.path.dirname(cur)
                if not parent or parent == cur:
                    break
                bases.append(parent)
                cur = parent
        cwd = os.getcwd()
        if cwd not in bases:
            bases.append(cwd)
        return bases

    def _normalize_map_paths(self) -> None:
        """Rewrite every ``file`` entry in the loaded netlist to a real path.

        Done once at load time rather than at each call site: ``file`` is
        surfaced by drivers/loads/fanin/trace records, port and signal
        declarations and ``modules_in_file``, all of which flow through
        different code paths (including ``TraceEngine``, which never sees this
        class). Normalising the map is the single choke point that makes every
        one of them hand back a path the caller can open.
        """
        cache: dict = {}

        def fix(node):
            if isinstance(node, dict):
                f = node.get("file")
                if isinstance(f, str) and f:
                    if f not in cache:
                        cache[f] = self.resolve_file(f)
                    node["file"] = cache[f]
                for v in node.values():
                    fix(v)
            elif isinstance(node, list):
                for v in node:
                    fix(v)

        fix(self.maps.get("modules"))

    def resolve_file(self, f: Optional[str]) -> Optional[str]:
        """Map a netlist file entry onto an existing absolute path.

        Returns the raw entry when nothing resolves, so callers still report
        a location instead of silently dropping it.
        """
        if not f:
            return None
        if os.path.isabs(f):
            return f
        for base in self._path_bases():
            cand = os.path.normpath(os.path.join(base, f))
            if os.path.exists(cand):
                return cand
        return f

    @property
    def has_netlist(self) -> bool:
        return self.engine is not None

    @property
    def verible(self) -> bool:  # kept for summary compat
        return False

    def signal_width(self, full_path: str) -> Optional[int]:
        """Declared bit width of a signal/port from the netlist (or None).

        Resolves ``full_path`` to its owning module via the trace engine, then
        looks the leaf up in that module's ports/signals width maps. Used by the
        FST bus-aggregation to validate merged widths against the RTL truth.
        """
        if not self.has_netlist:
            return None
        try:
            _inst, leaf, mod = self._resolve(full_path)
        except Exception:  # pylint: disable=broad-except
            return None  # unresolvable path -> no width hint (never crash)
        if not mod or mod not in self.maps.get("modules", {}):
            return None
        m = self.maps["modules"][mod]
        for table in ("ports", "signals"):
            info = m.get(table, {}).get(leaf)
            if info and info.get("width"):
                return int(info["width"])
        return None

    def netlist_health(self) -> dict:
        """Netlist-build health so callers know whether to trust connectivity /
        driver / trace answers (and why they might be limited).

        Reports module/instance counts, elaboration diagnostics, how many body
        members were skipped (partial-elaboration robustness), and a coarse
        ``status``/``trust`` verdict with an actionable hint on failure.
        """
        if not self.has_netlist:
            return {
                "available": False,
                "status": "unavailable",
                "trust": "none",
                "modules": 0,
                "reason": "netlist not built (elaboration failed or no sources)",
                "hint": "pass a filelist with +incdir+/+define+ (or incdirs=/"
                        "defines= to prepare_session); connectivity/driver/trace "
                        "are disabled until the netlist builds",
            }
        mods = self.maps.get("modules", {})
        diagnostics = int(self.maps.get("diagnostics", 0) or 0)
        skipped = sum(int(m.get("skipped_members", 0) or 0) for m in mods.values())
        n_inst = len(self.maps.get("instance_tree", {}) or {})
        is_partial = bool(self.maps.get("partial"))
        dsum = self.maps.get("diagnostics_summary", {}) or {}
        # trust is driven by *errors*, not total diagnostics: elaborating real
        # UVM produces thousands of harmless lint WARNINGS (IntBoolConv, etc.)
        # that must not be mistaken for a broken netlist.
        errors = int(dsum.get("errors", 0) or 0)
        warnings = max(diagnostics - errors, 0)
        n_lints = int(dsum.get("lints", 0) or 0)
        if errors == 0 and not is_partial:
            status = "ok" if skipped == 0 else "ok_with_warnings"
            trust = "full"
        else:
            status, trust = "ok_with_warnings", "partial"
        health = {
            "available": True,
            "status": status,
            "trust": trust,
            "partial": is_partial,
            "modules": len(mods),
            "instances": n_inst,
            "diagnostics": diagnostics,
            "diagnostic_errors": errors,
            "diagnostic_warnings": warnings,
            # slang style-lint diagnostics reclassified out of the error count
            # (EmptyBody/SignCompare/...): informational, never affect trust.
            "diagnostic_lints": n_lints,
            "skipped_members": skipped,
            "source_files": len(self.files),
            "note": ("clean elaboration" if trust == "full" else
                     f"{errors} error(s), {warnings} warning(s)"
                     + (f", {n_lints} style lint(s)" if n_lints else "")
                     + "; warnings are usually harmless UVM lint. Some "
                     "connectivity/trace paths may be incomplete only if "
                     "errors > 0."),
        }
        # surface actionable guidance (missing include/define/package) so the
        # user knows exactly what to add to make the netlist complete.
        if dsum.get("actionable_hints"):
            health["actionable_hints"] = dsum["actionable_hints"]
        if dsum.get("by_code"):
            health["top_diagnostic_codes"] = dict(
                list(dsum["by_code"].items())[:6])
        # self-healing report: incdirs/package-files the builder auto-discovered
        # (so the user can fold them back into the .f) and any tops that failed.
        auto = self.maps.get("auto_resolved") or {}
        if (auto.get("added_incdirs") or auto.get("added_files")
                or auto.get("rounds") or auto.get("uvm_incdirs")):
            health["auto_resolved"] = auto
        if self.maps.get("failed_tops"):
            health["failed_tops"] = self.maps["failed_tops"]
        return health

    # -- category 8: files -------------------------------------------------
    def all_files(self) -> List[str]:
        return list(self.files)

    def files_by_short_name(self, short: str, exact: bool = False) -> List[str]:
        if exact:
            return [f for f in self.files if os.path.basename(f) == short]
        s = short.lower()
        return [f for f in self.files if s in os.path.basename(f).lower()]

    def modules_in_file(self, full_file_path: str) -> List[str]:
        ap = os.path.abspath(full_file_path)
        if self.has_netlist:
            out = []
            for name, m in self.maps["modules"].items():
                resolved = self.resolve_file(m.get("file"))
                if resolved and os.path.abspath(resolved) == ap:
                    out.append(name)
            return out
        return []

    # -- declarations (2.5 / 3-line) ---------------------------------------
    def module_declaration(self, module_name: str) -> Optional[dict]:
        m = self.maps.get("modules", {}).get(module_name)
        if not m:
            return None
        return {"file": self.resolve_file(m.get("file")),
                "line": m.get("line"), "module": module_name}

    def signal_declaration(self, signal_leaf: str,
                           candidate_files: Optional[List[str]] = None) -> Optional[dict]:
        if not self.has_netlist:
            return None
        # search modules for the leaf in their loc map
        for m in self.maps["modules"].values():
            loc = m.get("loc", {}).get(signal_leaf)
            if loc and loc.get("line"):
                return {"file": self.resolve_file(loc["file"]),
                        "line": loc["line"]}
        return None

    # -- helpers -----------------------------------------------------------
    def _full(self, inst: str, name: str) -> str:
        return f"{inst}.{name}" if inst else name

    def _resolve(self, full_path: str):
        """Resolve using resolve_path (generate-scope aware).

        Returns (inst, leaf, mod) where inst is the matched instance prefix
        and leaf is the signal/port name within the resolved module.
        """
        mod, inst, leaf, _recs = self.engine.resolve_path(full_path)
        return inst, leaf, mod

    # -- cross-hierarchy helpers -----------------------------------------

    #: how many hierarchy hops to follow when a net has no local driver
    _MAX_HOPS = 4

    def _port_direction(self, module_def: Optional[str],
                        port: str) -> Optional[str]:
        """Declared direction of ``port`` on the instantiated module."""
        m = self.maps.get("modules", {}).get(module_def or "")
        if not m:
            return None
        return (m.get("ports", {}) or {}).get(port, {}).get("direction")

    def _peer_paths(self, full_path: str, want: str = "drivers") -> List[str]:
        """Connected signals, filtered to one direction across the hierarchy.

        ``want="drivers"`` returns what can drive ``full_path`` (same-module
        fan-in plus sub-module **output** ports). ``want="loads"`` returns what
        it drives (loads plus **input** ports).

        Direction matters: an input port is driven *by* this signal, so
        following it while hunting a driver walks away from the answer, and
        ``top.rst_n`` would be reported as driven by the flip-flop it feeds.
        Ports whose direction is unknown are still followed, since a missing
        port map should cost recall, not the whole feature.
        """
        inst, leaf, mod = self._resolve(full_path)
        if not mod or mod not in self.maps["modules"]:
            return []
        m = self.maps["modules"][mod]
        upstream = (want == "drivers")
        peers = set(m.get("fanin" if upstream else "loads", {}).get(leaf, []))
        out = [self._full(inst, s) for s in sorted(peers)]
        want_dir = "output" if upstream else "input"
        for ins in m.get("instances", []):
            for port, sig in ins.get("conns", {}).items():
                if sig != leaf:
                    continue
                d = self._port_direction(ins.get("def"), port)
                if d is None or d == want_dir:
                    out.append(f"{self._full(inst, ins['name'])}.{port}")
        seen, res = set(), []
        for p in out:
            if p not in seen:
                seen.add(p)
                res.append(p)
        return res

    # -- category 5: connectivity / drivers / loads / fan-in ---------------
    def drivers(self, full_path: str, _depth: int = 0,
                _seen: Optional[set] = None) -> dict:
        if not self.has_netlist:
            return Unavailable("signal_drivers", "netlist not built").to_dict()
        mod, _leaf, recs = self.engine.module_drivers(full_path)
        if mod is None:
            inst, leaf = self.engine.split(full_path)
            reason, hint = self.engine.classify_empty(inst, leaf, None, "drivers")
            return {"available": True, "signal": full_path, "drivers": [],
                    "reason": reason, "hint": hint}
        if not recs:
            # A net with no local driver is often just a wire to a sub-module
            # (``top.count`` driven by ``top.u_counter.count``). ``loads``
            # already followed these connections; do the same here instead of
            # reporting a misleading "undriven".
            cross = self._drivers_via_peers(full_path, _depth, _seen)
            if cross:
                return cross
            inst, leaf = self.engine.split(full_path)
            reason, hint = self.engine.classify_empty(inst, leaf, mod, "drivers")
            return {"available": True, "signal": full_path, "module": mod,
                    "drivers": [], "reason": reason, "hint": hint}
        inst, leaf = self.engine.split(full_path)
        out = []
        for r in recs:
            d = {**{k: r[k] for k in ("kind", "file", "line", "snippet")},
                        "rhs": [self._full(inst, s) for s in r["rhs"]],
                        "control": [self._full(inst, s) for s in r["control"]]}
            # netlist file entries may be relative to the build cwd; hand back
            # something the caller can actually open
            d["file"] = self.resolve_file(d.get("file"))
            # Include port_ref for instance_port drivers so callers can trace
            # cross-module connections without a separate active_drivers call.
            if r.get("port_ref"):
                pr = r["port_ref"]
                d["port_ref"] = {
                    "instance": pr.get("instance"),
                    "def": pr.get("def"),
                    "port": pr.get("port"),
                    "direction": pr.get("direction")}
            # Include guard conditions so callers can evaluate which branch
            # is active without a separate active_drivers call.
            if r.get("guard"):
                d["guard"] = r["guard"]
            out.append(d)
        return {"available": True, "signal": full_path, "module": mod, "drivers": out}

    def _drivers_via_peers(self, full_path: str, depth: int,
                           seen: Optional[set]) -> Optional[dict]:
        """Follow connections out of ``full_path`` looking for a real driver."""
        if depth >= self._MAX_HOPS:
            return None
        seen = set() if seen is None else seen
        seen.add(full_path)
        for peer in self._peer_paths(full_path):
            if peer in seen:
                continue
            sub = self.drivers(peer, _depth=depth + 1, _seen=seen)
            if sub.get("drivers"):
                return {
                    "available": True,
                    "signal": full_path,
                    "module": sub.get("module"),
                    "drivers": sub["drivers"],
                    "resolved_via": peer,
                    "note": (f"no driver in this module; followed the "
                             f"connection to {peer}"),
                }
        return None

    def loads(self, full_path: str) -> dict:
        if not self.has_netlist:
            return Unavailable("signal_loads", "netlist not built").to_dict()
        inst, leaf, mod = self._resolve(full_path)
        if not mod or mod not in self.maps["modules"]:
            reason, hint = self.engine.classify_empty(inst, leaf, mod, "loads")
            return {"available": True, "signal": full_path,
                    "loads": [], "reason": reason, "hint": hint}
        m = self.maps["modules"][mod]
        lds = m.get("loads", {}).get(leaf, [])
        # Cross-module loads: if this signal is a sub-module output port (e.g.
        # uart_core.tx), its loads are stored in the PARENT module under the key
        # "instance_name.port_name" (e.g. "uart_core.tx").  Search all modules
        # for loads entries that reference this signal as "instance.leaf".
        if not lds and inst:
            # Build the relative instance-leaf key as seen from the parent
            parts = inst.split(".")
            for i in range(len(parts) - 1, 0, -1):
                parent_inst = ".".join(parts[:i])
                parent_mod = self.engine.resolve_module(parent_inst)
                if parent_mod and parent_mod in self.maps["modules"]:
                    pm = self.maps["modules"][parent_mod]
                    # The sub-module is parts[i]; the signal is leaf
                    inst_leaf = f"{parts[i]}.{leaf}"
                    cross_lds = pm.get("loads", {}).get(inst_leaf, [])
                    if cross_lds:
                        lds = [self._full(parent_inst, s) for s in cross_lds]
                        return {"available": True, "signal": full_path,
                                "loads": lds}
        if not lds:
            # Downstream across the hierarchy: this signal feeds sub-module
            # input ports (top.rst_n -> top.u_counter.rst_n). Those peers *are*
            # the loads, so no recursion is needed here.
            peers = self._peer_paths(full_path, "loads")
            if peers:
                return {"available": True, "signal": full_path,
                        "loads": peers}
        if not lds:
            reason, hint = self.engine.classify_empty(inst, leaf, mod, "loads")
            return {"available": True, "signal": full_path,
                    "loads": [], "reason": reason, "hint": hint}
        return {"available": True, "signal": full_path,
                "loads": [self._full(inst, s) for s in lds]}

    def fan_in(self, signal_path: str, transitive: bool = False,
               max_signals: int = 500) -> dict:
        if not self.has_netlist:
            return Unavailable("signal_fanin", "netlist not built").to_dict()
        inst, leaf, mod = self._resolve(signal_path)
        if not mod or mod not in self.maps["modules"]:
            reason, hint = self.engine.classify_empty(inst, leaf, mod, "fanin")
            return {"available": True, "signal": signal_path,
                    "fan_in": [], "reason": reason, "hint": hint}
        fmap = self.maps["modules"][mod].get("fanin", {})
        if not transitive:
            res = fmap.get(leaf, [])
        else:
            seen, stack = set(), list(fmap.get(leaf, []))
            while stack and len(seen) < max_signals:
                s = stack.pop()
                if s in seen:
                    continue
                seen.add(s)
                stack.extend(fmap.get(s, []))
            res = sorted(seen)
        if not res:
            # Same asymmetry as drivers(): a net whose cone lives in a
            # sub-module must follow the connection instead of reporting
            # "undriven".
            cross = self._fanin_via_peers(signal_path, transitive,
                                          max_signals, 0, None)
            if cross:
                return cross
            reason, hint = self.engine.classify_empty(inst, leaf, mod, "fanin")
            return {"available": True, "signal": signal_path,
                    "fan_in": [], "reason": reason, "hint": hint}
        return {"available": True, "signal": signal_path,
                "fan_in": [self._full(inst, s) for s in res]}

    def _fanin_via_peers(self, signal_path: str, transitive: bool,
                         max_signals: int, depth: int,
                         seen: Optional[set]) -> Optional[dict]:
        if depth >= self._MAX_HOPS:
            return None
        seen = set() if seen is None else seen
        seen.add(signal_path)
        for peer in self._peer_paths(signal_path):
            if peer in seen:
                continue
            sub = self.fan_in(peer, transitive=transitive,
                              max_signals=max_signals)
            if sub.get("fan_in"):
                return {
                    "available": True,
                    "signal": signal_path,
                    "fan_in": sub["fan_in"],
                    "resolved_via": peer,
                    "note": (f"no fan-in in this module; followed the "
                             f"connection to {peer}"),
                }
        return None

    def connectivity(self, full_path: str) -> dict:
        if not self.has_netlist:
            return Unavailable("signal_connectivity", "netlist not built").to_dict()
        inst, leaf, mod = self._resolve(full_path)
        if not mod or mod not in self.maps["modules"]:
            reason, hint = self.engine.classify_empty(inst, leaf, mod, "loads")
            return {"available": True, "signal": full_path,
                    "connected": [], "reason": reason, "hint": hint}
        m = self.maps["modules"][mod]
        peers = set(m.get("fanin", {}).get(leaf, [])) | set(m.get("loads", {}).get(leaf, []))
        # instance port connections referencing this signal
        port_peers = []
        for ins in m.get("instances", []):
            for port, sig in ins.get("conns", {}).items():
                if sig == leaf:
                    port_peers.append(f"{self._full(inst, ins['name'])}.{port}")
        # De-duplicate while preserving sorted order: the same target may
        # appear via both fanin/loads and instance port connections, producing
        # redundant entries that confuse the user.
        result = sorted(set(
            [self._full(inst, s) for s in sorted(peers)] + port_peers))
        if not result:
            reason, hint = self.engine.classify_empty(inst, leaf, mod, "loads")
            return {"available": True, "signal": full_path,
                    "connected": [], "reason": reason, "hint": hint}
        return {"available": True, "signal": full_path,
                "connected": result}

    # -- category 5.5 / 6: active drivers + trace --------------------------
    def active_drivers(self, signal_full_path: str, time: str) -> dict:
        if not self.has_netlist:
            return Unavailable("active_drivers", "netlist not built").to_dict()
        return self.engine.active_drivers(signal_full_path, time)

    def driver_contributors(self, driver_unique_id: str) -> dict:
        if not self.has_netlist:
            return Unavailable("driver_contributors", "netlist not built").to_dict()
        return self.engine.driver_contributors(driver_unique_id)

    def trace_value(self, signal_path: str, time_point: str,
                    max_depth: int = 12) -> dict:
        if not self.has_netlist:
            return Unavailable("trace_value", "netlist not built").to_dict()
        return self.engine.trace_value(signal_path, time_point, max_depth=max_depth)

    def trace_x(self, signal_path: str, time_point: str,
                max_depth: int = 12) -> dict:
        if not self.has_netlist:
            return Unavailable("trace_x", "netlist not built").to_dict()
        return self.engine.trace_x(signal_path, time_point, max_depth=max_depth)
