"""Coverage report parser (Indago-parity: coverage query).

Parses Xcelium/xrun coverage exported via ``urg`` (Unified Report Generator)
without any license — a pure bypass text/CSV parser.

Two input formats are supported (they are complementary):

* **urg text report** (``urg -format text``): a per-instance hierarchy tree with
  six cumulative metrics (Overall/Block/Expression/Toggle/Fsm/Functional), each
  as ``XX.XX% (covered/total)`` or ``n/a``. Indentation (``|--`` / ``| |--``)
  encodes the design hierarchy. This is the primary summary source.

* **all_bins.csv** (``urg -format details``): a flat CSV where each row is a
  covergroup / coverpoint / bin / assertion. The ``Assertion Status Grade``
  column carries per-assertion pass rate (used by assertion_source too).

The parser is defensive: coverage reports are optional and formats vary across
xrun versions, so a malformed line is skipped rather than fatal.
"""
from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# One metric cell: "84.80% (1880/2217)" or "100.00% (187/187)" or "n/a".
_METRIC = re.compile(r"(\d+(?:\.\d+)?)%\s*\((\d+)/(\d+)\)")

# The six metric columns in urg text reports, in order.
_METRIC_NAMES = ["overall", "block", "expression", "toggle", "fsm", "functional"]

# A hierarchy row: optional indent markers ("|--", "| |--") + name + metrics.
# We split the name from the metrics by locating the first metric/"n/a" token.
_INDENT = re.compile(r"^([|\s-]*)")


@dataclass
class CoverageNode:
    name: str
    depth: int
    metrics: Dict[str, Optional[dict]] = field(default_factory=dict)
    children: List["CoverageNode"] = field(default_factory=list)

    def to_dict(self, with_children: bool = True) -> dict:
        d = {"name": self.name, "depth": self.depth}
        d.update({k: v for k, v in self.metrics.items()})
        if with_children:
            d["children"] = [c.to_dict() for c in self.children]
        return d


def _parse_metric_cell(tok: str) -> Optional[dict]:
    m = _METRIC.search(tok)
    if not m:
        return None
    covered, total = int(m.group(2)), int(m.group(3))
    return {"pct": float(m.group(1)), "covered": covered, "total": total}


def _row_metrics(rest: str) -> Dict[str, Optional[dict]]:
    """Extract up to six metric cells (order-preserving) from the tail of a row.

    ``n/a`` cells become None. Cells are matched positionally against
    _METRIC_NAMES; a leading ``XX% (a/b)`` token or ``n/a`` delimits columns.
    """
    # Tokenize into metric cells or "n/a" in appearance order.
    cells: List[Optional[dict]] = []
    pos = 0
    # Walk the string collecting each "pct (a/b)" or standalone "n/a".
    token_re = re.compile(r"(\d+(?:\.\d+)?%\s*\(\d+/\d+\))|(n/a)")
    for m in token_re.finditer(rest):
        if m.group(1):
            cells.append(_parse_metric_cell(m.group(1)))
        else:
            cells.append(None)
    metrics: Dict[str, Optional[dict]] = {}
    for i, name in enumerate(_METRIC_NAMES):
        metrics[name] = cells[i] if i < len(cells) else None
    return metrics


class CoverageSource:
    """Loads a urg text report and/or all_bins.csv into queryable structures."""

    def __init__(self, report_path: Optional[str] = None,
                 csv_path: Optional[str] = None):
        self.report_path = report_path
        self.csv_path = csv_path
        self.roots: List[CoverageNode] = []
        self.by_name: Dict[str, CoverageNode] = {}
        self.csv_rows: List[dict] = []
        if report_path and os.path.exists(report_path):
            self._parse_text_report(report_path)
        if csv_path and os.path.exists(csv_path):
            self._parse_csv(csv_path)

    @property
    def available(self) -> bool:
        return bool(self.roots or self.csv_rows)

    # -- urg text report ----------------------------------------------------
    def _parse_text_report(self, path: str):
        stack: List[CoverageNode] = []
        with open(path, "r", errors="replace") as fh:
            for raw in fh:
                line = raw.rstrip("\n")
                if not line.strip():
                    continue
                low = line.strip().lower()
                if low.startswith(("legend:", "name", "---")):
                    continue
                # depth from indent markers: count "|" plus leading spaces groups
                indent = _INDENT.match(line).group(1)
                depth = indent.count("|")
                # the name is the first whitespace-delimited token after indent
                body = line[len(indent):]
                # split name from metric tail: name runs up to the first metric
                mfirst = re.search(r"\s(\d+(?:\.\d+)?%\s*\(|n/a)", body)
                if mfirst:
                    name = body[:mfirst.start()].strip()
                    rest = body[mfirst.start():]
                else:
                    name = body.strip()
                    rest = ""
                if not name:
                    continue
                node = CoverageNode(name=name, depth=depth,
                                    metrics=_row_metrics(rest))
                # attach to tree by depth
                while stack and stack[-1].depth >= depth:
                    stack.pop()
                if stack:
                    stack[-1].children.append(node)
                else:
                    self.roots.append(node)
                stack.append(node)
                self.by_name.setdefault(name, node)

    # -- all_bins.csv -------------------------------------------------------
    def _parse_csv(self, path: str):
        with open(path, "r", errors="replace", newline="") as fh:
            reader = csv.reader(fh)
            header = next(reader, None)
            for row in reader:
                if len(row) < 5:
                    continue
                self.csv_rows.append({
                    "numbering": row[0],
                    "exclusion": row[1] if len(row) > 1 else "",
                    "unr": row[2] if len(row) > 2 else "",
                    "name": row[3] if len(row) > 3 else "",
                    "grade": row[4] if len(row) > 4 else "",
                    "covered": row[5] if len(row) > 5 else "",
                    "assertion_status_grade": row[6] if len(row) > 6 else "",
                    "score": row[7] if len(row) > 7 else "",
                })

    # -- queries ------------------------------------------------------------
    def summary(self, instance: Optional[str] = None,
                max_depth: int = 2) -> dict:
        """Coverage tree (optionally rooted at an instance), pruned to depth."""
        def prune(node: CoverageNode, d: int) -> dict:
            out = node.to_dict(with_children=False)
            if d < max_depth:
                out["children"] = [prune(c, d + 1) for c in node.children]
            else:
                out["children_truncated"] = len(node.children)
            return out

        if instance:
            node = self.by_name.get(instance)
            if not node:
                # try suffix match on hierarchical instance name
                for nm, nd in self.by_name.items():
                    if nm.endswith(instance) or instance.endswith(nm):
                        node = nd
                        break
            if not node:
                return {"available": False,
                        "reason": f"instance '{instance}' not in coverage report"}
            return {"available": True, "tree": prune(node, 0)}
        return {"available": True,
                "tree": [prune(r, 0) for r in self.roots]}

    def detail(self, instance: str) -> dict:
        """Full metric detail for one instance node (no child pruning)."""
        node = self.by_name.get(instance)
        if not node:
            return {"available": False,
                    "reason": f"instance '{instance}' not found"}
        return {"available": True, "node": node.to_dict(with_children=True)}

    def low_coverage(self, threshold: float = 90.0, metric: str = "overall",
                     limit: int = 100) -> List[dict]:
        """All nodes whose given metric pct is below threshold (coverage holes)."""
        out = []
        for nm, nd in self.by_name.items():
            cell = nd.metrics.get(metric)
            if cell and cell["pct"] < threshold:
                out.append({"name": nm, "metric": metric, **cell})
        out.sort(key=lambda x: x["pct"])
        return out[:limit]
