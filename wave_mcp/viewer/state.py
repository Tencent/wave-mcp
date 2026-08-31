"""View-state document for the wave-mcp viewer.

Single source of truth shared by the MCP tools (PUT desired), the browser
shell (poll desired / write back actual) and ``get_view_state`` (read).

Schema: dev-docs/viewer-schema-v1.md (public copy will land in docs/).
Thread-safe; the HTTP server accesses it from worker threads.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1

_COLORS = {"red", "green", "blue", "orange", "yellow", "purple"}
_FORMATS = {"hex", "bin", "dec", "signed", "ascii"}
_CONFIDENCE = {"high", "medium", "low"}


class ViewStateError(ValueError):
    """Raised when a desired-state fragment fails validation."""


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ViewStateError(msg)


def _check_time(obj: Any, what: str) -> Dict[str, str]:
    _require(isinstance(obj, dict), f"{what} must be an object")
    _require("time" in obj, f"{what}.time is required")
    unit = obj.get("unit", "ps")
    return {"time": str(obj["time"]), "unit": str(unit)}


def _check_signal(sig: Any) -> Dict[str, Any]:
    _require(isinstance(sig, dict), "signal entry must be an object")
    _require(bool(sig.get("path")), "signal.path is required")
    out: Dict[str, Any] = {"path": str(sig["path"])}
    if sig.get("source") is not None:
        out["source"] = str(sig["source"])
    if sig.get("color") is not None:
        _require(sig["color"] in _COLORS,
                 f"signal.color must be one of {sorted(_COLORS)}")
        out["color"] = sig["color"]
    if sig.get("group") is not None:
        out["group"] = str(sig["group"])
    if sig.get("format") is not None:
        _require(sig["format"] in _FORMATS,
                 f"signal.format must be one of {sorted(_FORMATS)}")
        out["format"] = sig["format"]
    return out


def _check_marker(mk: Any) -> Dict[str, Any]:
    _require(isinstance(mk, dict), "marker entry must be an object")
    out = _check_time(mk, "marker")
    if mk.get("label") is not None:
        out["label"] = str(mk["label"])
    if mk.get("color") is not None:
        _require(mk["color"] in _COLORS,
                 f"marker.color must be one of {sorted(_COLORS)}")
        out["color"] = mk["color"]
    return out


def _check_annotation(an: Any, seq: int) -> Dict[str, Any]:
    _require(isinstance(an, dict), "annotation entry must be an object")
    _require(bool(an.get("markdown")), "annotation.markdown is required")
    out: Dict[str, Any] = {
        "id": str(an.get("id") or f"a{seq}"),
        "timestamp": str(an.get("timestamp")
                         or time.strftime("%Y-%m-%dT%H:%M:%S%z")),
        "markdown": str(an["markdown"]),
    }
    if an.get("confidence") is not None:
        _require(an["confidence"] in _CONFIDENCE,
                 f"annotation.confidence must be one of {sorted(_CONFIDENCE)}")
        out["confidence"] = an["confidence"]
    if an.get("evidence") is not None:
        _require(isinstance(an["evidence"], list),
                 "annotation.evidence must be a list of strings")
        out["evidence"] = [str(e) for e in an["evidence"]]
    return out


class ViewState:
    """Mutable view-state document with revision tracking."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self.revision = 0
        self.desired: Dict[str, Any] = {
            "waveform": {"sources": []},
            "signals": [],
            "cursor": None,
            "viewport": None,
            "markers": [],
            "diff": None,
            "annotations": [],
        }
        self.actual: Dict[str, Any] = {
            "applied_revision": 0,
            "cursor": None,
            "viewport": None,
            "selected_signals": [],
            "displayed_signals": [],
            "user_dirty": False,
            "updated_at": None,
        }
        # FST timescale exponent of the focused waveform; needed to turn
        # schema times (which carry a unit) into Surfer's raw numbers.
        self.timescale_exp: int = 0

    # -- desired ---------------------------------------------------------

    def set_sources(self, sources: List[Dict[str, Any]]) -> None:
        with self._lock:
            clean = []
            for i, s in enumerate(sources):
                _require(bool(s.get("path")), "source.path is required")
                entry = {
                    "id": str(s.get("id") or chr(ord("a") + i)),
                    "path": str(s["path"]),
                    "label": str(s.get("label") or ""),
                }
                if s.get("end_time") is not None:
                    entry["end_time"] = int(s["end_time"])
                if s.get("timescale_exp") is not None:
                    self.timescale_exp = int(s["timescale_exp"])
                clean.append(entry)
            self.desired["waveform"]["sources"] = clean
            self._bump()

    def update_desired(
        self,
        signals: Optional[List[Any]] = None,
        cursor: Optional[Dict[str, Any]] = None,
        viewport: Optional[Dict[str, Any]] = None,
        markers: Optional[List[Any]] = None,
        diff: Optional[Dict[str, Any]] = None,
        annotations: Optional[List[Any]] = None,
    ) -> int:
        """Apply a partial desired update. ``None`` keeps the old value;
        lists replace entirely, except annotations which append (log flow).

        Atomic: every field is validated into a staging area first, then
        committed in one step — a validation error in any field leaves the
        committed state completely untouched."""
        with self._lock:
            staged: Dict[str, Any] = {}
            if signals is not None:
                staged["signals"] = [_check_signal(s) for s in signals]
            if cursor is not None:
                staged["cursor"] = _check_time(cursor, "cursor")
            if viewport is not None:
                _require("from" in viewport and "to" in viewport,
                         "viewport requires from/to")
                staged["viewport"] = {
                    "from": str(viewport["from"]),
                    "to": str(viewport["to"]),
                    "unit": str(viewport.get("unit", "ps")),
                }
            if markers is not None:
                staged["markers"] = [_check_marker(m) for m in markers]
            if diff is not None:
                staged["diff"] = diff
                fd = diff.get("first_divergence")
                if fd:  # auto-marker at the divergence point
                    mk = _check_time(fd, "diff.first_divergence")
                    mk.update({"label": "first divergence", "color": "red"})
                    base_markers = staged.get("markers",
                                              self.desired["markers"])
                    if mk not in base_markers:
                        staged["markers"] = list(base_markers) + [mk]
            new_anns: List[Dict[str, Any]] = []
            if annotations is not None:
                base = len(self.desired["annotations"])
                known = {a["id"] for a in self.desired["annotations"]}
                for j, an in enumerate(annotations):
                    item = _check_annotation(an, base + j + 1)
                    if item["id"] not in known and \
                            item["id"] not in {a["id"] for a in new_anns}:
                        new_anns.append(item)

            # ---- commit point: nothing above mutated self.desired --------
            self.desired.update(staged)
            self.desired["annotations"].extend(new_anns)
            # recompute the sucl cache under the lock so long-pollers always
            # see a snapshot whose commands match its desired fields; the
            # shell reloads the Surfer iframe when this string changes
            # (runtime InjectMessage cursor control is a silent no-op on the
            # pinned build, so boot-time commands are the reliable path).
            from .translate import desired_to_sucl
            self.desired["startup_commands_cache"] = desired_to_sucl(
                self.desired, self.timescale_exp)
            return self._bump()

    def _bump(self) -> int:
        self.revision += 1
        self._cond.notify_all()
        return self.revision

    # -- actual ----------------------------------------------------------

    def write_actual(self, payload: Dict[str, Any]) -> None:
        with self._lock:
            for key in ("applied_revision", "cursor", "viewport",
                        "selected_signals", "displayed_signals", "user_dirty"):
                if key in payload:
                    self.actual[key] = payload[key]
            self.actual["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    # -- read ------------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "version": SCHEMA_VERSION,
                "revision": self.revision,
                "desired": self.desired,
                "actual": self.actual,
            }

    def wait_change(self, since: int, timeout: float = 25.0) -> Dict[str, Any]:
        """Long-poll helper: block until revision > since or timeout."""
        deadline = time.time() + timeout
        with self._cond:
            while self.revision <= since:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
        return self.snapshot()
