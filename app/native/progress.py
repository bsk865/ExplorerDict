"""Original statistics sections and task scope, based only on saved activity."""
from datetime import date, timedelta
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QPainter, QColor
from PySide6.QtWidgets import (QWidget, QLabel, QVBoxLayout, QHBoxLayout, QScrollArea,
                              QProgressBar, QButtonGroup, QSizePolicy)
from .widgets import button, FlowLayout
from ..reading_preferences import colors


def label(text, *, size=None, muted=False):
    value = QLabel(text)
    value.setTextFormat(Qt.TextFormat.PlainText)
    value.setWordWrap(True)
    if size:
        value.setStyleSheet(f'font-size:{size}px;')
    if muted:
        value.setObjectName('muted')
    return value


class ActivityChart(QWidget):
    def __init__(self, days, palette):
        super().__init__()
        self.days, self.colors = days, palette
        self.setFixedHeight(132)
        self.setMinimumWidth(0)
        self.setAccessibleName('最近七天每日打开文章数')
        self.setAccessibleDescription('；'.join(f"{d['date']}：{d['articles_opened']}篇" for d in days))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        maximum = max([1] + [d['articles_opened'] for d in self.days])
        width = self.width() / max(1, len(self.days))
        for i, day in enumerate(self.days):
            x = i * width
            painter.setPen(QColor(self.colors['ink']))
            painter.drawText(QRectF(x, 0, width, 22), Qt.AlignmentFlag.AlignCenter, str(day['articles_opened']))
            height = max(3, day['articles_opened'] / maximum * 80)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor('#5b9685'))
            bar_width = min(36, width * .6)
            painter.drawRoundedRect(QRectF(x+(width-bar_width)/2, 108-height, bar_width, height), 3, 3)
            painter.setPen(QColor(self.colors['muted']))
            painter.drawText(QRectF(x, 110, width, 22), Qt.AlignmentFlag.AlignCenter, day['date'][5:])
        painter.end()


