#!/usr/bin/env python3
"""Compare fstdumper output (FST converted to VCD by fst2vcd) with the
Xcelium-native VCD written by the same simulation run.

Part of wave-mcp (Apache-2.0), used by deploy/verify_fstdumper.sh.

usage: compare_fst_vcd.py [--verdict] <fst2vcd.vcd> <baseline.vcd> [scope_prefix]

Per signal, values are normalised (bit vectors left-extended to full width,
consecutive duplicates removed) and the whole transition list is compared.
Named events are compared by trigger times only (fstdumper writes 1, the
Xcelium VCD dumper writes x).

--verdict prints one line "[verify] PASS: ..." / "[verify] FAIL: ..." and
exits 0 / 1.  PASS means: no width / value / tail difference on any common
signal, every baseline signal missing from the FST is a parameter
(fstdumper never dumps constants), and no FST signal name carries a
backslash.  Extra FST signals are fine (Xcelium's VCD dumper omits static
task variables).
"""
import re
import sys
from collections import defaultdict, Counter

TS_UNIT = {"s": 10**15, "ms": 10**12, "us": 10**9, "ns": 10**6, "ps": 10**3, "fs": 1}


# Escaped identifiers in Xcelium's own VCD: signal names without the
# backslash ("esc+id"), scope names with it ("\\u+esc").  RTL tools (wave-mcp's
# netlist) use the plain name for both; comparisons ignore the backslash.
ESCAPED = {}      # path -> signal refs written with a leading backslash


def _unesc(name):
    return name[1:] if name.startswith("\\") and len(name) > 1 else name


def parse_vcd(path):
    sigs = {}            # fullname -> (vtype, size, scope_types)
    blasted = defaultdict(dict)   # bus fullname -> {bit index: bit signal name}
    id2names = defaultdict(list)
    scope = []           # list of (type, name)
    tmul = 1
    vals = defaultdict(list)
    t = 0
    with open(path, "r", errors="replace") as f:
        it = iter(f)
        # ---- header ----
        buf = ""
        for line in it:
            buf += line
            if "$enddefinitions" in line:
                break
        toks = buf.split()
        i = 0
        while i < len(toks):
            tk = toks[i]
            if tk == "$timescale":
                j = i + 1
                ts = ""
                while toks[j] != "$end":
                    ts += toks[j]
                    j += 1
                m = re.match(r"(\d+)\s*([a-z]+)", ts)
                tmul = int(m.group(1)) * TS_UNIT[m.group(2)]
                i = j
            elif tk == "$scope":
                # escaped identifiers: Xcelium's VCD drops the backslash
                scope.append((toks[i + 1], _unesc(toks[i + 2])))
                i += 3
            elif tk == "$upscope":
                scope.pop()
                i += 1
            elif tk == "$var":
                j = i + 1
                parts = []
                while toks[j] != "$end":
                    parts.append(toks[j])
                    j += 1
                vtype, size, ident, ref = parts[0], int(parts[1]), parts[2], parts[3]
                if ref.startswith("\\"):
                    ESCAPED.setdefault(path, []).append(ref)
                name = ".".join(s[1] for s in scope) + "." + _unesc(ref)
                rng = parts[4] if len(parts) > 4 else ""
                mb = re.fullmatch(r"\[(\d+)\]", rng)
                if size == 1 and mb:
                    # bit-blasted bus member (Xcelium VCD style "sig [3]")
                    bitname = "%s[%s]" % (name, mb.group(1))
                    blasted[name][int(mb.group(1))] = bitname
                    sigs[bitname] = (vtype, 1, tuple(s[0] for s in scope))
                    id2names[ident].append(bitname)
                else:
                    sigs[name] = (vtype, size, tuple(s[0] for s in scope))
                    id2names[ident].append(name)
                i = j
            i += 1
        # ---- body ----
        for line in it:
            line = line.strip()
            if not line:
                continue
            c = line[0]
            if c == "#":
                t = int(line[1:]) * tmul
                continue
            if c in "bBrRsS":
                v, ident = line[1:].split(None, 1)
                v = (c.lower() if c in "rRsS" else "") + v.lower()
            elif c in "01xzXZuUwWlLhH-":
                v, ident = line[0].lower(), line[1:]
            else:
                continue  # $dumpvars/$end/$comment etc.
            for n in id2names.get(ident, ()):
                vals[n].append((t, v))
    # re-assemble bit-blasted buses into vectors, left (MSB) end first.
    # Xcelium declares the bits from the left end: [7] .. [0] for [7:0],
    # but [0] .. [3] for an ascending [0:3], so keep declaration order.
    for bus, bits in blasted.items():
        if bus in sigs:
            continue
        idx = list(bits)
        ev = sorted((t, b, v) for b in idx for (t, v) in vals.pop(bits[b], []))
        cur = {b: "x" for b in idx}
        out = []
        k = 0
        while k < len(ev):
            t0 = ev[k][0]
            while k < len(ev) and ev[k][0] == t0:
                cur[ev[k][1]] = ev[k][2]
                k += 1
            out.append((t0, "".join(cur[b] for b in idx)))
        st = sigs[bits[idx[0]]]
        for b in idx:
            sigs.pop(bits[b], None)
        sigs[bus] = (st[0], len(idx), st[2])
        vals[bus] = out
    return sigs, vals


