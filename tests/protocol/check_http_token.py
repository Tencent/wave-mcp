#!/usr/bin/env python3
"""Protocol check: the HTTP transport's shared-secret guard (``WAVE_MCP_TOKEN``).

Startup rules
  * ``--host 0.0.0.0`` without the token is refused, and the message names
    the variable to set;
  * a too-short token is refused;
  * loopback without a token starts as before (check_http_sessions covers it).

Request rules, against a real server started with the token
  * no ``Authorization`` header -> 401 before any tool runs;
  * wrong token -> 401;
  * right token -> full tool surface, sessions work.

Also checks that ``prepare_session`` without ``out_dir`` lands the session under
``WAVE_MCP_SESSION_ROOT`` at the inputs' identity, that an explicit
``out_dir`` is used as given, and that ``WAVE_MCP_AUDIT_LOG`` records one JSON
line per call with no argument text in it.

Run directly; exits non-zero on any failed check.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SAMPLE = os.path.join(ROOT, "examples", "sample", "session")
SAMPLE_FST = os.path.join(ROOT, "examples", "sample", "dump.fst")
SAMPLE_F = os.path.join(ROOT, "examples", "sample", "rtl.f")
TOKEN = "protocol-check-token-0123456789abcdef"

PASSED, FAILED = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSED if cond else FAILED).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  ({detail})" if detail and not cond else ""))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port(port: int, proc: subprocess.Popen, timeout: float = 20) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _startup_refuses(label: str, argv: list, env: dict, expect: str) -> None:
    proc = subprocess.run([sys.executable, "-m", "wave_mcp.server", *argv],
                          cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=60)
    check(f"startup refused: {label}", proc.returncode != 0 and expect in proc.stderr,
          proc.stderr[-300:])


async def _call(cs: ClientSession, name: str, args=None):
    res = await cs.call_tool(name, args or {})
    return res.structured_content or {}, res.is_error, res


async def _refused(url: str, label: str, headers: dict) -> None:
    async with httpx.AsyncClient() as c:
        r = await c.post(url, headers={**headers, "Accept": "application/json, text/event-stream",
                                       "Content-Type": "application/json"},
                         json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2025-06-18",
                                          "capabilities": {},
                                          "clientInfo": {"name": "x", "version": "0"}}})
    check(f"{label} -> 401", r.status_code == 401, f"{r.status_code} {r.text[:120]}")
    check(f"{label} -> WWW-Authenticate: Bearer",
          r.headers.get("www-authenticate", "").lower().startswith("bearer"))
    check(f"{label} -> hint names WAVE_MCP_TOKEN", "WAVE_MCP_TOKEN" in r.text)


async def run(port: int, session_root: str, tmp: str) -> None:
    url = f"http://127.0.0.1:{port}/mcp"
    print("== without or with a wrong token ==")
    await _refused(url, "no Authorization header", {})
    await _refused(url, "wrong token", {"Authorization": "Bearer not-the-token-at-all-xxx"})
    await _refused(url, "wrong scheme", {"Authorization": f"Basic {TOKEN}"})

    print("== with the token ==")
    client = httpx.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"})
    async with streamable_http_client(url, http_client=client) as (r, w):
        async with ClientSession(r, w) as cs:
            await cs.initialize()
            tools = {t.name for t in (await cs.list_tools()).tools}
            check("37 tools over authenticated HTTP", len(tools) == 37, str(len(tools)))
            o, _, _ = await _call(cs, "open_session", {"session_path": SAMPLE})
            check("open_session works", bool(o.get("session_id")), str(o)[:160])
            ls, _, _ = await _call(cs, "session_info", {"list_sessions": True})
            check("server status has no deployment field",
                  "deployment" not in (ls.get("server") or {}), str(ls.get("server"))[:120])
            await _call(cs, "close_session", {"session_id": o["session_id"]})

            print("== out_dir optional ==")
            p1, _, _ = await _call(cs, "prepare_session",
                                   {"wave_path": SAMPLE_FST, "top": "top_tb",
                                    "filelist_path": SAMPLE_F})
            sp = p1.get("session_path", "")
            check("prepare without out_dir succeeds", p1.get("status") == "ready", str(p1)[:200])
            check("lands under WAVE_MCP_SESSION_ROOT", sp.startswith(session_root + os.sep), sp)
            check("directory named by the 16-hex identity",
                  re.fullmatch(r"[0-9a-f]{16}", os.path.basename(sp)) is not None, sp)
            p2, _, _ = await _call(cs, "prepare_session",
                                   {"wave_path": SAMPLE_FST, "top": "top_tb",
                                    "filelist_path": SAMPLE_F})
            check("same inputs -> same session_path", p2.get("session_path") == sp)
            reused = [s for s in p2.get("steps", []) if s.get("step") == "build_netlist"]
            check("second prepare reused the netlist",
                  bool(reused) and reused[0].get("reused") is True, str(reused)[:160])
            st, _, _ = await _call(cs, "open_static_session",
                                   {"top": "top_tb", "filelist_path": SAMPLE_F})
            st_steps = [s for s in st.get("steps", []) if s.get("step") == "build_netlist"]
            check("static session on the same sources reuses that netlist too",
                  bool(st_steps) and st_steps[0].get("reused") is True, str(st_steps)[:160])
            explicit = os.path.join(tmp, "my-sess")
            p3, _, _ = await _call(cs, "prepare_session",
                                   {"wave_path": SAMPLE_FST, "out_dir": explicit,
                                    "top": "top_tb", "filelist_path": SAMPLE_F})
            check("explicit out_dir used as given", p3.get("session_path") == explicit,
                  str(p3.get("session_path")))
            check("explicit out_dir has its own netlist inside",
                  os.path.exists(os.path.join(explicit, "netlist", "maps.json")))
            for sid in (p1.get("session_id"), p2.get("session_id"),
                        st.get("session_id"), p3.get("session_id")):
                if sid:
                    await _call(cs, "close_session", {"session_id": sid})


def _check_audit(path: str, tmp: str) -> None:
    import json
    import stat
    print("== audit log ==")
    check("audit file exists", os.path.exists(path), path)
    if not os.path.exists(path):
        return
    check("audit file is 0600", stat.S_IMODE(os.stat(path).st_mode) == 0o600,
          oct(os.stat(path).st_mode))
    lines = [l for l in open(path, encoding="utf-8").read().splitlines() if l.strip()]
    recs = []
    for l in lines:
        try:
            recs.append(json.loads(l))
        except ValueError:
            recs.append(None)
    check("every line is JSON", all(r is not None for r in recs), str(lines[:2]))
    recs = [r for r in recs if r]
    tools = [r.get("tool") for r in recs]
    check("open_session / prepare_session / close_session recorded",
          {"open_session", "prepare_session", "close_session"} <= set(tools), str(sorted(set(tools))))
    check("records carry ts, request_id, status, elapsed_s",
          all({"ts", "request_id", "status", "elapsed_s"} <= set(r) for r in recs))
    prep = [r for r in recs if r.get("tool") == "prepare_session" and r.get("status") == "ok"]
    check("prepare_session ok records name the dataset identity",
          bool(prep) and all(isinstance(r.get("dataset"), dict) and "identity" in r["dataset"]
                             for r in prep), str(prep[:1]))
    blob = "\n".join(lines)
    check("no path or argument text in the audit log",
          tmp not in blob and "dump.fst" not in blob and "top_tb" not in blob and SAMPLE not in blob)
    check("request ids are unique", len({r["request_id"] for r in recs}) == len(recs))


def main() -> int:
    if not os.path.exists(SAMPLE_FST):
        print("  [SKIP] sample waveform missing")
        return 0
    tmp = tempfile.mkdtemp(prefix="wave-mcp-token-")
    session_root = os.path.join(tmp, "sessions")
    audit_path = os.path.join(tmp, "audit", "wave-mcp.jsonl")
    base_env = dict(os.environ, WAVE_MCP_SESSION_TTL="0",
                    WAVE_MCP_SESSION_ROOT=session_root,
                    WAVE_MCP_AUDIT_LOG=audit_path)
    base_env.pop("WAVE_MCP_TOKEN", None)
    port = _free_port()
    print("== startup rules ==")
    _startup_refuses("non-loopback host without token",
                     ["--transport", "http", "--host", "0.0.0.0", "--port", str(port)],
                     base_env, "WAVE_MCP_TOKEN")
    _startup_refuses("token too short",
                     ["--transport", "http", "--port", str(port)],
                     dict(base_env, WAVE_MCP_TOKEN="short"), "at least")

    env = dict(base_env, WAVE_MCP_TOKEN=TOKEN)
    proc = subprocess.Popen([sys.executable, "-m", "wave_mcp.server", "--transport",
                             "http", "--port", str(port)], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        if not _wait_port(port, proc):
            err = proc.stderr.read() if proc.stderr else ""
            print(f"  [FAIL] server did not come up on {port}: {err[-400:]}")
            return 1
        asyncio.run(run(port, session_root, tmp))
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        _check_audit(audit_path, tmp)
    finally:
        if proc.poll() is None:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n  http token suite: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("  failed:", ", ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
