"""前台门控策略验证（从简免配置版）。

新默认（用户要求「阅读白名单挡太多正常软件」）：
* **未知普通程序（微信/QQ/内部工具…）也允许尝试 UIA 读取**；
* 已知游戏 / 游戏平台进程（含 War Thunder 的 ``aces.exe``）→ 禁止，**零 UIA 调用**；
* 全屏前台 → 保守禁止；
* 桌面 / 本程序自身窗口 / 无前台 → 不读取；
* 用户手动暂停 / 手动游戏模式优先级最高，环境变化不得覆盖。

「禁止」的硬保证：零 UIA 调用、不显示浮层、不装全局鼠标钩子、丢弃在途结果。
"""
from __future__ import annotations

import unittest
from unittest import mock

from app.gate import (
    R_DESKTOP, R_FULLSCREEN, R_GAME_MODE, R_GAME_PROCESS, R_NO_FOREGROUND, R_OK,
    R_PAUSED, R_SELF, AccessGate, GateController,
)
from app.permissions import GAME_APPS, READING_APPS
from tests.support import FakeBridge, foreground, make_service, ok_response, temp_db


class TestGatePolicy(unittest.TestCase):
    def setUp(self):
        self._db_ctx = temp_db()
        self.db = self._db_ctx.__enter__()
        from app.config import Config
        self.cfg = Config(self.db)
        self.gate = AccessGate(self.cfg)

    def tearDown(self):
        self._db_ctx.__exit__(None, None, None)

    # ------------------------------------------------------- 默认放开
    def test_unknown_normal_app_is_allowed(self):
        """核心变更：未知普通程序不再被白名单挡掉。"""
        for exe in ("wechat.exe", "qq.exe", "some_random_tool.exe", "typora-unknown.exe",
                    "com.tencent.wechat.exe", "unknown_reader.exe"):
            with foreground(title="任意窗口", app=exe):
                d = self.gate.evaluate()
            self.assertTrue(d.allowed, f"{exe} 应当允许尝试读取（{d.reason}）")
            self.assertEqual(d.reason, R_OK)
            self.assertTrue(d.allows_read())
            self.assertTrue(d.allows_overlay())

    def test_known_reader_apps_are_allowed_too(self):
        for exe in ("msedge.exe", "chrome.exe", "firefox.exe", "WINWORD.EXE", "wps.exe",
                    "Acrobat.exe", "SumatraPDF.exe", "notepad.exe"):
            with foreground(title="doc", app=exe):
                d = self.gate.evaluate()
            self.assertTrue(d.allowed, f"{exe} 应该允许取词")
            self.assertEqual(d.reason, R_OK)

    def test_reading_apps_list_still_labels_known_apps(self):
        """清单仍然存在，但只用于显示名（不再决定放行）。"""
        with foreground(app="msedge.exe"):
            d = self.gate.evaluate()
        self.assertEqual(d.app_name, READING_APPS["msedge.exe"])
        with foreground(app="wechat.exe"):
            d = self.gate.evaluate()
        self.assertEqual(d.app_name, "wechat.exe", "未知进程回退到 exe 名")
        self.assertTrue(d.allowed, "未知进程照样放行")

    # ------------------------------------------------------- 游戏避让
    def test_game_process_blocked_with_clear_reason(self):
        for exe in ("steam.exe", "cs2.exe", "yuanshen.exe", "battle.net.exe",
                    "leagueclient.exe", "wegame.exe"):
            with foreground(title="游戏", app=exe):
                d = self.gate.evaluate()
            self.assertFalse(d.allowed, f"{exe} 必须被禁止")
            self.assertEqual(d.reason, R_GAME_PROCESS, f"{exe} → {d.reason}")
            self.assertTrue(d.hard_blocked)
            self.assertFalse(d.allows_overlay())

    def test_war_thunder_aces_is_a_known_game(self):
        """本机实际在玩的 War Thunder：``aces.exe`` 必须在游戏清单里，硬禁止。"""
        self.assertIn("aces.exe", GAME_APPS, "aces.exe 必须被当作游戏进程")
        with foreground(title="War Thunder", app="aces.exe",
                        exe_path=r"D:\war thunder\WarThunder\win64\aces.exe"):
            d = self.gate.evaluate()
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, R_GAME_PROCESS)
        self.assertNotIn("aces.exe", READING_APPS, "aces.exe 绝不能被当成阅读应用")

    def test_fullscreen_blocks_any_app(self):
        for exe in ("chrome.exe", "wechat.exe"):
            with foreground(app=exe, fullscreen=True):
                d = self.gate.evaluate()
            self.assertFalse(d.allowed, f"{exe} 全屏必须暂停")
            self.assertEqual(d.reason, R_FULLSCREEN)
            self.assertTrue(d.fullscreen)
            self.assertTrue(d.hard_blocked)

    # ------------------------------------------------------- 读取/浮层区分
    def test_self_and_desktop_blocked(self):
        with foreground(app="msedge.exe", is_self=True):
            d = self.gate.evaluate()
        self.assertEqual(d.reason, R_SELF)
        self.assertFalse(d.allows_overlay(), "主界面在前台时不应弹浮层")
        info = {"hwnd": 55501, "pid": 9999, "title": "桌面", "class": "Progman",
                "exe": "C:\\explorer.exe", "app": "explorer.exe", "is_self": False}
        with mock.patch("app.gate.w32.foreground_info", return_value=info), \
                mock.patch("app.gate.w32.is_probably_desktop", return_value=True):
            d = self.gate.evaluate()
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, R_DESKTOP)
        self.assertFalse(d.allows_overlay())

    def test_no_foreground_blocked(self):
        info = {"hwnd": 0, "pid": 0, "title": "", "class": "", "exe": "", "app": "",
                "is_self": False}
        with mock.patch("app.gate.w32.foreground_info", return_value=info):
            d = self.gate.evaluate()
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, R_NO_FOREGROUND)

    # ---------------------------------------------------------- 用户开关
    def test_manual_pause_beats_everything(self):
        self.cfg.set_bool("capture.enabled", False)
        with foreground(app="msedge.exe"):
            d = self.gate.evaluate()
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, R_PAUSED)
        self.assertTrue(d.blocked_by_user)
        self.assertTrue(d.hard_blocked)

    def test_game_mode_disables_everything(self):
        self.gate.set_game_mode(True)
        with foreground(app="msedge.exe"):
            d = self.gate.evaluate()
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, R_GAME_MODE)
        self.assertTrue(d.blocked_by_user)
        self.assertTrue(d.hard_blocked)

    def test_game_mode_off_does_not_override_manual_pause(self):
        """游戏模式关闭后，如果用户之前手动暂停了，仍然必须保持暂停。"""
        self.gate.set_game_mode(True)
        with foreground(app="msedge.exe"):
            self.assertEqual(self.gate.evaluate().reason, R_GAME_MODE)
        self.cfg.set_bool("capture.enabled", False)   # 用户又手动暂停
        self.gate.set_game_mode(False)                # 关闭游戏模式
        with foreground(app="msedge.exe"):
            d = self.gate.evaluate()
        self.assertFalse(d.allowed, "手动暂停不得被「游戏模式关闭」覆盖")
        self.assertEqual(d.reason, R_PAUSED)
        self.cfg.set_bool("capture.enabled", True)
        with foreground(app="msedge.exe"):
            self.assertTrue(self.gate.evaluate().allowed)

    # ---------------------------------------------------------- 控制器
    def test_controller_fires_only_on_transition(self):
        events: list[tuple[str, str]] = []
        ctl = GateController(self.gate,
                             on_lockdown=lambda d: events.append(("lock", d.reason)),
                             on_release=lambda d: events.append(("release", d.reason)))
        with foreground(app="steam.exe"):
            for _ in range(5):
                ctl.update()
        self.assertEqual(events, [("lock", R_GAME_PROCESS)],
                         "同一受限状态重复轮询不得反复触发")
        with foreground(app="wechat.exe"):
            for _ in range(5):
                ctl.update()
        self.assertEqual(events[-1], ("release", R_OK),
                         "未知普通程序应当解除受限")
        self.assertEqual(len(events), 2)
        self.assertFalse(ctl.is_locked())

    def test_controller_ignores_reason_change_with_same_allowed_state(self):
        """allowed 与 reason 都没变 → 不触发；reason 变了但都允许 → 也不重复动作。"""
        events: list[tuple[str, str]] = []
        ctl = GateController(self.gate,
                             on_lockdown=lambda d: events.append(("lock", d.reason)),
                             on_release=lambda d: events.append(("release", d.reason)))
        with foreground(app="wechat.exe"):
            ctl.update()
            ctl.update()
        with foreground(app="chrome.exe"):
            ctl.update()
        self.assertEqual(events, [], "允许 → 允许不应触发任何显隐")


