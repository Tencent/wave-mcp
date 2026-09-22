"""One request's fixed view of the world.

A tool call resolves several things before it can run: who is asking, which
work session that maps to, what the session's query defaults were at that
moment, and what the call's arguments mean once defaults and units are applied.
Doing any of that twice (once in the tool body, again in a fingerprint wrapper)
is how two halves of one reply end up describing two different states. The
snapshot resolves everything once, up front, and everything downstream reads it.

The snapshot also collects the *effective* parameters as they are normalized
(signal list after the defaults filled it, times in waveform units after
parsing), which is what the reply's ``_query`` reports and what the
``_fp.query`` digest is computed from. Two calls that end up asking the same
question therefore carry the same digest whether the caller spelled every
argument out or leaned on the defaults.
"""
from __future__ import annotations

import contextvars
from typing import Any, Dict, List, Optional, Set

from .context import Principal
from .identity import question_digest

#: Bumped whenever the *meaning* of a tool's arguments or of the ``_query``
#: normalization changes, so a digest from an older server is never mistaken
#: for the same question.
API_SCHEMA_VERSION = "0.3"


class RequestSnapshot:
    """Everything one tool call reads from, fixed at the start of the call.

    ``defaults`` is a copy: a concurrent ``query_defaults_set`` on the same
    session changes the store, not this request. ``effective`` starts as the
    caller's arguments and is overwritten with normalized values by the
    resolvers while the tool runs; it is what ``digest()`` hashes.
    """

    __slots__ = ("tool", "principal", "session", "lease", "defaults",
                 "arguments", "effective", "from_defaults", "mode",
                 "timescale_exp")

    def __init__(self, tool: str, principal: Principal, session,
                 lease, defaults: Dict[str, Any],
                 arguments: Dict[str, Any]) -> None:
        self.tool = tool
        self.principal = principal
        self.session = session
        self.lease = lease
        self.defaults = defaults
        self.arguments = dict(arguments)
        # Argument names that only steer the request (which session, which
        # revision) are not part of the question being asked.
        self.effective: Dict[str, Any] = {
            k: v for k, v in arguments.items()
            if k not in ("session_id", "defaults_revision") and v is not None}
        self.from_defaults: Set[str] = set()
        self.mode: Optional[str] = None
        fst = getattr(session, "fst", None) if session is not None else None
        self.timescale_exp = fst.timescale_exp if fst is not None else None

    # -- recording what the call actually asked ------------------------------
    def set(self, name: str, value: Any, *, from_default: bool = False) -> None:
        """Record a normalized effective value for one argument."""
        self.effective[name] = value
        if from_default:
            self.from_defaults.add(name)

    def unset(self, name: str) -> None:
        self.effective.pop(name, None)

    @property
    def defaults_revision(self) -> Optional[int]:
        return self.defaults.get("revision") if self.defaults else None

    # -- what the reply reports ---------------------------------------------
    def query_report(self, fmt_time) -> Dict[str, Any]:
        """Compact description of the question that was actually answered.

        Only applicable fields appear: a point read has ``time``, a window read
        has ``start``/``end``, a structural query has neither. Times are given
        in their readable form; the digest uses the integer units underneath.
        """
        out: Dict[str, Any] = {}
        if self.mode:
            out["mode"] = self.mode
        for key in ("path", "paths"):
            if key in self.effective:
                out[key] = self.effective[key]
        for key in ("time", "start", "end"):
            val = self.effective.get(key)
            if isinstance(val, int) and not isinstance(val, bool):
                out[key] = fmt_time(val, self.timescale_exp)
            elif val is not None:
                out[key] = val
        if "limit" in self.effective:
            out["limit"] = self.effective["limit"]
        if self.defaults_revision is not None:
            out["defaults_revision"] = self.defaults_revision
        if self.from_defaults:
            out["from_defaults"] = sorted(self.from_defaults)
        return out

    def digest(self) -> str:
        """Hash of the normalized question, independent of how it was phrased.

        Excludes the session, the owner, the defaults revision and whether a
        value came from the defaults: those say *who* asked and *how*, not
        *what*. Includes the tool, the schema version and the timescale, because
        the same integer means a different instant under a different timescale.
        """
        return question_digest(self.tool, self.effective,
                               schema=API_SCHEMA_VERSION,
                               timescale_exp=self.timescale_exp, mode=self.mode)

    def fingerprint(self) -> Dict[str, Any]:
        """``_fp`` for a session-scoped query: ``{dataset, query}``.

        ``dataset`` is the session's dataset identity and version plus the
        version of each primary input; ``query`` is the question digest.
        """
        out: Dict[str, Any] = {}
        if self.session is not None:
            out["dataset"] = self.session.fingerprint()
        out["query"] = self.digest()
        return out


#: The snapshot of the request running on this thread, if any.
CURRENT: contextvars.ContextVar[Optional[RequestSnapshot]] = \
    contextvars.ContextVar("wave_mcp_request", default=None)


def current() -> Optional[RequestSnapshot]:
    return CURRENT.get()


def digest_of(tool: str, arguments: Dict[str, Any],
              timescale_exp: Optional[int] = None) -> str:
    """Digest for a stateless tool that has no session (e.g. a waveform diff)."""
    return question_digest(tool, arguments, schema=API_SCHEMA_VERSION,
                           timescale_exp=timescale_exp, mode=None)


__all__ = ["API_SCHEMA_VERSION", "CURRENT", "RequestSnapshot", "current",
           "digest_of"]
