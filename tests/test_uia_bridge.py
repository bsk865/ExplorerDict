"""T10 UIA helper 协议连通（真实 Windows PowerShell + UIAutomationClient）。

预检：这个类会启动 PowerShell 宿主并（在真实前台窗口上）尝试 UIA 文本读取，
因此 ``setUpClass`` 第一行做**实时真实前台**预检：游戏/全屏/非白名单前台时
明确 skip，不启动 helper、不读取任何窗口。
"""
from __future__ import annotations

import unittest
from pathlib import Path

from tests.support import ROOT, requires_real_foreground
from app.uia_bridge import UiaBridge, find_powershell

HELPER = ROOT / "scripts" / "uia_helper.ps1"


@unittest.skipUnless(find_powershell(), "系统没有 Windows PowerShell，跳过 UIA 集成测试")
@unittest.skipUnless(HELPER.exists(), "uia_helper.ps1 不存在")
class TestUiaBridgeReal(unittest.TestCase):
    @classmethod
    @requires_real_foreground("UIA helper 真实连通用例（会启动 helper 并读取前台选区）")
    def setUpClass(cls):
        cls.bridge = UiaBridge(HELPER)
        cls.started = cls.bridge.start()

    @classmethod
    def tearDownClass(cls):
        cls.bridge.stop()

    def test_helper_starts(self):
        self.assertTrue(self.started, f"helper 启动失败: {self.bridge.last_error()}")
        self.assertIsNotNone(self.bridge.pid())

    def test_ping_protocol(self):
        self.assertTrue(self.bridge.ping(10.0))

    def test_check_reports_capability(self):
        result = self.bridge.check(10.0)
        self.assertTrue(result.get("ok"), result)
        self.assertIn("textpattern_available", result)

    def test_selection_returns_structured_dict(self):
        """即使当前没有选区，也必须返回结构化结果而不是抛异常。

        传入**实时前台**的 hwnd/pid（helper 只允许读这个窗口的选区）。
        """
        from app import win32util as w32

        fg = w32.foreground_info()
        if not fg.get("hwnd") or not fg.get("pid"):
            self.skipTest("当前没有前台窗口，无法验证选区读取路径")
        resp = self.bridge.get_selection(
            context_chars=40, timeout=10.0,
            expected_hwnd=int(fg["hwnd"]), expected_pid=int(fg["pid"]),
        )
        self.assertIsInstance(resp, dict)
        self.assertIn("ok", resp)
        if resp.get("ok"):
            self.assertIsInstance(resp.get("text"), str)
            self.assertIsInstance(resp.get("context"), str)
        else:
            self.assertIn("reason", resp)

    def test_expected_window_mismatch_returns_foreground_changed(self):
        """expected 与实际前台不符 → helper 直接拒绝，**零候选读取**。"""
        from app import win32util as w32

        fg = w32.foreground_info()
        if not fg.get("hwnd"):
            self.skipTest("当前没有前台窗口")
        bogus_hwnd = int(fg["hwnd"]) + 1 if int(fg["hwnd"]) < 0xFFFFFF else int(fg["hwnd"]) - 1
        resp = self.bridge.get_selection(
            context_chars=40, timeout=10.0,
            expected_hwnd=bogus_hwnd, expected_pid=int(fg.get("pid") or 1),
        )
        self.assertFalse(resp.get("ok"), resp)
        self.assertIn(resp.get("reason"), ("foreground_changed", "foreground_changed_before_call"))
        self.assertEqual(resp.get("reads", 0), 0,
                         "前台身份不符时 helper 不得尝试任何 TextPattern 读取")

    def test_helper_reports_read_count(self):
        """契约：selection 响应带 reads 字段（真实读取尝试次数），成功路径 ≥ 1。"""
        from app import win32util as w32

        fg = w32.foreground_info()
        if not fg.get("hwnd") or not fg.get("pid"):
            self.skipTest("当前没有前台窗口")
        resp = self.bridge.get_selection(
            expected_hwnd=int(fg["hwnd"]), expected_pid=int(fg["pid"]), timeout=10.0)
        self.assertIn("reads", resp, resp)
        self.assertIsInstance(resp["reads"], int)


