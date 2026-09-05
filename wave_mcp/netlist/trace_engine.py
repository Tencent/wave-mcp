"""Trace engine: structural (UHDM-equivalent netlist) x temporal (FST values).

Combines the pyslang netlist with FST values to provide:
  * active drivers of a signal at a time (value-informed)
  * driver contributors
  * trace_value : backward driver-chain walk, each node annotated with its value
                  at the time point and the driving code location
  * trace_x     : same walk, following only fan-ins that are X at the time point

Structural data comes from the elaborated netlist (accurate). Temporal decisions
(which branch / fan-in is active) use FST values and are best-effort — matching
the "approximate" positioning for trace in the requirements.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .. import timeutil
from . import expr_eval


class TraceEngine:
    def __init__(self, maps: dict, fst):
        self.modules: Dict[str, dict] = maps.get("modules", {})
        self.instance_tree: Dict[str, str] = maps.get("instance_tree", {})
        self.fst = fst
        # reverse index: leaf instance name -> [(full_netlist_key, module), ...].
        # Netlist keys are rooted at the DUT top (e.g. ``decode.u_decode_unit``)
        # while FST scope paths are rooted at the sim top (``top_tb.U_DECODE.
        # u_decode_unit``); matching on the leaf (plus longer suffix to break
        # ties) bridges the two without needing top_tb to elaborate. Built once
        # for O(1) leaf lookup instead of scanning the whole tree per scope.
        self._leaf_index: Dict[str, List[Tuple[str, str]]] = {}
        for key, mod in self.instance_tree.items():
            leaf = key.rsplit(".", 1)[-1]
            self._leaf_index.setdefault(leaf, []).append((key, mod))

    # -- resolution ---------------------------------------------------------
    @staticmethod
    def _suffix_len(a: List[str], b: List[str]) -> int:
        """Number of trailing dotted segments shared by two split paths."""
        n = 0
        i, j = len(a) - 1, len(b) - 1
        while i >= 0 and j >= 0 and a[i] == b[j]:
            n += 1
            i -= 1
            j -= 1
        return n

    def _resolve_key(self, instance_path: str) -> Tuple[Optional[str], Optional[str]]:
        """Resolve to ``(module, matched_netlist_key)`` — key drives anchoring.

        The netlist key is the instance_tree entry that matched (so the anchor
        pass can derive a parent instance's mapping from a resolved child).
        """
        # 1. exact hierarchical match (only when top_tb itself elaborated)
        if instance_path in self.instance_tree:
            return self.instance_tree[instance_path], instance_path
        # 2. leaf + longest-suffix match against the (possibly partial) netlist.
        #    FST adds a top prefix (top_tb.U_DECODE.) the DUT-rooted netlist lacks,
        #    so we anchor on the leaf and, when several instances share a leaf but
        #    map to different modules, prefer the one sharing the longest path
        #    suffix (uses parent context). Ambiguous ties decline (honest null)
        #    rather than guess wrong.
        fst_parts = instance_path.split(".")
        cands = self._leaf_index.get(fst_parts[-1])
        if cands:
            if len(cands) == 1:
                return cands[0][1], cands[0][0]
            best: Optional[Tuple[str, str]] = None
            best_len = -1
            tie = False
            for key, mod in cands:
                slen = self._suffix_len(fst_parts, key.split("."))
                if slen > best_len:
                    best_len, best, tie = slen, (mod, key), False
                elif slen == best_len and best is not None and mod != best[0]:
                    tie = True
            if best is not None and not tie:
                return best[0], best[1]
        # 3. FST scope metadata. ``module_name`` is empty for VCD->FST (VCD
        #    carries no module type), but ``definition_name`` is filled in by
        #    resolve_definitions()'s anchor pass, which already derived e.g.
        #    ``top_tb.U_DECODE`` -> ``decode`` from the netlist. Honour it:
        #    without this the DUT *root* scope never resolves (its FST instance
        #    name differs from the netlist root key), so every signal declared
        #    directly on the DUT top reports "unresolved_path" even though the
        #    netlist has its drivers. Children still resolve via the leaf
        #    index, which is why only the root looked broken.
        sc = self.fst.scopes.get(instance_path) if self.fst else None
        if sc:
            for name in (getattr(sc, "module_name", ""),
                         getattr(sc, "definition_name", "")):
                if name and name in self.modules:
                    return name, None
        return None, None

    def resolve_module(self, instance_path: str) -> Optional[str]:
        return self._resolve_key(instance_path)[0]

    def resolve_definitions(self, scope_paths: List[str]) -> Dict[str, str]:
        """Batch-resolve FST scope paths -> module def, with anchor propagation.

        Two passes:
          1. direct: exact / leaf / longest-suffix match (see ``_resolve_key``).
          2. anchor ("向上推导"): a scope whose *children* matched netlist keys
             ``NK.child`` must itself correspond to netlist parent ``NK`` — so we
             map it to ``instance_tree[NK]``. This recovers the DUT-root node
             (e.g. ``top_tb.U_DECODE`` -> ``decode``) via the netlist even when
             the instance name differs from the module name, and it uses netlist
             truth instead of a name guess. Iterated so anchors propagate upward
             through multiple wrapper levels. Only unambiguous votes are taken.
        """
        resolved: Dict[str, str] = {}
        matched_key: Dict[str, str] = {}
        for p in scope_paths:
            mod, key = self._resolve_key(p)
            if mod:
                resolved[p] = mod
                if key:
                    matched_key[p] = key
        # children index for the anchor pass
        children: Dict[str, List[str]] = {}
        for p in scope_paths:
            if "." in p:
                children.setdefault(p.rsplit(".", 1)[0], []).append(p)
        changed = True
        rounds = 0
        while changed and rounds < 16:
            changed = False
            rounds += 1
            for p, kids in children.items():
                if p in resolved:
                    continue
                votes: Dict[str, int] = {}
                for c in kids:
                    mk = matched_key.get(c)
                    if mk and "." in mk:
                        pk = mk.rsplit(".", 1)[0]
                        votes[pk] = votes.get(pk, 0) + 1
                if not votes:
                    continue
                ranked = sorted(votes.items(), key=lambda x: -x[1])
                if len(ranked) == 1 or ranked[0][1] > ranked[1][1]:
                    pk = ranked[0][0]
                    mod = self.instance_tree.get(pk)
                    if mod:
                        resolved[p] = mod
                        matched_key[p] = pk
                        changed = True
        return resolved

    def split(self, full_path: str) -> Tuple[str, str]:
        if "." in full_path:
            inst, leaf = full_path.rsplit(".", 1)
        else:
            inst, leaf = "", full_path
        return inst, leaf

    def resolve_path(self, full_path: str
                     ) -> Tuple[Optional[str], str, str, List[dict]]:
        """Resolve ``full_path`` -> (module, instance_path, leaf, drivers).

        ``instance_path`` is the *true* instance hierarchy (no struct field
        suffix), so callers can correctly build child signal paths. ``leaf`` may
        be a field-qualified name (e.g. ``tl_h_o.a_ready``). Falls back to the
        deepest dotted prefix that resolves to a module when ``rsplit('.', 1)``
        misattributes a struct field onto the instance part.

        Generate-scope aware: when the instance path contains generate scopes
        (``gen_xxx``, ``array[idx]``) absent from ``instance_tree``, the walk-
        back picks the nearest parent that *does* resolve. If the original
        trailing token is a declared port/signal in that parent module, it is
        used as the leaf — this correctly recovers signals inside generate
        blocks without relying on naming conventions.
        """
        # fast path: trailing token is the leaf
        inst, leaf = self.split(full_path)
        mod = self.resolve_module(inst)
        if mod and mod in self.modules:
            return (mod, inst, leaf,
                    self._lookup_drivers(self.modules[mod]["drivers"], leaf))
        # walk back to deepest instance prefix that resolves to a module
        parts = full_path.split(".")
        orig_leaf = parts[-1]
        for cut in range(len(parts) - 1, 0, -1):
            cand_inst = ".".join(parts[:cut])
            cand_mod = self.resolve_module(cand_inst)
            if cand_mod and cand_mod in self.modules:
                m = self.modules[cand_mod]
                # generate-scope recovery: if the original trailing token is a
                # real port/signal in the resolved parent module, use it as the
                # leaf instead of the joined field-level name.
                if orig_leaf in m.get("ports", {}) or orig_leaf in m.get("signals", {}):
                    return (cand_mod, cand_inst, orig_leaf,
                            self._lookup_drivers(m["drivers"], orig_leaf))
                # field-level path (struct/bit-select): use joined leaf
                cand_leaf = ".".join(parts[cut:])
                return (cand_mod, cand_inst, cand_leaf,
                        self._lookup_drivers(m["drivers"], cand_leaf))
        return None, inst, leaf, []

    @staticmethod
    def _lookup_drivers(drivers: Dict[str, List[dict]], leaf: str) -> List[dict]:
        """Exact leaf match, else aggregate all field-level ``leaf.<field>``."""
        if leaf in drivers:
            return drivers[leaf]
        prefix = leaf + "."
        return [r for k, recs in drivers.items() if k.startswith(prefix)
                for r in recs]

    def module_drivers(self, full_path: str) -> Tuple[Optional[str], str, List[dict]]:
        mod, _inst, leaf, recs = self.resolve_path(full_path)
        return mod, leaf, recs

    # -- empty-result classification ----------------------------------------
    def classify_empty(self, inst: str, leaf: str, mod: Optional[str],
                       context: str) -> Tuple[str, str]:
        """Classify why a connectivity query returned empty for this signal.

        context: "loads" | "fanin" | "drivers"
        Returns (reason, hint) so callers can distinguish structurally-expected
        emptiness (input ports, parameters, DUT outputs) from genuine gaps.
        """
        if not mod or mod not in self.modules:
            return ("unresolved_path",
                    f"Cannot resolve '{leaf}' to any module in the netlist. "
                    "It may be a testbench-only signal, a generate-scope "
                    "reference, or a hierarchical path the netlist did not "
                    "elaborate.")

        m = self.modules[mod]
        port_info = m.get("ports", {}).get(leaf)
        sig_info = m.get("signals", {}).get(leaf)

        # Not in ports or signals → likely parameter / localparam / constant
        if not port_info and not sig_info:
            return ("parameter_or_constant",
                    f"'{leaf}' is not declared as a port or signal in module "
                    f"'{mod}'. It is likely a parameter, localparam, enum "
                    "value, or constant — these have no dynamic connectivity.")

        if port_info:
            direction = port_info.get("direction", "implicit")
            if context in ("fanin", "drivers"):
                if direction == "input":
                    return ("primary_input",
                            f"'{leaf}' is an input port of module '{mod}'. "
                            "Input ports are driven externally (testbench or "
                            "parent module) and have no internal RTL driver.")
                if direction == "inout":
                    return ("inout_port",
                            f"'{leaf}' is an inout port of module '{mod}'. "
                            "Bidirectional ports may have conditional drivers "
                            "not captured in static analysis.")
            if context == "loads":
                if direction == "output":
                    has_parent = bool(inst) and "." in inst and any(
                        inst.startswith(p + ".") for p in self.instance_tree)
                    if has_parent:
                        return ("submodule_output_port",
                                f"'{leaf}' is an output port of instance "
                                f"'{inst}' (module '{mod}'). Its loads are "
                                "tracked at the parent module level; if empty, "
                                "the parent may not connect this output to any "
                                "consumer.")
                    return ("dut_output_port",
                            f"'{leaf}' is an output port of the top-level DUT "
                            f"'{mod}'. DUT outputs connect to the testbench, "
                            "which is outside the netlist scope.")
                if direction == "input":
                    return ("unused_input",
                            f"'{leaf}' is an input port of module '{mod}' with "
                            "no internal loads. The signal is declared but not "
                            "read by any logic in this module.")

        if sig_info:
            kind = sig_info.get("kind", "signal")
            if context == "loads":
                return ("unused_signal",
                        f"'{leaf}' is a {kind} in module '{mod}' with no "
                        "loads. Nothing in this module reads this signal.")
            if context in ("fanin", "drivers"):
                return ("undriven_signal",
                        f"'{leaf}' is a {kind} in module '{mod}' with no "
                        "driver. It may be a tie-off, unused declaration, or "
                        "driven by a construct not captured in static analysis "
                        "(e.g. generate).")

        return ("genuinely_empty",
                "No connectivity entries found; the signal exists in the "
                "netlist but has no recorded connections for this query type.")

    # -- value helpers ------------------------------------------------------
    def _units(self, time: str) -> int:
        return timeutil.time_to_fst_units(time, self.fst.timescale_exp)

    def _value(self, inst: str, relname: str, units: int) -> Optional[dict]:
        full = f"{inst}.{relname}" if inst else relname
        if full not in self.fst.signals:
            return None
        return self.fst.value_at(full, units)

    @staticmethod
    def _is_x(val: Optional[dict]) -> bool:
        if not val:
            return False
        v = val.get("value", "")
        return "x" in v.lower() or "z" in v.lower()

    # -- category 5.5: active drivers --------------------------------------
    def active_drivers(self, signal_full_path: str, time: str) -> dict:
        mod, inst, leaf, recs = self.resolve_path(signal_full_path)
        if mod is None:
            return {"available": False, "reason": f"cannot resolve module for {signal_full_path}"}
        if not recs:
            reason, hint = self.classify_empty(inst, leaf, mod, "drivers")
            return {"available": True, "signal": signal_full_path, "time": time,
                    "active_drivers": [], "reason": reason, "hint": hint}
        units = self._units(time)
        annotated = []
        for i, r in enumerate(recs):
            ctrl_vals = {c: (self._value(inst, c, units) or {}).get("value")
                         for c in r["control"]}
            rhs_vals = {s: (self._value(inst, s, units) or {}).get("value")
                        for s in r["rhs"]}
            ad = {
                "driver_unique_id": f"{mod}.{leaf}#{i}",
                "kind": r["kind"], "file": r["file"], "line": r["line"],
                "snippet": r["snippet"],
                "rhs": r["rhs"], "control": r["control"],
                "rhs_values": rhs_vals, "control_values": ctrl_vals,
            }
            if r.get("port_ref"):
                pr = r["port_ref"]
                ad["driven_by_instance_port"] = {
                    "instance": pr["instance"], "def": pr.get("def"),
                    "port": pr["port"], "direction": pr.get("direction")}
            annotated.append(ad)
        # precise: evaluate each driver's branch guard with FST values at `time`;
        # fall back to the value-informed heuristic only when undecidable.
        likely, method = self._active_by_guard(inst, recs, units)
        vf = self._value_fn(inst, units)
        for i, r in enumerate(recs):
            annotated[i]["guard_active"] = self._guard_holds(r.get("guard", []), vf)
        return {"available": True, "signal": signal_full_path, "time": time,
                "module": mod, "active_drivers": annotated,
                "likely_active_index": likely, "selection_method": method,
                "note": {"single": "single static driver",
                         "guard": "selected by branch-condition evaluation (precise)",
                         "heuristic": "guards not decisive; value-informed best-effort",
                         "heuristic(guard-x)": "guard is X at this time; value-informed best-effort",
                         }.get(method, method)}

    # -- active-driver selection -------------------------------------------
    def _value_fn(self, inst: str, units: int):
        def vf(name: str) -> Optional[str]:
            full = f"{inst}.{name}" if inst else name
            if full not in self.fst.signals:
                return None
            v = self.fst.value_at(full, units)
            return v.get("value") if v else None
        return vf

    @staticmethod
    def _guard_holds(guard: List[dict], vf) -> Optional[bool]:
        """True/False if all guard items decidably (un)hold; None if undecidable (X)."""
        if not guard:
            return None
        for item in guard:
            s = expr_eval.guard_satisfied(item.get("cond", {}), item.get("expect", 1), vf)
            if s is None:
                return None
            if not s:
                return False
        return True

    def _active_by_guard(self, inst: str, recs: List[dict], units: int) -> Tuple[Optional[int], str]:
        if len(recs) == 1:
            return 0, "single"
        vf = self._value_fn(inst, units)
        hits, undecidable = [], False
        for i, r in enumerate(recs):
            if not r.get("guard"):
                continue
            h = self._guard_holds(r["guard"], vf)
            if h is None:
                undecidable = True
            elif h:
                hits.append(i)
        if len(hits) == 1:
            return hits[0], "guard"
        return (self._likely_active(inst, recs, units),
                "heuristic(guard-x)" if undecidable else "heuristic")

    def _last_change(self, inst: str, sig: str, units: int) -> int:
        full = f"{inst}.{sig}" if inst else sig
        if full not in self.fst.signals:
            return -1
        rows = self.fst.values_between(full, self.fst.start_time, units, 5000)
        return rows[-1]["time_units"] if rows else -1

    def _likely_active(self, inst: str, recs: List[dict], units: int) -> Optional[int]:
        """Best-effort active-driver pick (approximate, value-informed).

        Heuristic: prefer the driver whose fan-in signals changed most recently
        at/just before `time`; on a tie prefer a data-path driver (non-empty RHS)
        over a constant/reset-branch driver. True branch-condition evaluation is
        not performed, so this is an approximation (see requirements)."""
        if len(recs) == 1:
            return 0
        best_i, best_key = None, None
        for i, r in enumerate(recs):
            last = -1
            for s in r["rhs"] + r["control"]:
                last = max(last, self._last_change(inst, s, units))
            key = (last, 1 if r["rhs"] else 0)  # later change wins; data-path breaks tie
            if best_key is None or key > best_key:
                best_key, best_i = key, i
        return best_i

    def driver_contributors(self, driver_unique_id: str) -> dict:
        try:
            head, idx = driver_unique_id.rsplit("#", 1)
            mod, leaf = head.split(".", 1)
            idx = int(idx)
        except (ValueError, IndexError):
            return {"available": False, "reason": f"unknown driver id {driver_unique_id}"}
        if mod not in self.modules:
            return {"available": False, "reason": f"unknown driver id {driver_unique_id}"}
        # Use _lookup_drivers to handle field-qualified paths (e.g. tl_h_o.a_ready
        # when the driver key is the root tl_h_o).  Direct dict lookup fails for
        # reggen-generated struct field assigns, causing driver_contributors to
        # return unavailable.
        recs = self._lookup_drivers(self.modules[mod]["drivers"], leaf)
        if idx >= len(recs):
            return {"available": False, "reason": f"driver index {idx} out of range for {driver_unique_id}"}
        rec = recs[idx]
        return {"available": True, "driver_unique_id": driver_unique_id,
                "rhs_signals": rec["rhs"], "control_signals": rec["control"],
                "file": rec["file"], "line": rec["line"], "snippet": rec["snippet"]}

    # -- category 6: trace_value / trace_x ---------------------------------
    def trace_value(self, signal_path: str, time_point: str, max_depth: int = 12,
                    x_only: bool = False) -> dict:
        units = self._units(time_point)
        visited = set()

        def node(full_path: str, depth: int) -> dict:
            val = self.fst.value_at(full_path, units) if full_path in self.fst.signals else None
            entry = {
                "signal": full_path,
                "value": (val or {}).get("value"),
                "hex": (val or {}).get("hex"),
            }
            key = full_path
            if key in visited or depth >= max_depth:
                entry["truncated"] = True
                return entry
            visited.add(key)

            # resolve_path gives the TRUE instance path (without struct-field
            # suffix), so child signal paths are built against the instance, not
            # against a mis-split "inst.field" prefix.
            mod, inst, _leaf, recs = self.resolve_path(full_path)
            if mod is None:
                entry["boundary"] = "unresolved-module"
                return entry
            if not recs:
                entry["boundary"] = "primary-input/port/constant"
                if x_only and self._is_x(val):
                    entry["note"] = ("X root cause candidate: no driver in this "
                                     "module — undriven primary input / port "
                                     "(driven above this scope or not at all)")
                return entry

            # multi-driver net: if branch-guard evaluation says 2+ drivers are
            # simultaneously active (tri-state contention -> X), expose ALL of
            # them as parallel branches instead of heuristically picking one.
            if len(recs) > 1:
                vf = self._value_fn(inst, units)
                actives = [i for i, r in enumerate(recs)
                           if self._guard_holds(r.get("guard", []), vf)]
                if len(actives) >= 2:
                    entry["driver_conflict"] = {
                        "reason": "multiple drivers active at this time "
                                  "(bus contention -> X)",
                        "active_driver_count": len(actives),
                        "total_driver_count": len(recs)}
                    conflict_nodes = []
                    for i in actives:
                        r = recs[i]
                        dnode = {"driver": {
                            "kind": r["kind"], "file": r["file"],
                            "line": r["line"], "snippet": r["snippet"],
                            "selection_method": "conflict"}}
                        contribs = []
                        for s2 in dict.fromkeys(r["rhs"] + r["control"]):
                            child_full = f"{inst}.{s2}" if inst else s2
                            contribs.append(node(child_full, depth + 1))
                        if contribs:
                            dnode["contributors"] = contribs
                        conflict_nodes.append(dnode)
                    entry["conflicting_drivers"] = conflict_nodes
                    return entry

            # choose driver: precise branch-guard evaluation, heuristic fallback
            ai, method = self._active_by_guard(inst, recs, units)
            rec = recs[ai if ai is not None else 0]
            entry["driver"] = {"kind": rec["kind"], "file": rec["file"],
                               "line": rec["line"], "snippet": rec["snippet"],
                               "selection_method": method}
            if rec["kind"] == "nonblocking":
                entry["note"] = "sequential (registered) — value latched from a previous cycle"

            # Plan-1: cross-module trace. The active driver is a sub-instance output
            # port -> descend into that instance and continue from the internal net
            # that the port maps to (instance.<port>), so the chain crosses hierarchy.
            if rec["kind"] == "instance_port" and rec.get("port_ref"):
                pr = rec["port_ref"]
                child_inst = f"{inst}.{pr['instance']}" if inst else pr["instance"]
                child_full = f"{child_inst}.{pr['port']}"
                entry["crosses_into"] = {"instance": pr["instance"],
                                         "def": pr.get("def"),
                                         "port": pr["port"],
                                         "direction": pr.get("direction")}
                if not (x_only and not self._is_x(
                        self.fst.value_at(child_full, units)
                        if child_full in self.fst.signals else None)):
                    entry["contributors"] = [node(child_full, depth + 1)]
                return entry

            contributors = []
            for s in dict.fromkeys(rec["rhs"] + rec["control"]):
                child_full = f"{inst}.{s}" if inst else s
                if x_only:
                    cv = self.fst.value_at(child_full, units) if child_full in self.fst.signals else None
                    if not self._is_x(cv):
                        continue
                contributors.append(node(child_full, depth + 1))
            if contributors:
                entry["contributors"] = contributors
            # sequential boundary: do not chase past a register's own data more than noted
            return entry

        root = node(signal_path, 0)

        # Build tree summary for the user (depth / truncation reporting)
        tree_summary = {"max_depth": 0, "total_nodes": 0,
                        "truncated_nodes": 0, "modules_crossed": 0}

        def _summarize(n, depth=0):
            if not isinstance(n, dict):
                return
            tree_summary["total_nodes"] += 1
            tree_summary["max_depth"] = max(tree_summary["max_depth"], depth)
            if n.get("truncated"):
                tree_summary["truncated_nodes"] += 1
            if n.get("crosses_into"):
                tree_summary["modules_crossed"] += 1
            for c in n.get("contributors", []):
                _summarize(c, depth + 1)
            for d in n.get("conflicting_drivers", []):
                for c in d.get("contributors", []):
                    _summarize(c, depth + 1)

        _summarize(root)

        return {"available": True, "signal": signal_path, "time": time_point,
                "mode": "trace_x" if x_only else "trace_value", "tree": root,
                "tree_summary": tree_summary}

    def trace_x(self, signal_path: str, time_point: str,
                max_depth: int = 12) -> dict:
        val = self.fst.value_at(signal_path, self._units(time_point)) \
            if signal_path in self.fst.signals else None
        if not self._is_x(val):
            return {"available": True, "signal": signal_path, "time": time_point,
                    "result": "no-x", "value": (val or {}).get("value"),
                    "note": "signal has no X/Z at this time"}
        res = self.trace_value(signal_path, time_point, max_depth=max_depth, x_only=True)
        res["note"] = ("X-trace is approximate: follows fan-ins that are X at the time "
                       "point; X-optimism corner cases may not be covered.")
        return res