class TestGateBlocksUia(unittest.TestCase):
    """被硬禁止时：零 UIA 调用、不建批次、不写词条。"""

    def _blocked_cases(self):
        return [
            ("游戏进程", dict(app="steam.exe")),
            ("War Thunder", dict(app="aces.exe")),
            ("全屏窗口", dict(app="msedge.exe", fullscreen=True)),
            ("全屏未知程序", dict(app="wechat.exe", fullscreen=True)),
        ]

    def test_zero_uia_calls_when_blocked(self):
        for name, fg_kwargs in self._blocked_cases():
            with self.subTest(name):
                with temp_db() as db:
                    bridge = FakeBridge(ok_response(text="secret"))
                    svc, _ = make_service(db, bridge)
                    with foreground(**fg_kwargs):
                        svc.handle_gesture(10, 10, "drag", delay=0)
                        sel, reason = svc.attempt_capture(10, 10, "drag")
                    self.assertIsNone(sel)
                    self.assertNotEqual(reason, "")
                    self.assertEqual(bridge.calls, [], "被阻止时绝不允许调用 UIA")
                    self.assertEqual(db.count_entries(), 0, "被阻止时不得写入词条")
                    self.assertEqual(db.list_batches(), [], "被阻止时不得创建批次")

    def test_game_mode_zero_uia_calls(self):
        with temp_db() as db:
            bridge = FakeBridge(ok_response(text="secret"))
            svc, cfg = make_service(db, bridge)
            cfg.set_bool("gate.game_mode", True)
            with foreground(app="wechat.exe"):
                svc.handle_gesture(10, 10, "drag", delay=0)
            self.assertEqual(bridge.calls, [])
            self.assertEqual(db.count_entries(), 0)

    def test_manual_pause_zero_uia_calls(self):
        with temp_db() as db:
            bridge = FakeBridge(ok_response(text="secret"))
            svc, cfg = make_service(db, bridge)
            cfg.set_bool("capture.enabled", False)
            with foreground(app="msedge.exe"):
                svc.handle_gesture(10, 10, "drag", delay=0)
            self.assertEqual(bridge.calls, [])
            self.assertEqual(db.count_entries(), 0)

    def test_unknown_app_now_reads_but_does_not_write_db(self):
        """未知普通程序：允许读取一次，但**选词不落库**（要用户点「记录」）。"""
        with temp_db() as db:
            events: list[tuple[str, dict]] = []
            bridge = FakeBridge(ok_response(text="alpha"))
            svc, _ = make_service(db, bridge)
            svc._on_event = lambda e, p: events.append((e, p))
            with foreground(app="wechat.exe"):
                svc.handle_gesture(10, 10, "drag", delay=0)
            self.assertEqual(len(bridge.calls), 1, "未知普通程序应当尝试读取")
            self.assertEqual(db.count_entries(), 0, "选词阶段绝不落库")
            self.assertEqual(db.list_batches(), [], "选词阶段绝不建批次")
            self.assertEqual([e for e, _ in events], ["selection_ready"])


