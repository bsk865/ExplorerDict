"""Saved words and image objects, with source-bound review state."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem, QSplitter, QLabel, QTextBrowser
from .widgets import button, heading
from .dialogs import EditForm


class SavedPage(QWidget):
    def __init__(self, host, rows):
        super().__init__()
        self.host = host
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(heading('收藏与回顾'))
        split = QSplitter()
        self.rows = QListWidget()
        self.rows.setWordWrap(True)
        for row in rows:
            if not row.get('saved'):
                continue
            item = QListWidgetItem(row['title']+'\n'+row['article_title']+(' · 已理解' if row.get('understood') else ''), self.rows)
            item.setData(Qt.ItemDataRole.UserRole, row)
        split.addWidget(self.rows)
        detail = QWidget()
        column = QVBoxLayout(detail)
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setMaximumHeight(180)
        column.addWidget(self.image)
        self.note = QTextBrowser()
        self.note.setOpenLinks(False)
        column.addWidget(self.note, 1)
        commands = QHBoxLayout()
        self.review = button('标记理解', self.toggle_understood)
        self.edit = button('记录心得', self.edit_note)
        self.open = button('回到原文', self.open_source)
        self.remove = button('取消收藏', self.unsave)
        for control in (self.review, self.edit, self.open, self.remove):
            commands.addWidget(control)
        column.addLayout(commands)
        split.addWidget(detail)
        split.setSizes([350, 700])
        split.setChildrenCollapsible(False)
        layout.addWidget(split, 1)
        self.rows.currentItemChanged.connect(self.show_item)
        self.rows.itemDoubleClicked.connect(lambda _: self.open_source())
        self.show_item()

    def selected(self):
        item = self.rows.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def show_item(self, *_):
        row = self.selected()
        for control in (self.review, self.edit, self.open, self.remove):
            control.setEnabled(bool(row))
        self.image.clear()
        self.image.hide()
        if not row:
            self.note.setPlainText('选择一项收藏' if self.rows.count() else '还没有收藏')
            return
        self.review.setText('已理解' if row.get('understood') else '标记理解')
        self.note.setPlainText(row['title']+'\n\n'+row['article_title']+' · '+row['version'][:12]+'\n\n'+(row.get('note') or ''))
        if row.get('asset'):
            _, raw = self.host.service.store.asset(row['asset'])
            pixmap = QPixmap()
            pixmap.loadFromData(raw)
            self.image.setPixmap(pixmap.scaled(320, 180, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            self.image.show()

    def toggle_understood(self):
        row = self.selected()
        if row:
            row.update(self.host.service.store.update_object(row['id'], {'understood': not row.get('understood')}))
            self.rows.currentItem().setData(Qt.ItemDataRole.UserRole, row)
            self.show_item()

    def edit_note(self):
        row = self.selected()
        if row:
            def save(body):
                row.update(self.host.service.store.update_object(row['id'], body))
                self.rows.currentItem().setData(Qt.ItemDataRole.UserRole, row)
            if EditForm('收藏心得', [('note', '心得', 'long', row.get('note', ''), None)], save, self).exec():
                self.show_item()

    def open_source(self):
        row = self.selected()
        if row:
            self.host.open_article(row['article_id'], block=row['block'], object_id=row['id'])

    def unsave(self):
        row = self.selected()
        if row:
            self.host.run(lambda: self.host.service.store.update_object(row['id'], {'saved': False}))
