#!/usr/bin/env bash
# Build the wave-mcp-viewer-assets package (Surfer WASM + surver binary).
#
# The viewer assets are EUPL-1.2 (Surfer project); they are distributed as
# a SEPARATE package; this alone does not determine license impact. This script
# packs an sdist/wheel from a prepared asset directory and embeds the
# EUPL-1.2 license text plus a provenance NOTICE (see docs/licenses/).
#
# Usage:
#   deploy/build_viewer_assets.sh <asset_dir> [version] [--slim]
#
# Release builds ship two variants of the same assets:
#   --slim    drops the upstream source archives (sources/, about 136 MB of
#             the 146 MB wheel) and is what gets uploaded to PyPI, whose
#             per-file limit is 100 MB. The version stays plain, e.g.
#             1.0.0 (always the wave-mcp version, see viewer-pin.sh).
#   (default) keeps every material file and is attached to the GitHub
#             release next to the slim wheel. Pass a local version such as
#             1.0.0+materials so the two filenames never collide.
# Both variants keep the dependency notices, inventories and manifest; the
# slim one points at the release for the source archives.
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
HERE=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(cd "$HERE/.." && pwd)
OUT="$HERE/viewer-assets-build"

# Pinned Surfer provenance comes from deploy/viewer-pin.sh (single source of
# truth) so this script, build_surver_static.sh and docker_build_all.sh can
# agree on the intended source pin. The pin alone is not build evidence.
# shellcheck source=deploy/viewer-pin.sh
source "$HERE/viewer-pin.sh"
VERSION=""
SLIM=0
shift
while [[ $# -gt 0 ]]; do
  case "$1" in
    --slim) SLIM=1; shift;;
    *)      VERSION="$1"; shift;;
  esac
done
[[ -n "$VERSION" ]] || VERSION="$VIEWER_ASSETS_VERSION"

[[ -n "${VIEWER_MATERIALS:-}" ]] || { echo "ERROR: VIEWER_MATERIALS must name the matching material directory" >&2; exit 1; }
python3 "$HERE/redistribution_materials.py" check --component viewer \
  --materials "$VIEWER_MATERIALS" --artifact "$ASSET_DIR"

[[ -f "$ASSET_DIR/surver" ]] || { echo "missing $ASSET_DIR/surver"; exit 1; }
[[ -f "$ASSET_DIR/wasm/index.html" ]] || { echo "missing $ASSET_DIR/wasm/index.html"; exit 1; }

# -- wellen version gate -------------------------------------------------
# Surfer rejects a client/server wellen mismatch at connect time with
# "Version incompatibility!", which surfaces to the user as a waveform
# that silently never loads. Both artifacts embed the wellen version as a
# plain "wellen-X.Y.Z" string, so compare them here and refuse to ship a
# broken pair. Matching versions do not prove a shared source commit;
# source/build correspondence must be reviewed separately.
extract_wellen() {                       # $1 = binary, echoes "X.Y.Z"
  local v
  v=$(grep -ao 'wellen-0\.[0-9][0-9.]*' "$1" 2>/dev/null |
      head -1 | sed 's/^wellen-//')
  if [[ -z "$v" ]]; then
    v=$(grep -ao 'wellen-[0-9][0-9.]*' "$1" 2>/dev/null |
        head -1 | sed 's/^wellen-//')
  fi
  echo "$v"
}

WELLEN_BIN=$(extract_wellen "$ASSET_DIR/surver")
WELLEN_WASM=$(extract_wellen "$ASSET_DIR/wasm/surfer_bg.wasm")
if [[ -z "$WELLEN_BIN" || -z "$WELLEN_WASM" ]]; then
  echo "ERROR: could not detect the wellen version of both artifacts"
  echo "       surver: '${WELLEN_BIN:-<none>}'  wasm: '${WELLEN_WASM:-<none>}'"
  echo "       refusing to build an unverifiable asset pair."
  exit 1
fi
if [[ "$WELLEN_BIN" != "$WELLEN_WASM" ]]; then
  echo "ERROR: wellen version mismatch (refusing to build):"
  echo "       surver binary : $WELLEN_BIN"
  echo "       wasm client   : $WELLEN_WASM"
  echo "       Surfer rejects mixed versions at connect time, so the"
  echo "       waveform would never load. Rebuild both from the same ref."
  exit 1
