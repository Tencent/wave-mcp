# Changelog

All notable changes to wave-mcp are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.1] - 2026-09-22

### 中文

#### 修复

- `install.sh` 安装到共享盘（NFS）前缀时改为原子替换并加安装锁：先整树staged 复制再 rename 换入，旧树留待下次安装清扫；`flock` 拒绝并发安装；检测到本机仍有运行中的 wave-mcp 时提示其重启后生效。此前在有进程运行的共享前缀上覆盖安装可能中途失败并留下损坏的安装。

#### 变更

- 两个项目级验证脚本（`tests/functional_verify.py`、`tests/full_quality_check_all_tools.py`）入库并随离线 bundle 分发，装机后可直接跑功能验证。

### English

#### Fixed

- `install.sh` now performs atomic swaps with an install lock for shared (NFS) prefixes: new trees are staged then renamed into place, stale trees are swept by the next install, `flock` rejects concurrent installs, and a notice is printed when running wave-mcp processes are detected on the host. Previously, overwriting a live shared prefix could abort mid-install and leave a corrupted tree.

#### Changed

- Two project-level verification scripts (`tests/functional_verify.py`, `tests/full_quality_check_all_tools.py`) are now tracked and shipped in offline bundles for post-install functional checks.

## [1.0.0] - 2026-09-22

本段先中文后英文。1.0 之后的每个版本都按这个顺序写。

### 中文

**兼容承诺**：从 1.0 起，工具名、参数名、`_fp` 的形状和 `session.json` 的形状在 1.x 内不再做破坏性变更，要破坏就等 2.0。暂时不能跟进的用户可以钉 `wave-mcp<1.0`。

#### 破坏性变更

**工具面收拢，参数名统一。** 合掉六个工具；同一概念在不同工具里曾有多套拼法（时间三种、路径四种），每次换工具 Agent 都得重读 schema。不留别名：MCP 客户端每个会话重读 schema，CLI 从签名派生参数，两者都会自己跟上改名，而别名会让每一次成功调用永远为一次罕见的旧调用付费。写死旧名的调用方会收到指明替代者的报错，而不是一个不透明的失败。合并与改名对照表见下方英文段的两张表。文件系统路径保持自己的命名空间（`wave_path`、`vcd_path`、`fsdb_path`、`session_path`、`fst_paths`、`out_path`），裸 `path` 永远指设计层次，不指磁盘文件。

第二轮改名把同样的「一概念一名字」用到准备与转换工具上：`expected_revision` 改 `defaults_revision`；`fsdb_scopes`/`fsdb_signals_file` 改 `scopes`/`signals_file`；`mode` 改 `pack`，取值用压缩器本名 fastlz/lz4/zlib；两个转换工具的输出 `fst_path` 改 `out_path`，`prepare_session` 不再接受 `fst_path`（转换产物进用户缓存）；`max_signals`/`max_scopes` 改 `limit`；`levels` 改 `max_depth`；`transitive` 并入 `max_depth`（1 只看直接一层，更大则跨层跟随，上限 8）；`filter_by_name`/`filter_by_type` 改 `name_contains`/`signal_type`；`parallel` 删除。

**未声明的参数和退役的工具名在线上直接拒绝，并给出替代名。** SDK 原来会丢掉工具未声明的键，`transitive=True` 会得到一个单跳的回答而没有任何提示；退役工具名只会收到裸的「Unknown tool」。现在两者都在 SDK 公开的 `call_tool` 入口按公开 schema 检查：未声明的键拒绝，退役参数名或工具名拒绝并在消息里给出现名，不碰 SDK 内部。

**执行有界。** 每次工具调用经过准入门：同时最多 `WAVE_MCP_WORKERS`（4）个在跑、每用户 `WAVE_MCP_PER_OWNER_RUNNING`（2）个；其余进容量 `WAVE_MCP_QUEUE_CAPACITY`（32）的队列按用户公平轮转，最多等 `WAVE_MCP_QUEUE_TIMEOUT`（30 秒），超出返回结构化的 `server_busy`/`queue_timeout`，不会无声挂起。每用户最多 `WAVE_MCP_PER_OWNER_SESSIONS`（16）个会话；空闲 `WAVE_MCP_SESSION_TTL`（1800 秒）且无在途请求的会话由后台回收，共享数据随最后一个引用释放。SIGTERM/SIGINT 时停止准入、拒绝等待者、给在跑的调用宽限期并回收自己的 viewer 子进程。`session_info(list_sessions=True)` 带 `server` 块报占用与上限。已开始的调用绝不中断；转换子进程独立进程组，超时只回收它和它 fork 的东西。

**HTTP 要让别的机器连过来必须设 `WAVE_MCP_TOKEN`。** wave-mcp 以启动它的 OS 账号运行，自身没有用户模型，能读写什么由操作系统决定。HTTP 监听唯一多出来的风险是同机或同网段的其它进程能连到它，`WAVE_MCP_TOKEN` 堵这一点：设了以后每个请求必须带 `Authorization: Bearer <同一值>`，否则在任何工具执行前返回 401；不设则绑非回环 `--host` 拒绝启动，报错直接写明要设哪个变量。只绑回环不带 token 与 stdio 都和以前一样。一台机器服务多人就每人一个进程（或一个替他们起进程的前门），不要一个进程假装是所有人。每个工具现在都登记数据访问类别（session/path/view/server），未登记就注册是启动错误。

**`out_dir` 可选，且永不改写。** `prepare_session`、`open_static_session`（以及 `wave-session --out`）不再要求会话目录。不传时落到会话根（`$WAVE_MCP_SESSION_ROOT`，默认 `~/.wave-mcp/sessions`）下以输入身份命名的目录，同一份设计从哪里调都同址。缺省落点的波形会话把网表放在其 RTL 源码的身份目录，也就是同源静态会话的位置：先探索、后加波形、复用一次精化，不需要记任何目录。传了 `out_dir` 就原样使用，静默的 `<root>/<basename>-<digest>` 重映射删除。`prepare_session` 的首个位置参数改为 `wave_path`。

**派生文件搬出你的目录。** 自动转换（`prepare_session` 等触发）转出的 `.fst` 以前写在源波形旁边（只读时退到缓存目录），`maps.json.msgpack` 写在每个会话网表旁边。现在都在 `$WAVE_MCP_CACHE_ROOT`（默认 `~/.wave-mcp/cache`）的 `fst/` 与 `netlist-cache/` 下，和已有的 `fsdb2fst/`、`viewer/` 并列。输入所在目录零写入，只读回归区直接可用，缓存原子写，两个进程转同一波形只构建一次。按需转换工具（`convert_vcd_to_fst`/`convert_fsdb_to_fst`）不传 `out_path` 时也统一落这一缓存根（`~/.wave-mcp/cache/fst/`），「派生文件不写入输入目录」的承诺覆盖全部转换路径，要写到别处就显式传 `out_path`。`XDG_CACHE_HOME`/`XDG_DATA_HOME` 不再参与解析，wave-mcp 写盘的一切默认位置都在 `~/.wave-mcp/` 一个目录下，找得到也删得掉；`WAVE_MCP_SESSION_ROOT`/`WAVE_MCP_CACHE_ROOT` 语义不变，显式设置的环境不受影响。`session.json` 记录会话依赖的缓存（`caches[]`），`session_info` 报每项是否仍是构建时的版本。旧产物不迁移不删除。

`session.json` 的 `fst_hash`/`filelist_hash`（整文件 SHA-1）换成 `fst_version`（全包统一的 16 位 size+mtime+头样本摘要），旧清单照常打开。查询回复的 `_fp` 改为 `{dataset: {identity, version, wave, netlist}, query}`：`identity` 说哪份设计，`version` 说哪一版。

