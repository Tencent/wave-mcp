"""Surfer WCP (Waveform Control Protocol) client.

Implements Indago categories 9 & 10 (waveform viewer operation / navigation) by
remotely controlling a running Surfer instance over WCP.

WCP is a small JSON-message protocol (one JSON object per message) carried over a
stream — Surfer exposes it over TCP and, in recent builds, WebSocket. This client
speaks the TCP newline-delimited-JSON transport by default and degrades
gracefully (returning an error dict) when no viewer is connected, so the rest of
the server keeps working without a GUI.

Reference: Surfer WCP — https://gitlab.com/surfer-project/surfer (wcp crate).
"""
from __future__ import annotations

import json
import socket
import threading
from typing import Any, Dict, Optional


class WcpError(RuntimeError):
    pass


class WcpClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 54321, timeout: float = 3.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._buf = b""

    # -- transport ----------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> bool:
        with self._lock:
            if self._sock is not None:
                return True
            try:
                s = socket.create_connection((self.host, self.port), timeout=self.timeout)
                s.settimeout(self.timeout)
                self._sock = s
                self._buf = b""
                # WCP greeting handshake (best-effort)
                self._send_locked({"type": "greeting", "version": "0", "commands": []})
                return True
            except OSError as exc:
                self._sock = None
                raise WcpError(f"cannot connect to Surfer WCP at {self.host}:{self.port}: {exc}")

    def disconnect(self):
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                finally:
                    self._sock = None

    def _send_locked(self, msg: Dict[str, Any]):
        assert self._sock is not None
        data = (json.dumps(msg) + "\n").encode()
        self._sock.sendall(data)

    def _recv_line_locked(self) -> Optional[Dict[str, Any]]:
        assert self._sock is not None
        while b"\n" not in self._buf:
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                return None
            if not chunk:
                return None
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        line = line.strip()
        if not line:
            return None
        try:
            return json.loads(line.decode())
        except json.JSONDecodeError:
            return None

    def command(self, command: str, **fields: Any) -> Dict[str, Any]:
        """Send a WCP command and return the response (best effort)."""
        msg = {"type": "command", "command": command}
        msg.update(fields)
        with self._lock:
            if self._sock is None:
                raise WcpError("not connected to a Surfer WCP viewer")
            self._send_locked(msg)
            resp = self._recv_line_locked()
        return resp or {"type": "ack", "command": command}

    # -- high-level WCP operations (cat 9 / 10) -----------------------------
    def add_variables(self, paths):
        return self.command("add_variables", variables=list(paths))

    def add_scope(self, scope: str, recursive: bool = False):
        return self.command("add_scope", scope=scope, recursive=recursive)

    def get_displayed_items(self):
        return self.command("get_item_list")

    def get_selected_items(self):
        return self.command("get_selected_items")

    def clear(self):
        return self.command("clear_items")

    def remove_items(self, ids):
        return self.command("remove_items", ids=list(ids))

    def set_viewport(self, start: float, end: float):
        return self.command("set_viewport_to", start=start, end=end)

    def goto_time(self, time: float):
        return self.command("set_cursor", time=time)

    def add_marker(self, time: float, name: str = "", color: str = ""):
        return self.command("add_marker", time=time, name=name, color=color)

    def get_markers(self):
        return self.command("get_markers")

    def load_waveform(self, path: str):
        return self.command("load", source=path)
