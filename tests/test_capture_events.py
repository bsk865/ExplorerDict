"""捕获事件契约 + 单按钮浮条的集成测试（Tk 只创建一个实例）。

> 本轮**不执行**这些用例（会创建真实 Tk 窗口）。它们已经按最终交互更新到
> 当前接口：浮条只有一个按钮「解释并记录」，划选只弹条、不落库、不联网。

覆盖：
* ``handle_gesture`` → ``selection_ready`` → 浮条按契约显示（**零写库、零联网**）；
* 点唯一按钮 → 幂等落库 → 再按 ``entry_id`` 解释（结果写回同一条词条）；
* 切到游戏/全屏/暂停后**迟到**的选区事件不得上屏；
* 旧世代 / 换窗口的结果同样拒绝；
* 鼠标按下（含点在浮条上）的元信息判定。

注意：这些是**模拟门控**测试（伪造前台窗口信息 / 门控判定），
**不安装真实全局鼠标钩子**，也不触碰真实 data\\explorer_dict.sqlite3。
真实跨应用鼠标划词属于人工验收项（见 验收报告.md）。
"""
from __future__ import annotations

import unittest

from tests.support import (
    BAR_HWND, MOUSE_HOOK_INSTALLS, FakeBridge, foreground, ok_response,
    requires_real_foreground, temp_data_dir,
)

try:
    import tkinter as tk
except ImportError:  # pragma: no cover
    tk = None


class HookSpy:
    """代替真实 WH_MOUSE_LL：记录安装/卸载次数，绝不安装全局钩子。"""

    def __init__(self):
        self.running = False
        self.starts = 0
        self.stops = 0

    def start(self, timeout: float = 5.0) -> bool:
        self.starts += 1
        self.running = True
        return True

    def stop(self, timeout: float = 3.0) -> None:
        self.stops += 1
        self.running = False

    def suppress(self, seconds: float = 0.4) -> None:
        pass

    def last_error(self) -> int:
        return 0


class UiaSpy:
    """代替 UiaBridge：记录是否被启用/停止，绝不启动 PowerShell。"""

    def __init__(self):
        self.enabled = True
        self.stop_calls = 0
        self.start_calls = 0

    def set_enabled(self, value: bool) -> None:
        self.enabled = bool(value)

    def stop(self) -> None:
        self.stop_calls += 1

    def start(self) -> bool:
        self.start_calls += 1
        return True

    def pid(self):
        return 4242

    def ping(self, timeout: float = 1.0) -> bool:
        return True

    def last_error(self) -> str:
        return ""


def _make_sel(term="alpha", context="…alpha beta…", app="msedge.exe", pid=9999, hwnd=55501):
    from app.models import CapturedSelection, SourceInfo, now_iso

    src = SourceInfo(hwnd=hwnd, pid=pid, exe=f"C:\\Program Files\\{app}", app=app,
                     title="Some Document", url="https://a.example.com/p",
                     confidence="url_document", doc_key="url:https://a.example.com/p")
    return CapturedSelection(term=term, context=context, method="ui_textpattern",
                             source=src, captured_at=now_iso())


@unittest.skipIf(tk is None, "没有 tkinter")
class _AppCase(unittest.TestCase):
    """公共装置：临时库 + 一个 App（不调用 start()，不装真实钩子/热键）。"""

    @requires_real_foreground("捕获事件契约用例（会创建主窗口与浮窗）")
    def setUp(self):
        from app.config import Config
        from app.db import Database
        from app.main import App
        from app.ui import theme
        import app.paths as paths
        from app import win32util as w32

        self._data = temp_data_dir()
        self.tmp = self._data.__enter__()
        self.root = tk.Tk()
        w32.enable_dpi_awareness()
        theme.init(self.root, 96)
        self.db = Database(paths.db_path())
        self.config = Config(self.db)
        self.app = App(self.root, self.db, self.config)
        self.hook = HookSpy()
        self.uia = UiaSpy()
        self.bridge = FakeBridge(ok_response(text="alpha"))
        self.app.mouse_hook = self.hook
        self.app.uia = self.uia
        self.app.capture_service.bridge = self.bridge
        self.root.update()

    def tearDown(self):
        try:
            self.app._closing = True
            for aid in self.root.tk.call("after", "info"):
                try:
                    self.root.after_cancel(aid)
                except tk.TclError:  # pragma: no cover
                    pass
        except tk.TclError:  # pragma: no cover
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        try:
            self.db.close()
        except Exception:
            pass
        self._data.__exit__(None, None, None)

    # ------------------------------------------------------------- 辅助
    def pump_events(self, times: int = 3) -> None:
        for _ in range(times):
            self.app._pump()
            try:
                self.root.update()
            except tk.TclError:  # pragma: no cover
                return

    def press(self, x: int, y: int, *, overlay: bool) -> None:
        """把一次「全局左键按下」的元信息塞进 UI 队列（不装真实钩子）。"""
        self.app._ui_q.put(("overlay_press", {
            "x": x, "y": y, "when": __import__("time").monotonic(),
            "hwnd": BAR_HWND if overlay else 777001,
            "is_overlay": overlay,
            "generation": self.app.capture_service.current_generation(),
        }))


