from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from app.workspace_store import WorkspaceStore
from tools.reset_reading_content import reset


class ContentResetTests(unittest.TestCase):
    def test_reset_clears_materials_and_keeps_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = WorkspaceStore(root / 'data')
            store.import_article('测试资料', [{'type':'text','text':'测试正文'}])
            store.put('settings', 'model', {'model':'unchanged'})
            store.put('sessions', 'draft', {'note':'旧笔记'})
            with closing(sqlite3.connect(root / 'data/explorer_dict.sqlite3')) as db:
                db.executescript("CREATE TABLE settings(k TEXT,v TEXT); CREATE TABLE secrets(name TEXT,blob BLOB); CREATE TABLE entries(id INTEGER,term TEXT); INSERT INTO settings VALUES('api.model','keep'); INSERT INTO settings VALUES('ui.panel.x','-9999'); INSERT INTO secrets VALUES('api_key',X'1234'); INSERT INTO entries VALUES(1,'旧词');")
                db.commit()
            backup, _ = reset(root)
            self.assertEqual(store.summaries(), [])
            self.assertEqual(store.get('settings','model'), {'model':'unchanged'})
            self.assertIsNone(store.get('sessions','draft'))
            with closing(sqlite3.connect(root / 'data/explorer_dict.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT * FROM entries').fetchall(), [])
                self.assertEqual(db.execute('SELECT * FROM settings').fetchall(), [('api.model','keep')])
                self.assertEqual(db.execute('SELECT blob FROM secrets').fetchone()[0], b'\x12\x34')
            with closing(sqlite3.connect(backup / 'reading_workspace.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM articles').fetchone()[0], 1)
