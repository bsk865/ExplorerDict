"""Check the isolated distribution without launching it or reading user data."""
import argparse
import hashlib
import json
from pathlib import Path


def audit(directory):
    directory = directory.resolve()
    required = [
        '探索词典-原生版.exe', '探索词典.exe',
        '_native_runtime/python314.dll',
        '_native_runtime/PySide6/Qt6Widgets.dll',
        '_native_runtime/PySide6/plugins/platforms/qwindows.dll',
        '_native_runtime/web/assets/native-icons.json',
        '_native_runtime/web/assets/brand-mark.svg',
        '_native_runtime/assets/native-chevron.svg',
        '_native_runtime/assets/native-chevron-up.svg',
        '_runtime/web/index.html',
    ]
    errors = ['missing: ' + name for name in required if not (directory / name).is_file()]
    for runtime in ('_runtime', '_native_runtime'):
        index = directory/runtime/'web/index.html'
        if index.is_file():
            import re
            for filename in re.findall(r'href="([^"/]+\.css)"', index.read_text(encoding='utf-8')):
                if not (index.parent/filename).is_file():
                    errors.append('missing: '+runtime+'/web/'+filename)
    files = sorted(p for p in directory.rglob('*') if p.is_file())
    names = [p.relative_to(directory).as_posix() for p in files]
    for name in names:
        lower = name.lower()
        if 'webengine' in lower:
            errors.append('browser engine: ' + name)
        if (lower.startswith('data/') or lower.endswith(('.sqlite3', '.sqlite3-wal', '.sqlite3-shm'))
                or lower in {'storage-location.json', 'storage-location.tmp'}):
            errors.append('local data: ' + name)
    if not any(name.startswith('_native_runtime/models/multilingual-e5-small/') and name.endswith('.onnx') for name in names):
        errors.append('missing: local embedding model')
    if not any(name.startswith('_runtime/') for name in names):
        errors.append('missing: capture runtime')
    for palette in ('clay', 'blue', 'forest', 'ocean', 'parchment', 'violet'):
        for art in ('xiaotan-avatar', 'reading-scene'):
            name = f'_native_runtime/web/assets/generated/{palette}-{art}.webp'
            if name not in names:
                errors.append('missing: ' + name)
    executables = {}
    for name in required[:2]:
        path = directory / name
        if path.is_file():
            with path.open('rb') as stream:
                executables[name] = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'directory': str(directory), 'file_count': len(files),
            'bytes': sum(p.stat().st_size for p in files), 'executables_sha256': executables,
            'errors': errors, 'passed': not errors}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    report = audit(args.directory)
    result = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(result, encoding='utf-8')
    print(result)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
