"""The original today layout, backed by local tasks and reading history."""
from datetime import datetime, date
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
                              QScrollArea, QCheckBox, QProgressBar)
from .widgets import button, ReadingArt
from .navigation import icon
from ..paths import resource_root
from ..reading_preferences import colors


def label(text, size=13, bold=False):
    value = QLabel(text)
    value.setTextFormat(Qt.TextFormat.PlainText)
    value.setWordWrap(True)
    value.setStyleSheet(f'font-size:{size}px; font-weight:{600 if bold else 400};')
    return value


class TodayWeather(QWidget):
    def __init__(self, host, palette):
        super().__init__()
        self.palette = palette
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(14)
        self.symbol = QLabel()
        self.symbol.setFixedSize(40, 40)
        row.addWidget(self.symbol)
        self.copy = QVBoxLayout()
        self.city = label('天气')
        self.condition = label('', 15)
        self.updated = label('', 11)
        self.updated.setObjectName('muted')
        for item in (self.city, self.condition, self.updated):
            self.copy.addWidget(item)
        row.addLayout(self.copy, 1)
        actions = QVBoxLayout()
        self.change = button('选择城市', host.choose_city)
        self.change.setStyleSheet('border:0; padding:6px; font-size:12px;')
        actions.addWidget(self.change)
        self.provider = label('Open-Meteo', 11)
        self.provider.setObjectName('muted')
        self.provider.setAlignment(Qt.AlignmentFlag.AlignRight)
        actions.addWidget(self.provider)
        row.addLayout(actions)
        from ..study_weather import saved
        self.show_value(saved(host.service.store))

    def show_value(self, value):
        configured = value.get('status') != 'unconfigured'
        self.symbol.setVisible(configured)
        self.condition.setVisible(configured)
        self.updated.setVisible(configured)
        self.provider.setVisible(configured)
        self.change.setText('更换城市' if configured else '选择城市')
        self.city.setText(value.get('city', {}).get('name', '') if configured else '天气')
        code = value.get('code')
        name, symbol = ('晴', 'sun') if code == 0 else ('多云', 'cloud') if code in {1, 2, 3} else (
            '雾', 'fog') if code in {45, 48} else ('雪', 'snow') if code in {71, 73, 75, 77, 85, 86} else (
            '雷雨', 'storm') if code in {95, 96, 99} else ('毛毛雨', 'rain') if code in {51, 53, 55, 56, 57} else (
            '雨', 'rain') if code in {61, 63, 65, 66, 67, 80, 81, 82} else ('天气', 'cloud')
        self.symbol.setPixmap(icon(symbol, self.palette['accent']).pixmap(36, 36))
        temperature = value.get('temperature')
        self.condition.setText(f'{temperature:g}°C · {name}' if temperature is not None else '正在更新天气…' if value.get('status') == 'loading' else '暂时无法获取天气')
        stamp = str(value.get('observed_at') or '').replace('T', ' ')
        self.updated.setText(('更新失败 · ' if value.get('status') == 'stale' else '') + ('更新于 '+stamp if stamp else ''))


