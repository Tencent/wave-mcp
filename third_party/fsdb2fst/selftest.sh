#!/usr/bin/env bash
# Offline smoke test for the shipped fsdb2fst sources.
#
# Compiles fsdb2fst against the vendored ffrAPI stub (no Verdi, no FsdbReader
# needed) and converts a tiny scripted FSDB, proving the shipped sources are
# complete and buildable. Run it on a fresh checkout or from inside an offline
# bundle (fsdb2fst-src/) to validate the build inputs before touching a real
# Verdi machine.
#
# Usage: bash selftest.sh
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

command -v g++ >/dev/null 2>&1 || { echo "[selftest] ERROR: g++ not found" >&2; exit 1; }
for f in fsdb2fst.cpp ffrAPI_stub.h ffrAPI_stub_impl.cpp fst/fstapi.c fst/lz4.c fst/fastlz.c; do
    [ -f "$SRC_DIR/$f" ] || { echo "[selftest] ERROR: missing $SRC_DIR/$f" >&2; exit 1; }
done

echo "[selftest] compiling fsdb2fst with the offline ffrAPI stub ..."
g++ -O0 -std=c++17 -w -DFFRAPI_STUB -I"$SRC_DIR" -I"$SRC_DIR/fst" \
    -o "$WORK/fsdb2fst" \
    "$SRC_DIR/fsdb2fst.cpp" "$SRC_DIR/ffrAPI_stub_impl.cpp" \
    "$SRC_DIR/fst/fstapi.c" "$SRC_DIR/fst/lz4.c" "$SRC_DIR/fst/fastlz.c" \
    -lz -lpthread -ldl

printf 'scale 1ns\nscope top\nvar clk 1 0\nvar sig 1 0\nupscope\nvc 0 1 0\nvc 0 2 0\nvc 10 1 1\nvc 10 2 1\nvc 20 1 0\nvc 30 2 0\n' > "$WORK/fixture.txt"

echo "[selftest] converting a scripted FSDB ..."
FSDB2FST_STUB_SCRIPT="$WORK/fixture.txt" \
    "$WORK/fsdb2fst" -v "$WORK/fake.fsdb" "$WORK/out.fst"
test -s "$WORK/out.fst" || { echo "[selftest] ERROR: no FST output" >&2; exit 1; }
test -s "$WORK/out.fst.hier" || { echo "[selftest] ERROR: no .fst.hier output" >&2; exit 1; }

echo "[selftest] OK: stub build + conversion smoke passed"
