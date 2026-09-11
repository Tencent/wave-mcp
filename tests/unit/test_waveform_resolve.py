"""Unit tests for convert.waveform_kind / resolve_waveform / UnsupportedWaveformError."""
from __future__ import annotations

import json
import os
import shutil
import tempfile

import pytest

from wave_mcp import convert


# ---------------------------------------------------------------------------
# waveform_kind
# ---------------------------------------------------------------------------

class TestWaveformKind:
    def test_fst(self):
        assert convert.waveform_kind("dump.fst") == "fst"
        assert convert.waveform_kind("/a/b/c.FST") == "fst"

    def test_vcd(self):
        assert convert.waveform_kind("sim.vcd") == "vcd"
        assert convert.waveform_kind("SIM.VCD") == "vcd"

    def test_fsdb(self):
        assert convert.waveform_kind("run.fsdb") == "fsdb"
        assert convert.waveform_kind("RUN.FSDB") == "fsdb"

    def test_unsupported_ghw(self):
        with pytest.raises(convert.UnsupportedWaveformError, match=r"\.ghw"):
            convert.waveform_kind("trace.ghw")

    def test_unsupported_vpd(self):
        with pytest.raises(convert.UnsupportedWaveformError, match=r"\.vpd"):
            convert.waveform_kind("dump.vpd")

    def test_unsupported_no_ext(self):
        with pytest.raises(convert.UnsupportedWaveformError, match=r"\(none\)"):
            convert.waveform_kind("shm_dir")

    def test_unsupported_random(self):
        with pytest.raises(convert.UnsupportedWaveformError):
            convert.waveform_kind("data.csv")


# ---------------------------------------------------------------------------
# resolve_waveform
# ---------------------------------------------------------------------------

