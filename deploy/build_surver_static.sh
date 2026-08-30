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

# cargo-about template: one heading per crate with its license text.
# Mirrors surfer's own about.hbs approach (upstream has about.toml).
cat > "$OUT/crates-about.hbs" <<'EOF'
# Crate licenses for surver

This file lists every crate statically linked into the surver binary
and its license text, generated with cargo-about. The surver binary is
an unmodified build of the Surfer project (EUPL-1.2); the crates below
carry their own permissive licenses.

{{#each licenses}}
## {{{name}}}

Used by: {{#each used_by}}{{{crate.name}}} {{/each}}

{{{text}}}

{{/each}}
EOF

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
  # license report for every crate statically linked into surver
  # (MIT/Apache-2.0 dual etc.); ship it next to the binary so the
  # viewer assets package can redistribute the notices.
  cargo install --locked --quiet cargo-about || cargo install cargo-about
  cargo about generate --fail /out/crates-about.hbs \
      > /out/surver-crate-licenses.html 2>/dev/null \
    || echo 'WARNING: cargo-about generate failed; license report missing'
  cargo about generate --fail /out/crates-about.hbs \
      | python3 -c 'import html,re,sys;t=sys.stdin.read();t=re.sub(r\"<[^>]+>\",\" \",t);print(re.sub(r\"\\n{3,}\",\"\\n\\n\",html.unescape(t)))' \
      > /out/surver-crate-licenses.txt 2>/dev/null \
    || true
"

echo "built: $OUT/surver"
file "$OUT/surver" || true
if [[ -f "$OUT/surver-crate-licenses.txt" ]]; then
  echo "crate license report: $OUT/surver-crate-licenses.txt (+ .html)"
  echo "  -> build_viewer_assets.sh picks it up automatically when the"
  echo "     surver binary is placed as <asset_dir>/surver next to it."
fi
echo
echo "verify on the oldest target machine:  $OUT/surver --help"
echo "then place it into the viewer asset dir as <asset_dir>/surver and"
echo "rebuild the assets package with deploy/build_viewer_assets.sh"
