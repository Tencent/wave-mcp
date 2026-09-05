# Third-party notices

wave-mcp is released under the MIT License (see the top-level `LICENSE`).
It uses the following third-party components, each under its own license.
Their notices are retained here as required.

## Python dependencies (installed via pip; imported at runtime)

| Component | License | Project |
| --- | --- | --- |
| mcp (Model Context Protocol SDK) | MIT | https://github.com/modelcontextprotocol/python-sdk |
| pyslang | MIT | https://github.com/MikePopoloski/slang |
| pylibfst | MIT + BSD-2-Clause | https://github.com/mschlaegl/pylibfst |

`pylibfst` bundles the FST library and its compressors:
- libfst / fstapi: MIT (Tony Bybell), https://github.com/gtkwave/libfst
- LZ4: BSD-2-Clause (Yann Collet)
- FastLZ: MIT (Ariya Hidayat)

## Bundled binary (offline/self-contained release only)

The offline bundle ships a `vcd2fst` converter built from source. It is an
aggregation: `vcd2fst` is a separate program invoked as a subprocess and does
NOT link into or affect the MIT license of wave-mcp itself.

`vcd2fst` is built from the GTKWave sources and is composed of:

| Component | License | Source |
| --- | --- | --- |
| vcd2fst helper (`src/helpers/vcd2fst.c`) | MIT | https://github.com/gtkwave/gtkwave |
| libfst / fstapi | MIT | https://github.com/gtkwave/libfst |
| LZ4 | BSD-2-Clause | https://github.com/lz4/lz4 |
| FastLZ | MIT | https://github.com/ariya/FastLZ |
| jrb (libfdr red-black tree) | LGPL-2.1 | https://github.com/josborn8/libfdr |

Note on the LGPL-2.1 component (`jrb`): the offline bundle satisfies LGPL-2.1 by
providing the corresponding build recipe (`deploy/build_vcd2fst.sh`, which
records the exact GTKWave version/commit) so the binary can be rebuilt/relinked.
The GTKWave GUI application is GPL-licensed, but wave-mcp does NOT use or ship
it; only the MIT FST library plus the above converter sources are used.

## FSDB converter (local build artifact only, never distributed)

`third_party/fsdb2fst/` contains the source of `fsdb2fst`, an FSDB-to-FST
converter built by `deploy/build_fsdb2fst.sh`. The converter source and the
vendored FST writer are:

| Component | License | Source |
| --- | --- | --- |
| fsdb2fst.cpp (this repo) | MIT (wave-mcp) | original code; FSDB timescale parsing and the ffrAPI stub signatures were informed by the public TraceWeave implementation, see below |
| fstapi / libfst | MIT | https://github.com/gtkwave/libfst (via gtkwave 3.3.121) |
| LZ4 | BSD-2-Clause (Yann Collet) | via gtkwave 3.3.121 |
| FastLZ | MIT (Ariya Hidayat) | via gtkwave 3.3.121 |

The converter additionally links at BUILD time against the Synopsys
FsdbReader runtime (`libnffr.so` + `libnsys.so` from a local Verdi
installation, `$VERDI_HOME/share/FsdbReader/linux64`). Those libraries are
proprietary Synopsys property: they are NEVER committed, vendored, or
redistributed; the produced binary is a local artifact and is excluded from
git, PyPI, and the offline bundle (`third_party/verdi_runtime/` is
gitignored). Runtime use of the FsdbReader libraries performs no Synopsys
license checkout (verify in your own environment, e.g. with `lmstat`).

### Attribution: TraceWeave

FSDB support is the newest part of wave-mcp. It was added on 2026-08-31 and is
the one area where we learned from prior public work rather than starting from
a blank page. Credit is due here, and earlier commits under-stated it.

| Item | Detail |
| --- | --- |
| Project | TraceWeave |
| Author | gokeshenzhen (一辉) |
| License | MIT, Copyright (c) 2025 gokeshenzhen |
| Link | https://github.com/gokeshenzhen/TraceWeave |

What we consulted, and how far it goes:

