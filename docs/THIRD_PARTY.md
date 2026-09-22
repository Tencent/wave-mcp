# Third-party notices

wave-mcp is released under the Apache-2.0 License (see the top-level `LICENSE`,
which also carries the consolidated third-party attribution notices).
It uses the following third-party components, each under its own license.
Their notices are retained here as required.

## Python dependencies (installed via pip; imported at runtime)

| Component | License | Project |
| --- | --- | --- |
| mcp (Model Context Protocol SDK) | MIT | https://github.com/modelcontextprotocol/python-sdk |
| pyslang | MIT | https://github.com/MikePopoloski/slang |
| pylibfst | BSD-3-Clause (wrapper) + MIT (FST/FastLZ) + BSD-2-Clause (LZ4) | https://github.com/mschlaegl/pylibfst |

`pylibfst` retains its BSD-3-Clause wrapper notice (Manfred SCHLAEGL, 2022).
It loads its native library through CFFI in the Python process, links zlib,
and bundles the FST library and its compressors:
- libfst / fstapi: MIT (Tony Bybell), https://github.com/gtkwave/libfst
- LZ4: BSD-2-Clause (Yann Collet)
- FastLZ: MIT (Ariya Hidayat)

## Bundled binary (offline/self-contained release only)

The offline bundle optionally ships a `vcd2fst` converter built from source.
It runs as a separate subprocess, communicating through command-line
arguments, files or FIFOs; it does not link into the wave-mcp process.
This describes the technical boundary, not a determination of the license
consequences of the combined distribution.

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

## FSDB converter

`third_party/fsdb2fst/` contains the source of `fsdb2fst`, an FSDB-to-FST
converter built by `deploy/build_fsdb2fst.sh`. The converter source and the
vendored FST writer are:

| Component | License | Source |
| --- | --- | --- |
| fsdb2fst.cpp (this repo) | MIT, including TraceWeave source and copyright notices; see below | written by us, with specific parts informed by the public TraceWeave implementation: the FSDB timescale parser, the ffrAPI stub signature subset and FsdbReader build layout, the FSDB bit-array ordering, and the time-tag struct layout. See the attribution section below for the exact scope |
| fstapi / libfst | MIT | https://github.com/gtkwave/libfst (via gtkwave 3.3.121) |
| LZ4 | BSD-2-Clause (Yann Collet) | via gtkwave 3.3.121 |
| FastLZ | MIT (Ariya Hidayat) | via gtkwave 3.3.121 |

The converter is built on demand on the user's machine, so its source and the
build script ship in the wheel, the sdist and the offline bundle; without them
the auto-build has nothing to compile. What ships is source only. The compiled
`fsdb2fst` binary is a local artifact and is not distributed: the build that
produces it links against a Verdi installation that differs per site.

The vendored FST writer files are byte-identical to the GTKWave 3.3.121
originals recorded in [`licenses/vcd2fst.SOURCES.json`](licenses/vcd2fst.SOURCES.json),
and each file retains its upstream copyright header. The corresponding notices
ship as [`licenses/vcd2fst.fstapi.LICENSE`](licenses/vcd2fst.fstapi.LICENSE),
[`licenses/vcd2fst.lz4.LICENSE`](licenses/vcd2fst.lz4.LICENSE) and
[`licenses/vcd2fst.fastlz.LICENSE`](licenses/vcd2fst.fastlz.LICENSE), named for
the first component that vendored them; they cover both converters.

The converter additionally links at BUILD time against the Synopsys
FsdbReader runtime (`libnffr.so` + `libnsys.so` from a local Verdi
installation, `$VERDI_HOME/share/FsdbReader/linux64`). Those libraries are
proprietary Synopsys property: they are NEVER committed, vendored, or
redistributed, and no distribution carries them or any Verdi header
(`third_party/verdi_runtime/` is gitignored). The `ffrAPI_stub.*` files in
that directory are our own minimal declarations used for an offline compile
check, not Synopsys headers. Runtime use of the FsdbReader libraries performs
no Synopsys license checkout (verify in your own environment, e.g. with
`lmstat`).

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

- The upstream fstdumper plugin source and binary are excluded from
  wave-mcp distribution inputs. The inspected v0.2.6 core wheel, sdist and
  both offline bundles contain neither; this is not an assertion about
  every historical image. `deploy/build_fstdumper.sh` performs no clone/fetch:
  users obtain the upstream source themselves, then the script applies the
  patches with `patch -p1` and builds the `.so` in that directory. The plugin
  is loaded by the simulator, not linked into the wave-mcp process. This
  technical boundary does not determine the license consequences of using
  the plugin with a simulator.
- The patch files are derived work of the patched files and are NOT covered
  by wave-mcp's MIT license. `src/sys_fst.c` carries a GPL-2.0-or-later
  header, Copyright (c) 1999-2021 Stephen Williams (steve@icarus.com); its
  later version option is exercised here, so the patches are distributed
  under GPL-3.0, consistent with the upstream top level license. The
  patches were modified 2026-08-31 by the wave-mcp project contributors
  (Tencent). Attribution, modification dates and change summaries are
  recorded in [licenses/fstdumper-patches.NOTICE](licenses/fstdumper-patches.NOTICE).
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

