#!/usr/bin/env bash
# Build the fstdumper VPI plugin for Xcelium (xrun) from a source checkout
# that YOU have already obtained.
#
# What it does: take a pristine upstream fstdumper checkout, apply the
# Xcelium patch shipped in this repo, and build fstdumper.so without the
# upstream -DDEBUG flag. Run it once per environment; the resulting .so can
# be shared across a team. Check the result with deploy/verify_fstdumper.sh.
#
# LICENSING (important): fstdumper is GPL-3.0 and is NOT distributed with
# wave-mcp. This script never downloads it either: it does not clone, fetch or
# otherwise retrieve the upstream source. You obtain the source yourself
# (see docs/XCELIUM_FST_GUIDE.md) and point the script at that directory.
# The plugin is loaded by xrun at simulation time and is never linked into the
# wave-mcp process, so wave-mcp stays Apache-2.0. The patch under
# third_party/fstdumper/ is a derivative work of GPL code and is itself
# GPL-3.0. See docs/licenses/fstdumper-patches.NOTICE.
#
# Requirements: gcc, g++, make, patch, zlib headers (-lz).
# Build in the SAME environment that runs xrun: the plugin depends on
# libz.so.1 and libc.so.6, so a glibc mismatch shows up as a load failure.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PATCH="$REPO_ROOT/third_party/fstdumper/fstdumper-xcelium.patch"
UPSTREAM_URL="https://github.com/semify-eda/fstdumper"
PIN_COMMIT="3c7506333d2c9ad098d4526209621ea401a077ba"
SRC_DIR="${FSTDUMPER_SRC_DIR:-${FSTDUMPER_BUILD_DIR:-}}"

log()  { printf '[build_fstdumper] %s\n' "$*"; }
fail() { printf '[build_fstdumper] ERROR: %s\n' "$*" >&2; exit "${2:-1}"; }

usage() {
    cat <<EOF
Usage: FSTDUMPER_SRC_DIR=<checkout> bash deploy/build_fstdumper.sh [--no-patch]
       bash deploy/build_fstdumper.sh <checkout> [--no-patch]

  <checkout>   directory containing the upstream fstdumper source that you
               obtained yourself (this script does not download anything),
               pristine at commit ${PIN_COMMIT:0:7}
  --no-patch   build pristine upstream (NOT recommended: on Xcelium it loses
               interface, generate-block and SystemVerilog-typed signals)

Environment:
  FSTDUMPER_SRC_DIR     path to your upstream checkout (alternative to the
                        positional argument; FSTDUMPER_BUILD_DIR is accepted
                        as a legacy alias)
  DEBUG_BUILD=1         keep upstream's -DDEBUG tracing (very verbose)

Obtaining the source (GPL-3.0, not shipped with wave-mcp), on a machine of
your choice:
  git clone $UPSTREAM_URL.git /path/to/fstdumper
  git -C /path/to/fstdumper checkout ${PIN_COMMIT:0:7}
EOF
}

APPLY_PATCH=1
while [ $# -gt 0 ]; do
    case "$1" in
        --no-patch) APPLY_PATCH=0 ;;
        --perf-opt) fail "--perf-opt was removed: the optional performance patch was dropped
       (about 1% faster, not part of the validated build). Run without it." ;;
        -h|--help)  usage; exit 0 ;;
        -*)         fail "unknown option: $1 (see --help)" ;;
        *)          [ -z "$SRC_DIR" ] || fail "source directory given twice: '$SRC_DIR' and '$1'"
                    SRC_DIR="$1" ;;
    esac
    shift
done

for tool in gcc g++ make patch; do
    command -v "$tool" >/dev/null 2>&1 || fail "$tool not found in PATH"
done

if [ -z "$SRC_DIR" ]; then
    usage >&2
    fail "no fstdumper source directory given.
       wave-mcp does not ship or download fstdumper (GPL-3.0). Obtain the
       source yourself (see --help), then re-run:
         bash deploy/build_fstdumper.sh /path/to/fstdumper"
fi
[ -d "$SRC_DIR" ] || fail "source directory not found: $SRC_DIR"
[ -f "$SRC_DIR/Makefile" ] && [ -f "$SRC_DIR/src/sys_fst.c" ] \
    || fail "no Makefile / src/sys_fst.c in $SRC_DIR: is this an fstdumper checkout?"
[ -f "$PATCH" ] || fail "patch not found: $PATCH"
SRC_DIR="$(cd "$SRC_DIR" && pwd)"
OUT_SO="$SRC_DIR/fstdumper.so"

log "using fstdumper source at $SRC_DIR"
cd "$SRC_DIR"

if [ -d .git ] && command -v git >/dev/null 2>&1; then
    head_id="$(git rev-parse HEAD 2>/dev/null || true)"
    if [ -n "$head_id" ] && [ "$head_id" != "$PIN_COMMIT" ]; then
        log "WARNING: checkout is at ${head_id:0:7}, the patch targets ${PIN_COMMIT:0:7}"
    fi
fi

# --force: never prompt and never guess a reversed patch; stdin closed so
# patch cannot wait for terminal input.
pdry() { patch -p1 --force --silent --dry-run "$@" -i "$PATCH" >/dev/null 2>&1 < /dev/null; }

if [ "$APPLY_PATCH" = "1" ]; then
    if pdry --reverse; then
        log "patch already applied, skipping"
    elif pdry --forward; then
        patch -p1 --force --forward --silent -i "$PATCH" < /dev/null >/dev/null
        log "applied $(basename "$PATCH")"
    else
        fail "cannot apply $(basename "$PATCH") to $SRC_DIR.
       The tree is neither pristine upstream ${PIN_COMMIT:0:7} nor already
       patched with this version (a tree patched by an older wave-mcp
       release also ends up here). Start from a fresh checkout of
       ${PIN_COMMIT:0:7} in a new directory and run this script again."
    fi
else
    log "WARNING: building pristine upstream, Xcelium patch NOT applied"
fi

CFLAGS_BUILD="-I src/config -fPIC -O2"
[ "${DEBUG_BUILD:-0}" = "1" ] && CFLAGS_BUILD="$CFLAGS_BUILD -DDEBUG"

log "building fstdumper.so ..."
rm -rf obj "$OUT_SO"
make fstdumper.so CFLAGS="$CFLAGS_BUILD" < /dev/null

[ -f "$OUT_SO" ] || fail "build reported success but $OUT_SO is missing"
if command -v nm >/dev/null 2>&1; then
    nm -D "$OUT_SO" | grep -q ' T vlog_startup_routines_bootstrap' \
        || fail "entry point vlog_startup_routines_bootstrap not exported by $OUT_SO"
fi

log "build OK: $OUT_SO"
cat <<EOF

Check it on this machine (needs xrun, and fst2vcd from GTKWave):
  bash $REPO_ROOT/deploy/verify_fstdumper.sh --so $OUT_SO

Then add these to your existing xrun command (nothing else changes):

  xrun -64bit -access +r \\
    -loadvpi $OUT_SO:vlog_startup_routines_bootstrap \\
    -f your_filelist.f \\
    $REPO_ROOT/examples/xcelium_fst/fst_dump_cfg.sv \\
    -top your_tb -top fst_dump \\
    -define 'FST_DUMP_TOP=your_tb' \\
    -define 'FST_DUMP_FILE="waves.fst"'

Then hand the result to wave-mcp:
  prepare_session(wave_path="waves.fst", filelist_path="your_filelist.f")

Full guide, including what direct FST cannot capture:
  docs/XCELIUM_FST_GUIDE.md
EOF