随合并而来的行为变化：`signal_values` 按批回答，值放在 `signals[i].values`，多个信号一遍扫完；`limit` 对超量结果做均匀降采样（保留首末变化）而不是截断前缀，回复带 `sampled`、`sample_rate`、`total_available`、`resume_from`（把它作为 `start` 回传即可全分辨率补看被抽稀的细节，它与首个保留区间重叠，不是分页游标）；`name_contains` 在波形会话里也按实例叶名过滤；`diff_waveforms` 比较 N 个 run，`fst_paths` 是列表，每个分歧信号带 `groups`（按取值划分的 run 下标），两 run 时仍有可读的 `value_a`/`value_b`。

#### 新增

- `sample_at_clock`：按周期出表而不是变化列表；只有干净的 0→1（或 1→0）时钟跳变算边沿。
- `fsm_transitions`：寄存器实际发生的状态转移（计数、首次出现）与各赋值分支的守卫是否成立过。明确不是覆盖率，不报「缺口」；守卫不可判定的转移报 `undecided_at`。
- `signal_downstream`：前向可达，`signal_fanin` 的镜像，跨层次跟随；带 `time` 时报各下游信号在该时刻后的首次变化，标为相关而非因果。
- `fold_transactions`：按你定义的条件把活动折成事务记录。不内置任何协议表；条件按边沿处理（前一刻为假、此刻为真才算，从 x/z 出来不算）；窗口末尾仍开着的事务保留并标 `incomplete: true`；没有可配对开启的结束边沿报 `unmatched_end` 而不是丢弃。
- 查询默认值 `query_defaults_set`/`query_defaults_get`/`query_defaults_clear`：为后续调用存一组默认信号与时间窗。查询工具可省略 `paths`/`start`/`end`，显式参数永远优先，用了默认值的回复在 `_query.from_defaults` 里列出继承了哪些字段；`"min"`/`"max"` 仍是显式请求。多信号默认集不给单信号工具供值，而是报歧义。默认值按会话存放，带 `defaults_revision`，查询可钉住一个修订号，默认值被改动则回 `defaults_conflict`。只存波形坐标，不存假设。
- `signal_activity`：一遍扫文件给出窗口内每个信号的翻转数、x/z 时间占比、恒定标志、首末变化与两端取值。
- `find_time_windows`：布尔条件成立的时间区间，条件用 `expr_eval` 表达式对象（与网表守卫同一结构）。只在所引用信号的变化点求值；不可判定的时间单独返回，不当成命中；窗口末尾仍为真的区间标 `open_ended`。
- 回复指纹 `_fp`：数据集身份与版本加有效问题的摘要。四种问法（显式/默认、`10ns`/`10000ps`、字符串/单元素列表）得到同一个 `query` 摘要。成功回复另带 `_query`（可读形式的有效参数）。
- 工作会话与已加载数据分离：不带 id 的 `open_session` 总是新建会话；同一设计开两次得到两个共享一份数据的会话（`resource_shared: true`）；多会话时调用必须带 `session_id`（否则 `ambiguous_session`）。恢复 id 时输入不匹配拒绝而不是换数据，文件在盘上变了报 `input_changed`。会话摘要带 `dataset: {identity, version}`。
- 审计日志 `WAVE_MCP_AUDIT_LOG`：每次工具调用一行 JSON，写到 0600 文件或 `stderr`，含时间、request id、工具、状态、错误类型、耗时、数据集身份与版本；不含参数、路径、信号名、值。不设则关闭。
- viewer 资产改为用户自建：EUPL-1.2 的 Surfer WASM 与 surver 不再由 wave-mcp 分发（不进 PyPI、不进 Release 附件、不进离线包），`viewer` extra 移除。按 [docs/SELF_BUILD.md](docs/SELF_BUILD.md) 用仓库内脚本从钉定 commit 自建，一次构建长期使用；已装过旧资产包的环境继续被识别。三个用户自建组件（viewer 资产、fsdb2fst、fstdumper）统一为同一套自举标准：脚本不自动下载、版本钉死单一事实源、产物不入库、缓存目录统一、缺失时优雅降级并给出指引。

#### 修复

- `install.sh` 覆盖安装时重建 venv 解释器链接：`venv --upgrade` 默认跳过已存在的 `bin/python*`，导致链接钉在旧解释器而 `pyvenv.cfg` 已写新版本。现在先删链接再 upgrade，覆盖安装后解释器一致。
- 进程内 API 找 `vcd2fst` 增加 `<prefix>/bin` 兜底：不经 launcher 直接 import 时，PATH 上没有 vcd2fst 也能找到装在解释器前缀下的那份；`$VCD2FST_BIN` 仍最优先。
- 离线 bundle 新增 `TEST-BUILD-NOTES`（构建时间、commit、内容清单）与 `SHA256SUMS`（全文件校验），传输截断在装机前就能发现。

- 每次 surver 启动都成功（此前约 1/64 因 token 以 `-` 开头被 clap 当选项而失败），token 改为 `--token=<value>` 附着传递。
- 视图上限触发的淘汰会在 `open_wave_view` 回复里报 `evicted_view_id`。
- viewer 入口不再无限等 service worker（约 4 秒有界），连不上后端时给出原因与建议而不是空白页，`get_view_state` 能答「页面是否真的起来了」。
- 波形里不存在的信号报进 `warnings`，不再静默丢弃。
- 导航更新只发变化的部分，长断线后重连波形流而不只重连状态轮询，存储被拒的 webview 落到明确的错误卡片。
- 出于与 EUPL 查看器的许可隔离，shell 页面不再读取查看器应用内部状态：`get_view_state` 的 `actual` 不再回传用户游标位置与 `user_dirty`（保留 `applied_revision`、`page_ready`、`page_error`），对比双栏不随单栏手动缩放联动（agent 更新视窗仍同步注入两栏）。shell 与查看器只通过页面加载参数和标准 postMessage 通信。
- 时间零带单位：可读时间里的 `"0"` 现在是 `0ns`（按波形自身时间单位，100 ps 时基下为 `0ps`），摘要不受影响。
- `open_session` 传不存在的路径返回结构化的 `not_found`，清单损坏返回 `invalid_argument`，不再抛异常。
- `wave-session` 把 `.f` 路径本身而不是其中列出的文件交给精化器，CLI 建出的会话全是静态的；现在传解析后的文件列表以及 `+incdir+`/`+define+`。

#### 变更

- viewer 文档注明上游窄面板行为（约 460px 以下波形区折叠）。
- 离线包不再带 Berkeley DB（独立 CPython 钉到 python-build-standalone 20260901 / CPython 3.11.16，移除 `_dbm`）。
- `build_fstdumper.sh` 不再下载任何东西，GPL-3.0 的 fstdumper 源码由用户提供目录。
- viewer 资产包的字体归属：Ubuntu Font Licence 文本带 Canonical 版权声明，NOTICE 列出每个内嵌字体的版权人。

### English

#### Changed (breaking)

**The tool surface was consolidated and the parameter names unified.** Six
tools were merged away and the argument names differed between tools for the
same concept (three spellings of a time, four of a path), so every tool change
cost an agent a fresh read of the schema. There is no alias shim: an MCP client
re-reads the tool schema each session and the CLI derives its flags from the
signature, so both follow the rename on their own, whereas an alias would tax
every successful reply forever to serve a rare stale call. A hardcoded caller
gets a message naming the replacement instead of an opaque failure.

Pin `wave-mcp<1.0` if you cannot adapt yet. From 1.0 on, tool names, parameter
names, the `_fp` shape and the `session.json` shape stay compatible within
1.x; anything that would break them waits for 2.0.

Merged tools:

| Removed | Use instead |
| --- | --- |
| `signal_values_in_range` | `signal_values(paths, start=..., end=...)` |
| `signal_value_at` | `signal_values(paths, time=...)` |
| `instances_of_module` | `find_instances(module=...)` |
| `instances_of_module_matching` | `find_instances(module=..., name_contains=...)` |
| `list_child_instances` | `find_instances(under=...)` |
| `list_files` | `files()` |
| `find_files` | `files(name=...)` |
| `modules_in_file` | `files(modules_of=...)` |

Renamed parameters:

