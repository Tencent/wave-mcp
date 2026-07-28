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
- 架构 x86_64；glibc **≥ 2.17**（pylibfst manylinux 门槛；多数企业 Linux 满足）。
- 多用户 Python 版本不一 → **bundle 自带独立 Python**（默认 3.11），与用户机器 Python 无关（连装没装都行）。

---

## 1. 在有网开发机生成 bundle

```bash
# 1a. 先下载一个可重定位的独立 Python（python-build-standalone, install_only, x86_64-gnu, 3.11.x）
#     例如从 github.com/astral-sh/python-build-standalone releases 下载：
#     cpython-3.11.x+YYYYMMDD-x86_64-unknown-linux-gnu-install_only.tar.gz
#
# 1b. 准备一个与目标机 glibc(2.28) 兼容的 vcd2fst（见第 4 节方案），可选

deploy/build_offline_bundle.sh \
    --out /tmp/wave-mcp-bundle \
    --python /path/to/cpython-3.11.x-...-install_only.tar.gz \
    --vcd2fst /path/to/glibc228/vcd2fst        # 可选
```

产物：`/tmp/wave-mcp-bundle/` 与 `/tmp/wave-mcp-bundle.tar.gz`。
> 注意：生成 bundle 的机器**架构必须与目标一致(x86_64)**；wheel 是 cp311，故须搭配 3.11 的独立 Python。

---

## 2. 拷贝到隔离网并安装

```bash
# 在隔离网共享盘解压
tar -xzf wave-mcp-bundle.tar.gz -C /shared/
cd /shared/wave-mcp-bundle

# 离线安装（创建 venv + 从本地 wheelhouse 装，无需联网/编译）
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

bundle 里的所有 Python 依赖都是 wheel（glibc 兼容已满足），**唯一的原生二进制是 `vcd2fst`**。三选一：

1. **(推荐) 用脚本在 manylinux_2_28 容器编一个**（已验证）：
   ```bash
   deploy/build_vcd2fst.sh --out /tmp/vcd2fst-out      # 需要 docker
   # 产出 /tmp/vcd2fst-out/vcd2fst，最高仅需 GLIBC_2.14，依赖 libz/libpthread/libc
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
