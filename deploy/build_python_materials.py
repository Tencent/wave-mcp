#!/usr/bin/env python3
"""Build the redistribution materials for a standalone CPython runtime.

The offline bundle can embed a relocatable CPython from python-build-standalone
(``build_offline_bundle.sh --python``). Shipping that runtime means shipping its
notices and corresponding sources too, so the bundle script requires a matching
``--python-materials`` directory. This script produces one, verified against
upstream by SHA256 at every step:

  1. downloads the install_only tarball (what the bundle embeds) and the full
     archive of the same release/target (PYTHON.json and the upstream licence
     texts live only in the latter);
  2. proves every install_only file is byte-identical to the full build, so
     PYTHON.json really describes the binaries that ship;
  3. collects the source archive of every dependency whose licence PYTHON.json
     declares (conservative superset from the release's
     pythonbuild/downloads.py, not a link graph);
  4. declares the extensions the bundle strips. Today: _dbm, which statically
     links Berkeley DB (Sleepycat, copyleft). Nothing else in the runtime links
     it and wave-mcp never imports dbm/shelve, so the bundle removes it and its
     licence/source are not shipped;
  5. runs deploy/redistribution_materials.py check on the result.

Usage (pinned default runtime):
  python3 deploy/build_python_materials.py --out /path/to/python-materials

Then:
  deploy/build_offline_bundle.sh --out /path/to/bundle \\
      --python <printed install_only tarball> --python-materials /path/to/python-materials

Needs network access to github.com and mozilla.org, about 450 MB of downloads
(cached under --work and reused), and either the ``zstandard`` Python module
or the ``zstd`` command to read the full archive.

A passing check is not a legal opinion; see docs/PACKAGING_MATERIALS.md.
"""
import argparse
import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

#: the runtime wave-mcp's own offline bundles embed; override with the flags
PINNED_RELEASE = '20260901'
PINNED_PYVER = '3.11.16'
TRIPLE = 'x86_64-unknown-linux-gnu'
#: hashes of the pinned release, from its SHA256SUMS (checked 2026-09-17)
PINNED_SHA = {
    f'cpython-{PINNED_PYVER}+{PINNED_RELEASE}-{TRIPLE}-install_only.tar.gz':
        'faa0758583a63f14c5eee516af82738403b59c13edda6fc0a21d953febd89eed',
    f'cpython-{PINNED_PYVER}+{PINNED_RELEASE}-{TRIPLE}-pgo+lto-full.tar.zst':
        'b48f6e7b70f366018b586a6d6234c551d05b172a3e6c79ddaf33fb90e7038acb',
}
#: GitHub tag archive of the build scripts (not listed in SHA256SUMS)
PINNED_BUILD_SRC_SHA = 'bf4776e7bdb751a58fa6879d280b678f60b955b62b19ec98a32903fd82cee1c6'
MPL_URL = 'https://www.mozilla.org/media/MPL/2.0/index.815ca599c9df.txt'
REPO_ROOT = Path(__file__).resolve().parent.parent

STRIPPED_TEMPLATE = {
    '_dbm': {
        'reason': ('statically links Berkeley DB 6.0.19 (Sleepycat licence, copyleft); '
                   'wave-mcp never imports dbm/shelve and no other runtime file links libdb; '
                   'removed at bundle time so neither the module nor its licence/source ship'),
        'files': [],
        'sources_excluded': ['bdb'],
    },
}


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def fetch(url, dest, expected=None, attempts=3):
    """Download ``url`` to ``dest``, verified against ``expected`` when given.

    Mirror networks (sourceforge in particular) occasionally hand back a bad
    body from one mirror; a hash mismatch or network error is retried a few
    times before giving up. A cached file with the right hash is reused.
    """
    if dest.exists() and (expected is None or sha256(dest) == expected):
        return dest
    part = dest.with_name(dest.name + '.part')
    last = ''
    for attempt in range(1, attempts + 1):
        print('  fetching', url, '' if attempt == 1 else f'(attempt {attempt})', flush=True)
        try:
            with urllib.request.urlopen(url, timeout=300) as r, open(part, 'wb') as f:
                shutil.copyfileobj(r, f)
                served = r.geturl()
        except OSError as exc:
            last = f'download failed: {exc}'
            continue
        if expected is None or sha256(part) == expected:
            part.replace(dest)
            return dest
        last = f'SHA256 mismatch (served by {served})'
    part.unlink(missing_ok=True)
    sys.exit(f'ERROR: {url}: {last} after {attempts} attempts')


