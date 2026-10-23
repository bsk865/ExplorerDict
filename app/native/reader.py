"""Native original-text rendering and version-bound annotations."""
from bs4 import BeautifulSoup
from PySide6.QtCore import Qt, QUrl, Signal, QTimer, QMimeData, QEvent, QSize
from PySide6.QtGui import QTextCursor, QTextCharFormat, QTextBlockFormat, QColor, QImage, QTextImageFormat, QPixmap, QIcon
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QSplitter,
                              QListWidget, QListWidgetItem, QLabel, QTabWidget, QMenu, QApplication, QGridLayout, QDialog, QTabBar, QScrollArea)
from ..reading_annotations import block_text
from .widgets import button, heading
from .dialogs import EditForm
from .chat import ChatPanel
from .notes import NotesPanel
from .materials import article_menu, article_info
from .mathtext import prepare_math
from ..reading_preferences import colors
from ..paths import resource_root


def utf16_length(value):
    return len(value.encode('utf-16-le')) // 2


def python_offset(value, units):
    return len(value.encode('utf-16-le')[:units * 2].decode('utf-16-le', errors='ignore'))


def source_spans(source, rendered, start, atoms=None):
    """Map source characters to Qt positions, allowing HTML layout whitespace."""
    spans = []
    index, position = 0, start
    def space(char):
        return char.isspace() or char in '\ufdd0\ufdd1'
    source_index = 0
    while source_index < len(source):
        char = source[source_index]
        if atoms and source_index in atoms:
            while index < len(rendered) and space(rendered[index]):
                position += utf16_length(rendered[index])
                index += 1
            if index >= len(rendered) or rendered[index] != '\ufffc':
                return None
            end = atoms[source_index]
            spans.extend([(position, position+1)] * (end-source_index))
            source_index = end
            position += 1
            index += 1
            continue
        if not space(char):
            while index < len(rendered) and space(rendered[index]):
                position += utf16_length(rendered[index])
                index += 1
        if index < len(rendered) and (char == rendered[index] or
                                      (space(char) and space(rendered[index]))):
            end = position + utf16_length(rendered[index])
            spans.append((position, end))
            position = end
            index += 1
        elif space(char):
            spans.append((position, position))
        else:
            return None
        source_index += 1
    return spans if all(space(char) for char in rendered[index:]) else None


