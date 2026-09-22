"""Bounded execution: how many tool calls run at once, and who waits.

The MCP SDK runs every synchronous tool on a worker thread from an unbounded
(well, 40-wide) pool and knows nothing about owners, sessions or how heavy a
call is. Left alone, one client looping over a large design could hold every
thread while another client's first ``open_session`` never gets a turn. This
module is the gate in front of the tool body:

* at most ``workers`` calls run at once, at most ``per_owner_running`` of them
  for any one owner;
* callers past that wait in a bounded queue (``queue_capacity``); when it is
  full the call is refused with ``server_busy`` instead of piling up;
* waiting is fair between owners: the next slot goes to the owner that has
  waited longest since its last grant, not to whoever queued the most;
* a wait longer than ``queue_wait_timeout`` seconds is refused with
  ``queue_timeout``; the caller keeps nothing and can retry later;
* ``shutdown`` stops admitting, refuses every waiter, and waits for running
  calls up to a grace period.

Honest limits. A call that has started runs to completion: Python threads and
the C waveform scans inside them cannot be interrupted from outside, and
``anyio``'s thread hand-off does not cancel them either. "Cancel" therefore
means "refuse before it starts"; nothing here claims to stop a running scan,
and no slot or lease is recycled before the body really returns. Limits are
read from the environment once (``WAVE_MCP_WORKERS`` etc.) and validated as
positive integers; a tool argument cannot raise them.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, Optional, TypeVar

T = TypeVar("T")
log = logging.getLogger("wave_mcp.exec")


# -- configuration -------------------------------------------------------------
@dataclass(frozen=True)
class ExecutionLimits:
    """Service-side limits. Conservative starting values, not a performance claim."""

    workers: int = 4
    queue_capacity: int = 32
    per_owner_running: int = 2
    per_owner_sessions: int = 16
    idle_session_ttl: float = 1800.0     # seconds; 0 disables idle reaping
    queue_wait_timeout: float = 30.0     # seconds
    shutdown_grace: float = 30.0         # seconds

    ENV = {
        "workers": "WAVE_MCP_WORKERS",
        "queue_capacity": "WAVE_MCP_QUEUE_CAPACITY",
        "per_owner_running": "WAVE_MCP_PER_OWNER_RUNNING",
        "per_owner_sessions": "WAVE_MCP_PER_OWNER_SESSIONS",
        "idle_session_ttl": "WAVE_MCP_SESSION_TTL",
        "queue_wait_timeout": "WAVE_MCP_QUEUE_TIMEOUT",
        "shutdown_grace": "WAVE_MCP_SHUTDOWN_GRACE",
    }

    @classmethod
    def from_env(cls) -> "ExecutionLimits":
        """Limits from the environment; unset or invalid values keep the default.

        An invalid value is logged and ignored rather than crashing the server:
        a typo in a deployment file should not take the service down, and the
        default is safe.
        """
        values: Dict[str, Any] = {}
        for field, var in cls.ENV.items():
            raw = os.environ.get(var, "").strip()
            if not raw:
                continue
            try:
                val = float(raw) if field in ("idle_session_ttl", "queue_wait_timeout",
                                              "shutdown_grace") else int(raw)
            except ValueError:
                log.warning("ignoring %s=%r: not a number", var, raw)
                continue
            allow_zero = field == "idle_session_ttl"
            if val < 0 or (val == 0 and not allow_zero):
                log.warning("ignoring %s=%r: must be positive", var, raw)
                continue
            values[field] = val
        limits = cls(**values)
        if limits.per_owner_running > limits.workers:
            limits = cls(**{**values, "per_owner_running": limits.workers})
        return limits


# -- refusals ------------------------------------------------------------------
class ExecutionError(Exception):
    """A refusal at the gate, rendered by the tool boundary as a structured reply."""

    error_type = "execution_error"

    def __init__(self, message: str, hint: str = "", **extra: Any) -> None:
        super().__init__(message)
        self.message, self.hint, self.extra = message, hint, extra

    def payload(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"status": "error", "error_type": self.error_type,
                               "error": self.message}
        if self.hint:
            out["hint"] = self.hint
        out.update(self.extra)
        return out


class ServerBusy(ExecutionError):
    """The wait queue is full."""
    error_type = "server_busy"


class QueueTimeout(ExecutionError):
    """The call waited longer than the configured limit without a slot."""
    error_type = "queue_timeout"


class ServerShuttingDown(ExecutionError):
    """The server is draining and admits nothing new."""
    error_type = "server_shutting_down"


class ResourceLimit(ExecutionError):
    """An owner is at a quota (sessions, views)."""
    error_type = "resource_limit"


# -- the gate ------------------------------------------------------------------
class _Ticket:
    __slots__ = ("owner", "granted", "refused", "enqueued_at")

    def __init__(self, owner: str, now: float) -> None:
        self.owner = owner
        self.granted = False
        self.refused: Optional[ExecutionError] = None
        self.enqueued_at = now


class BoundedExecutor:
    """Admission control for tool calls. One instance per server process."""

    def __init__(self, limits: Optional[ExecutionLimits] = None) -> None:
        self.limits = limits or ExecutionLimits()
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._running_total = 0
        self._running: Dict[str, int] = {}
        self._waiting: Deque[_Ticket] = deque()
        self._last_grant: Dict[str, float] = {}
        self._shutting_down = False
        self._counts = {"admitted": 0, "queued": 0, "completed": 0, "failed": 0,
                        "busy": 0, "timeout": 0, "refused_shutdown": 0}
        self._wait_total = 0.0
        self._run_total = 0.0

    # -- public -------------------------------------------------------------
    def run(self, owner: str, fn: Callable[[], T], *, tool: str = "") -> T:
        """Run ``fn`` inside one execution slot for ``owner``.

        Blocks while waiting for a slot (bounded by ``queue_wait_timeout``),
        raises an ``ExecutionError`` when refused, and always returns the slot
        when ``fn`` returns or raises. The slot is held for the whole body: a
        C scan that outlives the client's patience still owns it.
        """
        waited = self._acquire(owner)
        t0 = time.monotonic()
        ok = False
        try:
            out = fn()
            ok = True
            return out
        finally:
            elapsed = time.monotonic() - t0
            self._release(owner, ok, waited, elapsed)
            log.debug("tool=%s owner=%s waited=%.3fs ran=%.3fs ok=%s",
                      tool, owner[:8], waited, elapsed, ok)

    def shutdown(self, grace: Optional[float] = None) -> Dict[str, int]:
        """Stop admitting, refuse everything waiting, drain running calls.

        Waits up to ``grace`` seconds (default from the limits) for running
        bodies to return; those still running afterwards are reported, not
        killed, because they cannot be. Returns ``{refused, still_running}``.
        """
        grace = self.limits.shutdown_grace if grace is None else grace
        with self._cond:
            self._shutting_down = True
            refused = 0
            while self._waiting:
                t = self._waiting.popleft()
                t.refused = ServerShuttingDown(
                    "the server is shutting down",
                    hint="retry against a running server")
                refused += 1
            self._counts["refused_shutdown"] += refused
            self._cond.notify_all()
            deadline = time.monotonic() + grace
            while self._running_total and time.monotonic() < deadline:
                self._cond.wait(timeout=max(0.0, deadline - time.monotonic()))
            return {"refused": refused, "still_running": self._running_total}

    def stats(self) -> Dict[str, Any]:
        """Counters and current occupancy for diagnostics and tests."""
        with self._lock:
            done = self._counts["completed"] + self._counts["failed"]
            return {
                **self._counts,
                "running": self._running_total,
                "waiting": len(self._waiting),
                "running_by_owner": dict(self._running),
                "avg_wait_s": round(self._wait_total / self._counts["admitted"], 4)
                if self._counts["admitted"] else 0.0,
                "avg_run_s": round(self._run_total / done, 4) if done else 0.0,
                "limits": {"workers": self.limits.workers,
                           "queue_capacity": self.limits.queue_capacity,
                           "per_owner_running": self.limits.per_owner_running,
                           "queue_wait_timeout": self.limits.queue_wait_timeout},
            }

    # -- internals ----------------------------------------------------------
    def _can_run(self, owner: str) -> bool:
        return (self._running_total < self.limits.workers
                and self._running.get(owner, 0) < self.limits.per_owner_running)

    def _grant(self, owner: str, now: float) -> None:
        self._running_total += 1
        self._running[owner] = self._running.get(owner, 0) + 1
        self._last_grant[owner] = now
        self._counts["admitted"] += 1

    def _acquire(self, owner: str) -> float:
        """Take a slot, waiting if needed. Returns the time spent waiting."""
        with self._cond:
            now = time.monotonic()
            if self._shutting_down:
                self._counts["refused_shutdown"] += 1
                raise ServerShuttingDown("the server is shutting down",
                                         hint="retry against a running server")
            # Nobody waiting and room for this owner: go. A waiting queue means
            # somebody was here first; joining it keeps the order honest.
            if not self._waiting and self._can_run(owner):
                self._grant(owner, now)
                return 0.0
            if len(self._waiting) >= self.limits.queue_capacity:
                self._counts["busy"] += 1
                raise ServerBusy(
                    f"{len(self._waiting)} calls are already waiting for "
                    f"{self.limits.workers} execution slots",
                    hint="retry shortly; narrow the query or split the work if "
                         "this persists", waiting=len(self._waiting),
                    workers=self.limits.workers)
            ticket = _Ticket(owner, now)
            self._waiting.append(ticket)
            self._counts["queued"] += 1
            self._dispatch(now)
            deadline = now + self.limits.queue_wait_timeout
            while not ticket.granted and ticket.refused is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._waiting.remove(ticket)
                    self._counts["timeout"] += 1
                    raise QueueTimeout(
                        f"no execution slot became free within "
                        f"{self.limits.queue_wait_timeout:.0f}s",
                        hint="the server is saturated; retry later or reduce "
                             "concurrent calls", waited_s=round(
                                 time.monotonic() - now, 2))
                self._cond.wait(timeout=remaining)
            if ticket.refused is not None:
                raise ticket.refused
            return time.monotonic() - ticket.enqueued_at

    def _release(self, owner: str, ok: bool, waited: float, elapsed: float) -> None:
        with self._cond:
            self._running_total -= 1
            left = self._running.get(owner, 1) - 1
            if left > 0:
                self._running[owner] = left
            else:
                self._running.pop(owner, None)
            self._counts["completed" if ok else "failed"] += 1
            self._wait_total += waited
            self._run_total += elapsed
            self._dispatch(time.monotonic())
            self._cond.notify_all()

    def _dispatch(self, now: float) -> None:
        """Grant free slots to waiters, fairest owner first. Lock held."""
        while self._running_total < self.limits.workers:
            eligible = [t for t in self._waiting if self._can_run(t.owner)]
            if not eligible:
                return
            # The owner that has gone longest without a grant wins; among that
            # owner's tickets the earliest one. A never-granted owner sorts first.
            pick = min(eligible, key=lambda t: (self._last_grant.get(t.owner, -1.0),
                                                t.enqueued_at))
            self._waiting.remove(pick)
            pick.granted = True
            self._grant(pick.owner, now)


__all__ = ["ExecutionLimits", "ExecutionError", "ServerBusy", "QueueTimeout",
           "ServerShuttingDown", "ResourceLimit", "BoundedExecutor"]
