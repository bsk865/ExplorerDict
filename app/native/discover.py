"""Themed discovery feed. Async work belongs to the window, never a transient page."""
from datetime import datetime
from hashlib import sha256
import re
import time
from PySide6.QtCore import QObject, Signal, Qt, QTimer, QUrl
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                              QScrollArea, QStackedWidget, QButtonGroup, QSizePolicy)
from .widgets import button, heading, FlowLayout, PageComboBox
from ..discovery import canonical, stamp, TOPICS, SOURCES
from ..reading_preferences import colors
from .external_links import open_source, copy_link


def label(text, muted=False):
    value = QLabel(text)
    value.setTextFormat(Qt.TextFormat.PlainText)
    value.setWordWrap(True)
    value.setMinimumWidth(0)
    value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    if muted:
        value.setObjectName('muted')
    return value


class DiscoveryController(QObject):
    changed = Signal()

    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.service = host.service.discovery
        self.busy = False
        self.message = ''
        self.kind = 'all'
        self.query = ''
        self.limit = 20
        self.cover_loading = False
        self.cover_attempts = set()

    def maybe_refresh(self):
        if (self.host.discovery_updates and not self.host.closing and self.service.preferences()['automatic']
                and time.time() - self.service.state().get('attempted', 0) > 6*3600):
            self.refresh()

    def work(self, action, message, success):
        if self.busy or self.host.closing:
            return
        self.busy, self.message = True, message
        self.changed.emit()
        def done(value):
            self.busy = False
            self.message = success(value)
            self.changed.emit()
        def failed(message):
            self.busy = False
            self.message = message
            self.changed.emit()
        self.host.tasks.submit(action, done, failed)

    def refresh(self):
        self.cover_attempts.clear()
        self.work(self.service.refresh, '正在更新订阅，已有内容仍可阅读…',
                  lambda state: '；'.join(state.get('errors', [])) or '订阅内容已更新')

    def load_covers(self, items):
        if (self.busy or self.cover_loading or self.host.closing or not self.host.discovery_updates
                or self.host.current_page != 'discover'):
            return
        keys = [row['id'] for row in items if row.get('cover_url') and not row.get('cover_file')
                and row['id'] not in self.cover_attempts][:12]
        if not keys:
            return
        self.cover_attempts.update(keys)
        self.cover_loading = True
        def done(_):
            self.cover_loading = False
            self.changed.emit()
        self.host.tasks.submit(lambda: self.service.cache_covers(keys), done, done)

    def add(self, url):
        try:
            url = canonical(url)
        except ValueError as exc:
            self.message = str(exc)
            self.changed.emit()
            return
        self.work(lambda: self.service.add_link(url), '正在读取文章信息和来源封面…',
                  lambda item: '已收集「'+item['title']+'」，可在“收集”中查看')

    def collect(self, item, mode='full'):
        self.work(lambda: self.service.import_item(item['id'], mode=mode),
                  '正在下载并提取全文…' if mode == 'full' else '正在保存摘要…',
                  lambda article: '全文已加入资料库，可打开阅读和问答。' if article.get('source_status') != 'abstract'
                  else '已单独保存摘要，未取得全文。')

    def flag(self, item, name, value):
        self.service.flag(item['id'], name, value)
        self.changed.emit()


