# 隔离网 / 离线环境部署手册

面向场景：**多机器、多用户、Python 版本不一、无外网、无 GTKWave、共享存储部署、无感启停**（常见于芯片研发的隔离网 / air-gapped 环境）。

核心思路：在**有网的开发机**生成一个**自包含 bundle**（自带独立 Python + 全部 wheel + 可选 vcd2fst + 源码），整体拷到**隔离网共享盘**，`install.sh` 一次安装；用户的 MCP 客户端用 **stdio** 起共享盘里的 `wave-mcp` 脚本即可，**无感启动、退出自动回收、多用户进程级隔离、零运维**。

```
[有网开发机]  build_offline_bundle.sh  ──tar──▶  [隔离网共享盘]  install.sh  ──▶  bin/wave-mcp
                                                                                      ▲
                                                          每个用户 MCP 客户端 stdio 调用（无感）
```

---

## 0. 目标机前提（典型）
- 架构 x86_64；glibc 版本按打包时的 `--target-glibc` 分两档：
  - **默认（2.28）**：目标机 glibc **≥ 2.28**（Ubuntu 18.10+ / Debian 10+ / CentOS 8+），用官方 pyslang/cryptography wheel。
  - **兼容档（2.17）**：目标机 glibc **≥ 2.17**（CentOS 7 / RHEL 7），需先用 `deploy/build_pyslang_manylinux2014.sh` 自编 pyslang wheel，见第 1 节。
- 多用户 Python 版本不一 → **bundle 自带独立 Python**（默认 3.11，其本身最低要求 glibc 2.17），与用户机器 Python 无关（连装没装都行）。

---

## 1. 在有网开发机生成 bundle

### 1.0 推荐：Docker 一键流水线（构建侧容器化，交付物仍是 tarball）

打包机只需要 docker（首轮需联网），一条命令产出完整发布矩阵：

```bash
deploy/docker_build_all.sh \
    --viewer /path/to/viewer-assets \      # 可选：Surfer WASM 资产目录
    --python https://github.com/astral-sh/python-build-standalone/releases/download/20241016/cpython-3.11.10+20241016-x86_64-unknown-linux-gnu-install_only.tar.gz

# 产物（dist/）：
#   wave-mcp-bundle-glibc2.28.tar.gz   主流机器（CentOS 8+ / Ubuntu 18.10+）
#   wave-mcp-bundle-glibc2.17.tar.gz   老机器（CentOS 7 / RHEL 7）
```

流水线内部四个 stage 全部在钉死版本的容器里执行，任何机器构建产物一致：
rust:alpine 编 musl 静态 surver（老机器 viewer 必需，官方 surver 要 glibc≥2.34）、
manylinux2014 编 pyslang wheel、manylinux_2_28 / manylinux2014 分别组装两档 bundle
并跑 glibc 审计。surver 与 pyslang 产物有缓存（`deploy/.docker-build-cache/`），
版本不变不重编；`--rebuild` 强制全量，`--skip-legacy` 只出 2.28 快速档。

### viewer 资产的版本钉定

surver（服务端）和 Surfer WASM（客户端）各自内嵌一个 wellen 版本，两者不一致时
Surfer 会在连接阶段直接拒绝，用户看到的是波形一直加载不出来。所以这两个产物必须
来自同一个上游 commit，钉定值集中在 `deploy/viewer-pin.sh` 一个文件里，其余脚本
一律 source 它，不再各写一份。

要升级 Surfer 版本，改 `deploy/viewer-pin.sh` 里的 `SURFER_REF` 和两个版本号，然后
两侧都重建：`build_surver_static.sh` 重编 surver，wasm 侧取同一 commit 的官方 CI
`pages_build` 产物，再用 `build_viewer_assets.sh` 重新打包。打包脚本会做两道校验，
一是 surver 与 wasm 互校，二是与钉定值比对，任一不符直接失败；`docker_build_all.sh`
的缓存也按 ref 判断，钉定值变了会自动重编，不需要手工清缓存。最后更新
`deploy/viewer-assets/PROVENANCE.md`。**不要把 `SURFER_REF` 钉到 release tag**，
tag 与 CI 发布的 wasm 快照并不同步。

