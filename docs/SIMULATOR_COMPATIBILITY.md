# 仿真器兼容性说明

wave-mcp **不跑仿真器**，只消费仿真产出的波形（FST）+ RTL 源码。因此它在设计上
**与仿真器无关**，代码里没有任何 xrun/Cadence 特定假设。但"换到 VCS 等陌生仿真器
是否全功能可用"需要分层看，尤其是 trace / 驱动类工具。

## 能力分层

| 能力 | 依赖 | 跨仿真器稳定性 |
| --- | --- | --- |
| 层次 / 信号 / 信号值 | **FST 文件格式**（pylibfst 读取） | ✅ 稳定，与产波形的仿真器无关 |
| 连接 / 驱动 / 扇入扇出 | **pyslang 解析标准 SystemVerilog** | ✅ 结构分析与仿真器无关 |
| **trace_value / active_drivers** | 网表 × FST，靠**路径匹配**对齐 | ⚠️ 唯一需要实测确认的点 |

- 前两类是**格式无关的稳健核心**：只要有 FST（+ 正确 filelist），任何仿真器都能用。
- 第三类叠加了"波形路径 ↔ 网表实例名"的对齐，是跨仿真器最脆弱处（见下）。

## 注意点一：各仿真器怎么拿到 FST

| 仿真器 | 直接产 FST | 获取 FST 的方式 |
| --- | --- | --- |
| Verilator | ✅ `--trace-fst` | 零转换（最省事） |
| Icarus (iverilog) | ✅ `-fst` | 零转换 |
| **VCS** | ❌ | VCD → `vcd2fst`；VPD/FSDB 需先 `vpd2vcd`/`fsdb2vcd` 转 VCD |
| Xcelium (xrun) | ✅ 加载 fstdumper VPI 插件 | **推荐**：[fstdumper 直出 FST](XCELIUM_FST_GUIDE.md)，零转换；备选：VCD → `vcd2fst` |
| Questa/ModelSim | ❌ | VCD → `vcd2fst` |

> `prepare_session` 传入 `.vcd` 会自动调 `vcd2fst` 转换；传入 `.fst` 则直读。
> 功能不受影响。Xcelium 用户建议用 fstdumper 直出 FST（见上表链接），
> 免去 VCD 中间文件；其余非 Verilator/Icarus 场景多一步 VCD→FST。

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

- **generate 块 / 实例数组**：`gen[0].u_foo` / `genblk1` / 转义名 `\gen[0].u_foo `。
  不同仿真器在波形里的写法不一定一致，与 pyslang 命名一旦不同就对不上。
- **转义标识符、interface/modport、匿名块**同理。

**失败模式是诚实降级**：对不上时返回 `module not resolved` / `available: false`，
遇到多义**主动弃权（返回 null）而非猜测**。最坏情况是部分跨层 trace 用不了，
**绝不会静默给出错误答案**。

## 已验证 / 未验证的环境

- **已验证**：Xcelium(xrun) 的 VCD→FST、Verilator `--trace-fst`。
- **已验证（四态）**：Icarus Verilog 12.0 的 VCD→FST。Verilator 是两态仿真器，
  波形里没有真实 X/Z；四态路径用 Icarus 专项验证（`tests/fourstate/`，两套设计
  共 71 项严格断言），覆盖：
  - 未复位寄存器 X、X 经组合逻辑传播、驱动后 X 清除；
  - 三态总线悬空 Z、单驱动取值、双驱动冲突出 X、撤驱回 Z；三态 assign 的
    guard 四态求值（`guard_active` 在使能已知时精确 True/False，使能为 X 时
    如实报 None）；
  - `trace_x`：X 因果树逐级回溯（含 file/line/snippet）；多驱动冲突时展开
    全部活跃驱动为并列分支（`conflicting_drivers` + `driver_conflict` 元信息）；
    X 根因叶节点带终止原因说明（未驱动的输入端口）；
  - always 块 if/else guard 在复位为 X 时判定为 None、复位已知时可判定；
    case/casez（通配）驱动的 control 含选择子、X 选择子走 default；
  - 位选/段选驱动与部分位 X 总线；X 门控锁存器；for-generate 逐位三态阵列
    （逐位 Z 值正确）；wor 线或消解（1 胜出）；
  - 部分 dump（`$dumpvars` 深度限制，RTL 有的信号波形没有）：值查询返回空、
    静态 drivers 仍可用，均不崩溃；
  - 负路径：不存在的信号、越界时间、损坏的 FST 文件均干净报错不崩溃。
  该轮测试修复了两个真实缺陷：pyslang 11 下 ternary 条件信号从 control/fanin
  丢失（`pred` 恒 None，需读 `conditions[].expr`）；连续赋值从不提取
  guard/control 导致 `active_drivers` 对 assign 类驱动无筛选能力。修复后在
  OpenTitan uart/aes 重建网表回归 10 万+ 项检查全过。
- **未验证**：真实 VCS dump；真实 xrun + 业务代码（UVM 层次、加密 IP、
  interface/modport、xrun 对未命名 generate 块的命名差异），内网测试计划中。

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
