"""Immediate theme selection with the original six illustrated previews."""
import sqlite3
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QButtonGroup, QSizePolicy)
from .surfaces import QDialog
from .widgets import button, stylesheet
from ..paths import resource_root
from .. import reading_preferences

THEMES = [('clay', '暖纸书桌'), ('violet', '暮光花笺'), ('parchment', '旧书手札'),
          ('blue', '星夜书房'), ('forest', '林间晨读'), ('ocean', '海盐晴空')]


class ThemePreview(QWidget):
    def __init__(self, key):
        super().__init__()
        self.pixmap = QPixmap(str(resource_root() / 'web/assets/generated' / (key + '-reading-scene.webp')))
        self.setFixedHeight(66)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        painter = QPainter(self)
        if not self.pixmap.isNull():
            scaled = self.pixmap.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                        Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap((self.width() - scaled.width()) // 2, (self.height() - scaled.height()) // 2, scaled)


class AppearanceDialog(QDialog):
    changed = Signal()

    def __init__(self, directory, parent=None):
        super().__init__(parent)
        self.directory = directory
        self.value = reading_preferences.load(directory)
        self.setWindowTitle('偏好设置')
        self.setMinimumWidth(380)
        self.resize(620, 300)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        self.grid = QGridLayout()
        self.grid.setSpacing(9)
        layout.addLayout(self.grid)
        self.group = QButtonGroup(self)
        self.choices = {}
        for key, name in THEMES:
            control = button('', lambda checked=False, k=key: self.apply(palette=k))
            control.setCheckable(True)
            control.setAccessibleName(name)
            control.setObjectName('themeChoice')
            control.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            control.setFixedHeight(110)
            contents = QVBoxLayout(control)
            contents.setContentsMargins(7, 7, 7, 7)
            contents.setSpacing(5)
            contents.addWidget(ThemePreview(key))
            title = QLabel(name)
            title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            contents.addWidget(title)
            self.group.addButton(control)
            self.choices[key] = control
        self.message = QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        footer = QHBoxLayout()
        self.mode = button('', lambda: self.apply(mode='light' if self.value['mode'] == 'dark' else 'dark'))
        footer.addWidget(self.mode)
        footer.addStretch()
        footer.addWidget(button('关闭', self.accept))
        layout.addLayout(footer)
        self.columns = 0
        self.render()
        self.reflow()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.reflow()

    def reflow(self):
        columns = 2 if self.width() <= 560 else 3
        if not hasattr(self, 'columns') or columns == self.columns:
            return
        for i in range(3):
            self.grid.setColumnStretch(i, 0)
        for control in self.choices.values():
            self.grid.removeWidget(control)
        for i, control in enumerate(self.choices.values()):
            self.grid.addWidget(control, i // columns, i % columns)
        for i in range(columns):
            self.grid.setColumnStretch(i, 1)
        self.columns = columns

    def render(self):
        palette = reading_preferences.colors(self.value['palette'], self.value['mode'])
        self.setStyleSheet(stylesheet(self.value) + f'''
            QPushButton#themeChoice {{padding:0;border:1px solid {palette['line']};background:{palette['bg']};}}
            QPushButton#themeChoice:checked {{border:2px solid {palette['accent']};background:{palette['bg']};}}
        ''')
        self.group.setExclusive(False)
        for key, control in self.choices.items():
            control.setChecked(key == self.value['palette'])
        self.group.setExclusive(True)
        self.mode.setText('切换纸面' if self.value['mode'] == 'dark' else '切换夜读')

    def apply(self, **changes):
        value = {**self.value, **changes}
        if value == self.value:
            return
        try:
            reading_preferences.save(self.directory, value)
        except (ValueError, OSError, sqlite3.Error):
            self.message.setText('未能保存外观，请稍后重试。')
            self.render()
            return
        self.value = value
        self.message.clear()
        self.render()
        self.changed.emit()
