#!/usr/bin/env bash
# One-command Docker build pipeline for wave-mcp offline bundles.
#
# Produces the full release matrix in dist/:
#   wave-mcp-bundle-glibc2.28.tar.gz   mainstream hosts (CentOS 8+/Ubuntu 18.10+)
#   wave-mcp-bundle-glibc2.17.tar.gz   legacy hosts (CentOS 7 / RHEL 7)
#
# The ONLY requirement on this machine is docker (+ network on first run).
# Every toolchain difference lives inside pinned build containers; target
# machines never need docker — they receive plain tarballs as before.
#
# Stages (cached: stages skip themselves when their artifact exists):
#   1. rust:alpine          -> musl static surver        (legacy viewer)
#   2. manylinux2014        -> pyslang manylinux2014 whl (legacy python dep)
#   3. manylinux_2_28       -> assemble 2.28 bundle
#   4. manylinux2014        -> assemble 2.17 bundle
#
# Usage:
#   deploy/docker_build_all.sh [--viewer <asset_dir>] [--python <tar.gz|url>]
#       [--skip-legacy] [--rebuild]
#
#   --viewer   Surfer WASM asset dir (with wasm/index.html). The surver
#              binary inside is REPLACED by the musl build for the 2.17
#              bundle; the 2.28 bundle keeps whatever surver the dir has
#              (or also uses the musl one if the dir has none).
#   --python   python-build-standalone install_only tarball/URL to embed.
#   --skip-legacy  build only the 2.28 bundle (fast path).
#   --rebuild  ignore caches, rebuild surver + pyslang from scratch.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CACHE="$REPO_ROOT/deploy/.docker-build-cache"
DIST="$REPO_ROOT/dist"
VIEWER_SRC=""
PYTHON_SRC=""
SKIP_LEGACY=0
REBUILD=0
PYSLANG_VER="$(grep -oP 'pyslang>=\K[0-9.]+' "$REPO_ROOT/pyproject.toml" || echo 11.0.0)"
# Must match the ref the bundled wasm snapshot was built from; build_surver_static.sh
# defaults to the same value. Do NOT bump to a release tag: a tag that predates the
# wasm snapshot yields a different wellen version, and build_viewer_assets.sh then
# refuses the pair (measured 2026-09-02: v0.7.0 -> wellen 0.20.5 vs wasm 0.25.6,
# which made the 2.17 bundle stage fail silently).
SURFER_REF="86eedfd0cda70fc0a61ab200ebf37aabf97c5cde"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --viewer) VIEWER_SRC="$2"; shift 2;;
    --python) PYTHON_SRC="$2"; shift 2;;
    --skip-legacy) SKIP_LEGACY=1; shift;;
    --rebuild) REBUILD=1; shift;;
    *) echo "unknown arg: $1"; exit 1;;
  esac
done

command -v docker >/dev/null || { echo "ERROR: docker required"; exit 1; }
mkdir -p "$CACHE" "$DIST"
[[ "$REBUILD" == "1" ]] && rm -rf "$CACHE"/surver-static "$CACHE"/pyslang-whl

# -- stage 1: musl static surver (needed by 2.17; also fallback for 2.28) --
if [[ -n "$VIEWER_SRC" ]]; then
  if [[ ! -x "$CACHE/surver-static/surver" ]]; then
    echo "[stage1] building musl static surver ($SURFER_REF) ..."
    "$REPO_ROOT/deploy/build_surver_static.sh" "$SURFER_REF" \
        "$CACHE/surver-static"
  else
    echo "[stage1] cached musl surver OK"
  fi
fi

# -- stage 2: pyslang manylinux2014 wheel (2.17 only) -----------------------
if [[ "$SKIP_LEGACY" == "0" ]]; then
  PYSLANG_WHL=$(ls "$CACHE"/pyslang-whl/pyslang-*manylinux*2014*.whl \
                   "$CACHE"/pyslang-whl/pyslang-*manylinux_2_17*.whl \
                   2>/dev/null | head -1 || true)
  if [[ -z "$PYSLANG_WHL" ]]; then
    echo "[stage2] building pyslang $PYSLANG_VER manylinux2014 wheel ..."
    "$REPO_ROOT/deploy/build_pyslang_manylinux2014.sh" \
        --version "$PYSLANG_VER" --out "$CACHE/pyslang-whl"
    PYSLANG_WHL=$(ls "$CACHE"/pyslang-whl/pyslang-*manylinux*.whl | head -1)
  else
    echo "[stage2] cached pyslang wheel OK: $(basename "$PYSLANG_WHL")"
  fi
fi

