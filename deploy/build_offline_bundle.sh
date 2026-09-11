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
# Optional inputs require matching --python-materials, --viewer-materials,
# or --vcd2fst-materials directories. See docs/PACKAGING_MATERIALS.md.
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
PYTHON_MATERIALS=""
VCD_MATERIALS=""
VIEWER_MATERIALS=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT="$2"; shift 2;;
    --python) PYTHON_SRC="$2"; shift 2;;
    --vcd2fst) VCD2FST_SRC="$2"; shift 2;;
    --target-glibc) TARGET_GLIBC="$2"; shift 2;;
    --pyslang-wheel) PYSLANG_WHEEL="$2"; shift 2;;
    --viewer) VIEWER_SRC="$2"; shift 2;;
    --python-materials) PYTHON_MATERIALS="$2"; shift 2;;
    --vcd2fst-materials) VCD_MATERIALS="$2"; shift 2;;
    --viewer-materials) VIEWER_MATERIALS="$2"; shift 2;;
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

# Check required notices before downloading or replacing an existing bundle.
LIC_SRC="$REPO_ROOT/docs/licenses"
REQUIRED_NOTICES=(LICENSE docs/THIRD_PARTY.md docs/licenses/README.md
  docs/licenses/TraceWeave-MIT.txt docs/licenses/LGPL-2.1.txt docs/licenses/EUPL-1.2.txt)
if [[ -n "$VCD2FST_SRC" ]]; then
  for f in vcd2fst.fstapi.LICENSE vcd2fst.fastlz.LICENSE vcd2fst.lz4.LICENSE; do
    REQUIRED_NOTICES+=("docs/licenses/$f")
  done
fi
for notice in "${REQUIRED_NOTICES[@]}"; do
  if [[ ! -s "$REPO_ROOT/$notice" ]]; then
    echo "ERROR: missing or empty required notice: $notice" >&2
    exit 1
  fi
done

# Every optional binary/runtime needs a matching, complete material inventory.
MATERIAL_CHECK="$REPO_ROOT/deploy/redistribution_materials.py"
check_materials() {
  local component="$1" artifact="$2" materials="$3"
  [[ -n "$materials" ]] || { echo "ERROR: --${component}-materials required with --${component}" >&2; exit 1; }
  python3 "$MATERIAL_CHECK" check --component "$component" --materials "$materials" --artifact "$artifact"
}
if [[ -n "$PYTHON_SRC" ]]; then
  check_materials python "$PYTHON_SRC" "$PYTHON_MATERIALS"
fi
if [[ -n "$VCD2FST_SRC" ]]; then
  [[ -x "$VCD2FST_SRC" ]] || { echo "ERROR: vcd2fst must be executable" >&2; exit 1; }
  check_materials vcd2fst "$VCD2FST_SRC" "$VCD_MATERIALS"
fi
if [[ -n "$VIEWER_SRC" ]]; then
  check_materials viewer "$VIEWER_SRC" "$VIEWER_MATERIALS"
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
    if ! VIEWER_LOG=$(VIEWER_MATERIALS="$VIEWER_MATERIALS" "$REPO_ROOT/deploy/build_viewer_assets.sh" "$VIEWER_SRC" 2>&1); then
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
    curl -fL "$PYTHON_SRC" -o "$OUT/_py.tar.gz"
    python3 "$MATERIAL_CHECK" check --component python --materials "$PYTHON_MATERIALS" --artifact "$OUT/_py.tar.gz"
    tar -xzf "$OUT/_py.tar.gz" -C "$OUT/python" --strip-components=1
    rm -f "$OUT/_py.tar.gz"
  elif [[ -f "$PYTHON_SRC" ]]; then
    tar -xzf "$PYTHON_SRC" -C "$OUT/python" --strip-components=1
  elif [[ -d "$PYTHON_SRC" ]]; then
    cp -r "$PYTHON_SRC"/. "$OUT/python/"
  fi
  [[ -x "$OUT/python/bin/python3" ]] || { echo "ERROR: python/bin/python3 not found" >&2; exit 1; }
  python3 "$MATERIAL_CHECK" check --component python --materials "$PYTHON_MATERIALS" --artifact "$OUT/python"
  echo "    standalone python identity OK"
