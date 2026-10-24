"""Compact, labelled forms for existing content types."""
from datetime import date, timedelta
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QVBoxLayout, QFormLayout, QLineEdit, QPlainTextEdit, QListWidget, QListWidgetItem, QDialogButtonBox, QLabel, QHBoxLayout)
from .surfaces import QDialog
from .widgets import PageComboBox as QComboBox, PageSpinBox as QSpinBox
from .widgets import button
from .navigation import icon


class EditForm(QDialog):
    def __init__(self, title, fields, save, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(450)
        self.setMaximumWidth(650)
        self.fields = {}
        self.save = save
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 20)
        form = QFormLayout()
        self.form = form
        form.setVerticalSpacing(14)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        for key, label, kind, value, options in fields:
            if kind == 'long':
                control = QPlainTextEdit(str(value or ''))
                control.setMinimumHeight(110)
            elif kind == 'number':
                control = QSpinBox()
                control.setRange(*options)
                control.setValue(value)
            elif kind == 'choice':
                control = QComboBox()
                for name, data in options:
                    control.addItem(name, data)
                control.setCurrentIndex(max(0, control.findData(value)))
            elif kind == 'multiple':
                control = QListWidget()
                control.setMaximumHeight(170)
                for name, data in options:
                    item = QListWidgetItem(name, control)
                    item.setData(Qt.ItemDataRole.UserRole, data)
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(Qt.CheckState.Checked if data in (value or []) else Qt.CheckState.Unchecked)
            else:
                control = QLineEdit(str(value or ''))
                if kind == 'secret':
                    control.setEchoMode(QLineEdit.EchoMode.Password)
                if options:
                    control.setPlaceholderText(str(options))
            control.setAccessibleName(label)
            self.fields[key] = (kind, control)
            form.addRow(label, control)
        outer.addLayout(form)
        self.error = QLabel()
        self.error.setWordWrap(True)
        outer.addWidget(self.error)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText('保存')
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        self.buttons.accepted.connect(self.submit)
        self.buttons.rejected.connect(self.reject)
        outer.addWidget(self.buttons)

    def values(self):
        result = {}
        for key, (kind, control) in self.fields.items():
            if kind == 'long':
                result[key] = control.toPlainText()
            elif kind == 'number':
                result[key] = control.value()
            elif kind == 'choice':
                result[key] = control.currentData()
            elif kind == 'multiple':
                result[key] = [control.item(i).data(Qt.ItemDataRole.UserRole) for i in range(control.count())
                               if control.item(i).checkState() == Qt.CheckState.Checked]
            else:
                result[key] = control.text()
        return result

    def submit(self):
        try:
            self.save(self.values())
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        self.accept()


class NoteForm(EditForm):
    def __init__(self, note, articles, save, parent=None):
        choices = [(a['title'], a['id']) for a in articles]
        if note.get('article_id') and not any(key == note['article_id'] for _, key in choices):
            choices.append((note.get('article_title') or '原文', note['article_id']))
        fields = [('article_id', '所属文章', 'choice', note.get('article_id'), choices),
                  ('title', '标题', 'text', note.get('title'), None),
                  ('content', '正文', 'long', note.get('content'), None)]
        super().__init__('编辑笔记' if note.get('id') else '新建笔记', fields, save, parent)
        self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self.fields['article_id'][1].setEnabled(not bool(note.get('id')))
        self.fields['title'][1].setMaxLength(200)
        self.fields['content'][1].setMinimumHeight(240)
        if note.get('quote'):
            quote = QPlainTextEdit(note['quote'])
            quote.setReadOnly(True)
            quote.setAccessibleName('引用原文')
            quote.setMaximumHeight(100)
            self.layout().insertWidget(1, quote)


class TaskForm(EditForm):
    def __init__(self, task, articles, save, remove, split, parent=None):
        super().__init__('编辑计划' if task.get('id') else '添加计划',
                         task_fields(task, articles), save, parent)
        if not task.get('id'):
            return
        actions = QHBoxLayout()
        actions.setSpacing(12)
        def delete():
            if remove():
                self.reject()
        actions.addWidget(button('', delete, icon=icon('trash', self.palette().windowText().color().name()), tip='删除计划'))
        actions.addStretch()
        actions.addWidget(button('延后一天', self.delay))
        if task.get('status') != 'done' and task.get('minutes', 30) >= 10:
            def split_existing():
                self.reject()
                split()
            actions.addWidget(button('拆分', split_existing))
        self.layout().insertLayout(self.layout().count() - 1, actions)

    def delay(self):
        control = self.fields['date'][1]
        try:
            current = date.fromisoformat(control.text().strip() or date.today().isoformat())
            next_date = current + timedelta(days=1)
        except (ValueError, OverflowError):
            self.error.setText('请填写有效日期。')
            return
        control.setText(next_date.isoformat())
        self.error.clear()


def task_fields(task, articles):
    return [('title', '任务', 'text', task.get('title'), None),
            ('goal', '目标', 'text', task.get('goal'), None),
            ('date', '日期', 'text', task.get('date'), 'YYYY-MM-DD，可留空'),
            ('time', '时间', 'text', task.get('time'), 'HH:MM，可留空'),
            ('minutes', '预计分钟', 'number', task.get('minutes', 30), (5, 1440)),
            ('status', '状态', 'choice', task.get('status', 'todo'),
             [('待办', 'todo'), ('进行中', 'doing'), ('已完成', 'done')]),
            ('description', '说明', 'long', task.get('description'), None),
            ('article_ids', '关联文章', 'multiple', task.get('article_ids', []),
             [(a['title'], a['id']) for a in articles])]
