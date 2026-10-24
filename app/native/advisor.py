"""Planning dialogue with the existing validated plan writer."""
from datetime import date
from copy import deepcopy
from PySide6.QtCore import QObject, Signal, QTimer
from PySide6.QtCore import Qt, QDate, QTime
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QVBoxLayout, QHBoxLayout, QTextBrowser, QPlainTextEdit, QLabel, QListWidget, QListWidgetItem, QWidget, QToolButton, QCheckBox, QDateEdit, QTimeEdit, QSpinBox, QGridLayout, QScrollArea, QLayout)
from .surfaces import QDialog, QMessageBox
from .widgets import button
from ..paths import resource_root


class AdvisorController(QObject):
    changed = Signal()
    plans_changed = Signal()

    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.value = host.service.store.get('settings', 'native-advisor-draft', {})
        self.job = None
        self.submitted = None
        self.result = None
        self.message = ''
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.poll)

    def persist(self):
        self.host.service.store.put('settings', 'native-advisor-draft', self.value)

    def draft(self, text):
        self.value['request'] = text
        self.persist()

    def reset(self):
        if self.job:
            return
        self.value.update(request='', history=[])
        self.result = None
        self.message = ''
        self.persist()
        self.changed.emit()

    def submit(self):
        if self.job:
            return
        body = deepcopy(self.value)
        if not body.get('request', '').strip():
            return
        if len(body.get('history', [])) >= 16:
            self.message = '这次对话比较长，请重新开始，概括新的安排。'
            self.changed.emit()
            return
        try:
            result = self.host.service.workspace.start('study-plan', body)
        except ValueError as exc:
            self.message = str(exc)
        else:
            self.job = result['job']
            self.submitted = body
            self.message = '小探正在安排…'
            self.timer.start()
        self.changed.emit()

    def stop(self):
        with self.host.service.workspace.lock:
            job = self.host.service.workspace.jobs.get(self.job)
            if job:
                job['cancel'].set()
                self.message = '正在停止…'
        self.changed.emit()

    def poll(self):
        with self.host.service.workspace.lock:
            job = dict(self.host.service.workspace.jobs.get(self.job, {}))
        if job.get('status') == 'running':
            return
        self.timer.stop()
        self.job = None
        result = job.get('result')
        if job.get('status') == 'done' and result:
            if result.get('questions'):
                self.value['history'] = self.submitted.get('history', []) + [
                    {'role': 'user', 'content': self.submitted['request']},
                    {'role': 'assistant', 'content': '\n'.join(filter(None, [result.get('message'), *result['questions']]))}]
                self.value['request'] = ''
                self.message = '补充一句，继续安排'
                self.result = None
            else:
                self.value.update(request='', history=[])
                self.result = result
                self.message = f"已添加 {len(result.get('tasks', []))} 项安排"
                self.plans_changed.emit()
            self.persist()
        else:
            self.message = '已停止，需求已保留' if job.get('status') == 'cancelled' else job.get('message') or '暂时未能完成，请重试。'
        self.changed.emit()

    def undo(self):
        if not self.result or not self.result.get('batch_id') or self.job:
            return
        try:
            self.host.service.hub.undo_plan(self.result['batch_id'])
        except ValueError as exc:
            self.message = str(exc)
        else:
            self.result = None
            self.message = '本次添加已撤销'
            self.plans_changed.emit()
        self.changed.emit()