| Old | New |
| --- | --- |
| `full_path`, `signal_path`, `signal_full_path`, `instance_full_path`, `scope_full_path` | `path` (`paths` on the batch tools) |
| `time_as_string`, `time_point` | `time` |
| `start_time_as_string` / `end_time_as_string` | `start` / `end` |
| `max_number_of_values` | `limit` |
| `number_of_levels` | `max_depth` |
| `string_in_instance_name` | `name_contains` |
| `file_short_name` / `return_exact_names` | `name` / `exact` |
| `full_file_path` | `source_file` (reply field of `files(modules_of=...)`) |
| `fst_a` / `fst_b` (`diff_waveforms`) | `fst_paths` (a list of two or more) |

Filesystem paths keep their own namespace (`wave_path`, `vcd_path`,
`fsdb_path`, `session_path`, `fst_paths`, `out_path`): a bare `path` always
means a design hierarchy reference, never a file on disk.

A second round of renames follows the same one-concept-one-name rule on the
preparation and conversion tools:

| Old | New |
| --- | --- |
| `expected_revision` (`query_defaults_set` / `query_defaults_clear`) | `defaults_revision`, the name the query tools already use to pin a revision |
| `fsdb_scopes` / `fsdb_signals_file` (`prepare_session`) | `scopes` / `signals_file`, as on `convert_fsdb_to_fst` |
| `mode` (`prepare_session`, `convert_vcd_to_fst`, `wave-vcd2fst --mode`, `wave-session --convert-mode`) | `pack` with the compressor's own name: `speed` is `fastlz`, `balanced` is `lz4`, `size` is `zlib`. The same vocabulary `convert_fsdb_to_fst` already used |
| `fst_path` as the *output* of `convert_vcd_to_fst` / `convert_fsdb_to_fst` | `out_path` |
| `fst_path` on `prepare_session` | removed. Conversions land in the user cache (below); a caller who wants an FST at a chosen place calls the convert tool and then prepares from that file |
| `max_signals` (`list_signals`, `signal_downstream`), `max_scopes` (`find_instances`) | `limit`, the name the sampling tools already use for "at most this many" |
| `levels` (`find_instances`) | `max_depth`, as on `trace_value` / `trace_x` |
| `transitive` (`signal_fanin`, `signal_downstream`) | folded into `max_depth`: `1` is the direct answer (default), a larger value follows the chain across that many hierarchy levels (up to 8) |
| `filter_by_name` / `filter_by_type` (`list_signals`) | `name_contains` (as on `find_instances` / `list_modules`) / `signal_type` |
| `parallel` (`convert_vcd_to_fst`) | removed; the converter probes for parallel packing itself |

**Unknown arguments and retired tool names are refused on the wire, with the
replacement named.** The MCP SDK used to drop keys a tool does not declare,
so a caller still sending `transitive=True` got a one-hop answer with no sign
anything was ignored, and a retired tool name came back as a bare "Unknown
tool". Both are now checked at the SDK's public `call_tool` entry against the
public tool schema: an undeclared key is refused, a retired parameter or tool
name is refused with the current name in the message. No SDK internals are
touched.

**Execution is bounded.** Every tool call passes an admission gate: at most
`WAVE_MCP_WORKERS` (4) run at once and `WAVE_MCP_PER_OWNER_RUNNING` (2) per
user; the rest wait in a queue of `WAVE_MCP_QUEUE_CAPACITY` (32), fairly
between users, for up to `WAVE_MCP_QUEUE_TIMEOUT` (30 s). Beyond that a call
is refused with `server_busy` or `queue_timeout` as a structured reply, never
an opaque hang. Each user may hold `WAVE_MCP_PER_OWNER_SESSIONS` (16) open
sessions (`resource_limit` past that); sessions idle for
`WAVE_MCP_SESSION_TTL` (1800 s) with no request in flight are closed by a
background sweep, the shared data behind them going only with its last
reference. On SIGTERM/SIGINT the server stops admitting, refuses waiters,
gives running calls a grace period and reaps its own viewer children.
`session_info(list_sessions=True)` now includes a `server` block with the
occupancy counters and limits. A call that has started is never interrupted:
the waveform scans are C code, and a slot is returned only when the body
really returns. Converter subprocesses run in their own process group, so a
timeout or stall takes down the converter and whatever it forked, nothing else.

**HTTP from other machines needs `WAVE_MCP_TOKEN`.** wave-mcp runs as the OS
account that started it and has no user model of its own; what the process may
read or write is the operating system's decision, as with every other tool in
the flow. The one thing an HTTP listener adds is that other processes on the
host or network can reach it, and `WAVE_MCP_TOKEN` closes that: when set, every
request must carry `Authorization: Bearer <the same value>` or is refused with
401 before any tool runs, and binding a non-loopback `--host` without it is a
startup error whose message names the variable. Loopback without a token and
stdio are unchanged. To serve several people from one machine, run one process
per person (or a front door that does), not one process pretending to be all
of them. Every tool now carries a data-access class (session-, path-, view- or
server-scoped) and registering one without a class is a startup error.

**`out_dir` is optional and never rewritten.** `prepare_session` and
`open_static_session` (and `wave-session --out`) no longer require a
session directory. Omitted, the session lands under the session root
(`$WAVE_MCP_SESSION_ROOT`, default `~/.wave-mcp/sessions`) in a directory
named by the identity of
its inputs, so the same design asked for from anywhere resolves to one place.
The netlist of a defaulted waveform session lives at the identity of its RTL
sources, which is exactly where the static session on those sources puts it:
explore first, add the waveform later, reuse one elaboration, with no
directory to remember. Given, `out_dir` is used exactly as given; the silent
`<root>/<basename>-<digest>` remapping is gone. `wave_path` is now the first
positional parameter of `prepare_session`.

**Derived files moved out of your directories.** wave-mcp used to write the
converted `.fst` next to the source waveform (falling back to a cache dir when
that was read-only) and a `maps.json.msgpack` next to each session's netlist.
Both now live under `$WAVE_MCP_CACHE_ROOT`, default `~/.wave-mcp/cache`,
in `fst/` and `netlist-cache/`, alongside the existing
`fsdb2fst/` build output and `viewer/` assets. Nothing is written to the
directory an input lives in, read-only regression areas work as-is, cache
writes are atomic, and two processes converting the same waveform build it
once. The on-request converters (`convert_vcd_to_fst`/`convert_fsdb_to_fst`)
without `out_path` target the same cache root (`~/.wave-mcp/cache/fst/`), so
the "derived files never land in input directories" promise covers every
conversion path; pass `out_path` to place the output anywhere else.
`XDG_CACHE_HOME`/`XDG_DATA_HOME` no longer participate in resolution:
everything wave-mcp writes by default is found (and cleaned) under one
directory, `~/.wave-mcp/`. `WAVE_MCP_SESSION_ROOT`/`WAVE_MCP_CACHE_ROOT` keep
their meaning, so explicitly configured setups are unaffected.
`session.json` records the caches a session reads (`caches[]`) and
`session_info` reports whether each is still the version it was built against.
Old artefacts next to your waveforms are not touched or migrated; delete them
when convenient (`.gitignore` covers both patterns).

`session.json` also changed: `fst_hash` / `filelist_hash` (whole-file SHA-1)
gave way to `fst_version`, the same 16-hex size + mtime + head-sample digest
used everywhere else. Old manifests still open; their `fst_hash` is ignored.

`_fp` on query replies is now `{dataset: {identity, version, wave, netlist},
query}` instead of the flat `{wave, netlist, resource, query}`: `identity` says
which design, `version` which revision of it, and the two file versions keep
the per-input view a static session relies on (`wave == ""`).

Two behaviour changes ride along with the merges:

- `signal_values` answers for a batch of paths, so value rows now live under
  `signals[i].values` rather than at the top level, and several signals are
  read in one pass over the file instead of one pass each.