发布矩阵就是这两个包：glibc 向下兼容，2.17 包理论上覆盖所有机器；保留 2.28
包是因为它全部使用官方 wheel。**用户侧完全不需要 docker**，拿到的仍是
tarball + `install.sh`。

以下 1a~1c 为分步手工路径（不便使用 docker 或需单独重编某组件时参考）。

```bash
# 1a. download a relocatable Python (python-build-standalone, install_only,
#     x86_64-gnu, 3.11.x) from github.com/astral-sh/python-build-standalone
# 1b. optional: a glibc-2.28-compatible vcd2fst (see section 4)

deploy/build_offline_bundle.sh \
    --out /tmp/wave-mcp-bundle \
    --python /path/to/cpython-3.11.x-...-install_only.tar.gz \
    --vcd2fst /path/to/glibc228/vcd2fst        # optional
```

产物：`/tmp/wave-mcp-bundle/` 与 `/tmp/wave-mcp-bundle.tar.gz`。
> 注意：生成 bundle 的机器**架构必须与目标一致(x86_64)**；wheel 是 cp311，故须搭配 3.11 的独立 Python。
> 打包末尾会自动审计所有 wheel 的平台 tag，任何 wheel 要求的 glibc 超过 `--target-glibc` 会直接报错点名，防止依赖升级悄悄抬高目标机门槛。

### 1c. 目标机是 CentOS 7 / RHEL 7（glibc 2.17）

官方 pyslang wheel 要求 glibc ≥ 2.27，在 2.17 目标机上 import 直接报
`GLIBC_2.27 not found`，需先自编一个 manylinux2014（glibc 2.17）兼容 wheel：

```bash
# build the glibc-2.17 pyslang wheel (docker + network required)
deploy/build_pyslang_manylinux2014.sh --out /tmp/pyslang-manylinux2014

# bundle with the 2.17 baseline and the self-built wheel
# (cryptography is swapped to its manylinux2014 build automatically)
deploy/build_offline_bundle.sh \
    --out /tmp/wave-mcp-bundle-el7 \
    --target-glibc 2.17 \
    --pyslang-wheel /tmp/pyslang-manylinux2014/pyslang-*.whl \
    --python /path/to/cpython-3.11.x-...-install_only.tar.gz
```

> 独立 Python（python-build-standalone）本身最低要求 glibc 2.17，兼容 CentOS 7+，无需特殊处理。
> vcd2fst 用 `deploy/build_vcd2fst.sh` 的产物即可（最高仅需 GLIBC_2.14）。

#### 1c 维护注意事项（重要）

- **pyslang 升级后需重编 wheel**：自编 wheel 不随官方自动更新，同步依赖版本后重跑编译脚本并重新打 bundle，避免"代码按新版 API 写、wheel 停在旧版"。
- **cp 标签与 Python 版本绑定**：wheel 按 cp311 构建（`--py`），需与 bundle 内 standalone Python 小版本一致。
- **新版 pyslang 可能有编译问题**：gcc 11 是官方支持线下限，遇编译错误按容器内报错在脚本中补充修复。
- **编译环境**：需 docker + 联网，脚本会自动切换可用镜像源安装工具链。
- **参数成对**：`--target-glibc 2.17` 必须搭配 `--pyslang-wheel`（已强制校验）。
- **`pip install` 不适用 2.17**：pip 不按 glibc 选依赖，CentOS 7 用户只能走容器或 bundle。
- **验证基准**：脚本内置容器内自测，且已在 glibc 2.17 容器通过真实项目全量功能检查；重编后建议重跑自测。

---

## 2. 拷贝到隔离网并安装

```bash
# extract on the air-gapped shared drive
tar -xzf wave-mcp-bundle.tar.gz -C /shared/
cd /shared/wave-mcp-bundle

# offline install (venv + local wheelhouse, no network or compiler)
./install.sh --prefix /shared/wave-mcp
```
安装完成会打印 `bin/wave-mcp` 路径与一段 MCP 客户端配置。

