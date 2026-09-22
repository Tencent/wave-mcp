#!/usr/bin/env python3
"""Storage policy and identity standard: the guardrails from
dev-docs/planning/开发标准-身份-文件位置-参数.md.

Covers
  * source-level: hashlib only in runtime/identity.py, expanduser/XDG only in
    runtime/storage.py;
  * identity: the four digests answer their own question and nothing else;
  * storage: read-only input dirs get zero writes, two processes converting
    the same key build once, a corrupt cache heals itself, an explicit out_dir
    is never moved and an omitted one lands at the inputs' identity under the
    session root;
  * manifest: session.json records its caches and session_info reports them.

Run directly or via tests/run_regression.py.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import re
import shutil
import stat
import sys
import tempfile
from typing import Dict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

from wave_mcp.runtime import identity, storage                          # noqa: E402
from wave_mcp.runtime.storage import StoragePolicy                       # noqa: E402

PASSED, FAILED = [], []
EXAMPLES = os.path.join(ROOT, "examples")
FOURSTATE_VCD = os.path.join(ROOT, "tests", "fourstate", "sim", "fourstate.vcd")


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSED if cond else FAILED).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  ({detail})" if detail and not cond else ""))


class _env:
    """Temporarily set environment variables."""

    def __init__(self, **kv: str) -> None:
        self.kv, self.old = kv, {}

    def __enter__(self):
        for k, v in self.kv.items():
            self.old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# -- 1. source-level guardrails ----------------------------------------------
def _py_files():
    for dirpath, _dirs, files in os.walk(os.path.join(ROOT, "wave_mcp")):
        if "__pycache__" in dirpath:
            continue
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(dirpath, f)


def _stage_source_rules() -> None:
    print("== source-level rules ==")
    hashlib_hits, path_hits = [], []
    for path in _py_files():
        rel = os.path.relpath(path, ROOT)
        with open(path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                code = line.split("#", 1)[0]
                if re.search(r"\bimport hashlib\b|\bhashlib\.", code):
                    hashlib_hits.append(f"{rel}:{lineno}")
                if re.search(r"expanduser\(|os\.environ\.get\(\s*[\"']XDG_|Path\.home\(\)", code):
                    path_hits.append(f"{rel}:{lineno}")
    check("hashlib only in runtime/identity.py",
          all(h.startswith("wave_mcp/runtime/identity.py") for h in hashlib_hits),
          str(hashlib_hits))
    check("expanduser/XDG/Path.home only in runtime/storage.py",
          all(h.startswith("wave_mcp/runtime/storage.py") for h in path_hits),
          str(path_hits))


# -- 2. identity digests -------------------------------------------------------
def _write(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


def _stage_identity(tmp: str) -> None:
    print("== identity digests ==")
    a = os.path.join(tmp, "a.bin")
    _write(a, b"x" * 100)
    v1 = identity.file_version(a)
    check("file_version is 16 hex", len(v1) == 16 and int(v1, 16) >= 0, v1)
    moved = os.path.join(tmp, "moved.bin")
    shutil.copy2(a, moved)
    check("file_version survives rename/copy with mtime",
          identity.file_version(moved) == v1)
    _write(a, b"y" * 100)
    check("file_version changes on rewrite", identity.file_version(a) != v1)
    check("file_version of absent file is empty string",
          identity.file_version(os.path.join(tmp, "nope")) == "")

    base = tmp
    m1 = {"top": "t", "fst_path": "a.bin", "maps_path": "m.json", "filelist": ["s.sv"]}
    m2 = dict(m1)
    m2b = dict(m1, top="u")
    _write(os.path.join(tmp, "m.json"), b"{}")
    _write(os.path.join(tmp, "s.sv"), b"module s; endmodule")
    id1 = identity.dataset_identity(m1, base)
    check("dataset_identity equal for identical manifests",
          identity.dataset_identity(m2, base) == id1)
    check("dataset_identity differs on top", identity.dataset_identity(m2b, base) != id1)
    raw = json.dumps(m1).encode()
    ver1 = identity.dataset_version(raw, m1, base)
    _write(a, b"z" * 100)
    ver2 = identity.dataset_version(raw, m1, base)
    check("dataset_version changes when an input is rewritten", ver2 != ver1)
    check("dataset_identity does not change when an input is rewritten",
          identity.dataset_identity(m1, base) == id1)
    check("rewriting the manifest to identical bytes keeps the version",
          identity.dataset_version(raw, json.loads(raw), base) == ver2)

    q1 = identity.question_digest("t", {"paths": ["a"], "start": 1, "x": None}, schema="s")
    q2 = identity.question_digest("t", {"paths": ["a"], "start": 1}, schema="s")
    q3 = identity.question_digest("t", {"paths": ["b"], "start": 1}, schema="s")
    check("question_digest ignores None-valued args", q1 == q2)
    check("question_digest differs on a different question", q3 != q1)
    check("question_digest depends on timescale",
          identity.question_digest("t", {}, schema="s", timescale_exp=-9)
          != identity.question_digest("t", {}, schema="s", timescale_exp=-12))
    sig = identity.request_signature("prepare", {"wave": "/x"})
    check("request_signature is kind-prefixed",
          sig.startswith("prepare:") and len(sig) == len("prepare:") + 16, sig)
    check("cache_key is deterministic",
          identity.cache_key("a", "b") == identity.cache_key("a", "b")
          and identity.cache_key("a", "b") != identity.cache_key("b", "a"))


# -- 3. storage policy ---------------------------------------------------------
def _stage_session_dir(tmp: str) -> None:
    print("== session_dir ==")
    from wave_mcp import pipeline
    root = os.path.join(tmp, "root")
    counter = os.path.join(EXAMPLES, "sample", "counter.sv")
    tb = os.path.join(EXAMPLES, "sample", "top_tb.sv")
    with _env(WAVE_MCP_SESSION_ROOT=root):
        pol = StoragePolicy()
        explicit = os.path.join(tmp, "x")
        check("explicit out_dir used as given",
              pol.session_dir(explicit, "deadbeef") == explicit)
        check("explicit out_dir is not moved under the root",
              pol.session_dir("/proj/a/sess", "deadbeef") == "/proj/a/sess")
        d1 = pipeline.resolve_out_dir(None, wave_path=None, top="top_tb",
                                      files=[counter, tb])
        d2 = pipeline.resolve_out_dir(None, wave_path=None, top="top_tb",
                                      files=[counter, tb])
        d3 = pipeline.resolve_out_dir(None, wave_path=None, top="other",
                                      files=[counter, tb])
        d4 = pipeline.resolve_out_dir(None, wave_path="/w/dump.fst",
                                      top="top_tb", files=[counter, tb])
        check("omitted out_dir lands under the session root",
              d1.startswith(root + os.sep), d1)
        check("same inputs resolve to one place", d1 == d2)
        check("different top is a different place", d1 != d3)
        check("waveform session and static session differ", d1 != d4)
        check("directory name is the 16-hex identity",
              re.fullmatch(r"[0-9a-f]{16}", os.path.basename(d1)) is not None, d1)
        check("waveform session's netlist lives at the static identity",
              pipeline.netlist_home(d4, False, "top_tb", [counter, tb]) == d1)
        check("explicit out_dir keeps its netlist inside",
              pipeline.netlist_home(explicit, True, "top_tb", [counter, tb]) == explicit)
    with _env(WAVE_MCP_SESSION_ROOT=None):
        d = StoragePolicy().session_dir(None, "0123456789abcdef")
        check("no env: ~/.wave-mcp/sessions",
              d == os.path.join(os.path.expanduser("~"), ".wave-mcp",
                                "sessions", "0123456789abcdef"), d)


def _stage_cache_root(tmp: str) -> None:
    print("== cache root resolution ==")
    with _env(WAVE_MCP_CACHE_ROOT=os.path.join(tmp, "explicit")):
        check("WAVE_MCP_CACHE_ROOT wins",
              storage.policy().cache_root == os.path.join(tmp, "explicit"))
    with _env(WAVE_MCP_CACHE_ROOT=None):
        check("default is ~/.wave-mcp/cache",
              storage.policy().cache_root
              == os.path.join(os.path.expanduser("~"), ".wave-mcp", "cache"))
    d = StoragePolicy(cache_root=os.path.join(tmp, "c")).cache_dir("fst", "k1")
    check("cache_dir creates <root>/<kind>/<digest>",
          os.path.isdir(d) and os.path.basename(os.path.dirname(d)) == "fst"
          and len(os.path.basename(d)) == 16, d)
    check("user_path expands ~ and makes absolute",
          storage.user_path("~/x") == os.path.join(os.path.expanduser("~"), "x"))
    check("user_path home_relative anchors relative on $HOME",
          storage.user_path("rel/x", home_relative=True)
          == os.path.join(os.path.expanduser("~"), "rel", "x"))


def _stage_atomic(tmp: str) -> None:
    print("== atomic write ==")
    target = os.path.join(tmp, "atomic", "f.json")
    StoragePolicy.atomic_write_bytes(target, b"{}")
    check("atomic_write_bytes creates parents and file",
          os.path.exists(target) and open(target, "rb").read() == b"{}")
    leftovers = [f for f in os.listdir(os.path.dirname(target)) if f.startswith(".tmp-")]
    check("no temp file left behind", not leftovers, str(leftovers))

    def boom(_tmp: str) -> None:
        raise RuntimeError("writer failed")
    try:
        StoragePolicy.atomic_write(target, boom)
    except RuntimeError:
        pass
    check("failed writer leaves the old file intact",
          open(target, "rb").read() == b"{}")
    leftovers = [f for f in os.listdir(os.path.dirname(target)) if f.startswith(".tmp-")]
    check("failed writer leaves no temp file", not leftovers, str(leftovers))
    StoragePolicy.discard(target)
    StoragePolicy.discard(target)
    check("discard is idempotent", not os.path.exists(target))


def _convert_in_child(args) -> Dict:
    vcd, cache_root = args
    os.environ["WAVE_MCP_CACHE_ROOT"] = cache_root
    sys.path.insert(0, ROOT)
    from wave_mcp import convert
    got = convert.cached_fst(vcd, kind="vcd")
    return {"fst": got["fst_path"], "cached": got["cached"], "pid": os.getpid()}


def _stage_conversion_cache(tmp: str) -> None:
    print("== conversion cache ==")
    if not os.path.exists(FOURSTATE_VCD):
        print("  [SKIP] fourstate VCD not available")
        return
    from wave_mcp import convert
    src_dir = os.path.join(tmp, "ro-src")
    os.makedirs(src_dir)
    vcd = os.path.join(src_dir, "w.vcd")
    shutil.copy(FOURSTATE_VCD, vcd)
    cache_root = os.path.join(tmp, "cache")
    before = set(os.listdir(src_dir))
    os.chmod(src_dir, stat.S_IRUSR | stat.S_IXUSR)
    try:
        with _env(WAVE_MCP_CACHE_ROOT=cache_root):
            got = convert.cached_fst(vcd, kind="vcd")
    finally:
        os.chmod(src_dir, stat.S_IRWXU)
    check("read-only source dir: conversion succeeds", os.path.exists(got["fst_path"]))
    check("read-only source dir: zero writes next to the source",
          set(os.listdir(src_dir)) == before, str(os.listdir(src_dir)))
    check("artifact lives under <cache_root>/fst/",
          got["fst_path"].startswith(os.path.join(cache_root, "fst") + os.sep))
    check("conversion record beside the artifact",
          os.path.exists(os.path.join(got["cache_dir"], "conversion.json")))

    with _env(WAVE_MCP_CACHE_ROOT=cache_root):
        again = convert.cached_fst(vcd, kind="vcd")
    check("second call is a cache hit", again["cached"] and again["fst_path"] == got["fst_path"])

    # corrupt record -> self-heal
    rec = os.path.join(got["cache_dir"], "conversion.json")
    _write(rec, b"not json")
    with _env(WAVE_MCP_CACHE_ROOT=cache_root):
        healed = convert.cached_fst(vcd, kind="vcd")
    check("corrupt record: reconverted, no error",
          healed["cached"] is False and json.load(open(rec)).get("source") == vcd)

    # rewritten source -> invalidated
    with open(vcd, "ab") as fh:
        fh.write(b"\n")
    with _env(WAVE_MCP_CACHE_ROOT=cache_root):
        fresh = convert.cached_fst(vcd, kind="vcd")
    check("rewritten source: cache invalidated", fresh["cached"] is False)

    # two processes, same key: both succeed, exactly one converts
    cache2 = os.path.join(tmp, "cache2")
    ctx = mp.get_context("fork")
    with ctx.Pool(2) as pool:
        results = pool.map(_convert_in_child, [(vcd, cache2), (vcd, cache2)])
    converted = [r for r in results if not r["cached"]]
    check("two concurrent processes: both got the same artifact",
          results[0]["fst"] == results[1]["fst"], str(results))
    check("two concurrent processes: exactly one converted",
          len(converted) == 1, str(results))


def _stage_manifest_caches(tmp: str) -> None:
    print("== manifest caches ==")
    if not os.path.exists(FOURSTATE_VCD):
        print("  [SKIP] fourstate VCD not available")
        return
    from wave_mcp import pipeline
    from wave_mcp import server as srv
    vcd = os.path.join(tmp, "m.vcd")
    shutil.copy(FOURSTATE_VCD, vcd)
    cache_root = os.path.join(tmp, "cache3")
    with _env(WAVE_MCP_CACHE_ROOT=cache_root):
        out = pipeline.prepare_session(os.path.join(tmp, "sess"), vcd, top="",
                                       filelist=None)
        manifest = json.load(open(out["manifest"]))
        check("manifest records fst_version",
              len(manifest.get("fst_version") or "") == 16, str(manifest.get("fst_version")))
        check("manifest records the caller's wave_path",
              manifest.get("wave_path") == os.path.abspath(vcd))
        check("manifest lists the converted fst as a cache",
              any(c["kind"] == "fst" and c["path"] == manifest["fst_path"]
                  for c in manifest.get("caches", [])), str(manifest.get("caches")))
        check("no legacy fst_hash/filelist_hash",
              "fst_hash" not in manifest and "filelist_hash" not in manifest)
        sid = srv.open_session(out["session_path"])["session_id"]
        info = srv.session_info(session_id=sid)
        check("session_info reports caches with valid=True",
              info.get("caches") and all(c["valid"] for c in info["caches"]),
              str(info.get("caches")))
        srv.close_session(session_id=sid)

        # legacy manifest with fst_hash: opens without a false mismatch warning
        legacy_dir = os.path.join(tmp, "legacy")
        os.makedirs(legacy_dir)
        legacy = {"top": "top_tb",
                  "fst_path": os.path.join(EXAMPLES, "sample", "dump.fst"),
                  "fst_hash": "deadbeef" * 5}
        json.dump(legacy, open(os.path.join(legacy_dir, "session.json"), "w"))
        sid = srv.open_session(legacy_dir)["session_id"]
        info = srv.session_info(session_id=sid)
        check("legacy fst_hash is ignored, no mismatch warning",
              not any("fingerprint mismatch" in w for w in info.get("warnings", [])),
              str(info.get("warnings")))
        srv.close_session(session_id=sid)

        # netlist msgpack cache lands in the cache root, not beside maps.json
        sample = os.path.join(EXAMPLES, "sample", "session")
        maps = os.path.join(sample, "netlist", "maps.json")
        from wave_mcp.sources import rtl_source
        if rtl_source._msgpack is not None and os.path.exists(maps):
            rtl_source._load_maps_json(maps)
            check("no msgpack sidecar beside maps.json",
                  not os.path.exists(maps + ".msgpack"))
            check("msgpack cache under <cache_root>/netlist-cache/",
                  os.path.exists(rtl_source._maps_cache_path(maps))
                  and rtl_source._maps_cache_path(maps).startswith(
                      os.path.join(cache_root, "netlist-cache")))
        else:
            print("  [SKIP] msgpack not installed or sample netlist missing")


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="wave-mcp-storage-")
    try:
        _stage_source_rules()
        _stage_identity(os.path.join(tmp, "id") if os.makedirs(os.path.join(tmp, "id")) is None else "")
        _stage_session_dir(tmp)
        _stage_cache_root(tmp)
        _stage_atomic(tmp)
        _stage_conversion_cache(tmp)
        _stage_manifest_caches(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n  storage-policy suite: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("  failed:", ", ".join(FAILED))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
