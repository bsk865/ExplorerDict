"""Native topic shelves and article drag-and-drop."""
import json
from PySide6.QtCore import Qt, QMimeData, Signal, QSize, QPoint
from PySide6.QtGui import QDrag, QPainter, QPen, QColor, QTextLayout, QTextOption, QPixmap
from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QListWidget, QListWidgetItem, QAbstractItemView, QSplitter, QMenu, QLabel, QStyledItemDelegate, QScrollArea, QSizePolicy)
from .surfaces import QDialog
from .widgets import button, heading
from .dialogs import EditForm
from .materials import article_menu
from .topbar import ElidedLabel
from ..reading_preferences import colors
from .navigation import icon

ARTICLE_MIME = 'application/x-explorerdict-articles'


class ArticleList(QListWidget):
    def __init__(self, palette):
        super().__init__()
        self.colors = palette
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)

    def drag_preview(self, articles):
        ratio = self.devicePixelRatioF()
        preview = QPixmap(round(280 * ratio), round(56 * ratio))
        preview.setDevicePixelRatio(ratio)
        preview.fill(Qt.GlobalColor.transparent)
        painter = QPainter(preview)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(self.colors['accent']), 1))
        painter.setBrush(QColor(self.colors['surface']))
        painter.drawRoundedRect(1, 1, 278, 54, 6, 6)
        icon('file', self.colors['ink']).paint(painter, 14, 16, 24, 24)
        icon('plus', self.colors['accent']).paint(painter, 246, 18, 20, 20)
        painter.setFont(self.font())
        painter.setPen(QColor(self.colors['ink']))
        caption = articles[0]['title'] if len(articles) == 1 else f'{len(articles)} 篇资料'
        caption = painter.fontMetrics().elidedText(caption, Qt.TextElideMode.ElideRight, 184)
        painter.drawText(48, 0, 184, 56, Qt.AlignmentFlag.AlignVCenter, caption)
        painter.end()
        return preview

    def startDrag(self, supported):
        articles = [item.data(Qt.ItemDataRole.UserRole) for item in self.selectedItems()]
        ids = [article['id'] for article in articles]
        if not ids:
            return
        mime = QMimeData()
        mime.setData(ARTICLE_MIME, json.dumps(ids).encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(self.drag_preview(articles))
        drag.setHotSpot(QPoint(24, 24))
        drag.exec(Qt.DropAction.CopyAction)


class ThemeList(QListWidget):
    assigned = Signal(str, list)

    def __init__(self, accent='#a65e42'):
        super().__init__()
        self.drop_color = QColor(accent)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setMinimumWidth(180)
        self.setMaximumWidth(300)
        self.drop_target = None

    def set_drop_target(self, item):
        if self.drop_target is not item:
            self.drop_target = item
            self.viewport().update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.drop_target is not None:
            painter = QPainter(self.viewport())
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(self.drop_color, 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(self.visualItemRect(self.drop_target).adjusted(2, 2, -2, -2), 4, 4)
            painter.end()

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(ARTICLE_MIME):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        if event.mimeData().hasFormat(ARTICLE_MIME) and item and item.data(Qt.ItemDataRole.UserRole) not in ('', 'unfiled'):
            self.set_drop_target(item)
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            self.set_drop_target(None)
            event.ignore()

    def dragLeaveEvent(self, event):
        self.set_drop_target(None)
        event.accept()

    def dropEvent(self, event):
        self.set_drop_target(None)
        item = self.itemAt(event.position().toPoint())
        if not event.mimeData().hasFormat(ARTICLE_MIME) or not item or item.data(Qt.ItemDataRole.UserRole) in ('', 'unfiled'):
            event.ignore()
            return
        try:
            ids = json.loads(bytes(event.mimeData().data(ARTICLE_MIME)))
            if not isinstance(ids, list) or not ids or any(not isinstance(v, str) or not v for v in ids):
                raise ValueError()
        except (ValueError, TypeError):
            event.ignore()
            return
        self.assigned.emit(item.data(Qt.ItemDataRole.UserRole), ids)
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()


class RoomDelegate(QStyledItemDelegate):
    def __init__(self, palette, parent):
        super().__init__(parent)
        self.colors = palette

    def paint(self, painter, option, index):
        painter.save()
        rect = option.rect.adjusted(0, 0, -14, -14)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self.colors['surface']))
        painter.drawRoundedRect(rect, 6, 6)
        painter.fillRect(rect.x(), rect.y(), rect.width(), 5,
                         QColor(['#488879', '#ba6777', '#6596b2'][index.row() % 3]))
        icon('archive', self.colors['ink']).paint(painter, rect.x()+22, rect.y()+25, 24, 24)
        painter.setPen(QColor(self.colors['ink']))
        title, count = index.data().split('\n', 1)
        title_rect = rect.adjusted(22, 62, -18, -40)
        text = QTextLayout(title, option.font)
        wrap = QTextOption()
        wrap.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        text.setTextOption(wrap)
        text.beginLayout()
        y = title_rect.top()
        while True:
            line = text.createLine()
            if not line.isValid():
                break
            line.setLineWidth(title_rect.width())
            if y + 2 * line.height() > title_rect.bottom():
                remaining = title[line.textStart():]
                painter.drawText(title_rect.left(), int(y + line.ascent()),
                                 option.fontMetrics.elidedText(remaining, Qt.TextElideMode.ElideRight, title_rect.width()))
                break
            painter.drawText(title_rect.left(), int(y + line.ascent()),
                             title[line.textStart():line.textStart()+line.textLength()])
            y += line.height()
        text.endLayout()
        painter.drawText(rect.adjusted(22, 0, -18, -20), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, count)
        painter.restore()


