"""Native model, context and storage settings using shared persistence."""
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (QVBoxLayout, QHBoxLayout, QWidget, QFormLayout, QLineEdit, QLabel, QSlider, QScrollArea)
from .surfaces import QDialog, QFileDialog
from .widgets import button, PageSlider, PageComboBox
from .. import conversation_budget, storage_location, reading_preferences


def section_heading(text):
    label = QLabel(text)
    label.setStyleSheet('font-size:16px;font-weight:600;')
    return label


class SettingsDialog(QDialog):
    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.job = None
        self.setWindowTitle('设置')
        self.resize(620, 700)
        layout = QVBoxLayout(self)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        appearance = reading_preferences.load(host.service.store.directory)
        palette = reading_preferences.colors(appearance['palette'], appearance['mode'])
        self.scroll.verticalScrollBar().setStyleSheet(f'''
            QScrollBar:vertical {{background:{palette['bg']};width:12px;margin:0;}}
            QScrollBar::handle:vertical {{background:{palette['line']};min-height:24px;border-radius:5px;}}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{height:0;}}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{background:transparent;}}
        ''')
        body = QWidget()
        body.setObjectName('page')
        sections = QVBoxLayout(body)
        sections.setContentsMargins(8, 8, 16, 8)
        sections.setSpacing(22)
        self.scroll.setWidget(body)
        layout.addWidget(self.scroll)
        model = QWidget()
        form = QFormLayout(model)
        form.setContentsMargins(0, 0, 0, 0)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        form.setVerticalSpacing(16)
        form.addRow(section_heading('模型设置'))
        config = host.service.workspace.config()
        self.base = QLineEdit(config['base_url'])
        self.model = PageComboBox()
        self.model.setEditable(True)
        self.model.addItems(['deepseek-flash', 'deepseek-v4-pro'])
        self.model.setCurrentText(config['model'])
        self.effort = PageComboBox()
        self.update_efforts(config.get('reasoning_effort', ''))
        self.model.currentTextChanged.connect(lambda _: self.update_efforts(self.effort.currentData()))
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText('已保存，留空保持不变' if config['has_key'] else '填写服务商提供的密钥')
        form.addRow('接口地址', self.base)
        form.addRow('模型', self.model)
        form.addRow('推理强度', self.effort)
        form.addRow('API 密钥', self.key)
        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.save = button('保存配置', self.save_model, primary=True)
        self.detect = button('检测图片理解', self.detect_model)
        actions.addWidget(self.save)
        actions.addWidget(self.detect)
        actions.addStretch()
        form.addRow(actions)
        self.vision = QLabel()
        self.vision.setWordWrap(True)
        form.addRow(self.vision)
        hint = QLabel('图片检测会发送一张测试图，并产生少量用量。')
        hint.setWordWrap(True)
        form.addRow(hint)
        sections.addWidget(model)
        storage_entry = QWidget()
        storage_entry.setObjectName('settingsStorage')
        storage_entry.setStyleSheet(f'QWidget#settingsStorage {{border-top:1px solid {palette["line"]};}}')
        storage_actions = QVBoxLayout(storage_entry)
        storage_actions.setContentsMargins(0, 18, 0, 0)
        storage_actions.addWidget(section_heading('资料与缓存'))
        note = QLabel('文章、词条、对话与缓存保存在本机，主界面与浮窗共用。')
        note.setWordWrap(True)
        storage_actions.addWidget(note)
        row = QHBoxLayout()
        row.addWidget(button('更改存储位置', self.open_storage))
        self.storage_details = button('查看本地保存', self.show_storage_details)
        row.addWidget(self.storage_details)
        row.addStretch()
        storage_actions.addLayout(row)
        self.storage_card = QWidget()
        self.storage_card.setObjectName('infoCard')
        self.storage_card.setVisible(False)
        self.storage_card_layout = QVBoxLayout(self.storage_card)
        self.storage_card_layout.setContentsMargins(16, 14, 16, 14)
        self.storage_card_layout.setSpacing(10)
        storage_actions.addWidget(self.storage_card)
        sections.addWidget(storage_entry)
        context = QWidget()
        context.setObjectName('settingsContext')
        context.setStyleSheet(f'QWidget#settingsContext {{border-top:1px solid {palette["line"]};}}')
        policy = QFormLayout(context)
        policy.setContentsMargins(0, 18, 0, 0)
        policy.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        policy.setVerticalSpacing(16)
        policy.addRow(section_heading('问答上下文预算'))
        note = QLabel('用于文章问答和资料库问答。达到预算后保存对话回顾，可接续新对话。')
        note.setWordWrap(True)
        policy.addRow(note)
        self.budget = PageSlider(Qt.Orientation.Horizontal)
        self.budget.setStyleSheet(f'''
            QSlider::groove:horizontal {{height:5px;background:{palette['line']};border-radius:2px;}}
            QSlider::sub-page:horizontal {{background:{palette['accent']};border-radius:2px;}}
            QSlider::handle:horizontal {{background:{palette['accent']};width:14px;margin:-5px 0;border-radius:7px;}}
        ''')
        self.budget.setAccessibleName('问答上下文预算')
        self.budget_value = QLabel()
        self.budget.valueChanged.connect(lambda value: self.budget_value.setText(f'{value:,} tokens'))
        policy.addRow('本段预算', self.budget_value)
        policy.addRow(self.budget)
        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.default_budget = button('恢复默认值', lambda: self.budget.setValue(self.budget.maximum()))
        actions.addWidget(button('保存预设', self.save_context, primary=True))
        actions.addWidget(self.default_budget)
        actions.addStretch()
        policy.addRow(actions)
        sections.addWidget(context)
        self.storage_dialog = QWidget()
        self.storage_dialog.setObjectName('infoCard')
        self.storage_dialog.setVisible(False)
        storage_actions.addWidget(self.storage_dialog)
        storage = self.storage_dialog
        area = QVBoxLayout(storage)
        area.setContentsMargins(20, 22, 20, 20)
        self.location = QLabel(str(host.service.store.directory.resolve()))
        self.location.setWordWrap(True)
        area.addWidget(QLabel('当前资料位置'))
        area.addWidget(self.location)
        self.pending = QLabel()
        self.pending.setWordWrap(True)
        area.addWidget(self.pending)
        self.target = QLineEdit()
        self.target.setPlaceholderText('选择一个空文件夹')
        row = QHBoxLayout()
        row.addWidget(self.target, 1)
        row.addWidget(button('选择文件夹', self.browse))
        area.addLayout(row)
        note = QLabel('位置变更会在应用完全退出后生效，原目录会保留。')
        note.setWordWrap(True)
        area.addWidget(note)
        row = QHBoxLayout()
        row.addWidget(button('保存位置', self.save_location, primary=True))
        row.addWidget(button('取消变更', self.cancel_location))
        area.addLayout(row)
        area.addStretch()
        self.location_status = QLabel()
        self.location_status.setWordWrap(True)
        area.addWidget(self.location_status)
        area.addWidget(button('收起', self.storage_dialog.hide), alignment=Qt.AlignmentFlag.AlignRight)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        footer = QHBoxLayout()
        footer.addStretch()
        footer.addWidget(button('关闭', self.close))
        layout.addLayout(footer)
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.poll)
        self.show_storage()
        self.show_vision()
        self.load_budget(config)

    def update_efforts(self, selected):
        from ..config import effort_choices
        self.effort.clear()
        choices = list(effort_choices(self.model.currentText()))
        if selected and selected not in choices:
            choices.append(selected)
        for value in choices:
            self.effort.addItem(value or '模型默认', value)
        self.effort.setCurrentIndex(max(0, self.effort.findData(selected)))

    def load_budget(self, config):
        p = config['conversation_policy']
        self.budget.setRange(min(8192, p['ceiling']), p['ceiling'])
        self.budget.setValue(p['budget'])
        self.budget_value.setText(f"{p['budget']:,} tokens")

    def open_storage(self):
        self.show_storage()
        self.location_status.clear()
        self.storage_dialog.setVisible(not self.storage_dialog.isVisible())
        if self.storage_dialog.isVisible():
            QTimer.singleShot(0, lambda: self.scroll.ensureWidgetVisible(self.target))

    def save_model(self):
        try:
            self.host.service.store.save_config({'base_url': self.base.text(), 'model': self.model.currentText(), 'key': self.key.text(),
                                                 'reasoning_effort': self.effort.currentData()})
        except ValueError as exc:
            self.status.setText(str(exc))
            return False
        self.key.clear()
        self.key.setPlaceholderText('已保存，留空保持不变')
        self.status.setText('模型设置已保存')
        value = self.host.service.workspace.config()
        self.load_budget(value)
        self.show_vision()
        return True

    def detect_model(self):
        if self.job or not self.save_model():
            return
        try:
            result = self.host.service.workspace.start('detect', {})
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.job = result['job']
        for widget in (self.base, self.model, self.effort, self.key, self.save, self.detect):
            widget.setEnabled(False)
        self.status.setText('正在检测图片理解…')
        self.timer.start()

    def poll(self):
        with self.host.service.workspace.lock:
            job = dict(self.host.service.workspace.jobs.get(self.job, {}))
        if job.get('status') == 'running':
            return
        self.timer.stop()
        self.job = None
        for widget in (self.base, self.model, self.effort, self.key, self.save, self.detect):
            widget.setEnabled(True)
        self.status.setText('检测完成' if job.get('status') == 'done' else job.get('message', '检测暂未完成'))
        self.show_vision()

    def show_vision(self):
        status = self.host.service.workspace.config()['vision'].get('status')
        self.vision.setText({'supported': '当前模型已通过图片理解检测', 'unsupported': '当前模型不支持图片理解',
                             'unknown': '暂时无法确认图片理解能力'}.get(status, '尚未检测图片理解'))

    def save_context(self):
        try:
            conversation_budget.save_policy(self.host.service.store, self.budget.value())
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.load_budget(self.host.service.workspace.config())
        self.status.setText('预算预设已保存')

    def browse(self):
        directory = QFileDialog.getExistingDirectory(self, '选择空文件夹')
        if directory:
            self.target.setText(directory)

    def show_storage_details(self):
        if self.storage_card.isVisible():
            self.storage_card.hide()
            self.storage_details.setText('查看本地保存')
            return
        self.storage_details.setEnabled(False)
        def loaded(info):
            from shiboken6 import isValid
            if not isValid(self):
                return
            self.storage_details.setEnabled(True)
            if not self.isVisible():
                return
            layout = self.storage_card_layout
            while layout.count():
                item = layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            layout.addWidget(section_heading('本地保存'))
            for value in (info['directory'], info['database']):
                text = QLabel(value)
                text.setTextFormat(Qt.TextFormat.PlainText)
                text.setWordWrap(True)
                layout.addWidget(text)
            for row in info['items']:
                layout.addWidget(QLabel(f"{row['label']}：{row['count']} 条，{row['bytes']/1024:.1f} KB 内容"))
            self.storage_card.show()
            self.storage_details.setText('收起保存信息')
            QTimer.singleShot(0, lambda: self.scroll.ensureWidgetVisible(self.storage_card))
        def failed(_):
            from shiboken6 import isValid
            if isValid(self):
                self.storage_details.setEnabled(True)
                self.status.setText('暂时无法读取本地保存信息，请稍后重试。')
        self.host.tasks.submit(self.host.service.storage_info, loaded, failed)

    def show_storage(self):
        config = storage_location.read()
        if config.get('error'):
            self.pending.setText('上次迁移未完成，仍使用当前位置。请重新选择空文件夹。')
        else:
            self.pending.setText(('下次启动使用：'+config['pending']) if config.get('pending') else '继续使用当前位置')
        self.target.setText(config.get('pending', ''))

    def save_location(self):
        try:
            with self.host.service.workspace.lock:
                if any(j['status'] == 'running' and not j.get('background') for j in self.host.service.workspace.jobs.values()):
                    raise ValueError('请等待当前任务完成后再修改存储位置。')
                storage_location.schedule(self.host.service.store.directory, self.target.text())
            self.show_storage()
            self.status.setText('位置已保存，完全退出后生效')
        except (ValueError, OSError) as exc:
            self.status.setText(str(exc) if isinstance(exc, ValueError) else '无法保存位置，请检查文件夹权限。')
        self.location_status.setText(self.status.text())

    def cancel_location(self):
        try:
            storage_location.schedule(self.host.service.store.directory, str(self.host.service.store.directory.resolve()))
        except (ValueError, OSError):
            self.status.setText('未能取消位置变更，请稍后重试。')
            self.location_status.setText(self.status.text())
            return
        self.show_storage()
        self.status.setText('已取消位置变更')
        self.location_status.setText(self.status.text())
