#!/usr/bin/env bash
# Install the wave-mcp offline bundle on the air-gapped target (no internet).
#
# Run from inside the unpacked bundle directory (e.g. on the shared drive):
#   ./install.sh [--prefix <install_dir>] [--python <interpreter|prefix>]
#
# Creates a venv (interpreter picked by: --python > bundled standalone python
# > VIRTUAL_ENV/$PYTHON > python3 > python3.X cascade; gated on the wheel
# cp version) and installs wave-mcp + deps from the offline wheelhouse.
# Then emits a ready-to-use launcher and an MCP client config snippet.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX="$HERE"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2;;
    --python) PY_OVERRIDE="$2"; shift 2;;
    *) echo "unknown arg: $1"; exit 1;;
  esac
done

# 0) normalize the install prefix ------------------------------------------
# The launcher generated in step 3 bakes RUNTIME in as a literal string, and an
# MCP client spawns that launcher with its own cwd (the user's project dir),
# not the install dir. A relative --prefix would therefore produce a launcher
# that cannot find its own interpreter, which clients only report as -32000.
# Resolve it here so the baked path is absolute by construction.
# Creating bin/ up front doubles as a write-permission probe: a bad --prefix
# now fails in seconds instead of after the whole wheelhouse install.
if ! mkdir -p "$PREFIX/bin" 2>/dev/null; then
  echo "ERROR: cannot create $PREFIX/bin (check the path and write permission)"
  exit 1
fi
PREFIX="$(cd "$PREFIX" && pwd)"
echo "[*] install prefix: $PREFIX"

# 1) pick a python interpreter ---------------------------------------------
# Priority: --python arg > bundled standalone python > user-selected env
# (VIRTUAL_ENV / PYTHON) > python3 on PATH > versioned python3.X cascade.
# Every candidate is gated on the wheelhouse cpXX tag; a mismatched candidate
# is skipped (with a note) so install keeps trying the next one.
# cp version gate: the project wheel itself is py3-none-any (pure python),
# so anchor on the BINARY dependency wheels (cffi/pyslang/pylibfst/...) which
# carry the cpXX tag of the python the wheelhouse was built for.
WANT_CP="$(ls "$HERE"/wheels/*.whl 2>/dev/null | xargs -n1 basename 2>/dev/null \
  | grep -oE 'cp[0-9]+' | sort | uniq -c | sort -rn | head -1 | awk '{print $2}' || true)"
if [[ "$WANT_CP" == cp* ]]; then WANT_CP="${WANT_CP#cp}"; fi
BASE_PY=""
BASE_PY_WHY=""

pyver() { "$1" -c 'import sys;print("%d%d"%sys.version_info[:2])' 2>/dev/null || true; }

try_py() {  # try_py <path> <why>: set BASE_PY if executable and cp-matched
  local cand="$1" why="$2" got
  [[ -n "$cand" && -x "$cand" ]] || return 1
  if [[ -n "$WANT_CP" ]]; then
    got="$(pyver "$cand")"
    if [[ "$got" != "$WANT_CP" ]]; then
      echo "[!] skip $why: $cand is cp$got, wheels are cp$WANT_CP"
      return 1
    fi
  fi
  BASE_PY="$cand"; BASE_PY_WHY="$why"
  return 0
}

# 1a) explicit override: --python <interpreter or prefix>
if [[ -n "${PY_OVERRIDE:-}" ]]; then
  if [[ -d "$PY_OVERRIDE" ]]; then
    BASE_PY=""
    for cand in "$PY_OVERRIDE/bin/python3" "$PY_OVERRIDE/bin/python"; do
      [[ -x "$cand" ]] && { BASE_PY="$cand"; break; }
    done
    [[ -z "$BASE_PY" ]] && { echo "ERROR: no python executable under $PY_OVERRIDE/bin/"; exit 1; }
  else
    [[ -x "$PY_OVERRIDE" ]] || { echo "ERROR: --python not executable: $PY_OVERRIDE"; exit 1; }
    BASE_PY="$PY_OVERRIDE"
  fi
  BASE_PY_WHY="--python override"
  if [[ -n "$WANT_CP" && "$(pyver "$BASE_PY")" != "$WANT_CP" ]]; then
    echo "ERROR: --python is cp$(pyver "$BASE_PY"), wheels are cp$WANT_CP."
    exit 1
  fi
