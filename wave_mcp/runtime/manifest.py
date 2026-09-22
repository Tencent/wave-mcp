"""Session manifest helpers: locate it, resolve the inputs it names.

A session is described by one ``session.json``. Every place that needs to know
"which files does this manifest bind" (the loader, the identity digests, the
staleness check) must enumerate them the same way, or two callers will disagree
about whether two manifests describe the same data. That enumeration lives here
and nowhere else.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

MANIFEST_NAME = "session.json"


def manifest_path(session_path: str) -> str:
    """Resolve a session directory or manifest path to the manifest file."""
    if os.path.isdir(session_path):
        return os.path.join(session_path, MANIFEST_NAME)
    return session_path


def resolve(base: str, path: Optional[str]) -> Optional[str]:
    """A manifest entry as an absolute path, relative entries anchored on ``base``."""
    if not path:
        return None
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(base, path))


def read_filelist_file(path: Optional[str]) -> List[str]:
    """Source paths listed in a ``.f`` filelist, resolved against its own dir."""
    if not path or not os.path.exists(path):
        return []
    base = os.path.dirname(path)
    out: List[str] = []
    with open(path) as fh:
        for line in fh:
            s = line.strip()
            if not s or s.startswith(("#", "//", "-")):
                continue
            out.append(resolve(base, s))
    return out


def manifest_filelist(base_dir: str, manifest: Dict[str, Any]) -> List[str]:
    """The source list a manifest binds, whether inline or via ``filelist_path``."""
    filelist = manifest.get("filelist")
    if not filelist and manifest.get("filelist_path"):
        filelist = read_filelist_file(resolve(base_dir, manifest["filelist_path"]))
    return [resolve(base_dir, f) for f in (filelist or [])]


def manifest_inputs(base_dir: str, manifest: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """The three primary inputs a manifest names, as absolute paths (or None)."""
    return {
        "fst": resolve(base_dir, manifest.get("fst_path")),
        "uhdm": resolve(base_dir, manifest.get("uhdm_db")),
        "maps": resolve(base_dir, manifest.get("maps_path")),
    }


__all__ = ["MANIFEST_NAME", "manifest_path", "resolve", "read_filelist_file",
           "manifest_filelist", "manifest_inputs"]
