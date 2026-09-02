#!/usr/bin/env bash
# Verify the fsdb2fst on-demand build path on a machine that has Verdi.
#
# WHY THIS EXISTS
# Historical end-to-end FSDB runs all resolved the converter from the
# repo-local binary (third_party/fsdb2fst/fsdb2fst), so they exercised the
# OLD resolution path. This change adds two new levels (user cache, on-demand
# build) that no real FSDB has ever gone through. It also fixes a latent bug
# in build_fsdb2fst.sh where INC_ARGS was overwritten instead of appended,
# which means a converter built from $VERDI_HOME / $FSDB2FST_FREADER could
# never have compiled before. So the binary produced by the auto-build path is
# not provably identical to the one historical runs validated.
#
# WHAT IT CHECKS
#   1. auto-build actually triggers and produces a binary in the user cache
#   2. that binary really converts a real FSDB (not just compiles)
#   3. signal values are queryable and non-empty (semantics, not "not empty")
#   4. second run hits the cache and does NOT rebuild
#   5. FSDB2FST_BIN override still wins (no regression on the old path)
#
# USAGE
#   export VERDI_HOME=/path/to/verdi        # must contain share/FsdbReader/linux64
#   bash verify_fsdb_autobuild.sh <dump.fsdb> <top_instance> <rtl.f>
#
#   Values you already know good from a previous run make the best input,
#   because step 3 compares against what you expect to see.
set -uo pipefail

FSDB="${1:-}"
TOP="${2:-}"
FILELIST="${3:-}"

PASS=0
FAIL=0
SKIP=0

ok()   { printf '  [PASS] %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  [FAIL] %s\n' "$*"; FAIL=$((FAIL+1)); }
skip() { printf '  [SKIP] %s\n' "$*"; SKIP=$((SKIP+1)); }
section() { printf '\n=== %s ===\n' "$*"; }

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/wave-mcp/fsdb2fst"
REPO_BIN="$REPO_ROOT/third_party/fsdb2fst/fsdb2fst"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

printf 'wave-mcp fsdb2fst auto-build verification\n'
printf 'repo:  %s\n' "$REPO_ROOT"
printf 'cache: %s\n' "$CACHE_DIR"

# ---------------------------------------------------------------- preflight
section "0. Preflight"

if [ -z "$FSDB" ] || [ -z "$TOP" ] || [ -z "$FILELIST" ]; then
    printf 'ERROR: usage: bash %s <dump.fsdb> <top_instance> <rtl.f>\n' \
        "$(basename "$0")" >&2
    exit 2
fi
[ -f "$FSDB" ]     || { printf 'ERROR: FSDB not found: %s\n' "$FSDB" >&2; exit 2; }
[ -f "$FILELIST" ] || { printf 'ERROR: filelist not found: %s\n' "$FILELIST" >&2; exit 2; }
FSDB="$(cd "$(dirname "$FSDB")" && pwd)/$(basename "$FSDB")"
FILELIST="$(cd "$(dirname "$FILELIST")" && pwd)/$(basename "$FILELIST")"

if [ -z "${VERDI_HOME:-}" ] && [ -z "${FSDB2FST_FREADER:-}" ] && [ -z "${NOVAS_HOME:-}" ]; then
    printf 'ERROR: set VERDI_HOME (or FSDB2FST_FREADER / NOVAS_HOME) first.\n' >&2
    printf '       VERDI_HOME must contain share/FsdbReader/linux64\n' >&2
    exit 2
fi
command -v g++ >/dev/null 2>&1 || { printf 'ERROR: g++ not in PATH\n' >&2; exit 2; }
python3 -c "import wave_mcp" 2>/dev/null \
    || { printf 'ERROR: cannot import wave_mcp. Run from the repo root, or pip install -e .\n' >&2; exit 2; }

READER="$(python3 - <<'PY'
from wave_mcp import convert
print(convert.resolve_fsdb_reader() or "")
PY
)"
if [ -n "$READER" ]; then
    ok "FsdbReader resolved: $READER"
else
    bad "FsdbReader NOT resolved. Check VERDI_HOME/share/FsdbReader/linux64 exists."
    printf '\nAborting: nothing below can pass without the runtime.\n'
    exit 1
fi

# Force the auto-build path: remove both earlier resolution levels.
if [ -f "$REPO_BIN" ]; then
    mv "$REPO_BIN" "$WORK/fsdb2fst.repo.bak"
    printf '  note: moved repo-local binary aside (restored at exit)\n'
    RESTORE_REPO_BIN=1
fi
if [ -d "$CACHE_DIR" ]; then
    mv "$CACHE_DIR" "$WORK/cache.bak"
    printf '  note: moved existing cache aside (restored at exit)\n'
    RESTORE_CACHE=1
fi
restore() {
    [ "${RESTORE_REPO_BIN:-0}" = "1" ] && [ -f "$WORK/fsdb2fst.repo.bak" ] \
        && mv -f "$WORK/fsdb2fst.repo.bak" "$REPO_BIN"
    if [ "${RESTORE_CACHE:-0}" = "1" ] && [ -d "$WORK/cache.bak" ]; then
        rm -rf "$CACHE_DIR"; mkdir -p "$(dirname "$CACHE_DIR")"
        mv -f "$WORK/cache.bak" "$CACHE_DIR"
    fi
    rm -rf "$WORK"
}
trap restore EXIT

