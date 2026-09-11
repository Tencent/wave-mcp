"""View-state document for the wave-mcp viewer.

Single source of truth shared by the MCP tools (PUT desired), the browser
shell (poll desired / write back actual) and ``get_view_state`` (read).

Schema: see docs/WAVE_VIEWER.md.
Thread-safe; the HTTP server accesses it from worker threads.
"""
from __future__ import annotations

import difflib
import re
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

from ..timeutil import VALID_UNITS

SCHEMA_VERSION = 1

_COLORS = {"red", "green", "blue", "orange", "yellow", "purple"}
_FORMATS = {"hex", "bin", "dec", "signed", "ascii"}
_CONFIDENCE = {"high", "medium", "low"}

#: Fields accepted in each desired-state fragment. Unknown fields are
#: rejected with the allowed list and a did-you-mean suggestion: a typo like
#: {"time_units": 100} used to pass as a bare "cursor.time is required" and
#: the even closer {"time": 100, "time_units": "ns"} was accepted outright
#: and silently defaulted to ps, which is a wrong answer rather than a
#: missing one.
_TIME_FIELDS = ("time", "unit")
_SIGNAL_FIELDS = ("path", "color", "group", "format", "source")
_VIEWPORT_FIELDS = ("from", "to", "unit")
_ANNOTATION_FIELDS = ("id", "timestamp", "markdown", "confidence", "evidence")
_DIFF_FIELDS = ("source_a", "source_b", "first_divergence")

_UNIT_LIST = ", ".join(VALID_UNITS)
_TIME_EXPECTED = {
    "time": "integer digits, optionally with a unit suffix "
            "('1523400' or '1523400ps')",
    "unit": f"one of {_UNIT_LIST} (default 'ps')",
}
_TIME_EXAMPLE = {"time": "1523400", "unit": "ps"}

#: "100ns" style value: digits plus a unit suffix, whitespace tolerated.
_SUFFIX_TIME_RE = re.compile(r"^(\d+)\s*([a-zA-Z]+)$")

class ViewStateError(ValueError):
    """Raised when a desired-state fragment fails validation.

    Carries the failing parameter plus, where available, the expected shape,
    a did-you-mean suggestion for typos and a concrete example, so callers
    can build an actionable error reply instead of a bare message."""

    def __init__(self, msg: str, *, parameter: Optional[str] = None,
                 expected: Optional[Any] = None,
                 did_you_mean: Optional[str] = None,
                 example: Optional[Any] = None) -> None:
        super().__init__(msg)
        self.parameter = parameter
        self.expected = expected
        self.did_you_mean = did_you_mean
        self.example = example

def _require(cond: bool, msg: str, **kw: Any) -> None:
    if not cond:
        raise ViewStateError(msg, **kw)


def _reject_unknown(obj: Dict[str, Any], allowed: Sequence[str],
                    what: str, parameter: str) -> None:
    """Raise for any field outside ``allowed``, suggesting a close match."""
    for key in obj:
        if key in allowed:
            continue
        close = difflib.get_close_matches(key, list(allowed), n=1, cutoff=0.4)
        raise ViewStateError(
            f"{what} has unknown field {key!r}; allowed fields: "
            + ", ".join(allowed),
            parameter=parameter,
            did_you_mean=close[0] if close else None)


def _check_unit(raw: Any, what: str, parameter: str) -> Optional[str]:
    """Validate/normalize an optional time unit; None keeps the default."""
    if raw is None:
        return None
    unit = str(raw).strip().lower()
    _require(unit in VALID_UNITS,
             f"{what} {raw!r} is not a known time unit; expected one of "
             f"{_UNIT_LIST}",
             parameter=parameter, expected=_TIME_EXPECTED,
             example=_TIME_EXAMPLE)
    return unit