- `limit` (formerly `max_number_of_values`) *downsamples* an oversized result
  evenly, keeping the first and last change, instead of truncating it to a
  leading prefix. The reply says what happened via `sampled`, `sample_rate` and
  `total_available`, and `resume_from` gives the time to pass back as `start`
  to walk the skipped detail at full resolution (it overlaps the first kept
  interval by design; it is not a pagination cursor); narrow `start`/`end` for
  full detail directly.
- `name_contains` now filters on the instance's own leaf name in waveform
  sessions too. It used to match the whole path there, so a parent scope's name
  matched every child; static sessions always filtered on the leaf.
- `diff_waveforms` compares **N runs**, not two: `fst_paths` takes a list, and
  each diverging signal reports `groups`, the run indices partitioned by the
  value they hold. A regression sweep produces more than two runs, and how they
  split is itself the evidence (three against one points at that run's stimulus;
  a two-two split points at a configuration difference). The two-run case still
  carries readable `value_a` / `value_b`. Because the reply shape changed, an
  alias for `fst_a`/`fst_b` would not have saved a caller any work.

#### Added

- **`sample_at_clock`**: a per-cycle table instead of a change list. Synchronous
  logic is reasoned about per cycle, and raw changes make every combinational
  glitch and every picosecond of skew look like an event. Only clean 0->1 (or
  1->0) clock transitions count as edges: a clock through x or z captured
  nothing definite, so it yields no cycle.
- **`fsm_transitions`**: the state transitions a register actually made (with
  counts and first occurrence) plus which RTL branches assigning it ever had
  their guard hold. Explicitly **not coverage**: coverage needs a denominator
  from design intent, and one dump cannot tell an unreachable state from an
  unexercised one, so nothing is reported as a gap. A guard undecidable at some
  transitions reports `undecided_at` rather than claiming the branch never ran.
- **`signal_downstream`**: forward reachability, the mirror of `signal_fanin`
  (`signal_loads` gives one hop; this follows the chain across hierarchy
  boundaries). With `time`, each downstream signal also reports its first change
  after that instant, which is correlation and is labelled as such, not causation.
- **`fold_transactions`**: folds activity into transaction records you define.
  **No protocol library is built in**: no AXI/APB/AHB tables, since a built-in
  table is wrong for any design that deviates from the spec, and wrong silently.
  Conditions are treated as edges (a level that merely stays true opens nothing).
  A transaction still open at the window end is kept with `incomplete: true` and
  no invented end time; an end with nothing open (or nothing open under its
  `id_field`) is reported as `unmatched_end` rather than dropped. An edge is
  "false before, true now": a condition coming out of x/z opens or closes
  nothing, and that time is counted as undecidable.
- **Query defaults** (`query_defaults_set` / `query_defaults_get` /
  `query_defaults_clear`): the default signals and time window for the calls
  that follow. Debugging one failure means
  asking many questions about the same few signals in the same window, and
  restating them every time is where paths get mistyped and windows drift apart.
  Query tools may then omit `paths` / `start` / `end`; an explicit argument
  always wins, and any reply that drew a default says so in `_query`
  (`from_defaults` lists the inherited fields) so the range of the data is
  never ambiguous. `start="min"` / `end="max"` stay an explicit request, so a
  caller can widen back out while defaults are set. A default set holding
  several signals supplies no default to the single-signal tools: it reports
  the ambiguity instead of picking one. Defaults are per session and carry a
  `defaults_revision`; a query may pin one and is refused with
  `defaults_conflict` if the defaults moved underneath it. They hold waveform
  coordinates only, never a hypothesis or a next step.
- **`signal_activity`**: per-signal activity over a window in one file pass:
  toggle count, time-weighted x/z share, constant flag, first/last change and
  the values held at both ends. Answers "which of these signals actually moved,
  and did any spend time unknown" without pulling raw timelines first.
- **`find_time_windows`**: the time intervals where a boolean condition over
  signals holds, expressed as an `expr_eval` expression object (the same
  structure the netlist guards already use, so there is no second expression
  language). Evaluated only at the changes of the signals it references, so no
  sampling grid and no missed glitches. A condition that cannot be decided
  (x/z reaching the expression) is never reported as a hit, and the undecidable
  time is returned separately so an empty result is not misread as "it never
  happened". An interval still true at the window end is marked `open_ended`.
- **Environment fingerprint on query replies** (`_fp`): the dataset identity
  and version plus a digest of the effective question. Two runs can be shown
  to have been answered from the same inputs and the same question, which is
  what makes an agent trajectory reproducible; four phrasings of one question
  (explicit vs defaulted, `10ns` vs `10000ps`, a string vs a one-item list)
  give one `query` digest. The waveform identity is size + mtime + a head
  sample, never a whole-file hash, so a multi-GB dump stays cheap. Successful
  replies also carry `_query`, the effective parameters in readable form.
- **Work sessions are separate from loaded data.** `open_session` without an
  id always creates a new session with its own defaults; opening the same
  design twice gives two sessions sharing one parsed waveform and netlist
  (`resource_shared: true`). With several open, a call must name its
  `session_id` (`ambiguous_session` otherwise); `session_info(list_sessions=
  True)` lists them. Resuming an id refuses mismatched inputs instead of
  re-pointing the session, and reports `input_changed` when the files moved on
  disk. Each session summary now carries `dataset: {identity, version}`.
- **Audit log** (`WAVE_MCP_AUDIT_LOG`): one JSON line per tool call, to a
  0600 file or `stderr`, with timestamp, request id, tool, status, error type,
  elapsed time and the dataset identity/version. Never arguments, paths,
  signal names or values. Off unless the variable is set; the
  `wave_mcp.audit` logger is there for deployments that want a handler of
  their own.
- **Viewer assets are now user-built.** The EUPL-1.2 Surfer WASM and surver
  are no longer distributed by wave-mcp (no PyPI package, no release
  attachment, no offline-bundle inclusion); the `viewer` extra is removed.
  Build them from the pinned commit with the in-repo scripts, following
  [docs/SELF_BUILD.en.md](docs/SELF_BUILD.en.md): build once, use long-term.
  Environments with a previously installed assets package keep working. The
  three self-built components (viewer assets, fsdb2fst, fstdumper) now share
  one bootstrap standard: scripts never download upstream sources, versions
  are pinned to a single source of truth, artifacts stay out of the repo and
  distributions, one cache root, and graceful degradation with guidance when
  a component is missing.

#### Fixed

- `install.sh` rebuilds the venv interpreter links on reinstall: `venv
  --upgrade` skips existing `bin/python*` links, pinning the old interpreter
  while `pyvenv.cfg` already names the new one. The links are removed first so
  the upgrade recreates them.
- In-process API lookup of `vcd2fst` falls back to `<prefix>/bin`: importing
  wave_mcp without the launcher (no PATH edit) now finds the binary installed
  next to the interpreter; `$VCD2FST_BIN` still wins.
- The offline bundle ships `TEST-BUILD-NOTES` (build time, commit, content
  inventory) and `SHA256SUMS` (all files), so a truncated transfer is caught
  before an install.
- **Every surver start now succeeds, instead of roughly 1 in 64 failing.**
  The session token is random and its alphabet includes `-`, so about 1/64
  of tokens start with a dash. Passed as a separate argv item, that token
  was parsed by clap as an option and surver exited with code 2 before
  starting ("surver exited early (code 2)"), and port retries could not
  help because the token stayed the same. The token is now passed attached
  (`--token=<value>`), where it cannot be read as an option.
- **The view cap now reports its eviction.** Opening a view past
  `WAVE_MCP_MAX_VIEWS` (default 8) closes the oldest one, whose page stops
  updating with no other notice. The `open_wave_view` reply now carries
  `evicted_view_id` (`evicted_view_ids` when several go at once) so the
  caller can tell the user or reopen the view.
- **The viewer entry no longer waits on a service worker forever.** The
  entry page waited on `navigator.serviceWorker.ready` with no timeout, so
  an environment where the worker never becomes ready (some embedded IDE
  browsers) stayed on "Loading wave viewer..." indefinitely, which looked
  exactly like a blank viewer. Every wait is now bounded (about 4 s
  total) and the shell takes over even when no worker is in control.
