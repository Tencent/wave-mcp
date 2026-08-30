"""Viewer asset discovery for wave-mcp.

Assets = Surfer WASM bundle + surver binary (EUPL-1.2, distributed
separately from the MIT core). Lookup order (first hit wins):

1. ``WAVE_MCP_VIEWER_ASSETS`` env var (offline bundle sets this)
2. installed ``wave_mcp_viewer_assets`` pip package
3. ``~/.cache/wave-mcp/viewer/`` (populated by first-run download)

A valid asset dir contains ``surver`` (executable) and ``wasm/index.html``.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

_CACHE_DIR = Path.home() / ".cache" / "wave-mcp" / "viewer"


def _valid(root: Path) -> bool:
    return (root / "surver").is_file() and (root / "wasm" / "index.html").is_file()


def find_assets() -> Optional[Dict[str, Any]]:
    """Locate viewer assets; return {root, surver, wasm, origin} or None."""
    # 1. explicit env (air-gapped bundle)
    env = os.environ.get("WAVE_MCP_VIEWER_ASSETS")
    if env:
        root = Path(env)
        if _valid(root):
            return _hit(root, "env")

    # 2. pip assets package
    try:
        import wave_mcp_viewer_assets  # type: ignore
        root = Path(wave_mcp_viewer_assets.__file__).parent / "data"
        if _valid(root):
            return _hit(root, "pip")
    except ImportError:
        pass

    # 3. user cache (first-run download target)
    if _valid(_CACHE_DIR):
        return _hit(_CACHE_DIR, "cache")

    return None


def _hit(root: Path, origin: str) -> Dict[str, Any]:
    return {
        "root": str(root),
        "surver": str(root / "surver"),
        "wasm": str(root / "wasm"),
        "origin": origin,
    }


def unavailable_hint() -> Dict[str, Any]:
    """Uniform degradation payload, style-aligned with _no_waveform."""
    return {
        "available": False,
        "feature": "wave viewer",
        "hint": (
            "viewer assets not found; install with `pip install "
            "wave-mcp[viewer]`, or set WAVE_MCP_VIEWER_ASSETS to an asset "
            "directory (offline bundles ship one), or place assets under "
            f"{_CACHE_DIR}. Analysis tools are unaffected."
        ),
    }


def shell_web_dir() -> str:
    """Our own shell assets (MIT, shipped with the core package)."""
    return str(Path(__file__).parent / "web")


def have_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def try_open_browser(url: str) -> bool:
    if not have_display():
        return False
    opener = shutil.which("xdg-open")
    if not opener:
        return False
    import subprocess
    try:
        subprocess.Popen([opener, url], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False
