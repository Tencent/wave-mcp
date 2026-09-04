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
#   licenses/    full license texts of all redistributed components
#   install.sh, wave-mcp (launcher), mcp.json.example
#
# Usage:
#   deploy/build_offline_bundle.sh --out /tmp/wave-mcp-bundle \
#       [--target-glibc 2.28|2.17] \
#       [--python <cpython-*-install_only.tar.gz | dir | URL>] \
#       [--pyslang-wheel <pyslang-*manylinux2014*.whl>] \
#       [--vcd2fst /usr/bin/vcd2fst] [--no-tar]
#
# --target-glibc sets the minimum glibc of the TARGET machines (default 2.28):
#   2.28  official pyslang/cryptography wheels (Ubuntu 18.10+ / CentOS 8+)
#   2.17  CentOS 7 / RHEL 7 support; requires a self-built pyslang wheel from
#         deploy/build_pyslang_manylinux2014.sh, passed via --pyslang-wheel
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT=""
PYTHON_SRC=""
VCD2FST_SRC=""
DO_TAR=1
TARGET_GLIBC="2.28"
PYSLANG_WHEEL=""
VIEWER_SRC=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT="$2"; shift 2;;
    --python) PYTHON_SRC="$2"; shift 2;;
    --vcd2fst) VCD2FST_SRC="$2"; shift 2;;
    --target-glibc) TARGET_GLIBC="$2"; shift 2;;
    --pyslang-wheel) PYSLANG_WHEEL="$2"; shift 2;;
    --viewer) VIEWER_SRC="$2"; shift 2;;
    --no-tar) DO_TAR=0; shift;;
    *) echo "unknown arg: $1"; exit 1;;
  esac
done
[[ -z "$OUT" ]] && { echo "ERROR: --out <dir> required"; exit 1; }
# Absolutize --out: step 6 does `tar -C "$(dirname "$OUT")"`, which degrades to
# "." for a bare name and would silently depend on the caller's cwd.
mkdir -p "$(dirname "$OUT")" 2>/dev/null || true
OUT="$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"
case "$TARGET_GLIBC" in
  2.28|2.17) ;;
  *) echo "ERROR: --target-glibc must be 2.28 or 2.17"; exit 1;;
esac
if [[ "$TARGET_GLIBC" == "2.17" && -z "$PYSLANG_WHEEL" ]]; then
  echo "ERROR: --target-glibc 2.17 requires --pyslang-wheel (official pyslang"
  echo "       wheels need glibc >= 2.27). Build one first:"
  echo "       deploy/build_pyslang_manylinux2014.sh --out /tmp/pyslang-manylinux2014"
  exit 1
fi
if [[ -n "$PYSLANG_WHEEL" && ! -f "$PYSLANG_WHEEL" ]]; then
  echo "ERROR: --pyslang-wheel not found: $PYSLANG_WHEEL"; exit 1
fi

echo "[*] bundle output: $OUT"
rm -rf "$OUT"; mkdir -p "$OUT"/{wheels,src,bin}

# 1) offline wheelhouse: project wheel + all dependency wheels --------------
echo "[*] building project wheel + downloading dependency wheels (target glibc >= $TARGET_GLIBC) ..."
python3 -m pip wheel --no-deps -w "$OUT/wheels" "$REPO_ROOT" >/dev/null
python3 -m pip download -r "$REPO_ROOT/requirements.txt" -d "$OUT/wheels" >/dev/null

# mcp SDK v2 imports cryptography at module load, so it must be present.
# Ensure the bundled wheel matches the target glibc baseline.
CRYPTO_WHL=$(ls "$OUT/wheels"/cryptography-*.whl 2>/dev/null || true)
if [[ -n "$CRYPTO_WHL" ]]; then
  if [[ "$TARGET_GLIBC" == "2.17" && "$CRYPTO_WHL" != *manylinux2014* && "$CRYPTO_WHL" != *manylinux_2_17* ]]; then
    echo "[*] replacing cryptography with a manylinux2014 (glibc 2.17) build ..."
    CRYPTO_VER=$(basename "$CRYPTO_WHL" | cut -d- -f2)
    rm -f "$CRYPTO_WHL"
    python3 -m pip download "cryptography==$CRYPTO_VER" --no-deps -d "$OUT/wheels" \
        --only-binary=:all: --platform manylinux2014_x86_64 \
        --python-version "$(python3 -c 'import sys;print("%d%d"%sys.version_info[:2])')" >/dev/null
  elif [[ "$TARGET_GLIBC" == "2.28" && "$CRYPTO_WHL" == *manylinux_2_34* ]]; then
    echo "[*] replacing high-glibc cryptography with manylinux_2_28 build ..."
    rm -f "$CRYPTO_WHL"
    python3 -m pip download "cryptography==43.0.3" --no-deps -d "$OUT/wheels" >/dev/null
  fi
