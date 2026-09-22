#!/usr/bin/env bash
# Build the fstdumper VPI plugin for Xcelium (xrun) from a source checkout
# that YOU have already obtained.
#
# What it does: take an existing upstream fstdumper checkout, apply the
# Xcelium fix patches shipped in this repo, and build fstdumper.so. Run it
# once per environment; the resulting .so can be shared across a team.
#
# LICENSING (important): fstdumper is GPL-3.0 and is NOT distributed with
# wave-mcp. This script never downloads it either: it does not clone, fetch or
# otherwise retrieve the upstream source. You obtain the source yourself
# (see docs/XCELIUM_FST_GUIDE.md) and point the script at that directory.
# The plugin is loaded by xrun at simulation time and is never linked into the
# wave-mcp process, so wave-mcp stays MIT. The patches under
# third_party/fstdumper/ are derivative works of GPL-3.0 code and are
# themselves GPL-3.0. See docs/THIRD_PARTY.md.
#
# Requirements: gcc, make, patch, zlib headers (-lz).
# Build in the SAME environment that runs xrun: the plugin depends on
# libz.so.1 and libc.so.6, so a glibc mismatch shows up as a load failure.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PATCH_DIR="$REPO_ROOT/third_party/fstdumper"
UPSTREAM_URL="https://github.com/semify-eda/fstdumper"
SRC_DIR="${FSTDUMPER_SRC_DIR:-${FSTDUMPER_BUILD_DIR:-}}"

log()  { printf '[build_fstdumper] %s\n' "$*"; }
fail() { printf '[build_fstdumper] ERROR: %s\n' "$*" >&2; exit "${2:-1}"; }

usage() {
    cat <<EOF
Usage: FSTDUMPER_SRC_DIR=<checkout> bash deploy/build_fstdumper.sh [--perf-opt] [--no-patch]
       bash deploy/build_fstdumper.sh <checkout> [--perf-opt] [--no-patch]

  <checkout>   directory containing the upstream fstdumper source that you
               obtained yourself (this script does not download anything)
  --perf-opt   also apply fstdumper-perf-opt.patch (optional speedups)
  --no-patch   build pristine upstream (NOT recommended: loses interface
               signals, emits redundant transitions, drops the last change
               at \$finish)

Environment:
  FSTDUMPER_SRC_DIR     path to your upstream checkout (alternative to the
                        positional argument; FSTDUMPER_BUILD_DIR is accepted
                        as a legacy alias)

Obtaining the source (GPL-3.0, not shipped with wave-mcp), on a machine of
your choice:
  git clone --depth 1 $UPSTREAM_URL.git /path/to/fstdumper
EOF
}

APPLY_PATCH=1
APPLY_PERF=0
while [ $# -gt 0 ]; do
    case "$1" in
        --perf-opt) APPLY_PERF=1 ;;
        --no-patch) APPLY_PATCH=0 ;;
        -h|--help)  usage; exit 0 ;;
        -*)         fail "unknown option: $1 (see --help)" ;;
        *)          [ -z "$SRC_DIR" ] || fail "source directory given twice: '$SRC_DIR' and '$1'"
                    SRC_DIR="$1" ;;
    esac
    shift
done

for tool in gcc make patch; do
    command -v "$tool" >/dev/null 2>&1 || fail "$tool not found in PATH"
done

if [ -z "$SRC_DIR" ]; then
    usage >&2
    fail "no fstdumper source directory given.
       wave-mcp does not ship or download fstdumper (GPL-3.0). Obtain the
       source yourself, e.g.
         git clone --depth 1 $UPSTREAM_URL.git /path/to/fstdumper
       then re-run:
         bash deploy/build_fstdumper.sh /path/to/fstdumper"
fi
[ -d "$SRC_DIR" ] || fail "source directory not found: $SRC_DIR"
[ -f "$SRC_DIR/Makefile" ] || fail "no Makefile in $SRC_DIR: is this an fstdumper checkout?"
SRC_DIR="$(cd "$SRC_DIR" && pwd)"
OUT_SO="$SRC_DIR/fstdumper.so"

log "using fstdumper source at $SRC_DIR"
cd "$SRC_DIR"

apply_patch() {
    local p="$1"
    [ -f "$p" ] || fail "patch not found: $p"
    if patch -p1 --dry-run --forward --silent < "$p" >/dev/null 2>&1; then
        patch -p1 --forward < "$p" >/dev/null
        log "applied $(basename "$p")"
    elif patch -p1 --dry-run --reverse --silent < "$p" >/dev/null 2>&1; then
        log "already applied, skipping $(basename "$p")"
    else
        fail "cannot apply $(basename "$p"): upstream may have moved.
       Build without patches using --no-patch (with the known caveats), or
       resolve the conflict by hand in $SRC_DIR"
    fi
}

if [ "$APPLY_PATCH" = "1" ]; then
    apply_patch "$PATCH_DIR/fstdumper-xcelium-fixes.patch"
    [ "$APPLY_PERF" = "1" ] && apply_patch "$PATCH_DIR/fstdumper-perf-opt.patch"
else
    log "WARNING: building pristine upstream, Xcelium fixes NOT applied"
fi

log "building fstdumper.so ..."
make fstdumper.so

[ -f "$OUT_SO" ] || fail "build reported success but $OUT_SO is missing"

log "build OK: $OUT_SO"
cat <<EOF

Next: add these to your existing xrun command (nothing else changes).

  xrun -64bit +access+r \\
    -loadvpi $OUT_SO:vlog_startup_routines_bootstrap \\
    -f your_filelist.f \\
    $REPO_ROOT/examples/xcelium_fst/fst_dump_cfg.sv \\
    -top your_tb -top fst_dump \\
    -define 'FST_DUMP_TOP=your_tb' \\
    -define 'FST_DUMP_FILE="waves.fst"'

Then hand the result to wave-mcp:
  prepare_session(wave_path="waves.fst", filelist_path="your_filelist.f")

Full guide, including when direct FST is the wrong choice:
  docs/XCELIUM_FST_GUIDE.md
EOF
