"""最终交互（单按钮「解释并记录」）的回归测试 —— **纯 mock，零 GUI**。

覆盖用户本轮明确列出的每一条：

* 唯一按钮：先幂等落库 → 再异步解释 → 结果写回**同一个 entry_id**；
* 无 Key：先保存 + 显示「已记录，待解释」+ 底部可点的配置提示 / 重试入口；
  重试用同 entry_id，不重复记词、不加词频；
* 划选 / 点别处 / 外部关闭：零写库、零网络；
* 迟到结果不重开窗口、不覆盖新词；
* 按钮回调先于钩子事件被消费，也不被同一次点击误收；
* UIA 慢返回晚于外部点击 → 结果作废；
* self 窗口不拆服务、不取消置顶；游戏/全屏/游戏模式仍然是硬阻断；
* 普通窗口切换 / 同 HWND 标题（标签页）变化 → 收浮条 + 作废旧选区；
* 重新划选同一个词不被「签名冷却」吞掉；
* 浮窗：先定位后映射、可见时不重复映射、负坐标夹取、隐藏不重开。

这些用例一个 Tk 窗口都不创建：``tests.support.headless_app`` 把窗口层与
Win32 全部换成记录型假对象，``floating_probe`` 用真实 ``FloatingWindow``
代码 + 假 Tk widget 验证显示语义。绝不安装钩子、绝不启动 UIA、绝不联网。
"""
from __future__ import annotations

import inspect
import threading
import time
import unittest

from tests.support import (
    BAR_HWND, DEFAULT_HEADLESS_FG, FakeBridge, headless_app, headless_gesture,
    headless_press, ok_response, pump_app, run_gesture_worker,
)


# --------------------------------------------------------------------- 工具
def make_sel(term="alpha", context="…alpha beta…", app="msedge.exe", pid=9999, hwnd=55501):
    from app.models import CapturedSelection, SourceInfo, now_iso

    src = SourceInfo(hwnd=hwnd, pid=pid, exe=f"C:\\Program Files\\{app}", app=app,
                     title="Some Document", url="https://a.example.com/p",
                     confidence="url_document", doc_key="url:https://a.example.com/p")
    return CapturedSelection(term=term, context=context, method="ui_textpattern",
                             source=src, captured_at=now_iso())


def show_selection(app, sel, point=(10, 10)):
    """把一份内存快照事件交给 App（模拟划选完成 → 弹浮条）。"""
    app._ui_q.put(("selection_ready", {
        "selection": sel, "point": point, "kind": "drag", "reason": "",
        "generation": app.capture_service.current_generation(),
        "hwnd": sel.source.hwnd, "pid": sel.source.pid,
    }))
    pump_app(app)


def retitle_foreground(title: str) -> None:
    """改**实时**前台标题（同一个 dict 对象）。

    模拟「浏览器已经切了标签页，但 ``ForegroundWatcher`` 的 0.7s 轮询还没轮到」——
    这是 ``_dispatch_gesture`` 必须自己同步采样前台的场景。
    """
    from app import win32util as w32

    w32.foreground_info()["title"] = str(title)


