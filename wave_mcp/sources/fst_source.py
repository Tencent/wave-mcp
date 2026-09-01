"""FST waveform data source (backed by pylibfst / fstapi).

Responsibilities:
  * design hierarchy (scope tree) from FST scopes
  * signal listing per instance with width / direction / type
  * signal value queries: point value, value over a time window, full history

This is the **strong point** of the open-source stack: FST supports fast random
access, which matches the AI point-query / search workload.

NOTE: FST does *not* carry source declaration file/line nor connectivity. Those
are provided by the RTL static-refinement source (stage 3).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import re

import pylibfst
from pylibfst import ffi, lib

from .. import timeutil

# Some writers (e.g. vcd2fst) embed the vector range in the variable name, like
# "count [7:0]". Strip a trailing bit-range (with colon) to get the canonical
# leaf name; keep single-index "[n]" array elements intact.
_RANGE_SUFFIX = re.compile(r"^(.*?)\s*\[\d+:\d+\]\s*$")

# A single unpacked-array element / split bit, e.g. "bus [31]" or "arr[0]".
# FST/VCD writers emit each element as its own VAR; we aggregate these back into
# one bus entry at the listing layer (per-element signals stay queryable).
_ELEM_INDEX = re.compile(r"^(.+?)\s*\[(\d+)\]\s*$")
# Underscore-split style some tools/hand-written dumps use: "data_7" ... "data_0".
# Riskier (a real signal may legitimately end in _<n>), so it is only applied to
# groups of >=2 members sharing a base AND is gated behind ``underscore_style``.
_ELEM_USCORE = re.compile(r"^(.+?)_(\d+)$")

# ---- enum mappings ---------------------------------------------------------

_SCOPE_TYPE = {
    0: "module", 1: "task", 2: "function", 3: "begin", 4: "fork",
    5: "generate", 6: "struct", 7: "union", 8: "class", 9: "interface",
    10: "package", 11: "program",
}

_VAR_TYPE = {
    0: "event", 1: "integer", 2: "parameter", 3: "real", 4: "real_parameter",
    5: "reg", 6: "supply0", 7: "supply1", 8: "time", 9: "tri", 10: "triand",
    11: "trior", 12: "trireg", 13: "tri0", 14: "tri1", 15: "wand", 16: "wire",
    17: "wor", 18: "port", 19: "sparray", 20: "realtime", 21: "string",
    22: "bit", 23: "logic", 24: "int", 25: "shortint", 26: "longint",
    27: "byte", 28: "enum", 29: "shortreal",
}

_VAR_DIR = {0: "implicit", 1: "input", 2: "output", 3: "inout", 4: "buffer", 5: "linkage"}

_REG_TYPES = {"reg", "trireg", "logic", "bit", "integer", "int", "shortint", "longint", "byte"}
_PARAM_TYPES = {"parameter", "real_parameter"}


@dataclass
class Signal:
    name: str           # leaf name
    full_path: str
    scope: str          # owning instance path
    length: int         # bit width
    handle: int
    var_type: str       # e.g. wire / reg / logic
    direction: str      # input / output / inout / implicit

    @property
    def category(self) -> str:
        if self.var_type in _PARAM_TYPES:
            return "Parameter"
        if self.direction in ("input", "output", "inout", "buffer", "linkage"):
            return "Port"
        if self.var_type in _REG_TYPES:
            return "Internal-register"
        return "Internal-wire"

    def matches_type(self, flt: Optional[str]) -> bool:
        if not flt:
            return True
        f = flt.strip().lower()
        cand = {self.category.lower(), self.direction.lower(), self.var_type.lower()}
        if self.direction in ("input", "output", "inout", "buffer", "linkage"):
            cand.add("port")
        return f in cand

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "full_path": self.full_path,
            "scope": self.scope,
            "width": self.length,
            "type": self.category,
            "var_type": self.var_type,
            "direction": self.direction,
        }


@dataclass
class Scope:
    full_path: str
    name: str          # instance name
    scope_type: str    # module / interface / generate / ...
    parent: str
    module_name: str = ""   # module *definition* name (FST scope component)
    definition_name: str = ""  # module def resolved from pyslang netlist (if any)
    definition_source: str = ""  # provenance: netlist / inferred / manual / ""
    children: List[str] = field(default_factory=list)

    @property
    def module_type(self) -> str:
        # prefer the netlist-resolved definition name (e.g. real module name),
        # then the FST component, finally the FST scope kind.
        return self.definition_name or self.module_name or self.scope_type


class FstSource:
    """Thread-safe-ish wrapper around a single FST file.

    The hierarchy is parsed once at open and cached. Value queries acquire a
    lock because the underlying fstReader context is single-threaded.
    """

    def __init__(self, fst_path: str):
        self.path = fst_path
        self._lock = threading.RLock()
        self._ctx = lib.fstReaderOpen(fst_path.encode())
        if self._ctx == ffi.NULL:
            raise FileNotFoundError(f"cannot open FST: {fst_path}")
        self.timescale_exp: int = int(lib.fstReaderGetTimescale(self._ctx))
        self.start_time: int = int(lib.fstReaderGetStartTime(self._ctx))
        self.end_time: int = int(lib.fstReaderGetEndTime(self._ctx))
        self.scopes: Dict[str, Scope] = {}
        self.signals: Dict[str, Signal] = {}
        self._by_handle: Dict[int, Signal] = {}
        self._modules: Dict[str, List[str]] = {}  # module name -> instance paths
        self._parse_hierarchy()

    # -- lifecycle ----------------------------------------------------------
    def close(self):
        with self._lock:
            if self._ctx != ffi.NULL:
                lib.fstReaderClose(self._ctx)
                self._ctx = ffi.NULL

    # -- hierarchy ----------------------------------------------------------
    def _parse_hierarchy(self):
        lib.fstReaderIterateHierRewind(self._ctx)
        cur = ""
        stack: List[str] = []
        # ensure a synthetic root exists implicitly via empty parent
        while True:
            h = lib.fstReaderIterateHier(self._ctx)
            if h == ffi.NULL:
                break
            htyp = h.htyp
            if htyp == lib.FST_HT_SCOPE:
                stack.append(cur)
                name = pylibfst.string(h.u.scope.name)
                stype = _SCOPE_TYPE.get(h.u.scope.typ, str(h.u.scope.typ))
                try:
                    component = pylibfst.string(h.u.scope.component)
                except Exception:  # pylint: disable=broad-except
                    component = ""  # FFI field may be absent/undecodable; optional
                parent = cur
                cur = f"{cur}.{name}" if cur else name
                sc = Scope(full_path=cur, name=name, scope_type=stype,
                           parent=parent, module_name=component)
                self.scopes[cur] = sc
                if parent in self.scopes:
                    self.scopes[parent].children.append(cur)
                if component:
                    self._modules.setdefault(component, []).append(cur)
            elif htyp == lib.FST_HT_UPSCOPE:
                cur = stack.pop() if stack else ""
            elif htyp == lib.FST_HT_VAR:
                raw_name = pylibfst.string(h.u.var.name)
                m = _RANGE_SUFFIX.match(raw_name)
                name = m.group(1) if m else raw_name
                full = f"{cur}.{name}" if cur else name
                vtype = _VAR_TYPE.get(h.u.var.typ, str(h.u.var.typ))
                vdir = _VAR_DIR.get(h.u.var.direction, str(h.u.var.direction))
                sig = Signal(
                    name=name, full_path=full, scope=cur,
                    length=int(h.u.var.length), handle=int(h.u.var.handle),
                    var_type=vtype, direction=vdir,
                )
                self.signals[full] = sig
                self._by_handle.setdefault(sig.handle, sig)

    # scope kinds that are structural noise for *design* hierarchy browsing:
    # anonymous procedural blocks emitted by some writers/TB compilations.
    _NOISE_SCOPE_KINDS = {"begin", "fork"}

    def child_instances(self, instance_full_path: str, levels: int = 1,
                        max_scopes: int = 2000,
                        filter_noise: bool = False) -> List[dict]:
        """Return descendant scopes up to ``levels`` deep below the given path.

        ``filter_noise`` (default False for backward-compat) drops anonymous
        ``begin``/``fork`` procedural blocks that are not real instances, matching
        a design-hierarchy view. Interface/generate scopes are kept (they
        are legitimate design elements) but flagged via ``scope_kind`` so callers
        can filter further.
        """
        base = instance_full_path or ""
        base_depth = base.count(".") + (1 if base else 0)
        out: List[dict] = []
        for path, sc in self.scopes.items():
            if base:
                if not (path == base or path.startswith(base + ".")):
                    continue
                if path == base:
                    continue
            depth = path.count(".") + 1
            rel = depth - base_depth
            if rel < 1 or rel > levels:
                continue
            if filter_noise and sc.scope_type in self._NOISE_SCOPE_KINDS:
                continue
            out.append({
                "full_path": path,
                "name": sc.name,
                "module_type": sc.module_type,
                "scope_kind": sc.scope_type,
                "definition_name": sc.definition_name or None,
                "definition_source": sc.definition_source or None,
            })
            if len(out) >= max_scopes:
                break
        return out

    def annotate_definitions(self, resolver, source: str = "netlist",
                             only_empty: bool = False,
                             skip_kinds: Optional[set] = None) -> int:
        """Fill each scope's ``definition_name`` via a resolver callable.

        ``resolver`` maps an instance full path -> module definition name (e.g.
        ``TraceEngine.resolve_module``). ``source`` records provenance
        (``netlist`` / ``inferred`` / ``manual``) on each annotated scope so the
        caller can report confidence. When ``only_empty`` is True, scopes that
        already have a ``definition_name`` are left untouched (used for the
        lower-confidence name-inference pass so it never overrides the netlist).
        ``skip_kinds`` scope types (e.g. ``begin``/``clocking``) are skipped —
        they are procedural/TB noise, not module instances.

        Best-effort: unresolved scopes keep the empty default and fall back to
        FST component / scope kind. Returns the number of scopes annotated.
        """
        n = 0
        for path, sc in self.scopes.items():
            if only_empty and sc.definition_name:
                continue
            if skip_kinds and sc.scope_type in skip_kinds:
                continue
            try:
                dn = resolver(path)
            except Exception:  # pylint: disable=broad-except
                dn = None  # resolver is caller-supplied; treat any failure as unresolved
            if dn:
                sc.definition_name = dn
                sc.definition_source = source
                self._modules.setdefault(dn, [])
                if path not in self._modules[dn]:
                    self._modules[dn].append(path)
                n += 1
        return n

    def apply_definition_map(self, mapping: Dict[str, str], source: str,
                             only_empty: bool = False) -> int:
        """Apply a precomputed ``{full_path: definition_name}`` map with a given
        provenance ``source``. Used for the batch netlist resolution (which
        includes anchor propagation). Returns the count applied."""
        n = 0
        for path, dn in (mapping or {}).items():
            sc = self.scopes.get(path)
            if not sc or not dn:
                continue
            if only_empty and sc.definition_name:
                continue
            sc.definition_name = dn
            sc.definition_source = source
            self._modules.setdefault(dn, [])
            if path not in self._modules[dn]:
                self._modules[dn].append(path)
            n += 1
        return n

    def apply_scope_map(self, scope_map: Dict[str, str]) -> int:
        """Apply a manual ``{full_path: definition_name}`` override map.

        Highest-confidence source: authoritative user-supplied mapping in
        session.json. Overrides any netlist/inferred value. Returns the count
        applied.
        """
        n = 0
        for path, dn in (scope_map or {}).items():
            sc = self.scopes.get(path)
            if sc and dn:
                sc.definition_name = dn
                sc.definition_source = "manual"
                self._modules.setdefault(dn, [])
                if path not in self._modules[dn]:
                    self._modules[dn].append(path)
                n += 1
        return n

    def definition_coverage(self) -> dict:
        """How many *module* scopes have a resolved ``definition_name``.

        Only ``module``-kind scopes are counted as candidates (begin/clocking/
        fork are procedural/TB noise, not module instances). Lets callers report
        whether ``module_type`` is trustworthy and by which source.
        """
        candidates = 0
        resolved = 0
        by_source: Dict[str, int] = {}
        for sc in self.scopes.values():
            if sc.scope_type != "module":
                continue
            candidates += 1
            if sc.definition_name:
                resolved += 1
                src = sc.definition_source or "unknown"
                by_source[src] = by_source.get(src, 0) + 1
        return {
            "module_scopes": candidates,
            "resolved": resolved,
            "unresolved": candidates - resolved,
            "coverage_pct": round(100.0 * resolved / candidates, 1) if candidates else 0.0,
            "by_source": by_source,
        }

    def all_module_names(self, name_contains: Optional[str] = None) -> List[str]:
        """Module *definition* names seen in the design (from FST component)."""
        names = set(self._modules.keys())
        if not names:  # writer didn't emit components -> fall back to scope types
            names = {sc.scope_type for sc in self.scopes.values()}
        out = sorted(names)
        if name_contains:
            out = [n for n in out if name_contains.lower() in n.lower()]
        return out

    def instances_by_module(self, module: str,
                            contains: Optional[str] = None) -> List[str]:
        out = list(self._modules.get(module, []))
        if not out:  # fall back: match by instance/scope name
            out = [p for p, sc in self.scopes.items()
                   if sc.name == module or sc.module_name == module]
        if contains:
            out = [p for p in out if contains in p]
        return out

    def scope_info(self, scope_full_path: str) -> Optional[dict]:
        sc = self.scopes.get(scope_full_path)
        if not sc:
            return None
        return {
            "full_path": sc.full_path,
            "name": sc.name,
            "module_type": sc.module_type,
            "scope_kind": sc.scope_type,
            "definition_name": sc.definition_name or None,
            "definition_source": sc.definition_source or None,
            "parent": sc.parent,
            "num_children": len(sc.children),
        }

    # -- signals ------------------------------------------------------------
    # ordering for signal listings: logic signals first, parameters last, so a
    # small max_signals still surfaces meaningful signals (not just parameters).
    _CATEGORY_ORDER = {"Port": 0, "Internal-register": 1, "Internal-wire": 2,
                       "Parameter": 3}

    # optional callback set by the Session: base_full_path -> declared bit width
    # from the pyslang netlist, used to validate/annotate bus aggregation.
    width_hint = None  # type: Optional[callable]

    def signals_of_instance(self, instance_full_path: str,
                            filter_by_name: Optional[str] = None,
                            filter_by_type: Optional[str] = None,
                            max_signals: int = 2000,
                            aggregate_buses: bool = True,
                            underscore_style: bool = False) -> List[dict]:
        """List signals directly under an instance.

        ``aggregate_buses`` (default True) merges per-element/per-bit VARs that a
        writer split apart (e.g. ``bus [31] ... bus [0]``) back into a single
        bus entry ``bus[N:0]``; the per-element signals remain individually
        queryable via their full paths. Results are ordered ports -> registers
        -> wires -> parameters so a small ``max_signals`` stays useful.

        ``underscore_style`` (default False) additionally coalesces underscore
        bit-split names (``data_7 ... data_0``); off by default because a real
        signal can legitimately end in ``_<n>``. When a netlist width hint is
        available, aggregated widths are validated against the RTL declaration
        (``width_matches_rtl`` flag) to guard against false-positive merges.
        """
        base = instance_full_path or ""
        collected: List[Signal] = []
        for sig in self.signals.values():
            if sig.scope != base:
                continue
            if not sig.matches_type(filter_by_type):
                continue
            collected.append(sig)

        dicts = (self._aggregate_arrays(collected, underscore_style)
                 if aggregate_buses else [s.to_dict() for s in collected])

        if filter_by_name:
            fl = filter_by_name.lower()
            dicts = [d for d in dicts if fl in d["name"].lower()]

        dicts.sort(key=lambda d: (self._CATEGORY_ORDER.get(d.get("type"), 2),
                                  d["name"]))
        return dicts[:max_signals]

    def _rtl_width(self, scope: str, base_name: str) -> Optional[int]:
        """Best-effort declared bit width for ``scope.base_name`` from netlist."""
        if not self.width_hint:
            return None
        full = f"{scope}.{base_name}" if scope else base_name
        try:
            w = self.width_hint(full)
            return int(w) if w else None
        except Exception:  # pylint: disable=broad-except
            return None  # width hint is caller-supplied; any failure -> no hint

    def _aggregate_arrays(self, sigs: List["Signal"],
                          underscore_style: bool = False) -> List[dict]:
        """Merge split element VARs into one ``base[hi:lo]`` bus dict.

        Handles two split styles:
          * bracket: ``base [idx]`` / ``base[idx]``  (always on)
          * underscore: ``base_idx``                 (only if underscore_style)

        Non-array signals pass through unchanged. Aggregation needs >=2 elements
        sharing a base (a lone ``x[0]`` stays as-is). When a netlist width hint
        exists, the merged width is checked against the RTL declaration:
          * match  -> use it, ``width_matches_rtl: true``
          * differ -> keep computed width but flag ``width_matches_rtl: false``
                      (helps catch false merges from unequal-width fragments)
        """
        buckets: Dict[str, List[Tuple[int, "Signal", str]]] = {}
        passthrough: List[dict] = []
        order: List[str] = []
        for sig in sigs:
            m = _ELEM_INDEX.match(sig.name)
            style = "bracket"
            if not m and underscore_style:
                m = _ELEM_USCORE.match(sig.name)
                style = "underscore"
            if not m:
                passthrough.append(sig.to_dict())
                continue
            base_name, idx = m.group(1), int(m.group(2))
            if base_name not in buckets:
                buckets[base_name] = []
                order.append(base_name)
            buckets[base_name].append((idx, sig, style))

        merged: List[dict] = []
        for base_name in order:
            elems = buckets[base_name]
            if len(elems) < 2:
                # not a real split: restore the single element unchanged. If it
                # looks like "flag[0]" with no siblings, flag that it may be a
                # 1-bit slice of a wider bus that only dumped one bit (edge 6).
                d = elems[0][1].to_dict()
                if elems[0][2] == "bracket" and elems[0][0] == 0 \
                        and _ELEM_INDEX.match(elems[0][1].name):
                    d["maybe_scalar"] = True
                passthrough.append(d)
                continue
            idxs = sorted(i for i, _, _ in elems)
            hi, lo = idxs[-1], idxs[0]
            # sparse detection (edge 2): indices with gaps mean hi-lo+1 overstates
            # the real width. Report the present bits and mark sparse.
            span = hi - lo + 1
            sparse = (len(idxs) != span)
            first = min(elems, key=lambda e: e[0])[1]
            style = elems[0][2]
            base_scope = first.scope
            # detect unequal element widths (a sign of packed-slice fragments,
            # e.g. data[7:4] + data[3:0]) — do not blindly sum those.
            widths = {(s.length or 1) for _, s, _ in elems}
            elem_w = first.length or 1
            uniform = len(widths) == 1
            if uniform and elem_w > 1:
                total_w = elem_w * len(elems)   # unpacked array of vectors
            elif uniform:
                total_w = hi - lo + 1           # simple bit split
            else:
                total_w = sum(s.length or 1 for _, s, _ in elems)  # mixed slices
            entry = {
                "name": f"{base_name}[{hi}:{lo}]",
                "full_path": f"{base_scope}.{base_name}" if base_scope else base_name,
                "scope": base_scope,
                "width": total_w,
                "type": first.category,
                "var_type": first.var_type,
                "direction": first.direction,
                "element_count": len(elems),
                "split_style": style,
            }
            if not uniform:
                entry["uneven_elements"] = True
            if sparse:
                # gaps present: width from hi:lo is nominal, not all bits exist
                entry["sparse"] = True
                entry["present_bits"] = idxs
                entry["note"] = ("non-contiguous bit indices; width is the nominal "
                                 "[hi:lo] span, only present_bits actually exist")
            rtl_w = self._rtl_width(base_scope, base_name)
            if rtl_w is not None:
                entry["rtl_width"] = rtl_w
                # Unpacked array: RTL (pyslang) reports the per-element bit
                # width, not the total array bit width.  When FST elements are
                # uniform and each matches the RTL element width, the
                # aggregation is correct even though total_w != rtl_w.
                # When they genuinely differ (e.g. enum bit-width disagreement
                # between pyslang and the simulator), keep the FST-computed
                # total width — it reflects what the waveform actually contains.
                if uniform and rtl_w == elem_w:
                    entry["width_matches_rtl"] = True
                else:
                    entry["width_matches_rtl"] = (rtl_w == total_w)
                    if rtl_w != total_w:
                        # Do NOT override width with rtl_w: the FST-computed
                        # total is what the waveform actually holds.  Just flag
                        # the discrepancy so callers know.
                        entry["width_discrepancy"] = (
                            f"FST total={total_w} ({len(elems)}x{elem_w}-bit) vs "
                            f"RTL declared={rtl_w}; keeping FST width")
            merged.append(entry)
        return passthrough + merged

    def signal_info(self, full_path: str) -> Optional[dict]:
        sig = self.signals.get(full_path)
        if not sig:
            # aggregated bus name (e.g. "...bus[15:0]"): resolve to the base
            # signal and report its aggregated width from element signals.
            elems = self._element_signals(full_path)
            if not elems:
                return None
            # use the first element's metadata; width is the sum of all elements
            first = elems[0]
            total_w = sum(s.length for s in elems)
            d = first.to_dict()
            d["width"] = total_w
            d["msb"] = total_w - 1 if total_w > 1 else 0
            d["lsb"] = 0
            d["aggregated_from"] = len(elems)
            return d
        d = sig.to_dict()
        d.update({
            "msb": sig.length - 1 if sig.length > 1 else 0,
            "lsb": 0,
        })
        return d

    def find_signals(self, name_contains: str, limit: int = 200) -> List[str]:
        nc = name_contains.lower()
        out = [p for p in self.signals if nc in p.lower()]
        return out[:limit]

    # -- values -------------------------------------------------------------
    @staticmethod
    def _decode(buf) -> str:
        if buf == ffi.NULL:
            return ""
        s = ffi.string(buf).decode("latin-1")
        # fstReaderGetValueFromHandleAtTime returns reals as "r%.16g"
        # (fstapi convention). Strip the 'r' prefix so real signals surface
        # a plain number string; ordinary 0/1/x/z values never start with
        # 'r', so this is unambiguous.
        if s.startswith("r"):
            s = s[1:]
        return s

    @staticmethod
    def _to_hex(binval: str) -> Optional[str]:
        if not binval or any(c not in "01" for c in binval):
            return None
        return f"{int(binval, 2):x}"

    def value_at(self, full_path: str, time_units: int) -> Optional[dict]:
        sig = self.signals.get(full_path)
        if sig is None:
            # aggregated bus name (e.g. "...bus[15:0]") or unpacked-array root
            # ("...bus") returned by signals_of_instance: reconstruct by
            # concatenating the per-element values (MSB index first).
            return self._value_at_aggregated(full_path, time_units)
        with self._lock:
            buf = ffi.new("char[]", sig.length + 64)
            res = lib.fstReaderGetValueFromHandleAtTime(
                self._ctx, time_units, sig.handle, buf)
            val = self._decode(res) if res != ffi.NULL else ""
        return {
            "time": timeutil.format_fst_time(time_units, self.timescale_exp),
            "time_units": time_units,
            "value": val,
            "hex": self._to_hex(val),
        }

    def _element_signals(self, full_path: str) -> List["Signal"]:
        """Resolve an aggregated/array full_path to its per-element Signals.

        Accepts either ``scope.base[hi:lo]`` (aggregated form) or ``scope.base``
        (array root); returns the element signals sorted MSB-index-first, or []
        if the path does not correspond to a split array.
        """
        base = full_path
        m = _RANGE_SUFFIX.match(full_path)  # strip trailing [hi:lo] if present
        if m:
            base = m.group(1)
        elems: List[Tuple[int, "Signal"]] = []
        for fp, s in self.signals.items():
            mm = _ELEM_INDEX.match(fp)
            if not mm:
                continue
            eb = mm.group(1)
            if eb == base:
                elems.append((int(mm.group(2)), s))
        elems.sort(key=lambda e: e[0], reverse=True)  # MSB first
        return [s for _, s in elems]

    def _value_at_aggregated(self, full_path: str,
                             time_units: int) -> Optional[dict]:
        elems = self._element_signals(full_path)
        if not elems:
            return None
        parts: List[str] = []
        with self._lock:
            for s in elems:
                res = lib.fstReaderGetValueFromHandleAtTime(
                    self._ctx, time_units, s.handle, ffi.new("char[]", s.length + 64))
                parts.append(self._decode(res) if res != ffi.NULL else "")
        val = "".join(parts)
        return {
            "time": timeutil.format_fst_time(time_units, self.timescale_exp),
            "time_units": time_units,
            "value": val,
            "hex": self._to_hex(val),
            "aggregated_from": len(elems),
        }

    def _values_between_aggregated(self, full_path: str,
                                    start_units: int, end_units: int,
                                    max_values: int) -> Optional[List[dict]]:
        """Value-over-time for an aggregated bus (e.g. ``bus[15:0]``).

        Collects each element signal's value timeline, merges by timestamp
        (MSB-first concatenation), and returns the combined timeline.
        """
        elems = self._element_signals(full_path)
        if not elems:
            return None
        # collect per-element timelines
        per_elem: List[Dict[int, str]] = []
        for s in elems:
            rows = self._iter_values(s, start_units, end_units, max_values)
            per_elem.append({t: v for t, v in rows})
        # merge timestamps: union of all element timestamps, sorted
        all_ts = sorted(set().union(*[set(d.keys()) for d in per_elem]))
        if not all_ts:
            return []
        # also include start and end timestamps so the range is covered even if
        # no element changed exactly at those points — fetch point values.
        for boundary in (start_units, end_units):
            if boundary not in all_ts:
                # insert boundary with point values from each element
                all_ts.append(boundary)
        all_ts = sorted(set(all_ts))
        # build merged rows; carry forward last known value per element
        last_vals: List[str] = [""] * len(elems)
        out: List[dict] = []
        for t in all_ts:
            if t < start_units or t > end_units:
                continue
            parts: List[str] = []
            for i, d in enumerate(per_elem):
                if t in d:
                    last_vals[i] = d[t]
                parts.append(last_vals[i])
            val = "".join(parts)
            out.append({
                "time": timeutil.format_fst_time(t, self.timescale_exp),
                "time_units": t,
                "value": val,
                "hex": self._to_hex(val),
                "aggregated_from": len(elems),
            })
            if len(out) >= max_values:
                break
        return out

    def _iter_values(self, sig: Signal, start: int, end: int,
                     max_values: int) -> List[Tuple[int, str]]:
        collected: List[Tuple[int, str]] = []

        def cb(_data, time, facidx, value):
            t = int(time)
            if t < start or t > end:
                return  # precise filter (SetLimitTimeRange is only block-coarse)
            if len(collected) < max_values:
                collected.append((t, self._decode(value)))

        with self._lock:
            lib.fstReaderClrFacProcessMaskAll(self._ctx)
            lib.fstReaderSetFacProcessMask(self._ctx, sig.handle)
            lib.fstReaderSetLimitTimeRange(self._ctx, start, end)
            pylibfst.fstReaderIterBlocks2(self._ctx, cb, cb, None, ffi.NULL)
            # reset time range to full for subsequent queries
            lib.fstReaderSetLimitTimeRange(self._ctx, self.start_time, self.end_time)
            lib.fstReaderClrFacProcessMaskAll(self._ctx)
        return collected

    def values_between(self, full_path: str, start_units: int, end_units: int,
                       max_values: int = 5000) -> Optional[List[dict]]:
        sig = self.signals.get(full_path)
        if not sig:
            # aggregated bus name (e.g. "...bus[15:0]"): iterate all element
            # signals and merge their value timelines (MSB-first concatenation
            # at each timestamp). This mirrors value_at's aggregation approach.
            return self._values_between_aggregated(full_path, start_units,
                                                    end_units, max_values)
        rows = self._iter_values(sig, start_units, end_units, max_values)
        return [
            {
                "time": timeutil.format_fst_time(t, self.timescale_exp),
                "time_units": t,
                "value": v,
                "hex": self._to_hex(v),
            }
            for t, v in rows
        ]

    def all_values(self, full_path: str, max_values: int = 1000) -> Optional[List[dict]]:
        return self.values_between(full_path, self.start_time, self.end_time, max_values)
