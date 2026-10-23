import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout
from app.native.find import FindBar
from app.native.reader import OriginalText
from app.native.imports import import_files, BatchImportDialog
from app.native.service import NativeService


class ReadingToolsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_find_unicode_wrap_and_preserve_marks(self):
        parent = QWidget()
        layout = QVBoxLayout(parent)
        editor = OriginalText()
        editor.load_article({'blocks': [{'id': 'b0', 'type': 'text', 'text': '😀资料 one 资料 TWO'}]}, {})
        editor.annotate([{'style': 'yellow', 'segments': [{'block': 'b0', 'start': 1, 'end': 3}]}])
        bar = FindBar(editor, parent)
        layout.addWidget(bar)
        layout.addWidget(editor)
        parent.show()
        bar.open()
        bar.query.setText('资料')
        self.assertEqual('1 / 2', bar.count.text())
        self.assertEqual('资料', editor.textCursor().selectedText())
        self.assertEqual(2, editor.textCursor().selectionStart())
        bar.step(-1)
        self.assertEqual('2 / 2', bar.count.text())
        bar.step(1)
        self.assertEqual('1 / 2', bar.count.text())
        bar.query.setText('two')
        self.assertEqual('TWO', editor.textCursor().selectedText())
        bar.query.setText('不存在')
        self.assertEqual('未找到', bar.count.text())
        self.assertFalse(editor.textCursor().hasSelection())
        self.assertFalse(bar.next.isEnabled())
        bar.dismiss()
        self.assertTrue(bar.isHidden())
        selections = editor.extraSelections()
        self.assertEqual('资料', selections[0].cursor.selectedText())
        parent.close()

    def test_batch_keeps_successes_when_one_file_fails_and_can_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            service = NativeService(Path(folder)/'data')
            try:
                first, last = Path(folder)/'first.txt', Path(folder)/'last.txt'
                first.write_text('第一篇原文', encoding='utf-8')
                last.write_text('第二篇原文', encoding='utf-8')
                paths = [str(first), str(Path(folder)/'missing.txt'), str(last), str(first)]
                results = import_files(service, paths, lambda _: None, Event())
                self.assertEqual(3, len(results))
                self.assertIn('error', results[1])
                self.assertEqual(2, len(service.store.summaries()))
                cancel = Event()
                stopped = import_files(service, paths, lambda _: cancel.set(), cancel)
                self.assertEqual(1, len(stopped))
                self.assertEqual(2, len(service.store.summaries()))
            finally:
                service.close()

    def test_import_dialog_waits_for_current_file_before_closing(self):
        host = QWidget()
        host.tasks = Mock()
        host.service = Mock()
        dialog = BatchImportDialog(host, ['first.txt', 'second.txt'])
        dialog.show()
        dialog.reject()
        self.assertTrue(dialog.isVisible())
        self.assertTrue(dialog.cancel.is_set())
        self.assertFalse(dialog.stop.isEnabled())
        dialog.finished_import([{'name': 'first.txt', 'article_id': 'a'}])
        self.assertIn('未处理 1 个', dialog.status.text())
        dialog.reject()
        self.assertFalse(dialog.isVisible())
        host.close()


if __name__ == '__main__':
    unittest.main()
