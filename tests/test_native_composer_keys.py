import unittest
from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton
from app.native.widgets import QuestionInput


class ComposerKeysTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_enter_newline_and_control_enter_send_without_editing(self):
        editor = QuestionInput()
        send = QPushButton('发送')
        sent = []
        send.clicked.connect(lambda: sent.append(editor.toPlainText()))
        editor.submitted.connect(send.click)
        editor.setPlainText('原文问题')
        editor.moveCursor(editor.textCursor().MoveOperation.End)
        QTest.keyClick(editor, Qt.Key.Key_Return)
        self.assertEqual('原文问题\n', editor.toPlainText())
        self.assertEqual([], sent)
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            QTest.keyClick(editor, key, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(['原文问题\n', '原文问题\n'], sent)
        self.assertEqual('原文问题\n', editor.toPlainText())
        editor.close()

    def test_disabled_button_and_repeated_key_do_not_send(self):
        editor = QuestionInput()
        send = QPushButton('发送')
        sent = []
        send.clicked.connect(lambda: sent.append(True))
        editor.submitted.connect(send.click)
        send.setEnabled(False)
        QTest.keyClick(editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        self.assertFalse(sent)
        send.setEnabled(True)
        repeat = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return,
                           Qt.KeyboardModifier.ControlModifier, '\r', True, 2)
        QApplication.sendEvent(editor, repeat)
        self.assertFalse(sent)
        editor.setEnabled(False)
        QApplication.sendEvent(editor, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return,
                                                 Qt.KeyboardModifier.ControlModifier))
        self.assertFalse(sent)
        editor.close()


if __name__ == '__main__':
    unittest.main()
