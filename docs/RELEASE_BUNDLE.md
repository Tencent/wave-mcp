# wave-mcp 发版（离线 bundle）标准流程

面向**有网开发机**的发版 SOP：从源码打出一个自包含 bundle，做完自检后交付到隔离网共享盘安装。
安装侧（隔离网内如何 install / 接入）见 `docs/DEPLOY_AIRGAP.md`，本文只讲**怎么打、怎么验、怎么发**。

```
[开发机] 改代码 → build_vcd2fst.sh(一次性/换版才需) → build_offline_bundle.sh → 自检 → tar
        └────────────────────────────────────────────────────────────┘
                                    ▼
                         交付到隔离网共享盘 → install.sh（见 DEPLOY_AIRGAP.md）
```

---

## 0. 一次性准备（换 Python / 换 GTKWave 版本才需重做）

1. **独立 Python 运行时**（bundle 自带，与目标机 Python 无关）
   从 python-build-standalone 下载 `cpython-3.11.x-...-x86_64-unknown-linux-gnu-install_only.tar.gz`，
   放到固定路径，例如 `/tmp/cpython311.tar.gz`。
   > wheel 是 cp311，故必须搭配 3.11 的独立 Python。

2. **带并行的 vcd2fst**（glibc≤2.14，隔离网 glibc 2.28 可跑）
   ```bash
   deploy/build_vcd2fst.sh --out /tmp/vcd2fst-out      # 需要 docker
   ```
   - 在 manylinux_2_28 容器里用 gcc 直编 `fstapi.c`，**关键编译宏**：
     `-DHAVE_LIBPTHREAD=1 -DFST_WRITER_PARALLEL=1`（缺任一，`-p` 运行时 exit(255)）。
   - 脚本自带 `-p` 自检：**自检不过会 fail build**，杜绝出没有并行的坏包。
   - 产物 `/tmp/vcd2fst-out/vcd2fst`，`objdump -T` 最高应为 `GLIBC_2.14`。
   > 这一步不常做——只要 GTKWave 版本不变、编译宏不变，`vcd2fst` 可长期复用。

---

## 1. 打 bundle

```bash
deploy/build_offline_bundle.sh \
    --out /tmp/wave-mcp-bundle-vX.Y \
    --python /tmp/cpython311.tar.gz \
    --vcd2fst /tmp/vcd2fst-out/vcd2fst
```

产物：`/tmp/wave-mcp-bundle-vX.Y/`（目录）+ `/tmp/wave-mcp-bundle-vX.Y.tar.gz`（约 58M）。

bundle 内容：
| 目录 | 内容 |
| --- | --- |
| `python/` | 独立 Python 3.11 运行时（可重定位）|
| `wheels/` | 离线 wheelhouse（本项目 wheel + 全部依赖，**32 个**）|
| `src/` | 源码副本（参考用）|
| `bin/vcd2fst` + `bin/lib/` | glibc 兼容的 vcd2fst + 依赖库 |
| `install.sh` / `wave-mcp.template` / `mcp.json.example` / `VERSION` | 安装器 + 启动器模板 + 客户端配置样例 |

> 打包脚本会**自动剔除 cryptography** wheel：它是 `mcp → pyjwt[crypto]` 的可选传递依赖、wave-mcp 不用，
> 且新版需 glibc 2.34（隔离网 2.28 装不上）。`install.sh` 用 `pip install --no-deps` 防止被重新拉回。

---

## 2. 发版前自检（务必全过再发）

在开发机对**刚打出的 bundle**跑以下检查（本项目历次发版实际用的清单）：

```bash
B=/tmp/wave-mcp-bundle-vX.Y

# 2.1 wheel 内含最新源码（改了哪个模块就 grep 哪个）
unzip -l $B/wheels/wave_mcp-*.whl | grep -E "name_infer|slang_netlist|server.py"

# 2.2 工具数 = 48
grep -c "@mcp.tool" $B/src/wave_mcp/server.py         # 期望 48

# 2.3 wheels 数 = 32，且不含 cryptography
ls $B/wheels | wc -l
ls $B/wheels | grep -i crypto && echo "!! 不应出现 cryptography" || echo "OK: 无 cryptography"

# 2.4 vcd2fst 并行 -p 自检 + glibc 检查
td=$(mktemp -d)
printf '$timescale 1ns $end\n$scope module t $end\n$var wire 1 ! a $end\n$upscope $end\n$enddefinitions $end\n#0\n0!\n#1\n1!\n' > $td/p.vcd
$B/bin/vcd2fst -F -p -v $td/p.vcd -f $td/p.fst && echo "OK: -p 并行可用" || echo "!! 并行失败"
objdump -T $B/bin/vcd2fst | grep -oE 'GLIBC_[0-9.]+' | sort -uV | tail -1   # 期望 ≤ GLIBC_2.14

# 2.5 冒烟 + 单测（在源码仓库跑，非 bundle）
python3 tests/smoke_test.py
python3 tests/test_definition_name.py
```

