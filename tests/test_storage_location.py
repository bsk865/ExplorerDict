from contextlib import closing
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app import storage_location as location

class StorageLocationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'old';self.source.mkdir()
        self.target=self.root/'new'
        self.env=patch.dict(os.environ,{'EXPLORER_DICT_STORAGE_CONFIG':str(self.root/'location.json')});self.env.start()
        with closing(sqlite3.connect(self.source/'reading_workspace.sqlite3')) as db:
            db.execute('CREATE TABLE notes(text TEXT)');db.execute("INSERT INTO notes VALUES('user data')");db.commit()
        (self.source/'capture-status.json').write_text('{}')
        (self.source/'asset.bin').write_bytes(b'asset')
    def tearDown(self):self.env.stop();self.temp.cleanup()
    def test_schedule_then_copy_and_switch_preserves_original(self):
        location.schedule(self.source,self.target)
        self.assertEqual(location.read()['active'],str(self.source))
        self.assertTrue(location.apply_pending(self.source))
        self.assertEqual(location.read()['active'],str(self.target))
        for directory in [self.source,self.target]:
            with closing(sqlite3.connect(directory/'reading_workspace.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT text FROM notes').fetchone()[0],'user data')
        self.assertFalse((self.target/'capture-status.json').exists())
        self.assertEqual((self.target/'asset.bin').read_bytes(),b'asset')
    def test_nonempty_or_nested_destination_rejected(self):
        self.target.mkdir();(self.target/'keep').write_text('keep')
        for dest in [self.target,self.source/'nested',self.root]:
            with self.assertRaises(ValueError):location.schedule(self.source,dest)
        self.assertFalse(location.config_path().exists())
    def test_failed_copy_never_switches_active(self):
        location.schedule(self.source,self.target)
        with patch('app.storage_location.shutil.copy2',side_effect=OSError('disk full')):
            self.assertFalse(location.apply_pending(self.source))
        self.assertEqual(location.read()['active'],str(self.source))
        self.assertIn('error',location.read())
    def test_other_instance_cannot_migrate_pending_data(self):
        location.schedule(self.source,self.target)
        self.assertFalse(location.apply_pending(self.root/'unrelated'))
        self.assertEqual(location.read()['active'],str(self.source))
    def test_cancel_pending_preserves_location(self):
        location.schedule(self.source,self.target);location.schedule(self.source,self.source)
        self.assertNotIn('pending',location.read())

    def test_unconfirmed_component_exit_blocks_migration(self):
        location.schedule(self.source,self.target)
        self.assertFalse(location.apply_pending(self.source,components_stopped=False))
        self.assertEqual(location.read()['active'],str(self.source))
        self.assertIn('pending',location.read())
        self.assertIn('error',location.read())
        self.assertFalse((self.target/'reading_workspace.sqlite3').exists())
