"""Identity and version digests: the only place wave-mcp hashes anything.

Four questions, four functions. Everything else in the package that needs to
decide "did this change" goes through one of them (see
dev-docs/planning/开发标准-身份-文件位置-参数.md, section 1):

    file_version(path)             which revision of this file is this
    dataset_identity(manifest)     do these inputs describe the same design
    dataset_version(manifest)      which revision of that design is this
    question_digest(tool, args)    what exactly was asked

plus ``request_signature``, which labels what a caller *literally* asked for
before any normalization, so a resume can be refused before anything is
written.

All digests are sha256 truncated to 16 hex characters. They are identities, not
checksums: ``file_version`` samples size, mtime and the first 64 KiB, so a
rewrite that preserves all three goes unnoticed. That is a deliberate trade
against hashing multi-GB waveforms on every call; callers that need a hard
guarantee re-check around the operation and report a revision rather than a
promise. None of these values is an authentication token.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, Optional

from .manifest import manifest_filelist, manifest_inputs

#: Bytes sampled from the head of a file by ``file_version``.
HEAD_SAMPLE = 1 << 16
#: Length of every digest this module returns.
DIGEST_LEN = 16


def _digest(*chunks: bytes) -> str:
    h = hashlib.sha256()
    for c in chunks:
        h.update(c)
    return h.hexdigest()[:DIGEST_LEN]


def file_version(path: Optional[str], head: int = HEAD_SAMPLE) -> str:
    """Which revision of ``path`` this is: size + mtime_ns + head sample.

    Moving or renaming the file does not change it; rewriting it does. Returns
    "" when the file is absent, which reads as "this input does not exist"
    (a static session has no waveform).
    """
    if not path or not os.path.exists(path):
        return ""
    try:
        st = os.stat(path)
        with open(path, "rb") as fh:
            sample = fh.read(head)
    except OSError:
        return ""
    return _digest(f"{st.st_size}:{st.st_mtime_ns}:".encode(), sample)


def dataset_identity(manifest: Dict[str, Any], base_dir: str) -> str:
    """Do these inputs describe the same design: paths and build semantics only.

    Two manifests naming the same waveform, netlist, top and source list are the
    same dataset and may share one loaded resource, whatever the files currently
    contain. ``top``, ``scope_map`` and filelist order participate because the
    loaded readers carry annotations derived from them.
    """
    payload = dict(manifest_inputs(base_dir, manifest))
    payload.update({
        "top": manifest.get("top", ""),
        "filelist": manifest_filelist(base_dir, manifest),
        "scope_map": sorted((manifest.get("scope_map") or {}).items()),
    })
    # what the filelist declared beyond the resolved files (dropped entries,
    # -y/-v libraries): two inputs that resolve to the same files but declared
    # different things are different designs. Absent on complete filelists,
    # so their identity is unchanged.
    if manifest.get("declared_inputs"):
        payload["declared_inputs"] = manifest["declared_inputs"]
    return _digest(json.dumps(payload, sort_keys=True, default=str).encode())


def dataset_version(manifest_raw: bytes, manifest: Dict[str, Any],
                    base_dir: str) -> str:
    """Which revision of the dataset this is: manifest bytes + every input's version.

    The manifest is taken by content so rewriting an identical one is not a
    change; each named input contributes its ``file_version``. Any rewrite of
    any input (or of the manifest itself) yields a new value.
    """
    parts = [f"manifest:{_digest(manifest_raw)}"]
    for label, path in manifest_inputs(base_dir, manifest).items():
        if path:
            parts.append(f"{label}:{path}:{file_version(path)}")
    for src in manifest_filelist(base_dir, manifest):
        parts.append(f"src:{src}:{file_version(src)}")
    return _digest("|".join(parts).encode())


def question_digest(tool: str, effective: Dict[str, Any], *,
                    schema: str, timescale_exp: Optional[int] = None,
                    mode: Optional[str] = None) -> str:
    """What was asked, independent of how it was phrased.

    Includes the tool, schema version, timescale and mode because the same
    integer means a different instant under a different timescale. Excludes
    the session, the owner, the defaults revision and the origin of each value:
    those describe who asked and how, not what.
    """
    payload = {"tool": tool, "schema": schema, "timescale_exp": timescale_exp,
               "mode": mode, "args": canonical(effective)}
    return _digest(json.dumps(payload, sort_keys=True, default=str).encode())


def request_signature(kind: str, raw_args: Dict[str, Any]) -> str:
    """What the caller literally asked for, before any conversion or build.

    Built from the arguments as given (paths already normalized by the caller),
    never from what they produced: the resume check has to run before
    ``prepare_session`` overwrites the manifest it would otherwise compare to.
    Not mergeable with ``question_digest``, which takes normalized effective
    parameters after resolution.
    """
    blob = json.dumps({"kind": kind, "parts": raw_args}, sort_keys=True,
                      default=str)
    return f"{kind}:{_digest(blob.encode())}"


def cache_key(*parts: str) -> str:
    """One digest over several already-computed identities or versions.

    The sanctioned way to build a cache directory name from, say, a dataset
    identity plus a tool version: callers pass the parts, never raw stat data.
    """
    return _digest("|".join(parts).encode())


def canonical(value: Any) -> Any:
    """JSON-stable form of an argument tree: drop None, keep list order.

    Order is preserved because for signal lists it is the order of the reply.
    """
    if isinstance(value, dict):
        return {k: canonical(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [canonical(v) for v in value]
    return value


__all__ = ["HEAD_SAMPLE", "DIGEST_LEN", "file_version", "dataset_identity",
           "dataset_version", "question_digest", "request_signature",
           "cache_key", "canonical"]
