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

The offline bundle optionally ships a `vcd2fst` converter built from source. It is an
aggregation: `vcd2fst` is a separate program invoked as a subprocess and does
NOT link into or affect the MIT license of wave-mcp itself.

`vcd2fst` is built from the GTKWave sources and is composed of:

| Component | License | Source |
| --- | --- | --- |
| vcd2fst helper (`src/helpers/vcd2fst.c`) | MIT | https://github.com/gtkwave/gtkwave |
| libfst / fstapi | MIT | https://github.com/gtkwave/libfst |
| LZ4 | BSD-2-Clause | https://github.com/lz4/lz4 |
| FastLZ | MIT | https://github.com/ariya/FastLZ |
| jrb (libfdr red-black tree) | LGPL-2.1-or-later | https://github.com/josborn8/libfdr |

The original helper, fstapi, FastLZ, LZ4 and jrb notices are retained in
`licenses/vcd2fst.*.LICENSE`; `licenses/vcd2fst.SOURCES.json` identifies their
exact GTKWave source archive and file hashes. Each distributed converter also
requires a matching `materials/vcd2fst/` directory containing that source,
original notices, generated build configuration, build recipe and application
objects with a relink script. Any copied zlib runtime is included in its
fingerprint inventory with the package's original notice. A recipe alone is
not treated as proof that all LGPL distribution requirements are satisfied.
The GTKWave GUI is not linked or shipped as a binary. The complete upstream
source archive retained for rebuilding includes other files under their own
original licenses. See [packaging materials](PACKAGING_MATERIALS.md).

## FSDB converter (local build artifact only, never distributed)

`third_party/fsdb2fst/` contains the source of `fsdb2fst`, an FSDB-to-FST
converter built by `deploy/build_fsdb2fst.sh`. The converter source and the
vendored FST writer are:

| Component | License | Source |
| --- | --- | --- |
| fsdb2fst.cpp (this repo) | MIT, including TraceWeave source and copyright notices; see below | written by us, with specific parts informed by the public TraceWeave implementation: the FSDB timescale parser, the ffrAPI stub signature subset and FsdbReader build layout, the FSDB bit-array ordering, and the time-tag struct layout. See the attribution section below for the exact scope |
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

wave-mcp's FSDB support was written with reference to TraceWeave. It was added
on 2026-08-31 and is the area where we drew on prior public work rather than
starting from a blank page. Credit is due here, and earlier commits under-stated
it.

| Item | Detail |
| --- | --- |
| Project | TraceWeave |
| Author | gokeshenzhen (一辉) |
| License | MIT, Copyright (c) 2025 gokeshenzhen |
| Link | https://github.com/gokeshenzhen/TraceWeave |
| License text | [`licenses/TraceWeave-MIT.txt`](licenses/TraceWeave-MIT.txt) |

Two different kinds of influence are worth separating, because they are not the
same thing: code we wrote with reference to TraceWeave's public implementation,
and a feature whose priority its work influenced. Writing code independently and
being influenced in design are not mutually exclusive, so both are listed below.

**1. Implementation-level references (FSDB converter only)**

- `ParseScaleFs()` in `fsdb2fst.cpp` was implemented with reference to
  TraceWeave's `_ParseScaleFs`, including the time-unit conversion table and
  the convention for handling a parse failure (an unparseable scale yields 0
  and the caller aborts rather than assuming a unit).
- The stub in `ffrAPI_stub_impl.cpp` and `ffrAPI_stub.h` mirrors the subset of
  ffrAPI that the public TraceWeave wrapper exercises, and its FsdbReader
  build layout follows the same setup, so offline stub builds behave like the
  Verdi-backed build.
- The FSDB per-bit array ordering (MSB-first, `vc[i] -> s[i]`) was confirmed
  against TraceWeave's verified wrapper. An earlier commit stated this
  explicitly; the reference was dropped on 2026-09-01 and is restored.
- The time-tag struct layout (`fsdbXTag` being layout-compatible with
  `fsdbTag64`, as used in the converter's time-tag conversion) was likewise
  cross-checked against the same wrapper.

The surrounding conversion pipeline, the ffrAPI load path, the FST writer
path, the pass-through tick time model, and the scriptable offline stub engine
are ours.

**2. Design-level influence**

- `diff_waveforms` (`wave_mcp/diff.py`) was written independently and reuses no
  code from TraceWeave. Locating the first divergence between a pass and a fail
  waveform was already on our development roadmap. TraceWeave's
  `diff_first_divergence` came earlier, and we referred to it when prioritising
  the feature, which we credit here. Our implementation feeds the diverging
  signals into wave-mcp's own netlist tools (`signal_fanin` /
  `active_drivers` / `signal_drivers`) and dual-waveform viewer
  (`open_wave_view`).
- This is the only design-level influence we are aware of. The broader feature
  set and tool organisation of wave-mcp were shaped by the capability set of
  established commercial waveform debug tools, not by TraceWeave.

**3. What was developed independently**

The early core architecture of wave-mcp, including the initial pyslang static
netlist, trace engine, and MCP tool layer, was developed independently and is
present in our 2026-07-16 repository commit, more than six weeks before FSDB
support was added on 2026-08-31. This timeline refers to that early architecture,
not to the completion of all tools available today. Independent code development
does not imply the absence of design influence.

TraceWeave is MIT-licensed, so reading and reusing it is permitted. MIT also
requires that attribution travel with the code, and we got this wrong: comments
naming the project came in with the converter on 2026-08-31, then were dropped
on 2026-09-01 during a broader cleanup of vendor references, which left the
file described as `original code`. That description was inaccurate, and the
scope stated in later commits was narrower than what had actually been
consulted. Both are corrected above and in the source file headers, and our
thanks go to the TraceWeave author for the work we could read and build on.

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

If the offline bundle embeds standalone CPython (python-build-standalone),
its matching upstream `PYTHON.json` and all declared dependency licenses must
accompany the runtime in `materials/python/`. CPython's own license does not
cover every bundled library (such as OpenSSL, Tcl/Tk, bzip2 or libffi).
The material manifest binds the runtime to the exact upstream build and
payload hashes; unresolved or mismatched inputs stop packaging.

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

### Fonts embedded in the surver binary

The `surver` binary embeds fonts via the `epaint_default_fonts` crate. Its
license is `(MIT OR Apache-2.0) AND OFL-1.1 AND Ubuntu-font-1.0`, a
conjunction: the font licenses apply in addition to the crate license.
Earlier asset builds recorded this component as an unknown license, which
left the font texts out of the distributed package. All three are now
vendored under `docs/licenses/`:

| Font | License | File |
| --- | --- | --- |
| Hack, NotoEmoji, Ubuntu-Light | OFL-1.1 | `epaint-default-fonts.OFL-1.1.txt` |
| Ubuntu font family | Ubuntu-font-1.0 | `epaint-default-fonts.Ubuntu-font-1.0.txt` |
| emoji-icon-font | MIT | `epaint-default-fonts.emoji-icon-font.MIT.txt` |

Provenance for these texts is recorded in
`epaint-default-fonts.SOURCES.txt`.

Viewer assets additionally require `redistribution/` with exact source archives,
dependency notices and an inventory tied to the asset hashes. A Cargo.lock
inventory is a conservative source superset, not evidence that every listed
crate was linked. Native surver and WASM build dependencies and bundled fonts
or JavaScript resources must be reviewed separately. Unknown entries or missing
source/build correspondence must be resolved before a release is approved.
