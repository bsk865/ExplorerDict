"""Article notes with durable drafts and serialized background saves."""
from uuid import uuid4
from PySide6.QtCore import QObject, Signal, QTimer, Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QToolButton, QScrollArea)
from .surfaces import QMessageBox
from .widgets import button


class NoteDraft(QObject):
    changed = Signal()
    saved = Signal(object)

    def __init__(self, owner, body):
        super().__init__(owner)
        self.owner = owner
        self.body = dict(body)
        self.body.setdefault('draft_id', uuid4().hex)
        self.dirty = False
        self.busy = False
        self.revision = 0
        self.retired = False
        self.message = '已保存' if body.get('id') else '尚未填写正文'
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(650)
        self.timer.timeout.connect(self.flush)

    def edit(self, title, content):
        if self.retired:
            return
        self.body.update(title=title, content=content)
        self.revision += 1
        self.dirty = True
        self.message = '正在保存…' if content.strip() else '尚未填写正文'
        self.owner.persist()
        self.timer.start()
        self.changed.emit()

    def flush(self):
        self.timer.stop()
        if self.retired or self.busy or not self.dirty or not self.body.get('content', '').strip():
            return
        if len(self.body['content'].strip()) > 20000:
            self.message = '内容超过20000字，请精简后保存'
            self.changed.emit()
            return
        self.busy = True
        revision, body = self.revision, dict(self.body)
        def done(value):
            self.busy = False
            if self.retired:
                return
            if value is None:
                self.timer.start()
                return
            self.body['id'] = value['id']
            self.dirty = self.revision != revision
            self.message = '正在保存…' if self.dirty else '已保存'
            self.owner.persist()
            self.saved.emit(value)
            self.changed.emit()
            if self.dirty:
                self.flush()
        def failed(message):
            self.busy = False
            if self.retired:
                return
            self.message = '保存未完成，草稿已保留'
            self.changed.emit()
        def save():
            with self.owner.host.service.store.lock:
                if self.retired:
                    return None
                return self.owner.host.service.hub.save('notes', body)
        self.owner.host.tasks.submit(save, done, failed)
        self.changed.emit()


class NotesController(QObject):
    changed = Signal()

    def __init__(self, host, article, bundle):
        super().__init__(host)
        self.host, self.article = host, article
        self.key = 'native-note-drafts:' + article['id']
        self.items = list(bundle['notes'])
        self.legacy = list(bundle['legacy'])
        self.drafts = []
        self.active = None
        for body in host.service.store.get('settings', self.key, []):
            draft = self.make(body)
            draft.dirty = True
            draft.message = '草稿已恢复'
        if not self.active:
            self.make({'article_id': article['id']})

    def make(self, body):
        draft = NoteDraft(self, body)
        draft.saved.connect(self.saved)
        self.drafts.append(draft)
        self.active = draft
        return draft

    def persist(self):
        self.host.service.store.put('settings', self.key, [dict(d.body) for d in self.drafts if d.dirty])

    def saved(self, value):
        self.items = [value] + [item for item in self.items if item['id'] != value['id']]
        self.changed.emit()

    def select(self, body=None):
        body = body or {'article_id': self.article['id']}
        old = next((d for d in self.drafts if
                    (body.get('id') and d.body.get('id') == body['id']) or
                    (body.get('draft_id') and d.body['draft_id'] == body['draft_id'])), None)
        if self.active is not old:
            self.active.flush()
        self.active = old or self.make(body)
        self.changed.emit()

    def remove(self):
        draft = self.active
        if draft.busy:
            return
        draft.timer.stop()
        draft.busy = True
        draft.changed.emit()
        key = draft.body.get('id')
        def done(_):
            self.items = [item for item in self.items if item['id'] != key]
            self.drafts.remove(draft)
            if self.active is draft:
                self.make({'article_id': self.article['id']})
            self.persist()
            self.changed.emit()
        def failed(message):
            draft.busy = False
            draft.message = '删除未完成，请重试'
            draft.changed.emit()
        if key:
            self.host.tasks.submit(lambda: self.host.service.hub.delete('notes', key), done, failed)
        else:
            done(None)

    def flush(self):
        for draft in self.drafts:
            draft.flush()

    def pause_matching(self, matches):
        paused = [draft for draft in self.drafts if matches(draft.body)]
        for draft in paused:
            draft.retired = True
            draft.timer.stop()
        return paused

    def finish_removal(self, paused, success):
        if not paused:
            return
        for draft in paused:
            if success:
                if draft in self.drafts:
                    self.drafts.remove(draft)
                if self.active is draft:
                    self.make({'article_id': self.article['id']})
            else:
                draft.retired = False
                if draft.dirty:
                    draft.timer.start()
        if success:
            self.persist()
            self.changed.emit()


