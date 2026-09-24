"""Lazy netlist store: open a large maps.json without parsing it on every open.

A chip-level maps.json reaches hundreds of MB (measured 864 MB on a 2731-module
design). The old loader parsed all of it on every session open and every CLI
query, then walked every node to rewrite file paths, so a millisecond query
cost ~19 s. This module keeps a derived, per-module index next to nothing the
user owns (the derived-cache layer, ``<cache_root>/netlist-store/``):

    <digest>/meta.bin      everything except ``modules`` + the module name list
    <digest>/modules.bin   modules, each marshalled separately, back to back
    <digest>/index.bin     name -> (offset, length) into modules.bin

The digest is ``file_version(maps.json)``: rebuilding the netlist yields a new
store, an unchanged one keeps hitting the same entry. ``marshal`` is used
because it ships with every CPython and reads plain dict/list/str/int trees
several times faster than JSON; the store is private to one interpreter
version, which is part of the digest. The netlist build writes the store from
its in-memory result (:func:`prebuild`), so even the first open skips the
JSON parse; a store that is missing or stale is rebuilt from maps.json.

:class:`LazyModules` is a read-only ``Mapping`` that loads a module on first
access. Code that walks every module (``values()``/``items()``) still works,
just without the saving; the common per-path queries touch a handful.
"""
from __future__ import annotations

import gc
import json
import marshal
import mmap
import os
import sys
from collections.abc import Mapping
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

from ..runtime import storage
from ..runtime.identity import cache_key, file_version
from ..runtime.storage import StoragePolicy

#: bump when the on-disk layout changes
STORE_FORMAT = "2"
#: maps.json smaller than this is parsed directly: the store only pays off when
#: parsing dominates, and small sample netlists stay one plain file
LAZY_MIN_BYTES = 8 * 1024 * 1024
#: environment switch to force (``1``) or disable (``0``) the lazy store
LAZY_ENV = "WAVE_MCP_LAZY_NETLIST"


def _store_dir(maps_path: str, create: bool) -> str:
    key = cache_key(file_version(maps_path), STORE_FORMAT,
                    f"py{sys.version_info[0]}.{sys.version_info[1]}")
    d = os.path.join(storage.policy().cache_dir("netlist-store", create=create), key)
    return d


def lazy_enabled(maps_path: str) -> bool:
    flag = os.environ.get(LAZY_ENV, "").strip()
    if flag == "0":
        return False
    if flag == "1":
        return True
    try:
        return os.path.getsize(maps_path) >= LAZY_MIN_BYTES
    except OSError:
        return False


class LazyModules(Mapping):
    """``modules`` of a netlist, each loaded (and post-processed) on first use."""

    def __init__(self, store: str, names: list,
                 fixup: Optional[Callable[[dict], None]] = None) -> None:
        self._store = store
        self._names = names
        self._name_set = set(names)
        self._loaded: Dict[str, dict] = {}
        self._index: Optional[Dict[str, Tuple[int, int]]] = None
        self._mm: Optional[mmap.mmap] = None
        self._fh = None
        self.fixup = fixup
        #: name -> declaring file / line, from the store summary (no decode)
        self.files: Dict[str, Optional[str]] = {}
        self.lines: Dict[str, Optional[int]] = {}

    def _open(self) -> None:
        with open(os.path.join(self._store, "index.bin"), "rb") as fh:
            self._index = marshal.load(fh)
        self._fh = open(os.path.join(self._store, "modules.bin"), "rb")
        self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)

    def __getitem__(self, name: str) -> dict:
        hit = self._loaded.get(name)
        if hit is not None:
            return hit
        if name not in self._name_set:
            raise KeyError(name)
        if self._index is None:
            self._open()
        off, length = self._index[name]
        mod = marshal.loads(self._mm[off:off + length])
        if self.fixup is not None:
            self.fixup(mod)
        self._loaded[name] = mod
        return mod

    def __contains__(self, name: object) -> bool:
        return name in self._name_set

    def __iter__(self) -> Iterator[str]:
        return iter(self._names)

    def __len__(self) -> int:
        return len(self._names)

    def keys(self):  # names only: never loads a module
        return list(self._names)

    @property
    def loaded_count(self) -> int:
        return len(self._loaded)

    def close(self) -> None:
        if self._mm is not None:
            self._mm.close()
            self._mm = None
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __del__(self):  # pragma: no cover - best effort
        try:
            self.close()
        except Exception:  # pylint: disable=broad-except
            pass


