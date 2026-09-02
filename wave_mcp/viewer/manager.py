"""View lifecycle manager: ties surver + ViewerServer + ViewState together.

Used by both the ``wave-view`` CLI and the three MCP viewer tools. One
process hosts at most a handful of views; each view owns one ViewState
and one surver instance (file-set keyed, reusable).
"""
from __future__ import annotations

import os
import secrets
import time
from typing import Any, Dict, List, Optional

from . import find_assets, shell_web_dir, unavailable_hint
from .state import ViewState
from .surver import SurverManager
from .server import ViewerServer
from .translate import desired_to_sucl


def _fst_meta(path: str):
    """Header-only read of (end_time, timescale_exp); ms even on huge files."""
    try:
        from pylibfst import lib
        ctx = lib.fstReaderOpen(path.encode())
        if not ctx:
            return None, None
        try:
            end = int(lib.fstReaderGetEndTime(ctx))
            ts = int(lib.fstReaderGetTimescale(ctx))
            return end, ts
        finally:
            lib.fstReaderClose(ctx)
    except Exception:
        return None, None


class ViewManager:
    _instance: Optional["ViewManager"] = None

    DEFAULT_OWNER = "local"
    DEFAULT_MAX_VIEWS = 8

    @classmethod
    def instance(cls) -> "ViewManager":
        if cls._instance is None:
            cls._instance = ViewManager()
        return cls._instance

    def __init__(self) -> None:
        self.assets = find_assets()
        self._surver_mgr: Optional[SurverManager] = None
        self._views: Dict[str, Dict[str, Any]] = {}
        try:
            self.max_views = int(os.environ.get("WAVE_MCP_MAX_VIEWS",
                                                self.DEFAULT_MAX_VIEWS))
        except ValueError:
            self.max_views = self.DEFAULT_MAX_VIEWS

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
        owner: Optional[str] = None,
        title: Optional[str] = None,
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
                            "paths and that the surver binary is executable "
                            "(chmod +x) and runs on this host"}

        state = ViewState()
        sources = []
        for i, p in enumerate(surver.fst_paths):   # resolved absolute paths
            end_time, ts = _fst_meta(p)
            entry = {
                "id": chr(ord("a") + i),
                "path": p,
                "label": (labels[i] if labels and i < len(labels) else ""),
                "end_time": end_time,
            }
            if ts is not None:
                entry["timescale_exp"] = ts
            sources.append(entry)
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
            # owner is a label only: today every view belongs to the single
            # local user. A future multi-user server mode fills it per client
            # and scopes list/close by it, so record it from the start.
            "owner": owner or self.DEFAULT_OWNER,
            "title": title or "",
            "created_at": time.time(),
            "fst_paths": list(surver.fst_paths),
        }
        self._evict_if_needed(keep=view_id)
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

    def list_views(self, owner: Optional[str] = None) -> Dict[str, Any]:
        """Inventory of open views, newest first.

        ``owner`` filters by the label recorded at open time; it exists so a
        future multi-user server mode can scope the listing per client
        without changing this signature.
        """
        if not self.available:
            return unavailable_hint()
        items = []
        for vid, v in self._views.items():
            if owner is not None and v.get("owner") != owner:
                continue
            items.append({
                "view_id": vid,
                "url": v["url"],
                "title": v.get("title", ""),
                "owner": v.get("owner", self.DEFAULT_OWNER),
                "fst_paths": v.get("fst_paths", []),
                "created_at": v.get("created_at"),
                "revision": v["state"].snapshot()["revision"],
                "surver_alive": v["surver"].alive(),
            })
        items.sort(key=lambda d: d.get("created_at") or 0, reverse=True)
        return {"available": True, "count": len(items),
                "max_views": self.max_views, "views": items}

    def close_view(self, view_id: str) -> Dict[str, Any]:
        """Close one view and free its HTTP server and surver reference."""
        if not self.available:
            return unavailable_hint()
        view = self._views.pop(view_id, None)
        if view is None:
            return {"available": False, "error": f"unknown view_id {view_id}",
                    "known_views": list(self._views)}
        errors = []
        try:
            view["server"].stop()
        except Exception as exc:                       # never fail a close
            errors.append(f"http server: {exc}")
        surver_stopped = False
        try:
            surver_stopped = self._surver().release(view["surver"])
        except Exception as exc:
            errors.append(f"surver: {exc}")
        out = {"available": True, "closed": view_id,
               "surver_stopped": surver_stopped,
               "remaining": len(self._views)}
        if errors:
            out["warnings"] = errors
        return out

    def close_all(self, owner: Optional[str] = None) -> Dict[str, Any]:
        """Close every view, optionally only those of one owner."""
        if not self.available:
            return unavailable_hint()
        targets = [vid for vid, v in self._views.items()
                   if owner is None or v.get("owner") == owner]
        closed = [vid for vid in targets
                  if self.close_view(vid).get("closed")]
        return {"available": True, "closed": closed, "count": len(closed),
                "remaining": len(self._views)}

    def _evict_if_needed(self, keep: Optional[str] = None) -> None:
        """Close oldest views past max_views so long runs cannot pile up."""
        if self.max_views <= 0:
            return
        while len(self._views) > self.max_views:
            oldest = min(
                (vid for vid in self._views if vid != keep),
                key=lambda v: self._views[v].get("created_at") or 0,
                default=None)
            if oldest is None:
                return
            self.close_view(oldest)