- **A viewer page that cannot reach its backend now says so.** The shell
  shows a loading overlay until the streaming backend answers its probe,
  retries once automatically (letting the worker take control, which is
  what gateway-rewritten headers need), and otherwise displays the reason
  plus what to try instead of a blank page. The page reports readiness as
  `actual.page_ready` / `actual.page_error`, so `get_view_state` can
  answer "did the page actually come up".
- **Signals the waveform does not contain are reported, not silently
  dropped.** `open_wave_view` / `update_wave_view` check requested names
  against the FST (cached, advisory, never blocking the open) and list
  unknown ones in `warnings`; the page shows the same notice. A view
  opened without any signals gets a note as well, instead of an
  unexplained empty pane.
- **`snapshot()` carries the warnings list**, so the page and any HTTP
  client see the same advisories the tool replies carry.
- **Navigation updates re-send only what changed.** Sending the whole
  navigation set on every update moved the window: a viewport-only update
  replayed the cursor and its GoToTime scroll as well, and that scroll
  overrode the requested zoom whenever the cursor sat outside the new
  window. Cursor/viewport/marker deltas are now diffed against what the
  frame already carries.
- **License isolation from the EUPL viewer: no viewer-state readback.** The
  shell no longer reads state out of the viewer app (the former get_state
  polling crossed that boundary), so `get_view_state`'s `actual` no longer
  reports the user's cursor position or `user_dirty` (it keeps
  `applied_revision`, `page_ready`, `page_error`), and compare panes do not
  follow a manual zoom in one pane (agent viewport updates still reach both
  panes). The shell talks to the viewer only through page-load URL
  parameters and standard postMessage.
- **A long disconnect reconnects the waveform stream, not just the state
  polling.** After sleep, a dropped VPN, or a rebuilt forwarded port, the
  page could recover its polling while the pane stayed empty, because the
  stream never reconnected. After four consecutive failed polls the shell
  re-points the frame at the backend once it answers again.
- **Storage-denied webviews no longer break the recovery path.**
  `sessionStorage` access in the self-heal path is guarded, so sandboxed
  IDE viewers settle on the explicit error card instead of freezing on
  "Connecting...".
