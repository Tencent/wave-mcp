# FSDB 波形接入指南（fsdb2fst）

wave-mcp 直读 FST，不读 FSDB。FSDB 是 Synopsys 的闭源格式，读取详细数据绕不开
Verdi 的 FsdbReader 运行库。本文介绍自带的 `fsdb2fst` 单程转换器：直接用
FsdbReader 读 FSDB、用 fstapi 写 FST，**不经过 VCD 中间文件**，产物与原生 FST
完全一致，查询工具零改动。

**FsdbReader 运行库（`libnffr.so` + `libnsys.so`）运行时不 checkout 任何
license**，与打开 Verdi GUI 不同。唯一的前提是环境里得有这两个 `.so`。

## 三行速查

在 MCP 配置里给出 `VERDI_HOME`，然后把 `.fsdb` 丢给 `prepare_session`，没有别的步骤：

```json
{
  "mcpServers": {
    "wave-mcp": {
      "command": "python",
      "args": ["-m", "wave_mcp.server"],
      "env": { "VERDI_HOME": "/tools/synopsys/verdi/T-2022.06-SP1" }
    }
  }
}
```

`VERDI_HOME` 填 Verdi 的**安装根目录**，不是 `verdi` 可执行文件所在的 `bin/`。
填对的判据只有一条，跑这行能列出东西就对了：

```bash
ls $VERDI_HOME/share/FsdbReader/linux64      # 应看到 libnffr.so、libnsys.so
```

`which verdi` 给出的是 `<根目录>/bin/verdi`，去掉末尾的 `/bin/verdi` 即为该填的值。
老版本安装里这个变量叫 `NOVAS_HOME`，两者含义相同，都设时优先用 `VERDI_HOME`。

```
prepare_session(wave_path="dump.fsdb", filelist_path="your_filelist.f")
```

首次转换时 wave-mcp 会自动编一次 `fsdb2fst`（需要 `g++`，约十几秒），之后直接复用。
升级 wave-mcp 后若转换器源码有变动，下次转换会自动重编，不需要手动处理。

> 需要 `wave-mcp>=0.2.6`：更早的版本没把转换器源码打进包，自动编译这一步会跳过。

