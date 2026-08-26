# wave-mcp — Open-source, license-free MCP server for RTL waveform debug

<img src="docs/images/penglai-logo.png" alt="Penglai Lab" width="200"/>

[![PyPI version](https://img.shields.io/pypi/v/wave-mcp)](https://pypi.org/project/wave-mcp/)
[![Python versions](https://img.shields.io/pypi/pyversions/wave-mcp)](https://pypi.org/project/wave-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

English | [简体中文](README.md)

**wave-mcp is an open-source RTL waveform debug MCP server from the Penglai Lab verification team
at Tencent** — a debugging toolkit for LLMs: it reads **FST waveforms + an RTL netlist** and
provides **27 MCP tools** for hierarchy exploration, signal queries, driver analysis, and
value/X tracing. **MIT licensed — no commercial license required, unlimited concurrency.**

> As long as your simulator can dump **FST** (Verilator `--trace-fst`, Icarus, or by converting
> VCD to FST), wave-mcp can read it for debugging. It **does not run a simulator** — you produce
> the waveform with your own flow and hand the result to it.

---

## Why wave-mcp

Chip verification takes more than 50% of the development cycle, and waveform debugging is the
highest-frequency activity in it. In the LLM era, engineers want AI agents to read waveforms,
query signals, and trace X root causes — but commercial debug MCPs require expensive licenses
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
| Tool coverage | All 27/27 tools exercised |

![Tool call distribution](docs/images/tool-calls-distribution.png)

## Features

- **Waveform queries**: design hierarchy, instances, signals (width/direction/type, with bus
  aggregation), signal values (point / range, random access).
- **Static analysis (pyslang netlist)**: connectivity, drivers, fan-in/fan-out, declaration
  locations (file:line).
- **Waveform-free static analysis**: `open_static_session` builds a session from RTL alone —
  **analyze the design structure before simulation**.
- **Value tracing**: `trace_value` walks the driver chain backwards across module boundaries with
  a real FST value at every node; `trace_x` chases X root causes.
- **Self-healing netlist**: auto-adds `+incdir+` / package sources from pyslang diagnostics and
  recompiles; degrades gracefully without affecting other tools.
- **Consistency checks**: warns when the source or waveform changed but the netlist is stale —
  never silently returns wrong results.
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

> **Linux x86_64 works out of the box** (all Python deps above ship prebuilt wheels).
> macOS / Windows / arm64 currently lack a prebuilt `pylibfst` wheel and require building from
> source (cmake+gcc+zlib). For air-gapped / offline environments, see
> [`docs/DEPLOY_AIRGAP.md`](docs/DEPLOY_AIRGAP.md).

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

Or call the `prepare_session` MCP tool directly from your Code Agent — see below.

## Code Agent integration

`prepare_session` is the unified MCP entry point. When your Code Agent wants to analyze a
waveform, **call it first** — pass in the waveform your simulator produced, and in one shot it
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
- **Cursor**: write to `.cursor/mcp.json`
- **VS Code Copilot**: write to `.vscode/mcp.json`
- Other agents (Gemini CLI / Qwen Code / OpenHands, etc.): paste the JSON above into their
  respective `mcpServers` configuration

### Waveform-free static analysis (usable before simulation)

`open_static_session` builds a netlist and opens a session from RTL alone — **no waveform, no
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
to upgrade to a full session — the built netlist is reused.

### VCD → FST conversion (vcd2fst setup, optional)

If your simulator only dumps VCD (e.g. xrun), convert it to FST first: **~1/50 the size, fast
random access**. Conversion relies on the `vcd2fst` tool shipped with GTKWave:

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

## Tools (27, in 8 categories)

| Category | Tools | Notes |
| --- | --- | --- |
| Waveform prep | `prepare_session` / `open_static_session` / `convert_vcd_to_fst` | waveform → session in one shot; static analysis needs no waveform; never runs a simulator |
| Session mgmt | `open_session` / `close_session` / `session_info` | `session_info` includes netlist_health + definition_coverage |
| Hierarchy | `list_child_instances` / `list_modules` / `instances_of_module`(`_matching`) / `scope_info` | three-layer module-def resolution: netlist → name inference → scope_map |
| Signals | `list_signals` / `signal_info` | width/direction/type from FST (with bus aggregation); declaration from the netlist |
| Values | `signal_values` / `signal_values_in_range` / `signal_value_at` | FST's strength, random access |
| Driver analysis | `signal_connectivity` / `signal_drivers` / `signal_loads` / `signal_fanin` / `active_drivers` / `driver_contributors` | pyslang netlist (statically precise) + 4-value branch evaluation |
| Value / X tracing | `trace_value` / `trace_x` | netlist × FST back-traversal, cross-module drill-down |
| Files | `list_files` / `find_files` / `modules_in_file` | filelist + pyslang netlist |

> The driver-analysis and tracing categories require the pyslang netlist (pass the right
> filelist/incdirs/defines to `prepare_session`).

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
- **Air-gapped / offline bundle**: one-command packaging on a networked machine, then offline
  install on the air-gapped side, with standalone Python + all wheels + optional vcd2fst:

  ```bash
  # 1) networked dev machine: one-command packaging (optional --python <standalone py> --vcd2fst <binary>)
  deploy/build_offline_bundle.sh --out /tmp/wave-mcp-bundle

  # 2) air-gapped shared drive: extract and install offline (no internet, no compiler)
  tar -xzf wave-mcp-bundle.tar.gz -C /shared/ && cd /shared/wave-mcp-bundle
  ./install.sh --prefix /shared/wave-mcp      # produces the bin/wave-mcp launcher
  ```

  See [`docs/DEPLOY_AIRGAP.md`](docs/DEPLOY_AIRGAP.md) (vcd2fst compatibility options + troubleshooting).

## FAQ

**Q1: Do I need a commercial license?**
No. MIT licensed, unlimited concurrency, unlimited machines. This is the core difference from
commercial debug MCPs.

**Q2: Which simulators are supported?**
Any simulator that produces FST or VCD: Verilator (`--trace-fst`), Icarus, xrun, VCS, etc.
wave-mcp never runs a simulator — it consumes the waveform you already produced.

**Q3: Is the data accurate?**
Yes. Fully validated on a real production chip project with 2.25M signals at 100% value
correctness; hierarchy and file tools (`scope_info` / `find_files` / `modules_in_file`) pass
on all 32/32 modules.

**Q4: Can I use it without a waveform?**
Yes. `open_static_session` does static analysis from RTL alone (before simulation) — a unique
wave-mcp capability.

**Q5: Does it support macOS / Windows?**
Linux x86_64 works out of the box. macOS / Windows / arm64 require building `pylibfst` from
source (see [System requirements](#system-requirements)).

**Q6: How does it perform on large waveforms?**
FST + a C-based reader (pylibfst) + a resident process + random access, matching the AI's
point-query patterns; validated on million-scale-scope modules.

**Q7: How do I hook it into my Code Agent?**
See [Code Agent integration](#code-agent-integration) — one `mcpServers` JSON block.

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
- **The netlist is built once offline and persisted** (`maps.json`: DriverMap/FanInMap/LoadMap/
  LocMap + instance_tree); loaded at startup, never rebuilt per query.
- **MCP responses**: `structuredContent` (machine-readable) + `content[].text` human-readable text.

## License

This project is released under the **MIT** license (see [`LICENSE`](LICENSE)). All dependencies
are permissively licensed (MIT/BSD) with no copyleft contamination; the `vcd2fst` converter
shipped in the offline bundle is built from GTKWave's MIT sources.
See [`docs/THIRD_PARTY.md`](docs/THIRD_PARTY.md).

## Directory layout

```
wave_mcp/
  server.py              # MCP server, registers all 27 tools
  session.py             # Session / session.json / fingerprint check / definition_name
  pipeline.py            # prepare_session / prepare_static_session orchestration
  sources/               # fst_source + rtl_source
  netlist/               # slang_netlist / trace_engine / expr_eval / name_infer
  cli/                   # wave-session / wave-vcd2fst
deploy/                  # offline bundle build + install
examples/                # example library (see table above)
tests/                   # regression entry run_regression.py
docs/                    # DEPLOY_AIRGAP / SIMULATOR_COMPATIBILITY / THIRD_PARTY
```
