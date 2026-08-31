# Xcelium (xrun) 直接产出 FST 波形指南

wave-mcp 直读 FST。Xcelium 原生不支持 dump FST，常规做法是先 dump VCD 再用
`vcd2fst` 转换，但 VCD 中间文件体积大、转换耗时。本文介绍另一条路：通过开源
VPI 插件 [fstdumper](https://github.com/semify-eda/fstdumper) 让 xrun 在仿真时
**直接产出 FST**，零转换、免商用波形 license、文件体积小。

产出的 `.fst` 可直接传给 wave-mcp 的 `prepare_session`，也能用 GTKWave 打开。

> **实测状态**：本流程已在 Xcelium `xrun(64) 25.09-a081`、glibc 2.28、
> gcc 8.5.0 上完成端到端验证（加载、dump、wave-mcp 全链路、逐跳变值比对）。
> 上游官方还实测过 Xcelium 18.03 / 19.09 与 Icarus Verilog 11。
> fstdumper 不是通用替代，接入前请读[适用范围与已知限制](#适用范围与已知限制)。

## 原理

fstdumper 是一个 C 语言编写的 VPI 插件（改编自 Icarus Verilog 的 FST dumper，
底层使用 GTKWave 的 fstapi）。它在仿真器启动时注册一组自定义系统任务：

| 系统任务 | 作用 | 对应的 VCD 原生任务 |
| --- | --- | --- |
| `$fstDumpfile("x.fst")` | 指定输出文件 | `$dumpfile` |
| `$fstDumpvars(depth, scope)` | 指定 dump 深度与范围 | `$dumpvars` |
| `$fstDumpon` / `$fstDumpoff` | 开 / 停记录 | `$dumpon` / `$dumpoff` |
| `$fstDumpall` | 强制记录当前全部值 | `$dumpall` |
| `$fstDumpflush` | 刷写到磁盘 | `$dumpflush` |
| `$fstDumplimit(n)` | 限制文件大小 | `$dumplimit` |

VPI 是 IEEE 1800 标准接口，插件与 wave-mcp 无任何代码关联，互不影响各自的
许可义务（fstdumper 为 GPL-3.0，wave-mcp 为 MIT，见下文许可说明）。

## 许可说明（重要）

fstdumper 采用 **GPL-3.0** 许可，与 wave-mcp（MIT）许可不同，因此**不随
wave-mcp 分发**，需要你自行从上游获取并编译。这不构成使用障碍：fstdumper
只在仿真时被 xrun 加载，与 wave-mcp 进程零链接，正常使用没有 GPL 义务问题。

wave-mcp 仓库 `third_party/fstdumper/` 提供一套针对 Xcelium 的修复补丁，
补丁是 GPL-3.0 代码的衍生作品，同样按 GPL-3.0 分发，**不适用** wave-mcp
的 MIT 许可（详见 [THIRD_PARTY.md](THIRD_PARTY.md)）。

## 第一步：获取源码，应用补丁并编译

依赖仅有 zlib（`-lz`）和 gcc，一般环境都齐备。

```bash
git clone https://github.com/semify-eda/fstdumper.git
cd fstdumper

# 应用 Xcelium 修复补丁（强烈建议，未打补丁会丢 interface 信号、
# 产生冗余跳变、$finish 同刻最后一次跳变丢失）
patch -p1 < /path/to/fstdumper-xcelium-fixes.patch

make fstdumper.so
```

补丁来源：本仓库 `third_party/fstdumper/fstdumper-xcelium-fixes.patch`，
全部改动已在 Xcelium 25.09 上回归验证（106/106 信号逐跳变与基线严格一致、
VPI 报错清零）。修了什么：

1. interface 完全不 dump（`types[]` / 类型映射 / scope switch 三处缺
   `vpiInterface`），丢所有 interface 信号；
2. "值未变化"的冗余跳变记录（Xcelium 会对未变化的值也触发回调）；
3. `$finish` 同一时刻的最后一次跳变丢失；
4. `show_this_item_x()` 缓冲区少分配 1 字节的越界写；
5. 对 named event 调 `vpi_get_value` 触发 `VPI NOVALOB` 报错。

产出 `fstdumper.so`。建议放到团队共享目录，编译一次全项目复用。注意在与
xrun 相同的环境编译（`-64bit` 对应 64 位，glibc 版本过低的老机器需就地编译，
插件只依赖 `libz.so.1` 与 `libc.so.6`）。

## 第二步：编写 dump 控制模块

推荐做法是**不改动现有 testbench**，新增一个独立的 top 模块专管 dump。
用编译期宏做参数化，一份文件服务所有 case：

```systemverilog
// fst_dump_cfg.sv
`ifndef FST_DUMP_TOP
`define FST_DUMP_TOP top_tb          // 默认：整个 tb
`endif
`ifndef FST_DUMP_LEVEL
`define FST_DUMP_LEVEL 0             // 默认：0 = 无限深度
`endif
`ifndef FST_DUMP_FILE
`define FST_DUMP_FILE "waves.fst"
`endif

module fst_dump;
  initial begin
    $fstDumpfile(`FST_DUMP_FILE);
    $fstDumpvars(`FST_DUMP_LEVEL, `FST_DUMP_TOP);
  end
endmodule
```

三个实测发现，直接决定上面模板的写法：

1. **`$fstDumpvars` 的 scope 参数必须与实际 tb 顶层实例名一致**（本例为
   `top_tb`）。这是最常见的翻车点：scope 对不上时仿真照常跑完、不报任何
   错误，但 FST 里没有任何信号。
2. **不要用 `+fstfile=` plusarg 指定文件名**。源码不支持 plusarg 解析，
   `$value$plusargs` 在 Xcelium 上也实测不可靠（读不到值），写了无效。
   文件名用宏（`-define FST_DUMP_FILE=...`）或直接改模板默认值。
3. **文件必须纯 ASCII**。中文注释会被 xmvlog 报 `*W,NONPRT` 警告，一条
   中文注释能刷出几十条。

## 第三步：xrun 命令行加载插件

在原有 xrun 命令上追加四处（其余选项一律不动）：

```bash
xrun -64bit \
  +access+r \
  -loadvpi /path/to/fstdumper.so:vlog_startup_routines_bootstrap \
  -f your_filelist.f \
  /path/to/fst_dump_cfg.sv \
  -top top_tb -top fst_dump \
  -define 'FST_DUMP_FILE="waves.fst"'
```

逐项解释：

| 追加项 | 作用 |
| --- | --- |
| `+access+r` | 开放信号读权限，VPI 回调读值必需，没有它波形为空 |
| `-loadvpi ...so:vlog_startup_routines_bootstrap` | 加载插件并执行注册入口，冒号后的函数名固定写法 |
| `fst_dump_cfg.sv` + 第二个 `-top fst_dump` | 编入 dump 控制模块，与原 top 并列 elaborate |
| `-define FST_DUMP_FILE=...` | 指定输出文件名（可省，默认 `waves.fst`） |

**必须追加第二个 `-top fst_dump`**，否则 `fst_dump` 模块不会被 elaborate，
FST 不会生成，且不报任何错误（静默失败，最坑的一种）。

命令行控制 dump 范围（三个维度均实测生效）：

```bash
# 默认：整个 top_tb，全深度
... -top top_tb -top fst_dump fst_dump_cfg.sv

# 只 dump DUT 子树
... -define FST_DUMP_TOP=top_tb.u_eci2apb

# 只 dump 10 层（大模块提速用）
... -define FST_DUMP_TOP=top_tb.u_clkgen -define FST_DUMP_LEVEL=10
```

> 注意传宏用 `-define`，不要用 `+define+`：`+define+` 在 elaboration 阶段
> 才生效，`fst_dump_cfg.sv` 编译时看不到，`ifndef` 恒为真，宏不会生效。

集成到 Makefile 回归流时，可以仿照如下模式做成开关（默认不挂，回归零开销，
需要波形的 case 再加）：

```makefile
ifeq ($(wave_tools),fst)
  XRUN_WAVE_OPTS = +access+r \
    -loadvpi $(FSTDUMPER_SO):vlog_startup_routines_bootstrap \
    $(CFG_DIR)/fst_dump_cfg.sv -top fst_dump \
    -define 'FST_DUMP_FILE="$(WAVE_OUT).fst"'
endif
```

## 第四步：产出的 FST 交给 wave-mcp

仿真结束后 `waves.fst` 直接可用，无任何转换步骤：

```
prepare_session(wave_path="waves.fst", filelist_path="your_filelist.f")
```

两个体检指标的实测口径（详见 [SIMULATOR_COMPATIBILITY.md](SIMULATOR_COMPATIBILITY.md)）：

- `netlist_health.trust` 只由 filelist 决定，与用哪种方式产波形无关；
  只给 `rtl.f` 时 `partial` 是正常现象，两条路径表现完全一致。
- `definition_coverage.coverage_pct` 的分母是 FST 里 module 类 scope 的数量，
  **不同产波形方式之间不可横向比较**。判断直出 FST 的好坏，看信号覆盖
  （共同信号数 / 基线信号数）与 `trace_value` 能否展开，而不是这个百分比。

## 适用范围与已知限制

**直出的最大价值在超大设计**：VCD 超 10 GB 后 `vcd2fst` 会长时间转换直至
超时失败（实测 18.4 GB 与 55.8 GB 的 VCD 均在 3 小时超时），而直出不经过
VCD，没有这个上限。某个 55.8 GB VCD 的模块，基线路径合计超 4.5 小时且拿不到
可用 FST，直出端到端 2.5 小时一步到位，磁盘占用 4.3 GB。

**代价是层次覆盖不全**。Xcelium 25.09 的 VPI 只实现了最小子集，
`vpiGenScope` / `vpiTask` / `vpiFunction` / `vpiNamedBegin` / `vpiNamedFork`
的子 scope 遍历全部不支持，这是改不了的硬限制。后果：

1. **generate block 下的一切信号拿不到**（最大的坑）。DUT 主体若包在
   `generate if (CFG) begin : GEN_XXX` 里，里面所有信号都丢。实测一个
   89% 信号在 generate 下的模块，覆盖率只剩 4%；反之 generate 占比 0.01%
   的模块覆盖 99.9%。
2. **task / function 局部量拿不到**。
3. interface 本体的信号拿得到（需打补丁），但 interface 内的
   modport / clocking block 子 scope 拿不到。

实测跨度：不同设计覆盖率在 0.03% ~ 99.95% 之间，取决于 DUT 主体有没有被
generate 包住、task 局部量占比多少。接入前可以用一个只跑到 time 0 就
`$finish` 的探针模块（`$fstDumpvars` 在 time 0 就完成整个层次遍历）秒级
预检能拿到多少信号，不用跑完仿真。

**性能注意**：全量注册回调在大设计上会显著拖慢仿真（实测一个 7.7 万信号的
模块从 17 分钟拖到 2.5 小时）。想提速只能收窄 `$fstDumpvars` 的 scope 或
深度（`FST_DUMP_LEVEL`），或用 `$fstDumpon/$fstDumpoff` 限时间窗。

**选择建议**：

| 场景 | 建议 |
| --- | --- |
| 模块 VCD 超 10 GB | 必须用直出，基线转换必然失败 |
| DUT 主体不在 generate 里 | 直出，省 VCD 磁盘和转换时间 |
| 需要看 generate / task 内部中间值 | 保留 VCD 路径 |
| 回归跑批 | 默认不挂 `fst_dump`，零开销；需要波形时再加 |

## 排错速查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| FST 根本没生成，且不报错 | 忘了加第二个 `-top fst_dump` | 补上，与原 top 并列 elaborate |
| FST 文件极小 / GTKWave 打开无信号 | `$fstDumpvars` 的 scope 与 tb 顶层实例名不符 | 核对实例名（区分大小写），改宏 `FST_DUMP_TOP` |
| `+fstfile=` 写了没效果 | 源码不支持 plusarg 解析 | 用 `-define FST_DUMP_FILE=...` |
| `-define` 配了但没生效 | 用成了 `+define+` | 改用 `-define` |
| 波形全程无翻转 | 缺 `+access+r` | 命令行补上 |
| `-loadvpi` 报找不到符号 | 冒号后入口名写错 | 固定写 `vlog_startup_routines_bootstrap` |
| 加载 .so 失败 | 编译环境与运行环境 glibc / 位数不匹配 | 在与 xrun 相同的环境重新 make |
| 日志刷 `VPI CANTIT / not supported` | generate / task 子 scope 遍历不支持 | Xcelium 硬限制，评估该设计是否适合直出 |
| 中文注释刷 `*W,NONPRT` 警告 | 文件含非 ASCII 字符 | dump 控制文件改纯 ASCII |
| 仿真变慢明显 | 全层次 dump 注册回调开销 | 收窄 `FST_DUMP_TOP` / 限 `FST_DUMP_LEVEL` |
