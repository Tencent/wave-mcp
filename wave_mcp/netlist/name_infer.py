"""Instance-name -> module-definition inference (netlist-independent fallback).

Why this exists
---------------
xrun dumps VCD with ``$scope module <instance-name> $end`` — the *instance*
name only, never the module *definition* name. So the FST ``component`` field is
empty and ``module_type`` degrades to the generic ``"module"``. The accurate
source is the pyslang netlist (elaboration), but on real UVM designs elaboration
is often *partial* (missing precompiled ``uvm_pkg`` / include dirs), leaving many
scopes with ``definition_name = null``.

This module recovers ``definition_name`` from a *naming convention* that holds in
practice (verified against a real decode_tb dump)::

    U_DECODE                        -> decode        (strip U_ prefix, exact)
    u_decode_unit                   -> decode_unit   (strip u_ prefix, exact)
    u_dffr_dec_warp_sche_vld_p1_o   -> dffr          (longest boundary-prefix)

The set of known module *definition* names is obtained by a cheap regex scan of
the source files (``module <name>``) — it does NOT require elaboration to
succeed, so it works exactly when the netlist is missing or partial.

The result is a best-effort, clearly-labelled fallback (``definition_source =
"inferred"``); the netlist (``"netlist"``) and a manual ``scope_map``
(``"manual"``) always win over it.
"""
from __future__ import annotations

import os
import re
from typing import Callable, Dict, Iterable, Optional, Set

# module / macromodule / primitive / interface declarations.
# Interfaces ARE included (2026-08-31): UVM tb filelists (e.g. a DUT-only
# rtl.f) declare them and their instances show up in the FST scope tree, so
# without them the exact-match tiers can never resolve e.g. ``u_clk_rst_if``
# -> ``clk_rst_if``. ``interface class`` is excluded from the capture via the
# lookahead (it is a class definition, not a scope-producing declaration).
# packages / programs stay excluded: packages are never instantiated as
# hierarchy scopes, and program instances are rare enough to decide later.
_MODULE_RE = re.compile(
    r"^\s*(?:module|macromodule|interface)(?!_)\s+(?!class\b)([A-Za-z_]\w*)",
    re.MULTILINE)

# instance-name prefixes commonly used in RTL/TB coding styles. Stripped before
# matching so ``U_DECODE`` / ``u_decode`` both resolve to ``decode``.
_INST_PREFIX_RE = re.compile(r"^(?:u|i|g|gen|inst|the)_", re.IGNORECASE)

# SV *interface* instance naming — these are NOT module instances, so the
# boundary-prefix heuristic must not force them onto a module (the #1 source of
# false positives: ``u_decode_tb__clk_rst_if`` was wrongly -> ``decode``).
# Exact/strip-exact tiers are unaffected (they only match a real declaration).
_INTERFACE_SUFFIX_RE = re.compile(r"(?:_if|_vif|_intf|_bfm|_agent_interface)$",
                                  re.IGNORECASE)

# don't infer from too-short module names (e.g. a 1-2 char name would match far
# too many instance names by prefix and produce noise).
_MIN_MODULE_LEN = 3


def extract_module_names(files: Iterable[str]) -> Set[str]:
    """Regex-scan source files for ``module <name>`` declarations.

    Cheap and elaboration-independent: reads each file once. Returns the set of
    declared module definition names (original casing preserved).
    """
    names: Set[str] = set()
    for f in files or []:
        if not f or not os.path.exists(f):
            continue
        try:
            with open(f, "r", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for m in _MODULE_RE.finditer(text):
            names.add(m.group(1))
    return names


def _strip_prefix(name: str) -> str:
    return _INST_PREFIX_RE.sub("", name, count=1)


def infer_definition(instance_name: str, known: Dict[str, str],
                     allow_prefix: bool = True) -> Optional[str]:
    """Infer a module definition name for one instance name.

    ``known`` maps lower-cased module name -> original-cased module name.

    Resolution tiers (most to least confident); returns the original-cased
    module name or ``None`` when no confident match is found:
      1. exact match on the raw instance name (e.g. ``tc_if`` -> ``tc_if``)
      2. exact match after stripping a ``u_``/``i_``/... prefix
         (``U_DECODE`` -> ``decode``, ``u_decode_unit`` -> ``decode_unit``)
      3. [only if ``allow_prefix``] longest module name that is a ``_``-boundary
         prefix of the stripped instance name (``u_dffr_dec_...`` -> ``dffr``).
         Ties are rejected as ambiguous.

    Tiers 1-2 are equality against names actually declared in the source, so
    they are safe. Tier 3 is a heuristic for irregular leaf-cell naming and is
    gated by ``allow_prefix`` so callers can keep it out of high-confidence
    results.
    """
    if not instance_name or not known:
        return None
    raw = instance_name.lower()
    if raw in known:
        return known[raw]
    stripped = _strip_prefix(instance_name).lower()
    if not stripped:
        return None
    if stripped in known:
        return known[stripped]
    # Xcelium concatenated scope names: ``u_eci2apb__apb_slave_if`` is the
    # instance ``u_eci2apb`` bound to interface ``apb_slave_if``. The segment
    # after the last ``__`` is a real declared name, so exact match on it is
    # declaration-backed (same confidence as tiers 1-2). Measured gap
    # 2026-08-31: interface scopes like ``top_tb.u_eci2apb__apb_slave_if``
    # stayed ``definition_name = null`` without this.
    if "__" in instance_name:
        tail = instance_name.rsplit("__", 1)[-1]
        tl = tail.lower()
        if tl in known:
            return known[tl]
        tl = _strip_prefix(tail).lower()
        if tl and tl in known:
            return known[tl]
    if not allow_prefix:
        return None
    # interface instances (``*_if`` / ``*_vif`` / ...) are not module instances:
    # never let the boundary-prefix heuristic force them onto a module name.
    if _INTERFACE_SUFFIX_RE.search(instance_name):
        return None
    # tier 3: longest boundary-prefix match
    best: Optional[str] = None
    best_len = 0
    ambiguous = False
    for lname, orig in known.items():
        if len(lname) < _MIN_MODULE_LEN or len(lname) >= len(stripped):
            continue
        # boundary: module name followed by '_' in the stripped instance name
        if stripped.startswith(lname) and stripped[len(lname)] == "_":
            if len(lname) > best_len:
                best, best_len, ambiguous = orig, len(lname), False
            elif len(lname) == best_len and orig != best:
                ambiguous = True
    if best and not ambiguous:
        return best
    return None


def make_name_resolver(module_names: Iterable[str],
                       allow_prefix: bool = True) -> Callable[[str], Optional[str]]:
    """Build a ``full_path -> definition_name`` resolver from module names.

    The returned callable takes an FST scope's full hierarchical path, uses its
    leaf (instance) name, and applies :func:`infer_definition`. ``allow_prefix``
    toggles the low-confidence boundary-prefix tier so the caller can run a
    high-confidence (exact-only) pass separately from a heuristic pass.
    """
    known: Dict[str, str] = {}
    for n in module_names or []:
        if n:
            known.setdefault(n.lower(), n)

    def _resolve(full_path: str) -> Optional[str]:
        leaf = full_path.rsplit(".", 1)[-1] if full_path else full_path
        return infer_definition(leaf, known, allow_prefix=allow_prefix)

    return _resolve
