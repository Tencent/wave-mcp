# wave-mcp — 基于 xrun 的开源 Indago MCP 替代方案

去 License、无并发上限的 AI 辅助波形调试 MCP Server。用 **FST 波形 + pyslang RTL 网表** 等开源数据源，对齐 Cadence Indago / Verisium Debug MCP 的核心能力（工具名为独立的简洁命名），共 **25 个工具**。本项目以 **MIT** 许可开源。

> 仿真器：xrun（Cadence Xcelium）。xrun 仿真与 dump 波形本身不占 Indago License —— 真正吃 License 的是 Indago 的波形引擎与调试数据库。本项目替换掉该波形引擎层，MCP 层完全开源自建，**支持任意并发**。

---

## 架构

```
xrun 仿真 → dump 开源波形(FST) → wave-mcp Server(多数据源聚合) → LLM 客户端(MCP)
                                      ↑
        FST 波形 + pyslang RTL 网表
```

数据源（`wave_mcp/sources/` + `wave_mcp/netlist/`）：

| 数据源 | 实现 | 覆盖的 Indago 类别 | 状态 |
| --- | --- | --- | --- |
| `fst_source.py`  | `pylibfst`（fstapi，随机访问）+ 总线聚合 | 层次探索、信号、信号值（2/3/4） | ✅ 已实现 |
| `name_infer.py`  | 实例名→模块定义名命名推断 | 补全 module_type（netlist 未覆盖时兜底） | ✅ 已实现 |
| `netlist/` + `rtl_source.py`  | **pyslang**（完整 elaboration）+ FST | 连接、驱动、扇入扇出、trace、文件/声明（5/6/8、2.5） | ✅ 已实现（单一后端，无需 Surelog/UHDM/Verible） |

一个 **session** = 一个隔离的调试上下文（一人一模块），由 `session.json` 清单把所有数据源绑定在一起，并记录指纹做一致性校验（源码改了/重仿了但网表没更新 → 报警，绝不静默给错结果）。

---

## 安装

```bash
# 直接从 git 安装（Linux x86_64 开箱即用；依赖 mcp + pylibfst + pyslang）
pip install git+https://github.com/<your-org>/wave-mcp.git
# 或克隆后本地安装：
#   git clone <repo> && cd wave_mcp && pip install -e .

# 系统二进制（按需）：vcd2fst（GTKWave，VCD→FST 转换；已有 FST 则不需要）
#   Debian/Ubuntu: sudo apt install gtkwave  |  macOS: brew install gtkwave
# 离线/隔离网自包含安装见 docs/DEPLOY_AIRGAP.md；发版打包见 docs/RELEASE_BUNDLE.md
```

> 平台支持：**Linux x86_64** 有全部依赖的预编译 wheel，`pip` 开箱即用（已测 Python 3.9–3.13）。
> macOS / Windows / arm64 因 `pylibfst` 暂无预编译 wheel，需源码编译（cmake+gcc+zlib）；
> 隔离网用户建议直接用离线自包含包（见下）。

## 快速开始

```bash
# 1) 生成样例（手写 VCD -> vcd2fst -> FST + filelist）
python examples/make_sample.py

# 2) 打包成 session（阶段5 的"无痛封装"）
python -m wave_mcp.cli.build_session \
    --fst examples/sample/dump.fst \
    --top top_tb --filelist examples/sample/rtl.f \
    --out examples/sample/session

# 3) 端到端冒烟测试（不依赖 GUI/网表）
python tests/smoke_test.py

# 4) 启动 MCP Server（stdio，推荐：一人一进程）
python -m wave_mcp.server --session examples/sample/session
```

想用**真实波形**跑通（开源、无需 xrun）？看 **Verilator quickstart**：用 Verilator
`--trace-fst` 产出真实 FST 再 `prepare_session` 直读、查询，一条命令搞定：

```bash
python examples/verilator_quickstart/run.py   # 需 verilator>=5；详见该目录 README
```

