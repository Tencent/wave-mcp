#!/usr/bin/env bash
# build_and_smoke.sh : one-shot fsdb2fst build + smoke test for a machine
# with a real Verdi installation.
#
# Usage:
#   bash build_and_smoke.sh [VERDI_HOME]
#     - VERDI_HOME optional if the env var is already set correctly
#     - run from anywhere; the script locates itself
#
# What it does (each step prints its own PASS/FAIL summary at the end):
#   1. locate $VERDI_HOME/share/FsdbReader (linux64 libs + ffrAPI.h)
#   2. build third_party/fsdb2fst/fsdb2fst  (g++, links -lnffr -lnsys -lz)
#   3. smoke: --help, --info, license check during a real conversion
#   4. full conversion of an FSDB you pass as env FSDB2FST_SAMPLE (optional)
#   5. prints PASS/FAIL table + what to copy back for round-2 verification
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="$HERE/fsdb2fst"   # self-contained package layout
mkdir -p "$SRC_DIR" 2>/dev/null || true

log()  { printf '\n\033[1m[build_and_smoke] %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; }
note() { printf '  \033[33mNOTE\033[0m %s\n' "$*"; }

RESULT_LINES=()
record() { RESULT_LINES+=("$1"); }

# ---- step 1: locate FsdbReader ---------------------------------------------
log "step 1: locate FsdbReader"
VH="${1:-${VERDI_HOME:-}}"
READER=""
if [ -z "$VH" ]; then
    for d in /tools/synopsys/verdi* /opt/synopsys/verdi* /usr/synopsys/verdi*; do
        [ -d "$d" ] && VH="$d" && break
    done
fi
if [ -n "$VH" ] && [ -d "$VH/share/FsdbReader/linux64" ]; then
    READER="$VH/share/FsdbReader"
fi
if [ -n "$READER" ]; then
    ok "FsdbReader found: $READER"
    ls -la "$READER/linux64" | grep -E "libnffr|libnsys" || true
    [ -f "$READER/ffrAPI.h" ] && ok "ffrAPI.h present" \
        || bad "ffrAPI.h missing under $READER"
else
    bad "FsdbReader not found. Tried VERDI_HOME=${VH:-<unset>}"
    note "if Verdi lives elsewhere: bash build_and_smoke.sh /path/to/verdi"
fi
record "FsdbReader located: $([ -n "$READER" ] && echo YES || echo NO)"
if [ -z "$READER" ]; then record "build: SKIPPED (no FsdbReader)"; fi

# ---- step 2: build -----------------------------------------------------------
log "step 2: build fsdb2fst"
BUILD_OK=0
if [ -n "$READER" ]; then
    mkdir -p "$SRC_DIR"
    g++ -O2 -std=c++17 -w \
        -I"$READER" -I"$SRC_DIR/fst" \
        -o "$SRC_DIR/fsdb2fst" \
        "$SRC_DIR/fsdb2fst.cpp" \
        "$SRC_DIR/fst/fstapi.c" \
        "$SRC_DIR/fst/lz4.c" \
        "$SRC_DIR/fst/fastlz.c" \
        -L"$READER/linux64" -lnffr -lnsys \
        -lz -lpthread -ldl \
        -Wl,-rpath,"$READER/linux64" -Wl,-rpath,'$ORIGIN' 2> "$SRC_DIR/build.log" \
    && BUILD_OK=1
fi
if [ $BUILD_OK -eq 1 ]; then
    ok "build OK: $SRC_DIR/fsdb2fst"
    note "build.log has warnings suppressed (-w); rerun manually without -w if needed"
    record "build: OK"
else
    if [ -n "$READER" ]; then
        bad "build failed, log: $SRC_DIR/build.log"
        tail -20 "$SRC_DIR/build.log"
        record "build: FAILED"
    fi
fi

# ---- step 3: smoke -----------------------------------------------------------
log "step 3: CLI smoke"
if [ $BUILD_OK -eq 1 ]; then
    if "$SRC_DIR/fsdb2fst" --help >/dev/null 2>&1; then
        ok "--help works"; record "--help: OK"
    else
        bad "--help broken"; record "--help: BROKEN"
    fi
    if ! "$SRC_DIR/fsdb2fst" /nonexistent.fsdb >/dev/null 2>&1; then
        ok "missing-file rejected cleanly (nonzero rc)"; record "error path: OK"
    else
        bad "missing-file should fail"; record "error path: BROKEN"
    fi
else
    record "--help: SKIPPED"
    record "error path: SKIPPED"
fi

# ---- step 4: real conversion (needs FSDB2FST_SAMPLE) -------------------------
log "step 4: real conversion"
SAMPLE="${FSDB2FST_SAMPLE:-}"
if [ $BUILD_OK -eq 1 ] && [ -n "$SAMPLE" ] && [ -f "$SAMPLE" ]; then
    OUT="${SAMPLE%.*}.fst"
    # license check while converting: watch for new Synopsys license checkouts
    "$SRC_DIR/fsdb2fst" -v "$SAMPLE" "$OUT"
    if [ $? -eq 0 ] && [ -f "$OUT" ]; then
        ok "converted: $OUT ($(du -h "$OUT" | cut -f1))"
        record "conversion: OK"
        "$SRC_DIR/fsdb2fst" --info "$SAMPLE" 2>&1 | head -15
    else
        bad "conversion failed (see stderr above)"
        record "conversion: FAILED"
    fi
elif [ $BUILD_OK -ne 1 ]; then
    note "skipped (build failed)"
    record "conversion: SKIPPED"
else
    note "skipped: set FSDB2FST_SAMPLE=/path/to/dump.fsdb to auto-test"
    note "no FSDB at hand? generate one from our sample testbench (step 5b)"
    record "conversion: SKIPPED (no sample)"
fi

# ---- step 5: extra pointers ---------------------------------------------------
log "step 5: extras"
cat <<'EOF'
  5a. license spot-check (run conversion, then):
        $LM_LICENSE_FILE/bin/lmstat -a 2>/dev/null | grep -A2 -i Verdi
      conversion must NOT create a Verdi/license checkout entry.
      (ffrAPI is documented license-free at runtime; confirm on your site.)

  5b. no sample FSDB? make one from the bundled testbench (tiny, seconds):
        # Verdi toolchain ships vcs-like flow; simplest: use verdi's
        # fsdb writer from any existing sim. If you have xrun:
        #   xrun -64bit top_tb.sv counter.sv -sv \
        #     -input @$fsdbDumpfile("dump.fsdb") etc.
        # or convert our VCD golden with Verdi's vcd2fsdb:
        #   $VERDI_HOME/platform/LINUX64/bin/vcd2fsdb dump.vcd dump.fsdb
        # (path varies by release; `which vcd2fsdb` finds it)

  5c. copy back for round-2 verification (on the wave-mcp dev machine):
        fsdb2fst/fsdb2fst   (the binary)
        sample/dump.fsdb    (sample input, small please)
        sample/dump.fst     (its output)
        fsdb2fst/build.log  (if build had warnings)
EOF

# ---- summary ------------------------------------------------------------------
log "summary"
for l in "${RESULT_LINES[@]}"; do printf '  %s\n' "$l"; done
if [ $BUILD_OK -eq 1 ]; then
    log "binary ready: $SRC_DIR/fsdb2fst"
    log "next: copy fsdb2fst + sample .fsdb/.fst back for round-2 verification"
elif [ -n "$READER" ]; then
    log "build failed; check $SRC_DIR/build.log and share it back"
else
    log "FsdbReader not found: run with explicit path, e.g."
    log "  bash build_and_smoke.sh /tools/synopsys/verdi/<release>"
fi