class TestResolveWaveform:
    def test_fst_direct(self, tmp_path):
        fst = tmp_path / "dump.fst"
        fst.write_bytes(b"\x00")  # dummy
        got = convert.resolve_waveform(str(fst))
        assert got["kind"] == "fst"
        assert got["converted"] is False
        assert os.path.abspath(got["fst_path"]) == os.path.abspath(str(fst))

    def test_fst_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            convert.resolve_waveform(str(tmp_path / "missing.fst"))

    def test_unsupported_format(self, tmp_path):
        p = tmp_path / "dump.ghw"
        p.write_bytes(b"\x00")
        with pytest.raises(convert.UnsupportedWaveformError):
            convert.resolve_waveform(str(p))

    def test_vcd_converts_and_caches(self):
        """VCD -> FST conversion via resolve_waveform, then second call hits cache."""
        src = os.path.join(os.path.dirname(__file__), os.pardir,
                           "fourstate", "sim", "fourstate.vcd")
        if not os.path.exists(src):
            pytest.skip("fourstate VCD not available")
        tmp = tempfile.mkdtemp()
        try:
            work = os.path.join(tmp, "test.vcd")
            shutil.copy(src, work)
            a = convert.resolve_waveform(work)
            assert a["kind"] == "vcd"
            assert a["converted"] is True
            assert os.path.exists(a["fst_path"])
            # second call must hit cache
            b = convert.resolve_waveform(work)
            assert b["cached"] is True
            assert os.path.abspath(a["fst_path"]) == os.path.abspath(b["fst_path"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_bidirectional_cache_hit(self):
        """resolve_waveform and pipeline.prepare_session share the same FST."""
        src = os.path.join(os.path.dirname(__file__), os.pardir,
                           "fourstate", "sim", "fourstate.vcd")
        if not os.path.exists(src):
            pytest.skip("fourstate VCD not available")
        from wave_mcp import pipeline
        from wave_mcp.session import open_session
        tmp = tempfile.mkdtemp()
        try:
            work = os.path.join(tmp, "bidir.vcd")
            shutil.copy(src, work)
            # direction: viewer first, then analysis
            a = convert.resolve_waveform(work)
            b = pipeline.prepare_session(os.path.join(tmp, "sess"), work,
                                         top="", filelist=None)
            s = open_session(b["session_path"])
            assert os.path.abspath(a["fst_path"]) == os.path.abspath(s.fst_path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# _artifact_fallback_root
# ---------------------------------------------------------------------------

class TestFallbackRoot:
    def test_deterministic(self):
        a = convert._artifact_fallback_root()
        b = convert._artifact_fallback_root()
        assert a == b
        assert "wave-mcp" in a

    def test_used_when_src_readonly(self):
        """When source dir is not writable, resolve_waveform uses the shared fallback root."""
        src = os.path.join(os.path.dirname(__file__), os.pardir,
                           "fourstate", "sim", "fourstate.vcd")
        if not os.path.exists(src):
            pytest.skip("fourstate VCD not available")
        tmp = tempfile.mkdtemp()
        try:
            work = os.path.join(tmp, "ro.vcd")
            shutil.copy(src, work)
            orig = convert._dir_writable
            convert._dir_writable = lambda d: False
            try:
                got = convert.resolve_waveform(work)
                assert got["fst_path"].startswith(convert._artifact_fallback_root())
            finally:
                convert._dir_writable = orig
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# wave-session CLI must share the same cache
# ---------------------------------------------------------------------------

class TestBuildSessionCliCache:
    """Regression guard: build_session used to call convert.convert() directly
    and write the FST into --out, so the same waveform was converted again by
    every other entry point. Found on the test machine, 2026-09-10."""

    def _session_fst(self, session_dir):
        with open(os.path.join(session_dir, "session.json")) as fh:
            return json.load(fh)["fst_path"]

    def _run_cli(self, vcd, out):
        import subprocess
        import sys
        return subprocess.run(
            [sys.executable, "-m", "wave_mcp.cli.build_session",
             "--vcd", vcd, "--out", out, "--no-netlist"],
            capture_output=True, text=True)

    def _src(self):
        src = os.path.join(os.path.dirname(__file__), os.pardir,
                           "fourstate", "sim", "fourstate.vcd")
        if not os.path.exists(src):
            pytest.skip("fourstate VCD not available")
        return src

    def test_cli_then_viewer_share_fst(self, tmp_path):
        work = str(tmp_path / "a.vcd")
        shutil.copy(self._src(), work)
        out = str(tmp_path / "sess")
        r = self._run_cli(work, out)
        assert r.returncode == 0, r.stderr
        cli_fst = self._session_fst(out)
        got = convert.resolve_waveform(work)
        assert os.stat(cli_fst).st_ino == os.stat(got["fst_path"]).st_ino
        assert got["cached"] is True

    def test_viewer_then_cli_share_fst(self, tmp_path):
        work = str(tmp_path / "b.vcd")
        shutil.copy(self._src(), work)
        first = convert.resolve_waveform(work)
        out = str(tmp_path / "sess")
        r = self._run_cli(work, out)
        assert r.returncode == 0, r.stderr
        assert os.stat(first["fst_path"]).st_ino == \
            os.stat(self._session_fst(out)).st_ino

    def test_cli_does_not_hide_fst_in_out_dir(self, tmp_path):
        """The FST must live in the shared cache location, not inside --out."""
        work = str(tmp_path / "c.vcd")
        shutil.copy(self._src(), work)
        out = str(tmp_path / "sess")
        self._run_cli(work, out)
        cli_fst = self._session_fst(out)
        assert os.path.dirname(os.path.abspath(cli_fst)) != os.path.abspath(out)

    def test_cli_rejects_unsupported_format(self, tmp_path):
        bad = tmp_path / "dump.ghw"
        bad.write_bytes(b"\x00")
        r = self._run_cli(str(bad), str(tmp_path / "sess"))
        assert r.returncode != 0
        assert "unsupported waveform format" in (r.stderr + r.stdout)