### 标准工作流（模型分析波形的统一入口）

团队固定流程：**仿真产出波形 → 转 FST → 读取分析**。`prepare_session` 是这条链的统一入口——模型想开始分析波形时**第一步就调它**，传入仿真已产出的波形文件，一次完成"（转换 →）建网表 → 建 session → 打开"，返回即可直接查询。

> **它不跑仿真器**：请先用你自己的流程（xrun / Verilator / 任意）跑出波形，再把结果 `.fst` / `.vcd` 交给它。这样开源版本不承载任何 shell 执行面。

```
prepare_session ─┬─ 波形文件入口                 # .fst 直读 / .vcd 自动转
                 ├─ convert VCD → FST           # 仅当传入 .vcd，默认 speed(fastlz)
                 ├─ build netlist (pyslang)     # 可选，enables 连接/驱动/trace
                 ├─ build session.json + 指纹
                 └─ open session                # 完成后直接用查询类工具
```

模型侧调用示例（参数按你的项目填）：

```jsonc
prepare_session({
  "out_dir":      "sessions/my_module",
  "wave_path":    "sim/dump.fst",          // 仿真产出的波形：.fst 直读 / .vcd 自动转
  "top":          "top_tb",
  "filelist_path":"rtl.f",                 // 与仿真同一份 filelist
  "mode":         "speed"                   // VCD->FST：speed/balanced/size（仅 .vcd 时生效）
})
// 返回 ready + session 摘要后，接着调 signal_values / list_child_instances ...
```

> 传入 `.fst` 时零转换、直接读取；传入 `.vcd` 时自动转成 FST（体积约为 VCD 的 1/50）。
> 想拆开用也行：`convert_vcd_to_fst` → `open_session`。

### VCD → FST 转换（仿真的开源可解析 dump 是 VCD）

xrun 的开源可解析波形是 VCD，而本 Server 读的是 FST（体积约 1/50、随机访问快）。提供三种入口，且**转化越快越好**用三个杠杆：`-p` 并行 + `-F`(fastlz) 最快 + FIFO 流式。

```bash
# 方式1：后处理转换（最快参数：mode=speed=fastlz+parallel）
wave-vcd2fst --vcd sim/dump.vcd --fst sim/dump.fst --mode speed
#   mode: speed(fastlz,最快) / balanced(lz4) / size(zlib,最小)

# 方式2：流式转换——把转换时间藏进仿真时间里，仿真结束 FST 几乎同时就绪（端到端最快，零额外等待）
wave-vcd2fst --stream --vcd sim/dump.vcd --fst sim/dump.fst   # 建 FIFO + 后台起 vcd2fst
#   然后 TB 里 $dumpfile("sim/dump.vcd") 指向该 FIFO，正常跑 xrun 即可

# 方式3：建 session 时一步到位（自动转 + 打包）
wave-session --vcd sim/dump.vcd --top top_tb --filelist rtl.f --out sessions/mod
```

也可让 AI 直接调用 MCP 工具 `convert_vcd_to_fst(vcd_path, fst_path, mode, parallel)`。

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

- **模式一 stdio（推荐）**：每人本地起一个 Server 子进程，只加载自己模块的 FST+网表。零运维，最契合"每人各看各模块"。
  `python -m wave_mcp.server --session <session_dir>`
- **模式二 中心常驻 HTTP + 多 Session**：一个常驻服务，用 `session_id` 给每用户分隔离会话。
  `python -m wave_mcp.server --transport http --host 0.0.0.0 --port 8000`
  每个工具都接受可选 `session_id` 参数；先 `open_session(session_path, session_id=...)` 再调用其它工具。

### 隔离网 / 离线环境部署
多机器、多用户、Python 版本不一、无外网 → **共享存储自带独立 Python 运行时**，stdio 无感启停。
在有网机器 `deploy/build_offline_bundle.sh` 生成自包含 bundle，拷到隔离网共享存储 `install.sh` 离线安装。
详见 **`docs/DEPLOY_AIRGAP.md`**；研发进度与 To-Do 见 **`docs/PROGRESS.md`**。

