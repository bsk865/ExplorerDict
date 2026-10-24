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
        page = self.window.stack.currentWidget()
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
        self.window.navigate('translate')
        self.wait(lambda: not self.window.navigation_busy)
        page = self.window.stack.currentWidget()
        self.assertEqual('ja', page.target.currentData())
        self.assertEqual('こんにちは、世界。', page.result.toPlainText())
        self.assertIsNone(QApplication.activeModalWidget())
        self.assertLessEqual(page.minimumSizeHint().width(), page.width())
        page.original.setPlainText('New input')
        self.assertEqual('', page.result.toPlainText())
        self.assertNotIn('翻译完成', page.status.text())
        self.window.translation.flush()
        self.assertEqual('New input', self.service.store.get('settings', 'native-translation')['text'])

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


if __name__ == '__main__':
    unittest.main()