class TestCaptureIdentityChecks(unittest.TestCase):
    """捕获前后校验前台 hwnd/pid，核验 UIA 响应的 process_id / top hwnd。"""

    def test_uia_pid_mismatch_dropped(self):
        with temp_db() as db:
            bridge = FakeBridge(ok_response(text="alpha"), process_id=424242, hwnd=55501)
            svc, _ = make_service(db, bridge)
            with foreground(app="msedge.exe", pid=9999, hwnd=55501):
                sel, reason = svc.attempt_capture(10, 10, "drag")
            self.assertIsNone(sel)
            self.assertIn("uia_pid_mismatch", reason)
            self.assertEqual(db.count_entries(), 0)

    def test_uia_hwnd_mismatch_dropped(self):
        with temp_db() as db:
            bridge = FakeBridge(ok_response(text="alpha"), process_id=9999, hwnd=77777)
            svc, _ = make_service(db, bridge)
            with foreground(app="msedge.exe", pid=9999, hwnd=55501):
                sel, reason = svc.attempt_capture(10, 10, "drag")
            self.assertIsNone(sel)
            self.assertIn("uia_hwnd_mismatch", reason)

    def test_missing_uia_identity_dropped(self):
        """拿不到可核对身份 → fail-closed，丢弃结果。"""
        with temp_db() as db:
            resp = ok_response(text="alpha")
            resp.pop("process_id")
            resp.pop("hwnd")
            bridge = FakeBridge(resp)
            bridge.process_id = 0
            bridge.hwnd = 0
            svc, _ = make_service(db, bridge)
            with foreground(app="msedge.exe"):
                sel, reason = svc.attempt_capture(10, 10, "drag")
            self.assertIsNone(sel)
            self.assertEqual(reason, "uia_identity_missing")

    def test_uia_from_our_own_process_dropped(self):
        import os

        with temp_db() as db:
            bridge = FakeBridge(ok_response(text="alpha"), process_id=os.getpid(), hwnd=55501)
            svc, _ = make_service(db, bridge)
            with foreground(app="msedge.exe", pid=9999, hwnd=55501):
                sel, reason = svc.attempt_capture(10, 10, "drag")
            self.assertIsNone(sel)
            self.assertEqual(reason, "uia_self")

    def test_foreground_change_during_call_dropped(self):
        """UIA 调用期间用户切走了 → 结果作废。"""
        with temp_db() as db:
            bridge = FakeBridge(ok_response(text="alpha"))
            svc, _ = make_service(db, bridge)
            reader = {"hwnd": 55501, "pid": 9999, "title": "doc", "class": "C",
                      "exe": "C:\\msedge.exe", "app": "msedge.exe", "is_self": False}
            other = {"hwnd": 60002, "pid": 4242, "title": "另一个窗口", "class": "C",
                     "exe": "C:\\notepad.exe", "app": "notepad.exe", "is_self": False}
            calls = {"n": 0}

            def fake_fg():
                calls["n"] += 1
                # 第 1 次：门控判定；第 2 次：捕获前；第 3 次：捕获后（已切走）
                return dict(reader if calls["n"] <= 2 else other)

            with mock.patch("app.capture_service.w32.foreground_info", side_effect=fake_fg), \
                    mock.patch("app.gate.w32.foreground_info", side_effect=fake_fg), \
                    mock.patch("app.capture_service.w32.is_probably_desktop",
                               return_value=False), \
                    mock.patch("app.gate.w32.is_probably_desktop", return_value=False):
                sel, reason = svc.attempt_capture(10, 10, "drag")
            self.assertIsNone(sel)
            self.assertEqual(reason, "foreground_changed")

    def test_gate_closed_during_call_dropped(self):
        """UIA 调用期间用户开了游戏模式 → 在途结果作废。"""
        with temp_db() as db:
            svc_holder = {}

            def flip(index):
                # 请求返回前把游戏模式打开
                svc_holder["svc"].config.set_bool("gate.game_mode", True)
                return ok_response(text="alpha")

            bridge = FakeBridge(flip)
            svc, cfg = make_service(db, bridge)
            svc_holder["svc"] = svc
            with foreground(app="msedge.exe"):
                sel, reason = svc.attempt_capture(10, 10, "drag")
            self.assertIsNone(sel)
            self.assertIn("gate_closed_during_call", reason)
            self.assertEqual(db.count_entries(), 0)

    def test_mouse_over_own_window_is_excluded(self):
        """点/拖我们自己的浮条：鼠标落点在自己窗口上 → 不取词。"""
        with temp_db() as db:
            bridge = FakeBridge(ok_response(text="alpha"))
            svc, _ = make_service(db, bridge)
            with foreground(app="msedge.exe"):
                with mock.patch("app.capture_service.w32.window_root_from_point",
                                return_value=55501), \
                        mock.patch("app.capture_service.w32.is_self_window",
                                   return_value=True):
                    sel, reason = svc.attempt_capture(300, 300, "drag")
            self.assertIsNone(sel)
            self.assertEqual(reason, "self_window")
            self.assertEqual(bridge.calls, [], "点自己窗口时不得调用 UIA")

    def test_gesture_generation_discards_inflight(self):
        """门控翻转后，排队中的手势必须作废。"""
        with temp_db() as db:
            bridge = FakeBridge(ok_response(text="alpha"))
            svc, _ = make_service(db, bridge)
            gen = svc.current_generation()
            svc.bump_generation()
            with foreground(app="msedge.exe"):
                svc.handle_gesture(10, 10, "drag", delay=0, generation=gen)
            self.assertEqual(bridge.calls, [])
            self.assertEqual(db.count_entries(), 0)


if __name__ == "__main__":
    unittest.main()