def _write_store(maps_path: str, store: str, data: dict) -> None:
    """Split ``data`` into the three store files, atomically per file."""
    modules = data.get("modules") or {}
    names = list(modules)
    meta = {k: v for k, v in data.items() if k != "modules"}
    # summaries netlists built before 1.0.2 lack; computed once here so the
    # lazy open never has to decode every module to answer them
    if "skipped_members_total" not in meta:
        meta["skipped_members_total"] = sum(
            int((modules[n] or {}).get("skipped_members", 0) or 0) for n in names)
    if "roots" not in meta:
        meta["roots"] = [p for p in (data.get("instance_tree") or {})
                         if "." not in p]
    meta["__module_names__"] = names
    meta["__module_files__"] = {n: (modules[n] or {}).get("file") for n in names}
    meta["__module_lines__"] = {n: (modules[n] or {}).get("line") for n in names}
    index: Dict[str, Tuple[int, int]] = {}

    def write_modules(tmp: str) -> None:
        off = 0
        with open(tmp, "wb") as fh:
            for name in names:
                blob = marshal.dumps(modules[name])
                fh.write(blob)
                index[name] = (off, len(blob))
                off += len(blob)
    os.makedirs(store, exist_ok=True)
    StoragePolicy.atomic_write(os.path.join(store, "modules.bin"), write_modules)
    StoragePolicy.atomic_write_bytes(os.path.join(store, "index.bin"),
                                     marshal.dumps(index))
    # meta last: its presence marks a complete store
    StoragePolicy.atomic_write_bytes(os.path.join(store, "meta.bin"),
                                     marshal.dumps(meta))


def prebuild(maps_path: str, data: dict) -> bool:
    """Write the store for a just-written ``maps_path`` from ``data`` in memory.

    Called by the netlist build right after maps.json is written, so the first
    open does not parse the file again. Only for netlists that will open lazily;
    any failure is silent and the open path rebuilds from maps.json instead.
    """
    if not data or not data.get("modules") or not lazy_enabled(maps_path):
        return False
    try:
        store = _store_dir(maps_path, create=True)
        meta_path = os.path.join(store, "meta.bin")
        with storage.policy().locked(store):
            if not os.path.exists(meta_path):
                _write_store(maps_path, store, data)
        return True
    except (OSError, ValueError, TypeError):
        return False


def open_lazy(maps_path: str,
              fixup: Optional[Callable[[dict], None]] = None,
              loader: Optional[Callable[[str], dict]] = None) -> Optional[dict]:
    """Netlist maps with ``modules`` as a :class:`LazyModules`, or None.

    Builds the store on first use (one full parse, same cost as before) and
    reuses it afterwards. None means "use the eager path": store unwritable,
    corrupt, or the maps file unreadable.
    """
    try:
        store = _store_dir(maps_path, create=True)
    except OSError:
        return None
    meta_path = os.path.join(store, "meta.bin")
    pol = storage.policy()
    try:
        if not os.path.exists(meta_path):
            with pol.locked(store):
                if not os.path.exists(meta_path):
                    data = (loader or load_json)(maps_path)
                    if not data or not data.get("modules"):
                        return None
                    _write_store(maps_path, store, data)
        with open(meta_path, "rb") as fh:
            meta = marshal.load(fh)
        try:
            marker = os.path.join(store, storage.LAST_USED_NAME)
            with open(marker, "a"):
                pass
            os.utime(marker, None)
        except OSError:
            pass
    except (OSError, ValueError, EOFError, TypeError):
        for name in ("meta.bin", "index.bin", "modules.bin"):
            StoragePolicy.discard(os.path.join(store, name))
        return None
    names = meta.pop("__module_names__", [])
    lazy = LazyModules(store, names, fixup)
    lazy.files = meta.pop("__module_files__", {}) or {}
    lazy.lines = meta.pop("__module_lines__", {}) or {}
    meta["modules"] = lazy
    return meta


def load_json(path: str) -> dict:
    """Parse a maps.json; ``{}`` when unreadable.

    The cyclic garbage collector is paused for the parse: a netlist is a tree
    of millions of freshly allocated dicts and lists with no cycles, and the
    collector's repeated scans of that growing heap were about 45% of the parse
    time on a 1.2 GB netlist (14.7 s -> 8.2 s). It is re-enabled afterwards,
    only if it was enabled before.
    """
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}
    finally:
        if was_enabled:
            gc.enable()


__all__ = ["LazyModules", "open_lazy", "prebuild", "load_json", "lazy_enabled",
           "LAZY_MIN_BYTES", "LAZY_ENV", "STORE_FORMAT"]
