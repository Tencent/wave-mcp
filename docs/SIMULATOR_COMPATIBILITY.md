# 仿真器兼容性说明

wave-mcp **不跑仿真器**，只消费仿真产出的波形（FST）+ RTL 源码。因此它在设计上
**与仿真器无关**，代码里没有任何 xrun/Cadence 特定假设。但"换到 VCS 等陌生仿真器
是否全功能可用"需要分层看，尤其是 trace / 驱动类工具。

## 能力分层

| 能力 | 依赖 | 跨仿真器稳定性 |
| --- | --- | --- |
| 层次 / 信号 / 信号值 | **FST 文件格式**（pylibfst 读取） | ✅ 稳定，与产波形的仿真器无关 |
| 连接 / 驱动 / 扇入扇出 | **pyslang 解析标准 SystemVerilog** | ✅ 结构分析与仿真器无关 |
| **trace_value / active_drivers** | 网表 × FST，靠**路径匹配**对齐 | ✅ 稳定；对齐率取决于波形里的实例命名 |

- 前两类是**格式无关的稳健核心**：只要有 FST（+ 正确 filelist），任何仿真器都能用。
- 第三类叠加了"波形路径 ↔ 网表实例名"的对齐。对齐率取决于仿真器给 generate 块、
  实例数组、转义标识符的命名方式，换到新仿真器时按文末三步自查一遍；对不上也只会
  优雅降级，不会给出错误答案（见下）。

## 四种波形接入方式

wave-mcp 的唯一读取格式是 **FST**。其余格式都在进入 wave-mcp 之前先变成 FST，
一共四条路：

| 方式 | 定位 | 是否需要转换 | 需要什么 | 深度文档 |
| --- | --- | --- | --- | --- |
| **① FST 直读** | 首选，零成本 | 不需要 | 仿真器能 dump FST | 本文 |
| **② VCD 自动转换** | 通用兜底 | `prepare_session` 自动调 `vcd2fst` | GTKWave 的 `vcd2fst` | 本文 + [DEPLOY_AIRGAP.md](DEPLOY_AIRGAP.md) |
| **③ FSDB 转换** | 存量商用波形 | `prepare_session` 自动调 `fsdb2fst`（带缓存） | Verdi 的 FsdbReader 运行库（不占 license） | [FSDB_GUIDE.md](FSDB_GUIDE.md) |
| **④ Xcelium 直出** | Cadence 环境首选 | 不需要 | fstdumper VPI 插件（GPL-3.0，自行编译） | [XCELIUM_FST_GUIDE.md](XCELIUM_FST_GUIDE.md) |

一句话口径：**FST 直读；VCD 自动转换；Verilator / Icarus / Xcelium(fstdumper)
可从源头直出 FST；存量 FSDB 走 fsdb2fst 转换；SHM 不做**（Cadence 用户走 ④）。

### 按仿真器查

| 仿真器 | 直接产 FST | 推荐路径 |
| --- | --- | --- |
| Verilator | ✅ `--trace-fst` | ① 零转换（最省事） |
| Icarus (iverilog) | ✅ `-fst` | ① 零转换 |
| Xcelium (xrun) | ✅ 加载 fstdumper VPI 插件 | ④ [直出 FST](XCELIUM_FST_GUIDE.md)；备选 ② VCD → `vcd2fst` |
| VCS | ❌ | 存量 FSDB 走 ③ [fsdb2fst](FSDB_GUIDE.md)；否则 ② VCD → `vcd2fst`（VPD 需先 `vpd2vcd`） |
| Questa / ModelSim | ❌ | ② VCD → `vcd2fst` |

### ① FST 直读

`prepare_session(wave_path="x.fst", ...)` 直接读，没有任何转换步骤，也不需要
装 GTKWave。Verilator 与 Icarus 加一个命令行开关就能产 FST，是最省事的组合。

### ② VCD 自动转换

`prepare_session` 传入非 `.fst`、非 `.fsdb` 的波形一律当 VCD 处理，自动调
`vcd2fst` 转换，转换记录在返回的 `steps` 里（`convert_vcd_to_fst`）。功能不受
影响，只是多一次转换耗时与一份中间文件。转换与 FSDB 共用同一套缓存：产物落在
`.vcd` 旁（目录不可写时回退到 session 目录），同一份 VCD 反复建 session 只转
一次。三种压缩档位（`speed` / `balanced` / `size`）与流式转换用法见 README 的
「VCD → FST 转换」一节。

