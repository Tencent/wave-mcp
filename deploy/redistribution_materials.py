#!/usr/bin/env python3
"""Validate component material completeness and identity, not legal compliance."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import sys
import urllib.request


def require(ok, text):
    if not ok:
        raise ValueError(text)


def safe_path(name):
    p = PurePosixPath(name)
    require(bool(name) and not p.is_absolute() and '..' not in p.parts and '\\' not in name,
            'unsafe material path: ' + name)
    return p


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _walk_license_paths(obj, paths):
    """Collect every license path declared anywhere in a metadata tree."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == 'license_path':
                paths.add(value)
            elif key == 'license_paths':
                paths.update(value)
            else:
                _walk_license_paths(value, paths)
    elif isinstance(obj, list):
        for value in obj:
            _walk_license_paths(value, paths)


def _check_manifest(root, component):
    """Load and verify the shared manifest/fingerprint requirements."""
    manifest = json.loads((root / 'manifest.json').read_text())
    require(manifest.get('schema') == 1 and manifest.get('component') == component,
            'wrong material manifest schema/component')
    require('unresolved' in manifest and manifest['unresolved'] == [],
            'unresolved component material items: ' + str(manifest.get('unresolved')))
    files = manifest.get('files', {})
    require(bool(files), 'empty material manifest')
    for name, expected in files.items():
        p = root / safe_path(name)
        require(not p.is_symlink() and p.is_file() and p.resolve().is_relative_to(root),
                'missing/unsafe material file: ' + name)
        require(p.stat().st_size > 0 and sha(p) == expected, 'empty/changed material: ' + name)
    actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
    require(actual == set(files) | {'manifest.json'}, 'unlisted/missing material files')
    require(any(name.startswith('licenses/') for name in files), 'missing original license files')
    require(bool(manifest.get('source_urls')), 'missing source origin URLs')
    return manifest, files


def _check_vcd2fst(files):
    require('sources/gtkwave.tar.gz' in files and 'sources/build_vcd2fst.sh' in files,
            'vcd2fst matching source archive/build recipe missing')
    require('relink/relink.sh' in files and 'relink/vcd2fst.o' in files,
            'vcd2fst relink inputs missing')
    require('licenses/LGPL-2.1.txt' in files, 'jrb license text missing')


def _stripped_python_files(root, manifest):
    """Return (stripped_files, dropped_license_paths) declared in the manifest.

    A material set may declare optional CPython extension modules that the
    bundle deliberately removes (for example _dbm, whose statically linked
    Berkeley DB is the only copyleft component of the runtime and which
    wave-mcp never imports). Every stripped extension must be a real shared
    extension of the pinned build, its shared object must be a known payload
    file, and its license paths are then no longer required to ship unless
    some other component still declares them.
    """
    stripped = manifest.get('stripped_extensions') or {}
    if not stripped:
        return set(), set()
    metadata = json.loads((root / 'PYTHON.json').read_text())
    extensions = metadata.get('build_info', {}).get('extensions', {})
    payload = json.loads((root / 'install-payload.json').read_text())
    payload_files = {n.removeprefix('python/') for n in payload}
    files, dropped = set(), set()
    for name, info in stripped.items():
        require(name in extensions and not extensions[name][0].get('in_core'),
                'stripped extension is not a shared extension of this build: ' + name)
        ext = extensions[name][0]
        shared = ext.get('shared_lib', '').removeprefix('install/')
        declared = set(info.get('files') or [])
        require(bool(shared) and shared in declared, 'stripped extension shared_lib not listed: ' + name)
        require(declared.issubset(payload_files), 'stripped files unknown to payload: ' + name)
        require(bool(info.get('reason')), 'stripped extension needs a reason: ' + name)
        files |= declared
        dropped |= set(ext.get('license_paths') or [])
    # a license path still declared by core or by a kept extension stays required
    keep = set()
    _walk_license_paths({k: v for k, v in metadata.items() if k != 'build_info'}, keep)
    _walk_license_paths(metadata['build_info'].get('core', {}), keep)
    for name, variants in extensions.items():
        if name not in stripped:
            _walk_license_paths(variants, keep)
    return files, dropped - keep


