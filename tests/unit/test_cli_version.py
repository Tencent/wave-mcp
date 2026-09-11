"""Guard the CLI version surface and the bundle build-info contract.

Two defects were reported from the air-gapped test machine:

1. ``wave-mcp --version`` did not exist. The argparse setup only knew about
   ``--transport/--session/--host/--port``, so the one command every operator
   types first failed with ``unrecognized arguments`` and exit code 2.

2. The bundle shipped a root file literally named ``VERSION`` whose content was
   only a build timestamp (``2026-09-11T03:21:25Z``). It read like a broken
   version string. It is now ``BUILD_INFO`` and carries both the wave-mcp
   version and the build time.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

import wave_mcp
from wave_mcp import server

REPO = os.path.normpath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    """Run the wave-mcp CLI out of process so argparse really parses."""
    return subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.argv[0]='wave-mcp'; sys.argv[1:]=%r;"
         "from wave_mcp.cli.main import main; main()" % (list(args),)],
        capture_output=True, text=True)


class TestCLIVersionFlag:
    """``--version`` must work on both entry points and print a real version."""

    def test_server_entry_prints_version(self):
        r = _run_cli("--version")
        assert r.returncode == 0, f"--version failed: {r.stderr}"
        assert wave_mcp.__version__ in r.stdout

    def test_server_version_matches_module(self):
        r = _run_cli("--version")
        assert r.stdout.strip() == f"wave-mcp {wave_mcp.__version__}"

    def test_query_subcommand_prints_version(self):
        r = _run_cli("query", "--version")
        assert r.returncode == 0, f"query --version failed: {r.stderr}"
        assert wave_mcp.__version__ in r.stdout

    def test_version_is_semver_shaped(self):
        """Catch a stray timestamp or 'unknown' reaching the user."""
        assert re.fullmatch(r"\d+\.\d+\.\d+(?:[.\-+][0-9A-Za-z.\-]+)?",
                            wave_mcp.__version__), \
            f"__version__ is not a release number: {wave_mcp.__version__!r}"

    def test_help_lists_version(self):
        r = _run_cli("--help")
        assert "--version" in r.stdout


class TestBuildInfoFile:
    """The bundle must not ship a bare timestamp under the name VERSION."""

    def test_build_script_writes_build_info_not_version(self):
        script = os.path.join(REPO, "deploy", "build_offline_bundle.sh")
        with open(script, encoding="utf-8") as fh:
            body = fh.read()
        assert "BUILD_INFO" in body, "build script no longer writes BUILD_INFO"
        assert '$OUT/VERSION"' not in body, \
            "build script still writes a root file named VERSION"

    def test_build_info_carries_the_version(self):
        """A timestamp alone is what caused the confusion."""
        script = os.path.join(REPO, "deploy", "build_offline_bundle.sh")
        with open(script, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        # The block that writes BUILD_INFO: from the opening brace to the
        # redirect. Anchoring on the redirect avoids picking up the comment
        # above it, which also mentions the version.
        end = next(i for i, ln in enumerate(lines) if '> "$OUT/BUILD_INFO"' in ln)
        start = max(i for i, ln in enumerate(lines[:end]) if ln.strip() == "{")
        block = "\n".join(lines[start:end + 1])
        assert "wave_mcp_version" in block, \
            "BUILD_INFO does not record the wave-mcp version"
        assert "build_time_utc" in block, \
            "BUILD_INFO does not record the build time"

    def test_build_info_emits_parseable_pairs(self):
        """Execute the real block from the build script, not a re-implementation."""
        script = os.path.join(REPO, "deploy", "build_offline_bundle.sh")
        with open(script, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        end = next(i for i, ln in enumerate(lines) if '> "$OUT/BUILD_INFO"' in ln)
        start = max(i for i, ln in enumerate(lines[:end]) if ln.strip() == "{")
        block = "\n".join(lines[start:end + 1])

        out = subprocess.run(
            ["bash", "-c", block.replace('"$OUT/BUILD_INFO"', "/dev/stdout")],
            cwd=REPO, capture_output=True, text=True)
        assert out.returncode == 0, f"BUILD_INFO block failed: {out.stderr}"
        pairs = dict(line.split("=", 1) for line in out.stdout.strip().splitlines())
        assert pairs["wave_mcp_version"] == wave_mcp.__version__
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
                            pairs["build_time_utc"]), \
            f"build_time_utc is not an ISO-8601 UTC stamp: {pairs['build_time_utc']!r}"
