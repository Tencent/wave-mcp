"""RTL static-analysis data source (Indago categories 2.5, 3-line, 5, 6, 8).

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
from typing import Dict, List, Optional

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
        if maps_path and os.path.exists(maps_path):
            self._load_maps(maps_path)

    def _load_maps(self, maps_path: str):
        try:
            with open(maps_path) as fh:
                self.maps = json.load(fh)
        except (OSError, ValueError):
            self.maps = {}
            return
        if self.maps.get("modules"):
            self.engine = TraceEngine(self.maps, self.fst)
            # backfill files from netlist if filelist empty
            if not self.files:
                fs = {m.get("file") for m in self.maps["modules"].values() if m.get("file")}
                self.files = sorted(f for f in fs if f)

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
            inst, leaf, mod = self._resolve(full_path)
        except Exception:
            return None
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
            "skipped_members": skipped,
            "source_files": len(self.files),
            "note": ("clean elaboration" if trust == "full" else
                     f"{errors} error(s), {warnings} warning(s); warnings are "
                     "usually harmless UVM lint. Some connectivity/trace paths "
                     "may be incomplete only if errors > 0."),
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
            return [name for name, m in self.maps["modules"].items()
                    if m.get("file") and os.path.abspath(m["file"]) == ap]
        return []

    # -- declarations (2.5 / 3-line) ---------------------------------------
    def module_declaration(self, module_name: str) -> Optional[dict]:
        m = self.maps.get("modules", {}).get(module_name)
        if not m:
            return None
        return {"file": m.get("file"), "line": m.get("line"), "module": module_name}

    def signal_declaration(self, signal_leaf: str,
                           candidate_files: Optional[List[str]] = None) -> Optional[dict]:
        if not self.has_netlist:
            return None
        # search modules for the leaf in their loc map
        for m in self.maps["modules"].values():
            loc = m.get("loc", {}).get(signal_leaf)
            if loc and loc.get("line"):
                return {"file": loc["file"], "line": loc["line"]}
        return None

    # -- helpers -----------------------------------------------------------
    def _full(self, inst: str, name: str) -> str:
        return f"{inst}.{name}" if inst else name

    def _resolve(self, full_path: str):
        inst, leaf = self.engine.split(full_path)
        mod = self.engine.resolve_module(inst)
        return inst, leaf, mod

    # -- category 5: connectivity / drivers / loads / fan-in ---------------
    def drivers(self, full_path: str) -> dict:
        if not self.has_netlist:
            return Unavailable("signal_drivers", "netlist not built").to_dict()
        mod, leaf, recs = self.engine.module_drivers(full_path)
        if mod is None:
            return {"available": True, "signal": full_path, "drivers": [],
                    "note": "module not resolved (possibly a TB-only signal)"}
        inst, _ = self.engine.split(full_path)
        out = []
        for r in recs:
            out.append({**{k: r[k] for k in ("kind", "file", "line", "snippet")},
                        "rhs": [self._full(inst, s) for s in r["rhs"]],
                        "control": [self._full(inst, s) for s in r["control"]]})
        return {"available": True, "signal": full_path, "module": mod, "drivers": out}

    def loads(self, full_path: str) -> dict:
        if not self.has_netlist:
            return Unavailable("signal_loads", "netlist not built").to_dict()
        inst, leaf, mod = self._resolve(full_path)
        if not mod or mod not in self.maps["modules"]:
            return {"available": True, "signal": full_path, "loads": []}
        lds = self.maps["modules"][mod].get("loads", {}).get(leaf, [])
        return {"available": True, "signal": full_path,
                "loads": [self._full(inst, s) for s in lds]}

    def fan_in(self, signal_path: str, transitive: bool = False,
               max_signals: int = 500) -> dict:
        if not self.has_netlist:
            return Unavailable("signal_fanin", "netlist not built").to_dict()
        inst, leaf, mod = self._resolve(signal_path)
        if not mod or mod not in self.maps["modules"]:
            return {"available": True, "signal": signal_path, "fan_in": []}
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
        return {"available": True, "signal": signal_path,
                "fan_in": [self._full(inst, s) for s in res]}

    def connectivity(self, full_path: str) -> dict:
        if not self.has_netlist:
            return Unavailable("signal_connectivity", "netlist not built").to_dict()
        inst, leaf, mod = self._resolve(full_path)
        if not mod or mod not in self.maps["modules"]:
            return {"available": True, "signal": full_path, "connected": []}
        m = self.maps["modules"][mod]
        peers = set(m.get("fanin", {}).get(leaf, [])) | set(m.get("loads", {}).get(leaf, []))
        # instance port connections referencing this signal
        port_peers = []
        for ins in m.get("instances", []):
            for port, sig in ins.get("conns", {}).items():
                if sig == leaf:
                    port_peers.append(f"{self._full(inst, ins['name'])}.{port}")
        return {"available": True, "signal": full_path,
                "connected": [self._full(inst, s) for s in sorted(peers)] + port_peers}

    # -- category 5.5 / 6: active drivers + trace --------------------------
    def active_drivers(self, signal_full_path: str, time: str) -> dict:
        if not self.has_netlist:
            return Unavailable("active_drivers", "netlist not built").to_dict()
        return self.engine.active_drivers(signal_full_path, time)

    def driver_contributors(self, driver_unique_id: str) -> dict:
        if not self.has_netlist:
            return Unavailable("driver_contributors", "netlist not built").to_dict()
        return self.engine.driver_contributors(driver_unique_id)

    def trace_value(self, signal_path: str, time_point: str) -> dict:
        if not self.has_netlist:
            return Unavailable("trace_value", "netlist not built").to_dict()
        return self.engine.trace_value(signal_path, time_point)

    def trace_x(self, signal_path: str, time_point: str) -> dict:
        if not self.has_netlist:
            return Unavailable("trace_x", "netlist not built").to_dict()
        return self.engine.trace_x(signal_path, time_point)
