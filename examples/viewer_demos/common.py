#!/usr/bin/env python3
"""Shared plumbing for the viewer demos: stdio MCP driver + paths.

Each demo spawns wave-mcp over stdio, speaks JSON-RPC, and drives the
viewer exactly like a coding agent would. The viewer URL is printed; open
it in a browser to watch the scenario live.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent            # wave_mcp repo root

# wave-mcp package import path (repo checkout) — adjust if pip-installed
sys.path.insert(0, str(REPO))


class DemoDriver:
    """Minimal MCP stdio client for the demos."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self._id = 0
        self.view_id: str | None = None
        self.last_result: dict = {}

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        env = dict(os.environ)
        # viewer assets: pinned deploy/viewer-assets first (see its
        # PROVENANCE.md), then the docker staging dir, then pip package
        for cand in (REPO / "deploy" / "viewer-assets",
                     REPO / "deploy" / ".docker-build-cache" / "viewer-staged"):
            if cand.is_dir() and (cand / "wasm" / "index.html").is_file():
                env.setdefault("WAVE_MCP_VIEWER_ASSETS", str(cand))
                break
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "wave_mcp.server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            env=env, text=True)
        self._send("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "viewer-demo", "version": "0"}})
        self.notify("notifications/initialized")

    def stop(self) -> None:
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()

    def hold(self) -> None:
        """Keep the server (and the viewer URL) alive for inspection.

        Three modes:
        - ``--hold-sleep``: sleep forever without touching stdin (suitable
          for nohup/multiplexed deployments where there is no TTY).
        - ``--hold`` or ``DEMO_HOLD=1``: wait for Enter on stdin.
        - neither: behave exactly like stop().
        """
        if "--hold-sleep" in sys.argv:
            print("[demo] --hold-sleep: viewer stays alive without stdin. "
                  "Ctrl+C to stop.", flush=True)
            try:
                import time
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
            self.stop()
            return
        if "--hold" in sys.argv or os.environ.get("DEMO_HOLD"):
            print("[demo] viewer stays up. Open the URL above, poke around, "
                  "then press Enter here to finish.")
            try:
                input()
            except (KeyboardInterrupt, EOFError):
                pass
        self.stop()

    # -- rpc -------------------------------------------------------------
    def notify(self, method: str, params: dict | None = None) -> None:
        """JSON-RPC notification: fire and forget (no id, no response)."""
        assert self.proc, "call start() first"
        req = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            req["params"] = params
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()

    def _send(self, method: str, params: dict | None = None) -> dict:
        assert self.proc, "call start() first"
        self._id += 1
        req = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            req["params"] = params
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError(f"server closed during {method}")
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg

    def call(self, tool: str, args: dict | None = None) -> dict | str:
        """Call an MCP tool; remember the structured result, print a summary."""
        r = self._send("tools/call", {"name": tool, "arguments": args or {}})
        self.last_result = r.get("result", {})
        structured = self.last_result.get("structuredContent")
        text = "\n".join(c.get("text", "") for c in
                         self.last_result.get("content", [])
                         if c.get("type") == "text")

        # keep the demos readable: show the headline fields only
        brief: dict | str = structured if isinstance(structured, dict) else text
        if tool == "open_wave_view" and isinstance(brief, dict):
            self.view_id = brief.get("view_id")
            print(f"[viewer] view_id={self.view_id}")
            print(f"[viewer] open in browser: {brief.get('url')}")
            print(f"[viewer] ssh port-forward: {brief.get('ssh_hint')}")
        elif isinstance(brief, dict) and brief.get("status") == "error":
            print(f"[!] {tool} error: {brief.get('error')}")
        else:
            print(f"[ok] {tool}")
        return brief

    def last_structured(self) -> dict:
        sc = self.last_result.get("structuredContent")
        return sc if isinstance(sc, dict) else {}


def as_time(t) -> str:
    """FST value rows carry a unit suffix already ('125s'); pass through.

    Guard against double suffixes if a row ever lacks one.
    """
    s = str(t)
    return s if s and s[-1].isalpha() else s + "s"
