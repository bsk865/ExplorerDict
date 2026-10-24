"""Searchable management rows matching the original local content inventory."""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QComboBox, QLineEdit, QScrollArea, QSizePolicy, QLabel, QStackedWidget)
from .widgets import PageComboBox as QComboBox
from .widgets import heading, button
from .navigation import icon
from .progress import label
from .dialogs import EditForm
from ..reading_preferences import colors

KINDS = [('阅读笔记', 'notes'), ('计划', 'tasks'), ('文章资料', 'articles'), ('主题分类', 'themes'),
         ('词语与图片探索', 'objects'), ('旧笔记', 'legacy_notes'), ('问答记录', 'conversations'),
         ('规划记录', 'batches'), ('思维导图', 'maps'), ('阅读记录', 'reading'),
         ('天气城市', 'weather'), ('未提交草稿', 'native_drafts'), ('阅读标注', 'annotations')]

DELETE_SCOPES = {
    'notes': '只删除这篇笔记，文章原文保留。历史回答中的引用不变。',
    'tasks': '只删除这项计划，文章和笔记保留。',
    'articles': '删除文章及其历史版本、译文、探索记录、笔记、导图和问答。相关计划保留，并移除文章关联。',
    'themes': '只删除主题分类，其中的文章和笔记保留。',
    'objects': '删除此探索对象及其笔记与问答；关联的悬浮取词词条也会删除。文章原文和原图保留。',
    'object_note': '只清除笔记正文，原文、探索对象和问答保留。',
    'session_note': '只清除笔记正文，原文、探索对象和问答保留。',
    'conversations': '清空这组问答、输入草稿及对话回顾。独立保存的笔记保留。',
    'batches': '只删除这条规划记录，已生成的计划保留。',
    'maps': '删除该版本的导图及生成记录，文章原文保留。',
    'reading': '清除阅读记录和读完标记，文章保留。',
    'weather': '移除天气城市，以后可以重新选择。',
    'native_drafts': '丢弃这份未提交草稿，已保存的内容保留。',
    'annotations': '只删除这处标注，文章原文和笔记保留。',
}