`fsdb_scopes=["u_core"]` 收窄待加载信号；已选信号数超过默认 500 万内存阈值时会拒绝，
用它缩小范围即可。规模本身不再拒绝文件，详见下文规模说明。
遇到问题看[排错速查](#排错速查)；想手工构建或用命令行看[手工构建](#手工构建备选)与
[命令行用法](#命令行用法)。

### 两处缓存，别混淆

这条路径上有两个独立的缓存，排错时先分清是哪一个：

| 缓存的东西 | 位置 | 失效条件 |
| --- | --- | --- |
| **转换器二进制**（`fsdb2fst`） | `~/.cache/wave-mcp/fsdb2fst/<key>/` | 换 Verdi 路径或改转换器源码则重编 |
| **转换产物**（`.fst` + `.fst.hier`） | 默认落在 `.fsdb` 旁，目录不可写时回退 session 目录 | 源波形 mtime/size 变化，或切片参数变化则重转 |

前者让你只编一次转换器，后者让同一份波形反复建 session 只转一次。

## 许可与合规边界

`fsdb2fst.cpp` 是自研代码，**MIT**，随 wave-mcp 分发。它链接的 FsdbReader
运行库受 Synopsys EULA 约束：

- `libnffr.so` / `libnsys.so` 不随仓库和 PyPI 分发，只在运行时探测本机的
  `VERDI_HOME`。
- 编译产物 `fsdb2fst` 二进制是本机构建产物，不入库、不打进离线包：自动构建落在
  用户缓存目录，手工构建默认落在 `third_party/fsdb2fst/`（已被 `.gitignore` 排除）。
- 内附的 fstapi / lz4 / fastlz 来自 GTKWave（MIT），详见 [THIRD_PARTY.md](THIRD_PARTY.md)。

## 转换器怎么被找到

`fsdb2fst` 按五级顺序解析，第一个命中的生效。排错时对照这个顺序就知道当前用的是哪一份：

| 顺序 | 来源 | 说明 |
| --- | --- | --- |
| 1 | `$FSDB2FST_BIN` | 显式指定一个现成二进制。**指向的路径不可用时直接报错，不会静默回退** |
| 2 | repo-local `third_party/fsdb2fst/fsdb2fst` | 手工构建的默认落点 |
| 3 | 用户缓存 `~/.cache/wave-mcp/fsdb2fst/<key>/fsdb2fst` | 自动构建的落点 |
| 4 | `PATH` | 系统里已装的 `fsdb2fst` |
| 5 | **按需构建** | 以上都没有且能探测到 FsdbReader 时，自动编一次到用户缓存 |

自动构建需要三个条件同时满足：探测到 FsdbReader 运行库、有 `g++`、能找到转换器源码。
任一不满足时，报错会指名**具体缺哪一样**，不会只说"找不到"。

转换器源码有两种布局，按顺序解析，git checkout 优先，这样本地改动不会被旧的安装副本遮蔽：

| 布局 | 源码位置 | 构建脚本 |
| --- | --- | --- |
| git checkout | `third_party/fsdb2fst/` | `deploy/build_fsdb2fst.sh` |
| pip 安装 | `<prefix>/share/wave-mcp/fsdb2fst/` | `<prefix>/share/wave-mcp/deploy/build_fsdb2fst.sh` |

`<prefix>` 是 Python 环境前缀（虚拟环境里就是venv 目录）。报错信息会直接给出当前布局下
构建脚本的绝对路径，不用自己拼。

> **0.2.6 之前的版本装不了 FSDB**：转换器源码从未打进 wheel 和 sdist，自动构建这一级
> 因为没有源码可编而静默跳过，FSDB 功能实际只在 git checkout 下可用。0.2.0 到 0.2.5
> 六个版本都是这样。升级到 0.2.6 或更新版本即可。

关掉自动构建用 `WAVE_MCP_FSDB2FST_AUTOBUILD=0`。

**升级 wave-mcp 后二进制会自动重建**：缓存键包含 `fsdb2fst.cpp` 和 `fst/fstapi.c` 的
修改时间与大小，所以升级带来的转换器改动会让缓存键改变，下次转 FSDB 时自动重编一次
（约十几秒），之后继续复用。不需要手动删缓存，也不需要手动重建。

**自动构建不写仓库**：产物直接编到用户缓存，`third_party/` 全程不被写入。这样共享
checkout 或只读 checkout 都成立，也不会让 git 工作区出现构建产物。实现上靠构建脚本的
`FSDB2FST_OUT` 环境变量指定输出路径。

FsdbReader 运行库本身也按四级顺序探测：

1. repo-local `third_party/verdi_runtime/linux64/libnffr.so`
2. `$FSDB2FST_FREADER`（显式指定一份拷贝出来的 `share/FsdbReader` 目录）
3. `$VERDI_HOME/share/FsdbReader`
4. `$NOVAS_HOME/share/FsdbReader`（老版本安装）

注意路径是 `share/FsdbReader/`，**不是** `share/PLI/`（后者放的是 VPI dumper 库）。

## 手工构建（备选）

多数情况不需要这节：设好 `VERDI_HOME` 后首次转换会自动完成编译。下面适用于想显式构建、
要把二进制拷到别处复用、或自动构建失败需要排查的场景。

```bash
export VERDI_HOME=/path/to/verdi     # 必须含 share/FsdbReader/linux64

# git checkout
bash deploy/build_fsdb2fst.sh        # 默认产出 third_party/fsdb2fst/fsdb2fst

# pip 安装（路径以报错信息里给出的为准）
bash "$(python3 -c 'import sys,os; print(os.path.join(sys.prefix,"share","wave-mcp","deploy","build_fsdb2fst.sh"))')"

# 想换个落点（自动构建走的就是这条路）
FSDB2FST_OUT=/somewhere/fsdb2fst bash deploy/build_fsdb2fst.sh
```

**离线包用户**：解包后源码在 `fsdb2fst-src/`，构建脚本在 `fsdb2fst-src/deploy/`：

```bash
cd wave-mcp-bundle-*/fsdb2fst-src
VERDI_HOME=/path/to/verdi bash deploy/build_fsdb2fst.sh
```

**本机没有 Verdi 也能编译**：把整个 `share/FsdbReader/` 目录（头文件 + 两个
`.so`）拷过来，`export FSDB2FST_FREADER=<该目录>` 即可。

运行期不依赖 `LD_LIBRARY_PATH`：构建脚本把 RPATH（含 `$ORIGIN`）烘进二进制，
把两个 `.so` 放在二进制旁边就能跑。

## 切片与规模预检

大设计传切片参数收窄范围，切片参数参与产物缓存键，换了范围不会误用旧产物：

```
prepare_session(wave_path="dump.fsdb", fsdb_scopes=["u_core"], filelist_path="rtl.f")
prepare_session(wave_path="dump.fsdb", fsdb_signals_file="siglist.txt", filelist_path="rtl.f")
```

想先摸清文件规模再决定怎么转，用 `convert_fsdb_to_fst` 工具：

```
convert_fsdb_to_fst(fsdb_path="dump.fsdb", info_only=True)   # 只看刻度与信号统计
convert_fsdb_to_fst(fsdb_path="dump.fsdb", scopes=["u_core"]) # 手动转指定子树
```

转换如实记录：`prepare_session` 返回的 `steps` 里有 `convert_fsdb_to_fst`，
含耗时、是否命中缓存、以及信号统计（real / strength-skipped / unsupported-type）。

## 命令行用法

也可以脱离 wave-mcp 单独用这个转换器：

```bash
# 全量转换
fsdb2fst dump.fsdb dump.fst

# 按 scope 选择信号（缩小已选集合以满足内存阈值，多个子串是 OR 关系）
fsdb2fst -l u_core,uart dump.fsdb part.fst

# 按精确路径清单切片（一行一个路径，# 开头为注释）
fsdb2fst -L siglist.txt dump.fsdb part.fst

# 只看概要，不转换
fsdb2fst --info dump.fsdb
```

| 选项 | 作用 |
| --- | --- |
| `-l LIST` | 只转全路径含任一逗号分隔子串的信号（OR 语义） |
| `-L FILE` | 只转文件中精确列出的全路径 |
| `-p PACK` | FST 压缩：`lz4`（默认）/ `fastlz` / `zlib` |
| `--info` | 只打印文件 / 刻度 / 信号概要 |
| `--dump-tree` | 打印原始层次回调事件流，诊断 scope 路径问题 |
| `--allow-empty` | 没有值数据时也保留仅层次的输出 |
| `-v` | stderr 输出详细进度 |

**产物是 `.fst` 加 `.fst.hier` 两个文件，必须成对搬运。** fsdb2fst 用 sidecar
层次模式写出，缺 `.hier` 时 FST 直接打不开。这一点与 `vcd2fst` 不同，后者是
压缩层次单文件。

## 转换语义与能力边界

**时间刻度**：原样透传。FSDB 与 FST 同为「整数 tick x 10^N 秒」模型，转换只
把 FSDB 的刻度指数写进 FST 头，tick 值不做任何乘除，数值无损。支持 `1ns` /
`100fs` 这类常规写法，也支持 `0.01n`（= 10 ps）这类小数形式。刻度不是 10 的
整数次幂时明确报错而不猜。

**real 信号**：4 字节 float 与 8 字节 double 统一提升为 double 写入。读回时
wave-mcp 呈现为纯数字字符串。

**四态值**：Verilog 风格变量按 VCD 字节码（0/1/x/z）解码；VHDL `std_logic` /
`std_ulogic` 变量按 VHDL 字母表解码（`U`/`W`/`-` 折叠为 `x`，`L`/`H` 归为
`0`/`1`）。

**共享 idcode**：FSDB 里多个变量共用一个 idcode 时，第一个作为主变量，其余在
FST 里登记为 alias 共享同一句柄与值数据，不会重复存储。

**跳过的变量**：

- **强度值变量（strength，2 字节每位）**：跳过，与 wave-mcp 现有 FST 能力边界一致。
- **不可转换类型**：stream / transaction 变量、多维数组、property 与断言、
  coverage、SV/SystemC/AMS 内部类型等。这些的值负载不是普通位向量或实数。

`--info` 与转换日志会报出这三类计数：

```
[fsdb2fst] signals: 500 (1 real, 0 strength-skipped, 0 unsupported-type)
```

**常量信号没有值**：从不翻转的信号（parameter、常量驱动的 net）在 FST 里没有
值，表现同未驱动的 net。

## 超大文件的处理

[Issue #1](https://github.com/Tencent/wave-mcp/issues/1) 最初报告：约 112 MB、2280 万 VAR
的 FSDB 在转换时崩溃（`rc=-11`），筛选一个子模块仍失败。

**后续定位推翻了“规模导致崩溃”这一判断。** 实测中崩溃有两个互不相关的原因：

- **零值变化文件**：文件只有层次声明、完全没有值变化（既无 `$dumpvars` 初始值，也无
  任何时间戳）时，`ffrLoadSignals()` 会成功返回，但
  `ffrCreateTimeBasedVCTrvsHdl()` 返回的句柄内部迭代器为空，首次
  `ffrGetVarIdcodeXTagVCSeqNum()` 解引用空指针，触发 SIGSEGV。**崩溃点不在
  `ffrLoadSignals`**：`-v` 日志最后一行是 `loading value data ...`，容易被误读。
  该情形与文件规模无关，1429 信号、24 KB 的切片同样崩溃；转换器现在会在遍历前探测，
  给出明确报错而不是崩溃，`--allow-empty` 可只输出层次 FST。
- **运行环境不匹配**：报告者环境中，向 `LD_LIBRARY_PATH` 注入的 glibc 与二进制链接时的
  libc 不一致，进程在动态加载阶段、`main` 之前即崩溃（gdb 显示 `No stack`）。这与
  波形数据无关，属于环境问题，需另行排查。

**规模本身不是拒绝转换的理由。** 因此原先按“原始 VAR 总数”拒绝文件的保护已移除：它的
立论来自上述被推翻的判断，且属文件级硬拦，`-l` / `-L` / `fsdb_scopes` 都无法规避，会
拒绝实际可以正常转换的文件。

**保留一个保护，防的是内存而非崩溃：**

| 计数 | 默认阈值 | 作用 |
| --- | --- | --- |
| 筛选后的可转换信号路径数 | 500 万 | 超过即拒绝；所有已选信号的值数据一次性载入内存，用 `-l` / `-L` 缩小范围即可继续 |

这是 wave-mcp 的保守内存保护策略，**不是厂商公布的极限**。`FSDB2FST_MAX_SIGNALS`
可覆盖该阈值，`0` 关闭保护，非法值会报错。该默认值尚未用真实超大文件标定过峰值内存，
待实测后再调整。

`--info` / `--dump-tree` 只遍历层次，不加载值数据，不受该保护限制。

### `-l` 是子串匹配，不是按 scope 层级切分

`-l` 对信号全路径做子串匹配（多个值之间是 OR）。**传顶层 scope 名等于没筛**：几乎所有
信号路径都包含顶层名，筛完仍是全量，照样撞上 500 万保护。要真正收窄就传更深一层的子
scope 名，或用 `-L` 给精确路径清单。

实测例：一份 4961 万信号的 ZEBU 波形，传顶层 `-l canghaiv2_fullchip_emu_top.zebu_clk25m`
仍报全量超限；改传 `-l zebu_clk` 只选中 20 个信号并通过保护。

### 扁平单顶层设计目前无解

如果 FSDB 是扁平结构、只有一个顶层 scope、没有可拆的二级 scope，那么 `-l` / `-L`
再怎么调都绕不过去：上述 4961 万信号的文件即使筛到 20 个信号并通过了内存保护，
`ffrLoadSignals()` 仍然 SIGSEGV。当时机器有 3.9 TB 可用内存，排除 OOM，属 FsdbReader
自身对该文件的处理限制。

这种情况需要**在产生 FSDB 的工具侧分批导出**，wave-mcp 侧无法规避。转换器会在崩溃时
捕获信号并给出 `rc=3` 和明确说明（"crash inside the closed-source Verdi runtime, not an
out-of-memory kill"），而不是留下一个静默的 `rc=139` 让人怀疑是内存不足或 wave-mcp 的
问题。

`--info` 同时输出：

```text
[fsdb2fst] census: 12 total-vars, 3 unique-paths, 2 convertible
```

此行为格式示例。`total-vars` 是原始回调数，`unique-paths` 是按路径去重后的数量，
`convertible` 是其中支持转换的数量；转换日志另有 `selected` 表示筛选结果。
日志中的 `real` 指**浮点类型信号**，不是“真实/有效信号总数”；`signals`
为去重后的路径数，strength/unsupported 计数来自原始回调，不能简单相减推算。

**遇到崩溃时的排查顺序：**

- 先确认是否为零值变化文件：用 Verdi 的 `fsdb2vcd` 导出后统计 `$dumpvars` 段数与
  `#` 时间戳行数，两者都为 0 即属此类；若文件由多段 merge 而来，需逐段检查，而不是只看
  合并结果（`fsdbmerge` 取各段最小刻度）。
- 再确认运行环境：对比干净 `LD_LIBRARY_PATH` 与实际运行环境下 `--info` 的结果；若仅在
  注入 glibc 的环境中崩溃且 gdb 显示 `No stack`，属加载阶段问题。
- 内存不足时再用 `-l` / `-L` 按 scope 拆分，或在仿真源头减少 probe 范围。
- 仿真器支持时可直接生成 FST，避免这条 FsdbReader 加载路径。
- 仅提供脱敏后的 `--info` 统计、运行库版本和 dump/merge 参数即可继续诊断；不需要上传涉密波形。

`-l` / `-L` 在 `ffrLoadSignals` **之前**选择信号，但 `ffrLoadSignals()` 是无参调用，
加载的是整个文件的值数据，筛选只决定写入 FST 的范围。Python 入口会把 SIGSEGV 与缺失
动态库分开报告，保留原始诊断，不因日志出现 `libnffr` 就误导用户重新配置运行库。

**手工构建用户必须重新编译转换器，并确认 `FSDB2FST_BIN` 指向新二进制。**
仅更新 pip 包不能改变旧的 C++ 二进制；pip 包仍不附带转换器源码。

## 排错速查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 报 `fsdb2fst not found` 且提示 auto-build skipped | 没探测到 FsdbReader 运行库 | 在 MCP 配置的 `env` 里设 `VERDI_HOME`，或设 `FSDB2FST_FREADER` 指向拷来的 `share/FsdbReader` 目录 |
| 提示 auto-build attempted but failed | 自动编译失败，报错已附原因 | 看 `~/.cache/wave-mcp/fsdb2fst/*/build-failed.log`；缺 `g++` 时装编译器，或手工构建后用 `FSDB2FST_BIN` 指定 |
| 提示 auto-build unavailable | 找不到转换器源码；0.2.6 之前的 pip 包不含源码 | 升级到 `wave-mcp>=0.2.6`，或用 git checkout，或在别处构建后用 `FSDB2FST_BIN` 指向二进制 |
| 编译报 `ffrAPI.h: No such file` | `VERDI_HOME` 不对或缺 FsdbReader | `find / -name ffrAPI.h`，认准 `share/FsdbReader/` |
| 运行报找不到 `libnffr.so` | 二进制旁没有 `.so`，RPATH 也没命中 | 把两个 `.so` 拷到二进制同目录 |
| `cannot parse the FSDB time scale` | 刻度字符串不认识 | 把 `--info` 输出附在 issue 里反馈 |
| `no value data was loaded`（0 跳变） | 文件可能被截断，或该版本需换加载路径 | 先 `--info` 看概要；应急可加 `--allow-empty` |
| 产物打不开 | 只拷了 `.fst`，漏了 `.fst.hier` | 两个文件一起搬 |
| `selected signals exceed the in-core limit` | 已选信号数超保守内存阈值 | 用 `-l` / `-L` 减少选择，或调 `FSDB2FST_MAX_SIGNALS` |
| 传了 `-l` 仍报全量超限 | `-l` 是子串匹配，传顶层 scope 名等于没筛 | 改传更深一层的子 scope 名，或用 `-L` 给精确路径清单 |
| `FsdbReader crashed (SIGSEGV) while loading N selected signals`（rc=3） | 闭源 FsdbReader 内部崩溃，非 OOM、非 wave-mcp 缺陷 | 先 `--info` 看结构；扁平单顶层设计需在产生 FSDB 的工具侧分批导出 |
| `--dump-tree` 输出被截断 | 默认上限 2000 行，防止超大设计打爆磁盘 | 用 `FSDB2FST_DUMP_TREE_LINES=<n>` 调大，`0` 不限 |
| `contains no value change data at all` | 文件只有层次、无任何值变化 | 查 `$fsdbDumpvars` 参数与 dump 窗口；merge 产物需逐段查；应急可加 `--allow-empty` |
| `SIGSEGV (rc=-11)` | 零值变化文件，或运行环境 libc 不匹配 | 先按上一条查是否零值变化；若崩在 `main` 之前、gdb 显示 `No stack`，查 `LD_LIBRARY_PATH` 注入的 glibc；不是缺库的充分证据 |
| 某些信号在 FST 里没有值 | 常量 / 不翻转信号，或属跳过的类型 | 看转换日志的 strength / unsupported 计数 |
| 层次或 scope 路径可疑 | 需要看原始事件流 | `fsdb2fst --dump-tree x.fsdb \| head -50` |
