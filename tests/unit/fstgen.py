"""Tiny FST writer helper for tests: build waveforms from value tables.

Uses pylibfst's writer API, so test waveforms need no simulator at all.
"""
from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

from pylibfst import lib, ffi


def write_fst(path: str,
              signals: Dict[str, int],
              changes: Sequence[Tuple[int, str, str]],
              timescale_exp: int = -12,
              scope: str = "top") -> str:
    """Write an FST file.

    Args:
        path: output file.
        signals: name -> width (bits). Names are created under ``scope``
            unless they contain a dot (then split into nested scopes).
        changes: sequence of (time, signal_name, value_string). Times must
            be non-decreasing. value_string is binary ("0","1","x","z",
            "10101010") matching the signal width.
        timescale_exp: -12 = ps.
        scope: top scope name.
    """
    ctx = lib.fstWriterCreate(path.encode(), 1)
    assert ctx, f"cannot create {path}"
    lib.fstWriterSetTimescale(ctx, timescale_exp)
    lib.fstWriterSetScope(ctx, 0, scope.encode(), ffi.NULL)
    handles = {}
    for name, width in signals.items():
        # NOTE: vartype 0 is FST_VT_VCD_EVENT — viewers render that as
        # event arrows, not waveforms. Use a real wire type.
        handles[name] = lib.fstWriterCreateVar(
            ctx, lib.FST_VT_VCD_WIRE, 0, width, name.encode(), 0)
    lib.fstWriterSetUpscope(ctx)

    last_t = None
    for t, name, value in changes:
        if t != last_t:
            lib.fstWriterEmitTimeChange(ctx, t)
            last_t = t
        lib.fstWriterEmitValueChange(ctx, handles[name], value.encode())
    lib.fstWriterClose(ctx)
    return path


def clocked_pair(dir_a: str, dir_b: str,
                 n_cycles: int = 100, period: int = 2000,
                 diverge_cycle: int = 42) -> Tuple[str, str]:
    """Build a classic pass/fail pair: 8-bit counter, fail run's counter
    skips one value starting at ``diverge_cycle`` and an err flag pulses.

    Returns (pass_path, fail_path). Divergence time is
    ``diverge_cycle * period + period // 2`` (the posedge where err rises).
    """
    sigs = {"clk": 1, "cnt": 8, "err": 1}

    def build(bug: bool):
        changes: List[Tuple[int, str, str]] = [(0, "clk", "0"),
                                               (0, "cnt", "00000000"),
                                               (0, "err", "0")]
        cnt = 0
        for cyc in range(n_cycles):
            t_rise = cyc * period + period // 2
            t_fall = cyc * period + period
            changes.append((t_rise, "clk", "1"))
            if bug and cyc == diverge_cycle:
                changes.append((t_rise, "err", "1"))
                cnt = (cnt + 2) & 0xFF          # skip a value
            else:
                if bug and cyc == diverge_cycle + 1:
                    changes.append((t_rise, "err", "0"))
                cnt = (cnt + 1) & 0xFF
            changes.append((t_rise, "cnt", format(cnt, "08b")))
            changes.append((t_fall, "clk", "0"))
        return changes

    a = write_fst(f"{dir_a}/pass.fst", sigs, build(False))
    b = write_fst(f"{dir_b}/fail.fst", sigs, build(True))
    return a, b