class ContentPage(QWidget):
    def __init__(self, host, inventory, kind='notes'):
        super().__init__()
        self.host, self.inventory = host, inventory
        self.route = 'content'
        self.colors = colors(host.snapshot['preferences']['palette'], host.snapshot['preferences']['mode'])
        self.rows = []
        self.detail = None
        self.list_scroll = 0
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.pages = QStackedWidget()
        outer.addWidget(self.pages)
        self.listing = QWidget()
        self.pages.addWidget(self.listing)
        layout = QVBoxLayout(self.listing)
        layout.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout()
        title_box = heading('笔记管理')
        self.title = title_box.findChild(QLabel, 'heading')
        header.addWidget(title_box, 1)
        self.add = button('', self.create, primary=True)
        header.addWidget(self.add)
        layout.addLayout(header)
        self.toolbar = QWidget()
        self.toolbar.setObjectName('contentToolbar')
        self.toolbar.setStyleSheet(f'QWidget#contentToolbar {{border-top:1px solid {self.colors["line"]};border-bottom:1px solid {self.colors["line"]};}}')
        self.filters = QGridLayout(self.toolbar)
        self.filters.setContentsMargins(0, 16, 0, 16)
        self.filters.setSpacing(16)
        self.category = QComboBox()
        for title, key in KINDS:
            self.category.addItem(f'{title} · {len(inventory.get(key, []))}', key)
        self.category.setCurrentIndex(max(0, self.category.findData(kind)))
        self.search = QLineEdit()
        self.search.setPlaceholderText('标题或内容')
        self.article = QComboBox()
        self.article.addItem('全部文章', '')
        for article in inventory.get('articles', []):
            self.article.addItem(article['title'], article['id'])
        self.filter_fields = []
        for title, control in (('内容类型', self.category), ('搜索', self.search), ('所属文章', self.article)):
            field = QWidget()
            field.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            column = QVBoxLayout(field)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(7)
            column.addWidget(label(title, size=12, muted=True))
            control.setAccessibleName(title)
            control.setMinimumWidth(0)
            control.setMinimumHeight(40)
            control.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            column.addWidget(control)
            self.filter_fields.append(field)
        layout.addWidget(self.toolbar)
        self.count = label('', size=12, muted=True)
        layout.addWidget(self.count)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.verticalScrollBar().setStyleSheet(f'''
            QScrollBar:vertical {{background:{self.colors['bg']};width:12px;margin:0;}}
            QScrollBar::handle:vertical {{background:{self.colors['line']};min-height:24px;border-radius:5px;}}
            QScrollBar::handle:vertical:hover {{background:{self.colors['muted']};}}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{height:0;}}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{background:transparent;}}
        ''')
        body = QWidget()
        body.setObjectName('page')
        self.list_layout = QVBoxLayout(body)
        self.list_layout.setContentsMargins(0, 0, 10, 0)
        self.list_layout.setSpacing(0)
        self.scroll.setWidget(body)
        layout.addWidget(self.scroll, 1)
        saved = getattr(host, 'content_filters', {})
        if isinstance(saved, dict) and saved.get('kind') == self.kind:
            self.search.setText(saved.get('query', ''))
            self.article.setCurrentIndex(max(0, self.article.findData(saved.get('article', ''))))
        self.category.currentIndexChanged.connect(self.change_category)
        self.search.textChanged.connect(self.filter)
        self.article.currentIndexChanged.connect(self.filter)
        self.populate()

    @property
    def kind(self):
        return self.category.currentData()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.reflow()

    def reflow(self):
        if not hasattr(self, 'filter_fields'):
            return
        for field in self.filter_fields:
            self.filters.removeWidget(field)
        for i in range(3):
            self.filters.setColumnStretch(i, 0)
        notes = self.kind == 'notes'
        self.filter_fields[2].setVisible(notes)
        if self.width() <= 430:
            positions = [(0, 0, 1, 1), (1, 0, 1, 1), (2, 0, 1, 1)]
        elif self.width() <= 760:
            positions = [(0, 0, 1, 1), (1, 0, 1, 2), (0, 1, 1, 1)]
        else:
            positions = [(0, 0, 1, 1), (0, 1, 1, 1), (0, 2, 1, 1)]
        for field, position in zip(self.filter_fields, positions):
            self.filters.addWidget(field, *position)
        self.filters.setColumnStretch(0, 1)
        if self.width() > 430:
            self.filters.setColumnStretch(1, 2 if self.width() > 760 else 1)
            self.filters.setColumnStretch(2, 1 if notes and self.width() > 760 else 0)

    def change_category(self):
        self.search.blockSignals(True)
        self.article.blockSignals(True)
        self.search.clear()
        self.article.setCurrentIndex(0)
        self.search.blockSignals(False)
        self.article.blockSignals(False)
        self.populate()
        if hasattr(self.host, 'content_category_changed'):
            self.host.content_category_changed(self.kind)

    def populate(self):
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        self.rows = []
        self.title.setText('笔记管理' if self.kind == 'notes' else '内容管理')
        self.add.setText({'notes':'新建笔记', 'tasks':'添加计划', 'articles':'添加资料', 'themes':'新建主题'}.get(self.kind, ''))
        self.add.setVisible(bool(self.add.text()))
        for record in self.inventory.get(self.kind, []):
            row = self.build_row(record)
            self.list_layout.addWidget(row)
            self.rows.append((record, row))
        self.empty = label('没有匹配的内容。', muted=True)
        self.empty.setContentsMargins(0, 24, 0, 24)
        self.list_layout.addWidget(self.empty)
        self.list_layout.addStretch()
        self.reflow()
        self.filter()

    def filter(self, *_):
        query = self.search.text().casefold()
        article = self.article.currentData() if self.kind == 'notes' else ''
        self.host.content_filters = {'kind':self.kind, 'query':self.search.text(), 'article':article}
        count = 0
        for record, row in self.rows:
            text = ' '.join(str(record.get(k) or '') for k in ('title', 'content', 'request', 'article_title', 'description', 'search_text'))
            match = query in text.casefold() and (not article or record.get('article_id') == article)
            row.setVisible(match)
            count += match
        self.count.setText(f'{count} 项')
        self.empty.setVisible(not count)

    def build_row(self, record):
        row = QWidget()
        row.setObjectName('contentRow')
        row.setStyleSheet(f'QWidget#contentRow {{border-bottom:1px solid {self.colors["line"]};}}')
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 18, 0, 18)
        layout.setSpacing(16)
        copy = QVBoxLayout()
        copy.setSpacing(7)
        title = record.get('title') or record.get('article_title') or record.get('request') or '阅读笔记'
        name = label(title, size=15)
        font = name.font()
        font.setBold(True)
        name.setFont(font)
        copy.addWidget(name)
        detail = record.get('article_title') or ''
        if self.kind == 'notes':
            detail += ' · ' + ('小探整理' if record.get('origin') == 'assistant_confirmed' else '我的心得')
        elif self.kind == 'tasks':
            detail = f"{record.get('date') or '日期待定'} {record.get('time') or ''} · {'已完成' if record['status'] == 'done' else '待完成'}"
        elif self.kind == 'conversations':
            detail = f"{record.get('count', 0)} 条消息 · {record.get('archives', 0)} 段先前对话"
        elif self.kind == 'batches':
            detail = f"{len(record.get('tasks', []))} 项安排 · {'已撤销' if record.get('undone') else '已创建'}"
        if detail:
            copy.addWidget(label(detail, size=12, muted=True))
        if record.get('content'):
            copy.addWidget(label(record['content'][:160], muted=True))
        for i in range(copy.count()):
            copy.itemAt(i).widget().setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addLayout(copy, 1)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        if self.kind in {'notes', 'tasks', 'themes', 'legacy_notes'}:
            actions.addWidget(button('编辑', lambda: self.edit(record)))
        elif self.kind in {'conversations', 'batches', 'annotations', 'objects'}:
            actions.addWidget(button('', lambda: self.details(record), icon=icon('file', self.colors['muted']), tip='查看内容'))
        if self.kind == 'native_drafts':
            actions.addWidget(button('继续编辑', lambda: self.host.resume_draft(record)))
        elif self.source(record):
            actions.addWidget(button('阅读' if self.kind == 'articles' else '', lambda: self.open_source(record),
                                     icon=icon('book', self.colors['muted']), tip='查看原文'))
        actions.addWidget(button('', lambda: self.remove(record), icon=icon('trash', self.colors['muted']), tip='删除'))
        for i in range(actions.count()):
            control = actions.itemAt(i).widget()
            control.setFixedHeight(36)
            if not control.text():
                control.setFixedWidth(36)
            control.setAccessibleName(control.text() or control.toolTip())
        layout.addLayout(actions)
        return row

    def source(self, record):
        return record.get('id') if self.kind in {'articles', 'maps', 'reading'} else record.get('article_id')

    def open_source(self, record):
        key = self.source(record)
        if key:
            if record.get('object_id'):
                self.host.open_article(key, block=record.get('block'), object_id=record['object_id'])
            else:
                self.host.open_article(key, block=record.get('block'))

    def remove(self, record):
        kind = record['source'] + '_note' if self.kind == 'legacy_notes' else self.kind
        self.host.delete(kind, record['id'], record.get('title') or '选中内容')

    def create(self):
        if self.kind == 'notes':
            self.host.edit_note()
        elif self.kind == 'tasks':
            self.host.edit_task()
        elif self.kind == 'articles':
            self.host.import_material()
        elif self.kind == 'themes':
            self.edit({})

    def edit(self, record):
        if self.kind == 'notes':
            self.host.edit_note(record)
            return
        if self.kind == 'tasks':
            self.host.edit_task(record)
            return
        if self.kind == 'themes':
            dialog = EditForm('编辑主题' if record else '新建主题',
                              [('title', '名称', 'text', record.get('title'), None),
                               ('description', '简介', 'long', record.get('description'), None)],
                              lambda body: self.host.service.hub.save('themes', {**record, **body}), self)
        else:
            def save(body):
                if len(body['content']) > 10000:
                    raise ValueError('内容超过10000字，请精简后保存。')
                store = self.host.service.store
                update = store.update_object if record['source'] == 'object' else store.update_session
                update(record['id'], {'note':body['content']})
            dialog = EditForm('编辑已有笔记', [('content', '正文', 'long', record.get('content'), None)], save, self)
        if dialog.exec():
            self.host.refresh()

    def details(self, record):
        from .content_detail import ContentDetail
        self.back_to_list()
        self.list_scroll = self.scroll.verticalScrollBar().value()
        self.detail = ContentDetail(self, record)
        self.pages.addWidget(self.detail)
        self.pages.setCurrentWidget(self.detail)

    def back_to_list(self):
        if self.detail is None:
            return
        old, self.detail = self.detail, None
        old.hide()
        self.pages.setCurrentWidget(self.listing)
        self.pages.removeWidget(old)
        old.deleteLater()
        position = self.list_scroll
        QTimer.singleShot(0, self, lambda: self.scroll.verticalScrollBar().setValue(position))

    def active_scroll(self):
        return (self.detail.browser if self.detail else self.scroll).verticalScrollBar()
