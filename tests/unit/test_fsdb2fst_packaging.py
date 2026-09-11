"""Guard the fsdb2fst build inputs against silently dropping out of the package.

FSDB support shipped in v0.2.0 but the converter sources were never packaged,
so the on-demand build had nothing to compile and the feature was unreachable
for six releases (v0.2.0 through v0.2.5). These tests fail if that regresses.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from wave_mcp import convert

REPO = os.path.normpath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))


class TestBuildInputsResolvable:
    """convert.py must find the sources in a checkout AND after a pip install."""

    def test_source_dir_resolves_to_existing_sources(self):
        src = os.path.join(convert._FSDB2FST_SRC_DIR, "fsdb2fst.cpp")
        assert os.path.isfile(src), f"fsdb2fst.cpp not found at {src}"

    def test_build_script_resolves(self):
        assert os.path.isfile(convert._FSDB2FST_BUILD_SH), \
            f"build script not found at {convert._FSDB2FST_BUILD_SH}"

    def test_all_compile_units_present(self):
        """Every file the build script feeds to g++ must be resolvable."""
        needed = ["fsdb2fst.cpp", "fst/fstapi.c", "fst/lz4.c", "fst/fastlz.c",
                  "fst/fstapi.h", "fst/lz4.h", "fst/fastlz.h", "fst/config.h"]
        missing = [n for n in needed
                   if not os.path.isfile(os.path.join(convert._FSDB2FST_SRC_DIR, n))]
        assert not missing, f"missing build inputs: {missing}"

    def test_checkout_layout_wins_over_installed_copy(self):
        """A local edit must never be shadowed by an older installed copy."""
        repo_src = os.path.join(REPO, "third_party", "fsdb2fst", "fsdb2fst.cpp")
        if not os.path.isfile(repo_src):
            pytest.skip("not running from a checkout")
        assert os.path.abspath(convert._FSDB2FST_SRC_DIR) == \
            os.path.abspath(os.path.join(REPO, "third_party", "fsdb2fst"))


class TestPackagingDeclaresBuildInputs:
    """The packaging config itself must keep shipping these files."""

    def test_manifest_includes_sources(self):
        manifest = open(os.path.join(REPO, "MANIFEST.in")).read()
        assert "third_party/fsdb2fst" in manifest
        assert "build_fsdb2fst.sh" in manifest

    def test_pyproject_installs_sources_as_data_files(self):
        pyproject = open(os.path.join(REPO, "pyproject.toml")).read()
        assert "share/wave-mcp/fsdb2fst" in pyproject
        assert "fsdb2fst.cpp" in pyproject
        assert "build_fsdb2fst.sh" in pyproject

    def test_user_facing_guides_are_packaged(self):
        """Errors point at docs/FSDB_GUIDE.md, so it must ship."""
        pyproject = open(os.path.join(REPO, "pyproject.toml")).read()
        assert "docs/FSDB_GUIDE.md" in pyproject


class TestMissingBinaryErrorIsHonest:
    """The error must not tell users to run a file they do not have."""

    def test_offers_real_script_path_when_sources_present(self):
        if not os.path.isfile(convert._FSDB2FST_BUILD_SH):
            pytest.skip("sources not available in this layout")
        msg = str(convert.fsdb2fst_missing_error())
        assert convert._FSDB2FST_BUILD_SH in msg, \
            "error should name the actual build script path"
        assert "sources are not shipped" not in msg

    def test_no_contradictory_advice(self, monkeypatch):
        """With sources absent, do not advertise the build script at all."""
        monkeypatch.setattr(convert, "_FSDB2FST_BUILD_SH", "/nonexistent/build.sh")
        monkeypatch.setattr(convert, "_FSDB2FST_SRC_DIR", "/nonexistent")
        msg = str(convert.fsdb2fst_missing_error())
        assert "bash /nonexistent/build.sh" not in msg
        assert "$FSDB2FST_BIN" in msg


class TestSourcesActuallyCompile:
    """The packaged sources must be complete enough to build."""

    def test_compiles_with_stub_headers(self, tmp_path):
        import shutil
        if not shutil.which("g++"):
            pytest.skip("g++ not available")
        src = convert._FSDB2FST_SRC_DIR
        stub = os.path.join(src, "ffrAPI_stub.h")
        stub_impl = os.path.join(src, "ffrAPI_stub_impl.cpp")
        if not (os.path.isfile(stub) and os.path.isfile(stub_impl)):
            pytest.skip("offline compile stubs not shipped in this layout")
        out = str(tmp_path / "fsdb2fst_test")
        r = subprocess.run(
            ["g++", "-O2", "-std=c++17", "-w",
             '-DFFR_API_INCLUDE="ffrAPI_stub.h"',
             "-I", src, "-I", os.path.join(src, "fst"), "-o", out,
             os.path.join(src, "fsdb2fst.cpp"), stub_impl,
             os.path.join(src, "fst", "fstapi.c"),
             os.path.join(src, "fst", "lz4.c"),
             os.path.join(src, "fst", "fastlz.c"),
             "-lz", "-lpthread", "-ldl"],
            capture_output=True, text=True)
        assert r.returncode == 0, f"compile failed:\n{r.stderr[-1500:]}"
        assert os.path.isfile(out)
        h = subprocess.run([out, "-h"], capture_output=True, text=True)
        assert "fsdb2fst" in (h.stdout + h.stderr)
