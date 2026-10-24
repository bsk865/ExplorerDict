import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QVBoxLayout, QLabel, QLineEdit
from app.native.surfaces import QDialog, QMessageBox, QInputDialog, QFileDialog
from app.native.widgets import stylesheet


class SurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_dialog_and_picker_are_frameless_before_first_show(self):
        for factory in (QDialog, QMessageBox, QFileDialog):
            dialog = factory()
            self.assertTrue(dialog.windowFlags() & Qt.WindowType.FramelessWindowHint)
            if isinstance(dialog, QFileDialog):
                self.assertTrue(dialog.testOption(QFileDialog.Option.DontUseNativeDialog))
            elif isinstance(dialog, QMessageBox):
                dialog.setText('操作确认')
            else:
                QVBoxLayout(dialog).addWidget(QLabel('内容'))
            dialog.setStyleSheet(stylesheet({'palette': 'clay', 'mode': 'dark'}))
            dialog.show()
            self.app.processEvents()
            header = dialog._header
            self.assertTrue(header.isVisible())
            self.assertTrue(dialog.windowFlags() & Qt.WindowType.FramelessWindowHint)
            dialog.hide()
            dialog.show()
            self.assertIs(header, dialog._header)
            dialog.close()

    def test_question_preserves_cancel_default_and_button_result(self):
        def confirm():
            dialog = QApplication.activeModalWidget()
            self.assertIsInstance(dialog, QMessageBox)
            self.assertEqual(dialog.defaultButton(), dialog.button(QMessageBox.StandardButton.No))
            dialog.button(QMessageBox.StandardButton.Yes).click()
        QTimer.singleShot(0, confirm)
        self.assertEqual(QMessageBox.question(None, '确认', '确认操作？'), QMessageBox.StandardButton.Yes)

    def test_text_input_has_application_chrome_and_returns_edited_value(self):
        def confirm():
            dialog = QApplication.activeModalWidget()
            self.assertTrue(dialog.windowFlags() & Qt.WindowType.FramelessWindowHint)
            dialog.findChild(QLineEdit).setText('杭州')
            dialog.accept()
        QTimer.singleShot(0, confirm)
        self.assertEqual(QInputDialog.getText(None, '天气城市', '城市名称'), ('杭州', True))

    def test_picker_returns_existing_file_and_custom_overwrite_can_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '测试.md'
            path.write_text('已保存', encoding='utf-8')
            def choose():
                dialog = QApplication.activeModalWidget()
                self.assertIsInstance(dialog, QFileDialog)
                dialog.selectFile(str(path))
                dialog.accept()
            QTimer.singleShot(0, choose)
            selected, _ = QFileDialog.getOpenFileName(None, '导入', directory, 'Markdown (*.md)')
            self.assertEqual(Path(selected), path)
            dialog = QFileDialog(None, '导出', str(path), 'Markdown (*.md)')
            dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
            with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.No) as prompt:
                dialog.accept()
            prompt.assert_called_once()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
            self.assertEqual(path.read_text(encoding='utf-8'), '已保存')

    def test_save_picker_adds_suffix_and_directory_picker_keeps_current_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            def save():
                dialog = QApplication.activeModalWidget()
                dialog.selectFile('阅读笔记')
                dialog.accept()
            QTimer.singleShot(0, save)
            selected, _ = QFileDialog.getSaveFileName(None, '保存笔记', directory, 'Markdown (*.md)')
            self.assertEqual(Path(selected), Path(directory) / '阅读笔记.md')
            self.assertFalse(Path(selected).exists())
            QTimer.singleShot(0, lambda: QApplication.activeModalWidget().accept())
            self.assertEqual(Path(QFileDialog.getExistingDirectory(None, '存储位置', directory)), Path(directory))


if __name__ == '__main__':
    unittest.main()
