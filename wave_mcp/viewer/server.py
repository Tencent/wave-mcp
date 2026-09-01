"""HTTP server for the wave-mcp viewer.

Single local origin serving:
  * ``/``            -> shell assets (our MIT web/ dir), then Surfer WASM dir
  * ``/surver/*``    -> reverse proxy to the local surver (restores the
                        ``Server: Surfer`` header that reverse proxies strip)
  * ``/api/view-state`` GET (?since= long-poll) / PUT desired / POST actual
"""
from __future__ import annotations

import json
import posixpath
import socketserver
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from .state import ViewState, ViewStateError


class ViewerServer:
    def __init__(self, wasm_dir: str, shell_dir: str, surver_base: str,
                 state: ViewState, port: int = 0) -> None:
        self.wasm_dir = Path(wasm_dir)
        self.shell_dir = Path(shell_dir)
        self.surver_base = surver_base.rstrip("/")
        self.state = state
        handler = _make_handler(self)
        self.httpd = _ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever,
                                        daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


class _ThreadingHTTPServer(socketserver.ThreadingMixIn,
                           socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def _make_handler(owner: ViewerServer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # surfer web client detects a surver by `Server: Surfer`
        def version_string(self) -> str:
            return "Surfer"

        def log_message(self, fmt: str, *args) -> None:
            pass  # quiet; MCP stdio must stay clean

        def handle(self) -> None:
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError):
                pass  # client went away mid-request; routine for long-poll

        # -- helpers ----------------------------------------------------

        def _send(self, code: int, body: bytes,
                  ctype: str = "application/octet-stream",
                  extra: Optional[Dict[str, str]] = None) -> None:
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cross-Origin-Opener-Policy", "same-origin")
                self.send_header("Cross-Origin-Embedder-Policy",
                                 "require-corp")
                self.send_header("Cross-Origin-Resource-Policy",
                                 "cross-origin")
                self.send_header("Cache-Control", "no-store")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if body:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # long-poll clients drop connections routinely (page reload,
                # tab close); never let that surface as a traceback
                pass

        def _json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(obj).encode(),
                       "application/json; charset=utf-8")

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        # -- proxy ------------------------------------------------------

        def _proxy(self, send_body: bool) -> None:
            upstream = owner.surver_base + self.path[len("/surver"):]
            req = urllib.request.Request(
                upstream, method="GET" if send_body else "HEAD")
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    body = r.read() if send_body else b""
                    extra = {}
                    for k in ("x-wellen-version", "x-surfer-version"):
                        v = r.headers.get(k)
                        if v:
                            extra[k] = v
                    self._send(200, body,
                               r.headers.get("Content-Type",
                                             "application/octet-stream"),
                               extra)
            except urllib.error.HTTPError as e:
                self._send(e.code, b"")
            except OSError:
                self._send(502, b"")

        # -- static -----------------------------------------------------

        _CTYPES = {".html": "text/html; charset=utf-8",
                   ".js": "application/javascript",
                   ".css": "text/css",
                   ".wasm": "application/wasm",
                   ".json": "application/json",
                   ".png": "image/png", ".ico": "image/x-icon",
                   ".svg": "image/svg+xml"}

        # ``index.html`` is the Surfer WASM entry point that the shell loads in
        # its iframe. It must always resolve to wasm_dir, otherwise a page of
        # the same name shipped in shell_dir would shadow it and the viewer
        # would never boot.
        _WASM_FIRST = ("index.html",)

        def _static(self) -> None:
            rel = posixpath.normpath(urlparse(self.path).path.lstrip("/"))
            if rel in ("", "."):
                rel = "shell.html"
            if ".." in rel.split("/"):
                self._send(403, b"")
                return
            roots = ((owner.wasm_dir, owner.shell_dir)
                     if rel in self._WASM_FIRST
                     else (owner.shell_dir, owner.wasm_dir))
            for root in roots:
                fp = root / rel
                if fp.is_file():
                    ctype = self._CTYPES.get(fp.suffix,
                                             "application/octet-stream")
                    self._send(200, fp.read_bytes(), ctype)
                    return
            self._send(404, b"not found", "text/plain")

        # -- verbs ------------------------------------------------------

        def do_HEAD(self) -> None:
            if self.path.startswith("/surver/"):
                self._proxy(send_body=False)
            else:
                self._static()

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if self.path.startswith("/surver/"):
                self._proxy(send_body=True)
            elif parsed.path == "/api/view-state":
                qs = parse_qs(parsed.query)
                since = qs.get("since")
                if since:
                    snap = owner.state.wait_change(int(since[0]))
                else:
                    snap = owner.state.snapshot()
                self._json(200, snap)
            else:
                self._static()

        def do_PUT(self) -> None:
            if urlparse(self.path).path != "/api/view-state":
                self._send(404, b"")
                return
            try:
                payload = json.loads(self._read_body() or b"{}")
                rev = owner.state.update_desired(
                    signals=payload.get("signals"),
                    cursor=payload.get("cursor"),
                    viewport=payload.get("viewport"),
                    markers=payload.get("markers"),
                    diff=payload.get("diff"),
                    annotations=payload.get("annotations"),
                )
                self._json(200, {"ok": True, "revision": rev})
            except (ViewStateError, ValueError) as e:
                self._json(400, {"ok": False, "error": str(e)})

        def do_POST(self) -> None:
            # browser shell writes back actual state
            if urlparse(self.path).path != "/api/view-state/actual":
                self._send(404, b"")
                return
            try:
                payload = json.loads(self._read_body() or b"{}")
                owner.state.write_actual(payload)
                self._json(200, {"ok": True})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})

    return Handler
