#!/usr/bin/env bash
# Build a self-contained offline bundle for the air-gapped network.
#
# Run this on a CONNECTED machine that matches the target ARCH (x86_64) and a
# Python whose wheels match the bundled runtime (default cp311). It produces a
# directory + tarball that can be copied to the shared drive on the air-gapped
# network and installed with install.sh (no internet, no compiler needed).
#
# Bundle contents:
#   python/      standalone Python 3.11 (relocatable)        [--python]
#   wheels/      offline wheelhouse (this project + all deps)
#   src/         project source (also built as a wheel in wheels/)
#   bin/vcd2fst  glibc-compatible vcd2fst + libs              [--vcd2fst] (optional)
#   install.sh, wave-mcp (launcher), mcp.json.example
#
# Usage:
#   deploy/build_offline_bundle.sh --out /tmp/wave-mcp-bundle \
#       [--python <cpython-*-install_only.tar.gz | dir | URL>] \
#       [--vcd2fst /usr/bin/vcd2fst] [--no-tar]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT=""
PYTHON_SRC=""
VCD2FST_SRC=""
DO_TAR=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT="$2"; shift 2;;
    --python) PYTHON_SRC="$2"; shift 2;;
    --vcd2fst) VCD2FST_SRC="$2"; shift 2;;
    --no-tar) DO_TAR=0; shift;;
    *) echo "unknown arg: $1"; exit 1;;
  esac
done
[[ -z "$OUT" ]] && { echo "ERROR: --out <dir> required"; exit 1; }

echo "[*] bundle output: $OUT"
rm -rf "$OUT"; mkdir -p "$OUT"/{wheels,src,bin}

# 1) offline wheelhouse: project wheel + all dependency wheels --------------
echo "[*] building project wheel + downloading dependency wheels ..."
python3 -m pip wheel --no-deps -w "$OUT/wheels" "$REPO_ROOT" >/dev/null
python3 -m pip download -r "$REPO_ROOT/requirements.txt" -d "$OUT/wheels" >/dev/null

# Drop cryptography: it is only an OPTIONAL extra of pyjwt (pulled transitively
# via mcp -> pyjwt[crypto]) and is NOT used by wave-mcp. Recent cryptography
# wheels require a high glibc (e.g. 49.x needs manylinux_2_34 / glibc 2.34),
# which breaks install on older air-gapped targets (glibc 2.28). Removing it
# keeps the base MCP server (HS256 JWT + all tools) fully functional.
CRYPTO_WHL=$(ls "$OUT/wheels"/cryptography-*.whl 2>/dev/null || true)
if [[ -n "$CRYPTO_WHL" ]]; then
  echo "[*] dropping unnecessary high-glibc dep: $(basename "$CRYPTO_WHL")"
  rm -f "$CRYPTO_WHL"
fi
echo "    wheels: $(ls "$OUT/wheels" | wc -l) files"

# 2) project source (for reference / editable use) --------------------------
cp -r "$REPO_ROOT/wave_mcp" "$OUT/src/"
cp "$REPO_ROOT/requirements.txt" "$REPO_ROOT/pyproject.toml" "$OUT/src/" 2>/dev/null || true

# 3) standalone python (relocatable; makes target Python-version-independent)
if [[ -n "$PYTHON_SRC" ]]; then
  echo "[*] adding standalone python from: $PYTHON_SRC"
  mkdir -p "$OUT/python"
  if [[ "$PYTHON_SRC" == http*://* ]]; then
    curl -sL "$PYTHON_SRC" -o "$OUT/_py.tar.gz"; tar -xzf "$OUT/_py.tar.gz" -C "$OUT/python" --strip-components=1; rm -f "$OUT/_py.tar.gz"
  elif [[ -f "$PYTHON_SRC" ]]; then
    tar -xzf "$PYTHON_SRC" -C "$OUT/python" --strip-components=1
  elif [[ -d "$PYTHON_SRC" ]]; then
    cp -r "$PYTHON_SRC"/. "$OUT/python/"
  fi
  [[ -x "$OUT/python/bin/python3" ]] && echo "    standalone python OK" || echo "    WARN: python/bin/python3 not found"
else
  echo "[!] --python not given: bundle will rely on target's python3 (>=3.9, x86_64)."
  echo "    For version-independence, fetch python-build-standalone (install_only, x86_64-unknown-linux-gnu)"
  echo "    on a connected machine and re-run with --python <tarball>."
fi

# 4) vcd2fst (+ its shared libs) for VCD->FST -------------------------------
if [[ -n "$VCD2FST_SRC" && -x "$VCD2FST_SRC" ]]; then
  echo "[*] bundling vcd2fst from: $VCD2FST_SRC (verify target glibc compatibility!)"
  cp "$VCD2FST_SRC" "$OUT/bin/vcd2fst"; mkdir -p "$OUT/bin/lib"
  ldd "$VCD2FST_SRC" | awk '/=>/{print $3}' | grep -E 'libJudy|libz|libtdsp|libonion' | while read -r so; do
    [[ -f "$so" ]] && cp -L "$so" "$OUT/bin/lib/" || true
  done
  echo "    bundled libs: $(ls "$OUT/bin/lib" 2>/dev/null | wc -l)"
else
  echo "[!] --vcd2fst not given: install GTKWave on the target, or copy a glibc-$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}')-or-lower vcd2fst."
fi

# 5) installer + launcher template + client config -------------------------
cp "$REPO_ROOT/deploy/install.sh"        "$OUT/install.sh"
cp "$REPO_ROOT/deploy/wave-mcp.template" "$OUT/wave-mcp.template"
cp "$REPO_ROOT/deploy/mcp.json.example"  "$OUT/mcp.json.example"
chmod +x "$OUT/install.sh"
date -u +"%Y-%m-%dT%H:%M:%SZ" > "$OUT/VERSION"

# 5b) license + third-party notices (MIT project + bundled binary provenance)
cp "$REPO_ROOT/LICENSE" "$OUT/LICENSE" 2>/dev/null || echo "[!] no top-level LICENSE found"
[[ -d "$REPO_ROOT/licenses" ]] && cp -r "$REPO_ROOT/licenses" "$OUT/licenses"

echo "[*] bundle assembled at $OUT"
if [[ "$DO_TAR" == "1" ]]; then
  TAR="$OUT.tar.gz"
  tar -C "$(dirname "$OUT")" -czf "$TAR" "$(basename "$OUT")"
  echo "[*] tarball: $TAR ($(du -h "$TAR" | cut -f1))"
fi
echo "[done] copy the bundle/tarball to the shared drive and run install.sh there."
