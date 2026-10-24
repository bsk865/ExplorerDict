"""Native desktop window and navigation."""
from datetime import date, timedelta
from copy import deepcopy
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QStackedWidget, QButtonGroup, QSizeGrip, QStyle, QScrollArea)
from .surfaces import QMessageBox, QFileDialog, QInputDialog
from .widgets import Tasks, button, stylesheet
from .dialogs import EditForm, TaskForm, NoteForm
from .shelf import ShelfPage
from .study import StudyPage
from .reader import ReaderPage
from .library import LibraryController, LibraryPage
from .content import ContentPage, DELETE_SCOPES
from .advisor import AdvisorController, AdvisorDialog, BatchDialog
from .maps import MapPage
from .index import IndexController, IndexPage
from .saved import SavedPage
from .navigation import Sidebar
from .topbar import TopBar
from .discover import DiscoveryController, DiscoverPage
from .translation import TranslationController, TranslationPage, source_scope


class TitleBar(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.setFixedHeight(42)
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 0, 4, 0)
        row.addWidget(QLabel('探索词典'))
        row.addStretch()
        for icon, title, action in [(QStyle.StandardPixmap.SP_TitleBarMinButton, '最小化', window.showMinimized),
                                     (QStyle.StandardPixmap.SP_TitleBarMaxButton, '最大化 / 还原', self.maximize),
                                     (QStyle.StandardPixmap.SP_TitleBarCloseButton, '关闭', window.close)]:
            b = button('', action, icon=self.style().standardIcon(icon), tip=title)
            b.setFixedSize(40, 30)
            b.setAccessibleName(title)
            row.addWidget(b)

    def maximize(self):
        self.window.showNormal() if self.window.isMaximized() else self.window.showMaximized()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.window.windowHandle():
            self.window.windowHandle().startSystemMove()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.maximize()


