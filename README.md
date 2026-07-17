# wave-mcp — 开源、免 License 的 RTL 波形调试 MCP Server

[English](README.en.md) | 简体中文

用 **FST 波形 + pyslang RTL 网表** 等开源数据源，为 LLM 提供一套波形调试工具，
对齐 Cadence Indago / Verisium Debug 的核心能力（工具名为独立的简洁命名），
共 **25 个工具**。**无需任何商用 License，支持任意并发。** 以 **MIT** 许可开源。

> 只要你的仿真器能 dump **FST**（Verilator `--trace-fst`、Icarus、或把 VCD 转成 FST），
> wave-mcp 就能读它做调试。它**不跑仿真器**——你用自己的流程跑出波形，把结果交给它即可。

---

## 特性

- **波形查询**：设计层次、实例、信号（位宽/方向/类型，含总线聚合）、信号值（点查询 / 区间，随机访问）。
- **静态分析（pyslang 网表）**：连接、驱动、扇入/扇出、声明位置（文件:行号）。
- **值追踪**：`trace_value` 沿驱动链反向遍历、可跨模块下钻，每个节点带真实 FST 值；`trace_x` 追 X 根因。
- **网表自愈**：从 pyslang 诊断自动补 `+incdir+` / 包源并重编，自动探测 UVM 目录；失败时优雅降级，其余工具不受影响。
- **一致性校验**：`session.json` 记录波形/源码指纹，源码或波形变了但网表没更新会报警，绝不静默给错结果。
- **部署友好**：stdio（一人一进程，零运维）/ HTTP 多会话 / 离线自包含包（隔离网）。

---

## 架构

```
仿真器 → dump 波形(FST) → wave-mcp Server(多数据源聚合) → LLM 客户端(MCP)
                              ↑
              FST 波形 + pyslang RTL 网表
```

数据源（`wave_mcp/sources/` + `wave_mcp/netlist/`）：

| 数据源 | 实现 | 能力 |
| --- | --- | --- |
| `fst_source.py`  | `pylibfst`（fstapi，随机访问）+ 总线聚合 | 层次探索、信号、信号值 |
| `netlist/` + `rtl_source.py`  | **pyslang**（完整 elaboration）+ FST | 连接、驱动、扇入扇出、trace、文件/声明 |
| `netlist/name_infer.py`  | 实例名 → 模块定义名命名推断 | 网表未覆盖时兜底补全 module_type |

一个 **session** = 一个隔离的调试上下文（一人一模块），由 `session.json` 把数据源绑定在一起。

---

## 安装

```bash
# 从 git 安装（Linux x86_64 开箱即用；依赖 mcp + pylibfst + pyslang）
pip install git+https://github.com/<your-org>/wave-mcp.git
# 或克隆后本地安装：
#   git clone <repo> && cd wave-mcp && pip install -e .

# 系统二进制（按需）：vcd2fst（GTKWave，VCD→FST 转换；已有 FST 则不需要）
#   Debian/Ubuntu: sudo apt install gtkwave   |   macOS: brew install gtkwave
```

> **平台支持**：**Linux x86_64** 有全部依赖的预编译 wheel，`pip` 开箱即用（已测 Python 3.9–3.13）。
> macOS / Windows / arm64 因 `pylibfst` 暂无预编译 wheel，需源码编译（cmake+gcc+zlib）。
> 隔离网 / 离线环境见 [`docs/DEPLOY_AIRGAP.md`](docs/DEPLOY_AIRGAP.md)。

---

## 快速开始

**开源、无需商用仿真器**——用 Verilator 产真实 FST 再打开分析，一条命令：

```bash
python examples/verilator_quickstart/run.py   # 需 verilator>=5；详见该目录 README
```

或用内置的极小样例（手写 VCD → vcd2fst → FST）：

```bash
# 1) 生成样例
python examples/make_sample.py

# 2) 打包成 session
python -m wave_mcp.cli.build_session \
    --fst examples/sample/dump.fst \
    --top top_tb --filelist examples/sample/rtl.f \
    --out examples/sample/session

# 3) 端到端冒烟测试
python tests/smoke_test.py

# 4) 启动 MCP Server（stdio，推荐：一人一进程）
python -m wave_mcp.server --session examples/sample/session
```

