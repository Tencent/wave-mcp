#!/usr/bin/env python3
"""Deterministic fingerprint unit suite.

Covers the identity digest itself (size, mtime and head all participate), the
session-level cache, and the ``_fp`` envelope on query tools.

Run directly or via tests/run_regression.py.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)

from wave_mcp.session import open_session                           # noqa: E402
from wave_mcp.runtime.identity import file_version                    # noqa: E402
from wave_mcp import server as srv                                     # noqa: E402

PASSED, FAILED = [], []

EXAMPLES = os.path.join(HERE, "..", "..", "examples")
SAMPLE = os.path.join(EXAMPLES, "sample", "session")


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSED if cond else FAILED).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  ({detail})" if detail and not cond else ""))


def _make_session(root: str, *, fst: bool = True, maps: bool = True):
    """Build a self-contained session dir so tests never touch the repo data."""
    d = tempfile.mkdtemp(dir=root)
    manifest = {"top": "top_tb"}
    fst_path = None
    if fst:
        fst_path = os.path.join(d, "dump.fst")
        shutil.copy2(os.path.join(EXAMPLES, "sample", "dump.fst"), fst_path)
        manifest["fst_path"] = "dump.fst"
    if maps:
        shutil.copy2(os.path.join(SAMPLE, "netlist", "maps.json"),
                     os.path.join(d, "maps.json"))
        manifest["maps_path"] = "maps.json"
    with open(os.path.join(d, "session.json"), "w") as fh:
        json.dump(manifest, fh)
    return d, fst_path


def _stage_digest(tmp: str) -> None:
    print("== identity digest ==")
    p = os.path.join(tmp, "blob.bin")
    with open(p, "wb") as fh:
        fh.write(b"hello")
    h1 = file_version(p)
    check("digest is 16 hex chars", len(h1) == 16 and h1.isalnum(), h1)
    check("same file twice -> same digest", file_version(p) == h1)
    check("absent file -> empty digest",
          file_version(os.path.join(tmp, "nope.fst")) == "")

    with open(p, "ab") as fh:
        fh.write(b"more")
    h2 = file_version(p)
    check("size change -> different digest", h2 != h1, f"{h1} vs {h2}")

    # same size, restored mtime, different leading bytes: only the head differs
    with open(p, "wb") as fh:
        fh.write(b"HELLO")
    st = os.stat(p)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    h3 = file_version(p)
    check("head-only change -> different digest", h3 != h1, f"{h1} vs {h3}")

    st = os.stat(p)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
    h4 = file_version(p)
    check("mtime-only change -> different digest", h4 != h3, f"{h3} vs {h4}")


def _stage_session_fingerprint(tmp: str) -> None:
    print("== session fingerprint ==")
    d, fst = _make_session(tmp)
    s1 = open_session(d)
    fp1 = s1.input_versions()
    check("both inputs fingerprinted",
          len(fp1["wave"]) == 16 and len(fp1["netlist"]) == 16, str(fp1))
    check("repeated call is stable", s1.input_versions() == fp1, str(fp1))

    # cached: touching the file behind an open session must not change it
    st = os.stat(fst)
    os.utime(fst, ns=(st.st_atime_ns, st.st_mtime_ns + 5 * 10 ** 9))
    check("cached for the session lifetime", s1.input_versions() == fp1,
          str(s1.input_versions()))

    # a fresh open sees the new mtime: the identity really tracks the file
    s2 = open_session(d)
    check("re-opened session sees the changed waveform",
          s2.input_versions()["wave"] != fp1["wave"], str(s2.input_versions()))
    check("netlist digest untouched by a waveform change",
          s2.input_versions()["netlist"] == fp1["netlist"])

    # netlist-only (static) session: no waveform identity to report
    d3, _ = _make_session(tmp, fst=False)
    s3 = open_session(d3)
    fp3 = s3.input_versions()
    check("static session: wave is empty, netlist present",
          fp3["wave"] == "" and len(fp3["netlist"]) == 16, str(fp3))


def _stage_tool_envelope(tmp: str) -> None:
    print("== _fp envelope on tools ==")
    d, _fst = _make_session(tmp)
    sid = srv.open_session(d)["session_id"]

    a = srv.signal_values("top_tb.clk", 5, session_id=sid)
    b = srv.signal_values("top_tb.clk", 5, session_id=sid)
    check("query tool carries _fp", "_fp" in a, str(a.keys()))
    check("identical call -> identical _fp", a["_fp"] == b["_fp"], str(a["_fp"]))
    check("_fp shape is dataset/query",
          set(a["_fp"]) == {"dataset", "query"}, str(a["_fp"]))
    check("_fp.dataset carries identity, version and input versions",
          set(a["_fp"]["dataset"]) == {"identity", "version", "wave", "netlist"}
          and all(len(a["_fp"]["dataset"][k]) == 16
                  for k in ("identity", "version", "wave", "netlist")),
          str(a["_fp"]["dataset"]))
    check("a query reply also carries _query",
          isinstance(a.get("_query"), dict)
          and a["_query"].get("paths") == ["top_tb.clk"], str(a.get("_query")))

    c = srv.signal_values("top_tb.rst_n", 5, session_id=sid)
    check("different args -> same environment",
          c["_fp"]["dataset"] == a["_fp"]["dataset"])
    check("different args -> different query digest",
          c["_fp"]["query"] != a["_fp"]["query"])

    check("lifecycle tool carries no _fp",
          "_fp" not in srv.session_info(session_id=sid))
    check("another query family also annotated",
          "_fp" in srv.list_signals("top_tb", session_id=sid))

    # static session: the envelope still appears, with an empty wave id
    d2, _ = _make_session(tmp, fst=False)
    sid2 = srv.open_session(d2)["session_id"]
    out = srv.files(session_id=sid2)
    check("static session query still annotated",
          out.get("_fp", {}).get("dataset", {}).get("wave") == "",
          str(out.get("_fp")))

    srv.close_session(session_id=sid)
    srv.close_session(session_id=sid2)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="wave_fp_test_")
    print(f"== fingerprint unit suite (workdir {tmp}) ==")
    _stage_digest(tmp)
    _stage_session_fingerprint(tmp)
    _stage_tool_envelope(tmp)
    print(f"\n  fingerprint suite: {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("  failed:", FAILED)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
