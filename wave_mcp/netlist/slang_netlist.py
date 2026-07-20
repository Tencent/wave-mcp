"""Build a fully-elaborated static netlist from pyslang (slang SV front-end).

Single, complete backend for Indago categories 5 / 6 — no Surelog/UHDM, no
Verible. pyslang performs full elaboration (parameters, generate, interfaces,
packages), so the extracted maps are accurate, and it ships as a pip wheel
(portable to the air-gapped network).

Output maps (persisted as JSON, loaded by RtlSource):
  modules[def] = {
      file, line, ports{}, signals{},
      drivers{ lhs: [ {kind, rhs[], control[], file, line, snippet} ] },
      fanin{ sig: [sig...] },        # signals affecting sig
      loads{ sig: [sig...] },        # signals sig affects (fan-out)
      loc{ sig: {file, line} },
      instances[ {def, name, conns{port: signal}, line} ],
  }
  instance_tree{ hierarchical_path: def }   # from elaboration (for scope->module)
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional

# This module intentionally uses broad ``except Exception`` at many points: its
# whole job is to extract as much of the netlist as possible from a possibly
# broken/partial pyslang elaboration, where a single failing member (SVA,
# unresolved dep, FFI quirk) must never abort the whole build. Each site is a
# deliberate graceful-degradation boundary, so broad-except is disabled here.
# pylint: disable=broad-except

try:
    import pyslang as ps
    from pyslang.syntax import SyntaxTree
    from pyslang.ast import Compilation
    _PYSLANG_OK = True
except ImportError:  # pragma: no cover
    _PYSLANG_OK = False


class NetlistError(RuntimeError):
    pass


_DIR_MAP = {"In": "input", "Out": "output", "InOut": "inout", "Ref": "ref"}


def serialize_expr(e) -> dict:
    """Serialize a pyslang expression into a small dict tree for expr_eval."""
    try:
        t = type(e).__name__
        if t == "NamedValueExpression":
            return {"k": "sig", "name": e.symbol.name}
        if t == "IntegerLiteral":
            return {"k": "const", "lit": str(e.value)}
        if t == "ConversionExpression":
            return serialize_expr(e.operand)
        if t == "UnaryExpression":
            return {"k": "un", "op": str(e.op).split(".")[-1].split(":")[0],
                    "a": serialize_expr(e.operand)}
        if t == "BinaryExpression":
            return {"k": "bin", "op": str(e.op).split(".")[-1].split(":")[0],
                    "l": serialize_expr(e.left), "r": serialize_expr(e.right)}
    except Exception:
        pass
    return {"k": "unknown"}


def _and_all(nodes: List[dict]) -> dict:
    if not nodes:
        return {"k": "unknown"}
    node = nodes[0]
    for n in nodes[1:]:
        node = {"k": "bin", "op": "LogicalAnd", "l": node, "r": n}
    return node


def _lvalue_paths(node) -> List[str]:
    """Resolve an l-value expression into field/bit-qualified signal path(s).

    Unlike `_named_values` (which collapses everything to the root symbol name),
    this keeps the struct-member / bit-select suffix so that distinct fields of a
    packed struct port (e.g. ``tl_h_o.a_ready`` vs ``tl_h_o.d_valid``) become
    distinct driver targets instead of aggregating onto the root ``tl_h_o``.

    Concatenations on the LHS yield one path per concatenated l-value. Returns an
    empty list when no resolvable l-value is found.
    """
    if node is None:
        return []
    t = type(node).__name__
    if t == "NamedValueExpression":
        try:
            return [node.symbol.name]
        except Exception:
            return []
    if t == "MemberAccessExpression":
        base = _lvalue_paths(getattr(node, "value", None))
        mname = getattr(getattr(node, "member", None), "name", None)
        if base and mname:
            return [f"{base[0]}.{mname}"]
        return base
    if t in ("ElementSelectExpression", "RangeSelectExpression"):
        # keep the base path; bit/range index is dynamic-value dependent and we
        # model at the signal/field granularity (not per-bit) for the driver map.
        return _lvalue_paths(getattr(node, "value", None))
    if t == "ConversionExpression":
        return _lvalue_paths(getattr(node, "operand", None))
    if t == "ConcatenationExpression":
        out: List[str] = []
        try:
            for op in node.operands:
                out.extend(_lvalue_paths(op))
        except Exception:
            pass
        return out
    # fallback: collect root names underneath (e.g. assignment-pattern targets)
    return _named_values(node)


def _named_values(node) -> List[str]:
    """Collect referenced signal names under an expression / timing / statement."""
    out: List[str] = []
    if node is None:
        return out

    def cb(n):
        if type(n).__name__ == "NamedValueExpression":
            try:
                out.append(n.symbol.name)
            except Exception:
                pass
    try:
        node.visit(cb)
    except Exception:
        pass
    # de-dup preserve order
    seen = set()
    res = []
    for x in out:
        if x not in seen:
            seen.add(x)
            res.append(x)
    return res


class _Loc:
    def __init__(self, sm):
        self.sm = sm
        self._src_cache: Dict[str, List[str]] = {}

    def of(self, location):
        try:
            f = self.sm.getFileName(location)
            ln = self.sm.getLineNumber(location)
            return f, ln
        except Exception:
            return None, None

    def snippet(self, f, ln):
        if not f or not ln:
            return ""
        if f not in self._src_cache:
            try:
                with open(f, "r", errors="replace") as fh:
                    self._src_cache[f] = fh.read().splitlines()
            except OSError:
                self._src_cache[f] = []
        lines = self._src_cache[f]
        if 1 <= ln <= len(lines):
            return lines[ln - 1].strip()
        return ""


def _width(sym) -> int:
    try:
        t = sym.type
        w = getattr(t, "bitWidth", None)
        if w:
            return int(w)
    except Exception:
        pass
    return 1


class _ModuleBuilder:
    def __init__(self, name: str, loc: _Loc):
        self.name = name
        self.loc = loc
        self.file = None
        self.line = None
        self.ports: Dict[str, dict] = {}
        self.signals: Dict[str, dict] = {}
        self.drivers: Dict[str, List[dict]] = {}
        self.instances: List[dict] = []
        self.skipped_members = 0
        self._done = False

    def set_self_loc(self, sym):
        if self.file is None:
            self.file, self.line = self.loc.of(sym.location)

    def add_port(self, sym):
        direction = _DIR_MAP.get(str(getattr(sym, "direction", "")).split(".")[-1], "implicit")
        f, ln = self.loc.of(sym.location)
        self.ports[sym.name] = {"direction": direction, "width": _width(sym),
                                "type": "port", "line": ln, "file": f}

    def add_signal(self, sym, kind: str):
        if sym.name in self.signals:
            return
        f, ln = self.loc.of(sym.location)
        self.signals[sym.name] = {"kind": kind, "width": _width(sym), "line": ln, "file": f}

    def add_driver(self, lhs: str, kind: str, rhs: List[str], control: List[str],
                   location, guard: Optional[List[dict]] = None,
                   port_ref: Optional[dict] = None):
        f, ln = self.loc.of(location)
        rec = {"kind": kind, "rhs": sorted(set(rhs)), "control": sorted(set(control)),
               "guard": guard or [], "file": f, "line": ln,
               "snippet": self.loc.snippet(f, ln)}
        if port_ref is not None:
            rec["port_ref"] = port_ref
        self.drivers.setdefault(lhs, []).append(rec)

    def finalize(self) -> dict:
        fanin: Dict[str, List[str]] = {}
        loads: Dict[str, List[str]] = {}
        for lhs, recs in self.drivers.items():
            aff = set()
            for r in recs:
                aff.update(r["rhs"])
                aff.update(r["control"])
            fanin[lhs] = sorted(aff)
            for s in aff:
                loads.setdefault(s, [])
                if lhs not in loads[s]:
                    loads[s].append(lhs)
        loc = {}
        for n, d in {**self.ports, **self.signals}.items():
            loc[n] = {"file": d.get("file"), "line": d.get("line")}
        return {"name": self.name, "file": self.file, "line": self.line,
                "ports": self.ports, "signals": self.signals, "drivers": self.drivers,
                "fanin": fanin, "loads": loads, "loc": loc, "instances": self.instances,
                "skipped_members": self.skipped_members}


def _walk_statement(stmt, control: List[str], guard: List[dict], mb: _ModuleBuilder):
    """Recursively walk a procedural statement tree tracking control signals and
    the branch-guard conditions that must hold to reach each assignment."""
    if stmt is None:
        return
    t = type(stmt).__name__
    if t == "TimedStatement":
        ctl = control + _named_values(getattr(stmt, "timing", None))
        _walk_statement(getattr(stmt, "stmt", None), ctl, guard, mb)
        return
    if t == "BlockStatement":
        body = getattr(stmt, "body", None)
        items = []
        if body is not None:
            try:
                items = list(body)
            except TypeError:
                items = [body]
        for s in items:
            _walk_statement(s, control, guard, mb)
        return
    if t == "ConditionalStatement":
        cond_sigs: List[str] = []
        cond_nodes: List[dict] = []
        for c in getattr(stmt, "conditions", []) or []:
            ce = getattr(c, "expr", c)
            cond_sigs += _named_values(ce)
            cond_nodes.append(serialize_expr(ce))
        ctl = control + cond_sigs
        cond = _and_all(cond_nodes)
        _walk_statement(getattr(stmt, "ifTrue", None), ctl,
                        guard + [{"cond": cond, "expect": 1}], mb)
        _walk_statement(getattr(stmt, "ifFalse", None), ctl,
                        guard + [{"cond": cond, "expect": 0}], mb)
        return
    if t == "CaseStatement":
        case_expr = getattr(stmt, "expr", None)
        cond_sigs = _named_values(case_expr)
        ctl = control + cond_sigs
        case_node = serialize_expr(case_expr) if case_expr is not None else {"k": "unknown"}
        for item in getattr(stmt, "items", []) or []:
            labels = getattr(item, "expressions", None) or []
            eqs = []
            for lab in labels:
                eqs.append({"k": "bin", "op": "Equality", "l": case_node,
                            "r": serialize_expr(lab)})
            gitem = guard + ([{"cond": _or_all(eqs), "expect": 1}] if eqs else [])
            _walk_statement(getattr(item, "stmt", item), ctl, gitem, mb)
        _walk_statement(getattr(stmt, "defaultCase", None), ctl, guard, mb)
        return
    if t == "ExpressionStatement":
        e = getattr(stmt, "expr", None)
        if e is not None and type(e).__name__ == "AssignmentExpression":
            _record_assignment(e, control, guard, mb)
        return
    if t in ("ForLoopStatement", "ForeverStatement", "WhileLoopStatement",
             "RepeatLoopStatement"):
        _walk_statement(getattr(stmt, "body", None), control, guard, mb)
        return

    # fallback: flat-collect assignments under unknown statement, with given control
    def cb(n):
        if type(n).__name__ == "AssignmentExpression":
            _record_assignment(n, control, guard, mb)
    try:
        stmt.visit(cb)
    except Exception:
        pass


def _or_all(nodes: List[dict]) -> dict:
    if not nodes:
        return {"k": "unknown"}
    node = nodes[0]
    for n in nodes[1:]:
        node = {"k": "bin", "op": "LogicalOr", "l": node, "r": n}
    return node


def _driver_targets(expr) -> List[str]:
    """Field-qualified driver target(s) for an instance output port connection.

    Output-port connections come through as an ``AssignmentExpression`` (port
    drives the connected net) — its ``.left`` is the driven l-value. Otherwise
    treat the whole expression as the l-value.
    """
    if expr is None:
        return []
    if type(expr).__name__ == "AssignmentExpression":
        return _lvalue_paths(getattr(expr, "left", None))
    return _lvalue_paths(expr)


def _record_assignment(assign_expr, control: List[str], guard: List[dict],
                       mb: _ModuleBuilder):
    # Guard against InvalidExpression (a sub-expression failed to elaborate,
    # common in large designs with partial deps / SVA) which lacks .left/.right.
    if type(assign_expr).__name__ != "AssignmentExpression" or \
            not hasattr(assign_expr, "left"):
        return
    lhs = _lvalue_paths(assign_expr.left)
    rhs = _named_values(assign_expr.right)
    kind = "nonblocking" if getattr(assign_expr, "isNonBlocking", False) else "blocking"
    loc = assign_expr.sourceRange.start
    for l in (lhs or [None]):
        if l is None:
            continue
        mb.add_driver(l, kind, rhs, control, loc, guard)


def _process_member(m, mb: _ModuleBuilder, builders, loc: _Loc, prefix: str,
                    instance_tree: Dict[str, str]):
    """Process a single body member into the module builder.

    Shared by the module body and by generate blocks so that logic inside
    if-/for-generate (very common in OpenTitan prim_*) is not lost."""
    tn = type(m).__name__
    try:
        if tn == "PortSymbol":
            mb.add_port(m)
        elif tn == "NetSymbol":
            mb.add_signal(m, "net")
        elif tn == "VariableSymbol":
            mb.add_signal(m, "variable")
        elif tn == "ContinuousAssignSymbol":
            _record_assignment_continuous(getattr(m, "assignment", None), mb)
        elif tn == "ProceduralBlockSymbol":
            _walk_statement(getattr(m, "body", None), [], [], mb)
        elif tn == "InstanceSymbol":
            _record_instance(m, mb, builders, loc, prefix, instance_tree)
        elif tn in ("GenerateBlockSymbol", "GenerateBlockArraySymbol"):
            _process_generate(m, mb, builders, loc, prefix, instance_tree)
    except Exception:
        # A single member that fails to elaborate (partial deps, SVA, etc.)
        # must not abort extraction of the whole module. Count it as a skip.
        mb.skipped_members += 1


def _process_generate(gen, mb: _ModuleBuilder, builders, loc: _Loc, prefix: str,
                      instance_tree: Dict[str, str]):
    """Descend into a generate block / generate-block-array.

    * GenerateBlockArraySymbol (for-generate): iterate its entries.
    * GenerateBlockSymbol (if-/case-generate): one block; skip the branch that
      elaboration did not select (isUninstantiated) so we only keep live logic.
    """
    tn = type(gen).__name__
    if tn == "GenerateBlockArraySymbol":
        entries = getattr(gen, "entries", None)
        if entries is None:
            try:
                entries = list(gen)
            except TypeError:
                entries = []
        for blk in entries:
            _process_generate(blk, mb, builders, loc, prefix, instance_tree)
        return
    # GenerateBlockSymbol: skip unselected branch of if/case-generate
    if getattr(gen, "isUninstantiated", False):
        return
    members = []
    try:
        members = list(gen)
    except TypeError:
        members = list(getattr(gen, "members", []) or [])
    for m in members:
        _process_member(m, mb, builders, loc, prefix, instance_tree)


def _process_body(inst, mb: _ModuleBuilder, builders, loc: _Loc, prefix: str,
                  instance_tree: Dict[str, str]):
    body = inst.body
    mb.set_self_loc(body if hasattr(body, "location") else inst)
    for m in body:
        _process_member(m, mb, builders, loc, prefix, instance_tree)


def _record_assignment_continuous(a, mb: _ModuleBuilder):
    # A continuous-assign whose RHS/LHS failed to elaborate surfaces as an
    # InvalidExpression (no .left/.right). Skip it rather than crash so a single
    # bad assign in a large module doesn't abort the whole netlist build.
    if type(a).__name__ != "AssignmentExpression" or not hasattr(a, "left"):
        return
    lhs = _lvalue_paths(a.left)
    rhs = _named_values(a.right)
    loc = a.sourceRange.start
    for l in (lhs or [None]):
        if l is None:
            continue
        mb.add_driver(l, "assign", rhs, [], loc)


def _record_instance(inst, parent_mb, builders, loc, prefix, instance_tree):
    dname = _def_name(inst)
    iname = inst.name
    conns = {}
    conn_dirs: Dict[str, str] = {}
    _f, ln = loc.of(inst.location)
    try:
        for pc in inst.portConnections:
            port = getattr(pc, "port", None)
            pname = getattr(port, "name", None)
            if not pname:
                continue
            direction = _DIR_MAP.get(
                str(getattr(port, "direction", "")).split(".")[-1], "implicit")
            expr = getattr(pc, "expression", None)
            sigs = _named_values(expr)
            conns[pname] = sigs[0] if sigs else None
            conn_dirs[pname] = direction
            # Plan-1: an instance OUTPUT (or inout) port drives the external net it
            # is connected to. Register that as a driver of the external signal so
            # that pure-structural / pure-wiring modules (e.g. tlul_fifo_sync) and
            # cross-module trace can follow "who drives this" into the sub-instance.
            #
            # Field-level: when the connection targets a packed-struct field
            # (e.g. ``.wready_o(tl_h_o.a_ready)``), pyslang models it as an
            # AssignmentExpression whose .left is the field l-value. Use the
            # field-qualified path as the driver target so distinct fields of the
            # same struct port are not over-aggregated onto the root signal.
            if direction in ("output", "inout"):
                ext_paths = _driver_targets(expr)
                for ext in (ext_paths or sigs):
                    parent_mb.add_driver(
                        ext, "instance_port", rhs=[], control=[],
                        location=inst.location,
                        port_ref={"instance": iname, "def": dname,
                                  "port": pname, "direction": direction})
    except Exception:
        pass
    parent_mb.instances.append({"def": dname, "name": iname, "conns": conns,
                                "conn_dirs": conn_dirs, "line": ln})
    path = f"{prefix}.{iname}" if prefix else iname
    instance_tree[path] = dname
    # recurse into the child module definition (once)
    if dname not in builders:
        child = _ModuleBuilder(dname, loc)
        builders[dname] = child
        _process_body(inst, child, builders, loc, path, instance_tree)


def _def_name(inst) -> str:
    d = getattr(inst, "definition", None)
    if d is not None and hasattr(d, "name"):
        return d.name
    b = getattr(inst, "body", None)
    return getattr(b, "name", None) or inst.name


# diagnostic codes we translate into actionable, user-facing guidance so the
# caller knows *what to add* (incdir / define / missing package source) instead
# of just seeing "elaboration failed".
_DIAG_HINTS = {
    "UnknownPackage": "a package failed to import — add the file that defines it "
                      "to the filelist (order matters: packages before users), "
                      "or it lives in a precompiled lib (e.g. uvm_pkg) not visible "
                      "to pyslang.",
    "UndeclaredIdentifier": "undeclared identifier — usually a missing `include "
                            "(add its dir via +incdir+/incdirs) or a package not "
                            "imported.",
    "CouldNotOpenIncludeFile": "an `include file was not found — add its directory "
                               "via +incdir+ / incdirs.",
    "UnknownModule": "an instantiated module has no definition in the filelist — "
                     "add its source file.",
    "UnknownDirective": "an unknown/ungated `directive — a required +define+ is "
                        "probably missing.",
}


def _summarize_diagnostics(diags, sm, max_items: int = 40) -> dict:
    """Turn raw pyslang diagnostics into a structured, actionable summary.

    Groups by diagnostic code, counts errors vs warnings, samples a few
    human-readable messages, and surfaces which classes of missing inputs
    (include dirs / defines / package sources) likely caused a failed build.
    """
    try:
        de = ps.DiagnosticEngine(sm)
    except Exception:
        de = None
    by_code: Dict[str, int] = {}
    samples: List[dict] = []
    n_err = 0
    for dg in diags:
        code = str(getattr(dg, "code", "")).replace("DiagCode(", "").rstrip(")")
        by_code[code] = by_code.get(code, 0) + 1
        is_err = bool(getattr(dg, "isError", False))
        if is_err:
            n_err += 1
        if len(samples) < max_items:
            text = ""
            if de is not None:
                try:
                    text = de.reportAll(sm, [dg]).strip().splitlines()[0]
                except Exception:
                    text = ""
            samples.append({"code": code, "is_error": is_err, "text": text})
    # derive actionable hints from the codes that showed up
    hints = []
    for code in by_code:
        for key, msg in _DIAG_HINTS.items():
            if key.lower() in code.lower():
                hints.append({"code": code, "count": by_code[code], "hint": msg})
                break
    return {
        "total": len(diags),
        "errors": n_err,
        "by_code": dict(sorted(by_code.items(), key=lambda x: -x[1])),
        "samples": samples,
        "actionable_hints": hints,
    }


# file extensions we consider RTL sources / includes when indexing source roots
_SV_EXTS = {".sv", ".svh", ".v", ".vh", ".svp", ".vp", ".svi"}
# directories never worth walking when searching for missing includes/packages
_SKIP_DIRS = {".git", ".svn", "node_modules", ".cache", "__pycache__"}
# regex to find `package <name>;` when hunting a missing package source
_PKG_DECL_RE = None  # lazily compiled in _find_package_file


def _detect_cadence_uvm_incdirs() -> List[str]:
    """Best-effort discovery of the Cadence (xrun ``-uvmhome CDNS-1.2``) UVM src.

    UVM lives *outside* the design source tree (in the Xcelium install), so the
    source-root self-heal can't find ``uvm_macros.svh`` / ``uvm_pkg`` on its own.
    xrun uses ``-uvmhome CDNS-1.2`` which resolves under the tool install; we
    locate it from ``$UVMHOME`` / ``$CDS_INST_DIR`` or by walking up from the
    ``xrun`` binary, then return the ``.../sv/src`` dir(s) to add as incdirs.
    Returning the dir both resolves the `` `include `` and lets the package
    self-heal find ``uvm_pkg.sv`` there. Empty list when nothing is found (e.g.
    a non-Cadence flow) — purely additive, never fails the build.
    """
    import glob
    import shutil

    cands: List[str] = []

    def _add_glob(base: str):
        if not base:
            return
        for pat in (os.path.join(base, "tools*/methodology/UVM/CDNS-*/sv/src"),
                    os.path.join(base, "methodology/UVM/CDNS-*/sv/src"),
                    os.path.join(base, "UVM/CDNS-*/sv/src"),
                    os.path.join(base, "sv/src")):
            cands.extend(glob.glob(pat))

    # explicit env hints first
    for env in ("UVMHOME", "UVM_HOME", "CDS_UVMHOME"):
        v = os.environ.get(env, "")
        if v and os.path.isdir(v):
            _add_glob(v)
            if os.path.isdir(os.path.join(v, "sv", "src")):
                cands.append(os.path.join(v, "sv", "src"))
    for env in ("CDS_INST_DIR", "XCELIUM_HOME", "CDS_ROOT"):
        _add_glob(os.environ.get(env, ""))
    # derive the install root from the xrun binary: <root>/tools/bin/xrun
    xrun = shutil.which("xrun")
    if xrun:
        try:
            root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(xrun))))
            _add_glob(root)
        except Exception:
            pass
    # dedup, keep only real dirs, prefer the newest CDNS-* if several
    out: List[str] = []
    for d in sorted(set(cands), reverse=True):
        if os.path.isdir(d) and d not in out:
            out.append(d)
    return out


def _make_define_header(defines: Optional[List[str]]) -> str:
    """`define lines to prepend to each file (pyslang doesn't propagate macros
    across independent SyntaxTrees, so we inline them). See build_netlist."""
    if not defines:
        return ""
    lines = []
    for d in defines:
        if "=" in d:
            name, val = d.split("=", 1)
            lines.append(f"`define {name.strip()} {val}")
        else:
            lines.append(f"`define {d.strip()} 1")
    return "\n".join(lines) + "\n"


def _build_trees(files: List[str], incdirs: List[str], define_header: str):
    """Parse ``files`` into pyslang SyntaxTrees under a fresh SourceManager.

    Returns ``(sm, trees, parse_errors)``. Rebuilt from scratch each self-heal
    round so newly-discovered incdirs/files take effect cleanly.
    """
    sm = ps.SourceManager()
    for d in incdirs or []:
        try:
            sm.addUserDirectories(d)
        except Exception:
            pass
    trees = []
    parse_errors: List[str] = []
    for f in files:
        try:
            if define_header:
                with open(f, "r", errors="replace") as fh:
                    body = fh.read()
                text = define_header + f'`line 1 "{f}" 0\n' + body
                trees.append(SyntaxTree.fromText(text, sm, f))
            else:
                trees.append(SyntaxTree.fromFile(f, sm))
        except Exception as exc:
            parse_errors.append(f"{f}: {exc}")
    return sm, trees, parse_errors


def _collect_missing(diags) -> tuple:
    """Extract (missing_include_basenames, missing_package_names) from diags.

    Uses ``dg.args[0]`` which pyslang populates with the exact missing name
    (verified: CouldNotOpenIncludeFile -> 'foo.svh', UnknownPackage -> 'bar_pkg')
    — far more reliable than parsing rendered message text.
    """
    inc: set = set()
    pkg: set = set()
    for dg in diags:
        code = str(getattr(dg, "code", "")).replace("DiagCode(", "").rstrip(")")
        args = list(getattr(dg, "args", []) or [])
        if not args:
            continue
        first = str(args[0])
        if "CouldNotOpenIncludeFile" in code:
            inc.add(first)
        elif "UnknownPackage" in code:
            pkg.add(first)
    return inc, pkg


def _gather_roots(files: List[str], incdirs: List[str]) -> List[str]:
    """Directories to search for missing includes/packages: each source file's
    dir + its parent, plus the given incdirs + their parents (deduped)."""
    roots: List[str] = []
    seen = set()

    def _add(d: str):
        if d and os.path.isdir(d) and d not in seen:
            seen.add(d)
            roots.append(d)
    for f in list(files) + list(incdirs or []):
        d = f if os.path.isdir(f) else os.path.dirname(os.path.abspath(f))
        _add(d)
        _add(os.path.dirname(d))
    return roots


def _index_includes(roots: List[str], max_files: int = 60000) -> Dict[str, List[str]]:
    """basename -> [dirs that contain a file with that basename] (bounded walk)."""
    index: Dict[str, List[str]] = {}
    n = 0
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() not in _SV_EXTS:
                    continue
                index.setdefault(fn, [])
                if dirpath not in index[fn]:
                    index[fn].append(dirpath)
                n += 1
                if n >= max_files:
                    return index
    return index


def _find_package_file(pkgname: str, roots: List[str],
                       max_files: int = 60000) -> Optional[str]:
    """Find a source file that declares ``package <pkgname>;`` under roots.

    Scans file *content* (only files whose name looks like a package or is a
    plausible SV source) so an unresolved import can be auto-added to the
    filelist. Returns the file path or None (e.g. precompiled uvm_pkg won't be
    found, which is correct — it is genuinely external)."""
    global _PKG_DECL_RE
    import re
    if _PKG_DECL_RE is None:
        _PKG_DECL_RE = re.compile(r"^\s*package\s+([A-Za-z_]\w*)\s*;", re.MULTILINE)
    n = 0
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() not in (".sv", ".svh", ".v"):
                    continue
                n += 1
                if n >= max_files:
                    return None
                # cheap prefilter: package files almost always mention the name
                path = os.path.join(dirpath, fn)
                try:
                    with open(path, "r", errors="replace") as fh:
                        text = fh.read()
                except OSError:
                    continue
                if pkgname not in text:
                    continue
                for m in _PKG_DECL_RE.finditer(text):
                    if m.group(1) == pkgname:
                        return path
    return None


def _incdir_for_include(include_arg: str, found_dir: str) -> str:
    """Given an `` `include "sub/foo.svh" `` and the dir where foo.svh was found,
    return the incdir to add so the relative path resolves (strip the subdir)."""
    sub = os.path.dirname(include_arg)
    d = found_dir
    if sub:
        norm = os.path.normpath(found_dir)
        tail = os.path.normpath(sub)
        if norm.endswith(os.sep + tail) or norm.endswith(tail):
            d = norm[: len(norm) - len(tail)].rstrip(os.sep)
    return d or found_dir


def build_netlist(files: List[str], top: Optional[str] = None,
                  incdirs: Optional[List[str]] = None,
                  defines: Optional[List[str]] = None,
                  out_path: Optional[str] = None,
                  self_heal: bool = True, max_heal_rounds: int = 4,
                  auto_detect_uvm: bool = True) -> dict:
    if not _PYSLANG_OK:
        raise NetlistError("pyslang is not available; cannot build netlist")
    files = [f for f in files if f and os.path.exists(f)]
    if not files:
        raise NetlistError("no source files to elaborate")
    incdirs = list(incdirs or [])
    # UVM lives in the Xcelium install, outside the design tree — add it up front
    # so `include "uvm_macros.svh" resolves and the package self-heal can locate
    # uvm_pkg.sv there. Additive & best-effort (no-op in non-Cadence flows).
    uvm_incdirs: List[str] = []
    if auto_detect_uvm:
        for d in _detect_cadence_uvm_incdirs():
            if d not in incdirs:
                incdirs.append(d)
                uvm_incdirs.append(d)
    # +define+ macros: pyslang preprocesses each SyntaxTree independently, so a
    # macro defined in a separate tree does NOT propagate to other files. The
    # reliable, version-independent way is to prepend the `define lines to each
    # file's in-memory text — this makes `ifdef-guarded RTL elaborate exactly as
    # xrun (with the same +define+) sees it.
    define_header = _make_define_header(defines)

    # --- self-healing elaboration loop -------------------------------------
    # Many "partial" netlists on real UVM designs are a chain reaction from ONE
    # missing +incdir+ (CouldNotOpenIncludeFile) or an un-listed package source
    # (UnknownPackage). Rather than require the user to hand-complete the
    # filelist, we read the exact missing name from each diagnostic (dg.args[0])
    # and auto-resolve it against the source-tree roots, then re-elaborate.
    # Genuinely-external packages (e.g. precompiled uvm_pkg) simply won't be
    # found and we stop — the per-member try/except keeps DUT extraction alive.
    auto_added_incdirs: List[str] = []
    auto_added_files: List[str] = []
    healed_rounds = 0
    inc_index: Optional[Dict[str, List[str]]] = None
    roots: List[str] = []
    sm, trees, parse_errors = _build_trees(files, incdirs, define_header)
    if not trees:
        raise NetlistError("failed to parse any source file: " + "; ".join(parse_errors))
    comp = Compilation()
    for t in trees:
        comp.addSyntaxTree(t)
    diags = comp.getAllDiagnostics()

    while self_heal and healed_rounds < max_heal_rounds:
        miss_inc, miss_pkg = _collect_missing(diags)
        if not miss_inc and not miss_pkg:
            break
        if not roots:
            roots = _gather_roots(files, incdirs)
        progressed = False
        # resolve missing includes -> new incdirs
        if miss_inc:
            if inc_index is None:
                inc_index = _index_includes(roots)
            for inc_name in miss_inc:
                cand = inc_index.get(os.path.basename(inc_name))
                if not cand:
                    continue
                new_dir = _incdir_for_include(inc_name, cand[0])
                if new_dir and new_dir not in incdirs:
                    incdirs.append(new_dir)
                    auto_added_incdirs.append(new_dir)
                    progressed = True
        # resolve missing packages -> new source files
        for pkg in miss_pkg:
            pf = _find_package_file(pkg, roots)
            if pf and pf not in files:
                # packages must be visible early; prepend so they parse first
                files.insert(0, pf)
                auto_added_files.append(pf)
                progressed = True
        if not progressed:
            break  # nothing left we can auto-fix (external pkg / truly missing)
        healed_rounds += 1
        sm, trees, parse_errors = _build_trees(files, incdirs, define_header)
        if not trees:
            break
        comp = Compilation()
        for t in trees:
            comp.addSyntaxTree(t)
        diags = comp.getAllDiagnostics()

    diag_summary = _summarize_diagnostics(diags, sm)
    loc = _Loc(trees[0].sourceManager)

    builders: Dict[str, _ModuleBuilder] = {}
    instance_tree: Dict[str, str] = {}
    # TB/DUT decoupling: process each top independently under try/except so a
    # broken top (e.g. a UVM top pulling an unresolved uvm_pkg) cannot abort
    # extraction of a healthy DUT top / sibling.
    failed_tops: List[str] = []
    for top_inst in comp.getRoot().topInstances:
        try:
            dname = _def_name(top_inst)
            instance_tree[top_inst.name] = dname
            if dname not in builders:
                mb = _ModuleBuilder(dname, loc)
                builders[dname] = mb
                _process_body(top_inst, mb, builders, loc, top_inst.name, instance_tree)
        except Exception:
            failed_tops.append(getattr(top_inst, "name", "?"))

    modules = {name: b.finalize() for name, b in builders.items()}
    # partial when elaboration produced diagnostics or emitted no top instance
    # but we still recovered some modules — better to serve a partial netlist
    # (flagged) than to degrade every connectivity/trace tool to unavailable.
    partial = bool(diag_summary["errors"]) or not instance_tree
    result = {
        "tool": "pyslang",
        "version": getattr(ps, "__version__", "?"),
        "top": top or (list(instance_tree)[0] if instance_tree else None),
        "diagnostics": len(diags),
        "diagnostics_summary": diag_summary,
        "partial": partial and bool(modules),
        "parse_errors": parse_errors,
        # self-healing report: what we auto-discovered so the user can fold these
        # back into their .f (and see the netlist is no longer blocked on them).
        "auto_resolved": {
            "rounds": healed_rounds,
            "added_incdirs": auto_added_incdirs,
            "added_files": auto_added_files,
            "uvm_incdirs": uvm_incdirs,
        },
        "failed_tops": failed_tops,
        "modules": modules,
        "instance_tree": instance_tree,
    }
    if out_path:
        import json
        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
        with open(out_path, "w") as fh:
            json.dump(result, fh, indent=2)
    return result
