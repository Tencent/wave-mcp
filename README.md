# wave-mcp：开源、免 License 的 RTL 波形调试 MCP Server

<img src="docs/images/penglai-logo.png" alt="蓬莱实验室" width="200"/>

[![PyPI version](https://img.shields.io/pypi/v/wave-mcp)](https://pypi.org/project/wave-mcp/)
[![Python versions](https://img.shields.io/pypi/pyversions/wave-mcp)](https://pypi.org/project/wave-mcp/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)

[English](README.en.md) | 简体中文

**wave-mcp 是腾讯蓬莱实验室验证团队开源的一款 RTL 波形调试 MCP Server**，为 LLM 提供波形调试工具集：
读 **FST 波形 + RTL 网表**，提供层次探索、信号查询、驱动分析、值/X 态追踪、波形对比与浏览器波形查看器等 **37 个 MCP 工具**。
**Apache-2.0 开源，无需任何商用 License，支持任意并发。**

> **FST 直读，VCD / FSDB 自动转 FST**：Verilator `--trace-fst`、Icarus 直接产 FST 就能读；
> 手上只有 VCD 或 FSDB 也没关系，`prepare_session` 自动转换后再建 session（FSDB 转换不占 Verdi license）。
> 它**不跑仿真器**，你用自己的流程跑出波形，把结果交给它即可。

---

## 为什么是 wave-mcp

芯片验证占据开发周期 50% 以上的时间，波形调试是其中最高频的动作。而 LLM 时代，
工程师希望让 AI Agent 直接读波形、查信号、追 X 态根因，但市面上的商用调试
MCP 需要昂贵的 License，且并发受限。

wave-mcp 用**纯开源技术栈**（pylibfst + pyslang）提供完整波形调试能力：
**免 License、数据准确、真实芯片项目背书**。

## 生产级验证

在**真实生产级芯片项目**（几十个模块）上完整验证，并把 OpenTitan、香山纳入测试集：

![核心验证数据](docs/images/validation-overview.png)

| 维度 | 结果 |
| --- | --- |
| 测试规模 | **一百多个测试 case**（生产级项目 + OpenTitan 27 个 IP + 香山 38 个 IP） |
| 数据准确性 | **225 万信号级验证，值查询正确性 100%** |
| 工具调用 | 310 万多次调用全部通过 |
| 驱动分析 | 驱动 / 扇入 / 连通 / 追溯在生产级项目上全量验证 |
| 超大模块 | **百万级 scope 稳定完成分析** |
| 工具覆盖 | 37 个工具全部验证，含 viewer / diff 的单元与浏览器端到端覆盖 |

![工具调用分布](docs/images/tool-calls-distribution.png)

## 特性

- **波形查询**：设计层次、实例、信号（位宽/方向/类型，含总线聚合）、信号值（点查询 / 区间，随机访问）。
- **静态分析（pyslang 网表）**：连接、驱动、扇入/扇出、声明位置（文件:行号）。
- **无波形静态分析**：`open_static_session` 只凭 RTL 源码建 session，**仿真前即可分析设计结构**。
- **值追踪**：`trace_value` 沿驱动链反向遍历、可跨模块下钻，每个节点带真实 FST 值；`trace_x` 追 X 根因。
- **网表自愈**：从 pyslang 诊断自动补 `+incdir+` / 包源并重编；失败时优雅降级，其余工具不受影响。
- **一致性校验**：源码或波形变了但网表没更新会报警，绝不静默给错结果。
- **波形对比**：`diff_waveforms` 对 pass/fail 两份波形定位首个分歧时刻，按分歧时间排序信号，时钟对齐采样过滤毛刺。
- **波形查看器**：`open_wave_view` 让 agent 分析完直接弹浏览器波形，嫌疑信号 + 游标钉在出错时刻 + 分析说明弹窗；双波形对比两栏同步注入；`get_view_state` 确认页面连通与状态送达。
- **多会话与可复现**：同一设计可开多个独立 session 共享一份已加载数据；每条查询回复带 `_query`（有效参数）和 `_fp`（数据集身份/版本 + 问题摘要），两次回答是否来自同一输入同一问题一眼可辨。
- **部署友好**：stdio（一人一进程，零运维）/ HTTP（本机免配置，跨机器一个 `WAVE_MCP_TOKEN`）/ 离线自包含包（隔离网）。

FSDB 转换器（`third_party/fsdb2fst`）中的部分实现参考了 TraceWeave（MIT），`diff_waveforms` 的功能优先级亦受其影响，详见 [`docs/THIRD_PARTY.md`](docs/THIRD_PARTY.md)。

## 系统要求

| 依赖 | 版本要求 | 说明 |
| --- | --- | --- |
| Python | **3.10 – 3.13** | 已测 3.10–3.13 全部通过；mcp SDK 要求 ≥ 3.10 |
| glibc | **≥ 2.28** | pyslang 预编译 wheel 的要求（对应 Ubuntu 18.10+ / Debian 10+ / CentOS 8+） |
| mcp | **2.x**（`>=2.0.0,<3`） | MCP SDK v2，`pip install` 自动安装 |
| pylibfst | **≥ 0.2.1** | FST 波形读取（fstapi，随机访问） |
| pyslang | **≥ 11.0.0** | RTL 网表构建（完整 elaboration） |
| vcd2fst（可选） | GTKWave | 仅 VCD→FST 转换需要（`apt install gtkwave` / `brew install gtkwave`） |
| Verilator（示例） | ≥ 5 | 仅 `verilator_quickstart` 示例需要 |

> **Linux x86_64 开箱即用**（以上 Python 依赖均有预编译 wheel）；
> 其他平台仅 `pylibfst` 需源码编译（cmake+gcc+zlib），波形查看器暂不支持，详见 Q6。

标准环境直接 `pip install wave-mcp`。环境受限时按下表对号入座：

| 你的环境 | 方案 | 参考 |
| --- | --- | --- |
| 无外网（隔离网 / 加密网） | 有网机器 `deploy/docker_build_all.sh` 一键打离线 bundle，拷入后 `install.sh` 安装 | [DEPLOY_AIRGAP.md](docs/DEPLOY_AIRGAP.md) 第 1.0 节 |
| Python < 3.10 或无 Python | 无需升级目标机：bundle 自带独立 Python 3.11，与系统 Python 无关 | [DEPLOY_AIRGAP.md](docs/DEPLOY_AIRGAP.md) 第 7 节 |
| glibc < 2.28（CentOS 7 / RHEL 7） | `pip install` 不可用（官方 pyslang wheel 要求 glibc ≥ 2.28）；用 glibc 2.17 档 bundle，全链路兼容老机器；或在容器（如 `python:3.11-slim`）中运行 | [DEPLOY_AIRGAP.md](docs/DEPLOY_AIRGAP.md) 第 1c 节 |

> Docker 流水线默认产出 glibc 2.28 与 2.17 两档 bundle，覆盖以上全部受限场景；打包机只需 docker，目标机不需要。

---

## 快速开始

### 1. 安装

```bash
pip install wave-mcp
```

### 2. 跑一个示例

```bash
# 示例 A：Verilator 快启（counter 设计，产真实 FST，无需商用仿真器）
python examples/verilator_quickstart/run_demo.py      # 需 verilator>=5

# 示例 B：静态分析（UART 设计，无需波形、无需仿真器，展示仿真前分析）
python examples/static_analysis/run_demo.py

# 示例 C：极小内置样例（手写 VCD → vcd2fst → FST，零依赖）
python examples/sample/make_sample.py
```

### 3. 打开你的波形

```bash
# 一条命令：波形(.fst/.vcd) + filelist → session（自动转 FST + 建网表）
wave-session --fst sim/dump.fst --top top_tb --filelist rtl.f      # --out 可选

# 启动 MCP Server（stdio，推荐：一人一进程）
python -m wave_mcp.server --session sessions/my_module
```

或者在你的 Code Agent 里直接用 MCP 工具 `prepare_session`，见下文集成示例。

## CLI 模式

不挂 Code Agent 时，也能在终端直接调用全部 37 个工具（与 MCP 同名同参数）：

```bash
wave-mcp query --list                            # 列出全部 37 个工具

wave-mcp query signal_values --session sessions/my_module \
    --paths top.u_tx.tx_serial                  # 查询信号值变化

wave-mcp query signal_drivers --session sessions/my_module \
    --json-args '{"paths": "top.u_tx.tx_serial"}'       # JSON 传参
```

- 参数按工具签名自动生成，`wave-mcp query <工具名> --help` 查看
- 默认输出人读文本，加 `--json` 输出完整结构化结果
- 适合 CI 脚本、开发调试、快速验证；每次新增工具自动获得 CLI 接口

## Code Agent 集成

`prepare_session` 是 MCP 统一入口，Code Agent 想分析波形时**第一步调它**，
传入仿真产出的波形，一次完成"（转换 →）建网表 → 建 session → 打开"：

```jsonc
prepare_session({
  "wave_path":    "sim/dump.fst",          // .fst 直读 / .vcd 自动转
  "top":          "top_tb",
  "filelist_path":"rtl.f",                 // 与仿真同一份 filelist
  "pack":         "fastlz"                 // 转换压缩：fastlz/lz4/zlib，可省
})                                         // out_dir 可省：session 落到会话根下按输入身份命名的目录
// 返回 ready 后即可调 signal_values / find_instances / signal_drivers ...
```

**接入配置**（stdio，各家 Agent 的 MCP 配置）：

**Codex** 用 TOML，**其余 Agent 用 `mcpServers` JSON**：

```toml
# ~/.codex/config.toml（项目级用 .codex/config.toml）
[mcp_servers.wave-mcp]
command = "python"
args = ["-m", "wave_mcp.server", "--session", "/abs/path/to/sessions/my_module"]
```

- **Codex**：写入 `~/.codex/config.toml`（项目级用 `.codex/config.toml`），段名是 `mcp_servers`（下划线）；也可以在终端用 `codex mcp add wave-mcp -- python -m wave_mcp.server ...` 一行加好

```json
{
  "mcpServers": {
    "wave-mcp": {
      "command": "python",
      "args": ["-m", "wave_mcp.server", "--session", "/abs/path/to/sessions/my_module"]
    }
  }
}
```

- **Claude Code**：写入 `.mcp.json`（`claude mcp add` 或手工配置）
- **Cursor**：写入 `.cursor/mcp.json`（项目级）或 `~/.cursor/mcp.json`（全局）
- **Gemini CLI**：写入 `~/.gemini/settings.json` 的 `mcpServers` 字段（项目级用 `.gemini/settings.json`）
- 其余 Agent（Cline / Windsurf / Roo Code 等）：按各自的 `mcpServers` 配置填入上述 JSON 即可

### 无波形静态分析（仿真前即可用）

波形里有值，没有连接关系：信号这一拍是 0，波形本身回答不了它被谁驱动、驱动语句又被什么
条件门控。wave-mcp 在建 session 时就把这层关系从 RTL 源码里提出来：pyslang 完整精化
（参数、generate、interface 全展开）后持久化成一份**静态设计数据库**，驱动、扇入扇出、
连通、声明查询都跑在这份库上，不依赖仿真器，也不依赖任何商用工具。

`open_static_session` 只凭 RTL 源码建网表并打开 session，**不需要任何波形、不跑仿真**。
适合仿真前理解代码：查接口、查驱动/扇入扇出、浏览层次、做 code review。

```jsonc
open_static_session({
  "top":          "uart",
  "filelist_path":"rtl.f"
})
// 连接/驱动/层次/文件/声明类工具全部可用；值/追踪类工具返回明确的 "needs waveform" 提示
```

之后仿真产出波形时，用同一份 RTL 源码调 `prepare_session` 升级为完整 session，已建好的网表自动复用，不需要记住任何目录。

每条驱动记录都带完整语境：驱动类型、源码位置、语句片段、右值来源、以及压在这条语句上的
**全部门控条件**（可 4 值求值的表达式树）。以示例 B 的 UART 为例：

```yaml
# wave-mcp query signal_drivers --session ... --path uart_top.u_tx.tx_serial
drivers:
  - kind: nonblocking
    file: examples/static_analysis/uart_top.sv
    line: 91
    snippet: tx_serial <= shift_reg[0];
    rhs: uart_top.u_tx.shift_reg
    control: uart_top.u_tx.state, uart_top.u_tx.tick, ...
    guard:                          # 这条语句头上压着的全部条件
      - {cond: !rst_n, expect: 0}
      - {cond: tick, expect: 1}
      - {cond: state == DATA, expect: 1}
```

有了波形后，`active_drivers` 用 FST 值对 guard 做 4 值求值，直接告诉你**某一拍是哪条驱动
语句在起作用**；`trace_value` / `trace_x` 沿这张图反向遍历、跨模块下钻，每个节点带真实
波形值。静态连接关系与动态波形值在同一套工具里打通，这是纯静态设计数据库给不了的。

**驱动分析与追踪是按生产级健壮性打磨的**，不是 demo 功能：

- **真实项目全量验证**：驱动/扇入/连通/追溯在生产级芯片项目上全量验证，并以 OpenTitan
  27 个 IP + 香山 38 个 IP 按子模块层次逐一穷尽测试，覆盖功能正确性交叉校验而非仅
  "返回非空"。
- **精化失败不掀桌**：单个 top 精化失败（如 UVM 环境拉不到 uvm_pkg）只影响该 top，
  健康 DUT 的网表照常提取；缺 `+incdir+`/包源时从 pyslang 诊断自愈重编；仍失败则明确
  降级，值查询等其余工具不受影响，绝不静默给错结果。
- **部分网表也可用**：诊断有 error 但仍提取出模块时，以 `partial` 标志照常服务，
  `session_info` 的 netlist_health 如实上报覆盖率，让你知道答案的可信边界。

### VCD → FST 转换（vcd2fst 配置，可选）

如果你的仿真器只吐 VCD（如 Questa），建议先转 FST：**体积约 VCD 的 1/50，随机访问快**。
Xcelium (xrun) 用户推荐跳过 VCD，用 fstdumper 插件直接 dump FST，见
[Xcelium 直出 FST 指南](docs/XCELIUM_FST_GUIDE.md)（含一套
Xcelium 修复补丁，见指南与仓库 `third_party/fstdumper/`）。
转换依赖 GTKWave 附带的 `vcd2fst` 工具：

```bash
# Debian/Ubuntu
sudo apt install gtkwave
# macOS
brew install gtkwave
```

> **已有 FST 则完全不需要 vcd2fst**（如 Verilator `--trace-fst` 直接 dump FST）。
> 隔离网环境可用离线 bundle（自带 vcd2fst），见 [`docs/DEPLOY_AIRGAP.md`](docs/DEPLOY_AIRGAP.md)。

三个转换入口：

```bash
# ① 独立转换（后处理）：pack=fastlz(最快，默认) / lz4 / zlib(最小)
wave-vcd2fst --vcd sim/dump.vcd --fst sim/dump.fst --pack fastlz

# ② 流式转换：把转换时间藏进仿真时间，仿真结束 FST 几乎同时就绪
wave-vcd2fst --stream --vcd sim/dump.vcd --fst sim/dump.fst
#   建 FIFO + 后台起 vcd2fst，然后 TB 里 $dumpfile("sim/dump.vcd") 指向该 FIFO 正常跑仿真

# ③ 建 session 一步到位（自动转 + 打包）
wave-session --vcd sim/dump.vcd --top top_tb --filelist rtl.f
```

> 通过 MCP 工具使用时无需手动转换：`prepare_session` 传入 `.vcd` 会自动走 ① 的转换路径。

**转换产物放在哪**：转出的 FST 和原波形放在同一目录，文件名相同只换扩展名（`sim/dump.vcd` → `sim/dump.fst`，FSDB 另有 `dump.fst.hier`）。旁边已经有 FST（不管是你手动转的还是之前自动转的），只要不比原波形旧就直接用，不再重转；比原波形旧或打不开就覆盖。三种情况会改放到 `~/.wave-mcp/cache/fst/`（受 `WAVE_MCP_CACHE_ROOT` 影响）并在返回里说明原因和位置：原波形目录不可写、带 `scopes`/`signals_file` 的部分转换（不占用完整波形的文件名）、目录仍不可写时复用缓存里那份。

---

## 工具（37 个，12 大类）

| 类别 | 工具 | 说明 |
| --- | --- | --- |
| 波形准备 | `prepare_session` / `open_static_session` / `convert_vcd_to_fst` / `convert_fsdb_to_fst` | 波形入口 → session 一条龙（`.fst` / `.fsdb` / `.vcd` 自动识别，转出的 FST 放原波形旁边并复用）；静态分析无需波形；不跑仿真器 |
| 会话管理 | `open_session` / `close_session` / `session_info` | `session_info` 含 netlist_health + definition_coverage |
| 查询默认值 | `query_defaults_set` / `query_defaults_get` / `query_defaults_clear` | 按会话设定默认信号与时间窗；显式参数始终优先，用到默认值的回复在 `_query.from_defaults` 里列出继承项；带 `defaults_revision` 可钉住一版，被改动则报 `defaults_conflict` |
| 层次探索 | `find_instances` / `list_modules` / `scope_info` | 模块定义名三层解析：网表 → 命名推断 → 手工 scope_map |
| 信号查询 | `list_signals` / `signal_info` | 位宽/方向/类型来自 FST（含总线聚合）；声明位置来自网表 |
| 值查询 | `signal_values`（整段 / 窗口 / 单点，可批量多信号） / `signal_activity` / `find_time_windows` / `sample_at_clock` | FST 强项，随机访问；`sample_at_clock` 给按时钟边沿对齐的逐拍表 |
| 时序与事务 | `fold_transactions` / `fsm_transitions` | 事务定义由调用方给（不内置任何协议库）；FSM 只报实际发生的转移与分支，不报覆盖率 |
| 驱动分析 | `signal_connectivity` / `signal_drivers` / `signal_loads` / `signal_fanin` / `signal_downstream` / `active_drivers` / `driver_contributors` | pyslang 网表（静态精确）+ 分支条件 4 值求值选活跃驱动；`signal_downstream` 是 `signal_fanin` 的正向镜像，给 `time` 时联立波形给出下游首次变化时刻（相关性，非因果） |
| 值/X 态追踪 | `trace_value` / `trace_x` | 网表 × FST 值反向遍历，跨模块下钻 |
| 波形对比 | `diff_waveforms` | N 份波形首分歧定位：首分歧时刻 + 分歧信号排序 + 时钟对齐采样滤毛刺；多于两份时给分歧簇（按值分组的 run 下标）；分歧信号直接接 `signal_fanin`/`active_drivers` 做因果回溯 |
| 波形查看器 | `open_wave_view` / `update_wave_view` / `get_view_state` / `list_wave_views` / `close_wave_view` | agent 分析完自动弹浏览器波形：嫌疑信号 + 游标钉出错时刻 + 分析说明弹窗；双波形对比视图，agent 设置的缩放/游标/marker 同步注入两栏；`get_view_state` 报页面连通状态与已应用版本（送达确认）；`list_wave_views` / `close_wave_view` 管理视图生命周期，批量场景可收尾释放 |
| 文件 | `files`（列全部 / 按名查找 / 读某文件的模块） | filelist + pyslang 网表 |

> 驱动分析与追踪类需要 pyslang 网表建成（`prepare_session` 时给对 filelist/incdirs/defines）。
> 查看器类需要单独的资产（Surfer WASM + surver，EUPL-1.2）。wave-mcp 不分发这些资产，需按 [SELF_BUILD.md](docs/SELF_BUILD.md) 自行构建（一次构建长期使用，团队可共享）；未配置时相关工具优雅降级返回提示，分析工具不受影响。

### 波形查看器（wave-view）

```bash
# 打开单个波形（几十 GB 的 FST 也是秒开：surver 服务端流式，浏览器按需取数据）
wave-view dump.fst --signals top.u_dma.req_valid --cursor 1523400ps

# 双波形对比视图（上下两个 pane，agent 设置的缩放/游标同步注入两栏）
wave-view pass.fst fail.fst --labels pass fail

# VCD / FSDB 直接传，自动转 FST 后打开
wave-view sim.vcd
```

- 命令行打印 URL；桌面环境自动开浏览器，SSH/code agent 场景 IDE 终端自动转发端口点开即看。
- 波形格式：`.fst` 直接打开，`.vcd` / `.fsdb` 自动转成 FST，转出的 FST 放在原波形旁边并与 `prepare_session` 共用，同一个波形先分析后看图还是先看图后分析都只转一次，手动转好放在旁边的也直接用。其他格式在入口直接报错并列出支持的扩展名。
- agent 典型闭环：case 挂了 → `diff_waveforms([pass, fail])` 定位首分歧 → `signal_fanin` 回溯根因 → `open_wave_view` 双波形 + 分歧 marker + 分析说明弹窗一次呈现。
- 分析说明是可收起的 log 弹窗，说明里的时刻引用（如 `[85000ps](#t=85000ps)`）点击即跳游标，游标/视口/marker 更新为无闪刷新。
- 完整指南（MCP 工具参数、调试工作流、架构原理、部署与排障）见 [`docs/WAVE_VIEWER.md`](docs/WAVE_VIEWER.md)。
- 想先看效果，见 [`docs/VIEWER_SCREENSHOTS.md`](docs/VIEWER_SCREENSHOTS.md)：四个真实调试场景的界面截图，含一键复现步骤。

---

## 示例库

| 示例 | 路径 | 依赖 | 展示内容 |
| --- | --- | --- | --- |
| Verilator 快启 | `examples/verilator_quickstart/` | Verilator 5+ | counter 设计 → 真实 FST → prepare_session 全流程 |
| 静态分析 | `examples/static_analysis/` | 无（纯 Python） | UART 设计无波形分析：层次/驱动/扇入/声明 |
| 极小样例 | `examples/sample/make_sample.py` | 可选 vcd2fst | 手写 VCD → FST → session 冒烟 |

---

## 部署模式

- **stdio（推荐）**：每人本地起一个 Server 子进程，只加载自己模块的 FST+网表，零运维，不需要任何配置。
- **HTTP（本机）**：一个常驻服务，多个客户端各开自己的 session，`session_id` 显式区分。只绑回环地址时同样不需要配置：
  `python -m wave_mcp.server --transport http --port 8000`
- **HTTP（从别的机器连过来）**：服务要绑非回环地址（如 `--host 0.0.0.0`）时必须设 `WAVE_MCP_TOKEN`，不设则拒绝启动，报错会直接告诉你要设哪个变量。设了以后每个 HTTP 请求都要带同一串 token，不带或带错返回 401，任何工具都不会执行。token 是你自己生成的一串随机字符，两边填同一个即可，不是账号：

  ```bash
  # 服务端
  WAVE_MCP_TOKEN=$(openssl rand -hex 32) python -m wave_mcp.server --transport http --host 0.0.0.0 --port 8000
  ```

  ```json
  // 客户端 mcp.json
  {"mcpServers": {"wave-mcp": {
    "url": "http://server:8000/mcp",
    "headers": {"Authorization": "Bearer <同一串 token>"}
  }}}
  ```

  服务进程以启动它的 OS 账号运行，能读写哪些文件由操作系统决定；wave-mcp 不做多租户，一台机器服务多人就每人各起一个进程。TLS 由可信反向代理终止。
- **隔离网 / 离线自包含包**：有 docker 的机器一键打包（产出 glibc 2.28 / 2.17 两档），拷贝到隔离网离线安装，自带独立 Python + 全部 wheel + 可选 vcd2fst 与 viewer 资产：

  ```bash
  # ① 有网打包机（只需 docker）：一条命令产出两档 bundle
  deploy/docker_build_all.sh --viewer <资产目录> --python <独立Python包或URL>
  # 产物：dist/wave-mcp-bundle-glibc2.28.tar.gz（主流机器）
  #       dist/wave-mcp-bundle-glibc2.17.tar.gz（CentOS 7 老机器）

  # ② 隔离网共享盘：解压后离线安装（无需联网/编译/docker）
  tar -xzf wave-mcp-bundle-glibc2.28.tar.gz -C /shared/ && cd /shared/wave-mcp-bundle-glibc2.28
  ./install.sh --prefix /shared/wave-mcp      # 产出 bin/wave-mcp 启动器
  ```

  不便使用 docker 时可用分步脚本 `deploy/build_offline_bundle.sh` 手工打包。
  详见 [`docs/DEPLOY_AIRGAP.md`](docs/DEPLOY_AIRGAP.md)（含 vcd2fst 兼容性方案与排错）。

## 环境变量

**多数人一个都不用配**：装完直接用，读 FST/VCD、静态分析、波形查看器全都开箱可用。
只有两种情况需要配：要读 `.fsdb`（配 `VERDI_HOME`），或者 HTTP 服务要让别的机器连过来
（配 `WAVE_MCP_TOKEN`）。其余变量都是特殊环境下的微调开关，按需再查。

要配的话，写在 MCP 客户端配置的 `env` 里，**不要用 shell 的 `export`**：Agent 以子进程
方式拉起 Server，继承不到你交互式 shell 里的变量，写进 `env` 才稳定生效。

```json
{
  "mcpServers": {
    "wave-mcp": {
      "command": "wave-mcp",
      "env": {
        "VERDI_HOME": "/tools/synopsys/verdi/T-2022.06-SP1"
      }
    }
  }
}
```

| 变量 | 要配吗 | 作用 | 默认值 |
| --- | --- | --- | --- |
| `VERDI_HOME` | 读 `.fsdb` 时必配 | Verdi **安装根目录**（不是可执行文件所在的 `bin/`）。程序在其下找 `share/FsdbReader/linux64`，用法与排错见 [FSDB 波形接入指南](docs/FSDB_GUIDE.md) | 空。不配则 FSDB 输入不可用，其余功能正常 |
| `WAVE_MCP_SESSION_ROOT` | HOME 有配额时建议配 | 不传 `out_dir` 时 session 的落点根目录，目录名取自输入的身份摘要，同一份 RTL 不论从哪调用都落到同一处、复用同一份网表。传了 `out_dir` 就按传入值原样使用，不改写。芯片级设计的网表可达数百 MB，HOME 在 NFS 且有配额时指向本地大盘 | `~/.wave-mcp/sessions` |
| `WAVE_MCP_CACHE_ROOT` | HOME 有配额时建议配 | 派生缓存根目录：落不到原波形旁的 `.fst`（目录不可写或部分转换）、网表按模块索引（大小与网表相当）、`fsdb2fst` 构建产物、viewer 资产。完整转换的 FST 放在原波形旁边，不在这里；缓存删掉只是下次慢 | `~/.wave-mcp/cache` |
| `WAVE_MCP_LINT_CODES` | 不用配 | 逗号分隔的 slang 诊断码，追加到"按 lint 计、不降 trust"的内置列表（`MissingTimeScale`、`NewlineEOF` 等）。只对 slang 报为 error 的码生效，`WidthTruncate` 这类 warning 本来就不计入 errors，写进来没有作用 | 空 |
| `WAVE_MCP_LAZY_NETLIST` | 不用配 | 网表按模块懒加载：`1` 强制开启，`0` 关闭 | 空。网表 ≥ 8 MB 时自动开启 |
| `WAVE_MCP_VIEWER_PORT_BASE` | 多人共用主机建议配 | 把视图端口限制在 `[base, base+64)`，便于固定一条 `ssh -L` 转发规则；每人分一段互不重叠 | 空。每次随机取高位端口 |
| `NOVAS_HOME` | 不用配 | 同 `VERDI_HOME`，仅为老版本 Verdi 保留；两个都设时优先用 `VERDI_HOME` | 空 |
| `FSDB2FST_FREADER` | 不用配 | 直接指向拷来的 `share/FsdbReader` 目录，用于只拷了运行库、没装完整 Verdi 的机器 | 空。自动读 `VERDI_HOME` / `NOVAS_HOME` |
| `FSDB2FST_BIN` | 不用配 | 指定已编译好的 `fsdb2fst` | 空。自动探测，首次转换时按需就地编译 |
| `WAVE_MCP_FSDB2FST_AUTOBUILD` | 不用配 | 设 `0` 关闭首次自动编译 | `1`（开启） |
| `VCD2FST_BIN` | 不用配 | 指定 GTKWave `vcd2fst` 可执行文件 | `vcd2fst`（从 `PATH` 查找） |
| `WAVE_MCP_VIEWER_ASSETS` | 不用配 | 波形查看器资产目录（须含 `surver` 与 `wasm/index.html`），离线包安装时自动设好 | 空。依次找 pip 资产包、`~/.wave-mcp/cache/viewer/` |
| `WAVE_MCP_MAX_VIEWS` | 不用配 | 并发视图上限，超出则关掉最旧的；设 `0` 取消上限 | `8` |
| `WAVE_MCP_WORKERS` | 不用配 | 同时执行的工具调用上限；超出的排队等待 | `4` |
| `WAVE_MCP_QUEUE_CAPACITY` | 不用配 | 排队上限，满了直接返回 `server_busy` 而不是无限堆积 | `32` |
| `WAVE_MCP_PER_OWNER_RUNNING` | 不用配 | 单个用户同时在跑的调用上限，防止一个客户端占满全部槽位 | `2` |
| `WAVE_MCP_PER_OWNER_SESSIONS` | 不用配 | 单个用户可同时打开的 session 数，超出返回 `resource_limit` | `16` |
| `WAVE_MCP_SESSION_TTL` | 不用配 | 空闲多少秒后自动关闭无在途请求的 session；`0` 关闭回收 | `1800` |
| `WAVE_MCP_QUEUE_TIMEOUT` | 不用配 | 排队最长等多少秒，超时返回 `queue_timeout` | `30` |
| `WAVE_MCP_SHUTDOWN_GRACE` | 不用配 | 收到 SIGTERM/SIGINT 后给在跑调用的宽限秒数，之后才强制退出 | `30` |
| `WAVE_MCP_AUDIT_LOG` | 不用配 | 审计日志：每次工具调用一行 JSON（时间、request id、工具、状态、错误类型、耗时、数据集身份/版本，不含参数与路径）。填文件路径（0600 追加写）或 `stderr` | 空，不记录 |
| `WAVE_MCP_TOKEN` | HTTP 绑非回环地址时必配 | HTTP 传输的共享密钥（至少 16 字符，`openssl rand -hex 32` 生成）。设了以后每个请求须带 `Authorization: Bearer <同一值>`，否则 401；不设则只允许 `--host 127.0.0.1`。stdio 用不到 | 空 |

**Session 目录**：`prepare_session` / `open_static_session` 的 `out_dir` 通常不用传，session 会落到
`WAVE_MCP_SESSION_ROOT` 下以输入身份命名的目录里，同一份 RTL 的静态分析和波形分析自动共用一份网表，
不依赖 Agent 记住任何约定。只有 session 目录必须放在特定位置（例如和 testbench 一起入库）时才传 `out_dir`，
传了就原样使用。

**filelist 里的环境变量**：MCP server 由 IDE 拉起，不经过项目的 `cshrc`/`bashrc`。filelist 用到的
`$PROJ_ROOT` 之类变量要写进上面 `env` 块，否则对应条目会被跳过。跳过的条目会列在
`netlist_health.dropped_entries` / `undefined_env_vars` 和 `warnings` 里，`trust` 降为 `partial`，
这时"找不到驱动"不能当作设计结论。

**磁盘回收**：`wave-mcp gc` 列出 session 与缓存占用；`wave-mcp gc --older-than 30 --apply` 删除 30 天未打开的，
`wave-mcp gc --max-size 20G --apply` 按最久未用删到总量不超过 20G。不加 `--apply` 只预览。只清理上面两个根目录，
显式传了 `out_dir` 的 session 不会被动。

## FAQ

**Q1：支持 FSDB 和 SHM 吗？**
支持 FSDB，不支持 SHM。

FSDB 走转换通道：`prepare_session` 直接吃 `.fsdb`，自动调自带的 `fsdb2fst` 转成 FST，
不经过 VCD 中间文件，产物与原生 FST 一致，查询工具零改动。转换只需本机有 Verdi 的
FsdbReader 运行库，**运行时不占 license**，见 [FSDB 波形接入指南](docs/FSDB_GUIDE.md)。

SHM 不在计划内。Cadence Xcelium 用户不用转存量波形，推荐直接从仿真源头产出 FST
（fstdumper VPI 插件，免 license、零转换），见
[Xcelium 直出 FST 指南](docs/XCELIUM_FST_GUIDE.md)。

**Q2：需要商用 License 吗？**
不需要，Apache-2.0 开源、任意并发、不限机器数。这也是它区别于商用调试 MCP 的核心点。

选择开源路线不只是省 license 费：FSDB、SHM 这类闭源波形格式，读取详细数据绕不开商用工具，
license 成本难以支撑 AI Agent 深度融入工作流后产生的高并发、海量波形分析需求。
wave-mcp 走 FST + VCD 开源路线，正是为成千上万条波形的并发分析场景提供高性能的开源替代方案。

**Q3：支持哪些仿真器？**
任何能产出 FST 或 VCD 的仿真器：Verilator（`--trace-fst`）、Icarus（`-fst`）、
Xcelium（[fstdumper 直出 FST](docs/XCELIUM_FST_GUIDE.md)）、VCS（VCD 转换，
存量 FSDB 走 [fsdb2fst](docs/FSDB_GUIDE.md)）、Questa（VCD 转换）等。
wave-mcp 不跑仿真器，只消费你已产出的波形。四种波形接入方式（FST 直读 /
VCD 自动转换 / FSDB 转换 / Xcelium 直出）的对比与支持状态详见
[仿真器兼容性说明](docs/SIMULATOR_COMPATIBILITY.md)。

**Q4：数据准不准？**
准。在真实生产级芯片项目上做了 225 万信号级验证，值查询正确性 100%；
层次与文件类工具（`scope_info` / `files`）32/32 模块验证通过。

**Q5：没有波形也能用吗？**
能。`open_static_session` 只凭 RTL 源码做静态分析（仿真前可用），这是 wave-mcp 的独有能力。

**Q6：支持 macOS / Windows 吗？**
Linux x86_64 开箱即用，其他平台没有官方支持，但可以自己适配。

卡点只有一个：`pylibfst` 目前只发布 Linux x86_64 的 wheel。其余依赖都已覆盖多平台
（`pyslang` 有 macOS arm64 / universal2 / win_amd64 / linux aarch64 官方 wheel，
`mcp` 是纯 Python），所以装好编译环境（cmake + C 编译器 + zlib，Windows 需 MSVC）后
`pip install pylibfst` 走 sdist 自行编译，大多能装上，之后分析类工具即可正常使用。

波形查看器则确定不可用：它依赖的 `surver` 是 Linux x86-64 二进制，没有 macOS / Windows
构建。未安装时相关工具优雅降级返回提示，不影响分析类工具。想在自己平台跑起来，需要按
[Surfer 项目](https://surfer-project.org/)自行编译 surver 并用
`WAVE_MCP_VIEWER_ASSETS` 指向资产目录，注意 surver 与 WASM 必须来自同一 Surfer commit，
否则连接时会因 wellen 版本不一致拒绝加载。

也可以直接在容器里跑（如 `python:3.11-slim`），绕开平台差异，这条路最省事。

**Q7：大波形性能如何？**
FST + C 系读取库（pylibfst）+ 进程常驻 + 随机访问，契合 AI 点查询场景；
百万级 scope 的超大模块可稳定完成分析。

**Q8：怎么接入我的 Code Agent？**
见 [Code Agent 集成](#code-agent-集成)，一段 `mcpServers` JSON 即可。

**Q9：目标机器只有 Python 3.8 / 3.9，能用吗？**
能，用离线 bundle 就行，不需要升级目标机。

Docker 流水线打出的 bundle 自带独立 Python 3.11（python-build-standalone），安装时优先用
捆绑的解释器，与系统自带的 Python 完全无关，所以 3.8 / 3.9 照样能跑。

详见 [`docs/DEPLOY_AIRGAP.md`](docs/DEPLOY_AIRGAP.md) 第 7 节。

**Q10：CentOS 7 / glibc 2.17 上报 `GLIBC_2.27' not found` 怎么办？**
官方 pyslang wheel 要求 glibc ≥ 2.28，老机器直接 pip 装不上。用 Docker 流水线的
glibc 2.17 档产物即可：`deploy/docker_build_all.sh` 自动在容器内自编兼容 wheel
并组装 `wave-mcp-bundle-glibc2.17.tar.gz`，整条链路（独立 Python + wheel + vcd2fst
+ musl 静态 surver）在 glibc ≥ 2.17 均可运行，含 CentOS 7。
详见 [`docs/DEPLOY_AIRGAP.md`](docs/DEPLOY_AIRGAP.md) 第 1.0 节与第 1c 节。

**Q11：FSDB 转换器和 TraceWeave 是什么关系？**
wave-mcp 在 FSDB 支持这部分功能参考了 [TraceWeave](https://github.com/gokeshenzhen/TraceWeave)（MIT，Copyright (c) 2025 gokeshenzhen），主要是时间刻度解析、`ffrAPI` 离线 stub 的接口子集与构建布局、以及 FSDB 位序和时间标签结构的核对。此外，`diff_waveforms`（pass/fail 波形首个分歧点定位）的代码为独立编写、未复用其代码，这个功能原本排在我们的开发规划中，TraceWeave 的 `diff_first_divergence` 出现得更早，我们在排功能优先级时参考过它，这一点一并致谢。

归属上我们出过错：相关注释 2026-08-31 随转换器写入，9-01 清理 vendor 引用时被一并删掉，声明里一度把该文件写成 `original code`，后来补回时范围也偏窄。两处均已更正。

参考的具体范围、独立实现与设计影响的边界、以及这次更正的完整说明，见 [`docs/THIRD_PARTY.md`](docs/THIRD_PARTY.md) 的 Attribution 一节；TraceWeave 的 MIT 许可全文另存于 [`docs/licenses/TraceWeave-MIT.txt`](docs/licenses/TraceWeave-MIT.txt)。

wave-mcp 项目的主体核心能力（pyslang 静态网表、trace 引擎、MCP 工具层）为独立开发。

---

## 架构

```
仿真器 → dump 波形(FST) → wave-mcp Server(多数据源聚合) → LLM 客户端(MCP)
                              ↑
              FST 波形 + pyslang RTL 网表
```

| 数据源 | 实现 | 能力 |
| --- | --- | --- |
| `fst_source.py` | `pylibfst`（fstapi，随机访问）+ 总线聚合 | 层次探索、信号、信号值 |
| `netlist/` + `rtl_source.py` | **pyslang**（完整 elaboration）+ FST | 连接、驱动、扇入扇出、trace、文件/声明 |
| `netlist/name_infer.py` | 实例名 → 模块定义名命名推断 | 网表未覆盖时兜底补全 module_type |

一个 **session** = 一个隔离的调试上下文（一人一模块），由 `session.json` 绑定数据源。

## 实现要点

- **不用朴素解析大 VCD**（慢、易 OOM）；走 **FST + C 系读取库 + 进程常驻 + 随机访问**。
- **网表离线一次精化、落盘复用**：pyslang 精化结果持久化为 `netlist/maps.json`
  （DriverMap/FanInMap/LoadMap/LocMap + instance_tree），一个纯 JSON 文件，任何脚本可读。
  启动即加载，不每次重建；源码未变不重跑精化，静态 session 升级为波形 session 时同一份
  网表直接复用（新旧判断基于源文件 mtime）。不传 `out_dir` 时网表按 RTL 源码的身份定位，
  同一份源码的所有 session 共用一次精化。
- **生成产物集中在两处**：session 目录（默认 `~/.wave-mcp/sessions/<输入身份>/`，
  或你传的 `out_dir`）只放 `session.json`（清单 + 指纹）和 `netlist/maps.json`（网表）；
  VCD/FSDB 转出的 `.fst` 与网表二级缓存放 `~/.wave-mcp/cache/`。分析查询全程在内存进行，
  不改动 RTL 源码和原始波形所在目录；删掉这两处即完成全部清理。
- **MCP 返回**：`structuredContent`（机器可读）+ `content[].text` 人读文本。

## 开源协议

本项目以 **Apache-2.0** 许可发布（见 [`LICENSE`](LICENSE)，内含全部第三方组件声明）。
核心依赖均为宽松许可（MIT/BSD/Apache），无 copyleft 传染；离线包附带的 `vcd2fst` 转换器由 GTKWave MIT 源码构建。
详见 [`docs/THIRD_PARTY.md`](docs/THIRD_PARTY.md)。

## 目录结构

```
wave_mcp/
  server.py              # MCP server，注册全部 37 工具
  session.py             # WorkSession / 共享数据集资源 / session.json / 三层 definition_name
  runtime/               # identity（唯一哈希入口）/ storage（唯一写盘位置）/ executor / auth / audit / request
  pipeline.py            # prepare_session / prepare_static_session 编排
  diff.py                # diff_waveforms 首分歧定位（时钟对齐采样）
  sources/               # fst_source + rtl_source
  netlist/               # slang_netlist / trace_engine / expr_eval / name_infer
  viewer/                # 波形查看器：manager / surver / translate / state / web 前端
  cli/                   # wave-session / wave-vcd2fst / wave-view
deploy/                  # 离线 bundle 构建 + 安装（含 Docker 一键流水线）
examples/                # 示例库（见上表）
tests/                   # 回归套件 run_regression.py
CHANGELOG.md             # 版本变更记录
docs/                    # DEPLOY_AIRGAP / SIMULATOR_COMPATIBILITY / FSDB_GUIDE / XCELIUM_FST_GUIDE / THIRD_PARTY / WAVE_VIEWER / VIEWER_SCREENSHOTS
```