---

## 标准工作流（分析波形的统一入口）

`prepare_session` 是统一入口——想开始分析波形时**第一步就调它**，传入仿真已产出的
波形文件，一次完成"（转换 →）建网表 → 建 session → 打开"，返回即可直接查询。

```
prepare_session ─┬─ 波形文件入口                 # .fst 直读 / .vcd 自动转
                 ├─ convert VCD → FST           # 仅当传入 .vcd，默认 speed(fastlz)
                 ├─ build netlist (pyslang)     # 可选，启用 连接/驱动/trace
                 ├─ build session.json + 指纹
                 └─ open session                # 完成后直接用查询类工具
```

调用示例：

```jsonc
prepare_session({
  "out_dir":      "sessions/my_module",
  "wave_path":    "sim/dump.fst",          // 仿真产出的波形：.fst 直读 / .vcd 自动转
  "top":          "top_tb",
  "filelist_path":"rtl.f",                 // 与仿真同一份 filelist（启用网表/声明类工具）
  "mode":         "speed"                   // VCD->FST：speed/balanced/size（仅 .vcd 时生效）
})
// 返回 ready + session 摘要后，接着调 signal_values / list_child_instances ...
```

> 传入 `.fst` 零转换直读；传入 `.vcd` 自动转成 FST（体积约为 VCD 的 1/50）。
> 想拆开用也行：`convert_vcd_to_fst` → `open_session`。

### VCD → FST 转换

若仿真器只吐 VCD，可先转 FST（体积约 1/50、随机访问快）。三种入口：

```bash
# 后处理转换（最快参数：mode=speed=fastlz + 并行）
wave-vcd2fst --vcd sim/dump.vcd --fst sim/dump.fst --mode speed
#   mode: speed(fastlz,最快) / balanced(lz4) / size(zlib,最小)

# 流式转换——把转换时间藏进仿真时间里，仿真结束 FST 几乎同时就绪
wave-vcd2fst --stream --vcd sim/dump.vcd --fst sim/dump.fst   # 建 FIFO + 后台起 vcd2fst
#   然后 TB 里 $dumpfile("sim/dump.vcd") 指向该 FIFO，正常跑仿真即可

# 建 session 时一步到位（自动转 + 打包）
wave-session --vcd sim/dump.vcd --top top_tb --filelist rtl.f --out sessions/mod
```

### MCP 客户端配置（stdio）

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

---

## 部署模式

- **stdio（推荐）**：每人本地起一个 Server 子进程，只加载自己模块的 FST+网表。零运维。
  `python -m wave_mcp.server --session <session_dir>`
- **HTTP + 多 Session**：一个常驻服务，用 `session_id` 给每用户分隔离会话。
  `python -m wave_mcp.server --transport http --host 0.0.0.0 --port 8000`
  每个工具都接受可选 `session_id`；先 `open_session(session_path, session_id=...)` 再调用其它工具。
- **隔离网 / 离线自包含包**：有网机器用 `deploy/build_offline_bundle.sh` 生成自包含 bundle
  （自带独立 Python + 全部 wheel + 可选 vcd2fst），拷到目标机 `install.sh` 离线安装。
  详见 [`docs/DEPLOY_AIRGAP.md`](docs/DEPLOY_AIRGAP.md)。

---

## 工具（25 个）

| 类别 | 工具 | 说明 |
| --- | --- | --- |
| 波形准备 | `prepare_session` / `convert_vcd_to_fst` | 波形文件入口（.fst 直读 / .vcd 自动转）→ session 一条龙；不跑仿真器 |
| 会话管理 | `open_session` / `close_session` / `session_info` | `session_info` 含 netlist_health + definition_coverage |
| 层次探索 | `list_child_instances` / `list_modules` / `instances_of_module`(`_matching`) / `scope_info` | 模块定义名走三层解析：pyslang 网表 → 命名推断 → 手工 scope_map |
| 信号查询 | `list_signals` / `signal_info` | 位宽/方向/类型来自 FST（含总线聚合）；声明文件+行号来自网表 |
| 信号值 | `signal_values` / `signal_values_in_range` | FST 强项，随机访问 |
| 连接/驱动 | `signal_connectivity` / `signal_drivers` / `signal_loads` / `signal_fanin` / `active_drivers` / `driver_contributors` | pyslang 网表（静态精确）+ 分支条件 4 值求值选活跃驱动；无网表时优雅降级 |
| 值追踪 | `trace_value` / `trace_x` | pyslang 网表 × FST 值反向遍历，支持跨模块下钻；trace_x 近似 |
| 文件查询 | `list_files` / `find_files` / `modules_in_file` | filelist + pyslang 网表 |

