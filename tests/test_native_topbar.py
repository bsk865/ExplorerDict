import unittest
from unittest.mock import Mock, patch
from PySide6.QtWidgets import QApplication, QWidget
from app.native.topbar import TopBar
from app.native.window import NativeWindow
from app.native.widgets import stylesheet


class NativeTopBarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        self.host.closing = False
        self.host.sync_pending = False
        self.host.capture_enabled = False
        self.host.service = Mock()
        self.host.tasks = Mock()
        def submit(action, done, error):
            try:
                result = action()
            except Exception as exc:
                error(str(exc))
            else:
                done(result)
        self.host.tasks.submit.side_effect = submit
        for name in ('go_back', 'refresh_data', 'open_capture', 'model_settings', 'notice', 'refresh', 'navigate', 'filter'):
            setattr(self.host, name, Mock())
        self.bar = TopBar(self.host)
        self.host.topbar = self.bar
        self.host.search = self.bar.search
        self.bar.timer.stop()
        self.host.closing = True
        self.bar.setStyleSheet(stylesheet({'palette':'clay','mode':'light'}))
        self.bar.adapt(900, True)
        self.bar.resize(718, 64)
        self.bar.show()
        self.app.processEvents()
        self.host.closing = False

    def tearDown(self):
        self.host.closing = True
        self.bar.close()
        self.bar.deleteLater()
        self.host.deleteLater()

    def test_compact_capture_caption_fits_and_routes_work(self):
        self.assertTrue(self.bar.capture_label.isHidden())
        self.assertGreaterEqual(self.bar.caption.width(), self.bar.caption.fontMetrics().horizontalAdvance('悬浮取词'))
        self.bar.capture.click()
        self.host.open_capture.assert_called_once()
        self.bar.refresh.click()
        self.host.refresh_data.assert_called_once()
        self.bar.settings.click()
        self.host.model_settings.assert_called_once()
        self.bar.crumb.setText('很长的文章标题' * 50)
        self.app.processEvents()
        self.assertLessEqual(self.bar.settings.geometry().right(), self.bar.width())

    def test_capture_failure_releases_button_for_retry(self):
        with patch('app.capture_bridge.ensure_started', side_effect=ValueError('组件未就绪')):
            NativeWindow.open_capture(self.host)
        self.assertFalse(self.bar.busy)
        self.assertTrue(self.bar.capture.isEnabled())
        self.assertEqual('未能开启，点击重试', self.bar.capture_label.text())
        with patch('app.capture_bridge.ensure_started', return_value={'ready':True,'label':'划选后自动弹出'}) as start:
            NativeWindow.open_capture(self.host)
        start.assert_called_once_with(reveal=True)
        self.assertEqual('划选后自动弹出', self.bar.capture_label.text())

    def test_sync_failure_does_not_block_next_refresh(self):
        self.host.service.workspace.synchronize.side_effect = OSError('同步未完成')
        NativeWindow.synchronize(self.host, manual=True)
        self.assertFalse(self.host.sync_pending)
        self.assertTrue(self.bar.refresh.isEnabled())
        self.host.service.workspace.synchronize.side_effect = None
        self.host.service.workspace.synchronize.return_value = 0
        NativeWindow.synchronize(self.host, manual=True)
        self.host.refresh.assert_called_once()

    def test_global_search_enters_library_and_empty_search_stays(self):
        self.host.current_page = 'today'
        self.bar.search.setText('阅读')
        NativeWindow.search_articles(self.host)
        self.host.navigate.assert_called_once_with('shelf')
        self.host.navigate.reset_mock()
        self.bar.search.clear()
        NativeWindow.search_articles(self.host)
        self.host.navigate.assert_not_called()
        self.host.filter.assert_called_once_with('')


if __name__ == '__main__':
    unittest.main()