fi
# Both sides agreeing is necessary but not sufficient: they can agree on a
# version that no longer matches the pin, which means viewer-pin.sh is stale
# and the recorded provenance would be wrong. Cross-check against the pin.
if [[ "$WELLEN_BIN" != "$VIEWER_WELLEN_VERSION" ]]; then
  echo "ERROR: assets are wellen $WELLEN_BIN but deploy/viewer-pin.sh expects"
  echo "       $VIEWER_WELLEN_VERSION (SURFER_REF=$SURFER_REF)."
  echo "       Either these assets came from a different commit, or the pin was"
  echo "       bumped without updating VIEWER_WELLEN_VERSION. Fix the pin so the"
  echo "       NOTICE/PROVENANCE provenance stays truthful, then rebuild."
  exit 1
fi
echo "wellen version match: $WELLEN_BIN (surver + wasm, pin $SURFER_REF)"

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

cat > "$PKG/__init__.py" <<EOF
"""Viewer assets for wave-mcp (Surfer WASM + surver). EUPL-1.2.

Data files live in the \`\`data/\`\` subdirectory; wave_mcp.viewer discovers
them via this package. See THIRD_PARTY notes in the wave-mcp repository.

The package version follows wave-mcp; the upstream Surfer provenance is
recorded here and in NOTICE so the version number itself need not carry it.
"""
__version__ = "${VERSION}"
#: Intended upstream Surfer source pin; see redistribution build evidence.
UPSTREAM_SURFER_COMMIT = "${SURFER_REF}"
#: Upstream Surfer / wellen release the artifacts report at connect time.
UPSTREAM_SURFER_VERSION = "${VIEWER_WELLEN_VERSION}"
EOF

# EUPL-1.2 requires every copy to carry the license text. Vendor the official
# text from docs/licenses/ into the package (see docs/licenses/README.md).
LICENSE_SRC="$REPO_ROOT"/docs/licenses/EUPL-1.2.txt
if [[ -f "$LICENSE_SRC" ]]; then
  cp "$LICENSE_SRC" "$OUT/LICENSE-EUPL-1.2.txt"
  cp "$LICENSE_SRC" "$PKG/LICENSE-EUPL-1.2.txt"
else
  echo "ERROR: $LICENSE_SRC not found; refusing to build an EUPL package"
  echo "       without the license text. See docs/licenses/README.md."
  exit 1
fi

# NOTICE: state the pinned source and packaging changes without asserting
# that a link alone satisfies every applicable distribution requirement.
# The slim variant omits the source archives, so its NOTICE must say where
# they live instead of implying that they travel in this wheel.
if [[ "$SLIM" = "1" ]]; then
  MATERIALS_LINE="Packaging removes upstream service workers and sample waveforms. This slim
variant carries the dependency notices and inventories under redistribution/;
the corresponding source archives are distributed with the full material
wheel attached to the wave-mcp GitHub release:

  https://github.com/Tencent/wave-mcp/releases"
else
  MATERIALS_LINE="Packaging removes upstream service workers and sample waveforms. Exact
source archives, dependency notices and build evidence accompany the assets
under redistribution/."
fi
cat > "$OUT/NOTICE" <<EOF
wave-mcp-viewer-assets
======================