else
  echo "[!] --python not given: bundle will rely on target's python3 (>= 3.10, x86_64)."
  echo "    For version-independence, fetch python-build-standalone (install_only, x86_64-unknown-linux-gnu)"
  echo "    on a connected machine and re-run with --python <tarball>."
fi

# 4) vcd2fst (+ its shared libs) for VCD->FST -------------------------------
if [[ -n "$VCD2FST_SRC" && -x "$VCD2FST_SRC" ]]; then
  echo "[*] bundling vcd2fst from: $VCD2FST_SRC (verify target glibc compatibility!)"
  cp "$VCD2FST_SRC" "$OUT/bin/vcd2fst"; mkdir -p "$OUT/bin/lib"
  # Only copy the exact runtime libraries bound to the component manifest.
  if [[ -d "$VCD_MATERIALS/runtime" ]]; then
    cp -R "$VCD_MATERIALS/runtime/." "$OUT/bin/lib/"
  fi
  echo "    bundled libs: $(ls "$OUT/bin/lib" 2>/dev/null | wc -l)"
else
  echo "[!] --vcd2fst not given: install GTKWave on the target, or copy a glibc-$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}')-or-lower vcd2fst."
fi

# 5) installer + launcher template + client config -------------------------
cp "$REPO_ROOT/deploy/install.sh"        "$OUT/install.sh"
cp "$REPO_ROOT/deploy/wave-mcp.template" "$OUT/wave-mcp.template"
cp "$REPO_ROOT/deploy/mcp.json.example"  "$OUT/mcp.json.example"
chmod +x "$OUT/install.sh"
# Named BUILD_INFO, not VERSION: this file records WHEN the bundle was built
# (plus which wave-mcp went into it), not a semantic version. A bare timestamp
# under the name VERSION read like a broken version string and sent at least
# one operator looking for a release number that was never here.
{
  echo "wave_mcp_version=$(python3 -c 'import wave_mcp; print(wave_mcp.__version__)' 2>/dev/null || echo unknown)"
  echo "build_time_utc=$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
} > "$OUT/BUILD_INFO"

# 5b) retain project notices at the bundle root and in the source tree.
# Keep THIRD_PARTY.md beside licenses/ so its license links remain valid.
cp "$REPO_ROOT/LICENSE" "$OUT/LICENSE"
cp "$REPO_ROOT/docs/THIRD_PARTY.md" "$OUT/THIRD_PARTY.md"
cp "$REPO_ROOT/docs/PACKAGING_MATERIALS.md" "$OUT/PACKAGING_MATERIALS.md"
cp -R "$LIC_SRC" "$OUT/licenses"
mkdir -p "$OUT/src/docs"
cp "$REPO_ROOT/LICENSE" "$REPO_ROOT/README.md" "$REPO_ROOT/README.en.md" \
   "$REPO_ROOT/CHANGELOG.md" "$REPO_ROOT/MANIFEST.in" "$OUT/src/"
cp "$REPO_ROOT/docs/THIRD_PARTY.md" "$REPO_ROOT/docs/PACKAGING_MATERIALS.md" "$OUT/src/docs/"
cp -R "$LIC_SRC" "$OUT/src/docs/licenses"

# 5b-2) user guides the runtime errors point at. An air-gapped user cannot open
# a repository link, so the guides referenced from error messages have to be in
# the bundle or the guidance is a dead end.
mkdir -p "$OUT/docs"
for guide in FSDB_GUIDE.md WAVE_VIEWER.md WAVE_VIEWER.en.md DEPLOY_AIRGAP.md; do
  [[ -f "$REPO_ROOT/docs/$guide" ]] && cp "$REPO_ROOT/docs/$guide" "$OUT/docs/$guide"
done

# 5b-3) fsdb2fst build inputs. The Verdi FsdbReader runtime is proprietary and
# stays on the user's machine, but the converter sources are ours to ship and
# without them the on-demand build has nothing to compile.
mkdir -p "$OUT/fsdb2fst-src/fst" "$OUT/fsdb2fst-src/deploy"
cp "$REPO_ROOT/third_party/fsdb2fst/fsdb2fst.cpp" \
   "$REPO_ROOT/third_party/fsdb2fst/ffrAPI_stub.h" \
   "$REPO_ROOT/third_party/fsdb2fst/ffrAPI_stub_impl.cpp" "$OUT/fsdb2fst-src/"
