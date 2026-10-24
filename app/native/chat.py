"""Article and selected-object conversations without browser transport."""
from PySide6.QtCore import QObject, Signal, QTimer, Qt, QEvent
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QComboBox, QTextBrowser, QPlainTextEdit, QListWidget, QListWidgetItem, QLabel, QToolButton, QScrollArea, QPushButton, QLayout, QSizePolicy
from .widgets import PageComboBox as QComboBox
from .widgets import button, QuestionInput, FlowLayout
from .dialogs import EditForm
from .handoff import HandoffDialog, records as handoff_records
from .. import conversation_budget
from ..paths import resource_root


class ArticleConversation(QObject):
    changed = Signal()

    def __init__(self, host, article, object_id=''):
        super().__init__(host)
        self.host = host
        self.article_id, self.object_id = article['id'], object_id
        self.key = article['id'] + ':' + article['version'] + ':' + object_id
        self.session = host.service.store.get('sessions', self.key, {'messages': [], 'draft': ''})
        self.job = None
        self.message = ''
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.poll)

    def draft(self, value):
        self.session = self.host.service.store.update_session(self.key, {'draft': value})

    def send(self, question, scope='article', *, intent=''):
        if self.job or self.session.get('closed') or not question.strip():
            return
        self.draft(question)
        try:
            result = self.host.service.workspace.start('chat', {'article_id': self.article_id,
                'object_id': self.object_id, 'question': question, 'scope': scope, 'intent': intent})
        except ValueError as exc:
            self.message = str(exc)
        else:
            self.job = result['job']
            self.message = '小探正在整理回答…'
            self.timer.start()
        self.changed.emit()

    def poll(self):
        with self.host.service.workspace.lock:
            job = dict(self.host.service.workspace.jobs.get(self.job, {}))
        if job.get('status') == 'running':
            return
        self.timer.stop()
        self.job = None
        self.session = self.host.service.store.get('sessions', self.key, {'messages': [], 'draft': ''})
        if job.get('status') == 'done':
            if not self.session.get('closed'):
                self.draft('')
            self.message = '本段对话已结束，对话回顾已保存' if self.session.get('closed') else '回答已保存'
        else:
            self.message = '已停止，问题已保留' if job.get('status') == 'cancelled' else job.get('message') or '暂时未能完成，请重试。'
        self.changed.emit()

    def stop(self):
        with self.host.service.workspace.lock:
            job = self.host.service.workspace.jobs.get(self.job)
            if job:
                job['cancel'].set()
                self.message = '正在停止…'
        self.changed.emit()

    def restart(self):
        if not self.job:
            self.session = conversation_budget.restart(self.host.service.store, self.key)
            self.message = '已接续新对话'
            self.changed.emit()


