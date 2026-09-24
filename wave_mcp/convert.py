"""VCD -> FST conversion (fast path).

xrun (Xcelium) can dump open / parseable waveforms only as **VCD**, but the rest
of the open-source stack (and this server) works best on **FST** (1/50 the size,
random access). This module wraps GTKWave's ``vcd2fst`` with the fastest options
and offers a *streaming* mode that hides the conversion time inside simulation
time.

Speed levers (all available in the installed ``vcd2fst``):
  * ``-p`` parallel mode (uses multiple cores)
  * ``-F`` fastlz   -> fastest, slightly larger
  * ``-4`` lz4      -> default, good speed/size balance
  * ``-Z`` zlib     -> smallest, slowest

Two usage modes:
  1. **Post-process** (default): convert an existing .vcd to .fst.
  2. **Streaming** (fastest end-to-end): create a named pipe (FIFO), launch
     ``vcd2fst`` reading the FIFO in the background, then point ``$dumpfile`` at
     the FIFO. Conversion overlaps simulation, so the .fst is ready almost as
     soon as the sim finishes — near-zero extra wall-clock cost.

This module also converts **FSDB** (Synopsys, closed format) through the bundled
``fsdb2fst`` single-pass converter (see docs/FSDB_GUIDE.md). Unlike vcd2fst,
fsdb2fst is built locally against Verdi's FsdbReader runtime, so it is resolved
through ``$FSDB2FST_BIN`` -> repo-local ``third_party/fsdb2fst/fsdb2fst`` ->
user cache -> PATH. When none of those hold a usable binary but a FsdbReader
runtime is reachable (``$VERDI_HOME`` / ``$NOVAS_HOME`` / ``$FSDB2FST_FREADER``),
the converter is **built on demand** into the user cache, so setting
``VERDI_HOME`` in the MCP config is the only setup step a user has to do.

Both conversions share one placement rule (``cached_fst``): the FST lives
beside its source as ``<name>.fst`` and is reused from there, including one the
user converted by hand, until the source changes. Partial conversions and
unwritable source directories use the derived cache instead. FSDB files are
routinely GB-scale and take minutes to convert, so without reuse every
prepare_session on the same waveform would pay the full cost again.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import signal
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional

from .runtime import storage
from .runtime.identity import cache_key, file_version
from .runtime.storage import StoragePolicy

def _resolve_vcd2fst() -> str:
    """Locate vcd2fst: ``$VCD2FST_BIN`` | PATH | ``<prefix>/bin/vcd2fst``.

    The offline bundle's launcher puts ``$PREFIX/bin`` on PATH, but in-process
    API use (import wave_mcp directly, no launcher) has no such PATH edit; the
    binary installed next to the interpreter must still be found without the
    caller setting ``$VCD2FST_BIN`` by hand.
    """
    explicit = os.environ.get("VCD2FST_BIN")
    if explicit:
        return explicit
    if shutil.which("vcd2fst"):
        return "vcd2fst"
    prefixes = [sys.prefix, getattr(sys, "base_prefix", sys.prefix)]
    # The offline bundle installs the binary at <prefix>/bin while the venv
    # lives at <prefix>/runtime; the venv's parent directory is that prefix.
    prefixes += [os.path.dirname(p) for p in list(prefixes)]
    for prefix in dict.fromkeys(prefixes):
        cand = os.path.join(prefix, "bin", "vcd2fst")
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return "vcd2fst"


VCD2FST_BIN = _resolve_vcd2fst()
FSDB2FST_BIN_ENV = os.environ.get("FSDB2FST_BIN")

# repo-local build output of deploy/build_fsdb2fst.sh, used when $FSDB2FST_BIN
# is unset. Path is relative to this file: wave_mcp/ -> <repo>/third_party/...
_REPO_FSDB2FST = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    os.pardir, "third_party", "fsdb2fst", "fsdb2fst"))

# Sources and build script for the on-demand build. Present in a git checkout;
# absent in a pip install, where auto-build is simply skipped.
_REPO_ROOT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir))


def _resolve_build_inputs() -> tuple:
    """Locate the fsdb2fst sources and build script.

    Two layouts have to work. In a git checkout they sit at the repo root; in
    an installed package they land under ``<prefix>/share/wave-mcp`` via
    ``data-files``. Before this, only the checkout layout was resolved, so a
    pip install had nothing to compile and FSDB support was unreachable even
    with VERDI_HOME set correctly (the auto-build just returned None).

    Returns ``(src_dir, build_script)``; the checkout wins when both exist so
    local edits are never shadowed by an older installed copy.
    """
    candidates = [
        (os.path.join(_REPO_ROOT, "third_party", "fsdb2fst"),
         os.path.join(_REPO_ROOT, "deploy", "build_fsdb2fst.sh")),
    ]
    # sys.prefix covers a venv; base_prefix covers a --user or system install.
    for prefix in dict.fromkeys((sys.prefix, getattr(sys, "base_prefix", sys.prefix))):
        candidates.append((
            os.path.join(prefix, "share", "wave-mcp", "fsdb2fst"),
            os.path.join(prefix, "share", "wave-mcp", "deploy", "build_fsdb2fst.sh"),
        ))
    for src_dir, script in candidates:
        if os.path.isfile(os.path.join(src_dir, "fsdb2fst.cpp")) and os.path.isfile(script):
            return src_dir, script
    # Nothing usable: keep the checkout paths so error messages stay concrete.
    return candidates[0]


_FSDB2FST_SRC_DIR, _FSDB2FST_BUILD_SH = _resolve_build_inputs()

# Auto-build is on by default; WAVE_MCP_FSDB2FST_AUTOBUILD=0 disables it.
_AUTOBUILD_ENABLED = os.environ.get(
    "WAVE_MCP_FSDB2FST_AUTOBUILD", "1").strip().lower() not in ("0", "false", "no")

# pack (compressor) -> vcd2fst flag. Same vocabulary as fsdb2fst's ``-p``.
_PACK_FLAG = {
    "fastlz": "-F",     # fastest, slightly larger (vcd2fst default here)
    "lz4": "-4",        # good speed/size balance (fsdb2fst default)
    "zlib": "-Z",       # smallest, slowest
}
#: Compressors accepted by ``pack`` on both converters.
PACKS = tuple(_PACK_FLAG)


class ConversionError(RuntimeError):
    pass


@dataclass
class ConversionResult:
    vcd_path: str
    fst_path: str
    pack: str
    parallel: bool
    elapsed_sec: float
    vcd_bytes: Optional[int] = None
    fst_bytes: Optional[int] = None
    command: List[str] = field(default_factory=list)
    streaming: bool = False
    pid: Optional[int] = None

    @property
    def ratio(self) -> Optional[float]:
        if self.vcd_bytes and self.fst_bytes:
            return round(self.vcd_bytes / self.fst_bytes, 1)
        return None

    def to_dict(self) -> dict:
        return {
            "vcd_path": self.vcd_path,
            "fst_path": self.fst_path,
            "pack": self.pack,
            "parallel": self.parallel,
            "streaming": self.streaming,
            "pid": self.pid,
            "elapsed_sec": round(self.elapsed_sec, 3),
            "vcd_bytes": self.vcd_bytes,
            "fst_bytes": self.fst_bytes,
            "compression_ratio": self.ratio,
            "command": " ".join(self.command),
        }


def _check_bin():
    if shutil.which(VCD2FST_BIN) is None:
        raise ConversionError(
            f"'{VCD2FST_BIN}' not found — needed to convert VCD -> FST.\n"
            f"Options:\n"
            f"  * install GTKWave (provides vcd2fst):\n"
            f"      Debian/Ubuntu:  sudo apt install gtkwave\n"
            f"      Fedora/RHEL:    sudo dnf install gtkwave\n"
            f"      macOS:          brew install gtkwave\n"
            f"  * or set $VCD2FST_BIN to a vcd2fst binary (e.g. from the offline bundle)\n"
            f"  * or skip conversion entirely: dump FST directly from your simulator\n"
            f"      (Verilator --trace-fst, Icarus -fst, ...) and pass the .fst.")


# vcd2fst's -p (parallel) path is compiled behind FST_WRITER_PARALLEL. Many
# builds (incl. our air-gapped bundle) ship without it, so `-p` aborts at
# runtime with rc=255 ("FST_WRITER_PARALLEL not enabled during compile").
# We probe once and cache, and also hard-fallback if a real run still trips it.
_PARALLEL_SUPPORTED: Optional[bool] = None
# marker text emitted by fstapi when the parallel path is compiled out
_PARALLEL_DISABLED_MARK = "FST_WRITER_PARALLEL not enabled"


def _parallel_supported() -> bool:
    """Best-effort detect whether the vcd2fst binary supports ``-p`` at runtime.

    Probes by converting a tiny throwaway VCD with ``-p``; a build without the
    parallel path exits non-zero with the FST_WRITER_PARALLEL marker. Result is
    cached for the process. On any uncertainty we assume False (safe: serial
    conversion always works) so a first-time prepare_session never hard-fails.
    """
    global _PARALLEL_SUPPORTED
    if _PARALLEL_SUPPORTED is not None:
        return _PARALLEL_SUPPORTED
    _PARALLEL_SUPPORTED = False
    d = None
    try:
        import tempfile
        d = tempfile.mkdtemp(prefix="vcd2fst_probe_")
        vcd = os.path.join(d, "p.vcd")
        fst = os.path.join(d, "p.fst")
        # minimal valid VCD: one 1-bit signal toggling once
        with open(vcd, "w") as fh:
            fh.write("$timescale 1ns $end\n$scope module t $end\n"
                     "$var wire 1 ! a $end\n$upscope $end\n$enddefinitions $end\n"
                     "#0\n0!\n#1\n1!\n")
        proc = subprocess.run([VCD2FST_BIN, "-F", "-p", "-v", vcd, "-f", fst],
                              capture_output=True, text=True, timeout=30)
        out = (proc.stderr or "") + (proc.stdout or "")
        if proc.returncode == 0 and os.path.exists(fst) \
                and _PARALLEL_DISABLED_MARK not in out:
            _PARALLEL_SUPPORTED = True
    except (OSError, subprocess.SubprocessError):
        _PARALLEL_SUPPORTED = False  # probe failure -> assume no parallel (serial always works)
    finally:
        if d:
            shutil.rmtree(d, ignore_errors=True)  # never leave probe files in /tmp
    return _PARALLEL_SUPPORTED


def _build_cmd(vcd: str, fst: str, pack: str, parallel: bool,
               compress: bool) -> List[str]:
    flag = _PACK_FLAG.get(pack)
    if flag is None:
        raise ConversionError(f"unknown pack {pack!r}; expected one of {list(_PACK_FLAG)}")
    cmd = [VCD2FST_BIN, flag]
    # only add -p if the binary actually supports the parallel path; otherwise
    # it would abort with rc=255 and break every first-time conversion.
    if parallel and _parallel_supported():
        cmd.append("-p")
    if compress:
        cmd.append("-c")
    cmd += ["-v", vcd, "-f", fst]
    return cmd


# ---- conversion liveness guard --------------------------------------------
# subprocess.run(timeout=...) only bounds total wall-clock: a converter wedged
# on I/O would sit silently until the cap (the 53 GB VCD scenario burned hours
# with zero output). These helpers add a size-based default timeout plus an
# output-growth heartbeat, and surface a progress line so a long conversion is
# not a black box.

_HEARTBEAT_INTERVAL = 30.0   # emit a progress line at most this often
_STALL_LIMIT = 600.0         # no output growth for this long -> stuck

# throughputs (MB per minute) for the auto-timeout estimate. Small inputs are
# usually served from page cache / local SSD and run far faster, so they use a
# loftier number than multi-GB files (where disk backpressure dominates).
_THROUGHPUT_MB_PER_MIN = {"vcd": 200.0, "fsdb": 100.0}
_SMALL_WAVE_THROUGHPUT_MB_PER_MIN = {"vcd": 600.0, "fsdb": 300.0}
_SMALL_WAVE_MB = 2048.0      # below this, the fast throughput applies
_MIN_TIMEOUT = 300.0         # 5 minutes floor
_MAX_TIMEOUT = 4 * 3600.0    # 4 hours cap
_ESTIMATE_LOG_MIN_BYTES = 100 * 1024 * 1024   # log an estimate only past 100 MB


def _estimate_timeout(source_bytes: int, kind: str) -> float:
    """Size-based default timeout for a conversion.

    Conservative throughputs times a 3x safety margin, floored at 5 minutes
    and capped at 4 hours: a genuinely huge file still gets its time, but one
    stuck conversion cannot hold a session forever. Throughput is picked by
    size because conversion speed depends far more on disk backpressure than
    on the format (a 100 MB file and a 50 GB file differ by two orders of
    magnitude in achievable MB/min).
    """
    mb = max(source_bytes, 0) / (1024 * 1024)
    if mb < _SMALL_WAVE_MB:
        per_min = _SMALL_WAVE_THROUGHPUT_MB_PER_MIN.get(kind, 600.0)
    else:
        per_min = _THROUGHPUT_MB_PER_MIN.get(kind, 200.0)
    return max(_MIN_TIMEOUT, min(mb / per_min * 3 * 60.0, _MAX_TIMEOUT))


def _log_estimate(source_path: str, source_bytes: int, timeout: float,
                  kind: str) -> None:
    """One stderr line announcing a sizeable conversion before it starts.

    Only for conversions big enough to matter (>100 MB source): a shorter one
    finishes before the line could be read, and skipping it keeps the common
    case quiet. The client (or user) sees the expected time up front instead
    of discovering it by waiting.
    """
    if source_bytes < _ESTIMATE_LOG_MIN_BYTES:
        return
    est_min = timeout / 3.0 / 60.0   # strip the safety margin back out
    sys.stderr.write(
        f"[wave-mcp] converting {os.path.basename(source_path)} "
        f"({source_bytes / (1024 ** 3):.2f} GB {kind.upper()}): est "
        f"{est_min:.0f}-{est_min * 2:.0f} min, timeout {timeout / 60:.0f} min. "
        f"Narrow the scope (scopes / signals_file) or dump FST directly "
        f"from the simulator for faster results.\n")
    sys.stderr.flush()


def _run_with_heartbeat(cmd: List[str], fst_path: str, timeout: float,
                        kind: str) -> tuple:
    """Run one conversion command with liveness monitoring.

    Returns ``(returncode, combined_output)``. Raises ``ConversionError`` when
    the wall-clock cap is exceeded or the output file stops growing after it
    started (a wedged converter), so a stuck job fails fast with a precise
    message instead of silently burning its timeout.
    """
    def _bytes_of(path: str) -> int:
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    def _emit(msg: str) -> None:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()

    # Pipe the child's output to a temporary file rather than a pipe: with a
    # pipe, a chatty converter could fill the buffer and deadlock while we
    # poll (nobody is draining it), turning a healthy run into a false stall.
    with tempfile.TemporaryFile(mode="w+") as buf:
        # Own process group: a timeout or stall must take down the converter
        # *and anything it forked*, and only those. Killing by group id never
        # reaches other converters or viewers on the host.
        proc = subprocess.Popen(cmd, stdout=buf, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        _ACTIVE.add(proc)
        start = time.time()
        last_size = _bytes_of(fst_path)
        last_growth = start
        last_emit = start
        started_growing = False
        interval = 0.5   # poll fast at first so short conversions exit promptly
        try:
            while proc.poll() is None:
                now = time.time()
                if timeout and now - start > timeout:
                    raise ConversionError(
                        f"{kind} conversion exceeded its {timeout:.0f}s timeout "
                        f"after writing {last_size / 1e6:.1f} MB. Pass a larger "
                        f"timeout=, narrow the scope (scopes / "
                        f"signals_file), or split the waveform.")
                size = _bytes_of(fst_path)
                if size != last_size:
                    last_size = size
                    last_growth = now
                    if size > 0:
                        started_growing = True
                    if now - last_emit >= _HEARTBEAT_INTERVAL:
                        _emit(f"[wave-mcp] {kind} conversion: "
                              f"{size / 1e6:.1f} MB written "
                              f"({now - start:.0f}s elapsed)")
                        last_emit = now
                elif started_growing and now - last_growth > _STALL_LIMIT:
                    # Only meaningful once the converter has produced output:
                    # some converters build state in memory before the first
                    # write, which must not read as a stall (the total timeout
                    # still bounds that phase).
                    raise ConversionError(
                        f"{kind} conversion stalled: no output growth for "
                        f"{_STALL_LIMIT:.0f}s (stuck at "
                        f"{last_size / 1e6:.1f} MB). The converter may have "
                        f"hit an internal error or unresponsive I/O; re-run it "
                        f"by hand to see its own diagnostics.")
                elif now - last_emit >= _HEARTBEAT_INTERVAL:
                    # no growth this round: still say we are alive, and how
                    # long until the cap, so a slow read phase is not silent
                    _emit(f"[wave-mcp] {kind} conversion running "
                          f"({now - start:.0f}s elapsed, "
                          f"{last_size / 1e6:.1f} MB written, pid "
                          f"{proc.pid}"
                          f"{f', timeout {timeout:.0f}s' if timeout else ''})")
                    last_emit = now
                time.sleep(interval)
                interval = min(interval * 1.5, 5.0)
        finally:
            if proc.poll() is None:
                _kill_own_tree(proc)
            _ACTIVE.discard(proc)
        buf.seek(0)
        return proc.returncode, (buf.read() or "")


#: converters started by this process and not yet finished. The converter runs
#: in its own session so a stall kill reaches only its tree, which also means
#: the kernel does not stop it when this process dies: whoever ends the process
#: calls :func:`stop_active_conversions` first.
_ACTIVE: "set[subprocess.Popen[Any]]" = set()


def stop_active_conversions() -> int:
    """Terminate every converter this process started; returns how many."""
    procs = [p for p in list(_ACTIVE) if p.poll() is None]
    for p in procs:
        _kill_own_tree(p, grace=1.0)
    _ACTIVE.clear()
    return len(procs)


def install_exit_handlers() -> None:
    """Stop running converters on SIGTERM/SIGHUP/SIGINT and at exit.

    For the CLI entry points; the MCP server calls
    :func:`stop_active_conversions` from its own shutdown handler. Without
    this, ``timeout`` or a closed terminal kills wave-mcp but leaves the
    converter running, still writing a hidden temporary file beside the
    source.
    """
    import atexit
    atexit.register(stop_active_conversions)

    def _stop(signum, _frame):
        stop_active_conversions()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            if signal.getsignal(sig) in (signal.SIG_DFL, None):
                signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass


def _kill_own_tree(proc: "subprocess.Popen[Any]", grace: float = 2.0) -> None:
    """Terminate a child started with ``start_new_session=True`` and its group.

    SIGTERM first so the converter can drop partial output, SIGKILL after
    ``grace`` seconds. Scoped to the child's own process group; nothing else on
    the host is touched.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None

    def _signal(sig: int) -> None:
        try:
            if pgid is not None and pgid != os.getpgid(0):
                os.killpg(pgid, sig)
            else:
                proc.send_signal(sig)
        except OSError:
            pass

    _signal(signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        _signal(signal.SIGKILL)
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass


def _default_out_path(source: str) -> str:
    """Conversion target when a library caller passes no output path.

    ``<name>.fst`` beside the source, like the automatic conversions, or the
    source's cache directory when that directory is not writable. The MCP
    tools and CLIs go through :func:`default_fst` instead, which also records
    the conversion and reports the placement.
    """
    target = beside_path(source)
    if _dir_writable(os.path.dirname(target)) is None:
        return target
    kind = "fsdb" if source.lower().endswith(".fsdb") else "vcd"
    opts = _opts_for(kind, default_pack(kind), None, None)
    cache_dir = storage.policy().cache_dir("fst", source,
                                           json.dumps(opts, sort_keys=True))
    return os.path.join(cache_dir, os.path.basename(target))


def convert(vcd_path: str, fst_path: Optional[str] = None, pack: str = "fastlz",
            parallel: bool = True, compress: bool = False,
            timeout: Optional[float] = None) -> ConversionResult:
    """Convert an existing VCD file to FST. Returns timing + size stats.

    ``timeout=None`` (default) auto-estimates a size-based cap — see
    ``_estimate_timeout`` — and announce it on stderr for large inputs; pass
    an explicit value to override. The run is monitored: a converter that
    stops writing output for 10 minutes fails fast with a precise message
    instead of hanging until the cap.
    """
    _check_bin()
    vcd_path = os.path.abspath(vcd_path)
    if not os.path.exists(vcd_path):
        raise ConversionError(f"VCD not found: {vcd_path}")
    if fst_path is None:
        fst_path = _default_out_path(vcd_path)
    fst_path = os.path.abspath(fst_path)
    os.makedirs(os.path.dirname(fst_path) or ".", exist_ok=True)

    vcd_bytes = os.path.getsize(vcd_path)
    if timeout is None:
        timeout = _estimate_timeout(vcd_bytes, "vcd")
        _log_estimate(vcd_path, vcd_bytes, timeout, "vcd")
    cmd = _build_cmd(vcd_path, fst_path, pack, parallel, compress)
    used_parallel = "-p" in cmd
    t0 = time.time()
    rc, out = _run_with_heartbeat(cmd, fst_path, timeout, "vcd")
    elapsed = time.time() - t0
    # hard fallback: if -p slipped through (probe false-positive / stale cache)
    # and the binary lacks the parallel path, retry once without -p.
    if used_parallel and (rc != 0 or not os.path.exists(fst_path)) \
            and _PARALLEL_DISABLED_MARK in out:
        global _PARALLEL_SUPPORTED
        _PARALLEL_SUPPORTED = False  # remember for the rest of the process
        cmd = _build_cmd(vcd_path, fst_path, pack, False, compress)
        used_parallel = False
        t0 = time.time()
        rc, out = _run_with_heartbeat(cmd, fst_path, timeout, "vcd")
        elapsed = time.time() - t0
    if rc != 0 or not os.path.exists(fst_path):
        raise ConversionError(
            f"vcd2fst failed (rc={rc}): {out.strip() or '(no output)'}")
    return ConversionResult(
        vcd_path=vcd_path, fst_path=fst_path, pack=pack, parallel=used_parallel,
        elapsed_sec=elapsed, vcd_bytes=vcd_bytes,
        fst_bytes=os.path.getsize(fst_path), command=cmd)


def start_streaming(fifo_path: str, fst_path: Optional[str] = None,
                    pack: str = "fastlz", parallel: bool = True,
                    log_path: Optional[str] = None) -> ConversionResult:
    """Set up a streaming conversion: create a FIFO and launch vcd2fst in the
    background to consume it.

    Workflow (hides conversion in simulation time)::

        res = start_streaming("sim/dump.vcd", "sim/dump.fst")
        # in the TB:  $dumpfile("sim/dump.vcd");  (writes to the FIFO)
        # run xrun ... ; when sim finishes, dump.fst is ready.

    Returns immediately with the background process pid. The caller should
    ``waitpid`` / poll ``pid`` after the simulation finishes.
    """
    _check_bin()
    fifo_path = os.path.abspath(fifo_path)
    if fst_path is None:
        fst_path = os.path.splitext(fifo_path)[0] + ".fst"
    fst_path = os.path.abspath(fst_path)
    os.makedirs(os.path.dirname(fifo_path) or ".", exist_ok=True)

    # (re)create the FIFO
    if os.path.exists(fifo_path):
        if not os.path.exists(fifo_path) or not _is_fifo(fifo_path):
            os.remove(fifo_path)
    if not os.path.exists(fifo_path):
        os.mkfifo(fifo_path)

    cmd = _build_cmd(fifo_path, fst_path, pack, parallel, compress=False)
    logf = open(log_path, "w") if log_path else subprocess.DEVNULL
    # vcd2fst will block opening the FIFO until the writer (xrun) connects.
    proc = subprocess.Popen(cmd, stdout=logf, stderr=logf, start_new_session=True)
    return ConversionResult(
        vcd_path=fifo_path, fst_path=fst_path, pack=pack, parallel=parallel,
        elapsed_sec=0.0, command=cmd, streaming=True, pid=proc.pid)


def _is_fifo(path: str) -> bool:
    import stat
    try:
        return stat.S_ISFIFO(os.stat(path).st_mode)
    except OSError:
        return False


# =============================================================================
# FSDB -> FST (bundled fsdb2fst; see docs/FSDB_GUIDE.md)
# =============================================================================

def _fsdb2fst_cache_root() -> str:
    """Root of the locally built fsdb2fst binaries (derived-cache layer)."""
    return storage.policy().cache_dir("fsdb2fst", create=False)


def _fsdb2fst_cache_dir(key: str) -> str:
    """Per-key directory for one locally built fsdb2fst."""
    return os.path.join(_fsdb2fst_cache_root(), key)


# Formats wave-mcp can open. An unknown extension is rejected BY NAME instead
# of being handed to vcd2fst, whose parse error ("VCD not found" / rc=1 noise)
# blames the wrong thing and hides the real problem: the format is unsupported.
WAVEFORM_EXTENSIONS = (".fst", ".vcd", ".fsdb")


class UnsupportedWaveformError(ValueError):
    """Raised when a waveform path is not a format wave-mcp can open."""


def waveform_kind(path: str) -> str:
    """Classify a waveform path by extension: ``fst`` / ``vcd`` / ``fsdb``.

    Raises ``UnsupportedWaveformError`` naming the supported formats for
    anything else (GHW, VPD, SHM, no extension, ...) rather than letting it
    fall through to the VCD converter and fail with a misleading message.
    """
    lowered = str(path).lower()
    if lowered.endswith(".fst"):
        return "fst"
    if lowered.endswith(".fsdb"):
        return "fsdb"
    if lowered.endswith(".vcd"):
        return "vcd"
    ext = os.path.splitext(str(path))[1] or "(none)"
    raise UnsupportedWaveformError(
        f"unsupported waveform format: {path} (extension {ext!r}). "
        f"Supported: {', '.join(WAVEFORM_EXTENSIONS)} — .fst is read "
        f"directly, .vcd and .fsdb are converted to FST automatically. "
        f"Convert this file to one of those first, e.g. with the tool that "
        f"produced it (gtkwave / simvision / fsdb2vcd).")


def resolve_fsdb_reader() -> Optional[str]:
    """Locate a usable Verdi FsdbReader package for building fsdb2fst.

    Mirrors the resolution order of deploy/build_fsdb2fst.sh, minus the
    repo-local runtime symlink which the script handles on its own.
    Returns the FsdbReader directory, or None when no runtime is reachable.
    """
    explicit = os.environ.get("FSDB2FST_FREADER")
    if explicit and os.path.isdir(os.path.join(explicit, "linux64")):
        return explicit
    for var in ("VERDI_HOME", "NOVAS_HOME"):
        home = os.environ.get(var)
        if not home:
            continue
        cand = os.path.join(home, "share", "FsdbReader")
        if os.path.isdir(os.path.join(cand, "linux64")):
            return cand
    repo_runtime = os.path.join(_REPO_ROOT, "third_party", "verdi_runtime", "linux64")
    if os.path.exists(os.path.join(repo_runtime, "libnffr.so")):
        return repo_runtime
    return None


def _autobuild_cache_key(reader_dir: str) -> str:
    """Cache key over the FsdbReader location, converter sources and build flag.

    Changing Verdi version, editing fsdb2fst.cpp / fstapi.c, or changing the
    build script (e.g. enabling the parallel writer) yields a new key, so a
    stale binary is never reused.
    """
    parts = [reader_dir]
    for name in ("fsdb2fst.cpp", "fst/fstapi.c"):
        parts.append(f"{name}:{file_version(os.path.join(_FSDB2FST_SRC_DIR, name)) or 'missing'}")
    parts.append(f"build:{file_version(_FSDB2FST_BUILD_SH) or 'missing'}")
    return cache_key(*parts)


def _autobuild_fsdb2fst() -> Optional[str]:
    """Build fsdb2fst once into the user cache; return the binary or None.

    Silent by design: this runs on the first FSDB conversion so the user only
    has to set VERDI_HOME. Any failure returns None and the caller raises the
    usual actionable error, which now also reports why the build was skipped.
    """
    if not _AUTOBUILD_ENABLED:
        return None
    if not (os.path.isfile(_FSDB2FST_BUILD_SH)
            and os.path.isfile(os.path.join(_FSDB2FST_SRC_DIR, "fsdb2fst.cpp"))):
        return None  # pip install without sources: nothing to build from
    reader_dir = resolve_fsdb_reader()
    if not reader_dir:
        return None  # no FsdbReader runtime: cannot build, and cannot convert
    if not shutil.which("g++"):
        return None

    cache_dir = _fsdb2fst_cache_dir(_autobuild_cache_key(reader_dir))
    cached_bin = os.path.join(cache_dir, "fsdb2fst")
    if os.path.isfile(cached_bin) and os.access(cached_bin, os.X_OK):
        return cached_bin

    env = dict(os.environ)
    env.setdefault("FSDB2FST_FREADER", reader_dir)
    # Tell the build script where the sources actually are. In a pip install
    # they live under share/wave-mcp/fsdb2fst/ (no third_party/ level), but
    # the script's own dirname-based fallback expects a checkout layout. This
    # was the root cause of the 0.2.6 "auto-build unusable" defect.
    env["SRC_DIR"] = _FSDB2FST_SRC_DIR
    # Build straight into the per-user cache. Building into the checkout and
    # copying afterwards would (a) dirty the git tree, (b) make every later
    # resolve short-circuit on the repo-local level so the cache is never
    # consulted, and (c) fail outright on a read-only or shared checkout,
    # which is the case this cache exists for.
    env["FSDB2FST_OUT"] = cached_bin
    try:
        os.makedirs(cache_dir, exist_ok=True)
        proc = subprocess.run(
            ["bash", _FSDB2FST_BUILD_SH],
            env=env, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0 or not os.path.isfile(cached_bin):
            _record_autobuild_failure(cache_dir, proc.stderr or proc.stdout)
            # A failed build can leave a partial file behind; drop it so the
            # next run retries instead of trusting a broken binary.
            try:
                os.remove(cached_bin)
            except OSError:
                pass
            return None
        os.chmod(cached_bin, 0o755)
        return cached_bin
    except (OSError, subprocess.SubprocessError) as exc:
        _record_autobuild_failure(cache_dir, str(exc))
        return None


def _record_autobuild_failure(cache_dir: str, detail: str) -> None:
    """Persist the last build failure so the error message can cite it."""
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(os.path.join(cache_dir, "build-failed.log"), "w") as fh:
            fh.write((detail or "").strip()[-4000:])
    except OSError:
        pass


def _last_autobuild_failure() -> Optional[str]:
    """Return the tail of the most recent auto-build failure, if any."""
    root = _fsdb2fst_cache_root()
    newest: Optional[tuple] = None
    try:
        for entry in os.listdir(root):
            log = os.path.join(root, entry, "build-failed.log")
            if os.path.isfile(log):
                mtime = os.path.getmtime(log)
                if newest is None or mtime > newest[0]:
                    newest = (mtime, log)
    except OSError:
        return None
    if not newest:
        return None
    try:
        with open(newest[1]) as fh:
            tail = fh.read().strip().splitlines()
        return "\n".join(tail[-6:]) if tail else None
    except OSError:
        return None


def resolve_fsdb2fst() -> Optional[str]:
    """Locate the fsdb2fst binary, building it on demand when possible.

    Order: ``$FSDB2FST_BIN`` -> repo-local build output -> user cache -> PATH
    -> on-demand build (needs a reachable FsdbReader plus g++).

    Returns the resolved path, or None when nothing usable was found so callers
    can raise an actionable error instead of crashing.
    """
    if FSDB2FST_BIN_ENV:
        cand = shutil.which(FSDB2FST_BIN_ENV) or FSDB2FST_BIN_ENV
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return os.path.abspath(cand)
        return None  # explicitly pointed somewhere broken: do not silently fall back
    if os.path.isfile(_REPO_FSDB2FST) and os.access(_REPO_FSDB2FST, os.X_OK):
        return _REPO_FSDB2FST
    reader_dir = resolve_fsdb_reader()
    if reader_dir:
        cached_bin = os.path.join(
            _fsdb2fst_cache_dir(_autobuild_cache_key(reader_dir)), "fsdb2fst")
        if os.path.isfile(cached_bin) and os.access(cached_bin, os.X_OK):
            return cached_bin
    found = shutil.which("fsdb2fst")
    if found:
        return os.path.abspath(found)
    return _autobuild_fsdb2fst()


def _fsdb2fst_missing_error() -> ConversionError:
    where = f"$FSDB2FST_BIN={FSDB2FST_BIN_ENV!r}" if FSDB2FST_BIN_ENV \
        else ("$FSDB2FST_BIN (unset), repo-local third_party/fsdb2fst/fsdb2fst, "
              f"user cache {_fsdb2fst_cache_root()}, PATH")
    # Explain why the on-demand build did not save the day, so the user gets
    # one concrete next step instead of a menu.
    if not _AUTOBUILD_ENABLED:
        why = "auto-build disabled by WAVE_MCP_FSDB2FST_AUTOBUILD=0"
    elif not os.path.isfile(_FSDB2FST_BUILD_SH):
        why = ("auto-build unavailable: converter sources are not shipped in the "
               "PyPI package, use a git checkout or set $FSDB2FST_BIN")
    elif not resolve_fsdb_reader():
        why = ("auto-build skipped: no Verdi FsdbReader runtime found. Set "
               "VERDI_HOME (must contain share/FsdbReader/linux64) in your MCP "
               "config, or FSDB2FST_FREADER to a copied share/FsdbReader dir")
    elif not shutil.which("g++"):
        why = ("auto-build skipped: g++ not found in PATH. Install g++, or build "
               "the converter on another machine and point $FSDB2FST_BIN at it")
    else:
        detail = _last_autobuild_failure()
        why = ("auto-build attempted but failed"
               + (f":\n    {detail}" if detail else ", see build-failed.log in the cache dir"))
    # Only offer the build script when it actually exists: telling a pip user to
    # run a file the package never shipped is what made this error misleading.
    have_sources = os.path.isfile(_FSDB2FST_BUILD_SH) and os.path.isfile(
        os.path.join(_FSDB2FST_SRC_DIR, "fsdb2fst.cpp"))
    options = [
        "set VERDI_HOME in your MCP config and retry; the converter is then "
        "built once automatically (needs g++)",
    ]
    if have_sources:
        options.append(f"or build it explicitly: bash {_FSDB2FST_BUILD_SH}")
    else:
        options.append("or install a build that ships the converter sources "
                       "(pip install 'wave-mcp>=0.2.6'), or use a git checkout")
    options.append("or set $FSDB2FST_BIN to an existing fsdb2fst binary")
    options.append("or convert manually and pass the .fst instead:\n"
                   "      fsdb2fst dump.fsdb dump.fst")
    return ConversionError(
        f"'fsdb2fst' not found — needed to convert FSDB -> FST.\n"
        f"Searched: {where}\n"
        f"Why not built automatically: {why}\n"
        f"Options:\n"
        + "".join(f"  * {o}\n" for o in options)
        + f"See docs/FSDB_GUIDE.md for the full setup (the FsdbReader runtime "
        f"checks out no license).")



@dataclass
class FsdbConversionResult:
    fsdb_path: str
    fst_path: str
    elapsed_sec: float
    binary: str = ""
    fsdb_bytes: Optional[int] = None
    fst_bytes: Optional[int] = None
    scopes: List[str] = field(default_factory=list)
    signals_file: Optional[str] = None
    command: List[str] = field(default_factory=list)
    cached: bool = False
    stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "fsdb_path": self.fsdb_path,
            "fst_path": self.fst_path,
            "binary": self.binary,
            "elapsed_sec": round(self.elapsed_sec, 3),
            "fsdb_bytes": self.fsdb_bytes,
            "fst_bytes": self.fst_bytes,
            "scopes": self.scopes,
            "signals_file": self.signals_file,
            "cached": self.cached,
            "command": " ".join(self.command),
            **({"stats": self.stats} if self.stats else {}),
        }


# fsdb2fst prints a one-line census we surface as-is, e.g.
#   [fsdb2fst] signals: 500 (1 real, 0 strength-skipped, 0 unsupported-type)
def _parse_fsdb_stats(out: str) -> dict:
    import re
    stats: dict = {}
    m = re.search(r"signals:\s*(\d+)\s*\((\d+) real, (\d+) strength-skipped, "
                  r"(\d+) unsupported-type\)", out)
    if m:
        stats.update(signals=int(m.group(1)), real=int(m.group(2)),
                     strength_skipped=int(m.group(3)),
                     unsupported_type=int(m.group(4)))
    m = re.search(r"census:\s*(\d+) total-vars, (\d+) unique-paths, "
                  r"(\d+) convertible", out)
    if m:
        stats.update(total_vars=int(m.group(1)), unique_paths=int(m.group(2)),
                     convertible=int(m.group(3)))
    m = re.search(r"selected:\s*(\d+) signals", out)
    if m:
        stats["selected"] = int(m.group(1))
    m = re.search(r"(\d+) vars, (\d+) transitions", out)
    if m:
        stats.update(vars_written=int(m.group(1)),
                     transitions=int(m.group(2)))
    m = re.search(r"scale:\s*(\S+)\s*\(", out)
    if m:
        stats["scale"] = m.group(1)
    return stats


def _fsdb_failure(binary: str, returncode: int, out: str, *,
                  info_only: bool = False) -> ConversionError:
    """Distinguish converter/reader crashes from dynamic-loader failures."""
    detail = out.strip()
    operation = "fsdb2fst --info" if info_only else "fsdb2fst"
    if returncode == 3 and "FsdbReader crashed" in detail:
        # fsdb2fst caught the fatal signal itself and already printed which
        # phase crashed and how many signals were selected; passing its own
        # diagnosis through beats wrapping it in a second, vaguer one.
        return ConversionError(f"{operation}: {detail}")
    if returncode == -signal.SIGSEGV:
        startup_crash = not detail
        return ConversionError(
            f"{operation} crashed with SIGSEGV (rc={returncode}); the converter "
            f"or FsdbReader runtime crashed, not necessarily a missing library.\n"
            f"{detail}\n"
            + ("The process produced no output at all, which points at a crash "
               "during dynamic loading, before main. That is usually a libc "
               "mismatch: compare a clean LD_LIBRARY_PATH against this "
               "environment (an injected glibc is a common cause) and check "
               "whether gdb reports 'No stack'.\n"
               if startup_crash else
               "Two causes are known: a FSDB carrying no value change data at "
               "all (rebuild fsdb2fst from current source to get a precise "
               "error instead of this crash), and a libc mismatch at load time "
               "when LD_LIBRARY_PATH injects a different glibc. Size alone does "
               "not cause this; -l/-L and scopes cannot work around it.\n")
            + f"Run fsdb2fst --info in the same environment and share sanitized "
            f"counts and the FsdbReader version, not the confidential FSDB. "
            f"See docs/FSDB_GUIDE.md.")
    if ("error while loading shared libraries:" in detail
            or "cannot open shared object file" in detail):
        return ConversionError(
            f"fsdb2fst could not load the Verdi FsdbReader runtime:\n"
            f"  {detail}\n"
            f"Copy libnffr.so and libnsys.so next to {binary} (its RPATH "
            f"searches $ORIGIN), or rebuild with deploy/build_fsdb2fst.sh "
            f"on a machine with $VERDI_HOME set. See docs/FSDB_GUIDE.md.")
    return ConversionError(f"{operation} failed (rc={returncode}): {detail}")


def convert_fsdb(fsdb_path: str, fst_path: Optional[str] = None,
                 scopes: Optional[List[str]] = None,
                 signals_file: Optional[str] = None,
                 pack: str = "lz4",
                 timeout: Optional[float] = None) -> FsdbConversionResult:
    """Convert an FSDB waveform to FST via the bundled fsdb2fst (single pass).

    ``scopes`` maps to ``-l`` (OR over substrings) and ``signals_file`` to
    ``-L``; both narrow the selected set, which is what the converter's
    in-core memory guard bounds.

    fsdb2fst writes the hierarchy as a ``<fst>.hier`` sidecar, and pylibfst
    cannot open the FST without it, so the sidecar is validated here rather
    than surfacing later as a confusing "FST not found".

    ``timeout=None`` (default) auto-estimates a size-based cap and logs it for
    large inputs; the run is heartbeat-monitored so a wedged converter fails
    fast with a precise message instead of hanging until the cap.
    """
    binary = resolve_fsdb2fst()
    if binary is None:
        raise _fsdb2fst_missing_error()

    fsdb_path = os.path.abspath(fsdb_path)
    if not os.path.exists(fsdb_path):
        raise ConversionError(f"FSDB not found: {fsdb_path}")
    if fst_path is None:
        fst_path = _default_out_path(fsdb_path)
    fst_path = os.path.abspath(fst_path)
    os.makedirs(os.path.dirname(fst_path) or ".", exist_ok=True)

    if pack not in _PACK_FLAG:
        raise ConversionError(f"unknown pack {pack!r}; expected one of {list(_PACK_FLAG)}")
    cmd = [binary, "-v"]
    if pack != "lz4":
        cmd += ["-p", pack]
    if scopes:
        cmd += ["-l", ",".join(scopes)]
    if signals_file:
        signals_file = os.path.abspath(signals_file)
        if not os.path.exists(signals_file):
            raise ConversionError(f"signals file not found: {signals_file}")
        cmd += ["-L", signals_file]
    cmd += [fsdb_path, fst_path]

    fsdb_bytes = os.path.getsize(fsdb_path)
    if timeout is None:
        timeout = _estimate_timeout(fsdb_bytes, "fsdb")
        _log_estimate(fsdb_path, fsdb_bytes, timeout, "fsdb")
    t0 = time.time()
    try:
        rc, out = _run_with_heartbeat(cmd, fst_path, timeout, "fsdb")
    except OSError as exc:
        raise ConversionError(
            f"cannot execute fsdb2fst at {binary}: {exc}\n"
            f"If it was built on another machine, copy libnffr.so and "
            f"libnsys.so next to the binary (the RPATH looks in $ORIGIN).") from exc
    elapsed = time.time() - t0

    if rc != 0 or not os.path.exists(fst_path):
        raise _fsdb_failure(binary, rc, out)

    hier = fst_path + ".hier"
    if not os.path.exists(hier):
        raise ConversionError(
            f"fsdb2fst produced {fst_path} but not its required "
            f"{os.path.basename(hier)} sidecar; the FST cannot be opened "
            f"without it. Please report this file.")

    return FsdbConversionResult(
        fsdb_path=fsdb_path, fst_path=fst_path, elapsed_sec=elapsed,
        binary=binary, fsdb_bytes=fsdb_bytes,
        fst_bytes=os.path.getsize(fst_path), scopes=list(scopes or []),
        signals_file=signals_file, command=cmd,
        stats=_parse_fsdb_stats(out))


def fsdb2fst_missing_error() -> ConversionError:
    """Public alias so callers can render the same actionable guidance."""
    return _fsdb2fst_missing_error()


def fsdb_info(fsdb_path: str, timeout: Optional[float] = 600) -> dict:
    """Report an FSDB's time scale and signal census without converting it.

    Useful before committing to a long conversion on a huge file: it reads only
    the hierarchy, so it returns in seconds even on multi-GB designs.
    """
    binary = resolve_fsdb2fst()
    if binary is None:
        raise _fsdb2fst_missing_error()
    fsdb_path = os.path.abspath(fsdb_path)
    if not os.path.exists(fsdb_path):
        raise ConversionError(f"FSDB not found: {fsdb_path}")
    try:
        proc = subprocess.run([binary, "--info", fsdb_path],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConversionError(f"fsdb2fst --info failed: {exc}") from exc
    out = (proc.stderr or "") + (proc.stdout or "")
    if proc.returncode != 0:
        raise _fsdb_failure(binary, proc.returncode, out, info_only=True)
    return {"binary": binary, "fsdb_path": fsdb_path,
            "stats": _parse_fsdb_stats(out), "report": out.strip()}


# =============================================================================
# Converted FST placement and reuse (VCD and FSDB)
# =============================================================================
#
# Users keep a converted waveform next to its source, whether they ran the
# converter by hand or a tool did it for them, so that is where wave-mcp looks
# first and where it writes by default: ``<dir>/<name>.vcd`` pairs with
# ``<dir>/<name>.fst`` (FSDB adds ``<name>.fst.hier``). Three cases depart
# from "reuse or write beside silently", and each returns a ``notice`` that
# says what happened, why, and where the FST is:
#
#   * the source directory is not writable (or writing there fails): the
#     FST goes to the derived-cache layer and the source dir is untouched;
#   * the conversion is partial (``scopes`` / ``signals_file``): a slice must
#     not take the name every later caller reads as the full waveform, so it
#     goes to the cache as well;
#   * an FST beside the source is stale (older than the source, changed since
#     we wrote it, missing its ``.hier``, or unreadable): it is overwritten,
#     whoever wrote it, the same way a re-simulation overwrites the dump.
#
# Only the ``.fst`` (and ``.hier``) land beside the source. The conversion
# record and the lock live in the cache, keyed by the target path, so the
# source directory never gains bookkeeping files. A beside FST is reused when
# its record matches the current source version, or, lacking a record of its
# own (converted by hand), when it is not older than the source and opens.

#: Conversion record stored with a cached FST (cache placement).
_CACHE_RECORD = "conversion.json"
#: Record for an FST placed beside its source, kept in the cache.
_BESIDE_RECORD = "beside.json"
#: Bump when the converted output changes for the same input and options.
_CONVERT_TOOL_VERSION = 2


def _conversion_record(src: str, opts: dict) -> dict:
    """What a cached FST was produced from: source version plus options."""
    return {"source": os.path.abspath(src), "source_version": file_version(src),
            "options": opts, "tool_version": _CONVERT_TOOL_VERSION}


def _lock_wait(source: str, kind: str, timeout: Optional[float]) -> float:
    """How long to wait for another process converting the same waveform.

    That holder is itself bounded by the conversion timeout, so waiting one
    conversion's worth plus a margin covers a healthy holder; past it the
    holder is stuck (or the lock is, on a network filesystem) and the call
    fails with the holder named instead of hanging.
    """
    if timeout:
        return float(timeout) + 120.0
    try:
        size = os.path.getsize(source)
    except OSError:
        size = 0
    return _estimate_timeout(size, kind) + 120.0


def _artifact_ok(fst_path: str, need_hier: bool) -> bool:
    if not os.path.exists(fst_path):
        return False
    if need_hier and not os.path.exists(fst_path + ".hier"):
        return False  # sidecar lost in transit: the FST is unusable
    return True


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path) as fh:
            got = json.load(fh)
    except (OSError, ValueError):
        return None
    return got if isinstance(got, dict) else None


def _cache_hit(fst_path: str, record_path: str, want: dict,
               need_hier: bool) -> Optional[dict]:
    """The stored conversion detail when the cached FST matches ``want``."""
    if not (_artifact_ok(fst_path, need_hier) and os.path.exists(record_path)):
        return None
    have = _read_json(record_path)
    if have is None:
        return None  # unreadable/corrupt record: reconvert
    # compare only the identity fields: the record also carries a "detail"
    # blob, so comparing whole dicts would never match.
    if all(have.get(k) == v for k, v in want.items()):
        return have.get("detail", {})
    return None


def beside_path(source: str) -> str:
    """The FST that belongs next to ``source``: same directory and stem."""
    return os.path.splitext(os.path.abspath(source))[0] + ".fst"


def _fst_opens(fst_path: str) -> bool:
    """Whether the FST reader accepts the file (header and hierarchy reachable)."""
    try:
        from pylibfst import ffi, lib
    except ImportError:  # pragma: no cover - hard dependency
        return True
    ctx = lib.fstReaderOpen(fst_path.encode())
    if ctx == ffi.NULL:
        return False
    lib.fstReaderClose(ctx)
    return True


def _dir_writable(directory: str) -> Optional[str]:
    """``None`` when files can be created in ``directory``, else the reason."""
    if not os.path.isdir(directory):
        return "directory does not exist"
    if not os.access(directory, os.W_OK | os.X_OK):
        return "no write permission"
    try:
        fd, probe = tempfile.mkstemp(prefix=".wave-mcp-probe-", dir=directory)
    except OSError as exc:
        return exc.strerror or str(exc)
    os.close(fd)
    with contextlib.suppress(OSError):
        os.remove(probe)
    return None


def _beside_state(source: str, target: str, kind: str,
                  record_path: str) -> tuple:
    """Classify the FST beside ``source``: ``(state, detail)``.

    ``state`` is ``"absent"``, ``"hit"`` (reuse), ``"hand"`` (reuse, no record
    of ours: converted by hand or by an older wave-mcp) or ``"stale"`` with a
    human-readable reason in ``detail``.
    """
    need_hier = (kind == "fsdb")
    if not os.path.exists(target):
        return "absent", {}
    if need_hier and not os.path.exists(target + ".hier"):
        return "stale", {"reason": f"{os.path.basename(target)}.hier is missing"}
    rec = _read_json(record_path)
    if rec and rec.get("target_version") == file_version(target):
        # the file is still the one we wrote: our record decides
        if (rec.get("source") == source
                and rec.get("source_version") == file_version(source)
                and rec.get("tool_version") == _CONVERT_TOOL_VERSION):
            if _fst_opens(target):
                return "hit", rec.get("detail", {})
            return "stale", {"reason": "the FST cannot be opened"}
        return "stale", {"reason": "the source waveform changed since it "
                                   "was converted"}
    try:
        newer = os.stat(target).st_mtime_ns >= os.stat(source).st_mtime_ns
    except OSError:
        newer = False
    if not newer:
        return "stale", {"reason": "it is older than the source waveform"}
    if not _fst_opens(target):
        return "stale", {"reason": "the FST cannot be opened"}
    return "hand", {}


def _run_converter(source: str, out: str, kind: str, *, pack: str,
                   scopes: Optional[List[str]], signals_file: Optional[str],
                   timeout: Optional[float]) -> dict:
    if kind == "fsdb":
        return convert_fsdb(source, out, scopes=scopes,
                            signals_file=signals_file, pack=pack,
                            timeout=timeout).to_dict()
    return convert(source, out, pack=pack, timeout=timeout).to_dict()


def _tmp_prefix(target: str) -> str:
    return os.path.join(os.path.dirname(target),
                        f".{os.path.basename(target)}.wave-mcp-")


def _sweep_dead_tmp(target: str) -> None:
    """Remove hidden temporaries a killed conversion left beside ``target``.

    Only this host's, and only when the pid in the name is gone: a live
    process (or one on another host sharing the directory) may still be
    writing its own. Called with the target's lock held.
    """
    prefix = _tmp_prefix(target)
    base = os.path.basename(prefix)
    mine = f"{socket.gethostname()}-"
    try:
        names = os.listdir(os.path.dirname(target))
    except OSError:
        return
    for name in names:
        if not name.startswith(base):
            continue
        rest = name[len(base):]
        if not rest.startswith(mine):
            continue
        pid = rest[len(mine):].split(".", 1)[0]
        if not pid.isdigit() or _pid_alive(int(pid)):
            continue
        with contextlib.suppress(OSError):
            os.remove(os.path.join(os.path.dirname(target), name))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _convert_beside(source: str, target: str, kind: str, *, pack: str,
                    timeout: Optional[float]) -> dict:
    """Convert under a hidden temporary name in the target directory, then
    rename into place so no reader ever sees a half-written FST."""
    _sweep_dead_tmp(target)
    tmp = (f"{_tmp_prefix(target)}{socket.gethostname()}-{os.getpid()}"
           f".tmp.fst")
    leftovers = (tmp, tmp + ".hier")
    try:
        res = _run_converter(source, tmp, kind, pack=pack, scopes=None,
                             signals_file=None, timeout=timeout)
        if kind == "fsdb":
            os.replace(tmp + ".hier", target + ".hier")
        os.replace(tmp, target)
    finally:
        for p in leftovers:
            with contextlib.suppress(OSError):
                os.remove(p)
    res["fst_path"] = target
    try:
        res["fst_bytes"] = os.path.getsize(target)
    except OSError:
        pass
    return res


def _opts_for(kind: str, pack: str, scopes: Optional[List[str]],
              signals_file: Optional[str]) -> dict:
    opts = {"kind": kind, "pack": pack}
    if kind == "fsdb":
        opts.update(scopes=sorted(scopes or []),
                    signals_file=os.path.abspath(signals_file) if signals_file else None)
    return opts


def _in_cache(source: str, kind: str, opts: dict, *,
              scopes: Optional[List[str]], signals_file: Optional[str],
              timeout: Optional[float], force: bool) -> dict:
    """Cache placement: ``fst/<digest of source path + options>/<name>.fst``."""
    pol = storage.policy()
    cache_dir = pol.cache_dir("fst", source, json.dumps(opts, sort_keys=True))
    base = os.path.splitext(os.path.basename(source))[0]
    fst_path = os.path.join(cache_dir, base + ".fst")
    record_path = os.path.join(cache_dir, _CACHE_RECORD)
    need_hier = (kind == "fsdb")
    want = _conversion_record(source, opts)

    if not force:
        detail = _cache_hit(fst_path, record_path, want, need_hier)
        if detail is not None:
            return {"fst_path": fst_path, "cached": True,
                    "cache_dir": cache_dir, "detail": detail}
    with pol.locked(cache_dir, what=f"another conversion of {source}",
                    wait=_lock_wait(source, kind, timeout)):
        if not force:
            # Somebody may have finished this exact conversion while we waited.
            detail = _cache_hit(fst_path, record_path, want, need_hier)
            if detail is not None:
                return {"fst_path": fst_path, "cached": True,
                        "cache_dir": cache_dir, "detail": detail}
        # A stale or corrupt artifact is replaced, not reported.
        for stale in (fst_path, fst_path + ".hier", record_path):
            StoragePolicy.discard(stale)
        res = _run_converter(source, fst_path, kind, pack=opts["pack"],
                             scopes=scopes, signals_file=signals_file,
                             timeout=timeout)
        record = dict(want)
        record["detail"] = res
        try:
            StoragePolicy.atomic_write_bytes(
                record_path, json.dumps(record, indent=2).encode())
        except OSError:
            pass  # the record is an optimisation, never fail the conversion over it
    return {"fst_path": fst_path, "cached": False,
            "cache_dir": cache_dir, "detail": res}


def _slice_desc(scopes: Optional[List[str]], signals_file: Optional[str]) -> str:
    parts = []
    if scopes:
        parts.append(f"scopes={list(scopes)}")
    if signals_file:
        parts.append(f"signals_file={os.path.abspath(signals_file)}")
    return ", ".join(parts)


def cached_fst(source: str, *, kind: str,
               scopes: Optional[List[str]] = None,
               signals_file: Optional[str] = None,
               pack: Optional[str] = None,
               timeout: Optional[float] = None,
               force: bool = False) -> dict:
    """See :func:`_cached_fst`; a lock that never frees is a ConversionError."""
    try:
        return _cached_fst(source, kind=kind, scopes=scopes,
                           signals_file=signals_file, pack=pack,
                           timeout=timeout, force=force)
    except storage.LockBusy as exc:
        raise ConversionError(str(exc)) from exc


def _cached_fst(source: str, *, kind: str,
                scopes: Optional[List[str]] = None,
                signals_file: Optional[str] = None,
                pack: Optional[str] = None,
                timeout: Optional[float] = None,
                force: bool = False) -> dict:
    """Give ``source`` an FST, reusing a valid one and converting otherwise.

    ``kind`` is ``"fsdb"`` or ``"vcd"``. A full conversion lives beside the
    source as ``<name>.fst`` (see the section comment above); partial
    conversions and unwritable source directories fall back to the cache.
    ``force=True`` skips reuse and always converts (the explicit convert
    tools), still into the same place so later sessions pick it up.

    Returns ``fst_path``, ``cached`` (reused without converting),
    ``placement`` (``"beside"`` / ``"cache"``), ``notice`` (why the FST is
    where it is, or what was replaced; ``None`` when nothing needs saying),
    ``cache_dir`` for cache placements and the conversion ``detail``.
    """
    source = os.path.abspath(source)
    if not os.path.exists(source):
        raise ConversionError(f"{kind.upper()} not found: {source}")
    pack = pack or default_pack(kind)
    opts = _opts_for(kind, pack, scopes, signals_file)
    target = beside_path(source)
    need_hier = (kind == "fsdb")

    # -- partial conversion: never under the full-waveform name ------------
    if scopes or signals_file:
        got = _in_cache(source, kind, opts, scopes=scopes,
                        signals_file=signals_file, timeout=timeout, force=force)
        got["placement"] = "cache"
        got["notice"] = (
            f"Partial conversion ({_slice_desc(scopes, signals_file)}) was "
            f"placed in the wave-mcp cache at {got['fst_path']}, not at "
            f"{target}: that name is reserved for the full waveform, which "
            f"later sessions and viewers pick up automatically. Convert "
            f"without scopes / signals_file to get the full waveform at "
            f"{target}, or pass an explicit out_path to keep this slice "
            f"elsewhere.")
        return got

    pol = storage.policy()
    rec_dir = pol.cache_dir("fst", "beside", target)
    record_path = os.path.join(rec_dir, _BESIDE_RECORD)

    if not force:
        state, info = _beside_state(source, target, kind, record_path)
        if state in ("hit", "hand"):
            return {"fst_path": target, "cached": True, "placement": "beside",
                    "notice": None, "detail": info,
                    "reused_by_hand": state == "hand"}

    blocked = _dir_writable(os.path.dirname(source))
    if blocked is None:
        with pol.locked(rec_dir, what=f"another conversion of {source}",
                        wait=_lock_wait(source, kind, timeout)):
            state, info = _beside_state(source, target, kind, record_path)
            if state in ("hit", "hand") and not force:
                return {"fst_path": target, "cached": True,
                        "placement": "beside", "notice": None, "detail": info,
                        "reused_by_hand": state == "hand"}
            notice = None
            if state == "stale":
                notice = (f"Replaced the existing {target}"
                          f"{' (+ .hier)' if need_hier else ''}: "
                          f"{info.get('reason')}. To keep such a file, "
                          f"rename it before converting, or convert to "
                          f"another name with out_path (CLI: --fst).")
            elif state in ("hit", "hand"):
                notice = (f"Replaced the existing {target}"
                          f"{' (+ .hier)' if need_hier else ''}: a conversion "
                          f"was requested explicitly. Pass out_path (CLI: "
                          f"--fst) to write elsewhere instead.")
            try:
                res = _convert_beside(source, target, kind, pack=pack,
                                      timeout=timeout)
            except OSError as exc:
                blocked = (f"writing there failed "
                           f"({exc.strerror or exc})")
            else:
                record = _conversion_record(source, opts)
                record["target"] = target
                record["target_version"] = file_version(target)
                record["detail"] = res
                try:
                    StoragePolicy.atomic_write_bytes(
                        record_path, json.dumps(record, indent=2).encode())
                except OSError:
                    pass
                return {"fst_path": target, "cached": False,
                        "placement": "beside", "notice": notice,
                        "detail": res}

    # -- source directory unwritable: cache fallback -----------------------
    got = _in_cache(source, kind, opts, scopes=None, signals_file=None,
                    timeout=timeout, force=force)
    got["placement"] = "cache"
    verb = "Reused the FST" if got["cached"] else "Converted FST was placed"
    got["notice"] = (
        f"{verb} in the wave-mcp cache at {got['fst_path']} because "
        f"{os.path.dirname(source)} is not writable ({blocked}); the usual "
        f"place, {target}, was left untouched. Make the directory writable "
        f"to keep the FST beside the source, or set WAVE_MCP_CACHE_ROOT to "
        f"move the cache.")
    return got


def default_fst(source: str, *, kind: str,
                scopes: Optional[List[str]] = None,
                signals_file: Optional[str] = None,
                pack: Optional[str] = None,
                timeout: Optional[float] = None) -> dict:
    """Default output of the explicit convert tools (no ``out_path`` given).

    Always converts, into the same place ``cached_fst`` reads from, so the
    result is reused by every later session and viewer on that waveform.
    """
    return cached_fst(source, kind=kind, scopes=scopes,
                      signals_file=signals_file, pack=pack, timeout=timeout,
                      force=True)

def default_pack(kind: str) -> str:
    """Default compressor per converter: fastlz for vcd2fst, lz4 for fsdb2fst.

    They differ because the converters differ: vcd2fst's fastlz path is the
    fast one there, while fsdb2fst was tuned around lz4. ``pack=None`` on the
    callers below means "that converter's default".
    """
    return "lz4" if kind == "fsdb" else "fastlz"


def resolve_waveform(path: str, *,
                     scopes: Optional[List[str]] = None,
                     signals_file: Optional[str] = None,
                     pack: Optional[str] = None,
                     timeout: Optional[float] = None) -> dict:
    """Resolve any supported waveform path to an openable FST.

    Single entry point shared by the analysis path (``prepare_session``) and
    the viewer path (``open_wave_view``) so a waveform converted by one is
    reused by the other instead of being converted again:

        .fst  -> returned as-is
        .vcd  -> cached_fst(kind="vcd")
        .fsdb -> cached_fst(kind="fsdb")
        other -> UnsupportedWaveformError

    Defaults match ``prepare_session`` (no slicing, the converter's default pack);
    callers must not vary them, since the conversion options are part of the
    cache key and a mismatch silently forces a second conversion.

    Returns ``{fst_path, kind, converted, cached, source, placement,
    notice}``; ``notice`` explains a cache placement or an overwritten FST.
    """
    source = os.path.abspath(path)
    kind = waveform_kind(source)
    if not os.path.exists(source):
        raise FileNotFoundError(f"{kind.upper()} not found: {source}")
    if kind == "fst":
        return {"fst_path": source, "kind": kind, "converted": False,
                "cached": False, "source": source}
    got = cached_fst(
        source, kind=kind, scopes=scopes, signals_file=signals_file,
        pack=pack, timeout=timeout)
    return {"fst_path": got["fst_path"], "kind": kind, "converted": True,
            "cached": bool(got.get("cached")), "source": source,
            "placement": got.get("placement"), "notice": got.get("notice"),
            "cache_dir": got.get("cache_dir"), "detail": got.get("detail", {})}
