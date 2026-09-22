"""Result-size control for value queries.

A waveform window can hold millions of changes; an agent asking for "the values
here" must not have its context blown by the answer, and must not be handed a
silently truncated prefix that looks like the whole story either.

``limit`` is therefore a *target size*, and overflow is handled by evenly
downsampling the timeline rather than cutting it at the front. The reply always
states what happened (``sampled``, ``sample_rate``, ``total_available``) because
the parameter name alone cannot: "limit" reads like truncation everywhere else
in the world, so the behaviour is reported rather than implied.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

#: Fallback target when the caller does not pass one. Large enough for a real
#: timeline, small enough to survive an LLM context window.
DEFAULT_LIMIT = 1000

#: Hard ceiling on rows pulled out of the engine for one query, independent of
#: ``limit``: this bounds the work, while ``limit`` bounds the reply.
MAX_SCAN = 2_000_000


def resolve_limit(limit: Optional[int]) -> int:
    """Normalize a caller-supplied ``limit`` into a usable target size."""
    if limit is None:
        return DEFAULT_LIMIT
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, n)


def downsample(rows: List[Any], limit: int) -> Dict[str, Any]:
    """Reduce ``rows`` to at most ``limit`` entries, evenly spread.

    The first and last rows are always kept: they carry the values held at the
    window boundaries, which is what a caller needs to orient itself. Interior
    rows are picked on an even stride so the shape of the timeline survives.

    Returns a dict with the (possibly reduced) rows plus honest bookkeeping. The
    bookkeeping keys are only present when something was actually dropped, so an
    unsampled reply stays clean.

    ``resume_from`` is the time of the *second* kept row, i.e. the start of the
    first interval whose detail was dropped. Passing it back as ``start`` with
    the same limit walks the timeline at full resolution instead of re-sampling
    the same coarse view, which is what "give me the rest" has to mean when the
    reply was thinned rather than truncated. The name says the usage: it is a
    resume point that overlaps the first kept interval, not a pagination cursor
    pointing past the returned rows.
    """
    total = len(rows)
    if total <= limit:
        return {"values": rows, "count": total}

    if limit == 1:
        kept = [rows[0]]
    else:
        # even stride across the interior, endpoints pinned
        step = (total - 1) / (limit - 1)
        idx = sorted({int(round(i * step)) for i in range(limit)})
        idx = [min(i, total - 1) for i in idx]
        kept = [rows[i] for i in sorted(set(idx))]

    out = {
        "values": kept,
        "count": len(kept),
        "sampled": True,
        "sample_rate": round(len(kept) / total, 6),
        "total_available": total,
        "note": (f"{total} changes matched; evenly downsampled to {len(kept)}. "
                 "Pass resume_from back as start to walk the skipped detail, "
                 "or narrow start/end for the full-resolution timeline."),
    }
    nxt = _resume_from(kept)
    if nxt is not None:
        out["resume_from"] = nxt
    return out


def _resume_from(kept: List[Any]) -> Optional[Any]:
    """Where a caller should resume to see the detail this reply skipped.

    Returns the ``time`` of the second kept row (the end of the first fully
    detailed span), or None when the rows are not time-stamped dicts. Callers
    pass it back as ``start``.
    """
    if len(kept) < 2 or not isinstance(kept[1], dict):
        return None
    return kept[1].get("time")
