# wave-mcp 研发进度 & To-Do

> 开源、去 License、无并发上限的 xrun(Xcelium) 波形调试 MCP Server —— Indago/Verisium Debug MCP 的替代方案。
> 本文档维护**当前进度**与**未来研发 To-Do**。最后更新：2026-06-30。

---

## 1. 一句话现状

10 大类、41 个工具全部实现（对齐 Indago）；单一技术栈 **FST(pylibfst) + xrun.log + Surfer WCP + pyslang 网表**，无需 Surelog/UHDM/Verible。核心调试能力（会话/层次/信号/值/日志/波形/连接/驱动/trace）端到端跑通。**已在真实大型 RTL + 真实 Verilator FST 上验证**：用开源 Verilator 对 OpenTitan `tlul_adapter_host` / `tlul_socket_1n` / lowRISC `ibex_core`(17 模块/1993 信号) 生成 FST，端到端断言全 PASS——结构提取、字段级 driver、active_drivers 定位 RTL 源、trace_value 跨模块穿透(最深 4 层 `ibex_core→if_stage→prefetch_buffer→fetch_fifo`)且节点带真实波形值。验证套件见 `tests/verilator_fst/`。

---

## 2. 已完成（含对应代码）

### 数据源层 `wave_mcp/sources/`
- [x] `fst_source.py` — pylibfst 封装：层次(scope 树)、信号(位宽/方向/类型)、值查询（点查询 / 区间 / 全量，随机访问）。
- [x] `log_source.py` — xrun.log + UVM 报文解析（error/warning/关键词/按索引）。
- [x] `wcp_client.py` — Surfer WCP 客户端（增删信号、分组、zoom、marker、跳转），无 viewer 时优雅降级。
- [x] `rtl_source.py` — 基于 pyslang 网表 + FST：连接/驱动/扇入扇出/声明定位/文件查询 + active_drivers/trace 转发；无网表时结构化降级。

### 静态分析 + trace `wave_mcp/netlist/`
- [x] `slang_netlist.py` — pyslang 完整 elaboration，构建并持久化 `maps.json`：DriverMap/FanInMap/LoadMap/LocMap + instance_tree + 端口连接 + **分支守卫(guard)**。
  - [x] **generate 穿透**：`_process_generate` 递归 if-/for-/case-generate（跳过 `isUninstantiated` 分支），prim_fifo_sync 等的内部逻辑/子实例不再漏（OpenTitan 实测 driver 由 0→正常量级）。
  - [x] **实例端口驱动（方案1）**：子实例 output/inout 端口连接登记为外部信号的 `instance_port` driver（带 `port_ref`），纯连线模块（tlul_fifo_sync）driver 不再为 0，driver 语义与 Indago 对齐。
  - [x] **字段级 driver 建模**：新增 `_lvalue_paths`/`_driver_targets`，LHS（过程/连续/实例端口连接）保留 struct 成员与位选路径，packed-struct 端口按字段拆分 driver（`tl_h_o.a_ready <- reqfifo.wready_o`、`tl_d_o.d_ready <- rspfifo.wready_o`，含嵌套 `tl_d_o.a_user.cmd_intg`），不再过聚合到根信号。tlul_fifo_sync driven 4→25。
- [x] `trace_engine.py` — 结构维(网表) × 时间维(FST 值) 反向遍历；active_drivers / driver_contributors / trace_value / trace_x。
  - [x] **跨模块 trace**：trace 遇到 `instance_port` driver 时穿进子实例（`crosses_into`），从子实例内部端口网继续追，链路跨层次。OpenTitan 实测三层穿透 `tlul_fifo_sync → rspfifo → u_fifo_cnt` 直到寄存器边界。
  - [x] **字段级路径解析**：`resolve_path` 返回 (module, 真实实例路径, 字段 leaf, drivers)，修复字段路径被 `rsplit('.')` 误拆导致的子节点路径污染；支持字段精确查询 + struct 根查询聚合回退。
  - [x] **大型模块健壮性**：`_record_assignment`/`_record_assignment_continuous` 对 `InvalidExpression`(部分依赖/SVA 未完整 elaborate) 加类型防护；`_process_member` 加 per-member 兜底(`skipped_members` 计数)，单个成员失败不再中止整模块提取。修复前 ibex_core 因 InvalidExpression 直接崩溃，修复后正常提取 17 模块/233 driver/168 instance_port。
- [x] `expr_eval.py` — 4 值(0/1/x/z) 表达式求值器，用于**分支条件求值**精确判定活跃驱动；不支持的算子→x→回退启发式。

### 波形准备 / 编排
- [x] `convert.py` — VCD→FST（vcd2fst，speed/balanced/size 三档 + 并行 + FIFO 流式）。
- [x] `pipeline.py` — `run_simulation` / `prepare_session`（xrun→VCD→FST→解析日志→建网表→建会话）/ `build_manifest` / `build_netlist_maps`。

### 会话 / 服务 / CLI
- [x] `session.py` — Session/SessionManager、session.json 清单、**指纹一致性校验**、分层降级。
- [x] `server.py` — FastMCP，41 工具，stdio / streamable-http / sse，工具支持 `session_id` 多会话隔离。
- [x] `cli/build_session.py`（`wave-session`）、`cli/vcd2fst.py`（`wave-vcd2fst`）。
- [x] `examples/make_sample.py`、`tests/smoke_test.py`。