> 第 连接/驱动 与 追踪 类需要 pyslang 网表建成（`prepare_session` 时给对 filelist/incdirs/defines）；
> `active_drivers` / `trace_x` 在条件为 X 或表达式超出 4 值求值子集时为 value-informed 近似，会标注
> `selection_method`，但始终提供精确的静态驱动链 + 每节点 FST 值 + 代码位置。

---

## 实现要点

- **不用朴素解析大 VCD**（慢、易 OOM）；走 **FST + C 系读取库（pylibfst）+ 进程常驻 + 随机访问**，契合 AI 的点查询/搜索场景。
- **网表离线一次性构建并持久化**（`maps.json`：DriverMap/FanInMap/LoadMap/LocMap + instance_tree），Server 启动加载，不每次重建。
- **definition_name 三层解析**：netlist（含锚点向上推导）→ 命名推断（含 interface 守卫、置信度分级）→ 手工 `scope_map`。
- **MCP 返回**：`structuredContent`（机器可读）+ `content[].text` 人读文本（无转义 `\n` / `\"`）。

---

## 目录结构

```
wave_mcp/
  server.py              # MCP server，注册全部 25 工具（FastMCP）
  session.py             # Session / SessionManager / session.json / 指纹校验 / 三层 definition_name
  pipeline.py            # prepare_session：波形文件入口(.fst 直读/.vcd 自动转)→网表→session 编排
  convert.py             # vcd2fst 封装：并行能力探测 + 串行 fallback + FIFO 流式
  timeutil.py            # 时间字符串 <-> FST 时间单位换算
  sources/
    fst_source.py        # pylibfst：层次 / 信号 / 值 / 总线聚合
    rtl_source.py        # pyslang 网表加载 + 查询：连接/驱动/trace/文件/netlist_health
  netlist/
    slang_netlist.py     # pyslang elaboration → maps.json + 自愈 + UVM 探测
    trace_engine.py      # 结构×时间 trace 引擎 + definition_name 解析
    expr_eval.py         # 4 值(0/1/x/z) 分支条件求值
    name_infer.py        # 实例名→模块定义名命名推断
  cli/build_session.py   # wave-session：组装 session 目录 + 指纹
  cli/vcd2fst.py         # wave-vcd2fst：VCD→FST（含流式）
deploy/                  # 离线 bundle 构建 + 安装脚本（见 docs/DEPLOY_AIRGAP.md）
examples/make_sample.py             # 生成极小样例
examples/verilator_quickstart/      # Verilator 开箱示例（--trace-fst 产真实 FST，无需 xrun）
tests/                              # smoke_test / 单元测试
LICENSE                            # MIT
licenses/THIRD_PARTY.md            # 第三方组件许可声明
```

---

## 开源协议

本项目以 **MIT** 许可发布（见 [`LICENSE`](LICENSE)）。

依赖与内置组件均为宽松许可，无 copyleft 传染：`mcp` / `pyslang` / `pylibfst` 皆为
MIT/BSD。离线包附带的 `vcd2fst` 转换器由 GTKWave 的 **MIT** 源码（libfst/fstapi +
vcd2fst helper）构建，作为独立进程调用（聚合关系，不影响 wave-mcp 的 MIT 许可）；
其内含的 `jrb` 组件为 LGPL-2.1，随包提供构建脚本以满足可重链接义务。
详见 [`licenses/THIRD_PARTY.md`](licenses/THIRD_PARTY.md)。
