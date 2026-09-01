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
through ``$FSDB2FST_BIN`` -> repo-local ``third_party/fsdb2fst/fsdb2fst`` -> PATH.

Both conversions share one **artifact cache** (``cached_fst``): a converted FST
is reused as long as the source waveform's (path, mtime, size) and the slicing
options are unchanged. FSDB files are routinely GB-scale and take minutes to
convert, so without this every prepare_session on the same waveform would pay
the full cost again.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import List, Optional

VCD2FST_BIN = os.environ.get("VCD2FST_BIN", "vcd2fst")
FSDB2FST_BIN_ENV = os.environ.get("FSDB2FST_BIN")

# repo-local build output of deploy/build_fsdb2fst.sh, used when $FSDB2FST_BIN
# is unset. Path is relative to this file: wave_mcp/ -> <repo>/third_party/...
_REPO_FSDB2FST = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    os.pardir, "third_party", "fsdb2fst", "fsdb2fst"))

# mode -> packing flag
_MODE_FLAG = {
    "speed": "-F",      # fastlz, fastest
    "balanced": "-4",   # lz4, default
    "size": "-Z",       # zlib, smallest
}


class ConversionError(RuntimeError):
    pass


@dataclass
class ConversionResult:
    vcd_path: str
    fst_path: str
    mode: str
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
            "mode": self.mode,
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


def _build_cmd(vcd: str, fst: str, mode: str, parallel: bool,
               compress: bool) -> List[str]:
    flag = _MODE_FLAG.get(mode)
    if flag is None:
        raise ConversionError(f"unknown mode {mode!r}; expected one of {list(_MODE_FLAG)}")
    cmd = [VCD2FST_BIN, flag]
    # only add -p if the binary actually supports the parallel path; otherwise
    # it would abort with rc=255 and break every first-time conversion.
    if parallel and _parallel_supported():
        cmd.append("-p")
    if compress:
        cmd.append("-c")
    cmd += ["-v", vcd, "-f", fst]
    return cmd


def convert(vcd_path: str, fst_path: Optional[str] = None, mode: str = "speed",
            parallel: bool = True, compress: bool = False,
            timeout: Optional[float] = None) -> ConversionResult:
    """Convert an existing VCD file to FST. Returns timing + size stats."""
    _check_bin()
    vcd_path = os.path.abspath(vcd_path)
    if not os.path.exists(vcd_path):
        raise ConversionError(f"VCD not found: {vcd_path}")
    if fst_path is None:
        fst_path = os.path.splitext(vcd_path)[0] + ".fst"
    fst_path = os.path.abspath(fst_path)
    os.makedirs(os.path.dirname(fst_path) or ".", exist_ok=True)

    cmd = _build_cmd(vcd_path, fst_path, mode, parallel, compress)
    used_parallel = "-p" in cmd
    vcd_bytes = os.path.getsize(vcd_path)
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    elapsed = time.time() - t0
    out = (proc.stderr or "") + (proc.stdout or "")
    # hard fallback: if -p slipped through (probe false-positive / stale cache)
    # and the binary lacks the parallel path, retry once without -p.
    if used_parallel and (proc.returncode != 0 or not os.path.exists(fst_path)) \
            and _PARALLEL_DISABLED_MARK in out:
        global _PARALLEL_SUPPORTED
        _PARALLEL_SUPPORTED = False  # remember for the rest of the process
        cmd = _build_cmd(vcd_path, fst_path, mode, False, compress)
        used_parallel = False
        t0 = time.time()
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        elapsed = time.time() - t0
    if proc.returncode != 0 or not os.path.exists(fst_path):
        raise ConversionError(
            f"vcd2fst failed (rc={proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")
    return ConversionResult(
        vcd_path=vcd_path, fst_path=fst_path, mode=mode, parallel=used_parallel,
        elapsed_sec=elapsed, vcd_bytes=vcd_bytes,
        fst_bytes=os.path.getsize(fst_path), command=cmd)


