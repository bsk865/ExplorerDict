"""Keep a conversation readable while its controls and background state change."""
from copy import deepcopy
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QTextBrowser


class TranscriptBrowser(QTextBrowser):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._identity = object()
        self._revision = None

    def update_content(self, identity, revision, write):
        if identity == self._identity and revision == self._revision:
            return
        same_conversation = identity == self._identity
        bar = self.verticalScrollBar()
        position = bar.value()
        following = bar.maximum() - position <= 2
        cursor = self.textCursor()
        selection = (cursor.anchor(), cursor.position())
        prefix = self.toPlainText()[:max(selection)] if cursor.hasSelection() else None
        write()
        if same_conversation and prefix is not None and self.toPlainText().startswith(prefix):
            cursor = QTextCursor(self.document())
            cursor.setPosition(selection[0])
            cursor.setPosition(selection[1], QTextCursor.MoveMode.KeepAnchor)
            self.setTextCursor(cursor)
        bar.setValue(bar.maximum() if not same_conversation or following else position)
        self._identity = identity
        # Controllers can append messages and update evidence in place.
        self._revision = deepcopy(revision)
