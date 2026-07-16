# Verilator + FST 动态验证 (开源大型模块)

在无法接入商用仿真器 (xrun) 的环境下，用开源 **Verilator** 对真实
OpenTitan / lowRISC Ibex 模块生成 **FST** 波形，端到端验证 wave_mcp 的
结构提取 (netlist) 与跨模块/字段级 trace 在真实波形上的功能正确性。

## 依赖
- `verilator` >= 5.0 (需 `--trace-fst`)
- 本地 OpenTitan 源码树 (默认 `/data/home/wukongxin/opentitan`，可用 `OT=` 覆盖)
- 已安装 wave_mcp (pyslang + pylibfst)

## 用法
```bash
# 1) 生成 FST (默认输出 /tmp/wave_verify, 可用 OUT= 覆盖)
bash tests/verilator_fst/build_fst.sh

# 2) 跑功能正确性验证 (带 PASS/FAIL 断言)
FST_DIR=/tmp/wave_verify python3 tests/verilator_fst/run_verify.py
```

## 验证目标模块
| 模块 | 类型 | 覆盖能力 |
|---|---|---|
| `tlul_adapter_host` | TL-UL host 适配器 | struct 端口、跨实例 driver |
| `tlul_socket_1n`    | 1-to-N 总线交换 | 字段级 struct driver、三层跨模块穿透 (`socket → fifo → prim_fifo`) |
| `ibex_core`         | RISC-V 核心 (17 模块/1993 信号) | 大规模层次、四层穿透 `core → if_stage → prefetch_buffer → fetch_fifo` |

## 验证维度 (run_verify.py 的断言)
1. **netlist:extract / drivers / field_level** — pyslang 提取成功、driver 非空、struct 字段级 driver 出现
2. **fst:signals** — 真实 FST 信号可读
3. **active_drivers** — 能定位 driver 到 RTL 源 (file/line/snippet) 或正确判定为边界
4. **trace:cross_module / has_values** — trace_value 跨模块穿透 (`crosses_into`) 且节点带真实波形值

## 已知边界
- Verilator FST 默认不 dump 全部内部组合 wire，故深层组合节点可能取不到值
  (trace 结构仍正确，止于可见边界)。这是波形 dump 粒度，非引擎缺陷。
- testbench 为最小驱动 (NOP/简单 handshake)，目的是产生波形而非功能覆盖。

## 文件
- `build_fst.sh` — 一键 Verilator 编译 + 仿真生成三个 FST
- `tb_*.cpp`      — 各模块的 C++ testbench (FST 路径可用 `FST_OUT` 环境变量覆盖)
- `run_verify.py` — 统一的 netlist+FST+trace 断言 runner
- `verify_netlist.py` / `verify_trace_s1n.py` / `verify_trace_ibex.py` — 单项详查脚本
