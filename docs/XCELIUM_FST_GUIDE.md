# Xcelium (xrun) 直接产出 FST 波形指南

wave-mcp 直读 FST。Xcelium 原生不支持 dump FST，常规做法是先 dump VCD 再用
`vcd2fst` 转换，但 VCD 中间文件体积大、转换耗时。本文介绍另一条路：通过开源
VPI 插件 [fstdumper](https://github.com/semify-eda/fstdumper) 让 xrun 在仿真时
**直接产出 FST**，零转换、免商用波形 license、文件体积小。

产出的 `.fst` 可直接传给 wave-mcp 的 `prepare_session`，也能用 GTKWave 打开。

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

VPI 是 IEEE 1800 标准接口。上游官方实测过 Xcelium 18.03 / 19.09，本文档
只覆盖 Xcelium 路径。

## 许可说明（重要）

fstdumper 采用 **GPL-3.0** 许可，与 wave-mcp（MIT）许可不同，因此**不随
wave-mcp 分发**，需要你自行从上游获取并编译。这不构成使用障碍：fstdumper
只在仿真时被 xrun 加载，与 wave-mcp 进程零链接，正常使用没有 GPL 义务问题。

## 第一步：获取并编译 fstdumper

依赖仅有 zlib（`-lz`）和 gcc/g++，一般环境都齐备。

```bash
git clone https://github.com/semify-eda/fstdumper.git
cd fstdumper
make fstdumper.so
```

产出 `fstdumper.so`。建议放到团队共享目录，编译一次全项目复用。

内网无法访问 GitHub 时，可通过内部镜像或离线包获取源码后同样 `make` 即可。

> 提示：默认 `CFLAGS` 带 `-DDEBUG`，仿真时会打印较多调试信息。介意的话
> 编译前把 Makefile 中的 `-DDEBUG` 去掉再 make。

## 第二步：编写 dump 控制模块

推荐做法是**不改动现有 testbench**，新增一个独立的 top 模块专管 dump：

```systemverilog
// fst_dump.sv
module fst_dump;

  string fst_file;

  initial begin
    if (!$value$plusargs("fstfile=%s", fst_file))
      fst_file = "waves.fst";

    $fstDumpfile(fst_file);

    // 注意：第二个参数必须是你的 testbench 顶层实例名！
    // 写错不会报错，但产出的 FST 是空的（只有文件头没有信号）。
    $fstDumpvars(0, top_tb);

    $fstDumpon;
  end

endmodule
```

两个关键点：

1. **`$fstDumpvars` 的 scope 参数必须与实际 tb 顶层实例名一致**（本例为
   `top_tb`）。这是最常见的翻车点：scope 对不上时仿真照常跑完、不报任何
   错误，但 FST 里没有任何信号。
2. 深度参数 `0` 表示递归 dump 全部层次。大型设计若只关心某个子系统，可以
   收窄 scope（如 `$fstDumpvars(0, top_tb.u_dut.u_uart)`）来控制文件体积，
   但注意 wave-mcp 的 trace / 驱动类工具需要波形层次与 RTL 对齐，建议
   dump 范围至少覆盖你要分析的完整子树。

## 第三步：xrun 命令行加载插件

在原有 xrun 命令上追加三处（其余选项一律不动）：

```bash
xrun -64bit \
  +access+r \
  -loadvpi /path/to/fstdumper.so:vlog_startup_routines_bootstrap \
  -f your_filelist.f \
  fst_dump.sv \
  -top top_tb -top fst_dump \
  +fstfile=waves.fst
```

逐项解释：

| 追加项 | 作用 |
| --- | --- |
| `+access+r` | 开放信号读权限，VPI 回调读值必需，没有它波形为空 |
| `-loadvpi ...so:vlog_startup_routines_bootstrap` | 加载插件并执行注册入口，冒号后的函数名固定写法 |
| `fst_dump.sv` + `-top fst_dump` | 编入 dump 控制模块，与原 top 并列 elaborate |
| `+fstfile=waves.fst` | 运行时指定输出文件名（可省，默认 `waves.fst`） |

集成到 Makefile 回归流时，可以仿照如下模式做成开关：

```makefile
ifeq ($(wave_tools),fst)
  XRUN_WAVE_OPTS = +access+r \
    -loadvpi $(FSTDUMPER_SO):vlog_startup_routines_bootstrap \
    $(TB_DIR)/fst_dump.sv -top fst_dump +fstfile=$(WAVE_OUT).fst
endif
```

## 第四步：产出的 FST 交给 wave-mcp

仿真结束后 `waves.fst` 直接可用，无任何转换步骤：

```
prepare_session(wave_path="waves.fst", filelist_path="your_filelist.f")
```

首次接入建议按
[SIMULATOR_COMPATIBILITY.md](SIMULATOR_COMPATIBILITY.md)
的「换到新仿真器时的验证步骤」做一次体检：看 `session_info` 的
`netlist_health.trust` 与 `definition_coverage.coverage_pct`，再挑一个
含 generate / 实例数组的信号试 `trace_value`，确认路径对齐正常。

## 排错速查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| FST 文件极小 / GTKWave 打开无信号 | `$fstDumpvars` 的 scope 与 tb 顶层实例名不符 | 核对实例名（区分大小写），改 `fst_dump.sv` |
| 波形全程无翻转 | 缺 `+access+r` | 命令行补上 |
| `-loadvpi` 报找不到符号 | 冒号后入口名写错 | 固定写 `vlog_startup_routines_bootstrap` |
| 加载 .so 失败 | 编译环境与运行环境 glibc / 位数不匹配 | 在与 xrun 相同的环境（`-64bit` 对应 64 位）重新 make |
| 仿真变慢明显 | 全层次 dump 开销 | 收窄 `$fstDumpvars` 的 scope 或深度 |
