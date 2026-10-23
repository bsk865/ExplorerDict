"""Preserve a stopped installation and compare it with a deployment backup."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from datetime import datetime


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    root = Path(__file__).resolve().parents[1]
    target = root / 'artifacts' / 'startup-incident' / datetime.now().strftime('%Y%m%d-%H%M%S')
    target.mkdir(parents=True)
    snapshot = target / 'snapshot'
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode('utf-8').split('\0')
    files = {root / name for name in tracked if name and (root / name).is_file()}
    for name in ('_runtime', '_reading_runtime_v7', 'data'):
        files.update(p for p in (root / name).rglob('*') if p.is_file())
    files.update(root.glob('*.exe'))
    for name in ('storage-location.json', 'storage-location.tmp'):
        if (root / name).is_file():
            files.add(root / name)
    manifest = []
    for source in sorted(files):
        relative = source.relative_to(root)
        destination = snapshot / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        before = digest(source)
        shutil.copy2(source, destination)
        if digest(destination) != before or digest(source) != before:
            raise RuntimeError(f'Changed during preservation: {relative}')
        manifest.append({'path': relative.as_posix(), 'size': source.stat().st_size,
                         'mtime_ns': source.stat().st_mtime_ns, 'sha256': before})
    (target / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    backup = root / 'artifacts/deployment-backups/requests-20261021-175758'
    changes = []
    names = ('_reading_runtime_v7', '_runtime')
    current = {m['path']: m for m in manifest if m['path'].startswith(tuple(n + '/' for n in names))
               or m['path'] in ('探索词典-初版1.exe', '探索词典.exe')}
    old = {p.relative_to(backup).as_posix(): p for n in names for p in (backup / n).rglob('*') if p.is_file()}
    old.update({p.name: p for p in backup.glob('*.exe')})
    for name in sorted(current.keys() | old.keys()):
        if name not in old:
            changes.append({'path': name, 'change': 'added'})
        elif name not in current:
            changes.append({'path': name, 'change': 'missing'})
        elif digest(old[name]) != current[name]['sha256']:
            changes.append({'path': name, 'change': 'modified'})
    (target / 'backup-comparison.json').write_text(json.dumps(changes, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'incident': str(target), 'files_verified': len(manifest),
                      'changes': changes}, ensure_ascii=False))


if __name__ == '__main__':
    main()