fi

# pyslang: official wheels are manylinux_2_27+; for 2.17 targets swap in the
# self-built manylinux2014 wheel (deploy/build_pyslang_manylinux2014.sh).
if [[ -n "$PYSLANG_WHEEL" ]]; then
  echo "[*] using self-built pyslang wheel: $(basename "$PYSLANG_WHEEL")"
  rm -f "$OUT/wheels"/pyslang-*.whl
  cp "$PYSLANG_WHEEL" "$OUT/wheels/"
fi

# viewer assets (optional): accept a prebuilt wave_mcp_viewer_assets wheel
# or a raw asset dir (surver + wasm/), packed on the fly. For 2.17 targets
# the surver inside MUST be the musl static build
# (deploy/build_surver_static.sh) — the official binary needs glibc 2.34.
if [[ -n "$VIEWER_SRC" ]]; then
  if [[ -f "$VIEWER_SRC" && "$VIEWER_SRC" == *.whl ]]; then
    echo "[*] bundling viewer assets wheel: $(basename "$VIEWER_SRC")"
    cp "$VIEWER_SRC" "$OUT/wheels/"
  elif [[ -d "$VIEWER_SRC" ]]; then
    echo "[*] packing viewer assets from dir: $VIEWER_SRC"
    # Do NOT swallow this: a wellen version mismatch (or any other build
    # failure) must surface here, otherwise the bundle step just stops with
    # no clue (measured 2026-09-02). Capture, then replay on failure.
    if ! VIEWER_LOG=$("$REPO_ROOT/deploy/build_viewer_assets.sh" "$VIEWER_SRC" 2>&1); then
      echo "ERROR: failed to pack viewer assets from $VIEWER_SRC"
      echo "$VIEWER_LOG" | sed 's/^/       /'
      exit 1
    fi
    if [[ -n "${VERBOSE:-}" ]]; then
      echo "$VIEWER_LOG" | sed 's/^/       /'
    fi
    cp "$REPO_ROOT"/deploy/viewer-assets-build/dist/wave_mcp_viewer_assets-*.whl \
       "$OUT/wheels/"
  else
    echo "ERROR: --viewer must be a .whl or an asset dir"; exit 1
  fi
  if [[ "$TARGET_GLIBC" == "2.17" ]]; then
    echo "    NOTE: verify the bundled surver is the musl static build;"
    echo "          official surver binaries need glibc >= 2.34."
  fi
fi
echo "    wheels: $(ls "$OUT/wheels" | wc -l) files"

# 1b) audit: fail loudly if ANY wheel needs a newer glibc than the target ----
# (catches silent baseline bumps when deps are added or upgraded later)
echo "[*] auditing wheel platform tags against target glibc $TARGET_GLIBC ..."
python3 - "$OUT/wheels" "$TARGET_GLIBC" <<'PYEOF'
import os, re, sys
wheel_dir, target = sys.argv[1], tuple(map(int, sys.argv[2].split(".")))
LEGACY = {"manylinux1": (2, 5), "manylinux2010": (2, 12), "manylinux2014": (2, 17)}
bad = []
for fn in sorted(os.listdir(wheel_dir)):
    if not fn.endswith(".whl"):
        continue
    plat = fn[:-4].split("-")[-1]
    if plat.startswith(("any", "py3")):
        continue
    reqs = []
    for tag in plat.split("."):
        m = re.match(r"manylinux_(\d+)_(\d+)", tag)
        if m:
            reqs.append((int(m.group(1)), int(m.group(2))))
        elif tag.split("_")[0] in LEGACY:
            reqs.append(LEGACY[tag.split("_")[0]])
    if reqs and min(reqs) > target:
        bad.append((fn, min(reqs)))
if bad:
    for fn, req in bad:
        print(f"    FAIL {fn}: needs glibc >= {req[0]}.{req[1]}")
    sys.exit(1)
print("    all wheels compatible with glibc >=", ".".join(map(str, target)))
PYEOF

# 2) project source (for reference / editable use) --------------------------
cp -r "$REPO_ROOT/wave_mcp" "$OUT/src/"
cp "$REPO_ROOT/requirements.txt" "$REPO_ROOT/pyproject.toml" "$OUT/src/" 2>/dev/null || true

