# Packaging source and license materials

A successful build is not a legal opinion. Before distributing a release,
review its actual contents and applicable license requirements. These checks
prevent known missing materials and mismatched artifacts; they do not replace
that review.

## Core wheel and sdist

Retain the project LICENSE, THIRD_PARTY.md and the complete docs/licenses/
directory. The wheel installs these under share/doc/wave-mcp/. Keep original
copyrights and permission notices intact. The core does not bundle proprietary
FsdbReader libraries or the locally built FSDB converter.

## Optional components

| Input | Required accompanying material |
| --- | --- |
| `--python` | `--python-materials DIR`: matching upstream PYTHON.json, every declared dependency license, full/install-only payload comparison and origin hashes |
| `--vcd2fst` | `--vcd2fst-materials DIR`: exact GTKWave archive, generated configuration, original notices, build recipe, application objects/relink script, and copied runtime library notices |
| `--viewer` | `--viewer-materials DIR`: exact source/lockfiles, all applicable dependency and embedded-resource notices, reproducible build/source correspondence, and asset fingerprints |

The directories include manifest.json (schema 1). Its files map lists every
material file and SHA256. source_urls identifies original sources. unresolved
must be an empty list only after its listed material gaps have actually been
resolved. artifact_sha256 binds an archive or executable; artifact_files binds
a viewer directory. Python directories are checked against install-payload.json.
Do not produce an empty unresolved list merely to pass a check.

For a new vcd2fst build:

```bash
bash deploy/build_vcd2fst.sh --src /path/to/gtkwave-3.3.121.tar.gz --out /path/to/new-build
bash deploy/build_offline_bundle.sh --out /path/to/new-bundle \
  --vcd2fst /path/to/new-build/vcd2fst \
  --vcd2fst-materials /path/to/new-build/redistribution
```

The generated redistribution directory includes source and relink inputs for
the actual binary. A three-file generic license collection is not a substitute.
The converter uses contrib/rtlbrowse/jrb.c, matching the header included by
vcd2fst.c; do not select a different jrb.c using an unordered file search.

For standalone Python, obtain both install_only and full archives from the
same python-build-standalone release and target. Verify official checksums,
compare the actual install-only files with the full archive, and retain
PYTHON.json plus its declared licenses. The PSF text alone does not cover all
linked or embedded dependencies. Preserve the runtime's own notices too.

For viewer assets, export material for both native surver and the WASM build.
Cargo.lock can supply a conservative source inventory but does not establish
which target/features were built. Keep registry checksums, exact git commits,
source availability, native-library notices, font/JavaScript notices and the
actual build settings. Do not reuse an old unknown-license report as approval.

```bash
VIEWER_MATERIALS=/path/to/viewer-materials \
  bash deploy/build_viewer_assets.sh /path/to/assets
```

The asset wheel carries the materials under wave_mcp_viewer_assets/redistribution/.
Packaging removes upstream service workers and sample waveforms; provenance
must describe such packaging changes rather than claim that every file is
unmodified.

## Verification and release review

Optional material directories are copied into the offline bundle under
materials/python, materials/viewer and materials/vcd2fst. Run the same checker
against the final directory and verify that the final compressed archive
contains those files byte-for-byte. Test installation without an index and
exercise each included optional component on the intended target platform.

```bash
python3 deploy/redistribution_materials.py check --component vcd2fst \
  --materials /path/to/bundle/materials/vcd2fst \
  --artifact /path/to/bundle/bin/vcd2fst
```

Wheel dependencies retain their own notices under licenses/wheels/, preserving
wheel identity and original paths. Review packages for which metadata or notice
files are insufficient; a license expression is not the permission text.

For source-availability or relinking requirements, retain actual corresponding
source, local modifications, build/configuration inputs and the necessary
relink materials. Have the release's distribution approach reviewed by the
responsible open-source/legal reviewers. Do not infer full compliance from the
presence of a source URL, a generic license text, or this checker passing.
