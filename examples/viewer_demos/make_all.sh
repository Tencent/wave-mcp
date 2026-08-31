#!/usr/bin/env bash
# Build all viewer demo waveforms: iverilog sim -> VCD -> FST -> session.
#
# Usage:
#   ./make_all.sh            # build everything
#   ./make_all.sh xprop      # rebuild a single demo
#
# Requires on PATH: iverilog, vcd2fst. Python 3.10+ with wave_mcp installed
# (or WAVE_MCP_VIEWER_ASSETS set for the viewer tools).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/waves"
ALL="xprop fsm_stuck cdc crc_diff"
PY="${WAVE_MCP_PYTHON:-python3}"

mkdir -p "$OUT"

build_one() {
  local demo="$1"
  echo "== $demo =="
  (cd "$HERE/rtl/$demo" && iverilog -g2012 -o "$OUT/$demo.vvp" *.sv \
      ${demo:+$([ "$demo" = "crc_diff" ] && echo "")})
  # crc_diff: two runs, pass (PATTERN_A) and fail (PATTERN_B)
  if [[ "$demo" == "crc_diff" ]]; then
    (cd "$HERE/rtl/crc_diff" && iverilog -g2012 -DPATTERN_B -o "$OUT/crc_diff_fail.vvp" crc_diff_top.sv)
    (cd "$OUT" && vvp crc_diff_fail.vvp && mv dump.vcd crc_fail.vcd && \
       vcd2fst crc_fail.vcd crc_fail.fst && rm crc_fail.vcd crc_diff_fail.vvp)
    (cd "$OUT" && vvp "$demo.vvp" && mv dump.vcd crc_pass.vcd && \
       vcd2fst crc_pass.vcd crc_pass.fst && rm crc_pass.vcd crc_diff.vvp)
    echo "   pass: $OUT/crc_pass.fst  fail: $OUT/crc_fail.fst"
  else
    (cd "$OUT" && vvp "$demo.vvp" && vcd2fst dump.vcd "$demo.fst" && rm dump.vcd "$demo.vvp")
    echo "   wave: $OUT/$demo.fst"
  fi
}

if [[ $# -gt 0 ]]; then
  for d in "$@"; do build_one "$d"; done
else
  for d in $ALL; do build_one "$d"; done
fi

echo
echo "== building static sessions (netlist) for each demo =="
cd "$HERE"
for d in $ALL; do
  TOP="xprop_tb"; [[ "$d" == "fsm_stuck" ]] && TOP="fsm_stuck_tb"
  [[ "$d" == "cdc" ]] && TOP="cdc_tb"; [[ "$d" == "crc_diff" ]] && TOP="crc_diff_tb"
  "$PY" -m wave_mcp.cli.build_session \
    --fst "waves/$d.fst" \
    --filelist "rtl/$d/$d.f" \
    --top "$TOP" \
    --out "waves/session_$d" >/dev/null 2>&1 || \
  "$PY" -m wave_mcp.cli.build_session \
    --fst "waves/crc_pass.fst" \
    --filelist "rtl/crc_diff/crc_diff.f" \
    --top "crc_diff_tb" \
    --out "waves/session_crc_diff" >/dev/null 2>&1
  echo "   session: waves/session_$d"
done

echo
echo "done. waveforms in $OUT"
