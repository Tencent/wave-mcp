"""A test-side client that always names its session.

Every session-scoped MCP call carries a ``session_id`` in production. Tests that
lean on the single-session fallback would hide a whole class of bug (an id
resolving to the wrong session, or a call quietly answering about another
conversation's data) and would start failing the moment a second session
existed, which is exactly what the multi-session work introduces. So each test
opens its own work session through the public tool and every call names it.

Usage in a test stage::

    from session_client import bound
    srv = bound(SAMPLE)          # shadows the module for this scope
    srv.signal_values(CLK)       # carries the session_id explicitly

Non-session attributes (``mcp``, ``SESSIONS``, the rename-hint helpers) are
forwarded to the real module unchanged, so one name covers both.
"""
from __future__ import annotations

import inspect
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(HERE, "..", "..")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from wave_mcp import server as _server            # noqa: E402


class SessionClient:
    """The server module, with session-scoped calls pinned to one session."""

    __slots__ = ("_module", "session_id")

    def __init__(self, session_path: str, module=None) -> None:
        object.__setattr__(self, "_module",
                           module if module is not None else _server)
        reply = self._module.open_session(session_path)
        if reply.get("status") != "connected":
            raise RuntimeError(f"could not open {session_path}: {reply}")
        object.__setattr__(self, "session_id", reply["session_id"])

    def __getattr__(self, name: str):
        # Private names must not fall through, or the slots above would recurse
        # into this method while they are still unset.
        if name.startswith("_"):
            raise AttributeError(name)
        fn = getattr(self._module, name)
        try:
            accepts = "session_id" in inspect.signature(fn).parameters
        except (TypeError, ValueError):
            accepts = False
        if not accepts:
            return fn

        def call(*args, **kwargs):
            kwargs.setdefault("session_id", self.session_id)
            return fn(*args, **kwargs)

        return call

    # -- the resource behind the session, for engine cross-checks -----------
    @property
    def fst(self):
        return self._module.SESSIONS.get(self.session_id).fst

    @property
    def resource(self):
        return self._module.SESSIONS.get(self.session_id).resource

    @property
    def defaults(self):
        return self._module.SESSIONS.get(self.session_id).query_defaults

    def close(self) -> dict:
        return self._module.close_session(session_id=self.session_id)

    def new_session(self, session_path: str) -> dict:
        """Open *another* session through the public tool.

        Goes to the module rather than through ``__getattr__`` because the
        forwarded ``open_session`` would carry this client's session_id and be
        read as a resume request.
        """
        return self._module.open_session(session_path)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"SessionClient(session_id={self.session_id!r})"


def bound(session_path: str, module=None) -> SessionClient:
    """Open a session through the public tool and bind a client to it."""
    return SessionClient(session_path, module)
