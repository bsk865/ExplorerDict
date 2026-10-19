import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import capture_bridge as bridge
from app.workspace_store import WorkspaceStore


class CaptureBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(bridge, 'data_dir', return_value=self.root),
                        patch.object(bridge, 'project_root', return_value=self.root),
                        patch.object(bridge, '_launch', None)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def test_launch_once_while_starting(self):
        (self.root / '探索词典.exe').touch()
        child = Mock(); child.poll.return_value = None
        with patch.object(bridge, 'running', return_value=False), patch.object(bridge.subprocess, 'Popen', return_value=child) as start:
            bridge.ensure_started(); bridge.ensure_started()
            start.assert_called_once()

    def test_existing_instance_is_revealed_without_duplicate(self):
        with patch.object(bridge, 'running', return_value=True), patch.object(bridge, 'reveal_existing', return_value=True) as reveal, patch.object(bridge.subprocess, 'Popen') as start:
            bridge.ensure_started(reveal=True)
            reveal.assert_called_once(); start.assert_not_called()

    def test_missing_companion_has_actionable_error(self):
        with patch.object(bridge, 'running', return_value=False):
            with self.assertRaisesRegex(ValueError, '重新安装'):
                bridge.ensure_started()

    def test_close_waits_for_companion_startup_receiver(self):
        with patch.object(bridge, 'running', side_effect=[True, True, False]), patch.object(bridge, 'stop_existing', side_effect=[False, True]) as stop, patch.object(bridge.time, 'sleep'):
            self.assertTrue(bridge.stop_and_wait())
            self.assertEqual(stop.call_count, 2)

    def test_stale_or_dead_process_never_claims_ready(self):
        status_file = self.root / 'capture-status.json'
        status_file.write_text(json.dumps({'ready': True, 'updated': time.time() - 10}))
        with patch.object(bridge, 'running', return_value=True):
            self.assertFalse(bridge.status()['ready'])
        status_file.write_text(json.dumps({'ready': True, 'updated': time.time()}))
        with patch.object(bridge, 'running', return_value=False):
            self.assertFalse(bridge.status()['ready'])

    def test_close_waits_even_before_child_creates_mutex(self):
        child = Mock(); child.poll.side_effect = [None, 0]
        with patch.object(bridge, '_launch', child), patch.object(bridge, 'running', return_value=False), patch.object(bridge, 'stop_existing') as stop, patch.object(bridge.time, 'sleep'):
            self.assertTrue(bridge.stop_and_wait())
            stop.assert_called_once()

    def test_reveal_closes_event_even_on_failure(self):
        backend = Mock(); backend.open_event.return_value = (123, 0)
        backend.set_event.side_effect = OSError('unavailable')
        with patch.object(bridge, 'ActivationBackend', return_value=backend):
            with self.assertRaises(OSError):
                bridge.reveal_existing()
        backend.close_handle.assert_called_once_with(123)

    def test_headless_entry_does_not_start_capture(self):
        import reading_desktop
        with patch.object(reading_desktop.sys, 'argv', ['reading_desktop.py', '--headless']), patch.object(bridge, 'start_on_launch') as capture, patch('app.workspace_server.main') as server:
            reading_desktop.main()
        capture.assert_not_called(); server.assert_called_once()

    def test_empty_store_and_demo_hidden_without_deleting_user_data(self):
        store = WorkspaceStore(self.root)
        self.assertEqual(store.summaries(), [])
        demo = store.import_article('旧示例', [{'type': 'text', 'text': '旧示例正文'}], origin='demo')
        own = store.import_article('我的文章', [{'type': 'text', 'text': '自己导入的正文'}])
        self.assertEqual([a['id'] for a in store.summaries()], [own['id']])
        self.assertEqual(store.article(demo['id'])['title'], '旧示例')
