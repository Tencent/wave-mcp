"""S5 protocol check: explicit sessions over a real streamable-HTTP server.

Two MCP clients connect to one ``wave-mcp --transport http`` process. Each opens
its own work session on the same design, sets different query defaults, and
queries by explicit ``session_id``; the replies must come from the right
session and share one loaded resource. The listing also has to show the
server-status block, and a retired S5 parameter must be rejected by the wire
schema rather than silently accepted.

Local mode: both clients are the same OS owner, so they can see each other's
sessions. Owner isolation belongs to S6.

Run directly; exits non-zero on any failed check.
"""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SAMPLE = os.path.join(ROOT, "examples", "sample", "session")
CLK = "top_tb.clk"
RST = "top_tb.rst_n"

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


async def _call(cs: ClientSession, name: str, args=None):
    res = await cs.call_tool(name, args or {})
    return res.structured_content or {}, res.is_error, res


async def run(port: int) -> None:
    url = f"http://127.0.0.1:{port}/mcp"
    async with streamable_http_client(url) as (r1, w1), \
            streamable_http_client(url) as (r2, w2):
        async with ClientSession(r1, w1) as a, ClientSession(r2, w2) as b:
            await a.initialize()
            await b.initialize()
            tools = {t.name for t in (await a.list_tools()).tools}
            check("37 tools over HTTP", len(tools) == 37, str(len(tools)))

            print("== two clients, two explicit sessions ==")
            oa, _, _ = await _call(a, "open_session", {"session_path": SAMPLE})
            ob, _, _ = await _call(b, "open_session", {"session_path": SAMPLE})
            sa, sb = oa.get("session_id"), ob.get("session_id")
            check("both opened", bool(sa) and bool(sb) and sa != sb, f"{sa} {sb}")
            check("second open reused the loaded resource",
                  ob.get("resource_shared") is True, str(ob)[:160])

            await _call(a, "query_defaults_set", {"paths": CLK, "session_id": sa})
            await _call(b, "query_defaults_set", {"paths": RST, "session_id": sb})
            va, _, _ = await _call(a, "signal_values", {"limit": 3, "session_id": sa})
            vb, _, _ = await _call(b, "signal_values", {"limit": 3, "session_id": sb})
            check("client A answered from A's defaults",
                  va.get("_query", {}).get("paths") == [CLK], str(va.get("_query")))
            check("client B answered from B's defaults",
                  vb.get("_query", {}).get("paths") == [RST], str(vb.get("_query")))
            check("same dataset, different question",
                  va["_fp"]["dataset"] == vb["_fp"]["dataset"]
                  and va["_fp"]["query"] != vb["_fp"]["query"])

            print("== cross-session addressing is explicit ==")
            vx, _, _ = await _call(b, "signal_values", {"limit": 3, "session_id": sa})
            check("B may address A's session by id (same local owner)",
                  vx.get("_query", {}).get("paths") == [CLK], str(vx)[:120])
            amb, _, _ = await _call(a, "signal_values", {"limit": 3})
            check("omitting session_id with two open -> ambiguous_session",
                  amb.get("error_type") == "ambiguous_session", str(amb)[:120])

            print("== server status and retired parameters ==")
            ls, _, _ = await _call(a, "session_info", {"list_sessions": True})
            check("listing shows both sessions", ls.get("count") == 2, str(ls.get("count")))
            check("listing carries server status with limits",
                  isinstance(ls.get("server"), dict) and "limits" in ls["server"],
                  str(ls.get("server"))[:160])
            _, err, res = await _call(a, "signal_fanin",
                                      {"path": CLK, "transitive": True, "session_id": sa})
            text = " ".join(getattr(c, "text", "") for c in res.content)
            check("retired 'transitive' is rejected on the wire", err, text[:160])
            check("the refusal names the replacement (max_depth)", "max_depth" in text, text[:160])
            _, err, res = await _call(a, "signal_value_at", {"path": CLK, "time": "10ns"})
            text = " ".join(getattr(c, "text", "") for c in res.content)
            check("retired tool name is refused on the wire", err, text[:160])
            check("the refusal names the replacement tool", "signal_values" in text, text[:160])
            _, err, res = await _call(a, "no_such_tool_xyz", {})
            text = " ".join(getattr(c, "text", "") for c in res.content)
            check("a plain unknown tool is still 'Unknown tool'", err and "Unknown tool" in text, text[:160])
            _, err, res = await _call(a, "signal_values", {"paths": CLK, "bogus": 1, "session_id": sa})
            text = " ".join(getattr(c, "text", "") for c in res.content)
            check("an undeclared key without a rename is refused plainly",
                  err and "'bogus'" in text and "renamed" not in text, text[:160])

            print("== concurrent queries from both clients ==")
            outs = await asyncio.gather(*[
                _call(a if i % 2 == 0 else b, "signal_values",
                      {"limit": 3, "session_id": sa if i % 2 == 0 else sb})
                for i in range(8)])
            ok = all(o[0].get("_query", {}).get("paths") == [CLK if i % 2 == 0 else RST]
                     for i, o in enumerate(outs))
            check("8 interleaved calls each answered from their own session", ok)

            await _call(a, "close_session", {"session_id": sa})
            ls, _, _ = await _call(b, "session_info", {"list_sessions": True})
            check("closing A leaves B", ls.get("count") == 1
                  and ls["sessions"][0]["session_id"] == sb, str(ls)[:160])
            vb2, _, _ = await _call(b, "signal_values", {"limit": 3, "session_id": sb})
            check("B still answers after A closed", "_fp" in vb2)
            await _call(b, "close_session", {"session_id": sb})


def main() -> int:
    port = _free_port()
    env = dict(os.environ, WAVE_MCP_SESSION_TTL="0")
    proc = subprocess.Popen([sys.executable, "-m", "wave_mcp.server", "--transport",
                             "http", "--port", str(port)], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        if not _wait_port(port, proc):
            err = proc.stderr.read() if proc.stderr else ""
            print(f"  [FAIL] server did not come up on {port}: {err[-400:]}")
            return 1
        asyncio.run(run(port))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    print(f"\n  http multi-session suite: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("  failed:", ", ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
