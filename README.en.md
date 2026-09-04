# wave-mcp: Open-source, license-free MCP server for RTL waveform debug

<img src="docs/images/penglai-logo.png" alt="Penglai Lab" width="200"/>

[![PyPI version](https://img.shields.io/pypi/v/wave-mcp)](https://pypi.org/project/wave-mcp/)
[![Python versions](https://img.shields.io/pypi/pyversions/wave-mcp)](https://pypi.org/project/wave-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

English | [简体中文](README.md)

**wave-mcp is an open-source RTL waveform debug MCP server from the Penglai Lab verification team
at Tencent**, a debugging toolkit for LLMs: it reads **FST waveforms + an RTL netlist** and
provides **34 MCP tools** for hierarchy exploration, signal queries, driver analysis, waveform diff, a browser wave viewer, and
value/X tracing. **MIT licensed, no commercial license required, unlimited concurrency.**

> **Direct FST reads; VCD / FSDB convert automatically.** Verilator `--trace-fst` and Icarus
> emit FST, which wave-mcp reads directly. If all you have is VCD or FSDB, `prepare_session`
> converts it and then opens the session (FSDB conversion checks out no Verdi license).
> It **does not run a simulator**: you produce the waveform with your own flow and hand the
> result to it.

---

## Why wave-mcp

Chip verification takes more than 50% of the development cycle, and waveform debugging is the
highest-frequency activity in it. In the LLM era, engineers want AI agents to read waveforms,
query signals, and trace X root causes, but commercial debug MCPs require expensive licenses
and are concurrency-limited.

wave-mcp provides full waveform debugging capability with a **pure open-source stack**
(pylibfst + pyslang): **no license, accurate data, and validated on real chip projects**.

## Production-grade validation

Fully validated on a **real production chip project** (tens of modules), with OpenTitan and
XiangShan added to the test set:

![Validation overview](docs/images/validation-overview.png)

| Dimension | Result |
| --- | --- |
| Test scale | **100+ test cases** (production project + OpenTitan 27 IPs + XiangShan 38 IPs) |
| Data accuracy | **2.25M signals validated, 100% value correctness** |
| Tool calls | 3.1 million+ calls all passed |
| Driver analysis | drivers / fan-in / connectivity / tracing fully validated on production projects |
| Huge modules | **million-scale scopes analyzed stably** |
| Tool coverage | all 34 tools validated, including unit and browser e2e coverage for viewer / diff |

![Tool call distribution](docs/images/tool-calls-distribution.png)

## Features

- **Waveform queries**: design hierarchy, instances, signals (width/direction/type, with bus
  aggregation), signal values (point / range, random access).
- **Static analysis (pyslang netlist)**: connectivity, drivers, fan-in/fan-out, declaration
  locations (file:line).
- **Waveform-free static analysis**: `open_static_session` builds a session from RTL alone;
  **analyze the design structure before simulation**.
- **Value tracing**: `trace_value` walks the driver chain backwards across module boundaries with
  a real FST value at every node; `trace_x` chases X root causes.
- **Self-healing netlist**: auto-adds `+incdir+` / package sources from pyslang diagnostics and
  recompiles; degrades gracefully without affecting other tools.
- **Consistency checks**: warns when the source or waveform changed but the netlist is stale;
  never silently returns wrong results.
- **Waveform diff**: `diff_waveforms` pinpoints the first divergence between a pass and a fail
  run, ranks diverging signals by time, and filters glitches with clock-aligned sampling.
- **Wave viewer**: `open_wave_view` lets the agent pop a browser waveform right after its
  analysis: suspect signals, cursor pinned at the failure time, and an analysis popup;
  dual-waveform lockstep compare; `get_view_state` tells the agent what you are looking at.
- **Deployment-friendly**: stdio (one process per user, zero ops) / HTTP multi-session /
  self-contained offline bundle (air-gapped networks).

## System requirements

| Dependency | Version | Notes |
| --- | --- | --- |
| Python | **3.10 – 3.13** | Tested on 3.10–3.13; the mcp SDK requires >= 3.10 |
| glibc | **>= 2.28** | Required by the prebuilt pyslang wheels (Ubuntu 18.10+ / Debian 10+ / CentOS 8+) |
| mcp | **2.x** (`>=2.0.0,<3`) | MCP SDK v2, installed by `pip` automatically |
| pylibfst | **>= 0.2.1** | FST waveform reading (fstapi, random access) |
| pyslang | **>= 11.0.0** | RTL netlist building (full elaboration) |
| vcd2fst (optional) | GTKWave | Only needed for VCD→FST conversion (`apt install gtkwave` / `brew install gtkwave`) |
| Verilator (examples) | >= 5 | Only needed by the `verilator_quickstart` example |

> **Linux x86_64 works out of the box** (all Python deps above ship prebuilt wheels);
> on other platforms only `pylibfst` needs building from source (cmake+gcc+zlib), and the
> wave viewer is not supported. See Q6.

On a standard environment, just `pip install wave-mcp`. For constrained environments, find your case below:

| Your environment | Solution | Reference |
| --- | --- | --- |
| No internet (air-gapped network) | build an offline bundle with `deploy/docker_build_all.sh` on a connected machine, copy it in, install with `install.sh` | [DEPLOY_AIRGAP.md](docs/DEPLOY_AIRGAP.md) section 1.0 |
| Python < 3.10 or no Python | no target upgrade needed: the bundle embeds a standalone Python 3.11, independent of the system Python | [DEPLOY_AIRGAP.md](docs/DEPLOY_AIRGAP.md) section 7 |
| glibc < 2.28 (CentOS 7 / RHEL 7) | `pip install` does not work (official pyslang wheels require glibc >= 2.28); use the glibc 2.17 bundle, whose whole chain runs on legacy hosts; or run inside a container (e.g. `python:3.11-slim`) | [DEPLOY_AIRGAP.md](docs/DEPLOY_AIRGAP.md) section 1c |

> The Docker pipeline produces both glibc 2.28 and 2.17 bundles by default, covering all the
> constrained cases above; only the build machine needs docker, target machines never do.

---

## Quickstart

### 1. Install

```bash
pip install wave-mcp
```

### 2. Run an example

```bash
# Example A: Verilator quickstart (counter design, real FST, no commercial simulator)
python examples/verilator_quickstart/run.py      # needs verilator>=5

# Example B: static analysis (UART design, no waveform, no simulator needed)
python examples/static_analysis/run.py

# Example C: tiny built-in sample (hand-written VCD → vcd2fst → FST, zero deps)
python examples/make_sample.py
```

### 3. Open your waveform

```bash
# One command: waveform (.fst/.vcd) + filelist → session (auto-convert + netlist)
wave-session --fst sim/dump.fst --top top_tb --filelist rtl.f --out sessions/my_module

# Start the MCP server (stdio, recommended: one process per user)
python -m wave_mcp.server --session sessions/my_module
```

Or call the `prepare_session` MCP tool directly from your Code Agent (see below).

## CLI queries (`wave-mcp query`)

All 34 tools are also callable from the terminal, without an agent:

```bash
wave-mcp query --list                            # list every tool

wave-mcp query signal_values --session sessions/my_module \
    --full_path top.u_tx.tx_serial              # query signal value changes

wave-mcp query signal_drivers --session sessions/my_module \
    --json-args '{"full_path": "top.u_tx.tx_serial"}'   # JSON args
```

- Flags are generated from each tool's signature; `wave-mcp query <tool> --help`
- Human-readable output by default; add `--json` for the full structured result
- Handy for CI scripts, development debugging and quick checks; every new tool
  gets a CLI surface automatically

## Code Agent integration

`prepare_session` is the unified MCP entry point. When your Code Agent wants to analyze a
waveform, **call it first**: pass in the waveform your simulator produced, and in one shot it
does "(convert →) build netlist → build session → open":

```jsonc
prepare_session({
  "out_dir":      "sessions/my_module",
  "wave_path":    "sim/dump.fst",          // .fst read directly / .vcd auto-converted
  "top":          "top_tb",
  "filelist_path":"rtl.f",                 // same filelist as the sim
  "mode":         "speed"                  // VCD->FST: speed/balanced/size
})
// once it returns "ready", call signal_values / list_child_instances / signal_drivers ...
```

**Client configuration** (stdio):

**Codex uses TOML**, other agents use `mcpServers` JSON:

```toml
# ~/.codex/config.toml (or project-scoped .codex/config.toml)
[mcp_servers.wave-mcp]
command = "python"
args = ["-m", "wave_mcp.server", "--session", "/abs/path/to/sessions/my_module"]
```

- **Codex**: write to `~/.codex/config.toml` (or project-scoped `.codex/config.toml`); the section
  name is `mcp_servers` (with an underscore). You can also register it in one line with
  `codex mcp add wave-mcp -- python -m wave_mcp.server ...`

```json
{
  "mcpServers": {
    "wave-mcp": {
      "command": "python",
      "args": ["-m", "wave_mcp.server", "--session", "/abs/path/to/sessions/my_module"]
    }
  }
}
```

- **Claude Code**: write to `.mcp.json` (`claude mcp add` or manual config)
- **Cursor**: write to `.cursor/mcp.json` (project) or `~/.cursor/mcp.json` (global)
- **Gemini CLI**: put it under the `mcpServers` key in `~/.gemini/settings.json`
  (or project-scoped `.gemini/settings.json`)
- Other agents (Cline / Windsurf / Roo Code, etc.): paste the JSON above into their
  respective `mcpServers` configuration

### Waveform-free static analysis (usable before simulation)

A waveform has values but no connectivity: it can tell you a signal is 0 at this cycle, but not
who drives it or which conditions gate the driving statement. wave-mcp extracts that layer from
the RTL source at session build time: pyslang fully elaborates the design (parameters, generate
blocks, interfaces all expanded) and the result is persisted as a **static design database**.
Driver, fan-in/fan-out, connectivity and declaration queries all run on this database, with no
simulator and no commercial tooling involved.

`open_static_session` builds a netlist and opens a session from RTL alone: **no waveform, no
simulation**. Great for pre-simulation code understanding: interface queries, driver/fan-in
exploration, hierarchy browsing, code review.

```jsonc
open_static_session({
  "out_dir":      "sessions/my_module",
  "top":          "uart",
  "filelist_path":"rtl.f"
})
// connectivity/driver/hierarchy/file/declaration tools all work;
// value & trace tools return a clear "needs waveform" message
```

When the simulation later produces a waveform, call `prepare_session` with the **same out_dir**
to upgrade to a full session; the built netlist is reused.

Every driver record carries full context: driver kind, source location, statement snippet, RHS
source, and **every gating condition stacked on that statement** (as a 4-state-evaluable
expression tree). From the UART design in example B:

```yaml
# wave-mcp query signal_drivers --session ... --full_path uart_top.u_tx.tx_serial
drivers:
  - kind: nonblocking
    file: examples/static_analysis/uart_top.sv
    line: 91
    snippet: tx_serial <= shift_reg[0];
    rhs: uart_top.u_tx.shift_reg
    control: uart_top.u_tx.state, uart_top.u_tx.tick, ...
    guard:                          # every condition stacked on this statement
      - {cond: !rst_n, expect: 0}
      - {cond: tick, expect: 1}
      - {cond: state == DATA, expect: 1}
```

Once a waveform is available, `active_drivers` evaluates the guards against FST values
(4-state) and tells you **which driving statement is active at a given cycle**; `trace_value` /
`trace_x` walk this graph backwards across module boundaries with a real waveform value at
every node. Static connectivity and dynamic waveform values meet in the same toolset, which is
something a purely static design database cannot offer.

**Driver analysis and tracing are hardened to production-grade robustness**, not demo features:

- **Fully validated on real projects**: drivers / fan-in / connectivity / tracing are fully
  validated on a production chip project, plus OpenTitan (27 IPs) and XiangShan (38 IPs)
  exhaustively tested per sub-module hierarchy, with functional cross-checks rather than
  just "non-empty output".
- **Elaboration failures don't take the session down**: a broken top (e.g. a UVM top that
  cannot resolve uvm_pkg) only affects that top, and a healthy DUT netlist is still extracted;
  missing `+incdir+` / package sources are self-healed from pyslang diagnostics and recompiled;
  if it still fails, the netlist degrades explicitly and value queries keep working. Never a
  silently wrong answer.
- **Partial netlists still serve**: when elaboration has errors but modules were extracted, the
  netlist is served with a `partial` flag, and `session_info`'s netlist_health reports the
  actual coverage so you know the confidence boundary of every answer.

### VCD → FST conversion (vcd2fst setup, optional)

If your simulator only dumps VCD (e.g. Questa), convert it to FST first: **~1/50 the size, fast
random access**. Xcelium (xrun) users can skip VCD entirely and dump FST directly via the
fstdumper plugin, see the [Xcelium FST guide](docs/XCELIUM_FST_GUIDE.md) (includes a set of
Xcelium fix patch, see the guide and `third_party/fstdumper/`).
Conversion relies on the `vcd2fst` tool shipped with GTKWave:

```bash
# Debian/Ubuntu
sudo apt install gtkwave
# macOS
brew install gtkwave
```

> **If you already have FST, you don't need vcd2fst at all** (e.g. Verilator `--trace-fst`
> dumps FST directly). Air-gapped environments can use the offline bundle (bundles vcd2fst),
> see [`docs/DEPLOY_AIRGAP.md`](docs/DEPLOY_AIRGAP.md).

Three conversion entry points:

```bash
# 1) Standalone conversion (post-process): mode=speed(fastlz, fastest) / balanced(lz4) / size(zlib, smallest)
wave-vcd2fst --vcd sim/dump.vcd --fst sim/dump.fst --mode speed

# 2) Streaming conversion: hides conversion time inside sim time; FST is ready almost when the sim ends
wave-vcd2fst --stream --vcd sim/dump.vcd --fst sim/dump.fst
#   creates a FIFO + starts vcd2fst in the background; point $dumpfile("sim/dump.vcd") at the FIFO

# 3) One-step session build (auto-convert + package)
wave-session --vcd sim/dump.vcd --top top_tb --filelist rtl.f --out sessions/mod
```

> No manual conversion needed when using the MCP tools: `prepare_session` auto-converts when
> given a `.vcd` (path 1 above).

---

## Tools (34, in 10 categories)

| Category | Tools | Notes |
| --- | --- | --- |
| Waveform prep | `prepare_session` / `open_static_session` / `convert_vcd_to_fst` / `convert_fsdb_to_fst` | waveform → session in one shot (`.fst` / `.fsdb` / `.vcd` auto-detected, conversions cached); static analysis needs no waveform; never runs a simulator |
| Session mgmt | `open_session` / `close_session` / `session_info` | `session_info` includes netlist_health + definition_coverage |
| Hierarchy | `list_child_instances` / `list_modules` / `instances_of_module`(`_matching`) / `scope_info` | three-layer module-def resolution: netlist → name inference → scope_map |
| Signals | `list_signals` / `signal_info` | width/direction/type from FST (with bus aggregation); declaration from the netlist |
| Values | `signal_values` / `signal_values_in_range` / `signal_value_at` | FST's strength, random access |
| Driver analysis | `signal_connectivity` / `signal_drivers` / `signal_loads` / `signal_fanin` / `active_drivers` / `driver_contributors` | pyslang netlist (statically precise) + 4-value branch evaluation |
| Value / X tracing | `trace_value` / `trace_x` | netlist × FST back-traversal, cross-module drill-down |
| Waveform diff | `diff_waveforms` | first-divergence localization between a pass and a fail run: exact divergence time, ranked diverging signals, clock-aligned sampling to filter glitches; divergers feed straight into `signal_fanin`/`active_drivers` for causal backtracking |
| Wave viewer | `open_wave_view` / `update_wave_view` / `get_view_state` / `list_wave_views` / `close_wave_view` | agent-driven browser waveform: suspect signals + cursor pinned at the failure time + an analysis-log popup; dual-waveform compare view with lockstep sync; `get_view_state` tells the agent what the user is looking at (conversational two-way debug); `list_wave_views` / `close_wave_view` manage view lifecycle so batch runs can clean up |
| Files | `list_files` / `find_files` / `modules_in_file` | filelist + pyslang netlist |

> The driver-analysis and tracing categories require the pyslang netlist (pass the right
> filelist/incdirs/defines to `prepare_session`).
> The viewer category needs the optional assets package: `pip install wave-mcp[viewer]`
> (Surfer WASM + surver, distributed separately under EUPL-1.2; the core stays MIT).
> Without it the viewer tools degrade gracefully with a hint; analysis tools are unaffected.

### Wave viewer (wave-view)

```bash
# open one waveform (tens-of-GB FSTs open instantly: surver streams
# server-side, the browser fetches only what's on screen)
wave-view dump.fst --signals top.u_dma.req_valid --cursor 1523400ps

# dual-waveform compare view (two panes, lockstep zoom/cursor sync)
wave-view pass.fst fail.fst --labels pass fail
```

- The CLI prints a URL; desktops auto-open a browser, and in SSH / code-agent
  sessions the IDE terminal auto-forwards the localhost port.
- Typical agent loop: a case fails → `diff_waveforms(pass, fail)` pinpoints the
  first divergence → `signal_fanin` backtracks the cause → `open_wave_view`
  presents both waveforms, a divergence marker and the analysis popup at once.
- The analysis log is a collapsible popup; time references inside it (e.g.
  `[85000ps](#t=85000ps)`) jump the cursor on click, and cursor/viewport/marker
  updates are flicker-free.
- Full guide (MCP tool parameters, two-way debug workflow, architecture,
  deployment and troubleshooting): [`docs/WAVE_VIEWER.en.md`](docs/WAVE_VIEWER.en.md).
- Want to see it first? [`docs/VIEWER_SCREENSHOTS.md`](docs/VIEWER_SCREENSHOTS.md)
  shows the UI across four real debug scenarios, with steps to reproduce them.

---

## Examples

| Example | Path | Dependencies | Shows |
| --- | --- | --- | --- |
| Verilator quickstart | `examples/verilator_quickstart/` | Verilator 5+ | counter design → real FST → prepare_session end-to-end |
| Static analysis | `examples/static_analysis/` | none (pure Python) | UART waveform-free analysis: hierarchy/drivers/fan-in/declarations |
| Tiny sample | `examples/make_sample.py` | optional vcd2fst | hand-written VCD → FST → session smoke test |

---

## Deployment modes

- **stdio (recommended)**: each user starts a local server subprocess that loads only their own
  module's FST + netlist. Zero ops.
- **HTTP + multi-session**: one long-running service, per-user isolated sessions via `session_id`.
  `python -m wave_mcp.server --transport http --host 0.0.0.0 --port 8000`
- **Air-gapped / offline bundle**: one command on a docker-equipped machine produces both
  glibc tiers; copy to the air-gapped side and install offline, with standalone Python +
  all wheels + optional vcd2fst and viewer assets:

  ```bash
  # 1) networked build machine (docker only): one command, both bundles
  deploy/docker_build_all.sh --viewer <asset_dir> --python <standalone-py-tarball-or-URL>
  # outputs: dist/wave-mcp-bundle-glibc2.28.tar.gz  (mainstream hosts)
  #          dist/wave-mcp-bundle-glibc2.17.tar.gz  (CentOS 7 legacy hosts)

  # 2) air-gapped shared drive: extract and install offline (no internet, no compiler, no docker)
  tar -xzf wave-mcp-bundle-glibc2.28.tar.gz -C /shared/ && cd /shared/wave-mcp-bundle-glibc2.28
  ./install.sh --prefix /shared/wave-mcp      # produces the bin/wave-mcp launcher
  ```

  If docker is not an option, the step-by-step `deploy/build_offline_bundle.sh` still works.
  See [`docs/DEPLOY_AIRGAP.md`](docs/DEPLOY_AIRGAP.md) (vcd2fst compatibility options + troubleshooting).

## Environment variables

**Most people set none of these.** A fresh install reads FST/VCD, does static analysis and
runs the wave viewer out of the box. Two cases need configuration: reading `.fsdb` (set
`VERDI_HOME`), and several people sharing one host (set `WAVE_MCP_SESSION_ROOT`). The rest
are tuning knobs for unusual environments, look them up when you hit one.

When you do set them, put them in the `env` block of your MCP client config rather than
`export`ing them in a shell: the agent spawns the server as a child process and does not
inherit your interactive shell's environment.

```json
{
  "mcpServers": {
    "wave-mcp": {
      "command": "wave-mcp",
      "env": {
        "VERDI_HOME": "/tools/synopsys/verdi/T-2022.06-SP1",
        "WAVE_MCP_SESSION_ROOT": "~/wave-sessions"
      }
    }
  }
}
```

| Variable | Configure? | Purpose | Default |
| --- | --- | --- | --- |
| `VERDI_HOME` | required for `.fsdb` | Verdi **installation root** (not the `bin/` directory holding the executable). `share/FsdbReader/linux64` is resolved under it; see the [FSDB guide](docs/FSDB_GUIDE.md) for usage and troubleshooting | empty. FSDB input unavailable, everything else works |
| `WAVE_MCP_SESSION_ROOT` | recommended on a shared host | Root for session directories. Once set, every `out_dir` lands inside it, so the deployment decides the location instead of the agent | empty. `out_dir` is used as given |
| `WAVE_MCP_VIEWER_PORT_BASE` | recommended on a shared host | Confines view ports to `[base, base+64)` so one `ssh -L` rule keeps working; give each user a non-overlapping window | empty. A random high port per view |
| `NOVAS_HOME` | no | Same meaning as `VERDI_HOME`, kept for older Verdi installs; `VERDI_HOME` wins when both are set | empty |
| `FSDB2FST_FREADER` | no | Points straight at a copied `share/FsdbReader` directory, for machines with the runtime but no full Verdi install | empty. Read from `VERDI_HOME` / `NOVAS_HOME` |
| `FSDB2FST_BIN` | no | Use a prebuilt `fsdb2fst` binary | empty. Auto-detected, built on demand at first conversion |
| `WAVE_MCP_FSDB2FST_AUTOBUILD` | no | Set to `0` to disable the first-run auto build | `1` (enabled) |
| `VCD2FST_BIN` | no | Path to the GTKWave `vcd2fst` executable | `vcd2fst` (found via `PATH`) |
| `WAVE_MCP_VIEWER_ASSETS` | no | Viewer asset directory (must contain `surver` and `wasm/index.html`); the offline bundle sets this for you | empty. Falls back to the pip assets package, then `~/.cache/wave-mcp/viewer/` |
| `WAVE_MCP_MAX_VIEWS` | no | Cap on concurrent views, closing the oldest past the cap; `0` removes the cap | `8` |
| `XDG_CACHE_HOME` | no | Cache root (`fsdb2fst` build output, viewer assets) | `~/.cache` |

**Session directory convention**: keep sessions under `~/wave-sessions/<project>_<module>/` and
reuse one `out_dir` per module so the static and waveform sessions share a netlist. Avoid `/tmp`
(lost on reboot, forcing re-elaboration) and shared drives (users collide on one directory).
Setting `WAVE_MCP_SESSION_ROOT` enforces this without relying on the agent's prompt.

## FAQ

**Q1: Does it support FSDB / SHM?**
FSDB yes, SHM no.

FSDB goes through the conversion path: `prepare_session` takes `.fsdb` directly and calls the
bundled `fsdb2fst` to write FST, with no VCD intermediate. The result is identical to a native
FST file, so every query tool works unchanged. Conversion only needs Verdi's FsdbReader runtime
on your own machine and **checks out no license at runtime**, see the
[FSDB guide](docs/FSDB_GUIDE.md).

SHM is not planned. Cadence Xcelium users do not need to convert existing waveforms: dump FST
straight from the simulator instead (the fstdumper VPI plugin, license-free, zero conversion),
see the [Xcelium FST guide](docs/XCELIUM_FST_GUIDE.md).

**Q2: Do I need a commercial license?**
No. MIT licensed, unlimited concurrency, unlimited machines. This is the core difference from
commercial debug MCPs.

Going open source is not only about saving license fees. Closed formats like FSDB and SHM cannot
be read in full detail without commercial tooling, and that license cost makes it hard to sustain
the high-concurrency, massive-volume waveform analysis that AI agents will generate once they are
deeply embedded in the verification workflow. wave-mcp takes the open FST + VCD route precisely
to serve thousands of concurrent waveform analyses with a high-performance open-source stack.

**Q3: Which simulators are supported?**
Any simulator that produces FST or VCD: Verilator (`--trace-fst`), Icarus (`-fst`),
Xcelium ([FST via fstdumper](docs/XCELIUM_FST_GUIDE.md)), VCS (VCD conversion; existing
FSDB via [fsdb2fst](docs/FSDB_GUIDE.md)), Questa (VCD conversion), etc.
wave-mcp never runs a simulator; it consumes the waveform you already produced.
See [simulator compatibility](docs/SIMULATOR_COMPATIBILITY.md) for the four intake paths
(direct FST / auto VCD conversion / FSDB conversion / Xcelium direct dump) and their
validation status.

**Q4: Is the data accurate?**
Yes. Fully validated on a real production chip project with 2.25M signals at 100% value
correctness; hierarchy and file tools (`scope_info` / `find_files` / `modules_in_file`) pass
on all 32/32 modules.

**Q5: Can I use it without a waveform?**
Yes. `open_static_session` does static analysis from RTL alone (before simulation), a unique
wave-mcp capability.

**Q6: Does it support macOS / Windows?**
Linux x86_64 works out of the box. Other platforms are not officially supported, but you can
adapt it yourself.

There is exactly one blocker: `pylibfst` currently publishes wheels for Linux x86_64 only.
Everything else is already covered (`pyslang` ships official macOS arm64 / universal2 /
win_amd64 / linux aarch64 wheels, and `mcp` is pure Python), so with a build toolchain in
place (cmake, a C compiler and zlib; MSVC on Windows) `pip install pylibfst` builds from the
sdist and usually succeeds, after which the analysis tools work normally.

The wave viewer definitely will not work: its `surver` backend is a Linux x86-64 binary with
no macOS or Windows build. When the assets are absent the viewer tools degrade gracefully with
a hint and the analysis tools are unaffected. To run it on your own platform you would build
surver yourself from the [Surfer project](https://surfer-project.org/) and point
`WAVE_MCP_VIEWER_ASSETS` at the asset directory, keeping in mind that surver and the WASM
client must come from the same Surfer commit or the client refuses to load on a wellen
version mismatch.

Running inside a container (e.g. `python:3.11-slim`) sidesteps all of this and is the easiest
route.

**Q7: How does it perform on large waveforms?**
FST + a C-based reader (pylibfst) + a resident process + random access, matching the AI's
point-query patterns; validated on million-scale-scope modules.

**Q8: How do I hook it into my Code Agent?**
See [Code Agent integration](#code-agent-integration): one `mcpServers` JSON block.

**Q9: My target machine only has Python 3.8 / 3.9. Can I still use it?**
Yes, via the offline bundle, with no upgrade needed on the target.

Offline bundles from the Docker pipeline embed a standalone Python 3.11
(python-build-standalone); the installer prefers the bundled interpreter and never touches the
system Python, so 3.8 / 3.9 hosts run it fine.

See section 7 of [`docs/DEPLOY_AIRGAP.md`](docs/DEPLOY_AIRGAP.md).

**Q10: I get `GLIBC_2.27' not found` on CentOS 7 / glibc 2.17. What now?**
Official pyslang wheels require glibc >= 2.28, so pip cannot work on legacy hosts. Use the
glibc 2.17 tier from the Docker pipeline: `deploy/docker_build_all.sh` builds the compatible
wheel inside a container automatically and assembles `wave-mcp-bundle-glibc2.17.tar.gz`;
the whole chain (standalone Python + wheels + vcd2fst + musl static surver) runs on
glibc >= 2.17, including CentOS 7. See sections 1.0 and 1c of
[`docs/DEPLOY_AIRGAP.md`](docs/DEPLOY_AIRGAP.md).

---

## Architecture

```
simulator → dump waveform (FST) → wave-mcp server (multi-source) → LLM client (MCP)
                                       ↑
                       FST waveform + pyslang RTL netlist
```

| Source | Implementation | Capabilities |
| --- | --- | --- |
| `fst_source.py` | `pylibfst` (fstapi, random access) + bus aggregation | hierarchy, signals, signal values |
| `netlist/` + `rtl_source.py` | **pyslang** (full elaboration) + FST | connectivity, drivers, fan-in/out, trace, files/declarations |
| `netlist/name_infer.py` | instance name → module definition name inference | module_type fallback |

A **session** = one isolated debug context, tied together by a `session.json` manifest.

## Implementation notes

- **No naive parsing of large VCDs** (slow, easy to OOM); uses **FST + a C-based reader library +
  a resident process + random access**.
- **The netlist is elaborated once and persisted for reuse**: the pyslang result is written to
  `netlist/maps.json` (DriverMap/FanInMap/LoadMap/LocMap + instance_tree), a plain JSON file any
  script can read. Loaded at startup, never rebuilt per query; unchanged sources skip
  re-elaboration, and upgrading a static session to a waveform session reuses the same netlist
  (freshness is checked against source file mtimes).
- **All generated artifacts live in the session directory**: `prepare_session` writes only to
  the `out_dir` you choose: `session.json` (manifest + fingerprints), `netlist/maps.json`
  (the netlist), plus a converted `.fst` only when the input is a VCD. Queries run entirely
  in memory with no on-disk index or cache, and nothing is written into your RTL sources or
  the original waveform directory; deleting the session directory is a complete cleanup.
- **MCP responses**: `structuredContent` (machine-readable) + `content[].text` human-readable text.

## License

This project is released under the **MIT** license (see [`LICENSE`](LICENSE)). All dependencies
are permissively licensed (MIT/BSD) with no copyleft contamination; the `vcd2fst` converter
shipped in the offline bundle is built from GTKWave's MIT sources.
See [`docs/THIRD_PARTY.md`](docs/THIRD_PARTY.md).

## Directory layout

```
wave_mcp/
  server.py              # MCP server, registers all 34 tools
  session.py             # Session / session.json / fingerprint check / definition_name
  pipeline.py            # prepare_session / prepare_static_session orchestration
  diff.py                # diff_waveforms first-divergence localization (clock-aligned sampling)
  sources/               # fst_source + rtl_source
  netlist/               # slang_netlist / trace_engine / expr_eval / name_infer
  viewer/                # wave viewer: manager / surver / translate / state / web frontend
  cli/                   # wave-session / wave-vcd2fst / wave-view
deploy/                  # offline bundle build + install (incl. Docker one-shot pipeline)
examples/                # example library (see table above)
tests/                   # regression entry run_regression.py
CHANGELOG.md             # release notes
docs/                    # DEPLOY_AIRGAP / SIMULATOR_COMPATIBILITY / FSDB_GUIDE / XCELIUM_FST_GUIDE / THIRD_PARTY / WAVE_VIEWER / VIEWER_SCREENSHOTS
```
