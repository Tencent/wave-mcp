"""Beside-the-source FST placement: reuse, overwrite, cache fallbacks, notices.

Checks the rule in dev-docs/planning/开发标准-身份-文件位置-参数.md section 2:
a full conversion lives at ``<dir>/<name>.fst`` and is reused from there
(hand-converted included); partial conversions and unwritable directories use
the cache; a stale FST beside the source is overwritten; every departure from
"reuse or write beside" carries a notice. Needs vcd2fst (the fourstate VCD).
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import shutil
import stat
import sys
import tempfile
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

from wave_mcp import convert, pipeline                                   # noqa: E402

SRC = os.path.join(ROOT, "tests", "fourstate", "sim", "fourstate.vcd")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    if not os.path.exists(SRC):
        pytest.skip("fourstate VCD not available")
    try:
        convert._check_bin()  # pylint: disable=protected-access
    except convert.ConversionError:
        pytest.skip("vcd2fst not available")
    cache = tmp_path / "cache"
    monkeypatch.setenv("WAVE_MCP_CACHE_ROOT", str(cache))
    src_dir = tmp_path / "sim"
    src_dir.mkdir()
    vcd = src_dir / "dump.vcd"
    shutil.copy(SRC, vcd)
    return {"vcd": str(vcd), "dir": str(src_dir), "cache": str(cache),
            "beside": str(src_dir / "dump.fst")}


def _listing(d):
    return sorted(os.listdir(d))


def test_first_conversion_lands_beside_with_no_bookkeeping(env):
    got = convert.resolve_waveform(env["vcd"])
    assert got["fst_path"] == env["beside"]
    assert got["placement"] == "beside" and got["notice"] is None
    assert got["cached"] is False
    # only the FST itself: no record, lock or temp file next to the source
    assert _listing(env["dir"]) == ["dump.fst", "dump.vcd"]


def test_second_call_reuses_beside(env):
    convert.resolve_waveform(env["vcd"])
    mtime = os.stat(env["beside"]).st_mtime_ns
    again = convert.resolve_waveform(env["vcd"])
    assert again["cached"] is True and again["fst_path"] == env["beside"]
    assert os.stat(env["beside"]).st_mtime_ns == mtime


def test_hand_converted_fst_is_reused(env):
    convert.convert(env["vcd"], env["beside"])        # user ran vcd2fst by hand
    ino = os.stat(env["beside"]).st_ino
    got = convert.resolve_waveform(env["vcd"])
    assert got["cached"] is True and got["fst_path"] == env["beside"]
    assert got["notice"] is None
    assert os.stat(env["beside"]).st_ino == ino
    assert not os.path.exists(os.path.join(env["cache"], "fst")) or \
        not any(f.endswith(".fst") for _r, _d, fs in os.walk(env["cache"]) for f in fs)


def test_stale_hand_fst_is_overwritten_with_notice(env):
    convert.convert(env["vcd"], env["beside"])
    old = time.time() - 3600
    os.utime(env["beside"], (old, old))             # older than the source
    got = convert.resolve_waveform(env["vcd"])
    assert got["cached"] is False and got["fst_path"] == env["beside"]
    assert got["notice"] and "Replaced" in got["notice"] \
        and "older than the source" in got["notice"]
    assert os.stat(env["beside"]).st_mtime > old + 10


def test_our_fst_invalidated_when_source_redumped(env):
    convert.resolve_waveform(env["vcd"])
    with open(env["vcd"], "ab") as fh:
        fh.write(b"\n")
    got = convert.resolve_waveform(env["vcd"])
    assert got["cached"] is False
    assert "source waveform changed" in got["notice"]


def test_corrupt_beside_fst_is_overwritten(env):
    with open(env["beside"], "wb") as fh:
        fh.write(b"not an fst")
    got = convert.resolve_waveform(env["vcd"])
    assert got["cached"] is False and "cannot be opened" in got["notice"]
    assert convert._fst_opens(env["beside"])      # pylint: disable=protected-access


def test_readonly_dir_falls_back_to_cache_with_notice(env):
    before = _listing(env["dir"])
    os.chmod(env["dir"], stat.S_IRUSR | stat.S_IXUSR)
    try:
        if os.access(env["dir"], os.W_OK):
            pytest.skip("running as root: permissions not enforced")
        got = convert.resolve_waveform(env["vcd"])
        again = convert.resolve_waveform(env["vcd"])
    finally:
        os.chmod(env["dir"], stat.S_IRWXU)
    assert got["placement"] == "cache"
    assert got["fst_path"].startswith(os.path.join(env["cache"], "fst"))
    assert "not writable" in got["notice"] and env["beside"] in got["notice"]
    assert again["cached"] is True and "Reused" in again["notice"]
    assert _listing(env["dir"]) == before


def test_unwritable_dir_detected_without_root_semantics(env, monkeypatch):
    """Same fallback, forced through the writability probe (works as root)."""
    monkeypatch.setattr(convert, "_dir_writable", lambda d: "no write permission")
    got = convert.resolve_waveform(env["vcd"])
    assert got["placement"] == "cache" and "not writable" in got["notice"]
    assert _listing(env["dir"]) == ["dump.vcd"]


def test_write_failure_beside_falls_back(env, monkeypatch):
    real = convert._convert_beside                 # pylint: disable=protected-access

    def boom(*a, **k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(convert, "_convert_beside", boom)
    got = convert.resolve_waveform(env["vcd"])
    assert got["placement"] == "cache" and "writing there failed" in got["notice"]
    monkeypatch.setattr(convert, "_convert_beside", real)


def test_partial_conversion_goes_to_cache_with_notice(env, monkeypatch):
    """Slicing is FSDB-only; exercise the placement rule with a fake converter."""
    fsdb = os.path.join(env["dir"], "big.fsdb")
    with open(fsdb, "wb") as fh:
        fh.write(b"FSDB")

    def fake(source, out, kind, **kw):
        convert.convert(env["vcd"], out)
        with open(out + ".hier", "wb") as fh:
            fh.write(b"h")
        return {"fsdb_path": source, "fst_path": out}
    monkeypatch.setattr(convert, "_run_converter", fake)
    got = convert.cached_fst(fsdb, kind="fsdb", scopes=["top.u_core"])
    assert got["placement"] == "cache"
    assert "Partial conversion" in got["notice"] and "u_core" in got["notice"]
    assert not os.path.exists(os.path.join(env["dir"], "big.fst"))
    full = convert.cached_fst(fsdb, kind="fsdb")
    assert full["fst_path"] == os.path.join(env["dir"], "big.fst")
    assert os.path.exists(full["fst_path"] + ".hier")
    assert _listing(env["dir"]) == ["big.fsdb", "big.fst", "big.fst.hier", "dump.vcd"]
    # fsdb beside FST without .hier is stale and gets rebuilt
    os.remove(full["fst_path"] + ".hier")
    again = convert.cached_fst(fsdb, kind="fsdb")
    assert again["cached"] is False and ".hier is missing" in again["notice"]


def test_convert_tool_default_shares_the_file(env):
    from wave_mcp import server
    res = server.convert_vcd_to_fst(env["vcd"])
    assert res["status"] == "ok" and res["fst_path"] == env["beside"]
    assert res["placement"] == "beside"
    got = convert.resolve_waveform(env["vcd"])
    assert got["cached"] is True and got["fst_path"] == env["beside"]
    again = server.convert_vcd_to_fst(env["vcd"])   # explicit request: converts
    assert again["status"] == "ok" and "Replaced" in again["notice"]


def test_prepare_session_reports_notice_in_hints(env, tmp_path, monkeypatch):
    monkeypatch.setenv("WAVE_MCP_SESSION_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setattr(convert, "_dir_writable", lambda d: "no write permission")
    res = pipeline.prepare_session(None, env["vcd"])
    assert any("not writable" in h for h in res["hints"])
    step = res["steps"][0]
    assert step["placement"] == "cache"


def _child(args):
    vcd, cache = args
    os.environ["WAVE_MCP_CACHE_ROOT"] = cache
    sys.path.insert(0, ROOT)
    from wave_mcp import convert as c
    return c.resolve_waveform(vcd)["cached"]


def test_two_processes_convert_once(env):
    ctx = mp.get_context("fork")
    with ctx.Pool(2) as pool:
        res = pool.map(_child, [(env["vcd"], env["cache"])] * 2)
    assert sorted(res) == [False, True]
    assert _listing(env["dir"]) == ["dump.fst", "dump.vcd"]


def test_notices_say_how_to_keep_the_file(env):
    convert.convert(env["vcd"], env["beside"])
    old = time.time() - 3600
    os.utime(env["beside"], (old, old))
    got = convert.resolve_waveform(env["vcd"])
    assert "rename it before converting" in got["notice"]
    assert "out_path" in got["notice"]


def test_partial_notice_points_at_the_full_conversion(env, monkeypatch):
    fsdb = os.path.join(env["dir"], "big.fsdb")
    with open(fsdb, "wb") as fh:
        fh.write(b"FSDB")

    def fake(source, out, kind, **kw):
        convert.convert(env["vcd"], out)
        with open(out + ".hier", "wb") as fh:
            fh.write(b"h")
        return {}
    monkeypatch.setattr(convert, "_run_converter", fake)
    got = convert.cached_fst(fsdb, kind="fsdb", scopes=["top.u_core"])
    assert "without scopes / signals_file" in got["notice"]
    assert os.path.join(env["dir"], "big.fst") in got["notice"]


def test_busy_lock_is_bounded_and_names_the_holder(env, monkeypatch, capsys):
    import fcntl
    from wave_mcp.runtime import storage
    rec = storage.policy().cache_dir("fst", "beside", env["beside"])
    lock = os.path.join(rec, storage.LOCK_NAME)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.write(fd, b"pid 4242@elsewhere\n")
    monkeypatch.setattr(convert, "_lock_wait", lambda *a: 1.0)
    try:
        with pytest.raises(convert.ConversionError) as exc:
            convert.resolve_waveform(env["vcd"])
    finally:
        os.close(fd)
    assert "pid 4242@elsewhere" in str(exc.value) and lock in str(exc.value)
    assert "waiting for another conversion" in capsys.readouterr().err
    assert _listing(env["dir"]) == ["dump.vcd"]


def test_killed_run_leaves_no_temp_after_next_conversion(env):
    import socket
    dead = 2 ** 22 + 12345          # beyond pid_max defaults: never alive
    host = socket.gethostname()
    stale = os.path.join(env["dir"], f".dump.fst.wave-mcp-{host}-{dead}.tmp.fst")
    other = os.path.join(env["dir"], f".dump.fst.wave-mcp-otherhost-{dead}.tmp.fst")
    for p in (stale, stale + ".hier", other):
        with open(p, "wb") as fh:
            fh.write(b"partial")
    convert.resolve_waveform(env["vcd"])
    names = _listing(env["dir"])
    assert os.path.basename(stale) not in names
    assert os.path.basename(stale) + ".hier" not in names
    assert os.path.basename(other) in names     # another host's: not ours to judge


def test_stop_active_conversions_kills_the_converter_tree(tmp_path):
    import subprocess
    marker = tmp_path / "alive"
    script = tmp_path / "conv.sh"
    script.write_text(f"#!/bin/sh\nsleep 30 & wait\n")
    script.chmod(0o755)
    proc = subprocess.Popen([str(script)], start_new_session=True)
    convert._ACTIVE.add(proc)                  # pylint: disable=protected-access
    assert convert.stop_active_conversions() == 1
    assert proc.wait(timeout=5) is not None
    out = subprocess.run(["pgrep", "-g", str(proc.pid)], capture_output=True)
    assert out.returncode != 0 and not marker.exists()
