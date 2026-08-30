#!/usr/bin/env bash
# Build the wave-mcp-viewer-assets package (Surfer WASM + surver binary).
#
# The viewer assets are EUPL-1.2 (Surfer project); they are distributed as
# a SEPARATE package so the MIT core stays license-clean. This script
# packs an sdist/wheel from a prepared asset directory.
#
# Usage:
#   deploy/build_viewer_assets.sh <asset_dir> [version]
#
# <asset_dir> layout (validated):
#   surver            executable, ideally the musl static build
#   wasm/index.html   Surfer WASM bundle (CI job pages_build, or self-built;
#                     wellen version MUST match the surver binary)
#
# Version pairing rule: WASM and surver must come from the SAME Surfer
# build. Mixed wellen versions are rejected at connect time by Surfer.
set -euo pipefail

ASSET_DIR=${1:?usage: build_viewer_assets.sh <asset_dir> [version]}
VERSION=${2:-0.7.0}
HERE=$(cd "$(dirname "$0")" && pwd)
OUT="$HERE/viewer-assets-build"

[[ -f "$ASSET_DIR/surver" ]] || { echo "missing $ASSET_DIR/surver"; exit 1; }
[[ -f "$ASSET_DIR/wasm/index.html" ]] || { echo "missing $ASSET_DIR/wasm/index.html"; exit 1; }

# surfer's own service worker must not be shipped: wave-mcp serves its own
# sw.js (header restore + version handshake) from the shell directory.
rm -rf "$OUT"
PKG="$OUT/wave_mcp_viewer_assets"
mkdir -p "$PKG/data/wasm"
cp "$ASSET_DIR/surver" "$PKG/data/surver"
chmod +x "$PKG/data/surver"
cp -r "$ASSET_DIR/wasm/." "$PKG/data/wasm/"
rm -f "$PKG/data/wasm/sw.js" "$PKG/data/wasm/sw_new.js" \
      "$PKG/data/wasm/sw.js.orig" "$PKG/data/wasm/view.html" \
      "$PKG/data/wasm"/*.vcd "$PKG/data/wasm"/*.fst 2>/dev/null || true

cat > "$PKG/__init__.py" <<'EOF'
"""Viewer assets for wave-mcp (Surfer WASM + surver). EUPL-1.2.

Data files live in the ``data/`` subdirectory; wave_mcp.viewer discovers
them via this package. See THIRD_PARTY notes in the wave-mcp repository.
"""
EOF

cat > "$OUT/pyproject.toml" <<EOF
[build-system]
requires = ["setuptools>=64"]
build-backend = "setuptools.build_meta"

[project]
name = "wave-mcp-viewer-assets"
version = "$VERSION"
description = "Waveform viewer assets (Surfer WASM + surver) for wave-mcp"
license = { text = "EUPL-1.2" }
requires-python = ">=3.10"

[tool.setuptools]
packages = ["wave_mcp_viewer_assets"]

[tool.setuptools.package-data]
wave_mcp_viewer_assets = ["data/surver", "data/wasm/**"]
EOF

cat > "$OUT/README.md" <<'EOF'
# wave-mcp-viewer-assets

Prebuilt Surfer WASM bundle + surver binary consumed by `wave-mcp`'s
viewer (`wave-view`, `open_wave_view`). Licensed EUPL-1.2 (Surfer
project); distributed separately from the MIT-licensed wave-mcp core.

Install together with the core:

    pip install wave-mcp[viewer]
EOF

( cd "$OUT" && python3 -m pip wheel --no-deps -w dist . >/dev/null )
echo "built:"
ls -la "$OUT/dist/"
echo
echo "install locally:  pip install $OUT/dist/*.whl"
echo "offline bundle:   copy the wheel into the bundle's wheels/ dir"
