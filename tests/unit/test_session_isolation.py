#!/usr/bin/env python3
"""Session isolation unit suite (v0.3.0 S2).

A work session is what a client talks to; a dataset resource is what costs
memory. This suite pins down the split and the lifetime rules around it:

- ids are random and opaque, so a path never addresses somebody's session;
- opening the same design twice gives two sessions sharing ONE loaded resource,
  because the second open must not re-parse the waveform;
- the two sessions never share query defaults, and closing one leaves the other
  working;
- a close during a running query does not destroy the reader underneath it, and
  the reader is destroyed exactly once, when the last reference goes;
- a failed load is not cached, so the retry after the file appears succeeds;
- manifests that differ in build semantics (top, netlist, scope_map) are not
  served by one shared reader;
- resuming refuses unknown ids, other owners' ids and inputs that no longer
  match, and a prepare resume is checked before anything is written;
- the listing and the ambiguity rules behave as documented.

Concurrency is driven with Events, never with sleeps: a query is parked inside
the reader so the close is guaranteed to land while it runs.

Run directly or via tests/run_regression.py.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)

from wave_mcp import server as srv                          # noqa: E402
from wave_mcp.sources.fst_source import FstSource           # noqa: E402

PASSED, FAILED = [], []

EXAMPLES = os.path.join(HERE, "..", "..", "examples")
SAMPLE = os.path.join(EXAMPLES, "sample", "session")
SAMPLE_MANIFEST = os.path.join(SAMPLE, "session.json")
SAMPLE_DUMP = os.path.join(EXAMPLES, "sample", "dump.fst")
SAMPLE_MAPS = os.path.join(SAMPLE, "netlist", "maps.json")
COUNTER_SV = os.path.join(EXAMPLES, "sample", "counter.sv")
CLK = "top_tb.clk"


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSED if cond else FAILED).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  ({detail})" if detail and not cond else ""))


def _loads() -> int:
    return srv.SESSIONS.stats()["resources"]["loads"]


def _resources() -> dict:
    return srv.SESSIONS.stats()["resources"]


def _destroys() -> int:
    return _resources()["destroys"]


def _write_manifest(directory: str, manifest: dict) -> str:
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "session.json")
    with open(path, "w") as fh:
        json.dump(manifest, fh)
    return path


def _variant(root: str, name: str, **extra) -> str:
    """A manifest that names the sample waveform (and maps, unless told not to)."""
    manifest = {"top": "top_tb", "fst_path": SAMPLE_DUMP}
    if extra.pop("maps", True):
        manifest["maps_path"] = SAMPLE_MAPS
    manifest.update(extra)
    return os.path.dirname(_write_manifest(os.path.join(root, name), manifest))


def _stage_opening(tmp: str) -> None:
    print("== two sessions, one loaded resource ==")
    before = _loads()
    first = srv.open_session(SAMPLE)
    second = srv.open_session(SAMPLE)
    a, b = first["session_id"], second["session_id"]

    check("ids are distinct", a != b, f"{a} vs {b}")
    check("ids are opaque: random, no path or design fragment",
          len(a) == 16 and a.isalnum() and "sample" not in a
          and "top" not in a, a)
    check("the second open says the resource was shared",
          second.get("resource_shared") is True,
          str(second.get("resource_shared")))
    check("both sessions report the same resource id",
          first["resource_id"] == second["resource_id"],
          f"{first.get('resource_id')} vs {second.get('resource_id')}")
    check("the resource was loaded once for both",
          _loads() == before + 1, f"{_loads()} vs {before + 1}")
    check("one live resource, two references",
          _resources()["keys"] == 1 and _resources()["refs"] == 2,
          str(_resources()))
    check("nothing destroyed yet", _resources()["destroys"] == 0,
          str(_resources()))
    check("the shared readers are the same object",
          srv.SESSIONS.get(a).resource is srv.SESSIONS.get(b).resource)

    print("== defaults are per session, close releases one reference ==")
    srv.query_defaults_set(paths=[CLK], start="10ns", session_id=a)
    check("defaults land on the session that set them",
          srv.query_defaults_get(session_id=a)["query_defaults"]["paths"] == [CLK])
    check("the other session is untouched",
          srv.query_defaults_get(session_id=b)["query_defaults"]["is_set"] is False,
          str(srv.query_defaults_get(session_id=b)["query_defaults"]))

    check("close reports disconnected",
          srv.close_session(session_id=a).get("status") == "disconnected")
    check("closing one session leaves the resource alive",
          _resources()["destroys"] == 0 and _resources()["refs"] == 1,
          str(_resources()))
    out = srv.signal_values(CLK, session_id=b)
    check("the other session still answers",
          out.get("signals", [{}])[0].get("count", 0) > 0, str(out)[:160])
    check("a closed id is not addressable",
          srv.signal_values(CLK, session_id=a).get("error_type")
          == "session_not_found")
    check("closing again reports no-such-session",
          srv.close_session(session_id=a).get("status") == "no-such-session")
    check("closing an id that never existed reports the same",
          srv.close_session(session_id="0" * 16).get("status")
          == "no-such-session")

    check("the last close destroys the resource",
          srv.close_session(session_id=b).get("status") == "disconnected"
          and _resources()["destroys"] == 1, str(_resources()))
    check("no resources held afterwards", _resources()["keys"] == 0,
          str(_resources()))


def _stage_resolution() -> None:
    print("== session resolution rules ==")
    check("a query with nothing open -> no_active_session",
          srv.signal_values(CLK).get("error_type") == "no_active_session")
    check("a bare close with nothing open -> no_active_session",
          srv.close_session().get("error_type") == "no_active_session")

    a = srv.open_session(SAMPLE)["session_id"]
    out = srv.signal_values(CLK)
    check("with exactly one session the id may be omitted",
          out.get("status") != "error"
          and out["signals"][0]["count"] > 0, str(out)[:160])
    check("a query-defaults call also resolves the only session",
          srv.query_defaults_get().get("status") == "ok")

    b = srv.open_session(SAMPLE)["session_id"]
    err = srv.signal_values(CLK)
    check("with two sessions the id is required",
          err.get("error_type") == "ambiguous_session", str(err))
    check("the refusal points at the listing instead of listing ids",
          "session_info" in (err.get("hint") or "")
          and b not in json.dumps(err), str(err))
    check("a bare close with two sessions refuses too",
          srv.close_session().get("error_type") == "ambiguous_session")
    check("an unknown id beats the ambiguity rule",
          srv.signal_values(CLK, session_id="0" * 16).get("error_type")
          == "session_not_found")
    check("session_info on an unknown id is refused the same way",
          srv.session_info(session_id="0" * 16).get("error_type")
          == "session_not_found")

    srv.close_session(session_id=a)
    srv.close_session(session_id=b)
    check("both closed", _resources()["keys"] == 0, str(_resources()))


def _stage_listing(tmp: str) -> None:
    print("== session_info(list_sessions=True) ==")
    a = srv.open_session(SAMPLE)["session_id"]
    b = srv.open_session(SAMPLE)["session_id"]
    srv.query_defaults_set(paths=[CLK], session_id=b)

    out = srv.session_info(list_sessions=True)
    ids = [s["session_id"] for s in out["sessions"]]
    check("lists every session of this owner, oldest first", ids == [a, b],
          str(ids))
    check("count agrees with the list", out["count"] == 2, str(out["count"]))
    entry = out["sessions"][0]
    check("an entry carries the manifest path",
          entry["session_path"] == os.path.abspath(SAMPLE_MANIFEST),
          str(entry.get("session_path")))
    check("an entry carries mode and resource id",
          entry["mode"] == "full" and bool(entry["resource_id"]), str(entry))
    check("per-session defaults are reported",
          entry["query_defaults"]["is_set"] is False
          and out["sessions"][1]["query_defaults"]["is_set"] is True,
          str([e["query_defaults"] for e in out["sessions"]]))
    check("listing and naming a session cannot be combined",
          srv.session_info(session_id=a, list_sessions=True).get("error_type")
          == "invalid_argument")
    check("another owner sees none of them",
          srv.SESSIONS.list_sessions("someone-else") == [])
    try:
        srv.SESSIONS.get(a, "someone-else")
        check("another owner cannot read a session by id", False, "no error")
    except srv.SessionError as exc:
        check("another owner cannot read a session by id",
              exc.error_type == "session_not_found", exc.error_type)
    check("another owner cannot close a session by id",
          srv.SESSIONS.close(a, "someone-else") is False)
    check("the session is still there after those attempts",
          srv.session_info(session_id=a).get("status") != "error")

    srv.close_session(session_id=a)
    srv.close_session(session_id=b)


def _stage_resume(tmp: str) -> None:
    print("== resume ==")
    first = srv.open_session(SAMPLE)
    a = first["session_id"]
    srv.query_defaults_set(paths=[CLK], start="10ns", session_id=a)
    before = _loads()

    again = srv.open_session(SAMPLE, session_id=a)
    check("resuming returns the same id", again["session_id"] == a)
    check("resuming keeps the defaults",
          again["query_defaults"]["paths"] == [CLK]
          and again["query_defaults"]["start"] == 10,
          str(again["query_defaults"]))
    check("resuming does not reload the data", _loads() == before,
          f"{_loads()} vs {before}")
    check("an unknown id is refused by open_session",
          srv.open_session(SAMPLE, session_id="0" * 16).get("error_type")
          == "session_not_found")

    other = _variant(tmp, "other_netlist", maps=False)
    err = srv.open_session(other, session_id=a)
    check("resuming with different inputs is refused",
          err.get("error_type") == "session_input_mismatch", str(err))
    check("the refused resume did not disturb the session",
          srv.query_defaults_get(session_id=a)["query_defaults"]["paths"]
          == [CLK])
    srv.close_session(session_id=a)

    print("== a changed input is reported, never silently served ==")
    d = os.path.join(tmp, "changing")
    fst_copy = os.path.join(d, "dump.fst")
    os.makedirs(d, exist_ok=True)
    shutil.copy2(SAMPLE_DUMP, fst_copy)
    _write_manifest(d, {"top": "top_tb", "fst_path": "dump.fst"})
    old = srv.open_session(d)
    check("the session opens on the copied waveform", bool(old["session_id"]),
          str(old)[:120])
    before = _loads()

    st = os.stat(fst_copy)
    os.utime(fst_copy, ns=(st.st_atime_ns, st.st_mtime_ns + 5 * 10 ** 9))
    err = srv.open_session(d, session_id=old["session_id"])
    check("a re-dumped waveform is reported as input_changed",
          err.get("error_type") == "input_changed", str(err))
    check("the refusing reply points at the way forward",
          "omit session_id" in (err.get("hint") or ""), str(err.get("hint")))
    check("no second load happened just for the check", _loads() == before,
          f"{_loads()} vs {before}")

    fresh = srv.open_session(d)
    check("a fresh open picks up the new version",
          fresh["session_id"] != old["session_id"]
          and fresh["resource_id"] != old["resource_id"],
          f"{fresh.get('resource_id')} vs {old.get('resource_id')}")
    check("the new version is a second resource", _loads() == before + 1,
          f"{_loads()} vs {before + 1}")
    check("the old session is still usable on its own revision",
          srv.signal_values(CLK, session_id=old["session_id"])
          .get("signals", [{}])[0].get("count", 0) > 0)
    srv.close_session(session_id=old["session_id"])
    srv.close_session(session_id=fresh["session_id"])


def _stage_inflight_close(tmp: str) -> None:
    print("== a close during a query keeps the reader alive ==")
    destroys_before = _destroys()
    sid = srv.open_session(SAMPLE)["session_id"]
    resource = srv.SESSIONS.get(sid).resource
    started, proceed = threading.Event(), threading.Event()
    original = FstSource._iter_values_multi

    def parked(self, *args, **kwargs):
        if self is resource.fst:
            started.set()
            proceed.wait(20)
        return original(self, *args, **kwargs)

    result: dict = {}
    FstSource._iter_values_multi = parked
    thread = threading.Thread(
        target=lambda: result.update(out=srv.signal_values(CLK, session_id=sid)),
        daemon=True)
    try:
        thread.start()
        check("the query reached the reader", started.wait(20))
        check("the session plus the in-flight lease hold two references",
              _resources()["refs"] == 2, str(_resources()))
        check("close succeeds while the query runs",
              srv.close_session(session_id=sid).get("status") == "disconnected")
        check("the resource is not destroyed under the query",
              _destroys() == destroys_before, str(_resources()))
        proceed.set()
        thread.join(20)
        out = result.get("out", {})
        check("the in-flight query still returned real data",
              out.get("signals", [{}])[0].get("count", 0) > 0, str(out)[:200])
        check("the resource is destroyed exactly once, after the query",
              _destroys() == destroys_before + 1 and _resources()["keys"] == 0,
              str(_resources()))
        check("the closed session is not addressable afterwards",
              srv.signal_values(CLK, session_id=sid).get("error_type")
              == "session_not_found")
    finally:
        FstSource._iter_values_multi = original
        proceed.set()
        thread.join(20)


def _stage_failed_load(tmp: str) -> None:
    print("== a failed load is not cached ==")
    d = os.path.join(tmp, "missing_fst")
    _write_manifest(d, {"top": "top_tb", "fst_path": "not-there.fst"})
    before = _loads()
    reply = srv.open_session(d)
    check("a missing waveform is a structured error, not an exception",
          reply.get("status") == "error"
          and reply.get("error_type") == "not_found"
          and "not-there.fst" in reply.get("error", ""), str(reply))
    check("the error names the parameter and says how to fix it",
          reply.get("parameter") == "session_path" and reply.get("hint"),
          str(reply))

    st = _resources()
    check("the failure left no cached entry", st["keys"] == 0, str(st))
    check("nothing is stuck in the loading state",
          st["loading"] == 0 and st["refs"] == 0, str(st))
    check("the attempt is counted, but nothing is served", _loads() == before + 1,
          f"{_loads()} vs {before + 1}")

    print("== a retry after the file appears succeeds ==")
    shutil.copy2(SAMPLE_DUMP, os.path.join(d, "not-there.fst"))
    destroys_before = _destroys()
    second = srv.open_session(d)
    check("the retry opens", second.get("status") == "connected",
          str(second)[:160])
    check("the retry really loaded", _loads() == before + 2, f"{_loads()}")
    check("exactly one resource is now live", _resources()["keys"] == 1,
          str(_resources()))
    srv.close_session(session_id=second["session_id"])
    check("closing it destroys the resource",
          _destroys() == destroys_before + 1, str(_resources()))


def _stage_not_shared(tmp: str) -> None:
    print("== different build semantics are different resources ==")
    before = _loads()
    same_a = _variant(tmp, "same_a")
    same_b = _variant(tmp, "same_b")
    other_top = _variant(tmp, "other_top", top="TOP_ELSEWHERE")
    other_maps = _variant(tmp, "other_maps", maps=False)
    scoped = _variant(tmp, "scoped",
                      scope_map={"top_tb.u_counter": "counter"})

    sa = srv.open_session(same_a)["session_id"]
    sb = srv.open_session(same_b)["session_id"]
    check("identical manifests share one resource", _loads() == before + 1,
          f"{_loads()} vs {before + 1}")

    st = srv.open_session(other_top)["session_id"]
    check("a different top is a different resource", _loads() == before + 2,
          f"{_loads()}")
    sm = srv.open_session(other_maps)["session_id"]
    check("a different netlist is a different resource", _loads() == before + 3,
          f"{_loads()}")
    ss = srv.open_session(scoped)["session_id"]
    check("a different scope_map is a different resource",
          _loads() == before + 4, f"{_loads()}")
    check("four distinct resources are live, five sessions on top",
          _resources()["keys"] == 4 and _resources()["refs"] == 5,
          str(_resources()))
    check("each session answers from its own data",
          all(srv.signal_values(CLK, session_id=s)
              .get("signals", [{}])[0].get("count", 0) > 0
              for s in (sa, sb, st, sm, ss)))

    for sid in (sa, sb, st, sm, ss):
        srv.close_session(session_id=sid)
    check("closing all releases every resource", _resources()["keys"] == 0,
          str(_resources()))


def _stage_prepare_resume(tmp: str) -> None:
    print("== a prepare resume is checked before anything is written ==")
    sid = srv.open_session(SAMPLE)["session_id"]
    target = os.path.join(tmp, "prepared")
    refused = srv.open_static_session(out_dir=target, top="counter",
                                      filelist=[COUNTER_SV], session_id=sid)
    check("a mismatched resume is refused with the reason",
          refused.get("error_type") == "session_input_mismatch", str(refused))
    check("nothing was written before the refusal",
          not os.path.exists(os.path.join(target, "session.json")),
          str(os.listdir(target) if os.path.isdir(target) else "no dir"))
    srv.close_session(session_id=sid)

    print("== the same inputs resume, and keep the session's defaults ==")
    first = srv.open_static_session(out_dir=target, top="counter",
                                    filelist=[COUNTER_SV])
    check("the static session opens", first.get("mode") == "static",
          str(first)[:120])
    srv.query_defaults_set(paths=["counter.count"], session_id=first["session_id"])
    loads_before = _loads()
    again = srv.open_static_session(out_dir=target, top="counter",
                                    filelist=[COUNTER_SV],
                                    session_id=first["session_id"])
    check("re-preparing with the same inputs resumes the session",
          again.get("session_id") == first["session_id"], str(again)[:160])
    check("the session keeps its defaults across a re-prepare",
          again["query_defaults"]["paths"] == ["counter.count"],
          str(again.get("query_defaults")))
    check("re-preparing reuses the loaded resource",
          _loads() == loads_before, f"{_loads()} vs {loads_before}")
    srv.close_session(session_id=first["session_id"])


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="wave_sess_test_")
    print(f"== session isolation suite (workdir {tmp}) ==")
    try:
        check("the suite starts with no sessions open",
              srv.SESSIONS.stats()["sessions"] == 0,
              str(srv.SESSIONS.stats()))
        _stage_opening(tmp)
        _stage_resolution()
        _stage_listing(tmp)
        _stage_resume(tmp)
        _stage_inflight_close(tmp)
        _stage_failed_load(tmp)
        _stage_not_shared(tmp)
        _stage_prepare_resume(tmp)
    finally:
        for entry in srv.SESSIONS.list_sessions():
            srv.close_session(session_id=entry["session_id"])
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n  session isolation suite: {len(PASSED)} passed, "
          f"{len(FAILED)} failed")
    if FAILED:
        print("  failed:", FAILED)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
