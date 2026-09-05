"""Surver process management for the wave-mcp viewer.

One surver per view (loopback only, random high port, random token).
Lifecycle is tied to the owning process: all surver children are reaped
on exit, matching the zero-ops philosophy of the stdio deployment mode.
"""
from __future__ import annotations

import atexit
import secrets
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Sequence


class SurverError(RuntimeError):
    pass


def _die_with_parent() -> None:
    """Ask the kernel to SIGTERM this child when its parent dies.

    Signal handlers cannot cover SIGKILL or a hard crash of the owning
    process, which would leave surver running forever holding a port. Linux
    PR_SET_PDEATHSIG closes that gap. Best effort: silently skipped on
    non-Linux or if prctl is unavailable.
    """
    try:
        import ctypes

        PR_SET_PDEATHSIG = 1
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(PR_SET_PDEATHSIG, 15, 0, 0, 0)
    except Exception:
        pass


def _free_port(exclude: Optional[Sequence[int]] = None) -> int:
    from . import alloc_port
    return alloc_port(exclude=exclude)


class SurverInstance:
    def __init__(self, binary: str, fst_paths: List[str]) -> None:
        for p in fst_paths:
            if not Path(p).is_file():
                raise SurverError(f"waveform not found: {p}")
        self.fst_paths = [str(Path(p).resolve()) for p in fst_paths]
        self.token = secrets.token_urlsafe(12)
        # surver is a separate binary, so it cannot inherit a reserved socket;
        # it only receives a port number. Probing a port and letting the child
        # bind it later races with every other process on the host, and under a
        # full regression run that surfaced as an intermittent "surver failed
        # to start". We cannot hold the port for the child either: a listening
        # socket makes the child's own bind fail with EADDRINUSE. So the race
        # is handled where it actually materialises: if the child fails to
        # come up, retire that port and retry on a different one.
        proc, port = self._spawn_with_retry(binary)
        self.port = port
        self.proc = proc
        self._wait_ready()

    def _spawn_with_retry(self, binary: str, attempts: int = 4):
        """Start surver, retrying on a fresh port when a spawn fails.

        Only a failure to *bind* is worth retrying; other causes (missing
        file, non-executable binary) are deterministic and would just be
        retried to the same outcome, so those surface on the first try.
        """
        retired = []
        last_exc = None
        for i in range(attempts):
            port = _free_port(exclude=retired)
            try:
                proc = subprocess.Popen(
                    [binary, "--port", str(port), "--bind-address", "127.0.0.1",
                     "--token", self.token, *self.fst_paths],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    preexec_fn=_die_with_parent,
                )
            except OSError as exc:
                last_exc = exc
                retired.append(port)
                continue
            # Ask surver itself, via its token-authenticated status endpoint.
            # Merely being able to connect is not proof that *our* surver is
            # up: the port may have been taken by an unrelated listener that
            # accepts connections while our own child has already died.
            deadline = time.time() + 2.0
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                if self._surver_ready(port):
                    return proc, port
                time.sleep(0.05)
            if proc.poll() is not None:
                last_exc = SurverError(
                    f"surver exited early (code {proc.returncode}) "
                    f"on port {port}")
                retired.append(port)
                continue
            # still running but not answering yet: let _wait_ready give it
            # the full timeout rather than restarting on a fresh port
            return proc, port
        raise last_exc or SurverError("could not start surver on any port")

    def _surver_ready(self, port: int) -> bool:
        """True only when the token endpoint of *our* surver answers."""
        url = f"http://127.0.0.1:{port}/{self.token}/get_status"
        try:
            with urllib.request.urlopen(url, timeout=0.5) as r:
                return r.status == 200
        except OSError:
            return False

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def token_url(self) -> str:
        return f"{self.base_url}/{self.token}"

    def _wait_ready(self, timeout: float = 15.0) -> None:
        deadline = time.time() + timeout
        url = f"{self.token_url}/get_status"
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise SurverError(
                    f"surver exited early (code {self.proc.returncode}); "
                    "check that the binary is executable (chmod +x) and that "
                    "the waveform files are readable and intact")
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    if r.status == 200:
                        return
            except OSError:
                time.sleep(0.2)
        self.stop()
        raise SurverError("surver did not become ready in time")

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class SurverManager:
    """Keyed registry of surver instances; reuse by identical file set."""

    def __init__(self, binary: str) -> None:
        self.binary = binary
        self._instances: Dict[str, SurverInstance] = {}
        self._refs: Dict[str, int] = {}
        atexit.register(self.stop_all)

    def get_or_start(self, fst_paths: List[str]) -> SurverInstance:
        key = "|".join(sorted(str(Path(p).resolve()) for p in fst_paths))
        inst = self._instances.get(key)
        if inst and inst.alive():
            self._refs[key] = self._refs.get(key, 0) + 1
            return inst
        inst = SurverInstance(self.binary, fst_paths)
        self._instances[key] = inst
        self._refs[key] = 1
        return inst

    def release(self, inst: SurverInstance) -> bool:
        """Drop one reference; stop the process when the last one goes.

        Instances are shared by identical file set, so closing one view must
        not kill a surver another view is still streaming from. Returns True
        if the process was actually stopped.
        """
        key = "|".join(sorted(inst.fst_paths))
        if key not in self._instances:
            return False
        self._refs[key] = self._refs.get(key, 1) - 1
        if self._refs[key] > 0:
            return False
        inst.stop()
        self._instances.pop(key, None)
        self._refs.pop(key, None)
        return True

    def stop_all(self) -> None:
        for inst in self._instances.values():
            inst.stop()
        self._instances.clear()
        self._refs.clear()
