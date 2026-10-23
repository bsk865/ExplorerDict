import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication
from app.native.handoff import HandoffDialog


class HandoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_archive_selection_and_export_leave_session_unchanged(self):
        session = {'archives': [{'handoff': '# 第一段\n\n原文引用 [S1]', 'ended_at': '2026-10-23T01:00:00Z'}],
                   'handoff': '# 第二段\n\n待继续的问题', 'ended_at': '2026-10-23T02:00:00Z', 'closed': True}
        original = copy.deepcopy(session)
        dialog = HandoffDialog(Mock(), session)
        self.assertTrue(dialog.document.isReadOnly())
        self.assertEqual(0, dialog.pages.currentIndex())
        dialog.entries[1].click()
        self.assertEqual(session['handoff'], dialog.document.toPlainText())
        dialog.back.click()
        self.assertEqual(0, dialog.pages.currentIndex())
        dialog.entries[0].click()
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / '交接.md'
            with patch('app.native.handoff.QFileDialog.getSaveFileName', return_value=(str(target), '')):
                dialog.export.click()
            self.assertEqual(session['archives'][0]['handoff'], target.read_text(encoding='utf-8'))
        self.assertEqual(original, session)
        dialog.close()

    def test_empty_session_and_cancel_export(self):
        dialog = HandoffDialog(Mock(), {})
        self.assertFalse(dialog.export.isEnabled())
        dialog.close()
        dialog = HandoffDialog(Mock(), {'handoff': '本机记录'})
        dialog.entries[0].click()
        with patch('app.native.handoff.QFileDialog.getSaveFileName', return_value=('', '')), patch('pathlib.Path.write_text') as write:
            dialog.export.click()
            write.assert_not_called()
        dialog.close()

    def test_export_failure_keeps_record_and_allows_retry(self):
        dialog = HandoffDialog(Mock(), {'handoff': '# 记录'})
        dialog.entries[0].click()
        with patch('app.native.handoff.QFileDialog.getSaveFileName', return_value=('record.md', '')), patch('pathlib.Path.write_text', side_effect=PermissionError()):
            dialog.export.click()
        self.assertEqual('# 记录', dialog.document.toPlainText())
        self.assertTrue(dialog.export.isEnabled())
        self.assertIn('重试', dialog.status.text())
        dialog.close()
