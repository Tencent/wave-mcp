# 仿真器兼容性说明

wave-mcp **不跑仿真器**，只消费仿真产出的波形（FST）+ RTL 源码。因此它在设计上
**与仿真器无关**——代码里没有任何 xrun/Cadence 特定假设。但"换到 VCS 等陌生仿真器
是否全功能可用"需要分层看，尤其是 trace / 驱动类工具。

## 能力分层

| 能力 | 依赖 | 跨仿真器稳定性 |
| --- | --- | --- |
| 层次 / 信号 / 信号值 | **FST 文件格式**（pylibfst 读取） | ✅ 稳定，与产波形的仿真器无关 |
| 连接 / 驱动 / 扇入扇出 | **pyslang 解析标准 SystemVerilog** | ✅ 结构分析与仿真器无关 |
| **trace_value / active_drivers** | 网表 × FST，靠**路径匹配**对齐 | ⚠️ 唯一需要实测确认的点 |

- 前两类是**格式无关的稳健核心**：只要有 FST（+ 正确 filelist），任何仿真器都能用。
- 第三类叠加了"波形路径 ↔ 网表实例名"的对齐，是跨仿真器最脆弱处（见下）。

## 注意点一：多数仿真器不直接产 FST

只有部分仿真器能直接 dump FST；其余需要一步转换：

| 仿真器 | 直接产 FST | 获取 FST 的方式 |
| --- | --- | --- |
| Verilator | ✅ `--trace-fst` | 零转换（最省事） |
| Icarus (iverilog) | ✅ `-fst` | 零转换 |
| **VCS** | ❌ | VCD → `vcd2fst`；VPD/FSDB 需先 `vpd2vcd`/`fsdb2vcd` 转 VCD |
| Xcelium (xrun) | ❌（只吐 VCD/私有格式） | VCD → `vcd2fst` |
| Questa/ModelSim | ❌ | VCD → `vcd2fst` |

> `prepare_session` 传入 `.vcd` 会自动调 `vcd2fst` 转换；传入 `.fst` 则直读。
> 功能不受影响，只是非 Verilator/Icarus 时多一步 VCD→FST。

## 注意点二：trace / 驱动的命门是"路径对齐"

trace / 驱动能否 work，取决于 **FST 里的实例层次名**能否与 **pyslang 从 RTL 推出的
实例名**对上。引擎（`netlist/trace_engine.py` 的 `_resolve_key`）用「叶子名 + 最长
后缀」匹配来桥接两种根路径：

```
FST 路径：top_tb.U_DECODE.u_decode_unit   （根在仿真顶层）
网表 key： decode.u_decode_unit            （根在 DUT）
→ 叶子 + 最长后缀匹配，不要求 testbench 顶层参与 elaboration
```

这套机制**本身是跨仿真器的**，但以下**命名差异**可能让匹配失败：

- **generate 块 / 实例数组**：`gen[0].u_foo` / `genblk1` / 转义名 `\gen[0].u_foo ` ——
  不同仿真器在波形里的写法不一定一致，与 pyslang 命名一旦不同就对不上。
- **转义标识符、interface/modport、匿名块**同理。

**失败模式是诚实降级**：对不上时返回 `module not resolved` / `available: false`，
遇到多义**主动弃权（返回 null）而非猜测**——最坏情况是部分跨层 trace 用不了，
**绝不会静默给出错误答案**。

## 已验证 / 未验证的环境

- **已验证**：Xcelium(xrun) 的 VCD→FST、Verilator `--trace-fst`。
- **未验证**：真实 VCS dump。纯模块实例的设计大概率无碍；含大量 generate/数组
  实例的设计建议按下方步骤实测。

## 换到新仿真器时的验证步骤

1. 用该仿真器产出波形，`prepare_session(wave_path=..., filelist_path=<同一份.f>)`。
2. 看 `session_info` 返回的两项体检指标：
   - **`netlist_health.trust`**：`full` / `partial`（网表是否干净建成）。
   - **`definition_coverage.coverage_pct`**：波形 scope 与网表的对齐率。
     高 = trace/驱动基本可用；低 = 命名对齐有问题。
3. 挑一个含 generate/数组实例的信号试 `trace_value`，确认跨层节点是否 resolved。

## 一句话总结

格式无关的核心（层次 / 信号 / 值 + 静态连接 / 驱动）在任意仿真器下都可用；
**trace 的跨层路径对齐是唯一需要按上述步骤实测确认的点**，且即便不完美也只会
优雅降级、不会误导。
