# fsdb2fst 上机测试手册（Verdi 机器一次性操作）

目标：在有 Verdi 的机器上编译出真实的 `fsdb2fst` 二进制，并用一个样例
FSDB 完成首次真实转换。预计总耗时 10 分钟内（不含传文件）。

## 0. 带上这个包

把 `verdi_test/` 整个目录（含 `third_party/fsdb2fst/` 源码与样例）传到
Verdi 机器，比如放到 `~/fsdb2fst_test/`。唯一要求：

- `g++` 可用（GCC 7 及以上，支持 -std=c++17）
- `zlib` 开发头文件（几乎所有 Linux 机器都有；缺了报
  `zlib.h: No such file` 时找 IT 或 `yum install zlib-devel`）
- `VERDI_HOME` 指向 Verdi 安装目录（或者你直接把路径作为脚本第一个参数）

不需要 license，不需要写权限到 Verdi 安装目录，全程只在测试目录里产文件。

## 1. 一键构建 + 冒烟

```bash
cd ~/fsdb2fst_test/deploy/verdi_test
bash build_and_smoke.sh            # VERDI_HOME 已设置时
bash build_and_smoke.sh /tools/synopsys/verdi/XXX   # 或者显式传路径
```

脚本会自动：定位 FsdbReader → 编译 → `--help`/错误路径冒烟 →
（如果环境变量 `FSDB2FST_SAMPLE` 指向一个 .fsdb）真实转换一次。
结束打印 PASS/FAIL 表。

## 2. 没有现成 .fsdb？两个办法（任选其一，推荐 2.1）

### 2.1 用 Verdi 自带 vcd2fsdb 转换（最快，直接可对拍）

包里已带 golden 数据 `sample/dump.vcd`
（来自 `examples/sample/` 的 counter 测试台），直接：

```bash
VCD2FSDB=$(command -v vcd2fsdb || ls $VERDI_HOME/platform/*/bin/vcd2fsdb 2>/dev/null | head -1)
$VCD2FSDB sample/dump.vcd sample/dump.fsdb
```

优点：dump.vcd 是已验证的 golden，FSDB→FST 转换后可以逐值和 VCD 对拍
（见第 4 节）。

### 2.2 用现有项目波形

任何你们手头的小号 .fsdb 都行（先拿小的，<100MB）。
设置后重跑一键脚本即可让它自动转换：

```bash
FSDB2FST_SAMPLE=/path/to/dump.fsdb bash build_and_smoke.sh
```

## 3. license 验证（顺手做）

转换过程中不应出现任何 Verdi/license checkout。最直接的验证：
转换前后各跑一次 lmstat 对比，或转换时另开终端看：

```bash
watch -n1 "$LM_LICENSE_FILE 对应的 lmstat -a | grep -A2 -i verdi"
```

ffrAPI 运行库官方口径是不占 license，但我们按惯例实证一下，把结果记进
5c 的反馈里。

## 4. 转换完顺手做 3 个快速检查（都只读，不花时间）

```bash
cd ~/fsdb2fst_test
B=fsdb2fst/fsdb2fst

# 1) 文件是合法 FST
$B --info sample/dump.fsdb            # 读原始 FSDB 概要
fst2vcd sample/dump.fst | head -30    # 或用 gtkwave 的 fst2vcd，能读即结构合法

# 2) 与 VCD golden 对拍（仅当样例来自 vcd2fsdb 转换）
fst2vcd sample/dump.fst > /tmp/roundtrip.vcd
diff <(grep -v '^\$date\|^\$version\|^\$comment\|^\$timescale\|^\$enddefinitions\|^$' /tmp/roundtrip.vcd) \
     <(grep -v '^\$date\|^\$version\|^\$comment\|^\$timescale\|^\$enddefinitions\|^$' sample/dump.vcd) \
  && echo "ROUNDTRIP OK" || echo "roundtrip diff (把两个文件都带回来分析)"

# 3) 时间刻度一致
fst2vcd sample/dump.fst | grep timescale
grep timescale sample/dump.vcd
```

注意：fst2vcd 不是必须的，只是最直观的对拍方式；没有的话跳过，
把 .fst 带回来在 wave-mcp 侧用 pylibfst 全量核对（第 5 节）。

## 5. 带回 wave-mcp 机器的文件清单（round-2 验证用）

| 文件 | 用途 |
| --- | --- |
| `fsdb2fst/fsdb2fst` | 真实二进制（放回 wave-mcp 仓库 `third_party/fsdb2fst/` 同名路径即可直接用） |
| `sample/dump.fsdb` | 样例输入（小文件） |
| `sample/dump.fst` | 转换产物 |
| `fsdb2fst/build.log` | 仅当编译有告警/失败时 |
| license 观察结果 | 一句话结论（占/不占） |

round-2（在 wave-mcp 侧做，无需你再操作）：把 .fst 喂给
`prepare_session`，跑 27 个工具全量 + 与 `examples/sample/dump.vcd`
的 golden 值对拍，确认转换无损后 `.fsdb` 路线即宣告打通。

## 6. 出问题怎么办

- 编译失败：把 `fsdb2fst/build.log` 带回来，或者直接
  `bash build_and_smoke.sh 2>&1 | tail -40` 截给我。
- `ffrAPI.h: No such file`：VERDI_HOME 不对，`find /tools -name ffrAPI.h`
  找到真实位置后 `export VERDI_HOME=<其上级三级目录>`。
- 转换报 "cannot parse the FSDB time scale"：这是设计上的 fail-loud，
  把 `--info` 输出（含 ffrGetScaleUnit 原始字符串）带回来即可。
- 转换报 "no value data was loaded"：样例可能是特殊版本 FSDB，带回来
  我们看是否需要换 ffrAPI 的加载路径（留了 --allow-empty 出口）。
