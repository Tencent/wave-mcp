#!/bin/bash
# 用 Verilator 编译三个 OpenTitan 模块并生成真实 FST 波形, 供 trace 验证使用.
# 依赖: verilator (>=5.0), 本地 OpenTitan 源码树.
set -e
OT=${OT:-/data/home/wukongxin/opentitan}
PRIM=$OT/hw/ip/prim/rtl
TLUL=$OT/hw/ip/tlul/rtl
TOP=$OT/hw/top_earlgrey/rtl
IBEX=$OT/hw/vendor/lowrisc_ibex/rtl
DVU=$OT/hw/dv/sv/dv_utils
OUT=${OUT:-/tmp/wave_verify}
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT"
COMMON="--cc --trace-fst -Wno-fatal -Wno-lint -Wno-WIDTH -Wno-UNOPTFLAT -Wno-CASEINCOMPLETE +define+SYNTHESIS"

echo "=== [1/3] tlul_adapter_host ==="
verilator $COMMON -I$PRIM -I$TLUL -I$TOP \
  $TOP/top_pkg.sv $PRIM/prim_secded_pkg.sv $PRIM/prim_mubi_pkg.sv $TLUL/tlul_pkg.sv $PRIM/prim_util_pkg.sv \
  --top-module tlul_adapter_host -y $PRIM -y $TLUL $TLUL/tlul_adapter_host.sv \
  --exe "$HERE/tb_adapter_host.cpp" --build -o "$OUT/Vah" -Mdir "$OUT/obj_ah"
FST_OUT="$OUT/adapter_host.fst" "$OUT/Vah"

echo "=== [2/3] tlul_socket_1n ==="
verilator $COMMON -I$PRIM -I$TLUL -I$TOP \
  $TOP/top_pkg.sv $PRIM/prim_secded_pkg.sv $PRIM/prim_mubi_pkg.sv $PRIM/prim_count_pkg.sv $TLUL/tlul_pkg.sv $PRIM/prim_util_pkg.sv \
  --top-module tlul_socket_1n -y $PRIM -y $TLUL $TLUL/tlul_socket_1n.sv \
  --exe "$HERE/tb_socket_1n.cpp" --build -o "$OUT/Vs1n" -Mdir "$OUT/obj_s1n"
FST_OUT="$OUT/socket_1n.fst" "$OUT/Vs1n"

echo "=== [3/3] ibex_core ==="
verilator $COMMON -I$PRIM -I$IBEX -I$DVU \
  $PRIM/prim_util_pkg.sv $PRIM/prim_cipher_pkg.sv $IBEX/ibex_pkg.sv \
  --top-module ibex_core -y $PRIM -y $IBEX $IBEX/ibex_core.sv \
  --exe "$HERE/tb_ibex.cpp" --build -o "$OUT/Vibex" -Mdir "$OUT/obj_ibex"
FST_OUT="$OUT/ibex_core.fst" "$OUT/Vibex"

echo "=== FST 生成完毕 ==="
ls -la "$OUT"/*.fst
