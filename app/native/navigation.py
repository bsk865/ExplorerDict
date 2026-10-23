"""Native navigation using the legacy interface's brand and icon assets."""
import json
from functools import lru_cache
from xml.etree import ElementTree
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QLabel,
                              QButtonGroup, QGridLayout, QDialog, QSizePolicy)
from ..paths import resource_root
from ..reading_preferences import colors
from .widgets import button


@lru_cache(maxsize=1)
def icon_sources():
    return json.loads((resource_root() / 'web/assets/native-icons.json').read_text(encoding='utf-8'))


@lru_cache(maxsize=128)
def icon(name, color):
    root = ElementTree.fromstring('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        'fill="none" stroke="'+color+'" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round">'
        + icon_sources()[name] + '</svg>')
    for child in root:
        if child.get('class') == 'icon-fill':
            child.set('fill', color)
            child.set('stroke', 'none')
            child.set('opacity', '.08')
    renderer = QSvgRenderer(ElementTree.tostring(root))
    pixmap = QPixmap(48, 48)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return QIcon(pixmap)


ROUTES = [('today', '今日', 'grid'), ('shelf', '主题资料库', 'archive'), ('plans', '计划', 'list'),
          ('progress', '探索统计', 'map'), ('library', '资料库问答', 'search'),
          ('notes', '笔记管理', 'note'), ('content', '内容管理', 'settings')]


