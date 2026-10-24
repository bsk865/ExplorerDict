import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from PySide6.QtCore import QObject, QEvent
from PySide6.QtWidgets import QApplication
from app.native.service import NativeService
from app.native.window import NativeWindow
from app.native.translation import TranslationPage
from app.native.external_links import open_source


class TranslationUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = NativeService(Path(self.temp.name))
        self.service.workspace.rag.request = Mock()
        self.service.workspace.map_preloader.offer = Mock()
        self.capture = patch('app.native.topbar.TopBar.check_capture', lambda self:None)
        self.capture.start()
        self.window = NativeWindow(self.service, discovery_updates=False)
        self.window.show()
        self.wait(lambda: not self.window.navigation_busy and not self.window.tasks.pending)

    def wait(self, predicate):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.01)
        self.fail('UI operation did not finish')

    def tearDown(self):
        self.window.close()
        self.wait(lambda: not self.window.tasks.pending and not any(j.get('status') == 'running' for j in self.service.workspace.jobs.values()))
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.capture.stop()
        self.temp.cleanup()

    def test_language_controls_result_persistence_and_narrow_page(self):
        self.window.open_translation('Hello world.', '演示原文')
        self.wait(lambda: not self.window.navigation_busy)
        page = self.window.translation_tool
        self.assertIsInstance(page, TranslationPage)
        self.window.resize(900, 620)
        page.source.setCurrentIndex(page.source.findData('en'))
        page.target.setCurrentIndex(page.target.findData('ja'))
        client = Mock(model='test-model')
        client.chat.return_value = 'こんにちは、世界。'
        with patch.object(self.service.workspace, 'client', return_value=client):
            page.start.click()
            self.wait(lambda: self.window.translation.job is None)
        self.assertEqual('こんにちは、世界。', page.result.toPlainText())
        self.assertIn('英语翻译为日语', client.chat.call_args.args[0][0]['content'])
        self.assertEqual('Hello world.', page.original.toPlainText())
        self.assertTrue(self.window.translation.state['complete'])
        self.window.navigate('shelf')
        self.wait(lambda: not self.window.navigation_busy)
        self.window.open_translation(origin='演示原文')
        page = self.window.translation_tool
        self.assertEqual('ja', page.target.currentData())
        self.assertEqual('こんにちは、世界。', page.result.toPlainText())
        self.assertIsNone(QApplication.activeModalWidget())
        self.assertLessEqual(page.minimumSizeHint().width(), page.width())
        page.original.setPlainText('New input')
        self.assertEqual('', page.result.toPlainText())
        self.assertNotIn('翻译完成', page.status.text())
        self.window.translation.flush()
        self.assertEqual('New input', self.service.store.get('settings', page.controller.storage_key)['text'])

    def test_running_translation_survives_navigation_without_new_page_or_popup(self):
        entered, released = threading.Event(), threading.Event()
        client = Mock(model='test-model')
        def reply(*args, **kwargs):
            entered.set()
            released.wait(3)
            return '你好'
        client.chat.side_effect = reply
        self.window.open_translation('Hello')
        self.wait(lambda: not self.window.navigation_busy)
        try:
            with patch.object(self.service.workspace, 'client', return_value=client):
                self.window.translation.start()
                self.wait(entered.is_set)
                self.window.navigate('shelf')
                self.wait(lambda: not self.window.navigation_busy)
                released.set()
                self.wait(lambda: self.window.translation.job is None)
                self.assertEqual('shelf', self.window.current_page)
                self.assertIsNone(QApplication.activeModalWidget())
                self.assertEqual('你好', self.window.translation.state['result'])
        finally:
            released.set()

    def test_reader_translation_uses_original_text_and_browser_conflict_stays_inline(self):
        article = self.service.import_text('原文', 'Original paragraph.')
        self.window.open_article(article['id'])
        self.wait(lambda: not self.window.navigation_busy)
        self.window.stack.currentWidget().translate()
        self.wait(lambda: not self.window.navigation_busy)
        self.assertEqual('Original paragraph.', self.window.translation.state['text'])
        with patch('app.native.external_links.edge_privilege_conflict', return_value=True), \
             patch('PySide6.QtGui.QDesktopServices.openUrl') as browser:
            self.assertFalse(open_source(self.window, 'https://arxiv.org/abs/2610.12345'))
        browser.assert_not_called()
        self.assertIn('权限不同', self.window.status.text())
        self.assertIsNone(QApplication.activeModalWidget())

    def test_float_handoff_keeps_current_draft_and_language_choices_without_api_call(self):
        from app.workspace_bridge import send_translation
        self.window.open_translation('Existing draft', '悬浮选区')
        self.wait(lambda: not self.window.navigation_busy)
        page = self.window.translation_tool
        page.source.setCurrentIndex(page.source.findData('en'))
        page.target.setCurrentIndex(page.target.findData('ja'))
        self.window.navigate('shelf')
        self.wait(lambda: not self.window.navigation_busy)
        with patch.object(self.service.workspace, 'start') as start:
            send_translation(self.service.store.directory, 'New selected text', '悬浮选区')
            self.window.receive_handoff()
            self.wait(lambda: not self.window.navigation_busy)
            page = self.window.translation_tool
            self.assertIsInstance(page, TranslationPage)
            self.assertEqual('Existing draft', page.original.toPlainText())
            self.assertFalse(page.incoming.isHidden())
            page.load_incoming.click()
            self.assertEqual('New selected text', page.original.toPlainText())
            self.assertEqual('en', page.source.currentData())
            self.assertEqual('ja', page.target.currentData())
            self.assertNotIn('incoming', self.window.translation.state)
            start.assert_not_called()
        self.assertIsNone(QApplication.activeModalWidget())

    def test_html_only_article_translation_uses_its_visible_text_and_keeps_other_result_separate(self):
        article = self.service.store.import_article('富文本文章', [dict(type='text', html='<p>Hello <b>world</b>.</p>')])
        other = self.service.import_text('另一篇文章', 'Other article.')
        other_controller = self.window.translation_for('article:'+other['id'])
        other_controller.state.update(text='Other article.', result='另一篇的译文', complete=True)
        other_controller.flush()
        self.window.open_article(article['id'])
        self.wait(lambda: not self.window.navigation_busy)
        reader = self.window.stack.currentWidget()
        with patch.object(self.service.workspace, 'start') as start:
            reader.translate()
            start.assert_not_called()
        self.assertEqual('Hello world.', reader.translation_page.original.toPlainText())
        self.assertEqual('', reader.translation_page.result.toPlainText())
        self.assertEqual('article:'+article['id'], reader.translation_page.controller.scope)
        self.assertIsNone(QApplication.activeModalWidget())
        self.assertEqual('另一篇的译文', other_controller.state['result'])

    def test_articles_keep_translations_languages_and_background_jobs_separate(self):
        first = self.service.import_text('Article A', 'First original.')
        second = self.service.import_text('Article B', 'Second original.')
        self.assertNotIn('translate', self.window.nav)
        entered, released = threading.Event(), threading.Event()
        client = Mock(model='test-model')
        def reply(*args, **kwargs):
            entered.set()
            released.wait(3)
            return '第一篇译文'
        client.chat.side_effect = reply
        self.window.open_article(first['id'])
        self.wait(lambda: not self.window.navigation_busy)
        reader = self.window.stack.currentWidget()
        reader.translate()
        page = reader.translation_page
        page.target.setCurrentIndex(page.target.findData('ja'))
        a = page.controller
        self.assertEqual('reader', self.window.current_page)
        self.assertIs(reader.reading_tools.currentWidget(), page)
        try:
            with patch.object(self.service.workspace, 'client', return_value=client):
                a.start()
                self.wait(entered.is_set)
                self.window.open_article(second['id'])
                self.wait(lambda: not self.window.navigation_busy)
                other = self.window.stack.currentWidget()
                self.assertIsNone(other.translation_page)
                other.translate()
                b = other.translation_page.controller
                self.assertEqual('Second original.', b.state['text'])
                self.assertEqual('', b.state['result'])
                self.assertEqual('zh', b.state['target'])
                self.assertIsNone(b.job)
                released.set()
                self.wait(lambda: a.job is None)
                self.assertEqual('', other.translation_page.result.toPlainText())
                self.assertEqual(second['id'], self.window.article_id)
                b.draft(target='en')
                b.flush()
        finally:
            released.set()
        self.window.open_article(first['id'])
        self.wait(lambda: not self.window.navigation_busy)
        reader = self.window.stack.currentWidget()
        reader.translate()
        self.assertEqual('第一篇译文', reader.translation_page.result.toPlainText())
        self.assertEqual('ja', reader.translation_page.target.currentData())
        reader.show_original()
        self.assertIs(reader.reading_tools.currentWidget(), reader.paper)
        from app.native.translation import TranslationController
        restored_a = TranslationController(self.window, 'article:' + first['id'])
        restored_b = TranslationController(self.window, 'article:' + second['id'])
        self.assertEqual('第一篇译文', restored_a.state['result'])
        self.assertEqual('ja', restored_a.state['target'])
        self.assertEqual('', restored_b.state['result'])
        self.assertEqual('en', restored_b.state['target'])

    def test_deleted_article_translation_cannot_return_on_delayed_save_or_restart(self):
        first = self.service.import_text('待删除的文章', 'Text to remove.')
        second = self.service.import_text('保留的文章', 'Text to keep.')
        a = self.window.translation_for('article:' + first['id'])
        b = self.window.translation_for('article:' + second['id'])
        a.prefill('Text to remove.', first['title'])
        a.state.update(result='待删除译文', complete=True)
        a.flush()
        b.prefill('Text to keep.', second['title'])
        b.state.update(result='保留译文', complete=True)
        b.flush()
        a.draft(text='尚未保存的修改')
        self.service.remove('articles', first['id'])
        self.assertIsNone(self.service.store.get('settings', a.storage_key))
        self.assertFalse(a.flush())
        with patch.object(self.service.workspace, 'start') as start:
            a.start()
            start.assert_not_called()
        a.shutdown()
        self.assertIsNone(self.service.store.get('settings', a.storage_key))
        self.assertEqual('保留译文', self.service.store.get('settings', b.storage_key)['result'])
        self.assertEqual('保留译文', b.state['result'])

    def test_article_translation_resumes_after_reopening_and_can_restart(self):
        from app.api_client import ApiError
        from app.native.translation import TranslationController, TranslationPage
        first = self.service.import_text('Long article', 'A' * 2400 + 'Last paragraph.')
        second = self.service.import_text('Other article', 'Other original.')
        controller = self.window.translation_for('article:' + first['id'])
        controller.prefill(first['text'], first['title'])
        client = Mock(base_url='https://example.org/v1', model='test-model', reasoning_effort='high', thinking='')
        client.chat.side_effect = ['第一段译文', ApiError('network', 'Interrupted')]
        with patch.object(self.service.workspace, 'client', return_value=client):
            controller.start()
            self.wait(lambda: controller.job is None)
        saved = self.service.store.get('settings', controller.storage_key)
        self.assertEqual(['第一段译文'], saved['checkpoint']['parts'])
        self.assertFalse(saved['complete'])
        restored = TranslationController(self.window, controller.scope)
        page = TranslationPage(self.window, restored)
        self.assertEqual('继续翻译', page.start.text())
        self.assertFalse(page.restart.isHidden())
        other = self.window.translation_for('article:' + second['id'])
        self.assertEqual('', other.state['result'])
        self.assertNotIn('checkpoint', other.state)
        client.chat.reset_mock()
        client.chat.side_effect = ['末段译文']
        with patch.object(self.service.workspace, 'client', return_value=client):
            page.start.click()
            self.wait(lambda: restored.job is None)
        self.assertEqual(1, client.chat.call_count)
        self.assertEqual('Last paragraph.', client.chat.call_args.args[0][1]['content'])
        self.assertEqual('第一段译文\n\n末段译文', page.result.toPlainText())
        self.assertEqual('重新翻译', page.start.text())
        self.assertTrue(page.restart.isHidden())
        # Explicit restart from an incomplete saved draft must discard all old segments.
        restarted = TranslationController(self.window, controller.scope)
        restarted.state.update(saved)
        restart_page = TranslationPage(self.window, restarted)
        client.chat.reset_mock()
        client.chat.side_effect = ['新第一段', '新末段']
        with patch.object(self.service.workspace, 'client', return_value=client):
            restart_page.restart.click()
            self.wait(lambda: restarted.job is None)
        self.assertEqual(2, client.chat.call_count)
        self.assertEqual('新第一段\n\n新末段', restarted.state['result'])
        self.assertEqual('', other.state['result'])
        page.deleteLater()
        restart_page.deleteLater()

    def test_shutdown_saves_completed_segments_before_next_reply_returns(self):
        from app.native.translation import TranslationController
        article = self.service.import_text('Long article', 'A' * 2400 + 'Last paragraph.')
        controller = self.window.translation_for('article:' + article['id'])
        controller.prefill(article['text'], article['title'])
        entered, released = threading.Event(), threading.Event()
        client = Mock(base_url='https://example.org/v1', model='test-model', reasoning_effort='high', thinking='')
        def reply(messages, **kwargs):
            if messages[1]['content'] == 'Last paragraph.':
                entered.set()
                released.wait(3)
                return '不能在停止后写入的末段'
            return '已完成的第一段'
        client.chat.side_effect = reply
        try:
            with patch.object(self.service.workspace, 'client', return_value=client):
                controller.start()
                self.wait(entered.is_set)
                job_id = controller.job
                # No timer poll is required before exiting: shutdown reads the live checkpoint.
                controller.shutdown()
                saved = self.service.store.get('settings', controller.storage_key)
                self.assertEqual('已完成的第一段', saved['result'])
                self.assertEqual(['已完成的第一段'], saved['checkpoint']['parts'])
                self.assertFalse(saved['complete'])
                released.set()
                self.wait(lambda: self.service.workspace.jobs[job_id]['status'] != 'running')
                controller.poll()
        finally:
            released.set()
        restored = TranslationController(self.window, controller.scope)
        self.assertEqual('已完成的第一段', restored.state['result'])
        self.assertEqual(['已完成的第一段'], restored.state['checkpoint']['parts'])

    def test_discovery_and_floating_text_cannot_replace_article_translation(self):
        article = self.service.import_text('Bound article', 'Article text.')
        self.window.open_article(article['id'])
        self.wait(lambda: not self.window.navigation_busy)
        reader = self.window.stack.currentWidget()
        reader.translate()
        controller = reader.translation_page.controller
        controller.state.update(result='文章译文', complete=True)
        controller.flush()
        controller.changed.emit()
        from app.workspace_bridge import send_translation
        send_translation(self.service.store.directory, 'Floating text', '悬浮选区')
        self.window.receive_handoff()
        self.assertEqual('Floating text', self.window.translation_tool.original.toPlainText())
        self.assertEqual('文章译文', controller.state['result'])
        self.assertEqual('reader', self.window.current_page)
        self.window.close_translation()
        self.assertIs(self.window.content_area.currentWidget(), self.window.stack)
        self.assertEqual('文章译文', reader.translation_page.result.toPlainText())
        self.window.open_translation('Preview A', 'Title A', 'discovery:a')
        self.window.translation.state.update(result='摘要译文 A', complete=True)
        self.window.translation.flush()
        self.window.open_translation('Preview B', 'Title B', 'discovery:b')
        self.assertEqual('', self.window.translation_tool.result.toPlainText())
        self.window.open_translation('Preview A', 'Title A', 'discovery:a')
        self.assertEqual('摘要译文 A', self.window.translation_tool.result.toPlainText())
        self.assertIsNone(QApplication.activeModalWidget())


if __name__ == '__main__':
    unittest.main()
