"""Translation tools with independent drafts and jobs for each reading source."""
from pathlib import Path
from hashlib import sha256
from PySide6.QtCore import QObject, QTimer, Signal, Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPlainTextEdit, QLabel, QSplitter, QApplication
from .widgets import button, heading, PageComboBox, FlowLayout
from .surfaces import QFileDialog
from ..translation import LANGUAGES


def source_scope(text, origin):
    identity = origin.strip()
    if not identity:
        return 'scratch'
    if identity == '悬浮取词':
        identity += '\n' + text
    return 'source:' + sha256(identity.encode('utf-8')).hexdigest()


class TranslationController(QObject):
    changed = Signal()

    def __init__(self, host, scope='scratch'):
        super().__init__(host)
        self.host = host
        self.scope = scope
        self.storage_key = 'native-translation:' + scope
        self.state = dict(source='auto', target='zh', text='', result='', origin='', complete=False)
        self.state.update(host.service.store.get('settings', self.storage_key, {}))
        self.job = None
        self.message = ''
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.poll)
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(350)
        self.save_timer.timeout.connect(self.flush)

    def flush(self):
        self.save_timer.stop()
        store = self.host.service.store
        with store.lock:
            if self.scope.startswith('article:') and not store.get('articles', self.scope.removeprefix('article:')):
                return False
            store.put('settings', self.storage_key, self.state)
        return True

    def draft(self, **values):
        if self.job:
            return
        changed = any(self.state.get(key) != value for key, value in values.items())
        self.state.update(values)
        if changed:
            self.state.update(result='', complete=False)
            self.state.pop('checkpoint',None)
            self.message = ''
        self.save_timer.start()

    def prefill(self, text, origin=''):
        if self.job:
            self.host.notice('翻译仍在进行，原文已保留。请完成或停止后再换内容。')
            return False
        self.draft(text=text, origin=origin)
        self.flush()
        return True

    def receive(self, value):
        if self.job or self.state.get('text', '').strip() and self.state['text'] != value['text']:
            self.state['incoming'] = value
            self.flush()
            self.changed.emit()
        else:
            self.prefill(**value)
            self.changed.emit()

    def load_incoming(self):
        if self.job:
            return
        value = self.state.pop('incoming', None)
        if value:
            self.prefill(**value)
            self.changed.emit()

    def dismiss_incoming(self):
        self.state.pop('incoming', None)
        self.flush()
        self.changed.emit()

    def start(self, *, restart=False):
        if self.job:
            return
        if not self.flush():
            self.message = '原文章已删除，无法继续翻译。'
            self.changed.emit()
            return
        try:
            body={key:self.state[key] for key in ('source','target','text')}
            if not restart and not self.state.get('complete') and self.state.get('checkpoint'):
                body['resume']=self.state['checkpoint']
            result = self.host.service.workspace.start('translate', body)
        except ValueError as exc:
            self.message = str(exc)
        else:
            if not body.get('resume'):
                self.state['result']=''
                self.state.pop('checkpoint',None)
            self.state['complete']=False
            self.job = result['job']
            self.message = '正在准备翻译…'
            self.timer.start()
            self.flush()
        self.changed.emit()

    def poll(self):
        with self.host.service.workspace.lock:
            job = dict(self.host.service.workspace.jobs.get(self.job, {}))
        if job.get('partial') is not None:
            self.state['result'] = job['partial']
        checkpoint_changed = job.get('checkpoint') is not None and job['checkpoint'] != self.state.get('checkpoint')
        if checkpoint_changed:
            self.state['checkpoint']=job['checkpoint']
        self.message = job.get('message', self.message)
        if job.get('status') == 'running':
            if checkpoint_changed:
                self.flush()
            self.changed.emit()
            return
        self.timer.stop()
        self.job = None
        if job.get('status') == 'done':
            self.state.update(result=job['result']['text'], complete=job['result']['complete'])
            self.message = '翻译完成，原文与译文已保存在本机。'
        elif job.get('status') == 'cancelled':
            self.message = '已停止，原文和已完成的译文已保留；译文可能不完整。'
        else:
            self.message = job.get('message') or '翻译未完成，可以重试。'
            if self.state.get('result'):
                self.message += ' 已保留的译文尚不完整。'
        self.flush()
        self.changed.emit()

    def stop(self):
        with self.host.service.workspace.lock:
            job = self.host.service.workspace.jobs.get(self.job)
            if job:
                job['cancel'].set()
                self.message = '正在停止，已完成的译文会保留…'
        self.changed.emit()

    def shutdown(self):
        if self.job:
            self.stop()
            with self.host.service.workspace.lock:
                snapshot = dict(self.host.service.workspace.jobs.get(self.job, {}))
                partial = snapshot.get('partial')
            if partial is not None:
                self.state.update(result=partial, complete=False)
            if snapshot.get('checkpoint') is not None:
                self.state['checkpoint']=snapshot['checkpoint']
        self.timer.stop()
        self.flush()