class DiscoverPage(QWidget):
    def __init__(self, host):
        super().__init__(host)
        self.host, self.controller = host, host.discovery
        self.service = self.controller.service
        self.detail_id = None
        self.palette = colors(host.snapshot['preferences']['palette'], host.snapshot['preferences']['mode'])
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 12)
        top = QHBoxLayout()
        top.addWidget(heading('发现', '从你的好奇心出发，遇见新的内容'), 1)
        self.refresh_button = button('更新内容', self.controller.refresh)
        top.addWidget(self.refresh_button)
        top.addWidget(button('兴趣与来源', self.show_preferences))
        outer.addLayout(top)
        self.status = label('', True)
        outer.addWidget(self.status)
        self.stack = QStackedWidget(self)
        outer.addWidget(self.stack, 1)
        self.feed = QWidget(self.stack)
        feed_layout = QVBoxLayout(self.feed)
        feed_layout.setContentsMargins(0, 0, 0, 0)
        tabs = FlowLayout(spacing=8)
        self.group = QButtonGroup(self)
        self.tabs = {}
        for key, text in [('all', '推荐'), ('news', '新闻'), ('paper', '论文'), ('article', '收集'), ('saved', '收藏')]:
            control = button(text, lambda checked=False, key=key: self.filter_kind(key))
            control.setCheckable(True)
            control.setChecked(self.controller.kind == key)
            control.setObjectName('nav')
            self.group.addButton(control)
            self.tabs[key] = control
            tabs.addWidget(control)
        feed_layout.addLayout(tabs)
        self.interests = label('', True)
        feed_layout.addWidget(self.interests)
        links = QHBoxLayout()
        self.url = QLineEdit()
        self.url.setPlaceholderText('粘贴公众号或浏览器文章链接')
        self.url.setAccessibleName('收集文章链接')
        self.add_button = button('收集链接', lambda: self.controller.add(self.url.text()))
        self.url.returnPressed.connect(self.add_button.click)
        links.addWidget(self.url, 1)
        links.addWidget(self.add_button)
        feed_layout.addLayout(links)
        self.search = QLineEdit(self.controller.query)
        self.search.setPlaceholderText('筛选发现内容的标题与简介')
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(180)
        self.search_timer.timeout.connect(self.render)
        self.search.textChanged.connect(self.search_changed)
        feed_layout.addWidget(self.search)
        self.scroll = QScrollArea(self.feed)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        feed_layout.addWidget(self.scroll, 1)
        self.stack.addWidget(self.feed)
        self.controller.changed.connect(self.render)
        self.render()

    def filter_kind(self, kind):
        self.controller.kind, self.controller.limit = kind, 20
        self.render()

    def search_changed(self, text):
        self.controller.query, self.controller.limit = text, 20
        self.search_timer.start()

    def render(self):
        c = self.controller
        self.refresh_button.setEnabled(not c.busy)
        self.add_button.setEnabled(not c.busy)
        state = self.service.state()
        updated = state.get('updated', 0)
        when = datetime.fromtimestamp(updated).strftime('%m-%d %H:%M') if updated else '尚未更新'
        self.status.setText((c.message+' · ' if c.message else '')+'上次更新：'+when)
        topics = self.service.interests()
        self.interests.setText('关注方向：'+'、'.join(topic for topic, _ in topics) if topics else '还没有兴趣记录：可在“兴趣与来源”中选择方向，或先阅读、收藏一些资料。')
        position = self.scroll.verticalScrollBar().value()
        body = QWidget(self.scroll)
        body.setObjectName('page')
        rows = QVBoxLayout(body)
        rows.setContentsMargins(0, 8, 8, 8)
        rows.setSpacing(14)
        items = self.service.ranked(c.kind, c.query)
        for item in items[:c.limit]:
            rows.addWidget(self.card(item, body))
        if not items:
            rows.addWidget(label('这里还没有内容。更新订阅、调整筛选，或粘贴一篇文章链接。'))
            rows.addWidget(label('发现内容来自原始来源；只有主动加入资料库后，才会用于本地问答。', True))
        if len(items) > c.limit:
            rows.addWidget(button(f'继续查看 · 还有 {len(items)-c.limit} 条', self.more))
        rows.addStretch()
        old = self.scroll.takeWidget()
        if old:
            old.hide()
            old.deleteLater()
        self.scroll.setWidget(body)
        self.scroll.verticalScrollBar().setValue(position)
        if self.stack.currentWidget() is self.feed:
            QTimer.singleShot(0, lambda controller=c, visible=items[:c.limit]: controller.load_covers(visible))
        if self.detail_id and self.stack.currentWidget() is not self.feed:
            self.show_detail(self.detail_id)

    def more(self):
        self.controller.limit += 20
        self.render()

    def card(self, item, parent):
        card = QWidget(parent)
        card.setObjectName('discoveryCard')
        card.setStyleSheet(f"QWidget#discoveryCard {{background:{self.palette['bg']};border:1px solid {self.palette['line']};border-radius:10px;}}")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        date = stamp(item.get('published', ''))
        date_label = datetime.fromtimestamp(date).strftime('%Y-%m-%d') if date else '日期未提供'
        layout.addWidget(label(' · '.join(filter(None, [item['source'], date_label, item.get('publication', '')])), True))
        main = QHBoxLayout()
        cover_file = item.get('cover_file', '')
        if re.fullmatch(r'[a-f0-9]{64}\.jpg', cover_file):
            picture = QPixmap(str(self.service.cache_dir/cover_file))
            if not picture.isNull():
                cover = QLabel(card)
                cover.setFixedSize(128, 90)
                cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
                cover.setPixmap(picture.scaled(128, 90, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                cover.setToolTip('原站封面 · '+item['source'])
                main.addWidget(cover)
        text = QVBoxLayout()
        title = label(item['title'])
        title.setStyleSheet('font-size:17px;font-weight:600;')
        text.addWidget(title)
        text.addWidget(label(item.get('summary', '')[:240] or '来源未提供简介，打开原文了解详情。', True))
        main.addLayout(text, 1)
        layout.addLayout(main)
        layout.addWidget(label(item['reason'], True))
        actions = FlowLayout(spacing=8)
        actions.addWidget(button('查看内容', lambda: self.show_detail(item['id']), primary=True))
        actions.addWidget(button('已收藏' if item.get('saved') else '收藏', lambda: self.controller.flag(item, 'saved', not item.get('saved'))))
        actions.addWidget(button('不感兴趣', lambda: self.controller.flag(item, 'hidden', True)))
        layout.addLayout(actions)
        return card

    def replace_secondary(self, widget):
        while self.stack.count() > 1:
            old = self.stack.widget(1)
            old.hide()
            self.stack.removeWidget(old)
            old.deleteLater()
        self.stack.addWidget(widget)
        self.stack.setCurrentWidget(widget)

    def back(self):
        self.detail_id = None
        self.stack.setCurrentWidget(self.feed)

    def show_detail(self, key):
        item = next((row for row in self.service.ranked() + self.service.ranked('saved') if row['id'] == key), None)
        if not item:
            self.back()
            return
        self.detail_id = key
        scroll = QScrollArea(self.stack)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget(scroll)
        body.setObjectName('page')
        layout = QVBoxLayout(body)
        layout.addWidget(button('返回发现', self.back))
        title = label(item['title'])
        title.setStyleSheet('font-size:23px;font-weight:600;')
        layout.addWidget(title)
        layout.addWidget(label(item['source']+' · '+(item.get('publication') or item.get('description_type', '来源简介')), True))
        if item.get('authors'):
            layout.addWidget(label(item['authors'], True))
        layout.addWidget(label(item.get('summary') or '来源未提供简介，请在原站阅读。'))
        layout.addWidget(label('这里预览来源摘要。加入资料库将下载论文全文并保留原始 PDF；摘要可单独保存。' if item['kind']=='paper' else '加入资料库将读取可访问的文章正文；受限页面不会用简介代替全文。', True))
        layout.addWidget(label(item['url'], True))
        actions = FlowLayout(spacing=8)
        actions.addWidget(button('打开原站', lambda: open_source(self.host, item['url'])))
        actions.addWidget(button('复制链接', lambda: copy_link(self.host, item['url'])))
        actions.addWidget(button('翻译摘要' if item['kind'] == 'paper' else '翻译简介',
                                 lambda: self.host.open_translation(item.get('summary', ''), item['title'])))
        article_id = item.get('article_id')
        article = self.host.service.store.get('articles', article_id) if article_id else None
        if article:
            actions.addWidget(button('打开资料', lambda: self.host.open_article(article_id), primary=True))
        if not article or article.get('source_status') == 'abstract':
            collect = button('获取全文并加入资料库', lambda: self.controller.collect(item), primary=True)
            collect.setEnabled(not self.controller.busy)
            actions.addWidget(collect)
        if item['kind'] == 'paper' and not item.get('abstract_article_id') and not (article and article.get('source_status') == 'abstract'):
            abstract = button('仅保存摘要', lambda: self.controller.collect(item, 'abstract'))
            abstract.setEnabled(not self.controller.busy)
            actions.addWidget(abstract)
        layout.addLayout(actions)
        layout.addStretch()
        scroll.setWidget(body)
        self.replace_secondary(scroll)

    def show_preferences(self):
        self.detail_id = None
        prefs = self.service.preferences()
        scroll = QScrollArea(self.stack)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget(scroll)
        body.setObjectName('page')
        layout = QVBoxLayout(body)
        layout.addWidget(button('返回发现', self.back))
        layout.addWidget(heading('兴趣与来源', '兴趣分析保留在本机，不上传阅读记录'))
        topic = QLineEdit('，'.join(prefs['topics']))
        topic.setPlaceholderText('兴趣方向，多个用逗号分隔')
        layout.addWidget(label('主动关注'))
        layout.addWidget(topic)
        tags = FlowLayout(spacing=6)
        for name in TOPICS:
            tags.addWidget(button(name, lambda checked=False, name=name: topic.setText(topic.text()+'，'+name if topic.text() else name)))
        layout.addLayout(tags)
        blocked = QLineEdit('，'.join(prefs['blocked']))
        blocked.setPlaceholderText('不想看到的主题或关键词，多个用逗号分隔')
        layout.addWidget(label('屏蔽关键词'))
        layout.addWidget(blocked)
        personal = button('根据阅读与探索记录推荐')
        personal.setCheckable(True)
        personal.setChecked(prefs['personal'])
        personal.setObjectName('nav')
        auto = button('启动后自动更新（间隔至少6小时）')
        auto.setCheckable(True)
        auto.setChecked(prefs['automatic'])
        auto.setObjectName('nav')
        layout.addWidget(personal)
        layout.addWidget(auto)
        error = label('', True)
        layout.addWidget(error)
        def save():
            latest = self.service.preferences()
            latest.update(topics=re.split('[,，\n]', topic.text()), blocked=re.split('[,，\n]', blocked.text()),
                          personal=personal.isChecked(), automatic=auto.isChecked())
            self.service.save_preferences(latest)
            error.setText('偏好已保存')
            self.controller.changed.emit()
        layout.addWidget(button('保存偏好', save, primary=True))
        def restore_hidden():
            flags = self.service.store.get('settings', 'discovery-flags', {})
            for value in flags.values():
                value.pop('hidden', None)
            self.service.store.put('settings', 'discovery-flags', flags)
            error.setText('已恢复被忽略的内容')
            self.controller.changed.emit()
        layout.addWidget(button('恢复被忽略的内容', restore_hidden))
        layout.addWidget(heading('订阅来源', '支持公开 RSS / Atom；公众号文章请使用发现页的链接收集'))
        source_rows = QVBoxLayout()
        layout.addLayout(source_rows)
        def populate_sources():
            while source_rows.count():
                old = source_rows.takeAt(0).widget()
                old.hide()
                old.deleteLater()
            for source in self.service.preferences()['sources']:
                row = QWidget(body)
                column = QVBoxLayout(row)
                column.addWidget(label(source['name']+' · '+('论文' if source['kind']=='paper' else '新闻')))
                column.addWidget(label(source['url'], True))
                controls = QHBoxLayout()
                def toggle(checked=False, sid=source['id']):
                    value = self.service.preferences()
                    for s in value['sources']:
                        if s['id'] == sid:
                            s['enabled'] = not s['enabled']
                    self.service.save_preferences(value)
                    populate_sources()
                def remove(checked=False, sid=source['id']):
                    value = self.service.preferences()
                    value['sources'] = [s for s in value['sources'] if s['id'] != sid]
                    self.service.save_preferences(value)
                    populate_sources()
                controls.addWidget(button('已订阅' if source['enabled'] else '已暂停', toggle))
                controls.addWidget(button('移除来源', remove))
                controls.addStretch()
                column.addLayout(controls)
                source_rows.addWidget(row)
        populate_sources()
        name = QLineEdit()
        name.setPlaceholderText('来源名称')
        url = QLineEdit()
        url.setPlaceholderText('RSS / Atom 地址（不是普通文章网址）')
        kind = PageComboBox()
        kind.addItem('新闻 / 技术文章', 'news')
        kind.addItem('论文', 'paper')
        for control in (name, url, kind):
            layout.addWidget(control)
        def add_source():
            try:
                address = canonical(url.text())
                if not name.text().strip():
                    raise ValueError('请填写来源名称。')
                value = self.service.preferences()
                if any(s['url'] == address for s in value['sources']):
                    raise ValueError('已经订阅这个地址。')
                value['sources'].append(dict(id=sha256(address.encode()).hexdigest()[:16], name=name.text().strip()[:80],
                                            url=address, kind=kind.currentData(), enabled=True))
                self.service.save_preferences(value)
                name.clear()
                url.clear()
                populate_sources()
                error.setText('来源已添加，返回后点击“更新内容”获取文章。')
            except ValueError as exc:
                error.setText(str(exc))
        layout.addWidget(button('添加订阅源', add_source))
        layout.addStretch()
        scroll.setWidget(body)
        self.replace_secondary(scroll)
