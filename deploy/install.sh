#!/usr/bin/env bash
# Install the wave-mcp offline bundle on the air-gapped target (no internet).
#
# Run from inside the unpacked bundle directory (e.g. on the shared drive):
#   ./install.sh [--prefix <install_dir>]
#
# Creates a venv (using the bundled standalone python if present, else the
# target's python3) and installs wave-mcp + deps from the offline wheelhouse.
# Then emits a ready-to-use launcher and an MCP client config snippet.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX="$HERE"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2;;
    *) echo "unknown arg: $1"; exit 1;;
  esac
done

# 1) pick a python interpreter ---------------------------------------------
if [[ -x "$HERE/python/bin/python3" ]]; then
  BASE_PY="$HERE/python/bin/python3"
  echo "[*] using bundled standalone python: $BASE_PY"
else
  BASE_PY="$(command -v python3 || true)"
  [[ -z "$BASE_PY" ]] && { echo "ERROR: no python3 found and no bundled python/. "; exit 1; }
  echo "[*] using target python3: $BASE_PY ($($BASE_PY -V 2>&1))"
  echo "    (note: the offline wheels match the build machine's Python version; if this"
  echo "     python's version differs, bundle a matching standalone python via --python instead)"
fi

# 2) create venv + offline install -----------------------------------------
RUNTIME="$PREFIX/runtime"
echo "[*] creating venv at $RUNTIME"
"$BASE_PY" -m venv "$RUNTIME"
"$RUNTIME/bin/python" -m pip install --no-index --find-links "$HERE/wheels" --upgrade pip >/dev/null 2>&1 || true
echo "[*] installing wave-mcp + deps from offline wheelhouse ..."
# Install every wheel in the offline wheelhouse with --no-deps. The wheelhouse
# already contains the complete, resolved dependency set, so we do NOT let pip
# re-resolve — this avoids pip chasing the optional `pyjwt[crypto]` extra
# (declared by mcp) and failing on the deliberately-omitted cryptography wheel
# (high glibc, unused by wave-mcp). See build_offline_bundle.sh step 1.
"$RUNTIME/bin/python" -m pip install --no-index --no-deps "$HERE"/wheels/*.whl

# 3) generate launcher ------------------------------------------------------
mkdir -p "$PREFIX/bin"
sed -e "s#@RUNTIME@#$RUNTIME#g" -e "s#@BUNDLE@#$HERE#g" "$HERE/wave-mcp.template" > "$PREFIX/bin/wave-mcp"
chmod +x "$PREFIX/bin/wave-mcp"

# 4) sanity check -----------------------------------------------------------
echo "[*] sanity check ..."
"$RUNTIME/bin/python" - <<'PY'
import wave_mcp, pylibfst, pyslang
from wave_mcp import server
print("   wave_mcp", wave_mcp.__version__, "| pyslang", pyslang.__version__, "| OK")
PY

echo
echo "[done] launcher: $PREFIX/bin/wave-mcp"
echo "Add this to each user's MCP client config (stdio). The model drives the"
echo "session via prepare_session/launch, so no fixed args are required:"
echo "-----------------------------------------------------------------------"
cat <<EOF
{
  "mcpServers": {
    "wave-mcp": {
      "command": "$PREFIX/bin/wave-mcp",
      "args": []
    }
  }
}
EOF
echo "-----------------------------------------------------------------------"
