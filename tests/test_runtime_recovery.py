"""运行时恢复回归（纯 mock：不建 Tk 窗口、不装钩子、不启 UIA、不联网）。

覆盖本轮修复的四条链路：

* **服务生命周期**：构造期绝不启动服务；只有硬阻断才不装鼠标钩子
  （self / 桌面这类软受限也装，取词门控不变）；钩子缺失时由门控轮询补装，
  失败重试有 2 秒下限；UIA 的 start / resume 绝不并发重叠；
* **页面跟随**：首次有效来源也要通知视图并更新页面世代；切页 A→B→A 时
  新页面的首选区不被迟到事件误杀；``resume``（从自身窗口 / 桌面回到同一页）
  只在手工浏览非 None 时恢复跟随，不清理选区、不递增世代；
* **托盘**：菜单回调跨线程只投 ``_ui_q``，退出时清理图标；后台打开遵守硬门控；
* **重复启动**：``activation`` 呼出提示一次「已在运行」，首次 ``--open-main``
  不提示，硬阻断期间提示暂存到安全时；旧实例没有事件通道时保持现有 fallback；
* **UIA helper 回收**：请求超时当场淘汰卡死的 helper（不留永远等不到响应的
  队列项、重置重启预算），下一次调用能重新拉起；
* **helper 脚本结构**（静态文本检查，**不代表实测运行覆盖**）：候选有界、
  先验证后读 TextPattern、浏览器子 renderer 单独字段、不给 Name 当选中文本。
"""
from __future__ import annotations

import ast
import contextlib
import io
import re
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.support import (
    DEFAULT_HEADLESS_FG, FakeBridge, headless_app, ok_response, pump_app,
    run_gesture_worker,
)

from app.uia_bridge import UiaBridge


# --------------------------------------------------------------------- 工具
def _self_fg() -> dict:
    return {"hwnd": 55599, "pid": 1234, "title": "探索词典", "class": "TkTopLevel",
            "exe": "C:\\Program Files\\python.exe", "app": "python.exe",
            "is_self": True}


def _game_fg() -> dict:
    return {"hwnd": 91001, "pid": 9101, "title": "某游戏", "class": "GameWin",
            "exe": "C:\\Program Files\\steam.exe", "app": "steam.exe",
            "is_self": False}


@contextlib.contextmanager
def fake_foreground(info: dict):
    """把**实时前台**换成给定元信息（gate 与 capture_service 读的是同一个模块）。"""
    with mock.patch("app.win32util.foreground_info", return_value=dict(info)), \
            mock.patch("app.win32util.is_probably_desktop", return_value=False), \
            mock.patch("app.win32util.is_fullscreen", return_value=False):
        yield info


def _decision(allowed: bool, reason: str):
    from app.gate import GateDecision

    return GateDecision(allowed=allowed, reason=reason)


def _make_sel(term: str = "alpha", hwnd: int = 55501, pid: int = 9999):
    from app.models import CapturedSelection, SourceInfo, now_iso

    return CapturedSelection(
        term=term, context=f"…{term} beta…", method="ui_textpattern",
        source=SourceInfo(hwnd=hwnd, pid=pid, exe="C:\\Program Files\\msedge.exe",
                          app="msedge.exe", title="Some Document",
                          url="https://a.example.com/p", confidence="url_document",
                          doc_key="url:https://a.example.com/p"),
        captured_at=now_iso())


def _queue_selection(app, sel, page_epoch=None) -> None:
    app._ui_q.put(("selection_ready", {
        "selection": sel, "point": (10, 10), "kind": "drag", "reason": "",
        "generation": app.capture_service.current_generation(),
        "page_epoch": (app.capture_service.current_page_epoch()
                       if page_epoch is None else page_epoch),
        "hwnd": sel.source.hwnd, "pid": sel.source.pid}))