class NativeWindow(QMainWindow):
    ready = Signal()

    def __init__(self, service, *, discovery_updates=True):
        super().__init__()
        self.service = service
        self.discovery_updates = discovery_updates
        self.discovery = DiscoveryController(self)
        self.tasks = Tasks(self)
        self.tasks.error.connect(self.notice)
        self.translation_controllers = {}
        self.translation = self.translation_for('scratch')
        self.library = LibraryController(self)
        self.article_chats = {}
        self.note_controllers = {}
        self.maps = {}
        self.index_controller = IndexController(self)
        self.advisor = AdvisorController(self)
        self.advisor.plans_changed.connect(self.refresh)
        self.plan_view = 'all'
        self.week_offset = 0
        self.advisor_dialog = None
        self.snapshot = {}
        self.current_page = 'discover'
        self.article_id = None
        self.article_block = None
        self.article_object = None
        self.article_draft = None
        self.article_note = None
        self.weather_value = None
        self.weather_loading = False
        self.history = []
        self.pending_route = None
        self.navigation_busy = False
        self.generation = 0
        self.closing = False
        self.capture_enabled = False
        self.sync_pending = False
        self.setWindowTitle('探索词典 · 阅读空间')
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setMinimumSize(900, 620)
        self.resize(1380, 880)
        body = QWidget()
        self.setCentralWidget(body)
        outer = QVBoxLayout(body)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)
        outer.addWidget(TitleBar(self))
        content = QHBoxLayout()
        content.setSpacing(0)
        self.sidebar = Sidebar(self)
        self.nav = self.sidebar.buttons
        self.nav_group = self.sidebar.group
        content.addWidget(self.sidebar)
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        self.topbar = TopBar(self)
        self.back, self.crumb, self.search = self.topbar.back, self.topbar.crumb, self.topbar.search
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(250)
        self.search_timer.timeout.connect(self.search_articles)
        self.search.textChanged.connect(lambda _: self.search_timer.start())
        right.addWidget(self.topbar)
        self.stack = QStackedWidget()
        self.stack.setContentsMargins(28, 0, 28, 20)
        self.content_area = QStackedWidget()
        self.content_area.addWidget(self.stack)
        self.translation_tool = None
        right.addWidget(self.content_area, 1)
        self.status = QLabel('保存在本机')
        self.status.setObjectName('muted')
        footer = QHBoxLayout()
        footer.setContentsMargins(28, 10, 16, 10)
        footer.addWidget(self.status)
        footer.addStretch()
        footer.addWidget(QSizeGrip(self))
        right.addLayout(footer)
        content.addLayout(right, 1)
        outer.addLayout(content, 1)
        self.setStyleSheet(stylesheet({'palette': 'clay', 'mode': 'light'}))
        self.index_timer = QTimer(self)
        self.index_timer.setInterval(30000)
        self.index_timer.timeout.connect(lambda: self.run(self.service.workspace.rag.request, lambda _: None))
        self.index_timer.start()
        self.sync_timer = QTimer(self)
        self.sync_timer.setInterval(4000)
        self.sync_timer.timeout.connect(self.synchronize)
        self.sync_timer.start()
        self.handoff_timer = QTimer(self)
        self.handoff_timer.setInterval(250)
        self.handoff_timer.timeout.connect(self.receive_handoff)
        self.handoff_timer.start()
        QTimer.singleShot(0, lambda: self.run(self.service.workspace.rag.request, lambda _: None))
        self.refresh()

    def notice(self, message):
        self.status.setText(message)
        QTimer.singleShot(8000, lambda: self.status.setText('保存在本机') if not self.closing else None)

    def open_capture(self):
        from ..capture_bridge import ensure_started
        if self.topbar.busy:
            return
        self.topbar.busy = True
        self.topbar.capture.setEnabled(False)
        self.topbar.render_capture({'label':'正在开启'})
        self.capture_enabled = True
        def finish(result):
            self.topbar.busy = False
            self.topbar.capture.setEnabled(True)
            self.topbar.render_capture(result)
        def failed(message):
            finish({'label':'未能开启，点击重试'})
            self.notice(message)
        self.tasks.submit(lambda: ensure_started(reveal=True), finish, failed)

    def refresh_data(self):
        if self.current_page == 'discover':
            self.discovery.refresh()
            return
        if not self.sync_pending and not self.closing:
            self.synchronize(manual=True)

    def synchronize(self, manual=False):
        if (not manual and not self.capture_enabled) or self.sync_pending or self.closing:
            return
        self.sync_pending = True
        self.topbar.refresh.setEnabled(False)
        def synced(count):
            self.sync_pending = False
            self.topbar.refresh.setEnabled(True)
            if (count or manual) and not self.closing:
                self.refresh()
        def failed(message):
            self.sync_pending = False
            self.topbar.refresh.setEnabled(True)
            if manual:
                self.notice(message)
        self.tasks.submit(self.service.workspace.synchronize, synced, failed)

    def run(self, action, done=None):
        if not self.closing:
            self.tasks.submit(action, done or (lambda _: self.refresh()))

    def refresh(self):
        self.close_translation()
        self.navigation_busy = True
        self.back.setEnabled(False)
        previous = self.stack.currentWidget()
        if isinstance(previous, ReaderPage):
            previous.save_view()
        self.generation += 1
        version = self.generation
        page, article = self.current_page, self.article_id
        restored = self.pending_route if self.pending_route and (self.pending_route['page'], self.pending_route['article']) == (page, article) else None
        def read():
            snapshot = self.service.snapshot()
            bundle = self.service.article(article) if page in {'reader', 'map'} and article and self.service.store.get('articles', article) else None
            if page in {'content', 'notes'}:
                bundle = self.service.inventory()
            elif page == 'saved':
                bundle = self.service.store.saved_entries()
            return snapshot, bundle
        def show(result):
            if self.closing or version != self.generation:
                return
            self.snapshot, bundle = result
            self.setStyleSheet(stylesheet(self.snapshot['preferences']))
            self.topbar.theme(self.snapshot['preferences'])
            self.topbar.adapt(self.width(), page in {'reader', 'map'})
            if page == 'reader' and bundle:
                widget = ReaderPage(self, bundle)
                old_reader = self.stack.currentWidget()
                if isinstance(old_reader, ReaderPage) and old_reader.article['id'] == article:
                    widget.resume_assistant = old_reader.assistant_visible
                widget.focus_object = self.article_object
                if restored and 'reader' in restored:
                    widget.route_view = restored['reader']
                self.article_object = None
                widget.focus_note = self.article_draft
                self.article_draft = None
                widget.focus_saved_note = self.article_note
                self.article_note = None
                if self.article_block:
                    widget.focus_block = self.article_block
                    self.article_block = None
                title = bundle['article']['title']
            elif page == 'map' and bundle:
                widget = MapPage(self, bundle)
                title = '思维导图 / ' + bundle['article']['title']
            elif page == 'discover':
                widget = DiscoverPage(self)
                title = '发现'
            elif page == 'library':
                widget = LibraryPage(self, self.snapshot)
                if restored and 'library' in restored:
                    scope = restored['library']
                    available = {a['id'] for a in self.snapshot['articles']
                                 if a.get('chars') and a.get('source_status') != 'pending_source'}
                    scope = dict(scope, article_ids=([a['id'] for a in self.snapshot['articles']] if scope.get('scope', 'all') == 'all' else [aid for aid in scope['article_ids'] if aid in available]))
                    if scope != self.library.body:
                        self.library.select(scope['article_ids'], scope['include_notes'], scope['mode'], scope.get('scope', 'all'))
                title = '资料库问答'
            elif page == 'index':
                widget = IndexPage(self)
                title = '知识索引'
            elif page == 'saved':
                widget = SavedPage(self, bundle)
                title = '收藏与回顾'
            elif page in {'content', 'notes'}:
                previous = self.stack.currentWidget()
                kind = previous.kind if isinstance(previous, ContentPage) and previous.route == page else ('notes' if page == 'notes' else getattr(self, 'content_filters', {}).get('kind', 'notes'))
                if restored:
                    kind = restored.get('kind', kind)
                widget = ContentPage(self, bundle, kind)
                widget.route = page
                title = '笔记管理' if kind == 'notes' else '内容管理'
            elif page in {'shelf', 'reader', 'map'}:
                if page in {'reader', 'map'}:
                    self.current_page, self.article_id = 'shelf', None
                previous = self.stack.currentWidget()
                theme = previous.theme if isinstance(previous, ShelfPage) else ''
                if restored:
                    theme = restored.get('theme', theme)
                widget = ShelfPage(self, self.snapshot, theme)
                title = '主题资料库'
            else:
                widget = StudyPage(self, self.snapshot, page)
                title = {'today': '今日', 'notes': '心得与笔记', 'plans': '学习计划', 'progress': '探索统计'}[page]
            old = self.stack.currentWidget()
            self.stack.addWidget(widget)
            self.stack.setCurrentWidget(widget)
            if old:
                old.hide()
                self.stack.removeWidget(old)
                old.deleteLater()
            self.crumb.setText('我的空间 / ' + title)
            self.navigation_busy = False
            self.back.setEnabled(bool(self.history))
            selected_page = 'shelf' if self.current_page in {'reader', 'map'} else self.current_page
            self.sidebar.update_content(self.snapshot['articles'], self.snapshot['preferences'], selected_page)
            self.filter(self.search.text())
            if restored:
                if self.pending_route is restored:
                    self.pending_route = None
                def restore_scroll():
                    if self.stack.currentWidget() is widget:
                        bar = self.page_scroll(widget)
                        if bar is not None:
                            bar.setValue(restored.get('scroll', 0))
                QTimer.singleShot(0, restore_scroll)
            if page == 'today':
                self.update_weather()
            elif page == 'discover':
                QTimer.singleShot(0, self.discovery.maybe_refresh)
            self.ready.emit()
        def failed(message):
            if not self.closing and version == self.generation:
                self.navigation_busy = False
                self.back.setEnabled(bool(self.history))
                self.notice(message)
        self.tasks.submit(read, show, failed)

    def navigate(self, page, article=None, remember=True):
        if remember and (page, article) != (self.current_page, self.article_id):
            self.history.append(self.navigation_state())
            self.history = self.history[-50:]
            self.pending_route = None
        self.current_page, self.article_id = page, article
        self.refresh()

    def go_back(self):
        if self.history and not self.navigation_busy:
            target = self.history[-1]
            if target.get('library') and target['library'] != self.library.body and self.library.busy:
                self.notice('资料库正在回答，请稍后返回。')
                return
            route = self.history.pop()
            self.pending_route = route
            self.search_timer.stop()
            self.search.blockSignals(True)
            self.search.setText(route['search'])
            self.search.blockSignals(False)
            for name, value in route['state'].items():
                setattr(self, name, deepcopy(value))
            self.navigate(route['page'], route['article'], False)

    @staticmethod
    def page_scroll(widget):
        if isinstance(widget, DiscoverPage):
            return widget.scroll.verticalScrollBar()
        if isinstance(widget, ShelfPage):
            return widget.articles.verticalScrollBar()
        if isinstance(widget, ContentPage):
            return widget.scroll.verticalScrollBar()
        if isinstance(widget, StudyPage):
            scroll = widget.findChild(QScrollArea)
            return scroll.verticalScrollBar() if scroll else None
        return None

    def navigation_state(self):
        widget = self.stack.currentWidget()
        route = {'page':self.current_page, 'article':self.article_id, 'search':self.search.text(),
                 'state':{name:deepcopy(getattr(self, name)) for name in
                          ('content_filters', 'plan_filters', 'plan_view', 'week_offset', 'progress_scope')
                          if hasattr(self, name)}}
        if isinstance(widget, ShelfPage):
            route['theme'] = widget.theme
        if isinstance(widget, ContentPage):
            route['kind'] = widget.kind
        if isinstance(widget, LibraryPage):
            route['library'] = deepcopy(self.library.body)
        if isinstance(widget, ReaderPage):
            route['reader'] = {
                'assistant': widget.assistant_visible,
                'object': widget.chat.focus.currentData(),
                'tab': widget.tabs.currentIndex(),
                'scroll': widget.text.verticalScrollBar().value(),
                'split': widget.splitter.sizes(),
                'translation': widget.translation_page is not None and widget.reading_tools.currentWidget() is widget.translation_page,
            }
        bar = self.page_scroll(widget)
        route['scroll'] = bar.value() if bar is not None else 0
        return route

    def open_article(self, key, remember=True, block=None, object_id=None, note_draft=None, note_id=None):
        self.article_block = block
        self.article_object = object_id
        self.article_draft = note_draft
        self.article_note = note_id
        self.navigate('reader', key, remember)
        self.tasks.submit(lambda: self.service.activity.record(key), lambda _: None)

    def translation_for(self, scope):
        if scope not in self.translation_controllers:
            self.translation_controllers[scope] = TranslationController(self, scope)
        return self.translation_controllers[scope]

    def close_translation(self):
        self.content_area.setCurrentWidget(self.stack)
        if self.translation_tool is not None:
            self.content_area.removeWidget(self.translation_tool)
            self.translation_tool.deleteLater()
            self.translation_tool = None

    def show_translation(self, controller):
        self.close_translation()
        self.translation = controller
        self.translation_tool = TranslationPage(self, controller, self.close_translation)
        self.content_area.addWidget(self.translation_tool)
        self.content_area.setCurrentWidget(self.translation_tool)

    def open_translation(self, text='', origin='', scope=None):
        controller = self.translation_for(scope or source_scope(text, origin))
        if text and text != controller.state['text'] and not controller.prefill(text, origin):
            return
        self.show_translation(controller)

    def receive_handoff(self):
        if self.closing or self.navigation_busy:
            return
        from PySide6.QtWidgets import QApplication
        if QApplication.activeModalWidget() is not None:
            return
        from ..workspace_bridge import take_translation
        value = take_translation(self.service.store.directory)
        if value:
            controller = self.translation_for(source_scope(value['text'], value.get('origin', '')))
            controller.receive(value)
            self.show_translation(controller)
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def resume_draft(self, record):
        if record['id'] == 'native-advisor-draft':
            self.show_advisor()
        elif record.get('article_id'):
            self.open_article(record['article_id'], note_draft=record['draft_id'])

    def filter(self, query):
        widget = self.stack.currentWidget()
        if isinstance(widget, ShelfPage):
            widget.filter(query)

    def search_articles(self):
        if self.closing:
            return
        query = self.search.text()
        if query.strip() and self.current_page != 'shelf':
            self.navigate('shelf')
        else:
            self.filter(query)

    def delete(self, kind, key, title):
        prompt = DELETE_SCOPES.get(kind, '此操作会删除选中的内容。')
        if QMessageBox.question(self, '删除确认', f'删除「{title}」？\n{prompt}',
                                 QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                 QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            related = self.service.store.related_versions(key) if kind == 'articles' else set()
            def matches(body):
                return (kind == 'notes' and body.get('id') == key or
                        kind == 'native_drafts' and key == 'note-draft:'+body.get('draft_id', '') or
                        kind == 'articles' and body.get('article_id') in related)
            paused = [(controller, controller.pause_matching(matches)) for controller in self.note_controllers.values()]
            def removed(_):
                for controller, drafts in paused:
                    controller.finish_removal(drafts, True)
                if kind == 'native_drafts' and key == 'native-advisor-draft':
                    self.advisor.value = {}
                    self.advisor.result = None
                    self.advisor.message = ''
                    self.advisor.changed.emit()
                if kind == 'weather':
                    self.weather_value = None
                if kind in {'articles', 'conversations', 'notes', 'object_note', 'session_note'}:
                    ids = {a['id'] for a in self.service.store.summaries()}
                    selected = [a for a in self.library.body['article_ids'] if a in ids]
                    self.library.select(selected, self.library.body['include_notes'], self.library.body['mode'],
                                        self.library.body.get('scope', 'all'))
                self.refresh()
            def failed(message):
                for controller, drafts in paused:
                    controller.finish_removal(drafts, False)
                self.notice(message)
            self.tasks.submit(lambda: self.service.remove(kind, key), removed, failed)
            return True
        return False

    def import_material(self):
        menu = QMessageBox(self)
        menu.setWindowTitle('添加资料')
        menu.setText('选择资料来源')
        file_button = menu.addButton('本地文件', QMessageBox.ButtonRole.ActionRole)
        text_button = menu.addButton('粘贴原文', QMessageBox.ButtonRole.ActionRole)
        web_button = menu.addButton('读取网页', QMessageBox.ButtonRole.ActionRole)
        menu.addButton('取消', QMessageBox.ButtonRole.RejectRole)
        menu.exec()
        if menu.clickedButton() == file_button:
            paths, _ = QFileDialog.getOpenFileNames(self, '选择资料（可多选）', '', '资料 (*.txt *.md *.pdf *.docx *.png *.jpg *.jpeg *.webp *.bmp)')
            if paths:
                from .imports import BatchImportDialog
                dialog = BatchImportDialog(self, paths)
                dialog.exec()
                articles = [row['article_id'] for row in dialog.results if 'article_id' in row]
                if len(articles) == 1:
                    self.open_article(articles[0])
                else:
                    self.navigate('shelf')
        elif menu.clickedButton() == web_button:
            from .materials import extract
            extract(self)
        elif menu.clickedButton() == text_button:
            dialog = EditForm('添加原文', [('title', '标题', 'text', '', None), ('text', '原文', 'long', '', None)],
                              lambda b: self.service.import_text(b['title'], b['text']), self)
            if dialog.exec():
                self.navigate('shelf')

    def edit_task(self, task=None):
        task = task if isinstance(task, dict) else {'date': date.today().isoformat()}
        dialog = TaskForm(task, self.snapshot.get('articles', []),
                          lambda b: self.service.hub.save('tasks', {**task, **b}),
                          lambda: self.delete('tasks', task['id'], task['title']),
                          lambda: self.split_task(task), self)
        if dialog.exec():
            self.refresh()

    def show_advisor(self):
        if self.advisor_dialog is None:
            self.advisor_dialog = AdvisorDialog(self)
        self.advisor_dialog.show()
        self.advisor_dialog.raise_()
        self.advisor_dialog.activateWindow()

    def plan_history(self):
        BatchDialog(self).exec()

    def split_task(self, task):
        dialog = EditForm('拆分计划', [('minutes', '第一部分分钟', 'number', task['minutes']//2, (5, task['minutes']-5))],
                          lambda body: self.service.hub.split_task(task['id'], body['minutes']), self)
        if dialog.exec():
            self.refresh()

    def delay_task(self, task):
        tomorrow = (date.fromisoformat(task.get('date') or date.today().isoformat()) + timedelta(days=1)).isoformat()
        self.run(lambda: self.service.hub.save('tasks', {'id': task['id'], 'date': tomorrow}))

    def content_category_changed(self, kind):
        page = self.stack.currentWidget()
        if not isinstance(page, ContentPage):
            return
        self.current_page = 'notes' if kind == 'notes' else 'content'
        page.route = self.current_page
        self.crumb.setText('我的空间 / ' + ('笔记管理' if kind == 'notes' else '内容管理'))
        self.sidebar.update_content(self.snapshot['articles'], self.snapshot['preferences'], self.current_page)

    def edit_note(self, note=None):
        note = note if isinstance(note, dict) else {}
        if not note and not self.snapshot.get('articles'):
            self.notice('先添加一篇资料，再记录笔记。')
            return
        dialog = NoteForm(note, self.snapshot.get('articles', []),
                          lambda b: self.service.hub.save('notes', {**note, **b}), self)
        if dialog.exec():
            self.refresh()

    def choose_city(self):
        from ..study_weather import cities, choose
        name, ok = QInputDialog.getText(self, '天气城市', '城市名称')
        if not ok or not name.strip():
            return
        def found(rows):
            if not rows:
                self.notice('没有找到城市，请试试拼音。')
                return
            labels = [r['name'] + ' · ' + r['region'] + ' · ' + r['country'] for r in rows]
            label, accepted = QInputDialog.getItem(self, '选择城市', '城市', labels, editable=False)
            if accepted:
                self.run(lambda: choose(self.service.store, rows[labels.index(label)]), lambda _: self.update_weather(force=True))
        self.run(lambda: cities(name), found)

    def update_weather(self, force=False):
        from ..study_weather import current, saved
        if self.weather_loading:
            self.weather_refresh_pending = getattr(self, 'weather_refresh_pending', False) or force
            return
        page = self.stack.currentWidget()
        local = saved(self.service.store)
        if isinstance(page, StudyPage) and page.page == 'today':
            page.show_weather(local)
        if self.weather_value is not None and not force and self.weather_value.get('city') == local.get('city'):
            if isinstance(page, StudyPage) and page.page == 'today':
                page.show_weather(self.weather_value)
            return
        self.weather_loading = True
        def fetch():
            try:
                return current(self.service.store)
            except Exception:
                return dict(local, status='unavailable')
        def loaded(value):
            self.weather_loading = False
            if getattr(self, 'weather_refresh_pending', False):
                self.weather_refresh_pending = False
                if not self.closing:
                    self.update_weather(force=True)
                return
            self.weather_value = value
            page = self.stack.currentWidget()
            if not self.closing and isinstance(page, StudyPage) and page.page == 'today':
                page.show_weather(value)
        self.run(fetch, loaded)

    def preferences(self):
        from .appearance import AppearanceDialog
        dialog = AppearanceDialog(self.service.store.directory, self)
        dialog.changed.connect(self.refresh)
        dialog.exec()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'sidebar'):
            self.sidebar.adapt(self.width(), self.height())
        if hasattr(self, 'topbar'):
            self.topbar.adapt(self.width(), self.current_page in {'reader', 'map'})

    def model_settings(self):
        from .settings import SettingsDialog
        SettingsDialog(self).exec()

    def closeEvent(self, event):
        if not self.closing:
            page = self.stack.currentWidget()
            if isinstance(page, ReaderPage):
                page.save_view()
            self.closing = True
            for controller in self.translation_controllers.values():
                controller.shutdown()
            self.topbar.timer.stop()
            self.search_timer.stop()
            for controller in self.note_controllers.values():
                controller.flush()
            self.index_timer.stop()
            self.sync_timer.stop()
            self.handoff_timer.stop()
            self.index_controller.timer.stop()
            self.advisor.timer.stop()
            self.library.timer.stop()
            for controller in [*self.article_chats.values(), *self.maps.values()]:
                controller.timer.stop()
            self.service.close()
        with self.service.workspace.lock:
            running = any(job.get('status') == 'running' for job in self.service.workspace.jobs.values())
            for job in self.service.workspace.jobs.values():
                if job.get('status') == 'running':
                    job['cancel'].set()
        if self.tasks.pending or running or self.service.workspace.rag.running:
            self.status.setText('正在完成保存…')
            QTimer.singleShot(100, self.close)
            event.ignore()
            return
        event.accept()
