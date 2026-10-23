"""Today, plans, notes and observed study statistics."""
from datetime import date, timedelta, datetime
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon, QTransform
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
                              QTableWidgetItem, QHeaderView, QAbstractItemView, QCheckBox,
                              QProgressBar, QListWidget, QListWidgetItem, QMenu, QScrollArea,
                              QButtonGroup)
from .widgets import button, heading


def progress(tasks):
    done = sum(t['status'] == 'done' for t in tasks)
    return done, len(tasks)


def progress_row(title, tasks):
    row = QWidget()
    layout = QVBoxLayout(row)
    layout.setContentsMargins(0, 8, 0, 8)
    done, total = progress(tasks)
    line = QHBoxLayout()
    line.addWidget(QLabel(title))
    line.addStretch()
    line.addWidget(QLabel(f'{done} / {total}'))
    layout.addLayout(line)
    bar = QProgressBar()
    bar.setRange(0, max(total, 1))
    bar.setValue(done)
    bar.setTextVisible(False)
    layout.addWidget(bar)
    return row


def task_table(host, tasks):
    table = QTableWidget(len(tasks), 5)
    table.setHorizontalHeaderLabels(['完成', '时间', '任务', '预计', '操作'])
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.verticalHeader().hide()
    table.setShowGrid(False)
    table.setAlternatingRowColors(True)
    table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
    table.setColumnWidth(0, 58)
    table.setColumnWidth(1, 150)
    table.setColumnWidth(3, 80)
    table.setColumnWidth(4, 150)
    table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    def context(point):
        row = table.rowAt(point.y())
        if 0 <= row < len(tasks):
            task_menu(host, tasks[row], table.viewport().mapToGlobal(point))
    table.customContextMenuRequested.connect(context)
    for row, task in enumerate(tasks):
        check = QCheckBox()
        check.setAccessibleName('完成 ' + task['title'])
        check.setChecked(task['status'] == 'done')
        check.toggled.connect(lambda value, key=task['id']: host.run(
            lambda: host.service.hub.save('tasks', {'id': key, 'status': 'done' if value else 'todo'})))
        holder = QWidget()
        hl = QHBoxLayout(holder)
        hl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hl.addWidget(check)
        table.setCellWidget(row, 0, holder)
        table.setItem(row, 1, QTableWidgetItem((task.get('date') or '日期待定') + '\n' + (task.get('time') or '')))
        table.setItem(row, 2, QTableWidgetItem(task['title'] + ('\n' + task['goal'] if task.get('goal') else '')))
        table.setItem(row, 3, QTableWidgetItem(str(task['minutes']) + ' 分钟'))
        actions = QWidget()
        bar = QHBoxLayout(actions)
        bar.setContentsMargins(3, 3, 3, 3)
        bar.addWidget(button('编辑', lambda checked=False, t=task: host.edit_task(t)))
        bar.addWidget(button('删除', lambda checked=False, t=task: host.delete('tasks', t['id'], t['title'])))
        table.setCellWidget(row, 4, actions)
        table.setRowHeight(row, 62)
    return table


def task_menu(host, task, point):
    menu = QMenu(host)
    menu.addAction('编辑计划', lambda: host.edit_task(task))
    menu.addAction('延后一天', lambda: host.delay_task(task))
    if task['status'] != 'done' and task['minutes'] >= 10:
        menu.addAction('拆分计划', lambda: host.split_task(task))
    menu.addSeparator()
    for status, label in [('todo', '待办'), ('doing', '进行中'), ('done', '已完成')]:
        action = menu.addAction(label, lambda checked=False, s=status: host.run(
            lambda: host.service.hub.save('tasks', {'id': task['id'], 'status': s})))
        action.setCheckable(True)
        action.setChecked(task['status'] == status)
    menu.addSeparator()
    menu.addAction('删除计划', lambda: host.delete('tasks', task['id'], task['title']))
    menu.exec(point)


def week_table(host, tasks, start):
    table = QTableWidget(1, 7)
    table.setHorizontalHeaderLabels([(start+timedelta(days=i)).strftime('%m-%d') + '\n' + day
                                    for i, day in enumerate(['周一', '周二', '周三', '周四', '周五', '周六', '周日'])])
    table.verticalHeader().hide()
    table.horizontalHeader().setMinimumSectionSize(105)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    for column in range(7):
        day = (start + timedelta(days=column)).isoformat()
        rows = QListWidget()
        rows.setWordWrap(True)
        for task in tasks:
            if task.get('date') != day:
                continue
            status = {'todo': '待办', 'doing': '进行中', 'done': '已完成'}[task['status']]
            item = QListWidgetItem(f"{task.get('time') or '时间待定'} · {task['minutes']}分钟\n{task['title']}\n{status}", rows)
            item.setData(Qt.ItemDataRole.UserRole, task)
            item.setToolTip(task.get('description', ''))
        if not rows.count():
            empty = QListWidgetItem('暂无安排', rows)
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
        rows.itemClicked.connect(lambda item: host.edit_task(item.data(Qt.ItemDataRole.UserRole)) if item.data(Qt.ItemDataRole.UserRole) else None)
        rows.itemActivated.connect(lambda item: host.edit_task(item.data(Qt.ItemDataRole.UserRole)) if item.data(Qt.ItemDataRole.UserRole) else None)
        rows.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        def context(point, widget=rows):
            item = widget.itemAt(point)
            if item and item.data(Qt.ItemDataRole.UserRole):
                task_menu(host, item.data(Qt.ItemDataRole.UserRole), widget.viewport().mapToGlobal(point))
        rows.customContextMenuRequested.connect(context)
        table.setCellWidget(0, column, rows)
    return table


