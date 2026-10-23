import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PySide6.QtWidgets import QApplication, QWidget
from app.native.service import NativeService
from app.native.settings import SettingsDialog
from app import storage_location


class StorageSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'EXPLORER_DICT_STORAGE_CONFIG': str(self.root/'location.json')})
        self.env.start()
        self.host = QWidget()
        self.host.service = NativeService(self.root/'data')
        self.dialog = SettingsDialog(self.host)

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self.host.service.close()
        self.host.deleteLater()
        self.app.processEvents()
        self.env.stop()
        self.temp.cleanup()

    def test_failed_migration_is_visible_without_debug_details(self):
        storage_location.write({'active': str(self.root/'data'), 'pending': str(self.root/'next'),
                                'error': 'Internal exception detail'})
        self.dialog.show_storage()
        self.assertIn('迁移未完成', self.dialog.pending.text())
        self.assertNotIn('Internal', self.dialog.pending.text())
        self.assertEqual(str(self.root/'next'), self.dialog.target.text())

    def test_cancel_failure_preserves_pending_then_retry_succeeds(self):
        storage_location.schedule(self.root/'data', self.root/'next')
        with patch('app.native.settings.storage_location.schedule', side_effect=OSError()):
            self.dialog.cancel_location()
        self.assertIn('未能取消', self.dialog.status.text())
        self.assertIn('pending', storage_location.read())
        self.dialog.cancel_location()
        self.assertNotIn('pending', storage_location.read())
        self.assertEqual('已取消位置变更', self.dialog.status.text())
