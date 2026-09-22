"""Audit trail: one JSON line per tool call, off unless asked for.

Enabled by ``WAVE_MCP_AUDIT_LOG``: a file path (appended, created 0600), or
``stderr``. Each record carries what an operator needs to answer "who ran
what, when, against which data, and how it went", and nothing a caller typed:

    ts           ISO-8601 UTC
    request_id   random per call, also useful to correlate with client logs
    tool         tool name
    status       ok | error | refused
    error_type   the structured error type when status != ok
    elapsed_s    wall time of the tool body
    dataset      {identity, version} of the data the reply is about, when known

No arguments, no paths, no signal names, no values: those are the caller's
data, and an audit line that leaked them would be a second copy to protect.
Records go through the ``wave_mcp.audit`` logger, so a deployment that wants
them elsewhere (syslog, a collector) attaches its own handler and leaves the
environment variable unset.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from . import storage

AUDIT_ENV = "WAVE_MCP_AUDIT_LOG"

log = logging.getLogger("wave_mcp.audit")
log.propagate = False


def configure_from_env() -> Optional[str]:
    """Attach a handler per ``WAVE_MCP_AUDIT_LOG``; returns the destination or None."""
    raw = os.environ.get(AUDIT_ENV, "").strip()
    if not raw:
        return None
    if raw.lower() == "stderr":
        handler: logging.Handler = logging.StreamHandler(sys.stderr)
        dest = "stderr"
    else:
        dest = storage.user_path(raw, home_relative=True)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        handler = logging.StreamHandler(os.fdopen(fd, "a", encoding="utf-8"))
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    return dest


def enabled() -> bool:
    return log.isEnabledFor(logging.INFO) and bool(log.handlers)


def new_request_id() -> str:
    return secrets.token_hex(8)


def record(tool: str, status: str, elapsed_s: float, *,
           request_id: Optional[str] = None,
           error_type: Optional[str] = None,
           dataset: Optional[Dict[str, Any]] = None) -> None:
    """Emit one audit line. Cheap no-op when auditing is off."""
    if not enabled():
        return
    entry: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "request_id": request_id or new_request_id(),
        "tool": tool,
        "status": status,
        "elapsed_s": round(elapsed_s, 4),
    }
    if error_type:
        entry["error_type"] = error_type
    if dataset:
        entry["dataset"] = {k: dataset[k] for k in ("identity", "version") if k in dataset}
    log.info(json.dumps(entry, sort_keys=True))


def outcome_of(reply: Any) -> tuple:
    """``(status, error_type, dataset)`` read off a tool reply."""
    if not isinstance(reply, dict):
        return "ok", None, None
    fp = reply.get("_fp")
    dataset = fp.get("dataset") if isinstance(fp, dict) else reply.get("dataset")
    if not isinstance(dataset, dict):
        dataset = None
    if reply.get("status") == "error" or "error_type" in reply:
        return "error", reply.get("error_type") or "error", dataset
    return "ok", None, dataset


__all__ = ["AUDIT_ENV", "configure_from_env", "enabled", "new_request_id",
           "record", "outcome_of", "log"]