def build_today(page, layout, snapshot):
    host = page.host
    preference = snapshot['preferences']
    palette = colors(preference['palette'], preference['mode'])
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    body = QWidget()
    body.setObjectName('page')
    scroll.setWidget(body)
    layout.addWidget(scroll)
    column = QVBoxLayout(body)
    column.setContentsMargins(0, 18, 0, 0)
    column.setSpacing(0)
    page.today_scroll = scroll
    page.today_banner = ReadingArt(preference['palette'])
    page.today_banner.setFixedHeight(240)
    hero = QVBoxLayout(page.today_banner)
    hero.setContentsMargins(32, 24, 32, 24)
    hero.setSpacing(8)
    hero.addStretch()
    title = label('今日', 32)
    title.setStyleSheet('font-size:32px; color:#243b43;')
    hero.addWidget(title)
    greeting = label('给今天，留一点探索的时间。', 15)
    greeting.setStyleSheet('font-size:15px; color:#243b43;')
    hero.addWidget(greeting)
    day = date.today()
    day_label = label(f'{day.year}年{day.month}月{day.day}日星期'+ '一二三四五六日'[day.weekday()], 12)
    day_label.setStyleSheet('font-size:12px; color:#243b43;')
    hero.addWidget(day_label)
    actions = QHBoxLayout()
    actions.setSpacing(8)
    actions.addWidget(button('请小探规划', host.show_advisor))
    actions.addWidget(button('添加计划', host.edit_task, primary=True))
    actions.addStretch()
    hero.addSpacing(10)
    hero.addLayout(actions)
    hero.addStretch()
    column.addWidget(page.today_banner)
    strip = QWidget()
    strip.setStyleSheet(f'QWidget#dayStrip {{border-bottom:1px solid {palette["line"]};}}')
    strip.setObjectName('dayStrip')
    row = QHBoxLayout(strip)
    row.setContentsMargins(0, 20, 0, 20)
    row.setSpacing(24)
    clock = QVBoxLayout()
    clock.setSpacing(0)
    page.clock = label('', 32)
    page.clock.setMinimumWidth(130)
    clock.addWidget(page.clock)
    clock.addWidget(label('此刻', 12))
    row.addLayout(clock)
    page.today_weather = TodayWeather(host, palette)
    row.addWidget(page.today_weather, 1)
    column.addWidget(strip)
    page.timer = QTimer(page)
    def tick():
        page.clock.setText(datetime.now().strftime('%H:%M'))
        if date.today() != day:
            page.timer.stop()
            host.refresh()
    page.timer.timeout.connect(tick)
    page.timer.start(1000)
    tick()
    tasks = sorted((dict(t) for t in snapshot['study']['tasks'] if t.get('date') == day.isoformat()), key=lambda t:t.get('time', ''))
    heading = QHBoxLayout()
    heading.setContentsMargins(0, 28, 0, 14)
    heading.addWidget(label('今日安排', 18, True), 1)
    progress = QWidget()
    progress.setMaximumWidth(240)
    progress.setMinimumWidth(180)
    progress_layout = QVBoxLayout(progress)
    progress_layout.setContentsMargins(0, 0, 0, 0)
    progress_layout.setSpacing(6)
    progress_copy = QHBoxLayout()
    progress_copy.addWidget(label('今日进度', 11))
    page.today_count = label('', 11)
    page.today_count.setAlignment(Qt.AlignmentFlag.AlignRight)
    progress_copy.addWidget(page.today_count)
    progress_layout.addLayout(progress_copy)
    page.today_progress = QProgressBar()
    page.today_progress.setTextVisible(False)
    page.today_progress.setRange(0, max(1, len(tasks)))
    progress_layout.addWidget(page.today_progress)
    heading.addWidget(progress)
    column.addLayout(heading)
    def update_progress():
        done = sum(t['status'] == 'done' for t in tasks)
        page.today_count.setText(f'{done} / {len(tasks)} 项' if tasks else '暂无安排')
        page.today_progress.setValue(done)
    update_progress()
    page.today_checks = []
    articles = {a['id']: a for a in snapshot['articles']}
    for task in tasks:
        entry = QWidget()
        entry.setObjectName('todayTask')
        entry.setStyleSheet(f'QWidget#todayTask {{border-top:1px solid {palette["line"]};}}')
        grid = QGridLayout(entry)
        grid.setContentsMargins(0, 16, 0, 16)
        grid.setHorizontalSpacing(14)
        check = QCheckBox()
        check.setAccessibleName('完成 '+task['title'])
        check.setChecked(task['status'] == 'done')
        page.today_checks.append(check)
        grid.addWidget(check, 0, 0)
        time = label(task['date']+'\n'+(task.get('time') or '时间待定'), 11)
        time.setFixedWidth(90)
        grid.addWidget(time, 0, 1)
        edit = button('', lambda checked=False, t=task: host.edit_task(t))
        edit.setAccessibleName(task['title'])
        edit.setStyleSheet('border:0; padding:0; text-align:left;')
        copy = QVBoxLayout(edit)
        copy.setContentsMargins(0, 0, 0, 0)
        task_title = label(task['title'], 13, True)
        def appearance(done, title=task_title):
            font = title.font()
            font.setStrikeOut(done)
            title.setFont(font)
        appearance(task['status'] == 'done')
        for item in (task_title, label((task.get('goal', '')+' · ' if task.get('goal') else '')+str(task['minutes'])+' 分钟', 11)):
            item.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            copy.addWidget(item)
        grid.addWidget(edit, 0, 2)
        grid.setColumnStretch(2, 1)
        links = QGridLayout()
        links.setSpacing(3)
        link_count = 0
        for aid in task.get('article_ids', []):
            if aid in articles:
                link = button('', lambda checked=False, key=aid: host.open_article(key), icon=icon('book', palette['muted']), tip=articles[aid]['title'])
                link.setAccessibleName('阅读 '+articles[aid]['title'])
                link.setFixedSize(32, 32)
                link.setStyleSheet('border:0; padding:4px;')
                links.addWidget(link, link_count // 4, link_count % 4)
                link_count += 1
        if links.count():
            grid.addLayout(links, 0, 3)
        def complete(value, task=task, check=check, appearance=appearance):
            check.setEnabled(False)
            def saved(result):
                task.update(result)
                appearance(task['status'] == 'done')
                check.setEnabled(True)
                update_progress()
            def failed(message):
                check.blockSignals(True)
                check.setChecked(task['status'] == 'done')
                check.blockSignals(False)
                check.setEnabled(True)
                host.notice(message)
            host.tasks.submit(lambda:host.service.hub.save('tasks', {'id': task['id'], 'status':'done' if value else 'todo'}), saved, failed)
        check.toggled.connect(complete)
        entry.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        from .study import task_menu
        entry.customContextMenuRequested.connect(lambda point, t=task, widget=entry:task_menu(host, t, widget.mapToGlobal(point)))
        column.addWidget(entry)
    if not tasks:
        empty = QWidget()
        empty_layout = QHBoxLayout(empty)
        empty_layout.setContentsMargins(0, 24, 0, 24)
        avatar = QLabel()
        avatar.setPixmap(QPixmap(str(resource_root()/'web/assets/generated'/(preference['palette']+'-xiaotan-avatar.webp'))).scaled(48, 48, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        empty_layout.addWidget(avatar)
        copy = QVBoxLayout()
        copy.addWidget(label('从一件小事开始', 14, True))
        copy.addWidget(label('读一段资料，记一个想法，或安排下一步。', 12))
        empty_layout.addLayout(copy, 1)
        empty_layout.addWidget(button('添加计划', host.edit_task, icon=icon('plus', palette['muted'])))
        column.addWidget(empty)
    recent = sorted((r for r in snapshot['activity']['readings'] if r['article_id'] in articles), key=lambda r:r['last_opened'], reverse=True)[:3]
    if recent:
        column.addSpacing(28)
        column.addWidget(label('继续阅读', 18, True))
        for reading in recent:
            aid = reading['article_id']
            text = articles[aid]['title']+'\n'+('已读完 · 再次回顾' if reading['completed'] else '阅读中')
            control = button('', lambda checked=False, key=aid:host.open_article(key))
            control.setAccessibleName(articles[aid]['title'])
            control.setStyleSheet('text-align:left; border:0; padding:12px 0;')
            row = QHBoxLayout(control)
            row.setContentsMargins(0, 8, 0, 8)
            symbol = QLabel()
            symbol.setPixmap(icon('book', palette['muted']).pixmap(20, 20))
            copy = label(text, 13)
            for widget in (symbol, copy):
                widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            row.addWidget(symbol)
            row.addWidget(copy, 1)
            column.addWidget(control)
    column.addStretch(1)
