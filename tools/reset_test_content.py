"""Explicit, backed-up reset of local user content while retaining configuration."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile

ROOT = Path(__file__).resolve().parents[1]
TABLES = {
    'explorer_dict.sqlite3': set('batches entries relations explain_cache event_log chat_turns map_graphs tags entry_tags node_pins map_edge_blocks articles article_map_drafts article_map_edits'.split()),
    'reading_workspace.sqlite3': set('articles assets objects sessions attempts source_chunks source_indexes visual_index capture_links study_themes study_memberships study_notes study_tasks study_batches study_reading study_visits reading_annotations rag_documents rag_vectors map_parts'.split()),
}


def preserved(db, name):
    if name == 'explorer_dict.sqlite3':
        settings = db.execute("SELECT * FROM settings WHERE k != 'ui.current_batch_id' ORDER BY k").fetchall()
        secrets = db.execute('SELECT * FROM secrets ORDER BY rowid').fetchall()
    else:
        settings = [r for r in db.execute('SELECT * FROM settings ORDER BY id') if not content_setting(r[0])]
        secrets = []
    return hashlib.sha256(repr((settings, secrets)).encode()).hexdigest()


def content_setting(key):
    return (key.startswith(('native-note-drafts:', 'native-reading:'))
            or key in {'native-advisor-draft', 'study-weather-cache'})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    source = args.directory.resolve(strict=True)
    report = {'directory': str(source), 'before': {}, 'after': {}, 'settings_preserved': {}}
    for name, allowed in TABLES.items():
        with closing(sqlite3.connect((source/name).as_uri()+'?mode=ro', uri=True)) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            unknown = tables - allowed - {'settings', 'secrets', 'sqlite_sequence'}
            if unknown:
                raise ValueError(f'Unreviewed tables in {name}: {sorted(unknown)}')
            report['before'][name] = {t: db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                                      for t in sorted(tables & allowed)}
    if not args.apply:
        print(json.dumps(report, ensure_ascii=False))
        return
    parent = ROOT/'artifacts/content-reset-backups'
    parent.mkdir(parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix='before-reset-', dir=parent))
    report['backup'] = str(backup)
    for name in TABLES:
        with closing(sqlite3.connect((source/name).as_uri()+'?mode=ro', uri=True)) as db:
            with closing(sqlite3.connect(backup/name)) as copied:
                db.backup(copied)
                assert copied.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    for name in TABLES:
        with closing(sqlite3.connect(source/name)) as db:
            before = preserved(db, name)
            with db:
                for table in report['before'][name]:
                    db.execute(f'DELETE FROM "{table}"')
                if name == 'explorer_dict.sqlite3':
                    db.execute("UPDATE settings SET v='0' WHERE k='ui.current_batch_id'")
                    db.execute('DELETE FROM sqlite_sequence')
                else:
                    keys = [r[0] for r in db.execute('SELECT id FROM settings') if content_setting(r[0])]
                    db.executemany('DELETE FROM settings WHERE id=?', [(k,) for k in keys])
                assert preserved(db, name) == before, 'Configuration changed'
                assert not db.execute('PRAGMA foreign_key_check').fetchall()
            report['after'][name] = {t: db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                                     for t in report['before'][name]}
            report['settings_preserved'][name] = preserved(db, name) == before
            assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    report['passed'] = all(not count for values in report['after'].values() for count in values.values())
    (backup/'reset-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