（可选）在开发机临时 install 一遍验证端到端：
```bash
tar -xzf $B.tar.gz -C /tmp/_verify && /tmp/_verify/wave-mcp-bundle-vX.Y/install.sh --prefix /tmp/_verify/inst
# install.sh 末尾自带 sanity check：import wave_mcp/pyslang/pylibfst 并打印版本
```

**自检清单（逐条打勾）**
- [ ] wheel 含本次改动的源码
- [ ] 工具数 37
- [ ] wheels 32 个、无 cryptography
- [ ] `vcd2fst -p` 通过、GLIBC ≤ 2.14
- [ ] smoke_test / test_definition_name 通过
- [ ] （可选）本机 install sanity check 通过

---

## 3. 版本号与变更记录

- **命名**：`wave-mcp-bundle-v<MAJOR>.<MINOR>.tar.gz`（如 `v4.19`）。`VERSION` 文件由打包脚本写入 UTC 时间戳。
- **每次发版在下方追加一行变更摘要**（谁、日期、动了什么、为什么）。

| 版本 | 日期 | 关键变更 |
| --- | --- | --- |
| v2 | 2026-06-30 | 41 工具；含 cryptography（隔离网装不上）；vcd2fst 无并行 |
| v3 | 2026-07-01 | 剔除 cryptography + `--no-deps`，修复隔离网安装 |
| v4 | 2026-07-01 | 新增覆盖率/断言/module_type，41→48 工具 |
| v4.11–v4.12 | 2026-07-01 | 网表 incdir/defines 修复、结构化诊断、CSV header 化 |
| v4.13 | 2026-07-02 | vcd2fst 重编开 `FST_WRITER_PARALLEL`（并行可用）|
| v4.14–v4.15 | 2026-07-02 | definition_name 三层解析 + 自愈 incdir/package |
| v4.16–v4.17 | 2026-07-02 | MCP structuredContent + content[].text 人读文本 |
| v4.18 | 2026-07-02 | UVM 目录自动探测 + interface 守卫 + leaf/后缀匹配 |
| v4.19 | 2026-07-02 | 锚点向上推导 + netlist_health 区分 error/warning |

---

## 4. 交付与升级/回滚
- 把 `wave-mcp-bundle-vX.Y.tar.gz` 拷到隔离网共享盘，按 `docs/DEPLOY_AIRGAP.md` 安装到 `/shared/wave-mcp-vX.Y`。
- **升级**：安装新版到新目录，客户端 `command` 指向新 `bin/wave-mcp`。
- **回滚**：客户端 `command` 指回旧 `bin/wave-mcp`（旧 bundle 保留即可）。

---

## 5. 常见坑
- **vcd2fst `-p` 报 `FST_WRITER_PARALLEL not enabled`**：编译漏了 `-DHAVE_LIBPTHREAD=1 -DFST_WRITER_PARALLEL=1`（两个都要）。重编。
- **隔离网 install 报 cryptography / GLIBC_2.34**：bundle 没剔除 cryptography 或没用 `--no-deps`。重打包。
- **sanity check import 失败**：目标机 Python 与 wheel(cp311) 不匹配 → 必须用 `--python` 带独立 3.11。
- **UVM 环境 netlist 仍 partial**：`xrun` 不在 PATH（UVM 目录探测依赖它）→ 设 `$UVMHOME`/`$CDS_INST_DIR` 或把 xrun 加进 PATH；查 `netlist_health.auto_resolved.uvm_incdirs` 是否探测到。
- **大量 pyslang warning 但非 error**：属正常（UVM lint）；看 `netlist_health.diagnostic_errors` 是否为 0，为 0 即可信。
