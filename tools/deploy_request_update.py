"""Deploy both verified desktop packages while preserving user databases."""
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.paths import data_dir


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def stopped():
    result = subprocess.run(['powershell', '-NoProfile', '-Command',
        "Get-Process | Where-Object { $_.ProcessName -like '探索词典*' } | Select-Object -ExpandProperty Id"],
        capture_output=True, check=True)
    if result.stdout.strip():
        raise RuntimeError('Close the applications before deploying')


def database_hashes(directory):
    hashes = {}
    for name in ('explorer_dict.sqlite3', 'reading_workspace.sqlite3'):
        path = directory / name
        if path.exists():
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
                hashes[name] = hashlib.sha256('\n'.join(db.iterdump()).encode()).hexdigest()
    return hashes


def main():
    stopped()
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup = ROOT / 'artifacts/deployment-backups' / ('requests-' + stamp)
    staging = ROOT / ('_request-staging-' + stamp)
    directory = data_dir().resolve()
    before = database_hashes(directory)
    backup.mkdir(parents=True)
    staging.mkdir()
    for name in before:
        with closing(sqlite3.connect(directory / name)) as source, closing(sqlite3.connect(backup / name)) as target:
            source.backup(target)
            assert target.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    packages = [
        (ROOT / 'artifacts/package/探索词典', '探索词典.exe', '_runtime'),
        (ROOT / 'artifacts/reading-package/探索词典-初版1', '探索词典-初版1.exe', '_reading_runtime_v7'),
    ]
    names = []
    checked = 0
    for package, exe, runtime in packages:
        assert (package / exe).is_file() and (package / runtime).is_dir()
        shutil.copy2(package / exe, staging / exe)
        shutil.copytree(package / runtime, staging / runtime)
        names.extend([exe, runtime])
        for source in [package / exe, *(package / runtime).rglob('*')]:
            if source.is_file():
                assert digest(source) == digest(staging / source.relative_to(package))
                checked += 1
    stopped()
    moved, installed = [], []
    try:
        for name in names:
            if (ROOT / name).exists():
                (ROOT / name).rename(backup / name)
                moved.append(name)
            (staging / name).rename(ROOT / name)
            installed.append(name)
        assert before == database_hashes(directory), 'Database changed during deployment'
        for package, exe, runtime in packages:
            for source in [package / exe, *(package / runtime).rglob('*')]:
                if source.is_file():
                    assert digest(source) == digest(ROOT / source.relative_to(package))
    except Exception:
        for name in reversed(installed):
            (ROOT / name).rename(staging / name)
        for name in reversed(moved):
            (backup / name).rename(ROOT / name)
        raise
    staging.rmdir()
    report = {'backup': str(backup), 'files_verified': checked, 'database_unchanged': True,
              'executables': {exe: digest(ROOT / exe) for _, exe, _ in packages}}
    (backup / 'deployment.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True))


if __name__ == '__main__':
    main()
