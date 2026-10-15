"""Explicit maintenance reset. Run only after all app processes have stopped."""
from contextlib import closing
import datetime
import json
import shutil
import sqlite3
from pathlib import Path


def reset(root):
    root = Path(root).resolve()
    data = root / 'data'
    backup = root / 'artifacts' / ('content-reset-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(parents=True, exist_ok=False)
    report = {}
    for name in ('explorer_dict.sqlite3', 'reading_workspace.sqlite3'):
        source = data / name
        if not source.exists():
            continue
        with closing(sqlite3.connect(source)) as db:
            with closing(sqlite3.connect(backup / name)) as target:
                db.backup(target)
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            protected = {t: db.execute('SELECT * FROM "'+t+'"').fetchall() for t in tables if t in {'settings','secrets'}}
            contents = [t for t in tables if t not in protected]
            report[name] = {t: db.execute('SELECT count(*) FROM "'+t+'"').fetchone()[0] for t in contents}
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute('PRAGMA secure_delete=ON')
            with db:
                for table in contents:
                    db.execute('DELETE FROM "'+table+'"')
            for table, rows in protected.items():
                assert db.execute('SELECT * FROM "'+table+'"').fetchall() == rows
            # Reset stale off-screen/oversized floating geometry only, not model preferences.
            if name == 'explorer_dict.sqlite3':
                with db:
                    db.execute("DELETE FROM settings WHERE k LIKE 'ui.panel.%'")
            assert all(db.execute('SELECT count(*) FROM "'+t+'"').fetchone()[0] == 0 for t in contents)
            assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert not db.execute('PRAGMA foreign_key_check').fetchall()
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            db.execute('VACUUM')
    for name in ('exports', 'logs', 'workspace-browser'):
        source = (data / name).resolve()
        assert source.parent == data.resolve()
        if source.exists():
            shutil.move(str(source), str(backup / name))
    for source in [*data.glob('workspace-session*.json'), data / 'capture-status.json']:
        if source.exists():
            shutil.move(str(source), str(backup / source.name))
    (backup / 'reset-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return backup, report


if __name__ == '__main__':
    backup, report = reset(Path(__file__).resolve().parent.parent)
    print(json.dumps({'backup':str(backup), 'cleared':report}, ensure_ascii=False))