class TestUiaHelperWindowGate(unittest.TestCase):
    """直接打 helper 的 JSON 协议：身份不符时**零读取**，不需要真实前台窗口。

    仍会启动 PowerShell helper 进程（它会枚举前台窗口），因此同样先做实时前台预检。
    """

    @classmethod
    @requires_real_foreground("UIA helper 协议用例（会启动 helper 进程）")
    def setUpClass(cls):
        from app.uia_bridge import find_powershell

        if not find_powershell():
            raise unittest.SkipTest("系统没有 Windows PowerShell")
        if not HELPER.exists():
            raise unittest.SkipTest("uia_helper.ps1 不存在")
        cls.bridge = UiaBridge(HELPER)
        if not cls.bridge.start():
            raise unittest.SkipTest(f"UIA helper 启动失败: {cls.bridge.last_error()}")

    @classmethod
    def tearDownClass(cls):
        cls.bridge.stop()

    def test_missing_expected_window_reads_nothing(self):
        resp = self.bridge._request({"cmd": "selection", "contextChars": 20}, timeout=10.0)
        self.assertFalse(resp.get("ok"), resp)
        self.assertEqual(resp.get("reason"), "expected_window_required")
        self.assertEqual(resp.get("reads"), 0, "没有可核对身份时不得读取")

    def test_bogus_expected_window_reads_nothing(self):
        """用一个几乎不可能存在的 hwnd：helper 侧 real 前台必然不符 → 零读取。"""
        resp = self.bridge._request(
            {"cmd": "selection", "contextChars": 20, "x": 1, "y": 1,
             "expectedHwnd": 0x7FFFFFF0, "expectedPid": 0x7FFFFFF0},
            timeout=10.0,
        )
        self.assertFalse(resp.get("ok"), resp)
        self.assertEqual(resp.get("reason"), "foreground_changed")
        self.assertEqual(resp.get("reads"), 0,
                         "实时前台不等于 expected 时，helper 不得尝试 TextPattern 读取")

    def test_check_without_expected_window_reads_nothing(self):
        resp = self.bridge._request({"cmd": "check"}, timeout=10.0)
        self.assertFalse(resp.get("ok"), resp)
        self.assertEqual(resp.get("reason"), "expected_window_required")


class TestUiaBridgeFailsClosed(unittest.TestCase):
    """没有可核对的期望窗口时：fail-closed，连请求都不发。"""

    def test_get_selection_requires_expected_window(self):
        bridge = UiaBridge(Path("Z:/definitely/not/here/uia_helper.ps1"))
        for kwargs in ({}, {"expected_hwnd": 1234}, {"expected_pid": 99}):
            resp = bridge.get_selection(context_chars=40, **kwargs)
            self.assertFalse(resp.get("ok"), resp)
            self.assertEqual(resp.get("reason"), "expected_window_required")
        bridge.stop()

    def test_foreground_changed_before_call_blocks_request(self):
        from unittest import mock

        from tests.support import foreground

        bridge = UiaBridge(HELPER)
        sent: list[dict] = []
        with foreground(app="chrome.exe", hwnd=55501, pid=9999):
            with mock.patch.object(bridge, "_request",
                                   side_effect=lambda payload, timeout=3.0: sent.append(payload)):
                # 期望的窗口不是当前前台 → 客户端侧就直接拒绝，不发请求
                resp = bridge.get_selection(expected_hwnd=55599, expected_pid=9999)
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("reason"), "foreground_changed_before_call")
        self.assertEqual(sent, [], "身份不符时不得向 helper 发送任何请求")

    def test_check_requires_foreground(self):
        from unittest import mock

        from tests.support import no_process_foreground

        bridge = UiaBridge(HELPER)
        with no_process_foreground():
            with mock.patch.object(bridge, "_request",
                                   side_effect=AssertionError("不得发请求")):
                resp = bridge.check(timeout=1.0)
        self.assertFalse(resp.get("ok"))
        self.assertEqual(resp.get("reason"), "no_foreground")


class TestUiaBridgeDegradation(unittest.TestCase):
    """helper 不可用时必须优雅退化，不能抛异常。"""

    def test_missing_script(self):
        bridge = UiaBridge(Path("Z:/definitely/not/here/uia_helper.ps1"))
        self.assertFalse(bridge.start())
        resp = bridge.get_selection()
        self.assertFalse(resp["ok"])
        self.assertIn("reason", resp)
        bridge.stop()

    def test_stop_is_idempotent(self):
        bridge = UiaBridge(Path("Z:/definitely/not/here/uia_helper.ps1"))
        bridge.stop()
        bridge.stop()
        self.assertFalse(bridge.get_selection()["ok"])


if __name__ == "__main__":
    unittest.main()
