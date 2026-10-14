# Xcelium (xrun) 直接产出 FST 波形指南

wave-mcp 直读 FST。Xcelium 原生不支持 dump FST，常规做法是先 dump VCD 再用
`vcd2fst` 转换，但 VCD 中间文件体积大、转换耗时。本文介绍另一条路：通过开源
VPI 插件 [fstdumper](https://github.com/semify-eda/fstdumper) 让 xrun 在仿真时
**直接产出 FST**，零转换、免商用波形 license、文件体积小。

产出的 `.fst` 可直接传给 wave-mcp 的 `prepare_session`，也能用 GTKWave 打开。

## 速查

```bash
# 1 取上游源码（GPL-3.0，wave-mcp 不随包、不代下载），钉在补丁对应的提交
git clone https://github.com/semify-eda/fstdumper.git /path/to/fstdumper
git -C /path/to/fstdumper checkout 3c75063

# 2 打补丁并编译（在跑 xrun 的同类机器上，一次就够，产物可给全组共用）
bash deploy/build_fstdumper.sh /path/to/fstdumper

# 3 验证（需要 xrun 和 GTKWave 的 fst2vcd），最后一行应为 [verify] PASS
bash deploy/verify_fstdumper.sh --so /path/to/fstdumper/fstdumper.so

# 4 在原有 xrun 命令上追加这几项，其余选项不动；路径用绝对路径
xrun -64bit -access +r \
  -loadvpi /path/to/fstdumper/fstdumper.so:vlog_startup_routines_bootstrap \
  -f your_filelist.f \
  /abs/path/to/wave-mcp/examples/xcelium_fst/fst_dump_cfg.sv \
  -top your_tb -top fst_dump \
  -define 'FST_DUMP_TOP=your_tb' -define 'FST_DUMP_FILE="waves.fst"'
```

产出的 `waves.fst` 直接给 `prepare_session(wave_path="waves.fst", filelist_path="your_filelist.f")`。

四个最容易翻车的点：
- 第二个 `-top fst_dump` 必须写，漏了不生成 FST（静默失败）；
- `-top fst_dump` 必须排在原 tb 的 `-top` **之后**：第一个 `-top` 给快照命名，也是 `-input` Tcl 脚本的默认 scope，排反了会让脚本里不带 scope 的 `probe -create -all` 报 `*SE,PRNONE`（见第四步）；
- `FST_DUMP_TOP` 必须是真实的 tb 顶层实例路径，写错则 FST 里没有信号；
- 输出文件名要么在运行时给 `+fst_wave_file=<path>`，要么编译时给 `FST_DUMP_FILE`，两个都没有就不 dump（日志里有一行 `FST dump off`）。

**版本兼容性**：已验证 Xcelium 24.09、25.09、26.05、26.08（glibc 2.28），
系统 gcc 8.5 与 GCC 13.2 编出的插件都可用。

## 原理

fstdumper 是一个 C 语言编写的 VPI 插件（改编自 Icarus Verilog 的 FST dumper，
底层使用 GTKWave 的 fstapi）。它在仿真器启动时注册一组系统任务：

| 系统任务 | 作用 | 对应的 VCD 原生任务 |
| --- | --- | --- |
| `$fstDumpfile("x.fst")` | 指定输出文件 | `$dumpfile` |
| `$fstDumpvars(depth, scope)` | 指定 dump 深度与范围 | `$dumpvars` |
| `$fstDumpon` / `$fstDumpoff` | 开 / 停记录 | `$dumpon` / `$dumpoff` |
| `$fstDumpall` | 强制记录当前全部值 | `$dumpall` |
| `$fstDumpflush` | 刷写到磁盘 | `$dumpflush` |
| `$fstDumplimit(n)` | 限制文件大小 | `$dumplimit` |

上游插件按 Icarus 的 VPI 写成，在 Xcelium 上拿不到 interface、generate 块、
SystemVerilog 类型等大量信号。wave-mcp 维护一个补丁让它在 Xcelium 上可用，见下文。

## 许可说明

fstdumper 采用 **GPL-3.0** 许可，与 wave-mcp（Apache-2.0）不同，因此**不随 wave-mcp
分发**，需要你自行获取并编译。插件只在仿真时被 xrun 加载，与 wave-mcp 进程没有
链接关系。

仓库 `third_party/fstdumper/fstdumper-xcelium.patch` 是针对上游源码的补丁，属于
GPL 代码的衍生作品，按 GPL-3.0 分发，**不适用** wave-mcp 的 Apache-2.0 许可。构建、验证
脚本和 dump 控制模块是 wave-mcp 自己的文件（Apache-2.0）。详见 [licenses/fstdumper-patches.NOTICE](licenses/fstdumper-patches.NOTICE)，许可全文在同目录。

## 第一步：获取源码并构建

依赖：gcc、g++、make、patch、zlib 头文件，不需要网络。

```bash
git clone https://github.com/semify-eda/fstdumper.git /path/to/fstdumper
git -C /path/to/fstdumper checkout 3c75063
bash deploy/build_fstdumper.sh /path/to/fstdumper    # 产出 /path/to/fstdumper/fstdumper.so
```

- 源码目录也可以用环境变量 `FSTDUMPER_SRC_DIR` 传入。
- 脚本可以重复执行：补丁已打会跳过，只重新编译。
- 默认不带上游 Makefile 的 `-DDEBUG`（否则仿真时刷屏）；排查插件问题时用 `DEBUG_BUILD=1`。
- 补丁只能打在干净的 3c75063 上。用旧版 wave-mcp 打过补丁的源码树会被拒绝，换一份干净源码即可。
- 必须在跑 xrun 的同类机器上编译：插件只依赖 `libz.so.1` 和 `libc.so.6`，glibc 不匹配会加载失败。
- 产出的 `fstdumper.so` 建议放到团队共享目录，编一次全组复用。把 `.so` 给别人时，连同打过补丁的源码和许可证一起给。

**隔离网**：在有网机器上 clone 并 checkout，把源码目录整体拷到隔离网，其余步骤不变。
离线 bundle 已自带构建脚本、验证脚本、补丁和 dump 控制模块
（`deploy/`、`third_party/fstdumper/`、`examples/xcelium_fst/`）。

**补丁做了什么**（只影响 Xcelium，Icarus 构建保持上游行为）：

- 层次：遍历 interface；通过 `vpiInternalScope` 拿到 generate 块、实例数组、named begin / fork、静态 task / function 局部变量、clocking block。
- 类型：enum、packed struct / union、packed 数组、`bit` 型 net、`integer` net、clocking block 信号。
- 数据：每个信号只声明一次；端口合并后的同一根网线写成 FST 别名，只注册一个回调；去掉值未变化的冗余跳变；`$finish` 同一时刻的最后一次跳变不丢。
- 名字：转义名和带 `$` 的名字按 RTL 写；按声明写范围（`[0:3]`、`[8:1]`）；写端口方向和 interface 定义名，wave-mcp 的 `list_signals signal_type=Port`、`find_instances` 因此可用。
- 稳定性：跳过加密 IP 内的对象和没有读权限的对象（否则会崩溃或刷 VPI 报错）；FST 表示不了的对象每次 `$dumpvars` 汇总成一行 `FST info`，不逐条打 warning。

## 第二步：验证

```bash
bash deploy/verify_fstdumper.sh --so /path/to/fstdumper/fstdumper.so
```

脚本编译 `deploy/fstdumper_verify/vpi_coverage_tb.sv`（覆盖上面列出的层次和类型），
在**同一次仿真**里同时用 fstdumper 和 Xcelium 原生 VCD dumper 记录，再逐信号、逐跳变
比对。另外检查日志里没有 VPI 报错、FST 里没有重复声明、端口合并的网线写成了别名。

- 通过时最后一行是 `[verify] PASS: 93 signals identical (717 transitions), 16 parameters not dumped, 9 extra in FST`。16 个是 genvar 和 localparam（fstdumper 不 dump 常量），9 个是静态 task 局部变量（Xcelium 自带的 VCD dumper 不输出）。
- 产物默认写到 `$TMPDIR` 下的新目录，`--out DIR` 可指定；`--` 之后的参数原样传给 xrun。
- 没打补丁或用旧补丁编的 `.so` 会 `FAIL`，并打印原因。

## 第三步：dump 控制模块

推荐**不改动现有 testbench**，把 [`examples/xcelium_fst/fst_dump_cfg.sv`](../examples/xcelium_fst/fst_dump_cfg.sv)
作为第二个 top 一起编译。它在 `initial` 里调用 `$fstDumpfile` / `$fstDumpvars`，
一份文件服务所有用例。

输出文件按顺序取第一个有的：

1. 运行时 `+fst_wave_file=<path>`（适合 Makefile 按用例名拼路径）；
2. 编译时 `-define 'FST_DUMP_FILE="<path>"'`；
3. 都没有：不 dump，只打印一行。用这个模块编过的快照，在不要波形的运行里没有额外开销。

编译期选项：

| 宏 | 默认 | 说明 |
| --- | --- | --- |
| `FST_DUMP_TOP` | `top_tb` | dump 的 scope，必须与实际实例路径一致（区分大小写），也可以是某个子模块或 interface 实例 |
| `FST_DUMP_LEVEL` | `0` | dump 深度，0 为不限 |
| `FST_DUMP_FILE` | 无 | 输出文件名，见上 |

- `-define` 与 `+define+` 都可以，但必须是**编译**选项；改了宏要重新编译。
- 文件保持纯 ASCII：中文注释会让 xmvlog 刷 `*W,NONPRT` 警告。

## 第四步：xrun 命令行

| 追加项 | 作用 |
| --- | --- |
| `-access +r` | 开放信号读权限，VPI 读值必需，没有它波形全程不翻转；已有更大权限时重复无害 |
| `-loadvpi <so>:vlog_startup_routines_bootstrap` | 加载插件并执行注册入口，冒号后的函数名固定 |
| `fst_dump_cfg.sv` + 第二个 `-top fst_dump` | 编入 dump 控制模块，与原 top 并列 elaborate |
| `-define FST_DUMP_*` | 可选，见上表 |

注意：
- xrun 只要出现任何 `-top`，就不再自动推断顶层，所以原来的 tb 顶层也要显式写 `-top`。
- **`-top fst_dump` 必须排在原 tb 的 `-top` 之后**。第一个 `-top` 决定快照名（`Loading snapshot worklib.<第一个 top>:sv`），也是 `-input` Tcl 脚本的默认 scope。`fst_dump` 排在前面时，同一个快照再用别的波形工具跑，脚本里不带 scope 的 `probe -create -shm -all` 只会 probe `fst_dump`，报 `*SE,PRNONE: No new objects to probe in scope fst_dump` / `*SE,PRBFAIL`，xrun 返回非 0（Xcelium 24.09 / 25.09 / 26.05 / 26.08 均如此）。

集成到 Makefile 回归流时，可以做成开关，默认不挂，需要波形的用例再打开。`-top fst_dump` 要追加在原 `-top <tb>` 那一行**之后**（Makefile 里 `+=` 按文件中出现的先后拼接；原 `-top` 写在文件后段时，把 `-top fst_dump` 单独放到它后面）：

```makefile
ifeq ($(wave_tools),fst)
  # compile
  CMP_OPTS += -access +r \
    -loadvpi $(FSTDUMPER_SO):vlog_startup_routines_bootstrap \
    $(FST_DUMP_SV) $(FST_DUMP_DEFINES)
  # run
  RUN_OPTS += +fst_wave_file=$(WAVE_OUT).fst
endif

# ... later, right after the line that adds -top <your_tb> ...
ifeq ($(wave_tools),fst)
  CMP_OPTS += -top fst_dump
endif
```

确认顺序：`make -n <编译目标> wave_tools=fst | tr ' ' '\n' | grep -A1 '^-top$'`，应先出 tb 顶层、后出 `fst_dump`。

编译期的 `-loadvpi` 会记录进快照，运行期不必再加。跳过编译、复用快照的流程里，
切换波形开关或改了 `FST_DUMP_*` 之后要先重编。

## 第五步：交给 wave-mcp

```
prepare_session(wave_path="waves.fst", filelist_path="your_filelist.f")
```

- 只给 FST：层次、信号列表、取值、`signal_activity`、`find_time_windows`、`sample_at_clock`、`diff_waveforms` 都可用；端口带方向，interface 实例带定义名。
- 再给 RTL filelist：多出驱动、扇入、`trace_value`、`trace_x` 等结构化能力。FST 里的路径与 RTL 网表一致，可以直接拿 FST 路径查询。
- 千万信号量级的 FST 建会话可能要一分钟以上，超过 MCP 客户端的默认请求超时。会话会在后台照常建好，用 `session_info(list_sessions=true)` 找到它继续用。

## 能 dump 的与拿不到的

**能 dump**：module / interface（含实例数组）、generate 块（具名、`genblkN`、for / if、嵌套）、
named begin / fork、静态 task / function 局部变量、clocking block 信号；
`logic` / `reg` / `wire` / `bit` / `byte` / `shortint` / `int` / `longint` / `integer` / `time` / `real` / `event`、
enum（显示数值）、packed struct / union / 多维 packed 数组（整体向量，不拆字段）、`bit` 型 net、`integer` net。

**拿不到**：

1. parameter / localparam / genvar：fstdumper 不 dump 常量。
2. automatic 变量：只在调用期间存在，Xcelium 不允许挂值变化回调。
3. FST 表示不了的对象：unpacked 数组（含 RAM 模型的存储体）、动态数组 / 队列、string、class 句柄、unpacked struct / union。
4. 加密 IP（`pragma protect`）内部的对象，仿真器不给名字，原生 VCD 同样不输出。
5. 没有读权限的对象，例如预编译 VIP 的 interface，`-access +r` 管不到。
6. 混合语言设计里的 VHDL 部分：fstdumper 只遍历 Verilog / SV 层次。

第 2 到 5 类在每次 `$dumpvars` 后汇总成一行 `FST info: $dumpvars: not dumped ...`，
不影响仿真结果。需要看这些对象时用 SimVision / Indago。

**同一时刻内的零宽毛刺只保留最终值**：fstdumper 在每个仿真时刻末尾读一次值，原生
VCD 会记下同一时刻内的每个中间值。查竞争、毛刺类问题时用原生波形。

**event 的写法**：named event 每次触发在 FST 里记为 `1`，原生 VCD 记为 `x`，触发
时刻一致。

**原生 VCD 也会漏记**：拼接式连续赋值 `assign {a, b} = bus;` 驱动的变量，以及
`assign #d` 驱动的 interface 变量，实测原生 VCD 从头到尾不变，fstdumper 记下的值与
RTL 一致。两边对不上时不要默认原生 VCD 是对的。

## 性能

开销主要来自每个信号一个值变化回调。补丁把端口合并后的同一根网线合成一个回调，
一个约 1 万信号的设计，仿真 CPU 时间：不开波形 210 s，逐端口注册 445 s（+112%），
合并后 237 s（+12%）。更大的设计可以用 `FST_DUMP_TOP` / `FST_DUMP_LEVEL` 收窄范围。

**直出的最大价值在超大设计**：VCD 超过 10 GB 后 `vcd2fst` 会长时间转换直至超时
（18.4 GB 与 55.8 GB 的 VCD 均在 3 小时超时），直出不经过 VCD，没有这个上限。

## 排错速查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 日志有 `FST dump off`，没有 FST | 运行时没给 `+fst_wave_file=`，编译时也没给 `FST_DUMP_FILE` | 补上其中一个 |
| FST 没生成，日志里也没有 `[fst_dump]` | 漏了第二个 `-top fst_dump` | 补上 |
| tb 没跑，只 elaborate 了 `fst_dump` | 原 tb 顶层没有显式 `-top` | 显式写原顶层的 `-top` |
| fst 编过的快照换别的波形工具跑，报 `*SE,PRNONE ... scope fst_dump`；日志是 `Loading snapshot worklib.fst_dump:sv` | `-top fst_dump` 排在了 tb 顶层前面 | 把 `-top fst_dump` 挪到原 `-top` 之后，重编 |
| 编译日志 `*W,LIBLDFL` / `*W,MISSYST $fstDumpfile` | `.so` 路径错或没编 | 检查 `-loadvpi` 路径，重新构建 |
| FST 里没有信号 | `FST_DUMP_TOP` 与实例路径不符 | 核对实例名，区分大小写 |
| 波形全程不翻转 | 缺 `-access +r` | 补上 |
| `FST_DUMP_*` 不生效 | 放进了运行期参数，或复用快照没重编 | 放进编译选项并重编 |
| 日志刷 `FST warning: Unsupported argument type`、缺 generate / interface 信号 | 用的是未打补丁的上游插件 | 用 `build_fstdumper.sh` 重编 |
| `build_fstdumper.sh` 报 cannot apply | 源码不是干净的 3c75063 | 换一份干净源码 |
| 加载 `.so` 失败 | 编译与运行环境 glibc / 位数不一致 | 在跑 xrun 的机器上重编 |
| 刷 `*W,NONPRT` | dump 控制文件含非 ASCII 字符 | 改成纯 ASCII |
| 仿真明显变慢 | 全层次 dump 注册回调 | 收窄 `FST_DUMP_TOP` / `FST_DUMP_LEVEL` |
| 某个对象看不到 | 属于“拿不到”的几类 | 看日志 `FST info: ... not dumped` 行 |
