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

## Standalone Python runtime (offline bundle only)

If the offline bundle embeds a standalone CPython (python-build-standalone),
CPython is distributed under the Python Software Foundation License (PSF).