def wait_for(app, predicate, timeout: float = 5.0) -> bool:
    """等后台解释线程完成，并把 UI 队列彻底消费掉（含最后一条结果事件）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pump_app(app)
        if predicate():
            # 结果先写库、再入队；这里再把队列排空，保证界面状态也已更新
            time.sleep(0.02)
            pump_app(app, times=5)
            return True
        time.sleep(0.02)
    pump_app(app)
    return bool(predicate())


def configure_key(cfg, model="model-A"):
    cfg.set("api.base_url", "https://api.example.com/v1")
    cfg.set("api.model", model)
    cfg.set_api_key("sk-test-key-not-real")


class FakeClient:
    """假模型客户端：绝不联网。

    ``topic``：解释响应里的可选主题名（默认空串 = 模型没给）。
    ``on_call``：在返回结果**之前**执行的回调（模拟「解释期间用户编辑 / 移动了词条」）。
    """

    def __init__(self, one_line="一句话解释", error=None, topic="", on_call=None):
        self.one_line = one_line
        self.error = error
        self.topic = topic
        self.on_call = on_call
        self.calls: list[tuple[str, str]] = []

    def explain(self, term, context=""):
        self.calls.append((term, context))
        if self.on_call is not None:
            self.on_call()
        if self.error is not None:
            raise self.error
        from app.models import ExplainResult

        return ExplainResult(one_line=self.one_line, detail="详细说明", examples=["例1"],
                             raw="{}", from_cache=False,
                             model_config="https://api.example.com/v1|model-A",
                             topic=self.topic)


# =================================================== 单按钮：先保存再解释
class TestUnifiedButtonSavesThenExplains(unittest.TestCase):
    def test_click_records_then_explains_same_entry(self):
        with headless_app() as app:
            configure_key(app.config)
            client = FakeClient("alpha 的释义")
            app.explain_service.make_client = lambda **kw: client
            show_selection(app, make_sel())
            self.assertTrue(app.selection_bar.visible, "划选后应弹出浮条")

            entry_id = app.selection_bar.click_action()

            self.assertIsNotNone(entry_id)
            rows = app.db.list_entries()
            self.assertEqual(len(rows), 1, "点唯一按钮必须落库一条词条")
            self.assertEqual(rows[0]["term"], "alpha")
            self.assertEqual(rows[0]["context"], "…alpha beta…")
            self.assertIn("a.example.com", rows[0]["source_url"])
            self.assertFalse(app.selection_bar.visible, "点按钮后浮条必须收起")

            self.assertTrue(wait_for(app, lambda: app.db.get_entry(entry_id)["explain_status"] == "ok"),
                            "解释结果必须写回同一个 entry_id")
            row = app.db.get_entry(entry_id)
            self.assertEqual(row["one_line"], "alpha 的释义")
            self.assertEqual(client.calls, [("alpha", "…alpha beta…")],
                             "解释请求必须用这份选区快照")
            self.assertEqual(app.explain_window.entry_id, entry_id)
            self.assertEqual(app.explain_window.status, "ok")
            self.assertGreaterEqual(app.main.refresh_entries_calls, 1,
                                    "已有 entry 的结果必须刷新卡片")

    def test_error_result_refreshes_card_with_entry_id(self):
        from app.api_client import ApiError

        with headless_app() as app:
            configure_key(app.config)
            app.explain_service.make_client = lambda **kw: FakeClient(
                error=ApiError("auth", "Key 无效"))
            show_selection(app, make_sel())
            entry_id = app.selection_bar.click_action()
            self.assertTrue(wait_for(
                app, lambda: app.db.get_entry(entry_id)["explain_status"] == "error"))
            self.assertIn("解释失败", app.main.status_label.cget("text"))
            self.assertEqual(app.explain_window.entry_id, entry_id)
            self.assertEqual(app.main.refresh_entries_calls > 0, True)

    def test_consecutive_clicks_record_only_once(self):
        with headless_app() as app:
            configure_key(app.config)
            app.explain_service.make_client = lambda **kw: FakeClient()
            sel = make_sel()
            show_selection(app, sel)
            eid1 = app.explain_and_record_selection(sel)
            eid2 = app.explain_and_record_selection(sel)
            self.assertEqual(eid1, eid2, "同一次选区的连续点击必须复用同一个 entry")
            self.assertEqual(app.db.count_entries(), 1)
            self.assertEqual(app.db.get_entry(eid1)["repeat_count"], 1,
                             "重复点击不得再加词频")

    def test_new_selection_of_same_word_bumps_repeat_count(self):
        """跨选区的重复词仍然走原有去重规则（repeat_count +1），不受幂等守卫影响。"""
        with headless_app() as app:
            configure_key(app.config)
            app.explain_service.make_client = lambda **kw: FakeClient()
            sel = make_sel()
            show_selection(app, sel)
            eid1 = app.selection_bar.click_action()
            show_selection(app, make_sel())          # 用户重新划选了同一个词
            eid2 = app.selection_bar.click_action()
            self.assertEqual(eid1, eid2, "同词同境去重后应合并为同一条词条")
            self.assertEqual(app.db.get_entry(eid1)["repeat_count"], 2)


# =========================================================== 无 Key 先保存
class TestNoKeySavesFirstAndRetryIsIdempotent(unittest.TestCase):
    def test_no_key_still_records_and_shows_pending_entry(self):
        with headless_app() as app:
            self.assertFalse(app.config.has_api_key())
            show_selection(app, make_sel())
            entry_id = app.selection_bar.click_action()

            self.assertIsNotNone(entry_id)
            self.assertEqual(app.db.count_entries(), 1, "没有 Key 也必须先保存")
            self.assertTrue(app.explain_window.visible, "必须显示待解释窗口")
            self.assertEqual(app.explain_window.status, "pending_no_key")
            self.assertEqual(app.explain_window.entry_id, entry_id)
            self.assertIn("已记录", app.explain_window.message)
            self.assertIn("待解释", app.explain_window.message)
            self.assertNotIn("重新解释", app.explain_window.message,
                             "正文不得再提已删除的按钮名")
            self.assertIn("待解释", app.main.status_label.cget("text"))

    def test_retry_uses_same_entry_without_duplicate_record(self):
        with headless_app() as app:
            show_selection(app, make_sel())
            entry_id = app.selection_bar.click_action()
            self.assertEqual(app.db.count_entries(), 1)

            # 用户去设置里配好 Key，回来点「重新解释」
            configure_key(app.config)
            client = FakeClient("补上的释义")
            app.explain_service.make_client = lambda **kw: client
            self.assertTrue(app.explain_window.click_retry())
            self.assertTrue(wait_for(
                app, lambda: app.db.get_entry(entry_id)["explain_status"] == "ok"))

            self.assertEqual(app.db.count_entries(), 1, "重试绝不重复记词")
            self.assertEqual(app.db.get_entry(entry_id)["repeat_count"], 1,
                             "重试绝不加词频")
            self.assertEqual(app.db.get_entry(entry_id)["one_line"], "补上的释义")
            self.assertEqual(client.calls, [("alpha", "…alpha beta…")])

    def test_retry_after_error_keeps_same_entry(self):
        from app.api_client import ApiError

        with headless_app() as app:
            configure_key(app.config)
            app.explain_service.make_client = lambda **kw: FakeClient(
                error=ApiError("network", "连接失败"))
            show_selection(app, make_sel())
            entry_id = app.selection_bar.click_action()
            self.assertTrue(wait_for(
                app, lambda: app.db.get_entry(entry_id)["explain_status"] == "error"))
            self.assertEqual(app.explain_window.status, "error")

            app.explain_service.make_client = lambda **kw: FakeClient("重试成功")
            self.assertTrue(app.explain_window.click_retry())
            self.assertTrue(wait_for(
                app, lambda: app.db.get_entry(entry_id)["explain_status"] == "ok"))
            self.assertEqual(app.db.count_entries(), 1)
            self.assertEqual(app.db.get_entry(entry_id)["repeat_count"], 1)

    def test_retry_without_entry_does_nothing(self):
        with headless_app() as app:
            app.explain_window.entry_id = None
            self.assertFalse(app.explain_window.click_retry())
            self.assertEqual(app.db.count_entries(), 0)


# ================================================== 划选 / 点别处：零副作用
class TestZeroWriteZeroNetwork(unittest.TestCase):
    def _forbid_network(self, app):
        calls = []

        def _no_client(**kw):  # pragma: no cover - 被调用即失败
            calls.append(kw)
            raise AssertionError("不允许发起任何网络请求")

        app.explain_service.make_client = _no_client
        return calls

    def test_selection_alone_writes_nothing(self):
        bridge = FakeBridge(ok_response(text="alpha"))
        with headless_app(bridge=bridge) as app:
            calls = self._forbid_network(app)
            headless_gesture(app)
            pump_app(app)
            self.assertEqual(app.db.count_entries(), 0, "划选不得落库")
            self.assertEqual(app.db.list_batches(), [], "划选不得建批次")
            self.assertEqual(calls, [])
            self.assertEqual(len(bridge.calls), 1, "划选只做一次 UIA 读取")

    def test_outside_click_voids_selection_without_writing(self):
        bridge = FakeBridge(ok_response(text="alpha"))
        with headless_app(bridge=bridge) as app:
            calls = self._forbid_network(app)
            show_selection(app, make_sel())
            self.assertTrue(app.selection_bar.visible)
            headless_press(app, 900, 900, overlay=False)
            pump_app(app)
            self.assertTrue(app.selection_bar.visible,
                            "点别处只作废选区，窗口由用户控制")
            self.assertIsNone(app._current_selection, "旧选区必须作废")
            self.assertEqual(app.db.count_entries(), 0, "作废选区不得保存")
            self.assertEqual(app.db.list_batches(), [])
            self.assertEqual(calls, [])

    def test_press_on_overlay_does_not_dismiss_bar(self):
        with headless_app() as app:
            show_selection(app, make_sel())
            headless_press(app, 60, 60, overlay=True)
            pump_app(app)
            self.assertTrue(app.selection_bar.visible, "点在浮条上不得收起浮条")

    def test_late_press_from_before_bar_shown_is_ignored(self):
        """队列里迟到的旧按下（早于浮条显示）不得作废刚弹出来的选区。"""
        with headless_app() as app:
            show_selection(app, make_sel())
            app._ui_q.put(("overlay_press", {
                "x": 900, "y": 900, "when": 500.0, "hwnd": 555003,
                "is_overlay": False, "generation": 1,
            }))
            pump_app(app)
            self.assertTrue(app.selection_bar.visible)
            self.assertIsNotNone(app._current_selection,
                                 "早于浮条显示的旧按下不得作废新选区")

            # 浮条显示之后的按下才允许作废（窗口本身依旧由用户控制）
            app._ui_q.put(("overlay_press", {
                "x": 900, "y": 900, "when": app._bar_shown_at + 0.001,
                "hwnd": 555003, "is_overlay": False,
                "generation": app.capture_service.current_generation(),
            }))
            pump_app(app)
            self.assertTrue(app.selection_bar.visible)
            self.assertIsNone(app._current_selection, "之后的按下作废旧选区")


# ================================================ 按钮回调 vs 钩子事件顺序
class TestButtonCallbackBeatsHookConsumption(unittest.TestCase):
    def _arm_bar(self, app):
        show_selection(app, make_sel())
        self.assertTrue(app.selection_bar.visible)

    def test_button_callback_runs_before_press_event_is_consumed(self):
        with headless_app() as app:
            self._arm_bar(app)
            headless_press(app, 60, 60, overlay=True)      # 钩子线程当场入队
            eid = app.selection_bar.click_action()          # 按钮回调先执行
            self.assertIsNotNone(eid)
            self.assertTrue(app.explain_window.visible)
            pump_app(app)                                   # 之后才消费那次按下
            self.assertTrue(app.explain_window.visible,
                            "同一次点击不得把自己的解释窗收掉")
            self.assertEqual(app.db.count_entries(), 1)
            self.assertEqual(app.selection_bar.action_calls, 1)

    def test_press_event_consumed_before_callback_still_fine(self):
        with headless_app() as app:
            self._arm_bar(app)
            headless_press(app, 60, 60, overlay=True)
            pump_app(app)
            self.assertTrue(app.selection_bar.visible, "按下元信息已判定点在浮层上")
            eid = app.selection_bar.click_action()
            self.assertIsNotNone(eid)
            self.assertTrue(app.explain_window.visible)


# =================================================== 迟到结果 / 新词覆盖
class TestLateResultDoesNotReviveOrOverwrite(unittest.TestCase):
    def test_late_result_never_reopens_closed_window(self):
        with headless_app() as app:
            configure_key(app.config)
            gate = threading.Event()

            class SlowClient(FakeClient):
                def explain(self, term, context=""):
                    gate.wait(5)
                    return super().explain(term, context)

            app.explain_service.make_client = lambda **kw: SlowClient("迟到的释义")
            show_selection(app, make_sel())
            entry_id = app.selection_bar.click_action()
            self.assertTrue(app.explain_window.visible)

            app.explain_window.click_close()            # 用户自己关掉结果窗
            self.assertFalse(app.explain_window.visible)
            gate.set()                                   # 结果这时才回来
            self.assertTrue(wait_for(
                app, lambda: app.db.get_entry(entry_id)["explain_status"] == "ok"),
                "关闭窗口不得阻止已经请求的写回")
            self.assertFalse(app.explain_window.visible, "迟到的结果绝不得重开窗口")
            self.assertEqual(app.explain_window.results_while_hidden, 1,
                             "结果确实回来了，但只在隐藏状态写内容，没有重开窗口")

    def test_late_result_does_not_overwrite_new_word(self):
        with headless_app() as app:
            configure_key(app.config)
            gate = threading.Event()

            class SlowClient(FakeClient):
                def explain(self, term, context=""):
                    gate.wait(5)
                    return super().explain(term, context)

            app.explain_service.make_client = lambda **kw: SlowClient("alpha 的释义")
            show_selection(app, make_sel(term="alpha"))
            eid_a = app.selection_bar.click_action()

            # 用户马上选了新词 → 旧窗口收起、旧选区作废
            show_selection(app, make_sel(term="beta", context="ctx B"))
            self.assertFalse(app.explain_window.visible)
            gate.set()
            self.assertTrue(wait_for(
                app, lambda: app.db.get_entry(eid_a)["explain_status"] == "ok"))
            self.assertFalse(app.explain_window.visible, "旧结果不得重开窗口")
            self.assertIsNone(app.explain_window.entry_id, "窗口归属不得被旧结果改写")
            self.assertTrue(app.selection_bar.visible, "新词的浮条必须还在")
            self.assertEqual(app.selection_bar.selection.term, "beta")

    def test_result_for_other_entry_is_refused_by_window(self):
        with headless_app() as app:
            configure_key(app.config)
            app.explain_service.make_client = lambda **kw: FakeClient()
            show_selection(app, make_sel())
            eid = app.selection_bar.click_action()
            self.assertTrue(wait_for(app, lambda: app.explain_window.status == "ok"))
            # 手动投一条别的词条的结果：窗口必须拒绝
            self.assertFalse(app.explain_window.show_result(
                int(eid) + 999, "ok", None, ""))
            self.assertEqual(app.explain_window.entry_id, eid)


# ============================================================ UIA 慢返回
class TestSlowUiaReturnAfterExternalClick(unittest.TestCase):
    def test_result_arriving_after_external_press_is_discarded(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingBridge(FakeBridge):
            def get_selection(self, *a, **kw):
                entered.set()
                release.wait(5)
                return super().get_selection(*a, **kw)

        bridge = BlockingBridge(ok_response(text="alpha"))
        with headless_app(bridge=bridge) as app:
            gen = app.capture_service.current_generation()
            worker = threading.Thread(
                target=app.capture_service.handle_gesture,
                args=(10, 10, "drag"),
                kwargs={"delay": 0, "generation": gen},
            )
            worker.start()
            self.assertTrue(entered.wait(5), "UIA 读取应已开始")
            headless_press(app, 900, 900, overlay=False)   # 用户在等待期间点了别处
            self.assertGreater(app.capture_service.current_generation(), gen)
            release.set()
            worker.join(5)
            pump_app(app)

            self.assertFalse(app.selection_bar.visible, "过期 UIA 结果不得弹浮条")
            self.assertEqual(app.db.count_entries(), 0)
            self.assertEqual(app.db.list_batches(), [])


# ====================================================== 门控：软 / 硬阻断
class TestGateSoftAndHardBlocks(unittest.TestCase):
    def test_self_window_does_not_churn_services_or_drop_topmost(self):
        from tests.support import foreground

        with headless_app() as app:
            self.assertTrue(app.effective_topmost(), "普通前台下主界面应保持置顶")
            hook, uia = app.mouse_hook, app.uia
            with foreground(title="探索词典", app="python.exe", pid=1234, hwnd=55599,
                            is_self=True):
                decision = app._apply_gate()
            self.assertTrue(decision.soft_blocked, decision.reason)
            self.assertEqual(hook.stops, 0, "self 窗口不得卸载鼠标钩子")
            self.assertEqual(hook.starts, 0, "self 窗口不得重装鼠标钩子")
            self.assertEqual(uia.stop_calls, 0, "self 窗口不得停止 UIA helper")
            self.assertTrue(uia.enabled)
            self.assertTrue(app.effective_topmost(), "self 窗口不得取消置顶")
            self.assertTrue(app.selection_bar.topmost)

    def test_game_process_hard_blocks_and_keeps_zero_uia(self):
        from tests.support import foreground

        bridge = FakeBridge(ok_response(text="secret"))
        with headless_app(bridge=bridge) as app:
            hook, uia = app.mouse_hook, app.uia
            with foreground(title="游戏", app="steam.exe", hwnd=70001, pid=7001):
                decision = app._apply_gate()
            self.assertTrue(decision.hard_blocked)
            self.assertEqual(hook.stops, 1, "硬阻断必须卸载鼠标钩子")
            self.assertFalse(uia.enabled, "硬阻断必须禁止新的 UIA 请求")
            self.assertEqual(uia.stop_calls, 1, "硬阻断必须停掉 helper")
            self.assertFalse(app.effective_topmost(), "硬阻断必须取消置顶")

            with foreground(title="游戏", app="steam.exe", hwnd=70001, pid=7001):
                sel, reason = app.capture_service.attempt_capture(10, 10, "drag")
            self.assertIsNone(sel)
            self.assertEqual(reason, "game_process")
            self.assertEqual(bridge.calls, [], "硬阻断必须零 UIA 调用")
            self.assertEqual(app.db.count_entries(), 0)

    def test_startup_in_game_does_not_go_topmost(self):
        game_fg = dict(DEFAULT_HEADLESS_FG, title="游戏", app="aces.exe",
                       exe=r"D:\war thunder\WarThunder\win64\aces.exe", hwnd=70009, pid=7009)
        with headless_app(fg=game_fg) as app:
            self.assertFalse(app.effective_topmost(), "游戏里启动不得置顶")
            self.assertFalse(app.selection_bar.topmost)
            self.assertFalse(app.explain_window.topmost)
            self.assertIn(("-topmost", False), [c for c in app.fake_root.attributes_calls])

    def test_topmost_not_relifted_when_unchanged(self):
        with headless_app() as app:
            before = len(app.fake_root.attributes_calls)
            lifts = app.fake_root.lift_calls
            app.apply_topmost()
            app.apply_topmost()
            self.assertEqual(len(app.fake_root.attributes_calls), before,
                             "置顶状态没变就不该再改 attributes")
            self.assertEqual(app.fake_root.lift_calls, lifts, "状态没变不得重复 lift")


# ================================================= 前景切换 / 重新划选
class TestForegroundSwitchInvalidatesSelection(unittest.TestCase):
    def test_other_window_switch_keeps_bar_and_voids_selection(self):
        with headless_app() as app:
            app.capture_service.note_foreground(dict(DEFAULT_HEADLESS_FG))
            show_selection(app, make_sel())
            token_before = app._selection_token
            app.capture_service.note_foreground({
                "hwnd": 66001, "pid": 4242, "title": "别的文档", "app": "firefox.exe",
                "exe": "C:\\firefox.exe", "is_self": False})
            pump_app(app)
            self.assertTrue(app.selection_bar.visible,
                            "换窗口只作废旧选区，不得自动收起窗口")
            self.assertIsNone(app._current_selection, "旧选区必须作废")
            self.assertEqual(app._selection_token, token_before + 1)
            self.assertIn("前台窗口已切换", app.main.status_label.cget("text"))
            self.assertEqual(app.main.follow_calls, 1,
                             "换页面要让主界面列表跟着当前页面走（只记待办）")

    def test_same_hwnd_title_change_voids_selection(self):
        """浏览器切标签页：HWND 不变、标题变了 —— 旧选区同样必须作废。"""
        with headless_app() as app:
            app.capture_service.note_foreground(dict(DEFAULT_HEADLESS_FG))
            show_selection(app, make_sel())
            app.capture_service.note_foreground(
                dict(DEFAULT_HEADLESS_FG, title="另一个标签页"))
            pump_app(app)
            self.assertTrue(app.selection_bar.visible,
                            "换标签页只作废选区，不得自动收起窗口")
            self.assertIsNone(app._current_selection)
            self.assertIn("标签页", app.main.status_label.cget("text"))

    def test_reselection_after_dismiss_is_not_suppressed(self):
        """作废旧选区后，用户重新划选**同一个词**必须立刻生效（没有签名冷却）。"""
        with headless_app() as app:
            sel = make_sel()
            show_selection(app, sel)
            headless_press(app, 900, 900, overlay=False)
            pump_app(app)
            self.assertIsNone(app._current_selection, "点别处只作废旧选区")

            show_selection(app, make_sel())          # 立刻重新划选同一个词
            self.assertTrue(app.selection_bar.visible, "合法重新划选不得被冷却吞掉")
            self.assertEqual(app.selection_bar.token, app._selection_token)
            self.assertIsNotNone(app._current_selection)


# ==================================== 切页世代：迟到事件 / 新页首选区（本轮修复）
class TestPageEpochGuardsNewPageFirstSelection(unittest.TestCase):
    """切换阅读页后**第一个词**不能被迟到的 ``foreground_changed`` 事件清掉。

    事件链路（产品真实时序）：

    1. ``ForegroundWatcher``（后台线程）0.7s 轮询发现前台换了 → 同步调用
       ``CaptureService.note_foreground``：**当场**把 ``generation`` 与
       ``page_epoch`` 各 +1（旧结果立刻失效），并把 ``foreground_changed``
       事件**排队**到 UI 线程；
    2. 用户在**新页面**划第一个词 → 手势排队时带的是**新**世代指纹，
       ``selection_ready`` 也排进同一个队列；
    3. UI 泵按顺序消费：先处理那条**迟到的** ``foreground_changed``。

    旧实现在第 3 步无条件作废「当前选区」，于是新页面的首选区被旧事件清掉
    （用户看到的就是「切页后划词不检测 / 面板又空了 / 点了没反应」）。
    现在按**页面世代**判定，并且世代递增**仍然在 note_foreground 里同步发生**，
    没有挪到 UI 消费时执行。
    """

    # 页面身份只靠**标题**区分（同一个进程、同一个 HWND = 浏览器里切标签页），
    # 与假前台的 hwnd / pid / app 保持一致，否则选区会被「前台身份变了」判过期
    PAGE_A = dict(DEFAULT_HEADLESS_FG, title="Page A")
    PAGE_B = dict(DEFAULT_HEADLESS_FG, title="Page B")

    def _switch_page(self, app, page, prior=None):
        """让服务认定前台切到 ``page``；返回被**排队**的事件负载（不消费，模拟迟到）。"""
        payloads: list[dict] = []
        original = app.capture_service._on_foreground_change

        def _queue(cb, src, kind, meta):
            app._ui_q.put(("foreground_changed",
                           {"source": src, "kind": kind,
                            "page_epoch": (meta or {}).get("page_epoch"),
                            "generation": (meta or {}).get("generation")}))

        def _spy(src, kind, meta=None):
            payloads.append({"source": src, "kind": kind, "meta": dict(meta or {})})
            _queue(original, src, kind, meta)

        if prior is not None:
            app.capture_service.note_foreground(prior)
        app.capture_service._on_foreground_change = _spy
        try:
            app.capture_service.note_foreground(page)
        finally:
            app.capture_service._on_foreground_change = original
        return payloads[0] if payloads else {}

    def _press(self, app):
        """模拟一次全局鼠标按下：这是产品里划词的起点（会推高 ``generation``）。"""
        from unittest import mock

        with mock.patch("app.capture_service.w32.window_root_from_point",
                        return_value=int(DEFAULT_HEADLESS_FG["hwnd"])):
            meta = app.capture_service.handle_mouse_press(
                20, 20, time.monotonic(), overlay_hwnds=app.overlay_hwnds())
        app._ui_q.put(("overlay_press", meta))
        return meta

    def test_foreground_change_bumps_generation_and_page_epoch_synchronously(self):
        """世代递增必须在 ``note_foreground`` 里**当场**发生（不准挪到 UI 消费）。"""
        with headless_app(fg=self.PAGE_A) as app:
            svc = app.capture_service
            app.capture_service.note_foreground(self.PAGE_A)
            gen, epoch = svc.current_generation(), svc.current_page_epoch()
            app.capture_service.note_foreground(self.PAGE_B)
            # 一次调用之后（**还没消费任何 UI 事件**）指纹就必须已经变了
            self.assertGreater(svc.current_generation(), gen,
                               "前台一变，旧结果必须当场失效")
            self.assertGreater(svc.current_page_epoch(), epoch,
                               "页面世代同样要在检测到切换时就 +1")

    def test_late_foreground_change_keeps_the_new_page_first_selection(self):
        bridge = FakeBridge(ok_response(text="新页首词", context="…新页首词…"))
        with headless_app(fg=self.PAGE_A, bridge=bridge) as app:
            app.capture_service.note_foreground(self.PAGE_A)
            payload = self._switch_page(app, self.PAGE_B, prior=self.PAGE_A)
            self.assertTrue(payload, "切换必须产生一条延迟事件")
            # 真实前台此刻确实已经在新页面上（只是那条事件还没被消费）
            retitle_foreground("Page B")
            token_after_switch = app._selection_token

            # 用户已经在新页面上划好了第一个词（走真实的入队 → 工作线程链路）
            self._press(app)
            app._on_gesture(20, 20, "drag")
            run_gesture_worker(app)
            pump_app(app)                       # 迟到事件与 selection_ready 一起被消费

            self.assertIsNotNone(app._current_selection,
                                 "迟到的前台变化事件不得清新页面首选区")
            self.assertEqual(app._current_selection.term, "新页首词")
            self.assertEqual(app._selection_page_epoch,
                             app.capture_service.current_page_epoch(),
                             "选区必须记在新页面的世代上")
            self.assertTrue(app.selection_bar.visible, "面板必须仍然显示新页面选区")
            self.assertEqual(app._selection_token, token_after_switch + 2,
                             "只允许「迟到事件空作废一次 + 新选区自身一次」："
                             "新选区装好之后不得再被任何事件推高序号")
            self.assertNotIn("已作废", str(app.main.status_label.cget("text")))

    def test_late_foreground_change_still_voids_the_old_page_selection(self):
        """迟到的作废语义不能因为上面那条修复而失效：旧页选区照旧作废。"""
        with headless_app(fg=self.PAGE_A) as app:
            app.capture_service.note_foreground(self.PAGE_A)
            show_selection(app, make_sel(term="旧页词"))
            token_before = app._selection_token

            self._switch_page(app, self.PAGE_B)
            self.assertIsNotNone(app._current_selection, "事件还没消费，选区先留着")
            pump_app(app)

            self.assertIsNone(app._current_selection, "旧页面的选区必须被作废")
            self.assertIsNone(app._selection_page_epoch)
            self.assertEqual(app._selection_token, token_before + 1)
            self.assertIn("已作废", str(app.main.status_label.cget("text")))

    def test_page_change_during_uia_call_discards_the_result(self):
        """UIA 调用期间前台换页 → 结果算旧页面，丢弃（``page_changed``）。"""
        entered = threading.Event()
        release = threading.Event()

        class BlockingBridge(FakeBridge):
            def get_selection(self, *a, **kw):
                entered.set()
                release.wait(5)
                return super().get_selection(*a, **kw)

        bridge = BlockingBridge(ok_response(text="旧结果"))
        with headless_app(fg=self.PAGE_A, bridge=bridge) as app:
            app.capture_service.note_foreground(self.PAGE_A)
            self._press(app)
            app._on_gesture(20, 20, "drag")
            worker = threading.Thread(target=run_gesture_worker, args=(app,))
            worker.start()
            self.assertTrue(entered.wait(5), "UIA 读取应已开始")

            app.capture_service.note_foreground(self.PAGE_B)   # 用户在读取期间切页
            release.set()
            worker.join(5)
            pump_app(app)

            self.assertIsNone(app._current_selection, "换页后返回的结果不得显示")
            self.assertEqual(app.capture_service.last_failure_reason, "page_changed")
            self.assertEqual(app.db.count_entries(), 0)
            self.assertEqual(app.db.list_batches(), [])

    def test_external_press_does_not_change_the_page_epoch(self):
        """外部按下只推高 ``generation``：新页面的第一个词仍属于当前页面。"""
        with headless_app(fg=self.PAGE_B) as app:
            app.capture_service.note_foreground(self.PAGE_B)
            epoch = app.capture_service.current_page_epoch()
            gen = app.capture_service.current_generation()
            headless_press(app, 900, 900, overlay=False)
            self.assertGreater(app.capture_service.current_generation(), gen)
            self.assertEqual(app.capture_service.current_page_epoch(), epoch,
                             "点别处不是换页面，页面世代不准动")

    def test_same_epoch_late_foreground_event_keeps_the_new_selection(self):
        """**同代乱序**：事件与新选区属于同一个 ``page_epoch``，事件后到也不得清它。

        旧实现的 keep 条件多要求 ``epoch < current_epoch``，于是「新选区先落、
        同代的前台事件后到」时照样把选区清掉（面板空了、点了没反应）。
        """
        from unittest import mock

        with headless_app(fg=self.PAGE_B) as app:
            app.capture_service.note_foreground(self.PAGE_A)
            with mock.patch.object(app.capture_service, "_on_foreground_change",
                                   lambda *a, **k: None):
                # 切到 B，但事件**不排队**：模拟「同代事件后到」
                app.capture_service.note_foreground(self.PAGE_B)
            epoch = app.capture_service.current_page_epoch()
            generation = app.capture_service.current_generation()
            source = app.capture_service.last_source()

            sel = make_sel(term="同代首词")
            app._ui_q.put(("selection_ready", {
                "selection": sel, "point": (10, 10), "kind": "drag", "reason": "",
                "generation": generation, "page_epoch": epoch,
                "hwnd": sel.source.hwnd, "pid": sel.source.pid}))
            pump_app(app)
            self.assertIsNotNone(app._current_selection, "新页面选区先被消费")
            self.assertEqual(app._selection_page_epoch, epoch)
            token = app._selection_token

            app._ui_q.put(("foreground_changed", {
                "source": source, "kind": "window", "page_epoch": epoch,
                "generation": generation}))
            pump_app(app)

            self.assertIsNotNone(app._current_selection,
                                 "同代迟到的前台事件不得清新页面选区")
            self.assertEqual(app._current_selection.term, "同代首词")
            self.assertEqual(app._selection_page_epoch, epoch, "世代标记不得被清掉")
            self.assertEqual(app._selection_token, token, "保留选区时不得推高选区序号")
            self.assertNotIn("已作废", str(app.main.status_label.cget("text")))

    def test_first_gesture_on_a_new_page_is_not_lost_before_the_watcher_ticks(self):
        """watcher（0.7s）还没轮询到，新页面上的第一个词也必须算新页面。

        ``_dispatch_gesture`` 在入队前**同步**采一次前台：否则手势带的是旧页面的
        世代指纹，结果会被 page_changed 丢掉（用户看到「切页后第一个词划了没反应」）。
        """
        bridge = FakeBridge(ok_response(text="新页首词", context="…新页首词…"))
        with headless_app(fg=self.PAGE_A, bridge=bridge) as app:
            app.capture_service.note_foreground(self.PAGE_A)
            epoch_before = app.capture_service.current_page_epoch()
            # 真实前台已经换了页（同 HWND、标题变了），但 watcher 还没轮到它
            retitle_foreground("Page B")

            app._on_gesture(20, 20, "drag")

            self.assertGreater(app.capture_service.current_page_epoch(), epoch_before,
                               "入队前必须同步发现换页：页面世代当场 +1")
            run_gesture_worker(app)
            pump_app(app)
            self.assertEqual(app.capture_service.last_failure_reason, "")
            self.assertIsNotNone(app._current_selection, "新页面第一个词不得被丢掉")
            self.assertEqual(app._current_selection.term, "新页首词")
            self.assertEqual(app._selection_page_epoch,
                             app.capture_service.current_page_epoch(),
                             "选区必须记在新页面的世代上")

    def test_title_change_during_uia_call_discards_the_result(self):
        """UIA 调用期间同 HWND 换标题（切标签页）→ 旧结果必须丢弃。

        这条路径上 ``page_epoch`` 可能还没被 watcher 推高，只能靠「读取前后
        标题不一致」判定。
        """
        entered = threading.Event()
        release = threading.Event()

        class TitleSwitchingBridge(FakeBridge):
            def get_selection(self, *a, **kw):
                entered.set()
                release.wait(5)
                return super().get_selection(*a, **kw)

        bridge = TitleSwitchingBridge(ok_response(text="旧页结果"))
        with headless_app(fg=self.PAGE_A, bridge=bridge) as app:
            app.capture_service.note_foreground(self.PAGE_A)
            app._on_gesture(20, 20, "drag")
            worker = threading.Thread(target=run_gesture_worker, args=(app,))
            worker.start()
            self.assertTrue(entered.wait(5), "UIA 读取应已开始")

            # 同 HWND、同 PID，只有标题变了；watcher 还没轮询到（epoch 也没变）
            retitle_foreground("Page B")
            release.set()
            worker.join(5)
            pump_app(app)

            self.assertIsNone(app._current_selection, "换标签页后返回的旧结果不得显示")
            self.assertEqual(app.capture_service.last_failure_reason,
                             "foreground_title_changed")
            self.assertEqual(app.db.count_entries(), 0)


# ==================================================== 浮窗显示语义（真实类）
class TestFloatingWindowDiscipline(unittest.TestCase):
    def test_first_show_positions_before_mapping(self):
        from tests.support import floating_probe

        probe = floating_probe()
        try:
            probe.win.show_at((200, 150), "t1", text="正在解释…")
            events = probe.events()
            self.assertIn("deiconify", events)
            first_geo = next(i for i, e in enumerate(events) if e.startswith("geometry:"))
            first_map = events.index("deiconify")
            self.assertLess(first_geo, first_map, "必须先定位再映射（否则会闪一帧）")
            self.assertLess(events.index("update_idletasks"), first_map)
        finally:
            probe.close()

    def test_repeated_show_does_not_remap(self):
        from tests.support import floating_probe

        probe = floating_probe()
        try:
            probe.win.show_at((200, 150), "t1", text="词")
            maps = probe.deiconify_count()
            geos = len(probe.widget.geometry_specs)
            self.assertTrue(probe.win.show_at((200, 150), "t1"))
            self.assertEqual(probe.deiconify_count(), maps, "已可见时不得重复映射")
            self.assertEqual(len(probe.widget.geometry_specs), geos, "位置没变不得重复移动")
            self.assertEqual(probe.show_calls(), 1)
        finally:
            probe.close()

    def test_hide_is_idempotent(self):
        from tests.support import floating_probe

        probe = floating_probe()
        try:
            probe.win.show_at((200, 150), "t1", text="词")
            before = probe.events().count("withdraw")
            self.assertTrue(probe.win.hide())
            self.assertFalse(probe.win.hide())
            self.assertEqual(probe.events().count("withdraw"), before + 1,
                             "已隐藏时不得再调用 withdraw")
        finally:
            probe.close()

    def test_hidden_content_uses_signed_geometry_on_negative_monitor(self):
        from tests.support import floating_probe
        from app.ui.floating import format_geometry, tk_position

        self.assertEqual(format_geometry(-1920, 0), "+-1920+0")
        self.assertEqual(format_geometry(10, -5), "+10+-5")
        probe = floating_probe(width=220, height=70, work_area=(-1920, 0, 0, 1080))
        try:
            probe.win.prepare_hidden((100, 100), "t1", text="词")
            spec = probe.widget.geometry_specs[-1]
            self.assertEqual(spec, "+-222+100",
                             "负坐标显示器上的夹取必须给出带符号的 geometry")
            # 语义校验（不只是「带不带负号」）：解析出来的模式必须是绝对坐标
            xm, x, ym, y = tk_position(spec)
            self.assertEqual((xm, ym), ("+", "+"), "绝不能用 Tk 的右/下边缘模式")
            self.assertEqual((x, y), (-222, 100))
            self.assertEqual(probe.deiconify_count(), 0, "prepare_hidden 不得映射窗口")
        finally:
            probe.close()

    def test_empty_render_does_not_erase_loading_hint(self):
        from tests.support import floating_probe

        probe = floating_probe()
        try:
            probe.win._render("正在解释…")
            probe.win.prepare_hidden((200, 150), "t1")
            probe.win.show_at((200, 150), "t1")           # text=None：不得重画
            self.assertEqual(probe.rendered, ["正在解释…"])
            probe.win.show_at((200, 150), "t1")
            self.assertEqual(probe.rendered, ["正在解释…"], "重复显示不得清空提示")
        finally:
            probe.close()

    def test_show_result_never_reopens_hidden_window(self):
        from tests.support import floating_probe

        probe = floating_probe()
        try:
            probe.win.show_at((200, 150), "t1", text="词")
            probe.win.hide()
            maps = probe.deiconify_count()
            self.assertFalse(probe.win.show_at((200, 150), "t1", text="结果",
                                               reopen=False))
            self.assertEqual(probe.deiconify_count(), maps, "隐藏后不得被结果重开")
            self.assertEqual(probe.rendered[-1], "结果", "内容仍然可以写进去")
        finally:
            probe.close()

    def test_close_by_user_then_hidden_result_does_not_reopen(self):
        from tests.support import floating_probe

        probe = floating_probe()
        try:
            probe.win.show_at((200, 150), "t1", text="词")
            probe.win.close_by_user()
            maps = probe.deiconify_count()
            self.assertFalse(probe.win.show_at((200, 150), "t1", text="结果",
                                               reopen=False))
            self.assertEqual(probe.deiconify_count(), maps)
        finally:
            probe.close()


# ================================================ A. geometry = 绝对坐标
class TestGeometrySemantics(unittest.TestCase):
    """负坐标必须被解释成**虚拟屏幕绝对坐标**，不是「距右/下边缘的偏移」。

    只断言「字符串里带负号」是不够的：``-100+20`` 也带负号，但 Tk 会把它
    解释成「距右边缘 100 像素」。这里按 Tk 文档语义**解析**位置串，验证它
    真正落到哪个绝对坐标上。
    """

    @staticmethod
    def _resolve(spec: str, screen_w: int = 1920, screen_h: int = 1080) -> tuple[int, int]:
        """按 Tk 语义解析位置串 → 绝对坐标（mode '-' 表示距右/下边缘）。"""
        from app.ui.floating import tk_position

        xm, x, ym, y = tk_position(spec)
        abs_x = screen_w - abs(x) if xm == "-" else x
        abs_y = screen_h - abs(y) if ym == "-" else y
        return abs_x, abs_y

    def test_negative_x_resolves_to_absolute_coordinate(self):
        from app.ui.floating import format_geometry

        spec = format_geometry(-100, 20)
        self.assertEqual(spec, "+-100+20")
        self.assertEqual(self._resolve(spec), (-100, 20), "必须落在左侧显示器上")
        # 反例：边缘模式写法会跑到主屏右侧 —— 这正是本轮修掉的缺陷
        self.assertEqual(self._resolve("-100+20"), (1920 - 100, 20))
        self.assertNotEqual(self._resolve("-100+20"), (-100, 20))

    def test_negative_y_resolves_to_absolute_coordinate(self):
        from app.ui.floating import format_geometry

        spec = format_geometry(30, -40)
        self.assertEqual(spec, "+30+-40")
        self.assertEqual(self._resolve(spec), (30, -40), "必须落在上方显示器上")
        self.assertEqual(self._resolve("+30-40"), (30, 1080 - 40))
        self.assertNotEqual(self._resolve("+30-40"), (30, -40))

    def test_positive_coordinates_are_absolute_too(self):
        from app.ui.floating import format_geometry, tk_position

        spec = format_geometry(640, 480)
        self.assertEqual(spec, "+640+480")
        self.assertEqual(tk_position(spec), ("+", 640, "+", 480))
        self.assertEqual(self._resolve(spec), (640, 480))

    def test_never_uses_right_bottom_edge_mode(self):
        from app.ui.floating import format_geometry, tk_position

        for x, y in ((-1920, 0), (0, -1080), (-100, -20), (100, 100)):
            xm, _x, ym, _y = tk_position(format_geometry(x, y))
            self.assertEqual((xm, ym), ("+", "+"),
                             f"({x},{y}) 不得使用 Tk 的右/下边缘偏移模式")


# ============================================ B. 统一显示门控（实时前台）
class TestOverlayDisplayGate(unittest.TestCase):
    """所有浮层入口都必须经过 ``show_at`` 的实时门控：拒绝时只隐藏、零 deiconify。"""

    def test_show_at_denied_never_maps(self):
        from tests.support import DisplayGateStub, floating_probe

        probe = floating_probe(display_gate=DisplayGateStub("hard"))
        try:
            self.assertFalse(probe.win.show_at((200, 150), "t1", text="词"))
            self.assertEqual(probe.deiconify_count(), 0, "被拒绝时一次都不许映射")
            self.assertFalse(probe.win.visible)
        finally:
            probe.close()

    def test_visible_window_is_hidden_when_gate_flips_to_hard(self):
        from tests.support import DisplayGateStub, floating_probe

        gate = DisplayGateStub("allow")
        probe = floating_probe(display_gate=gate)
        try:
            self.assertTrue(probe.win.show_at((200, 150), "t1", text="词"))
            self.assertTrue(probe.win.visible)
            maps = probe.deiconify_count()
            gate.mode = "hard"                     # 用户切进全屏游戏
            self.assertFalse(probe.win.show_at((200, 150), "t1"))
            self.assertFalse(probe.win.visible, "硬阻断下已显示的浮层必须收起")
            self.assertEqual(probe.deiconify_count(), maps, "收起过程中零 deiconify")
        finally:
            probe.close()

    def test_explain_window_late_result_under_hard_gate_is_not_mapped(self):
        from tests.support import ExplainWindowProbe, make_explain_result

        probe = ExplainWindowProbe()
        try:
            self.assertTrue(probe.win.show_recorded(None, 5, request_token=1, status="pending"))
            self.assertTrue(probe.visible)
            maps = probe.deiconify_count()
            probe.gate.mode = "hard"               # 结果回来前用户已经进了游戏
            self.assertFalse(probe.win.show_result(
                5, "ok", make_explain_result("迟到的释义"), "", request_token=1))
            self.assertFalse(probe.visible, "游戏前台下必须立即收起结果窗")
            self.assertEqual(probe.deiconify_count(), maps, "拒绝时零 deiconify")
            self.assertIn("withdraw", probe.env.toplevel().events)
        finally:
            probe.close()

    def test_soft_gate_keeps_visible_result_window_but_blocks_new_mapping(self):
        from tests.support import ExplainWindowProbe, make_explain_result

        probe = ExplainWindowProbe()
        try:
            self.assertTrue(probe.win.show_recorded(None, 5, request_token=1, status="pending"))
            probe.gate.mode = "soft"               # 用户点开设置 → 前台变成 self
            self.assertTrue(probe.win.show_result(
                5, "ok", make_explain_result("设置期间回来的释义"), "", request_token=1),
                "已经打开的结果窗在 self 前台下必须保留")
            self.assertTrue(probe.visible)
            probe.win.hide()
            self.assertFalse(probe.win.show_recorded(None, 6, request_token=2, status="pending"),
                             "软受限下不得**新**映射结果窗")
            self.assertEqual(probe.deiconify_count(), 1)
        finally:
            probe.close()

    def test_selection_bar_never_shows_under_soft_or_hard_gate(self):
        from tests.support import SelectionBarProbe

        for mode in ("soft", "hard"):
            probe = SelectionBarProbe(gate_mode=mode)
            try:
                self.assertFalse(probe.bar.show_selection(make_sel(), 1, (10, 10)), mode)
                self.assertFalse(probe.visible)
                self.assertEqual(probe.env.deiconify_count(), 0)
            finally:
                probe.close()

    def test_hard_block_stops_display_but_requested_write_still_lands(self):
        from tests.support import foreground

        with headless_app() as app:
            configure_key(app.config)
            app.explain_service.make_client = lambda **kw: FakeClient("游戏里的释义")
            show_selection(app, make_sel())
            eid = app.selection_bar.click_action()
            self.assertTrue(app.explain_window.visible)

            with foreground(title="某游戏", app="steam.exe", hwnd=70001, pid=7001):
                self.assertFalse(app.overlay_allowed(), "游戏前台不允许任何浮层")
                decision = app._apply_gate(force=True)
                self.assertTrue(decision.hard_blocked)
            self.assertFalse(app.explain_window.visible, "硬阻断必须收起结果窗")

            self.assertTrue(wait_for(
                app, lambda: app.db.get_entry(eid)["explain_status"] == "ok"),
                "门控只阻止显示，用户已经请求的写库不受影响")
            self.assertFalse(app.explain_window.visible, "结果不得把窗口在游戏后重开")


# =================================== C. 结果按 entry + 请求 token 双重归属
class TestExplainResultRequestToken(unittest.TestCase):
    def test_real_window_rejects_stale_request_token(self):
        from tests.support import ExplainWindowProbe, make_explain_result

        probe = ExplainWindowProbe()
        try:
            self.assertTrue(probe.win.show_recorded(None, 5, request_token=7, status="pending"))
            before = probe.body()
            self.assertFalse(probe.win.show_result(
                5, "ok", make_explain_result("旧请求的释义"), "", request_token=6),
                "同 entry 但旧请求 token 的结果必须被拒绝")
            self.assertEqual(probe.win.status, "pending", "新请求的状态不得被旧结果覆盖")
            self.assertEqual(probe.body(), before, "窗口内容不得被旧结果改写")
            self.assertTrue(probe.win.show_result(
                5, "ok", make_explain_result("当前请求的释义"), "", request_token=7))
            self.assertIn("当前请求的释义", probe.body())
        finally:
            probe.close()

    def test_real_window_rejects_other_entry_even_with_matching_token(self):
        from tests.support import ExplainWindowProbe, make_explain_result

        probe = ExplainWindowProbe()
        try:
            self.assertTrue(probe.win.show_recorded(None, 5, request_token=7, status="pending"))
            self.assertFalse(probe.win.show_result(
                6, "ok", make_explain_result("别的词条"), "", request_token=7))
            self.assertEqual(probe.win.entry_id, 5)
            self.assertEqual(probe.win.status, "pending")
        finally:
            probe.close()

    def test_real_window_still_accepts_legacy_result_without_token(self):
        from tests.support import ExplainWindowProbe, make_explain_result

        probe = ExplainWindowProbe()
        try:
            self.assertTrue(probe.win.show_recorded(None, 5, request_token=7, status="pending"))
            self.assertTrue(probe.win.show_result(5, "ok", make_explain_result("旧回调"), ""),
                            "没有 token 信息的旧回调保持「只看 entry」的历史语义")
        finally:
            probe.close()

    def test_old_result_queued_before_retry_cannot_overwrite_new_request(self):
        """同 entry：旧结果晚于「重新解释」被消费 → 刷新卡片，但不改窗口状态。"""
        gate = threading.Event()

        class SlowClient(FakeClient):
            def explain(self, term, context=""):
                gate.wait(5)
                return super().explain(term, context)

        with headless_app() as app:
            configure_key(app.config)
            app.explain_service.make_client = lambda **kw: FakeClient("第一次的释义")
            show_selection(app, make_sel())
            eid = app.selection_bar.click_action()
            old_token = app.explain_window.request_token
            self.assertIsNotNone(old_token)

            # 旧请求在后台完成：先写库、再把结果事件排队（UI 线程还没消费）
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and \
                    app.db.get_entry(eid)["explain_status"] != "ok":
                time.sleep(0.02)
            self.assertEqual(app.db.get_entry(eid)["explain_status"], "ok")
            time.sleep(0.05)

            # 用户在旧结果被消费之前点了「重新解释」→ 新的请求 token（第二次请求阻塞）
            app.explain_service.make_client = lambda **kw: SlowClient("第二次的释义")
            started = False
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not started:
                started = app.retry_explain_entry(eid)
                if not started:
                    time.sleep(0.02)
            self.assertTrue(started, "旧请求结束后必须能发起重试")
            new_token = app.explain_window.request_token
            self.assertNotEqual(new_token, old_token, "重试必须生成新的请求 token")
            self.assertEqual(app.explain_window.status, "pending")

            refreshes = app.main.refresh_entries_calls
            pump_app(app, times=3)          # 这时才消费排队的**旧**结果

            self.assertGreater(app.main.refresh_entries_calls, refreshes,
                               "旧结果仍必须按 entry_id 刷新卡片")
            self.assertEqual(app.explain_window.request_token, new_token)
            self.assertEqual(app.explain_window.status, "pending",
                             "旧请求的结果不得覆盖新请求的窗口状态")
            self.assertIsNone(app.explain_window.result)
            self.assertEqual(app.db.get_entry(eid)["one_line"], "第一次的释义")

            gate.set()                      # 新请求完成：token 匹配 → 正常写回窗口
            self.assertTrue(wait_for(
                app, lambda: app.explain_window.status == "ok"))
            self.assertIn("第二次的释义", app.explain_window.result.one_line)


# ================================ D. 配 Key 往返：结果窗不消失 + 明确恢复
class TestPendingWindowSurvivesSettingsRoundTrip(unittest.TestCase):
    def test_self_foreground_soft_block_keeps_windows(self):
        """self 前台（本程序窗口）：**保留**已打开的窗口与待处理词。

        软受限只是一条「不要在这里新映射浮层」的规则（实时门控执行），
        没有权力替用户把已经打开的选区条 / 结果窗收起来。
        """
        from tests.support import foreground

        with headless_app() as app:
            show_selection(app, make_sel())
            self.assertTrue(app.selection_bar.visible)
            app.explain_window.show_recorded(make_sel(), 1, status="pending_no_key",
                                             message="没有 Key，点「打开设置」")
            self.assertTrue(app.explain_window.visible)

            with foreground(title="设置 — 探索词典", app="python.exe", pid=1234,
                            hwnd=55599, is_self=True):
                decision = app._apply_gate(force=True)

            self.assertTrue(decision.soft_blocked, decision.reason)
            self.assertTrue(app.selection_bar.visible,
                            "self 前台不得收起用户已经打开的选区条")
            self.assertIsNotNone(app.selection_bar.selection, "待处理词必须保留")
            self.assertTrue(app.explain_window.visible,
                            "用户打开的结果窗必须保留，否则「重新解释」入口会消失")

    def test_restore_brings_back_same_entry_without_network(self):
        with headless_app() as app:
            show_selection(app, make_sel())
            eid = app.selection_bar.click_action()
            self.assertEqual(app.explain_window.status, "pending_no_key")
            token = app.explain_window.request_token

            app._hide_overlays()            # 窗口被前台切换收起（不是用户关闭）
            self.assertFalse(app.explain_window.visible)

            client_calls: list = []
            app.explain_service.make_client = lambda **kw: client_calls.append(kw)
            self.assertTrue(app.restore_pending_explain_window())
            self.assertTrue(app.explain_window.visible, "明确入口必须能找回窗口")
            self.assertEqual(app.explain_window.entry_id, eid)
            self.assertEqual(app.explain_window.request_token, token,
                             "恢复的是同一条词条、同一个请求 token")
            self.assertEqual(app.db.count_entries(), 1, "恢复窗口绝不重复记词")
            self.assertEqual(client_calls, [], "恢复窗口绝不联网")

    def test_restore_refuses_after_user_closed(self):
        with headless_app() as app:
            show_selection(app, make_sel())
            app.selection_bar.click_action()
            app.explain_window.click_close()
            self.assertTrue(app.explain_window.closed_by_user)
            self.assertFalse(app.restore_pending_explain_window())
            self.assertFalse(app.explain_window.visible, "用户关掉的窗口不得被找回")

    def test_real_window_restore_under_soft_gate_is_explicit(self):
        from tests.support import ExplainWindowProbe

        probe = ExplainWindowProbe(gate_mode="soft")
        try:
            self.assertFalse(probe.win.show_recorded(
                None, 9, status="pending_no_key", message="没有 Key"),
                "软受限下不得自动新映射结果窗")
            self.assertEqual(probe.deiconify_count(), 0)
            self.assertTrue(probe.win.restore(), "显式恢复入口允许在软受限下显示")
            self.assertEqual(probe.deiconify_count(), 1)
            self.assertEqual(probe.win.entry_id, 9)
            self.assertIn("没有 Key", probe.body())
        finally:
            probe.close()

    def test_real_window_restore_refused_after_user_close(self):
        from tests.support import ExplainWindowProbe

        probe = ExplainWindowProbe()
        try:
            self.assertTrue(probe.win.show_recorded(None, 9, status="pending_no_key",
                                                    message="没有 Key"))
            probe.win.close_by_user()
            maps = probe.deiconify_count()
            self.assertFalse(probe.win.restore())
            self.assertEqual(probe.deiconify_count(), maps, "找回动作不得重开用户关掉的窗口")
        finally:
            probe.close()

    def test_selection_bar_repeated_callbacks_act_once(self):
        from tests.support import SelectionBarProbe

        probe = SelectionBarProbe()
        try:
            self.assertTrue(probe.bar.show_selection(make_sel(), 1, (10, 10)))
            probe.bar._on_action()
            probe.bar._on_action()          # 连续重复回调（双击 / 重复派发）
            probe.bar._on_action()
            self.assertEqual(len(probe.actions), 1, "同一次选区只允许操作一次")
        finally:
            probe.close()

    def test_selection_bar_action_ignored_after_hidden(self):
        from tests.support import SelectionBarProbe

        probe = SelectionBarProbe()
        try:
            self.assertTrue(probe.bar.show_selection(make_sel(), 1, (10, 10)))
            probe.bar.hide()                # 门控 / 点别处已经收起浮条
            probe.bar._on_action()          # 迟到的按钮回调
            self.assertEqual(probe.actions, [], "已收起的浮条不得再触发记录与解释")
        finally:
            probe.close()

    def test_selection_bar_new_selection_can_act_again(self):
        from tests.support import SelectionBarProbe

        probe = SelectionBarProbe()
        try:
            self.assertTrue(probe.bar.show_selection(make_sel(), 1, (10, 10)))
            probe.bar._on_action()
            probe.bar.hide()
            self.assertTrue(probe.bar.show_selection(make_sel(term="beta"), 2, (20, 20)))
            probe.bar._on_action()
            self.assertEqual(len(probe.actions), 2, "新选区必须能再次操作")
        finally:
            probe.close()


# ============================================================ 静态契约
class TestSingleButtonContract(unittest.TestCase):
    def test_selection_bar_has_only_one_action(self):
        from app.ui.selection_bar import ACTION_TEXT, SelectionBar

        self.assertEqual(ACTION_TEXT, "解释并记录")
        src = inspect.getsource(SelectionBar)
        self.assertNotIn("btn_record", src)
        self.assertNotIn("_on_record", src)

    def test_explain_window_has_no_record_action(self):
        from app.ui.explain_window import ExplainWindow

        src = inspect.getsource(ExplainWindow)
        self.assertNotIn("btn_record", src)
        self.assertNotIn("_on_record", src)
        self.assertFalse(hasattr(ExplainWindow, "_on_record"))
        self.assertEqual(src.count("self._button(btns"), 3,
                         "解释窗只应有 重新解释 / 打开设置 / 关闭 三个动作")
        for text in ("重新解释", "打开设置", "关闭"):
            self.assertIn(text, src)

    def test_app_exposes_unified_action_only(self):
        from app.main import App

        self.assertTrue(hasattr(App, "explain_and_record_selection"))
        self.assertTrue(hasattr(App, "retry_explain_entry"))
        self.assertFalse(hasattr(App, "record_selection"),
                         "不应再有独立的「记录」入口")
        self.assertFalse(hasattr(App, "explain_selection"))

    def test_no_undefined_grace_constants_left(self):
        import app.main as app_main

        for name in ("OVERLAY_CLICK_GRACE_SECONDS", "REVEAL_SUPPRESS_SECONDS",
                     "CLICK_EVENT_MAX_AGE", "EXPLAIN_TOKEN_VOID"):
            self.assertFalse(hasattr(app_main, name),
                             f"{name} 已废弃，不应再出现（避免未定义常量）")


if __name__ == "__main__":
    unittest.main()
