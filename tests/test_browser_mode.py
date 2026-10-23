import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app import browser_mode


class BrowserModeTests(unittest.TestCase):
    def test_reuses_running_service(self):
        guard = MagicMock()
        guard.acquire.return_value = False
        with patch.object(browser_mode, 'data_dir', return_value=Path('isolated')), \
                patch.object(browser_mode, 'SingleInstance', return_value=guard), \
                patch.object(browser_mode, 'existing_url', return_value='http://127.0.0.1:1234/session?token=test'), \
                patch('webbrowser.open') as opened:
            browser_mode.main()
        opened.assert_called_once()
        guard.release.assert_not_called()

    def test_starts_browser_server_and_releases_guard(self):
        guard = MagicMock()
        guard.acquire.return_value = True
        with patch.object(browser_mode, 'data_dir', return_value=Path('isolated')), \
                patch.object(browser_mode, 'SingleInstance', return_value=guard), \
                patch('app.workspace_server.main') as serve, \
                patch('sys.argv', ['browser_entry.pyw']):
            browser_mode.main()
        serve.assert_called_once()
        guard.release.assert_called_once()

    def test_rejects_external_session_address(self):
        directory = MagicMock()
        (directory / 'workspace-session-v7.json').read_text.return_value = '{"url":"https://example.com/session"}'
        with patch('urllib.request.build_opener') as opener:
            self.assertIsNone(browser_mode.existing_url(directory))
        opener.assert_not_called()


if __name__ == '__main__':
    unittest.main()
