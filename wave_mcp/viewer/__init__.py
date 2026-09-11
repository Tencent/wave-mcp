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
import socket
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

_CACHE_DIR = Path.home() / ".cache" / "wave-mcp" / "viewer"

#: Port range size when a deterministic base is configured. Two ports are
#: needed per view (shell HTTP server + streaming backend), so a window of 64
#: comfortably covers the default 8-view cap.
PORT_WINDOW = 64


def port_base() -> Optional[int]:
    """Deterministic viewer port base, or None for ephemeral ports.

    Random high ports are fine locally, but they make ``ssh -L`` forwarding
    painful: the port changes on every view, so no fixed rule can be set up
    in advance. Setting ``WAVE_MCP_VIEWER_PORT_BASE`` confines the viewer to
    ``[base, base + PORT_WINDOW)`` so one forwarding rule (or one firewall
    hole, for a shared host) covers every view.
    """
    raw = os.environ.get("WAVE_MCP_VIEWER_PORT_BASE", "").strip()
    if not raw:
        return None
    try:
        base = int(raw)
    except ValueError:
        return None
    return base if 1024 <= base <= 65535 - PORT_WINDOW else None


def alloc_port(host: str = "127.0.0.1",
               exclude: Optional[Sequence[int]] = None) -> int:
    """Pick a free port, honouring the configured base when present.

    Falls back to an ephemeral port if the whole configured window is taken,
    so a busy host degrades instead of failing to open a view.

    Note: this only *probes*. The returned port is unbound again by the time
    the caller receives it, so on a busy host another process can take it
    before the caller binds. Prefer :func:`alloc_port_socket`, which holds
    the port; this one remains for callers that cannot use a socket, such as
    handing a port number to a child process. ``exclude`` skips ports already
    known to be unusable, so a retry can move on instead of repeating one.
    """
    skip = set(exclude or ())
    base = port_base()
    if base is not None:
        for candidate in range(base, base + PORT_WINDOW):
            if candidate in skip:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind((host, candidate))
                    return candidate
                except OSError:
                    continue
    for _ in range(64):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, 0))
            port = s.getsockname()[1]
        if port not in skip:
            return port
    raise OSError("could not find a free port outside the excluded set")


def alloc_port_socket(host: str = "127.0.0.1") -> socket.socket:
    """Reserve a free port and return it *already bound and listening*.

    Probing a port and binding it later is a TOCTOU race: between the two,
    any other process on the host can take the port, and the bind then fails
    with EADDRINUSE. Under a full regression run, which starts many viewers
    and survers in quick succession, that showed up as an intermittent
    "surver failed to start".

    Returning a listening socket closes the window: the kernel keeps the
    port reserved for as long as this socket is open. Callers either use it
    directly (HTTP server) or keep it open until a child has bound the port.
    """
    base = port_base()
    if base is not None:
        for candidate in range(base, base + PORT_WINDOW):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, candidate))
                s.listen(1)
                return s
            except OSError:
                s.close()
                continue
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, 0))
    s.listen(1)
    return s


def _valid(root: Path) -> bool:
    return (root / "surver").is_file() and (root / "wasm" / "index.html").is_file()

#: Why an explicitly configured WAVE_MCP_VIEWER_ASSETS was rejected, if it was.
#: Kept so the degradation hint can name the real cause instead of telling a
#: user who did configure the env var to "set WAVE_MCP_VIEWER_ASSETS".
_env_miss: Optional[str] = None

#: Why the installed assets package / the user cache was rejected, if either
#: was found but incomplete. Without these, a user whose pip package was
#: installed but broken was told to "pip install wave-mcp[viewer]" again,
#: which pointed away from the actual problem.
_pip_miss: Optional[str] = None
_cache_miss: Optional[str] = None

def _record_env_miss(raw: str, resolved: Path) -> None:
    global _env_miss
    detail = f"WAVE_MCP_VIEWER_ASSETS={raw!r}"
    if str(resolved) != raw:
        detail += f" (resolved to {resolved})"
    if not resolved.is_dir():
        detail += " does not exist"
    else:
        detail += " is missing surver and/or wasm/index.html"
    _env_miss = detail