# 2b) field test kit + built-in sample (fieldkit selftest needs examples/sample)
echo "[*] adding fieldkit + regression entry + sample session ..."
mkdir -p "$OUT/tests"
cp -r "$REPO_ROOT/tests/fieldkit" "$OUT/tests/fieldkit"
cp -r "$REPO_ROOT/tests/unit"     "$OUT/tests/unit"
cp    "$REPO_ROOT/tests/run_regression.py" "$OUT/tests/"
cp    "$REPO_ROOT/tests/README.md"         "$OUT/tests/" 2>/dev/null || true
mkdir -p "$OUT/tests/fourstate"
cp -r "$REPO_ROOT/tests/fourstate/rtl" "$REPO_ROOT/tests/fourstate/tb" \
      "$REPO_ROOT/tests/fourstate"/run_fourstate*.py "$OUT/tests/fourstate/" 2>/dev/null || true
mkdir -p "$OUT/examples"
cp -r "$REPO_ROOT/examples/sample" "$OUT/examples/sample"

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
  echo "[!] --python not given: bundle will rely on target's python3 (>= 3.10, x86_64)."
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
cp "$REPO_ROOT/docs/THIRD_PARTY.md" "$OUT/THIRD_PARTY.md" 2>/dev/null || true

# 5c) full license texts for redistributed third-party components -----------
# MIT/BSD require retaining the license+copyright notice on redistribution;
# LGPL-2.1 requires the license text; EUPL-1.2 likewise. THIRD_PARTY.md alone
# does not satisfy this, so ship a licenses/ directory next to the binaries.
mkdir -p "$OUT/licenses"
LIC_SRC="$REPO_ROOT/docs/licenses"
if [[ -n "$VCD2FST_SRC" && -x "$VCD2FST_SRC" ]]; then
  cp "$LIC_SRC/LGPL-2.1.txt" "$OUT/licenses/" 2>/dev/null || \
    echo "[!] missing $LIC_SRC/LGPL-2.1.txt (jrb component)"
  # permissive components compiled into vcd2fst (MIT/BSD texts)
  for f in vcd2fst.fstapi.LICENSE vcd2fst.fastlz.LICENSE vcd2fst.lz4.LICENSE; do
    [[ -f "$LIC_SRC/$f" ]] && cp "$LIC_SRC/$f" "$OUT/licenses/"
  done
fi
if [[ -n "$VIEWER_SRC" ]]; then
  cp "$LIC_SRC/EUPL-1.2.txt" "$OUT/licenses/" 2>/dev/null || \
    echo "[!] missing $LIC_SRC/EUPL-1.2.txt (surfer component)"
fi
# wheelhouse notices: copy every wheel's own license file (covers all pip
# deps: pyslang, mcp, cryptography, pylibfst, ... and the viewer assets
# wheel, which carries its embedded EUPL text).
python3 - "$OUT/wheels" "$OUT/licenses" <<'PYEOF'
import base64, os, sys
wheel_dir, out_dir = sys.argv[1], sys.argv[2]
import zipfile
for fn in sorted(os.listdir(wheel_dir)):
    if not fn.endswith(".whl"):
        continue
    try:
        with zipfile.ZipFile(os.path.join(wheel_dir, fn)) as z:
            for name in z.namelist():
                base = name.rsplit("/", 1)[-1]
                low = base.lower()
                if low.startswith(("license", "licence", "copying", "notice")) and \
                   low.endswith((".txt", ".md", ".rst", "")):
                    stem = fn.split("-")[0]
                    data = z.read(name)
                    # skip huge vendored trees (e.g. cryptography's rust crates)
                    if len(data) > 2_000_000:
                        continue
                    with open(os.path.join(out_dir, f"{stem}.{base or 'LICENSE'}"), "wb") as f:
                        f.write(data)
                    break
    except Exception as e:
        print(f"    [warn] {fn}: {e}")
PYEOF
echo "    licenses/: $(ls "$OUT/licenses" 2>/dev/null | wc -l) files"

echo "[*] bundle assembled at $OUT"
if [[ "$DO_TAR" == "1" ]]; then
  TAR="$OUT.tar.gz"
  tar -C "$(dirname "$OUT")" -czf "$TAR" "$(basename "$OUT")"
  echo "[*] tarball: $TAR ($(du -h "$TAR" | cut -f1))"
fi
echo "[done] copy the bundle/tarball to the shared drive and run install.sh there."
