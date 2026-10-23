import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from app.native.service import NativeService
from app.native.window import NativeWindow
from app.native.shelf import ShelfPage
from app.native.content import ContentPage
from app.native.reader import ReaderPage


class NativeRouteHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = NativeService(Path(self.temp.name))
        self.service.workspace.map_preloader.offer = Mock()
        self.service.workspace.rag.request = Mock()
        self.article = self.service.import_text('阅读资料', '用于返回路径验证的原文。')
        self.theme = self.service.hub.save('themes', {'title':'我的主题'})
        self.service.hub.assign(self.theme['id'], [self.article['id']], 'add')
        for i in range(16):
            self.service.hub.save('notes', {'article_id':self.article['id'], 'title':f'心得{i}', 'content':'阅读记录。'*15})
        self.capture_patch = patch('app.native.topbar.TopBar.check_capture', lambda self: None)
        self.capture_patch.start()
        self.window = NativeWindow(self.service, discovery_updates=False)
        self.window.show()
        self.wait(lambda: self.window.stack.currentWidget() is not None)

    def wait(self, condition):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            self.app.processEvents()
            if condition():
                return
            time.sleep(.01)
        self.fail('navigation did not finish')

    def move(self, page):
        old = self.window.stack.currentWidget()
        self.window.navigate(page)
        self.wait(lambda: self.window.stack.currentWidget() is not old)

    def tearDown(self):
        self.window.close()
        self.wait(lambda: not self.window.tasks.pending)
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.capture_patch.stop()
        self.temp.cleanup()

    def test_return_from_article_restores_theme_and_search_without_redirect(self):
        self.move('shelf')
        page = self.window.stack.currentWidget()
        page.select_theme(self.theme['id'])
        self.window.search.setText('阅读')
        self.window.search_timer.stop()
        self.window.filter('阅读')
        self.window.open_article(self.article['id'])
        self.wait(lambda: isinstance(self.window.stack.currentWidget(), ReaderPage))
        self.window.search.setText('暂存搜索')
        count = len(self.window.history)
        self.window.go_back()
        self.window.go_back()
        self.assertEqual(count - 1, len(self.window.history))
        self.assertFalse(self.window.search_timer.isActive())
        self.wait(lambda: isinstance(self.window.stack.currentWidget(), ShelfPage))
        self.assertEqual(self.theme['id'], self.window.stack.currentWidget().theme)
        self.assertEqual('阅读', self.window.search.text())
        old = self.window.stack.currentWidget()
        self.window.go_back()
        self.wait(lambda: self.window.stack.currentWidget() is not old)
        self.assertEqual('', self.window.stack.currentWidget().theme)

    def test_delete_note_keeps_all_library_scope(self):
        note = self.service.hub.notes(self.article['id'])['notes'][0]
        self.window.library.body.update(scope='all', article_ids=[self.article['id']])
        with patch('app.native.window.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes):
            self.window.delete('notes', note['id'], note['title'])
        self.wait(lambda: not self.window.tasks.pending)
        self.assertEqual('all', self.window.library.body['scope'])
        saved = self.service.store.get('settings', 'native-library-scope', {})
        self.assertEqual('all', saved['scope'])
        self.assertNotIn(note['id'], [n['id'] for n in self.service.hub.notes(self.article['id'])['notes']])

    def test_narrow_reader_object_actions_wrap_without_horizontal_overflow(self):
        obj = self.service.store.object(self.article['id'], {'type': 'term', 'block': 'b0', 'title': '原文'})
        self.window.resize(900, 620)
        self.window.open_article(self.article['id'])
        self.wait(lambda: isinstance(self.window.stack.currentWidget(), ReaderPage))
        reader = self.window.stack.currentWidget()
        for _ in range(5):
            self.app.processEvents()
        reader.set_assistant_visible(True)
        chat = reader.chat
        chat.set_focus(obj['id'])
        try:
            for closed, job in [(False, None), (False, 'fixture'), (True, None)]:
                chat.controller.session['closed'] = closed
                chat.controller.job = job
                chat.render()
                for _ in range(20):
                    self.app.processEvents()
                self.assertEqual(0, chat.horizontalScrollBar().maximum())
                self.assertLessEqual(chat.widget().width(), chat.viewport().width())
                controls = [chat.explain, chat.saved, chat.understood, chat.note, chat.send,
                            chat.stop if job else chat.restart if closed else chat.send]
                for control in controls:
                    self.assertTrue(control.isVisible())
                    self.assertGreaterEqual(control.geometry().left(), 0)
                    self.assertLess(control.geometry().right(), chat.widget().width())
                self.assertFalse(chat.understood.geometry().intersects(chat.explain.geometry()))
                chat.ensureWidgetVisible(chat.send)
                self.app.processEvents()
                self.assertTrue(chat.viewport().rect().contains(chat.send.mapTo(chat.viewport(), chat.send.rect().center())))
        finally:
            chat.controller.job = None

    def test_return_restores_management_filter_and_scroll(self):
        self.move('notes')
        page = self.window.stack.currentWidget()
        page.search.setText('心得')
        page.article.setCurrentIndex(page.article.findData(self.article['id']))
        self.app.processEvents()
        page.scroll.verticalScrollBar().setValue(120)
        self.assertEqual(120, page.scroll.verticalScrollBar().value())
        self.window.open_article(self.article['id'])
        self.wait(lambda: isinstance(self.window.stack.currentWidget(), ReaderPage))
        self.window.content_filters = {'kind':'articles', 'query':'different', 'article':''}
        self.window.go_back()
        self.wait(lambda: isinstance(self.window.stack.currentWidget(), ContentPage))
        restored = self.window.stack.currentWidget()
        self.wait(lambda: restored.scroll.verticalScrollBar().value() == 120)
        self.assertEqual('notes', restored.kind)
        self.assertEqual('心得', restored.search.text())
        self.assertEqual(self.article['id'], restored.article.currentData())

    def test_return_restores_reader_fold_and_tab_from_history(self):
        obj = self.service.store.object(self.article['id'], {
            'type': 'term', 'title': '原文', 'block': self.article['blocks'][0]['id']})
        self.window.open_article(self.article['id'])
        self.wait(lambda: isinstance(self.window.stack.currentWidget(), ReaderPage))
        self.app.processEvents()
        page = self.window.stack.currentWidget()
        page.chat.set_focus(obj['id'])
        page.tabs.setCurrentIndex(1)
        page.set_assistant_visible(False)
        self.move('shelf')
        # A newer per-article preference must not replace this route's snapshot.
        self.service.store.put('settings', 'native-reading:' + self.article['id'], {'tab': 0})
        self.window.go_back()
        self.wait(lambda: isinstance(self.window.stack.currentWidget(), ReaderPage))
        self.app.processEvents()
        restored = self.window.stack.currentWidget()
        self.assertEqual(1, restored.tabs.currentIndex())
        self.assertEqual(obj['id'], restored.chat.focus.currentData())
        self.assertFalse(restored.assistant_visible)
        restored.set_assistant_visible(True)
        self.move('shelf')
        self.window.go_back()
        self.wait(lambda: isinstance(self.window.stack.currentWidget(), ReaderPage))
        self.app.processEvents()
        self.assertTrue(self.window.stack.currentWidget().assistant_visible)

    def test_library_return_restores_scope_and_its_draft(self):
        other = self.service.import_text('第二份资料', '另一份用于比较的文字资料。')
        self.move('library')
        self.window.library.select([self.article['id']], True, 'question')
        self.wait(lambda: bool(self.window.library.key))
        original_key = self.window.library.key
        self.window.stack.currentWidget().input.setPlainText('第一份资料的待提问内容')
        self.move('shelf')
        self.move('library')
        self.window.library.select([other['id']], False, 'question')
        self.wait(lambda: bool(self.window.library.key))
        other_key = self.window.library.key
        self.window.stack.currentWidget().input.setPlainText('第二份资料的待提问内容')
        self.window.go_back()
        self.wait(lambda: isinstance(self.window.stack.currentWidget(), ShelfPage))
        history_count = len(self.window.history)
        self.window.library.busy = True
        with patch.object(self.window, 'notice', lambda message: None):
            self.window.go_back()
        self.assertEqual(history_count, len(self.window.history))
        self.assertEqual(other_key, self.window.library.key)
        self.window.library.busy = False
        self.window.go_back()
        self.wait(lambda: self.window.current_page == 'library' and not self.window.navigation_busy
                  and self.window.library.key == original_key)
        page = self.window.stack.currentWidget()
        self.assertEqual([self.article['id']], self.window.library.body['article_ids'])
        self.assertTrue(page.notes.isChecked())
        self.assertEqual('第一份资料的待提问内容', page.input.toPlainText())
        self.assertEqual('第二份资料的待提问内容', self.service.store.get('sessions', other_key)['draft'])

    def test_library_return_excludes_removed_articles(self):
        self.move('library')
        self.window.library.select([self.article['id']], True, 'question')
        self.wait(lambda: bool(self.window.library.key))
        self.move('shelf')
        self.service.remove('articles', self.article['id'])
        self.window.go_back()
        self.wait(lambda: self.window.current_page == 'library' and not self.window.navigation_busy)
        self.assertEqual([], self.window.library.body['article_ids'])
        self.assertIsNone(self.window.library.key)
        self.assertFalse(self.window.stack.currentWidget().send.isEnabled())

    def test_search_and_back_keyboard_shortcuts(self):
        self.move('shelf')
        self.move('notes')
        self.window.activateWindow()
        self.app.processEvents()
        self.window.search.blockSignals(True)
        self.window.search.setText('待检索的文章')
        self.window.search.blockSignals(False)
        QTest.keyClick(self.window, Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier)
        self.app.processEvents()
        self.assertTrue(self.window.search.hasFocus())
        self.assertEqual('待检索的文章', self.window.search.selectedText())
        QTest.keyClick(self.window, Qt.Key.Key_Left, Qt.KeyboardModifier.AltModifier)
        self.wait(lambda: isinstance(self.window.stack.currentWidget(), ShelfPage))

    def test_control_enter_uses_article_and_library_send_actions(self):
        self.window.open_article(self.article['id'])
        self.wait(lambda: isinstance(self.window.stack.currentWidget(), ReaderPage))
        chat = self.window.stack.currentWidget().chat
        sent = []
        chat.input.setPlainText('根据原文解释这个问题')
        with patch.object(chat.controller, 'send', lambda text, scope: sent.append((text, scope))):
            QTest.keyClick(chat.input, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
            self.assertEqual([('根据原文解释这个问题', 'article')], sent)
            chat.controller.session['closed'] = True
            chat.render()
            QTest.keyClick(chat.input, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(1, len(sent))
        self.move('library')
        self.window.library.select([self.article['id']], True, 'question')
        self.wait(lambda: bool(self.window.library.key))
        page = self.window.stack.currentWidget()
        page.input.setPlainText('比较原文与我的笔记')
        with patch.object(self.window.library, 'request', lambda text: sent.append(text)):
            QTest.keyClick(page.input, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
            self.assertEqual('比较原文与我的笔记', sent[-1])
            self.window.library.busy = True
            self.window.library.changed.emit()
            QTest.keyClick(page.input, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(2, len(sent))
        self.window.library.busy = False


if __name__ == '__main__':
    unittest.main()
