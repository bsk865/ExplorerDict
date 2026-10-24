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
from app.native.discover import DiscoverPage, refresh_message
from app.reading_preferences import PALETTES
from app.native.widgets import stylesheet
from tests.test_discovery import FEED, SOURCE
from app.discovery import parse_feed


class WindowWatcher(QObject):
    def __init__(self):
        super().__init__()
        self.shown = []

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Show and hasattr(obj, 'isWindow') and obj.isWindow():
            self.shown.append(type(obj).__name__)
        return False


class NativeDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = NativeService(Path(self.temp.name))
        self.service.workspace.map_preloader.offer = Mock()
        self.service.workspace.rag.request = Mock()
        prefs = self.service.discovery.preferences()
        prefs['sources'] = [SOURCE]
        self.service.discovery.save_preferences(prefs)
        self.service.store.put('settings', 'discovery-state', {'items':parse_feed(FEED, SOURCE), 'updated':0})
        self.capture = patch('app.native.topbar.TopBar.check_capture', lambda self:None)
        self.capture.start()
        self.window = NativeWindow(self.service, discovery_updates=False)
        self.window.show()
        self.wait(lambda: not self.window.navigation_busy and not self.window.tasks.pending)
        self.watch = WindowWatcher()
        self.app.installEventFilter(self.watch)

    def wait(self, condition):
        end = time.monotonic()+5
        while time.monotonic()<end:
            self.app.processEvents()
            if condition():
                return
            time.sleep(.01)
        self.fail('operation did not finish')

    def tearDown(self):
        self.app.removeEventFilter(self.watch)
        self.window.close()
        self.wait(lambda:not self.window.tasks.pending)
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.capture.stop()
        self.temp.cleanup()

    def test_inline_settings_details_filters_and_navigation_without_popups(self):
        self.assertIsInstance(self.window.stack.currentWidget(), DiscoverPage)
        self.window.resize(900,620)
        page = self.window.stack.currentWidget()
        for palette in PALETTES:
            self.window.setStyleSheet(stylesheet({'palette':palette,'mode':'dark'}))
            page.show_preferences()
            for _ in range(5):self.app.processEvents()
            self.assertEqual(0,page.stack.currentWidget().horizontalScrollBar().maximum())
            page.back()
        page.show_detail(self.service.discovery.state()['items'][0]['id'])
        self.app.processEvents()
        self.assertEqual(1,page.stack.currentIndex())
        page.back()
        page.tabs['news'].click()
        page.tabs['saved'].click()
        page.tabs['all'].click()
        self.assertEqual([],self.watch.shown)
        self.assertIsNone(self.app.activePopupWidget())
        self.assertIsNone(self.app.activeModalWidget())
        for target in ['shelf','discover','today','discover']:
            self.window.navigate(target)
            self.wait(lambda:not self.window.navigation_busy and not self.window.tasks.pending)
        self.assertEqual([],self.watch.shown)

    def test_late_refresh_does_not_reopen_page_or_pop_up(self):
        entered, release = threading.Event(), threading.Event()
        def refresh():
            entered.set()
            release.wait(3)
            return {'errors':['Example：更新失败，保留上次内容']}
        try:
            with patch.object(self.service.discovery,'refresh',refresh):
                self.window.discovery.refresh()
                self.wait(entered.is_set)
                self.window.navigate('shelf')
                self.wait(lambda:not self.window.navigation_busy)
                release.set()
                self.wait(lambda:not self.window.discovery.busy)
                self.assertEqual('shelf',self.window.current_page)
                self.assertIn('更新失败',self.window.discovery.message)
                self.assertEqual([],self.watch.shown)
        finally:
            release.set()

    def test_many_failed_sources_have_compact_status_and_details_in_settings(self):
        state = self.service.discovery.state()
        names = ['来源名称' * 20 + str(i) for i in range(12)]
        state.update(errors=[name + '：更新失败，保留上次内容' for name in names],
                     refresh_counts={'sources': 12, 'failed': 12, 'empty': 0, 'articles': 0})
        self.window.resize(900, 620)
        page = self.window.stack.currentWidget()
        self.window.discovery.message = refresh_message(state)
        page.render()
        self.app.processEvents()
        self.assertIn('全部 12 个来源更新失败', page.status.text())
        self.assertLess(len(page.status.text()), 100)
        self.assertGreater(page.scroll.viewport().height(), 100)
        self.assertEqual([], self.watch.shown)
        self.assertIn('尚未启用', refresh_message(dict(refresh_counts=dict(sources=0, failed=0, empty=0, articles=0))))

    def test_website_subscription_can_finish_after_navigation_without_popup(self):
        entered,release=threading.Event(),threading.Event()
        candidate=dict(SOURCE,id='new-feed',name='Publisher news',url='https://example.org/new-feed')
        def probe(_):
            entered.set();release.wait(3)
            return [candidate]
        try:
            page=self.window.stack.currentWidget()
            page.show_preferences()
            page.subscription.url.setText('https://example.org')
            before=len(self.service.discovery.preferences()['sources'])
            with patch('app.native.discover.find_feeds',side_effect=probe):
                page.subscription.find.click()
                self.wait(entered.is_set)
                self.window.navigate('shelf')
                self.wait(lambda:not self.window.navigation_busy)
                release.set()
                self.wait(lambda:not self.window.discovery.busy)
            self.assertEqual('shelf',self.window.current_page)
            self.assertEqual(before,len(self.service.discovery.preferences()['sources']))
            self.window.navigate('discover')
            self.wait(lambda:not self.window.navigation_busy)
            page=self.window.stack.currentWidget()
            page.show_preferences()
            self.assertEqual(1,page.subscription.choices.count())
            self.window.resize(900,620)
            for _ in range(5):self.app.processEvents()
            self.assertEqual(0,page.stack.currentWidget().horizontalScrollBar().maximum())
            page.subscription.subscribe.click()
            self.assertEqual(before+1,len(self.service.discovery.preferences()['sources']))
            self.assertEqual('https://example.org/new-feed',self.service.discovery.preferences()['sources'][-1]['url'])
            self.assertEqual([],self.watch.shown)
            self.assertIsNone(self.app.activeModalWidget())
        finally:release.set()


if __name__ == '__main__':
    unittest.main()
