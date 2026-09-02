#!/usr/bin/env bash
# Build the fstdumper VPI plugin for Xcelium (xrun) in one command.
#
# What it does: fetch the upstream fstdumper source, apply the Xcelium fix
# patches shipped in this repo, and build fstdumper.so. Run it once per
# environment; the resulting .so can be shared across a team.
#
# LICENSING (important): fstdumper is GPL-3.0 and is NOT distributed with
# wave-mcp. This script only automates fetching and building it on your own
# machine. The plugin is loaded by xrun at simulation time and is never linked
# into the wave-mcp process, so wave-mcp stays MIT. The patches under
# third_party/fstdumper/ are derivative works of GPL-3.0 code and are
# themselves GPL-3.0. See docs/THIRD_PARTY.md.
#
# Requirements: git, gcc, make, zlib headers (-lz).
# Build in the SAME environment that runs xrun: the plugin depends on
# libz.so.1 and libc.so.6, so a glibc mismatch shows up as a load failure.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PATCH_DIR="$REPO_ROOT/third_party/fstdumper"
UPSTREAM="${FSTDUMPER_REPO:-https://github.com/semify-eda/fstdumper.git}"
BUILD_DIR="${FSTDUMPER_BUILD_DIR:-$REPO_ROOT/third_party/fstdumper/build}"
OUT_SO="$BUILD_DIR/fstdumper.so"

log()  { printf '[build_fstdumper] %s\n' "$*"; }
fail() { printf '[build_fstdumper] ERROR: %s\n' "$*" >&2; exit "${2:-1}"; }

usage() {
    cat <<'EOF'
Usage: bash deploy/build_fstdumper.sh [--perf-opt] [--no-patch] [--force]

  --perf-opt   also apply fstdumper-perf-opt.patch (optional speedups)
  --no-patch   build pristine upstream (NOT recommended: loses interface
               signals, emits redundant transitions, drops the last change
               at $finish)
  --force      re-clone into a clean build dir

Environment:
  FSTDUMPER_REPO        upstream git URL (default: semify-eda/fstdumper)
  FSTDUMPER_BUILD_DIR   build location (default: third_party/fstdumper/build)
EOF
}

APPLY_PATCH=1
APPLY_PERF=0
FORCE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --perf-opt) APPLY_PERF=1 ;;
        --no-patch) APPLY_PATCH=0 ;;
        --force)    FORCE=1 ;;
        -h|--help)  usage; exit 0 ;;
        *)          fail "unknown option: $1 (see --help)" ;;
    esac
    shift
done

for tool in git gcc make patch; do
    command -v "$tool" >/dev/null 2>&1 || fail "$tool not found in PATH"
done

if [ "$FORCE" = "1" ] && [ -d "$BUILD_DIR" ]; then
    log "removing existing build dir (--force)"
    rm -rf "$BUILD_DIR"
fi

if [ -d "$BUILD_DIR/.git" ]; then
    log "reusing existing checkout at $BUILD_DIR"
else
    log "cloning $UPSTREAM -> $BUILD_DIR"
    mkdir -p "$(dirname "$BUILD_DIR")"
    git clone --depth 1 "$UPSTREAM" "$BUILD_DIR" \
        || fail "clone failed. No network? Clone manually, then re-run with
       FSTDUMPER_BUILD_DIR=<your checkout>"
fi

cd "$BUILD_DIR"

apply_patch() {
    local p="$1"
    [ -f "$p" ] || fail "patch not found: $p"
    if patch -p1 --dry-run --forward --silent < "$p" >/dev/null 2>&1; then
        patch -p1 --forward < "$p" >/dev/null
        log "applied $(basename "$p")"
    elif patch -p1 --dry-run --reverse --silent < "$p" >/dev/null 2>&1; then
        log "already applied, skipping $(basename "$p")"
    else
        fail "cannot apply $(basename "$p") — upstream may have moved.
       Build without patches using --no-patch (with the known caveats), or
       resolve the conflict by hand in $BUILD_DIR"
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