class TranslationPage(QWidget):
    def __init__(self, host, controller=None, close=None):
        super().__init__(host)
        self.host, self.controller = host, controller or host.translation
        self.rendering = False
        self.article_bound = self.controller.scope.startswith('article:')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*( (12, 4, 12, 8) if self.article_bound else (24, 20, 24, 16) ))
        header = QHBoxLayout()
        header.addWidget(QLabel('原文与译文') if self.article_bound else heading('翻译', '保留原文，对照理解不同语言的内容'), 1)
        if close:
            header.addWidget(button('返回原文' if self.controller.scope.startswith('article:') else '收起翻译', close))
        outer.addLayout(header)
        languages = QHBoxLayout()
        languages.addWidget(QLabel('从'))
        self.source, self.target = PageComboBox(), PageComboBox()
        for key, name in LANGUAGES.items():
            self.source.addItem(name, key)
            if key != 'auto':
                self.target.addItem(name, key)
        self.source.setAccessibleName('源语言')
        self.target.setAccessibleName('目标语言')
        self.source.setCurrentIndex(max(0, self.source.findData(self.controller.state['source'])))
        self.target.setCurrentIndex(max(0, self.target.findData(self.controller.state['target'])))
        languages.addWidget(self.source, 1)
        self.swap = button('交换', self.swap_languages, tip='交换语言')
        self.swap.setAccessibleName('交换语言')
        languages.addWidget(self.swap)
        languages.addWidget(QLabel('译为'))
        languages.addWidget(self.target, 1)
        outer.addLayout(languages)
        self.origin = QLabel()
        self.origin.setWordWrap(True)
        self.origin.setObjectName('muted')
        self.origin.setTextFormat(Qt.TextFormat.PlainText)
        outer.addWidget(self.origin)
        self.incoming = QWidget()
        incoming_row = QHBoxLayout(self.incoming)
        incoming_row.setContentsMargins(0, 0, 0, 0)
        self.incoming_label = QLabel('')
        self.incoming_label.setTextFormat(Qt.TextFormat.PlainText)
        self.incoming_label.setWordWrap(True)
        incoming_row.addWidget(self.incoming_label, 1)
        self.load_incoming = button('载入新内容', self.controller.load_incoming)
        incoming_row.addWidget(self.load_incoming)
        incoming_row.addWidget(button('忽略', self.controller.dismiss_incoming))
        outer.addWidget(self.incoming)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.original, self.result = QPlainTextEdit(), QPlainTextEdit()
        self.original.setPlaceholderText('本文待翻译的内容' if self.article_bound else '输入或粘贴需要翻译的内容。')
        self.original.setAccessibleName('待翻译原文')
        self.result.setPlaceholderText('译文显示在这里')
        self.result.setAccessibleName('译文')
        self.result.setReadOnly(True)
        self.original.setPlainText(self.controller.state['text'])
        for title, editor in [('原文', self.original), ('译文', self.result)]:
            box = QWidget()
            column = QVBoxLayout(box)
            column.setContentsMargins(0, 6, 6, 6)
            column.addWidget(QLabel(title))
            column.addWidget(editor, 1)
            split.addWidget(box)
        split.setChildrenCollapsible(False)
        split.setSizes([500, 500])
        outer.addWidget(split, 1)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        actions = FlowLayout(spacing=10)
        self.start = button('开始翻译', self.controller.start, primary=True)
        self.restart = button('重新翻译',lambda:self.controller.start(restart=True))
        self.stop = button('停止', self.controller.stop)
        self.copy = button('复制译文', self.copy_result)
        self.export = button('保存译文', self.save_result)
        for control in (self.start, self.restart, self.stop, self.copy, self.export):
            actions.addWidget(control)
        outer.addLayout(actions)
        note = QLabel('译文自动保存在本文中，不替换原文。' if self.article_bound else '译文自动保存在本机，不替换原文。')
        note.setObjectName('muted')
        note.setWordWrap(True)
        outer.addWidget(note)
        self.original.textChanged.connect(self.draft)
        self.source.currentIndexChanged.connect(self.draft)
        self.target.currentIndexChanged.connect(self.draft)
        self.controller.changed.connect(self.render)
        self.render()

    def draft(self, *_):
        if not self.rendering:
            self.controller.draft(text=self.original.toPlainText(), source=self.source.currentData(), target=self.target.currentData())
            self.render()

    def swap_languages(self):
        source, target = self.source.currentData(), self.target.currentData()
        if source == 'auto' or self.controller.job:
            return
        self.source.blockSignals(True)
        self.target.blockSignals(True)
        self.source.setCurrentIndex(self.source.findData(target))
        self.target.setCurrentIndex(self.target.findData(source))
        self.source.blockSignals(False)
        self.target.blockSignals(False)
        self.draft()

    def render(self):
        self.rendering = True
        state, busy = self.controller.state, bool(self.controller.job)
        for combo, language in ((self.source, state['source']), (self.target, state['target'])):
            combo.setCurrentIndex(max(0, combo.findData(language)))
        for editor, text in ((self.original, state['text']), (self.result, state['result'])):
            if editor.toPlainText() != text:
                position = editor.verticalScrollBar().value()
                editor.setPlainText(text)
                editor.verticalScrollBar().setValue(position)
        self.original.setReadOnly(busy)
        self.source.setEnabled(not busy)
        self.target.setEnabled(not busy)
        self.swap.setEnabled(not busy and self.source.currentData() != 'auto')
        self.start.setEnabled(not busy)
        checkpoint=state.get('checkpoint')
        resumable=not state.get('complete') and isinstance(checkpoint,dict) and bool(checkpoint.get('parts'))
        self.start.setText('重新翻译' if state.get('complete') else '继续翻译' if resumable else '开始翻译')
        self.restart.setVisible(resumable and not busy)
        self.stop.setVisible(busy)
        self.copy.setEnabled(bool(state['result']))
        self.export.setEnabled(bool(state['result']))
        self.origin.setText('来源：' + state['origin'] if state.get('origin') else '')
        self.origin.setVisible(bool(state.get('origin')) and (not self.article_bound or ' · 选中内容' in state['origin']))
        incoming = state.get('incoming')
        self.incoming.setVisible(bool(incoming))
        self.load_incoming.setEnabled(not busy)
        self.incoming_label.setText('新内容：'+incoming['text'][:100]+'。当前内容已保留，载入后替换。' if incoming else '')
        self.status.setText(self.controller.message or ('已恢复上次译文' if state['result'] and state.get('complete') else '已恢复原文与部分译文，译文尚不完整' if state['result'] else '选择语言后开始翻译'))
        self.rendering = False

    def copy_result(self):
        QApplication.clipboard().setText(self.controller.state['result'])
        self.host.notice('译文已复制')

    def save_result(self):
        filename, _ = QFileDialog.getSaveFileName(self, '保存译文', '译文.md', 'Markdown (*.md);;文本 (*.txt)')
        if filename:
            state = self.controller.state
            text = ('# 译文' + ('（尚未完成）' if not state.get('complete') else '') + '\n\n'
                    + LANGUAGES[state['source']] + ' → ' + LANGUAGES[state['target']] + '\n\n' + state['result'])
            try:
                Path(filename).write_text(text, encoding='utf-8')
                self.host.notice('译文已保存')
            except OSError:
                self.host.notice('未能保存译文，请检查文件夹权限。')
