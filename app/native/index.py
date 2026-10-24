"""Local semantic index status and source-chunk inspection."""
from PySide6.QtCore import QObject, Signal, QTimer, Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QListWidget, QListWidgetItem, QLabel)
from .surfaces import QMessageBox
from .widgets import button, heading


class IndexController(QObject):
    changed = Signal()

    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.state = {}
        self.chunks = {}
        self.document = None
        self.offset = 0
        self.revision = 0
        self.loading = False
        self.message = ''
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def tick(self):
        if self.host.current_page == 'index' and not self.host.closing:
            self.refresh()

    def refresh(self, rebuild=False):
        if self.loading:
            return
        self.loading = True
        def read():
            try:
                rag = self.host.service.workspace.rag
                if rebuild:
                    rag.reset()
                rag.request()
                return rag.status()
            except Exception:
                return {'failure': True}
        def loaded(value):
            self.loading = False
            if value.get('failure'):
                self.message = '暂时无法读取索引，请稍后重试。'
            else:
                self.state = value
                self.message = ''
            self.changed.emit()
        self.host.run(read, loaded)

    def inspect(self, document, offset=0):
        self.revision += 1
        revision = self.revision
        self.document, self.offset = document, offset
        self.chunks = {}
        def read():
            try:
                return self.host.service.workspace.rag.inspect(document, offset)
            except ValueError as exc:
                return {'message': str(exc)}
            except Exception:
                return {'message': '暂时无法读取片段，请稍后重试。'}
        def loaded(value):
            if revision == self.revision:
                self.chunks = value
                self.changed.emit()
        self.host.run(read, loaded)


class IndexPage(QWidget):
    def __init__(self, host):
        super().__init__()
        self.host, self.controller = host, host.index_controller
        self.rendering = False
        self.document_rows = None
        self.chunk_rows = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(heading('知识索引'))
        tools = QHBoxLayout()
        self.status = QLabel('正在读取…')
        self.status.setWordWrap(True)
        tools.addWidget(self.status, 1)
        tools.addWidget(button('更新索引', self.controller.refresh))
        self.rebuild = button('重建索引', self.reset)
        tools.addWidget(self.rebuild)
        layout.addLayout(tools)
        split = QSplitter()
        self.documents = QListWidget()
        self.documents.setWordWrap(True)
        self.documents.currentItemChanged.connect(self.select)
        split.addWidget(self.documents)
        right = QWidget()
        detail = QVBoxLayout(right)
        detail.setContentsMargins(12, 0, 0, 0)
        self.chunks = QListWidget()
        self.chunks.setWordWrap(True)
        self.chunks.itemDoubleClicked.connect(self.open_source)
        detail.addWidget(self.chunks, 1)
        self.info = QLabel()
        detail.addWidget(self.info)
        pages = QHBoxLayout()
        self.previous = button('上一页', lambda: self.controller.inspect(self.controller.document, max(0, self.controller.offset-30)))
        self.next = button('下一页', lambda: self.controller.inspect(self.controller.document, self.controller.offset+30))
        pages.addWidget(self.previous)
        pages.addWidget(self.next)
        pages.addStretch()
        pages.addWidget(button('查看原文', lambda: self.open_source(self.chunks.currentItem())))
        detail.addLayout(pages)
        split.addWidget(right)
        split.setSizes([380, 660])
        split.setChildrenCollapsible(False)
        layout.addWidget(split, 1)
        self.controller.changed.connect(self.render)
        self.render()
        self.controller.refresh()

    def render(self):
        self.rendering = True
        c, state = self.controller, self.controller.state
        if c.message:
            summary = c.message
        elif not state:
            summary = '正在读取…'
        elif not state.get('available'):
            summary = '语义检索暂不可用，仍可使用关键词检索'
        elif state.get('running'):
            progress = state.get('progress', [0, 0])
            summary = f'正在建立索引 · {progress[0]} / {progress[1]} 个片段'
        else:
            summary = f"{state.get('ready', 0)} 份资料可检索 · {state.get('vectors', 0)} 个片段"
        self.status.setText(summary)
        self.rebuild.setEnabled(not state.get('running'))
        labels = {'ready': '可语义检索', 'pending': '等待索引', 'error': '尚未完成，可重试', 'empty': '暂无文字'}
        documents = state.get('documents', [])
        if documents != self.document_rows:
            scroll = self.documents.verticalScrollBar().value()
            self.documents.clear()
            for row in documents:
                item = QListWidgetItem(f"{row['title']}\n{labels.get(row['state'], '')} · {row['chunks']} 个片段", self.documents)
                item.setData(Qt.ItemDataRole.UserRole, row)
                if row['id'] == c.document:
                    self.documents.setCurrentItem(item)
            self.documents.verticalScrollBar().setValue(scroll)
            self.document_rows = documents
        chunks = (c.document, c.offset, c.chunks.get('items', []))
        if chunks != self.chunk_rows:
            self.chunks.clear()
            for i, row in enumerate(chunks[2]):
                item = QListWidgetItem(f"片段 {c.offset+i+1}\n{row['quote']}", self.chunks)
                item.setData(Qt.ItemDataRole.UserRole, row)
            self.chunk_rows = chunks
        total = c.chunks.get('total', 0)
        self.info.setText(c.chunks.get('message') or ('原文已变化，正在更新索引。' if c.chunks.get('pending') else f'共 {total} 个片段'))
        self.previous.setEnabled(bool(c.document and c.offset))
        self.next.setEnabled(bool(c.document and c.offset+30 < total))
        self.rendering = False

    def select(self, current, previous=None):
        if current and not self.rendering:
            self.controller.inspect(current.data(Qt.ItemDataRole.UserRole)['id'])

    def reset(self):
        if QMessageBox.question(self, '重建检索索引', '重新整理检索索引？原文、笔记和问答会保留。',
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.controller.refresh(True)

    def open_source(self, item):
        if item:
            row = item.data(Qt.ItemDataRole.UserRole)
            self.host.open_article(row['article_id'], block=row.get('block'))
