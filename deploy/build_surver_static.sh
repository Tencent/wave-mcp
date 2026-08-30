#!/usr/bin/env bash
# Build a statically-linked (musl) surver for old-glibc machines.
#
# Why: the official surver binary needs glibc >= 2.34; encrypted-network
# hosts often run CentOS 7 (glibc 2.17). A musl static build runs on any
# Linux regardless of glibc. Same pattern as deploy/build_vcd2fst.sh:
# differences live on the build side, target machines stay untouched.
#
# Requires: docker with network access (run OUTSIDE the airgap, copy the
# binary into the offline bundle afterwards).
#
# Usage:
#   deploy/build_surver_static.sh [surfer_git_ref] [out_dir]
set -euo pipefail

REF=${1:-v0.7.0}
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${2:-"$HERE/surver-static"}
mkdir -p "$OUT"

# rust:alpine ships a musl toolchain natively; build only the surver crate.
docker run --rm -v "$OUT":/out rust:alpine sh -exc "
  apk add --no-cache git musl-dev openssl-dev openssl-libs-static pkgconfig
  git clone --depth 1 --branch $REF \
      https://gitlab.com/surfer-project/surfer.git /src
  cd /src
  git submodule update --init --recursive --depth 1
  # static openssl for reqwest/native-tls if the ref pulls it in
  export OPENSSL_STATIC=1
  cargo build --release -p surver
  BIN=\$(find target/release -maxdepth 1 -type f -name surver)
  cp \"\$BIN\" /out/surver
  # verify: no dynamic interpreter
  if ldd /out/surver 2>&1 | grep -qv 'not a dynamic executable\|statically'; then
    echo 'WARNING: binary may not be fully static:'
    ldd /out/surver || true
  fi
"

echo "built: $OUT/surver"
file "$OUT/surver" || true
echo
echo "verify on the oldest target machine:  $OUT/surver --help"
echo "then place it into the viewer asset dir as <asset_dir>/surver and"
echo "rebuild the assets package with deploy/build_viewer_assets.sh"