- **Time zero renders with a unit.** `0` in a reply's readable times was the
  one value without a unit (`"0"` next to `"5ns"`); it now reads `0ns` (the
  waveform's own unit, `0ps` for a 100 ps timescale). Digests are unaffected.
- **`open_session` on a missing path is a structured error**, `not_found`
  with a hint, instead of an exception; a corrupt manifest is
  `invalid_argument`.
- **`wave-session` built the netlist from the wrong input.** The CLI handed
  the `.f` path itself to the elaborator instead of the files it lists, so the
  netlist step silently failed and every CLI-built session was static. It now
  passes the parsed file list plus the filelist's `+incdir+` / `+define+`.

#### Changed

- **Viewer docs note the upstream narrow-pane behavior.** Below roughly
  460px of pane width the upstream viewer collapses the waveform area
  (measured: rendering fades out between 520px and 460px and returns from
  ~580px); the troubleshooting section now says to widen the pane.
- **Offline bundle no longer ships Berkeley DB.** The standalone CPython is
  now pinned to python-build-standalone 20260901 (CPython 3.11.16), where
  `_dbm` is a separate shared object that nothing else links. The bundle
  removes it, and the material check requires the file to be absent and
  neither the Sleepycat licence text nor the Berkeley DB source to ship.
  wave-mcp never imports `dbm`; `dbm.dumb` stays available.
- **`build_fstdumper.sh` no longer downloads anything.** The GPL-3.0
  fstdumper source is obtained by the user and passed as a directory
  (`bash deploy/build_fstdumper.sh /path/to/fstdumper`); the script only
  applies the patches and builds there. `FSTDUMPER_BUILD_DIR` is still
  accepted as an alias for the new `FSTDUMPER_SRC_DIR`.
- **Font attribution in the viewer assets package.** The Ubuntu Font Licence
  text now carries the Canonical copyright notice recorded in the font, and
  the package NOTICE lists the copyright holder of every embedded font
  (Ubuntu, Noto Emoji, Hack, emoji-icon-font).

## [0.2.6] - 2026-09-11

### Added

- **The viewer accepts VCD and FSDB, not just FST.** `open_wave_view` and the
  `wave-view` CLI used to require an FST, so viewing a VCD meant converting it
  by hand first. They now resolve any supported waveform through one entry
  point: FST opens directly, VCD and FSDB are converted first.
- **Conversion results are shared between the analysis and viewer paths.** Both
  go through the same content-addressed cache, so a waveform converted by
  `prepare_session` is reused when the viewer opens it, and vice versa. This
  matters most for GB-scale FSDBs, where a conversion costs minutes: whichever
  order you use, it happens once.

### Changed

- **`signal_fanin` now reports every direct peer of a boundary net.** A net
  with no module-local fan-in record (a struct port such as `reg2hw`, an
  aggregated bus, a sub-module output) used to resolve to the internal
  fan-in of one arbitrary peer, one or more levels deeper, so the reply
  neither matched the hierarchy of `signal_connectivity` nor covered the
  other branches. Direct mode now returns all directly connected peer
  ports, and `transitive: true` expands the cone behind each of them;
  `fan_in` is now a strict subset of `connectivity` for every signal.
  `signal_drivers` intentionally keeps its record-level answer
  (kind/file/line/snippet per driver); use it when source locations are
  what you need.

### Fixed

- **Viewer parameter mistakes are now actionable and no longer look like an
  outage.** A cursor passed as `{"time_units": 100}` came back as a generic
  `available: false` reply whose message named neither the offending field nor
  the correct shape, and the related `{"time": 100, "time_units": "ns"}` was
  accepted outright and silently treated as ps. The viewer tools now validate
  against an allowlist per parameter and return `status: error` with
  `error_type: invalid_argument`, the failing `parameter`, a `did_you_mean`
  suggestion, the `expected` shape and an `example`. `available: false` is now
  reserved for the feature itself being unavailable: missing viewer assets or
  a surver that cannot start.
- **Malformed times are rejected instead of dropped later.** Time values must
  be integer digits with an optional unit suffix (`"1523400"`, `"1523400ps"`);
  a suffix that conflicts with the declared `unit`, an unknown unit, `None`
  and fractional values are all errors. Previously these passed state
  validation and were dropped during command translation, so a view opened
  without its cursor or marker and said nothing. The suffixed form used by
  `diff_waveforms` (`first_divergence.time` is `"85ns"`) keeps working and is
  normalized to `{time, unit}`.
- **Dropped commands are reported.** `open_wave_view` / `update_wave_view`
  surface a `warnings` list when a command cannot be generated, so a drop is
  never silent.
- **Non-object fragments raise typed errors.** A non-dict `viewport`, `diff`,
  `signals`, `markers` or `annotations` used to escape as `TypeError` /
  `AttributeError` past the catch list in the tool layer; they are now
  `ViewStateError` with the same structured reply.
- **`labels` and the waveform count are validated.** `labels` must have one
  entry per waveform and at most two waveforms can be opened; both were
  silently ignored before.
- **Analysis tools answer malformed times with the same structured error.**
  `signal_values_in_range`, `signal_value_at`, `active_drivers`, `trace_value`
  and `trace_x` no longer raise bare `ValueError`s.
- **A failed viewer open no longer leaks a surver process.** The reference
  taken before validation and server setup is released on failure, and surver
  startup errors now include the last lines of its stderr, which used to be
  discarded to `/dev/null`.
- **Viewer asset discovery names the real problem.** An installed but
  incomplete assets package, or a partial `~/.cache/wave-mcp/viewer`, now
  produces a hint pointing at the actual directory instead of repeating
  "pip install wave-mcp[viewer]".
- **The browser shell survives malformed times and shows state failures.**
  `bigIntParts` guards its BigInt conversion, and a bad time value can no
  longer abort a whole state update without a visible sign.
- **Empty waveform values and extreme times no longer break guard
  evaluation.** A time past the dump's last change makes FST value lookups
  return an empty string; branch-guard comparisons crashed on it
  (`invalid literal for int()`), and `==` guards could read it as equal, a
  confident wrong answer. Empty now means unknown, so guards stay
  undecidable and `active_drivers` keeps answering. A time string beyond
  the 64-bit FST range is rejected with the structured `invalid_argument`
  reply instead of a raw `OverflowError`.
- **FSDB support was unreachable for anyone who installed from PyPI.** The
  converter is built on demand because the Verdi FsdbReader runtime cannot be
  redistributed, but the converter sources and the build script were never
  packaged, so the auto-build had nothing to compile. FSDB shipped as a
  supported format in 0.2.0 and stayed unusable through 0.2.5 unless you worked
  from a git checkout. `third_party/fsdb2fst/` and `deploy/build_fsdb2fst.sh`
  now ship in both the wheel and the sdist, and the sources are resolved from
  the installed location as well as from a checkout (a checkout still wins, so
  local edits are never shadowed).
- **The "fsdb2fst not found" error told you to run a file you did not have.**
  It advised `bash deploy/build_fsdb2fst.sh` even when it had just reported
  that the sources were not shipped. The build script is now only offered when
  it exists, and the message names its absolute path.
- **Guides referenced from error messages were missing from the package.**
  `docs/FSDB_GUIDE.md` and `docs/WAVE_VIEWER.md` are cited in runtime errors
  and docstrings but shipped in neither the wheel nor the offline bundle. Both,
  plus `WAVE_VIEWER.en.md` and `DEPLOY_AIRGAP.md`, are now included.
- **The offline bundle could not build the FSDB converter at all.** It carried
  no converter sources, which is the worst case for an air-gapped host that
  cannot clone the repository. The bundle now ships `fsdb2fst-src/` and a
  `docs/` directory.
- **Unsupported waveform formats are now rejected by name at the entry point.**
  A `.ghw` or `.vpd` used to fall through to the VCD converter and fail as
  "VCD not found" or with a parse error from inside `vcd2fst`, both of which
  point at the wrong problem. The supported extensions are listed in the error
  instead.
- **`wave-session --vcd` no longer converts a second private copy.** The CLI
  called the converter directly and wrote the FST inside `--out`, so a waveform
  already converted by `prepare_session` or the viewer was converted again, and
  the copy it produced was invisible to them. It now goes through the same
  shared cache as every other entry point. `--vcd` also accepts `.fsdb`.
- **The fallback cache directory is now stable across sessions.** When the
  source directory is read-only, conversions previously landed in the
  per-session output directory, so every new session reconverted the same
  waveform. They now land in `~/.cache/wave-mcp/fst-cache` (honouring
  `XDG_CACHE_HOME`). Waveforms in writable source directories are unaffected:
  their FST still sits next to the source, exactly as before.
- **`wave-mcp --version` now works.** The server argument parser only knew
  `--transport`, `--session`, `--host` and `--port`, so the first command most
  people type failed with `unrecognized arguments` and exit code 2. It now
  prints `wave-mcp <version>` and exits 0, on both `wave-mcp` and
  `wave-mcp query`.
- **The bundle no longer ships a file named `VERSION` holding only a
  timestamp.** That file recorded when the bundle was built
  (`2026-09-11T03:21:25Z`), which read like a broken version string and sent at
  least one operator hunting for a release number that was never in it. It is
  now `BUILD_INFO` and carries `wave_mcp_version` and `build_time_utc`.
- **The on-demand fsdb2fst auto-build was broken in installed packages.** The
  build script derived its source directory from its own filesystem location
  (`dirname "$0"/../third_party/fsdb2fst`), but after a wheel install the
  sources land under `share/wave-mcp/fsdb2fst/` (no `third_party/` layer).
  `convert.py` now passes the resolved `SRC_DIR` to the script via an
  environment variable, and the script honours it when set, falling back to the
  checkout layout for manual invocations.

## [0.2.5] - 2026-09-08

### Fixed

- **Embedded font license texts corrected.** The notice shipped for
  `Hack-Regular.ttf` was wrong: the crate's declared license string
  (`... AND OFL-1.1 AND Ubuntu-font-1.0`) had been read as a per font mapping, so
  Hack was recorded as OFL-1.1. Hack is actually MIT (Copyright 2018 Source
  Foundry Authors) plus Bitstream Vera (Copyright 2003 Bitstream, Inc., with
  Reserved Font Names "Bitstream" and "Vera"), which means the declared
  `OFL-1.1` covers `NotoEmoji-Regular.ttf` only. The upstream notice is now
  vendored at `docs/licenses/epaint-default-fonts.Hack.txt` and
  `epaint-default-fonts.SOURCES.txt` records the mapping taken from the upstream
  per font notices rather than from the declared string.
- **Blank SIL template replaced with the upstream text.** The OFL-1.1 file was a
  generic template still carrying unfilled `<Copyright Holder>` and
  `<Reserved Font Name>` placeholders, so it named no copyright holder and did
  not satisfy the notice requirement. It is now the verbatim
  `fonts/OFL.txt` that upstream distributes with the font. All four font notices
  are byte identical to `epaint_default_fonts` 0.35.0.
- **Placeholder gate added to the asset build.** `build_viewer_assets.sh` now
  refuses to package any font license text that still contains template
  placeholders, so a blank notice cannot silently ship again.

### Changed

- `deploy/viewer-pin.sh` pins the viewer asset package to `0.25.6.post1`. The
  pinned surver/wasm pair is unchanged, so the wellen version assertion stays at
  `0.25.6`; the post release suffix marks a packaging only fix that ships the
  same binaries with the corrected font notices.

## [0.2.4] - 2026-09-08

### Changed

- **TraceWeave attribution completed, with implementation reference and design
  influence stated separately.** A closer review of our own commit history showed
  that the earlier notices, while accurate as far as they went, described a
  narrower scope than what had actually been consulted, and that two different
  kinds of influence were being collapsed into one sentence. Both are now
  separated:
  - Code-level references, all within the FSDB converter: `ParseScaleFs` was
    implemented with reference to TraceWeave's `_ParseScaleFs` (unit conversion
    plus the parse-failure convention); the `ffrAPI` stub mirrors the subset it
    exercises and follows the same FsdbReader build layout; the FSDB per-bit
    MSB-first ordering (`vc[i] -> s[i]`) and the `fsdbXTag`/`fsdbTag64` layout
    compatibility were cross-checked against its verified wrapper. The last two
    were stated in the original 2026-08-31 commit, dropped on 2026-09-01, and
    are restored.
  - Design-level influence: `diff_waveforms` was written independently and reuses
    no code. First-divergence localization was already on our development roadmap.
    `diff_first_divergence` came earlier, and we referred to it when prioritising
    the feature; that is now credited in `wave_mcp/diff.py` and the notice. It is
    the only design-level influence we are aware of; the broader feature set and
    tool organisation follow the capability set of established commercial waveform
    debug tools.
  - Wording such as "the only place" and "from scratch" is scoped to code, so the
    code-level and design-level relationships are stated separately rather than
    collapsed into one claim.
- **TraceWeave MIT license text vendored** at
  `docs/licenses/TraceWeave-MIT.txt`, with its original copyright line intact and
  linked from `docs/THIRD_PARTY.md`, so attribution travels with any
  redistribution.
- **Attribution surfaced in both READMEs**, as a one-line note after the feature
  list linking to the full notice, instead of living only in the FAQ.

### Added

- **`LICENSE`, `docs/THIRD_PARTY.md`, `docs/PACKAGING_MATERIALS.md` and
  `docs/licenses/` now install with the wheel** via `data-files`, so the
  attribution and third-party notices travel with a plain `pip install`
  instead of living only in the sdist or the offline bundle.
- **Complete redistribution materials for the optional components**: original
  source archives, license texts and provenance records for the standalone
  Python runtime (52 files), `vcd2fst` (27 files) and the viewer assets,
  with hashes verified against upstream. See `docs/PACKAGING_MATERIALS.md`.