This package redistributes build artifacts of the Surfer
project (https://gitlab.com/surfer-project/surfer), licensed under the
European Union Public Licence v1.2 (EUPL-1.2):

  - upstream release: Surfer / wellen ${VIEWER_WELLEN_VERSION}
  - intended Surfer source commit: ${SURFER_REF}
  - surver binary: locally built with deploy/build_surver_static.sh;
    source/build correspondence must be established from build records
  - wasm/ bundle: supplied build artifact; CI job/source correspondence
    must be established independently of its wellen version
    (sha256 of surfer_bg.wasm:
    $(sha256sum "$ASSET_DIR/wasm/surfer_bg.wasm" | cut -d' ' -f1))

${MATERIALS_LINE}

The upstream source for the pinned ref is:

  https://gitlab.com/surfer-project/surfer/-/tree/${SURFER_REF}

The full EUPL-1.2 text ships in this package as LICENSE-EUPL-1.2.txt.
wave-mcp itself (the consuming project) is Apache-2.0 licensed. surver runs as a
separate subprocess; the WASM runs in the browser inside an iframe. The
core shell communicates with the viewer only through page-load URL
parameters and standard window.postMessage; it does not import viewer
modules or call viewer functions.
These technical facts alone do not determine the effect on the core license.

Viewer font materials from the epaint_default_fonts crate; native surver
inclusion and font subsetting/conversion have not been established. No
unmodified-distribution assertion is made. Texts under licenses/fonts/:

  - Ubuntu Font Family (Ubuntu-Light): Copyright 2011 Canonical Ltd.
    Licensed under the Ubuntu Font Licence 1.0.
  - Noto Emoji: Copyright 2013 Google Inc. SIL Open Font License 1.1.
  - Hack: Copyright 2018 Source Foundry Authors (MIT); derived from
    Bitstream Vera Sans Mono, Copyright 2003 Bitstream, Inc.
  - emoji-icon-font: Copyright (c) 2014 John Slegers (MIT).
EOF
cp "$OUT/NOTICE" "$PKG/NOTICE"

# The viewer material inventory includes epaint_default_fonts fonts. Its
# declared license is a conjunction that includes OFL-1.1 and Ubuntu-font-1.0,
# both of which require the license text to travel with the fonts. Earlier
# asset builds shipped only the per-crate report, which recorded this component
# as an unknown license and left the font texts out. Copy them into the package
# and fail the build if they are missing.
#
# Hack needs its own notice: it is MIT (Source Foundry) plus Bitstream Vera,
# NOT OFL, so the declared "OFL-1.1" covers NotoEmoji only. See
# docs/licenses/epaint-default-fonts.SOURCES.txt for the per-font mapping.
mkdir -p "$PKG/licenses/fonts"
FONT_MISSING=0
for f in epaint-default-fonts.OFL-1.1.txt \
         epaint-default-fonts.Ubuntu-font-1.0.txt \
         epaint-default-fonts.emoji-icon-font.MIT.txt \
         epaint-default-fonts.Hack.txt \
         epaint-default-fonts.SOURCES.txt; do
  if [[ -f "$REPO_ROOT/docs/licenses/$f" ]]; then
    # An unfilled SIL/UFL template names no copyright holder, so shipping one
    # would assert compliance while carrying a blank notice. Refuse it. Only
    # license texts are scanned: SOURCES.txt documents this very rule and so
    # legitimately quotes the placeholder strings.
    if [[ "$f" != *.SOURCES.txt ]] \
       && grep -qE '<Copyright Holder>|<Reserved Font Name>|<dates>' "$REPO_ROOT/docs/licenses/$f"; then
      echo "ERROR: docs/licenses/$f still contains license template placeholders."
      echo "       Ship the notice that upstream distributes with the font, not"
      echo "       a blank template. See epaint-default-fonts.SOURCES.txt."
      FONT_MISSING=1
      continue
    fi
    cp "$REPO_ROOT/docs/licenses/$f" "$PKG/licenses/fonts/$f"
  else
    echo "ERROR: docs/licenses/$f not found; viewer font material coverage"
    echo "       requires these texts even where native inclusion is unverified."
    FONT_MISSING=1
  fi
done
[[ "$FONT_MISSING" -eq 0 ]] || exit 1
cp -r "$PKG/licenses" "$OUT/licenses"

# Required source/license materials are copied intact into the asset package.
python3 "$HERE/redistribution_materials.py" copy --component viewer \
  --materials "$VIEWER_MATERIALS" --output "$PKG/redistribution"

# --slim drops the upstream source archives that dominate the wheel (about
# 136 MB of 146 MB). The dependency notices, inventories and manifest stay,
# and both the manifest and the NOTICE point at the release for the source
# archives. The full set (the default) is attached to the GitHub release
# next to the slim wheel that goes to PyPI.
if [[ "$SLIM" = "1" ]]; then
  rm -rf "$PKG/redistribution/sources"
  python3 - "$PKG/redistribution/manifest.json" <<'PYEOF'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
manifest = json.loads(path.read_text())
manifest['files'] = {name: digest
                     for name, digest in manifest.get('files', {}).items()
                     if not name.startswith('sources/')}
manifest.setdefault('advisory_notes', []).append(
    'Slim packaging: sources/ is not embedded in this wheel. The source '
    'archives ship in the full material wheel attached to the wave-mcp '
    'GitHub release (https://github.com/Tencent/wave-mcp/releases).')
path.write_text(json.dumps(manifest, indent=1) + '\n')
PYEOF
fi

# Legacy per-crate report retained as supplemental information only.
# Optional: per-crate license report generated by deploy/build_surver_static.sh
# (cargo registry scan). Shipped alongside the binary when present.
CRATE_REPORT="$ASSET_DIR/surver-crate-licenses.txt"
if [[ -f "$CRATE_REPORT" ]]; then
  cp "$CRATE_REPORT" "$PKG/surver-crate-licenses.txt"
fi
if [[ -d "$ASSET_DIR/crates-license-files" ]]; then
  mkdir -p "$PKG/crates-license-files"
  cp "$ASSET_DIR"/crates-license-files/*.txt "$PKG/crates-license-files/" 2>/dev/null || true
fi

cat > "$OUT/pyproject.toml" <<EOF
[build-system]
requires = ["setuptools>=64"]
build-backend = "setuptools.build_meta"

[project]
name = "wave-mcp-viewer-assets"
version = "$VERSION"
description = "Waveform viewer assets (Surfer WASM + surver) for wave-mcp; version follows wave-mcp, upstream Surfer ${VIEWER_WELLEN_VERSION} (${SURFER_REF:0:12})"
readme = "README.md"
license = { text = "EUPL-1.2" }
requires-python = ">=3.10"

[tool.setuptools]
packages = ["wave_mcp_viewer_assets"]
license-files = ["LICENSE-EUPL-1.2.txt"]
# Contains a platform binary, so this is not a pure-Python distribution.
zip-safe = false

[tool.setuptools.package-data]
wave_mcp_viewer_assets = [
    "data/surver",
    "data/wasm/**",
    "LICENSE-EUPL-1.2.txt",
    "NOTICE",
    "surver-crate-licenses.txt",
    "crates-license-files/**",
    "licenses/**",
    "redistribution/**",
]

# The package ships a Linux x86-64 ELF (surver), so it must not be published
# as py3-none-any: pip would happily install it on macOS/Windows/arm64 and the
# viewer would fail at runtime instead of degrading with a clear hint.
# surver is musl static-pie with no glibc dependency, so the lowest manylinux
# tag is honest here and keeps CentOS 7 era hosts eligible.
[tool.distutils.bdist_wheel]
plat-name = "manylinux_2_17_x86_64"
EOF

cat > "$OUT/README.md" <<'EOF'
# wave-mcp-viewer-assets

Prebuilt Surfer WASM bundle + surver binary consumed by `wave-mcp`'s
viewer (`wave-view`, `open_wave_view`). Licensed EUPL-1.2 (Surfer
project); distributed separately from the Apache-2.0-licensed wave-mcp core.

The full EUPL-1.2 license text and a provenance NOTICE ship with this
package (`LICENSE-EUPL-1.2.txt`, `NOTICE`). See NOTICE for the intended
source pin, required build evidence and packaging changes. The packaging
removes upstream service workers and sample waveforms.

Not distributed by the wave-mcp project: build these assets yourself
following docs/SELF_BUILD.md in the wave-mcp repository, then install
this locally-built wheel or point WAVE_MCP_VIEWER_ASSETS at the asset
directory.
EOF

( cd "$OUT" && python3 -m pip wheel --no-deps -w dist . >/dev/null )
echo "built:"
ls -la "$OUT/dist/"
echo
echo "install locally:  pip install $OUT/dist/*.whl"
echo "offline bundle:   copy the wheel into the bundle's wheels/ dir"