class Sidebar(QScrollArea):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName('sidebar')
        self.setWidget(body)
        self.rail = QVBoxLayout(body)
        self.rail.setContentsMargins(16, 18, 16, 12)
        self.rail.setSpacing(0)
        brand = QHBoxLayout()
        brand.setSpacing(11)
        self.mark = QLabel()
        brand.addWidget(self.mark)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(3)
        name = QLabel('探索词典')
        name.setObjectName('brand')
        brand_text.addWidget(name)
        self.tagline = QLabel('KEEP YOUR CURIOSITY')
        self.tagline.setStyleSheet('font-size:7px;')
        brand_text.addWidget(self.tagline)
        brand.addLayout(brand_text, 1)
        self.rail.addLayout(brand)
        self.workspace = QLabel('●  我的阅读空间     LOCAL')
        self.workspace.setStyleSheet('font-size:10px; padding:18px 6px 12px;')
        self.rail.addWidget(self.workspace)
        self.group = QButtonGroup(self)
        self.buttons = {}
        for route, label, _ in ROUTES:
            control = button(label, lambda checked=False, key=route: host.navigate(key))
            control.setObjectName('nav')
            control.setCheckable(True)
            control.setIconSize(QSize(19, 19))
            self.group.addButton(control)
            self.buttons[route] = control
            self.rail.addWidget(control)
            self.rail.addSpacing(4)
        section = QHBoxLayout()
        section.setContentsMargins(8, 10, 8, 4)
        label = QLabel('阅读材料')
        label.setObjectName('muted')
        label.setStyleSheet('font-size:11px;')
        section.addWidget(label, 1)
        self.add = button('', host.import_material, tip='添加资料')
        self.add.setAccessibleName('添加资料')
        self.add.setFixedSize(28, 24)
        self.add.setStyleSheet('padding:2px;')
        section.addWidget(self.add)
        self.rail.addLayout(section)
        self.recent = QVBoxLayout()
        self.recent.setSpacing(0)
        self.rail.addLayout(self.recent)
        self.rail.addStretch(1)
        self.bottom = QWidget()
        bottom = QGridLayout(self.bottom)
        bottom.setContentsMargins(0, 10, 0, 0)
        bottom.setSpacing(4)
        self.character = button('小探', self.show_character)
        self.character.setIconSize(QSize(36, 36))
        self.character.setMinimumHeight(58)
        self.character.setStyleSheet('text-align:left; font-size:12px; padding:9px 10px;')
        bottom.addWidget(self.character, 0, 0, 1, 2)
        self.preferences = button('偏好设置', host.preferences)
        self.settings = button('设置', host.model_settings)
        for control in (self.preferences, self.settings):
            control.setStyleSheet('border:0; padding:6px 0; font-size:11px;')
            control.setIconSize(QSize(15, 15))
        bottom.addWidget(self.preferences, 1, 0)
        bottom.addWidget(self.settings, 1, 1)
        self.rail.addWidget(self.bottom)
        self.preference = {'palette': 'clay', 'mode': 'light'}
        self.article_ids = None
        self.update_content([], self.preference, 'today')

    def adapt(self, width, height):
        self.setFixedWidth(180 if width <= 900 else 195 if width <= 1150 else 224)
        compact = height <= 740
        self.rail.setContentsMargins(16, 10 if compact else 18, 16, 12)
        self.tagline.setVisible(not compact and width > 900)
        size = 32 if compact else 40
        self.mark.setFixedSize(size, size)
        prefix = '' if self.preference['palette'] == 'blue' else self.preference['palette']+'-'
        self.mark.setPixmap(QPixmap(str(resource_root() / 'web/assets' / (prefix+'brand-mark.svg'))).scaled(
            size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        for control in self.buttons.values():
            control.setFixedHeight(32 if compact else 36)

    def update_content(self, articles, preference, active):
        self.preference = preference
        palette = colors(preference['palette'], preference['mode'])
        muted, accent = palette['muted'], palette.get('accent-text', palette['accent'])
        self.group.setExclusive(False)
        for route, _, symbol in ROUTES:
            control = self.buttons[route]
            control.setChecked(route == active)
            control.setIcon(icon(symbol, accent if route == active else muted))
        self.group.setExclusive(True)
        self.add.setIcon(icon('plus', muted))
        self.settings.setIcon(icon('settings', muted))
        self.preferences.setIcon(icon('settings', muted))
        self.character.setIcon(QIcon(str(resource_root() / 'web/assets/generated' / (preference['palette']+'-xiaotan-avatar.webp'))))
        identity = (tuple((a['id'], a['title']) for a in articles[:6]), muted)
        if self.article_ids != identity:
            self.article_ids = identity
            while self.recent.count():
                old = self.recent.takeAt(0).widget()
                old.hide()
                old.deleteLater()
            for article in articles[:6]:
                control = button('', lambda checked=False, aid=article['id']: self.host.open_article(aid))
                control.setAccessibleName(article['title'])
                control.setToolTip(article['title'])
                control.setStyleSheet('border:0; text-align:left; padding:0;')
                row = QHBoxLayout(control)
                row.setContentsMargins(8, 8, 8, 8)
                row.setSpacing(8)
                picture = QLabel()
                picture.setPixmap(icon('file', accent).pixmap(14, 14))
                picture.setFixedSize(14, 20)
                title = QLabel(article['title'])
                title.setTextFormat(Qt.TextFormat.PlainText)
                title.setWordWrap(True)
                title.setMaximumHeight(40)
                title.setMinimumWidth(0)
                title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                title.setStyleSheet('font-size:11px;')
                for widget in (picture, title):
                    widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                    row.addWidget(widget)
                self.recent.addWidget(control)
        self.adapt(self.host.width(), self.host.height())

    def show_character(self):
        dialog = QDialog(self.host)
        dialog.setWindowTitle('小探')
        dialog.resize(520, 360)
        layout = QHBoxLayout(dialog)
        portrait = QLabel()
        portrait.setPixmap(QPixmap(str(resource_root() / 'web/assets/generated' /
            (self.preference['palette']+'-xiaotan-portrait.webp'))).scaled(220, 290,
             Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        layout.addWidget(portrait)
        column = QVBoxLayout()
        column.addStretch()
        column.addWidget(QLabel('陪你多理解一点。'))
        column.addWidget(QLabel('从一个词、一张图出发，\n把问题放回原文里。'))
        def preferences():
            dialog.accept()
            self.host.preferences()
        column.addWidget(button('换一个阅读主题', preferences, primary=True))
        column.addStretch()
        layout.addLayout(column)
        dialog.exec()
