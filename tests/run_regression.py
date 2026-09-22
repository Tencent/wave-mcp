#!/usr/bin/env python3
"""wave-mcp unified regression entry.

One command runs every suite that can run in the current environment:

    python3 tests/run_regression.py            # run everything available
    python3 tests/run_regression.py --quick    # skip slow project regressions
    python3 tests/run_regression.py --jobs 8   # suite-level parallelism

Suites (auto-skipped when their prerequisites are missing):
  unit       - smoke_test + test_definition_name (examples/sample session)
  fourstate  - 4-state X/Z suites; needs iverilog (rebuilds VCDs on the fly)
  projects   - project-level functional verification over prebuilt assets
               (auto-skipped when assets are not configured)

Independent suites run concurrently; each suite is itself a subprocess, so
the pool only overlaps their wall-clock instead of running test logic on
threads. Per-suite output is captured and printed as one block when that
suite finishes (concurrent runs stay readable), and the summary keeps a
stable order regardless of completion order.

Exit code: 0 if every executed suite passed, 1 otherwise.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

#: lines of one suite's captured output printed before trimming the middle
_OUTPUT_LIMIT = 400

#: suites in the order the summary reports them (stable across runs)
_SUITE_ORDER = ["unit/smoke", "unit/definition_name", "unit/dut_root",
                "unit/diff", "unit/activity", "unit/predicate",
                "unit/fingerprint", "unit/storage_policy", "unit/execution_limits",
                "unit/merge_rename",
                "unit/query_defaults",
                "unit/session_isolation", "unit/request_snapshot",
                "unit/p3", "protocol/query_defaults", "protocol/http_sessions",
                "protocol/http_token",
                "unit/viewer", "unit/cli_version",
                "unit/fsdb2fst_packaging", "viewer/e2e",
                "fourstate/base", "fourstate/ext",
                "projects/functional_verify"]


def run(name, cmd, cwd=ROOT):
    """Run one suite as a subprocess and capture its output.

    Captured rather than streamed so concurrent suites stay readable: the
    whole block is printed when the suite finishes. Failures keep the tail,
    which is where test runners put their summary.
    """
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           errors="replace")
        ok = p.returncode == 0
        out = (p.stdout or "") + (p.stderr or "")
    except OSError as exc:
        ok, out = False, f"failed to launch: {exc}"
    return {"suite": name, "ok": ok, "elapsed": round(time.time() - t0, 1),
            "command": " ".join(str(c) for c in cmd), "output": out}


def print_block(result):
    """Print one suite's banner plus its captured output (middle-trimmed)."""
    print(f"\n{'='*66}\n  [{result['suite']}] {result.get('command', '')}\n{'='*66}")
    lines = (result.get("output") or "").rstrip().splitlines()
    if not lines:
        print("  (no output)")
    elif len(lines) <= _OUTPUT_LIMIT:
        print("\n".join(lines))
    else:
        head = max(10, _OUTPUT_LIMIT // 10)
        tail = _OUTPUT_LIMIT - head
        print("\n".join(lines[:head]))
        print(f"  ... ({len(lines) - head - tail} lines omitted) ...")
        print("\n".join(lines[-tail:]))
    sys.stdout.flush()

def _has_pytest() -> bool:
    """Whether pytest is importable in this interpreter.

    pytest is a dev-only dependency and is deliberately NOT in the offline
    wheelhouse, so the two pytest-based suites must skip (with the reason
    printed) instead of failing every install on an air-gapped host.
    """
    try:
        import pytest  # noqa: F401  pylint: disable=unused-import
        return True
    except ImportError:
        return False


def _fourstate_task():
    """Build the 4-state sims, then run both suites (independent session dirs).

    Returns a list of suite results so the summary still reports
    fourstate/base and fourstate/ext separately. The iverilog builds must
    precede the runs; the two suites themselves do not share state.
    """
    fs = os.path.join(HERE, "fourstate")
    sim = os.path.join(fs, "sim")
    suites = [("fourstate/base", "run_fourstate_test.py"),
              ("fourstate/ext", "run_fourstate_ext_test.py")]
    try:
        os.makedirs(sim, exist_ok=True)
        for rtl, tb, vvp in [("fourstate_top.sv", "tb_fourstate.sv",
                              "fourstate.vvp"),
                             ("fourstate_ext.sv", "tb_fourstate_ext.sv",
                              "fourstate_ext.vvp")]:
            subprocess.run(["iverilog", "-g2005-sv",
                            "-o", os.path.join(sim, vvp),
                            os.path.join(fs, "rtl", rtl),
                            os.path.join(fs, "tb", tb)],
                           check=True, capture_output=True, text=True)
            subprocess.run(["vvp", vvp], cwd=sim, check=True,
                           capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        return [{"suite": n, "ok": False, "elapsed": 0.0, "command": "(build)",
                 "output": f"4-state build failed: {detail}"}
                for n, _ in suites]
    # rebuild sessions from scratch so netlist changes are picked up
    for d in ("session", "session_ext"):
        shutil.rmtree(os.path.join(fs, d), ignore_errors=True)
    return [run(n, [PY, os.path.join(fs, script)]) for n, script in suites]


# Project-level verification needs prebuilt assets, located via the
# WAVE_MCP_PROJECT_ASSETS env var (colon-separated) or an optional
# tests/project_assets.txt file. Without either, the suite is skipped.
ASSETS_ENV = "WAVE_MCP_PROJECT_ASSETS"
ASSETS_FILE = os.path.join(HERE, "project_assets.txt")

def project_assets_ready():
    raw = os.environ.get(ASSETS_ENV, "")
    if not raw and os.path.exists(ASSETS_FILE):
        with open(ASSETS_FILE, encoding="utf-8") as f:
            raw = f.read()
    assets = [p.strip() for p in raw.replace("\n", os.pathsep).split(os.pathsep)
              if p.strip()]
    return any(os.path.exists(p) for p in assets)

def main():
    ap = argparse.ArgumentParser(description="wave-mcp regression runner")
    ap.add_argument("--quick", action="store_true",
                    help="skip slow project-level regressions")
    ap.add_argument("--jobs", type=int, default=0,
                    help="concurrent suite slots (default: half the CPUs, "
                         "between 2 and 8; pass 1 to force serial)")
    args = ap.parse_args()

    if args.jobs > 0:
        jobs = args.jobs
    else:
        jobs = max(2, min(8, (os.cpu_count() or 4) // 2))

    tasks = []   # (suite label, callable returning a result dict / list)
    skipped = []

    # ---- unit tests (always runnable: sample session ships in-repo) --------
    unit_tests = [
        ("unit/smoke",
         [PY, os.path.join(HERE, "unit", "smoke_test.py")]),
        ("unit/definition_name",
         [PY, os.path.join(HERE, "unit", "test_definition_name.py")]),
        ("unit/dut_root",
         [PY, os.path.join(HERE, "unit", "test_dut_root.py")]),
        ("unit/diff",
         [PY, os.path.join(HERE, "unit", "test_diff.py")]),
        ("unit/activity",
         [PY, os.path.join(HERE, "unit", "test_activity.py")]),
        ("unit/predicate",
         [PY, os.path.join(HERE, "unit", "test_predicate.py")]),
        ("unit/fingerprint",
         [PY, os.path.join(HERE, "unit", "test_fingerprint.py")]),
        ("unit/storage_policy",
         [PY, os.path.join(HERE, "unit", "test_storage_policy.py")]),
        ("unit/execution_limits",
         [PY, os.path.join(HERE, "unit", "test_execution_limits.py")]),
        ("unit/merge_rename",
         [PY, os.path.join(HERE, "unit", "test_merge_rename.py")]),
        ("unit/query_defaults",
         [PY, os.path.join(HERE, "unit", "test_query_defaults.py")]),
        ("unit/session_isolation",
         [PY, os.path.join(HERE, "unit", "test_session_isolation.py")]),
        ("unit/request_snapshot",
         [PY, os.path.join(HERE, "unit", "test_request_snapshot.py")]),
        ("unit/p3",
         [PY, os.path.join(HERE, "unit", "test_p3_capabilities.py")]),
        ("unit/viewer",
         [PY, os.path.join(HERE, "unit", "test_viewer.py")]),
    ]
    for name, cmd in unit_tests:
        tasks.append((name, (lambda n=name, c=cmd: run(n, c))))

    # ---- protocol check: drives a real stdio server subprocess, so the
    # registered schema and argument marshalling are exercised the way a client
    # hits them (an in-process call can pass while the wire layer refuses) ----
    proto = os.path.join(HERE, "protocol", "check_query_defaults.py")
    if os.path.exists(proto):
        tasks.append(("protocol/query_defaults",
                      lambda: run("protocol/query_defaults", [PY, proto])))
    else:
        skipped.append(("protocol/query_defaults",
                        "protocol check not present in this package"))
    http_proto = os.path.join(HERE, "protocol", "check_http_sessions.py")
    if os.path.exists(http_proto):
        tasks.append(("protocol/http_sessions",
                      lambda: run("protocol/http_sessions", [PY, http_proto])))
    else:
        skipped.append(("protocol/http_sessions",
                        "protocol check not present in this package"))
    token_proto = os.path.join(HERE, "protocol", "check_http_token.py")
    if not os.path.exists(token_proto):
        skipped.append(("protocol/http_token",
                        "protocol check not present in this package"))
    elif importlib.util.find_spec("httpx") is None:
        # httpx is a dev-only client dependency (the server side uses uvicorn);
        # offline bundles do not carry it, so skip rather than fail the run.
        skipped.append(("protocol/http_token",
                        "httpx not available (dev dependency, not in the "
                        "offline wheelhouse; install httpx to enable)"))
    else:
        tasks.append(("protocol/http_token",
                      lambda: run("protocol/http_token", [PY, token_proto])))

    # ---- pytest-based unit suites: pytest is a dev dependency and is not in
    # the offline wheelhouse, so on a bundle host they skip instead of failing
    # the whole run. Where pytest IS available, the dev-file assertions inside
    # skip themselves (see the unit test files). ----
    pytest_tests = [
        ("unit/cli_version",
         [PY, "-m", "pytest", os.path.join(HERE, "unit", "test_cli_version.py"), "-q"]),
        ("unit/fsdb2fst_packaging",
         [PY, "-m", "pytest", os.path.join(HERE, "unit", "test_fsdb2fst_packaging.py"), "-q"]),
    ]
    if _has_pytest():
        for name, cmd in pytest_tests:
            tasks.append((name, (lambda n=name, c=cmd: run(n, c))))
    else:
        for name, _ in pytest_tests:
            skipped.append((name, "pytest not available (dev dependency, not in "
                                  "the offline wheelhouse; install pytest to enable)"))

    # ---- viewer browser e2e (self-skips without assets/playwright) ---------
    # The test file itself is not shipped in the offline bundle (it needs
    # playwright + chromium), so an unconditional dispatch always failed
    # there. Schedule only when the file exists.
    e2e_script = os.path.join(HERE, "viewer_e2e.py")
    if os.path.exists(e2e_script):
        tasks.append(("viewer/e2e",
                      lambda: run("viewer/e2e", [PY, e2e_script])))
    else:
        skipped.append(("viewer/e2e", "test not shipped in this package "
                                      "(needs playwright + chromium)"))

    # ---- 4-state suites (need iverilog; regenerate waves for a fresh run) --
    if shutil.which("iverilog") and shutil.which("vvp"):
        tasks.append(("fourstate", _fourstate_task))
    else:
        skipped.append(("fourstate", "iverilog/vvp not in PATH"))

    # ---- project-level functional verification -----------------------------
    harness = os.path.join(HERE, "functional_verify.py")
    if args.quick:
        skipped.append(("projects", "--quick"))
    elif not os.path.exists(harness):
        skipped.append(("projects", "verification harness not present"))
    elif project_assets_ready():
        tasks.append(("projects/functional_verify",
                      lambda: run("projects/functional_verify", [PY, harness])))
    else:
        skipped.append(("projects", "project assets not configured"))

    print(f"  launching {len(tasks)} suite(s) with up to {jobs} concurrent "
          f"slot(s)...")
    sys.stdout.flush()

    results = []
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = {pool.submit(fn): name for name, fn in tasks}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                got = fut.result()
            except Exception as exc:  # pylint: disable=broad-except
                got = {"suite": name, "ok": False, "elapsed": 0.0,
                       "command": "(runner)",
                       "output": f"suite runner crashed: {exc!r}"}
            for item in (got if isinstance(got, list) else [got]):
                print_block(item)
                results.append(item)

    # ---- summary (stable order regardless of completion order) -------------
    by_name = {r["suite"]: r for r in results}
    print(f"\n{'='*66}\n  REGRESSION SUMMARY\n{'='*66}")
    ok = True
    for name in _SUITE_ORDER:
        r = by_name.get(name)
        if r is None:
            continue
        mark = "PASS" if r["ok"] else "FAIL"
        ok = ok and r["ok"]
        print(f"  [{mark}] {name:32s} {r['elapsed']:>7.1f}s")
    for name, why in skipped:
        print(f"  [SKIP] {name:32s} ({why})")
    print(f"{'='*66}")
    print(f"  overall: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