---

## 3. 用户接入（stdio，无感启停）

每个用户在自己的 MCP 客户端配置里加（见 `mcp.json.example`）：
```json
{ "mcpServers": { "wave-mcp": { "command": "/shared/wave-mcp/bin/wave-mcp", "args": [] } } }
```
- **启动**：客户端首次调用即把脚本拉起为子进程 → 无感。
- **退出**：客户端/会话关闭，子进程自动结束 → 无需 stop。
- **隔离**：每个用户各一个进程，互不影响；共享盘只读。
- 模型开始分析时调用 `prepare_session`（波形文件入口：.fst 直读 / .vcd 自动转 → 建网表 → 建会话），随后用查询类工具。

---

## 4. vcd2fst（隔离网没有 GTKWave）

bundle 里的 Python 依赖都是 wheel，glibc 兼容性由打包时的 `--target-glibc` 保证（末尾有自动审计）。**唯一的原生二进制是 `vcd2fst`**。三选一：

1. **(推荐) 用脚本在 manylinux_2_28 容器编一个**：
   ```bash
   deploy/build_vcd2fst.sh --out /tmp/vcd2fst-out      # needs docker
   # output needs only GLIBC_2.14 (libz/libpthread/libc)
   ```
   这样产出的二进制在任何 glibc≥2.14 的机器都能跑（含目标 2.28）。再 `--vcd2fst /tmp/vcd2fst-out/vcd2fst` 打进 bundle。
2. **从一台 glibc 2.28 机器拷现成 vcd2fst** + `ldd` 出的 `libz` 等，用 `--vcd2fst` 打进 bundle（脚本会一并带 `bin/lib/`、设好 `LD_LIBRARY_PATH`）。
> 不要直接用 glibc 2.38 机器编的 vcd2fst，会报 `GLIBC_2.3x not found`。
3. **目标机装 GTKWave**：若隔离网有本地 yum 镜像，`dnf install gtkwave` 也行（很多隔离网装不了，故非首选）。

> 校验：`ldd /shared/wave-mcp/bin/vcd2fst` 不应报 `not found`；`objdump -T vcd2fst | grep GLIBC | sort -V | tail -1` 应 ≤ 目标 glibc。

---

## 5. 升级 / 回滚
- 升级：在开发机重新 `build_offline_bundle.sh`，拷新 tar，解压到新目录，`install.sh --prefix /shared/wave-mcp-vN`，切换客户端 `command` 指向新路径。
- 回滚：客户端 `command` 指回旧 `bin/wave-mcp` 即可（旧 bundle 保留）。

## 6. 排错
- `install.sh` 的 sanity check 分两段：先 import wave_mcp/pyslang/pylibfst，再从 `/` 目录实跑一次生成的 `bin/wave-mcp`。import 失败多为 Python 版本与 wheel(cp311) 不匹配 → 用 `--python` 带独立 3.11；launcher 段失败说明脚本里有依赖 cwd 的路径，安装阶段就会拦住，不会流到客户端。
- **客户端只报 `-32000`（连不上 MCP server）**：这是客户端对"子进程起不来"的统一报错，看不到真实原因。直接在 shell 里手动执行一次 launcher 即可拿到诊断：`cd / && /shared/wave-mcp/bin/wave-mcp query --list`。若提示解释器不存在，说明安装时 `--prefix` 用了相对路径（客户端以 stdio 拉起时子进程 cwd 是用户项目目录，不是安装目录）→ 用绝对路径重跑 `install.sh --prefix /shared/wave-mcp`。
- **`--prefix` 一律用绝对路径**：`install.sh` 现在会自行绝对化并在开头就校验写权限，但显式给绝对路径最省事。
- **viewer 打不开、提示 assets not found**：`WAVE_MCP_VIEWER_ASSETS` 要用绝对路径（相对值会按 `$HOME` 解析）；工具返回的 hint 会写明是路径不存在还是缺 `surver` / `wasm/index.html`。
- 启动后连接/驱动/trace 不可用：多为该模块 pyslang elaboration 失败（filelist 不全/语法）→ 看 `prepare_session` 返回的 `build_netlist` step 的 error；其余工具不受影响（优雅降级）。