def build_progress(page, layout, snapshot):
    host = page.host
    palette = colors(snapshot['preferences']['palette'], snapshot['preferences']['mode'])
    page.progress_scroll = scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    body = QWidget()
    body.setObjectName('page')
    scroll.setWidget(body)
    outer = QVBoxLayout(body)
    outer.setContentsMargins(0, 0, 12, 16)
    outer.setSpacing(22)
    counts = snapshot['study']['stats']
    activity = snapshot['activity']
    completed = {r['article_id'] for r in activity.get('readings', []) if r.get('completed')}
    metrics = QWidget()
    flow = FlowLayout(metrics, spacing=32)
    for name, count in [('资料', counts['articles']), ('心得与笔记', counts['notes']),
                        ('已完成任务', counts['completed_tasks']), ('已标记读完', len(completed))]:
        metric = QWidget()
        column = QVBoxLayout(metric)
        column.setContentsMargins(0, 12, 24, 12)
        column.addWidget(label(str(count), size=32))
        column.addWidget(label(name, muted=True))
        flow.addWidget(metric)
    outer.addWidget(metrics)
    completion = QWidget()
    completion.setObjectName('taskCompletion')
    completion.setStyleSheet(f'QWidget#taskCompletion {{border-top:1px solid {palette["line"]};border-bottom:1px solid {palette["line"]};}}')
    block = QVBoxLayout(completion)
    block.setContentsMargins(0, 24, 0, 24)
    block.setSpacing(14)
    heading = QHBoxLayout()
    heading.addWidget(label('每一步，都有回响', size=18), 1)
    scopes = QWidget()
    scope_layout = QHBoxLayout(scopes)
    scope_layout.setContentsMargins(3, 3, 3, 3)
    scope_layout.setSpacing(3)
    page.progress_scopes = {}
    page.progress_group = QButtonGroup(page)
    for key, title in [('today', '今日'), ('week', '本周'), ('all', '全部')]:
        control = button(title)
        control.setCheckable(True)
        control.setStyleSheet(f'QPushButton {{border:0;padding:6px 10px;}} QPushButton:checked {{background:{palette["surface"]};color:{palette["accent"]};}}')
        page.progress_group.addButton(control)
        page.progress_scopes[key] = control
        scope_layout.addWidget(control)
    heading.addWidget(scopes)
    block.addLayout(heading)
    count_line = QHBoxLayout()
    count_line.addWidget(label('任务完成进度'), 1)
    page.progress_count = label('')
    count_line.addWidget(page.progress_count)
    block.addLayout(count_line)
    page.task_progress = QProgressBar()
    page.task_progress.setTextVisible(False)
    page.task_progress.setFixedHeight(9)
    block.addWidget(page.task_progress)
    foot = QHBoxLayout()
    page.progress_message = label('', muted=True)
    foot.addWidget(page.progress_message, 1)
    page.progress_percent = label('', size=24)
    foot.addWidget(page.progress_percent)
    block.addLayout(foot)
    tasks = snapshot['study']['tasks']
    def select(scope):
        host.progress_scope = scope
        today = date.today()
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
        selected = [t for t in tasks if scope == 'all' or (t.get('date') == today.isoformat() if scope == 'today'
                    else start.isoformat() <= t.get('date', '') <= end.isoformat())]
        total = len(selected)
        done = sum(t['status'] == 'done' for t in selected)
        page.progress_scopes[scope].setChecked(True)
        page.progress_count.setText(f'{done} / {total} 项' if total else '暂无安排')
        page.task_progress.setRange(0, max(1, total))
        page.task_progress.setValue(done)
        page.task_progress.setAccessibleName({'today':'今日','week':'本周','all':'全部'}[scope]+'任务完成进度')
        page.progress_percent.setText(f'{int(done/total*100+.5)}%' if total else '—')
        page.progress_message.setText('留一点时间，开始一件想做的事。' if not total else
            '这一程已完成，给自己的坚持一个肯定。' if done == total else
            f'已经完成 {done} 项，继续按自己的节奏前进。' if done else '从第一项开始，每一步都算数。')
    for key, control in page.progress_scopes.items():
        control.clicked.connect(lambda checked=False, k=key: select(k))
    scope = getattr(host, 'progress_scope', 'week')
    select(scope if scope in page.progress_scopes else 'week')
    outer.addWidget(completion)
    outer.addWidget(label('最近的心得', size=20))
    notes = snapshot['study']['notes']
    for note in notes[:10]:
        control = button('', lambda checked=False, n=note: host.open_article(n['article_id'], note_id=n['id']))
        control.setStyleSheet(f'QPushButton {{border:0;border-bottom:1px solid {palette["line"]};border-radius:0;text-align:left;}} QPushButton:hover {{background:{palette["surface"]};}}')
        control.setAccessibleName(note.get('title') or note.get('article_title', '心得'))
        control.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row = QVBoxLayout(control)
        row.addWidget(label(note.get('title') or note.get('article_title', '心得')))
        row.addWidget(label(note['content'][:100], muted=True))
        for text in control.findChildren(QLabel):
            text.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        outer.addWidget(control)
    if not notes:
        outer.addWidget(label('还没有记录心得。', muted=True))
    outer.addWidget(label('最近七天', size=20))
    days = activity.get('days', [])
    since = days[0]['date'] if days else date.today().isoformat()
    recent_notes = sum(n.get('created_at', '')[:10] >= since for n in notes)
    recent_tasks = sum(t['status'] == 'done' and (t.get('completed_at') or '')[:10] >= since for t in tasks)
    outer.addWidget(label(f'新增 {recent_notes} 篇心得，完成 {recent_tasks} 项计划。'))
    outer.addWidget(ActivityChart(days, palette))
    outer.addWidget(label('每日打开的文章数 · 同一文章每天计一次', muted=True))
    outer.addWidget(label('主题积累', size=20))
    page.topic_progress = {}
    for topic in snapshot['study']['themes']:
        ids = {m['article_id'] for m in snapshot['study']['memberships'] if m['theme_id'] == topic['id']}
        done = len(ids & completed)
        row = QHBoxLayout()
        row.setSpacing(20)
        title = label(topic['title'])
        title.setMinimumWidth(0)
        row.addWidget(title, 1)
        bar = QProgressBar()
        bar.setRange(0, max(1, len(ids)))
        bar.setValue(done)
        bar.setTextVisible(False)
        bar.setMaximumWidth(240)
        bar.setMinimumWidth(60)
        bar.setAccessibleName(topic['title']+'阅读进度')
        page.topic_progress[topic['id']] = bar
        row.addWidget(bar, 1)
        row.addWidget(label(f'已读完 {done} / {len(ids)} 篇', muted=True))
        outer.addLayout(row)
    if not snapshot['study']['themes']:
        outer.addWidget(label('还没有主题分类。', muted=True))
    outer.addStretch()
    layout.addWidget(scroll, 1)
