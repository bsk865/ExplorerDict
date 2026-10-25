"""Hook callbacks must keep returning even while capture work is blocked."""
import ctypes
import threading
import unittest
from unittest.mock import patch

from app.mouse_hook import MouseHook
from app import win32util as w32


class MouseDispatchTests(unittest.TestCase):
    def test_blocked_capture_does_not_block_hook_and_preserves_event_order(self):
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        calls = []

        def select(*args):
            entered.set()
            release.wait(2)
            calls.append(('select', args))
            done.set()

        hook = MouseHook(select, lambda *args: calls.append(('press', args)))
        worker = threading.Thread(target=hook._dispatch)
        worker.start()
        down = w32.MSLLHOOKSTRUCT()
        down.pt.x, down.pt.y = 10, 20
        up = w32.MSLLHOOKSTRUCT()
        up.pt.x, up.pt.y = 80, 20
        try:
            with patch.object(w32, 'window_root_from_point', return_value=123):
                hook._handle(w32.WM_LBUTTONDOWN, ctypes.addressof(down))
            hook._handle(w32.WM_LBUTTONUP, ctypes.addressof(up))
            self.assertTrue(entered.wait(1))
            hook._handle(w32.WM_LBUTTONDOWN, ctypes.addressof(down))
            hook._handle(w32.WM_LBUTTONUP, ctypes.addressof(up))
            self.assertEqual(hook._events.qsize(), 1)
            self.assertEqual([c[0] for c in calls], ['press', 'press'])
        finally:
            release.set()
            # Drain both ordered events before asking the worker to stop.
            done.wait(1)
            hook._stop_flag.set()
            worker.join(1)
        self.assertEqual([c[0] for c in calls], ['press', 'press', 'select', 'select'])
        self.assertEqual(calls[-1][1], (80, 20, 'drag'))

    def test_idle_does_not_reinstall_but_lost_events_during_movement_do(self):
        hook = MouseHook(lambda *args: None)
        hook._probe_point = (10, 20)
        hook._last_event = 1
        hook._hook = 7
        with patch.object(w32, 'get_cursor_pos', side_effect=[(10, 20), (30, 20)]), \
             patch('app.mouse_hook.time.monotonic', return_value=10), \
             patch.object(w32, 'install_mouse_hook', return_value=(8, object(), 0)) as install, \
             patch.object(w32, 'unhook') as unhook:
            hook._check_health()
            install.assert_not_called()
            hook._check_health()
            install.assert_called_once()
            unhook.assert_called_once_with(7)
            self.assertEqual(hook._hook, 8)

    def test_recent_events_do_not_reinstall(self):
        hook = MouseHook(lambda *args: None)
        hook._probe_point, hook._last_event = (10, 20), 9.5
        with patch.object(w32, 'get_cursor_pos', return_value=(30, 20)), \
             patch('app.mouse_hook.time.monotonic', return_value=10), \
             patch.object(w32, 'install_mouse_hook') as install:
            hook._check_health()
            install.assert_not_called()

    def test_polling_detects_drag_when_native_hook_never_calls_back(self):
        presses = []
        hook = MouseHook(lambda *args: None, lambda *args: presses.append(args))
        with patch.object(w32, 'left_button_down', side_effect=[True, True, False, False]), \
             patch.object(w32, 'get_cursor_pos', side_effect=[(10, 20), (80, 20)]), \
             patch('app.mouse_hook.time.monotonic', side_effect=[1, 1.04, 1.04, 1.2, 1.24, 1.24]):
            for _ in range(4):
                hook._poll_buttons()
        self.assertEqual(len(presses), 1)
        self.assertEqual(hook._events.get_nowait(), (80, 20, 'drag'))
        self.assertTrue(hook._events.empty())

    def test_native_events_and_polling_do_not_duplicate_edges(self):
        presses=[]
        hook=MouseHook(lambda *args: None, lambda *args: presses.append(args))
        down=w32.MSLLHOOKSTRUCT(); down.pt.x, down.pt.y=10, 20
        up=w32.MSLLHOOKSTRUCT(); up.pt.x, up.pt.y=80, 20
        hook._handle(w32.WM_LBUTTONDOWN, ctypes.addressof(down))
        with patch.object(w32, 'left_button_down', return_value=True):
            hook._poll_buttons()
        hook._handle(w32.WM_LBUTTONDOWN, ctypes.addressof(down))
        hook._handle(w32.WM_LBUTTONUP, ctypes.addressof(up))
        with patch.object(w32, 'left_button_down', return_value=False):
            hook._poll_buttons()
        hook._handle(w32.WM_LBUTTONUP, ctypes.addressof(up))
        self.assertEqual(len(presses), 1)
        self.assertEqual(hook._events.get_nowait(), (80,20,'drag'))
        self.assertTrue(hook._events.empty())
