"""SVA assertion status parser (Indago-parity: assertion query).

Two complementary, license-free sources:

* **xrun.log**: native ``xmsim: *E,ASRT*`` / ``*F,ASRT*`` failure lines carry the
  failed assertion, its (optional) file:line, time, and the ``$error`` text.
  Fast and available the moment simulation ends, but only shows *failures*.

* **all_bins.csv** (urg ``-format details``): the ``Assertion Status Grade``
  column gives the per-assertion pass rate for every assertion (name starting
  with ``a_``); cover properties (``c_``) appear with an empty grade. Gives the
  full pass/fail landscape but requires urg to have run.

The combined view lets the model answer "which assertions failed / when / why"
and "what is each assertion's status", matching Indago's assertion tooling.
"""
from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

# xmsim: *E,ASRTST (File: x.sv, Line: 42):(Time: 1200 NS + 0): (a_foo) message
# The tag after ASRT varies (ASRTST / ASRTSEQ / ...); file/line/time optional.
_ASRT_RE = re.compile(
    r"(?:xmsim|xmelab)\s*:\s*\*([EFW]),ASRT\w*\s*"
    r"(?:\(File:\s*([^,]+),\s*Line:\s*(\d+)\)\s*)?"
    r":?\s*(?:\(Time:\s*([^)]+)\)\s*)?"
    r":?\s*(.*)$"
)
# assertion name often appears parenthesized in the message, e.g. "(a_foo)".
_ASRT_NAME = re.compile(r"\(([acp]_[A-Za-z0-9_]+)\)")

_SEV = {"E": "ERROR", "F": "FATAL", "W": "WARNING"}


@dataclass
class AssertionFailure:
    index: int
    severity: str
    name: Optional[str]
    time: Optional[str]
    file: Optional[str]
    line: Optional[int]
    message: str
    raw: str

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "severity": self.severity,
            "assertion": self.name,
            "time": self.time,
            "source": {"file": self.file, "line": self.line},
            "message": self.message.strip(),
        }


@dataclass
class AssertionStatus:
    name: str
    kind: str            # "assert" (a_) / "cover" (c_/p_)
    pass_grade: Optional[float]   # 100.0 => never failed; None for cover props
    covered: Optional[float]
    numbering: str = ""

    def to_dict(self) -> dict:
        return {
            "assertion": self.name,
            "kind": self.kind,
            "pass_grade": self.pass_grade,
            "covered": self.covered,
            "numbering": self.numbering,
        }


class AssertionSource:
    def __init__(self, log_path: Optional[str] = None,
                 csv_path: Optional[str] = None):
        self.failures: List[AssertionFailure] = []
        self.statuses: List[AssertionStatus] = []
        if log_path and os.path.exists(log_path):
            self._parse_log(log_path)
        if csv_path and os.path.exists(csv_path):
            self._parse_csv(csv_path)

    @property
    def available(self) -> bool:
        return bool(self.failures or self.statuses)

    # -- xrun.log failures --------------------------------------------------
    def _parse_log(self, path: str):
        idx = 0
        with open(path, "r", errors="replace") as fh:
            for raw in fh:
                line = raw.rstrip("\n")
                if ",ASRT" not in line:
                    continue
                m = _ASRT_RE.search(line)
                if not m:
                    continue
                msg = m.group(5) or ""
                nm = _ASRT_NAME.search(msg)
                self.failures.append(AssertionFailure(
                    index=idx, severity=_SEV.get(m.group(1), m.group(1)),
                    name=nm.group(1) if nm else None,
                    file=m.group(2).strip() if m.group(2) else None,
                    line=int(m.group(3)) if m.group(3) else None,
                    time=m.group(4).strip() if m.group(4) else None,
                    message=msg, raw=line,
                ))
                idx += 1

    # -- all_bins.csv statuses ---------------------------------------------
    @staticmethod
    def _grade(tok: str) -> Optional[float]:
        tok = (tok or "").strip().rstrip("%")
        if not tok:
            return None
        try:
            return float(tok)
        except ValueError:
            return None

    @staticmethod
    def _col_index(header: List[str], *candidates: str,
                   default: int = -1) -> int:
        """Find a column by (case-insensitive, substring) header name.

        Falls back to ``default`` positional index if no header matches, so the
        parser is resilient to urg column-order changes across versions.
        """
        low = [h.strip().lower() for h in header]
        for cand in candidates:
            c = cand.lower()
            for i, h in enumerate(low):
                if h == c or c in h:
                    return i
        return default

    def _parse_csv(self, path: str):
        with open(path, "r", errors="replace", newline="") as fh:
            reader = csv.reader(fh)
            header = next(reader, None) or []
            # resolve columns by header name (robust to reordering); fall back to
            # the documented positional layout when a header is absent.
            i_num = self._col_index(header, "numbering", default=0)
            i_name = self._col_index(header, "name", default=3)
            i_grade = self._col_index(header, "overall average grade",
                                      "grade", default=4)
            i_asrt = self._col_index(header, "assertion status grade",
                                     "assertion status", default=6)
            need = max(i_name, i_grade, i_asrt)
            for row in reader:
                if len(row) <= need:
                    continue
                name = row[i_name].strip()
                if not (name.startswith("a_") or name.startswith("c_")
                        or name.startswith("p_")):
                    continue
                asrt_grade = self._grade(row[i_asrt]) if i_asrt >= 0 else None
                kind = "assert" if name.startswith("a_") else "cover"
                self.statuses.append(AssertionStatus(
                    name=name, kind=kind, pass_grade=asrt_grade,
                    covered=self._grade(row[i_grade]) if i_grade >= 0 else None,
                    numbering=row[i_num] if i_num >= 0 else "",
                ))

    # -- queries ------------------------------------------------------------
    def all_failures(self, limit: int = 300) -> List[dict]:
        return [f.to_dict() for f in self.failures][:limit]

    def status(self, name_contains: Optional[str] = None,
               only_failing: bool = False, limit: int = 500) -> List[dict]:
        out = []
        for s in self.statuses:
            if name_contains and name_contains.lower() not in s.name.lower():
                continue
            if only_failing:
                # only assertions with a known grade below 100% count as failing;
                # cover properties (grade None) are never "failing".
                if s.kind != "assert" or s.pass_grade is None \
                        or s.pass_grade >= 100.0:
                    continue
            out.append(s.to_dict())
        return out[:limit]

    def summary(self) -> dict:
        asserts = [s for s in self.statuses if s.kind == "assert"]
        covers = [s for s in self.statuses if s.kind == "cover"]
        failing = [s for s in asserts if (s.pass_grade or 0) < 100.0]
        return {
            "num_failures_in_log": len(self.failures),
            "num_assertions": len(asserts),
            "num_cover_properties": len(covers),
            "num_failing_assertions": len(failing),
            "failing_assertions": [s.name for s in failing][:100],
        }
