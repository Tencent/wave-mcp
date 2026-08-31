"""Translate desired view state into Surfer startup commands (sucl).

We deliberately target the stable command layer (sucl / startup_commands),
not the unstable ``inject_message`` Message JSON API.

Probed syntax on the pinned Surfer build (see dev-docs):
  * time arguments are RAW NUMBERS in waveform timescale units — a unit
    suffix like ``83000ps`` is rejected (InvalidParameter);
  * cursor:   ``cursor_set <t>`` (+ ``goto_time <t>`` to scroll there);
  * markers:  ``marker_set_at <n> <t>`` (n starts at 1); ``marker_add``
    is a GUI-only command and fails in batch mode;
  * viewport: ``zoom_to <from> <to>``; ``zoom_to_range`` is not sucl.
"""
from __future__ import annotations

from typing import Any, Dict, List

# NOTE: schema times carry a unit; the FST timescale for our sessions is
# ps in all validated flows. v1 passes the raw value through and documents
# ps as the canonical schema unit; revisit if a non-ps timescale shows up.


def _raw(obj: Dict[str, Any], timescale_exp: int = 0) -> str:
    """Convert a schema time {time, unit} into waveform-native units.

    Schema times carry a unit (default ps); Surfer's batch commands take
    RAW NUMBERS in the waveform's own timescale, so passing ``830s``
    straight through makes it fail with InvalidParameter. Reuse the
    project-wide time parser so viewer and analysis tools agree.
    """
    from ..timeutil import time_to_fst_units

    val = str(obj["time"])
    unit = str(obj.get("unit") or "ps")
    text = val if val.endswith(unit) else f"{val}{unit}"
    try:
        return str(time_to_fst_units(text, int(timescale_exp)))
    except ValueError:
        # unknown unit or malformed value: pass the number through rather
        # than dropping the command entirely
        return str(val)


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
            # quote: multi-word group names would otherwise parse as
            # several parameters (ExtraParameters("domain"))
            cmds.append(f'divider_add "{group}"')
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
        cmds.append(
            f"zoom_to "
            f"{_raw({'time': vp['from'], 'unit': vunit}, timescale_exp)} "
            f"{_raw({'time': vp['to'], 'unit': vunit}, timescale_exp)}")
    elif desired.get("signals"):
        cmds.append("zoom_fit")

    cur = desired.get("cursor")
    if cur:
        t = _raw(cur, timescale_exp)
        cmds.append(f"cursor_set {t}")
        cmds.append(f"goto_time {t}")

    for i, mk in enumerate(desired.get("markers", []), start=1):
        cmds.append(f"marker_set_at {i} {_raw(mk, timescale_exp)}")

    return ";".join(cmds)
