import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget, QLabel
from app.native.service import NativeService
from app.native.settings import SettingsDialog


class NativeStorageInfoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = NativeService(Path(self.temp.name))
        self.service.import_text('测试资料', '已经保存的内容。')
        self.host = QWidget()
        self.host.service = self.service
        self.host.tasks = Mock()
        with patch('app.native.settings.storage_location.read', return_value={}):
            self.dialog = SettingsDialog(self.host)
        self.dialog.show()
        self.app.processEvents()

    def tearDown(self):
        self.dialog.close()
        self.host.close()
        self.service.close()
        self.temp.cleanup()

    def test_storage_card_shows_actual_local_counts_without_new_window(self):
        self.host.tasks.submit.side_effect = lambda work, done, failed: done(work())
        windows = set(QApplication.topLevelWidgets())
        self.dialog.show_storage_details()
        captured = [label.text() for label in self.dialog.storage_card.findChildren(QLabel)]
        self.assertIn(str(self.service.store.directory.resolve()), captured)
        self.assertTrue(any('文章与导图：1 条' in text for text in captured))
        self.assertTrue(self.dialog.storage_details.isEnabled())
        self.assertTrue(self.dialog.storage_card.isVisible())
        self.assertIsNone(QApplication.activeModalWidget())
        self.assertEqual(windows, set(QApplication.topLevelWidgets()))
        self.dialog.show_storage_details()
        self.assertFalse(self.dialog.storage_card.isVisible())

    def test_storage_error_and_late_result_do_not_open_modal(self):
        self.host.tasks.submit.side_effect = lambda work, done, failed: failed('private diagnostic')
        self.dialog.show_storage_details()
        self.assertNotIn('private diagnostic', self.dialog.status.text())
        self.assertTrue(self.dialog.storage_details.isEnabled())
        self.host.tasks.submit.side_effect = None
        self.dialog.show_storage_details()
        done = self.host.tasks.submit.call_args.args[1]
        self.dialog.close()
        done(self.service.storage_info())
        self.assertIsNone(QApplication.activeModalWidget())


if __name__ == '__main__':
    unittest.main()
