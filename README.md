# wave-mcp：开源、免 License 的 RTL 波形调试 MCP Server

<img src="docs/images/penglai-logo.png" alt="蓬莱实验室" width="200"/>

[![PyPI version](https://img.shields.io/pypi/v/wave-mcp)](https://pypi.org/project/wave-mcp/)
[![Python versions](https://img.shields.io/pypi/pyversions/wave-mcp)](https://pypi.org/project/wave-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[English](README.en.md) | 简体中文

**wave-mcp 是腾讯蓬莱实验室验证团队开源的一款 RTL 波形调试 MCP Server**，为 LLM 提供波形调试工具集：
读 **FST 波形 + RTL 网表**，提供层次探索、信号查询、驱动分析、值/X 态追踪、波形对比与浏览器波形查看器等 **34 个 MCP 工具**。
**MIT 开源，无需任何商用 License，支持任意并发。**

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
| 工具覆盖 | 34 个工具全部验证，含 viewer / diff 的单元与浏览器端到端覆盖 |

![工具调用分布](docs/images/tool-calls-distribution.png)

## 特性

- **波形查询**：设计层次、实例、信号（位宽/方向/类型，含总线聚合）、信号值（点查询 / 区间，随机访问）。
- **静态分析（pyslang 网表）**：连接、驱动、扇入/扇出、声明位置（文件:行号）。
- **无波形静态分析**：`open_static_session` 只凭 RTL 源码建 session，**仿真前即可分析设计结构**。
- **值追踪**：`trace_value` 沿驱动链反向遍历、可跨模块下钻，每个节点带真实 FST 值；`trace_x` 追 X 根因。
- **网表自愈**：从 pyslang 诊断自动补 `+incdir+` / 包源并重编；失败时优雅降级，其余工具不受影响。
- **一致性校验**：源码或波形变了但网表没更新会报警，绝不静默给错结果。
- **波形对比**：`diff_waveforms` 对 pass/fail 两份波形定位首个分歧时刻，按分歧时间排序信号，时钟对齐采样过滤毛刺。
- **波形查看器**：`open_wave_view` 让 agent 分析完直接弹浏览器波形，嫌疑信号 + 游标钉在出错时刻 + 分析说明弹窗；双波形 lockstep 对比；`get_view_state` 反向感知用户在看什么。
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

## CLI 模式

不挂 Code Agent 时，也能在终端直接调用全部 34 个工具（与 MCP 同名同参数）：

```bash
wave-mcp query --list                            # 列出全部 34 个工具

wave-mcp query signal_values --session sessions/my_module \
    --full_path top.u_tx.tx_serial              # 查询信号值变化

wave-mcp query signal_drivers --session sessions/my_module \
    --json-args '{"full_path": "top.u_tx.tx_serial"}'   # JSON 传参
```

- 参数按工具签名自动生成，`wave-mcp query <工具名> --help` 查看
- 默认输出人读文本，加 `--json` 输出完整结构化结果
- 适合 CI 脚本、开发调试、快速验证；每次新增工具自动获得 CLI 接口

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
  "out_dir":      "sessions/my_module",
  "top":          "uart",
  "filelist_path":"rtl.f"
})
// 连接/驱动/层次/文件/声明类工具全部可用；值/追踪类工具返回明确的 "needs waveform" 提示
```

之后仿真产出波形时，用**同一个 out_dir** 调 `prepare_session` 升级为完整 session，已建好的网表直接复用。

每条驱动记录都带完整语境：驱动类型、源码位置、语句片段、右值来源、以及压在这条语句上的
**全部门控条件**（可 4 值求值的表达式树）。以示例 B 的 UART 为例：

```yaml
# wave-mcp query signal_drivers --session ... --full_path uart_top.u_tx.tx_serial
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

## 工具（34 个，10 大类）

| 类别 | 工具 | 说明 |
| --- | --- | --- |
| 波形准备 | `prepare_session` / `open_static_session` / `convert_vcd_to_fst` / `convert_fsdb_to_fst` | 波形入口 → session 一条龙（`.fst` / `.fsdb` / `.vcd` 自动识别，转换带缓存）；静态分析无需波形；不跑仿真器 |
| 会话管理 | `open_session` / `close_session` / `session_info` | `session_info` 含 netlist_health + definition_coverage |
| 层次探索 | `list_child_instances` / `list_modules` / `instances_of_module`(`_matching`) / `scope_info` | 模块定义名三层解析：网表 → 命名推断 → 手工 scope_map |
| 信号查询 | `list_signals` / `signal_info` | 位宽/方向/类型来自 FST（含总线聚合）；声明位置来自网表 |
| 值查询 | `signal_values` / `signal_values_in_range` / `signal_value_at` | FST 强项，随机访问 |
| 驱动分析 | `signal_connectivity` / `signal_drivers` / `signal_loads` / `signal_fanin` / `active_drivers` / `driver_contributors` | pyslang 网表（静态精确）+ 分支条件 4 值求值选活跃驱动 |
| 值/X 态追踪 | `trace_value` / `trace_x` | 网表 × FST 值反向遍历，跨模块下钻 |
| 波形对比 | `diff_waveforms` | pass/fail 双波形首分歧定位：首分歧时刻 + 分歧信号排序 + 时钟对齐采样滤毛刺；分歧信号直接接 `signal_fanin`/`active_drivers` 做因果回溯 |
| 波形查看器 | `open_wave_view` / `update_wave_view` / `get_view_state` / `list_wave_views` / `close_wave_view` | agent 分析完自动弹浏览器波形：嫌疑信号 + 游标钉出错时刻 + 分析说明弹窗；双波形对比视图 lockstep 联动；`get_view_state` 让 agent 感知用户当前看什么（对话式双向调试）；`list_wave_views` / `close_wave_view` 管理视图生命周期，批量场景可收尾释放 |
| 文件 | `list_files` / `find_files` / `modules_in_file` | filelist + pyslang 网表 |

> 驱动分析与追踪类需要 pyslang 网表建成（`prepare_session` 时给对 filelist/incdirs/defines）。
> 查看器类需要安装可选资产包：`pip install wave-mcp[viewer]`（Surfer WASM + surver，EUPL-1.2 独立分发，核心包保持 MIT）；未安装时相关工具优雅降级返回提示，分析工具不受影响。

### 波形查看器（wave-view）

```bash
# 打开单个波形（几十 GB 的 FST 也是秒开：surver 服务端流式，浏览器按需取数据）
wave-view dump.fst --signals top.u_dma.req_valid --cursor 1523400ps

# 双波形对比视图（上下两个 pane，缩放/游标 lockstep 联动）
wave-view pass.fst fail.fst --labels pass fail
```

- 命令行打印 URL；桌面环境自动开浏览器，SSH/code agent 场景 IDE 终端自动转发端口点开即看。
- agent 典型闭环：case 挂了 → `diff_waveforms(pass, fail)` 定位首分歧 → `signal_fanin` 回溯根因 → `open_wave_view` 双波形 + 分歧 marker + 分析说明弹窗一次呈现。
- 分析说明是可收起的 log 弹窗，说明里的时刻引用（如 `[85000ps](#t=85000ps)`）点击即跳游标，游标/视口/marker 更新为无闪刷新。
- 完整指南（MCP 工具参数、双向调试工作流、架构原理、部署与排障）见 [`docs/WAVE_VIEWER.md`](docs/WAVE_VIEWER.md)。
- 想先看效果，见 [`docs/VIEWER_SCREENSHOTS.md`](docs/VIEWER_SCREENSHOTS.md)：四个真实调试场景的界面截图，含一键复现步骤。

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

全部变量都可以写在 MCP 客户端配置的 `env` 里，**推荐这样配**：Agent 以子进程方式拉起 Server，
继承不到你交互式 shell 里 `export` 的变量，写进 `env` 才稳定生效。

```json
{
  "mcpServers": {
    "wave-mcp": {
      "command": "wave-mcp",
      "env": {
        "VERDI_HOME": "/path/to/verdi",
        "WAVE_MCP_SESSION_ROOT": "~/wave-sessions"
      }
    }
  }
}
```

| 变量 | 作用 | 缺省行为 |
| --- | --- | --- |
| `WAVE_MCP_SESSION_ROOT` | Session 落点根目录。设置后 `out_dir` 一律解析到该目录下，落点由部署决定而非由模型自由选择 | 不限制，按传入的 `out_dir` 落盘 |
| `VERDI_HOME` / `NOVAS_HOME` | 定位 Verdi FsdbReader 运行库，读 `.fsdb` 所需（须含 `share/FsdbReader/linux64`） | 不设则 FSDB 输入不可用，其余功能正常 |
| `FSDB2FST_FREADER` | 直接指向拷来的 `share/FsdbReader` 目录，替代 `VERDI_HOME` | 回落到 `VERDI_HOME` / `NOVAS_HOME` |
| `FSDB2FST_BIN` | 指定已编译好的 `fsdb2fst` | 自动探测，必要时首次转换就地编译 |
| `WAVE_MCP_FSDB2FST_AUTOBUILD` | 设 `0` 关闭首次自动编译 | 开启 |
| `VCD2FST_BIN` | 指定 GTKWave `vcd2fst` 可执行文件 | 从 `PATH` 找 `vcd2fst` |
| `WAVE_MCP_VIEWER_ASSETS` | 波形查看器资产目录（须含 `surver` 与 `wasm/index.html`） | 依次找 pip 资产包、`~/.cache/wave-mcp/viewer/` |
| `WAVE_MCP_VIEWER_PORT_BASE` | 把视图端口限制在 `[base, base+64)`，便于固定一条 `ssh -L` 转发规则；多人共用一台主机时各取一段 | 随机高端口 |
| `WAVE_MCP_MAX_VIEWS` | 并发视图上限，超出淘汰最旧的视图；`0` 关闭上限 | 8 |
| `XDG_CACHE_HOME` | 缓存根目录（`fsdb2fst` 构建产物、viewer 资产） | `~/.cache` |

**Session 目录约定**：建议统一放 `~/wave-sessions/<项目>_<模块>/`，同一模块的静态分析与波形分析
复用同一个 `out_dir` 以复用网表；不要用 `/tmp`（重启即丢，网表需重新精化）或共享盘（多人撞目录）。
配上 `WAVE_MCP_SESSION_ROOT` 即可强制生效，不依赖 Agent 是否记得这条约定。

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
不需要，MIT 开源、任意并发、不限机器数。这也是它区别于商用调试 MCP 的核心点。

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
层次与文件类工具（`scope_info` / `find_files` / `modules_in_file`）32/32 模块验证通过。

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
  网表直接复用（新旧判断基于源文件 mtime）。
- **生成产物集中在 session 目录**：`prepare_session` 只向你指定的 `out_dir` 写入
  `session.json`（清单 + 指纹）、`netlist/maps.json`（网表），以及仅当输入是 VCD 时
  转换出的 `.fst`。分析查询全程在内存进行，不落盘任何索引或缓存，也不改动 RTL 源码
  和原始波形所在目录；删掉 session 目录即完成全部清理。
- **MCP 返回**：`structuredContent`（机器可读）+ `content[].text` 人读文本。

## 开源协议

本项目以 **MIT** 许可发布（见 [`LICENSE`](LICENSE)）。依赖均为宽松许可（MIT/BSD），
无 copyleft 传染；离线包附带的 `vcd2fst` 转换器由 GTKWave MIT 源码构建。
详见 [`docs/THIRD_PARTY.md`](docs/THIRD_PARTY.md)。

## 目录结构

```
wave_mcp/
  server.py              # MCP server，注册全部 34 工具
  session.py             # Session / session.json / 指纹校验 / 三层 definition_name
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
