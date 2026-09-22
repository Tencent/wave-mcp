"""Who a request runs as.

wave-mcp runs as the OS account that started it, and that account is the one
owner every request belongs to. The server does not model tenants: a shared
host is served by one process per user (stdio, or HTTP bound to loopback or
protected by ``WAVE_MCP_TOKEN``), and what a user may read or write is decided
by the operating system, not re-implemented here.

``owner_id`` is kept on sessions and resources so the session layer has one
place to ask "whose is this" should a front door ever route requests to
per-user workers; today it is always ``LOCAL_OWNER``.
"""
from __future__ import annotations


class Principal:
    """The identity a request runs as."""

    __slots__ = ("owner_id",)

    def __init__(self, owner_id: str) -> None:
        self.owner_id = owner_id

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"Principal(owner_id={self.owner_id!r})"


#: The owner every request runs as.
LOCAL_OWNER = "local"


def local_principal() -> Principal:
    """The single-user identity used by stdio, the CLI and HTTP alike."""
    return Principal(LOCAL_OWNER)