The current offline build strips the optional `_dbm` extension module
from its CPython 3.11.16+20260901 runtime. The inspected local candidate
could not import `_dbm` and contained no libdb files or Berkeley DB markers
in the scanned ELF files. This candidate change has not updated the
published v0.2.6 bundles, whose CPython 3.11.10+20241016 runtime still
contains Berkeley DB 6.0.19. The older bundles remain unchanged.

The build provider describes its 6.0.19 copy as Sleepycat-licensed and its
retained notice says Sleepycat; Oracle also publishes a general statement
that the 6.0 series uses AGPL. These different source statements are not
resolved here into a definitive license classification for the older copy.

For the current candidate, `materials/python/manifest.json` records the
stripped file under `stripped_extensions`; the material check then requires
the file to be absent from the shipped runtime and neither the Berkeley DB
licence text nor its source archive to ship. `dbm.dumb` (pure Python) remains
available.

Runtime libraries and source-only materials are recorded separately. The
inspected candidate uses XZ 5.8.3 liblzma under 0BSD, but its complete XZ
source archive also includes GPL and LGPL files. It uses OpenSSL 3.5.8 at
runtime, while the material superset also contains OpenSSL 1.1.1w source
under the older OpenSSL/SSLeay terms. The python-build-standalone build
project source is MPL-2.0; that does not make CPython itself MPL-2.0.
Each original source notice must be retained rather than replaced by the
runtime library license. Tcl and Tk also require their separate original
notices. Candidate materials still require final package verification.

## Wave viewer assets (user-built, NOT distributed)

The optional viewer (`wave-view`, `open_wave_view`) consumes a SEPARATE
asset directory containing:

| Component | License | Project |
| --- | --- | --- |
| Surfer (WASM waveform viewer) | EUPL-1.2 | https://gitlab.com/surfer-project/surfer |
| surver (Surfer remote server) | EUPL-1.2 | https://gitlab.com/surfer-project/surfer |

Starting with the release after v0.2.6, wave-mcp does NOT distribute these
EUPL-1.2 artifacts: no PyPI assets package, no GitHub release attachment,
no offline-bundle inclusion on our side. Users build the assets locally
from the pinned upstream source following `docs/SELF_BUILD.md`; the pin is
`deploy/viewer-pin.sh`, and `deploy/build_surver_static.sh` /
`deploy/build_viewer_assets.sh` are the build entry points (they never
download upstream sources themselves). Previously published asset packages
remain subject to their own recorded notices.

These EUPL-1.2 binaries are not included in the Apache-2.0-licensed `wave-mcp`
core package or repository. `surver` runs as a separate subprocess using
command-line arguments, pipes and HTTP. The WASM bundle runs in the browser
inside an iframe; the core shell communicates with it only through page-load
URL parameters and standard window.postMessage, and does not import viewer
modules or call viewer functions (an earlier shell revision polled the
viewer's get_state through contentWindow eval; that direct call has been
removed). Separate packaging alone does not determine license impact.
The assets package is built by
`deploy/build_viewer_assets.sh`, which records the Surfer version; a
statically-linked `surver` for old-glibc hosts can be reproduced with
`deploy/build_surver_static.sh`. wave-mcp's own shell assets
(`wave_mcp/viewer/web/`) are original Apache-2.0-licensed code.

### Viewer font materials

The `epaint_default_fonts` crate contains font files. Its
license is `(MIT OR Apache-2.0) AND OFL-1.1 AND Ubuntu-font-1.0`, a
conjunction: the font licenses apply in addition to the crate license.
Earlier asset builds recorded this component as an unknown license, which
left the font texts out of the distributed package. All three are now
vendored under `docs/licenses/`:

| Font | License | Copyright | File |
| --- | --- | --- | --- |
| NotoEmoji | OFL-1.1 | Copyright 2013 Google Inc. | `epaint-default-fonts.OFL-1.1.txt` |
| Hack | MIT + Bitstream Vera; DejaVu public-domain contributions | Source Foundry Authors; Bitstream, Inc. | `epaint-default-fonts.Hack.txt` |
| Ubuntu-Light (Ubuntu font family) | Ubuntu-font-1.0 | Copyright 2011 Canonical Ltd. | `epaint-default-fonts.Ubuntu-font-1.0.txt` |
| emoji-icon-font | MIT | John Slegers | `epaint-default-fonts.emoji-icon-font.MIT.txt` |

The Ubuntu Font Licence 1.0 text and the font's own Canonical copyright
notice are retained together. Ubuntu and Hack markers were found in the
inspected WASM; native `surver` embedding has not been established. Neither
these markers nor a Cargo.lock entry proves byte-identical redistribution
or the absence of subsetting or conversion. The current notices do not
claim that the fonts are unmodified. These notice corrections are local
changes awaiting the next viewer package; published wheels are unchanged.

The emoji-icon-font upstream README also identifies Icomoon, Wikimedia and
OpenSans sources. Its retained MIT notice is not a completed per-icon
source/license mapping; that mapping and target-specific inclusion remain
to be established.

Provenance for these texts is recorded in
`epaint-default-fonts.SOURCES.txt`.

Viewer assets additionally require `redistribution/` with exact source archives,
dependency notices and an inventory tied to the asset hashes. A Cargo.lock
inventory is a conservative source superset, not evidence that every listed
crate was linked. Native surver and WASM build dependencies and bundled fonts
or JavaScript resources must be reviewed separately. Unknown entries or missing
source/build correspondence must be resolved before a release is approved.