elif [[ -x "$HERE/python/bin/python3" ]]; then
  BASE_PY="$HERE/python/bin/python3"
  BASE_PY_WHY="bundled standalone python"
fi

# 1b) still nothing: user-selected env, then PATH, then versioned cascade.
if [[ -z "$BASE_PY" ]]; then
  VE_PY="${VIRTUAL_ENV:-/nonexistent}/bin/python"
  [[ -x "$VE_PY" && "${VIRTUAL_ENV:-}" != "/" && -n "${VIRTUAL_ENV:-}" ]] \
    && try_py "$VE_PY" "VIRTUAL_ENV" || true
  PY_ENV="${PYTHON:-}"; PY_ENV="${PY_ENV/#\~/$HOME}"
  [[ -n "${PYTHON:-}" && -x "$PY_ENV" ]] && try_py "$PY_ENV" '$PYTHON' || true
  try_py "$(command -v python3 2>/dev/null || true)" "PATH python3" || true
  if [[ -z "$BASE_PY" ]]; then
    for v in 3.13 3.12 3.11 3.10 3.9 3.8; do
      try_py "$(command -v "python$v" 2>/dev/null || true)" "python$v" && break
    done
  fi
fi

if [[ -z "$BASE_PY" ]]; then
  echo "ERROR: no usable python found and no bundled python."
  [[ -n "$WANT_CP" ]] && echo "       wheels need a cp$WANT_CP interpreter (python 3.$(echo "$WANT_CP" | sed 's/^3//').x)."
  echo "       fix: install python, or rebuild the bundle with"
  echo "       --python <standalone tarball> for version independence."
  exit 1
fi
echo "[*] using python: $BASE_PY ($("$BASE_PY" -V 2>&1)) [$BASE_PY_WHY]"

# 2) create venv + offline install -----------------------------------------
RUNTIME="$PREFIX/runtime"
echo "[*] creating venv at $RUNTIME"
"$BASE_PY" -m venv "$RUNTIME"
"$RUNTIME/bin/python" -m pip install --no-index --find-links "$HERE/wheels" --upgrade pip >/dev/null 2>&1 || true
echo "[*] installing wave-mcp + deps from offline wheelhouse ..."
# Install every wheel in the offline wheelhouse with --no-deps. The wheelhouse
# already contains the complete, resolved dependency set (including a
# glibc-2.28-compatible cryptography build required by mcp SDK v2), so we do
# NOT let pip re-resolve. See build_offline_bundle.sh step 1.
"$RUNTIME/bin/python" -m pip install --no-index --no-deps "$HERE"/wheels/*.whl

# 3) generate launcher ------------------------------------------------------
sed -e "s#@RUNTIME@#$RUNTIME#g" -e "s#@BUNDLE@#$HERE#g" "$HERE/wave-mcp.template" > "$PREFIX/bin/wave-mcp"
chmod +x "$PREFIX/bin/wave-mcp"

# 4) sanity check -----------------------------------------------------------
# Check the import surface first, then the launcher itself. The launcher check
# runs from a DIFFERENT cwd on purpose: an MCP client starts it from the user's
# project dir, so any path in it that depends on cwd must fail here, at install
# time, rather than in the client as a bare -32000.
echo "[*] sanity check: imports ..."
"$RUNTIME/bin/python" - <<'PY'
import wave_mcp, pylibfst, pyslang
from wave_mcp import server
print("   wave_mcp", wave_mcp.__version__, "| pyslang", pyslang.__version__, "| OK")
PY

echo "[*] sanity check: launcher (from an unrelated cwd) ..."
if ! LAUNCH_OUT=$(cd / && "$PREFIX/bin/wave-mcp" query --help 2>&1); then
  echo "ERROR: the generated launcher failed to start from cwd '/'."
  echo "       This is exactly how an MCP client will start it, so fix it now:"
  echo "$LAUNCH_OUT" | sed 's/^/       /'
  exit 1
fi
echo "   launcher OK"

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
