"""Native library conversations using the existing retrieval and job pipeline."""
from html import escape
from PySide6.QtCore import QObject, Signal, QTimer, Qt, QRegularExpression
from PySide6.QtGui import QPixmap, QTextCursor, QTextDocument, QColor, QTextBlockFormat, QTextCharFormat
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem, QCheckBox, QTextBrowser, QScrollArea, QPlainTextEdit, QLabel, QDialogButtonBox, QMenu)
from .surfaces import QDialog, QMessageBox
from .widgets import button, heading, FlowLayout
from .. import library_assistant, conversation_budget, reading_preferences
from .dialogs import EditForm
from .topbar import ElidedLabel
from .transcript import TranscriptBrowser
from .handoff import HandoffDialog, records as handoff_records
from ..paths import resource_root


class LibraryController(QObject):
    changed = Signal()
    previewed = Signal()

    def __init__(self, host):
        super().__init__(host)
        self.host = host
        saved_scope = host.service.store.get('settings', 'native-library-scope', {})
        self.initialized = bool(saved_scope)
        self.body = {'article_ids': [], 'include_notes': False, 'mode': 'question', **saved_scope, 'scope': saved_scope.get('scope', 'all')}
        self.key = None
        self.session = {}
        self.hits = []
        self.job = None
        self.busy = False
        self.message = ''
        self.revision = 0
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.poll)

    def select(self, ids, notes, mode, scope='selected'):
        if self.busy:
            return
        self.revision += 1
        revision = self.revision
        self.body = {'article_ids': ids, 'include_notes': notes, 'mode': mode, 'scope': scope}
        self.initialized = True
        self.host.service.store.put('settings', 'native-library-scope', self.body)
        self.key, self.session, self.hits = None, {}, []
        self.message = ''
        self.changed.emit()
        if not ids and scope != 'all':
            return
        body = self.body.copy()
        def load():
            try:
                return library_assistant.session(self.host.service.workspace, body)
            except Exception:
                return {'error': '资料暂时未能打开，请重新选择资料。'}
        def loaded(value):
            if revision == self.revision:
                if 'error' in value:
                    self.message = value['error']
                else:
                    self.key, self.session = value['key'], value['session']
                self.changed.emit()
        self.host.run(load, loaded)

    def draft(self, text):
        if not self.key:
            return
        store = self.host.service.store
        with store.lock:
            saved = store.get('sessions', self.key, {'messages': []})
            saved.update(draft=text, article_ids=self.body['article_ids'])
            store.put('sessions', self.key, saved)
        self.session['draft'] = text

    def request(self, text, preview=False, mode='question'):
        if self.busy or not self.key or self.session.get('closed') or not text.strip():
            return
        self.draft(text)
        body = dict(self.body, question=text.strip(), session_key=self.key, mode=mode)
        self.busy = True
        self.message = '正在查找资料…' if preview else '小探正在整理回答…'
        self.changed.emit()
        # Return errors as values so a failed submission always releases the controls.
        def work():
            try:
                if preview:
                    return {'hits': library_assistant.search(self.host.service.workspace, body)[3]}
                return self.host.service.workspace.start('library-chat', body)
            except ValueError as exc:
                return {'error': str(exc)}
            except Exception:
                return {'error': '暂时未能完成，请稍后重试。'}
        def started(result):
            if 'job' in result:
                self.job = result['job']
                self.timer.start()
            else:
                self.busy = False
                self.hits = result.get('hits', [])
                self.message = result.get('error') or (f'找到 {len(self.hits)} 处相关内容' if self.hits else '暂未找到相关内容')
            self.changed.emit()
            if preview and 'error' not in result:
                self.previewed.emit()
        self.host.run(work, started)

    def poll(self):
        workspace = self.host.service.workspace
        with workspace.lock:
            job = workspace.jobs.get(self.job, {})
            status = job.get('status')
            message = job.get('message', '')
        if status == 'running':
            return
        self.timer.stop()
        self.busy, self.job = False, None
        self.session = workspace.store.get('sessions', self.key, {})
        self.hits = [hit for m in self.session.get('messages', [])[-1:] for hit in m.get('evidence', [])]
        self.message = '回答已保存' if status == 'done' else ('已停止' if status == 'cancelled' else message or '暂时未能完成，请重试。')
        self.changed.emit()

    def stop(self):
        with self.host.service.workspace.lock:
            job = self.host.service.workspace.jobs.get(self.job)
            if job:
                job['cancel'].set()
                self.message = '正在停止…'
        self.changed.emit()

    def restart(self):
        if self.busy or not self.key:
            return
        self.session = conversation_budget.restart(self.host.service.store, self.key)
        self.hits = []
        self.message = '已接续新对话，旧对话已保留'
        self.changed.emit()

    def clear(self):
        if self.busy or not self.key:
            return
        with self.host.service.workspace.lock:
            if any(job.get('status') == 'running' and not job.get('background') for job in self.host.service.workspace.jobs.values()):
                self.host.notice('请先停止当前任务，再清空对话。')
                return
            self.session = {'messages': [], 'draft': ''}
            self.host.service.store.put('sessions', self.key, self.session)
        self.hits = []
        self.message = ''
        self.changed.emit()