class StudyPage(QWidget):
    def __init__(self, host, snapshot, page):
        super().__init__()
        self.host = host
        self.page = page
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        if page == 'today':
            from .today import build_today
            build_today(self, layout, snapshot)
            return
        titles = {'today': '今日', 'plans': '学习计划', 'notes': '心得与笔记', 'progress': '探索统计'}
        layout.addWidget(heading(titles[page]))
        tasks = sorted(snapshot['study']['tasks'], key=lambda t: (t.get('date', ''), t.get('time', '')))
        today = date.today().isoformat()
        if page in {'today', 'plans'}:
            bar = QHBoxLayout()
            bar.addWidget(button('添加计划', host.edit_task, primary=True))
            bar.addWidget(button('请小探规划', host.show_advisor))
            bar.addStretch()
            if page == 'plans':
                bar.addWidget(button('小探规划记录', host.plan_history))
            layout.addLayout(bar)
            if page == 'plans':
                views = QHBoxLayout()
                group = QButtonGroup(self)
                for key, label in [('all', '全部'), ('week', '本周')]:
                    control = button(label, lambda checked=False, k=key: self.set_plan_view(k))
                    control.setCheckable(True)
                    control.setChecked(host.plan_view == key)
                    group.addButton(control)
                    views.addWidget(control)
                views.addStretch()
                layout.addLayout(views)
                start = date.today() - timedelta(days=date.today().weekday()) + timedelta(weeks=host.week_offset)
                if host.plan_view == 'week':
                    from .navigation import icon
                    from ..reading_preferences import colors
                    arrow = icon('arrow-right', colors(snapshot['preferences']['palette'], snapshot['preferences']['mode'])['ink'])
                    back = QIcon(arrow.pixmap(24, 24).transformed(QTransform().rotate(180)))
                    week_navigation = QHBoxLayout()
                    previous = button('', lambda: self.change_week(-1),
                                      icon=back, tip='上一周')
                    following = button('', lambda: self.change_week(1),
                                       icon=arrow, tip='下一周')
                    previous.setFixedSize(36, 36)
                    following.setFixedSize(36, 36)
                    week_navigation.addWidget(previous)
                    week_navigation.addStretch()
                    week_navigation.addWidget(QLabel(f"{start:%Y-%m-%d} 至 {start+timedelta(days=6):%Y-%m-%d}"))
                    week_navigation.addStretch()
                    week_navigation.addWidget(following)
                    layout.addLayout(week_navigation)
            from .plans import PlanList, WeekPlans
            layout.addWidget(WeekPlans(host, tasks, start, snapshot['preferences']) if host.plan_view == 'week' else PlanList(host, tasks, snapshot), 1)
        elif page == 'notes':
            bar = QHBoxLayout()
            bar.addWidget(button('新建笔记', host.edit_note, primary=True))
            bar.addStretch()
            layout.addLayout(bar)
            self.notes = QListWidget()
            for note in snapshot['study']['notes']:
                label = (note.get('title') or '心得') + '  ·  ' + note.get('article_title', '') + '\n' + note['content'][:180]
                item = QListWidgetItem(label, self.notes)
                item.setData(Qt.ItemDataRole.UserRole, note)
            self.notes.itemDoubleClicked.connect(lambda item: host.edit_note(item.data(Qt.ItemDataRole.UserRole)))
            self.notes.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self.notes.customContextMenuRequested.connect(self.note_menu)
            layout.addWidget(self.notes, 1)
        else:
            from .progress import build_progress
            build_progress(self, layout, snapshot)

    def set_plan_view(self, value):
        self.host.plan_view = value
        self.host.week_offset = 0
        self.host.refresh()

    def change_week(self, delta):
        self.host.week_offset += delta
        self.host.refresh()

    def show_weather(self, value):
        if hasattr(self, 'today_weather'):
            self.today_weather.show_value(value)

    def note_menu(self, point):
        item = self.notes.itemAt(point)
        if item:
            note = item.data(Qt.ItemDataRole.UserRole)
            menu = QMenu(self)
            menu.addAction('编辑笔记', lambda: self.host.edit_note(note))
            menu.addAction('查看原文', lambda: self.host.open_article(note['article_id']))
            menu.addAction('删除笔记', lambda: self.host.delete('notes', note['id'], note.get('title') or '这篇笔记'))
            menu.exec(self.notes.mapToGlobal(point))
