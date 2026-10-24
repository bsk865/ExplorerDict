"""View and export the existing local conversation handoffs."""
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import (QVBoxLayout, QHBoxLayout, QWidget, QStackedWidget, QScrollArea, QPlainTextEdit, QLabel)
from .surfaces import QDialog, QFileDialog
from .widgets import button


def records(session):
    return list(session.get('archives', [])) + ([session] if session.get('handoff') else [])


class HandoffDialog(QDialog):
    def __init__(self, host, session, parent=None):
        super().__init__(parent)
        self.host = host
        self.records = records(session)
        self.setWindowTitle('对话回顾')
        self.resize(640, 520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        note = QLabel('原对话与对话回顾保存在本机，可随时导出。')
        note.setWordWrap(True)
        note.setObjectName('muted')
        layout.addWidget(note)
        self.pages = QStackedWidget()
        listing = QScrollArea()
        listing.setWidgetResizable(True)
        listing.setFrameShape(QScrollArea.Shape.NoFrame)
        rows = QWidget()
        rows.setObjectName('handoffRows')
        rows.setStyleSheet('QWidget#handoffRows {background:transparent;}')
        rows_layout = QVBoxLayout(rows)
        rows_layout.setContentsMargins(0, 0, 0, 0)
        rows_layout.setSpacing(10)
        self.entries = []
        for index, record in enumerate(self.records):
            try:
                date = datetime.fromisoformat(record.get('ended_at', '')).astimezone().strftime('%Y-%m-%d %H:%M')
            except (ValueError, TypeError):
                date = '时间未记录'
            entry = button(f'第 {index+1} 段 · {date}', lambda checked=False, i=index: self.select(i))
            entry.setMinimumHeight(44)
            entry.setStyleSheet('text-align:left;')
            rows_layout.addWidget(entry)
            self.entries.append(entry)
        if not self.records:
            rows_layout.addWidget(QLabel('当前会话尚未生成对话回顾。'))
        rows_layout.addStretch()
        listing.setWidget(rows)
        rows.setAutoFillBackground(False)
        listing.viewport().setAutoFillBackground(False)
        self.pages.addWidget(listing)
        self.document = QPlainTextEdit()
        self.document.setReadOnly(True)
        self.document.setAccessibleName('对话回顾正文')
        self.pages.addWidget(self.document)
        layout.addWidget(self.pages, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setObjectName('muted')
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.back = button('返回列表', self.show_list)
        actions.addWidget(self.back)
        self.export = button('导出 Markdown', self.save)
        actions.addWidget(self.export)
        actions.addStretch()
        actions.addWidget(button('关闭', self.reject))
        layout.addLayout(actions)
        self.show_list()

    def select(self, index):
        text = self.records[index].get('handoff', '') if 0 <= index < len(self.records) else ''
        self.document.setPlainText(text or '当前会话尚未生成对话回顾。')
        self.pages.setCurrentIndex(1)
        self.back.show()
        self.export.show()
        self.export.setEnabled(bool(text))
        self.status.clear()
        self.document.setFocus()

    def show_list(self):
        self.pages.setCurrentIndex(0)
        self.back.hide()
        self.export.hide()
        self.export.setEnabled(False)
        self.status.clear()

    def save(self):
        if not self.export.isEnabled():
            return
        filename, _ = QFileDialog.getSaveFileName(self, '导出对话回顾', '小探-对话回顾.md', 'Markdown (*.md)')
        if not filename:
            return
        try:
            Path(filename).write_text(self.document.toPlainText(), encoding='utf-8')
        except OSError:
            self.status.setText('未能保存，请选择其他位置后重试。')
            return
        self.status.setText('对话回顾已导出。')
