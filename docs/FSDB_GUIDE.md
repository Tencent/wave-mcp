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
      "env": { "VERDI_HOME": "/path/to/verdi" }
    }
  }
}
```

```
prepare_session(wave_path="dump.fsdb", filelist_path="your_filelist.f")
```

首次转换时 wave-mcp 会自动编一次 `fsdb2fst`（需要 `g++`，约十几秒），之后直接复用。

超大设计（选中信号超 500 万）需要切片，传 `fsdb_scopes=["u_core"]` 收窄范围。
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

自动构建需要三个条件同时满足：探测到 FsdbReader 运行库、有 `g++`、仓库里有转换器源码
（pip 安装的包不含源码，此时这一级自动跳过）。任一不满足时，报错会指名**具体缺哪一样**，
不会只说"找不到"。

关掉自动构建用 `WAVE_MCP_FSDB2FST_AUTOBUILD=0`。

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
bash deploy/build_fsdb2fst.sh        # 默认产出 third_party/fsdb2fst/fsdb2fst

# 想换个落点（自动构建走的就是这条路）
FSDB2FST_OUT=/somewhere/fsdb2fst bash deploy/build_fsdb2fst.sh
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

# 按 scope 切片（超大设计必用，多个子串是 OR 关系）
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

FsdbReader 在超大设计（千万信号量级的门级展开）上会在内部崩溃，且与内存余量
无关、切片也未必能绕过。fsdb2fst 的应对是提前拦而不是让它崩：

- 选中信号数超过 500 万时直接报错并提示用 `-l` / `-L` 切片，阈值可用
  `FSDB2FST_MAX_SIGNALS=<n>` 覆盖，`0` 关闭。
- 文件整体超限但已切片时额外提示：这种规模下 FsdbReader 仍可能在内部崩溃，
  属于 reader 的限制而不是筛选的问题。
- FsdbReader 的返回码逐个检查，失败即明确报错。

## 排错速查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 报 `fsdb2fst not found` 且提示 auto-build skipped | 没探测到 FsdbReader 运行库 | 在 MCP 配置的 `env` 里设 `VERDI_HOME`，或设 `FSDB2FST_FREADER` 指向拷来的 `share/FsdbReader` 目录 |
| 提示 auto-build attempted but failed | 自动编译失败，报错已附原因 | 看 `~/.cache/wave-mcp/fsdb2fst/*/build-failed.log`；缺 `g++` 时装编译器，或手工构建后用 `FSDB2FST_BIN` 指定 |
| 提示 auto-build unavailable | pip 安装包内不含转换器源码 | 用 git checkout，或在别处构建后用 `FSDB2FST_BIN` 指向二进制 |
| 编译报 `ffrAPI.h: No such file` | `VERDI_HOME` 不对或缺 FsdbReader | `find / -name ffrAPI.h`，认准 `share/FsdbReader/` |
| 运行报找不到 `libnffr.so` | 二进制旁没有 `.so`，RPATH 也没命中 | 把两个 `.so` 拷到二进制同目录 |
| `cannot parse the FSDB time scale` | 刻度字符串不认识 | 把 `--info` 输出附在 issue 里反馈 |
| `no value data was loaded`（0 跳变） | 文件可能被截断，或该版本需换加载路径 | 先 `--info` 看概要；应急可加 `--allow-empty` |
| 产物打不开 | 只拷了 `.fst`，漏了 `.fst.hier` | 两个文件一起搬 |
| `selected signals exceed the in-core limit` | 选中信号数超阈值 | 用 `-l` / `-L` 切片，或调 `FSDB2FST_MAX_SIGNALS` |
| 某些信号在 FST 里没有值 | 常量 / 不翻转信号，或属跳过的类型 | 看转换日志的 strength / unsupported 计数 |
| 层次或 scope 路径可疑 | 需要看原始事件流 | `fsdb2fst --dump-tree x.fsdb \| head -50` |