class AdvisorDialog(QDialog):
    def __init__(self, host):
        super().__init__(host)
        self.host, self.controller = host, host.advisor
        self.rendering = False
        self.setWindowTitle('和小探安排计划')
        self.resize(680, 600)
        self.setMinimumSize(520, 450)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        intro = QHBoxLayout()
        avatar = QLabel()
        palette = host.snapshot.get('preferences', {}).get('palette', 'clay')
        avatar.setPixmap(QPixmap(str(resource_root() / 'web/assets/generated' / (palette + '-xiaotan-avatar.webp'))).scaled(
            48, 48, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        avatar.setFixedSize(48, 48)
        intro.addWidget(avatar)
        title = QLabel('小探 · 日程规划')
        title.setObjectName('heading')
        intro.addWidget(title)
        intro.addStretch()
        layout.addLayout(intro)
        self.history = QTextBrowser()
        self.history.setOpenLinks(False)
        self.history.setStyleSheet('QTextBrowser {border:0; padding:6px;}')
        layout.addWidget(self.history, 1)
        self.request = QPlainTextEdit()
        self.request.setPlaceholderText('今天上午开会，下午想读完一篇论文。')
        self.request.setMaximumHeight(120)
        layout.addWidget(self.request)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.preferences = QToolButton()
        self.preferences.setText('偏好与相关资料   可选')
        self.preferences.setCheckable(True)
        self.preferences.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.preferences.setArrowType(Qt.ArrowType.RightArrow)
        self.preferences.setStyleSheet('QToolButton {border:0; padding:6px 0; text-align:left;}')
        layout.addWidget(self.preferences)
        self.preference_panel = QScrollArea()
        self.preference_panel.setWidgetResizable(True)
        self.preference_panel.setMaximumHeight(240)
        self.preference_panel.setStyleSheet('QScrollArea {border:0;}')
        self.preference_panel.setVisible(False)
        preference_body = QWidget()
        preference_body.setObjectName('page')
        preference_layout = QVBoxLayout(preference_body)
        preference_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        preference_layout.setContentsMargins(0, 0, 8, 0)
        self.limited = QCheckBox('限定每天的可用时间')
        limits = self.controller.value.get('availability') or {}
        self.limited.setChecked(bool(limits))
        preference_layout.addWidget(self.limited)
        self.availability_panel = QWidget()
        grid = QGridLayout(self.availability_panel)
        grid.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        grid.setContentsMargins(0, 0, 0, 0)
        self.limit_fields = {}
        for index, (key, label, default) in enumerate([
                ('start_date', '开始日期', date.today().isoformat()),
                ('end_date', '截止日期', date.today().isoformat()),
                ('start_time', '开始时间', '14:00'), ('end_time', '结束时间', '18:00'),
                ('daily_minutes', '每天最多分钟', 60)]):
            value = limits.get(key, default)
            if key.endswith('_date'):
                control = QDateEdit(QDate.fromString(value, 'yyyy-MM-dd'))
                control.setCalendarPopup(True)
                control.setDisplayFormat('yyyy-MM-dd')
                control.dateChanged.connect(self.save_preferences)
            elif key.endswith('_time'):
                control = QTimeEdit(QTime.fromString(value, 'HH:mm'))
                control.setDisplayFormat('HH:mm')
                control.timeChanged.connect(self.save_preferences)
            else:
                control = QSpinBox()
                control.setRange(5, 1440)
                control.setValue(value)
                control.valueChanged.connect(self.save_preferences)
            row, column = (0, index) if index < 2 else (2, index-2)
            control.setMinimumHeight(36)
            label_widget = QLabel(label)
            label_widget.setMinimumHeight(22)
            grid.addWidget(label_widget, row, column)
            grid.addWidget(control, row+1, column)
            self.limit_fields[key] = control
        preference_layout.addWidget(self.availability_panel)
        self.availability_panel.setVisible(bool(limits))
        preference_layout.addWidget(QLabel('相关资料'))
        self.related_articles = QListWidget()
        self.related_articles.setMaximumHeight(110)
        self.related_articles.setMinimumHeight(80)
        selected = self.controller.value.get('article_ids', [])
        for article in host.snapshot.get('articles', []):
            item = QListWidgetItem(article['title'], self.related_articles)
            item.setData(Qt.ItemDataRole.UserRole, article['id'])
            item.setCheckState(Qt.CheckState.Checked if article['id'] in selected else Qt.CheckState.Unchecked)
        preference_layout.addWidget(self.related_articles)
        self.preference_panel.setWidget(preference_body)
        layout.addWidget(self.preference_panel)
        self.preferences.toggled.connect(self.toggle_preferences)
        self.limited.toggled.connect(self.toggle_limits)
        self.related_articles.itemChanged.connect(self.save_preferences)
        options = QHBoxLayout()
        self.reset = button('重新开始', self.controller.reset)
        self.undo = button('撤销本次添加', self.controller.undo)
        options.addWidget(self.reset)
        options.addStretch()
        options.addWidget(self.undo)
        layout.addLayout(options)
        actions = QHBoxLayout()
        actions.addStretch()
        self.cancel = button('停止', self.controller.stop)
        self.send = button('交给小探', self.controller.submit, primary=True)
        actions.addWidget(self.cancel)
        actions.addWidget(button('关闭', self.close))
        actions.addWidget(self.send)
        layout.addLayout(actions)
        self.request.textChanged.connect(self.save_draft)
        self.controller.changed.connect(self.render)
        self.render()

    def save_draft(self):
        if not self.rendering:
            self.controller.draft(self.request.toPlainText())

    def render(self):
        self.rendering = True
        c = self.controller
        lines = [('我：' if t['role'] == 'user' else '小探：') + t['content'] for t in c.value.get('history', [])]
        if c.result:
            lines += [c.result.get('message', '')]
            lines += [f"{t['date']} {t['time']} · {t['minutes']} 分钟\n{t['title']}\n{t.get('description', '')}" for t in c.result.get('tasks', [])]
        self.history.setPlainText('\n\n'.join(lines) or '想完成什么？把今天的安排告诉我。')
        self.history.verticalScrollBar().setValue(self.history.verticalScrollBar().maximum())
        if self.request.toPlainText() != c.value.get('request', ''):
            self.request.setPlainText(c.value.get('request', ''))
        self.status.setText(c.message)
        for control in (self.request, self.send, self.preferences, self.reset):
            control.setEnabled(not c.job)
        self.preference_panel.setEnabled(not c.job)
        self.reset.setVisible(bool(c.value.get('history')))
        self.cancel.setEnabled(bool(c.job))
        self.undo.setVisible(bool(c.result and c.result.get('batch_id')))
        self.undo.setEnabled(not c.job)
        self.send.setText('继续规划' if c.value.get('history') else '交给小探')
        self.rendering = False

    def toggle_preferences(self, expanded):
        self.preference_panel.setVisible(expanded)
        self.preferences.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)

    def toggle_limits(self, enabled):
        self.availability_panel.setVisible(enabled)
        self.save_preferences()

    def save_preferences(self, *_):
        if self.rendering:
            return
        limits = None
        if self.limited.isChecked():
            limits = {key: (control.date().toString('yyyy-MM-dd') if key.endswith('_date')
                           else control.time().toString('HH:mm') if key.endswith('_time') else control.value())
                      for key, control in self.limit_fields.items()}
        articles = [self.related_articles.item(i).data(Qt.ItemDataRole.UserRole)
                    for i in range(self.related_articles.count())
                    if self.related_articles.item(i).checkState() == Qt.CheckState.Checked]
        self.controller.value.update(availability=limits, article_ids=articles)
        self.controller.persist()


class BatchDialog(QDialog):
    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.setWindowTitle('小探规划记录')
        self.resize(650, 500)
        layout = QVBoxLayout(self)
        self.rows = QListWidget()
        layout.addWidget(self.rows)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        bar = QHBoxLayout()
        self.undo = button('撤销本次添加', self.revert)
        self.delete = button('删除记录', self.remove)
        bar.addWidget(self.undo)
        bar.addWidget(self.delete)
        bar.addStretch()
        bar.addWidget(button('关闭', self.close))
        layout.addLayout(bar)
        self.rows.currentItemChanged.connect(self.selection)
        self.populate()

    def populate(self):
        self.rows.clear()
        for batch in self.host.service.hub.snapshot()['batches']:
            label = f"{batch['created_at'][:10]} · {len(batch['tasks'])} 项" + (' · 已撤销' if batch['undone'] else '')
            item = QListWidgetItem(label + '\n' + batch['request'][:180], self.rows)
            item.setData(Qt.ItemDataRole.UserRole, batch)
            item.setToolTip('\n'.join(t['title'] for t in batch['tasks']))
        self.selection()

    def selected(self):
        item = self.rows.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def selection(self, *_):
        batch = self.selected()
        self.undo.setEnabled(bool(batch and not batch['undone']))
        self.delete.setEnabled(bool(batch))

    def revert(self):
        batch = self.selected()
        if not batch:
            return
        try:
            self.host.service.hub.undo_plan(batch['id'])
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.status.setText('本次添加已撤销')
        self.populate()
        self.host.refresh()

    def remove(self):
        batch = self.selected()
        if batch and QMessageBox.question(self, '删除规划记录', '删除这条规划记录？已添加的任务会保留。',
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                          QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.host.service.hub.delete('batches', batch['id'])
            self.populate()
            self.host.refresh()