class TestSelectionOnlyClassContract(_AppCase):
    """划选只弹条：零写库、零批次、零网络。"""

    def test_gesture_shows_single_button_bar_without_writing(self):
        with foreground(app="msedge.exe"):
            self.app._apply_gate(force=True)
            self.app.capture_service.handle_gesture(10, 10, "drag", delay=0)
            self.pump_events()

            self.assertEqual(len(self.bridge.calls), 1, "应当只发起一次 UIA 读取")
            self.assertEqual(self.db.count_entries(), 0, "划选绝不落库")
            self.assertEqual(self.db.list_batches(), [], "划选绝不建批次")
            self.assertTrue(self.app.selection_bar.visible, "划选后必须弹出浮条")
            self.assertEqual(self.app.selection_bar.term_label.cget("text"), "alpha")
            self.assertEqual(self.app.selection_bar.btn_action.cget("text"), "解释并记录")
            self.assertFalse(hasattr(self.app.selection_bar, "btn_record"),
                             "浮条不得再有独立的「记录」按钮")
            self.assertIn("解释并记录", self.app.main.status_label.cget("text"))

    def test_outside_press_dismisses_bar_without_saving(self):
        with foreground(app="msedge.exe"):
            self.app._apply_gate(force=True)
            self.app.capture_service.handle_gesture(10, 10, "drag", delay=0)
            self.pump_events()
            self.assertTrue(self.app.selection_bar.visible)

            self.press(900, 900, overlay=False)
            self.pump_events()
            self.assertFalse(self.app.selection_bar.visible, "点别处必须收起浮条")
            self.assertEqual(self.db.count_entries(), 0, "收起浮条不得保存")
            self.assertEqual(MOUSE_HOOK_INSTALLS, [], "不得安装真实全局鼠标钩子")


class TestUnifiedButtonIntegration(_AppCase):
    """点唯一按钮：先落库、再解释（解释调用由假服务记录）。"""

    def test_button_records_then_calls_entry_explain(self):
        calls: list[tuple[int, bool]] = []
        self.app.explain_service.explain_entry_async = (
            lambda eid, force=False: (calls.append((int(eid), bool(force))) or 1))
        self.config.set_api_key("sk-test-key-not-real")

        with foreground(app="msedge.exe"):
            self.app._apply_gate(force=True)
            self.app.capture_service.handle_gesture(10, 10, "drag", delay=0)
            self.pump_events()
            sel = self.app.current_selection()
            self.assertIsNotNone(sel)

            entry_id = self.app.explain_and_record_selection(sel)
            self.pump_events()

        self.assertIsNotNone(entry_id)
        self.assertEqual(self.db.count_entries(), 1, "点按钮必须落库")
        self.assertEqual(self.db.list_entries()[0]["term"], "alpha")
        self.assertEqual(calls, [(int(entry_id), False)], "随后必须解释同一个 entry_id")
        self.assertEqual(self.app.explain_window.entry_id, int(entry_id))
        self.assertFalse(self.app.selection_bar.visible, "点按钮后浮条收起")

    def test_no_key_records_and_shows_pending_entry(self):
        with foreground(app="msedge.exe"):
            self.app._apply_gate(force=True)
            self.app.capture_service.handle_gesture(10, 10, "drag", delay=0)
            self.pump_events()
            entry_id = self.app.explain_and_record_selection(self.app.current_selection())
            self.pump_events()

        self.assertIsNotNone(entry_id, "没有 API Key 也必须先保存")
        self.assertEqual(self.db.count_entries(), 1)
        self.assertTrue(self.app.explain_window.visible, "必须显示待解释窗口")
        self.assertEqual(self.app.explain_window.entry_id, int(entry_id))
        self.assertFalse(hasattr(self.app.explain_window, "btn_record"),
                         "解释窗不得有记录动作")


