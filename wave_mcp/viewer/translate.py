"""Translate desired view state into Surfer startup commands (sucl).

We deliberately target the stable command layer (sucl / startup_commands),
not the unstable ``inject_message`` Message JSON API.

Probed syntax on the pinned Surfer build (see dev-docs):
  * time arguments are RAW NUMBERS in waveform timescale units — a unit
    suffix like ``83000ps`` is rejected (InvalidParameter);
  * cursor:   ``cursor_set <t>`` (+ ``goto_time <t>`` to scroll there);
  * markers:  ``marker_set_at <t> <n>`` (time FIRST, then marker id/name,
    per upstream command_parser.rs at the pinned commit); ``marker_add``
    is a GUI-only command and fails in batch mode;
  * viewport: ``zoom_to <from> <to>``; ``zoom_to_range`` is not sucl.
  * dividers: ``divider_add <name>`` takes a SINGLE-WORD name. Quoting does
    not help: ``divider_add "fast domain"`` is dropped silently and the group
    heading never appears, so names are whitespace-collapsed before sending.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# NOTE: schema times carry a unit; the FST timescale for our sessions is
# ps in all validated flows. v1 passes the raw value through and documents
# ps as the canonical schema unit; revisit if a non-ps timescale shows up.


def _raw(obj: Dict[str, Any], timescale_exp: int = 0) -> Optional[str]:
    """Convert a schema time {time, unit} into waveform-native units.

    Schema times carry a unit (default ps); Surfer's batch commands take
    RAW NUMBERS in the waveform's own timescale, so passing ``830s``
    straight through makes it fail with InvalidParameter. Reuse the
    project-wide time parser so viewer and analysis tools agree.

    Returns ``None`` for an unknown unit or malformed value: the caller then
    drops that one command. Emitting the bare number instead would move the
    cursor or marker to an arbitrary time and look like a rendering bug, so
    an omitted command is both safer and self-evident in the command string.
    """
    from ..timeutil import time_to_fst_units, VALID_UNITS

    val = str(obj["time"])
    unit = str(obj.get("unit") or "ps")
    if unit.lower() not in VALID_UNITS:
        return None
    text = val if val.endswith(unit) else f"{val}{unit}"
    try:
        return str(time_to_fst_units(text, int(timescale_exp)))
    except ValueError:
        return None


def _divider_name(group: str) -> str:
    """Make a group name safe for Surfer's sucl parser.

    Probed on the pinned build: quoting does NOT protect a multi-word
    parameter. ``divider_add "fast domain"`` is dropped silently, so the whole
    group heading disappears while its signals still get added. Collapsing
    whitespace into underscores keeps the heading visible and readable.

    Non-ASCII names are left as-is: they are accepted and the divider is
    created correctly, it just renders as tofu boxes because the WASM font
    atlas carries no CJK glyphs. Losing the grouping would be worse than
    showing an unreadable label.
    """
    return "_".join(str(group).split())


def desired_to_sucl(desired: Dict[str, Any],
                    timescale_exp: int = 0) -> str:
    """Build the startup_commands string for the current desired state.

    ``timescale_exp`` is the FST timescale exponent (10**exp native units
    per second); it is required to turn schema times into the raw numbers
    Surfer's batch layer expects.
    """
    cmds: List[str] = []

    # multi-file surver: Surfer pops a file picker instead of auto-loading;
    # pre-select the file the view focuses on (probed: needs the FULL path).
    sources = (desired.get("waveform") or {}).get("sources") or []
    if len(sources) > 1:
        by_id = {s["id"]: s["path"] for s in sources}
        focus = None
        diff = desired.get("diff")
        if diff and diff.get("source_b") in by_id:
            focus = by_id[diff["source_b"]]      # fail side by convention
        else:
            for sig in desired.get("signals", []):
                if sig.get("source") in by_id:
                    focus = by_id[sig["source"]]
                    break
        if focus is None:
            focus = sources[-1]["path"]
        cmds.append(f"surver_select_file {focus}")

    # signals, grouped: divider per group, order preserved
    current_group = None
    for sig in desired.get("signals", []):
        group = sig.get("group")
        if group and group != current_group:
            # Whitespace is collapsed: a quoted multi-word name is dropped by
            # the sucl parser, taking the whole heading with it (probed).
            cmds.append(f'divider_add "{_divider_name(group)}"')
            current_group = group
        cmds.append(f"variable_add {sig['path']}")
        if sig.get("color"):
            # item_set_color applies to the last added item
            cmds.append(f"item_set_color {sig['color']}")
        if sig.get("format"):
            fmt = {"hex": "Hexadecimal", "bin": "Bits", "dec": "Unsigned",
                   "signed": "Signed", "ascii": "ASCII"}.get(sig["format"])
            if fmt:
                cmds.append(f"item_set_format {fmt}")

    # viewport / cursor / markers (all times in waveform-native units)
    vp = desired.get("viewport")
    if vp:
        vunit = vp.get("unit", "ps")
        lo = _raw({"time": vp["from"], "unit": vunit}, timescale_exp)
        hi = _raw({"time": vp["to"], "unit": vunit}, timescale_exp)
        if lo is not None and hi is not None:
            cmds.append(f"zoom_to {lo} {hi}")
    elif desired.get("signals"):
        cmds.append("zoom_fit")

    cur = desired.get("cursor")
    if cur:
        t = _raw(cur, timescale_exp)
        if t is not None:
            cmds.append(f"cursor_set {t}")
            cmds.append(f"goto_time {t}")

    # number markers over the *accepted* ones only, so a rejected time cannot
    # shift every subsequent marker onto the wrong id
    nxt = 1
    for mk in desired.get("markers", []):
        t = _raw(mk, timescale_exp)
        if t is None:
            continue
        cmds.append(f"marker_set_at {t} {nxt}")
        nxt += 1

    return ";".join(cmds)
