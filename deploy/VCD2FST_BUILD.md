# vcd2fst third-party source provenance

The offline bundle ships a small native `vcd2fst` converter (VCD → FST). It is
built from **permissively-licensed** sources only; it is NOT the GPL GTKWave
GUI application. wave-mcp invokes it as a separate subprocess (aggregation), so
it does not affect wave-mcp's MIT license.

## Components compiled into `vcd2fst`

| File | Role | License | Upstream |
| --- | --- | --- | --- |
| `vcd2fst.c` | VCD parser + driver | MIT | github.com/gtkwave/gtkwave (`src/helpers`) |
| `fstapi.c` / `fstapi.h` | FST reader/writer (libfst) | MIT | github.com/gtkwave/libfst |
| `lz4.c` / `lz4.h` | LZ4 compressor | BSD-2-Clause | github.com/lz4/lz4 |
| `fastlz.c` / `fastlz.h` | FastLZ compressor | MIT | github.com/ariya/FastLZ |
| `jrb.c` / `jrb.h` | red-black tree (libfdr) | LGPL-2.1 | github.com/josborn8/libfdr |

Pinned upstream: **GTKWave `3.3.121`** (override with `--gtkwave <ver>`).

> The only copyleft component is `jrb` (LGPL-2.1). LGPL-2.1 permits distributing
> the compiled binary as long as re-linking is possible; shipping this build
> recipe together with the pinned source satisfies that. (Long term, `jrb` can
> be swapped for a permissive tree to make the converter fully permissive.)

## Building

Default: fetch the pinned source and build (needs Docker + network on the
build machine):

```bash
deploy/build_vcd2fst.sh --out /tmp/vcd2fst-out
```

Fully offline / reproducible: pre-download the source tarball once on a
connected machine, then build from it with no network:

```bash
# on a connected machine:
curl -LO https://gtkwave.sourceforge.net/gtkwave-3.3.121.tar.gz
# then (network no longer required for the compile step):
deploy/build_vcd2fst.sh --src gtkwave-3.3.121.tar.gz --out /tmp/vcd2fst-out
```

To truly vendor the sources into this directory (commit them for an
air-gapped/reproducible build), extract the files listed above from the pinned
tarball into `third_party/vcd2fst/` and point `--src` at a tarball of this
directory. Keep each file's original license header intact.
