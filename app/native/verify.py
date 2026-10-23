"""Opt-in native-window smoke test, restricted to isolated test data."""
import json
import sys
import traceback
from PySide6.QtCore import QTimer, Qt, QEventLoop, QPoint, QUrl
from PySide6.QtWidgets import QApplication, QTabWidget, QTabBar, QListWidget, QLabel, QPushButton, QMessageBox, QScrollArea
from .reader import ReaderPage, OriginalText
from .shelf import ShelfPage
from .study import StudyPage
from .library import LibraryPage
from .content import ContentPage
from .maps import MapPage, MapView, MapScene
from .index import IndexPage
from .saved import SavedPage


def attach_verification(window, directory):
    report = {'pages': [], 'errors': []}
    routes = ['today', 'shelf', 'plans', 'notes', 'progress', 'saved', 'content', 'index', 'library', 'reader', 'map']
    index = 0
    scheduled = False
    library_phase = 0
    plans_phase = 0
    reader_phase = 0
    content_phase = 0
    imports_started = False

    def finish():
        nonlocal imports_started
        if not report['errors'] and not imports_started:
            imports_started = True
            def import_documents():
                bundles = []
                for name in ('sample.pdf', 'sample.docx'):
                    article = window.service.import_file(directory / 'import-fixtures' / name)
                    bundles.append((name, window.service.article(article['id'])))
                return bundles
            def imported(bundles):
                try:
                    for name, bundle in bundles:
                        article = bundle['article']
                        assert 'import verification' in article['text'], name + ': text missing'
                        assert bundle['assets'], name + ': image missing'
                        reader = OriginalText()
                        reader.resize(900, 700)
                        reader.load_article(article, bundle['assets'])
                        assert 'import verification' in reader.toPlainText()
                        reader.grab().save(str(directory / (name + '-reader.png')))
                        reader.close()
                    report['document_imports'] = [name for name, _ in bundles]
                    formula = OriginalText()
                    formula.resize(800, 400)
                    formula.load_article({'blocks': [{'id': 'formula', 'type': 'text', 'html':
                        '<h2>公式排版</h2><p>分式与根号： <math><mfrac><mi>a</mi><msqrt><msup><mi>x</mi><mn>2</mn></msup><mo>+</mo><msup><mi>y</mi><mn>2</mn></msup></msqrt></mfrac></math></p>'
                        '<p>求和： <math><munderover><mo>∑</mo><mrow><mi>i</mi><mo>=</mo><mn>1</mn></mrow><mi>n</mi></munderover><msub><mi>a</mi><mi>i</mi></msub></math></p>'
                        '<p>矩阵： <math><semantics><mfenced><mtable><mtr><mtd><mi>a</mi></mtd><mtd><mi>b</mi></mtd></mtr><mtr><mtd><mi>c</mi></mtd><mtd><mi>d</mi></mtd></mtr></mtable></mfenced></semantics></math></p>'}]}, {})
                    assert formula.toPlainText().count('\ufffc') == 3, 'formula rendering failed'
                    formula.grab().save(str(directory / 'formula-reader.png'))
                    formula.close()
                    report['formula_rendering'] = True
                    long_graph = {'root': 'root', 'nodes': [{'id':'root', 'parent':None, 'label':'阅读与核对'}]}
                    for number in range(60):
                        long_graph['nodes'].append({'id': str(number), 'parent':'root',
                            'label':f'节点{number}：整理阅读材料中的观点，并返回原文核对来源与结论。'})
                    long_view = MapView()
                    from ..reading_preferences import colors
                    preferences = window.snapshot['preferences']
                    long_scene = MapScene(long_graph, colors(preferences['palette'], preferences['mode']))
                    long_view.setScene(long_scene)
                    long_view.resize(900, 620)
                    long_view.show()
                    QApplication.processEvents()
                    long_scene.export(directory / 'long-map.svg')
                    long_scene.export(directory / 'long-map.png')
                    long_view.ensureVisible(long_scene.nodes['59'])
                    long_scene.nodes['59'].setSelected(True)
                    long_view.grab().save(str(directory / 'long-map-detail.png'))
                    long_view.close()
                    report['long_map_export'] = True
                except Exception as exc:
                    report['errors'].append(repr(exc))
                    (directory / 'import-error.log').write_text(traceback.format_exc(), encoding='utf-8')
                finish()
            window.run(import_documents, imported)
            return
        report['webengine_loaded'] = any('QtWebEngine' in name for name in sys.modules)
        report['passed'] = not report['errors'] and len(report['pages']) == len(routes) and not report['webengine_loaded']
        (directory / 'native-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        window.close()

    def inspect():
        nonlocal index, scheduled, library_phase, plans_phase, reader_phase, content_phase
        scheduled = False
        try:
            page = window.stack.currentWidget()
            assert page is not None
            if routes[index] == 'shelf':
                assert isinstance(page, ShelfPage)
                selected = page.themes.currentRow()
                if page.themes.count() > 2:
                    page.themes.set_drop_target(page.themes.item(2))
                    window.grab().save(str(directory / 'shelf-drop-target.png'))
                    assert page.themes.currentRow() == selected, 'drop target changed theme filter'
                    page.themes.set_drop_target(None)
                    report['shelf_drop_target_preserves_filter'] = True
                    assert page.rooms.isVisible(), 'theme rooms missing from all materials'
                    theme_id = page.rooms.item(0).data(Qt.ItemDataRole.UserRole)
                    page.rooms.itemClicked.emit(page.rooms.item(0))
                    assert page.theme == theme_id and not page.rooms.isVisible()
                    assert page.organize.isVisible() and page.edit_theme.isVisible()
                    window.grab().save(str(directory / 'shelf-selected-theme.png'))
                    page.select_theme('')
                    assert page.rooms.isVisible() and not page.organize.isVisible()
                    report['shelf_theme_rooms'] = True
                window.resize(900, 620)
                QApplication.processEvents()
                assert window.width() == 900, 'shelf header prevents compact width'
                assert page.articles.horizontalScrollBar().maximum() == 0, 'article rows overflow'
                row = page.articles.itemWidget(page.articles.item(0))
                assert row is not None
                for control in row.findChildren(QPushButton):
                    assert row.rect().contains(control.geometry()), 'article action clipped'
                window.grab().save(str(directory / 'shelf-compact.png'))
                original_topics = page.snapshot['study']['themes']
                page.snapshot['study']['themes'] = [dict(id=f'preview-{n}', title=f'主题{n}：'+('阅读与研究方法' * 9)) for n in range(30)]
                picker_errors = []
                def inspect_picker():
                    dialog = QApplication.activeModalWidget()
                    try:
                        scroll = dialog.findChild(QScrollArea)
                        assert scroll.verticalScrollBar().maximum() > 0
                        assert scroll.horizontalScrollBar().maximum() == 0
                        assert dialog.height() <= 560
                        scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
                        dialog.grab().save(str(directory / 'shelf-theme-picker.png'))
                    except Exception as exc:
                        picker_errors.append(str(exc))
                    finally:
                        if dialog:
                            dialog.reject()
                try:
                    QTimer.singleShot(50, inspect_picker)
                    page.classify(page.snapshot['articles'][0])
                finally:
                    page.snapshot['study']['themes'] = original_topics
                assert not picker_errors, str(picker_errors)
                report['shelf_theme_picker_scroll'] = True
                window.resize(1380, 880)
                QApplication.processEvents()
            if routes[index] == 'content':
                if content_phase == 0:
                    aid = window.snapshot['articles'][0]['id']
                    key = 'native-note-drafts:'+aid
                    drafts = window.service.store.get('settings', key, [])
                    drafts.append({'article_id':aid, 'draft_id':'native-verification-draft', 'title':'待整理的阅读心得', 'content':''})
                    window.service.store.put('settings', key, drafts)
                    content_phase = 1
                    window.refresh()
                    return
                if content_phase in {1, 3}:
                    assert isinstance(page, ContentPage)
                    page.category.setCurrentIndex(page.category.findData('native_drafts'))
                    draft, row = next((record, row) for record, row in page.rows
                                      if record['id'] == 'note-draft:native-verification-draft')
                    assert draft.get('draft_id') == 'native-verification-draft'
                    if content_phase == 1:
                        resume = next(b for b in row.findChildren(QPushButton) if b.text() == '继续编辑')
                        window.grab().save(str(directory / 'draft-management.png'))
                        content_phase = 2
                        resume.click()
                    else:
                        def confirm_draft_delete():
                            dialog = QApplication.activeModalWidget()
                            dialog.button(QMessageBox.StandardButton.Yes).click()
                        content_phase = 4
                        QTimer.singleShot(100, confirm_draft_delete)
                        next(b for b in row.findChildren(QPushButton) if b.toolTip() == '删除').click()
                    return
                if content_phase == 2:
                    assert isinstance(page, ReaderPage)
                    assert page.tabs.currentWidget() is page.notes
                    assert page.notes.title.text() == '待整理的阅读心得'
                    window.grab().save(str(directory / 'draft-restored.png'))
                    page.tabs.setCurrentWidget(page.chat)
                    content_phase = 3
                    window.go_back()
                    return
                assert not any(d['id'] == 'note-draft:native-verification-draft' for d in window.service.inventory()['native_drafts'])
                report['draft_management_restore_delete'] = True
            assert window.current_page == routes[index]
            assert isinstance(page, (ReaderPage, ShelfPage, StudyPage, LibraryPage, ContentPage, MapPage, IndexPage, SavedPage))
            assert window.width() >= 900 and window.height() >= 620
            if routes[index] == 'notes':
                assert isinstance(page, ContentPage) and page.kind == 'notes'
                note, row = page.rows[0]
                page.search.setText(note['content'][-8:])
                page.article.setCurrentIndex(page.article.findData(note['article_id']))
                assert not row.isHidden(), 'note filters hide matching content'
                assert any(b.text() == '编辑' for b in row.findChildren(QPushButton))
                page.search.setText('no-such-native-note-7410')
                assert not page.empty.isHidden() and page.count.text() == '0 项'
                page.category.setCurrentIndex(page.category.findData('tasks'))
                assert window.current_page == 'content' and window.nav['content'].isChecked()
                page.category.setCurrentIndex(page.category.findData('notes'))
                assert window.current_page == 'notes' and window.nav['notes'].isChecked()
                assert not page.search.text() and page.article.currentData() == ''
                report['note_management_filters_actions'] = True
            if isinstance(page, IndexPage):
                controller = window.index_controller
                if not controller.state:
                    QTimer.singleShot(150, inspect)
                    return
                if controller.state.get('available'):
                    if not controller.document and page.documents.count():
                        page.documents.setCurrentRow(0)
                        QTimer.singleShot(200, inspect)
                        return
                    if controller.chunks.get('pending'):
                        controller.inspect(controller.document)
                        QTimer.singleShot(500, inspect)
                        return
                    if not controller.chunks:
                        QTimer.singleShot(150, inspect)
                        return
                    assert page.chunks.count() > 0, 'semantic index has no source chunks'
                    report['semantic_chunks'] = True
            if routes[index] == 'plans':
                if plans_phase == 0:
                    window.grab().save(str(directory / 'plans-list.png'))
                    window.show_advisor()
                    plans_phase = 1
                    QTimer.singleShot(150, inspect)
                    return
                if plans_phase == 1:
                    dialog = window.advisor_dialog
                    assert dialog.isVisible() and dialog.send.isEnabled()
                    assert not dialog.preference_panel.isVisible(), 'advisor options should start collapsed'
                    assert not dialog.reset.isVisible(), 'empty dialogue should not show reset'
                    dialog.grab().save(str(directory / 'advisor.png'))
                    dialog.preferences.click()
                    assert dialog.preference_panel.isVisible()
                    assert not dialog.availability_panel.isVisible()
                    dialog.limited.setChecked(True)
                    assert dialog.availability_panel.isVisible()
                    assert window.advisor.value['availability']['daily_minutes'] == 60
                    dialog.grab().save(str(directory / 'advisor-options.png'))
                    dialog.limited.setChecked(False)
                    assert window.advisor.value['availability'] is None
                    dialog.preferences.click()
                    dialog.close()
                    window.plan_view = 'week'
                    plans_phase = 2
                    window.refresh()
                    return
                report['weekly_plan_view'] = True
            if isinstance(page, LibraryPage):
                if library_phase == 0:
                    library_phase = 1
                    page.articles.item(0).setCheckState(Qt.CheckState.Checked)
                if library_phase == 1 and window.library.key:
                    page.input.setPlainText('阅读')
                    page.preview.click()
                    library_phase = 2
                if library_phase < 2 or window.library.busy:
                    QTimer.singleShot(100, inspect)
                    return
                assert page.evidence.count() > 0, 'local search returned no evidence'
                assert page.input.toPlainText() == '阅读', 'draft was lost'
                search_dialog = QApplication.activeModalWidget()
                assert search_dialog and search_dialog.windowTitle() == '查找依据', 'search results modal missing'
                assert search_dialog.findChild(QListWidget).count() > 0, 'search dialog has no results'
                search_dialog.grab().save(str(directory / 'library-search.png'))
                search_dialog.reject()
                report['library_local_search'] = True
                window.resize(900, 620)
                QApplication.processEvents()
                assert not page.articles.isVisible(), 'scope list should be collapsed'
                assert page.width() <= window.width() - window.sidebar.width(), 'library is too wide'
                input_bottom = page.input.mapTo(page, page.input.rect().bottomRight()).y()
                send_top = page.send.mapTo(page, page.send.rect().topLeft()).y()
                assert input_bottom < send_top, 'library input overlaps send controls'
                assert page.send.mapTo(page, page.send.rect().bottomRight()).y() < page.height(), 'send is clipped'
                window.grab().save(str(directory / 'library-compact.png'))
                scope_before = list(window.library.body['article_ids'])
                dialog_errors = []
                def cancel_scope():
                    dialog = QApplication.activeModalWidget()
                    try:
                        assert dialog is not None, 'scope dialog did not open'
                        assert page.articles.isVisible(), 'scope choices are hidden'
                        dialog.grab().save(str(directory / 'library-scope.png'))
                        page.articles.item(0).setCheckState(Qt.CheckState.Unchecked)
                    except Exception as exc:
                        dialog_errors.append(str(exc))
                    finally:
                        if dialog:
                            dialog.reject()
                QTimer.singleShot(0, cancel_scope)
                page.scope_button.click()
                assert not dialog_errors, str(dialog_errors)
                assert window.library.body['article_ids'] == scope_before, 'cancel changed the scope'
                assert page.input.toPlainText() == '阅读', 'scope dialog lost the question'
                assert not page.articles.isVisible(), 'scope choices remained visible after cancel'
                report['library_scope_compact'] = True
                previous_messages = list(window.library.session.get('messages', []))
                window.library.session['messages'] = [
                    {'role': 'assistant', 'content': '阅读让想法逐渐清晰。 [S1]', 'evidence': [dict(window.library.hits[0], citation='S1')]},
                    {'role': 'user', 'content': '再查看一次来源'},
                    {'role': 'assistant', 'content': '保留每一次好奇。', 'evidence': [window.library.hits[-1]]},
                ]
                page.render()
                page.message_action(QUrl('library:sources:0'))
                assert page.expanded_sources == {0}, 'source groups are not independent'
                assert page.transcript.toPlainText().count('整理为笔记') == 2, 'historical answer action missing'
                citation_cursor = page.transcript.document().find('[S1]')
                assert citation_cursor.charFormat().anchorHref() == 'library:source:0:0', 'inline reference missing'
                window.resize(1380, 880)
                QApplication.processEvents()
                page.transcript.verticalScrollBar().setValue(0)
                window.grab().save(str(directory / 'library-history.png'))
                def close_reference():
                    dialog = QApplication.activeModalWidget()
                    try:
                        assert dialog is not None, 'reference preview did not open'
                        dialog.grab().save(str(directory / 'library-reference.png'))
                    except Exception as exc:
                        dialog_errors.append(str(exc))
                    finally:
                        if dialog:
                            dialog.reject()
                QTimer.singleShot(0, close_reference)
                page.message_action(QUrl('library:source:0:0'))
                assert not dialog_errors, str(dialog_errors)
                window.library.session['messages'] = previous_messages
                page.expanded_sources.clear()
                page.render()
                report['library_history_references'] = True
                previous_session = window.library.session
                window.library.session = dict(previous_session, archives=[
                    {'handoff': '# 第一段交接\n\n保留原文引用。', 'ended_at': '2026-10-23T01:00:00Z'},
                    {'handoff': '# 第二段交接\n\n继续阅读与核对。', 'ended_at': '2026-10-23T02:00:00Z'},
                ])
                page.render()
                assert page.handoff.isVisible(), 'handoff history entry missing'
                def inspect_handoff():
                    dialog = QApplication.activeModalWidget()
                    try:
                        assert dialog and dialog.windowTitle() == '会话交接记录', 'handoff dialog missing'
                        assert len(dialog.entries) == 2, 'archive count differs'
                        assert dialog.pages.currentIndex() == 0, 'history must open as list'
                        assert dialog.document.isReadOnly(), 'handoff should be read-only'
                        dialog.entries[0].click()
                        assert '第一段交接' in dialog.document.toPlainText(), 'selected archive differs'
                        dialog.grab().save(str(directory / 'conversation-handoff.png'))
                        dialog.back.click()
                        assert dialog.pages.currentIndex() == 0, 'return to history list failed'
                        dialog.grab().save(str(directory / 'conversation-handoff-list.png'))
                    except Exception as exc:
                        dialog_errors.append(str(exc))
                    finally:
                        if dialog:
                            dialog.reject()
                QTimer.singleShot(0, inspect_handoff)
                page.handoff.click()
                assert not dialog_errors, str(dialog_errors)
                window.library.session = previous_session
                page.render()
                report['conversation_handoff_history'] = True
                window.resize(1380, 880)
                QApplication.processEvents()
            image = window.grab()
            assert [control.text() for control in window.nav.values()] == [
                '今日', '主题资料库', '计划', '探索统计', '资料库问答', '笔记管理', '内容管理'], 'legacy navigation order changed'
            assert all(not control.icon().isNull() for control in window.nav.values()), 'navigation icon missing'
            assert not window.sidebar.mark.pixmap().isNull(), 'brand asset missing'
            assert window.sidebar.recent.count() > 0, 'recent materials missing'
            report['legacy_navigation'] = True
            if index == 0:
                from ..reading_preferences import PALETTES
                from .widgets import stylesheet
                for palette in sorted(PALETTES):
                    for mode in ('light', 'dark'):
                        preference = {'palette': palette, 'mode': mode}
                        window.setStyleSheet(stylesheet(preference))
                        window.sidebar.update_content(window.snapshot['articles'], preference, 'today')
                        QApplication.processEvents()
                        assert not window.sidebar.mark.pixmap().isNull(), f'{palette} brand missing'
                        assert not window.sidebar.character.icon().isNull(), f'{palette} avatar missing'
                        window.sidebar.grab().save(str(directory / f'sidebar-{palette}-{mode}.png'))
                window.setStyleSheet(stylesheet(window.snapshot['preferences']))
                window.sidebar.update_content(window.snapshot['articles'], window.snapshot['preferences'], 'today')
                report['sidebar_themes'] = True
                check = page.today_checks[0]
                initial = page.today_progress.value()
                def wait_today_task():
                    loop = QEventLoop()
                    timer = QTimer()
                    timer.setInterval(20)
                    timer.timeout.connect(lambda: loop.quit() if check.isEnabled() else None)
                    timer.start()
                    timeout = QTimer()
                    timeout.setSingleShot(True)
                    timeout.timeout.connect(loop.quit)
                    timeout.start(2000)
                    loop.exec()
                    timer.stop()
                    timeout.stop()
                    assert check.isEnabled(), 'task save did not complete'
                check.click()
                wait_today_task()
                assert page.today_progress.value() == initial + 1, 'today progress did not update'
                window.grab().save(str(directory / 'today-completed.png'))
                check.click()
                wait_today_task()
                assert page.today_progress.value() == initial, 'task completion could not be undone'
                page.show_weather({'status':'stale', 'city':{'name':'北京'}, 'temperature':18,
                                   'code':3, 'observed_at':'2026-10-23T08:00'})
                assert '18°C' in page.today_weather.condition.text()
                assert '更新失败' in page.today_weather.updated.text()
                assert not page.today_weather.symbol.pixmap().isNull()
                window.resize(900, 620)
                QApplication.processEvents()
                assert page.today_scroll.horizontalScrollBar().maximum() == 0, 'today content overflows compact viewport'
                window.grab().save(str(directory / 'today-compact.png'))
                page.show_weather({'status':'unconfigured'})
                window.resize(1380, 880)
                report['today_completion_weather'] = True
            assert not image.isNull()
            image.save(str(directory / (routes[index] + '.png')))
            if isinstance(page, ReaderPage):
                assert page.text.toPlainText().strip()
                assert len(page.text.extraSelections()) > 0
                if reader_phase == 0:
                    assert page.tabs.isVisible(), 'wide reader should initially show assistant, matching prototype'
                    assert not page.selection_bar.isVisible(), 'selection tools should start hidden'
                    image.save(str(directory / 'reader-wide.png'))
                    page.assistant_toggle.click()
                    QApplication.processEvents()
                    assert not page.tabs.isVisible()
                    full_width = page.text.width()
                    page.assistant_toggle.click()
                    QApplication.processEvents()
                    assert page.tabs.isVisible() and page.text.width() < full_width
                    window.resize(900, 620)
                    reader_phase = 1
                    QTimer.singleShot(200, inspect)
                    return
                assert window.width() == 900, 'reader minimum layout exceeds compact window'
                assert window.sidebar.width() == 180, 'compact rail width differs from legacy'
                assert window.sidebar.widget().width() <= window.sidebar.viewport().width(), 'sidebar clips horizontally'
                assert page.chat.send.geometry().right() <= page.chat.width(), 'chat controls overflow'
                assert page.chat.input.geometry().bottom() < page.chat.send.geometry().top(), 'composer overlaps send controls'
                page.chat.ensureWidgetVisible(page.chat.send)
                QApplication.processEvents()
                position = page.chat.send.mapTo(page.chat.viewport(), QPoint(0, 0))
                assert position.y() >= 0 and position.y()+page.chat.send.height() <= page.chat.viewport().height(), 'send control cannot be reached by scrolling'
                assert window.topbar.caption.width() >= window.topbar.caption.fontMetrics().horizontalAdvance('悬浮取词'), 'capture caption clipped'
                window.grab().save(str(directory / 'reader-chat-controls.png'))
                page.chat.verticalScrollBar().setValue(0)
                report['compact_topbar_chat'] = True
                assert page.text.horizontalScrollBar().maximum() == 0, 'original image overflows reader'
                for position, _, _ in page.text.images:
                    from PySide6.QtGui import QTextCursor
                    cursor = QTextCursor(page.text.document())
                    cursor.setPosition(position)
                    cursor.movePosition(QTextCursor.MoveOperation.NextCharacter, QTextCursor.MoveMode.KeepAnchor)
                    assert cursor.charFormat().toImageFormat().width() <= page.text.image_width(), 'image width exceeds column'
                report['compact_reader'] = True
                from PySide6.QtGui import QTextCursor
                cursor = page.text.textCursor()
                cursor.setPosition(page.text.ranges[0]['start'])
                cursor.movePosition(QTextCursor.MoveOperation.NextCharacter, QTextCursor.MoveMode.KeepAnchor, 2)
                page.text.setTextCursor(cursor)
                QApplication.processEvents()
                assert page.selection_bar.isVisible()
                assert page.selection_bar.geometry().right() <= page.text.viewport().width(), 'selection toolbar overflows'
                window.grab().save(str(directory / 'reader-selection.png'))
                page.dismiss_selection()
                assert not page.selection_bar.isVisible()
                def inspect_records():
                    dialog = QApplication.activeModalWidget()
                    try:
                        assert dialog is not None
                        filters = dialog.findChild(QTabBar)
                        rows = dialog.findChild(QListWidget)
                        assert filters.count() == 3 and rows.count() == 2
                        filters.setCurrentIndex(1)
                        assert rows.count() == 1 and rows.item(0).data(Qt.ItemDataRole.UserRole)['type'] == 'term'
                        filters.setCurrentIndex(2)
                        assert rows.count() == 1 and rows.item(0).data(Qt.ItemDataRole.UserRole)['type'] == 'image'
                        assert not rows.item(0).icon().isNull()
                        dialog.grab().save(str(directory / 'exploration-records.png'))
                        report['exploration_filters'] = True
                    except Exception as exc:
                        report['errors'].append(repr(exc))
                    finally:
                        if dialog:
                            dialog.reject()
                QTimer.singleShot(150, inspect_records)
                page.show_records('objects')
                assert report.get('exploration_filters')
                def inspect_info():
                    dialog = QApplication.activeModalWidget()
                    try:
                        assert dialog is not None
                        labels = [label.text() for label in dialog.findChildren(QLabel)]
                        assert page.article['title'] in labels
                        assert any('1 张图片' in label for label in labels)
                        dialog.grab().save(str(directory / 'article-info.png'))
                        report['article_information'] = True
                    except Exception as exc:
                        report['errors'].append(repr(exc))
                    finally:
                        if dialog:
                            dialog.reject()
                from .materials import article_info
                QTimer.singleShot(150, inspect_info)
                article_info(window, page.article, page)
                assert report.get('article_information')
                segment = page.text.ranges[0]
                mark = window.service.annotations.save({'article_id': page.article['id'],
                    'version': page.article['version'], 'style': 'yellow',
                    'segments': [{'block': segment['id'], 'start': 0, 'end': 4, 'quote': segment['text'][:4]}]})
                page.bundle['annotations']['items'] = [mark]
                def inspect_marks():
                    dialog = QApplication.activeModalWidget()
                    try:
                        assert dialog is not None
                        controls = dialog.findChildren(QPushButton)
                        swatches = [b for b in controls if b.accessibleName().startswith('改为')]
                        assert len(swatches) == 4 and sum(b.isChecked() for b in swatches) == 1
                        assert any(b.text() == '定位原文' for b in controls)
                        assert any(b.text() == '删除' for b in controls)
                        dialog.grab().save(str(directory / 'reading-marks.png'))
                        report['annotation_controls'] = True
                        next(b for b in swatches if b.accessibleName() == '改为绿色荧光').click()
                        QTimer.singleShot(100, lambda: inspect_mark_saved(dialog, 0))
                    except Exception as exc:
                        report['errors'].append(repr(exc))
                        if dialog:
                            dialog.reject()
                def inspect_mark_saved(dialog, attempt):
                    try:
                        controls = dialog.findChildren(QPushButton)
                        green = next(b for b in controls if b.accessibleName() == '改为绿色荧光')
                        if not green.isEnabled() and attempt < 30:
                            QTimer.singleShot(100, lambda: inspect_mark_saved(dialog, attempt+1))
                            return
                        assert green.isEnabled() and green.isChecked(), 'green control did not update'
                        assert next(item for item in window.service.annotations.list(page.article['id'])['items'] if item['id'] == mark['id'])['style'] == 'green', 'style not persisted'
                        next(b for b in controls if b.text() == '删除').click()
                        QTimer.singleShot(100, lambda: inspect_mark_deleted(dialog, 0))
                    except Exception as exc:
                        report['errors'].append(repr(exc))
                        dialog.reject()
                def inspect_mark_deleted(dialog, attempt):
                    try:
                        if page.bundle['annotations']['items'] and attempt < 30:
                            QTimer.singleShot(100, lambda: inspect_mark_deleted(dialog, attempt+1))
                            return
                        assert not any(item['id'] == mark['id'] for item in window.service.annotations.list(page.article['id'])['items']), 'mark not deleted'
                        assert page.marks_toggle.text() == '标注 0', 'count not updated'
                        assert any(label.text() == '还没有标注。' for label in dialog.findChildren(QLabel)), 'empty state missing'
                        report['annotation_edit_delete'] = True
                    except Exception as exc:
                        report['errors'].append(repr(exc))
                    finally:
                        dialog.reject()
                QTimer.singleShot(150, inspect_marks)
                page.show_records('marks')
                assert report.get('annotation_controls')
                assert report.get('annotation_edit_delete')
                page.show_notes()
                notes = page.notes
                notes.start({'block': segment['id'], 'quote': segment['text'][:4]})
                QApplication.processEvents()
                assert not notes.reference.isChecked() and not notes.quote.isVisible(), 'reference should begin folded'
                notes.reference.click()
                assert notes.quote.isVisible(), 'reference should expand'
                notes.title.setText('原文中的阅读发现')
                notes.content.setPlainText('把理解写下来，再回到原文核对。')
                notes.save.click()
                def wait_note_save():
                    loop = QEventLoop()
                    timer = QTimer()
                    timer.setInterval(20)
                    timer.timeout.connect(lambda: loop.quit() if not notes.draft.busy else None)
                    timer.start()
                    timeout = QTimer()
                    timeout.setSingleShot(True)
                    timeout.timeout.connect(loop.quit)
                    timeout.start(2000)
                    loop.exec()
                    timer.stop()
                    timeout.stop()
                    assert not notes.draft.busy, 'note save did not complete'
                wait_note_save()
                assert notes.draft.body.get('id'), 'inline note not saved'
                note_id = notes.draft.body['id']
                assert any(n['id'] == note_id for n in window.service.hub.notes(page.article['id'])['notes']), 'note missing from store'
                QApplication.processEvents()
                assert notes.horizontalScrollBar().maximum() == 0, 'notes panel overflows compact reader'
                window.grab().save(str(directory / 'reader-notes.png'))
                def confirm_note_delete():
                    dialog = QApplication.activeModalWidget()
                    dialog.button(QMessageBox.StandardButton.Yes).click()
                QTimer.singleShot(100, confirm_note_delete)
                notes.delete.click()
                wait_note_save()
                assert not any(n['id'] == note_id for n in window.service.hub.notes(page.article['id'])['notes']), 'note was not deleted'
                report['inline_notes'] = True
            if isinstance(page, MapPage):
                assert len(page.view.scene().nodes) == 3
                page.view.scene().nodes['n1'].setSelected(True)
                assert '保留每一次好奇' in page.quote.toPlainText()
                report['map_source_preview'] = True
                assert page.images.isVisible()
                assert page.view.scene().nodes['n1'].data(1), 'saved object association missing'
                def inspect_image():
                    dialog = QApplication.activeModalWidget()
                    try:
                        assert dialog is not None
                        tabs = dialog.findChild(QTabWidget)
                        view = tabs.currentWidget()
                        assert isinstance(view, MapView)
                        assert view.scene().itemsBoundingRect().width() == 720
                        before = view.transform().m11()
                        view.zoom(1.25)
                        assert view.transform().m11() > before
                        dialog.grab().save(str(directory / 'map-image-evidence.png'))
                        report['map_image_evidence'] = True
                    except Exception as exc:
                        report['errors'].append(repr(exc))
                    finally:
                        if dialog:
                            dialog.reject()
                QTimer.singleShot(200, inspect_image)
                page.open_images()
                assert report.get('map_image_evidence'), 'image evidence dialog failed'
            report['pages'].append(routes[index])
            index += 1
            if index == len(routes):
                finish()
            elif routes[index] == 'reader':
                def locate_reference():
                    dialog = QApplication.activeModalWidget()
                    try:
                        assert dialog is not None, 'reference preview missing before navigation'
                        control = next(b for b in dialog.findChildren(QPushButton) if b.text() == '在原文中定位')
                        control.click()
                    except Exception as exc:
                        report['errors'].append(repr(exc))
                        if dialog:
                            dialog.reject()
                        finish()
                QTimer.singleShot(0, locate_reference)
                page.open_evidence(page.evidence.item(0))
            elif routes[index] == 'map':
                window.resize(1380, 880)
                window.navigate('map', window.article_id)
            else:
                if routes[index] in window.nav:
                    window.nav[routes[index]].click()
                else:
                    window.navigate(routes[index])
        except Exception as exc:
            report['errors'].append(repr(exc))
            finish()

    def ready():
        nonlocal scheduled
        if not scheduled:
            scheduled = True
            QTimer.singleShot(300, inspect)

    def failed(message):
        report['errors'].append(message)
        finish()

    window.ready.connect(ready)
    window.tasks.error.connect(failed)
    def timeout():
        if not window.closing:
            report['errors'].append('native verification timeout')
            finish()
    QTimer.singleShot(25000, timeout)
