# Self-Build Guide

[中文版](SELF_BUILD.md)

The wave-mcp core package uses a permissive license (Apache-2.0). It does not carry or redistribute the components below, which are either license-incompatible or depend on commercial software. They all follow one bootstrap principle:

> **wave-mcp ships build scripts and pinned version info only. Sources and artifacts stay on your machine: we do not download, bundle, or redistribute them.**

The core package keeps its permissive license clean, and artifacts you build in your own environment are governed by their upstream licenses without creating redistribution obligations on our side.

## Shared conventions

All three self-built components follow the same management standard:

| Convention | Meaning |
| --- | --- |
| No auto-download | Build scripts never clone/fetch upstream sources; you obtain them yourself |
| Pinned versions | Each component has a single source of version truth (a pin file or SOURCES.json); scripts verify against it |
| Artifacts stay local | Build outputs are never committed, never enter the PyPI package or the distributed part of offline bundles |
| One cache root | Artifacts/assets live under `~/.wave-mcp/cache/` (override with `WAVE_MCP_CACHE_ROOT`), or an explicit per-component env var |
| Graceful degradation | When a component is missing, the affected tools return a guided error; all analysis tools keep working |

| Component | Upstream license | Build script | Version source of truth | Artifact location (search order) |
| --- | --- | --- | --- | --- |
| viewer assets (Surfer WASM + surver) | EUPL-1.2 | `deploy/build_surver_static.sh` + `deploy/build_viewer_assets.sh` | `deploy/viewer-pin.sh` | `WAVE_MCP_VIEWER_ASSETS` → `~/.wave-mcp/cache/viewer/` |
| fsdb2fst (FSDB converter) | Source MIT; build links your own Synopsys FsdbReader | `deploy/build_fsdb2fst.sh` (auto-triggered on first FSDB use) | `docs/licenses/vcd2fst.SOURCES.json` | `~/.wave-mcp/cache/fsdb2fst/` (`FSDB2FST_FREADER`/`VERDI_HOME` provide the runtime) |
| fstdumper (Xcelium VPI plugin) | GPL-3.0 | `deploy/build_fstdumper.sh` | `docs/licenses/fstdumper.SOURCES.json` | your checkout directory (self-managed; shareable within a team) |

## 1. Viewer assets (Surfer WASM + surver)

The waveform viewer frontend (Surfer WASM) and streaming backend (surver) are build artifacts of the EUPL-1.2 Surfer project. **wave-mcp no longer distributes these assets via PyPI or GitHub Releases**; the `pip install wave-mcp[viewer]` extra has been removed. Build once and the assets serve long-term; a whole team can share one copy.

Prerequisites: a network-connected machine with docker (the build machine and the machine using the assets can differ).

```bash
# 1. Build surver (musl static binary; runs on any x86-64 Linux)
#    The version is pinned in deploy/viewer-pin.sh; do not pass another ref
deploy/build_surver_static.sh            # outputs deploy/surver-static/surver

# 2. Obtain the Surfer WASM build of the SAME commit
#    Build it yourself from the pinned commit (trunk), or take the upstream
#    CI pages_build artifact; the wellen versions of both sides must match
#    (the packing script enforces this). Upstream source, see SURFER_REF in
#    deploy/viewer-pin.sh: https://gitlab.com/surfer-project/surfer

# 3. Assemble the asset directory
mkdir -p ~/.wave-mcp/cache/viewer/wasm
cp deploy/surver-static/surver ~/.wave-mcp/cache/viewer/
cp -r <wasm-build-output>/. ~/.wave-mcp/cache/viewer/wasm/
```

A valid asset directory contains an executable `surver` and `wasm/index.html`. Place it under `~/.wave-mcp/cache/viewer/` for auto-discovery, or point `WAVE_MCP_VIEWER_ASSETS` (absolute path) at it. Air-gapped sites: build on a connected machine, copy the directory in, and pack it into your own internal media via the offline bundle's `--viewer <asset-dir>` flag (note: that is internal copying within your organization; distributing further outside requires you to satisfy EUPL-1.2 obligations yourself, including shipping the license text and source location).

Verify: call `open_wave_view` from wave-mcp, or run `wave-view <fst-file>` directly. When assets are missing or incomplete, the tools return an error pointing back to this document.

## 2. fsdb2fst (FSDB converter)

Reading FSDB depends on the commercial Synopsys Verdi FsdbReader libraries, so `fsdb2fst` can only be built on your machine. The converter source (Apache-2.0, retaining the TraceWeave MIT origin and attribution) ships with the package; the build auto-triggers on the first `.fsdb` analysis, so manual action is rarely needed.

Prerequisites: a local Verdi install (`$VERDI_HOME` or `$NOVAS_HOME` set, or `FSDB2FST_FREADER` pointing at a `share/FsdbReader` directory), gcc, zlib headers.

```bash
# Usually unnecessary: the convert pipeline builds it on first .fsdb use
deploy/build_fsdb2fst.sh
```

The `fsdb2fst` binary lands in `~/.wave-mcp/cache/fsdb2fst/` and is a local artifact: never committed, never in any distribution. The Synopsys libraries are always your own; wave-mcp carries no Verdi files. Details in [FSDB_GUIDE.md](FSDB_GUIDE.md).

## 3. fstdumper (Xcelium VPI plugin)

The GPL-3.0 upstream plugin that lets Xcelium (xrun) dump FST directly during simulation. wave-mcp distributes neither its source nor binaries; only two fix patches ship with the repo (GPL-3.0, `third_party/fstdumper/`).

Prerequisites: clone upstream `https://github.com/semify-eda/fstdumper` yourself (pinned commit in `docs/licenses/fstdumper.SOURCES.json`), gcc, make, patch, zlib headers; build in the same environment that runs xrun.

```bash
git clone https://github.com/semify-eda/fstdumper /path/to/fstdumper
bash deploy/build_fstdumper.sh /path/to/fstdumper          # optionally --perf-opt
```

The `fstdumper.so` stays in your checkout, is loaded by xrun at simulation time, and never links into the wave-mcp process. Integration flow: [XCELIUM_FST_GUIDE.md](XCELIUM_FST_GUIDE.md).

## FAQ

**Why not just pip-install the viewer?** Surfer/surver are EUPL-1.2 (strong copyleft) components, incompatible with the core package's permissive-license positioning, and the legal boundary of EUPL contagion is uncertain. With distribution stopped, assets you build stay within your environment, create no redistribution obligation on our side, and keep the core license audit clean.

**How long does a build take?** Viewer assets about 15-30 minutes (Rust compilation inside docker); fsdb2fst seconds; fstdumper seconds. All three are build-once, use-long-term.

**What about existing `wave-mcp[viewer]` installs?** An already-installed assets package keeps working (the asset search order is unchanged; the pip package is still discovered), or uninstall it and self-build per this guide.
