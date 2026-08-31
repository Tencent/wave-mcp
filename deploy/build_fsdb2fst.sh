#!/usr/bin/env bash
# Build fsdb2fst : Synopsys FSDB -> FST single-pass converter.
#
# Layout (mirrors TraceWeave's proven setup, adapted for a standalone binary):
#   third_party/fsdb2fst/fsdb2fst.cpp   converter source (MIT, tracked in git)
#   third_party/fsdb2fst/fst/           fstapi + lz4 + fastlz (MIT, vendored)
#   third_party/fsdb2fst/ffrAPI_stub*   offline compile-check stubs (not used
#                                       by the real build)
#   third_party/verdi_runtime/linux64/  libnffr.so + libnsys.so symlinks
#                                       (NOT tracked in git; created here)
#
# FsdbReader resolution order (first match wins):
#   1. repo-local third_party/verdi_runtime/linux64/libnffr.so
#   2. $FSDB2FST_FREADER (explicit override, e.g. a copied share/FsdbReader dir)
#   3. $VERDI_HOME/share/FsdbReader
#   4. $NOVAS_HOME/share/FsdbReader (older installs)
#
# Runtime: the binary bakes an RPATH of $ORIGIN/../verdi_runtime/linux64 so it
# runs without LD_LIBRARY_PATH on this machine; on machines without the
# runtime, copy the two .so files next to the binary (fsdb2fst also searches
# $ORIGIN) or set LD_LIBRARY_PATH. libnffr.so checks out NO license at runtime.
#
# The produced fsdb2fst binary is a LOCAL build artifact: it is not committed,
# not put into PyPI, and never shipped in the offline bundle.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC_DIR="$REPO_ROOT/third_party/fsdb2fst"
RUNTIME_DIR="$REPO_ROOT/third_party/verdi_runtime/linux64"

log()  { printf '[build_fsdb2fst] %s\n' "$*"; }
fail() { printf '[build_fsdb2fst] ERROR: %s\n' "$*" >&2; exit "${2:-1}"; }

case "$(uname -s)" in
    Linux) ;;
    *) fail "FsdbReader runtime is only available for Linux." ;;
esac

command -v g++ >/dev/null 2>&1 || fail "g++ not found in PATH"
for f in fsdb2fst.cpp fst/fstapi.c fst/lz4.c fst/fastlz.c; do
    [ -f "$SRC_DIR/$f" ] || fail "missing $SRC_DIR/$f (fst sources must be vendored, see deploy/VCD2FST_BUILD.md for how they map to the gtkwave tarball)"
done

# ---- resolve the FsdbReader package ----------------------------------------
READER_DIR=""
if [ -n "${FSDB2FST_FREADER:-}" ]; then
    [ -d "$FSDB2FST_FREADER/linux64" ] || fail "FSDB2FST_FREADER=$FSDB2FST_FREADER has no linux64/ subdir"
    READER_DIR="$FSDB2FST_FREADER"
elif [ -e "$RUNTIME_DIR/libnffr.so" ]; then
    log "using repo-local runtime at $RUNTIME_DIR (linked earlier by setup)"
    READER_DIR=""   # link against the repo-local runtime directly
elif [ -n "${VERDI_HOME:-}" ] && [ -d "$VERDI_HOME/share/FsdbReader/linux64" ]; then
    READER_DIR="$VERDI_HOME/share/FsdbReader"
elif [ -n "${NOVAS_HOME:-}" ] && [ -d "$NOVAS_HOME/share/FsdbReader/linux64" ]; then
    READER_DIR="$NOVAS_HOME/share/FsdbReader"
else
    fail "FsdbReader not found. Provide one of:
       export VERDI_HOME=/path/to/verdi            (with share/FsdbReader/linux64)
       export FSDB2FST_FREADER=/path/to/FsdbReader (copied share dir)
       or link the runtime first into third_party/verdi_runtime/linux64"
fi

INC_ARGS=()
LIB_ARGS=()
RPATH_ARGS=()
INC_ARGS+=(-I"$SRC_DIR/fst")
if [ -n "$READER_DIR" ]; then
    [ -f "$READER_DIR/linux64/libnffr.so" ] || fail "missing $READER_DIR/linux64/libnffr.so"
    INC_ARGS=(-I"$READER_DIR")
    LIB_ARGS=(-L"$READER_DIR/linux64" -lnffr -lnsys)
    RPATH_ARGS=(-Wl,-rpath,"$READER_DIR/linux64" -Wl,-rpath,'$ORIGIN')
    log "FsdbReader package: $READER_DIR"
else
    [ -f "$RUNTIME_DIR/libnsys.so" ] || fail "repo-local runtime incomplete (missing libnsys.so)"
    LIB_ARGS=(-L"$RUNTIME_DIR" -lnffr -lnsys)
    RPATH_ARGS=(-Wl,-rpath,'$ORIGIN/../verdi_runtime/linux64')
    log "FsdbReader runtime: $RUNTIME_DIR (repo-local)"
fi

# ---- optional: link the repo-local runtime for portability ------------------
if [ -n "$READER_DIR" ] && [ -n "${FSDB2FST_LINK_RUNTIME:-}" ]; then
    mkdir -p "$RUNTIME_DIR"
    for lib in libnsys.so libnffr.so; do
        ln -sfn "$READER_DIR/linux64/$lib" "$RUNTIME_DIR/$lib"
        log "linked $RUNTIME_DIR/$lib -> $READER_DIR/linux64/$lib"
    done
    log "note: re-run this build to also bake the repo-local RPATH"
fi

# ---- build ------------------------------------------------------------------
OUT="$SRC_DIR/fsdb2fst"
log "building $OUT ..."
g++ -O2 -std=c++17 -w \
    "${INC_ARGS[@]}" \
    -o "$OUT" \
    "$SRC_DIR/fsdb2fst.cpp" \
    "$SRC_DIR/fst/fstapi.c" \
    "$SRC_DIR/fst/lz4.c" \
    "$SRC_DIR/fst/fastlz.c" \
    "${LIB_ARGS[@]}" \
    -lz -lpthread -ldl \
    "${RPATH_ARGS[@]}"

chmod 755 "$OUT"
log "build OK: $OUT"
log "RPATH: $(objdump -x "$OUT" 2>/dev/null | grep -m1 RUNPATH || echo 'none baked')"
log "next:  bash third_party/fsdb2fst/selftest.sh   (needs a sample .fsdb)"
