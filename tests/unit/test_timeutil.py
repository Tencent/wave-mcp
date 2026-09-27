"""Exact query coordinates, including sub-tick rounding and uint64 limits."""
from __future__ import annotations

import pytest

from wave_mcp.timeutil import parse_time_to_seconds, time_to_fst_units
from wave_mcp.viewer.translate import desired_to_sucl


@pytest.mark.parametrize("text, exp, expected", [
    ("15.5ns", -9, 16),
    ("62.5ns", -9, 62),
    ("0.0155us", -9, 16),
    ("15.499999999999999999999999999999ns", -9, 15),
    ("62.500000000000000000000000000001ns", -9, 63),
    ("9007199254740993fs", -15, 9007199254740993),
    ("9.007199254740993s", -15, 9007199254740993),
    ("18446744073709551615fs", -15, 2**64 - 1),
    ("18446744073709551615.4fs", -15, 2**64 - 1),
    ("  2.5 NS  ", -12, 2500),
    (".5", -3, 500),
    ("0fs", -15, 0),
])
def test_exact_tick_conversion(text, exp, expected):
    assert time_to_fst_units(text, exp) == expected


@pytest.mark.parametrize("text", [
    "18446744073709551616fs",
    "18446744073709551615.5fs",
    "9" * 400 + "s",
])
def test_out_of_range_is_a_value_error(text):
    with pytest.raises(ValueError, match="representable FST time range"):
        time_to_fst_units(text, -15)


@pytest.mark.parametrize("text", ["-1ns", "nan", "inf", "1fortnight", "1e3ns", ""])
def test_invalid_input_stays_rejected(text):
    for parser in (parse_time_to_seconds, lambda text: time_to_fst_units(text, -9)):
        with pytest.raises(ValueError):
            parser(text)


def test_seconds_parser_keeps_float_contract():
    value = parse_time_to_seconds(" 2.5 NS ")
    assert isinstance(value, float)
    assert value == pytest.approx(2.5e-9)


def test_viewer_cursor_uses_the_exact_tick():
    commands = desired_to_sucl({"cursor": {"time": "15.5", "unit": "ns"}}, -9)
    assert commands == "cursor_set 16;goto_time 16"


@pytest.mark.parametrize("text, exp, transition, expected", [
    ("15.5ns", -9, 16, "1"),
    ("62.5ns", -9, 63, "0"),
    ("9007199254740993fs", -15, 9007199254740993, "1"),
])
def test_signal_query_uses_the_exact_tick(tmp_path, text, exp, transition, expected):
    from fstgen import write_fst
    from session_client import bound
    from wave_mcp.pipeline import prepare_session

    fst_path = write_fst(str(tmp_path / "time.fst"), {"data": 1},
                         [(0, "data", "0"), (transition, "data", "1"),
                          (transition + 1, "data", "0")], timescale_exp=exp)
    result = prepare_session(str(tmp_path / "session"), fst_path, top="", filelist=None)
    client = bound(result["session_path"])
    try:
        reply = client.signal_values(paths=["top.data"], time=text)
        assert reply["signals"][0]["value"] == expected
        invalid = client.signal_values(paths=["top.data"], time="9" * 400 + "s")
        assert invalid["error_type"] == "invalid_argument"
        assert invalid["parameter"] == "time"
    finally:
        client.close()
