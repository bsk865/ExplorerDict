"""Date-grouped plan list matching the original management controls."""
from datetime import date, timedelta
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLineEdit,
                              QComboBox, QScrollArea, QCheckBox, QLabel, QSizePolicy)
from .widgets import PageComboBox as QComboBox
from .widgets import button
from .navigation import icon
from .progress import label
from ..reading_preferences import colors


class WeekPlans(QScrollArea):
    def __init__(self, host, tasks, start, preferences):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.days, self.entries, self.columns = [], [], 0
        palette = colors(preferences['palette'], preferences['mode'])
        self.verticalScrollBar().setStyleSheet(f'''
            QScrollBar:vertical {{background:{palette['bg']};width:12px;margin:0;}}
            QScrollBar::handle:vertical {{background:{palette['line']};min-height:24px;border-radius:5px;}}
            QScrollBar::handle:vertical:hover {{background:{palette['muted']};}}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{height:0;}}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{background:transparent;}}
        ''')
        body = QWidget()
        body.setObjectName('weekPlans')
        body.setStyleSheet(f'QWidget#weekPlans {{border-top:1px solid {palette["line"]};}}')
        self.grid = QGridLayout(body)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(0)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.setWidget(body)
        for i, weekday in enumerate(('周一', '周二', '周三', '周四', '周五', '周六', '周日')):
            day = start + timedelta(days=i)
            section = QWidget()
            section.setObjectName('weekDay')
            section.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            column = QVBoxLayout(section)
            column.setContentsMargins(10, 14, 10, 16)
            column.setSpacing(12)
            column.addWidget(label(day.strftime('%m-%d'), size=16))
            column.addWidget(label(weekday, size=12))
            scheduled = sorted((t for t in tasks if t.get('date') == day.isoformat()), key=lambda t: t.get('time') or '')
            for task in scheduled:
                entry = button('', lambda checked=False, t=task: host.edit_task(t))
                entry.setAccessibleName(task['title'])
                entry.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                text_color = palette['muted'] if task['status'] == 'done' else palette['ink']
                entry.setStyleSheet(f'QPushButton {{text-align:left;border-radius:6px;padding:0;}} QLabel {{color:{text_color};}}')
                copy = QVBoxLayout(entry)
                copy.setContentsMargins(8, 12, 8, 12)
                copy.setSpacing(8)
                copy.addWidget(label(f"{task.get('time') or '时间待定'} · {task['minutes']} 分钟", size=12))
                title = label(task['title'])
                font = title.font()
                font.setBold(True)
                title.setFont(font)
                copy.addWidget(title)
                copy.addWidget(label({'todo':'待完成', 'doing':'进行中', 'done':'已完成'}[task['status']], size=12))
                for text in entry.findChildren(QLabel):
                    text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                    text.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                from .study import task_menu
                entry.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                entry.customContextMenuRequested.connect(lambda point, t=task, w=entry: task_menu(host, t, w.mapToGlobal(point)))
                column.addWidget(entry)
                self.entries.append((task, entry))
            if not scheduled:
                column.addWidget(label('暂无安排', muted=True))
            column.addStretch()
            self.days.append(section)
        self.palette_colors = palette
        self.reflow()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.reflow()

    def reflow(self):
        if not hasattr(self, 'palette_colors'):
            return
        width = self.window().width()
        columns = 7 if width > 1100 else 3 if width > 650 else 1
        if columns == self.columns:
            return
        for i in range(7):
            self.grid.setColumnStretch(i, 0)
        for section in self.days:
            self.grid.removeWidget(section)
        for i, section in enumerate(self.days):
            edge = 'bottom' if columns == 1 else 'right'
            section.setStyleSheet(f'QWidget#weekDay {{border-{edge}:1px solid {self.palette_colors["line"]};}}')
            self.grid.addWidget(section, i // columns, i % columns)
        for i in range(columns):
            self.grid.setColumnStretch(i, 1)
        self.columns = columns


class PlanList(QWidget):
    def __init__(self, host, tasks, snapshot):
        super().__init__()
        self.host, self.rows, self.groups = host, [], []
        palette = colors(snapshot['preferences']['palette'], snapshot['preferences']['mode'])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索计划')
        self.search.setAccessibleName('搜索计划')
        self.status = QComboBox()
        self.status.setAccessibleName('计划状态')
        for title, key in [('全部状态', 'all'), ('未完成', 'pending'), ('已完成', 'done')]:
            self.status.addItem(title, key)
        saved = getattr(host, 'plan_filters', {})
        if not isinstance(saved, dict):
            saved = {}
        self.search.setText(saved.get('query', ''))
        self.status.setCurrentIndex(max(0, self.status.findData(saved.get('status', 'all'))))
        filters.addWidget(self.search, 1)
        filters.addWidget(self.status)
        layout.addLayout(filters)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName('page')
        column = QVBoxLayout(body)
        column.setContentsMargins(0, 10, 10, 0)
        self.scroll.setWidget(body)
        layout.addWidget(self.scroll, 1)
        articles = {a['id']: a for a in snapshot['articles']}
        last = None
        for task in tasks:
            day = task.get('date') or '未安排日期'
            if day != last:
                heading = label(day + (' · 今天' if day == date.today().isoformat() else ''), size=18)
                heading.setContentsMargins(0, 16, 0, 6)
                column.addWidget(heading)
                self.groups.append((day, heading))
                last = day
            entry = QWidget()
            entry.setObjectName('planRow')
            entry.setStyleSheet(f'QWidget#planRow {{border-bottom:1px solid {palette["line"]};}}')
            grid = QGridLayout(entry)
            grid.setContentsMargins(0, 16, 0, 16)
            grid.setHorizontalSpacing(14)
            check = QCheckBox()
            check.setAccessibleName('完成 '+task['title'])
            check.setChecked(task['status'] == 'done')
            grid.addWidget(check, 0, 0)
            stamp = label(task.get('time') or '时间待定', muted=True)
            stamp.setFixedWidth(76)
            grid.addWidget(stamp, 0, 1)
            edit = button('', lambda checked=False, t=task: host.edit_task(t))
            edit.setAccessibleName(task['title'])
            edit.setStyleSheet('QPushButton {border:0;padding:0;text-align:left;}')
            edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            copy = QVBoxLayout(edit)
            copy.setContentsMargins(0, 0, 0, 0)
            title = label(task['title'])
            font = title.font()
            font.setStrikeOut(task['status'] == 'done')
            title.setFont(font)
            copy.addWidget(title)
            copy.addWidget(label((task.get('goal', '')+' · ' if task.get('goal') else '')+str(task['minutes'])+' 分钟', muted=True))
            for text in edit.findChildren(QLabel):
                text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                text.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            grid.addWidget(edit, 0, 2)
            grid.setColumnStretch(2, 1)
            tools = QGridLayout()
            tools.setSpacing(4)
            actions = [(aid, articles[aid]) for aid in task.get('article_ids', []) if aid in articles]
            controls = []
            for aid, article in actions:
                controls.append(button('', lambda checked=False, key=aid: host.open_article(key),
                                       icon=icon('book', palette['muted']), tip='阅读 '+article['title']))
            controls += [button('', lambda checked=False, t=task: host.edit_task(t), icon=icon('note', palette['muted']), tip='编辑计划'),
                         button('', lambda checked=False, t=task: host.delete('tasks', t['id'], t['title']), icon=icon('trash', palette['muted']), tip='删除计划')]
            for i, control in enumerate(controls):
                control.setAccessibleName(control.toolTip())
                control.setFixedSize(32, 32)
                tools.addWidget(control, i // 3, i % 3)
            grid.addLayout(tools, 0, 3)
            def complete(value, t=task, checkbox=check):
                checkbox.setEnabled(False)
                def failed(message):
                    from shiboken6 import isValid
                    if isValid(checkbox):
                        checkbox.blockSignals(True)
                        checkbox.setChecked(t['status'] == 'done')
                        checkbox.blockSignals(False)
                        checkbox.setEnabled(True)
                    host.notice(message)
                host.tasks.submit(lambda: host.service.hub.save('tasks', {'id': t['id'], 'status': 'done' if value else 'todo'}),
                                  lambda _: host.refresh(), failed)
            check.toggled.connect(complete)
            entry.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            from .study import task_menu
            entry.customContextMenuRequested.connect(lambda point, t=task, w=entry: task_menu(host, t, w.mapToGlobal(point)))
            column.addWidget(entry)
            self.rows.append((task, day, entry))
        self.empty = label('还没有计划。', muted=True)
        column.addWidget(self.empty)
        column.addStretch()
        self.search.textChanged.connect(self.filter)
        self.status.currentIndexChanged.connect(self.filter)
        self.filter()

    def filter(self, *_):
        query, status = self.search.text().casefold(), self.status.currentData()
        self.host.plan_filters = {'query': self.search.text(), 'status': status}
        days = set()
        for task, day, row in self.rows:
            done = task['status'] == 'done'
            match = query in ' '.join(str(task.get(k, '')) for k in ('title', 'goal', 'date', 'time', 'minutes')).casefold()
            visible = match and (status == 'all' or (done if status == 'done' else not done))
            row.setVisible(visible)
            if visible:
                days.add(day)
        for day, heading in self.groups:
            heading.setVisible(day in days)
        self.empty.setText('没有匹配的计划。' if self.rows else '还没有计划。')
        self.empty.setVisible(not days)
