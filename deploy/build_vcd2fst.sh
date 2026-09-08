#!/usr/bin/env bash
# Build vcd2fst and its matching redistribution material directory.
# The linked helper/FST/FastLZ sources use MIT, LZ4 BSD-2-Clause, and
# jrb LGPL-2.1-or-later. Retain their actual source notices and relink inputs.
# This script does not establish compliance with every distribution obligation.
# Usage: build_vcd2fst.sh [--out DIR] [--gtkwave VER] [--src TARBALL] [--image IMG]
set -euo pipefail
OUT="/tmp/vcd2fst-out"
GTKWAVE_VER="3.3.121"
IMAGE="quay.io/pypa/manylinux_2_28_x86_64"
SRC=""
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT="$2"; shift 2;;
    --gtkwave) GTKWAVE_VER="$2"; shift 2;;
    --src) SRC="$2"; shift 2;;
    --image) IMAGE="$2"; shift 2;;
    *) echo "unknown arg: $1"; exit 1;;
  esac
done
command -v docker >/dev/null || { echo "ERROR: docker required"; exit 1; }
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
[[ ! -e "$OUT/redistribution" ]] || { echo "ERROR: use a fresh output directory"; exit 1; }
SRC_MOUNT=()
if [[ -n "$SRC" ]]; then
  [[ -f "$SRC" ]] || { echo "ERROR: source tarball not found"; exit 1; }
  SRC_ABS="$(cd "$(dirname "$SRC")" && pwd)/$(basename "$SRC")"
  SRC_MOUNT=(-v "$SRC_ABS":/src.tar.gz:ro)
fi

docker run --rm -i -v "$OUT":/out -v "$REPO_ROOT":/wave-repo:ro \
  "${SRC_MOUNT[@]}" -e GTKWAVE_VER="$GTKWAVE_VER" "$IMAGE" bash -s <<'BUILD'
set -euo pipefail
if ! rpm -q zlib-devel >/dev/null; then
  dnf -y -q install zlib-devel wget tar gzip
fi
cd /tmp
if [[ -f /src.tar.gz ]]; then
  cp /src.tar.gz gw.tar.gz
else
  curl -fL "https://gtkwave.sourceforge.net/gtkwave-${GTKWAVE_VER}.tar.gz" -o gw.tar.gz
fi
if [[ "$GTKWAVE_VER" == 3.3.121 ]]; then
  echo '5b05b6469bca675d9c9de60cf7ce8ad33afe13ef71aa29777898ced2ffe88397  gw.tar.gz' | sha256sum -c -
fi
/opt/python/cp311-cp311/bin/python - <<'PY'
import tarfile
with tarfile.open('/tmp/gw.tar.gz') as archive:
    archive.extractall('/tmp', filter='data')
PY
cd "/tmp/gtkwave-${GTKWAVE_VER}"
mkdir -p stub /out/relink
printf '#define PACKAGE_BUGREPORT "gtkwave"\n#define PACKAGE_VERSION "%s"\n#define PACKAGE_STRING "gtkwave %s"\n' "$GTKWAVE_VER" "$GTKWAVE_VER" > stub/config.h
printf '#define _(x) x\n#define WAVE_LOCALE_FIX\n#define WAVE_LOCALE_RELOAD\n' > stub/wave_locale.h
printf '#define _GNU_SOURCE 1\n#define __USE_GNU 1\n#include <getopt.h>\n' > stub/prelude.h
for source in src/helpers/vcd2fst.c src/helpers/fst/fstapi.c src/helpers/fst/lz4.c src/helpers/fst/fastlz.c contrib/rtlbrowse/jrb.c; do
  gcc -O2 -w -D_GNU_SOURCE -D__USE_GNU -DHAVE_LIBPTHREAD=1 -DFST_WRITER_PARALLEL=1 \
    -include stub/prelude.h -I stub -I src/helpers -I src/helpers/fst \
    -c "$source" -o "/out/relink/$(basename "${source%.c}").o"
done
gcc -o /out/vcd2fst /out/relink/*.o -lz -lpthread -Wl,-rpath,'$ORIGIN/lib'
cat > /out/relink/relink.sh <<'RELINK'
#!/usr/bin/env bash
# Supply an object built from your chosen jrb implementation as argument 1.
# The matching source and generated stub headers are under ../sources/.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
JRB_OBJECT="${1:-$HERE/jrb.o}"
OUTPUT="${2:-$HERE/vcd2fst-relinked}"
"${CC:-cc}" -o "$OUTPUT" "$HERE/vcd2fst.o" "$HERE/fstapi.o" \
  "$HERE/lz4.o" "$HERE/fastlz.o" "$JRB_OBJECT" -lz -lpthread
RELINK
chmod +x /out/relink/relink.sh
printf '$timescale 1ns $end\n$scope module t $end\n$var wire 1 ! a $end\n$upscope $end\n$enddefinitions $end\n#0\n0!\n#1\n1!\n' > /out/relink/smoke.vcd
/out/vcd2fst -F -p -v /out/relink/smoke.vcd -f /tmp/original.fst
/out/relink/relink.sh /out/relink/jrb.o /tmp/relinked
/tmp/relinked -F -p -v /out/relink/smoke.vcd -f /tmp/relinked.fst
test -s /tmp/original.fst && test -s /tmp/relinked.fst
gcc --version > /out/relink/toolchain.txt
/opt/python/cp311-cp311/bin/python /wave-repo/deploy/export_vcd_materials.py \
  --source "$PWD" --archive /tmp/gw.tar.gz --output /out --repo /wave-repo --version "$GTKWAVE_VER"
BUILD

echo "Built $OUT/vcd2fst and matching $OUT/redistribution"
echo "Pass both via --vcd2fst and --vcd2fst-materials to build_offline_bundle.sh."