def _check_python(root, files):
    require('PYTHON.json' in files and 'install-payload.json' in files,
            'Python upstream build metadata/payload inventory missing')
    manifest = json.loads((root / 'manifest.json').read_text())
    _, dropped = _stripped_python_files(root, manifest)
    metadata = json.loads((root / 'PYTHON.json').read_text())
    paths = set()
    _walk_license_paths(metadata, paths)
    paths -= dropped
    require(paths.issubset(files), 'Python metadata declares missing license files')
    require(dropped.isdisjoint(files),
            'license files of stripped extensions must not ship: ' + str(sorted(dropped & set(files))))
    require('sources/INVENTORY.json' in files, 'Python corresponding source inventory missing')
    inventory = json.loads((root / 'sources/INVENTORY.json').read_text())
    require(bool(inventory) and any(p.get('name', '').startswith('cpython-') for p in inventory),
            'Python source archive missing')
    if 'licenses/LICENSE.bdb.txt' in paths:
        require(any(p.get('name') == 'bdb' for p in inventory),
                'Berkeley DB corresponding source missing')
    else:
        require(not any(p.get('name') == 'bdb' for p in inventory),
                'Berkeley DB source listed although its extension is stripped')
    for item in inventory:
        require(item.get('path') in files and files[item['path']] == item.get('sha256'),
                'Python dependency source missing or mismatched: ' + item.get('name', '?'))


def _check_viewer(root, files):
    require('sources/Cargo.lock' in files and 'component-inventory.json' in files,
            'viewer matching lockfile/component inventory missing')
    inventory = json.loads((root / 'component-inventory.json').read_text())
    require(bool(inventory), 'empty viewer component inventory')
    for item in inventory:
        require(item.get('notices') and all(n in files for n in item['notices']),
                'viewer component notices incomplete: ' + item.get('name', '?'))
    require(any(n.startswith('sources/') and n.endswith(('.tar.gz', '.crate')) for n in files),
            'viewer source archives missing')


def _check_remote_artifact(manifest, component, artifact):
    require(component == 'python', 'remote artifact unsupported for this component')
    digest = hashlib.sha256()
    with urllib.request.urlopen(artifact, timeout=120) as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(data)
    require(digest.hexdigest() == manifest.get('artifact_sha256'), 'downloaded artifact differs')


def _check_dir_artifact(root, manifest, component, path):
    entries = manifest.get('artifact_files')
    stripped = set()
    if component == 'python':
        stripped, _ = _stripped_python_files(root, manifest)
        entries = {n.removeprefix('python/'): h for n, h in
                   json.loads((root / 'install-payload.json').read_text()).items()
                   if n.removeprefix('python/') not in stripped}
    require(bool(entries), 'missing directory artifact fingerprint')
    for name, expected in entries.items():
        member = path / safe_path(name)
        require(member.is_file() and member.resolve().is_relative_to(path.resolve())
                and sha(member) == expected, 'artifact directory mismatch: ' + name)
    observed = {p.relative_to(path).as_posix() for p in path.rglob('*')
                if p.is_file() and not p.is_symlink()}
    if component == 'python':
        require(observed == set(entries), 'extra/missing Python payload files')
        require(observed.isdisjoint(stripped),
                'stripped Python extension still present: ' + str(sorted(observed & stripped)))
    if component == 'viewer':
        relevant = {n for n in observed if n == 'surver' or n.startswith('wasm/')}
        require(relevant == set(entries), 'extra/missing viewer asset files')


def _check_artifact(root, manifest, component, artifact):
    if artifact.startswith(('https://', 'http://')):
        _check_remote_artifact(manifest, component, artifact)
        return
    path = Path(artifact)
    if path.is_dir():
        _check_dir_artifact(root, manifest, component, path)
        return
    require(path.is_file() and sha(path) == manifest.get('artifact_sha256'),
            'binary/archive fingerprint does not match material manifest')


def validate(materials, component, artifact=None):
    root = materials.resolve()
    manifest, files = _check_manifest(root, component)
    if component == 'vcd2fst':
        _check_vcd2fst(files)
    elif component == 'python':
        _check_python(root, files)
    elif component == 'viewer':
        _check_viewer(root, files)
    if artifact is not None:
        _check_artifact(root, manifest, component, artifact)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check', 'copy'])
    parser.add_argument('--component', required=True, choices=['python', 'viewer', 'vcd2fst'])
    parser.add_argument('--materials', required=True, type=Path)
    parser.add_argument('--artifact')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    manifest = validate(args.materials, args.component, args.artifact)
    if args.action == 'copy':
        require(args.output is not None, 'copy requires --output')
        require(not args.output.exists(), 'material destination already exists')
        shutil.copytree(args.materials, args.output)
        validate(args.output, args.component)
    print(f"PASS {args.component}: {len(manifest['files'])} material files verified; not a legal opinion")


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError) as error:
        print('ERROR: redistribution materials: ' + str(error), file=sys.stderr)
        sys.exit(1)
