# License texts bundled at packaging time

The offline bundle and the viewer assets package must carry the full
license text of every third-party component they redistribute. The
texts below are the official upstream copies, vendored here so that
builds work in containers without network access.

| File | License | Used by | Upstream source |
| --- | --- | --- | --- |
| `LGPL-2.1.txt` | LGPL-2.1 | `jrb` (libfdr) linked into the bundled `vcd2fst` binary | https://www.gnu.org/licenses/old-licenses/lgpl-2.1.txt |
| `EUPL-1.2.txt` | EUPL-1.2 | Surfer WASM + `surver` in the viewer assets package | https://gitlab.com/surfer-project/surfer/-/raw/v0.7.0/LICENSE-EUPL-1.2.txt |

Copy rules (enforced by the packaging scripts):

- `deploy/build_offline_bundle.sh` copies this directory into the
  bundle as `licenses/` whenever `--vcd2fst` is given (LGPL-2.1) and
  `--viewer` is given (EUPL-1.2), together with per-component MIT/BSD
  texts extracted from the wheelhouse.
- `deploy/build_viewer_assets.sh` embeds `EUPL-1.2.txt` and a NOTICE
  file into the `wave-mcp-viewer-assets` package.
- `deploy/build_surver_static.sh` generates a Rust-crate license
  report (`surver-crate-licenses.html` + `.json`) with `cargo-about`
  next to the surver binary.

When bumping the Surfer version or the GTKWave sources, re-check that
these texts still match the licenses of the pinned upstream refs.
