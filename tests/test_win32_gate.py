"""Win32 判定与钩子生命周期的直接验证（标准 API，无注入/无内存读取）。"""
from __future__ import annotations

import unittest
from unittest import mock

from app import win32util as w32
from tests.support import requires_real_foreground


class TestFullscreenLogic(unittest.TestCase):
    """is_fullscreen：只用窗口矩形 + 显示器矩形（+ IsZoomed）。"""

    def _call(self, rect, mon, zoomed=False, visible=True, iconic=False):
        with mock.patch("app.win32util.is_window_visible", return_value=visible), \
                mock.patch("app.win32util.is_iconic", return_value=iconic), \
                mock.patch("app.win32util.get_window_rect", return_value=rect), \
                mock.patch("app.win32util.monitor_rect_for_window", return_value=mon), \
                mock.patch("app.win32util.is_zoomed", return_value=zoomed):
            return w32.is_fullscreen(12345)

    def test_covers_monitor_is_fullscreen(self):
        self.assertTrue(self._call((0, 0, 1920, 1080), (0, 0, 1920, 1080)))

    def test_borderless_second_monitor_is_fullscreen(self):
        self.assertTrue(self._call((-1920, 0, 0, 1080), (-1920, 0, 0, 1080)))

    def test_maximized_is_not_fullscreen(self):
        """最大化窗口（含任务栏的工作区）不算全屏 —— 阅读时最大化很常见。"""
        self.assertFalse(self._call((0, 0, 1920, 1040), (0, 0, 1920, 1080), zoomed=True))

    def test_small_window_is_not_fullscreen(self):
        self.assertFalse(self._call((100, 100, 900, 700), (0, 0, 1920, 1080)))

    def test_hidden_or_minimized_is_not_fullscreen(self):
        self.assertFalse(self._call((0, 0, 1920, 1080), (0, 0, 1920, 1080), visible=False))
        self.assertFalse(self._call((0, 0, 1920, 1080), (0, 0, 1920, 1080), iconic=True))

    def test_missing_rect_is_not_fullscreen(self):
        self.assertFalse(self._call(None, (0, 0, 1920, 1080)))
        self.assertFalse(self._call((0, 0, 1920, 1080), None))


class TestMouseTargetHelpers(unittest.TestCase):
    def test_window_root_falls_back_to_self(self):
        with mock.patch("app.win32util.user32.GetAncestor", return_value=0):
            self.assertEqual(w32.window_root(4242), 4242)

    def test_window_root_from_point_returns_zero_for_nothing(self):
        with mock.patch("app.win32util.user32.WindowFromPoint", return_value=0):
            self.assertEqual(w32.window_root_from_point(10, 10), 0)