def upstream_sums(base, work, release):
    """Parse the release's SHA256SUMS (used when not on the pinned release)."""
    path = fetch(base + 'SHA256SUMS', work / f'SHA256SUMS-{release}')
    sums = {}
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) == 2:
            sums[parts[1].lstrip('*')] = parts[0]
    return sums


def open_zst_tar(path):
    """Stream-open a .tar.zst with zstandard, or the zstd command as fallback."""
    try:
        import zstandard
        stream = zstandard.ZstdDecompressor().stream_reader(path.open('rb'))
        return tarfile.open(fileobj=stream, mode='r|'), None
    except ImportError:
        pass
    if not shutil.which('zstd'):
        sys.exit('ERROR: reading the full archive needs the zstandard module '
                 '(pip install zstandard) or the zstd command')
    proc = subprocess.Popen(['zstd', '-dc', str(path)], stdout=subprocess.PIPE)
    return tarfile.open(fileobj=proc.stdout, mode='r|'), proc


def walk_license_paths(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == 'license_path':
                out.add(v)
            elif k == 'license_paths':
                out.update(v)
            else:
                walk_license_paths(v, out)
    elif isinstance(obj, list):
        for v in obj:
            walk_license_paths(v, out)


def build(out, work, release, pyver, build_src_sha, mpl_text):
    base = f'https://github.com/astral-sh/python-build-standalone/releases/download/{release}/'
    install_name = f'cpython-{pyver}+{release}-{TRIPLE}-install_only.tar.gz'
    full_name = f'cpython-{pyver}+{release}-{TRIPLE}-pgo+lto-full.tar.zst'
    if release == PINNED_RELEASE and pyver == PINNED_PYVER:
        sums = PINNED_SHA
        build_src_sha = build_src_sha or PINNED_BUILD_SRC_SHA
    else:
        sums = upstream_sums(base, work, release)
        if not build_src_sha:
            sys.exit('ERROR: --build-src-sha256 is required for a non-pinned release: the '
                     'build-script tag archive is not listed in SHA256SUMS; take its hash '
                     'from a source you trust')
    for name in (install_name, full_name):
        if name not in sums:
            sys.exit(f'ERROR: {name} is not in release {release}')
    stripped = json.loads(json.dumps(STRIPPED_TEMPLATE))

    print('[1] upstream archives', flush=True)
    install = fetch(base + install_name, work / install_name, sums[install_name])
    full = fetch(base + full_name, work / full_name, sums[full_name])
    build_src_url = ('https://github.com/astral-sh/python-build-standalone/archive/'
                     f'refs/tags/{release}.tar.gz')
    build_src = fetch(build_src_url, work / f'python-build-standalone-{release}.tar.gz',
                      build_src_sha)

    print('[2] full-build metadata and licences', flush=True)
    expected, meta = {}, None
    (out / 'licenses').mkdir(parents=True)
    tar, proc = open_zst_tar(full)
    with tar:
        for m in tar:
            if not m.isfile():
                continue
            if m.name.startswith('python/install/'):
                data = tar.extractfile(m).read()
                expected['python/' + m.name[len('python/install/'):]] = hashlib.sha256(data).hexdigest()
            elif m.name.startswith('python/licenses/'):
                (out / m.name[len('python/'):]).write_bytes(tar.extractfile(m).read())
            elif m.name == 'python/PYTHON.json':
                meta = json.loads(tar.extractfile(m).read())
                (out / 'PYTHON.json').write_text(json.dumps(meta, indent=2) + '\n')
    if proc is not None and proc.wait() != 0:
        sys.exit('ERROR: zstd failed to decompress the full archive')
    if not meta or meta.get('python_version') != pyver:
        sys.exit('ERROR: unexpected or missing PYTHON.json in the full archive')

    print('[3] install_only payload identity', flush=True)
    actual = {}
    (out / 'licenses/installed').mkdir()
    with tarfile.open(install) as t:
        for m in t:
            if not m.isfile():
                continue
            data = t.extractfile(m).read()
            actual[m.name] = hashlib.sha256(data).hexdigest()
            if Path(m.name).name.lower().startswith(('license', 'copying', 'notice', 'copyright')):
                dest = out / 'licenses/installed' / m.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
    bad = [n for n, h in actual.items() if expected.get(n) != h]
    if not actual or bad:
        sys.exit(f'ERROR: install_only differs from the full build: {bad[:5]}')
    (out / 'install-payload.json').write_text(json.dumps(actual, indent=2) + '\n')
    (out / 'install-only-omissions.json').write_text(
        json.dumps(sorted(expected.keys() - actual.keys()), indent=2) + '\n')
    print(f'    {len(actual)} payload files byte-identical to the full build')

    print('[4] stripped extensions', flush=True)
    exts = meta['build_info']['extensions']
    dropped_lic = set()
    for name, info in stripped.items():
        e = exts[name][0]
        if e['in_core']:
            sys.exit(f'ERROR: {name} is built into libpython; cannot strip')
        so = e['shared_lib'].removeprefix('install/')
        if 'python/' + so not in actual:
            sys.exit(f'ERROR: {name} shared_lib not in payload: {so}')
        info['files'] = [so]
        dropped_lic |= set(e.get('license_paths', []))
        print(f'    {name}: {so}  licences dropped: {sorted(e.get("license_paths", []))}')
    keep = set()
    walk_license_paths({k: v for k, v in meta.items() if k != 'build_info'}, keep)
    walk_license_paths(meta['build_info']['core'], keep)
    for name, variants in exts.items():
        if name not in stripped:
            walk_license_paths(variants, keep)
    dropped_lic -= keep
    for rel in dropped_lic:
        (out / rel).unlink(missing_ok=True)
        print('    removed licence text of stripped component:', rel)
    declared = set()
    walk_license_paths(meta, declared)
    missing_lic = sorted(rel for rel in declared - dropped_lic if not (out / rel).is_file())

    print('[5] corresponding sources', flush=True)
    with tarfile.open(build_src) as t:
        dl_src = t.extractfile(f'python-build-standalone-{release}/pythonbuild/downloads.py').read().decode()
    downloads = None
    for stmt in ast.parse(dl_src).body:
        if isinstance(stmt, ast.Assign) and any(getattr(x, 'id', '') == 'DOWNLOADS' for x in stmt.targets):
            downloads = ast.literal_eval(stmt.value)
    if not downloads:
        sys.exit('ERROR: DOWNLOADS table not found in pythonbuild/downloads.py')
    kept_lic_names = {p.rsplit('/', 1)[-1] for p in declared - dropped_lic}
    excluded = {s for i in stripped.values() for s in i['sources_excluded']}
    minor = 'cpython-' + '.'.join(pyver.split('.')[:2])
    chosen = {k: v for k, v in downloads.items()
              if v.get('license_file') in kept_lic_names
              and (not k.startswith('cpython-') or k == minor)
              and k not in excluded}
    src = out / 'sources'
    src.mkdir()
    shutil.copy2(build_src, src / build_src.name)
    (src / 'downloads.py').write_text(dl_src)
    rows = []
    for name, info in sorted(chosen.items()):
        fname = name + '-' + info['url'].rsplit('/', 1)[1]
        p = fetch(info['url'], work / fname, info['sha256'])
        shutil.copy2(p, src / fname)
        rows.append({'name': name, 'version': info.get('version'), 'url': info['url'],
                     'sha256': info['sha256'], 'path': f'sources/{fname}',
                     'license_file': info.get('license_file')})
    (src / 'INVENTORY.json').write_text(json.dumps(rows, indent=2) + '\n')
    print(f'    {len(rows)} dependency source archives verified; excluded: {sorted(excluded)}')

    # The upstream full archive can omit a licence text that PYTHON.json
    # declares (20260901: LICENSE.zlib-ng.txt). Take it from the corresponding
    # source archive that ships here and record where it came from.
    recovered = {}
    for rel in missing_lic:
        lic_name = rel.rsplit('/', 1)[-1]
        cands = [r for r in rows if r['license_file'] == lic_name]
        if not cands:
            sys.exit(f'ERROR: declared licence {rel} missing upstream and no source archive declares it')
        row = cands[0]
        with tarfile.open(src / row['path'].split('/', 1)[1]) as t:
            members = [m for m in t.getmembers() if m.isfile()
                       and m.name.count('/') == 1
                       and m.name.rsplit('/', 1)[1].lower().startswith(('license', 'copying'))]
            if not members:
                sys.exit(f'ERROR: no top-level licence file inside {row["path"]}')
            (out / rel).write_bytes(t.extractfile(members[0]).read())
        recovered[rel] = {'from_source': row['path'], 'member': members[0].name,
                          'note': 'declared in PYTHON.json but absent from the upstream full archive'}
        print(f'    recovered {rel} from {row["path"]}:{members[0].name}')
    for rel in declared - dropped_lic:
        if not (out / rel).is_file():
            sys.exit(f'ERROR: declared licence missing: {rel}')

    print('[6] notices and manifest', flush=True)
    mpl = mpl_text.read_bytes() if mpl_text else fetch(MPL_URL, work / 'MPL-2.0.txt').read_bytes()
    if b'Mozilla Public License Version 2.0' not in mpl:
        sys.exit('ERROR: MPL-2.0 text does not look like the licence')
    (out / 'licenses/python-build-standalone-MPL-2.0.txt').write_bytes(mpl)
    stripped_note = '\n'.join(
        f'  - {n}: {i["files"][0]} removed at bundle time. {i["reason"]}.' for n, i in stripped.items())
    (out / 'NOTICE.txt').write_text(
        f'CPython {pyver}, python-build-standalone {release}, {TRIPLE}.\n'
        'Original upstream notices are retained under licenses/.\n'
        'PYTHON.json is the matching upstream full-build metadata.\n'
        'Install-only payload files were compared byte-for-byte via SHA256 with the full build.\n'
        'No claim that the PSF license alone covers bundled dependencies is made.\n'
        '\nExtensions deliberately stripped from the shipped runtime (see manifest.json):\n'
        f'{stripped_note}\n')
    files = {p.relative_to(out).as_posix(): sha256(p) for p in out.rglob('*') if p.is_file()}
    manifest = {
        'schema': 1, 'component': 'python', 'version': f'{pyver}+{release}',
        'artifact_sha256': sums[install_name],
        'source_urls': [f'https://github.com/astral-sh/python-build-standalone/releases/tag/{release}'],
        'full_archive_sha256': sums[full_name],
        'payload_files_compared': len(actual),
        'stripped_extensions': stripped,
        'recovered_license_texts': recovered,
        'source_inventory_scope': ('Conservative superset selected from the matching release downloads.py '
                                   'by upstream declared license paths, minus stripped components; not a link graph'),
        'unresolved': [],
        'files': files,
    }
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'    {len(files)} material files written')
    return install


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', required=True, type=Path, help='material directory to create (must not exist)')
    ap.add_argument('--work', type=Path,
                    default=Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache')
                    / 'wave-mcp-build' / 'python-materials',
                    help='download cache, reused across runs (default: %(default)s)')
    ap.add_argument('--release', default=PINNED_RELEASE,
                    help='python-build-standalone release tag (default: %(default)s)')
    ap.add_argument('--python-version', default=PINNED_PYVER,
                    help='CPython version in that release (default: %(default)s)')
    ap.add_argument('--build-src-sha256', default=None,
                    help='SHA256 of the release tag source archive; required off the pinned release')
    ap.add_argument('--mpl-text', type=Path, default=None,
                    help='local MPL-2.0 text (licence of the build scripts); fetched if omitted')
    a = ap.parse_args()

    out = a.out.resolve()
    if out.exists():
        sys.exit(f'ERROR: {out} already exists; refusing to overwrite')
    work = a.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    # build next to the destination and rename on success: a failed run never
    # leaves a half-populated directory that looks like materials
    staging = Path(tempfile.mkdtemp(prefix=out.name + '.partial-', dir=out.parent))
    try:
        tmp_out = staging / 'materials'
        install = build(tmp_out, work, a.release, a.python_version,
                        a.build_src_sha256, a.mpl_text)
        print('[7] checking the result', flush=True)
        subprocess.run([sys.executable, str(REPO_ROOT / 'deploy/redistribution_materials.py'),
                        'check', '--component', 'python', '--materials', str(tmp_out),
                        '--artifact', str(install)], check=True)
        tmp_out.replace(out)
    except subprocess.CalledProcessError:
        sys.exit('ERROR: generated materials failed the check; nothing written to ' + str(out))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    print(f'\n[done] python materials: {out}')
    print(f'       runtime tarball:  {install}')
    print('next:\n  deploy/build_offline_bundle.sh --out <bundle dir> \\\n'
          f'      --python {install} \\\n      --python-materials {out}')


if __name__ == '__main__':
    main()
