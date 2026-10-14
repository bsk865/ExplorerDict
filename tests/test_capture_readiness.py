"""Initialization races and retired-helper isolation, without desktop input."""
from __future__ import annotations

import queue
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from app.uia_bridge import UiaBridge, UiaUnavailable
from tests.test_runtime_recovery import FakeProc
from tests.support import headless_app, run_gesture_worker

ROOT = Path(__file__).resolve().parents[1]
FG = {'hwnd': 55501, 'pid': 9999}


class TestHelperReadiness(unittest.TestCase):
    def test_live_process_is_not_ready_until_handshake_and_start_is_shared(self):
        bridge = UiaBridge(ROOT / 'scripts/uia_helper.ps1')
        proc = FakeProc()
        entered, release = threading.Event(), threading.Event()
        sent = []

        def request(payload, timeout):
            sent.append(payload['cmd'])
            if payload['cmd'] == 'ping':
                entered.set()
                release.wait(3)
                return {'ok': True}
            return {'ok': True, 'text': '原文依据'}

        with mock.patch.object(bridge, '_powershell', return_value='powershell'), \
                mock.patch('app.uia_bridge.subprocess.Popen', return_value=proc) as spawn, \
                mock.patch.object(bridge, '_read_loop'), mock.patch.object(bridge, '_drain_stderr'), \
                mock.patch.object(bridge, '_request', side_effect=request), \
                mock.patch('app.uia_bridge.w32.foreground_info', return_value=FG):
            starters = [threading.Thread(target=bridge.start) for _ in range(2)]
            for worker in starters:
                worker.start()
            self.assertTrue(entered.wait(1))
            began = time.monotonic()
            result = bridge.get_selection(expected_hwnd=55501, expected_pid=9999, timeout=.02)
            self.assertEqual(result['reason'], 'helper_initializing')
            self.assertLess(time.monotonic() - began, .2)
            self.assertEqual(sent, ['ping'], 'selection must not queue behind initialization')
            release.set()
            for worker in starters:
                worker.join(2)
                self.assertFalse(worker.is_alive())
            self.assertEqual(spawn.call_count, 1)
            self.assertTrue(bridge.get_selection(expected_hwnd=55501, expected_pid=9999)['ok'])
            bridge.stop()

    def test_retiring_old_process_does_not_clear_new_pending_request(self):
        bridge = UiaBridge()
        old, new = FakeProc(), FakeProc()
        bridge._proc = new
        bridge._ready = True
        response = queue.Queue()
        bridge._pending[42] = response
        bridge._terminate(old)
        bridge._read_loop(old)
        self.assertIn(42, bridge._pending)
        bridge._dispatch('{"id":42,"ok":true}')
        self.assertTrue(response.get_nowait()['ok'])
        bridge.stop()

    def test_foreground_change_during_initialization_prevents_selection(self):
        bridge = UiaBridge()
        with mock.patch('app.uia_bridge.w32.foreground_info', side_effect=[FG, {'hwnd': 888, 'pid': 9999}]), \
                mock.patch.object(bridge, 'ensure_started', return_value=True), \
                mock.patch.object(bridge, '_request') as request:
            result = bridge.get_selection(expected_hwnd=55501, expected_pid=9999)
        self.assertEqual(result['reason'], 'foreground_changed_before_call')
        request.assert_not_called()

    def test_a_failed_selection_does_not_run_a_second_blocking_start(self):
        bridge = UiaBridge()
        with mock.patch('app.uia_bridge.w32.foreground_info', return_value=FG), \
                mock.patch.object(bridge, 'ensure_started', return_value=True), \
                mock.patch.object(bridge, '_request', side_effect=UiaUnavailable('exited')), \
                mock.patch.object(bridge, '_restart') as restart:
            result = bridge.get_selection(expected_hwnd=55501, expected_pid=9999)
        self.assertFalse(result['ok'])
        restart.assert_not_called()


class TestGestureQueue(unittest.TestCase):
    def test_only_latest_pending_selection_is_read(self):
        with headless_app() as app:
            app._gesture_q.put((1, 2, 'drag', None, None, time.monotonic()))
            app._gesture_q.put((3, 4, 'drag', None, None, time.monotonic()))
            with mock.patch.object(app.capture_service, 'handle_gesture') as read:
                run_gesture_worker(app)
            self.assertEqual(read.call_count, 1)
            self.assertEqual(read.call_args.args[:2], (3, 4))
