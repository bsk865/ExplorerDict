import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from app.workspace_bridge import send_translation, take_translation
from app.models import CapturedSelection, SourceInfo
from app.ui.reading_panel import ReadingPanel


class WorkspaceHandoffTests(unittest.TestCase):
    def test_handoff_is_local_once_only_and_expires(self):
        with tempfile.TemporaryDirectory() as directory:
            first = send_translation(directory, '原文', '选区来源')
            old = json.loads(first.read_text(encoding='utf-8'))
            old['created'] = time.time() - 601
            first.write_text(json.dumps(old), encoding='utf-8')
            send_translation(directory, 'Second text', '来源二')
            self.assertEqual({'text': 'Second text', 'origin': '来源二'}, take_translation(directory))
            self.assertIsNone(take_translation(directory))
            self.assertEqual([], list((Path(directory)/'workspace-handoffs').glob('*.json')))

    def test_float_sends_current_unsaved_selection_without_requesting_explanation(self):
        host = SimpleNamespace(open_translation=Mock(), db=Mock())
        panel = SimpleNamespace(app=host, entry_id=8, selection=CapturedSelection('Current selected text', source=SourceInfo(title='浏览器文章')),
                                _set_hint=Mock())
        self.assertTrue(ReadingPanel._on_translate(panel))
        host.open_translation.assert_called_once_with('Current selected text', '浏览器文章')
        host.db.get_entry.assert_not_called()
        host.open_translation.reset_mock()
        panel.selection = None
        host.db.get_entry.return_value = dict(term='Stored word', source_title='论文')
        self.assertTrue(ReadingPanel._on_translate(panel))
        host.open_translation.assert_called_once_with('Stored word', '论文')

    def test_existing_native_workspace_is_revealed_without_starting_legacy_reader(self):
        from app import reading_launcher
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'探索词典-原生版.exe').touch()
            with patch.object(reading_launcher, 'project_root', return_value=root), \
                 patch.object(reading_launcher, 'data_dir', return_value=root), \
                 patch('app.capture_bridge.signal_event', return_value=True) as signal, \
                 patch.object(reading_launcher.subprocess, 'Popen') as launch:
                reading_launcher.open_workspace()
            launch.assert_not_called()
            self.assertTrue(signal.call_args.args[0].endswith('.Show'))
