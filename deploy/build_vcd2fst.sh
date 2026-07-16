#!/usr/bin/env bash
# Build a glibc-portable vcd2fst inside a manylinux_2_28 container (glibc 2.28).
#
# Why: the air-gapped target has glibc 2.28 and no GTKWave. A vcd2fst built on a
# newer host (e.g. glibc 2.38) would fail there with "GLIBC_2.3x not found".
# Building in manylinux_2_28 yields a binary that needs only GLIBC_2.14 + libz +
# libpthread — runs on essentially any modern Linux (incl. glibc 2.28).
#
# Output: <out>/vcd2fst  (feed it to build_offline_bundle.sh --vcd2fst <path>)
#
# Usage:  deploy/build_vcd2fst.sh [--out /tmp/vcd2fst-out] [--gtkwave 3.3.121]
set -euo pipefail

OUT="/tmp/vcd2fst-out"
GTKWAVE_VER="3.3.121"
IMAGE="quay.io/pypa/manylinux_2_28_x86_64"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT="$2"; shift 2;;
    --gtkwave) GTKWAVE_VER="$2"; shift 2;;
    --image) IMAGE="$2"; shift 2;;
    *) echo "unknown arg: $1"; exit 1;;
  esac
done

command -v docker >/dev/null || { echo "ERROR: docker required"; exit 1; }
mkdir -p "$OUT"
echo "[*] building vcd2fst (gtkwave $GTKWAVE_VER) in $IMAGE -> $OUT/vcd2fst"

docker run --rm -v "$OUT":/out "$IMAGE" bash -lc "
set -e
dnf -y -q install zlib-devel wget tar gzip >/dev/null 2>&1
cd /tmp
wget -q https://gtkwave.sourceforge.net/gtkwave-${GTKWAVE_VER}.tar.gz -O gw.tar.gz
tar xzf gw.tar.gz
cd gtkwave-${GTKWAVE_VER}
mkdir -p stub
printf '#define PACKAGE_BUGREPORT \"gtkwave\"\n#define PACKAGE_VERSION \"${GTKWAVE_VER}\"\n#define PACKAGE_STRING \"gtkwave ${GTKWAVE_VER}\"\n' > stub/config.h
printf '#define _(x) x\n#define WAVE_LOCALE_FIX\n#define WAVE_LOCALE_RELOAD\n' > stub/wave_locale.h
printf '#define _GNU_SOURCE 1\n#define __USE_GNU 1\n#include <getopt.h>\n' > stub/prelude.h
JRB=\$(find . -name jrb.c | head -1)
# NOTE: enabling the parallel writer needs TWO macros, not one. fstapi.c does:
#     #ifndef HAVE_LIBPTHREAD
#     #undef FST_WRITER_PARALLEL      <-- silently kills a bare -DFST_WRITER_PARALLEL
#     #endif
#     #ifdef FST_WRITER_PARALLEL ...  <-- the actual parallel code path
# In a normal ./configure build HAVE_LIBPTHREAD is written into config.h after
# pthread is detected; we use a stub config.h (no ./configure), so it is absent
# and the '#undef' wipes out FST_WRITER_PARALLEL -> fstWriterSetParallelMode()
# hits '#ifndef FST_WRITER_PARALLEL' and exit(255). Defining BOTH here is the
# exact equivalent of the autotools '--enable-fst-writer-parallel'. pthread is
# already linked (-lpthread), so no extra deps are needed.
gcc -O2 -w -D_GNU_SOURCE -D__USE_GNU -DHAVE_LIBPTHREAD=1 -DFST_WRITER_PARALLEL=1 -include stub/prelude.h \
    -I stub -I src/helpers -I src/helpers/fst -o /out/vcd2fst \
    src/helpers/vcd2fst.c src/helpers/fst/fstapi.c src/helpers/fst/lz4.c \
    src/helpers/fst/fastlz.c \"\$JRB\" -lz -lpthread
chmod 755 /out/vcd2fst
echo '--- ldd ---'; ldd /out/vcd2fst
echo -n '--- max GLIBC symbol: '; objdump -T /out/vcd2fst | grep -oE 'GLIBC_[0-9.]+' | sort -uV | tail -1
# self-test: prove '-p' (parallel) actually works now, else fail the build so a
# broken binary is never shipped again.
echo '--- parallel (-p) self-test ---'
td=\$(mktemp -d)
printf '\$timescale 1ns \$end\n\$scope module t \$end\n\$var wire 1 ! a \$end\n\$upscope \$end\n\$enddefinitions \$end\n#0\n0!\n#1\n1!\n' > \$td/p.vcd
if /out/vcd2fst -F -p -v \$td/p.vcd -f \$td/p.fst 2>\$td/err && [ -s \$td/p.fst ]; then
  echo 'PASS: -p works (FST_WRITER_PARALLEL enabled)'
else
  echo 'FAIL: -p still aborts:'; cat \$td/err; exit 1
fi
"

echo "[done] $OUT/vcd2fst"
echo "Next: deploy/build_offline_bundle.sh --out <bundle> --python <cpython-tar> --vcd2fst $OUT/vcd2fst"
