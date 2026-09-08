#!/usr/bin/env python3
"""Export verifiable vcd2fst build inputs and notices beside its binary."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--archive', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--repo', type=Path, required=True)
    p.add_argument('--version', required=True)
    args = p.parse_args()
    out = args.output
    material = out / 'redistribution'
    material.mkdir(parents=True, exist_ok=False)
    licenses = material / 'licenses'
    licenses.mkdir()
    source = material / 'sources'
    source.mkdir()
    shutil.copy2(args.archive, source / 'gtkwave.tar.gz')
    shutil.copytree(args.source / 'stub', source / 'stub')
    recipe = args.repo / 'deploy/build_vcd2fst.sh'
    shutil.copy2(recipe, source / 'build_vcd2fst.sh')
    shutil.copy2(Path(__file__), source / 'export_vcd_materials.py')
    shutil.copy2(args.repo / 'docs/licenses/LGPL-2.1.txt', licenses / 'LGPL-2.1.txt')
    names = ['src/helpers/vcd2fst.c', 'src/helpers/fst/fstapi.c',
             'src/helpers/fst/fstapi.h', 'src/helpers/fst/fastlz.c',
             'src/helpers/fst/fastlz.h', 'src/helpers/fst/lz4.c',
             'src/helpers/fst/lz4.h', 'contrib/rtlbrowse/jrb.c']
    for name in names:
        data = (args.source / name).read_bytes()
        if not data.startswith(b'/*') or b'*/' not in data:
            raise ValueError('missing original license header: ' + name)
        (licenses / (Path(name).name + '.notice')).write_bytes(data[:data.index(b'*/') + 2] + b'\n')
    shutil.copytree(out / 'relink', material / 'relink')
    runtime = material / 'runtime'
    runtime.mkdir()
    linked = subprocess.check_output(['ldd', str(out / 'vcd2fst')], text=True)
    rows = []
    for line in linked.splitlines():
        if '=>' not in line:
            continue
        soname, location = line.split('=>', 1)
        soname = soname.strip()
        if soname.startswith(('libc.so', 'libm.so', 'libpthread.so', 'libdl.so', 'librt.so')):
            continue
        if soname != 'libz.so.1':
            raise ValueError('unreviewed non-system linked library: ' + soname)
        library = Path(location.strip().split()[0])
        shutil.copy2(library, runtime / soname)
        notice = Path('/usr/share/licenses/zlib/README')
        if not notice.is_file():
            raise ValueError('zlib package copyright/license material missing')
        shutil.copy2(notice, licenses / 'zlib-README')
        package = subprocess.check_output(['rpm', '-q', 'zlib'], text=True).strip()
        rows.append({'soname': soname, 'package': package, 'sha256': digest(runtime / soname)})
    (material / 'NOTICE.txt').write_text(
        'vcd2fst is built from the enclosed GTKWave source archive.\n'
        'Its helper/FST/FastLZ and LZ4 retain their original MIT/BSD notices.\n'
        'jrb is Copyright (C) 2001 James S. Plank, LGPL-2.1-or-later.\n'
        'The complete unmodified upstream source archive, generated configuration,\n'
        'build recipe, application object files and relink script accompany this binary.\n'
        'The upstream archive also contains other components not linked into vcd2fst;\n'
        'their original notices remain in the archive.\n'
        'See relink/relink.sh for rebuilding with a modified jrb object.\n'
        'No blanket license-compliance assertion is made by this inventory.\n')
    files = {f.relative_to(material).as_posix(): digest(f)
             for f in material.rglob('*') if f.is_file()}
    manifest = {'schema': 1, 'component': 'vcd2fst', 'version': args.version,
                'artifact_sha256': digest(out / 'vcd2fst'), 'files': files,
                'source_urls': ['https://gtkwave.sourceforge.net/gtkwave-' + args.version + '.tar.gz'],
                'source_archive_sha256': digest(args.archive), 'runtime_libraries': rows,
                'unresolved': []}
    (material / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('Exported', len(files), 'vcd2fst material files; binary hash bound in manifest.json')


if __name__ == '__main__':
    main()