- `ParseScaleFs()` in `fsdb2fst.cpp` turns an FSDB scale string such as `1ns`
  or `100fs` into femtoseconds per tick. It was written with the public
  TraceWeave wrapper as a reference: we adopted its error contract (an
  unparseable scale yields 0 and the caller aborts instead of assuming a
  unit) and its unit table. The surrounding conversion pipeline, the FST
  writer path, and the tick pass-through time model are ours.
- The stub in `ffrAPI_stub_impl.cpp` mirrors the subset of ffrAPI that
  TraceWeave exercises, and its build layout follows the TraceWeave
  FsdbReader setup, so offline stub builds behave like the Verdi-backed build.

Everything else in wave-mcp, in particular the pyslang static netlist, the
trace engine, and the MCP tool surface, was developed independently and
predates our FSDB work by more than six weeks. TraceWeave's own pyslang
backend, its Source Graph, arrived later still, and its author describes it as
a fallback used when the Verdi NPI backend is unavailable.

TraceWeave is MIT-licensed, so reading and reusing it is permitted. MIT also
requires that attribution travel with the code, and we got this wrong for a
few days: comments naming the project came in with the converter on
2026-08-31, then were dropped on 2026-09-01 during a broader cleanup of
vendor references, which left the file described as `original code`. That
description was inaccurate. It is corrected above and in the two source file
headers, and our thanks go to the TraceWeave author for the work we could
read and build on.

`third_party/fstdumper/` carries patch files for the upstream
[fstdumper](https://github.com/semify-eda/fstdumper) project (GPL-3.0), a VPI
plugin that lets Xcelium (xrun) dump FST directly during simulation. See
[docs/XCELIUM_FST_GUIDE.md](XCELIUM_FST_GUIDE.md) for the integration flow.

| Component | License | Source |
| --- | --- | --- |
| fstdumper (upstream plugin) | GPL-3.0 | https://github.com/semify-eda/fstdumper |
| `fstdumper-xcelium-fixes.patch` | GPL-3.0 (inherits upstream license) | this repo |
| `fstdumper-perf-opt.patch` (optional) | GPL-3.0 (inherits upstream license) | this repo |

Licensing notes:

- fstdumper is NOT bundled, built, or redistributed by wave-mcp. Users clone
  the upstream repository, apply the patches with `patch -p1`, and build the
  `.so` themselves. The plugin is loaded by the simulator at simulation time
  and never links into wave-mcp, so this is a mere aggregation and does not
  affect the MIT license of wave-mcp.
- The patch files are derived work of GPL-3.0 code and are therefore
  distributed under GPL-3.0 as well. They are NOT covered by wave-mcp's MIT
  license.
- Compiled `.so` artifacts must never be committed or shipped with wave-mcp,
  its PyPI package, or its release assets.
- The fixes were also contributed upstream (semify-eda/fstdumper#6, fork
  xxin0816/fstdumper). Upstream has been inactive since 2023-09, so the
  local patches are maintained here on a long-term basis and are NOT gated
  on upstream acceptance; if upstream ever merges them, the local patches
  can be dropped at that point.

## Standalone Python runtime (offline bundle only)

If the offline bundle embeds a standalone CPython (python-build-standalone),
CPython is distributed under the Python Software Foundation License (PSF).

## Wave viewer assets (optional `wave-mcp-viewer-assets` package only)

The optional viewer (`wave-view`, `open_wave_view`) consumes a SEPARATE
assets package, `wave-mcp-viewer-assets`, containing:

| Component | License | Project |
| --- | --- | --- |
| Surfer (WASM waveform viewer) | EUPL-1.2 | https://gitlab.com/surfer-project/surfer |
| surver (Surfer remote server) | EUPL-1.2 | https://gitlab.com/surfer-project/surfer |

These EUPL-1.2 components are NOT bundled into the MIT-licensed `wave-mcp`
core package or repository. They are an aggregation: `surver` runs as a
separate subprocess and the WASM bundle is served as static files to the
user's browser; neither links into wave-mcp. The assets package is built by
`deploy/build_viewer_assets.sh`, which records the Surfer version; a
statically-linked `surver` for old-glibc hosts can be reproduced with
`deploy/build_surver_static.sh`. wave-mcp's own shell assets
(`wave_mcp/viewer/web/`) are original MIT-licensed code.