class NotesPanel(QScrollArea):
    def __init__(self, reader):
        super().__init__()
        self.reader, self.host = reader, reader.host
        controllers = self.host.note_controllers
        aid = reader.article['id']
        if aid not in controllers:
            controllers[aid] = NotesController(self.host, reader.article, reader.bundle['notes'])
        self.controller = controllers[aid]
        self.controller.items = list(reader.bundle['notes']['notes'])
        self.controller.legacy = list(reader.bundle['notes']['legacy'])
        for draft in list(self.controller.drafts):
            if draft.body.get('id') and not draft.dirty and not draft.busy:
                current = next((n for n in self.controller.items if n['id'] == draft.body['id']), None)
                if current:
                    draft.body.update(current)
                else:
                    self.controller.drafts.remove(draft)
                    if self.controller.active is draft:
                        self.controller.make({'article_id': aid})
        self.draft = None
        self.loading = False
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget()
        body.setObjectName('page')
        body.setStyleSheet('QPushButton {font-size:11px; padding:4px 6px;} QLabel {font-size:12px;}'
                          'QToolButton {font-size:12px; background:transparent; border:0; padding:4px 0;}'
                          'QToolButton:checked {background:transparent;}')
        self.setWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(8, 12, 8, 8)
        layout.setSpacing(10)
        top = QHBoxLayout()
        top.addWidget(QLabel('心得与笔记'), 1)
        top.addWidget(button('新建', lambda: self.controller.select()))
        layout.addLayout(top)
        self.reference = QToolButton()
        self.reference.setText('引用原文')
        self.reference.setCheckable(True)
        self.reference.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.quote = QLabel()
        self.quote.setWordWrap(True)
        self.quote.setTextFormat(Qt.TextFormat.PlainText)
        self.reference.toggled.connect(self.toggle_reference)
        layout.addWidget(self.reference)
        layout.addWidget(self.quote)
        layout.addWidget(QLabel('标题'))
        self.title = QLineEdit()
        self.title.setMaxLength(200)
        self.title.setPlaceholderText('这次阅读的发现')
        layout.addWidget(self.title)
        layout.addWidget(QLabel('我的心得'))
        self.content = QPlainTextEdit()
        self.content.setPlaceholderText('记录理解、疑问或新的想法')
        self.content.setMinimumHeight(120)
        self.content.setMaximumHeight(200)
        layout.addWidget(self.content)
        self.status = QLabel()
        self.status.setObjectName('muted')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.save = button('保存', lambda: self.draft.flush())
        self.delete = button('删除', self.remove, tip='删除笔记')
        actions.addStretch()
        actions.addWidget(self.save)
        actions.addWidget(self.delete)
        layout.addLayout(actions)
        self.entries = QVBoxLayout()
        layout.addLayout(self.entries)
        self.legacy_toggle = QToolButton()
        self.legacy_toggle.setCheckable(True)
        self.legacy_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.legacy = QLabel()
        self.legacy.setTextFormat(Qt.TextFormat.PlainText)
        self.legacy.setWordWrap(True)
        self.legacy.hide()
        self.legacy_toggle.toggled.connect(self.toggle_legacy)
        layout.addWidget(self.legacy_toggle)
        layout.addWidget(self.legacy)
        layout.addStretch()
        self.title.textEdited.connect(self.edit)
        self.content.textChanged.connect(self.edit)
        self.controller.changed.connect(self.render)
        self.render()

    def toggle_reference(self, checked):
        self.quote.setVisible(checked)
        self.reference.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)

    def toggle_legacy(self, checked):
        self.legacy.setVisible(checked)
        self.legacy_toggle.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)

    def render(self):
        controller = self.controller
        if self.draft is not controller.active:
            if self.draft:
                self.draft.changed.disconnect(self.sync_status)
            self.draft = controller.active
            self.draft.changed.connect(self.sync_status)
            self.loading = True
            self.title.setText(self.draft.body.get('title', ''))
            self.content.setPlainText(self.draft.body.get('content', ''))
            self.quote.setText(self.draft.body.get('quote', ''))
            self.reference.setVisible(bool(self.quote.text()))
            self.reference.setChecked(False)
            self.toggle_reference(False)
            self.loading = False
        while self.entries.count():
            item = self.entries.takeAt(0)
            item.widget().deleteLater()
        drafts = [dict(d.body, native_draft=True) for d in controller.drafts if d.dirty and not d.body.get('id')]
        for note in drafts + controller.items:
            row = QWidget()
            column = QVBoxLayout(row)
            column.setContentsMargins(0, 8, 0, 8)
            choose = button('', lambda checked=False, n=note: controller.select(n))
            title_layout = QVBoxLayout(choose)
            title_layout.setContentsMargins(6, 4, 6, 4)
            title = QLabel(note.get('title') or '阅读心得')
            title.setTextFormat(Qt.TextFormat.PlainText)
            title.setWordWrap(True)
            title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            title_layout.addWidget(title)
            choose.setAccessibleName(title.text())
            choose.setToolTip(note.get('title') or '阅读心得')
            column.addWidget(choose)
            preview = QLabel(note['content'][:130])
            preview.setTextFormat(Qt.TextFormat.PlainText)
            preview.setWordWrap(True)
            column.addWidget(preview)
            date = '草稿' if note.get('native_draft') else note.get('updated_at', '')[:10]
            if not note.get('native_draft') and note.get('version') != self.reader.article['version']:
                date += ' · 旧版本笔记'
            stamp = QLabel(date)
            stamp.setObjectName('muted')
            column.addWidget(stamp)
            if note.get('block'):
                column.addWidget(button('查看引用', lambda checked=False, n=note: self.locate(n)))
            self.entries.addWidget(row)
        self.legacy_toggle.setText(f'已有笔记 · {len(controller.legacy)}')
        self.legacy_toggle.setVisible(bool(controller.legacy))
        self.legacy.setText('\n\n'.join(n['title']+'\n'+n['note'] for n in controller.legacy))
        self.toggle_legacy(self.legacy_toggle.isChecked())
        self.sync_status()

    def sync_status(self):
        self.status.setText(self.draft.message)
        self.save.setEnabled(bool(self.content.toPlainText().strip()) and not self.draft.busy)
        self.delete.setVisible(bool(self.draft.body.get('id')) or self.draft.dirty)
        self.delete.setEnabled(not self.draft.busy)

    def edit(self):
        if not self.loading:
            self.draft.edit(self.title.text(), self.content.toPlainText())

    def locate(self, note):
        if note['article_id'] == self.reader.article['id']:
            self.reader.text.locate(note['block'])
        else:
            self.host.open_article(note['article_id'], block=note['block'])

    def remove(self):
        if QMessageBox.question(self, '删除笔记', '这条笔记将被删除，文章原文保留。') == QMessageBox.StandardButton.Yes:
            self.controller.remove()

    def start(self, anchor):
        self.controller.select({'article_id': self.reader.article['id'], **anchor})
        self.content.setFocus()
