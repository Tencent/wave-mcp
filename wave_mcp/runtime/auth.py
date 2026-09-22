"""Optional shared-secret guard for the HTTP transport.

wave-mcp trusts the OS account it runs as; there is no user model inside the
server. The one thing an HTTP listener adds is that *other* processes on the
host or network could reach the process. ``WAVE_MCP_TOKEN`` closes that gap:
when set, every HTTP request must carry ``Authorization: Bearer <token>`` with
the same value, and the server refuses to bind a non-loopback address without
it. Stdio never sees any of this.

The token is a shared secret between the person who started the server and
their own clients (``mcp.json`` ``headers``), not an account: the server does
not know *who* is calling, only that the caller was told the secret. Any
random string works (``openssl rand -hex 32``). It is compared in constant
time and never logged or echoed.
"""
from __future__ import annotations

import hmac
import os
from typing import Optional

TOKEN_ENV = "WAVE_MCP_TOKEN"
MIN_TOKEN_LEN = 16

#: Hosts that only the local machine can reach.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class TokenConfigError(Exception):
    """``WAVE_MCP_TOKEN`` is set but unusable; the server must not start."""


def configured_token() -> Optional[str]:
    """The token from the environment, validated; None when unset."""
    raw = os.environ.get(TOKEN_ENV)
    if raw is None or raw == "":
        return None
    tok = raw.strip()
    if len(tok) < MIN_TOKEN_LEN:
        raise TokenConfigError(
            f"{TOKEN_ENV} must be at least {MIN_TOKEN_LEN} characters; "
            f"generate one with: openssl rand -hex 32")
    if any(c.isspace() for c in tok):
        raise TokenConfigError(f"{TOKEN_ENV} must not contain whitespace")
    return tok


def is_loopback(host: str) -> bool:
    return host in LOOPBACK_HOSTS


def bearer_matches(header: Optional[str], token: str) -> bool:
    """Whether an ``Authorization`` header carries exactly ``token``."""
    if not header:
        return False
    scheme, _, value = header.strip().partition(" ")
    if scheme.lower() != "bearer" or not value:
        return False
    return hmac.compare_digest(value.strip().encode("utf-8"),
                               token.encode("utf-8"))


class BearerTokenMiddleware:
    """Pure ASGI middleware: refuse HTTP requests without the token.

    Sits in front of the whole app so nothing, not the MCP endpoint nor any
    future route, answers an unauthenticated request. A refusal is a plain
    401 with ``WWW-Authenticate: Bearer`` and a body that names the variable
    to set; it never reveals whether the token was close.
    """

    def __init__(self, app, token: str) -> None:
        self.app = app
        self._token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        header = None
        for name, value in scope.get("headers") or ():
            if name == b"authorization":
                header = value.decode("latin-1")
                break
        if bearer_matches(header, self._token):
            await self.app(scope, receive, send)
            return
        body = (b'{"error":"unauthorized","hint":"send Authorization: Bearer '
                b'<WAVE_MCP_TOKEN>; the value the server was started with"}')
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode()),
                                (b"www-authenticate", b"Bearer")]})
        await send({"type": "http.response.body", "body": body})


__all__ = ["TOKEN_ENV", "MIN_TOKEN_LEN", "LOOPBACK_HOSTS", "TokenConfigError",
           "configured_token", "is_loopback", "bearer_matches",
           "BearerTokenMiddleware"]