def find_assets() -> Optional[Dict[str, Any]]:
    """Locate viewer assets; return {root, surver, wasm, origin} or None."""
    global _pip_miss, _cache_miss
    # 1. explicit env (air-gapped bundle)
    env = os.environ.get("WAVE_MCP_VIEWER_ASSETS")
    if env:
        # An MCP client starts the server from the user's project dir, so a
        # relative value here would resolve against a cwd nobody intended and
        # then silently degrade to "viewer unavailable". Anchor it on $HOME
        # (a stable, user-owned base) and keep the reason retrievable.
        root = Path(env).expanduser()
        if not root.is_absolute():
            root = Path.home() / root
        if _valid(root):
            return _hit(root, "env")
        _record_env_miss(env, root)

    # 2. pip assets package
    try:
        import wave_mcp_viewer_assets  # type: ignore
        root = Path(wave_mcp_viewer_assets.__file__).parent / "data"
        if _valid(root):
            return _hit(root, "pip")
        _pip_miss = (f"the installed viewer assets package at {root} is "
                     "missing surver and/or wasm/index.html")
    except ImportError:
        pass

    # 3. user cache (first-run download target)
    if _valid(_CACHE_DIR):
        return _hit(_CACHE_DIR, "cache")
    if _CACHE_DIR.exists():
        _cache_miss = (f"the cached viewer assets at {_CACHE_DIR} are "
                       "incomplete (need surver and wasm/index.html)")

    return None


def _hit(root: Path, origin: str) -> Dict[str, Any]:
    return {
        "root": str(root),
        "surver": str(root / "surver"),
        "wasm": str(root / "wasm"),
        "origin": origin,
    }


def unavailable_hint() -> Dict[str, Any]:
    """Uniform degradation payload, style-aligned with _no_waveform.

    ``available: false`` is reserved for the viewer feature itself being
    unusable; parameter mistakes return ``invalid_argument_payload`` instead,
    so a typo never reads as an outage."""
    if _env_miss:
        hint = (
            f"viewer assets not found: {_env_miss}. Point "
            "WAVE_MCP_VIEWER_ASSETS at a directory containing `surver` and "
            "`wasm/index.html` (use an absolute path), or install with "
            "`pip install wave-mcp[viewer]`. Analysis tools are unaffected."
        )
    elif _pip_miss:
        hint = (
            f"viewer assets not usable: {_pip_miss}. Reinstall with "
            "`pip install --force-reinstall wave-mcp[viewer]`, or point "
            "WAVE_MCP_VIEWER_ASSETS at a complete asset directory. Analysis "
            "tools are unaffected."
        )
    elif _cache_miss:
        hint = (
            f"viewer assets not usable: {_cache_miss}. Delete that directory "
            "and reinstall `wave-mcp[viewer]` to re-download, or set "
            "WAVE_MCP_VIEWER_ASSETS. Analysis tools are unaffected."
        )
    else:
        hint = (
            "viewer assets not found; install with `pip install "
            "wave-mcp[viewer]`, or set WAVE_MCP_VIEWER_ASSETS to an asset "
            "directory (absolute path; offline bundles ship one), or place "
            f"assets under {_CACHE_DIR}. Analysis tools are unaffected."
        )
    return {
        "status": "error",
        "available": False,
        "error_type": "viewer_unavailable",
        "feature": "wave viewer",
        "hint": hint,
    }

def invalid_argument_payload(exc: BaseException) -> Dict[str, Any]:
    """Structured reply for a rejected viewer request.

    Format aligned with the other tools ("status" plus "error"), extended
    with the failing ``parameter`` and whatever the validator can offer:
    ``did_you_mean`` for a typo, ``expected`` / ``example`` for a time or
    shape problem. ``available`` is deliberately absent here: it answers
    whether the viewer works, and a malformed argument says nothing about
    that."""
    out: Dict[str, Any] = {"status": "error",
                           "error_type": "invalid_argument",
                           "error": str(exc)}
    for key in ("parameter", "did_you_mean", "expected", "example"):
        val = getattr(exc, key, None)
        if val is not None:
            out[key] = val
    return out


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