# ======================================================= 1. 服务生命周期
class TestServiceLifecycleRecovery(unittest.TestCase):
    def test_construction_never_starts_services(self):
        with headless_app() as app:
            self.assertFalse(app._services_started)
            self.assertEqual(app.mouse_hook.starts, 0, "构造期绝不安装钩子")
            self.assertEqual(app.uia.start_calls, 0, "构造期绝不拉起 UIA")

    def test_start_with_self_window_installs_hook(self):
        """双击 exe（``--open-main`` 先显示自己 → self 软受限）也必须装监听。"""
        with headless_app(fg=_self_fg()) as app:
            with mock.patch.object(app, "_start_uia_async") as uia_start:
                app._start_services()
            self.assertTrue(app._services_started)
            self.assertEqual(uia_start.call_count, 1)
            self.assertEqual(app.mouse_hook.starts, 1, "软受限前台也要安装鼠标钩子")
            self.assertTrue(app.mouse_hook.running)
            # 取词门控**不变**：软受限前台仍然不允许读取
            decision = app.gate_decision()
            self.assertFalse(decision.allowed)
            self.assertTrue(decision.soft_blocked)

    def test_hard_blocked_startup_keeps_hook_off_then_recovers(self):
        with headless_app(fg=_game_fg()) as app:
            with mock.patch.object(app, "_start_uia_async"):
                app._start_services()
            self.assertEqual(app.mouse_hook.starts, 0, "硬阻断启动不得安装钩子")
            self.assertFalse(app.mouse_hook.running)
            self.assertFalse(app.uia.enabled)
            self.assertTrue(app.gate_decision().hard_blocked)

            with fake_foreground(DEFAULT_HEADLESS_FG):
                app._apply_gate(force=True)
            self.assertEqual(app.mouse_hook.starts, 1, "离开硬阻断必须补装监听")
            self.assertTrue(app.mouse_hook.running)

    def test_apply_gate_reinstalls_missing_hook(self):
        with headless_app() as app:
            app._services_started = True
            app._apply_gate(force=True)
            self.assertEqual(app.mouse_hook.starts, 1)
            app.mouse_hook.running = False        # 监听被系统回收 / 掉了
            app._hook_retry_at = 0.0
            app._apply_gate()
            self.assertEqual(app.mouse_hook.starts, 2, "非硬阻断且钩子没跑就必须补装")

    def test_hook_retry_is_rate_limited(self):
        with headless_app() as app:
            app._services_started = True
            attempts: list[int] = []

            def _fail(timeout: float = 5.0) -> bool:
                attempts.append(1)
                return False

            app.mouse_hook.start = _fail
            app.mouse_hook.running = False
            app._hook_retry_at = 0.0
            self.assertFalse(app._ensure_mouse_hook())
            self.assertFalse(app._ensure_mouse_hook())
            self.assertFalse(app._ensure_mouse_hook())
            self.assertEqual(len(attempts), 1, "失败后 2 秒内绝不重试（更不每 60ms 重启）")
            app._hook_retry_at = 0.0
            self.assertFalse(app._ensure_mouse_hook())
            self.assertEqual(len(attempts), 2, "过了重试窗口才允许再试一次")

    def test_uia_start_and_resume_never_overlap(self):
        with headless_app() as app:
            active: list[int] = []
            peak: list[int] = []

            def _slow_start() -> bool:
                active.append(1)
                peak.append(len(active))
                time.sleep(0.05)
                active.pop()
                return True

            app.uia.start = _slow_start
            app._uia_wanted = True
            workers = [threading.Thread(target=app._start_uia, name=f"uia-{i}")
                       for i in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(5)
            self.assertEqual(max(peak), 1, "start / resume 共用一把锁：绝不并发交错")


# ========================================================= 2. 页面跟随
class TestPageFollowRecovery(unittest.TestCase):
    def test_first_valid_source_notifies_views_and_bumps_epoch(self):
        with headless_app() as app:
            svc = app.capture_service
            generation, epoch = svc.current_generation(), svc.current_page_epoch()
            self.assertIsNone(svc.last_source())

            svc.note_foreground(dict(DEFAULT_HEADLESS_FG))
            pump_app(app)

            self.assertIsNotNone(svc.last_source())
            self.assertGreater(svc.current_generation(), generation,
                               "首次有效来源也要当场作废旧结果")
            self.assertGreater(svc.current_page_epoch(), epoch,
                               "首次有效来源也要更新页面世代")
            self.assertEqual(app.main.follow_calls, 1, "首次来源必须通知视图跟随当前页")
            self.assertNotIn("已作废", str(app.main.status_label.cget("text")),
                             "首次来源没有旧选区可作废，不该弹作废提示")

    def test_a_b_a_stale_event_does_not_kill_the_first_selection(self):
        """切页 A→B→A：B 的迟到事件绝不能清掉回到 A 之后的首选区。"""
        page_a = dict(DEFAULT_HEADLESS_FG, title="Page A")
        page_b = dict(DEFAULT_HEADLESS_FG, title="Page B")
        bridge = FakeBridge(ok_response(text="A页首词", context="…A页首词…"))
        with headless_app(fg=page_a, bridge=bridge) as app:
            svc = app.capture_service
            svc.note_foreground(page_a)          # 首次：页面 A

            stale: list[dict] = []
            original = svc._on_foreground_change

            def _collect(src, kind, meta=None):
                stale.append({"source": src, "kind": kind,
                              "page_epoch": (meta or {}).get("page_epoch")})

            svc._on_foreground_change = _collect
            try:
                svc.note_foreground(page_b)      # A → B（事件先不排队，模拟迟到）
                svc.note_foreground(page_a)      # 又切回 A
            finally:
                svc._on_foreground_change = original
            self.assertEqual(len(stale), 2)

            # 用户回到 A 之后划的第一个词（真实入队 → 工作线程链路）
            app._on_gesture(20, 20, "drag")
            run_gesture_worker(app)
            pump_app(app)
            self.assertIsNotNone(app._current_selection, "A 页首选区必须已经装好")
            token = app._selection_token
            epoch = app._selection_page_epoch

            # B 页那条**迟到**事件现在才到：绝不能误杀 A 页的首选区
            app._ui_q.put(("foreground_changed", {
                "source": stale[0]["source"], "kind": "title",
                "page_epoch": stale[0]["page_epoch"]}))
            pump_app(app)
            self.assertIsNotNone(app._current_selection, "A→B→A 的首选区不得被误杀")
            self.assertEqual(app._current_selection.term, "A页首词")
            self.assertEqual(app._selection_token, token, "保留选区时不得推高选区序号")
            self.assertEqual(app._selection_page_epoch, epoch, "世代标记不得被清掉")
            self.assertNotIn("已作废", str(app.main.status_label.cget("text")))

    def test_resume_releases_manual_browse_without_touching_selection(self):
        with headless_app(overlays="panel", main_window="real") as app:
            svc = app.capture_service
            svc.note_foreground(dict(DEFAULT_HEADLESS_FG))
            _queue_selection(app, _make_sel())
            pump_app(app)
            other = int(app.db.create_batch("手工浏览主题", "url:https://other.example.com/x"))
            self.assertTrue(app.set_browse_scope(other))
            self.assertEqual(app.browse_scope(), other)

            token = app._selection_token
            generation, epoch = svc.current_generation(), svc.current_page_epoch()
            follow_spy = mock.Mock(wraps=app.main.follow_current_page)
            app.main.follow_current_page = follow_spy

            svc.note_foreground(dict(DEFAULT_HEADLESS_FG), resumed=True)
            pump_app(app)

            self.assertIsNone(app.browse_scope(), "resume 必须交还手工浏览")
            follow_spy.assert_called_once()
            self.assertIsNotNone(app._current_selection, "resume 不得清理本页选区")
            self.assertEqual(app._selection_token, token, "resume 不得推高选区序号")
            self.assertEqual(svc.current_generation(), generation)
            self.assertEqual(svc.current_page_epoch(), epoch,
                             "同页 resume 不递增任何世代")

    def test_resume_without_manual_browse_does_not_touch_views(self):
        with headless_app() as app:
            svc = app.capture_service
            svc.note_foreground(dict(DEFAULT_HEADLESS_FG))     # first
            follows_before = app.main.follow_calls
            svc.note_foreground(dict(DEFAULT_HEADLESS_FG), resumed=True)
            pump_app(app)
            self.assertIsNone(app.browse_scope())
            self.assertEqual(app.main.follow_calls, follows_before + 1,
                             "只有首次来源那一次跟随；没有手工浏览时 resume 什么都不做")

    def test_watcher_emits_resume_after_self_interruption(self):
        """ForegroundWatcher：自身/桌面打断后回到同一页要补一条 resumed 调用。"""
        from app.capture_service import ForegroundWatcher

        calls: list[tuple[int, bool]] = []

        class _Service:
            def note_foreground(self, info=None, *, resumed=False):
                calls.append((int((info or {}).get("hwnd") or 0), bool(resumed)))

        reader = dict(DEFAULT_HEADLESS_FG)
        sequence = [reader, dict(reader, is_self=True), dict(reader, is_self=True), reader]
        state = {"i": 0}

        def _fg():
            index = state["i"]
            state["i"] = index + 1
            if index < len(sequence):
                return dict(sequence[index])
            return dict(reader, is_self=True)

        watcher = ForegroundWatcher(_Service(), interval=0.01)
        with mock.patch("app.capture_service.w32.foreground_info", side_effect=_fg), \
                mock.patch("app.capture_service.w32.is_probably_desktop",
                           return_value=False):
            worker = threading.Thread(target=watcher.run, name="fg-watcher-test")
            worker.start()
            try:
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if any(resumed for _, resumed in calls):
                        break
                    time.sleep(0.01)
            finally:
                watcher.stop()
                worker.join(2)
        self.assertEqual(calls[0], (int(reader["hwnd"]), False),
                         "首次有效来源走普通 change")
        self.assertIn((int(reader["hwnd"]), True), calls,
                      "自身窗口打断后回到同一阅读页必须发 resume")


# ============================================================= 3. 托盘
class FakeTrayIcon:
    """``pystray.Icon`` 的最小替身：只记录 run / stop / notify。"""

    def __init__(self, name=None, image=None, title=None, menu=None):
        self.name, self.image, self.title, self.menu = name, image, title, menu
        self.runs = 0
        self.stops = 0
        self.notices: list[tuple[str, str]] = []
        self.started = threading.Event()
        self._release = threading.Event()

    def run(self):
        self.runs += 1
        self.started.set()
        self._release.wait(5)

    def stop(self):
        self.stops += 1
        self._release.set()

    def notify(self, message, title=None):
        self.notices.append((str(message), str(title)))


def _start_fake_tray(app):
    """给 App 装一个假 factory 并启动托盘；返回 (fake_icon 容器, TrayIcon)。"""
    from app.tray import TrayIcon

    box: dict = {}

    def _factory(name=None, image=None, title=None, menu=None):
        icon = FakeTrayIcon(name, image, title, menu)
        box["icon"] = icon
        return icon

    tray = TrayIcon(app, icon_factory=_factory, image_loader=lambda: object())
    self_ok = tray.start()
    assert self_ok, "假 factory 必须能启动"
    box["icon"].started.wait(3)
    app._tray = tray
    return box, tray


class TestTrayIntegration(unittest.TestCase):
    def test_menu_callbacks_only_enqueue_and_quit_cleans_up(self):
        from app.tray import EVENT_OPEN, EVENT_QUIT, EVENT_TOGGLE_PAUSE

        with headless_app() as app:
            box, tray = _start_fake_tray(app)
            icon = box["icon"]
            self.assertEqual(icon.runs, 1, "常驻一个图标：只跑一条消息循环")

            before_deiconify = app.fake_root.deiconify_calls
            before_config = app.config.capture_enabled

            # 从**别的线程**触发菜单回调：只允许投事件，绝不碰 Tk
            for callback in (tray._on_open, tray._on_toggle_pause):
                worker = threading.Thread(target=callback, args=(icon, None))
                worker.start()
                worker.join(5)
            self.assertEqual(app.fake_root.deiconify_calls, before_deiconify,
                             "托盘线程绝不直接开窗")
            self.assertEqual(app.config.capture_enabled, before_config,
                             "托盘线程绝不直接改配置")

            first = app._ui_q.get_nowait()
            second = app._ui_q.get_nowait()
            self.assertEqual([first[0], second[0]], [EVENT_OPEN, EVENT_TOGGLE_PAUSE])
            app._ui_q.put(first)
            app._ui_q.put(second)

            pump_app(app)                      # UI 线程消费：真正打开 + 切换暂停
            self.assertEqual(app.fake_root.deiconify_calls, before_deiconify + 1)
            self.assertNotEqual(app.config.capture_enabled, before_config)

            tray._on_quit(icon, None)
            self.assertEqual(app._ui_q.get_nowait()[0], EVENT_QUIT)

            tray.stop()
            self.assertEqual(icon.stops, 1, "退出必须删除图标")
            self.assertFalse(tray.running())
            tray.stop()
            self.assertEqual(icon.stops, 1)

    def test_app_start_creates_tray_and_quit_removes_it(self):
        with headless_app() as app:
            created: list = []

            def _factory(app_):
                from app.tray import TrayIcon

                box: dict = {}

                def _icon_factory(name=None, image=None, title=None, menu=None):
                    icon = FakeTrayIcon(name, image, title, menu)
                    box["icon"] = icon
                    return icon

                tray = TrayIcon(app_, icon_factory=_icon_factory,
                                image_loader=lambda: object())
                tray._box = box
                created.append(tray)
                return tray

            app.tray_factory = _factory
            app.start()
            self.assertEqual(len(created), 1, "app.start 必须创建托盘")
            self.assertIs(app._tray, created[0])
            created[0]._box["icon"].started.wait(3)

            app.selection_bar.destroy = lambda: None      # 假替身没有 Tk destroy
            app.quit()
            self.assertIsNone(app._tray, "退出必须删除托盘")
            self.assertEqual(created[0]._box["icon"].stops, 1)

    def test_tray_open_respects_hard_gate(self):
        from app.gate import R_GAME_PROCESS

        with headless_app() as app:
            _start_fake_tray(app)
            blocked = _decision(False, R_GAME_PROCESS)
            with mock.patch.object(app.gate, "evaluate", return_value=blocked):
                app._ui_q.put(("tray_open", None))
                pump_app(app)
            self.assertEqual(app.fake_root.deiconify_calls, 0,
                             "硬阻断下托盘打开主界面一次 deiconify 都不做")
            self.assertTrue(app._pending_open_main, "请求必须暂存，等安全时再兑现")

    def test_close_window_keeps_running_with_tray(self):
        with headless_app() as app:
            box, tray = _start_fake_tray(app)
            with mock.patch.object(app.fake_root, "withdraw") as withdraw:
                app.on_main_close()
                withdraw.assert_called_once()
            self.assertTrue(tray.running(), "关窗后托盘必须继续常驻")
            self.assertIs(app._tray, tray)


# ==================================================== 4. 重复启动提示
class TestAlreadyRunningNotice(unittest.TestCase):
    def test_activation_shows_notice_once(self):
        with headless_app(overlays="panel", main_window="real") as app:
            notices: list = []

            class _Tray:
                def notify(self, message, title=None):
                    notices.append(str(message))
                    return True

            app._tray = _Tray()
            self.assertTrue(app.request_open_main("activation"))
            self.assertEqual(app.fake_root.deiconify_calls, 0)
            self.assertTrue(app.reading_panel.visible)
            self.assertIn("已在运行", str(app.main.status_label.cget("text")))
            self.assertEqual(len(notices), 1, "托盘通知只发一次")

    def test_first_open_via_argv_shows_no_notice(self):
        with headless_app(overlays="panel", main_window="real") as app:
            notices: list = []

            class _Tray:
                def notify(self, message, title=None):
                    notices.append(str(message))
                    return True

            app._tray = _Tray()
            self.assertTrue(app.request_open_main("argv"))
            self.assertEqual(app.fake_root.deiconify_calls, 1)
            self.assertNotIn("已在运行", str(app.main.status_label.cget("text")),
                             "首次正常打开不显示重复提示")
            self.assertEqual(notices, [])

    def test_hard_blocked_activation_notice_is_deferred(self):
        from app.gate import R_GAME_PROCESS, R_OK

        with headless_app(overlays="panel", main_window="real") as app:
            notices: list = []

            class _Tray:
                def notify(self, message, title=None):
                    notices.append(str(message))
                    return True

            app._tray = _Tray()
            with mock.patch.object(app.gate, "evaluate",
                                   return_value=_decision(False, R_GAME_PROCESS)):
                self.assertFalse(app.request_open_main("activation"))
            self.assertTrue(app._pending_open_main)
            self.assertEqual(app.fake_root.deiconify_calls, 0)
            self.assertEqual(notices, [], "硬阻断期间提示必须暂存")

            with mock.patch.object(app.gate, "evaluate", return_value=_decision(True, R_OK)):
                self.assertTrue(app._drain_activation())
            self.assertEqual(app.fake_root.deiconify_calls, 0)
            self.assertTrue(app.reading_panel.visible)
            self.assertEqual(len(notices), 1, "安全后只提示一次")
            self.assertIn("已在运行", str(app.main.status_label.cget("text")))

    def test_old_instance_without_event_keeps_fallback_box(self):
        import desktop_entry
        from app import activation

        boxes: list[str] = []
        with mock.patch.object(desktop_entry, "message_box",
                               side_effect=lambda text: boxes.append(str(text)) or True), \
                mock.patch("bootstrap.main",
                           return_value=desktop_entry.EXIT_ALREADY_RUNNING), \
                mock.patch("app.activation.request_activation",
                           return_value=activation.REQUEST_NO_INSTANCE):
            code = desktop_entry.main([])
        self.assertEqual(code, desktop_entry.EXIT_ALREADY_RUNNING)
        self.assertEqual(len(boxes), 1, "旧版没有事件通道时保持现有中文提示")
        self.assertIn("完全退出", boxes[0])


# ================================================= 5. UIA helper 超时回收
class FakeProc:
    """子进程替身：记录 wait / kill 与流关闭，不创建任何真实进程。"""

    def __init__(self):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()
        self._alive = True
        self.waited = 0
        self.killed = 0

    def poll(self):
        return None if self._alive else 0

    def wait(self, timeout=None):
        self.waited += 1
        self._alive = False
        return 0

    def kill(self):
        self.killed += 1
        self._alive = False


class TestUiaBridgeTimeoutEviction(unittest.TestCase):
    FG = {"hwnd": 55501, "pid": 9999, "title": "Doc", "app": "msedge.exe",
          "exe": "C:\\Program Files\\msedge.exe", "is_self": False}

    def _bridge(self):
        bridge = UiaBridge(Path("Z:/unused/uia_helper.ps1"))
        proc = FakeProc()
        bridge._proc = proc
        bridge._restarts = 3
        return bridge, proc

    def _wait_gone(self, proc) -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if proc.waited or proc.killed:
                return
            time.sleep(0.01)

    def test_timeout_evicts_stuck_helper(self):
        bridge, proc = self._bridge()
        with mock.patch("app.uia_bridge.w32.foreground_info",
                        return_value=dict(self.FG)), \
                mock.patch.object(bridge, "_request",
                                  side_effect=TimeoutError("stuck")):
            resp = bridge.get_selection(expected_hwnd=55501, expected_pid=9999,
                                        timeout=0.01)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["reason"], "timeout")
        self.assertIsNone(bridge._proc, "卡死的 helper 必须当场摘掉（不占住下一次请求）")
        self.assertEqual(bridge._pending, {}, "不得留下永远等不到响应的队列项")
        self.assertEqual(bridge._restarts, 0, "淘汰后重置重启预算：下次可恢复")
        self._wait_gone(proc)
        self.assertTrue(proc.waited or proc.killed, "后台线程必须真的终止它")
        self.assertTrue(proc.stdin.closed, "管道必须释放")

    def test_next_call_starts_a_fresh_helper(self):
        bridge, _proc = self._bridge()
        with mock.patch("app.uia_bridge.w32.foreground_info",
                        return_value=dict(self.FG)), \
                mock.patch.object(bridge, "_request",
                                  side_effect=TimeoutError("stuck")):
            bridge.get_selection(expected_hwnd=55501, expected_pid=9999, timeout=0.01)

        starts: list[int] = []
        with mock.patch.object(bridge, "start",
                               side_effect=lambda: (starts.append(1), True)[1]), \
                mock.patch("app.uia_bridge.w32.foreground_info",
                           return_value=dict(self.FG)), \
                mock.patch.object(bridge, "_request",
                                  return_value={"ok": True, "text": "alpha"}):
            resp = bridge.get_selection(expected_hwnd=55501, expected_pid=9999,
                                        timeout=0.01)
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(len(starts), 1, "淘汰之后下一次取词必须能重新拉起 helper")

    def test_check_timeout_also_evicts(self):
        bridge, proc = self._bridge()
        with mock.patch("app.uia_bridge.w32.foreground_info",
                        return_value=dict(self.FG)), \
                mock.patch.object(bridge, "_request",
                                  side_effect=TimeoutError("stuck")):
            resp = bridge.check(timeout=0.01)
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["reason"], "timeout")
        self.assertIsNone(bridge._proc)
        self._wait_gone(proc)


