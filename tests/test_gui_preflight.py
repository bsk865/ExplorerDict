"""执行前 GUI 预检的策略与不可绕过性（纯逻辑，不创建窗口、不装钩子、不读 UIA）。

覆盖：
* 判定顺序：游戏模式 / 游戏进程 / 无前台 / 自身 / 桌面 / 非白名单 / 全屏 → 拒绝；
  只有白名单阅读应用 + 非全屏 + 非游戏模式 → 允许；
* 真实 ``check()`` 拿的是**实时真实前台**（本用例只断言它是 Windows 上的真实结论，
  不断言具体取值，因此用户在前台做什么都不会让测试失败）；
* **不可绕过**：把 ``app.win32util.foreground_info`` mock 成「允许状态」也不能让
  ``check()`` 放行 —— 预检不信任任何被伪造的前台信息；
* ``user_consented()`` 只是「用户主动同意」的标记，**不参与**放行判定。
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

from app import gui_preflight as gp
from tests.support import temp_db

ALLOW_CASES = [
    ("msedge.exe", "Chrome_WidgetWin_1"),
    ("chrome.exe", "Chrome_WidgetWin_1"),
    ("WINWORD.EXE", "OpusApp"),
    ("wps.exe", "KingsoftWPS"),
    ("SumatraPDF.exe", "SUMATRA_PDF_FRAME"),
    ("notepad.exe", "Notepad"),
]


def snap(app="msedge.exe", cls="Chrome_WidgetWin_1", *, hwnd=55501, pid=9999,
         fullscreen=False, is_self=False, exe=None, supported=True, error=""):
    from app.gui_preflight import ForegroundSnapshot

    if not supported:
        return ForegroundSnapshot(supported=False, error=error)
    return ForegroundSnapshot(hwnd=hwnd, pid=pid, exe=exe if exe is not None else f"C:\\x\\{app}",
                              app=app, title="doc", cls=cls, is_self=is_self,
                              fullscreen=fullscreen)


class TestPreflightPolicy(unittest.TestCase):
    """纯函数 evaluate_preflight：不碰真实系统。"""

    def test_whitelisted_reader_not_fullscreen_allowed(self):
        for exe, cls in ALLOW_CASES:
            d = gp.evaluate_preflight(snap(exe, cls), game_mode=False)
            self.assertTrue(d.allowed, f"{exe} 应放行：{d.label}")
            self.assertEqual(d.reason, gp.P_OK)

    def test_game_mode_denies_even_whitelisted_reader(self):
        d = gp.evaluate_preflight(snap("chrome.exe"), game_mode=True)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, gp.P_GAME_MODE)

    def test_known_game_process_denied(self):
        """已知游戏/游戏平台进程：拒绝，并给出更清楚的 game_process 原因。"""
        for exe in ("steam.exe", "cs2.exe", "yuanshen.exe"):
            d = gp.evaluate_preflight(snap(exe, "Valve001"), game_mode=False)
            self.assertFalse(d.allowed, f"{exe} 不得放行")
            self.assertEqual(d.reason, gp.P_GAME_PROCESS, f"{exe} → {d.reason}")

    def test_war_thunder_is_denied_even_if_not_in_game_list(self):
        """本机实际在玩的 War Thunder（``aces.exe``）必须被拒绝。

        ``aces.exe`` 在 ``GAME_APPS`` 里 → 原因 ``game_process``；
        即便某个游戏进程还没被收进清单，它也**不在**阅读白名单，
        因此照样被拒（原因 ``not_whitelisted``）。两条路都必须拒绝：
        「产品默认放开普通窗口」绝不等于「自动化预检也放开」。
        """
        from app.permissions import GAME_APPS, READING_APPS

        self.assertNotIn("aces.exe", READING_APPS, "aces.exe 绝不能被当成阅读应用")
        d = gp.evaluate_preflight(snap("aces.exe", "DagorWClass", exe=r"D:\war thunder\WarThunder\win64\aces.exe"),
                                  game_mode=False)
        self.assertFalse(d.allowed)
        # 在清单里 → game_process；不在清单里 → not_whitelisted。二者都必须拒绝。
        expected = gp.P_GAME_PROCESS if "aces.exe" in GAME_APPS else gp.P_NOT_WHITELISTED
        self.assertEqual(d.reason, expected)

    def test_war_thunder_fullscreen_foreground_is_denied(self):
        """真实前台正是「全屏 War Thunder」时（本机用户当前状态）的判定。"""
        d = gp.evaluate_preflight(
            snap("aces.exe", "DagorWClass", fullscreen=True, pid=29424, hwnd=917956,
                 exe=r"D:\war thunder\WarThunder\win64\aces.exe"),
            game_mode=True)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, gp.P_GAME_MODE)
        self.assertIn("游戏模式", d.skip_message("UI 冒烟"))

    def test_unknown_process_denied(self):
        d = gp.evaluate_preflight(snap("some_tool.exe", "SomeClass"), game_mode=False)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, gp.P_NOT_WHITELISTED)

    def test_fullscreen_whitelisted_reader_denied(self):
        d = gp.evaluate_preflight(snap("firefox.exe", "MozillaWindowClass",
                                       fullscreen=True), game_mode=False)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, gp.P_FULLSCREEN)

    def test_no_foreground_denied(self):
        d = gp.evaluate_preflight(snap(hwnd=0, pid=0, app="", exe=""), game_mode=False)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, gp.P_NO_FOREGROUND)

    def test_self_window_denied(self):
        d = gp.evaluate_preflight(snap("python.exe", "TkTopLevel", pid=os.getpid(),
                                       is_self=True), game_mode=False)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, gp.P_SELF)

    def test_desktop_denied(self):
        d = gp.evaluate_preflight(snap("explorer.exe", "Progman"), game_mode=False)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, gp.P_DESKTOP)

    def test_unreadable_foreground_fails_closed(self):
        d = gp.evaluate_preflight(snap(supported=False, error="OSError: boom"),
                                  game_mode=False)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, gp.P_UNKNOWN_ERROR)

    def test_non_windows_fails_closed(self):
        from app.gui_preflight import ForegroundSnapshot

        d = gp.evaluate_preflight(ForegroundSnapshot(supported=False, error="非 Windows 平台"),
                                  game_mode=False)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, gp.P_NOT_WINDOWS)

    def test_skip_message_is_explicit_and_actionable(self):
        d = gp.evaluate_preflight(snap("aces.exe", "DagorWClass"), game_mode=False)
        msg = d.skip_message("UI 冒烟")
        for token in ("未放行", "已跳过UI 冒烟", "未创建任何窗口",
                      "Ctrl+Alt+Shift+G"):
            self.assertIn(token, msg, msg)


class TestGameModeReadOnly(unittest.TestCase):
    """read_game_mode 只读检查设置库（不读密钥、不写库）。"""

    def test_defaults_to_false_without_file(self):
        self.assertFalse(gp.read_game_mode("/definitely/not/here/x.sqlite3"))

    def test_reads_setting_from_temp_db(self):
        with temp_db() as db:
            db.set_setting("gate.game_mode", "0")
            self.assertFalse(gp.read_game_mode(db.path))
            db.set_setting("gate.game_mode", "1")
            self.assertTrue(gp.read_game_mode(db.path))
            db.set_setting("gate.game_mode", "off")
            self.assertFalse(gp.read_game_mode(db.path))


class TestRealCheckUsesLiveForeground(unittest.TestCase):
    """真实 check()：读实时前台；伪造的前台信息不能让预检放行。"""

    def test_check_runs_on_windows_and_returns_decision(self):
        d = gp.check()
        self.assertIn(d.reason, gp.REASON_LABELS)
        self.assertEqual(d.allowed, d.reason == gp.P_OK)
        # 会创建窗口 ⇔ 前台确实是白名单阅读窗口且非全屏；这里只做自洽断言
        if d.allowed:
            self.assertFalse(d.snapshot.fullscreen)
            self.assertFalse(d.snapshot.is_self)

    def test_mocked_foreground_cannot_forge_allow(self):
        """把前台伪造成「白名单阅读窗口」也不能让 check() 放行。

        原理：``app.gui_preflight`` 在导入时就**冻结**了真实的
        ``win32util.foreground_info`` / ``is_fullscreen`` 函数对象，
        ``mock.patch`` 只改模块属性，改不到预检实际调用的那几个函数。
        两次调用都固定住 ``read_game_mode``（设置库）以便对照。
        """
        fake = {"hwnd": 55501, "pid": 9999, "title": "文档", "class": "Chrome_WidgetWin_1",
                "exe": "C:\\Program Files\\msedge.exe", "app": "msedge.exe", "is_self": False}
        with mock.patch("app.gui_preflight.read_game_mode", return_value=False):
            with mock.patch("app.win32util.foreground_info", return_value=fake), \
                    mock.patch("app.win32util.is_fullscreen", return_value=False):
                forged = gp.check()
            honest = gp.check()
        self.assertEqual(forged.reason, honest.reason,
                         "预检结论不得被 mock 的前台信息改变")
        self.assertEqual(forged.allowed, honest.allowed)

    def test_consent_env_does_not_change_decision(self):
        os.environ[gp.CONSENT_ENV] = "1"
        try:
            self.assertTrue(gp.user_consented())
            d = gp.check()
            self.assertEqual(d.allowed, d.reason == gp.P_OK)
        finally:
            os.environ.pop(gp.CONSENT_ENV, None)
        self.assertFalse(gp.user_consented())

    @unittest.skipUnless(sys.platform == "win32", "只在 Windows 上验证")
    def test_snapshot_is_live_and_wellformed(self):
        s = gp.snapshot_foreground()
        self.assertTrue(s.supported, s.error)
        self.assertIsInstance(s.hwnd, int)
        self.assertIsInstance(s.fullscreen, bool)
        self.assertIsInstance(s.is_self, bool)


if __name__ == "__main__":
    unittest.main()