- **Font licenses that the viewer binaries actually embed**: `OFL-1.1` and
  `Ubuntu-font-1.0` for the fonts shipped inside the `surver` binary. Earlier
  releases recorded `epaint_default_fonts` as an unknown license; both texts
  are now vendored and required by the packaging gate.
- **`deploy/redistribution_materials.py` packaging gate**: blocks a build when
  required redistribution materials are missing, unresolved or hash-mismatched,
  so a release cannot ship without them.

## [0.2.3] - 2026-09-05

### Fixed

- **Signals declared on the DUT top level reported `unresolved_path`.** Scope
  resolution tried three levels: an exact netlist key, a leaf-name match, then
  the FST `component` field. The last level was dead code, because that field is
  empty for every waveform we produce, both Verilator's native FST and
  VCD-derived ones, so it never resolved anything. Historical sessions never
  noticed, since their netlist was elaborated from the testbench top and its
  root key matched the FST root, which let level one absorb every lookup. A
  DUT-rooted netlist breaks that: the root key is the DUT (`decode`) while the
  FST root is the testbench (`top_tb.U_DECODE`), so levels one and two both miss
  and the root falls through to the branch that never works. Level three now
  also accepts `definition_name`, which the anchor pass derives from the
  netlist, so the root resolves and its direct signals become traceable.
- **Cross-hierarchy tracing only worked in one direction.** `loads()` already
  descended into sub-modules; `drivers()` did not, so the same wire returned a
  connection from `signal_connectivity` and `undriven_signal` from
  `signal_drivers`. Both directions now walk peers, filtered by port direction:
  upstream takes same-level fan-in plus sub-module **output** ports, downstream
  takes same-level loads plus sub-module **input** ports. Direction filtering is
  what keeps a source like a top-level reset from being reported as driven by
  the flops it feeds. Resolved results say which hop they came from.
- **Source paths from the netlist could not be opened.** `modules_in_file`
  always returned 0 and `signal_drivers` handed back paths such as
  `examples/sample/counter.sv` that resolve against nothing, because paths were
  stored relative to the elaboration cwd but looked up against the cwd at query
  time. Netlist builds now record the cwd they ran from and prefer absolute
  paths; every remaining relative path is rewritten once when the netlist loads,
  resolved against the build root, the netlist directory and its ancestors. One
  normalisation point, because these paths reach callers through drivers, loads,
  fan-in, trace results and declarations alike.
- **Filelists ignored environment variables.** `-F $PROJ_FE/rtl/foo.f` was taken
  literally, so an entire file group silently dropped out of elaboration as
  missing files. Tokens now pass through `os.path.expandvars`, and an undefined
  variable is left untouched so it still fails as a missing file rather than
  turning into a path that exists by accident.
- **A misspelled time unit silently moved the cursor.** `--cursor 1000nanoseconds`
  was accepted and landed at an arbitrary time with no warning, because the
  converter fell back to emitting the bare number when unit parsing failed,
  which means something entirely different from converting it. Unknown units are
  now rejected at the CLI and dropped from the generated command batch, and
  remaining markers are renumbered so one bad entry cannot shift the rest onto
  the wrong ids. Accepted units come from a single list in `timeutil`.
- **The viewer stayed blank in IDE-embedded browsers.** The page receives the
  backend token in the URL query string, and some embedded browsers drop the
  query on navigation, leaving the shell requesting a URL that 404s and a canvas
  that never paints. The token is now served alongside the view state and the
  page falls back to it, so the bare URL works. Each server still serves one
  view on localhost only.
- **Views intermittently failed to start.** Opening a view takes two ports, one
  for the shell and one for surver, and both were picked by binding a socket,
  reading the port, then closing it and binding again later. In between, the
  port belongs to nobody and any other process on the host can take it, so the
  second bind fails with the port already in use. It only reproduced under
  load, such as a full regression run starting many viewers in quick
  succession, where it surfaced as a random "surver failed to start". The shell
  server now receives an already-listening socket, so picking and binding are
  one operation. surver is a separate binary that only gets a port number, so
  it cannot inherit a socket and instead retires the port and retries on
  another one when the child fails to come up.

### Added

- `tests/unit/test_dut_root.py`, pinning the DUT-rooted netlist case above on a
  synthetic waveform written by pylibfst, so it needs no simulator and cannot be
  masked by a stale checked-in file. It asserts the conditions that make the bug
  reachable rather than only the outcome, so a later change cannot quietly turn
  it into a test of the leaf-name match. Reverting the fix fails 5 of its 8
  assertions.

## [0.2.2] - 2026-09-05

### Added

- **`WAVE_MCP_SESSION_ROOT` confines where session directories land.** `out_dir`
  is chosen by the calling model, so a drifted prompt could scatter sessions
  across `/tmp`, the cwd, or a shared regression directory, where two users with
  different filelists collide on one directory and silently inherit each other's
  netlist. Set this variable and every `out_dir` resolves inside it: a bare name
  or relative path lands in the root, a path already inside it is kept, and one
  pointing elsewhere is remapped in by basename. Unset, behaviour is unchanged.
  The reply's `session_path` is always the real location.
- **Environment variables documented in one table.** Both READMEs now carry the
  full set (session root, Verdi/FSDB, vcd2fst, viewer, cache) and state that
  these belong in the `env` block of the MCP client config, since an
  agent-spawned server does not inherit an interactive shell's exports.
  `VERDI_HOME` is now explicit about pointing at the install root rather than a
  subdirectory.

### Fixed

- **Viewer backends outlived the process that started them.** `SurverManager`
  relied solely on an `atexit` hook, which Python skips on `SIGTERM`, so
  killing a `wave-view` CLI or an MCP server left one `surver` per open view
  running indefinitely, holding memory and a listening port. Found in the field
  with four backends still alive 23 hours after their servers were abandoned.
  Both entry points now install `SIGTERM` / `SIGINT` / `SIGHUP` handlers that
  close views explicitly, and `surver` children additionally set
  `PR_SET_PDEATHSIG` so the kernel reaps them even when the parent is
  `SIGKILL`ed or crashes, which no in-process handler can cover. The
  server-side cleanup inspects `sys.modules` instead of importing the viewer,
  so installs without the optional assets are unaffected.

### Changed

