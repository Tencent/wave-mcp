# wave-mcp — 开源、免 License 的 RTL 波形调试 MCP Server

<img src="docs/images/penglai-logo.png" alt="蓬莱实验室" width="200"/>

[![PyPI version](https://img.shields.io/pypi/v/wave-mcp)](https://pypi.org/project/wave-mcp/)
[![Python versions](https://img.shields.io/pypi/pyversions/wave-mcp)](https://pypi.org/project/wave-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[English](README.en.md) | 简体中文

**wave-mcp 是腾讯蓬莱实验室验证团队开源的一款 RTL 波形调试 MCP Server**，为 LLM 提供波形调试工具集：
读 **FST 波形 + RTL 网表**，提供层次探索、信号查询、驱动分析、值/X 态追踪等 **27 个 MCP 工具**。
**MIT 开源，无需任何商用 License，支持任意并发。**

> 只要你的仿真器能 dump **FST**（Verilator `--trace-fst`、Icarus，或把 VCD 转 FST），
> wave-mcp 就能读它做调试。它**不跑仿真器**——你用自己的流程跑出波形，把结果交给它即可。

---

## 为什么是 wave-mcp

芯片验证占据开发周期 50% 以上的时间，波形调试是其中最高频的动作。而 LLM 时代，
工程师希望让 AI Agent 直接读波形、查信号、追 X 态根因——但市面上的商用调试
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
| 工具覆盖 | 27/27 全部工具实测 |

![工具调用分布](docs/images/tool-calls-distribution.png)

## 特性

- **波形查询**：设计层次、实例、信号（位宽/方向/类型，含总线聚合）、信号值（点查询 / 区间，随机访问）。
- **静态分析（pyslang 网表）**：连接、驱动、扇入/扇出、声明位置（文件:行号）。
- **无波形静态分析**：`open_static_session` 只凭 RTL 源码建 session——**仿真前即可分析设计结构**。
- **值追踪**：`trace_value` 沿驱动链反向遍历、可跨模块下钻，每个节点带真实 FST 值；`trace_x` 追 X 根因。
- **网表自愈**：从 pyslang 诊断自动补 `+incdir+` / 包源并重编；失败时优雅降级，其余工具不受影响。
- **一致性校验**：源码或波形变了但网表没更新会报警，绝不静默给错结果。
- **部署友好**：stdio（一人一进程，零运维）/ HTTP 多会话 / 离线自包含包（隔离网）。

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

> 操作系统 **Linux x86_64 开箱即用**（以上 Python 依赖均有预编译 wheel）。
> macOS / Windows / arm64 因 `pylibfst` 暂无预编译 wheel，需源码编译（cmake+gcc+zlib）。
> 隔离网 / 离线环境见 [`docs/DEPLOY_AIRGAP.md`](docs/DEPLOY_AIRGAP.md)。

---

## 快速开始

### 1. 安装

```bash
pip install wave-mcp
```

### 2. 跑一个示例

```bash
# 示例 A：Verilator 快启（counter 设计，产真实 FST，无需商用仿真器）
python examples/verilator_quickstart/run.py      # 需 verilator>=5

# 示例 B：静态分析（UART 设计，无需波形、无需仿真器，展示仿真前分析）
python examples/static_analysis/run.py

# 示例 C：极小内置样例（手写 VCD → vcd2fst → FST，零依赖）
python examples/make_sample.py
```

### 3. 打开你的波形

```bash
# 一条命令：波形(.fst/.vcd) + filelist → session（自动转 FST + 建网表）
wave-session --fst sim/dump.fst --top top_tb --filelist rtl.f --out sessions/my_module

# 启动 MCP Server（stdio，推荐：一人一进程）
python -m wave_mcp.server --session sessions/my_module
```

或者在你的 Code Agent 里直接用 MCP 工具 `prepare_session`，见下文集成示例。

## Code Agent 集成

`prepare_session` 是 MCP 统一入口，Code Agent 想分析波形时**第一步调它**，
传入仿真产出的波形，一次完成"（转换 →）建网表 → 建 session → 打开"：

```jsonc
prepare_session({
  "out_dir":      "sessions/my_module",
  "wave_path":    "sim/dump.fst",          // .fst 直读 / .vcd 自动转
  "top":          "top_tb",
  "filelist_path":"rtl.f",                 // 与仿真同一份 filelist
  "mode":         "speed"                  // VCD->FST：speed/balanced/size
})
// 返回 ready 后即可调 signal_values / list_child_instances / signal_drivers ...
```

**接入配置**（stdio，各家 Agent 的 MCP 配置）：

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
- **Cursor**：写入 `.cursor/mcp.json`
- **VS Code Copilot**：写入 `.vscode/mcp.json`
- 其余 Agent（Gemini CLI / Qwen Code / OpenHands 等）：按各自的 `mcpServers` 配置填入上述 JSON 即可

### 无波形静态分析（仿真前即可用）

`open_static_session` 只凭 RTL 源码建网表并打开 session——**不需要任何波形、不跑仿真**。
适合仿真前理解代码：查接口、查驱动/扇入扇出、浏览层次、做 code review。

```jsonc
open_static_session({
  "out_dir":      "sessions/my_module",
  "top":          "uart",
  "filelist_path":"rtl.f"
})
// 连接/驱动/层次/文件/声明类工具全部可用；值/追踪类工具返回明确的 "needs waveform" 提示
```

之后仿真产出波形时，用**同一个 out_dir** 调 `prepare_session` 升级为完整 session，已建好的网表直接复用。

### VCD → FST 转换（vcd2fst 配置，可选）

如果你的仿真器只吐 VCD（如 xrun），建议先转 FST：**体积约 VCD 的 1/50，随机访问快**。
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
# ① 独立转换（后处理）：mode=speed(fastlz,最快) / balanced(lz4) / size(zlib,最小)
wave-vcd2fst --vcd sim/dump.vcd --fst sim/dump.fst --mode speed

# ② 流式转换：把转换时间藏进仿真时间，仿真结束 FST 几乎同时就绪
wave-vcd2fst --stream --vcd sim/dump.vcd --fst sim/dump.fst
#   建 FIFO + 后台起 vcd2fst，然后 TB 里 $dumpfile("sim/dump.vcd") 指向该 FIFO 正常跑仿真

# ③ 建 session 一步到位（自动转 + 打包）
wave-session --vcd sim/dump.vcd --top top_tb --filelist rtl.f --out sessions/mod
```

> 通过 MCP 工具使用时无需手动转换：`prepare_session` 传入 `.vcd` 会自动走 ① 的转换路径。

---

## 工具（27 个，8 大类）

| 类别 | 工具 | 说明 |
| --- | --- | --- |
| 波形准备 | `prepare_session` / `open_static_session` / `convert_vcd_to_fst` | 波形入口 → session 一条龙；静态分析无需波形；不跑仿真器 |
| 会话管理 | `open_session` / `close_session` / `session_info` | `session_info` 含 netlist_health + definition_coverage |
| 层次探索 | `list_child_instances` / `list_modules` / `instances_of_module`(`_matching`) / `scope_info` | 模块定义名三层解析：网表 → 命名推断 → 手工 scope_map |
| 信号查询 | `list_signals` / `signal_info` | 位宽/方向/类型来自 FST（含总线聚合）；声明位置来自网表 |
| 值查询 | `signal_values` / `signal_values_in_range` / `signal_value_at` | FST 强项，随机访问 |
| 驱动分析 | `signal_connectivity` / `signal_drivers` / `signal_loads` / `signal_fanin` / `active_drivers` / `driver_contributors` | pyslang 网表（静态精确）+ 分支条件 4 值求值选活跃驱动 |
| 值/X 态追踪 | `trace_value` / `trace_x` | 网表 × FST 值反向遍历，跨模块下钻 |
| 文件 | `list_files` / `find_files` / `modules_in_file` | filelist + pyslang 网表 |

> 驱动分析与追踪类需要 pyslang 网表建成（`prepare_session` 时给对 filelist/incdirs/defines）。

---

## 示例库

| 示例 | 路径 | 依赖 | 展示内容 |
| --- | --- | --- | --- |
| Verilator 快启 | `examples/verilator_quickstart/` | Verilator 5+ | counter 设计 → 真实 FST → prepare_session 全流程 |
| 静态分析 | `examples/static_analysis/` | 无（纯 Python） | UART 设计无波形分析：层次/驱动/扇入/声明 |
| 极小样例 | `examples/make_sample.py` | 可选 vcd2fst | 手写 VCD → FST → session 冒烟 |

---

## 部署模式

- **stdio（推荐）**：每人本地起一个 Server 子进程，只加载自己模块的 FST+网表，零运维。
- **HTTP + 多 Session**：一个常驻服务，用 `session_id` 给每用户分隔离会话。
  `python -m wave_mcp.server --transport http --host 0.0.0.0 --port 8000`
- **隔离网 / 离线自包含包**：有网机器一键打包，拷贝到隔离网离线安装，自带独立 Python + 全部 wheel + 可选 vcd2fst：

  ```bash
  # ① 有网开发机：一键打包（可选 --python <独立Python包> --vcd2fst <二进制>）
  deploy/build_offline_bundle.sh --out /tmp/wave-mcp-bundle

  # ② 隔离网共享盘：解压后离线安装（无需联网/编译）
  tar -xzf wave-mcp-bundle.tar.gz -C /shared/ && cd /shared/wave-mcp-bundle
  ./install.sh --prefix /shared/wave-mcp      # 产出 bin/wave-mcp 启动器
  ```

  详见 [`docs/DEPLOY_AIRGAP.md`](docs/DEPLOY_AIRGAP.md)（含 vcd2fst 兼容性方案与排错）。

## FAQ

**Q1：需要商用 License 吗？**
不需要。MIT 开源，任意并发、不限机器数。这也是它区别于商用调试 MCP 的核心点。

**Q2：支持哪些仿真器？**
任何能产出 FST 或 VCD 的仿真器：Verilator（`--trace-fst`）、Icarus、xrun、VCS 等。
wave-mcp 不跑仿真器，只消费你已产出的波形。

**Q3：数据准不准？**
准。在真实生产级芯片项目上做了 225 万信号级验证，值查询正确性 100%；
层次与文件类工具（`scope_info` / `find_files` / `modules_in_file`）32/32 模块验证通过。

**Q4：没有波形也能用吗？**
能。`open_static_session` 只凭 RTL 源码做静态分析（仿真前可用），这是 wave-mcp 的独有能力。

**Q5：支持 macOS / Windows 吗？**
Linux x86_64 开箱即用。macOS / Windows / arm64 因 `pylibfst` 无预编译 wheel 需源码编译，
见[系统要求](#系统要求)。

**Q6：大波形性能如何？**
FST + C 系读取库（pylibfst）+ 进程常驻 + 随机访问，契合 AI 点查询场景；
实测百万级 scope 的超大模块稳定完成分析。

**Q7：怎么接入我的 Code Agent？**
见 [Code Agent 集成](#code-agent-集成)，一段 `mcpServers` JSON 即可。

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
- **网表离线一次性构建并持久化**（`maps.json`：DriverMap/FanInMap/LoadMap/LocMap + instance_tree），启动加载，不每次重建。
- **MCP 返回**：`structuredContent`（机器可读）+ `content[].text` 人读文本。

## 开源协议

本项目以 **MIT** 许可发布（见 [`LICENSE`](LICENSE)）。依赖均为宽松许可（MIT/BSD），
无 copyleft 传染；离线包附带的 `vcd2fst` 转换器由 GTKWave MIT 源码构建。
详见 [`docs/THIRD_PARTY.md`](docs/THIRD_PARTY.md)。

## 目录结构

```
wave_mcp/
  server.py              # MCP server，注册全部 27 工具
  session.py             # Session / session.json / 指纹校验 / 三层 definition_name
  pipeline.py            # prepare_session / prepare_static_session 编排
  sources/               # fst_source + rtl_source
  netlist/               # slang_netlist / trace_engine / expr_eval / name_infer
  cli/                   # wave-session / wave-vcd2fst
deploy/                  # 离线 bundle 构建 + 安装
examples/                # 示例库（见上表）
tests/                   # 回归套件 run_regression.py
docs/                    # DEPLOY_AIRGAP / SIMULATOR_COMPATIBILITY / THIRD_PARTY
```