---

## 工具覆盖度（25 工具，对齐 Indago 核心能力）

| 类别 | 工具 | 状态 |
| --- | --- | --- |
| 0 波形准备 | prepare_session / convert_vcd_to_fst | ✅ 波形文件入口（.fst 直读 / .vcd 自动转）→ session 一条龙；不跑仿真器 |
| 1 会话管理 | open_session / close_session / session_info | ✅ session_info 含 netlist_health + definition_coverage |
| 2 层次探索 | list_child_instances / list_modules / instances_of_module(_matching) / scope_info | ✅ 模块定义名走**三层解析**：pyslang 网表(leaf/后缀/锚点) → 命名推断 → 手工 scope_map；声明位置来自网表 |
| 3 信号查询 | list_signals / signal_info | ✅ 位宽/方向/类型来自 FST（含总线聚合 + RTL 位宽校验）；声明文件+行号来自 pyslang 网表 |
| 4 信号值 | signal_values / signal_values_in_range | ✅ FST 强项，随机访问 |
| 5 连接/驱动 | signal_connectivity / signal_drivers / signal_loads / signal_fanin / active_drivers / driver_contributors | ✅ pyslang 网表（连接/驱动/扇入扇出，静态精确）+ **分支条件 4 值求值**选活跃驱动，不确定回退启发式；无网表时优雅降级 |
| 6 值追踪 | trace_value / trace_x | ✅ pyslang 网表 × FST 值反向遍历，支持跨模块下钻；trace_x 近似 |
| 8 文件查询 | list_files / find_files / modules_in_file | ✅（filelist + pyslang 网表） |

全部工具均已实现（单一 pyslang + FST 后端）。第 5/6 类的活跃驱动判定 / trace_x 在条件为 X 或表达式超出 4 值求值子集时为 value-informed 近似，会标注 `selection_method`；但始终提供精确的静态驱动链 + 每节点 FST 值 + 代码位置。

### v4.x 主要增强
- **网表自愈**：从 pyslang 诊断（缺 include/package）自动补 incdir/包源并重编；自动探测 Cadence `-uvmhome` UVM 目录。
- **definition_name 三层解析**：netlist（含锚点向上推导）→ 命名推断（含 interface 守卫、置信度分级）→ 手工 scope_map。
- **netlist_health / definition_coverage**：区分 error 与 warning（大量 UVM lint 不误判为坏网表），并报告自愈回填项。
- **vcd2fst 并行**：编译期开 `FST_WRITER_PARALLEL`，多核转换 + 能力探测/串行 fallback。
- **隔离网离线**：剔除高 glibc 的 cryptography（非必需），`--no-deps` 离线安装；自带独立 Python + glibc≤2.14 的 vcd2fst。
- **MCP 返回**：structuredContent（机器可读）+ content[].text 人读文本（无转义 `\n`/`\"`）。

---

## 开发路线图

- [x] **阶段1 验证**：xrun dump VCD/FST + 波形可读。
- [x] **阶段2 基础 MCP**：FST 读取；实现第 3/4 类。**可上线，立即缓解 License 压力。**
- [x] **阶段3 静态分析**：用 **pyslang** 完整 elaboration，离线构建并持久化 DriverMap/FanInMap/LoadMap/LocMap + instance_tree；补齐第 2.5/3行号/5/8 类。
- [x] **阶段4 trace 引擎**：trace_value（多数场景准确）+ trace_x（近似）——"结构维(pyslang 网表) × 时间维(FST 波形值)"二维反向遍历。
- [x] **阶段5 工程化**：`wave-session`/`prepare_session` 封装 + `session.json` + 指纹一致性校验 + 失败分级降级；隔离网离线运行时打包（共享存储自带 Python + glibc 兼容 vcd2fst）已完成，见 `docs/RELEASE_BUNDLE.md` / `docs/DEPLOY_AIRGAP.md`。
- [x] **阶段6 数据质量与鲁棒性**：网表自愈（incdir/包 + UVM 探测）、definition_name 三层解析、netlist_health、vcd2fst 并行、MCP 人读返回。

