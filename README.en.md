# wave-mcp — Open-source, license-free MCP server for RTL waveform debug

<img src="docs/images/penglai-logo.png" alt="Penglai Lab" width="200"/>

English | [简体中文](README.md)

wave-mcp is an open-source RTL waveform debug MCP server from the **Penglai Lab** verification team at Tencent. It is an MCP toolkit that lets an LLM debug RTL waveforms using open-source data
sources — **FST waveforms + a pyslang RTL netlist**. **No commercial license required; unlimited
concurrency.** Released under the **MIT** license.

> As long as your simulator can dump **FST** (Verilator `--trace-fst`, Icarus,
> or by converting VCD to FST), wave-mcp can read it for debugging. It **does
> not run a simulator** — you produce the waveform with your own flow and hand
> the result to it.

---

## Features

- **Waveform queries**: design hierarchy, instances, signals (width/direction/
  type, with bus aggregation), signal values (point / range, random access).
- **Static analysis (pyslang netlist)**: connectivity, drivers, fan-in/fan-out,
  declaration location (file:line).
- **Value tracing**: `trace_value` walks the driver chain backwards, drilling
  across module boundaries, with a real FST value at every node; `trace_x`
  chases the root cause of an X.
- **Self-healing netlist**: automatically adds `+incdir+` / package sources from
  pyslang diagnostics and recompiles, auto-detects UVM directories; degrades
  gracefully on failure without affecting the other tools.
- **Consistency checks**: `session.json` records waveform/source fingerprints;
  if the source or waveform changes but the netlist is stale, it warns instead
  of silently returning wrong results.
- **Deployment-friendly**: stdio (one process per user, zero ops) / HTTP
  multi-session / self-contained offline bundle (air-gapped networks).

---

## Architecture

```
simulator → dump waveform (FST) → wave-mcp server (multi-source) → LLM client (MCP)
                                       ↑
                       FST waveform + pyslang RTL netlist
```

Data sources (`wave_mcp/sources/` + `wave_mcp/netlist/`):

| Source | Implementation | Capabilities |
| --- | --- | --- |
| `fst_source.py` | `pylibfst` (fstapi, random access) + bus aggregation | hierarchy, signals, signal values |
| `netlist/` + `rtl_source.py` | **pyslang** (full elaboration) + FST | connectivity, drivers, fan-in/out, trace, files/declarations |
| `netlist/name_infer.py` | instance name → module definition name inference | fills in module_type when the netlist doesn't cover it |

A **session** = one isolated debug context (one user, one module), tied together
by a `session.json` manifest that binds the data sources.

---

## Install

```bash
# install from git (works out of the box on Linux x86_64; deps: mcp + pylibfst + pyslang)
pip install git+https://github.com/Tencent/wave-mcp.git
# or clone and install locally:
#   git clone <repo> && cd wave-mcp && pip install -e .

# system binary (optional): vcd2fst (GTKWave, for VCD→FST; not needed if you already have FST)
#   Debian/Ubuntu: sudo apt install gtkwave   |   macOS: brew install gtkwave
```

> **Platform support**: **Linux x86_64** has prebuilt wheels for all
> dependencies, so `pip` works out of the box (tested on Python 3.9–3.13).
> macOS / Windows / arm64 currently lack a prebuilt `pylibfst` wheel and require
> building from source (cmake+gcc+zlib).
> For air-gapped / offline environments, see [`docs/DEPLOY_AIRGAP.md`](docs/DEPLOY_AIRGAP.md).

---

## Quickstart

**Open-source, no commercial simulator needed** — produce a real FST with
Verilator and open it for analysis, in one command:

```bash
python examples/verilator_quickstart/run.py   # needs verilator>=5; see the README in that directory
```

Or use the built-in tiny sample (hand-written VCD → vcd2fst → FST):

```bash
# 1) generate the sample
python examples/make_sample.py

# 2) build a session
python -m wave_mcp.cli.build_session \
    --fst examples/sample/dump.fst \
    --top top_tb --filelist examples/sample/rtl.f \
    --out examples/sample/session

# 3) end-to-end smoke test
python tests/smoke_test.py

# 4) start the MCP server (stdio, recommended: one process per user)
python -m wave_mcp.server --session examples/sample/session
```