- **FSDB converter attribution made accurate.** `fsdb2fst` is the newest part of
  wave-mcp and the place where prior public work was consulted at the code level
  rather than starting from scratch: `ParseScaleFs` (FSDB scale string to
  femtoseconds per tick) keeps the error contract and unit table of the public
  TraceWeave implementation, and the offline `ffrAPI` stub mirrors the subset of
  ffrAPI it exercises. Comments naming the project came in with the converter on
  2026-08-31 and were dropped on 2026-09-01 during a broader cleanup of vendor
  references, which left the file described as `original code`. That
  description was inaccurate. `docs/THIRD_PARTY.md` and the headers of both
  source files now carry the project name, author, MIT license, link, and our
  thanks. (The scope stated here was still narrower than what had actually been
  consulted; see [Unreleased](#unreleased) for the complete account.)

## [0.2.1] - 2026-09-04

### Added

- **`WAVE_MCP_SESSION_ROOT` pins where sessions land.** `out_dir` is chosen by
  the calling model, so a drifted prompt could scatter sessions across `/tmp`,
  the cwd, or a shared regression directory, where two users with different
  filelists can collide on one directory and silently inherit each other's
  netlist. Set this variable and every `out_dir` resolves inside it: a bare name
  or relative path lands in the root, a path already inside it is kept, and a
  path pointing elsewhere is remapped in by basename. Unset, behaviour is
  unchanged. The reply's `session_path` is always the real location.
- **Environment variables are documented in one place.** Both READMEs now carry
  the full table (session root, Verdi/FSDB, vcd2fst, viewer, cache) and state
  that these belong in the `env` block of the MCP client config, since an
  agent-spawned server does not inherit an interactive shell's exports.

### Fixed

- **Viewer backends outlived the process that started them.** `SurverManager`
  relied solely on an `atexit` hook, which Python skips on `SIGTERM`, so
  `kill`-ing a `wave-view` CLI or an MCP server left one `surver` per open view
  running indefinitely, holding memory and a listening port. Found in the field
  with four backends still alive 23 hours after their servers were abandoned.
  Both entry points now install `SIGTERM` / `SIGINT` / `SIGHUP` handlers that
  close views explicitly, and `surver` children additionally set
  `PR_SET_PDEATHSIG` so the kernel reaps them even when the parent is
  `SIGKILL`ed or crashes, which no in-process handler can cover. The server-side
  cleanup inspects `sys.modules` instead of importing the viewer, so installs
  without the optional assets are unaffected.
- **Air-gapped launcher could not find its own interpreter.** `install.sh` took
  `--prefix` verbatim, so a relative value baked a relative `RUNTIME` into the
  generated `bin/wave-mcp`. An MCP client spawns that launcher with the user's
  project directory as cwd, not the install directory, so the interpreter path
  resolved to nothing and the client reported only a bare `-32000`. The prefix
  is now absolutized (and probed for write permission) before anything is
  installed, the launcher anchors a relative `RUNTIME` on its bundle, and it
  prints the missing path, the bundle, and the cwd instead of dying silently.
  Reported from an on-site air-gapped deployment.
- **Install-time check now covers the launcher.** The sanity check ran the venv
  interpreter directly, which bypassed the generated launcher entirely, so any
  cwd-dependent path in it survived install and only surfaced in the client. It
  now also executes `bin/wave-mcp` from an unrelated cwd, reproducing how a
  client starts it.
- **`WAVE_MCP_VIEWER_ASSETS` silently ignored when relative.** A relative value
  resolved against whatever cwd the client happened to use and then degraded to
  "viewer unavailable" with a hint telling the user to set the variable they had
  already set. Relative values now resolve against `$HOME`, and the hint names
  the real cause (path missing, or `surver` / `wasm/index.html` absent).
- **Build scripts now absolutize `--out`.** `build_offline_bundle.sh` used
  `dirname "$OUT"` for the tarball step, and the two Docker-based builders pass
  `$OUT` as a `-v` mount source, where a relative path is rejected outright.
- **`open_session` description no longer mentions a sim log**, which it stopped
  loading; the tool description an agent sees now matches what it does.

## [0.2.0] - 2026-09-02

Three new capabilities on top of the 0.1.x tool set: a browser wave viewer the
agent can drive, pass/fail waveform diffing, and an FSDB input path. Tool count
goes from 27 to 34.

### Added

- **Browser wave viewer.** `open_wave_view` renders a session in a real
  waveform GUI (Surfer compiled to WASM, streamed by surver), so an agent can
  show you what it found instead of describing it. `update_wave_view` mutates a
  live view (signals, groups, colors, radix, cursor, markers, viewport,
  annotations) and `get_view_state` reads back what the user has since changed
  by hand, which makes the loop two-way. Install with the `viewer` extra
  (`pip install "wave-mcp[viewer]"`). Guides:
  [docs/WAVE_VIEWER.md](docs/WAVE_VIEWER.md),
  [docs/VIEWER_SCREENSHOTS.md](docs/VIEWER_SCREENSHOTS.md).
- **View lifecycle management.** `list_wave_views` reports the open views
  (id, url, title, waveform paths, revision, backend liveness) and
  `close_wave_view` closes one or all of them, releasing the per-view HTTP
  server. The streaming backend is shared per waveform file set and refcounted,
  so closing one view never cuts off another still reading the same waveform.
  A cap of 8 concurrent views (`WAVE_MCP_MAX_VIEWS`, 0 disables) evicts the
  oldest view so long batch runs cannot pile up views and processes.
- **Pinnable viewer ports.** Views use random high ports by default; setting
  `WAVE_MCP_VIEWER_PORT_BASE` confines them to a 64-port window so a single
  `ssh -L` rule keeps working across views, and several people on one host can
  each take their own window. Allocation falls back to an ephemeral port when
  the window is full.
- **`wave-view` CLI** for opening a waveform in the viewer without an MCP
  client, including `--signals` and remote-friendly port printing.
- **`diff_waveforms`** locates the first divergence between two runs of the
  same design (pass vs fail), reporting per-signal first-difference times and
  coverage of the compared signal set.
- **FSDB input.** `prepare_session` now accepts `.fsdb` directly and converts
  it via the bundled `fsdb2fst`, with `fsdb_scopes` / `fsdb_signals_file` for
  slicing large dumps. `convert_fsdb_to_fst` exposes the converter as its own
  tool, including `info_only` for a fast summary of a huge file before
  committing to a full conversion. See [docs/FSDB_GUIDE.md](docs/FSDB_GUIDE.md).
- **Conversion cache.** Both VCD and FSDB conversions now write the `.fst`
  next to the source waveform and reuse it across sessions. The cache key
  covers the source identity plus the slicing options, so changing the scope
  selection produces a fresh conversion instead of silently reusing a partial
  waveform.
- **Xcelium direct FST output** documented end to end, with the `fstdumper`
  VPI patches required to build it:
  [docs/XCELIUM_FST_GUIDE.md](docs/XCELIUM_FST_GUIDE.md).
- **Simulator compatibility matrix** covering the four ways to get a waveform
  in (FST direct, VCD auto-convert, FSDB conversion, Xcelium direct):
  [docs/SIMULATOR_COMPATIBILITY.md](docs/SIMULATOR_COMPATIBILITY.md).
- **Viewer demos.** Four runnable debug scenarios (X propagation, FSM
  deadlock, CDC pulse loss, pass/fail CRC divergence) under
  [examples/viewer_demos](examples/viewer_demos), plus a screenshot capture
  script.
- **Offline deployment**: one-command Docker pipeline for the air-gapped
  bundle matrix (glibc 2.17 / 2.28), viewer assets packaging, and a vendored
  license directory with a generated crate license report.

### Fixed

- Viewer served its own demo landing page for `/index.html`, shadowing the
  Surfer WASM entry point that the shell loads in an iframe. The viewer would
  come up with an empty waveform pane. Static resolution now always resolves
  the WASM entry from the assets directory.
- `update_wave_view` emitted marker commands with the arguments transposed, so
  markers landed at the wrong time.
- Closing a viewer HTTP server called `shutdown()` without `server_close()`,
  leaking the listening socket and its file descriptor.
- Viewer service worker returned `undefined` on a failed range request instead
  of an error response, stalling waveform streaming.
- Slang lint diagnostics were misclassified as errors, and interface scope
  names failed to resolve during elaboration.
- FST source: definition-name handling for RTL module types, plus scope
  filtering fixes for interface and generate blocks.

### Changed

- Session paths in the shipped demos are stored relative to the session
  directory, so a cloned repository runs the demos without rewriting paths.
- Documentation drops internal test logs, decision history and competitor
  comparisons in favour of support status and known limitations.
- Python interpreter detection in the deploy scripts now gates on the CPython
  version rather than guessing from the binary name.

## [0.1.1] - 2026-08-26

### Added

- `wave-mcp query` CLI subcommand exposing all 27 tools from the shell.
- Validation overview charts in the README.

## [0.1.0] - 2026-08-20

Initial public release: 27 MCP tools for RTL waveform debug over FST plus
SystemVerilog static analysis (pyslang elaboration), covering hierarchy
browsing, signal values, driver/load tracing, X-cause tracing and file-level
queries.

[0.2.2]: https://github.com/Tencent/wave-mcp/releases/tag/v0.2.2
[0.2.1]: https://github.com/Tencent/wave-mcp/releases/tag/v0.2.1
[0.2.0]: https://github.com/Tencent/wave-mcp/releases/tag/v0.2.0
[0.1.1]: https://github.com/Tencent/wave-mcp/releases/tag/v0.1.1
[0.1.0]: https://github.com/Tencent/wave-mcp/releases/tag/v0.1.0
