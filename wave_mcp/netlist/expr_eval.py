"""Tiny 4-state (0/1/x/z) evaluator for branch-guard conditions.

Used to decide which driver of a signal is *active* at a given time: each driver
carries a serialized guard (the if/case conditions that must hold to reach it),
and we evaluate those guards using the signals' FST values at the time point.

Expression trees are produced by ``slang_netlist.serialize_expr`` as small dicts:
  {"k":"sig","name":..}                      signal reference
  {"k":"const","lit":"8'd255"}               integer literal (SV syntax)
  {"k":"un","op":<UnaryOperator>,"a":node}
  {"k":"bin","op":<BinaryOperator>,"l":..,"r":..}
  {"k":"unknown"}                            unsupported -> evaluates to x

Bit strings are MSB-first over the alphabet {0,1,x,z}, matching FST values.
This is intentionally a pragmatic subset (common conditions: !rst, en,
sel==K, state==IDLE, a&&b); anything unsupported yields ``x`` so the caller
falls back to the value-informed heuristic — never a wrong confident answer.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

ValueFn = Callable[[str], Optional[str]]

_LIT_RE = re.compile(r"^\s*(\d+)?\s*'\s*([sS]?)([bodhBODH])\s*([0-9a-fA-FxXzZ_]+)\s*$")


def _norm(bits: Optional[str]) -> Optional[str]:
    if bits is None:
        return None
    return bits.strip().lower().replace("_", "")


def parse_literal(lit: str) -> Optional[str]:
    """Parse a SystemVerilog integer literal into an MSB-first bit string."""
    if lit is None:
        return None
    s = str(lit).strip()
    m = _LIT_RE.match(s)
    if m:
        width = int(m.group(1)) if m.group(1) else None
        base = m.group(3).lower()
        digits = m.group(4).lower()
        if base == "b":
            bits = digits
        elif base == "h":
            bits = "".join(_hex_to_bits(c) for c in digits)
        elif base == "o":
            bits = "".join(_oct_to_bits(c) for c in digits)
        else:  # decimal
            if any(c in "xz" for c in digits):
                return None
            bits = bin(int(digits))[2:]
        if width:
            bits = _resize(bits, width)
        return bits
    # plain decimal (no base)
    try:
        return bin(int(s))[2:]
    except ValueError:
        return None


def _hex_to_bits(c: str) -> str:
    if c in "xz":
        return c * 4
    return format(int(c, 16), "04b")


def _oct_to_bits(c: str) -> str:
    if c in "xz":
        return c * 3
    return format(int(c, 8), "03b")


def _resize(bits: str, width: int) -> str:
    if len(bits) >= width:
        return bits[-width:]
    pad = bits[0] if bits and bits[0] in "xz" else "0"  # x/z extend, else zero-extend
    return pad * (width - len(bits)) + bits


def _align(a: str, b: str):
    w = max(len(a), len(b))
    return _resize(a, w), _resize(b, w)


def truth(bits: Optional[str]) -> str:
    """Logical truth of a vector in a condition context: '0' / '1' / 'x'."""
    b = _norm(bits)
    if not b:
        return "x"
    if "1" in b:
        return "1"
    if "x" in b or "z" in b:
        return "x"
    return "0"


def evaluate(node: dict, vf: ValueFn) -> Optional[str]:
    """Evaluate an expression node to a bit string (MSB-first) or None/x-filled."""
    if not isinstance(node, dict):
        return None
    k = node.get("k")
    if k == "sig":
        return _norm(vf(node["name"]))
    if k == "const":
        return parse_literal(node.get("lit"))
    if k == "un":
        return _eval_unary(node, vf)
    if k == "bin":
        return _eval_binary(node, vf)
    return None  # unknown -> x downstream


def _eval_unary(node: dict, vf: ValueFn) -> Optional[str]:
    op = node.get("op", "")
    a = evaluate(node.get("a"), vf)
    if op in ("LogicalNot",):
        t = truth(a)
        return {"1": "0", "0": "1"}.get(t, "x")
    if op in ("BitwiseNot",):
        if a is None:
            return None
        return "".join("1" if c == "0" else "0" if c == "1" else "x" for c in a)
    if op in ("Minus", "Plus"):
        return a  # magnitude not needed for truth-based guards
    return None


def _eval_binary(node: dict, vf: ValueFn) -> Optional[str]:
    op = node.get("op", "")
    l = evaluate(node.get("l"), vf)
    r = evaluate(node.get("r"), vf)
    if op == "LogicalAnd":
        tl, tr = truth(l), truth(r)
        if tl == "0" or tr == "0":
            return "0"
        if tl == "1" and tr == "1":
            return "1"
        return "x"
    if op == "LogicalOr":
        tl, tr = truth(l), truth(r)
        if tl == "1" or tr == "1":
            return "1"
        if tl == "0" and tr == "0":
            return "0"
        return "x"
    if op in ("Equality", "Inequality"):
        if l is None or r is None:
            return "x"
        a, b = _align(l, r)
        if "x" in a + b or "z" in a + b:
            return "x"
        eq = a == b
        if op == "Inequality":
            eq = not eq
        return "1" if eq else "0"
    if op in ("CaseEquality", "CaseInequality"):
        if l is None or r is None:
            return "x"
        a, b = _align(l, r)
        eq = a == b
        if op == "CaseInequality":
            eq = not eq
        return "1" if eq else "0"
    if op in ("BinaryAnd", "BinaryOr", "BinaryXor"):
        if l is None or r is None:
            return None
        a, b = _align(l, r)
        out = []
        for x, y in zip(a, b):
            out.append(_bit_op(op, x, y))
        return "".join(out)
    if op in ("LessThan", "GreaterThan", "LessThanEqual", "GreaterThanEqual"):
        if l is None or r is None or "x" in (l + r) or "z" in (l + r):
            return "x"
        iv, jv = int(l, 2), int(r, 2)
        res = {"LessThan": iv < jv, "GreaterThan": iv > jv,
               "LessThanEqual": iv <= jv, "GreaterThanEqual": iv >= jv}[op]
        return "1" if res else "0"
    return None


def _bit_op(op: str, x: str, y: str) -> str:
    if op == "BinaryAnd":
        if x == "0" or y == "0":
            return "0"
        if x == "1" and y == "1":
            return "1"
        return "x"
    if op == "BinaryOr":
        if x == "1" or y == "1":
            return "1"
        if x == "0" and y == "0":
            return "0"
        return "x"
    # xor
    if x in "xz" or y in "xz":
        return "x"
    return "1" if x != y else "0"


def guard_satisfied(cond_node: dict, expect: int, vf: ValueFn) -> Optional[bool]:
    """True/False if the guard is decidably (un)satisfied, None if undecidable (x)."""
    t = truth(evaluate(cond_node, vf))
    if t == "x":
        return None
    return (t == "1") == bool(expect)
