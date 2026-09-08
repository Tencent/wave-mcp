# License texts bundled at packaging time

The offline bundle and the viewer assets package must carry the full
license text of every third-party component they redistribute. The
texts below are the official upstream copies, vendored here so that
builds work in containers without network access.

| File | License | Used by | Upstream source |
| --- | --- | --- | --- |
| `LGPL-2.1.txt` | LGPL-2.1 | `jrb` (libfdr) linked into the bundled `vcd2fst` binary | https://www.gnu.org/licenses/old-licenses/lgpl-2.1.txt |
| `EUPL-1.2.txt` | EUPL-1.2 | Surfer WASM + `surver` in the viewer assets package | https://gitlab.com/surfer-project/surfer/-/raw/v0.7.0/LICENSE-EUPL-1.2.txt |
| `TraceWeave-MIT.txt` | MIT (Copyright (c) 2025 gokeshenzhen) | parts of `third_party/fsdb2fst` written with TraceWeave as a reference; see [THIRD_PARTY.md](../THIRD_PARTY.md) | https://github.com/gokeshenzhen/TraceWeave |

Copy rules (enforced by the packaging configuration and scripts):

- The source distribution retains `docs/THIRD_PARTY.md` and this
  directory. The core wheel installs them under `share/doc/wave-mcp/`
  and `share/doc/wave-mcp/licenses/`, alongside the project's `LICENSE`.
- `deploy/build_offline_bundle.sh` always copies the complete directory
  as `licenses/`, beside `THIRD_PARTY.md`, and also retains these files
  in the bundled source tree. Missing required materials stop the build.
- Wheelhouse notices are retained under `licenses/wheels/`, grouped by
  wheel filename with their original paths, so multiple notices and
  identically named files are not dropped or overwritten.
- Carrying a license text does not mean that its component is bundled;
  see [THIRD_PARTY.md](../THIRD_PARTY.md) for the distribution scope.
- `deploy/build_viewer_assets.sh` embeds `EUPL-1.2.txt` and a NOTICE
  file into the `wave-mcp-viewer-assets` package.
- `deploy/build_surver_static.sh` generates a Rust-crate license
  report (`surver-crate-licenses.html` + `.json`) with `cargo-about`
  next to the surver binary.

When bumping the Surfer version or the GTKWave sources, re-check that
these texts still match the licenses of the pinned upstream refs.

## Optional component materials

`vcd2fst.fstapi.LICENSE`, `vcd2fst.fastlz.LICENSE`, `vcd2fst.lz4.LICENSE`,
`vcd2fst.helper.LICENSE` and `vcd2fst.jrb.LICENSE` are verbatim comment blocks
from GTKWave 3.3.121. `vcd2fst.SOURCES.json` records the archive and source
hashes. These text files alone do not replace binary-specific source and
relink materials. See [packaging materials](../PACKAGING_MATERIALS.md).

Optional Python, viewer and converter inputs require a corresponding verified
material directory. The packaging gate checks identity and completeness, not
legal suitability of every license or of the overall distribution.