class OriginalText(QTextEdit):
    imageClicked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setObjectName('originalText')
        self.ranges = []
        self.anchors = {}
        self.images = []
        self.formula_text = {}
        self.formula_html = {}
        self.image_press = None
        self.image_layout = QTimer(self)
        self.image_layout.setSingleShot(True)
        self.image_layout.setInterval(16)
        self.image_layout.timeout.connect(self.resize_images)
        self.setStyleSheet('QTextEdit {font-size:16px; padding:18px; border:0;}')

    def load_article(self, article, assets, ink='#302d29'):
        self.clear()
        self.ranges = []
        self.anchors = {}
        self.images = []
        self.formula_text = {}
        self.formula_html = {}
        cursor = self.textCursor()
        for block in article['blocks']:
            self.anchors[block['id']] = cursor.position()
            cursor.setCharFormat(QTextCharFormat())
            paragraph = QTextBlockFormat()
            paragraph.setBottomMargin(18)
            paragraph.setLineHeight(165, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
            cursor.setBlockFormat(paragraph)
            if block['type'] == 'text':
                text = block_text(block)
                start = cursor.position()
                spans = None
                atoms = {}
                if block.get('html'):
                    html, atoms = prepare_math(block['html'], self.document(), ink, self.formula_html)
                    cursor.beginEditBlock()
                    cursor.insertHtml(html)
                    probe = QTextCursor(cursor)
                    probe.setPosition(start, QTextCursor.MoveMode.KeepAnchor)
                    spans = source_spans(text, probe.selectedText(), start, atoms)
                    cursor.endEditBlock()
                    if spans is None:
                        self.document().undo(cursor)
                if spans is None:
                    cursor.insertText(text)
                    spans = source_spans(text, text, start)
                    atoms = {}
                for a, b in atoms.items():
                    self.formula_text[spans[a][0]] = text[a:b]
                self.ranges.append({'id': block['id'], 'text': text, 'start': start,
                                    'end': cursor.position(), 'spans': spans})
            elif block['type'] == 'image':
                image = QImage.fromData(assets.get(block.get('asset'), b''))
                if not image.isNull():
                    url = QUrl('asset:' + block['asset'])
                    self.document().addResource(2, url, image)
                    fmt = QTextImageFormat()
                    fmt.setName(url.toString())
                    fmt.setAnchor(True)
                    fmt.setAnchorHref('native-image:' + block['id'])
                    width = min(image.width(), self.image_width())
                    fmt.setWidth(width)
                    fmt.setHeight(image.height() * width / image.width())
                    self.images.append((cursor.position(), image.width(), image.height()))
                    cursor.insertImage(fmt)
                    cursor.setCharFormat(QTextCharFormat())
                cursor.insertBlock()
                cursor.insertText(block.get('caption', ''))
            cursor.insertBlock()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        self.setTextCursor(cursor)

    def createMimeDataFromSelection(self):
        original = super().createMimeDataFromSelection()
        cursor = self.textCursor()
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        if not any(start <= position < end for position in self.formula_text):
            return original
        mime = QMimeData()
        for kind in original.formats():
            mime.setData(kind, original.data(kind))
        text, position = [], start
        for char in cursor.selectedText():
            text.append(self.formula_text.get(position, '\n' if char in '\u2028\u2029\ufdd0\ufdd1' else char))
            position += utf16_length(char)
        mime.setText(''.join(text))
        if mime.hasHtml():
            html = BeautifulSoup(mime.html(), 'html.parser')
            for image in html.find_all('img'):
                source = self.formula_html.get(image.get('src'))
                if source:
                    image.replace_with(BeautifulSoup(source, 'html.parser').math)
            mime.setHtml(str(html))
        return mime

    def image_width(self):
        return max(1, self.viewport().width() - 2*self.document().documentMargin() - 2)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'image_layout'):
            self.image_layout.start()

    def resize_images(self):
        available = self.image_width()
        for position, width, height in self.images:
            cursor = QTextCursor(self.document())
            cursor.setPosition(position)
            cursor.movePosition(QTextCursor.MoveOperation.NextCharacter, QTextCursor.MoveMode.KeepAnchor)
            fmt = cursor.charFormat().toImageFormat()
            target = min(width, available)
            if abs(fmt.width() - target) > .5:
                fmt.setWidth(target)
                fmt.setHeight(height * target / width)
                cursor.setCharFormat(fmt)

    def mousePressEvent(self, event):
        self.image_press = (event.position().toPoint(), self.anchorAt(event.position().toPoint())) if event.button() == Qt.MouseButton.LeftButton else None
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        pressed = self.image_press
        self.image_press = None
        super().mouseReleaseEvent(event)
        if pressed and event.button() == Qt.MouseButton.LeftButton:
            point, anchor = pressed
            released = event.position().toPoint()
            if (anchor.startswith('native-image:') and anchor == self.anchorAt(released)
                    and (released-point).manhattanLength() < QApplication.startDragDistance()):
                self.imageClicked.emit(anchor.removeprefix('native-image:'))

    def selected_segments(self):
        cursor = self.textCursor()
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        result = []
        for block in self.ranges:
            left, right = max(start, block['start']), min(end, block['end'])
            if left < right:
                indices = [i for i, (a, b) in enumerate(block['spans'])
                           if a < right and b > left and a < b]
                if indices:
                    a, b = indices[0], indices[-1] + 1
                    result.append({'block': block['id'], 'start': a, 'end': b, 'quote': block['text'][a:b]})
        return result

    def annotate(self, items):
        selections = []
        for mark in items:
            for segment in mark['segments']:
                block = next((b for b in self.ranges if b['id'] == segment['block']), None)
                if not block:
                    continue
                spans = [(a, b) for a, b in block['spans'][segment['start']:segment['end']] if a < b]
                if not spans:
                    continue
                selection = QTextEdit.ExtraSelection()
                selection.cursor = QTextCursor(self.document())
                selection.cursor.setPosition(spans[0][0])
                selection.cursor.setPosition(spans[-1][1], QTextCursor.MoveMode.KeepAnchor)
                if mark['style'] == 'underline':
                    selection.format.setFontUnderline(True)
                    selection.format.setUnderlineColor(QColor('#b27448'))
                else:
                    selection.format.setBackground(QColor({'yellow': '#f5de87', 'green': '#b8dfbf', 'pink': '#efbfd0'}[mark['style']]))
                    selection.format.setForeground(QColor('#332e28'))
                selections.append(selection)
        self.setExtraSelections(selections)

    def locate(self, block_id):
        position = self.anchors.get(block_id)
        if position is not None:
            cursor = self.textCursor()
            cursor.setPosition(position)
            self.setTextCursor(cursor)
            self.ensureCursorVisible()