cp "$REPO_ROOT"/third_party/fsdb2fst/fst/*.c "$REPO_ROOT"/third_party/fsdb2fst/fst/*.h \
   "$OUT/fsdb2fst-src/fst/"
cp "$REPO_ROOT/deploy/build_fsdb2fst.sh" "$OUT/fsdb2fst-src/deploy/"

# 5c) retain ALL wheel notices, including declared License-File entries.
# Preserve both wheel identity and original member path to avoid collisions.
# Invalid/missing declared files, corrupt archives and write errors are fatal.
python3 - "$OUT/wheels" "$OUT/licenses/wheels" <<'PYEOF'
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import zipfile

wheel_dir, out_dir = map(Path, sys.argv[1:])
for wheel in sorted(wheel_dir.glob("*.whl")):
    with zipfile.ZipFile(wheel) as archive:
        members = [info for info in archive.infolist() if not info.is_dir()]
        names = {info.filename for info in members}
        if len(names) != len(members):
            raise ValueError(f"duplicate wheel members: {wheel.name}")
        declared = set()
        for info in members:
            if not info.filename.endswith(".dist-info/METADATA"):
                continue
            parent = PurePosixPath(info.filename).parent
            metadata = BytesParser().parsebytes(archive.read(info))
            for value in metadata.get_all("License-File", []):
                path = PurePosixPath(value)
                if path.is_absolute() or ".." in path.parts or "\\" in value:
                    raise ValueError(f"unsafe License-File in {wheel.name}: {value}")
                # PEP 639 and legacy setuptools/maturin wheel layouts.
                choices = {str(parent / subdir / path)
                           for subdir in ("licenses", "license_files", "")}
                found = choices & names
                if not found:
                    raise ValueError(f"missing License-File in {wheel.name}: {value}")
                declared.update(found)
        count = 0
        for info in members:
            path = PurePosixPath(info.filename)
            low = path.name.lower()
            is_notice = (
                info.filename in declared
                or any(part.lower() in {"licenses", "licences", "license_files"}
                       for part in path.parts[:-1])
                or low.startswith(("license", "licence", "copying", "notice", "copyright"))
                or low.endswith((".license", ".licence"))
                or low == "third_party.md"
            )
            if not is_notice:
                continue
            if path.is_absolute() or ".." in path.parts or "\\" in info.filename:
                raise ValueError(f"unsafe notice path in {wheel.name}: {info.filename}")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError(f"symlink notice in {wheel.name}: {info.filename}")
            destination = out_dir / wheel.name / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, destination.open("xb") as target:
                shutil.copyfileobj(source, target)
            count += 1
        if not count:
            print(f"    WARN: {wheel.name}: no license files detected; review upstream notices")
        else:
            print(f"    {wheel.name}: retained {count} notice files")
PYEOF
echo "    licenses/: $(find "$OUT/licenses" -type f | wc -l) files"

mkdir -p "$OUT/materials"
if [[ -n "$PYTHON_SRC" ]]; then
  python3 "$MATERIAL_CHECK" copy --component python --materials "$PYTHON_MATERIALS" --output "$OUT/materials/python"
fi
if [[ -n "$VCD2FST_SRC" ]]; then
  python3 "$MATERIAL_CHECK" copy --component vcd2fst --materials "$VCD_MATERIALS" --output "$OUT/materials/vcd2fst"
fi
if [[ -n "$VIEWER_SRC" ]]; then
  python3 "$MATERIAL_CHECK" copy --component viewer --materials "$VIEWER_MATERIALS" --output "$OUT/materials/viewer"
fi
cp "$MATERIAL_CHECK" "$OUT/materials/check.py"

echo "[*] bundle assembled at $OUT"
if [[ "$DO_TAR" == "1" ]]; then
  TAR="$OUT.tar.gz"
  tar -C "$(dirname "$OUT")" -czf "$TAR" "$(basename "$OUT")"
  echo "[*] tarball: $TAR ($(du -h "$TAR" | cut -f1))"
fi
echo "[done] copy the bundle/tarball to the shared drive and run install.sh there."