def _split_time(value: Any, what: str, parameter: str,
                declared_unit: Optional[str]) -> Dict[str, str]:
    """Normalize one time value into {"time": digits, "unit": unit}.

    Accepted forms: an integer, a digit string, or a digit string with a
    unit suffix ("100ns"). The suffixed form is what ``diff_waveforms``
    emits and what the demos hand back, so it keeps working and is
    normalized instead of rejected. A suffix that disagrees with
    ``declared_unit`` is an error: silently preferring either one would move
    the cursor to a time the caller did not ask for.
    """
    text = str(value).strip()
    match = _SUFFIX_TIME_RE.match(text)
    if match:
        digits, suffix = match.group(1), match.group(2).lower()
        _require(suffix in VALID_UNITS,
                 f"{what} {value!r} has unknown unit {match.group(2)!r}; "
                 f"expected one of {_UNIT_LIST}",
                 parameter=parameter, expected=_TIME_EXPECTED,
                 example=_TIME_EXAMPLE)
        if declared_unit is not None and declared_unit != suffix:
            raise ViewStateError(
                f"{what} {value!r} carries unit {suffix!r} but the declared "
                f"unit is {declared_unit!r}; make them match or drop the "
                "suffix",
                parameter=parameter, expected=_TIME_EXPECTED,
                example=_TIME_EXAMPLE)
        return {"time": digits, "unit": suffix}
    _require(text.isdigit(),
             f"{what} must be integer digits, optionally with a unit suffix "
             f"('1523400' or '1523400ps'); got {value!r}",
             parameter=parameter, expected=_TIME_EXPECTED,
             example=_TIME_EXAMPLE)
    return {"time": text, "unit": declared_unit or "ps"}


def _check_time(obj: Any, what: str, parameter: str,
                extra_fields: Sequence[str] = ()) -> Dict[str, str]:
    _require(isinstance(obj, dict),
             f'{what} must be an object like {{"time": "1523400", "unit": "ps"}}',
             parameter=parameter, example=_TIME_EXAMPLE)
    _reject_unknown(obj, _TIME_FIELDS + tuple(extra_fields), what, parameter)
    _require("time" in obj, f"{what}.time is required",
             parameter=parameter, expected=_TIME_EXPECTED,
             example=_TIME_EXAMPLE)
    unit = _check_unit(obj.get("unit"), f"{what}.unit", parameter)
    return _split_time(obj["time"], f"{what}.time", parameter, unit)


def _check_signal(sig: Any, index: int) -> Dict[str, Any]:
    what = f"signals[{index}]"
    _require(isinstance(sig, dict), f"{what} must be an object",
             parameter=what)
    _reject_unknown(sig, _SIGNAL_FIELDS, what, what)
    _require(bool(sig.get("path")), f"{what}.path is required",
             parameter=what)
    out: Dict[str, Any] = {"path": str(sig["path"])}
    if sig.get("source") is not None:
        out["source"] = str(sig["source"])
    if sig.get("color") is not None:
        _require(sig["color"] in _COLORS,
                 f"{what}.color must be one of {sorted(_COLORS)}",
                 parameter=what)
        out["color"] = sig["color"]
    if sig.get("group") is not None:
        out["group"] = str(sig["group"])
    if sig.get("format") is not None:
        _require(sig["format"] in _FORMATS,
                 f"{what}.format must be one of {sorted(_FORMATS)}",
                 parameter=what)
        out["format"] = sig["format"]
    return out


def _check_marker(mk: Any, index: int) -> Dict[str, Any]:
    what = f"markers[{index}]"
    _require(isinstance(mk, dict), f"{what} must be an object",
             parameter=what)
    out = _check_time(mk, what, what, extra_fields=("label", "color"))
    if mk.get("label") is not None:
        out["label"] = str(mk["label"])
    if mk.get("color") is not None:
        _require(mk["color"] in _COLORS,
                 f"{what}.color must be one of {sorted(_COLORS)}",
                 parameter=what)
        out["color"] = mk["color"]
    return out


def _check_annotation(an: Any, seq: int) -> Dict[str, Any]:
    what = "annotation"
    _require(isinstance(an, dict), f"{what} must be an object",
             parameter=what)
    _reject_unknown(an, _ANNOTATION_FIELDS, what, what)
    _require(bool(an.get("markdown")), f"{what}.markdown is required",
             parameter=what)
    out: Dict[str, Any] = {
        "id": str(an.get("id") or f"a{seq}"),
        "timestamp": str(an.get("timestamp")
                         or time.strftime("%Y-%m-%dT%H:%M:%S%z")),
        "markdown": str(an["markdown"]),
    }
    if an.get("confidence") is not None:
        _require(an["confidence"] in _CONFIDENCE,
                 f"{what}.confidence must be one of {sorted(_CONFIDENCE)}",
                 parameter=what)
        out["confidence"] = an["confidence"]
    if an.get("evidence") is not None:
        _require(isinstance(an["evidence"], list),
                 f"{what}.evidence must be a list of strings",
                 parameter=what)
        out["evidence"] = [str(e) for e in an["evidence"]]
    return out