# -- helper: run the existing bundle script inside a manylinux container ----
# The bundle script itself is unchanged; the container pins glibc + cp311.
# All inputs/outputs go through the /repo /cache /dist volumes; never mount
# host /tmp (it would shadow the container's own writable /tmp).
bundle_in_container() {
  local image="$1" glibc="$2" out_name="$3"; shift 3
  local extra_args=("$@")
  echo "[bundle] $out_name (glibc $glibc, image $image) ..."
  docker run --rm -v "$REPO_ROOT":/repo -v "$CACHE":/cache -v "$DIST":/dist \
    -w /repo "$image" bash -exc "
      export PATH=/opt/python/cp311-cp311/bin:\$PATH
      python3 -m pip install -q --upgrade pip build >/dev/null
      bash deploy/build_offline_bundle.sh --out /cache/build/$out_name \
          --target-glibc $glibc ${extra_args[*]} --no-tar
      tar -C /cache/build -czf /dist/$out_name.tar.gz $out_name
      rm -rf /cache/build/$out_name
      chown $(id -u):$(id -g) /dist/$out_name.tar.gz
    " >/dev/null
  echo "         -> dist/$out_name.tar.gz ($(du -h "$DIST/$out_name.tar.gz" | cut -f1))"
}

# -- viewer asset staging (returns the CONTAINER path under /cache) ----------
stage_viewer() {  # $1 = surver binary to embed
  local surver_bin="$1" staged="$CACHE/viewer-staged"
  rm -rf "$staged"; mkdir -p "$staged"
  cp -r "$VIEWER_SRC/wasm" "$staged/wasm"
  cp "$surver_bin" "$staged/surver"; chmod +x "$staged/surver"
  # carry the crate license report + license-file texts through staging so
  # build_viewer_assets.sh embeds them into the assets package
  [[ -f "$VIEWER_SRC/surver-crate-licenses.txt" ]] && \
    cp "$VIEWER_SRC/surver-crate-licenses.txt" "$staged/"
  [[ -f "$CACHE/surver-static/surver-crate-licenses.txt" ]] && \
    cp "$CACHE/surver-static/surver-crate-licenses.txt" "$staged/" 2>/dev/null || true
  [[ -d "$VIEWER_SRC/crates-license-files" ]] && \
    cp -r "$VIEWER_SRC/crates-license-files" "$staged/"
  [[ -d "$CACHE/surver-static/crates-license-files" ]] && \
    cp -r "$CACHE/surver-static/crates-license-files" "$staged/" 2>/dev/null || true
  echo "/cache/viewer-staged"
}

PY_ARGS=()
if [[ -n "$PYTHON_SRC" ]]; then
  if [[ -f "$PYTHON_SRC" ]]; then
    cp -f "$PYTHON_SRC" "$CACHE/python-standalone.tar.gz"
    PY_ARGS=(--python /cache/python-standalone.tar.gz)
  else
    # URL: download once into the cache, then treat as local
    echo "[*] fetching standalone python: $PYTHON_SRC"
    curl -sL "$PYTHON_SRC" -o "$CACHE/python-standalone.tar.gz"
    PY_ARGS=(--python /cache/python-standalone.tar.gz)
  fi
fi
mkdir -p "$CACHE/build"

# -- stage 3: mainstream bundle (glibc 2.28) --------------------------------
V_ARGS=()
if [[ -n "$VIEWER_SRC" ]]; then
  if [[ -x "$VIEWER_SRC/surver" ]]; then
    V_ARGS=(--viewer "$(stage_viewer "$VIEWER_SRC/surver" | tail -1)")
  else
    V_ARGS=(--viewer "$(stage_viewer "$CACHE/surver-static/surver" | tail -1)")
  fi
fi
bundle_in_container quay.io/pypa/manylinux_2_28_x86_64 2.28 \
    wave-mcp-bundle-glibc2.28 "${PY_ARGS[@]}" "${V_ARGS[@]}"

# -- stage 4: legacy bundle (glibc 2.17, musl surver mandatory) --------------
if [[ "$SKIP_LEGACY" == "0" ]]; then
  V_ARGS=()
  if [[ -n "$VIEWER_SRC" ]]; then
    V_ARGS=(--viewer "$(stage_viewer "$CACHE/surver-static/surver" | tail -1)")
  fi
  bundle_in_container quay.io/pypa/manylinux2014_x86_64 2.17 \
      wave-mcp-bundle-glibc2.17 \
      --pyslang-wheel "/cache/pyslang-whl/$(basename "$PYSLANG_WHL")" \
      "${PY_ARGS[@]}" "${V_ARGS[@]}"
fi

echo
echo "[done] release matrix in dist/:"
ls -la "$DIST"/*.tar.gz