### 性能要点（按需求文档）
- 禁止用 pyvcd 朴素解析大 VCD（慢 10~100×、易 OOM）。
- 正确路线：FST + Rust/C 系读取库（pylibfst）+ 进程常驻 + 预建索引 + LRU 缓存。
- AI 场景以点查询/搜索为主，正是 FST 随机访问强项。
- DriverMap/FanInMap 离线一次性构建并持久化（sqlite/json），Server 启动加载，不每次重建。

---

## 目录结构

```
wave_mcp/
  server.py              # MCP server，注册全部 25 工具（FastMCP）+ 人读文本渲染补丁
  session.py             # Session / SessionManager / session.json / 指纹校验 / 三层 definition_name
  pipeline.py            # prepare_session：波形文件入口(.fst 直读/.vcd 自动转)→网表→session 编排；.f filelist 解析
  convert.py             # vcd2fst 封装：并行能力探测 + 串行 fallback + FIFO 流式
  timeutil.py            # 时间字符串 <-> FST 时间单位换算
  sources/
    fst_source.py        # pylibfst：层次 / 信号 / 值 / 总线聚合
    rtl_source.py        # pyslang 网表加载 + 查询：连接/驱动/trace/文件/netlist_health
  netlist/
    slang_netlist.py     # pyslang elaboration → maps.json（drivers/fanin/loads/instance_tree）+ 自愈 + UVM 探测
    trace_engine.py      # 结构×时间 trace 引擎 + definition_name 解析(leaf/后缀/锚点)
    expr_eval.py         # 4 值(0/1/x/z) 分支条件求值
    name_infer.py        # 实例名→模块定义名命名推断（含 interface 守卫）
  cli/build_session.py   # wave-session：组装 session 目录 + 指纹
deploy/                  # 离线 bundle 构建 + 安装脚本（见 docs/RELEASE_BUNDLE.md）
examples/make_sample.py  # 生成样例
examples/verilator_quickstart/  # 开源 Verilator 开箱示例（--trace-fst 产真实 FST，无需 xrun）
tests/smoke_test.py      # 端到端冒烟测试
tests/test_definition_name.py  # definition_name 解析单测（锚点/后缀/interface 守卫）
LICENSE                  # MIT
licenses/THIRD_PARTY.md  # 第三方组件许可声明（mcp/pyslang/pylibfst；离线包内 vcd2fst）
```

---

## 开源协议

本项目以 **MIT** 许可发布（见 `LICENSE`）。

依赖与内置组件均为宽松许可，无 copyleft 传染：`mcp` / `pyslang` / `pylibfst` 皆为
MIT/BSD。离线包附带的 `vcd2fst` 转换器由 GTKWave 的 **MIT** 源码（libfst/fstapi +
vcd2fst helper）构建，作为独立进程调用（聚合关系，不影响 wave-mcp 的 MIT 许可）；
其内含的 `jrb` 组件为 LGPL-2.1，随包提供构建脚本以满足可重链接义务。详见
`licenses/THIRD_PARTY.md`。

---

## 待确认信息（影响阶段3/4 的可达成度）

1. **SV 语法复杂度**（generate / interface / package / UVM 使用程度）→ 决定 pyslang elaboration 成功率，影响 trace 类比例（UVM 环境靠自愈补 incdir/包 + UVM 目录探测；仍失败时 definition_name 走命名推断兜底）。
2. **trace_value/trace_x 是否刚需** → 决定是否投入阶段4，以及是否保留少量 Indago License 处理高精度根因。
3. **是否需要中心化部署（模式二 HTTP）**。
4. **典型波形规模**（文件大小/信号数/仿真时长）→ 用于性能压测设计。
