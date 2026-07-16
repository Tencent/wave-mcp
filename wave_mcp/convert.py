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
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import List, Optional

VCD2FST_BIN = os.environ.get("VCD2FST_BIN", "vcd2fst")

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
            f"'{VCD2FST_BIN}' not found. Install GTKWave (provides vcd2fst) or set "
            f"$VCD2FST_BIN.")


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
    except Exception:
        _PARALLEL_SUPPORTED = False
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
