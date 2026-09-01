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
from typing import Dict, List, Optional


class SurverError(RuntimeError):
    pass


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class SurverInstance:
    def __init__(self, binary: str, fst_paths: List[str]) -> None:
        for p in fst_paths:
            if not Path(p).is_file():
                raise SurverError(f"waveform not found: {p}")
        self.fst_paths = [str(Path(p).resolve()) for p in fst_paths]
        self.port = _free_port()
        self.token = secrets.token_urlsafe(12)
        self.proc = subprocess.Popen(
            [binary, "--port", str(self.port), "--bind-address", "127.0.0.1",
             "--token", self.token, *self.fst_paths],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._wait_ready()

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
                    "check glibc>=2.34 or use the musl build from the asset "
                    "package")
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