---

## Standard workflow (single entry point for waveform analysis)

`prepare_session` is the unified entry point — **call it first** when you want
to start analyzing a waveform. Pass in the waveform your simulator already
produced, and in one shot it does "(convert →) build netlist → build session →
open", after which you can query directly.

```
prepare_session ─┬─ waveform-file entry         # .fst read directly / .vcd auto-converted
                 ├─ convert VCD → FST           # only when a .vcd is given; default speed(fastlz)
                 ├─ build netlist (pyslang)     # optional; enables connectivity/drivers/trace
                 ├─ build session.json + fingerprints
                 └─ open session                # then use the query tools
```

Example call:

```jsonc
prepare_session({
  "out_dir":      "sessions/my_module",
  "wave_path":    "sim/dump.fst",          // waveform from the sim: .fst read directly / .vcd auto-converted
  "top":          "top_tb",
  "filelist_path":"rtl.f",                 // same filelist as the sim (enables netlist/declaration tools)
  "mode":         "speed"                   // VCD->FST: speed/balanced/size (only applies to .vcd)
})
// after it returns "ready" + a session summary, call signal_values / list_child_instances ...
```

> A `.fst` is read directly with zero conversion; a `.vcd` is auto-converted to
> FST (roughly 1/50 the size of VCD).
> You can also split it up: `convert_vcd_to_fst` → `open_session`.

### VCD → FST conversion

If your simulator only dumps VCD, convert it to FST first (~1/50 the size, fast
random access). Three entry points:

```bash
# post-process conversion (fastest params: mode=speed=fastlz + parallel)
wave-vcd2fst --vcd sim/dump.vcd --fst sim/dump.fst --mode speed
#   mode: speed(fastlz, fastest) / balanced(lz4) / size(zlib, smallest)

# streaming conversion — hides conversion time inside sim time; FST is ready almost when the sim ends
wave-vcd2fst --stream --vcd sim/dump.vcd --fst sim/dump.fst   # creates a FIFO + starts vcd2fst in the background
#   then point $dumpfile("sim/dump.vcd") in your TB at that FIFO and run the sim as usual

# do it in one step while building a session (auto-convert + package)
wave-session --vcd sim/dump.vcd --top top_tb --filelist rtl.f --out sessions/mod
```

### MCP client configuration (stdio)

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

---

## Deployment modes

- **stdio (recommended)**: each user starts a local server subprocess that loads
  only their own module's FST + netlist. Zero ops.
  `python -m wave_mcp.server --session <session_dir>`
- **HTTP + multi-session**: one long-running service that isolates each user's
  session via `session_id`.
  `python -m wave_mcp.server --transport http --host 0.0.0.0 --port 8000`
  Every tool accepts an optional `session_id`; call
  `open_session(session_path, session_id=...)` first, then the other tools.
- **Air-gapped / offline self-contained bundle**: on a networked machine run
  `deploy/build_offline_bundle.sh` to produce a self-contained bundle (bundled
  standalone Python + all wheels + optional vcd2fst), copy it to the target
  machine and install offline with `install.sh`.
  See [`docs/DEPLOY_AIRGAP.md`](docs/DEPLOY_AIRGAP.md).

---

## Tools (25)

