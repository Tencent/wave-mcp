#!/usr/bin/env bash
# Build a glibc-2.17-compatible (CentOS 7) pyslang wheel in a manylinux2014
# container. Requires docker + network.
#
# Output: <out>/pyslang-<ver>-cp<XY>-manylinux2014_x86_64.whl, which feeds
# build_offline_bundle.sh via --pyslang-wheel. Re-run with --version whenever
# the pyslang requirement changes.
#
# Usage:
#   deploy/build_pyslang_manylinux2014.sh [--out DIR] [--version VER]
#                                         [--py 311] [--image IMG]
set -euo pipefail

OUT="/tmp/pyslang-manylinux2014"
PYSLANG_VER="11.0.0"
PYTAG="311"                      # must match the bundled standalone Python
IMAGE="quay.io/pypa/manylinux2014_x86_64"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT="$2"; shift 2;;
    --version) PYSLANG_VER="$2"; shift 2;;
    --py) PYTAG="$2"; shift 2;;
    --image) IMAGE="$2"; shift 2;;
    *) echo "unknown arg: $1"; exit 1;;
  esac
done

command -v docker >/dev/null || { echo "ERROR: docker required"; exit 1; }
mkdir -p "$OUT"
echo "[*] building pyslang $PYSLANG_VER (cp$PYTAG, manylinux2014) in $IMAGE -> $OUT"

docker run --rm -v "$OUT":/out "$IMAGE" bash -lc "
set -e
export PIP_DISABLE_PIP_VERSION_CHECK=1

# --- toolchain: slang needs GCC >= 11 -------------------------------------
# The image ships gcc 10, so install devtoolset-11 from a working vault mirror.
if command -v g++ >/dev/null && [ \"\$(g++ -dumpversion | cut -d. -f1)\" -ge 11 ]; then
  echo \"[toolchain] image g++ \$(g++ -dumpversion) OK\"
else
  echo '[toolchain] installing devtoolset-11 from a centos-vault mirror ...'
  mkdir -p /etc/yum.repos.d/backup && mv /etc/yum.repos.d/*.repo /etc/yum.repos.d/backup/ 2>/dev/null || true
  for MIRROR in https://mirrors.tencent.com/centos-vault https://mirrors.aliyun.com/centos-vault; do
    cat > /etc/yum.repos.d/vault.repo <<EOF
[vault-base]
name=vault-base
baseurl=\$MIRROR/7.9.2009/os/x86_64/
gpgcheck=0
[vault-updates]
name=vault-updates
baseurl=\$MIRROR/7.9.2009/updates/x86_64/
gpgcheck=0
[vault-sclo-rh]
name=vault-sclo-rh
baseurl=\$MIRROR/7.9.2009/sclo/x86_64/rh/
gpgcheck=0
EOF
    yum clean all -q >/dev/null 2>&1 || true
    if yum install -y -q devtoolset-11-gcc devtoolset-11-gcc-c++ >/dev/null 2>&1; then
      echo \"[toolchain] devtoolset-11 installed from \$MIRROR\"
      break
    fi
    echo \"[toolchain] mirror failed: \$MIRROR, trying next ...\"
  done
  source /opt/rh/devtoolset-11/enable
  echo \"[toolchain] now using g++ \$(g++ -dumpversion)\"
fi

PY=/opt/python/cp${PYTAG}-cp${PYTAG}/bin/python
\$PY -m pip -q install -U pip build auditwheel cmake ninja
export PATH=\"/opt/python/cp${PYTAG}-cp${PYTAG}/bin:\$PATH\"
cmake --version | head -1

# --- gcc 11 shim: std::hash<std::filesystem::path> was added in GCC 12 ----
# Force-include a shim; no-op on newer GCC.
cat > /tmp/path_hash_shim.hpp <<'EOF'
#pragma once
#include <filesystem>
#include <functional>
#if defined(_GLIBCXX_RELEASE) && _GLIBCXX_RELEASE < 12
namespace std {
template<> struct hash<filesystem::path> {
    size_t operator()(const filesystem::path& p) const noexcept {
        return filesystem::hash_value(p);
    }
};
}
#endif
EOF
export CXXFLAGS=\"-include /tmp/path_hash_shim.hpp \${CXXFLAGS:-}\"

# --- build from sdist -------------------------------------------------------
mkdir -p /tmp/wheel-raw
\$PY -m pip wheel \"pyslang==${PYSLANG_VER}\" --no-deps --no-binary pyslang \
    -w /tmp/wheel-raw
RAW=\$(ls /tmp/wheel-raw/pyslang-*.whl)
echo \"[build] raw wheel: \$RAW\"

# --- repair to manylinux2014 + verify --------------------------------------
auditwheel repair --plat manylinux2014_x86_64 -w /out \"\$RAW\"
FIXED=\$(ls /out/pyslang-*manylinux2014*.whl)
echo \"[audit] repaired wheel: \$FIXED\"

\$PY -m pip -q install \"\$FIXED\"
\$PY - <<'EOF'
import platform, pyslang
from pyslang.syntax import SyntaxTree
from pyslang.ast import Compilation
print('[test] pyslang', pyslang.__version__, 'imports OK on glibc', platform.libc_ver()[1])
tree = SyntaxTree.fromText('module m(input a, output b); assign b = ~a; endmodule')
c = Compilation(); c.addSyntaxTree(tree)
assert not [d for d in c.getAllDiagnostics() if d.isError()], 'elaboration failed'
print('[test] mini elaboration OK')
EOF
chmod 644 /out/*.whl
"

echo "[done] $(ls "$OUT"/pyslang-*manylinux2014*.whl)"
echo "Next: deploy/build_offline_bundle.sh --target-glibc 2.17 --pyslang-wheel <the wheel above> ..."