---

## 7. 旧环境 QA（Python 3.8/3.9、glibc 2.17 高频问题）

很多用户的目标机是长期不升级的隔离 / 加密环境（CentOS 7、老发行版、系统 Python 3.6–3.9）。
一句话结论：**旧环境的差异全部在打包侧解决，目标机不需要（通常也不能）做任何升级。**

按目标机情况选打包方式：

| 目标机情况 | 打包方式 |
| --- | --- |
| glibc ≥ 2.28，系统 Python ≥ 3.10 | 默认打包即可（仍建议带 `--python`，不受目标机 Python 变动影响） |
| glibc ≥ 2.28，系统 Python < 3.10 或没有 Python | 默认打包 + `--python`（standalone 3.11） |
| glibc 2.17 – 2.27（CentOS 7 / RHEL 7 等） | `--target-glibc 2.17` + `--pyslang-wheel`（见 1c 节）+ `--python` |
| glibc < 2.17 | 不支持（standalone Python 本身要求 ≥ 2.17） |

**Q：目标机只有 Python 3.8 / 3.9（或根本没有 Python），wave-mcp 能跑吗？**
能，且不需要动目标机。打包时用 `--python` 带上 standalone CPython 3.11
（python-build-standalone，选 `install_only`、`x86_64-unknown-linux-gnu` 的 tarball），
`install.sh` 检测到 `python/bin/python3` 会**优先用捆绑解释器**建 venv，
完全不碰系统 Python。目标机装没装 Python、装的什么版本都无所谓。

**Q：能不能让 wave-mcp 原生支持 Python 3.8 / 3.9？**
不能，也不建议尝试。卡点不在 wave-mcp 自身代码，而在依赖链：
- `mcp` SDK（v1 / v2）硬性要求 Python ≥ 3.10，旧解释器上直接装不上；
- 官方 `pyslang` wheel 不发 cp38（cibuildwheel 明确 skip），cp39 也不保证长期跟进，
  自编意味着每次升级都要重新编译整个 slang C++ 工程；
- 3.8 / 3.9 均已 EOL（2024-10 / 2025-10）。

正确姿势是"带运行时"（`--python`），不是"降级代码"。

**Q：目标机 `import pyslang` 报 `GLIBC_2.27' not found`，怎么办？**
官方 pyslang wheel 的 glibc 门槛（≥ 2.27/2.28）超过了目标机（典型 CentOS 7 = 2.17）。
按 1c 节走：先用 `deploy/build_pyslang_manylinux2014.sh` 自编兼容 wheel，再用
`--target-glibc 2.17 --pyslang-wheel <wheel>` 重新打 bundle。打包末尾的 wheel 审计会兜底：
任何 wheel 的 glibc 要求超过 `--target-glibc` 都会报错点名，不会静默放行。

**Q：怎么确认目标机的 glibc 版本？**
```bash
getconf GNU_LIBC_VERSION      # 或 ldd --version | head -1
```
≥ 2.28 走默认档；2.17 ≤ 版本 < 2.28 走 1c 兼容档；< 2.17 不支持。

**Q：为什么不能在目标机直接 `pip install wave-mcp`？**
旧环境下两个问题叠加：pip 判定官方 manylinux_2_28 wheel 与本机 glibc 不兼容后会退化为
源码编译，而 pyslang 的 C++ 工程在无网、无新工具链（需 gcc ≥ 11）的目标机上几乎必然失败；
系统 Python < 3.10 时 `mcp` 本身也装不上。隔离网 + 旧环境只有 bundle 一条路，
这正是 bundle 存在的意义。

**Q：standalone Python 自己对 glibc 有要求吗？**
有，最低 **glibc 2.17**（与 CentOS 7 一致），所以 2.17 兼容档整条链路
（独立 Python + 全部 wheel + vcd2fst）都能在 CentOS 7 上跑通。
vcd2fst 用 `deploy/build_vcd2fst.sh` 的产物最高仅需 GLIBC_2.14，不构成额外门槛。
