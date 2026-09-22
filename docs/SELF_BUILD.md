# 自建组件指南（Self-Build Guide）

[English version](SELF_BUILD.en.md)

wave-mcp 核心包采用宽松许可（Apache-2.0），不携带、也不代为分发下列许可不兼容或依赖商业软件的组件。它们全部遵循同一条自举原则：

> **wave-mcp 只提供构建脚本和固定版本信息，源码和产物都留在你的机器上：我们不下载、不打包、不再分发。**

这样核心包的宽松许可保持干净，而你在自己环境里构建的产物只受对应上游许可约束，不产生再分发义务。

## 统一约定

三个自建组件遵循相同的管理标准：

| 约定 | 说明 |
| --- | --- |
| 不自动下载 | 构建脚本从不 clone/fetch 上游源码，需要你先自行获取 |
| 版本钉死 | 每个组件有单一版本事实源（pin 文件或 SOURCES.json），脚本按 pin 校验 |
| 产物不入库 | 构建产物不 commit、不进 PyPI 包、不进离线 bundle 的分发部分 |
| 缓存目录统一 | 产物/资产统一放 `~/.wave-mcp/cache/`（可用 `WAVE_MCP_CACHE_ROOT` 改），或用组件各自的环境变量显式指定 |
| 优雅降级 | 组件缺失时对应工具返回带指引的错误提示，其余分析工具不受影响 |

| 组件 | 上游许可 | 构建脚本 | 版本事实源 | 产物位置（查找顺序） |
| --- | --- | --- | --- | --- |
| viewer 资产（Surfer WASM + surver） | EUPL-1.2 | `deploy/build_surver_static.sh` + `deploy/build_viewer_assets.sh` | `deploy/viewer-pin.sh` | `WAVE_MCP_VIEWER_ASSETS` → `~/.wave-mcp/cache/viewer/` |
| fsdb2fst（FSDB 转换器） | 源码 MIT；构建时链接用户自备的 Synopsys FsdbReader | `deploy/build_fsdb2fst.sh`（首次用到 FSDB 时自动触发） | `docs/licenses/vcd2fst.SOURCES.json` | `~/.wave-mcp/cache/fsdb2fst/`（`FSDB2FST_FREADER`/`VERDI_HOME` 提供运行库） |
| fstdumper（Xcelium VPI 插件） | GPL-3.0 | `deploy/build_fstdumper.sh` | `docs/licenses/fstdumper.SOURCES.json` | 你的 checkout 目录（产物自管，团队内可共享） |

## 1. viewer 资产（Surfer WASM + surver）

波形查看器的前端（Surfer WASM）与流式后端（surver）是 EUPL-1.2 许可的 Surfer 项目构建产物。**wave-mcp 不再通过 PyPI 或 GitHub Release 分发这两样资产**，`pip install wave-mcp[viewer]` 已移除。构建一次后资产可长期使用，全团队可以共享同一份。

前置条件：一台可联网、装有 docker 的机器（构建机与使用机可以不是同一台）。

```bash
# 1. 构建 surver（musl 静态二进制，任何 x86-64 Linux 可用）
#    版本 pin 在 deploy/viewer-pin.sh，不要传其他 ref
deploy/build_surver_static.sh            # 产出 deploy/surver-static/surver

# 2. 获取同一 commit 的 Surfer WASM 构建
#    从 pin 的 commit 自行构建（trunk 模式），或取上游 CI pages_build 产物；
#    两侧 wellen 版本必须一致，打包脚本会强制校验
#    上游源码地址见 deploy/viewer-pin.sh 中的 SURFER_REF：
#    https://gitlab.com/surfer-project/surfer

# 3. 组装资产目录
mkdir -p ~/.wave-mcp/cache/viewer/wasm
cp deploy/surver-static/surver ~/.wave-mcp/cache/viewer/
cp -r <wasm构建产物>/. ~/.wave-mcp/cache/viewer/wasm/
```

合法的资产目录包含可执行的 `surver` 和 `wasm/index.html`。放在 `~/.wave-mcp/cache/viewer/` 即被自动发现，或用 `WAVE_MCP_VIEWER_ASSETS` 指向任意目录（绝对路径）。隔离网环境：在联网机器构建后把资产目录拷入，随离线 bundle 的 `--viewer <资产目录>` 参数打包进你自己的内部介质（注意：这属于你所在组织的内部复制，若再对外分发需自行满足 EUPL-1.2 义务，包括随附许可全文与源码地址）。

验证：启动 wave-mcp 后调用 `open_wave_view`，或直接 `wave-view <fst文件>`。资产缺失或不完整时，工具会返回指向本文档的错误提示。

## 2. fsdb2fst（FSDB 转换器）

FSDB 读取依赖 Synopsys Verdi 的 FsdbReader 商业库，因此 `fsdb2fst` 只能在你本机构建。转换器源码（Apache-2.0，保留 TraceWeave 的 MIT 来源与署名）随包携带，首次分析 `.fsdb` 文件时自动触发构建，一般无需手动操作。

前置条件：本机装有 Verdi（`$VERDI_HOME` 或 `$NOVAS_HOME` 已设置，或用 `FSDB2FST_FREADER` 指向 `share/FsdbReader` 目录）、gcc、zlib 头文件。

```bash
# 通常不需要手动跑：convert 管线首次遇到 .fsdb 会自动构建
deploy/build_fsdb2fst.sh
```

产物 `fsdb2fst` 落在 `~/.wave-mcp/cache/fsdb2fst/`，是本地构建产物：不 commit、不进任何分发包。Synopsys 库始终由你自备，wave-mcp 不携带任何 Verdi 文件。细节见 [FSDB_GUIDE.md](FSDB_GUIDE.md)。

## 3. fstdumper（Xcelium VPI 插件）

让 Xcelium（xrun）在仿真时直接 dump FST 的 GPL-3.0 上游插件。wave-mcp 不分发其源码或二进制，只随包携带两份修复补丁（GPL-3.0，见 `third_party/fstdumper/`）。

前置条件：自行 clone 上游 `https://github.com/semify-eda/fstdumper`（版本见 `docs/licenses/fstdumper.SOURCES.json` 的 pin commit）、gcc、make、patch、zlib 头文件；在跑 xrun 的同一环境构建。

```bash
git clone https://github.com/semify-eda/fstdumper /path/to/fstdumper
bash deploy/build_fstdumper.sh /path/to/fstdumper          # 可加 --perf-opt
```

产物 `fstdumper.so` 留在你的 checkout 目录，由 xrun 在仿真时加载，与 wave-mcp 进程无链接关系。集成流程见 [XCELIUM_FST_GUIDE.md](XCELIUM_FST_GUIDE.md)。

## 常见问题

**为什么不直接 pip 装 viewer？** Surfer/surver 是 EUPL-1.2（强 copyleft）组件，与核心包的宽松许可定位不兼容，且 EUPL 的传染边界在法律上存在不确定性。停止代为分发后，你本机构建的资产只在你的环境内使用，不产生我们的再分发义务，也让核心包的许可审计干净。

**构建一次要多久？** viewer 资产约 15-30 分钟（docker 内 Rust 编译）；fsdb2fst 秒级；fstdumper 秒级。三者都是一次构建长期使用。

**旧版本装过 `wave-mcp[viewer]` 的怎么办？** 已安装的资产包可继续使用（资产查找顺序不变，pip 包仍会被发现），也可以卸载后按本文自建。