# ============================================ 6. helper 脚本结构（静态检查）
HELPER = Path(__file__).resolve().parent.parent / "scripts" / "uia_helper.ps1"


class TestUiaHelperCandidateStructure(unittest.TestCase):
    """**静态结构检查**，不是运行覆盖：只断言脚本里存在这些有界 / 门控结构。"""

    @classmethod
    def setUpClass(cls):
        cls.raw = HELPER.read_bytes()
        cls.text = cls.raw.decode("ascii")

    def test_script_is_pure_ascii(self):
        # Windows PowerShell 5.1 用 ANSI 代码页读无 BOM 的 .ps1：
        # 非 ASCII 字面量会被破坏，所以必须保持纯 ASCII。
        self.assertTrue(all(byte < 128 for byte in self.raw))

    def test_candidate_budget_is_bounded(self):
        for token in ("$script:MaxCandidates", "$script:MaxAncestors",
                      "$script:MaxTopDocuments", "$script:MaxTopTextControls"):
            self.assertIn(token, self.text)
        self.assertIn("Add-AncestorCandidates", self.text, "必须有点下/焦点元素的有界祖先")
        self.assertIn("Add-TopLevelCandidates", self.text, "必须有验证过顶层下的有界搜索")
        # 绝不从桌面根枚举
        self.assertNotRegex(self.text, r"\$script:Root\s*\.\s*Find")

    def test_every_candidate_is_validated_before_textpattern(self):
        loop = self.text.split("while ($index -lt $candidates.Count)", 1)[1]
        loop = loop.split("function Handle-Check", 1)[0]
        self.assertLess(loop.index("Test-ForegroundIs $exp"),
                        loop.index("Try-SelectionFrom"),
                        "读取候选前必须先核对实时前台")
        self.assertLess(loop.index("Test-Candidate $cand.el $exp"),
                        loop.index("Try-SelectionFrom"),
                        "读取候选前必须先核对候选所属顶层窗口")
        self.assertIn("expected_window_required", self.text, "缺身份时 fail-closed")

    def test_browser_child_pid_is_separate_and_verified(self):
        self.assertIn("Test-BrowserChildCandidate", self.text)
        self.assertIn("BrowserWindowClasses", self.text)
        self.assertIn("BrowserExeNames", self.text)
        self.assertIn("candidate_element_pid_mismatch", self.text,
                      "普通非浏览器不同 pid 必须继续拒绝")
        success = self.text.split("function Handle-Selection", 1)[1].split("$result = @{", 1)[1].split("break", 1)[0]
        self.assertIn("process_id   = $exp.pid", success,
                      "响应里的 process_id 必须是验证过的顶层 pid")
        self.assertIn("hwnd         = $exp.hwnd", success,
                      "响应里的 hwnd 必须是验证过的顶层窗口")
        self.assertIn("element_process_id", success, "子 renderer pid 只能单独给字段")

    def test_no_name_as_selected_text_and_clear_reason(self):
        self.assertIn("no_candidate_element", self.text, "无可读选区要给明确 reason")
        self.assertNotIn("result.text = Safe-Str", self.text)
        self.assertNotRegex(self.text, r"\$result\.text\s*=\s*\$[A-Za-z]+\.Current\.Name",
                            "绝不把元素 Name 当成已选文本")
        self.assertIn("GetText", self.text, "选中文本只能来自 TextPattern 的 GetText")