class TestLateEventsAfterGateCloses(_AppCase):
    """(B) 切到不允许的前台后，**迟到**的选区事件都不得上屏。"""

    def _queue_selection(self, hwnd=55501, pid=9999, gen=None) -> None:
        sel = _make_sel(hwnd=hwnd, pid=pid)
        self.app._ui_q.put(("selection_ready", {
            "selection": sel, "point": (10, 10), "kind": "drag", "reason": "",
            "generation": self.app.capture_service.current_generation() if gen is None else gen,
            "hwnd": hwnd, "pid": pid,
        }))

    def _assert_hidden_and_dropped(self, label: str) -> None:
        self.pump_events()
        self.assertFalse(self.app.selection_bar.visible, f"{label}：迟到选区不得显示")
        self.assertFalse(self.app.selection_bar.win.winfo_ismapped(),
                         f"{label}：浮条窗口本身不得被映射")
        self.assertEqual(self.bridge.calls, [], f"{label}：不得发起 UIA 读取")
        self.assertEqual(MOUSE_HOOK_INSTALLS, [], f"{label}：不得安装真实全局鼠标钩子")

    def test_late_selection_after_game_process(self):
        with foreground(app="chrome.exe"):
            self.app._apply_gate(force=True)
            self._queue_selection()
        with foreground(title="某游戏", app="steam.exe", hwnd=70001, pid=7001):
            self.app._apply_gate()
            self._assert_hidden_and_dropped("游戏进程")

    def test_late_selection_after_fullscreen(self):
        with foreground(app="chrome.exe"):
            self.app._apply_gate(force=True)
            self._queue_selection()
        with foreground(app="chrome.exe", fullscreen=True):
            self.app._apply_gate()
            self._assert_hidden_and_dropped("全屏")

    def test_late_selection_after_manual_game_mode(self):
        with foreground(app="chrome.exe"):
            self.app._apply_gate(force=True)
            self._queue_selection()
            self.app.toggle_game_mode()
            self.assertTrue(self.app.game_mode())
        with foreground(app="chrome.exe"):
            self._assert_hidden_and_dropped("手动游戏模式")


class TestStaleGenerationRejected(_AppCase):
    """同一阅读窗口的**旧世代**结果同样拒绝。"""

    def test_old_generation_rejected_after_returning(self):
        with foreground(app="chrome.exe", hwnd=55501, pid=9999):
            self.app._apply_gate(force=True)
            stale_gen = self.app.capture_service.current_generation()
            self.app.capture_service.note_foreground(
                {"hwnd": 55502, "pid": 4242, "title": "别的文档", "app": "firefox.exe",
                 "exe": "C:\\firefox.exe", "is_self": False})
            self.app.capture_service.note_foreground(
                {"hwnd": 55501, "pid": 9999, "title": "Some Document", "app": "msedge.exe",
                 "exe": "C:\\msedge.exe", "is_self": False})
            self.assertGreater(self.app.capture_service.current_generation(), stale_gen)

            sel = _make_sel()
            self.app._ui_q.put(("selection_ready", {
                "selection": sel, "point": (10, 10), "kind": "drag", "reason": "",
                "generation": stale_gen, "hwnd": 55501, "pid": 9999,
            }))
            self.pump_events()
            self.assertFalse(self.app.selection_bar.visible,
                             "回到同一阅读窗口后，旧世代的选区必须被拒绝")

    def test_window_identity_change_rejects_late_result(self):
        with foreground(app="chrome.exe", hwnd=55501, pid=9999):
            self.app._apply_gate(force=True)
            gen = self.app.capture_service.current_generation()
            self.app._ui_q.put(("selection_ready", {
                "selection": _make_sel(), "point": (10, 10), "kind": "drag", "reason": "",
                "generation": gen, "hwnd": 55501, "pid": 9999,
            }))
            with foreground(app="firefox.exe", hwnd=55503, pid=4242):
                self.pump_events()
            self.assertFalse(self.app.selection_bar.visible,
                             "窗口身份不符的迟到结果不得显示浮条")


if __name__ == "__main__":
    unittest.main()
