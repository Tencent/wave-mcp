"""View lifecycle manager: ties surver + ViewerServer + ViewState together.

Used by both the ``wave-view`` CLI and the three MCP viewer tools. One
process hosts at most a handful of views; each view owns one ViewState
and one surver instance (file-set keyed, reusable).
"""
from __future__ import annotations

import collections
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import (find_assets, invalid_argument_payload, shell_web_dir,
               unavailable_hint)
from .state import ViewState, ViewStateError, validate_view_inputs
from .surver import SurverManager
from .server import ViewerServer


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
    except Exception:  # pylint: disable=broad-except
        return None, None


#: FST signal-name sets, keyed by (resolved path, mtime_ns, size). Building
#: one parses the var table (about 1.6 s for a 39 GB / 11.7k-signal file,
#: negligible for small ones), so a few are kept around.
_SIG_NAMES_CACHE: "collections.OrderedDict[tuple, frozenset]" = \
    collections.OrderedDict()
_SIG_NAMES_CACHE_MAX = 4


def _norm_sig(name: str) -> str:
    return re.sub(r"\s+", "", name).lower()


def _fst_signal_names(fst_path: str):
    """Normalized full paths of every signal in ``fst_path``, or None.

    None means "cannot check" (unreadable file, parse failure): the caller
    then skips the advisory check rather than guessing."""
    try:
        st = os.stat(fst_path)
        key = (str(Path(fst_path).resolve()), st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    hit = _SIG_NAMES_CACHE.get(key)
    if hit is not None:
        _SIG_NAMES_CACHE.move_to_end(key)
        return hit
    try:
        from ..sources.fst_source import FstSource
        src = FstSource(str(fst_path))
        try:
            names = frozenset(_norm_sig(n) for n in src.signals)
        finally:
            src.close()
    except Exception:  # pylint: disable=broad-except
        return None
    _SIG_NAMES_CACHE[key] = names
    while len(_SIG_NAMES_CACHE) > _SIG_NAMES_CACHE_MAX:
        _SIG_NAMES_CACHE.popitem(last=False)
    return names


def _signal_known(raw: str, names) -> bool:
    """Loose containment search for one requested signal name.

    Matches exact, whitespace-insensitive and case-insensitive forms, plus
    the aggregated spelling: a writer that keeps ``bus[7:0]`` as elements
    is still reached by asking for ``bus`` or ``bus[7:0]``."""
    n = _norm_sig(raw)
    if n in names:
        return True
    base = re.sub(r"\[[^\]]*\]$", "", n)
    if base != n:
        if base in names:
            return True
        probe = base + "["
        return any(x.startswith(probe) for x in names)
    return False


def _missing_signals(fst_paths, signals) -> List[str]:
    """Signal names not present in any of the given waveforms.

    Advisory only: a name the waveform does not contain makes Surfer drop
    that variable without a word, which reads as "the signal never
    appeared". The check is deliberately conservative: if any file cannot
    be parsed it stays quiet instead of reporting a false problem."""
    if not signals or not fst_paths:
        return []
    names = set()
    for p in fst_paths:
        got = _fst_signal_names(p)
        if got is None:
            return []
        names |= got
    out, seen = [], set()
    for s in signals:
        try:
            raw = str((s or {}).get("path") or "").strip()
        except (AttributeError, TypeError):
            continue
        if not raw or raw in seen or "*" in raw or "?" in raw:
            continue
        seen.add(raw)
        if not _signal_known(raw, names):
            out.append(raw)
    return out


def _signal_warning(name: str) -> str:
    return (f"signal not found in the waveform (the viewer will show "
            f"nothing for it): {name}")


def validate_open_args(fst_paths: List[str],
                       labels: Optional[List[str]] = None) -> None:
    """Reject open-wave-view argument mistakes before any process starts.

    The viewer shows one waveform, or two as a comparison. A third waveform
    would silently not be shown, and a ``labels`` list whose length did not
    match used to be ignored entry by entry, so both are validated here and
    reported instead. Pure function: nothing is started, which also keeps it
    directly testable without viewer assets."""
    n = len(fst_paths)
    if n < 1:
        raise ViewStateError("fst_paths must contain at least one waveform "
                             "path", parameter="fst_paths")
    if n > 2:
        raise ViewStateError(
            f"{n} waveform paths were given; the viewer shows at most two "
            "(one plain view, or two as a comparison)",
            parameter="fst_paths")
    if labels and len(labels) != n:
        raise ViewStateError(
            f"labels has {len(labels)} entries but there are {n} waveform(s);"
            " one label per waveform is required", parameter="labels")


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

    def _release_surver(self, surver: Any) -> None:
        """Return the reference get_or_start took for a view that never opened.

        get_or_start increments the refcount before the view exists, so an
        abandoned open used to leak a surver process (and its port) until the
        owning process exited. Best effort: cleanup must never mask the real
        error."""
        try:
            self._surver().release(surver)
        except Exception:  # pylint: disable=broad-except
            pass

    def _cleanup_failed_open(self, surver: Any, server: Any) -> None:
        if server is not None:
            try:
                server.stop()
            except Exception:  # pylint: disable=broad-except
                pass
        self._release_surver(surver)

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

        # Argument mistakes are answered before anything is started, with the
        # parameter named. This keeps a typo from spinning up a surver (or
        # from failing silently), and must run before any side effect.
        try:
            validate_open_args(list(fst_paths or []), labels)
            validate_view_inputs(signals=signals, cursor=cursor,
                                 viewport=viewport, markers=markers,
                                 diff=diff, annotations=annotations)
        except ViewStateError as exc:
            return invalid_argument_payload(exc)

        # Advisory: signal names the waveform does not contain. Cached per
        # file and never fatal; the names land in ``warnings`` so the caller
        # (and the page) can say why a signal would never appear.
        missing: List[str] = []
        try:
            missing = _missing_signals(
                [str(Path(p).resolve()) for p in (fst_paths or [])],
                signals or [])
        except (OSError, ValueError):
            missing = []

        from .surver import SurverError
        try:
            surver = self._surver().get_or_start(fst_paths)
        except SurverError as exc:
            return {"status": "error", "available": False,
                    "error_type": "surver_error", "error": str(exc),
                    "hint": "surver failed to start; check the waveform "
                            "paths and that the surver binary is executable "
                            "(chmod +x) and runs on this host"}

        state = ViewState()
        server = None
        try:
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
            if missing:
                state.warnings.extend(_signal_warning(m) for m in missing)

            server = ViewerServer(
                wasm_dir=self.assets["wasm"],
                shell_dir=shell_web_dir(),
                surver_base=surver.base_url,
                state=state,
                token=surver.token,
            )
            server.start()
        except ViewStateError as exc:
            # Already validated above; kept for safety so a failure here can
            # never strand the surver reference or leave a half-open view.
            self._cleanup_failed_open(surver, server)
            return invalid_argument_payload(exc)
        except Exception:  # pylint: disable=broad-except
            self._cleanup_failed_open(surver, server)
            raise

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
        evicted = self._evict_if_needed(keep=view_id)
        out = {
            "available": True,
            "view_id": view_id,
            "url": url,
            "native_hint": f"surfer {surver.token_url}",
            "ssh_hint": (f"ssh -L {server.port}:localhost:{server.port} "
                         f"<this-host>  # then open {url}"),
        }
        if evicted:
            # An evicted page stops updating with no other notice, so name it
            # here. Singular for the normal one-eviction case; the list covers
            # a burst (several views over the cap at once).
            out["evicted_view_id"] = evicted[0]
            if len(evicted) > 1:
                out["evicted_view_ids"] = evicted
        if state.warnings:
            out["warnings"] = list(state.warnings)
        return out

    def update_view(self, view_id: str, **kwargs) -> Dict[str, Any]:
        if not self.available:
            return unavailable_hint()
        view = self._views.get(view_id)
        if view is None:
            return {"status": "error", "error_type": "unknown_view",
                    "error": f"unknown view_id {view_id}",
                    "known_views": list(self._views)}
        try:
            rev = view["state"].update_desired(**kwargs)
        except ViewStateError as exc:
            return invalid_argument_payload(exc)
        try:
            if kwargs.get("signals"):
                miss = _missing_signals(view.get("fst_paths", []),
                                        kwargs["signals"])
                if miss:
                    view["state"].warnings.extend(
                        _signal_warning(m) for m in miss)
        except (OSError, ValueError):
            pass
        out = {"available": True, "view_id": view_id, "revision": rev,
               "url": view["url"]}
        if view["state"].warnings:
            out["warnings"] = list(view["state"].warnings)
        return out

    def get_state(self, view_id: str) -> Dict[str, Any]:
        if not self.available:
            return unavailable_hint()
        view = self._views.get(view_id)
        if view is None:
            return {"status": "error", "error_type": "unknown_view",
                    "error": f"unknown view_id {view_id}",
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
            return {"status": "error", "error_type": "unknown_view",
                    "error": f"unknown view_id {view_id}",
                    "known_views": list(self._views)}
        errors = []
        try:
            view["server"].stop()
        except Exception as exc:  # pylint: disable=broad-except
            errors.append(f"http server: {exc}")
        surver_stopped = False
        try:
            surver_stopped = self._surver().release(view["surver"])
        except Exception as exc:  # pylint: disable=broad-except
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

    def _evict_if_needed(self, keep: Optional[str] = None) -> List[str]:
        """Close oldest views past max_views so long runs cannot pile up.

        Returns the evicted ids (oldest first) so ``open_view`` can report
        them: an evicted page stops updating with no other notice."""
        evicted: List[str] = []
        if self.max_views <= 0:
            return evicted
        while len(self._views) > self.max_views:
            oldest = min(
                (vid for vid in self._views if vid != keep),
                key=lambda v: self._views[v].get("created_at") or 0,
                default=None)
            if oldest is None:
                return evicted
            self.close_view(oldest)
            evicted.append(oldest)
        return evicted
