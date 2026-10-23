"""In-document navigation without changing saved annotations."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLineEdit, QLabel
from .widgets import button


class FindBar(QWidget):
    def __init__(self, editor, parent):
        super().__init__(parent)
        self.editor = editor
        self.matches = []
        self.index = -1
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.query = QLineEdit()
        self.query.setPlaceholderText('查找文章内容')
        self.query.setAccessibleName('文内查找')
        self.query.setMinimumWidth(60)
        self.count = QLabel()
        self.previous = button('上一处', lambda: self.step(-1), tip='Shift+Enter')
        self.next = button('下一处', lambda: self.step(1), tip='Enter')
        for control in (self.query, self.count, self.previous, self.next, button('×', self.dismiss, tip='关闭查找')):
            row.addWidget(control, 1 if control is self.query else 0)
        self.query.textChanged.connect(self.search)
        self.shortcuts = []
        for sequence, action, owner in [('Ctrl+F', self.open, parent), ('F3', lambda: self.step(1), parent),
                                        ('Shift+F3', lambda: self.step(-1), parent),
                                        ('Return', lambda: self.step(1), self),
                                        ('Enter', lambda: self.step(1), self),
                                        ('Shift+Return', lambda: self.step(-1), self), ('Escape', self.dismiss, self)]:
            shortcut = QShortcut(QKeySequence(sequence), owner)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(action)
            self.shortcuts.append(shortcut)
        self.hide()

    def open(self):
        self.show()
        self.query.setFocus()
        self.query.selectAll()

    def search(self):
        self.matches = []
        query = self.query.text()
        cursor = QTextCursor(self.editor.document())
        if query:
            while True:
                cursor = self.editor.document().find(query, cursor)
                if cursor.isNull():
                    break
                self.matches.append(cursor)
        self.index = -1
        self.step(1)

    def step(self, direction):
        if self.isHidden() and self.query.text():
            self.open()
        available = bool(self.matches)
        self.previous.setEnabled(available)
        self.next.setEnabled(available)
        if available:
            self.index = (self.index + direction) % len(self.matches)
            self.editor.setTextCursor(self.matches[self.index])
            self.editor.ensureCursorVisible()
            self.count.setText(f'{self.index + 1} / {len(self.matches)}')
        else:
            cursor = self.editor.textCursor()
            cursor.clearSelection()
            self.editor.setTextCursor(cursor)
            self.count.setText('未找到' if self.query.text() else '')

    def dismiss(self):
        self.hide()
        cursor = self.editor.textCursor()
        cursor.clearSelection()
        self.editor.setTextCursor(cursor)
        self.editor.setFocus()
