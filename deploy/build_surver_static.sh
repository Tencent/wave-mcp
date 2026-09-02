#!/usr/bin/env bash
# Build a statically-linked (musl) surver for old-glibc machines.
#
# Why: the official surver binary needs glibc >= 2.34; encrypted-network
# hosts often run CentOS 7 (glibc 2.17). A musl static build runs on any
# Linux regardless of glibc. Same pattern as deploy/build_vcd2fst.sh:
# differences live on the build side, target machines stay untouched.
#
# Requires: docker with network access (run OUTSIDE the airgap, copy the
# binary into the offline bundle afterwards).
#
# Usage:
#   deploy/build_surver_static.sh [surfer_git_ref] [out_dir]
#
# The pinned ref lives in deploy/viewer-pin.sh (single source of truth) so the
# surver side and the wasm side cannot drift apart. Do not hardcode a ref here.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=deploy/viewer-pin.sh
source "$HERE/viewer-pin.sh"

REF=${1:-$SURFER_REF}
OUT=${2:-"$HERE/surver-static"}
mkdir -p "$OUT"

# crate-license extractor: runs INSIDE the build container (mounted via /out).
# Deterministic: parse Cargo.lock + each vendored crate's Cargo.toml license
# field. No extra tool to compile (cargo-about builds too slow on alpine).
# The report lists each crate's license expression; full texts are available
# from crates.io per crate, and the assets NOTICE points at this report.
cat > "$OUT/extract_crate_licenses.py" <<'PYEOF'
import re, os, glob, tarfile, io
lock = open('/src/Cargo.lock').read()
crates = {}
for block in lock.split('[[package]]'):
    name = re.search(r'name = "([^"]+)"', block)
    ver = re.search(r'version = "([^"]+)"', block)
    src = re.search(r'source = "([^"]+)"', block)
    if name and ver and src and 'registry' in src.group(1):
        crates[name.group(1)] = ver.group(1)

CARGO_HOMES = ['/usr/local/cargo', '/root/.cargo']

def find_toml(name, ver):
    # 1) extracted sources: prefer the registry-normalized Cargo.toml (direct
    # license field), fall back to Cargo.toml.orig (upstream form, often
    # license.workspace = true)
    for home in ('/usr/local/cargo', '/root/.cargo'):
        for sub in ('Cargo.toml', 'Cargo.toml.orig'):
            for path in glob.glob(f'{home}/registry/src/*/{name}-{ver}/{sub}'):
                try:
                    return open(path, encoding='utf-8', errors='replace').read()
                except Exception:
                    pass
    # 2) .crate archives in the download cache (works even if not extracted)
    for pat in (
        '/usr/local/cargo/registry/cache/*/%s-%s.crate' % (name, ver),
        '/root/.cargo/registry/cache/*/%s-%s.crate' % (name, ver),
    ):
        for path in glob.glob(pat):
            try:
                with tarfile.open(path, 'r:gz') as t:
                    orig = next((m for m in t.getmembers()
                                 if m.name.endswith('/Cargo.toml.orig')), None)
                    main = next((m for m in t.getmembers()
                                 if m.name.endswith('/Cargo.toml')), None)
                    if main is not None:
                        data = t.extractfile(main).read().decode('utf-8', errors='replace')
                        m2 = re.search(r'^license\s*=\s*"([^"]+)"', data, re.M)
                        if m2 and 'workspace' not in m2.group(1):
                            return data
                    if orig is not None:
                        return t.extractfile(orig).read().decode('utf-8', errors='replace')
                    if main is not None:
                        return t.extractfile(main).read().decode('utf-8', errors='replace')
            except Exception:
                pass
    return None

def extract_license_file(name, ver, rel):
    # pull the license file named by license-file= out of the .crate archive
    for pat in (
        '/usr/local/cargo/registry/cache/*/%s-%s.crate' % (name, ver),
        '/root/.cargo/registry/cache/*/%s-%s.crate' % (name, ver),
    ):
        for path in glob.glob(pat):
            try:
                with tarfile.open(path, 'r:gz') as t:
                    for m in t.getmembers():
                        tail = m.name.split('/', 1)[-1]
                        if tail == rel or tail.endswith('/' + rel):
                            return t.extractfile(m).read().decode('utf-8', errors='replace')
            except Exception:
                pass
    return None