class ThemeRooms(ThemeList):
    def __init__(self, palette):
        super().__init__(palette['accent'])
        self.setMaximumWidth(16777215)
        self.setMinimumWidth(0)
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setMovement(QListWidget.Movement.Static)
        # Static icon layout disables drops on QListView's viewport. Keep the
        # cards stationary while allowing articles to be filed onto them.
        self.viewport().setAcceptDrops(True)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setGridSize(QSize(214, 170))
        self.setItemDelegate(RoomDelegate(palette, self))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        columns = max(1, self.viewport().width() // 194)
        width = max(1, self.viewport().width() // columns)
        self.setGridSize(QSize(width, 170))
        for row in range(self.count()):
            self.item(row).setSizeHint(QSize(width, 170))
        self.setFixedHeight(min(340, max(170, ((self.count()+columns-1)//columns)*170)))


class ShelfPage(QWidget):
    def __init__(self, host, snapshot, theme=''):
        super().__init__()
        self.host, self.snapshot, self.theme = host, snapshot, theme
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        bar.addWidget(heading('主题资料库'), 1)
        bar.addWidget(button('知识索引', lambda: host.navigate('index')))
        bar.addWidget(button('笔记与收藏', lambda: host.navigate('saved')))
        bar.addWidget(button('新建主题', self.theme_form))
        bar.addWidget(button('添加资料', host.import_material, primary=True))
        layout.addLayout(bar)
        splitter = QSplitter()
        preferences = snapshot['preferences']
        self.themes = ThemeList(colors(preferences['palette'], preferences['mode'])['accent'])
        counts = {}
        for row in snapshot['study']['memberships']:
            counts[row['theme_id']] = counts.get(row['theme_id'], 0) + 1
        assigned = {row['article_id'] for row in snapshot['study']['memberships']}
        counts[''] = len(snapshot['articles'])
        counts['unfiled'] = sum(article['id'] not in assigned for article in snapshot['articles'])
        for label, key in [('全部资料', ''), ('未分类', 'unfiled')] + [(t['title'], t['id']) for t in snapshot['study']['themes']]:
            item = QListWidgetItem(label + f"   {counts.get(key, 0)}", self.themes)
            item.setToolTip(label)
            item.setData(Qt.ItemDataRole.UserRole, key)
            if key == theme:
                self.themes.setCurrentItem(item)
        self.themes.itemClicked.connect(lambda item: self.select_theme(item.data(Qt.ItemDataRole.UserRole)))
        self.themes.assigned.connect(self.assign)
        self.themes.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.themes.customContextMenuRequested.connect(self.theme_menu)
        self.articles = ArticleList(colors(preferences['palette'], preferences['mode']))
        self.articles.itemClicked.connect(self.read_article)
        self.articles.itemActivated.connect(self.read_article)
        self.articles.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.articles.customContextMenuRequested.connect(self.article_menu)
        contents = QWidget()
        column = QVBoxLayout(contents)
        column.setContentsMargins(24, 0, 0, 0)
        column.setSpacing(16)
        section = QHBoxLayout()
        self.section_title = QLabel('全部资料')
        self.section_title.setWordWrap(True)
        self.section_title.setStyleSheet('font-size:20px;font-weight:600;')
        section.addWidget(self.section_title, 1)
        self.organize = button('整理文章', self.members)
        self.edit_theme = button('编辑主题', self.edit_selected_theme)
        section.addWidget(self.organize)
        section.addWidget(self.edit_theme)
        column.addLayout(section)
        self.description = QLabel()
        self.description.setWordWrap(True)
        column.addWidget(self.description)
        self.rooms = ThemeRooms(colors(preferences['palette'], preferences['mode']))
        for topic in snapshot['study']['themes']:
            room = QListWidgetItem(topic['title'] + f"\n{counts.get(topic['id'], 0)} 篇文章", self.rooms)
            room.setData(Qt.ItemDataRole.UserRole, topic['id'])
            room.setSizeHint(QSize(214, 170))
            room.setToolTip(topic['title'])
        self.rooms.itemClicked.connect(lambda item: self.select_theme(item.data(Qt.ItemDataRole.UserRole)))
        self.rooms.assigned.connect(self.assign)
        column.addWidget(self.rooms)
        self.empty = QLabel('这里还没有文章。')
        self.empty.setObjectName('muted')
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.empty.setContentsMargins(8, 20, 8, 0)
        column.addWidget(self.empty, 1)
        column.addWidget(self.articles, 1)
        splitter.addWidget(self.themes)
        splitter.addWidget(contents)
        splitter.setSizes([220, 850])
        layout.addWidget(splitter, 1)
        self.select_theme(theme, remember=False)

    def select_theme(self, key, remember=True):
        if key not in ('', 'unfiled') and not any(t['id'] == key for t in self.snapshot['study']['themes']):
            key = ''
        if remember and key != self.theme and isinstance(getattr(self.host, 'history', None), list):
            self.host.history.append(self.host.navigation_state())
            self.host.history = self.host.history[-50:]
            self.host.back.setEnabled(True)
        self.theme = key
        selected = next((t for t in self.snapshot['study']['themes'] if t['id'] == key), None)
        self.section_title.setText(selected['title'] if selected else '未分类' if key == 'unfiled' else '全部资料')
        self.description.setText((selected or {}).get('description', ''))
        self.description.setVisible(bool(self.description.text()))
        self.organize.setVisible(bool(selected))
        self.edit_theme.setVisible(bool(selected))
        self.rooms.setVisible(not key and self.rooms.count() > 0)
        for row in range(self.themes.count()):
            if self.themes.item(row).data(Qt.ItemDataRole.UserRole) == key:
                self.themes.setCurrentRow(row)
                break
        self.filter(self.host.search.text())

    def edit_selected_theme(self):
        selected = next((t for t in self.snapshot['study']['themes'] if t['id'] == self.theme), None)
        if selected:
            self.theme_form(theme=selected)

    def filter(self, query):
        self.articles.clear()
        memberships = self.snapshot['study']['memberships']
        assigned = {m['article_id'] for m in memberships}
        in_theme = {m['article_id'] for m in memberships if m['theme_id'] == self.theme}
        for article in self.snapshot['articles']:
            if query.casefold() not in article['title'].casefold():
                continue
            if self.theme == 'unfiled' and article['id'] in assigned:
                continue
            if self.theme not in ('', 'unfiled') and article['id'] not in in_theme:
                continue
            n = sum(note['article_id'] == article['id'] for note in self.snapshot['study']['notes'])
            item = QListWidgetItem('', self.articles)
            item.setData(Qt.ItemDataRole.UserRole, article)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, article['title'])
            item.setToolTip(article['title'])
            item.setSizeHint(QSize(200, 76))
            row = QWidget()
            row.setObjectName('articleRow')
            row.setStyleSheet('QWidget#articleRow {background:transparent;}')
            line = QHBoxLayout(row)
            line.setContentsMargins(8, 10, 8, 10)
            symbol = QLabel()
            ink = colors(self.snapshot['preferences']['palette'], self.snapshot['preferences']['mode'])['ink']
            symbol.setPixmap(icon('file', ink).pixmap(24, 24))
            symbol.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            line.addWidget(symbol)
            labels = QVBoxLayout()
            labels.setSpacing(4)
            text = ElidedLabel(article['title'])
            text.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            labels.addWidget(text)
            detail = QLabel(f"{article.get('chars', 0)} 字 · {n} 篇笔记")
            detail.setObjectName('muted')
            detail.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            labels.addWidget(detail)
            line.addLayout(labels, 1)
            for name, tip, action in [
                ('arrow-right', '阅读', lambda checked=False, a=article: self.host.open_article(a['id'])),
                ('archive', '归入主题', lambda checked=False, a=article: self.classify(a)),
                ('trash', '删除资料', lambda checked=False, a=article: self.host.delete('articles', a['id'], a['title']))]:
                control = button('', action, icon=icon(name, ink), tip=tip)
                control.setAccessibleName(tip+'：'+article['title'])
                control.setFixedSize(34, 34)
                line.addWidget(control)
            self.articles.setItemWidget(item, row)
        empty = self.articles.count() == 0
        self.empty.setText('没有找到匹配的文章。' if query.strip() else '这里还没有文章。')
        self.empty.setVisible(empty)
        self.articles.setVisible(not empty)

    def read_article(self, item):
        from PySide6.QtWidgets import QApplication
        if not QApplication.keyboardModifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
            self.host.open_article(item.data(Qt.ItemDataRole.UserRole)['id'])

    def classify(self, article):
        dialog = QDialog(self)
        dialog.setWindowTitle('归入主题')
        dialog.resize(420, 300)
        dialog.setMaximumSize(650, 560)
        layout = QVBoxLayout(dialog)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        options = QWidget()
        options.setObjectName('themeChoices')
        options.setStyleSheet('QWidget#themeChoices {background:transparent;}')
        choices = QVBoxLayout(options)
        choices.setContentsMargins(0, 0, 8, 0)
        choices.setSpacing(10)
        scroll.setWidget(options)
        options.setAutoFillBackground(False)
        scroll.viewport().setAutoFillBackground(False)
        layout.addWidget(scroll, 1)
        status = QLabel()
        status.setWordWrap(True)
        controls = []
        assigned = {m['theme_id'] for m in self.snapshot['study']['memberships'] if m['article_id'] == article['id']}
        def save(key):
            for control in controls:
                control.setEnabled(False)
            def done(_):
                from shiboken6 import isValid
                if isValid(dialog):
                    dialog.accept()
                self.host.refresh()
            def failed(_):
                from shiboken6 import isValid
                if not isValid(dialog):
                    return
                status.setText('归类未保存，请重试。')
                for control in controls:
                    control.setEnabled(control.property('available'))
            self.host.tasks.submit(lambda: self.host.service.hub.assign(key, [article['id']], 'add'), done, failed)
        for topic in self.snapshot['study']['themes']:
            caption = topic['title'] + (' · 已归入' if topic['id'] in assigned else '')
            control = button('',
                             lambda checked=False, key=topic['id']: save(key))
            control.setAccessibleName(caption)
            control.setToolTip(caption)
            control.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            caption_layout = QVBoxLayout(control)
            caption_layout.setContentsMargins(12, 8, 12, 8)
            label = QLabel(caption)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            caption_layout.addWidget(label)
            control.setProperty('available', topic['id'] not in assigned)
            control.setEnabled(topic['id'] not in assigned)
            controls.append(control)
            choices.addWidget(control)
        choices.addStretch()
        if not controls:
            status.setText('请先新建一个主题。')
        layout.addWidget(status)
        layout.addWidget(button('关闭', dialog.reject))
        dialog.exec()

    def assign(self, key, ids):
        self.host.run(lambda: self.host.service.hub.assign(key, ids, 'add'))

    def theme_form(self, checked=False, theme=None):
        old = theme or {}
        dialog = EditForm('编辑主题' if old else '新建主题',
                          [('title', '名称', 'text', old.get('title'), None),
                           ('description', '简介', 'long', old.get('description'), None)],
                          lambda body: self.host.service.hub.save('themes', {**old, **body}), self)
        if dialog.exec():
            self.host.refresh()

    def members(self):
        theme = next((t for t in self.snapshot['study']['themes'] if t['id'] == self.theme), None)
        if not theme:
            self.host.notice('请先选择一个主题。')
            return
        ids = [m['article_id'] for m in self.snapshot['study']['memberships'] if m['theme_id'] == self.theme]
        dialog = EditForm('整理文章', [('ids', theme['title'], 'multiple', ids,
                           [(a['title'], a['id']) for a in self.snapshot['articles']])],
                          lambda body: self.host.service.hub.assign(self.theme, body['ids']), self)
        if dialog.exec():
            self.host.refresh()

    def theme_menu(self, point):
        item = self.themes.itemAt(point)
        if not item:
            return
        theme = next((t for t in self.snapshot['study']['themes'] if t['id'] == item.data(Qt.ItemDataRole.UserRole)), None)
        if theme:
            menu = QMenu(self)
            menu.addAction('编辑主题', lambda: self.theme_form(theme=theme))
            menu.addAction('删除主题', lambda: self.host.delete('themes', theme['id'], theme['title']))
            menu.exec(self.themes.mapToGlobal(point))

    def article_menu(self, point):
        item = self.articles.itemAt(point)
        if item:
            article = item.data(Qt.ItemDataRole.UserRole)
            menu = QMenu(self)
            menu.addAction('阅读', lambda: self.host.open_article(article['id']))
            details = article_menu(self.host, self.host.service.store.article(article['id']), self)
            details.setTitle('资料管理')
            menu.addMenu(details)
            menu.addAction('删除文章', lambda: self.host.delete('articles', article['id'], article['title']))
            menu.exec(self.articles.mapToGlobal(point))
