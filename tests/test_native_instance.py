import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from PySide6.QtWidgets import QApplication
from app.native.instance import acquire_reader, Registration, read_owner


class ReaderInstanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_registration_removes_only_its_own_record(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Registration(directory), Registration(directory)
            first.write()
            second.write()
            first.close()
            self.assertEqual(read_owner(directory)['token'], second.value['token'])
            second.close()
            self.assertEqual(read_owner(directory), {})

    def test_same_build_only_activates_existing_window(self):
        guard = Mock()
        guard.acquire.return_value = False
        with patch('app.native.instance.read_owner', return_value={'pid': 3, 'build': 'same'}), \
             patch('app.native.instance.build_identity', return_value='same'), \
             patch('app.native.instance.legacy_reader', return_value=None), \
             patch('app.capture_bridge.signal_event', return_value=True) as signal:
            acquired, _ = acquire_reader(self.app, guard, Path('.'), 'test')
        self.assertFalse(acquired)
        signal.assert_called_once_with('test.Show')
        guard.release.assert_called_once()

    def test_different_build_waits_for_mutex_after_graceful_close(self):
        guard = Mock()
        guard.acquire.side_effect = [False, False, True]
        with tempfile.TemporaryDirectory() as directory, \
             patch('app.native.instance.read_owner', return_value={'pid': 3, 'build': 'old'}), \
             patch('app.native.instance.build_identity', return_value='new'), \
             patch('app.native.instance.legacy_reader', return_value=None), \
             patch('app.capture_bridge.signal_event', return_value=True) as signal:
            acquired, _ = acquire_reader(self.app, guard, Path(directory), 'test')
        self.assertTrue(acquired)
        signal.assert_called_once_with('test.Switch')
        self.assertEqual(guard.release.call_count, 2)

    def test_unknown_legacy_instance_is_reported_without_activating_it(self):
        guard = Mock()
        guard.acquire.return_value = False
        with tempfile.TemporaryDirectory() as directory, \
             patch('app.native.instance.read_owner', return_value={}), \
             patch('app.native.instance.legacy_reader', return_value=None), \
             patch('app.capture_bridge.signal_event') as signal, \
             patch('app.native.surfaces.QMessageBox.information') as prompt:
            acquired, _ = acquire_reader(self.app, guard, Path(directory), 'test')
        self.assertFalse(acquired)
        signal.assert_not_called()
        prompt.assert_called_once()


if __name__ == '__main__':
    unittest.main()
