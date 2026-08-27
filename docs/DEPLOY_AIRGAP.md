# 隔离网 / 离线环境部署手册

面向场景：**多机器、多用户、Python 版本不一、无外网、无 GTKWave、共享存储部署、无感启停**（常见于芯片研发的隔离网 / air-gapped 环境）。

核心思路：在**有网的开发机**生成一个**自包含 bundle**（自带独立 Python + 全部 wheel + 可选 vcd2fst + 源码），整体拷到**隔离网共享盘**，`install.sh` 一次安装；用户的 MCP 客户端用 **stdio** 起共享盘里的 `wave-mcp` 脚本即可——**无感启动、退出自动回收、多用户进程级隔离、零运维**。

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

1. **(推荐) 用脚本在 manylinux_2_28 容器编一个**（已验证）：
   ```bash
   deploy/build_vcd2fst.sh --out /tmp/vcd2fst-out      # needs docker
   # output needs only GLIBC_2.14 (libz/libpthread/libc)
   ```
   这样产出的二进制在任何 glibc≥2.14 的机器都能跑（含目标 2.28）。再 `--vcd2fst /tmp/vcd2fst-out/vcd2fst` 打进 bundle。
2. **从一台 glibc 2.28 机器拷现成 vcd2fst** + `ldd` 出的 `libz` 等，用 `--vcd2fst` 打进 bundle（脚本会一并带 `bin/lib/`、设好 `LD_LIBRARY_PATH`）。
> 不要直接用 glibc 2.38 机器编的 vcd2fst —— 会报 `GLIBC_2.3x not found`。
3. **目标机装 GTKWave**：若隔离网有本地 yum 镜像，`dnf install gtkwave` 也行（很多隔离网装不了，故非首选）。

> 校验：`ldd /shared/wave-mcp/bin/vcd2fst` 不应报 `not found`；`objdump -T vcd2fst | grep GLIBC | sort -V | tail -1` 应 ≤ 目标 glibc。

---

## 5. 升级 / 回滚
- 升级：在开发机重新 `build_offline_bundle.sh`，拷新 tar，解压到新目录，`install.sh --prefix /shared/wave-mcp-vN`，切换客户端 `command` 指向新路径。
- 回滚：客户端 `command` 指回旧 `bin/wave-mcp` 即可（旧 bundle 保留）。

## 6. 排错
- `install.sh` 末尾 sanity check 会 import wave_mcp/pyslang/pylibfst；失败多为 Python 版本与 wheel(cp311) 不匹配 → 用 `--python` 带独立 3.11。
- 启动后连接/驱动/trace 不可用：多为该模块 pyslang elaboration 失败（filelist 不全/语法）→ 看 `prepare_session` 返回的 `build_netlist` step 的 error；其余工具不受影响（优雅降级）。
