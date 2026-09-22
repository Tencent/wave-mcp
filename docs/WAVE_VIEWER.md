# 波形查看器（wave-view）完全指南

[English version](WAVE_VIEWER.en.md)

wave-mcp 的分析工具回答"为什么错"，波形查看器负责"让你亲眼看到"。agent 定位到出错时刻后，一条 `open_wave_view` 就能在你的浏览器里弹出波形：嫌疑信号已经加好，游标钉在出错时刻，旁边的弹窗里是它的分析说明。agent 还能通过 `update_wave_view` 原地追加分析、移动游标，通过 `get_view_state` 确认页面已连通、状态已送达，接着往下聊。

本文覆盖查看器的安装、CLI、三个 MCP 工具、agent 工作流、架构与原理、部署场景和故障排查。快速上手看 [README](../README.md) 的"波形查看器"一节即可，这里是全量细节。

## 目录

1. [能做什么](#1-能做什么)
2. [安装](#2-安装)
3. [CLI 用法（wave-view）](#3-cli-用法wave-view)
4. [MCP 工具详解](#4-mcp-工具详解)
5. [agent 典型工作流](#5-agent-典型工作流)
6. [分析说明弹窗与时间锚点](#6-分析说明弹窗与时间锚点)
7. [双波形对比视图](#7-双波形对比视图)
8. [无闪更新机制](#8-无闪更新机制)
9. [架构与原理](#9-架构与原理)
10. [部署场景](#10-部署场景)
11. [许可说明](#11-许可说明)
12. [故障排查](#12-故障排查)
13. [已知限制](#13-已知限制)

## 1. 能做什么

- **秒开几十 GB 的 FST**：波形数据不进浏览器，由本机 surver 进程流式供给，浏览器只取屏幕上正在显示的那部分。打开速度和文件大小基本无关。
- **agent 驱动的呈现**：信号列表、游标位置、可视时间窗、时间轴 marker、分析说明，全部由 agent 通过 MCP 工具设置，用户打开链接即所见。
- **双波形对比**：pass/fail 两份波形上下两栏，agent 设置的缩放与游标同步注入两栏，配合 `diff_waveforms` 的首分歧结果自动打红色 marker。
- **送达确认**：`get_view_state` 回传页面连通状态和已应用版本，agent 能确认用户确实看到了最新状态。
- **无闪更新**：agent 后续调整游标/窗口/marker/说明时页面不刷新，波形不重载。

查看器是可选能力：没装资产包时五个 viewer 工具返回清晰的提示并优雅降级，其余 32 个分析工具完全不受影响。

## 2. 安装

查看器需要一个独立的资产目录（Surfer WASM 前端 + surver 流式后端，EUPL-1.2）。**wave-mcp 不分发这些资产**：按 [SELF_BUILD.md](SELF_BUILD.md) 用仓库内脚本自行构建，一次构建长期使用，团队可共享同一份。

资产按以下顺序查找，找到即用：

1. 环境变量 `WAVE_MCP_VIEWER_ASSETS` 指向的目录
2. pip 安装的 `wave-mcp-viewer-assets` 包（历史安装仍被识别，新版不再提供）
3. 用户缓存目录 `~/.wave-mcp/cache/viewer/`（自建资产的推荐位置）

一个合法的资产目录包含可执行的 `surver` 和 `wasm/index.html`。隔离网环境用 `deploy/build_offline_bundle.sh --viewer <资产目录>` 打进离线包即可，细节见 [DEPLOY_AIRGAP.md](DEPLOY_AIRGAP.md)。

**系统要求**：资产包内的 surver 是 musl 静态构建，不依赖 glibc，任何 x86-64 Linux（含 CentOS 7 这类老机器）都能直接跑。glibc 2.17 档离线 bundle 的存在原因是 `pyslang`，与 surver 无关。浏览器侧只要支持 WASM 的现代浏览器即可，机房服务器本身不需要图形环境。

## 3. CLI 用法（wave-view）

不经过 agent，人工也能直接开波形：

```bash
# 打开单个波形
wave-view dump.fst

# 带初始信号和游标位置
wave-view dump.fst --signals top.clk top.u_dma.req_valid --cursor 1523400ps

# 双波形对比
wave-view pass.fst fail.fst --labels pass fail

# 只打印 URL，不尝试拉起浏览器
wave-view dump.fst --no-browser

# VCD / FSDB 直接传，自动转 FST 后打开
wave-view sim.vcd
```

参数说明：

| 参数 | 含义 |
| --- | --- |
| `fst`（位置参数） | 1 个或 2 个波形路径，2 个进入对比视图。`.fst` 直接打开，`.vcd` / `.fsdb` 自动转成 FST |
| `--signals` | 初始加入的信号完整路径，空格分隔多个 |
| `--cursor` | 初始游标时刻，格式 `数字+单位`，如 `1523400ps`、`12ns` |
| `--labels` | 每个波形的显示标签，对比视图建议 `pass fail` |
| `--no-browser` | 不尝试本机拉起浏览器 |

转换产物带缓存，和 `prepare_session` 共用同一份 FST：同一个波形不管是先分析后看图，还是先看图后分析，都只转一次。缓存统一落在 `~/.wave-mcp/cache/fst/`（受 `WAVE_MCP_CACHE_ROOT` 影响），不写源文件目录，只读的回归目录也能用。GB 级 FSDB 转换耗时以分钟计，命中缓存则是秒级。

`.fst` / `.vcd` / `.fsdb` 以外的格式（如 `.ghw`、`.vpd`、SHM 目录）会在入口直接报错并列出支持的扩展名，不会落到转换器里报一个指向错误方向的问题。

命令会打印三行信息：浏览器 URL、原生 Surfer 客户端连接方式（`surfer <token_url>`）、SSH 端口转发命令。桌面环境（有 `DISPLAY`）自动用 `xdg-open` 拉起浏览器；SSH / code agent 场景下 VS Code、Cursor 等 IDE 终端会自动转发 localhost 端口，直接点 URL 即可。进程前台常驻，Ctrl-C 退出并回收全部子进程。

## 4. MCP 工具详解

### 4.1 open_wave_view

打开一个（或一对）波形视图，返回 URL 给用户。

```jsonc
open_wave_view({
  "fst_paths": ["sim/fail.fst"],            // 1 个普通视图，2 个对比视图
                                            // .fst 直接打开，.vcd / .fsdb 自动转 FST
  "signals": [                              // 初始信号
    {"path": "top.u_dma.req_valid", "color": "red"},
    {"path": "top.u_dma.grant", "group": "handshake"}
  ],
  "cursor":   {"time": "1523400", "unit": "ps"},   // 游标钉在出错时刻
  "viewport": {"from": "1500000", "to": "1600000", "unit": "ps"},
  "markers":  [{"time": "1523400", "unit": "ps", "label": "req 无 grant", "color": "red"}],
  "annotation": {                           // 分析说明，进弹窗
    "markdown": "在 [1523400ps](#t=1523400ps) req_valid 拉高但 grant 缺失…",
    "confidence": "high",
    "evidence": ["signal_fanin: …", "active_drivers: …"]
  }
})
```

返回：

```jsonc
{
  "available": true,
  "view_id": "a1b2c3d4",        // 后续 update / get_state 用
  "url": "http://127.0.0.1:NNNNN/view.html?token=…",
  "native_hint": "surfer http://127.0.0.1:MMMMM/TOKEN",   // 原生客户端连法
  "ssh_hint": "ssh -L NNNNN:localhost:NNNNN <this-host>"  // 手动转发命令
}
```

字段要点：

- `fst_paths` 每项可以是 `.fst`、`.vcd` 或 `.fsdb`，最多两份（一份普通视图，两份对比视图）。FST 直接打开，VCD / FSDB 先转成 FST 再打开，转换产物带缓存并与 `prepare_session` 共用，所以分析阶段已经转过的波形在这里不会再转一遍。其他扩展名（`.ghw`、`.vpd`、SHM 目录等）在入口直接返回 `{"status": "error", "error_type": "unsupported_format", "error": …, "hint": …}` 并列出支持的格式。
- `signals` 每项 `{path, color?, group?, format?, source?}`；对比视图里用 `source: "a"/"b"` 指定信号属于哪份波形，缺省两边都加。
- `group` 建议用简短的 ASCII 词。分组标题画在 Surfer 的 WASM 画布里，字体不含 CJK 字形，写中文会显示成方块（分组本身照常生效）。名字里的空格会自动折成下划线，因为 sucl 解析器不接受带空格的参数，原样发过去整条分组标题会被静默丢弃。想写中文说明放到 `annotation` 里，那里不限语言。
- `diff` 参数直接接 `diff_waveforms` 的结果引用 `{source_a, source_b, first_divergence}`（只认这三个字段），自动在首分歧时刻打红色 marker，不用手动换算；`first_divergence` 原样回填即可，`"85ns"` 这类带后缀的时间会被规范化。
- `labels` 给每份波形起显示名，与 CLI 的 `--labels` 一致；数量必须与波形数一致。
- 时间字段是严格的 `{"time": "1523400", "unit": "ps"}` 对象：`time` 为整数字符串（`"1523400ps"` 这种带单位后缀的写法同样接受并自动规范化），`unit` 取 `s / ms / us / ns / ps / fs`，缺省 `ps`；`viewport` 用 `{from, to, unit}`。未知字段、未知单位、后缀与 `unit` 冲突、非整数的时间值都会被拒绝并指出正确写法，不会静默忽略或落到默认值。
- 失败时都返回 `status: "error"` 和 `error_type`，不抛错：参数写错是 `invalid_argument`，附 `parameter`（哪个参数）、`did_you_mean`（能给出时）、`expected` / `example`（正确写法）；波形文件问题分别是 `file_not_found` / `unsupported_format` / `conversion_failed`；功能不可用才返回 `available: false` 加 hint（资产缺失 `viewer_unavailable`、surver 起不来 `surver_error`）。
- 打开或更新时，若有命令因故未能生成，返回值里会带 `warnings` 列表说明丢弃了什么、为什么，不再静默。

### 4.2 update_wave_view

原地更新已打开的视图，同一 URL，用户侧无刷新：

```jsonc
update_wave_view({
  "view_id": "a1b2c3d4",
  "cursor": {"time": "1531200", "unit": "ps"},
  "annotation": {"markdown": "进一步看 [1531200ps](#t=1531200ps)，grant 被 arb_mask 压住…"}
})
```

省略的参数保持现状；`signals` / `markers` 传入即整体替换；`annotation` 是例外，追加到分析日志弹窗末尾，形成一条时间线式的分析记录。返回递增的 `revision`，可用来确认前端是否已应用。

### 4.3 get_view_state

读取视图的送达状态：

```jsonc
get_view_state({"view_id": "a1b2c3d4"})
// 返回
{
  "available": true,
  "revision": 7,
  "actual": {                       // 浏览器实际回写
    "applied_revision": 7,          // 前端已应用到的版本
    "page_ready": true,             // 页面是否连通了波形后台
    "page_error": null
  },
  "desired_summary": {…}            // agent 侧期望状态摘要，便于对照
}
```

典型用法：`open_wave_view` / `update_wave_view` 之后确认页面确实连上了波形后台（`page_ready`），以及前端是否已应用最新状态（`applied_revision` 对比 `revision`）。页面空白或不可达时 `page_ready` 为 false，原因在 `page_error` 里。

> 出于与 EUPL 查看器的许可隔离，shell 页面不再从查看器应用内读取状态，`actual` 中不再回传用户游标位置和 `user_dirty`。

### 4.4 list_wave_views / close_wave_view

视图打开后会一直保留，直到显式关闭。交互式调试基本不用管，但批量场景（比如回归扫一批 case，每个都开一个视图）需要收尾，否则视图和后台流式进程会一直累积。

```
list_wave_views()
→ {"count": 2, "max_views": 8,
   "views": [{"view_id": "a1b2c3d4", "url": "...", "title": "uart case",
              "fst_paths": ["/path/fail.fst"], "revision": 3,
              "surver_alive": true}, ...]}

close_wave_view({"view_id": "a1b2c3d4"})
→ {"closed": "a1b2c3d4", "surver_stopped": true, "remaining": 1}

close_wave_view({"all_views": true})     # 批量收尾
```

两点行为值得知道：

一是流式后台按波形文件集共享。两个视图看同一份波形时只有一个后台进程，关掉其中一个不会影响另一个，只有最后一个使用者关闭时才真正停止，返回里的 `surver_stopped` 告诉你有没有停。

二是有个兜底上限。默认最多同时开 8 个视图，超了会自动关掉最老的那个，避免长时间批量跑把资源耗尽。用 `WAVE_MCP_MAX_VIEWS` 调整，设 0 表示不限制。被淘汰的视图页面本身不会有提示，所以 `open_wave_view` 的返回会带上 `evicted_view_id`（一次淘汰多个时带 `evicted_view_ids`），方便你告知使用者或按需重开。

### 4.5 端口配置

默认每个视图用两个随机高位端口（页面服务 + 波形流后台）。本机直接开浏览器时这样最省事，不用操心端口冲突。

但如果你在远端跑、靠 `ssh -L` 转发，随机端口会有点烦：端口每次都变，没法预先配好一条固定的转发规则。这时可以指定一个端口基址，视图就会被限制在 `[base, base+64)` 这个区间里：

```bash
export WAVE_MCP_VIEWER_PORT_BASE=45400
ssh -L 45400:localhost:45400 -L 45401:localhost:45401 <host>
```

区间给到 64 是因为每个视图占两个端口，默认 8 个视图的上限绰绰有余。区间内被占满时会自动退回随机端口，不会因为端口不够就打不开视图。基址非法（非数字、小于 1024、超出范围）时同样退回随机端口。

## 5. agent 典型工作流

**场景一：单波形根因呈现**

```
case 挂了
→ prepare_session(fail.fst, …)
→ trace_x / signal_fanin / active_drivers 定位根因时刻与信号
→ open_wave_view(fail.fst, 嫌疑信号 + cursor 钉出错时刻 + annotation 分析说明)
→ 用户打开 URL，所见即结论
```

**场景二：pass/fail 对比闭环**

```
同一 case 一好一坏两份波形
→ diff_waveforms([pass.fst, fail.fst], scope="top.u_dma", clock="top.clk")
    返回首分歧时刻 + 最早分歧信号（真凶候选，晚分歧多为下游传染）
→ signal_fanin / active_drivers 对最早分歧信号做因果回溯
→ open_wave_view([pass.fst, fail.fst], diff=…, annotation=…)
    双栏对比 + 分歧处红 marker + 结论弹窗，一次呈现
```

**场景三：对话式调试**

```
用户在视图里拖游标翻看
→ 用户："1544800ps 这里 grant 怎么是 0？"（时刻从波形界面读出报给 agent）
→ active_drivers(grant, t=1544800ps) 判断哪条驱动语句在起作用
→ update_wave_view 追加 annotation（带 [1544800ps](#t=1544800ps) 锚点）+ 补 marker
→ 用户点锚点，游标跳到该时刻，所见即结论
```

## 6. 分析说明弹窗与时间锚点

annotation 渲染在波形旁的日志弹窗里，可收起为悬浮胶囊，不遮挡波形。每条 annotation 支持：

- `markdown`：正文，支持基本 Markdown。
- `confidence`：置信度标注（如 high / medium / low），帮用户判断结论强度。
- `evidence`：证据列表，通常放分析工具的关键返回摘要。

正文里的时间引用写成锚点格式 `[85000ps](#t=85000ps)`，渲染成可点链接，点一下游标直接跳到那个时刻。这让分析说明变成可交互的导航目录：用户顺着 agent 的推理链一步步点过去，每一步波形都跟着走。

`update_wave_view` 的 annotation 是追加语义，多轮分析自然形成一份带时间戳的调试日志，全程留在弹窗里可回翻。

## 7. 双波形对比视图

`fst_paths` 传两个路径即进入对比视图：上下两栏各显示一份波形，agent 设置的缩放、游标、marker 同步注入两栏，指向同一时刻，肉眼对齐毫无负担。（出于与 EUPL 查看器的许可隔离，shell 不读取查看器内部状态，用户在单栏里的手动缩放不会自动带动另一栏；让 agent 更新视窗即可两栏同步。）

- `labels` 建议明确传 `["pass", "fail"]`，两栏标题一目了然。
- `signals` 缺省在两栏加同名信号；单独指定 `source` 可以只加某一侧。
- `diff` 参数接 `diff_waveforms` 结果后自动在首分歧时刻打红 marker，两栏同时可见。
- 两份波形由同一个 surver 进程服务（按文件集合复用），内存开销不随视图数线性增长。

## 8. 无闪更新机制

agent 调 `update_wave_view` 后，用户侧页面不刷新，波形不重载，具体分两类通道：

- **游标 / 视窗 / marker**：通过 Surfer 的运行时消息注入直接下发，波形画布原地移动，零闪烁。
- **信号增删**：走 Surfer 启动命令层重建信号列表，弹窗与视图状态保留。
- **annotation**：只进日志弹窗，完全不触碰波形。

实现上，前端 shell 对 `/api/view-state` 做长轮询（25 秒挂起，变更即返回），拿到 desired 状态增量后按上述通道分发；同时定期把页面连通状态和已应用版本回写到 `/api/view-state/actual`，这就是 `get_view_state` 读到的 `actual`。出于与 EUPL 查看器的许可隔离，shell 只通过页面加载参数和标准 postMessage 与查看器通信，不读取查看器内部状态。

## 9. 架构与原理

```
open_wave_view / wave-view CLI
        │
        ▼
ViewManager（单例，view_id 注册表）
        │ 每个视图
        ├── SurverInstance：surver 子进程，只绑 127.0.0.1，随机高位端口 + 随机 token
        │     波形数据流式服务，按 FST 文件集合复用实例
        ├── ViewerServer：本地 HTTP，托管 shell 前端 + Surfer WASM，
        │     反向代理 surver，提供 /api/view-state（GET 长轮询 / PUT desired / POST actual）
        └── ViewState：desired（agent 意图）与 actual（浏览器回写）双状态 + revision
```

关键设计取舍：

- **波形不进浏览器**。Surfer WASM 只做渲染，数据由 surver 按需流式供给，浏览器内存占用与文件大小解耦，这是几十 GB FST 秒开的根本原因。
- **只面向 Surfer 的稳定命令层**（startup commands）加运行时消息注入，刻意不依赖 GUI 内部结构，将来升级 Surfer 版本时适配面最小。
- **安全默认**：surver 与 ViewerServer 都只监听 127.0.0.1，URL 带随机 token，远程访问显式走 SSH 端口转发，不存在裸端口暴露。
- **生命周期与宿主绑定**：MCP server 或 CLI 进程退出时所有 surver 子进程统一回收，不留孤儿进程。
- **优雅降级**：资产缺失或 surver 启动失败返回结构化的 `available: false` 加可执行 hint（`viewer_unavailable` / `surver_error`）；view_id 不存在返回 `unknown_view`，参数写错返回 `invalid_argument` 并附修正建议；分析工具永不受牵连。

代码位置：[wave_mcp/viewer/](../wave_mcp/viewer/)（`manager.py` 视图编排、`surver.py` 子进程管理、`server.py` 本地 HTTP、`state.py` 双状态模型、`translate.py` Surfer 命令翻译、`web/` 前端 shell），MCP 工具注册在 [wave_mcp/server.py](../wave_mcp/server.py)。

## 10. 部署场景

**本机桌面**：有 `DISPLAY` 时 CLI 自动 `xdg-open` 拉起浏览器，MCP 工具返回 URL 由 agent 转述。

**SSH / 远程开发（最常见）**：VS Code、Cursor 等 IDE 的集成终端自动转发 localhost 端口，agent 给出 URL 直接点开即可。纯 SSH 终端用返回值里的 `ssh_hint` 命令手动转发：

```bash
ssh -L <port>:localhost:<port> <server>   # 然后本机浏览器打开 URL
```

**原生 Surfer 客户端**：本机装了 Surfer 桌面版的话，用返回值里的 `native_hint`（`surfer <token_url>`）直连 surver，体验与浏览器一致。

**隔离网**：离线 bundle 加 `--viewer` 参数把资产打进包，安装时自动设置 `WAVE_MCP_VIEWER_ASSETS`；老机器用 musl 静态 surver。见 [DEPLOY_AIRGAP.md](DEPLOY_AIRGAP.md)。

**多人共用一台机器**：每人在自己账号下各起一份 wave-mcp，不要多人共享同一个 server 进程。视图和 surver 都绑在回环上，各自进程互不可见，隔离直接由账号和文件权限保证，不需要额外配置。

唯一需要约定的是端口。默认随机端口本身不会冲突，但如果大家都想用固定端口做 `ssh -L` 转发，就得给每人分一段互不重叠的区间（每段 64 个，一人一段）：

```bash
# A 用户 ~/.bashrc
export WAVE_MCP_VIEWER_PORT_BASE=45400   # 占用 45400-45463
# B 用户 ~/.bashrc
export WAVE_MCP_VIEWER_PORT_BASE=45500   # 占用 45500-45563
```

另有两点值得注意。一是转换缓存写在每个用户自己的 `~/.wave-mcp/cache/` 下，不写源波形目录，所以多人分析同一份回归波形时各转一份；想共享一份产物，把 `WAVE_MCP_CACHE_ROOT` 指到同一个可写目录即可，同键并发转换只会构建一次。二是 `WAVE_MCP_MAX_VIEWS` 是每进程的上限，不是整机上限，机器上同时有几个人在用时，留意总的浏览器与 surver 进程数。

如果你要的是"主机起一个 server、把链接分发给其他人连"，当前版本还不支持，viewer 只绑回环。这个形态在规划中。

## 11. 许可说明

wave-mcp 核心（含 viewer 的 Python 编排层与前端 shell）是 Apache-2.0 许可。Surfer WASM 与 surver 二进制来自 [Surfer 项目](https://surfer-project.org/)，许可为 EUPL-1.2，**不随 wave-mcp 分发**，需按 [SELF_BUILD.md](SELF_BUILD.md) 在本机自建。shell 与 viewer 仅通过页面加载 URL 参数和标准 postMessage 通信。不装资产，核心功能零缺失。构建复现路径与完整法律口径见 [THIRD_PARTY.md](THIRD_PARTY.md)。

## 12. 故障排查

**工具返回 "viewer assets not found"**
按 [SELF_BUILD.md](SELF_BUILD.md) 自建资产，然后设置 `WAVE_MCP_VIEWER_ASSETS` 指向资产目录，或把资产放到 `~/.wave-mcp/cache/viewer/`。确认目录里有可执行的 `surver` 和 `wasm/index.html`。

**surver exited early / did not become ready**
先手动跑一下 `<资产目录>/surver --help` 看能否执行。资产包内的 surver 是静态构建、不依赖 glibc，所以执行失败通常不是库版本问题：优先检查文件是否丢了可执行位（`chmod +x`），以及是否被自建脚本换成了动态链接的版本（`file surver` 应显示 static-pie）。其次检查 FST 路径是否存在、文件是否完整。

**URL 打不开（远程场景）**
服务只监听 127.0.0.1，属预期行为。IDE 终端一般自动转发；不行就用返回的 `ssh_hint` 手动建立端口转发，再从本机浏览器访问。需要转发的是页面 URL 的那个端口（`view.html` / `shell.html` 所在的端口）；波形流由页面同源代理，surver 的端口不需要单独转发，两个都转发也不会有副作用。

**打开后白屏，或长时间停在加载画面**
入口页面对浏览器 Service Worker（用于修正经网关转发时被改写的响应头）的等待有上限：约 4 秒后无论 worker 是否就绪都会进入查看器，不再无限等待。查看器加载后若探测到波形后端不可达，会先自动重试一次（等后台 worker 接管后重新加载），仍失败则显示明确原因与处理建议，而不是留一个空白页。遇到白屏时：
- 先刷新一次；多数环境会在自动重试后恢复。
- 若页面提示后端不可达，检查端口转发是否建立、MCP server 进程是否存活（进程退出后 URL 即失效，需重新 `open_wave_view`）。
- 个别嵌入式浏览器禁用后台 worker（Service Worker）且又经网关访问时无法自动修复，建议改用常规浏览器标签页打开。

**页面开了但没有波形**
确认访问的是带 `?token=` 的完整 URL；token 不对 surver 会拒绝供数。若浏览器控制台报 WASM 加载失败，检查资产与 wave-mcp 钉定的 Surfer 版本是否匹配（按 [SELF_BUILD.md](SELF_BUILD.md) 从 `deploy/viewer-pin.sh` 的 pin 重建）。
另外，请求的信号名在波形中不存在时，`open_wave_view` / `update_wave_view` 会在返回的 `warnings` 里列出，页面顶部也会显示同样的提示；这类信号 Surfer 会静默忽略，不是页面故障。完全没有指定信号时，页面同样会提示"尚未选择信号"。

**update_wave_view 报 unknown view_id**
view 的生命周期跟随打开它的进程。MCP server 重启后旧 view_id 失效，重新 `open_wave_view` 即可；返回值里的 `known_views` 列出当前有效的视图。

## 13. 已知限制

- 信号增删走启动命令层重建，量大时有一次可感知的列表重载（游标/视窗/marker 更新始终无闪）。
- 出于与 EUPL 查看器的许可隔离，shell 不读取查看器内部状态：`get_view_state` 不回传用户游标位置和 `user_dirty`，对比视图两栏不随用户手动缩放自动联动（agent 更新视窗即可同步）。
- 对比视图目前支持 2 份波形，3 份及以上未支持。
- Surfer 版本升级可能改变其内部消息编码（BigInt 序列化格式）与命令集，资产包与核心版本需配套升级；我们只依赖其稳定命令层，适配成本可控但不为零。
- 浏览器窗口极窄时（约 460px 以下），Surfer 会压缩波形区导致波形不可见；这是上游查看器的布局行为，把窗口或 IDE 分屏面板拉宽即可恢复，其余功能不受影响。
- 浏览器端渲染上限取决于 Surfer WASM 本身，单屏同时展开数千条信号时建议分组折叠。

---

反馈问题请到 [GitHub Issues](https://github.com/Tencent/wave-mcp/issues)。
