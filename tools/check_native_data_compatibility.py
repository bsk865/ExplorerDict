"""Audit a SQLite backup of real data without modifying the source databases."""
import hashlib
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import subprocess
from contextlib import closing

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def fingerprints(path):
    result = {}
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as db:
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            quoted = '"'+name.replace('"', '""')+'"'
            hashes = []
            for row in db.execute('SELECT * FROM '+quoted):
                digest = hashlib.sha256()
                for value in row:
                    data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
                    digest.update(type(value).__name__.encode()+b':'+str(len(data)).encode()+b':'+data)
                hashes.append(digest.digest())
            result[name] = {'rows': len(hashes), 'sha256': hashlib.sha256(b''.join(sorted(hashes))).hexdigest()}
        result['_integrity'] = db.execute('PRAGMA integrity_check').fetchone()[0]
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source', nargs='?', type=Path, default=ROOT/'data')
    parser.add_argument('--executable', type=Path)
    args = parser.parse_args()
    source = args.source.resolve()
    parent = ROOT/'artifacts/native-compatibility'
    parent.mkdir(parents=True, exist_ok=True)
    destination = Path(tempfile.mkdtemp(prefix='copy-', dir=parent))
    before = {}
    source_before = {}
    for name in ('explorer_dict.sqlite3', 'reading_workspace.sqlite3'):
        source_before[name] = fingerprints(source/name)
        with closing(sqlite3.connect((source/name).as_uri()+'?mode=ro', uri=True)) as original:
            with closing(sqlite3.connect(destination/name)) as copied:
                original.backup(copied)
        before[name] = fingerprints(destination/name)
    (destination / '.native-compatibility-copy').touch()
    os.environ['EXPLORER_DICT_DATA_DIR'] = str(destination)
    os.environ['EXPLORER_DICT_STORAGE_CONFIG'] = str(destination/'storage-location.json')
    if args.executable:
        executable = args.executable.resolve()
        environment = os.environ.copy()
        environment.pop('PYTHONPATH', None)
        environment.pop('PYTHONHOME', None)
        system = Path(environment.get('SystemRoot', 'C:/Windows'))
        environment['PATH'] = str(system/'System32') + os.pathsep + str(system)
        with (destination/'process.log').open('w', encoding='utf-8') as log:
            result = subprocess.run([str(executable), '--verify-data-copy', '--data-dir', str(destination)],
                                    cwd=executable.parent, env=environment, stdout=log, stderr=log, timeout=90)
        code = result.returncode
    else:
        from app.native.compatibility import check_copy
        code = check_copy(destination)
    report = json.loads((destination/'compatibility-runtime.json').read_text(encoding='utf-8'))
    report.update(directory=str(destination), before=before, exit_code=code)
    report['after'] = {name: fingerprints(destination/name) for name in before}
    report['source_unchanged'] = all(fingerprints(source/name) == value for name, value in source_before.items())
    report['unchanged'] = all(report['after'][name].get(table) == value for name in before for table, value in before[name].items())
    report['passed'] = (report['passed'] and code == 0 and report['unchanged'] and report['source_unchanged']
                        and (not args.executable or report['frozen'])
                        and all(v['_integrity'] == 'ok' for v in report['after'].values()))
    output = destination/'compatibility.json'
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('directory', 'frozen', 'articles_loaded', 'marks_loaded', 'images_loaded', 'unchanged', 'source_unchanged', 'errors', 'passed')}, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