class LibraryPage(QWidget):
    def __init__(self, host, snapshot):
        super().__init__()
        self.host, self.controller = host, host.library
        self.snapshot = snapshot
        self.rendering = False
        self.expanded_sources = set()
        self.transcript_key = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 16)
        layout.setSpacing(16)
        title = QHBoxLayout()
        self.avatar = avatar = QLabel()
        self.avatar_pixmap = QPixmap(str(resource_root() / 'web/assets/generated' /
            (snapshot['preferences']['palette']+'-xiaotan-avatar.webp')))
        avatar.setPixmap(self.avatar_pixmap.scaled(42, 42,
             Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        title.addWidget(avatar)
        title.addWidget(heading('资料库问答'), 1)
        self.scope_button = button('查阅范围', self.show_scope)
        title.addWidget(self.scope_button)
        self.more = button('更多')
        self.menu = QMenu(self.more)
        self.menu.addAction('管理资料', lambda: host.navigate('shelf'))
        self.compare_action = self.menu.addAction('比较阅读')
        self.compare_action.triggered.connect(self.compare)
        self.save_note = self.menu.addAction('存为心得', lambda: self.answer_note())
        self.clear_action = self.menu.addAction('清空对话', self.clear_conversation)
        self.menu.addSeparator()
        self.menu.addAction('知识索引', lambda: host.navigate('index'))
        self.menu.addAction('对话用量', lambda: QMessageBox.information(self, '对话用量', self.context.text()))
        self.menu.addAction('查看对话回顾', self.show_handoff)
        self.more.setMenu(self.menu)
        title.addWidget(self.more)
        layout.addLayout(title)
        summary = QHBoxLayout()
        self.scope_count = QLabel()
        summary.addWidget(self.scope_count)
        self.scope_titles = ElidedLabel()
        self.scope_titles.setObjectName('muted')
        summary.addWidget(self.scope_titles, 1)
        self.articles = QListWidget(self)
        self.articles.hide()
        for article in snapshot['articles']:
            readable = bool(article.get('chars')) or article.get('source_status') == 'pending_source'
            detail = '已抓取的划词片段 · 未保存全文' if article.get('source_status') == 'pending_source' else f"可检索文字 · {article['chars']} 字" if readable else '暂无文字原文'
            item = QListWidgetItem(article['title'] + '\n' + detail, self.articles)
            item.setToolTip(article['title'] + '\n' + detail)
            item.setData(Qt.ItemDataRole.UserRole, article['id'])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if article['id'] in self.controller.body['article_ids'] else Qt.CheckState.Unchecked)
            if not readable:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                item.setCheckState(Qt.CheckState.Unchecked)
        self.notes = QCheckBox('包含我的笔记')
        self.notes.setChecked(self.controller.body['include_notes'])
        summary.addWidget(self.notes)
        layout.addLayout(summary)
        right = QWidget()
        self.conversation = right
        right.setObjectName('libraryConversation')
        colors = reading_preferences.colors(snapshot['preferences']['palette'], snapshot['preferences']['mode'])
        right.setStyleSheet('QWidget#libraryConversation {background:'+colors['surface']+
                           ';border:1px solid '+colors['line']+';border-radius:6px;}'
                           'QTextBrowser {background:transparent;border:0;padding:16px;}'
                           'QPlainTextEdit {background:'+colors['surface']+';}')
        conversation = QVBoxLayout(right)
        conversation.setContentsMargins(1, 1, 1, 1)
        conversation.setSpacing(0)
        tools = QHBoxLayout()
        self.context = QLabel(self)
        self.context.hide()
        self.restart = button('接续对话', self.controller.restart)
        tools.addStretch()
        self.handoff = button('对话回顾', self.show_handoff)
        tools.addWidget(self.handoff)
        tools.addWidget(self.restart)
        conversation.addLayout(tools)
        self.transcript = TranscriptBrowser()
        self.transcript.document().setDefaultStyleSheet('a {color:' + colors.get('accent-text', colors['accent']) + ';}')
        self.transcript.setOpenExternalLinks(False)
        self.transcript.setOpenLinks(False)
        self.transcript.anchorClicked.connect(self.message_action)
        self.welcome = QLabel('想了解哪一处？')
        self.welcome.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.welcome.setObjectName('muted')
        self.welcome.setStyleSheet('font-size:18px;')
        self.welcome.setMinimumHeight(80)
        self.welcome.setMaximumHeight(160)
        self.transcript.setMinimumHeight(80)
        conversation.addWidget(self.welcome, 3)
        conversation.addWidget(self.transcript, 3)
        self.evidence = QListWidget(self)
        self.evidence.hide()
        composer = QWidget()
        composer.setObjectName('libraryComposer')
        composer.setStyleSheet('QWidget#libraryComposer {border-top:1px solid '+colors['line']+';}')
        self.compose = compose = QVBoxLayout(composer)
        compose.setContentsMargins(16, 16, 16, 16)
        compose.setSpacing(12)
        from .widgets import QuestionInput
        self.input = QuestionInput()
        self.input.setPlaceholderText('输入你的问题…')
        self.input.setFixedHeight(84)
        compose.addWidget(self.input)
        privacy = QLabel('回答时，相关资料和对话会发送给你配置的模型服务。')
        privacy.setWordWrap(True)
        privacy.setObjectName('muted')
        privacy.setStyleSheet('font-size:12px;')
        compose.addWidget(privacy)
        actions = QHBoxLayout()
        self.status = QLabel()
        self.status.setWordWrap(True)
        actions.addWidget(self.status, 1)
        self.preview = button('查找依据', lambda: self.controller.request(self.input.toPlainText(), True))
        self.send = button('发送', lambda: self.controller.request(self.input.toPlainText()), primary=True)
        self.input.submitted.connect(self.send.click)
        self.cancel = button('停止', self.controller.stop)
        for control in (self.preview, self.send, self.cancel):
            actions.addWidget(control)
        compose.addLayout(actions)
        conversation.addWidget(composer)
        layout.addWidget(right, 1)
        self.notes.toggled.connect(self.choose)
        self.input.textChanged.connect(self.save_draft)
        self.evidence.itemDoubleClicked.connect(self.open_evidence)
        self.controller.changed.connect(self.render)
        self.controller.previewed.connect(self.show_results)
        available = [a['id'] for a in snapshot['articles']]
        scope = self.controller.body.get('scope', 'all')
        selected = available if scope == 'all' else [aid for aid in self.controller.body['article_ids'] if aid in available]
        if not self.controller.busy and (not self.controller.key or selected != self.controller.body['article_ids']):
            self.controller.select(selected, self.notes.isChecked(), 'question', scope)
        self.render()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        compact = self.height() < 570
        self.layout().setContentsMargins(*( (12, 10, 12, 8) if compact else (24, 24, 24, 16) ))
        self.layout().setSpacing(8 if compact else 16)
        self.compose.setContentsMargins(*( (10, 8, 10, 8) if compact else (16, 16, 16, 16) ))
        self.compose.setSpacing(6 if compact else 12)
        self.input.setFixedHeight(52 if compact else 84)
        size = 32 if compact else 42
        self.avatar.setPixmap(self.avatar_pixmap.scaled(size, size,
            Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def choose(self, *_):
        self.controller.select(self.controller.body['article_ids'], self.notes.isChecked(), 'question', self.controller.body.get('scope', 'all'))

    def compare(self):
        if not 2 <= len(self.controller.body['article_ids']) <= 12:
            self.host.notice('比较阅读请选择2至12篇文章。')
            return
        question = self.input.toPlainText().strip() or '这些文章有哪些共同点和差异？'
        self.controller.request(question, mode='compare')

    def show_scope(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('选择查阅资料')
        dialog.resize(560, 480)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        all_sources = QCheckBox('全部资料（自动包含以后导入和抓取的内容）')
        all_sources.setChecked(self.controller.body.get('scope') == 'all')
        layout.addWidget(all_sources)
        explanation = QLabel('默认查阅全部本地资料，也可以缩小范围。划词内容仅作为已抓取片段使用。')
        explanation.setWordWrap(True)
        explanation.setObjectName('muted')
        layout.addWidget(explanation)
        for i in range(self.articles.count()):
            item = self.articles.item(i)
            item.setCheckState(Qt.CheckState.Checked if item.data(Qt.ItemDataRole.UserRole) in self.controller.body['article_ids'] else Qt.CheckState.Unchecked)
        def select_theme(theme):
            ids = {m['article_id'] for m in self.snapshot['study']['memberships'] if m['theme_id'] == theme}
            for i in range(self.articles.count()):
                item = self.articles.item(i)
                selected = bool(item.flags() & Qt.ItemFlag.ItemIsEnabled) and (theme == 'all' or item.data(Qt.ItemDataRole.UserRole) in ids)
                item.setCheckState(Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked)
            all_sources.setChecked(theme == 'all')
        def narrow_scope(_):
            all_sources.setChecked(False)
        self.articles.itemChanged.connect(narrow_scope)
        themes = QWidget()
        themes.setObjectName('page')
        theme_layout = FlowLayout(themes)
        for title, key in [('全部资料', 'all')] + [(t['title'], t['id']) for t in self.snapshot['study']['themes']]:
            control = button(title if len(title) <= 24 else title[:23]+'…', lambda checked=False, value=key: select_theme(value), tip=title)
            control.setProperty('theme_id', key)
            control.setAccessibleName(title)
            theme_layout.addWidget(control)
        theme_scroll = QScrollArea()
        theme_scroll.setWidgetResizable(True)
        theme_scroll.setWidget(themes)
        theme_scroll.setMaximumHeight(max(44, min(120, theme_layout.heightForWidth(dialog.width()-48)+8)))
        theme_scroll.setMinimumHeight(44)
        theme_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(theme_scroll)
        layout.addWidget(self.articles)
        self.articles.show()
        controls = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        controls.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        apply = controls.addButton('使用所选资料', QDialogButtonBox.ButtonRole.AcceptRole)
        controls.rejected.connect(dialog.reject)
        def commit():
            if all_sources.isChecked():
                self.controller.select([a['id'] for a in self.snapshot['articles']], self.notes.isChecked(), 'question', 'all')
                dialog.accept()
                return
            ids = [self.articles.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.articles.count())
                   if self.articles.item(i).checkState() == Qt.CheckState.Checked and self.articles.item(i).flags() & Qt.ItemFlag.ItemIsEnabled]
            if not ids:
                self.host.notice('请至少选择一篇资料。')
                return
            self.controller.select(ids, self.notes.isChecked(), 'question')
            dialog.accept()
        apply.clicked.connect(commit)
        layout.addWidget(controls)
        dialog.exec()
        self.articles.itemChanged.disconnect(narrow_scope)
        self.articles.setParent(self)
        self.articles.hide()

    def clear_conversation(self):
        if QMessageBox.question(self, '清空当前对话', '只清空此资料范围的问答和草稿，原文与文章笔记保留。',
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.controller.clear()

    def save_draft(self):
        if not self.rendering:
            self.controller.draft(self.input.toPlainText())

    def render(self):
        self.rendering = True
        c = self.controller
        self.notes.blockSignals(True)
        self.notes.setChecked(c.body['include_notes'])
        self.notes.blockSignals(False)
        self.scope_count.setText(('全部资料 · ' if c.body.get('scope') == 'all' else '所选资料 · ') + f"{len(c.body['article_ids'])} 篇")
        self.scope_titles.setText('、'.join(a['title'] for a in self.snapshot['articles'] if a['id'] in c.body['article_ids']))
        self.welcome.setText('直接提问，小探会从本地资料中查找依据。' if c.body['article_ids'] else '导入文章或抓取阅读内容后，在这里直接提问。')
        self.welcome.setVisible(not c.session.get('messages'))
        self.transcript.setVisible(bool(c.session.get('messages')))
        self.scope_button.setEnabled(not c.busy)
        self.compare_action.setEnabled(bool(c.key) and not c.busy and not c.session.get('closed') and 2 <= len(c.body['article_ids']) <= 12)
        self.clear_action.setEnabled(not c.busy and bool(c.session.get('messages') or c.session.get('draft')))
        self.render_messages()
        draft = c.session.get('draft', '')
        if self.input.toPlainText() != draft:
            self.input.setPlainText(draft)
        self.evidence.clear()
        for hit in c.hits:
            item = QListWidgetItem(f"[{hit.get('citation', '')}] {hit.get('article_title', '')}\n{hit.get('quote', '')[:180]}", self.evidence)
            item.setData(Qt.ItemDataRole.UserRole, hit)
        self.evidence.hide()
        self.conversation.setMaximumHeight(16777215 if c.session.get('messages') else 368)
        policy = conversation_budget.policy(self.host.service.store)
        used = (c.session.get('context_usage') or {}).get('total_tokens')
        self.context.setText(f"对话预算 {policy['budget']:,}" + (f" · 已用 {used:,}" if isinstance(used, int) else ''))
        self.restart.setVisible(bool(c.session.get('closed')))
        count = len(handoff_records(c.session))
        self.handoff.setText(f'对话回顾 · {count}')
        self.handoff.setVisible(bool(count))
        self.restart.setEnabled(not c.busy)
        self.save_note.setEnabled(not c.busy and any(m.get('role') == 'assistant' for m in c.session.get('messages', [])))
        for control in (self.articles, self.notes):
            control.setEnabled(not c.busy)
        self.input.setEnabled(bool(c.key) and not c.busy)
        self.preview.setEnabled(bool(c.key) and not c.busy and not c.session.get('closed'))
        self.send.setEnabled(bool(c.key) and not c.busy and not c.session.get('closed'))
        self.cancel.setEnabled(bool(c.job))
        self.cancel.setVisible(bool(c.job))
        self.status.setText(c.message or ('正在打开资料库…' if not c.key else ''))
        self.rendering = False

    def show_handoff(self):
        HandoffDialog(self.host, self.controller.session, self).exec()

    def show_results(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('查找依据')
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.resize(480, 380)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        count = len(self.controller.hits)
        summary = QLabel(f'找到 {count} 处依据' if count else '未找到匹配内容，请调整问题或资料范围。')
        summary.setWordWrap(True)
        layout.addWidget(summary)
        results = QListWidget()
        results.setWordWrap(True)
        for hit in self.controller.hits:
            kind = '个人笔记' if hit.get('source') == 'personal_note' else '划词片段' if hit.get('source') == 'capture_context' else '文章原文'
            item = QListWidgetItem(kind+'\n'+hit.get('article_title', '')+'\n'+hit.get('quote', '')[:220], results)
            item.setData(Qt.ItemDataRole.UserRole, hit)
        def preview(item):
            hit = item.data(Qt.ItemDataRole.UserRole)
            dialog.accept()
            self.show_evidence(hit)
        results.itemClicked.connect(preview)
        layout.addWidget(results, 1)
        layout.addWidget(button('返回提问', dialog.reject), 0, Qt.AlignmentFlag.AlignLeft)
        dialog.open()

    def render_messages(self):
        if self.transcript_key != self.controller.key:
            self.expanded_sources.clear()
            self.transcript_key = self.controller.key
        self.transcript.update_content(self.controller.key,
            (self.controller.session.get('messages', []), self.expanded_sources), self.write_messages)

    def write_messages(self):
        self.transcript.clear()
        cursor = QTextCursor(self.transcript.document())
        for index, message in enumerate(self.controller.session.get('messages', [])):
            assistant = message.get('role') == 'assistant'
            cursor.insertHtml('<p><b>' + ('小探' if assistant else '我') + '</b></p>')
            cursor.insertBlock(QTextBlockFormat(), QTextCharFormat())
            content_start = cursor.position()
            cursor.insertMarkdown(message.get('content', ''), QTextDocument.MarkdownFeature.MarkdownNoHTML)
            content_end = cursor.position()
            evidence = message.get('evidence', [])
            if assistant and evidence:
                probe = QTextCursor(self.transcript.document())
                probe.setPosition(content_start)
                while True:
                    probe = self.transcript.document().find(QRegularExpression(r'\[S\d+\]'), probe)
                    if probe.isNull() or probe.selectionEnd() > content_end:
                        break
                    citation = probe.selectedText()[1:-1]
                    number = next((n for n, hit in enumerate(evidence) if hit.get('citation') == citation), None)
                    if number is not None:
                        fmt = probe.charFormat()
                        fmt.setAnchor(True)
                        fmt.setAnchorHref(f'library:source:{index}:{number}')
                        fmt.setFontUnderline(True)
                        colors = reading_preferences.colors(self.snapshot['preferences']['palette'], self.snapshot['preferences']['mode'])
                        fmt.setForeground(QColor(colors['accent-text']))
                        probe.setCharFormat(fmt)
            cursor.insertBlock(QTextBlockFormat(), QTextCharFormat())
            if evidence:
                cursor.insertHtml(f'<p><a href="library:sources:{index}">参考来源 · {len(evidence)}' +
                                  (' · 收起' if index in self.expanded_sources else ' · 展开') + '</a></p>')
                cursor.insertBlock(QTextBlockFormat(), QTextCharFormat())
                if index in self.expanded_sources:
                    for number, hit in enumerate(evidence):
                        label = escape(hit.get('article_title', '原文'))
                        if hit.get('source') == 'personal_note':
                            label += ' · 个人笔记'
                        elif hit.get('source') == 'capture_context':
                            label += ' · 划词片段'
                        cursor.insertHtml(f'<p><a href="library:source:{index}:{number}">{number+1}. {label}</a></p>')
                        cursor.insertBlock(QTextBlockFormat(), QTextCharFormat())
            if assistant and evidence:
                cursor.insertHtml(f'<p><a href="library:note:{index}">整理为笔记</a></p>')
                cursor.insertBlock(QTextBlockFormat(), QTextCharFormat())
            cursor.insertBlock(QTextBlockFormat(), QTextCharFormat())

    def message_action(self, url):
        parts = url.toString().split(':')
        if len(parts) < 3 or parts[0] != 'library':
            return
        try:
            index = int(parts[2])
            messages = self.controller.session.get('messages', [])
            if not 0 <= index < len(messages):
                return
            if parts[1] == 'sources':
                if index in self.expanded_sources:
                    self.expanded_sources.remove(index)
                else:
                    self.expanded_sources.add(index)
                scroll = self.transcript.verticalScrollBar().value()
                self.render_messages()
                self.transcript.verticalScrollBar().setValue(scroll)
            elif parts[1] == 'note':
                self.answer_note(index)
            elif parts[1] == 'source' and len(parts) == 4:
                number = int(parts[3])
                evidence = messages[index].get('evidence', [])
                if 0 <= number < len(evidence):
                    self.show_evidence(evidence[number])
        except (ValueError, IndexError):
            return

    def answer_note(self, index=None):
        c = self.controller
        messages = c.session.get('messages', [])
        if index is None:
            index = next((i for i in range(len(messages)-1, -1, -1) if messages[i].get('role') == 'assistant'), None)
        if c.busy or index is None or not 0 <= index < len(messages) or messages[index].get('role') != 'assistant':
            return
        content = messages[index]['content']
        articles = [(a['title'], a['id']) for a in self.host.snapshot['articles'] if a['id'] in c.body['article_ids']]
        fields = [('article_id', '关联文章', 'choice', '', articles),
                  ('title', '标题', 'text', '阅读整理', None), ('content', '心得', 'long', content, None)]
        dialog = EditForm('整理为心得', fields, lambda body: self.host.service.hub.save_answer_note(
            dict(body, session_key=c.key, message_index=index, source_content=content)), self)
        if dialog.exec():
            self.host.notice('心得已保存')

    def open_evidence(self, item):
        self.show_evidence(item.data(Qt.ItemDataRole.UserRole))

    def show_evidence(self, hit):
        dialog = QDialog(self)
        personal = hit.get('source') == 'personal_note'
        captured = hit.get('source') == 'capture_context'
        dialog.setWindowTitle('个人笔记依据' if personal else '划词上下文片段 · 未保存全文' if captured else '原文依据')
        dialog.resize(560, 400)
        layout = QVBoxLayout(dialog)
        title = QLabel((hit.get('note_title') if personal else None) or hit.get('article_title', ''))
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        layout.addWidget(title)
        excerpt = QTextBrowser()
        excerpt.setPlainText(hit.get('quote', ''))
        layout.addWidget(excerpt)
        controls = QHBoxLayout()
        controls.addStretch()
        controls.addWidget(button('返回提问', dialog.reject))
        def locate():
            try:
                article = self.host.service.store.article(hit['article_id'])
            except (ValueError, KeyError):
                self.host.notice('这篇资料已不存在。')
                return
            if personal:
                notes = self.host.service.hub.notes(hit['article_id'])['notes']
                if not any(n['id'] == hit.get('note_id') for n in notes):
                    self.host.notice('这条笔记已删除，以上保留的是回答时的引用。')
                    return
            elif article.get('version') != hit.get('version'):
                self.host.notice('原文版本已变化，请重新检索核对。')
                return
            dialog.accept()
            if personal:
                self.host.open_article(hit['article_id'], note_id=hit['note_id'])
            else:
                self.host.open_article(hit['article_id'], object_id=hit['object_id']) if hit.get('source') == 'capture_context' else self.host.open_article(hit['article_id'], block=hit.get('block'))
        controls.addWidget(button('查看笔记' if personal else '查看抓取片段' if captured else '在原文中定位', locate, primary=True))
        layout.addLayout(controls)
        dialog.exec()