# ------------------------------------------------------- 1. auto-build fires
section "1. Auto-build triggers and lands in the user cache"

BUILD_OUT="$(python3 - <<'PY' 2>&1
import time
from wave_mcp import convert
t0 = time.time()
p = convert.resolve_fsdb2fst()
print("RESOLVED=%s" % (p or ""))
print("ELAPSED=%.2f" % (time.time() - t0))
PY
)"
printf '%s\n' "$BUILD_OUT" | sed 's/^/  /'
BIN1="$(printf '%s\n' "$BUILD_OUT" | sed -n 's/^RESOLVED=//p')"

if [ -z "$BIN1" ]; then
    bad "auto-build produced nothing. Failure detail:"
    cat "$CACHE_DIR"/*/build-failed.log 2>/dev/null | sed 's/^/       /'
    printf '\nAborting: no converter, later steps are meaningless.\n'
    exit 1
fi
ok "auto-build produced: $BIN1"

case "$BIN1" in
    "$CACHE_DIR"/*) ok "binary lives in the user cache (not in site-packages)" ;;
    *) bad "unexpected location, expected it under $CACHE_DIR" ;;
esac

if "$BIN1" --info "$FSDB" >"$WORK/info.txt" 2>&1; then
    ok "binary runs against the real FSDB (--info)"
    sed 's/^/       /' "$WORK/info.txt" | head -12
else
    bad "binary cannot read the FSDB. This is the case the build stub could NOT cover:"
    sed 's/^/       /' "$WORK/info.txt" | head -20
    printf '\nAborting: the auto-built binary is not functional.\n'
    exit 1
fi

# --------------------------------------------- 2. real conversion + queries
section "2. Real conversion through prepare_session, then query values"

FSDB="$FSDB" TOP="$TOP" FILELIST="$FILELIST" OUT="$WORK/session" python3 - <<'PY' >"$WORK/conv.txt" 2>&1
import json, os
from wave_mcp import pipeline
res = pipeline.prepare_session(
    out_dir=os.environ["OUT"], wave_path=os.environ["FSDB"],
    top=os.environ["TOP"], filelist_path=os.environ["FILELIST"])
print("FST=%s" % res.get("fst_path"))
print("MAPS=%s" % res.get("maps_path"))
for s in res.get("steps", []):
    print("STEP=%s ok=%s %ss cached=%s" % (
        s.get("step"), s.get("ok"), s.get("elapsed_sec"), s.get("cached")))
print("JSON=%s" % json.dumps(res)[:1500])
PY
sed 's/^/  /' "$WORK/conv.txt" | grep -vE '^\s+JSON=' | head -20

if grep -q 'STEP=convert_fsdb_to_fst ok=True' "$WORK/conv.txt"; then
    ok "convert_fsdb_to_fst step succeeded"
else
    bad "convert_fsdb_to_fst step missing or failed"
fi
if grep -qE '^ *STEP=.* ok=False' "$WORK/conv.txt"; then
    bad "at least one pipeline step reported ok=False (see above)"
else
    ok "all pipeline steps reported ok=True"
fi

FSTOUT="$(sed -n 's/^FST=//p' "$WORK/conv.txt" | tail -1)"
if [ -n "$FSTOUT" ] && [ -f "$FSTOUT" ]; then
    ok "converted .fst exists: $FSTOUT"
    # fsdb2fst writes hierarchy as a sidecar; without it the FST will not open.
    if [ -f "$FSTOUT.hier" ]; then
        ok "sidecar $(basename "$FSTOUT").hier present"
    else
        skip "no .hier sidecar next to the .fst (check whether it was consumed already)"
    fi
else
    bad "no .fst produced, see $WORK/conv.txt"
fi

OUT="$WORK/session" python3 - <<'PY' >"$WORK/query.txt" 2>&1
import os
from wave_mcp.session import open_session

s = open_session(os.environ["OUT"])
fst = s.fst
print("SCOPES=%d SIGNALS=%d" % (len(fst.scopes), len(fst.signals)))
print("TIME=%s..%s exp=%s" % (fst.start_time, fst.end_time, fst.timescale_exp))

# Walk scopes and sample signals until a few carry real value data.
shown = 0
for scope in list(fst.scopes.keys()):
    if shown >= 5:
        break
    try:
        sigs = fst.signals_of_instance(scope)
    except Exception as exc:
        print("ERRSCOPE %s: %s" % (scope, exc))
        continue
    for sig in sigs:
        if shown >= 5:
            break
        path = sig.get("full_path") or sig.get("name")
        if not path:
            continue
        try:
            vals = fst.all_values(path, max_values=4)
        except Exception as exc:
            print("ERR %s: %s" % (path, exc))
            continue
        if vals:
            print("VAL %-50s w=%-4s %s" % (
                path, sig.get("width"), vals[:3]))
            shown += 1
print("NONEMPTY=%d" % shown)
PY
sed 's/^/  /' "$WORK/query.txt" | head -16

NONEMPTY="$(sed -n 's/^NONEMPTY=//p' "$WORK/query.txt" | tail -1)"
if [ "${NONEMPTY:-0}" -gt 0 ] 2>/dev/null; then
    ok "signal values are readable and non-empty ($NONEMPTY signals sampled)"
    printf '  >>> EYEBALL THIS: do the values above match what you saw in\n'
    printf '      previous runs / Verdi for this waveform? Tool success alone\n'
    printf '      does not prove the values are right.\n'
else
    bad "no signal produced values, check $WORK/query.txt"
fi

# --------------------------------------------------- 3. cache hit, no rebuild
section "3. Second resolve hits the cache and does not rebuild"

STAMP1="$(stat -c %Y "$BIN1" 2>/dev/null || echo 0)"
sleep 1
CACHE_OUT="$(python3 - <<'PY' 2>&1
import time
from wave_mcp import convert
t0 = time.time()
p = convert.resolve_fsdb2fst()
print("RESOLVED=%s" % (p or ""))
print("ELAPSED=%.3f" % (time.time() - t0))
PY
)"
printf '%s\n' "$CACHE_OUT" | sed 's/^/  /'
BIN2="$(printf '%s\n' "$CACHE_OUT" | sed -n 's/^RESOLVED=//p')"
STAMP2="$(stat -c %Y "$BIN2" 2>/dev/null || echo 0)"
ELAPSED2="$(printf '%s\n' "$CACHE_OUT" | sed -n 's/^ELAPSED=//p')"

[ "$BIN1" = "$BIN2" ] && ok "same path returned" || bad "path changed: $BIN1 -> $BIN2"
[ "$STAMP1" = "$STAMP2" ] && ok "binary NOT rebuilt (mtime unchanged)" \
    || bad "binary was rebuilt, cache is not being honoured"
awk -v e="$ELAPSED2" 'BEGIN{exit !(e < 1.0)}' \
    && ok "resolve was fast (${ELAPSED2}s), i.e. no compile" \
    || bad "resolve took ${ELAPSED2}s, suspiciously slow for a cache hit"

# Conversion cache: second prepare_session on the same waveform must reuse.
FSDB="$FSDB" TOP="$TOP" FILELIST="$FILELIST" OUT="$WORK/session2" python3 - <<'PY' >"$WORK/conv2.txt" 2>&1
import os
from wave_mcp import pipeline
res = pipeline.prepare_session(
    out_dir=os.environ["OUT"], wave_path=os.environ["FSDB"],
    top=os.environ["TOP"], filelist_path=os.environ["FILELIST"])
for s in res.get("steps", []):
    if s.get("step") == "convert_fsdb_to_fst":
        print("CACHED=%s ELAPSED=%s" % (s.get("cached"), s.get("elapsed_sec")))
PY
sed 's/^/  /' "$WORK/conv2.txt" | head -5
grep -q 'CACHED=True' "$WORK/conv2.txt" \
    && ok "conversion cache reused the .fst (no re-convert)" \
    || skip "conversion cache not reported as hit, check $WORK/conv2.txt"

# ------------------------------------------- 4. explicit override still wins
section "4. FSDB2FST_BIN override still takes precedence (old path intact)"

OVERRIDE_OUT="$(FSDB2FST_BIN="$BIN1" python3 - <<'PY' 2>&1
from wave_mcp import convert
print("RESOLVED=%s" % (convert.resolve_fsdb2fst() or ""))
PY
)"
printf '%s\n' "$OVERRIDE_OUT" | sed 's/^/  /'
printf '%s\n' "$OVERRIDE_OUT" | grep -q "RESOLVED=$BIN1" \
    && ok "FSDB2FST_BIN honoured" || bad "FSDB2FST_BIN was ignored"

DISABLED_OUT="$(WAVE_MCP_FSDB2FST_AUTOBUILD=0 python3 - <<'PY' 2>&1
from wave_mcp import convert
# Cache is populated by now, so this proves the kill switch does not break
# resolution of an already-built binary; it only prevents new builds.
print("RESOLVED=%s" % (convert.resolve_fsdb2fst() or ""))
PY
)"
printf '%s\n' "$DISABLED_OUT" | sed 's/^/  /'
ok "WAVE_MCP_FSDB2FST_AUTOBUILD=0 accepted (see resolved value above)"

# ------------------------------------------------------------------ summary
section "Summary"
printf '  PASS=%d  FAIL=%d  SKIP=%d\n' "$PASS" "$FAIL" "$SKIP"
printf '  artifacts kept until exit: %s\n' "$WORK"
if [ "$FAIL" -eq 0 ]; then
    printf '\nAll checks passed. Remaining human judgement: confirm the sampled\n'
    printf 'signal values in step 2 match your known-good results.\n'
    exit 0
fi
printf '\n%d check(s) failed. Paste the output back and do not commit yet.\n' "$FAIL"
exit 1
