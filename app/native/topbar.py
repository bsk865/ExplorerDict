"""Shared native navigation controls and local capture status."""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QShortcut, QKeySequence, QIcon, QTransform
from PySide6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLabel, QLineEdit, QSizePolicy, QStyle
from .widgets import button
from .navigation import icon
from ..reading_preferences import colors


class ElidedLabel(QLabel):
    def __init__(self, text=''):
        super().__init__()
        self.value = text
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setText(text)

    def setText(self, text):
        self.value = text
        self.setToolTip(text)
        super().setText(self.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, max(0, self.width())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setText(self.value)


class TopBar(QWidget):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self.setObjectName('topbar')
        self.busy = False
        self.checking = False
        self.row = QHBoxLayout(self)
        self.row.setSpacing(20)
        self.row.setContentsMargins(28, 12, 28, 12)
        self.back = button('', host.go_back, icon=self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowBack), tip='返回')
        self.back.setAccessibleName('返回')
        self.back.setFixedSize(36, 36)
        self.row.addWidget(self.back)
        self.crumb = ElidedLabel('我的空间 / 今日')
        self.crumb.setStyleSheet('font-size:12px;')
        self.row.addWidget(self.crumb, 1)
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索文章')
        self.search.setAccessibleName('搜索文章')
        self.search.setMinimumWidth(100)
        self.search.setMaximumWidth(340)
        self.search.setStyleSheet('font-size:11px;')
        self.row.addWidget(self.search, 2)
        self.refresh = button('刷新', host.refresh_data)
        self.row.addWidget(self.refresh)
        self.capture = button('', host.open_capture, tip='开启或找回悬浮窗口')
        self.capture.setAccessibleName('悬浮取词')
        capture_row = QHBoxLayout(self.capture)
        capture_row.setContentsMargins(9, 4, 9, 4)
        capture_row.setSpacing(5)
        self.dot = QLabel('●')
        self.dot.setFixedWidth(6)
        self.dot.setStyleSheet('color:#aaa197; font-size:8px;')
        capture_row.addWidget(self.dot)
        self.caption = QLabel('悬浮取词')
        self.caption.setStyleSheet('font-size:11px;')
        capture_row.addWidget(self.caption)
        self.capture_label = QLabel('正在检查')
        self.capture_label.setStyleSheet('font-size:9px;')
        capture_row.addWidget(self.capture_label)
        for item in (self.dot, self.caption, self.capture_label):
            item.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.row.addWidget(self.capture)
        self.settings = button('设置', host.model_settings)
        self.row.addWidget(self.settings)
        self.shortcut = QShortcut(QKeySequence('Ctrl+K'), host)
        self.shortcut.activated.connect(self.focus_search)
        self.back_shortcut = QShortcut(QKeySequence('Alt+Left'), host)
        self.back_shortcut.activated.connect(host.go_back)
        self.timer = QTimer(self)
        self.timer.setInterval(3000)
        self.timer.timeout.connect(self.check_capture)
        self.timer.start()
        QTimer.singleShot(0, self.check_capture)

    def focus_search(self):
        self.search.setFocus()
        self.search.selectAll()

    def adapt(self, width, reader=False):
        self.setFixedHeight(64 if reader else 78)
        self.row.setSpacing(10 if width <= 1150 else 20)
        self.capture_label.setVisible(width > 1150)
        self.capture.setMinimumWidth(self.capture.layout().sizeHint().width() + 24)

    def theme(self, preference):
        palette = colors(preference['palette'], preference['mode'])
        self.back.setIcon(QIcon(icon('arrow-right', palette['ink']).pixmap(24, 24).transformed(QTransform().rotate(180))))
        self.setStyleSheet(f'QWidget#topbar {{border-bottom:1px solid {palette["line"]};}}'
                          'QPushButton {font-size:11px; padding:5px 12px;}')
        for action in self.search.actions():
            self.search.removeAction(action)
            action.deleteLater()
        self.search.addAction(icon('search', palette['muted']), QLineEdit.ActionPosition.LeadingPosition)

    def render_capture(self, result):
        text = result.get('label') or '悬浮取词未开启'
        self.capture_label.setText(text)
        self.capture.setMinimumWidth(self.capture.layout().sizeHint().width() + 24)
        self.capture.setToolTip(text+' · 点击开启或找回悬浮窗口')
        self.dot.setStyleSheet('font-size:8px; color:'+('#65816c' if result.get('ready') else '#aaa197')+';')

    def check_capture(self):
        if self.busy or self.checking or self.host.closing or not self.isVisible():
            return
        from ..capture_bridge import status
        self.checking = True
        def done(result):
            self.checking = False
            if not self.busy:
                self.render_capture(result)
        def failed(message):
            self.checking = False
            if not self.busy:
                self.render_capture({'label':'状态暂不可用，点击重试'})
        self.host.tasks.submit(status, done, failed)
