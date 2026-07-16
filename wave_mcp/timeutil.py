"""Time string parsing / formatting and conversion to FST time units.

FST stores time as integer counts of ``10**timescale_exponent`` seconds, where
the exponent is returned by ``fstReaderGetTimescale`` (e.g. -9 == ns, -12 == ps).

User-facing time strings follow Indago/Verisium convention: ``"100ns"``,
``"3954ps"``, ``"1us"``, and the special tokens ``"min"`` / ``"max"`` handled by
callers.
"""
from __future__ import annotations

import re

# exponent (power of ten, in seconds) for each supported unit
_UNIT_EXP = {
    "s": 0,
    "ms": -3,
    "us": -6,
    "ns": -9,
    "ps": -12,
    "fs": -15,
}

_TIME_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([a-zA-Z]*)\s*$")


def parse_time_to_seconds(text: str) -> float:
    """Parse a time string such as ``"100ns"`` into seconds (float).

    A bare number (no unit) is interpreted as seconds.
    """
    m = _TIME_RE.match(str(text))
    if not m:
        raise ValueError(f"invalid time string: {text!r}")
    value = float(m.group(1))
    unit = (m.group(2) or "s").lower()
    if unit not in _UNIT_EXP:
        raise ValueError(f"unknown time unit in {text!r}; expected one of {list(_UNIT_EXP)}")
    return value * (10.0 ** _UNIT_EXP[unit])


def time_to_fst_units(text: str, timescale_exp: int) -> int:
    """Convert a user time string into integer FST time units."""
    seconds = parse_time_to_seconds(text)
    units = seconds / (10.0 ** timescale_exp)
    return int(round(units))


def fst_units_to_seconds(units: int, timescale_exp: int) -> float:
    return units * (10.0 ** timescale_exp)


def format_fst_time(units: int, timescale_exp: int) -> str:
    """Format FST integer time units as a human readable string.

    Picks the unit that keeps the number reasonably sized.
    """
    seconds = fst_units_to_seconds(units, timescale_exp)
    if seconds == 0:
        return "0"
    for unit, exp in sorted(_UNIT_EXP.items(), key=lambda kv: kv[1], reverse=True):
        scaled = seconds / (10.0 ** exp)
        if abs(scaled) >= 1.0:
            # keep integers integral
            if abs(scaled - round(scaled)) < 1e-9:
                return f"{int(round(scaled))}{unit}"
            return f"{scaled:.4g}{unit}"
    return f"{seconds:g}s"
