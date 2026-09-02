# Wave Viewer (wave-view): The Complete Guide

[中文版](WAVE_VIEWER.md)

wave-mcp's analysis tools answer "why is it wrong"; the wave viewer makes you see it with your own eyes. Once the agent has located the failure time, a single `open_wave_view` pops a waveform in your browser: suspect signals already added, cursor pinned at the failure time, and the agent's analysis note in a popup right next to it. As you drag the cursor around, the agent can even tell where you are looking via `get_view_state` and continue the conversation from there.

This guide covers installation, the CLI, the three MCP tools, agent workflows, architecture, deployment scenarios and troubleshooting. For a quick start, the "Wave viewer" section of the [README](../README.en.md) is enough; this document is the full detail.

## Contents

1. [What it does](#1-what-it-does)
2. [Installation](#2-installation)
3. [CLI usage (wave-view)](#3-cli-usage-wave-view)
4. [MCP tools in detail](#4-mcp-tools-in-detail)
5. [Typical agent workflows](#5-typical-agent-workflows)
6. [Analysis popup and time anchors](#6-analysis-popup-and-time-anchors)
7. [Dual-waveform compare view](#7-dual-waveform-compare-view)
8. [Flicker-free updates](#8-flicker-free-updates)
9. [Architecture](#9-architecture)
10. [Deployment scenarios](#10-deployment-scenarios)
11. [Licensing](#11-licensing)
12. [Troubleshooting](#12-troubleshooting)
13. [Known limitations](#13-known-limitations)

## 1. What it does

- **Tens-of-GB FSTs open in seconds**: waveform data never enters the browser. A local surver process streams it on demand, and the browser fetches only what is currently on screen. Open time is essentially independent of file size.
- **Agent-driven presentation**: signal list, cursor position, visible time window, timeline markers and the analysis note are all set by the agent through MCP tools. The user opens the link and sees the conclusion.
- **Dual-waveform compare**: pass/fail waveforms in two stacked panes with lockstep zoom and cursor sync; combined with `diff_waveforms`, a red marker lands on the first divergence automatically.
- **Two-way awareness**: `get_view_state` reports the user's current cursor position, displayed signals and viewport back to the agent, enabling "I explain what you are looking at" conversational debugging.
- **Flicker-free updates**: when the agent later adjusts the cursor/window/markers/notes, the page does not reload and the waveform does not flash.

The viewer is optional: without the assets package, the three viewer tools return a clear hint and degrade gracefully, and the 28 analysis tools are completely unaffected.

## 2. Installation

The viewer needs a separate assets package (the Surfer WASM frontend + the surver streaming backend):

```bash
pip install wave-mcp[viewer]
```

Assets are discovered in this order (first hit wins):

1. the directory pointed to by the `WAVE_MCP_VIEWER_ASSETS` env var (set automatically by the offline bundle installer)
2. the pip-installed `wave-mcp-viewer-assets` package
3. the user cache directory `~/.cache/wave-mcp/viewer/`

A valid asset directory contains an executable `surver` and `wasm/index.html`. Air-gapped environments simply build the offline bundle with `deploy/build_offline_bundle.sh --viewer <asset_dir>`; see [DEPLOY_AIRGAP.md](DEPLOY_AIRGAP.md).

**System requirements**: the surver binary in the assets package is a musl static build with no glibc dependency, so it runs on any x86-64 Linux including legacy hosts such as CentOS 7. The glibc 2.17 offline bundle exists because of `pyslang`, not surver. On the browser side, any modern WASM-capable browser works; the server machine itself needs no graphical environment.

## 3. CLI usage (wave-view)

You can open waveforms directly, without any agent:

```bash
# open one waveform
wave-view dump.fst

# with initial signals and a cursor position
wave-view dump.fst --signals top.clk top.u_dma.req_valid --cursor 1523400ps

# dual-waveform compare
wave-view pass.fst fail.fst --labels pass fail

# print the URL only, don't try to launch a browser
wave-view dump.fst --no-browser
```

Arguments:

| Argument | Meaning |
| --- | --- |
| `fst` (positional) | 1 or 2 FST paths; 2 paths enter the compare view |
| `--signals` | full signal paths to add initially, space separated |
| `--cursor` | initial cursor time, `number+unit`, e.g. `1523400ps`, `12ns` |
| `--labels` | display label per waveform; `pass fail` recommended for compare views |
| `--no-browser` | don't try to launch a local browser |

The command prints three lines: the browser URL, the native Surfer client connection (`surfer <token_url>`), and an SSH port-forward command. On desktops (with `DISPLAY`) it launches the browser via `xdg-open`; in SSH / code-agent sessions, IDE terminals (VS Code, Cursor, etc.) auto-forward localhost ports, so just click the URL. The process stays in the foreground; Ctrl-C exits and reaps all child processes.

## 4. MCP tools in detail

### 4.1 open_wave_view

Opens a waveform view (or a pair) and returns the URL for the user.

```jsonc
open_wave_view({
  "fst_paths": ["sim/fail.fst"],            // 1 = normal view, 2 = compare view
  "signals": [                              // initial signals
    {"path": "top.u_dma.req_valid", "color": "red"},
    {"path": "top.u_dma.grant", "group": "handshake"}
  ],
  "cursor":   {"time": "1523400", "unit": "ps"},   // pin the cursor at the failure time
  "viewport": {"from": "1500000", "to": "1600000", "unit": "ps"},
  "markers":  [{"time": "1523400", "unit": "ps", "label": "req without grant", "color": "red"}],
  "annotation": {                           // analysis note, shown in the popup
    "markdown": "At [1523400ps](#t=1523400ps) req_valid rises but grant is missing…",
    "confidence": "high",
    "evidence": ["signal_fanin: …", "active_drivers: …"]
  }
})
```

Returns:

```jsonc
{
  "available": true,
  "view_id": "a1b2c3d4",        // for later update / get_state calls
  "url": "http://127.0.0.1:NNNNN/view.html?token=…",
  "native_hint": "surfer http://127.0.0.1:MMMMM/TOKEN",   // native client connection
  "ssh_hint": "ssh -L NNNNN:localhost:NNNNN <this-host>"  // manual forwarding command
}
```

Field notes:

- Each `signals` entry is `{path, color?, group?, format?, source?}`. In compare views, `source: "a"/"b"` assigns a signal to one waveform; by default it is added to both.
- The `diff` parameter takes a `diff_waveforms` result reference `{source_a, source_b, first_divergence}` and automatically places a red marker at the first divergence, no manual conversion needed.
- `labels` names each waveform, same as the CLI `--labels`.
- If assets are missing or surver fails to start, the tool returns `{"available": false, "hint": …}` instead of raising.

### 4.2 update_wave_view

Updates an open view in place: same URL, no reload on the user side.

```jsonc
update_wave_view({
  "view_id": "a1b2c3d4",
  "cursor": {"time": "1531200", "unit": "ps"},
  "annotation": {"markdown": "Looking further at [1531200ps](#t=1531200ps), grant is masked by arb_mask…"}
})
```

Omitted parameters keep their current value. `signals` / `markers` replace entirely when passed. `annotation` is the exception: it appends to the analysis log popup, building a timeline-style record of the debug session. Returns an incrementing `revision`, useful to confirm the frontend has applied the change.

### 4.3 get_view_state

Reads what the user is actually looking at:

```jsonc
get_view_state({"view_id": "a1b2c3d4"})
// returns
{
  "available": true,
  "revision": 7,
  "actual": {                       // written back by the browser
    "cursor": {"time": "1544800", "unit": "ps"},
    "viewport": {…},
    "selected_signals": […],
    "displayed_signals": […],
    "user_dirty": true              // the user has touched the view (vs. agent-set state)
  },
  "desired_summary": {…}            // summary of the agent-side desired state, for comparison
}
```

Typical use: the user says "the value at my cursor looks wrong". The agent first calls `get_view_state` to grab the cursor time, then continues with `signal_value_at` / `active_drivers` from that moment. `user_dirty: true` means the user has manually adjusted the view; before updating, the agent may choose to respect the user's current viewpoint and only touch markers and notes without stealing the cursor.

### 4.4 list_wave_views / close_wave_view

`list_wave_views()` returns every open view (`view_id`, `url`, `title`, `fst_paths`, `revision`, `surver_alive`), newest first. `close_wave_view(view_id)` closes one; `close_wave_view(all_views=True)` closes them all. These matter for batch work such as regression triage, where views would otherwise pile up.

Two details worth knowing. First, a streaming backend is shared by waveform file set, so closing a view only stops that backend when it was the last user of it; the response reports this as `surver_stopped`. Second, there is a safety cap: at most 8 views are kept open by default and the oldest is evicted beyond that. Tune it with `WAVE_MCP_MAX_VIEWS`, or set 0 to disable the cap.

### 4.5 Port configuration

By default each view takes two random high ports (page server plus streaming backend). That is the least hassle locally, where you never think about port conflicts.

It gets annoying when you work on a remote host and forward with `ssh -L`, because the port changes on every view and no fixed forwarding rule can be prepared in advance. Set a port base and views are confined to `[base, base + 64)`:

```bash
export WAVE_MCP_VIEWER_PORT_BASE=45400
ssh -L 45400:localhost:45400 -L 45401:localhost:45401 <host>
```

The window is 64 because each view uses two ports, which comfortably covers the default 8-view cap. If the whole window is taken, allocation falls back to an ephemeral port instead of failing to open the view. An invalid base (non-numeric, below 1024, or out of range) falls back the same way.

## 5. Typical agent workflows

**Scenario 1: single-waveform root-cause presentation**

```
a case fails
→ prepare_session(fail.fst, …)
→ trace_x / signal_fanin / active_drivers locate the root-cause time and signal
→ open_wave_view(fail.fst, suspect signals + cursor pinned at the failure + annotation)
→ the user opens the URL; what they see is the conclusion
```

**Scenario 2: pass/fail comparison loop**

```
one passing and one failing waveform of the same case
→ diff_waveforms(pass.fst, fail.fst, scope="top.u_dma", clock="top.clk")
    returns the first divergence time + earliest diverging signals
    (prime suspects; later divergers are usually downstream contagion)
→ signal_fanin / active_drivers backtrack the earliest diverger
→ open_wave_view([pass.fst, fail.fst], diff=…, annotation=…)
    two-pane compare + red marker at the divergence + conclusion popup, all at once
```

**Scenario 3: conversational two-way debugging**

```
the user browses around dragging the cursor
→ user: "why is grant 0 here at my cursor?"
→ agent: get_view_state to grab the cursor time
→ active_drivers(grant, t=cursor time) to find which driving statement is active
→ update_wave_view appends an annotation + adds a marker at the key moment
  (without disturbing the user's viewpoint)
```

## 6. Analysis popup and time anchors

Annotations render in a log popup next to the waveform; it collapses into a floating capsule so it never blocks the view. Each annotation supports:

- `markdown`: the body, basic Markdown supported.
- `confidence`: a confidence tag (e.g. high / medium / low) to help the user judge how strong the conclusion is.
- `evidence`: a list of evidence items, typically key excerpts from analysis tool results.

Time references in the body use the anchor format `[85000ps](#t=85000ps)`, rendered as clickable links: one click jumps the cursor to that moment. This turns the analysis note into an interactive table of contents; the user clicks through the agent's reasoning chain step by step, and the waveform follows along.

`update_wave_view` annotations append, so multi-round analysis naturally builds a timestamped debug log that stays scrollable in the popup.

## 7. Dual-waveform compare view

Passing two paths in `fst_paths` enters the compare view: two stacked panes, one waveform each, with zoom, pan and cursor in full lockstep. The cursor points at the same moment in both waveforms, so visual alignment takes zero effort.

- Pass explicit `labels` like `["pass", "fail"]` so the pane titles are self-explanatory.
- By default `signals` adds the same-named signal to both panes; set `source` to add it to one side only.
- Feeding the `diff` parameter with a `diff_waveforms` result places a red marker at the first divergence, visible in both panes.
- Both waveforms are served by one surver process (reused per file set), so memory does not grow linearly with view count.

## 8. Flicker-free updates

After `update_wave_view`, the user's page does not reload and the waveform does not flash. Updates flow through two channels:

- **Cursor / viewport / markers**: delivered through Surfer's runtime message injection; the canvas moves in place, zero flicker.
- **Signal add/remove**: rebuilt through Surfer's startup command layer; popup and view state are preserved.
- **Annotations**: go to the log popup only, never touching the waveform.

Under the hood, the frontend shell long-polls `/api/view-state` (25-second hold, returns immediately on change), then dispatches desired-state deltas through the channels above. Meanwhile it writes Surfer's actual cursor state back to `/api/view-state/actual` about once a second; that is the `actual` you read via `get_view_state`. Desired and actual are stored separately, so the agent's intent and the user's interactions never overwrite each other.

## 9. Architecture

```
open_wave_view / wave-view CLI
        │
        ▼
ViewManager (singleton, view_id registry)
        │ per view
        ├── SurverInstance: surver subprocess, 127.0.0.1 only,
        │     random high port + random token; streams waveform data,
        │     reused per FST file set
        ├── ViewerServer: local HTTP, hosts the shell frontend + Surfer WASM,
        │     reverse-proxies surver, serves /api/view-state
        │     (GET long-poll / PUT desired / POST actual)
        └── ViewState: desired (agent intent) and actual (browser write-back)
              dual state + revision
```

Key design choices:

- **The waveform never enters the browser.** Surfer WASM only renders; surver streams data on demand. Browser memory is decoupled from file size, which is exactly why tens-of-GB FSTs open in seconds.
- **Target only Surfer's stable command layer** (startup commands) plus runtime message injection; deliberately avoid depending on GUI internals, keeping the adaptation surface minimal across Surfer upgrades.
- **Secure by default**: surver and ViewerServer listen on 127.0.0.1 only, URLs carry a random token, and remote access goes explicitly through SSH port forwarding. No bare port is ever exposed.
- **Lifecycle tied to the host process**: when the MCP server or CLI exits, all surver children are reaped together; no orphan processes.
- **Graceful degradation**: missing assets, surver startup failure or an unknown view_id all return a structured `available: false` with an actionable hint; the analysis tools are never dragged down.

Code lives in [wave_mcp/viewer/](../wave_mcp/viewer/) (`manager.py` view orchestration, `surver.py` subprocess management, `server.py` local HTTP, `state.py` dual-state model, `translate.py` Surfer command translation, `web/` the frontend shell); the MCP tools are registered in [wave_mcp/server.py](../wave_mcp/server.py).

## 10. Deployment scenarios

**Local desktop**: with `DISPLAY` set, the CLI launches the browser via `xdg-open`; MCP tools return the URL for the agent to relay.

**SSH / remote development (most common)**: integrated terminals of VS Code, Cursor etc. auto-forward localhost ports, so the URL from the agent just works. In a plain SSH terminal, use the `ssh_hint` command from the return value:

```bash
ssh -L <port>:localhost:<port> <server>   # then open the URL in your local browser
```

**Native Surfer client**: if the Surfer desktop app is installed locally, connect straight to surver with the `native_hint` (`surfer <token_url>`); the experience matches the browser.

**Air-gapped networks**: build the offline bundle with `--viewer` to pack the assets; the installer sets `WAVE_MCP_VIEWER_ASSETS` automatically. Use the musl static surver on old hosts. See [DEPLOY_AIRGAP.md](DEPLOY_AIRGAP.md).

**Several people on one host**: have each person run their own wave-mcp under their own account rather than sharing a single server process. Views and streaming backends bind to loopback, so the processes cannot see each other and isolation comes from accounts and file permissions. No extra configuration needed.

The one thing to agree on is ports. Random ports never collide by themselves, but if everyone wants fixed ports for `ssh -L` forwarding, give each person a non-overlapping window of 64:

```bash
# user A, ~/.bashrc
export WAVE_MCP_VIEWER_PORT_BASE=45400   # uses 45400-45463
# user B, ~/.bashrc
export WAVE_MCP_VIEWER_PORT_BASE=45500   # uses 45500-45563
```

Two more notes. Conversion artifacts are cached next to the source waveform, so several people analysing the same regression dump share one `.fst`, which saves time but requires that directory to be writable by them; when it is read-only, each falls back to its own session directory and converts separately, with no loss of function. And `WAVE_MCP_MAX_VIEWS` is a per-process cap rather than a per-host one, so keep an eye on the total number of browser and backend processes when several people work at once.

If what you want is one server on a host handing out links to other people, that is not supported yet: the viewer binds to loopback only. That mode is on the roadmap.

## 11. Licensing

The wave-mcp core (including the viewer's Python orchestration layer and the frontend shell) is MIT. The Surfer WASM bundle and the surver binary come from the [Surfer project](https://surfer-project.org/) under EUPL-1.2, and are therefore distributed in a **separate** `wave-mcp-viewer-assets` package. The relationship with the MIT core is aggregation, not linking, so the core license is unaffected. Skipping the assets package costs zero core functionality. Build reproduction paths and the full legal notes are in [THIRD_PARTY.md](THIRD_PARTY.md).

## 12. Troubleshooting

**Tools return "viewer assets not found"**
Follow the hint: `pip install wave-mcp[viewer]`, or point `WAVE_MCP_VIEWER_ASSETS` at an asset directory, or place assets under `~/.cache/wave-mcp/viewer/`. Verify the directory contains an executable `surver` and `wasm/index.html`.

**surver exited early / did not become ready**
First run `<asset_dir>/surver --help` manually to see whether it executes at all. The bundled surver is statically linked with no glibc dependency, so a failure to execute is usually not a library version problem: check first that the executable bit survived the copy (`chmod +x`), and that it was not replaced by a dynamically linked build of your own (`file surver` should report static-pie). Otherwise check that the FST path exists and the file is intact.

**URL won't open (remote scenarios)**
The service listens on 127.0.0.1 only, which is intended. IDE terminals usually auto-forward; if not, set up forwarding manually with the returned `ssh_hint` and open the URL from your local browser.

**Page opens but no waveform**
Make sure you opened the full URL including `?token=`; surver refuses to serve data on a wrong token. If the browser console reports a WASM load failure, check that the assets package version matches wave-mcp (reinstall `wave-mcp[viewer]`).

**update_wave_view reports unknown view_id**
A view's lifecycle follows the process that opened it. After an MCP server restart, old view_ids are gone; just call `open_wave_view` again. The `known_views` field in the response lists currently valid views.

## 13. Known limitations

- Signal add/remove goes through the startup command layer; with many signals there is one noticeable list reload (cursor/viewport/marker updates are always flicker-free).
- The `actual` in `get_view_state` is written back by the browser about once a second, so the cursor position can lag by up to 1 second.
- The compare view supports 2 waveforms; 3 or more are not supported.
- A Surfer upgrade may change its internal message encoding (BigInt serialization) and command set, so the assets package and the core must be upgraded as a pair. We only depend on the stable command layer, keeping the adaptation cost manageable but not zero.
- Browser-side rendering limits come from Surfer WASM itself; when expanding thousands of signals on one screen, use groups and fold them.

---

Questions and bug reports: [GitHub Issues](https://github.com/Tencent/wave-mcp/issues).
