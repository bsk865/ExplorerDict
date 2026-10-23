import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from PySide6.QtWidgets import QApplication
from app.native.appearance import AppearanceDialog, ThemePreview, THEMES
from app import reading_preferences


class NativeAppearanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.dialog = AppearanceDialog(self.directory)
        self.dialog.show()
        self.app.processEvents()

    def tearDown(self):
        self.dialog.close()
        self.temp.cleanup()

    def test_choices_and_mode_persist_immediately_and_reopen(self):
        changed = Mock()
        self.dialog.changed.connect(changed)
        for key, name in THEMES:
            control = self.dialog.choices[key]
            self.assertEqual(name, control.accessibleName())
            self.assertFalse(control.findChild(ThemePreview).pixmap.isNull())
            control.click()
            self.assertEqual(key, reading_preferences.load(self.directory)['palette'])
            self.assertEqual(1, sum(b.isChecked() for b in self.dialog.choices.values()))
        self.dialog.mode.click()
        self.assertEqual({'palette':'ocean', 'mode':'dark'}, reading_preferences.load(self.directory))
        self.assertEqual('切换纸面', self.dialog.mode.text())
        self.dialog.reject()
        reopened = AppearanceDialog(self.directory)
        self.assertTrue(reopened.choices['ocean'].isChecked())
        self.assertEqual('dark', reopened.value['mode'])
        reopened.close()
        self.assertEqual(6, changed.call_count)

    def test_failed_save_retains_previous_theme(self):
        changed = Mock()
        self.dialog.changed.connect(changed)
        with patch('app.native.appearance.reading_preferences.save', side_effect=OSError('test failure')):
            self.dialog.choices['forest'].click()
        self.assertTrue(self.dialog.choices['clay'].isChecked())
        self.assertEqual('clay', self.dialog.value['palette'])
        self.assertNotIn('test failure', self.dialog.message.text())
        changed.assert_not_called()

    def test_preview_grid_reflows_without_overlaps(self):
        for width, columns in ((620, 3), (400, 2), (620, 3)):
            self.dialog.resize(width, 450)
            for _ in range(3):
                self.app.processEvents()
            self.assertEqual(columns, self.dialog.columns)
            rectangles = [b.geometry() for b in self.dialog.choices.values()]
            for i, rect in enumerate(rectangles):
                self.assertLess(rect.right(), width)
                self.assertTrue(all(not rect.intersects(other) for other in rectangles[i+1:]))


if __name__ == '__main__':
    unittest.main()