VCD 的实际上限：超过 10 GB 后 `vcd2fst` 可能长时间转换直至超时失败（18.4 GB
与 55.8 GB 均在 3 小时超时）。这个量级只能走源头直出（④）。

### ③ FSDB 转换（fsdb2fst）

存量 FSDB 走自带的 `fsdb2fst` 单程转换器，**无 VCD 中间文件**，产物与原生 FST
完全一致、查询工具零改动。

在 MCP 配置里给出 `VERDI_HOME`，然后直接把 `.fsdb` 交给 `prepare_session`；首次
转换时转换器会自动编一次（需 `g++`），之后复用：

```python
prepare_session(wave_path="dump.fsdb", filelist_path="rtl.f")
prepare_session(wave_path="dump.fsdb", fsdb_scopes=["u_core"], filelist_path="rtl.f")  # 大设计切片
```

三条选路时就该知道的约束，其余细节（解析顺序、缓存、手工构建、转换语义、排错）
见 [FSDB_GUIDE.md](FSDB_GUIDE.md)：

- FsdbReader 运行库**运行时不占 license**，但 `.so` 受 Synopsys 版权约束，
  不入库、不进 PyPI，只在用户本机使用。
- 产物是 `.fst` + `.fst.hier` **两个文件，必须成对搬运**（缺 `.hier` 打不开）。
- 千万信号量级的门级设计受 FsdbReader 自身限制，需切片，且切片未必总能绕过。

### ④ Xcelium 直出 FST（fstdumper）

Cadence 环境的推荐路径：挂一个 GPL-3.0 的开源 VPI 插件让 xrun 在仿真时直接写
FST，零转换、免商用波形 license。代价是
Xcelium 的 VPI 不支持 generate / task 子 scope 遍历，层次覆盖取决于设计构成。
完整流程、补丁说明与适用范围见 [XCELIUM_FST_GUIDE.md](XCELIUM_FST_GUIDE.md)。

## trace / 驱动的命门是"路径对齐"

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

## 支持状态

| 环境 | 状态 | 说明 |
| --- | --- | --- |
| Verilator `--trace-fst` | ✅ 支持 | 两态仿真器，波形里没有真实 X/Z |
| Icarus Verilog | ✅ 支持 | 四态（X/Z）路径的主要覆盖来源 |
| Xcelium 直出 FST（fstdumper） | ✅ 支持 | 见下方已知限制 |
| Xcelium / Icarus 的 VCD → FST | ✅ 支持 | |
| FSDB → FST（fsdb2fst） | ✅ 支持 | Verdi V-2023.12 及以上 |
| VCS 直接 dump | ⚠️ 未覆盖 | 存量 FSDB 走 fsdb2fst；或转 VCD |

四态能力（X/Z 传播、三态总线、`trace_x` 因果回溯、多驱动冲突）有专项回归覆盖，
见 `tests/fourstate/`。

**已知限制**：

- Xcelium 直出受 VPI 限制，不支持 generate / task 子 scope 遍历，层次覆盖取决于
  设计构成，详见 [XCELIUM_FST_GUIDE.md](XCELIUM_FST_GUIDE.md)。作为补偿，直出的
  FST 携带 RTL 模块定义名，`trace_value` 对 DUT 内部信号的解析优于 VCD 转换路径。
- VCD 超过 10 GB 后 `vcd2fst` 可能长时间转换直至超时，这个量级建议从源头直出 FST。
- FSDB 在千万信号量级的门级设计上受 FsdbReader 限制，详见
  [FSDB_GUIDE.md](FSDB_GUIDE.md) 的「超大文件的处理」。
- 加密 IP、未命名 generate 块的命名差异可能影响 trace 的路径对齐，按下节三步自查。

## 换到新仿真器时的验证步骤

1. 用该仿真器产出波形，`prepare_session(wave_path=..., filelist_path=<同一份.f>)`。
2. 看 `session_info` 返回的两项体检指标：
   - **`netlist_health.trust`**：`full` / `partial`（网表是否干净建成）。
   - **`definition_coverage.coverage_pct`**：波形 scope 与网表的对齐率。
     高 = trace/驱动基本可用；低 = 命名对齐有问题。
3. 挑一个含 generate/数组实例的信号试 `trace_value`，确认跨层节点是否 resolved。

## 一句话总结

格式无关的核心（层次 / 信号 / 值 + 静态连接 / 驱动）在任意仿真器下都可用；
trace 的跨层路径对齐取决于波形里的实例命名，换到新仿真器时按上述三步自查即可，
且即便对齐不完美也只会优雅降级、不会误导。