| Category | Tools | Notes |
| --- | --- | --- |
| Waveform prep | `prepare_session` / `convert_vcd_to_fst` | waveform-file entry (.fst read directly / .vcd auto-converted) → session in one shot; never runs a simulator |
| Session mgmt | `open_session` / `close_session` / `session_info` | `session_info` includes netlist_health + definition_coverage |
| Hierarchy | `list_child_instances` / `list_modules` / `instances_of_module`(`_matching`) / `scope_info` | module definition name via three-layer resolution: pyslang netlist → name inference → manual scope_map |
| Signals | `list_signals` / `signal_info` | width/direction/type from FST (with bus aggregation); declaration file+line from the netlist |
| Signal values | `signal_values` / `signal_values_in_range` | FST's strength, random access |
| Connectivity/drivers | `signal_connectivity` / `signal_drivers` / `signal_loads` / `signal_fanin` / `active_drivers` / `driver_contributors` | pyslang netlist (statically precise) + 4-value branch-condition evaluation to pick the active driver; degrades gracefully without a netlist |
| Value tracing | `trace_value` / `trace_x` | pyslang netlist × FST value back-traversal, cross-module drill-down; trace_x is approximate |
| Files | `list_files` / `find_files` / `modules_in_file` | filelist + pyslang netlist |

> The connectivity/drivers and tracing categories require the pyslang netlist to
> be built (pass the right filelist/incdirs/defines to `prepare_session`).
> `active_drivers` / `trace_x` become value-informed approximations when a
> condition is X or an expression is outside the 4-value evaluation subset; they
> annotate `selection_method`, but always provide a precise static driver chain +
> per-node FST value + code location.

---

## Implementation notes

- **No naive parsing of large VCDs** (slow, easy to OOM); instead uses **FST + a
  C-based reader library (pylibfst) + a resident process + random access**,
  which fits the AI's point-query / search patterns.
- **The netlist is built once offline and persisted** (`maps.json`:
  DriverMap/FanInMap/LoadMap/LocMap + instance_tree); the server loads it at
  startup instead of rebuilding each time.
- **Three-layer definition_name resolution**: netlist (with anchor-based upward
  inference) → name inference (with an interface guard and confidence tiers) →
  manual `scope_map`.
- **MCP returns**: `structuredContent` (machine-readable) + `content[].text`
  human-readable text (no escaped `\n` / `\"`).

---

## Directory layout

```
wave_mcp/
  server.py              # MCP server, registers all 25 tools (FastMCP)
  session.py             # Session / SessionManager / session.json / fingerprint check / 3-layer definition_name
  pipeline.py            # prepare_session: waveform-file entry (.fst direct / .vcd auto-convert) → netlist → session
  convert.py             # vcd2fst wrapper: parallel capability probe + serial fallback + FIFO streaming
  timeutil.py            # time-string <-> FST time-unit conversion
  sources/
    fst_source.py        # pylibfst: hierarchy / signals / values / bus aggregation
    rtl_source.py        # pyslang netlist loading + queries: connectivity/drivers/trace/files/netlist_health
  netlist/
    slang_netlist.py     # pyslang elaboration → maps.json + self-healing + UVM detection
    trace_engine.py      # structure × time trace engine + definition_name resolution
    expr_eval.py         # 4-value (0/1/x/z) branch-condition evaluation
    name_infer.py        # instance name → module definition name inference
  cli/build_session.py   # wave-session: assemble a session dir + fingerprints
  cli/vcd2fst.py         # wave-vcd2fst: VCD→FST (incl. streaming)
deploy/                  # offline bundle build + install scripts (see docs/DEPLOY_AIRGAP.md)
examples/make_sample.py             # generate a tiny sample
examples/verilator_quickstart/      # Verilator out-of-the-box example (--trace-fst produces a real FST, no xrun)
tests/                              # smoke_test / unit tests
LICENSE                            # MIT
licenses/THIRD_PARTY.md            # third-party component license notices
```

---

## License

This project is released under the **MIT** license (see [`LICENSE`](LICENSE)).

All dependencies and bundled components are permissively licensed with no
copyleft contamination: `mcp` / `pyslang` / `pylibfst` are all MIT/BSD. The
`vcd2fst` converter shipped in the offline bundle is built from GTKWave's **MIT**
sources (libfst/fstapi + the vcd2fst helper) and invoked as a separate process
(an aggregation relationship that does not affect wave-mcp's MIT license); its
embedded `jrb` component is LGPL-2.1, and the build script is shipped with the
bundle to satisfy the relink obligation.
See [`licenses/THIRD_PARTY.md`](licenses/THIRD_PARTY.md).
