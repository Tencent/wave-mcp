#!/usr/bin/env python3
"""wave-mcp fieldkit — air-gapped site test runner with sanitized reporting.

Run INSIDE the target (air-gapped) environment:

    # 1. environment + built-in self test only (no user data needed)
    python3 tests/fieldkit/run_fieldkit.py

    # 2. + real-project test (your own waveform & filelist; results sanitized)
    python3 tests/fieldkit/run_fieldkit.py --wave sim/dump.vcd \
            --filelist rtl.f --top top_tb

Output channels (designed for copy-paste exfiltration with a size budget):
  L0  one summary line  (~120 chars)  — paste this if nothing else fits
  L1  compact text block (<= ~40 lines) — the normal feedback payload
  L2  fieldkit_report.json — full sanitized detail, if a file channel exists

SANITIZATION GUARANTEE: no signal names, module names, instance paths, file
paths or RTL text ever appear in L0/L1/L2. Only counts, ratios, timings,
version strings and error-class codes. Unexpected exception texts are reduced
to the exception CLASS NAME plus a stable 8-char digest of the message, so a
message that accidentally contains a path is never emitted verbatim.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

KIT_VERSION = "1.0"

# ---------------------------------------------------------------------------
# error classification: map a failure to a stable code the outside team can
# act on WITHOUT seeing any project data.
# ---------------------------------------------------------------------------
ERROR_CLASSES = {
    "E-ENV-PYVER": "python version unsupported",
    "E-ENV-IMPORT": "wave_mcp or a binary dep failed to import",
    "E-ENV-CMD": "console command missing (wave-mcp/wave-session/wave-vcd2fst)",
    "E-VCD-CONVERT": "VCD -> FST conversion failed",
    "E-VCD-DIALECT": "FST opened but hierarchy/signals look wrong (dialect?)",
    "E-FST-OPEN": "waveform file failed to open",
    "E-NETLIST-BUILD": "netlist elaboration failed entirely",
    "E-NETLIST-PARTIAL": "netlist built with elaboration errors (partial trust)",
    "E-NETLIST-PROTECT": "sources include encrypted/protected regions",
    "E-ALIGN-COVERAGE": "waveform<->netlist definition coverage below threshold",
    "E-TOOL-CRASH": "a query tool raised an exception",
    "E-TOOL-EMPTY": "a tool returned empty where data was expected",
    "E-PERF-SLOW": "operation exceeded its time budget",
}


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:8]


def _exc_code(exc: Exception) -> str:
    """Exception -> sanitized token: ClassName@digest (message never emitted)."""
    return f"{type(exc).__name__}@{_digest(str(exc))}"


def _bucket(n: int) -> str:
    for b in (1, 2, 4, 8, 16, 32, 64, 128):
        if n <= b:
            return f"<={b}"
    return ">128"


def _hist(d: dict) -> str:
    """dict -> 'key:count,key:count' sorted by count desc (keys are format/
    enum tokens like 'wire' or '<=8', never project identifiers)."""
    items = sorted(d.items(), key=lambda kv: -kv[1])
    return ",".join(f"{k}:{v}" for k, v in items[:8]) or "none"


class Report:
    def __init__(self):
        self.sections = {}
        self.errors = []          # list of (code, sanitized_context)
        self.t0 = time.time()

    def sec(self, name, **kv):
        self.sections.setdefault(name, {}).update(kv)

    def err(self, code, context=""):
        self.errors.append({"code": code, "ctx": context})

    # -- outputs -------------------------------------------------------------
    def l0_line(self) -> str:
        fails = {}
        for e in self.errors:
            fails[e["code"]] = fails.get(e["code"], 0) + 1
        fail_s = ",".join(f"{k}x{v}" for k, v in sorted(fails.items())) or "none"
        env = self.sections.get("env", {})
        ut = self.sections.get("selftest", {})
        pj = self.sections.get("project", {})
        parts = [
            f"WMFK{KIT_VERSION}",
            f"py{env.get('python', '?')}",
            f"self={ut.get('passed', 0)}/{ut.get('checks', 0)}",
        ]
        if pj:
            parts += [
                f"proj={pj.get('tools_pass', 0)}/{pj.get('tools_total', 0)}",
                f"trust={pj.get('netlist_trust', '?')}",
                f"cov={pj.get('def_coverage_pct', '?')}",
            ]
        parts.append(f"err={fail_s}")
        parts.append(f"t={int(time.time() - self.t0)}s")
        return "|".join(parts)

    def l1_text(self) -> str:
        L = ["=" * 62, f" wave-mcp FIELDKIT v{KIT_VERSION} sanitized report", "=" * 62]
        for name, kv in self.sections.items():
            L.append(f"[{name}]")
            for k, v in kv.items():
                L.append(f"  {k:24s} {v}")
        if self.errors:
            L.append("[errors]")
            for e in self.errors:
                L.append(f"  {e['code']:22s} {e['ctx']}")
        else:
            L.append("[errors]  none")
        L.append("-" * 62)
        L.append(" L0: " + self.l0_line())
        L.append("=" * 62)
        return "\n".join(L)

    def l2_json(self) -> dict:
        return {"fieldkit": KIT_VERSION, "generated_unix": int(time.time()),
                "sections": self.sections, "errors": self.errors,
                "l0": self.l0_line()}


# ---------------------------------------------------------------------------
def stage_env(rep: Report):
    pv = platform.python_version()
    rep.sec("env", python=pv, os=platform.system().lower(),
            machine=platform.machine(), glibc="-".join(platform.libc_ver()) or "n/a")
    if sys.version_info < (3, 10):
        rep.err("E-ENV-PYVER", f"py{pv}")
    for mod in ("wave_mcp", "pyslang", "pylibfst"):
        try:
            m = __import__(mod)
            rep.sec("env", **{f"{mod}_ver": getattr(m, "__version__", "?")})
        except Exception as exc:  # noqa: BLE001
            rep.err("E-ENV-IMPORT", f"{mod}:{_exc_code(exc)}")
    import shutil as sh
    missing = [c for c in ("wave-mcp", "wave-session", "wave-vcd2fst")
               if not sh.which(c)]
    rep.sec("env", console_cmds="ok" if not missing else f"missing:{len(missing)}")
    if missing:
        rep.err("E-ENV-CMD", f"n={len(missing)}")


def stage_selftest(rep: Report):
    """Built-in sample session: proves the tool survived transport intact."""
    checks = passed = 0

    def ck(cond):
        nonlocal checks, passed
        checks += 1
        passed += bool(cond)

    try:
        from wave_mcp.session import open_session
        s = open_session(os.path.join(ROOT, "examples", "sample", "session"))
        info = s.summary()
        ck(info.get("num_signals", 0) > 0)
        sig = next(iter(s.fst.signals))
        ck(s.fst.value_at(sig, s.fst.end_time) is not None)
        ck(s.rtl.has_netlist)
        ck(bool(s.rtl.drivers(sig)))
        s.close()
    except Exception as exc:  # noqa: BLE001
        rep.err("E-TOOL-CRASH", f"selftest:{_exc_code(exc)}")
    rep.sec("selftest", checks=checks, passed=passed)


def stage_fingerprint(rep: Report, wave, session):
    """Structural fingerprints for OFF-SITE synthetic reproduction.

    Everything emitted is a statistic over format/enum tokens (var types,
    bucketed counts, naming-pattern classes). No identifier is ever emitted;
    identifier NAMES are reduced to charset-class counts only.
    """
    import re

    # -- VCD dialect fingerprint (header scan, first 4 MB of text) ----------
    if wave and not wave.lower().endswith(".fst") and os.path.exists(wave):
        var_types = {}
        id_classes = {"plain": 0, "escaped": 0, "with_range": 0,
                      "with_index": 0}
        scope_types = {}
        depth = 0
        max_depth = 0
        extras = set()
        try:
            with open(wave, "r", errors="replace") as fh:
                for _ in range(200000):
                    line = fh.readline()
                    if not line or line.startswith("#"):
                        break
                    t = line.split()
                    if not t:
                        continue
                    if t[0] == "$var" and len(t) >= 5:
                        var_types[t[1]] = var_types.get(t[1], 0) + 1
                        name = " ".join(t[4:-1])
                        if name.startswith("\\"):
                            id_classes["escaped"] += 1
                        elif re.search(r"\[\d+:\d+\]", name):
                            id_classes["with_range"] += 1
                        elif re.search(r"\[\d+\]", name):
                            id_classes["with_index"] += 1
                        else:
                            id_classes["plain"] += 1
                    elif t[0] == "$scope" and len(t) >= 2:
                        scope_types[t[1]] = scope_types.get(t[1], 0) + 1
                        depth += 1
                        max_depth = max(max_depth, depth)
                    elif t[0] == "$upscope":
                        depth = max(0, depth - 1)
                    elif t[0] in ("$dumpoff", "$dumpon", "$comment",
                                  "$timescale", "$version"):
                        extras.add(t[0])
            rep.sec("fingerprint.vcd",
                    var_types=_hist(var_types),
                    identifier_classes=_hist(id_classes),
                    scope_types=_hist(scope_types),
                    max_scope_depth=max_depth,
                    header_directives=",".join(sorted(extras)) or "none")
        except OSError as exc:
            rep.err("E-FST-OPEN", f"fingerprint:{_exc_code(exc)}")

    # -- waveform / netlist structure fingerprint ----------------------------
    if session is None:
        return
    s = session
    try:
        widths = {}
        for sig in s.fst.signals.values():
            w = int(getattr(sig, "length", 1) or 1)
            widths[_bucket(w)] = widths.get(_bucket(w), 0) + 1
        depths = {}
        kinds = {}
        namepat = {"genblk_numbered": 0, "indexed_block": 0, "named": 0}
        for path, sc in s.fst.scopes.items():
            d = path.count(".") + 1
            depths[_bucket(d)] = depths.get(_bucket(d), 0) + 1
            k = getattr(sc, "scope_type", "?") or "?"
            kinds[k] = kinds.get(k, 0) + 1
            leaf = path.rsplit(".", 1)[-1]
            if re.fullmatch(r"genblk\d+(\[\d+\])?", leaf):
                namepat["genblk_numbered"] += 1
            elif re.search(r"\[\d+\]$", leaf):
                namepat["indexed_block"] += 1
            else:
                namepat["named"] += 1
        rep.sec("fingerprint.wave",
                signal_width_hist=_hist(widths),
                scope_depth_hist=_hist(depths),
                scope_kind_hist=_hist(kinds),
                block_naming=_hist(namepat))
        if s.rtl.has_netlist:
            mods = s.rtl.maps.get("modules", {})
            drv_kinds = {}
            per_mod_drv = {}
            skipped = {}
            for m in mods.values():
                nrec = 0
                for recs in (m.get("drivers", {}) or {}).values():
                    for r in recs:
                        drv_kinds[r.get("kind", "?")] = \
                            drv_kinds.get(r.get("kind", "?"), 0) + 1
                        nrec += 1
                per_mod_drv[_bucket(nrec)] = per_mod_drv.get(_bucket(nrec), 0) + 1
                sk = int(m.get("skipped_members", 0) or 0)
                if sk:
                    skipped[_bucket(sk)] = skipped.get(_bucket(sk), 0) + 1
            rep.sec("fingerprint.netlist",
                    driver_kind_hist=_hist(drv_kinds),
                    drivers_per_module_hist=_hist(per_mod_drv),
                    modules_with_skips=_hist(skipped))
    except Exception as exc:  # noqa: BLE001
        rep.err("E-TOOL-CRASH", f"fingerprint:{_exc_code(exc)}")


def stage_project(rep: Report, wave, filelist, top, budget_s, fingerprint=False):
    """Sanitized real-project run: counts / ratios / timings / codes only."""
    from wave_mcp import pipeline
    from wave_mcp.session import open_session

    out_dir = os.path.join(os.getcwd(), "fieldkit_session")
    t0 = time.time()
    try:
        res = pipeline.prepare_session(out_dir, wave, top=top or "",
                                       filelist_path=filelist)
    except Exception as exc:  # noqa: BLE001
        code = "E-VCD-CONVERT" if not wave.lower().endswith(".fst") else "E-FST-OPEN"
        rep.err(code, _exc_code(exc))
        return
    t_prepare = round(time.time() - t0, 1)

    try:
        s = open_session(res["manifest"])
    except Exception as exc:  # noqa: BLE001
        rep.err("E-FST-OPEN", _exc_code(exc))
        return
    info = s.summary()

    # --- health / alignment (numbers only) ---------------------------------
    nh = info.get("netlist_health", {})
    cov = (info.get("definition_coverage") or {})
    cov_pct = cov.get("coverage_pct", cov.get("resolved", 0))
    rep.sec("project",
            prepare_sec=t_prepare,
            num_scopes=info.get("num_scopes"),
            num_signals=info.get("num_signals"),
            netlist_trust=nh.get("trust"),
            netlist_modules=nh.get("modules"),
            elab_errors=nh.get("diagnostic_errors"),
            elab_warnings=nh.get("diagnostic_warnings"),
            skipped_members=nh.get("skipped_members"),
            def_coverage_pct=cov_pct)
    if nh.get("trust") == "none":
        rep.err("E-NETLIST-BUILD")
    elif nh.get("trust") == "partial":
        rep.err("E-NETLIST-PARTIAL", f"errors={nh.get('diagnostic_errors')}")
    try:
        if isinstance(cov_pct, (int, float)) and cov_pct < 50:
            rep.err("E-ALIGN-COVERAGE", f"pct={cov_pct}")
    except TypeError:
        pass

    # encrypted-source scan: counts only
    prot = 0
    for f in (s.filelist or []):
        try:
            if os.path.exists(f):
                with open(f, "rb") as fh:
                    if b"pragma protect" in fh.read(1 << 20):
                        prot += 1
        except OSError:
            pass
    if prot:
        rep.sec("project", protected_files=prot)
        rep.err("E-NETLIST-PROTECT", f"files={prot}")

    # --- tool sweep: every category, sampled, sanitized ---------------------
    sigs = list(s.fst.signals)
    step = max(1, len(sigs) // 25)
    sample = sigs[::step][:25]
    mid_t = (s.fst.start_time + s.fst.end_time) // 2

    tools_total = tools_pass = 0
    slow = []

    def sweep(name, fn, expect_data=False):
        nonlocal tools_total, tools_pass
        tools_total += 1
        t1 = time.time()
        try:
            got = 0
            for x in sample:
                r = fn(x)
                got += bool(r)
            dt = time.time() - t1
            if dt > budget_s:
                slow.append(f"{name}:{round(dt, 1)}s")
            if expect_data and got == 0 and sample:
                rep.err("E-TOOL-EMPTY", name)
            else:
                tools_pass += 1
        except Exception as exc:  # noqa: BLE001
            rep.err("E-TOOL-CRASH", f"{name}:{_exc_code(exc)}")

    sweep("value_at", lambda p: s.fst.value_at(p, mid_t), expect_data=True)
    sweep("values_between",
          lambda p: s.fst.values_between(p, s.fst.start_time, mid_t, 50))
    if s.rtl.has_netlist:
        sweep("drivers", lambda p: s.rtl.drivers(p))
        sweep("fan_in", lambda p: s.rtl.fan_in(p))
        sweep("loads", lambda p: s.rtl.loads(p))
        sweep("connectivity", lambda p: s.rtl.connectivity(p))
        sweep("trace_value",
              lambda p: s.rtl.trace_value(p, str(mid_t), max_depth=4))
    rep.sec("project", tools_total=tools_total, tools_pass=tools_pass)
    if slow:
        rep.sec("project", slow_tools=";".join(slow))
        rep.err("E-PERF-SLOW", f"n={len(slow)}")
    if fingerprint:
        stage_fingerprint(rep, wave, s)
    s.close()


def main():
    ap = argparse.ArgumentParser(description="wave-mcp air-gap fieldkit")
    ap.add_argument("--wave", help="waveform (.fst/.vcd) of YOUR project")
    ap.add_argument("--filelist", help="RTL filelist (.f) matching the wave")
    ap.add_argument("--top", default="", help="top module name")
    ap.add_argument("--budget", type=float, default=30.0,
                    help="per-tool time budget seconds (default 30)")
    ap.add_argument("--fingerprint", action="store_true",
                    help="collect structural fingerprints (statistics over "
                         "format/enum tokens; still fully sanitized) to help "
                         "off-site synthetic reproduction")
    ap.add_argument("--json", default="fieldkit_report.json",
                    help="L2 json output path")
    args = ap.parse_args()

    rep = Report()
    stage_env(rep)
    stage_selftest(rep)
    if args.wave:
        stage_project(rep, args.wave, args.filelist, args.top, args.budget,
                      fingerprint=args.fingerprint)

    print(rep.l1_text())
    try:
        with open(args.json, "w") as fh:
            json.dump(rep.l2_json(), fh, indent=1)
        print(f" L2 json: {args.json}")
    except OSError:
        pass
    fatal = ("E-ENV-PYVER", "E-ENV-IMPORT", "E-FST-OPEN", "E-NETLIST-BUILD",
             "E-VCD-CONVERT")
    return 0 if not any(e["code"] in fatal for e in rep.errors) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - last resort: still sanitized
        print(f"WMFK{KIT_VERSION}|FATAL|{type(exc).__name__}@"
              f"{hashlib.sha1(str(exc).encode()).hexdigest()[:8]}")
        traceback.print_exc(file=open(os.devnull, "w"))
        sys.exit(2)