class TestRealMouseHook(unittest.TestCase):
    """真实安装/卸载 WH_MOUSE_LL（**默认跳过**）。

    **安全前提**：只有在 ``app/gui_preflight.py`` 的**严格**预检放行时
    （前台是已知阅读应用 + 非全屏 + 未开游戏模式）才允许安装一次真实全局钩子。
    这里**故意不用**产品运行时的门控（它默认放开普通窗口），也不看任何 mock ——
    用户可能在微信 / 游戏 / 全屏前台，那些情况下必须 skip，连一次
    ``SetWindowsHookExW`` 都不发生。
    """

    def setUp(self):
        from app.config import Config
        from app.db import Database
        from app.gate import AccessGate
        from tests.support import tmp_dir

        self._dir = tmp_dir("hook_")
        self.tmp = self._dir.__enter__()
        self.db = Database(self.tmp / "hook.sqlite3")
        self.gate = AccessGate(Config(self.db))
        self.decision = self.gate.evaluate()
        self.hook = None

    def tearDown(self):
        if self.hook is not None:
            try:
                self.hook.stop()
            except Exception:  # pragma: no cover
                pass
        self.db.close()
        self._dir.__exit__(None, None, None)

    def _require_allowed_foreground(self):
        """用**严格** GUI 预检（实时真实前台）决定是否允许真实钩子/窗口。"""
        from tests.support import skip_unless_foreground_safe

        skip_unless_foreground_safe("真实鼠标钩子用例（会安装 WH_MOUSE_LL）")

    def test_install_then_remove(self):
        from app.mouse_hook import MouseHook

        self._require_allowed_foreground()
        hook = MouseHook(lambda *a: None)
        self.hook = hook
        if not hook.start():
            self.skipTest(f"当前环境无法安装鼠标钩子（GetLastError={hook.last_error()}）")
        try:
            self.assertTrue(hook.running, "钩子应处于运行状态")
            self.assertEqual(hook.last_error(), 0)
        finally:
            hook.stop()
        self.assertFalse(hook.running, "stop() 之后钩子必须已卸载")

    def test_no_install_when_gate_denies(self):
        """门控不允许时：App 连一次 SetWindowsHookExW 都不应发生。

        这正是「受限环境下确实没有鼠标监听」的可证形式：安装入口完全由门控把守。
        """
        from app.config import Config
        from app.db import Database
        from app.main import App
        import app.paths as paths
        from tests.support import MOUSE_HOOK_INSTALLS, foreground

        try:
            import tkinter as tk
        except ImportError:  # pragma: no cover
            self.skipTest("没有 tkinter")

        from app.ui import theme

        self._require_allowed_foreground()   # 会建 Tk 窗口 → 先做实时前台预检

        w32.enable_dpi_awareness()
        root = tk.Tk()
        calls: list[int] = []
        try:
            with foreground(app="steam.exe", title="某游戏", hwnd=91001, pid=9101):
                theme.init(root, 96)
                # 用独立的库，绝不触碰真实 data\explorer_dict.sqlite3
                db2 = Database(self.tmp / "hook_app.sqlite3")
                try:
                    app = App(root, db2, Config(db2))
                    with mock.patch("app.mouse_hook.w32.install_mouse_hook",
                                    side_effect=lambda cb: (calls.append(1), (0, None, 5))[1]), \
                            mock.patch.object(App, "_start_uia",
                                              lambda self: self._ui_q.put(("uia_ready", True))):
                        app._start_services()
                        root.update()
                    self.assertFalse(app.gate_decision().allowed)
                    self.assertFalse(app.mouse_hook.running,
                                     "受限前台下不应有正在运行的鼠标钩子")
                    app.mouse_hook.stop()
                    app.watcher.stop()
                    app.hotkeys.stop()
                    app.uia.stop()
                finally:
                    db2.close()
        finally:
            try:
                root.destroy()
            except tk.TclError:  # pragma: no cover
                pass
        self.assertEqual(calls, [], "门控不允许时不得尝试安装全局鼠标钩子")
        self.assertEqual(MOUSE_HOOK_INSTALLS, [])

    def test_stop_is_idempotent(self):
        from app.mouse_hook import MouseHook

        hook = MouseHook(lambda *a: None)
        hook.stop()
        hook.stop()
        self.assertFalse(hook.running)


class TestRealFullscreenWindow(unittest.TestCase):
    """用一个真实窗口验证全屏判定链路（矩形、显示器、IsZoomed 都走真实 Win32）。"""

    @requires_real_foreground("真实全屏窗口判定用例（会创建 Tk 窗口）")
    def test_real_window_fullscreen_transitions(self):
        try:
            import tkinter as tk
        except ImportError:  # pragma: no cover
            self.skipTest("没有 tkinter")

        w32.enable_dpi_awareness()
        root = tk.Tk()
        try:
            root.geometry("320x200+40+40")
            root.update()
            hwnd = w32.toplevel_hwnd(root.winfo_id())
            self.assertFalse(w32.is_fullscreen(hwnd), "普通小窗口不是全屏")

            root.state("zoomed")
            root.update()
            self.assertFalse(w32.is_fullscreen(hwnd),
                             "最大化窗口（有标题栏/工作区）不算全屏")

            root.state("normal")
            root.update()
            mon = w32.monitor_rect_for_window(hwnd)
            self.assertIsNotNone(mon)
            top = tk.Toplevel(root)
            top.overrideredirect(True)
            top.geometry(f"{mon[2] - mon[0]}x{mon[3] - mon[1]}+{mon[0]}+{mon[1]}")
            top.update()
            top.update_idletasks()
            hwnd2 = w32.toplevel_hwnd(top.winfo_id())
            self.assertTrue(w32.is_fullscreen(hwnd2),
                            "铺满整块显示器的无边框窗口必须判定为全屏 → 暂停取词")
        finally:
            try:
                root.destroy()
            except tk.TclError:  # pragma: no cover
                pass


if __name__ == "__main__":
    unittest.main()