def _check_viewport(vp: Any) -> Dict[str, str]:
    what = "viewport"
    _require(isinstance(vp, dict),
             'viewport must be an object like {"from": "0", "to": "1000", '
             '"unit": "ps"}',
             parameter=what)
    _reject_unknown(vp, _VIEWPORT_FIELDS, what, what)
    _require("from" in vp and "to" in vp, "viewport requires from/to",
             parameter=what,
             example={"from": "0", "to": "1000", "unit": "ps"})
    unit = _check_unit(vp.get("unit"), "viewport.unit", what)
    lo = _split_time(vp["from"], "viewport.from", what, unit)
    hi = _split_time(vp["to"], "viewport.to", what, unit)
    if unit is None:
        # No declared unit: both values must agree on their suffix-derived
        # unit, because the schema stores one unit for the pair. Bare numbers
        # default to ps on both sides, so they always agree.
        _require(lo["unit"] == hi["unit"],
                 "viewport.from/to carry different units "
                 f"({lo['unit']!r} vs {hi['unit']!r}); declare one "
                 "viewport.unit",
                 parameter=what)
    return {"from": lo["time"], "to": hi["time"],
            "unit": lo["unit"] if unit is None else unit}


def _stage_fragment(*, signals: Optional[List[Any]] = None,
                    cursor: Optional[Dict[str, Any]] = None,
                    viewport: Optional[Dict[str, Any]] = None,
                    markers: Optional[List[Any]] = None,
                    diff: Optional[Dict[str, Any]] = None,
                    base_markers: Sequence[Dict[str, Any]] = ()
                    ) -> Dict[str, Any]:
    """Validate a desired-update fragment into its normalized form.

    Pure: raises on the first problem and returns the fields a successful
    update would commit. ``base_markers`` supplies the current markers so
    the diff auto-marker dedupe matches commit-time behavior."""
    staged: Dict[str, Any] = {}
    if signals is not None:
        _require(isinstance(signals, list),
                 "signals must be a list of signal objects",
                 parameter="signals")
        staged["signals"] = [_check_signal(s, i)
                             for i, s in enumerate(signals)]
    if cursor is not None:
        staged["cursor"] = _check_time(cursor, "cursor", "cursor")
    if viewport is not None:
        staged["viewport"] = _check_viewport(viewport)
    if markers is not None:
        _require(isinstance(markers, list),
                 "markers must be a list of marker objects",
                 parameter="markers")
        staged["markers"] = [_check_marker(m, i)
                             for i, m in enumerate(markers)]
    if diff is not None:
        _require(isinstance(diff, dict),
                 'diff must be an object like {"source_a": "a", "source_b": '
                 '"b", "first_divergence": {...}}',
                 parameter="diff")
        _reject_unknown(diff, _DIFF_FIELDS, "diff", "diff")
        staged["diff"] = diff
        fd = diff.get("first_divergence")
        if fd:  # auto-marker at the divergence point
            mk = _check_time(fd, "diff.first_divergence", "diff")
            mk.update({"label": "first divergence", "color": "red"})
            base = staged.get("markers", list(base_markers))
            if mk not in base:
                staged["markers"] = list(base) + [mk]
    return staged


def validate_view_inputs(*, signals: Optional[List[Any]] = None,
                         cursor: Optional[Dict[str, Any]] = None,
                         viewport: Optional[Dict[str, Any]] = None,
                         markers: Optional[List[Any]] = None,
                         diff: Optional[Dict[str, Any]] = None,
                         annotations: Optional[List[Any]] = None) -> None:
    """Validate a desired update without touching any state or process.

    Used by the manager to answer a bad request before a surver is started;
    ``ViewState.update_desired`` runs the same checks again for direct users
    (HTTP PUT, tests). Raises ViewStateError on the first problem."""
    _stage_fragment(signals=signals, cursor=cursor, viewport=viewport,
                    markers=markers, diff=diff)
    if annotations is not None:
        _require(isinstance(annotations, list),
                 "annotations must be a list of annotation objects",
                 parameter="annotation")
        for j, an in enumerate(annotations):
            _check_annotation(an, j + 1)


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
        #: Notes about desired fields a sucl command could not be generated
        #: for (defense in depth; validated state should never produce any).
        #: Surfaced as ``warnings`` by open_wave_view / update_wave_view.
        self.warnings: List[str] = []

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
            staged = _stage_fragment(
                signals=signals, cursor=cursor, viewport=viewport,
                markers=markers, diff=diff,
                base_markers=self.desired["markers"])
            new_anns: List[Dict[str, Any]] = []
            if annotations is not None:
                _require(isinstance(annotations, list),
                         "annotations must be a list of annotation objects",
                         parameter="annotation")
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
            report: List[str] = []
            from .translate import desired_to_sucl
            self.desired["startup_commands_cache"] = desired_to_sucl(
                self.desired, self.timescale_exp, report=report)
            self.warnings = report
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