print('# Crate licenses for surver')
print()
print('Generated from Cargo.lock of the Surfer build (ref %s).' % os.environ.get('SURFER_REF', '<unknown>'))
print('The surver binary is an unmodified build of the Surfer project (EUPL-1.2).')
print('Each crate below is statically linked into the surver binary and carries its')
print('own permissive license (mostly MIT OR Apache-2.0 dual). Full license texts are')
print('available per crate from https://crates.io/crates/<name>/<version>.')
print()
print('%-40s %-12s %s' % ('crate', 'version', 'license'))
print('-' * 80)
os.makedirs('/out/crates-license-files', exist_ok=True)
unknown = 0
ws = {}
raw_ws = open('/src/Cargo.toml', encoding='utf-8', errors='replace').read()
mws = re.search(r'\[workspace\.package\](.*?)(?:\n\[|\Z)', raw_ws, re.S)
if mws:
    for k, v in re.findall(r'^(\w[\w-]*)\s*=\s*"([^"]+)"', mws.group(1), re.M):
        ws[k] = v
for name in sorted(crates):
    ver = crates[name]
    raw = find_toml(name, ver)
    lic = '?'
    note = ''
    if raw:
        m = re.search(r'^license\s*=\s*"([^"]+)"', raw, re.M)
        if m:
            lic = m.group(1)
        elif re.search(r'^license\.workspace\s*=\s*true', raw, re.M):
            lic = ws.get('license', 'workspace-inherited')
        else:
            mf = re.search(r'^license-file\s*=\s*"([^"]+)"', raw, re.M)
            if mf:
                txt = extract_license_file(name, ver, mf.group(1))
                if txt:
                    with open('/out/crates-license-files/%s-%s.txt' % (name, ver), 'w') as f:
                        f.write(txt)
                    lic = 'custom (see crates-license-files/%s-%s.txt)' % (name, ver)
                else:
                    lic = 'custom license-file: %s' % mf.group(1)
                note = ' (license-file)'
    if lic == '?' and not note:
        unknown += 1
    print('%-40s %-12s %s%s' % (name, ver, lic, note))
print('-' * 80)
print('total crates: %d, unknown license: %d' % (len(crates), unknown))
PYEOF

# rust:alpine ships a musl toolchain natively; build only the surver crate.
docker run --rm -e SURFER_REF="$REF" -v "$OUT":/out rust:alpine sh -exc "
  apk add --no-cache git musl-dev openssl-dev openssl-libs-static pkgconfig python3
  git clone https://gitlab.com/surfer-project/surfer.git /src
  cd /src
  git checkout --detach $REF
  git submodule update --init --recursive --depth 1
  # build fingerprint: exact upstream provenance for the NOTICE
  {
    echo \"surfer_ref=$REF\"
    echo \"surfer_version=\$(grep -m1 '^version = ' surver/Cargo.toml | sed 's/^version = \"//;s/\"$//')\"
    echo \"build_date=\$(date -u +%Y-%m-%dT%H:%M:%SZ)\"
    echo \"cargo_lock_wellen=\$(grep -A1 'name = .wellen.' Cargo.lock | grep version | head -1 | sed 's/^version = \"//;s/\"$//')\"
  } > /out/build-fingerprint.txt
  # static openssl for reqwest/native-tls if the ref pulls it in
  export OPENSSL_STATIC=1
  cargo build --release -p surver
  BIN=\$(find target/release -maxdepth 1 -type f -name surver)
  cp \"\$BIN\" /out/surver
  # verify: no dynamic interpreter
  if ldd /out/surver 2>&1 | grep -qv 'not a dynamic executable\|statically'; then
    echo 'WARNING: binary may not be fully static:'
    ldd /out/surver || true
  fi
  # license report for every crate statically linked into surver
  # (MIT/Apache-2.0 dual etc.); ship it next to the binary so the
  # viewer assets package can redistribute the notices.
  python3 /out/extract_crate_licenses.py > /out/surver-crate-licenses.txt \
    || echo 'WARNING: crate license report failed'
"

echo "built: $OUT/surver"
file "$OUT/surver" || true
if [[ -f "$OUT/surver-crate-licenses.txt" ]]; then
  echo "crate license report: $OUT/surver-crate-licenses.txt"
  echo "  -> build_viewer_assets.sh picks it up automatically when the"
  echo "     surver binary is placed as <asset_dir>/surver next to it."
fi
echo
echo "verify on the oldest target machine:  $OUT/surver --help"
echo "then place it into the viewer asset dir as <asset_dir>/surver and"
echo "rebuild the assets package with deploy/build_viewer_assets.sh"