def start_streaming(fifo_path: str, fst_path: Optional[str] = None,
                    mode: str = "speed", parallel: bool = True,
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

    cmd = _build_cmd(fifo_path, fst_path, mode, parallel, compress=False)
    logf = open(log_path, "w") if log_path else subprocess.DEVNULL
    # vcd2fst will block opening the FIFO until the writer (xrun) connects.
    proc = subprocess.Popen(cmd, stdout=logf, stderr=logf, start_new_session=True)
    return ConversionResult(
        vcd_path=fifo_path, fst_path=fst_path, mode=mode, parallel=parallel,
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

def resolve_fsdb2fst() -> Optional[str]:
    """Locate the fsdb2fst binary: $FSDB2FST_BIN -> repo-local -> PATH.

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
    found = shutil.which("fsdb2fst")
    return os.path.abspath(found) if found else None


def _fsdb2fst_missing_error() -> ConversionError:
    where = f"$FSDB2FST_BIN={FSDB2FST_BIN_ENV!r}" if FSDB2FST_BIN_ENV \
        else "$FSDB2FST_BIN (unset), repo-local third_party/fsdb2fst/fsdb2fst, PATH"
    return ConversionError(
        f"'fsdb2fst' not found — needed to convert FSDB -> FST.\n"
        f"Searched: {where}\n"
        f"Options:\n"
        f"  * build it once on a machine with Verdi:\n"
        f"      export VERDI_HOME=/path/to/verdi   # must contain share/FsdbReader/linux64\n"
        f"      bash deploy/build_fsdb2fst.sh\n"
        f"  * or set $FSDB2FST_BIN to an existing fsdb2fst binary\n"
        f"  * or convert manually and pass the .fst instead:\n"
        f"      fsdb2fst dump.fsdb dump.fst\n"
        f"See docs/FSDB_GUIDE.md for the full setup (the FsdbReader runtime "
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
    m = re.search(r"(\d+) vars, (\d+) transitions", out)
    if m:
        stats.update(vars_written=int(m.group(1)),
                     transitions=int(m.group(2)))
    m = re.search(r"scale:\s*(\S+)\s*\(", out)
    if m:
        stats["scale"] = m.group(1)
    return stats


def convert_fsdb(fsdb_path: str, fst_path: Optional[str] = None,
                 scopes: Optional[List[str]] = None,
                 signals_file: Optional[str] = None,
                 pack: str = "lz4",
                 timeout: Optional[float] = None) -> FsdbConversionResult:
    """Convert an FSDB waveform to FST via the bundled fsdb2fst (single pass).

    ``scopes`` maps to ``-l`` (OR over substrings) and ``signals_file`` to
    ``-L``; both slice a huge design down to a convertible subset.

    fsdb2fst writes the hierarchy as a ``<fst>.hier`` sidecar, and pylibfst
    cannot open the FST without it, so the sidecar is validated here rather
    than surfacing later as a confusing "FST not found".
    """
    binary = resolve_fsdb2fst()
    if binary is None:
        raise _fsdb2fst_missing_error()

    fsdb_path = os.path.abspath(fsdb_path)
    if not os.path.exists(fsdb_path):
        raise ConversionError(f"FSDB not found: {fsdb_path}")
    if fst_path is None:
        fst_path = os.path.splitext(fsdb_path)[0] + ".fst"
    fst_path = os.path.abspath(fst_path)
    os.makedirs(os.path.dirname(fst_path) or ".", exist_ok=True)

    cmd = [binary, "-v"]
    if pack and pack != "lz4":
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
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
    except OSError as exc:
        raise ConversionError(
            f"cannot execute fsdb2fst at {binary}: {exc}\n"
            f"If it was built on another machine, copy libnffr.so and "
            f"libnsys.so next to the binary (the RPATH looks in $ORIGIN).") from exc
    elapsed = time.time() - t0
    out = (proc.stderr or "") + (proc.stdout or "")

    if proc.returncode != 0 or not os.path.exists(fst_path):
        detail = (proc.stderr or proc.stdout or "").strip()
        # A loader failure means the binary is fine but the Synopsys runtime is
        # not reachable, so name that case explicitly.
        if "libnffr" in detail or "libnsys" in detail \
                or "shared object" in detail or "cannot open shared" in detail:
            raise ConversionError(
                f"fsdb2fst could not load the Verdi FsdbReader runtime:\n"
                f"  {detail}\n"
                f"Copy libnffr.so and libnsys.so next to {binary} (its RPATH "
                f"searches $ORIGIN), or rebuild with deploy/build_fsdb2fst.sh "
                f"on a machine with $VERDI_HOME set. See docs/FSDB_GUIDE.md.")
        # Otherwise fsdb2fst's own diagnostics are already actionable (unknown
        # timescale, signal-count guard with the -l/-L hint, load failures), so
        # pass them through instead of wrapping and losing the hint.
        raise ConversionError(
            f"fsdb2fst failed (rc={proc.returncode}): {detail}")

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
        raise ConversionError(
            f"fsdb2fst --info failed (rc={proc.returncode}): {out.strip()}")
    return {"binary": binary, "fsdb_path": fsdb_path,
            "stats": _parse_fsdb_stats(out), "report": out.strip()}


# =============================================================================
# Shared artifact cache (VCD and FSDB)
# =============================================================================
#
# The cache keys on the source waveform's identity (path, mtime, size) *and* the
# conversion options. Options must be part of the key: the same FSDB sliced with
# -l u_core yields an FST holding only that subtree, so keying on the file alone
# would reuse a partial FST when the slice changes and hand back a session with
# missing signals and no warning.

_CACHE_SUFFIX = ".wave-mcp-cache.json"


def _source_fingerprint(src: str, opts: dict) -> dict:
    st = os.stat(src)
    return {
        "source": os.path.abspath(src),
        "mtime_ns": st.st_mtime_ns,
        "size": st.st_size,
        "options": opts,
        "tool_version": 1,
    }


def _artifact_ok(fst_path: str, need_hier: bool) -> bool:
    if not os.path.exists(fst_path):
        return False
    if need_hier and not os.path.exists(fst_path + ".hier"):
        return False  # sidecar lost in transit: the FST is unusable
    return True


def _dir_writable(d: str) -> bool:
    """Probe writability by actually creating a file.

    ``os.access(W_OK)`` is not enough: it returns True for root even on a
    read-only-by-permission directory, so a shared read-only regression dir
    would look writable and the conversion would fail late instead of falling
    back to the session dir.
    """
    if not os.path.isdir(d):
        return False
    probe = os.path.join(d, ".wave-mcp-write-probe")
    try:
        with open(probe, "w"):
            pass
        os.remove(probe)
        return True
    except OSError:
        return False


def cached_fst(source: str, *, kind: str, fallback_dir: str,
               scopes: Optional[List[str]] = None,
               signals_file: Optional[str] = None,
               mode: str = "speed", pack: str = "lz4",
               timeout: Optional[float] = None) -> dict:
    """Convert ``source`` to FST, reusing a previous artifact when still valid.

    ``kind`` is ``"fsdb"`` or ``"vcd"``. The artifact is written next to the
    source waveform so every session on that waveform shares it; when that
    directory is not writable (read-only scratch, shared regression dirs) it
    falls back to ``fallback_dir`` (normally the session dir).

    Returns a dict with ``fst_path``, ``cached`` (bool) and the underlying
    conversion detail, ready to drop into a pipeline step.
    """
    source = os.path.abspath(source)
    if not os.path.exists(source):
        raise ConversionError(f"{kind.upper()} not found: {source}")

    opts = {"kind": kind}
    if kind == "fsdb":
        opts.update(scopes=sorted(scopes or []),
                    signals_file=os.path.abspath(signals_file) if signals_file else None,
                    pack=pack)
    else:
        opts.update(mode=mode)

    base = os.path.splitext(os.path.basename(source))[0]
    src_dir = os.path.dirname(source)
    out_dir = src_dir if _dir_writable(src_dir) else fallback_dir
    os.makedirs(out_dir, exist_ok=True)
    fst_path = os.path.join(out_dir, base + ".fst")
    cache_path = fst_path + _CACHE_SUFFIX
    need_hier = (kind == "fsdb")

    want = _source_fingerprint(source, opts)
    if _artifact_ok(fst_path, need_hier) and os.path.exists(cache_path):
        try:
            with open(cache_path) as fh:
                have = json.load(fh)
            # compare only the fingerprint fields: the stored record also carries
            # a "detail" blob, so comparing whole dicts would never match.
            if all(have.get(k) == v for k, v in want.items()):
                return {"fst_path": fst_path, "cached": True,
                        "cache_dir": out_dir, "detail": have.get("detail", {})}
        except (OSError, ValueError):
            pass  # unreadable/corrupt cache: just reconvert

    if kind == "fsdb":
        res = convert_fsdb(source, fst_path, scopes=scopes,
                           signals_file=signals_file, pack=pack,
                           timeout=timeout).to_dict()
    else:
        res = convert(source, fst_path, mode=mode, timeout=timeout).to_dict()

    record = dict(want)
    record["detail"] = res
    try:
        with open(cache_path, "w") as fh:
            json.dump(record, fh, indent=2)
    except OSError:
        pass  # cache is an optimisation, never fail the conversion over it
    return {"fst_path": fst_path, "cached": False,
            "cache_dir": out_dir, "detail": res}
