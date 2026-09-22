"""S1 protocol check: drive query_defaults_* over a real stdio MCP session.

In-process calls can pass while the wire layer rejects the same arguments (the
P1 lesson: a bare-list annotation made the SDK refuse a plain string even though
the function handled it). This drives an actual server subprocess with the MCP
SDK client, so the registered schema, the argument marshalling and the reply
shape are all exercised the way a client would hit them.

Run directly; exits non-zero on any failed check.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SAMPLE = os.path.join(ROOT, "examples", "sample", "session")
CLK = "top_tb.clk"
RST = "top_tb.rst_n"

PASSED, FAILED = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSED if cond else FAILED).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  ({detail})" if detail and not cond else ""))


async def main() -> int:
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "wave_mcp.server"], cwd=ROOT)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as cs:
            await cs.initialize()
            tools = {t.name: t for t in (await cs.list_tools()).tools}
            print("== registered tool names ==")
            check("tool count is 37", len(tools) == 37, str(len(tools)))
            for name in ("query_defaults_set", "query_defaults_get",
                         "query_defaults_clear"):
                check(f"{name} is listed", name in tools)
            for name in ("cursor_set", "cursor_get", "cursor_clear"):
                check(f"{name} is not listed", name not in tools)

            print("== the wire schema accepts both path forms ==")
            prop = tools["query_defaults_set"].input_schema["properties"]["paths"]
            variants = prop.get("anyOf") or prop.get("oneOf") or [prop]
            kinds = {v.get("type") for v in variants}
            check("paths offers string and array", {"string", "array"} <= kinds,
                  str(prop))
            check("defaults_revision is exposed",
                  "defaults_revision"
                  in tools["query_defaults_set"].input_schema["properties"])

            async def call(name, args=None):
                res = await cs.call_tool(name, args or {})
                return res.structured_content or {}, res.is_error

            out, _ = await call("open_session", {"session_path": SAMPLE})
            sid = out.get("session_id")
            check("session opened over stdio", bool(sid), str(out)[:120])

            print("== calls over the wire ==")
            out, err = await call("query_defaults_set",
                                  {"paths": CLK, "start": "10ns",
                                   "session_id": sid})
            check("a bare string path is accepted on the wire",
                  not err and out.get("query_defaults", {}).get("paths") == [CLK],
                  str(out)[:200])

            out, err = await call("query_defaults_set",
                                  {"paths": [CLK, RST], "end": "30ns",
                                   "session_id": sid})
            check("an array of paths is accepted too",
                  not err
                  and out["query_defaults"]["paths"] == [CLK, RST]
                  and out["query_defaults"]["end"] == 30, str(out)[:200])

            rev = out["query_defaults"]["revision"]
            out, err = await call("query_defaults_set",
                                  {"paths": CLK, "defaults_revision": rev,
                                   "session_id": sid})
            check("defaults_revision accepts the current revision",
                  not err and out.get("status") == "ok", str(out)[:200])
            out, err = await call("query_defaults_set",
                                  {"paths": CLK, "defaults_revision": rev,
                                   "session_id": sid})
            check("a stale defaults_revision is refused over the wire",
                  out.get("error_type") == "defaults_conflict", str(out)[:200])

            out, _ = await call("query_defaults_get", {"session_id": sid})
            check("get reports the revision",
                  isinstance(out.get("query_defaults", {}).get("revision"), int),
                  str(out)[:160])

            out, _ = await call("signal_values", {"session_id": sid})
            check("a query uses the stored defaults and says so",
                  out.get("_defaults_used") is True
                  and out.get("_fp") is not None, str(out)[:200])

            print("== an unknown name is rejected, and the session survives ==")
            # cursor_set never shipped, so it is just an unknown tool here.
            res = await cs.call_tool("cursor_set", {"paths": CLK})
            check("calling cursor_set returns an error result", res.is_error)
            text = " ".join(getattr(b, "text", "") or "" for b in res.content)
            check("the error names the unknown tool",
                  "cursor_set" in text, text[:160])
            check("the error does not pretend the call worked",
                  res.structured_content is None, str(res.structured_content))

            out, _ = await call("query_defaults_get", {"session_id": sid})
            check("the session still answers after that",
                  out.get("status") == "ok", str(out)[:120])

            out, _ = await call("query_defaults_clear", {"session_id": sid})
            check("clear empties the defaults",
                  out["query_defaults"]["is_set"] is False, str(out)[:160])

    print(f"\n  protocol check: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("  failed:", FAILED)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
