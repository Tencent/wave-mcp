# FSDB 自动构建验证包（给有 Verdi 的机器）

本次改动让 wave-mcp 在首次转换 FSDB 时**自动编一次 `fsdb2fst`**，用户只需在 MCP
配置里给出 `VERDI_HOME`。这台开发机没有 Verdi，所以自动构建产出的二进制**能否真正
读 FSDB** 没验过，需要你在有 Verdi 的机器上跑一次。

## 为什么历史的端到端跑过不算数

三条，都是这次必须重跑的原因：

1. **历史跑的是另一条代码路径**。以前 `third_party/fsdb2fst/fsdb2fst` 一直躺在仓库里，
   解析函数在第二级就命中返回了，新增的「用户缓存」和「按需构建」两级从没被执行过。
2. **从 `VERDI_HOME` 构建这条路以前根本编不过**。`build_fsdb2fst.sh` 里
   `INC_ARGS=(-I"$READER_DIR")` 是整体覆盖而不是追加，把 vendored 的 `fst/` 头文件
   路径弄丢了，必报 `fstapi.h: No such file`。也就是说历史上能跑通的二进制，必然都是
   走 repo-local runtime 分支编出来的，和自动构建走的分支不同源。这次改成了 `+=`。
3. **本地只用编译桩验证过**。我用空实现的假 `libnffr.so` 验证了「能编出来、能缓存」，
   但桩库不解析任何 FSDB 数据，**转换语义一步都没走**。

## 需要什么

- 一台装了 Verdi 的 Linux x86_64 机器，`$VERDI_HOME/share/FsdbReader/linux64` 存在
- `g++`（自动构建要用，十几秒的活）
- 一份**以前跑过、你知道正确结果**的 `.fsdb`，加它对应的 `top` 实例名和 `rtl.f`
- Python 3.10+ 环境，能 `import wave_mcp`

## 怎么跑

解压后在仓库根目录执行：

```bash
export VERDI_HOME=/path/to/verdi
bash verify_fsdb_autobuild.sh <dump.fsdb> <top_instance> <rtl.f>
```

脚本会打印 `PASS/FAIL/SKIP` 汇总，退出码非 0 表示有失败项。

## 脚本会验什么

| 步骤 | 验什么 | 为什么这条重要 |
| --- | --- | --- |
| 0 | FsdbReader 能否探测到 | 探测不到则后面全无意义，直接退出 |
| 1 | 自动构建真被触发、产物落在用户缓存、能 `--info` 读真实 FSDB | **这条是编译桩覆盖不到的核心** |
| 2 | 走 `prepare_session` 真转一遍，再查信号值 | 验转换语义，不只看「返回非空」 |
| 3 | 二次解析命中缓存不重编；转换缓存复用 `.fst` | 验缓存真生效，不是每次重来 |
| 4 | `FSDB2FST_BIN` 覆盖仍优先；关闭开关可用 | 防止动了新路径把老路径搞坏 |

## 注意事项

**脚本会临时移走两样东西，退出时自动恢复**：仓库里已有的
`third_party/fsdb2fst/fsdb2fst`，和已有的 `~/.cache/wave-mcp/fsdb2fst/`。这是故意的，
不移走就会命中旧路径，测不到新增逻辑。如果脚本被 `kill -9` 打断没恢复，去
`/tmp/tmp.XXXX/` 里找 `fsdb2fst.repo.bak` 和 `cache.bak` 手工搬回。

**第 2 步的信号值需要你亲自看一眼**。脚本只能判断「有值、非空」，判断不了「值对不对」。
按你定的规矩，非空不代表没缺陷，所以脚本会把采样到的 5 个信号的前几个跳变打出来，
请对照你以前的结果或 Verdi 确认数值一致。这一步机器替不了。

**别拿超大 FSDB 试**。选中信号超 500 万会被提前拦下来报错，那是预期行为不是缺陷。
挑个中等规模的、你熟悉的波形最合适。

**用完可以删掉缓存**：`rm -rf ~/.cache/wave-mcp/fsdb2fst`。

## 跑完给我什么

整段输出贴回来就行，特别是：

- 第 1 步 `RESOLVED=` 的路径和构建耗时
- 第 1 步 `--info` 的输出（信号统计那几行）
- 第 2 步 `VAL ...` 那几行，以及你对数值是否正确的判断
- 末尾 `PASS=x FAIL=y SKIP=z`

有 `FAIL` 的话，把 `~/.cache/wave-mcp/fsdb2fst/*/build-failed.log` 也一起贴上。

## 包里有什么

```
verify_fsdb_autobuild.sh          验证脚本（唯一要你执行的）
VERIFY_README.md                  本文件
wave_mcp/convert.py               改动主体：五级解析 + 按需构建 + 用户缓存
deploy/build_fsdb2fst.sh          修了 INC_ARGS 覆盖的缺陷（一行）
deploy/build_fstdumper.sh         新增：一键编 fstdumper 插件（方案 B，本次不验）
examples/xcelium_fst/fst_dump_cfg.sv  新增：开箱可用的 dump 控制模块（方案 B）
docs/FSDB_GUIDE.md                加了三行速查
docs/XCELIUM_FST_GUIDE.md         加了五行速查
CHANGES.diff                      完整改动 diff，便于 review
```

方案 B 那三件（Xcelium）不需要在这台机器验，它们只是把现成件和一键脚本交给用户，
真要验得有 Xcelium 环境，可以另外安排。

## 验完之后

这些改动**还没提交**。你确认通过后我提交到工蜂，GitHub 开源仓按惯例等你明确放行再推。