# ============================================ 7. 冻结运行时约束（静态检查）
APP_DIR = Path(__file__).resolve().parent.parent / "app"


class TestShippedCodeRunsInTheFrozenRuntime(unittest.TestCase):
    """出货代码不许碰冻结运行时里**没有**的标准库模块。

    实测（2026-10-03）：``探索词典.exe`` 的 ``_runtime`` 里 *没有*
    ``tkinter.ttk`` —— 打包时没有任何模块 import 它，PyInstaller 就没把它
    收进 ``PYZ.pyz``（298 个模块里 ``tkinter.*`` 只有 ``__init__`` /
    ``commondialog`` / ``constants`` / ``font`` / ``messagebox`` /
    ``simpledialog``）。而 frozen 入口**从磁盘加载 ``app`` 包**，于是源码里
    一句 ``from tkinter import ttk`` 就让整个程序在 ``import app.main``
    阶段崩掉（``ImportError: cannot import name 'ttk'``，exit=4）。
    下拉框那次就是这么把程序打挂的，所以在这里立一道静态护栏。
    """

    def _ttk_uses(self, path: Path) -> list[str]:
        """用 AST 找**真的**用到 ttk 的地方（字符串 / 注释里的说明不算）。"""
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "tkinter.ttk":
                        found.append(f"{node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module == "tkinter.ttk":
                    found.append(f"{node.lineno}: from tkinter.ttk import …")
                elif node.module == "tkinter":
                    for alias in node.names:
                        if alias.name == "ttk":
                            found.append(f"{node.lineno}: from tkinter import ttk")
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                    and node.value.id == "ttk":
                found.append(f"{node.lineno}: ttk.{node.attr}")
        return found

    def test_no_module_uses_tkinter_ttk(self):
        offenders = []
        for path in sorted(APP_DIR.rglob("*.py")):
            for hit in self._ttk_uses(path):
                offenders.append(f"{path.relative_to(APP_DIR.parent)}: {hit}")
        self.assertEqual(
            offenders, [],
            "冻结运行时里没有 tkinter.ttk（导入即起不来）：请改用 "
            "app.ui.widgets 的自绘控件或 tk.Entry / tk.Radiobutton")

    def test_the_settings_dialog_still_builds_its_own_controls(self):
        # 设置页是上面那次崩溃的现场：至少要确认它现在用的是纯 tk 控件
        text = (APP_DIR / "ui" / "settings_dialog.py").read_text(encoding="utf-8")
        self.assertNotIn("from tkinter import ttk", text)
        self.assertIn("tk.Radiobutton", text, "推理强度用单选按钮，不用下拉框")
        self.assertIn("model_entry", text, "模型名是可手输输入框")
        # 结果窗（保留实现）也是纯 tk 滚动条
        explain = (APP_DIR / "ui" / "explain_window.py").read_text(encoding="utf-8")
        self.assertNotIn("from tkinter import ttk", explain)
        self.assertIn("thin_scrollbar", explain)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
