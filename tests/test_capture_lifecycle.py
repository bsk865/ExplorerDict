"""Real Windows IPC with isolated helpers, without capture hooks or desktop UI."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from app import capture_bridge, storage_location


CHILD = r'''
import sys, time
from pathlib import Path
from app.single_instance import SingleInstance, mutex_name_for
from app.activation import ActivationReceiver
from app.capture_bridge import event_name
root = Path(sys.argv[1])
guard = SingleInstance(name=mutex_name_for(root))
assert guard.acquire()
(root / 'locked').touch()
time.sleep(float(sys.argv[2]))
receiver = ActivationReceiver(name=event_name() + '.Quit')
assert receiver.create()
(root / 'listening').touch()
try:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if sys.argv[3] == 'respond' and receiver.poll():
            (root / 'quit-received').touch()
            break
        time.sleep(.02)
finally:
    receiver.close()
    guard.release()
'''


@unittest.skipUnless(os.name == 'nt', 'Windows named events required')
class CaptureLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.children = []
        self.env = patch.dict(os.environ, {
            'EXPLORER_DICT_DATA_DIR': str(self.source),
            'EXPLORER_DICT_STORAGE_CONFIG': str(self.root / 'location.json')})
        self.env.start()
        self.launch = patch.object(capture_bridge, '_launch', None)
        self.launch.start()

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
        self.launch.stop()
        self.env.stop()
        self.temp.cleanup()

    def child(self, directory, delay=0, respond=True):
        directory.mkdir(exist_ok=True)
        child = subprocess.Popen([sys.executable, '-c', CHILD, str(directory), str(delay),
                                  'respond' if respond else 'ignore'],
            cwd=Path(__file__).resolve().parents[1],
            env=dict(os.environ, EXPLORER_DICT_DATA_DIR=str(directory)),
            creationflags=subprocess.CREATE_NO_WINDOW)
        self.children.append(child)
        end = time.monotonic() + 5
        while time.monotonic() < end and not (directory / 'locked').exists():
            self.assertIsNone(child.poll(), 'IPC helper exited during initialization')
            time.sleep(.02)
        self.assertTrue((directory / 'locked').exists())
        return child

    def test_delayed_receiver_is_stopped_before_migration(self):
        child = self.child(self.source, delay=.3)
        capture_bridge._launch = child
        (self.source / 'content.txt').write_text('retained content')
        target = self.root / 'destination'
        storage_location.schedule(self.source, target)
        self.assertTrue(capture_bridge.running())
        stopped = capture_bridge.stop_and_wait(timeout=3)
        self.assertTrue(stopped)
        self.assertEqual(0, child.wait(timeout=3))
        self.assertTrue((self.source / 'quit-received').exists())
        self.assertTrue(storage_location.apply_pending(self.source, components_stopped=stopped))
        self.assertEqual('retained content', (target / 'content.txt').read_text())

    def test_unresponsive_process_blocks_copy_and_retains_source(self):
        child = self.child(self.source, respond=False)
        capture_bridge._launch = child
        target = self.root / 'destination'
        storage_location.schedule(self.source, target)
        stopped = capture_bridge.stop_and_wait(timeout=.2)
        self.assertFalse(stopped)
        self.assertIsNone(child.poll())
        self.assertFalse(storage_location.apply_pending(self.source, components_stopped=stopped))
        self.assertEqual(str(self.source), storage_location.read()['active'])
        self.assertEqual([], list(target.iterdir()))

    def test_shutdown_does_not_signal_another_data_directory(self):
        other = self.root / 'other'
        child = self.child(other)
        self.assertFalse(capture_bridge.running())
        self.assertTrue(capture_bridge.stop_and_wait(timeout=.2))
        self.assertIsNone(child.poll())
        self.assertFalse((other / 'quit-received').exists())


if __name__ == '__main__':
    unittest.main()
