"""View lifecycle manager: ties surver + ViewerServer + ViewState together.

Used by both the ``wave-view`` CLI and the three MCP viewer tools. One
process hosts at most a handful of views; each view owns one ViewState
and one surver instance (file-set keyed, reusable).
"""
from __future__ import annotations

import secrets
from typing import Any, Dict, List, Optional

from . import find_assets, shell_web_dir, unavailable_hint
from .state import ViewState
from .surver import SurverManager
from .server import ViewerServer
from .translate import desired_to_sucl


def _fst_end_time(path: str):
    """Header-only end-time read (ms even on tens-of-GB files)."""
    try:
        from pylibfst import lib
        ctx = lib.fstReaderOpen(path.encode())
        if not ctx:
            return None
        try:
            return int(lib.fstReaderGetEndTime(ctx))
        finally:
            lib.fstReaderClose(ctx)
    except Exception:
        return None


class ViewManager:
    _instance: Optional["ViewManager"] = None

    @classmethod
    def instance(cls) -> "ViewManager":
        if cls._instance is None:
            cls._instance = ViewManager()
        return cls._instance

    def __init__(self) -> None:
        self.assets = find_assets()
        self._surver_mgr: Optional[SurverManager] = None
        self._views: Dict[str, Dict[str, Any]] = {}

    @property
    def available(self) -> bool:
        return self.assets is not None

    def _surver(self) -> SurverManager:
        assert self.assets is not None
        if self._surver_mgr is None:
            self._surver_mgr = SurverManager(self.assets["surver"])
        return self._surver_mgr

    # -- public API ------------------------------------------------------

    def open_view(
        self,
        fst_paths: List[str],
        signals: Optional[List[Any]] = None,
        cursor: Optional[Dict[str, Any]] = None,
        viewport: Optional[Dict[str, Any]] = None,
        markers: Optional[List[Any]] = None,
        diff: Optional[Dict[str, Any]] = None,
        annotations: Optional[List[Any]] = None,
        labels: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        if not self.available:
            return unavailable_hint()

        from .surver import SurverError
        try:
            surver = self._surver().get_or_start(fst_paths)
        except SurverError as exc:
            return {"available": False, "feature": "wave viewer",
                    "error": str(exc),
                    "hint": "surver failed to start; check the waveform "
                            "paths and that the surver binary runs on this "
                            "host (glibc>=2.34, or use the musl build)"}

        state = ViewState()
        sources = []
        for i, p in enumerate(surver.fst_paths):   # resolved absolute paths
            sources.append({
                "id": chr(ord("a") + i),
                "path": p,
                "label": (labels[i] if labels and i < len(labels) else ""),
                "end_time": _fst_end_time(p),
            })
        state.set_sources(sources)
        state.update_desired(signals=signals, cursor=cursor,
                             viewport=viewport, markers=markers,
                             diff=diff, annotations=annotations)

        server = ViewerServer(
            wasm_dir=self.assets["wasm"],
            shell_dir=shell_web_dir(),
            surver_base=surver.base_url,
            state=state,
        )
        server.start()

        view_id = secrets.token_hex(4)
        url = f"{server.base_url}/view.html?token={surver.token}"
        self._views[view_id] = {
            "state": state, "server": server, "surver": surver, "url": url,
        }
        return {
            "available": True,
            "view_id": view_id,
            "url": url,
            "native_hint": f"surfer {surver.token_url}",
            "ssh_hint": (f"ssh -L {server.port}:localhost:{server.port} "
                         f"<this-host>  # then open {url}"),
        }

    def update_view(self, view_id: str, **kwargs) -> Dict[str, Any]:
        if not self.available:
            return unavailable_hint()
        view = self._views.get(view_id)
        if view is None:
            return {"available": False, "error": f"unknown view_id {view_id}",
                    "known_views": list(self._views)}
        rev = view["state"].update_desired(**kwargs)
        return {"available": True, "view_id": view_id, "revision": rev,
                "url": view["url"]}

    def get_state(self, view_id: str) -> Dict[str, Any]:
        if not self.available:
            return unavailable_hint()
        view = self._views.get(view_id)
        if view is None:
            return {"available": False, "error": f"unknown view_id {view_id}",
                    "known_views": list(self._views)}
        snap = view["state"].snapshot()
        return {
            "available": True,
            "view_id": view_id,
            "url": view["url"],
            "revision": snap["revision"],
            "actual": snap["actual"],
            "desired_summary": {
                "signals": [s["path"] for s in snap["desired"]["signals"]],
                "cursor": snap["desired"]["cursor"],
                "viewport": snap["desired"]["viewport"],
                "markers": len(snap["desired"]["markers"]),
                "annotations": len(snap["desired"]["annotations"]),
            },
        }