class ReaderPage(QWidget):
    def __init__(self, host, bundle):
        super().__init__()
        self.host = host
        self.article = bundle['article']
        self.bundle = bundle
        self.focus_block = None
        self.focus_object = None
        self.resume_assistant = host.width() >= 1000
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        bar.addWidget(button('我的文章', lambda: host.navigate('shelf')))
        bar.addStretch()
        bar.addWidget(button('原文', lambda: self.text.setFocus()))
        bar.addWidget(button('导图', lambda: host.navigate('map', self.article['id'])))
        bar.addWidget(button('笔记', self.show_notes))
        self.assistant_toggle = button('问本文', self.toggle_assistant)
        bar.addWidget(self.assistant_toggle)
        material = button('文章操作')
        material.setMenu(article_menu(host, self.article, self))
        readings = host.snapshot.get('activity', {}).get('readings', [])
        self.completed = next((r.get('completed', False) for r in readings if r['article_id'] == self.article['id']), False)
        self.read_complete = button('已读完' if self.completed else '标记读完', self.toggle_complete)
        bar.addWidget(self.read_complete)
        layout.addLayout(bar)
        count = sum(block['type'] == 'image' for block in self.article['blocks'])
        title_row = QHBoxLayout()
        title_row.addWidget(heading(self.article['title'], ('网页文章' if self.article.get('source_url') else '我的资料') + f' · {count} 张图片'), 1)
        title_row.addWidget(button('资料信息', lambda: article_info(host, self.article, self)))
        layout.addLayout(title_row)
        self.selection_bar = QWidget(self)
        self.selection_layout = QGridLayout(self.selection_bar)
        self.selection_layout.setContentsMargins(8, 8, 8, 8)
        self.selection_layout.setSpacing(6)
        self.mark_buttons = []
        for style, label, color in [('yellow', '黄色荧光', '#eac951'), ('green', '绿色荧光', '#8bc69a'),
                                    ('pink', '粉色荧光', '#da9bb3'), ('underline', '下划线', None)]:
            b = button('U' if style == 'underline' else '', lambda checked=False, s=style: self.save_mark(s), tip=label)
            b.setAccessibleName(label)
            b.setFixedSize(36, 34)
            if color:
                b.setStyleSheet(f'background:{color}; border:1px solid #aa9a87; border-radius:5px;')
            self.mark_buttons.append(b)
        self.selection_note = button('记心得', self.note)
        self.selection_explore = button('理解这一处', self.explore)
        self.selection_close = button('×', self.dismiss_selection, tip='收起标注工具')
        self.selection_close.setFixedWidth(34)
        self.selection_controls = self.mark_buttons + [self.selection_note, self.selection_explore, self.selection_close]
        self.mark_row = QWidget()
        mark_layout = QHBoxLayout(self.mark_row)
        mark_layout.setContentsMargins(0, 0, 0, 0)
        mark_layout.setSpacing(6)
        for control in self.mark_buttons:
            mark_layout.addWidget(control)
        mark_layout.addStretch()
        self.selection_actions = QWidget()
        action_layout = QHBoxLayout(self.selection_actions)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(6)
        for control in (self.selection_note, self.selection_explore, self.selection_close):
            action_layout.addWidget(control)
        self.splitter = QSplitter()
        self.text = OriginalText()
        preference = host.snapshot['preferences']
        ink = colors(preference['palette'], preference['mode']).get('ink', '#302d29')
        palette = colors(preference['palette'], preference['mode'])
        self.selection_bar.setObjectName('selectionTools')
        self.selection_bar.setStyleSheet(f"QWidget#selectionTools {{background:{palette['bg']}; border:1px solid {palette['line']}; border-radius:6px;}}")
        self.selection_bar.setParent(self.text.viewport())
        self.text.viewport().installEventFilter(self)
        self.text.installEventFilter(self)
        self.text.verticalScrollBar().valueChanged.connect(self.selection_bar.hide)
        self.text.load_article(self.article, bundle.get('assets', {}), ink)
        self.text.annotate(bundle['annotations']['items'])
        self.text.selectionChanged.connect(self.selection_changed)
        self.paper = QWidget()
        paper_layout = QVBoxLayout(self.paper)
        paper_layout.setContentsMargins(16, 0, 16, 16)
        paper_layout.setSpacing(16)
        paper_tools = QHBoxLayout()
        source_label = '论文摘要 · 非全文' if self.article.get('source_status') == 'abstract' else '原文' if self.article.get('source_status') == 'readable' else '历史提取内容' if self.article.get('origin') == 'legacy' else '自编示例' if self.article.get('origin') == 'demo' else '本机文章'
        source = QLabel(source_label)
        source.setObjectName('muted')
        paper_tools.addWidget(source)
        paper_tools.addStretch()
        from .find import FindBar
        self.find_bar = FindBar(self.text, self)
        paper_tools.addWidget(button('查找', self.find_bar.open, tip='文内查找 · Ctrl+F'))
        self.marks_toggle = button(f"标注 {len(bundle['annotations']['items'])}", lambda: self.show_records('marks'))
        paper_tools.addWidget(self.marks_toggle)
        paper_tools.addWidget(material)
        paper_layout.addLayout(paper_tools)
        paper_layout.addWidget(self.find_bar)
        paper_layout.addWidget(self.text, 1)
        self.paper.setObjectName('readingPaper')
        self.paper.setStyleSheet(f"QWidget#readingPaper {{background:{palette.get('soft', palette['bg'])}; border-radius:8px;}}")
        self.text.setStyleSheet(f"QTextEdit {{font-size:14px; padding:24px; border:1px solid {palette['line']}; border-radius:4px; background:{palette['bg']};}}")
        self.splitter.addWidget(self.paper)
        self.tabs = QTabWidget()
        self.chat = ChatPanel(self)
        self.text.imageClicked.connect(self.open_image)
        self.tabs.addTab(self.chat, '理解与追问')
        self.notes = NotesPanel(self)
        self.tabs.addTab(self.notes, '我的笔记')
        marks = QListWidget()
        for mark in bundle['annotations']['items']:
            item = QListWidgetItem(' · '.join(s['quote'] for s in mark['segments'])[:180], marks)
            item.setData(Qt.ItemDataRole.UserRole, mark)
        marks.itemClicked.connect(lambda item: self.text.locate(item.data(Qt.ItemDataRole.UserRole)['segments'][0]['block']))
        marks.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        marks.customContextMenuRequested.connect(lambda point: self.mark_menu(marks, point))
        self.marks = marks
        marks.setParent(self)
        marks.hide()
        objects = QListWidget()
        for obj in self.article['objects']:
            item = QListWidgetItem(obj['title'], objects)
            item.setData(Qt.ItemDataRole.UserRole, obj)
        objects.itemClicked.connect(lambda item: self.choose_object(item.data(Qt.ItemDataRole.UserRole)))
        self.objects = objects
        objects.setParent(self)
        objects.hide()
        self.assistant = QWidget()
        assistant_layout = QVBoxLayout(self.assistant)
        assistant_layout.setContentsMargins(12, 12, 12, 8)
        assistant_layout.setSpacing(10)
        assistant_heading = QHBoxLayout()
        portrait = QLabel()
        portrait.setPixmap(QPixmap(str(resource_root() / 'web/assets/generated' / (preference['palette']+'-xiaotan-avatar.webp'))).scaled(
            37, 37, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        portrait.setFixedSize(37, 37)
        assistant_heading.addWidget(portrait)
        assistant_title = QLabel('小探，陪你读懂')
        assistant_title.setStyleSheet('font-size:14px; font-weight:500;')
        assistant_heading.addWidget(assistant_title, 1)
        close_assistant = button('×', lambda: self.set_assistant_visible(False), tip='收起小探')
        close_assistant.setFixedSize(28, 28)
        close_assistant.setStyleSheet('padding:0;')
        assistant_heading.addWidget(close_assistant)
        assistant_layout.addLayout(assistant_heading)
        assistant_layout.addWidget(self.tabs, 1)
        self.splitter.addWidget(self.assistant)
        self.splitter.setSizes([760, 300])
        self.splitter.setCollapsible(0, False)
        self.splitter.setCollapsible(1, False)
        layout.addWidget(self.splitter, 1)
        self.assistant_visible = False
        self.assistant.hide()
        self.selection_changed()
        QTimer.singleShot(0, self.restore_view)

    def save_view(self):
        self.notes.controller.flush()
        self.host.service.store.put('settings', 'native-reading:' + self.article['id'],
                                    {'scroll': self.text.verticalScrollBar().value(),
                                     'split': self.splitter.sizes(), 'tab': self.tabs.currentIndex()})

    def restore_view(self):
        value = self.host.service.store.get('settings', 'native-reading:' + self.article['id'], {})
        route = getattr(self, 'route_view', None)
        if route is not None:
            value = route
            self.chat.set_focus(route.get('object') or '')
            self.resume_assistant = route['assistant']
        if value.get('split'):
            self.splitter.setSizes(value['split'])
        self.tabs.setCurrentIndex(value.get('tab', 0))
        self.text.verticalScrollBar().setValue(value.get('scroll', 0))
        self.set_assistant_visible(self.resume_assistant)
        if self.focus_block:
            self.text.locate(self.focus_block)
        if self.focus_object:
            self.chat.set_focus(self.focus_object)
            self.tabs.setCurrentWidget(self.chat)
            self.set_assistant_visible(True)
        if getattr(self, 'focus_note', None):
            draft = next((d for d in self.notes.controller.drafts if d.body['draft_id'] == self.focus_note), None)
            if draft:
                self.notes.controller.select(draft.body)
                self.show_notes()
        if getattr(self, 'focus_saved_note', None):
            note = next((n for n in self.notes.controller.items if n['id'] == self.focus_saved_note), None)
            if note:
                self.notes.controller.select(note)
                self.show_notes()
            else:
                self.host.notice('这条笔记已删除，回答中的引用仍保留。')

    def set_assistant_visible(self, visible):
        self.assistant_visible = visible
        self.assistant.setVisible(visible)
        self.assistant_toggle.setText('收起小探' if visible else '问本文')
        if visible:
            saved = self.host.service.store.get('settings', 'native-reading:' + self.article['id'], {}).get('split', [])
            self.splitter.setSizes(saved if len(saved) == 2 and min(saved) > 0 else [760, 300])
        self.selection_bar.hide()

    def toggle_assistant(self):
        self.set_assistant_visible(not self.assistant_visible)

    def toggle_complete(self):
        completed = not self.completed
        def saved(value):
            self.completed = value['completed']
            self.read_complete.setText('已读完' if self.completed else '标记读完')
        self.host.run(lambda: self.host.service.activity.record(self.article['id'], completed), saved)

    def show_notes(self):
        self.set_assistant_visible(True)
        self.tabs.setCurrentWidget(self.notes)

    def show_records(self, kind):
        if kind == 'marks':
            self.show_marks()
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('标注' if kind == 'marks' else '探索记录')
        dialog.resize(560, 440)
        layout = QVBoxLayout(dialog)
        rows = QListWidget()
        original = self.marks if kind == 'marks' else self.objects
        if kind == 'objects':
            filters = QTabBar()
            for label in ('全部', '关键词', '图片'):
                filters.addTab(label)
            layout.addWidget(filters)
            rows.setIconSize(QSize(55, 55))
            rows.setWordWrap(True)
            def populate(index=0):
                rows.clear()
                for obj in self.article['objects']:
                    if index == 1 and obj['type'] != 'term' or index == 2 and obj['type'] == 'term':
                        continue
                    kind_label = {'term': '关键词', 'region': '图片局部', 'image': '整张图片'}[obj['type']]
                    item = QListWidgetItem(obj['title'] + '\n' + kind_label + (' · 已收藏' if obj.get('saved') else ''), rows)
                    item.setData(Qt.ItemDataRole.UserRole, obj)
                    if obj.get('asset'):
                        picture = QPixmap()
                        picture.loadFromData(self.bundle['assets'].get(obj['asset'], b''))
                        if not picture.isNull():
                            item.setIcon(QIcon(picture))
                empty.setVisible(rows.count() == 0)
            empty = QLabel('在原文中选词或点图，开始探索。')
            empty.setObjectName('muted')
            filters.currentChanged.connect(populate)
            populate()
            layout.addWidget(empty)
        else:
            for index in range(original.count()):
                rows.addItem(original.item(index).clone())
        if kind == 'marks':
            def locate(item):
                self.text.locate(item.data(Qt.ItemDataRole.UserRole)['segments'][0]['block'])
                dialog.accept()
            rows.itemClicked.connect(locate)
            rows.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            rows.customContextMenuRequested.connect(lambda point: self.mark_menu(rows, point))
        else:
            def choose(item):
                self.choose_object(item.data(Qt.ItemDataRole.UserRole))
                dialog.accept()
            rows.itemClicked.connect(choose)
        layout.addWidget(rows)
        layout.addWidget(button('关闭', dialog.close))
        dialog.exec()

    def show_marks(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('原文标注')
        dialog.resize(560, 440)
        layout = QVBoxLayout(dialog)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        layout.addWidget(scroll)
        layout.addWidget(button('关闭', dialog.close))
        items = list(self.bundle['annotations']['items'])
        styles = [('yellow', '黄色荧光', '#eac951'), ('green', '绿色荧光', '#8bc69a'),
                  ('pink', '粉色荧光', '#da9bb3'), ('underline', '下划线', None)]

        def populate():
            body = QWidget()
            body.setObjectName('page')
            column = QVBoxLayout(body)
            column.setContentsMargins(12, 12, 12, 12)
            column.setSpacing(20)
            if not items:
                column.addWidget(QLabel('还没有标注。'))
            for mark in items:
                row = QWidget()
                group = QVBoxLayout(row)
                group.setContentsMargins(0, 0, 0, 12)
                group.setSpacing(10)
                label = QLabel(dict((s, title) for s, title, _ in styles)[mark['style']])
                label.setObjectName('muted')
                group.addWidget(label)
                quote = QLabel('\n'.join(segment['quote'] for segment in mark['segments']))
                quote.setTextFormat(Qt.TextFormat.PlainText)
                quote.setWordWrap(True)
                group.addWidget(quote)
                actions = QHBoxLayout()
                actions.setSpacing(6)
                for style, title, color in styles:
                    control = button('U' if style == 'underline' else '',
                                     lambda checked=False, m=mark, s=style: change(m, s), tip='改为'+title)
                    control.setAccessibleName('改为'+title)
                    control.setFixedSize(32, 32)
                    control.setCheckable(True)
                    control.setChecked(mark['style'] == style)
                    if color:
                        control.setStyleSheet(f'QPushButton {{background:{color}; border:1px solid #aa9a87; border-radius:5px;}}'
                                             'QPushButton:checked {border:2px solid #514b46;}')
                    actions.addWidget(control)
                actions.addStretch()
                def locate(checked=False, m=mark):
                    self.text.locate(m['segments'][0]['block'])
                    dialog.accept()
                actions.addWidget(button('定位原文', locate))
                actions.addWidget(button('删除', lambda checked=False, m=mark: change(m, None), tip='删除标注'))
                group.addLayout(actions)
                column.addWidget(row)
            column.addStretch()
            old = scroll.takeWidget()
            scroll.setWidget(body)
            if old:
                old.deleteLater()

        def change(mark, style):
            scroll.setEnabled(False)
            def saved(value):
                items[:] = [value if item['id'] == mark['id'] else item for item in items] if style else [item for item in items if item['id'] != mark['id']]
                self.bundle['annotations']['items'] = list(items)
                self.marks_toggle.setText(f'标注 {len(items)}')
                self.text.annotate(items)
                populate()
                scroll.setEnabled(True)
            self.host.run(lambda: self.host.service.annotations.save(dict(mark, style=style)) if style else
                          self.host.service.annotations.delete({'article_id': self.article['id'], 'id': mark['id']}), saved)

        def failed(_):
            scroll.setEnabled(True)
        self.host.tasks.error.connect(failed)
        populate()
        try:
            dialog.exec()
        finally:
            self.host.tasks.error.disconnect(failed)

    def choose_object(self, obj):
        self.text.locate(obj['block'])
        self.chat.set_focus(obj['id'])
        self.tabs.setCurrentWidget(self.chat)
        self.set_assistant_visible(True)

    def open_image(self, block_id):
        obj = next((obj for obj in self.article['objects']
                    if obj['type'] == 'image' and obj['block'] == block_id), None)
        if obj:
            self.choose_object(obj)
            self.chat.open_image()

    def selection_changed(self):
        if hasattr(self, 'find_bar') and not self.find_bar.isHidden() and self.find_bar.query.hasFocus():
            self.selection_bar.hide()
            return
        segments = self.text.selected_segments()
        enabled = bool(segments)
        for b in self.mark_buttons:
            b.setEnabled(enabled)
        if not enabled:
            self.selection_bar.hide()
            return
        self.selection_note.setVisible(len(segments) == 1)
        self.selection_explore.setVisible(len(segments) == 1 and len(segments[0]['quote']) <= 200)
        while self.selection_layout.count():
            self.selection_layout.takeAt(0)
        narrow = self.text.viewport().width() < 470
        self.selection_layout.addWidget(self.mark_row, 0, 0)
        self.selection_layout.addWidget(self.selection_actions, 1 if narrow else 0, 0 if narrow else 1)
        self.selection_bar.adjustSize()
        rect = self.text.cursorRect()
        width, height = self.selection_bar.width(), self.selection_bar.height()
        x = max(4, min(rect.x(), self.text.viewport().width()-width-4))
        y = rect.bottom()+6
        if y+height > self.text.viewport().height():
            y = max(4, rect.top()-height-6)
        self.selection_bar.move(x, y)
        self.selection_bar.show()
        self.selection_bar.raise_()

    def dismiss_selection(self):
        cursor = self.text.textCursor()
        cursor.clearSelection()
        self.text.setTextCursor(cursor)
        self.selection_bar.hide()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Resize:
            self.selection_bar.hide()
        elif event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            self.dismiss_selection()
            return True
        return super().eventFilter(watched, event)

    def save_mark(self, style):
        body = {'article_id': self.article['id'], 'version': self.article['version'],
                'style': style, 'segments': self.text.selected_segments()}
        host = self.host
        host.run(lambda: host.service.annotations.save(body))

    def mark_menu(self, widget, point):
        item = widget.itemAt(point)
        if item:
            mark = item.data(Qt.ItemDataRole.UserRole)
            menu = QMenu(self)
            for style, label in [('yellow', '黄色荧光'), ('green', '绿色荧光'), ('pink', '粉色荧光'), ('underline', '下划线')]:
                host = self.host
                menu.addAction(label, lambda checked=False, s=style: host.run(
                    lambda: host.service.annotations.save(dict(mark, style=s))))
            menu.addSeparator()
            menu.addAction('删除标注', lambda: self.host.delete('annotations', mark['id'], '这条标注'))
            menu.exec(widget.mapToGlobal(point))

    def note(self):
        segments = self.text.selected_segments()
        anchor = segments[0] if segments else {}
        self.notes.start({'block': anchor.get('block', ''), 'quote': anchor.get('quote', '')})
        self.show_notes()

    def explore(self):
        segments = self.text.selected_segments()
        if len(segments) != 1 or not 0 < len(segments[0]['quote']) <= 200:
            self.host.notice('请在一个段落内选择200字以内的词句。')
            return
        body = {'type': 'term', 'block': segments[0]['block'], 'title': segments[0]['quote']}
        host, aid = self.host, self.article['id']
        def created(obj):
            if host.current_page == 'reader' and host.article_id == aid:
                host.open_article(aid, remember=False, object_id=obj['id'])
            else:
                host.notice('探索对象已保存')
        host.run(lambda: host.service.store.object(aid, body), created)
