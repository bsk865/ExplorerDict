"""门控在真实 App 上的运行时行为（Tk 只创建一个实例）。

> 本轮**不执行**这些用例（会创建真实 Tk 窗口）；接口已按最终交互更新。

验证用户新增的硬要求：
* **硬阻断**（游戏进程 / 全屏 / 手动游戏模式 / 用户暂停）：收起浮层、
  卸载全局鼠标钩子、停掉 UIA helper、取消置顶、丢弃在途手势，且零 UIA 调用；
* **软受限**（本程序自身窗口 / 桌面 / 无前台）：只收浮层，
  **不**卸载钩子、**不**停 UIA、**不**取消置顶；
* 未知普通程序默认放行（产品运行时），但划选仍然只弹条、不落库。
"""
from __future__ import annotations

import unittest

from tests.support import (
    FakeBridge, foreground, ok_response, requires_real_foreground, temp_data_dir,
)

try:
    import tkinter as tk
except ImportError:  # pragma: no cover
    tk = None


class HookSpy:
    """代替真实 WH_MOUSE_LL：记录安装/卸载次数。"""

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
    """代替 UiaBridge：记录禁用/停止次数，绝不启动 PowerShell。"""

    def __init__(self):
        self.enabled = True
        self.stop_calls = 0
        self.start_calls = 0
        self.requests = 0

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


@unittest.skipIf(tk is None, "没有 tkinter")
class TestGateRuntime(unittest.TestCase):
    @requires_real_foreground("门控运行时用例（会创建主窗口与浮窗）")
    def test_hard_and_soft_block_lifecycle(self):
        from app import win32util as w32
        from app.config import Config
        from app.db import Database
        from app.main import App
        from app.ui import theme
        import app.paths as paths

        with temp_data_dir() as tmp:
            root = tk.Tk()
            try:
                w32.enable_dpi_awareness()
                theme.init(root, 96)
                db = Database(paths.db_path())
                config = Config(db)
                bid = db.create_batch("【测试数据】门控用例", "url:https://t.example.com/x",
                                      "url_document")
                db.add_entry(batch_id=bid, term="【测试数据】术语", context="测试上下文",
                             doc_key="k", source_title="【测试数据】来源")

                app = App(root, db, config)
                hook = HookSpy()
                uia = UiaSpy()
                bridge = FakeBridge(ok_response(text="alpha"))
                app.mouse_hook = hook
                app.uia = uia
                app.capture_service.bridge = bridge
                root.update()

                # ---------- 1. 普通窗口（含未知程序）= 允许，且保持置顶 ----------
                with foreground(app="chrome.exe"):
                    d = app._apply_gate(force=True)
                    self.assertTrue(d.allowed, d.detail_text())
                    root.update()
                self.assertTrue(app.effective_topmost(), "允许的前台下应保持置顶")
                self.assertTrue(uia.enabled)
                self.assertEqual(hook.stops, 0)

                with foreground(app="some_unknown_tool.exe", hwnd=70011, pid=7011):
                    d = app._apply_gate()
                self.assertTrue(d.allowed, "未知普通程序默认放行（产品运行时）")

                # ---------- 2. 切到游戏进程 = 硬阻断 ----------
                gen_before = app.capture_service.current_generation()
                with foreground(title="某游戏", app="steam.exe"):
                    d = app._apply_gate()
                    self.assertFalse(d.allowed)
                    self.assertEqual(d.reason, "game_process")
                    self.assertTrue(d.hard_blocked)
                    root.update()

                    self.assertFalse(app.selection_bar.visible)
                    self.assertFalse(app.explain_window.visible)
                    self.assertFalse(app.effective_topmost(), "硬阻断必须取消置顶")
                    self.assertEqual(hook.stops, 1, "硬阻断必须卸载全局鼠标钩子")
                    self.assertFalse(uia.enabled)
                    self.assertEqual(uia.stop_calls, 1)
                    self.assertGreater(app.capture_service.current_generation(), gen_before)

                    app.grab_now()
                    self.assertTrue(app._gesture_q.empty(), "受限前台下不得触发取词")
                    sel, reason = app.capture_service.attempt_capture(10, 10, "drag")
                    self.assertIsNone(sel)
                    self.assertEqual(reason, "game_process")
                    self.assertEqual(bridge.calls, [], "硬阻断必须零 UIA 调用")
                    self.assertEqual(db.count_entries(), 1, "硬阻断下不得新增词条")

                # ---------- 3. 全屏同样硬阻断 ----------
                with foreground(app="chrome.exe", fullscreen=True):
                    d = app._apply_gate()
                self.assertFalse(d.allowed)
                self.assertEqual(d.reason, "fullscreen")

                # ---------- 4. 回到普通窗口 = 恢复服务与置顶 ----------
                with foreground(app="firefox.exe"):
                    d = app._apply_gate()
                    self.assertTrue(d.allowed, d.detail_text())
                    root.update()
                self.assertTrue(uia.enabled)
                self.assertTrue(app.effective_topmost(), "离开硬阻断应恢复用户置顶偏好")

                # ---------- 5. 本程序自身窗口 = 软受限：只收浮层 ----------
                stops_before = hook.stops
                with foreground(title="探索词典", app="python.exe", pid=1234, hwnd=55599,
                                is_self=True):
                    d = app._apply_gate()
                    root.update()
                self.assertTrue(d.soft_blocked, d.reason)
                self.assertEqual(hook.stops, stops_before, "self 窗口不得卸载钩子")
                self.assertEqual(uia.stop_calls, 1, "self 窗口不得再停 UIA")
                self.assertTrue(uia.enabled, "self 窗口不得禁用 UIA")
                self.assertTrue(app.effective_topmost(), "self 窗口不得取消主界面置顶")
                self.assertFalse(app.selection_bar.visible, "self 窗口不弹浮条")

                # ---------- 6. 用户手动暂停优先级最高 ----------
                with foreground(app="chrome.exe"):
                    app.toggle_capture()          # → 暂停
                    self.assertFalse(config.capture_enabled)
                    root.update()
                self.assertFalse(app.effective_topmost())
                with foreground(app="firefox.exe"):   # 回到阅读窗口也不能自动恢复
                    d = app._apply_gate()
                    self.assertFalse(d.allowed)
                    self.assertEqual(d.reason, "paused")
                with foreground(app="chrome.exe"):
                    app.toggle_capture()          # → 恢复
                    root.update()
                self.assertTrue(app.effective_topmost())

                # ---------- 7. 手动游戏模式 ----------
                with foreground(app="chrome.exe"):
                    app.toggle_game_mode()
                    root.update()
                self.assertTrue(app.game_mode())
                self.assertFalse(app.effective_topmost())
                with foreground(app="firefox.exe"):
                    d = app._apply_gate()
                    self.assertEqual(d.reason, "game_mode")
                self.assertEqual(bridge.calls, [], "游戏模式下必须零 UIA 调用")

                with foreground(app="chrome.exe"):
                    app.toggle_game_mode()        # → 关闭
                    root.update()
                self.assertFalse(app.game_mode())
                self.assertTrue(app.effective_topmost())

                db.close()
            finally:
                try:
                    root.destroy()
                except tk.TclError:
                    pass


if __name__ == "__main__":
    unittest.main()