def norm(v, size):
    if v[:1] in ("r", "s"):
        return v
    if len(v) < size:
        pad = v[0] if v[0] in "xz" else "0"
        v = pad * (size - len(v)) + v
    return v


def dedup(lst, size):
    out = []
    for t, v in lst:
        v = norm(v, size)
        if out and out[-1][0] == t:
            out[-1] = (t, v)          # last value at a time step wins
            if len(out) > 1 and out[-2][1] == v:
                out.pop()
            continue
        if out and out[-1][1] == v:
            continue
        out.append((t, v))
    return out


def event_times(lst):
    return sorted(set(t for t, _ in lst))


def main():
    args = sys.argv[1:]
    verdict = "--verdict" in args
    args = [a for a in args if a != "--verdict"]
    fst_vcd, base_vcd = args[0], args[1]
    prefix = args[2] if len(args) > 2 else ""
    fs, fv = parse_vcd(fst_vcd)
    esc_fst = ESCAPED.get(fst_vcd, [])
    bs, bv = parse_vcd(base_vcd)
    if prefix:
        fs = {k: v for k, v in fs.items() if k.startswith(prefix)}
        bs = {k: v for k, v in bs.items() if k.startswith(prefix)}
    common = sorted(set(fs) & set(bs))
    only_f = sorted(set(fs) - set(bs))
    only_b = sorted(set(bs) - set(fs))

    width_mismatch, val_mismatch, tail_only = [], [], []
    n_tr_f = n_tr_b = n_parameters = 0
    for n in common:
        sf, sb = fs[n][1], bs[n][1]
        if bs[n][0] == "event":
            a, b = event_times(fv.get(n, [])), event_times(bv.get(n, []))
            n_tr_f += len(a)
            n_tr_b += len(b)
            if a != b:
                val_mismatch.append((n, "event times", a[:3], b[:3]))
            continue
        if bs[n][0] == "parameter":
            n_parameters += 1
            continue      # constant, compared by presence only
        if sf != sb:
            width_mismatch.append((n, sf, sb))
            continue
        a, b = dedup(fv.get(n, []), sf), dedup(bv.get(n, []), sb)
        n_tr_f += len(a)
        n_tr_b += len(b)
        if a != b:
            if a == b[: len(a)] or b == a[: len(b)]:
                tail_only.append((n, len(a), len(b)))
            else:
                k = next(i for i, (x, y) in enumerate(zip(a, b)) if x != y) if any(
                    x != y for x, y in zip(a, b)) else min(len(a), len(b))
                val_mismatch.append((n, k, a[k:k + 2], b[k:k + 2]))

    if verdict:
        missing = [n for n in only_b if bs[n][0] != "parameter"]
        bad = []
        if not common:
            bad.append("no common signal")
        if width_mismatch:
            bad.append("%d width mismatch" % len(width_mismatch))
        if val_mismatch:
            bad.append("%d value mismatch" % len(val_mismatch))
        if tail_only:
            bad.append("%d tail difference" % len(tail_only))
        if missing:
            bad.append("%d non-parameter signals missing from FST, e.g. %s"
                       % (len(missing), ", ".join(missing[:3])))
        if esc_fst:
            bad.append("%d signal names written with a backslash (the native "
                       "VCD and RTL tools use none), e.g. %s" % (len(esc_fst), esc_fst[0]))
        if bad:
            print("[verify] FAIL: " + "; ".join(bad))
            sys.exit(1)
        n_par_missing = sum(1 for n in only_b if bs[n][0] == "parameter")
        print("[verify] PASS: %d signals identical (%d transitions), "
              "%d parameters not dumped, %d extra in FST"
              % (len(common), n_tr_f, n_par_missing, len(only_f)))
        sys.exit(0)

    print("fst signals      :", len(fs))
    print("baseline signals :", len(bs))
    print("common           :", len(common))
    print("only in fst      :", len(only_f))
    print("only in baseline :", len(only_b))
    print("width mismatch   :", len(width_mismatch))
    print("value mismatch   :", len(val_mismatch))
    print("tail-only diff   :", len(tail_only))
    print("transitions fst/base (common, dedup): %d / %d" % (n_tr_f, n_tr_b))

    # classify baseline-only signals by enclosing scope types / var type
    def cls(n):
        st = bs[n][2]
        if any(s in ("begin", "fork", "task", "function") for s in st):
            return "inside begin/generate/task scope"
        return "module/interface scope, vtype=" + bs[n][0]
    c = Counter(cls(n) for n in only_b)
    if only_b:
        print("\nbaseline-only breakdown:")
        for k, v in c.most_common():
            print("  %5d  %s" % (v, k))
        flat = [n for n in only_b if not cls(n).startswith("inside")]
        if flat:
            print("\nbaseline-only signals NOT under begin/generate scopes:")
            for n in flat:
                print("   ", n, bs[n][0], bs[n][1])
    for lbl, lst in (("only in fst", only_f), ("width mismatch", width_mismatch),
                     ("value mismatch", val_mismatch), ("tail-only", tail_only)):
        if lst:
            print("\n%s (first 20):" % lbl)
            for x in lst[:20]:
                print("   ", x)


if __name__ == "__main__":
    main()