### 工程化（部分）
- [x] `prepare_session` 一站式入口；自动建网表；失败分级降级。
- [x] 指纹校验（源码/波形改动报警，绝不静默给错）。
- [x] 加密网离线打包（见 `deploy/` + `docs/DEPLOY_AIRGAP.md`）：自带独立 Python(3.11.15) + 离线 wheelhouse + stdio 无感启停，已端到端验证（含真实 MCP initialize 握手）。
- [x] `deploy/build_vcd2fst.sh`：manylinux_2_28 容器编出 glibc-portable `vcd2fst`（最高仅需 GLIBC_2.14），已验证可转换样例 VCD。

---

## 3. 工具覆盖度（对齐 Indago）

10 类全部实现，详见 `README.md` 的覆盖度表。要点：
- 1/2/3/4/7/8/9/10 类：完成且可靠。
- 5 类（连接/驱动）：静态精确；active_drivers 用分支条件求值（精确，回退启发式）。
- 6 类（trace）：trace_value 用分支求值选驱动（准确）；**trace_x 近似**。

---

## 4. To-Do（按优先级）

### P0 — 上线 / 移植 / 真实性验证
- [x] **真实 RTL 验证（结构类，OpenTitan 四批）**：`tests/opentitan_elab_check.py` 对 4 批 10 个真实模块跑 build_netlist + pyslang 自身 elaboration 做 ground-truth 端口/实例对账。10/10 elaborate 成功、端口与实例对账全一致。已修：generate 穿透、实例端口驱动、GT 实例口径（递归 generate）、字段级建模。
- [x] **真实大型波形验证（动态类，Verilator FST）**：`tests/verilator_fst/` —— 用开源 Verilator 生成真实 FST，对 `tlul_adapter_host` / `tlul_socket_1n` / `ibex_core` 跑 netlist+FST+trace 断言，全 PASS。验证了 active_drivers 定位 RTL 源、trace_value 跨模块穿透(最深 4 层)且节点带真实波形值。修复了 ibex 触发的 InvalidExpression 崩溃。
  - 待办：for-generate 重复 driver 计数核实；Verilator FST 默认不 dump 全部内部组合 wire，深层组合节点取值受限（trace 结构正确，止于可见边界）——如需更密的值标注可加 `--trace-structs`/显式 dump 或换 xrun。
- [ ] **Surfer WCP 真机对接**：按运行中的 Surfer 核对 WCP 命令字段（命令名/参数），实测增删信号/zoom/marker。
- [ ] **加密网部署演练**：在与目标机一致(glibc 2.28)的机器上跑通离线 bundle，校验启动/退出/多用户隔离。

### P1 — 精度 / 正确性
- [ ] **差分回归测试框架**：同一批 (signal, time) 对拍 Indago 输出，自动比对、沉淀不一致 case 为回归用例。一致率作为"对不对/够不够"的客观标准。
- [ ] **trace_x 提升**：位级 X 传播建模（处理 `& | ?:` 的 X-optimism），更准定位 X 源头。
- [ ] **表达式求值器扩展**：移位 `<< >>`、拼接 `{}`、三目 `?:`、归约 `& | ^`、`inside`、简单函数；扩大 active_driver 精确覆盖面。
- [x] **跨模块边界 trace**：已落地（实例 output 端口登记为 `instance_port` driver，trace 穿进子实例 `crosses_into`），并已精化到 struct 端口**字段级粒度**（`resolve_path` + `_lvalue_paths`）。
- [ ] **时间维 trace**：对寄存器(nonblocking)回溯到"上一个时钟沿"的赋值时刻，做真正的跨周期因果链（当前在寄存器处标注边界并停）。

### P2 — 性能 / 工程
- [ ] **纯 Python FST 写入后备**：用 pylibfst writer 实现 VCD→FST，去掉 vcd2fst 原生二进制依赖（加密网移植只剩 wheel）。
- [ ] **大 FST 压测** + LRU 缓存 + 句柄/索引复用；point-query 与区间查询基准。
- [ ] **netlist 增量重建**：按源码指纹缓存，仅变更模块重建。
- [ ] **HTTP 多 Session 完善**：会话生命周期、并发隔离、（可选）鉴权、压测。
- [ ] `get_scope_module_information` 补实例化代码片段；`get_signal_information` 补 enum/struct 展开。
- [ ] 单元测试覆盖（expr_eval 真值表、log 解析、time 换算、netlist 提取）。

---

## 5. 已知限制（如实）
- trace_x 为近似；active_driver 遇不支持表达式回退启发式（结果标 `selection_method`）。
- 跨模块 trace 暂止于模块端口边界；时间维跨周期回溯未做。
- 仅在 `examples/sample` 玩具 RTL 验证，真实 SV 未压过，可能有 bug。
- VCD→FST 依赖 `vcd2fst`（GTKWave）系统二进制；加密网用 `deploy/build_vcd2fst.sh` 产 glibc-portable 版随包带（已验证）。
- 静态结构(连接/驱动/扇入)置信度高；动态因果是"逼近"，非精确复原。

---

## 6. 技术约束备忘
- 目标(加密网)：x86_64、glibc **2.28**、多机器多用户 Python 版本不一 → 共享盘自带独立 Python 运行时解决。
- 依赖 wheel 的 glibc 下限：pyslang 2.27 / pylibfst 2.17（均满足 2.28）。
- 部署推荐 **stdio**：客户端起共享盘脚本即无感启动，进程随客户端退出，多用户进程级隔离，零运维。
