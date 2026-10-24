"""Prevent a fresh reader build from silently shipping an older selection UI."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sources():
    files = [ROOT/'bootstrap.py', ROOT/'desktop_entry.py', ROOT/'scripts/UiaNative.cs', ROOT/'scripts/uia_helper.ps1']
    files += sorted((ROOT/'app').rglob('*.py'))
    files += [ROOT/'ExplorerDict.spec', ROOT/'NativeWorkspace.spec', ROOT/'web/index.html']
    files += sorted((ROOT/'web').glob('*.css'))
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('package', type=Path)
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--snapshot', type=Path)
    parser.add_argument('--source-snapshot', type=Path)
    args = parser.parse_args()
    path = args.package/'companion-source.json'
    current = sources()
    if args.snapshot:
        args.snapshot.parent.mkdir(parents=True, exist_ok=True)
        args.snapshot.write_text(json.dumps(current, indent=2), encoding='utf-8')
        print('Build source snapshot recorded.')
        return 0
    if args.write:
        if args.source_snapshot and json.loads(args.source_snapshot.read_text(encoding='utf-8')) != current:
            print('Sources changed during the build; rebuild before packaging.')
            return 1
        executable = args.package/'探索词典.exe'
        path.write_text(json.dumps(dict(sources=current, executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest()), indent=2), encoding='utf-8')
        print('Capture companion source manifest recorded.')
        return 0
    try:
        stored = json.loads(path.read_text(encoding='utf-8'))
        exe_hash = hashlib.sha256((args.package/'探索词典.exe').read_bytes()).hexdigest()
        valid = stored.get('sources') == current and stored.get('executable_sha256') == exe_hash
    except (OSError, ValueError):
        valid = False
    print('Capture companion matches current sources.' if valid else 'Capture companion is stale; rebuild with tools/build_app.ps1.')
    return 0 if valid else 1


if __name__ == '__main__':
    raise SystemExit(main())
