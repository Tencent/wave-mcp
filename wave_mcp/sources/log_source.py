"""xrun.log parser (Indago category 7).

Parses Xcelium / xrun simulation logs and UVM report messages so the server can
answer "all errors", "all warnings", keyword search, and per-index detail
without any license. This is a pure bypass parser.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Optional

# Xcelium native severities, e.g. "xmsim: *E,SOMECODE (file,12): message"
_XR_RE = re.compile(
    r"^\s*(?:xmsim|xmelab|xmvlog|xrun)\s*:\s*\*([EWFN]),(\w+)"
    r"(?:\s*\(([^,]+),(\d+)\))?\s*:?\s*(.*)$"
)
# UVM report, e.g. "UVM_ERROR file(line) @ 1200: reporter [TAG] message"
_UVM_RE = re.compile(
    r"(UVM_(?:INFO|WARNING|ERROR|FATAL))\s+([^\s(]+)\(?(\d+)?\)?\s*@\s*([\d.]+\s*\w*)\s*:"
    r"\s*([^\[]*)(?:\[([^\]]+)\])?\s*(.*)$"
)

_SEV_NATIVE = {"E": "ERROR", "W": "WARNING", "F": "FATAL", "N": "NOTE"}


@dataclass
class LogMessage:
    index: int
    severity: str            # ERROR / WARNING / FATAL / INFO / NOTE
    time: Optional[str]
    message: str
    file: Optional[str] = None
    line: Optional[int] = None
    scope: Optional[str] = None
    tag: Optional[str] = None
    code: Optional[str] = None
    raw: str = ""

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "type": self.severity,
            "time": self.time,
            "message": self.message.strip(),
            "source": {"file": self.file, "line_start": self.line},
            "scope": self.scope,
            "tag": self.tag,
            "code": self.code,
        }


class LogSource:
    def __init__(self, log_path: str):
        self.path = log_path
        self.messages: List[LogMessage] = []
        if log_path and os.path.exists(log_path):
            self._parse()

    def _parse(self):
        idx = 0
        with open(self.path, "r", errors="replace") as fh:
            for raw in fh:
                line = raw.rstrip("\n")
                msg = self._parse_line(line, idx)
                if msg is not None:
                    self.messages.append(msg)
                    idx += 1

    def _parse_line(self, line: str, idx: int) -> Optional[LogMessage]:
        m = _UVM_RE.search(line)
        if m:
            sev = m.group(1).replace("UVM_", "")
            return LogMessage(
                index=idx, severity=sev, time=m.group(4),
                file=m.group(2), line=int(m.group(3)) if m.group(3) else None,
                scope=(m.group(5) or "").strip() or None,
                tag=m.group(6), message=m.group(7) or "", raw=line,
            )
        m = _XR_RE.match(line)
        if m:
            sev = _SEV_NATIVE.get(m.group(1), m.group(1))
            return LogMessage(
                index=idx, severity=sev, code=m.group(2), time=None,
                file=m.group(3), line=int(m.group(4)) if m.group(4) else None,
                message=m.group(5) or "", raw=line,
            )
        return None

    # -- queries ------------------------------------------------------------
    def by_severity(self, severity: str, limit: int = 300) -> List[dict]:
        sev = severity.upper()
        return [m.to_dict() for m in self.messages if m.severity == sev][:limit]

    def errors(self, limit: int = 300) -> List[dict]:
        return [m.to_dict() for m in self.messages
                if m.severity in ("ERROR", "FATAL")][:limit]

    def warnings(self, limit: int = 300) -> List[dict]:
        return self.by_severity("WARNING", limit)

    def containing(self, search: str, limit: int = 300) -> List[dict]:
        s = search.lower()
        return [m.to_dict() for m in self.messages
                if s in m.raw.lower()][:limit]

    def by_indices(self, indices: List[int]) -> List[dict]:
        out = []
        for i in indices[:200]:
            if 0 <= i < len(self.messages):
                out.append(self.messages[i].to_dict())
        return out