class ChatPanel(QScrollArea):
    def __init__(self, reader):
        super().__init__()
        self.setObjectName('page')
        self.setStyleSheet('QComboBox {font-size:11px; padding:5px;} QPushButton {font-size:11px; padding:4px 6px;}')
        self.reader, self.host = reader, reader.host
        self.article = reader.article
        self.controller = None
        self.rendering = False
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget()
        body.setObjectName('page')
        self.setWidget(body)
        layout = QVBoxLayout(body)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        layout.setContentsMargins(8, 12, 8, 8)
        self.focus = QComboBox()
        self.focus.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.focus.setMinimumContentsLength(4)
        self.focus.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.focus.addItem('整篇文章', '')
        for obj in self.article['objects']:
            self.focus.addItem(obj['title'], obj['id'])
        layout.addWidget(self.focus)
        layout.addWidget(button('探索记录', lambda: self.reader.show_records('objects')))
        tools = FlowLayout(spacing=6)
        self.explain = button('解释', self.explain_focus)
        self.saved = button('收藏', self.save_object)
        self.understood = button('已理解', self.mark_understood)
        for control in (self.explain, self.saved, self.understood):
            tools.addWidget(control)
        layout.addLayout(tools)
        self.image_button = button('查看图片与选区', self.open_image)
        layout.addWidget(self.image_button)
        self.scope = QComboBox()
        self.scope.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.scope.addItem('查阅本文', 'article')
        self.scope.addItem('查阅资料书架', 'library')
        layout.addWidget(self.scope)
        self.focus_evidence_toggle = QToolButton()
        self.focus_evidence_toggle.setText('原文依据')
        self.focus_evidence_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.focus_evidence_toggle.setCheckable(True)
        self.focus_evidence_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.focus_evidence_toggle.setStyleSheet('QToolButton {border:0; padding:5px 0;}')
        layout.addWidget(self.focus_evidence_toggle)
        self.focus_evidence = QTextBrowser()
        self.focus_evidence.setMaximumHeight(110)
        self.focus_evidence.hide()
        layout.addWidget(self.focus_evidence)
        self.focus_locate = button('在原文定位', self.locate_focus)
        self.focus_locate.hide()
        layout.addWidget(self.focus_locate)
        self.focus_evidence_toggle.toggled.connect(self.toggle_focus_evidence)
        self.welcome = QScrollArea()
        self.welcome.setWidgetResizable(True)
        self.welcome.setFrameShape(QScrollArea.Shape.NoFrame)
        self.welcome.setMinimumHeight(90)
        welcome_body = QWidget()
        welcome_body.setObjectName('page')
        welcome_layout = QVBoxLayout(welcome_body)
        welcome_layout.setContentsMargins(6, 12, 6, 8)
        welcome_layout.setSpacing(12)
        avatar = QLabel()
        palette = self.host.snapshot['preferences']['palette']
        avatar.setPixmap(QPixmap(str(resource_root() / 'web/assets/generated' / (palette+'-xiaotan-portrait.webp'))).scaled(
            85, 85, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.welcome_avatar = avatar
        welcome_layout.addWidget(avatar)
        welcome_title = QLabel('从一个问题开始。')
        welcome_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        welcome_title.setWordWrap(True)
        self.welcome_title = welcome_title
        welcome_title.setStyleSheet('font-size:19px; font-weight:500;')
        welcome_layout.addWidget(welcome_title)
        self.suggestions = []
        for question in ('这篇文章的核心观点是什么？', '有哪些值得关注的概念？'):
            suggestion = button(question, lambda checked=False, text=question: self.input.setPlainText(text))
            suggestion.setStyleSheet('font-size:11px; padding:8px 6px;')
            welcome_layout.addWidget(suggestion)
            self.suggestions.append(suggestion)
        welcome_layout.addStretch()
        self.welcome.setWidget(welcome_body)
        self.welcome.viewport().installEventFilter(self)
        layout.addWidget(self.welcome, 3)
        self.messages = QTextBrowser()
        self.messages.setMinimumHeight(0)
        self.messages.setOpenLinks(False)
        self.messages.setStyleSheet('QTextBrowser {border:0; padding:6px;}')
        layout.addWidget(self.messages, 3)
        self.evidence = QListWidget()
        self.evidence.setMaximumHeight(100)
        self.evidence.itemDoubleClicked.connect(self.locate)
        layout.addWidget(self.evidence)
        self.handoff = button('对话回顾', lambda: HandoffDialog(self.host, self.controller.session, self).exec())
        layout.addWidget(self.handoff, 0, Qt.AlignmentFlag.AlignLeft)
        self.input = QuestionInput()
        self.input.setMaximumHeight(100)
        self.input.setMinimumHeight(54)
        self.input.setPlaceholderText('针对原文继续提问…')
        layout.addWidget(self.input)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        actions = FlowLayout(spacing=6)
        self.note = button('会话笔记', self.edit_note)
        self.restart = button('接续', lambda: self.controller.restart())
        self.stop = button('停止', lambda: self.controller.stop())
        self.send = button('发送', lambda: self.controller.send(self.input.toPlainText(), self.scope.currentData()), primary=True)
        self.input.submitted.connect(self.send.click)
        for control in (self.note, self.restart, self.stop, self.send):
            actions.addWidget(control)
        layout.addLayout(actions)
        for control in self.findChildren(QPushButton):
            control.setMinimumHeight(28)
        self.focus.currentIndexChanged.connect(self.choose)
        self.input.textChanged.connect(self.draft)
        self.choose()

    def choose(self, *_):
        self.focus_evidence_toggle.setChecked(False)
        if self.controller:
            self.controller.changed.disconnect(self.render)
        oid = self.focus.currentData()
        key = self.article['id'] + ':' + self.article['version'] + ':' + oid
        if key not in self.host.article_chats:
            self.host.article_chats[key] = ArticleConversation(self.host, self.article, oid)
        self.controller = self.host.article_chats[key]
        if not self.controller.job:
            self.controller.session = self.host.service.store.get('sessions', key, {'messages': [], 'draft': ''})
        self.controller.changed.connect(self.render)
        self.render()

    def eventFilter(self, watched, event):
        if hasattr(self, 'suggestions') and len(self.suggestions) == 2 and watched is self.welcome.viewport() and event.type() == QEvent.Type.Resize:
            height = max(90, self.viewport().height() - 230)
            narrow = self.welcome.viewport().width() < 240
            self.welcome_avatar.setVisible(height >= 200)
            self.welcome_title.setVisible(height >= 100)
            self.suggestions[1].setVisible(height >= 220)
            self.suggestions[0].setText('这篇文章的\n核心观点是什么？' if narrow else '这篇文章的核心观点是什么？')
            self.suggestions[1].setText('有哪些值得\n关注的概念？' if narrow else '有哪些值得关注的概念？')
        return super().eventFilter(watched, event)

    def set_focus(self, oid):
        index = self.focus.findData(oid)
        if index >= 0:
            self.focus.setCurrentIndex(index)

    def draft(self):
        if not self.rendering:
            self.controller.draft(self.input.toPlainText())

    def current_object(self):
        return next((obj for obj in self.article['objects'] if obj['id'] == self.focus.currentData()), None)

    def render(self):
        self.rendering = True
        c = self.controller
        self.messages.setMarkdown('\n\n'.join(('**我**' if m.get('role') == 'user' else '**小探**') + '\n\n' + m['content']
                                               for m in c.session.get('messages', [])))
        self.messages.verticalScrollBar().setValue(self.messages.verticalScrollBar().maximum())
        if self.input.toPlainText() != c.session.get('draft', ''):
            self.input.setPlainText(c.session.get('draft', ''))
        self.evidence.clear()
        for message in c.session.get('messages', [])[-1:]:
            for hit in message.get('evidence', []):
                item = QListWidgetItem(f"[{hit.get('citation', '')}] {hit.get('article_title', '')}\n{hit.get('quote', '')[:100]}", self.evidence)
                item.setData(Qt.ItemDataRole.UserRole, hit)
        self.evidence.setVisible(self.evidence.count() > 0)
        obj = self.current_object()
        welcome = not obj and not c.session.get('messages') and not c.job
        self.welcome.setVisible(welcome)
        self.messages.setVisible(not welcome)
        self.explain.setVisible(bool(obj))
        self.focus_evidence_toggle.setVisible(bool(obj))
        self.toggle_focus_evidence(bool(obj) and self.focus_evidence_toggle.isChecked())
        if obj:
            block = next((b for b in self.article['blocks'] if b['id'] == obj['block']), {})
            self.focus_evidence.setPlainText(obj.get('capture_context') or block.get('text') or block.get('caption', ''))
        self.saved.setVisible(bool(obj))
        self.understood.setVisible(bool(obj))
        self.saved.setText('已收藏' if obj and obj['saved'] else '收藏')
        self.understood.setText('已理解' if obj and obj['understood'] else '标记理解')
        self.scope.setVisible(not obj)
        self.image_button.setVisible(bool(obj and obj['type'] != 'term'))
        self.restart.setVisible(bool(c.session.get('closed')))
        count = len(handoff_records(c.session))
        self.handoff.setText(f'对话回顾 · {count}')
        self.handoff.setVisible(bool(count))
        for control in (self.input, self.send, self.explain, self.restart):
            control.setEnabled(not c.job)
        self.send.setEnabled(not c.job and not c.session.get('closed'))
        self.explain.setEnabled(not c.job and not c.session.get('closed'))
        self.stop.setEnabled(bool(c.job))
        self.stop.setVisible(bool(c.job))
        self.status.setText(c.message)
        self.status.setVisible(bool(c.message))
        self.rendering = False

    def toggle_focus_evidence(self, expanded):
        self.focus_evidence.setVisible(expanded)
        self.focus_locate.setVisible(expanded)
        self.focus_evidence_toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)

    def locate_focus(self):
        obj = self.current_object()
        if obj:
            self.reader.text.locate(obj['block'])

    def explain_focus(self):
        obj = self.current_object()
        subject = ('关键词“' + obj['title'] + '”') if obj and obj['type'] == 'term' else '当前图片选区' if obj and obj['type'] == 'region' else '当前图片' if obj else '这篇文章'
        self.controller.send('请解释' + subject + '。', 'article', intent='explain')

    def save_object(self):
        obj = self.current_object()
        if obj:
            obj.update(self.host.service.store.update_object(obj['id'], {'saved': not obj['saved']}))
            self.render()

    def open_image(self):
        from .images import ImageDialog
        obj = self.current_object()
        if obj and obj['type'] != 'term':
            raw = self.reader.bundle['assets'].get(obj['asset'], b'')
            ImageDialog(self.host, self.article, obj, raw).exec()

    def mark_understood(self):
        obj = self.current_object()
        if obj:
            obj.update(self.host.service.store.update_object(obj['id'], {'understood': not obj['understood']}))
            self.render()

    def edit_note(self):
        c = self.controller
        dialog = EditForm('会话笔记', [('note', '心得', 'long', c.session.get('note', ''), None)],
                          lambda body: self.host.service.store.update_session(c.key, body), self)
        if dialog.exec():
            c.session = self.host.service.store.get('sessions', c.key, {})
            self.host.notice('笔记已保存')

    def locate(self, item):
        hit = item.data(Qt.ItemDataRole.UserRole)
        if hit['article_id'] == self.article['id']:
            self.reader.text.locate(hit.get('block'))
        else:
            self.host.open_article(hit['article_id'], block=hit.get('block'))
