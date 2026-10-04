"""阅读面板（Dock + 展开浮窗）回归测试 —— **纯 mock，零真实窗口**。

覆盖用户本轮明确列出的每一条：

* 折叠 / 展开状态机（小方块 ↔ 面板 ↔ 完全隐藏、显式点击、用户关闭后可找回）；
* 外部点击折叠回小方块、点面板内部（含详情输入）**不**误折叠；
* 划选自动展开且**零写库零联网**；
* 唯一按钮「解释并记录」（同一次选区只操作一次）；
* 标签**本地读取**（点标签进详情不联网、长标签省略 + 完整词）；
* 拖动 / 改尺寸持久化 + 副屏（负坐标）与显示器变化夹回可见范围；
* 门控：硬阻断隐藏、软受限不新映射、迟到结果不重新展开；
* 追问输入框的**显式激活**路径（只有用户点了输入框才允许抢键盘）；
* 删除后的**右栏复位**：删单条 / 删整个主题 / 选到已删词条都必须把详情右栏整块
  清干净（词条还在库里时——搜索、手工浏览——一律不许清空）。

测试一个真实 Tk 窗口都不创建：``tests.support.PanelProbe`` 用真实
``ReadingPanel`` 代码 + 假 Tk 控件 + 假 Win32；``headless_app`` 连窗口层都换掉。
绝不安装钩子、绝不启动 UIA、绝不联网、绝不读取密钥。
"""
from __future__ import annotations

import contextlib
import json
import time
import unittest
from unittest import mock

from tests.support import (
    DEFAULT_HEADLESS_FG, FakePanelConfig, FakeWidget, PanelProbe, _FakeTkEnv,
    headless_app, make_explain_result, pump_app, temp_db,
)
from tests.test_unified_action import make_sel, show_selection

from app.ui import panel_geometry as geo
from app.ui import theme, widgets
from app.ui.main_window import EMPTY_DETAIL_HINT
from app.ui.reading_panel import (CLOSE_TEXT, EXPLAIN_TEXT, MODE_DOCK, MODE_HIDDEN,
                                  MODE_PANEL, NO_KEY_HINT, PAGE_DETAIL, PAGE_LIST,
                                  QUOTE_LINES, RETRY_TEXT, clip_tag)


def term_budget(panel) -> int:
    """面板**当前宽度**下「词语最多两行」的显示预算（半角单位）。

    与 ``ReadingPanel._render_tags`` 同源（同一个纯函数 + 同一批常量），所以
    「放得下两行 → 完整显示 / 放不下 → 省略」这条契约在**任何**面板宽度下
    都能被稳定断言，不写死某个词长。
    """
    col_w = panel._card_width(panel._tags_width())
    return geo.card_text_units(col_w, abs(theme.device_px(geo.CARD_TERM_PT)),
                               lines=geo.CARD_TERM_LINES,
                               pad_x_px=theme.px(geo.CARD_PAD_X))


class FakeEvent:
    """最小事件对象（拖动 / 改尺寸回调只用到 x_root / y_root / widget）。"""

    def __init__(self, x_root: int = 0, y_root: int = 0, widget=None):
        self.x_root = int(x_root)
        self.y_root = int(y_root)
        self.x = int(x_root)
        self.y = int(y_root)
        self.widget = widget


# ===================================================== 纯几何（不建任何控件）
class TestPanelGeometryPure(unittest.TestCase):
    """夹取 / 默认位置 / 持久化：纯函数，负坐标副屏也必须正确。"""

    def setUp(self):
        self.work = (-1920, 0, 0, 1080)      # 左侧副屏
        self.min_w, self.min_h = 300, 320

    def test_default_dock_is_right_aligned_and_centered(self):
        x, y = geo.default_dock_pos(self.work, size=44, margin=12)
        self.assertEqual(x, -56, "小方块应贴工作区右边缘（副屏上是负坐标）")
        self.assertEqual(y, 0 + (1080 - 44) // 2)

    def test_default_panel_sits_left_of_dock(self):
        dock = geo.default_dock_pos(self.work, size=44, margin=12)
        rect = geo.default_panel_rect(self.work, dock, size=(360, 460),
                                      min_w=self.min_w, min_h=self.min_h)
        self.assertEqual(rect[2:], (360, 460))
        self.assertLess(rect[0], dock[0], "面板应出现在小方块左侧")
        self.assertGreaterEqual(rect[0], self.work[0], "必须留在工作区内")
        self.assertLessEqual(rect[0] + rect[2], self.work[2])

    def test_clamp_keeps_window_visible_on_secondary_monitor(self):
        x, y, w, h = geo.clamp_rect(-5000, -5000, 360, 460, self.work,
                                    min_w=self.min_w, min_h=self.min_h)
        self.assertEqual((x, y), (self.work[0], self.work[1]),
                         "越界坐标必须被拉回工作区左上角（这里是左侧副屏）")
        self.assertLessEqual(x + w, self.work[2])
        self.assertLessEqual(y + h, self.work[3])
        self.assertEqual((w, h), (360, 460), "尺寸在范围内时应保持")

    def test_clamp_enforces_min_and_max_size(self):
        x, y, w, h = geo.clamp_rect(0, 0, 10, 10, self.work,
                                    min_w=self.min_w, min_h=self.min_h)
        self.assertEqual((w, h), (self.min_w, self.min_h), "不得小于最小值")
        x, y, w, h = geo.clamp_rect(0, 0, 99999, 99999, self.work,
                                    min_w=self.min_w, min_h=self.min_h)
        self.assertEqual((w, h), (1920, 1080), "不得大于工作区")

    def test_state_roundtrip_and_monitor_change(self):
        cfg = FakePanelConfig()
        state = geo.PanelState(dock_pos=(-56, 518), panel_rect=(-424, 310, 360, 460))
        state.save(cfg.set)
        loaded = geo.PanelState.load(cfg.get)
        self.assertEqual(loaded.dock_pos, (-56, 518))
        self.assertEqual(loaded.panel_rect, (-424, 310, 360, 460))
        # 副屏被拔掉：同一个保存位置必须被拉回主屏可见范围
        main = (0, 0, 2560, 1440)
        x, y, w, h = loaded.panel(main, min_w=self.min_w, min_h=self.min_h)
        self.assertGreaterEqual(x, main[0])
        self.assertGreaterEqual(y, main[1])
        self.assertLessEqual(x + w, main[2])
        self.assertLessEqual(y + h, main[3])

    def test_drag_state_distinguishes_click_from_drag(self):
        drag = geo.DragState(origin=(100, 100), start_rect=(10, 20, 44, 44))
        self.assertEqual(drag.update(101, 101), (11, 21))
        self.assertFalse(drag.moved, "小幅抖动仍算点击")
        self.assertEqual(drag.update(140, 150), (50, 70))
        self.assertTrue(drag.moved)

    def test_resize_state_respects_limits(self):
        drag = geo.DragState(origin=(0, 0), start_rect=(0, 0, 360, 460))
        self.assertEqual(drag.resize(-500, -500, min_w=300, min_h=320,
                                     max_w=2000, max_h=2000), (300, 320))
        self.assertEqual(drag.resize(99999, 99999, min_w=300, min_h=320,
                                     max_w=1920, max_h=1080), (1920, 1080))


# ======================================================= 词表滑轨（拖动换算）
class TestPanelScrollRailMapping(unittest.TestCase):
    """滑轨拖动必须换算成**内容**分数（``moveto`` 收到的是 first，不是滑块行程）。"""

    TRACK = 100

    def setUp(self):
        self.env = _FakeTkEnv()
        self.env.__enter__()
        self.calls: list[tuple] = []
        self.rail = widgets.ScrollRail(
            FakeWidget(None), command=lambda *a: self.calls.append(tuple(a)))
        self.rail.height_hint = self.TRACK

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def test_half_travel_maps_to_content_fraction(self):
        self.assertTrue(self.rail.set(0.0, 0.4))          # span = 0.4
        top, height = self.rail.thumb_rect(self.TRACK)
        self.rail._on_press(FakeEvent(4, top + height // 2))
        room = self.TRACK - height
        self.rail._on_motion(FakeEvent(4, int(round(self.rail._drag_offset + room / 2))))
        self.assertEqual(self.calls[-1][0], "moveto")
        self.assertAlmostEqual(float(self.calls[-1][1]), 0.3, places=6,
                               msg="滑块走一半 = 0.5 × (1 - 0.4) 的内容位置")

    def test_empty_and_full_content_do_not_scroll(self):
        for first, last in ((0.0, 0.0), (0.0, 1.0)):
            with self.subTest(first=first, last=last):
                self.calls.clear()
                self.rail.set(first, last)
                self.assertFalse(self.rail.scrollable())
                self.rail._on_press(FakeEvent(4, 50))
                self.rail._on_motion(FakeEvent(4, 80))
                self.assertEqual(self.calls, [], "空 / 满屏都不得发 moveto")


# =========================================================== 折叠 / 展开状态
class TestPanelModes(unittest.TestCase):
    def test_starts_hidden_then_dock(self):
        probe = PanelProbe()
        try:
            self.assertEqual(probe.mode, MODE_HIDDEN)
            self.assertFalse(probe.visible)
            self.assertTrue(probe.panel.show_dock())
            self.assertEqual(probe.mode, MODE_DOCK)
            self.assertTrue(probe.visible)
            self.assertEqual(probe.last_geometry(), "44x44+-56+518",
                             "小方块默认贴右边缘、垂直居中（这里是左侧副屏）")
        finally:
            probe.close()

    def test_toggle_expands_and_collapses(self):
        probe = PanelProbe()
        try:
            probe.panel.show_dock()
            self.assertTrue(probe.panel.toggle(), "点击小方块应展开")
            self.assertEqual(probe.mode, MODE_PANEL)
            self.assertEqual(probe.last_geometry(), "360x460+-424+310",
                             "展开态默认 360x460，出现在小方块左侧")
            self.assertTrue(probe.panel.toggle(), "再次点击应折叠")
            self.assertEqual(probe.mode, MODE_DOCK)
            self.assertTrue(probe.visible, "折叠后小方块仍在屏幕上")
        finally:
            probe.close()

    def test_collapse_does_not_stack_deiconify(self):
        probe = PanelProbe()
        try:
            probe.panel.show_dock()
            maps = probe.deiconify_count()
            probe.panel.expand()
            self.assertEqual(probe.deiconify_count(), maps,
                             "已可见的窗口展开时不得重新映射（只改尺寸/内容）")
            probe.panel.collapse_to_dock()
            probe.panel.collapse_to_dock()      # 幂等
            self.assertEqual(probe.deiconify_count(), maps,
                             "折叠只是缩放同一个窗口，一次都不该 remap")
        finally:
            probe.close()

    def test_user_close_blocks_auto_restore_but_explicit_recovers(self):
        probe = PanelProbe()
        try:
            probe.panel.show_dock()
            probe.panel.close_by_user()
            self.assertEqual(probe.mode, MODE_HIDDEN)
            self.assertFalse(probe.panel.restore_dock(), "用户关掉后不得自动找回")
            self.assertFalse(probe.visible)
            self.assertTrue(probe.panel.restore_dock(explicit=True),
                            "菜单 / 快捷键的显式恢复必须能找回小方块")
            self.assertEqual(probe.mode, MODE_DOCK)
        finally:
            probe.close()

    def test_hard_gate_never_maps(self):
        probe = PanelProbe(gate_mode="hard")
        try:
            self.assertFalse(probe.panel.show_dock())
            self.assertFalse(probe.visible)
            self.assertEqual(probe.deiconify_count(), 0)
        finally:
            probe.close()

    def test_selection_does_not_revive_user_closed_panel(self):
        """× 之后的新划选不得把窗口弹回来；快照保留，用户显式找回即可继续用。"""
        probe = PanelProbe()
        try:
            probe.panel.show_dock()
            probe.panel.close_by_user()
            maps = probe.deiconify_count()
            self.assertFalse(probe.panel.show_selection(make_sel(term="新词"), 7, (10, 10)),
                             "用户关闭后划选不得自动展开")
            self.assertFalse(probe.visible)
            self.assertEqual(probe.deiconify_count(), maps, "一次 deiconify 都不做")
            self.assertIsNotNone(probe.panel.selection, "待处理词必须保留")
            self.assertFalse(probe.panel.expand(), "自动路径依旧不得展开")
            self.assertTrue(probe.panel.expand(explicit=True), "用户显式找回后接着用")
            self.assertEqual(probe.mode, MODE_PANEL)
        finally:
            probe.close()

    def test_soft_gate_keeps_visible_dock_but_blocks_new_mapping(self):
        probe = PanelProbe(gate_mode="soft")
        try:
            self.assertFalse(probe.panel.show_dock(), "软受限下不得新映射")
            self.assertEqual(probe.deiconify_count(), 0)
            probe.gate.mode = "allow"
            self.assertTrue(probe.panel.show_dock())
            probe.gate.mode = "soft"
            self.assertTrue(probe.panel.show_dock(), "已显示的小方块在 self 前台下保留")
            self.assertTrue(probe.visible)
        finally:
            probe.close()


# ==================================================== 划选展开 / 点外部折叠
class TestSelectionAutoExpand(unittest.TestCase):
    def test_selection_expands_with_zero_write_and_zero_network(self):
        with temp_db() as db:
            probe = PanelProbe(db=db)
            try:
                calls: list = []
                probe.stub.explain_service = None
                sel = make_sel(term="卷积")
                self.assertTrue(probe.panel.show_selection(sel, 1, (10, 10)))
                self.assertEqual(probe.mode, MODE_PANEL, "划选后自动展开")
                self.assertFalse(probe.gate.calls[-1][2], "自动展开不是显式动作")
                self.assertEqual(db.count_entries(), 0, "划选不得落库")
                self.assertEqual(calls, [], "划选不得联网")
                self.assertEqual(probe.panel.current_question(), "")
                self.assertEqual(probe.actions, [], "自动展开不等于点了按钮")
                # 底部只显示**当前词**：选区 context 零占位，完整快照仍在内存里
                self.assertIn("卷积", str(probe.panel.quote_label.cget("text")))
                self.assertFalse(hasattr(probe.panel, "quote_meta"),
                                 "可见的选区上下文行（含控件与死引用）必须删除")
                self.assertEqual(str(sel.context), "…alpha beta…",
                                 "上下文仍完整留在内存快照里（截断只影响显示）")
            finally:
                probe.close()

    def test_auto_expand_never_activates_window(self):
        probe = PanelProbe()
        try:
            probe.panel.show_dock()
            probe.panel.show_selection(make_sel(), 1, (10, 10))
            self.assertEqual(probe.env.w32.activate_calls, [],
                             "自动展开必须保持 NOACTIVATE，绝不抢阅读焦点")
            self.assertIn(probe.env.toplevel().winfo_id(), probe.env.w32.no_activate)
        finally:
            probe.close()

    def test_outside_press_collapses_to_dock_and_keeps_pending_word(self):
        probe = PanelProbe()
        try:
            probe.panel.show_dock()
            sel = make_sel(term="alpha")
            probe.panel.show_selection(sel, 1, (10, 10))
            self.assertEqual(probe.mode, MODE_PANEL)
            probe.panel.collapse_to_dock(reason="outside")
            self.assertEqual(probe.mode, MODE_DOCK, "点外部只折叠，不消失")
            self.assertTrue(probe.visible)
            self.assertIs(probe.panel.selection, sel, "待处理词必须保留")
            self.assertTrue(probe.panel.expand())
            self.assertEqual(probe.mode, MODE_PANEL)
        finally:
            probe.close()

    def test_foreground_change_drops_pending_selection(self):
        probe = PanelProbe()
        try:
            probe.panel.show_dock()
            probe.panel.show_selection(make_sel(), 1, (10, 10))
            probe.panel.collapse_to_dock(drop_selection=True, reason="foreground_change")
            self.assertEqual(probe.mode, MODE_DOCK)
            self.assertIsNone(probe.panel.selection, "换了文档，旧选区必须作废")
        finally:
            probe.close()

    def test_input_click_is_the_only_explicit_activation(self):
        probe = PanelProbe()
        try:
            probe.panel.show_dock()
            probe.panel.show_selection(make_sel(), 1, (10, 10))
            hwnd = probe.env.toplevel().winfo_id()
            self.assertEqual(probe.env.w32.activate_calls, [])
            self.assertTrue(probe.panel._on_input_click(), "显式点击输入框才允许激活")
            self.assertEqual(probe.env.w32.activate_calls, [hwnd])
            self.assertNotIn(hwnd, probe.env.w32.no_activate,
                             "激活期间必须移除 WS_EX_NOACTIVATE")
            probe.panel.collapse_to_dock()
            self.assertIn(hwnd, probe.env.w32.no_activate,
                          "折叠后必须恢复 NOACTIVATE（下次自动展开不抢焦点）")
        finally:
            probe.close()


# ================================================== 唯一按钮 / 结果归属
class TestUnifiedActionAndResult(unittest.TestCase):
    def test_action_runs_once_per_selection(self):
        probe = PanelProbe()
        try:
            probe.panel.show_dock()
            sel = make_sel()
            probe.panel.show_selection(sel, 7, (10, 10))
            probe.panel._on_action()
            probe.panel._on_action()
            probe.panel._on_action()
            self.assertEqual(len(probe.actions), 1, "同一次选区只允许操作一次")
            probe.panel.show_selection(make_sel(term="beta"), 8, (10, 10))
            probe.panel._on_action()
            self.assertEqual(len(probe.actions), 2, "新选区必须能再次操作")
        finally:
            probe.close()

    def test_action_ignored_when_collapsed_or_hidden(self):
        probe = PanelProbe()
        try:
            probe.panel.show_dock()
            probe.panel.show_selection(make_sel(), 1, (10, 10))
            probe.panel.collapse_to_dock()
            probe.panel._on_action()
            self.assertEqual(probe.actions, [], "折叠后迟到的按钮回调不得落库")
            probe.panel.hide()
            probe.panel._on_action()
            self.assertEqual(probe.actions, [])
        finally:
            probe.close()

    def test_show_recorded_opens_detail_and_result_requires_matching_token(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = int(db.add_entry(batch_id=bid, term="alpha"))
            other = int(db.add_entry(batch_id=bid, term="beta"))
            probe = PanelProbe(db=db)
            try:
                probe.panel.show_dock()
                self.assertTrue(probe.panel.show_recorded(make_sel(), eid, request_token=3,
                                                          status="pending", explicit=True))
                self.assertEqual(probe.page, PAGE_DETAIL)
                self.assertEqual(probe.mode, MODE_PANEL)
                self.assertFalse(probe.panel.show_result(eid, "ok",
                                                         make_explain_result("旧"),
                                                         request_token=2),
                                 "旧请求 token 的结果必须被拒绝")
                self.assertFalse(probe.panel.show_result(other, "ok",
                                                         make_explain_result("别的词"),
                                                         request_token=3),
                                 "别的词条的结果必须被拒绝")
                self.assertTrue(probe.panel.show_result(eid, "ok",
                                                        make_explain_result("正确"),
                                                        request_token=3))
                self.assertIn("正确", probe.panel.def_text.get("1.0", "end"))
            finally:
                probe.close()

    def test_late_result_never_re_expands(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = int(db.add_entry(batch_id=bid, term="alpha"))
            probe = PanelProbe(db=db)
            try:
                probe.panel.show_dock()
                probe.panel.show_recorded(make_sel(), eid, request_token=1,
                                          status="pending", explicit=True)
                probe.panel.collapse_to_dock()
                maps = probe.deiconify_count()
                probe.panel.show_result(eid, "ok", make_explain_result("迟到的释义"),
                                        request_token=1)
                self.assertEqual(probe.mode, MODE_DOCK, "迟到的结果不得重新展开面板")
                self.assertEqual(probe.deiconify_count(), maps)
                probe.panel.expand()
                self.assertIn("迟到的释义", probe.panel.def_text.get("1.0", "end"),
                              "内容仍然写进去了，展开后才看到")
            finally:
                probe.close()

    def test_restore_refuses_after_user_close(self):
        probe = PanelProbe()
        try:
            probe.panel.show_dock()
            probe.panel.show_recorded(make_sel(), 5, request_token=None,
                                      status="pending_no_key", message="没有 Key",
                                      explicit=True)
            probe.panel.close_by_user()
            self.assertFalse(probe.panel.restore())
            self.assertEqual(probe.mode, MODE_HIDDEN)
        finally:
            probe.close()


# ============================================================ 标签（本地读取）
class TestTagsLocalOnly(unittest.TestCase):
    def _seed(self, db):
        bid = db.create_batch("默认批次")
        ids = []
        ids.append(db.add_entry(batch_id=bid, term="卷积", context="深度学习",
                                source_title="论文 A"))
        ids.append(db.add_entry(batch_id=bid, term="一个特别特别长的词语名称",
                                context="语境 B", source_title="论文 B"))
        db.update_entry(ids[0], explain_status="ok", one_line="一句话解释",
                        detail="详细说明")
        return bid, ids

    def test_terms_are_read_locally_and_long_tag_is_ellipsized(self):
        with temp_db() as db:
            bid, ids = self._seed(db)
            probe = PanelProbe(db=db)
            try:
                panel = probe.panel
                # 词语**按宽度换行、最多两行**：只有真的超过两行才省略。所以
                # 「刚好放得下」的超长词必须完整显示，「放不下」的才带省略号 ——
                # 两条都用面板自己那份预算构造，任何宽度下都成立。
                budget = term_budget(panel)
                fits_id = int(db.add_entry(batch_id=bid, term="M" * max(4, budget // 2)))
                over_id = int(db.add_entry(batch_id=bid, term="W" * (budget + 6)))
                self.assertTrue(panel.refresh_terms(force=True))
                shown = [str(w.cget("text")) for w in probe.env.widgets]
                self.assertIn("卷积", shown)
                self.assertIn("M" * max(4, budget // 2), shown,
                              "两行放得下的长词必须完整显示（不再按字数截断）")
                clipped = [t for t in shown if t.endswith("…")]
                self.assertTrue(clipped, "超过两行的长词必须省略")
                self.assertTrue(all(len(t) < len("W" * (budget + 6)) for t in clipped))
                def detail_text():
                    """详情页可见文本（标题 + 正文）：标题放不下时正文会补完整词。"""
                    return (str(probe.panel.term_title.cget("text")) + "\n"
                            + probe.panel.def_text.get("1.0", "end"))

                probe.panel.open_entry(over_id)
                self.assertIn("W" * (budget + 6), detail_text(),
                              "详情页必须始终给出完整词（卡片省略的只是显示）")
                probe.panel.open_entry(fits_id)
                self.assertIn("M" * max(4, budget // 2), detail_text())
            finally:
                probe.close()

    def test_refresh_terms_is_idempotent_when_data_unchanged(self):
        with temp_db() as db:
            bid, ids = self._seed(db)
            probe = PanelProbe(db=db)
            try:
                self.assertFalse(probe.panel.refresh_terms(),
                                 "数据没变时不得重建标签（门控轮询不会闪动）")
                self.assertTrue(probe.panel.refresh_terms(force=True))
                db.add_entry(batch_id=bid, term="新词")
                self.assertTrue(probe.panel.refresh_terms(),
                                "本地数据变化后才重建标签")
            finally:
                probe.close()

    def test_clicking_tag_opens_detail_without_network(self):
        with temp_db() as db:
            _, ids = self._seed(db)
            probe = PanelProbe(db=db)
            try:
                self.assertTrue(probe.panel.open_entry(ids[0]))
                self.assertEqual(probe.page, PAGE_DETAIL)
                self.assertEqual(probe.panel.entry_id, ids[0])
                body = probe.panel.def_text.get("1.0", "end")
                self.assertIn("一句话解释", body, "详情只用本地数据")
                self.assertEqual(probe.questions, [], "点标签绝不发起请求")
            finally:
                probe.close()

    def test_reopen_local_entry_keeps_db_examples_and_survives_dirty_json(self):
        """本地重开已解释词条：DB 里的 ``examples`` 必须显示，且脏 JSON 不崩、不联网。

        实时结果走 ``ExplainResult.as_text()``（例子已含在结果里）；这条路径针对的是
        **从库里重新打开**的词条 —— 旧实现只拼 ``one_line`` / ``detail``，例子丢了。
        """
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = int(db.add_entry(batch_id=bid, term="卷积"))
            db.update_entry(eid, explain_status="ok", one_line="一句话解释",
                            detail="详细说明",
                            examples=json.dumps(["例一：离散卷积", "例二：卷积核"],
                                                ensure_ascii=False))
            dirty = int(db.add_entry(batch_id=bid, term="脏例子词条"))
            db.update_entry(dirty, explain_status="ok", one_line="坏数据也能看",
                            examples="{这不是合法 JSON 数组")
            probe = PanelProbe(db=db)
            try:
                panel = probe.panel
                panel.show_dock()
                self.assertTrue(panel.open_entry(eid))
                body = panel.def_text.get("1.0", "end")
                self.assertIn("例一：离散卷积", body, "从库里打开必须带出 DB 例子")
                self.assertIn("例二：卷积核", body)

                panel.show_list()
                self.assertTrue(panel.open_entry(eid), "同一词条再次本地打开仍要成功")
                body = panel.def_text.get("1.0", "end")
                self.assertIn("例一：离散卷积", body, "重开后例子不得丢")
                self.assertIn("例二：卷积核", body)

                self.assertTrue(panel.open_entry(dirty),
                                "脏 JSON 例子不得让详情渲染失败")
                self.assertIn("坏数据也能看", panel.def_text.get("1.0", "end"))
                self.assertEqual(probe.questions, [], "本地重开绝不发起追问请求")
                self.assertEqual(probe.actions, [], "本地重开绝不触发解释 / 落库")
                self.assertEqual(db.count_entries(), 2, "读详情不得新增词条")
            finally:
                probe.close()

    def test_not_persisted_geometry_is_default(self):
        cfg = FakePanelConfig()
        state = cfg.panel_state()
        self.assertIsNone(state.panel_rect)
        self.assertIsNone(state.dock_pos)


# ==================================================== 拖动 / 改尺寸 / 持久化
class TestDragResizePersist(unittest.TestCase):
    def test_drag_moves_and_persists(self):
        probe = PanelProbe()
        try:
            probe.panel.show_dock()
            probe.panel.toggle()
            probe.panel._on_drag_start(FakeEvent(500, 400))
            probe.panel._on_drag_motion(FakeEvent(460, 430))
            probe.panel._on_drag_end(FakeEvent(460, 430))
            self.assertEqual(probe.panel.state.panel_rect,
                             (-464, 340, 360, 460),
                             "拖动距离必须落到面板位置（起点 -424+310，位移 -40+30）")
            saved = probe.config.panel_state()
            self.assertEqual(saved.panel_rect, probe.panel.state.panel_rect,
                             "拖动结束必须写进 settings")
        finally:
            probe.close()

    def test_dock_click_without_move_toggles(self):
        probe = PanelProbe()
        try:
            probe.panel.show_dock()
            probe.panel._on_drag_start(FakeEvent(100, 100))
            probe.panel._on_drag_end(FakeEvent(101, 101))
            self.assertEqual(probe.mode, MODE_PANEL, "小方块上的点击（无位移）= 展开")
        finally:
            probe.close()

    def test_resize_persists_size_and_respects_minimum(self):
        probe = PanelProbe()
        try:
            probe.panel.show_dock()
            probe.panel.toggle()
            probe.panel._on_resize_start(FakeEvent(0, 0))
            probe.panel._on_resize_motion(FakeEvent(60, 40))
            probe.panel._on_resize_end(FakeEvent(60, 40))
            rect = probe.panel.state.panel_rect
            self.assertEqual(rect[2:], (420, 500), "改尺寸必须生效")
            self.assertEqual(probe.config.panel_state().panel_rect, rect, "尺寸必须持久化")

            probe.panel._on_resize_start(FakeEvent(0, 0))
            probe.panel._on_resize_motion(FakeEvent(-9999, -9999))
            probe.panel._on_resize_end(FakeEvent(-9999, -9999))
            self.assertEqual(probe.panel.state.panel_rect[2:], (300, 320),
                             "不得小于最小尺寸")
        finally:
            probe.close()

    def test_monitor_change_pulls_panel_back_into_view(self):
        probe = PanelProbe()
        try:
            probe.panel.show_dock()
            probe.panel.toggle()
            probe.panel.state.panel_rect = (-3000, -2000, 360, 460)
            probe.env.w32.set_work_area((0, 0, 1920, 1080))   # 副屏没了
            self.assertTrue(probe.panel.expand())
            x, y, w, h = probe.panel.state.panel_rect
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x + w, 1920)
            self.assertLessEqual(y + h, 1080)
        finally:
            probe.close()

    def test_dock_drag_persists_dock_position(self):
        probe = PanelProbe()
        try:
            probe.panel.show_dock()
            probe.panel._on_drag_start(FakeEvent(0, 0))
            probe.panel._on_drag_motion(FakeEvent(-100, 50))
            probe.panel._on_drag_end(FakeEvent(-100, 50))
            self.assertEqual(probe.mode, MODE_DOCK, "拖动小方块不应触发展开")
            self.assertIsNotNone(probe.panel.state.dock_pos)
            self.assertEqual(probe.config.panel_state().dock_pos,
                             probe.panel.state.dock_pos)
        finally:
            probe.close()

    def test_dock_drag_reaches_the_work_area_corner(self):
        """fake 1920x1080：44x44 小方块必须能拖到 (1876, 1036)。

        1876 = 1920 - 44、1036 = 1080 - 44：夹取只能用小方块**自身尺寸**。
        旧实现拿展开面板的最小值 300x320 当 min_w/min_h，可停范围被压成
        x ≤ 1620、y ≤ 760 —— 方块永远贴不到屏幕右边、下边（用户报的自由拖动 bug）。
        """
        probe = PanelProbe(work_area=(0, 0, 1920, 1080))
        try:
            panel = probe.panel
            panel.show_dock()
            panel._on_drag_start(FakeEvent(100, 100, widget=panel.dock_label))
            panel._on_drag_motion(FakeEvent(9999, 9999, widget=panel.dock_label))
            self.assertEqual(panel._current_rect(), (1876, 1036, 44, 44),
                             "44 方块右下角必须能贴到工作区右下角")
            # 松手点与 Motion 同一点：不得跳位（尤其不得跳回 300x320 夹出的 1620,760）
            panel._on_drag_end(FakeEvent(9999, 9999, widget=panel.dock_label))
            self.assertEqual(panel._current_rect(), (1876, 1036, 44, 44),
                             "松手必须停在鼠标处，不得跳位")
            self.assertEqual(panel.state.dock_pos, (1876, 1036),
                             "松手必须持久化真实位置")
            self.assertEqual(probe.config.panel_state().dock_pos, (1876, 1036))
        finally:
            probe.close()

    def test_dock_drag_reaches_the_corner_of_a_negative_monitor(self):
        """负坐标副屏（工作区 -1920..0）：方块右下角 = (-44, 1036)，松手不跳位。"""
        probe = PanelProbe(work_area=(-1920, 0, 0, 1080))
        try:
            panel = probe.panel
            panel.show_dock()
            panel._on_drag_start(FakeEvent(-200, -200, widget=panel.dock_label))
            panel._on_drag_motion(FakeEvent(1000, 1000, widget=panel.dock_label))
            self.assertEqual(panel._current_rect(), (-44, 1036, 44, 44),
                             "负坐标副屏：方块应贴其右下角（x 为负，不被夹到 0）")
            panel._on_drag_end(FakeEvent(1000, 1000, widget=panel.dock_label))
            self.assertEqual(panel._current_rect(), (-44, 1036, 44, 44), "松手不跳位")
            self.assertEqual(panel.state.dock_pos, (-44, 1036))
        finally:
            probe.close()

    def test_panel_drag_still_keeps_the_panel_size_inside_the_work_area(self):
        """展开态不受本次修复影响：360x460 面板拖到右下角 = (1560, 620) 且尺寸不变。"""
        probe = PanelProbe(work_area=(0, 0, 1920, 1080))
        try:
            panel = probe.panel
            panel.show_dock()
            panel.toggle()
            panel._on_drag_start(FakeEvent(100, 100, widget=panel.title_label))
            panel._on_drag_motion(FakeEvent(9999, 9999, widget=panel.title_label))
            self.assertEqual(panel._current_rect(), (1560, 620, 360, 460),
                             "面板右 / 下边贴住工作区，尺寸不得被改")
            panel._on_drag_end(FakeEvent(9999, 9999, widget=panel.title_label))
            self.assertEqual(panel._current_rect(), (1560, 620, 360, 460), "松手不跳位")
        finally:
            probe.close()


# ============================================================ 追问（面板侧）
class TestPanelChat(unittest.TestCase):
    def _open(self, probe, db, entry_id=1):
        probe.panel.show_dock()
        probe.panel.open_entry(entry_id)
        return entry_id

    def test_send_requires_explicit_click_and_clears_input(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = db.add_entry(batch_id=bid, term="alpha")
            probe = PanelProbe(db=db)
            try:
                self._open(probe, db, eid)
                probe.panel.entry_input.insert(0, "它和 beta 有什么区别？")
                self.assertEqual(probe.panel.send_question(), "sent")
                self.assertEqual(probe.questions, [(eid, "它和 beta 有什么区别？")])
                self.assertEqual(probe.panel.current_question(), "", "发送成功才清空输入")
                self.assertTrue(probe.panel._chat_busy)
                self.assertEqual(probe.panel.send_question(), "busy",
                                 "上一条还在回答时：连按回车先看到「上一条还在回答中」")
                self.assertIn("上一条还在回答", str(probe.panel.hint_label.cget("text")))
                self.assertEqual(len(probe.questions), 1, "守卫挡下时不得重复发请求")
            finally:
                probe.close()

    def test_no_key_keeps_input_and_never_sends(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = db.add_entry(batch_id=bid, term="alpha")
            probe = PanelProbe(db=db, config_values={"_has_key": "0"})
            try:
                self._open(probe, db, eid)
                probe.panel.entry_input.insert(0, "只在本地的提问")
                self.assertEqual(probe.panel.send_question(), "unavailable")
                self.assertEqual(probe.panel.current_question(), "只在本地的提问",
                                 "没有 Key 时必须保留用户输入")
                self.assertIn("API Key", str(probe.panel.hint_label.cget("text")))
            finally:
                probe.close()

    def test_chat_result_is_scoped_to_current_entry_and_token(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            eid_a = db.add_entry(batch_id=bid, term="alpha")
            eid_b = db.add_entry(batch_id=bid, term="beta")
            probe = PanelProbe(db=db)
            try:
                self._open(probe, db, eid_a)
                probe.panel.entry_input.insert(0, "问题 A")
                probe.panel.send_question()
                token = probe.panel._chat_request_token
                self.assertFalse(probe.panel.on_chat_result(token, eid_b, "ok", "别的词的回答"),
                                 "别的词条的回答不得写进当前详情")
                self.assertFalse(probe.panel.on_chat_result(token + 99, eid_a, "ok", "旧请求"),
                                 "旧请求 token 的回答必须被拒绝")
                self.assertTrue(probe.panel.on_chat_result(token, eid_a, "ok", "回答 A"))
                self.assertFalse(probe.panel._chat_busy)
            finally:
                probe.close()

    def test_retry_button_only_retries_current_entry(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = db.add_entry(batch_id=bid, term="alpha")
            probe = PanelProbe(db=db)
            try:
                self._open(probe, db, eid)
                self.assertTrue(probe.panel._on_retry_chat())
                self.assertEqual(probe.retries, [eid])
            finally:
                probe.close()

    def test_chat_history_rendered_from_local_db(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = db.add_entry(batch_id=bid, term="alpha")
            db.add_chat_turn(entry_id=eid, role="user", content="之前的问题")
            db.add_chat_turn(entry_id=eid, role="assistant", content="之前的回答")
            db.add_chat_turn(entry_id=eid, role="assistant", content="连接超时",
                             status="error")
            probe = PanelProbe(db=db)
            try:
                self._open(probe, db, eid)
                body = probe.panel.chat_text.get("1.0", "end")
                self.assertIn("之前的问题", body)
                self.assertIn("之前的回答", body)
                self.assertIn("连接超时", body)
            finally:
                probe.close()


# ====================================================== App 级：面板与门控
class TestPanelAppWiring(unittest.TestCase):
    """headless App：同一个面板承担两个角色，且选区路径零写零网。"""

    def test_app_exposes_single_panel_for_both_roles(self):
        with headless_app() as app:
            self.assertIs(app.reading_panel, app.selection_bar,
                          "App 暴露的阅读面板就是选区角色那个对象")

    def test_build_overlays_returns_the_same_object_twice(self):
        """生产装配：选区条与结果窗是**同一个窗口**（用户只看到一个面板）。"""
        from unittest import mock

        import app.main as app_main

        sentinel = object()
        with mock.patch.object(app_main, "ReadingPanel", return_value=sentinel) as factory:
            first, second = app_main.build_overlays(None, object())
        self.assertIs(first, sentinel)
        self.assertIs(second, sentinel)
        factory.assert_called_once()

    def test_outside_click_voids_selection_without_writing(self):
        """点别处只**作废待处理选区**：不折叠 / 不收起窗口、不写库、不联网。"""
        with headless_app() as app:
            show_selection(app, make_sel())
            self.assertTrue(app.selection_bar.visible)
            app._ui_q.put(("overlay_press", {
                "x": 900, "y": 900, "when": app._bar_shown_at + 0.001,
                "hwnd": 555003, "is_overlay": False,
                "generation": app.capture_service.current_generation(),
            }))
            pump_app(app)
            self.assertTrue(app.selection_bar.visible,
                            "软事件（点别处）不得替用户收起窗口")
            self.assertIsNone(app._current_selection, "旧选区必须作废")
            self.assertEqual(app.db.count_entries(), 0, "作废选区不得写库")

    def test_press_on_panel_does_not_collapse(self):
        with headless_app() as app:
            show_selection(app, make_sel())
            app._ui_q.put(("overlay_press", {
                "x": 60, "y": 60, "when": app._bar_shown_at + 0.001,
                "hwnd": 555001, "is_overlay": True,
                "generation": app.capture_service.current_generation(),
            }))
            pump_app(app)
            self.assertTrue(app.selection_bar.visible, "点面板内部（含输入框）不得折叠")

    def test_foreground_change_keeps_panel_and_voids_selection(self):
        from tests.support import DEFAULT_HEADLESS_FG

        with headless_app() as app:
            app.capture_service.note_foreground(dict(DEFAULT_HEADLESS_FG))
            show_selection(app, make_sel())
            token_before = app._selection_token
            app.capture_service.note_foreground({
                "hwnd": 66001, "pid": 4242, "title": "别的文档", "app": "firefox.exe",
                "exe": "C:\\firefox.exe", "is_self": False})
            pump_app(app)
            self.assertTrue(app.selection_bar.visible,
                            "换页面只作废选区，不得自动折叠 / 收起窗口")
            self.assertIsNone(app._current_selection, "旧选区必须作废")
            self.assertEqual(app._selection_token, token_before + 1)

    def test_hard_gate_hides_panel_and_release_restores_dock_only(self):
        from tests.support import foreground

        with headless_app() as app:
            show_selection(app, make_sel())
            with foreground(title="游戏", app="steam.exe", hwnd=70001, pid=7001):
                app._apply_gate(force=True)
            self.assertFalse(app.explain_window.visible, "硬阻断必须隐藏面板")
            app._apply_gate(force=True)          # 回到允许的前台
            self.assertTrue(app.selection_bar.visible, "离开游戏恢复小方块")
            self.assertEqual(getattr(app.selection_bar, "collapse_calls", 0), 0,
                             "恢复只显示小方块，不做展开/折叠面板的动作")

    def test_no_key_chat_is_never_sent(self):
        with headless_app() as app:
            bid = app.db.create_batch("b")
            eid = app.db.add_entry(batch_id=bid, term="alpha")
            calls: list = []
            app.chat_service.make_client = lambda **kw: calls.append(kw)
            self.assertFalse(app.config.has_api_key())
            self.assertIsNone(app.ask_entry_question(eid, "为什么？"))
            self.assertEqual(calls, [], "没有 Key 时绝不发起任何请求")
            self.assertEqual(app.db.count_chat_turns(eid), 0, "也不写任何对话记录")
            self.assertIn("未能发送追问", app.main.status_label.cget("text"))


# ============================================================================
# 真实 App + 真实共享面板（假 Tk）：**同一个窗口**的端到端链路
# ============================================================================
#
# 这一组是「不许用两套 FakeSelectionBar / FakeExplainWindow 分开替身」的落点：
# ``headless_app(overlays="panel", main_window="real")`` 里窗口层是**真实的
# ReadingPanel / MainWindow**（只有 Tk 控件与 Win32 是替身，依然零真实窗口），
# 因此「门控轮询把用户正在打字的面板折叠掉」「唯一动作先缩成小方块再展开」
# 这类**共享窗口**才有的问题才能被真正复现。


class TestRealAppSharedPanelChain(unittest.TestCase):
    """同一窗口链路：self 前台保输入、唯一动作不闪、迟到结果不展开。"""

    @staticmethod
    def _open(**kwargs):
        return headless_app(overlays="panel", main_window="real", **kwargs)

    # ---------------------------------------------------------- 缺陷 1
    def test_self_foreground_keeps_panel_and_input(self):
        """面板自己在前台（用户正在输入）→ 门控轮询不得折叠、不得丢输入。"""
        with self._open() as app:
            panel = app.reading_panel
            show_selection(app, make_sel(term="卷积"))
            self.assertTrue(panel.is_open())
            panel.entry_input.insert(0, "我正在打字")
            self.assertTrue(panel._on_input_click(), "点输入框 = 显式激活")
            self.assertTrue(panel.input_active())

            app.fake_tk.w32.foreground_hwnd = panel.hwnd()
            app.fake_tk.w32.root_map[panel.hwnd()] = panel.hwnd()
            # 与真实 Tk 一致：panel.hwnd() 不是进程主窗口，is_self 由「是不是自家
            # 浮层」判定（见 AccessGate._is_self_window 的宿主谓词）
            app.fake_tk.w32.is_self_hwnds.discard(panel.hwnd())
            decision = app._apply_gate(force=True)
            self.assertEqual(decision.reason, "self_window")
            self.assertTrue(panel.is_open(), "self 前台下已展开的面板必须保留")
            self.assertTrue(panel.visible)
            self.assertEqual(panel.current_question(), "我正在打字",
                             "输入框内容绝不能被门控折叠吞掉")
            self.assertTrue(panel.input_active(), "输入状态保持")

    def test_self_foreground_with_child_hwnd_still_counts_as_our_panel(self):
        """Tk 输入框是子窗口：前台 HWND 是子窗口时也要认出「这是我们自己」。"""
        with self._open() as app:
            panel = app.reading_panel
            show_selection(app, make_sel())
            child = panel.hwnd() + 1
            app.fake_tk.w32.foreground_hwnd = child
            app.fake_tk.w32.root_map[child] = panel.hwnd()   # GetAncestor(GA_ROOT)
            app._apply_gate(force=True)
            self.assertTrue(panel.is_open(), "子控件在前台 = 面板在前台，不得折叠")

    def test_settings_dialog_foreground_keeps_panel_and_word(self):
        """主词典 / 设置在前台 → 面板**保持展开**，待处理词也保留。

        软受限只表示「不在这里**新映射**浮层」（实时门控执行），
        绝不是「把用户已经打开的面板收起来」的理由。
        """
        with self._open() as app:
            panel = app.reading_panel
            show_selection(app, make_sel(term="卷积"))
            app.fake_tk.w32.foreground_hwnd = 999001          # 别的本程序窗口
            app.fake_tk.w32.is_self_hwnds.add(999001)
            app.fake_tk.w32.root_map[999001] = 999001
            decision = app._apply_gate(force=True)
            self.assertEqual(decision.reason, "self_window")
            self.assertEqual(panel.mode_name(), MODE_PANEL,
                             "软受限（本程序窗口在前台）不得折叠面板")
            self.assertTrue(panel.is_open())
            self.assertIsNotNone(panel.selection, "配 Key 回来还要继续用这个词")
            self.assertEqual(panel.selection.term, "卷积")

    def test_soft_block_repeat_does_not_touch_own_panel(self):
        """重复的软受限回调（前台一直是我们的面板）也不得改变面板状态。"""
        from app.gate import GateDecision, R_SELF

        with self._open() as app:
            panel = app.reading_panel
            show_selection(app, make_sel())
            app.fake_tk.w32.foreground_hwnd = panel.hwnd()
            decision = GateDecision(allowed=False, reason=R_SELF)
            before = panel.win.geometry_specs[-1]
            app._soft_block_overlays(decision)
            self.assertTrue(panel.is_open())
            self.assertEqual(panel.win.geometry_specs[-1], before,
                             "保持已展开面板时不得下发新的 geometry")

    # ---------------------------------------------------------- 缺陷 2
    def test_single_action_never_collapses_the_shared_window(self):
        """唯一动作：不折叠成小方块、不 withdraw/缩尺寸，只有内容与页面变化。"""
        with self._open() as app:
            from tests.test_unified_action import (
                FakeClient, configure_key, wait_for,
            )

            configure_key(app.config)
            # 假解释客户端：绝不联网；收尾前等这次解释真正结束（关库之后
            # 不能再有后台写库线程，日志里也就不该出现 ProgrammingError）
            app.explain_service.make_client = lambda **kw: FakeClient("卷积的释义")
            panel = app.reading_panel
            show_selection(app, make_sel(term="卷积"))
            specs_before = list(panel.win.geometry_specs)
            deicon_before = app.fake_tk.deiconify_count()
            withdraw_before = app.fake_tk.withdraw_count()

            entry_id = panel._on_action()

            self.assertEqual(panel.win.geometry_specs, specs_before,
                             "同一次点击里不得出现「先 44x44 再 360x460」的中间态")
            self.assertEqual(app.fake_tk.deiconify_count(), deicon_before)
            self.assertEqual(app.fake_tk.withdraw_count(), withdraw_before)
            self.assertTrue(panel.is_open(), "面板保持展开（不是先折叠再展开）")
            self.assertTrue(panel.is_action_consumed(), "选区已被消费")
            self.assertEqual(panel.page, PAGE_DETAIL, "同一窗口切到该词详情")
            self.assertEqual(app.db.count_entries(), 1, "仍然照常落库")
            panel._on_action()
            panel._on_action()
            self.assertEqual(app.db.count_entries(), 1, "再次点击不会重复落库")
            self.assertTrue(wait_for(
                app, lambda: app.db.get_entry(entry_id)["explain_status"] == "ok"))
            self.assertTrue(wait_for(
                app, lambda: not app.explain_service.is_inflight(entry_id)),
                "测试收尾前解释 worker 必须已经结束")

    # ---------------------------------------------------------- 缺陷 7
    def test_manual_close_then_late_result_never_reopens(self):
        with self._open() as app:
            from tests.test_unified_action import FakeClient, configure_key, wait_for

            configure_key(app.config)
            client = FakeClient("迟到的释义")
            app.explain_service.make_client = lambda **kw: client
            panel = app.reading_panel
            show_selection(app, make_sel(term="卷积"))
            panel._on_action()
            entry_id = panel.entry_id
            self.assertIsNotNone(entry_id)
            panel.close_by_user()
            self.assertEqual(panel.mode_name(), MODE_HIDDEN)
            deicon_before = app.fake_tk.deiconify_count()

            self.assertTrue(
                wait_for(app, lambda: app.db.get_entry(entry_id)["explain_status"] == "ok"),
                "解释照常在后台写库")
            self.assertEqual(panel.mode_name(), MODE_HIDDEN, "手动关闭后不得被迟到结果弹回来")
            self.assertEqual(app.fake_tk.deiconify_count(), deicon_before)
            self.assertFalse(panel.restore(), "用户关闭过：显式 restore 也不找回")

    def test_game_to_normal_restores_dock_only(self):
        with self._open(config={"gate__game_mode": "1"}) as app:
            panel = app.reading_panel
            self.assertFalse(panel.show_dock(explicit=True),
                             "游戏模式（硬阻断）下不得映射任何窗口")
            self.assertFalse(panel.visible)
            # 离开游戏模式：只恢复小方块，绝不自动展开面板
            app.gate.set_game_mode(False)
            app._apply_gate(force=True)
            self.assertEqual(panel.mode_name(), MODE_DOCK,
                             "game → 普通只恢复 dock，不自动展开")
            self.assertTrue(panel.visible)
            self.assertTrue(panel.show_dock(force=True))
            self.assertEqual(panel.mode_name(), MODE_DOCK)

    def test_leaving_game_mode_never_autoexpands_panel(self):
        """游戏前台下展开过面板 → 恢复时也只给小方块。"""
        with self._open(config={"gate__game_mode": "1"}) as app:
            panel = app.reading_panel
            panel.expand(force=True)
            panel.close_by_user()
            self.assertFalse(panel.visible)
            app.gate.set_game_mode(False)
            panel.restore_dock(explicit=True)
            self.assertEqual(panel.mode_name(), MODE_DOCK)
            self.assertNotEqual(panel.mode_name(), MODE_PANEL)

    # ------------------------------------------------ 软受限轮询循环（缺陷 3）
    def test_normal_self_normal_polling_keeps_panel_expanded(self):
        """普通 → 设置 / 自身窗口 → 普通：轮询循环里窗口状态**一步都不动**。"""
        with self._open() as app:
            panel = app.reading_panel
            show_selection(app, make_sel(term="卷积"))
            self.assertTrue(panel.is_open())
            panel.entry_input.insert(0, "正在打字")
            app._apply_gate(force=True)                 # 普通阅读前台
            deicon = app.fake_tk.deiconify_count()
            withdraw = app.fake_tk.withdraw_count()
            specs = list(panel.win.geometry_specs)

            app.fake_tk.w32.foreground_hwnd = 999001    # 本程序另一个窗口（设置）
            app.fake_tk.w32.is_self_hwnds.add(999001)
            app.fake_tk.w32.root_map[999001] = 999001
            self.assertEqual(app._apply_gate(force=True).reason, "self_window")
            self.assertTrue(panel.is_open(), "self 前台下已展开的面板必须保留")

            app.fake_tk.w32.foreground_hwnd = 55501     # 回到普通阅读窗口
            self.assertTrue(app._apply_gate(force=True).allowed)

            self.assertTrue(panel.is_open(), "回到普通窗口后面板必须**仍然展开**")
            self.assertEqual(panel.mode_name(), MODE_PANEL,
                             "软受限 → 允许 不得把已展开的面板折叠成小方块")
            self.assertEqual(panel.current_question(), "正在打字", "输入不得被轮询吞掉")
            self.assertEqual(app.fake_tk.deiconify_count(), deicon, "零 deiconify")
            self.assertEqual(app.fake_tk.withdraw_count(), withdraw, "零 withdraw")
            self.assertEqual(panel.win.geometry_specs, specs, "零 geometry 调用")

    def test_normal_desktop_no_foreground_polling_keeps_panel_expanded(self):
        """普通 → 桌面 / 无前台 → 普通：同样一步都不动。"""
        with self._open() as app:
            panel = app.reading_panel
            show_selection(app, make_sel(term="卷积"))
            app._apply_gate(force=True)
            deicon = app.fake_tk.deiconify_count()
            withdraw = app.fake_tk.withdraw_count()
            specs = list(panel.win.geometry_specs)

            original = app.fake_tk.w32.is_probably_desktop
            app.fake_tk.w32.is_probably_desktop = lambda _hwnd: True
            try:
                self.assertEqual(app._apply_gate(force=True).reason, "desktop")
            finally:
                app.fake_tk.w32.is_probably_desktop = original
            self.assertTrue(panel.is_open(), "桌面在前台不得折叠面板")

            app.fake_tk.w32.foreground_hwnd = 0         # 无前台窗口
            app.fake_tk.w32.foreground_template = dict(DEFAULT_HEADLESS_FG, hwnd=0, pid=0)
            self.assertEqual(app._apply_gate(force=True).reason, "no_foreground")
            self.assertTrue(panel.is_open(), "无前台不得折叠面板")

            app.fake_tk.w32.foreground_hwnd = 55501     # 回到普通阅读窗口
            app.fake_tk.w32.foreground_template = dict(DEFAULT_HEADLESS_FG)
            self.assertTrue(app._apply_gate(force=True).allowed)
            self.assertEqual(panel.mode_name(), MODE_PANEL)
            self.assertEqual(app.fake_tk.deiconify_count(), deicon, "零 deiconify")
            self.assertEqual(app.fake_tk.withdraw_count(), withdraw, "零 withdraw")
            self.assertEqual(panel.win.geometry_specs, specs, "零 geometry 调用")

    def test_gate_flip_restores_hidden_panel_as_dock_only(self):
        """隐藏且用户没关过 → 门控翻转进允许时只补回小方块；状态没翻转时零调用。"""
        with self._open() as app:
            panel = app.reading_panel
            self.assertFalse(panel.visible)

            app.fake_tk.w32.foreground_hwnd = 999001    # 软受限：什么都不做
            app.fake_tk.w32.is_self_hwnds.add(999001)
            self.assertEqual(app._apply_gate(force=True).reason, "self_window")
            self.assertFalse(panel.visible, "软受限下不新映射窗口")

            app.fake_tk.w32.foreground_hwnd = 55501     # 回到普通：只补小方块
            self.assertTrue(app._apply_gate(force=True).allowed)
            self.assertEqual(panel.mode_name(), MODE_DOCK, "兜底只显示小方块，不展开")
            self.assertTrue(panel.visible)

            deicon = app.fake_tk.deiconify_count()
            specs = list(panel.win.geometry_specs)
            app._apply_gate(force=True)                 # allow → allow：状态没翻转
            self.assertEqual(app.fake_tk.deiconify_count(), deicon)
            self.assertEqual(panel.win.geometry_specs, specs)

    def test_closed_panel_is_never_revived_by_polling(self):
        """用户关闭过的小方块：任何门控轮询都不得复活它。"""
        with self._open() as app:
            panel = app.reading_panel
            panel.show_dock(explicit=True)
            panel.close_by_user()
            self.assertFalse(panel.visible)
            deicon = app.fake_tk.deiconify_count()

            app._apply_gate(force=True)                 # 普通
            app.fake_tk.w32.foreground_hwnd = 999001    # self
            app.fake_tk.w32.is_self_hwnds.add(999001)
            app._apply_gate(force=True)
            app.fake_tk.w32.foreground_hwnd = 55501     # 回到普通
            app._apply_gate(force=True)

            self.assertFalse(panel.visible, "用户关闭过的窗口绝不被轮询复活")
            self.assertEqual(panel.mode_name(), MODE_HIDDEN)
            self.assertEqual(app.fake_tk.deiconify_count(), deicon, "零 deiconify")

    def test_overlay_display_allowed_three_states(self):
        """显示门控的唯一判据：硬阻断一律拒绝；软受限保留已显示、拒绝新映射。"""
        from tests.support import foreground

        with self._open() as app:
            panel = app.reading_panel
            self.assertTrue(app.overlay_display_allowed(panel), "普通前台允许新浮层")
            self.assertTrue(app.overlay_allowed())

            with foreground(title="某游戏", app="steam.exe", hwnd=70001, pid=7001):
                self.assertFalse(app.overlay_display_allowed(panel),
                                 "硬阻断下不允许新映射")
                self.assertFalse(app.overlay_display_allowed(panel, already_visible=True),
                                 "硬阻断下已显示的也必须收起")
                self.assertFalse(app.overlay_display_allowed(panel, explicit=True),
                                 "硬阻断下显式动作也无效")

            app.fake_tk.w32.foreground_hwnd = 999001
            app.fake_tk.w32.is_self_hwnds.add(999001)
            self.assertFalse(app.overlay_display_allowed(panel),
                             "软受限（自身窗口）不新映射浮层")
            self.assertTrue(app.overlay_display_allowed(panel, already_visible=True),
                            "软受限下已显示的面板保留（用户正在里面打字）")
            self.assertTrue(app.overlay_display_allowed(panel, explicit=True),
                            "软受限下用户仍可显式找回面板（它不是自动浮层）")

    def test_new_selection_does_not_revive_user_closed_panel(self):
        """App 链路：用户关闭过 → 新选区只留快照与提示，绝不 deiconify。"""
        with self._open() as app:
            panel = app.reading_panel
            panel.show_dock(explicit=True)
            panel.close_by_user()
            self.assertFalse(panel.visible)
            deicon = app.fake_tk.deiconify_count()

            show_selection(app, make_sel(term="新词"))

            self.assertFalse(panel.visible, "× 之后的新选区不得复活窗口")
            self.assertEqual(app.fake_tk.deiconify_count(), deicon, "零 deiconify")
            self.assertEqual(panel.mode_name(), MODE_HIDDEN)
            self.assertIsNotNone(app._current_selection, "选区快照仍然保留在内存里")
            self.assertIn("面板已由用户关闭", app.main.status_label.cget("text"))

    def test_show_dock_already_visible_still_passes_live_gate(self):
        """已可见的小方块也不能绕过实时门控（游戏模式下必须消失）。"""
        with self._open() as app:
            panel = app.reading_panel
            self.assertTrue(panel.show_dock(explicit=True))
            self.assertTrue(panel.visible)
            app.gate.set_game_mode(True)          # 实时硬阻断（不触发轮询回调）
            deicon_before = app.fake_tk.deiconify_count()
            self.assertFalse(panel.show_dock(force=True),
                             "硬阻断下「已可见」也必须被拒绝")
            self.assertFalse(panel.visible)
            self.assertEqual(app.fake_tk.deiconify_count(), deicon_before,
                             "只隐藏，零 deiconify")

    # ---------------------------------------------------------- 缺陷 8
    def test_detail_input_and_local_tag_flow_on_real_chain(self):
        """详情输入发送 + 点标签本地读取，都在真实共享面板链路上成立。"""
        from tests.test_chat import FakeChatClient
        from tests.test_unified_action import configure_key, wait_for

        with self._open() as app:
            configure_key(app.config, model="model-A")
            client = FakeChatClient(answer="这是真实链路的回答")
            app.chat_service.make_client = lambda **kw: client
            panel = app.reading_panel
            bid = app.db.create_batch("批次")
            eid = int(app.db.add_entry(batch_id=bid, term="卷积",
                                       context="深度学习中的卷积"))
            app.capture_service.set_current_batch(bid, explicit=True)
            panel.show_dock(explicit=True)
            panel.refresh_terms()
            self.assertIn("卷积", [str(w.cget("text")) for w in panel.tags_area.children],
                          "标签列表按本地库渲染")

            # 点标签 = 展开同一窗口 + 纯本地读取（不联网、不写库）
            self.assertTrue(panel.open_entry(eid))
            self.assertEqual(panel.page, PAGE_DETAIL)
            self.assertTrue(panel.is_open(), "点标签在同窗展开详情")
            self.assertEqual(client.calls, [], "点标签绝不发请求")
            self.assertEqual(app.db.count_chat_turns(eid), 0)

            panel.entry_input.insert(0, "它和互相关有什么区别？")
            self.assertTrue(panel._on_input_click())
            self.assertEqual(panel.send_question(), "sent")
            self.assertTrue(wait_for(app, lambda: app.db.count_chat_turns(eid) == 2),
                            "问 + 答都写回同一个词条")
            self.assertTrue(wait_for(app, lambda: not panel._chat_busy))
            self.assertIn("这是真实链路的回答", panel.chat_text.get("1.0", "end"))
            self.assertTrue(panel.is_open(), "追问全程面板保持展开")


class TestPanelMenuAndBackgroundStart(unittest.TestCase):
    """面板顶栏形态 + 后台启动形态（真实主界面代码，假 Tk 控件）。

    当前契约：浮窗顶栏**只有**「主界面 / 折叠 / ×」三个入口，没有任何菜单 ——
    浮窗带 ``WS_EX_NOACTIVATE``，``tk_popup`` 抓取式菜单在实机上拿不到焦点，
    点了像没反应。宿主菜单（``app.ui.app_menu``）仍然给主窗口菜单栏用，
    并且必须始终包含「退出」。
    """

    def test_panel_topbar_has_no_menu_and_only_three_entries(self):
        from app.ui.app_menu import MENU_QUIT, build_menu, menu_items

        with headless_app(overlays="panel", main_window="real") as app:
            panel = app.reading_panel
            self.assertTrue(panel.show_dock(explicit=True))
            panel.expand(explicit=True)
            env = app.fake_tk
            self.assertIsNone(env.find_button("菜单"),
                              "浮窗里不得再有菜单入口（NOACTIVATE 拿不到焦点）")
            for label in ("主界面", "折叠", CLOSE_TEXT):
                self.assertIsNotNone(env.find_button(label), f"顶栏缺「{label}」入口")
            self.assertFalse(hasattr(panel, "_on_menu"), "菜单方法必须随控件一起删掉")

            # 宿主菜单仍在（主窗口菜单栏 = 「随时可明确退出」的兜底）
            ids = [item[0] for item in menu_items(app)]
            self.assertIn(MENU_QUIT, ids, "宿主菜单必须保留「退出」")
            menu = build_menu(panel.win, app)
            self.assertIsNotNone(menu, "宿主菜单必须真的能建出来")
            menu.destroy()

    def test_topbar_main_button_calls_app_open_main_window(self):
        """顶栏「主界面」：**真实假 Tk 按钮** → 真实 ``App.open_main_window``。"""
        with headless_app(overlays="panel", main_window="real") as app:
            panel = app.reading_panel
            panel.show_dock(explicit=True)
            panel.expand(explicit=True)
            button = app.fake_tk.find_button("主界面")
            self.assertIsNotNone(button)
            before = app.fake_root.deiconify_calls
            before_lift = app.fake_root.lift_calls
            self.assertTrue(button.invoke(), "「主界面」按钮必须走通 App.open_main_window")
            self.assertEqual(app.fake_root.deiconify_calls, before + 1,
                             "打开主界面 = 真实 App.open_main_window（deiconify + lift）")
            self.assertEqual(app.fake_root.lift_calls, before_lift + 1,
                             "打开主界面必须把主窗抬到最前")

    def test_background_start_shows_dock_and_never_auto_expands_panel(self):
        with headless_app(overlays="panel", main_window="real") as app:
            panel = app.reading_panel
            app._restore_dock(explicit=True)
            self.assertEqual(panel.mode_name(), MODE_DOCK,
                             "后台启动只留小方块，绝不浮出面板")
            app._apply_gate(force=True)
            self.assertEqual(panel.mode_name(), MODE_DOCK)
            app.root.withdraw()       # 主界面默认 withdraw（build_app 的行为）
            self.assertEqual(panel.mode_name(), MODE_DOCK)


class TestLaunchHidesMainWindowFirst(unittest.TestCase):
    """``main()`` 必须在 ``build_app`` **之前**就隐藏根窗口（防启动闪主窗口）。

    纯 fake：假 Tk 根窗口 + 假 build_app —— 不创建真实 Tk/Toplevel，不进入事件循环。
    记录的是事件顺序，因此「先隐藏、再装配」这条时序一旦被改回去就会失败。
    """

    def test_root_is_withdrawn_before_build_app(self):
        import tkinter as tk

        import app.main as app_main
        from unittest import mock

        events: list = []

        class FakeRoot:
            """最小假根窗口：只记录 withdraw / title / mainloop。"""

            def __init__(self):
                self.hidden = False

            def withdraw(self):
                self.hidden = True
                events.append("withdraw")

            def title(self, *_args):
                events.append("title")

            def mainloop(self):
                events.append("mainloop")

        class FakeApp:
            def start(self):
                events.append("start")

            def quit(self):
                events.append("quit")

        root = FakeRoot()

        def fake_build_app(fake_root, window_title=None):
            self.assertTrue(getattr(fake_root, "hidden", False),
                            "build_app 之前根窗口必须已经 withdraw（否则会闪主窗口）")
            events.append("build_app")
            return FakeApp()

        with mock.patch.object(tk, "Tk", return_value=root), \
                mock.patch.object(app_main, "build_app", side_effect=fake_build_app), \
                mock.patch.object(app_main, "setup_logging", lambda *a, **k: None), \
                mock.patch.object(app_main.w32, "enable_dpi_awareness", lambda: None):
            self.assertEqual(app_main.main([]), 0)

        self.assertIn("withdraw", events, "main() 必须先隐藏根窗口")
        self.assertIn("build_app", events, "装配仍要发生")
        self.assertLess(events.index("withdraw"), events.index("build_app"),
                        "顺序必须是「先隐藏 → 再 build_app」")
        self.assertIn("mainloop", events, "隐藏之后照常进入主循环")

    def test_build_app_keeps_its_own_withdraw_defense(self):
        """build_app 自己也要 withdraw（double 保险），两条路径都隐藏主窗口。"""
        import inspect

        import app.main as app_main

        source = inspect.getsource(app_main.build_app)
        self.assertIn("root.withdraw()", source,
                      "build_app 的隐藏防御不得被删掉（main 的提前隐藏是补充）")


class TestPageScopedTermList(unittest.TestCase):
    """词表只显示**当前阅读页面**的词：新页面是空表，绝不拿全库顶上。"""

    @staticmethod
    def _open(**kwargs):
        return headless_app(overlays="panel", main_window="real", **kwargs)

    @staticmethod
    def _page_source(app, title: str = "Some Document", hwnd: int = 55501):
        from app.models import SourceInfo

        src = SourceInfo(hwnd=hwnd, pid=9999, exe="C:\\Program Files\\msedge.exe",
                         app="msedge.exe", title=title)
        src.doc_key = app.capture_service.doc_key_for(src)
        return src

    @contextlib.contextmanager
    def _reading_page(self, app, title: str = "Some Document", hwnd: int = 55501):
        """把「当前阅读页面」设成指定页面：假 Win32，不读真实前台。"""
        with mock.patch("app.capture_service.w32", app.fake_tk.w32):
            app.capture_service.note_foreground(
                dict(DEFAULT_HEADLESS_FG, title=title, hwnd=hwnd))
        yield app.capture_service.last_source()

    @staticmethod
    def _select_on_page(app, sel, point=(10, 10)) -> None:
        """像真实捕获路径那样带**页面世代**投递一份选区（迟到事件判定的依据）。"""
        app._ui_q.put(("selection_ready", {
            "selection": sel, "point": point, "kind": "drag", "reason": "",
            "generation": app.capture_service.current_generation(),
            "page_epoch": app.capture_service.current_page_epoch(),
            "hwnd": sel.source.hwnd, "pid": sel.source.pid,
        }))
        pump_app(app)

    def test_probe_without_capture_service_keeps_whole_library(self):
        """没有任何来源信息的面板探针（旧替身）保持「全部词语」的历史行为。"""
        with temp_db() as db:
            bid = db.create_batch("旧的页", "url:https://old.example.com/a")
            db.add_entry(batch_id=bid, term="alpha")
            probe = PanelProbe(db=db)
            try:
                probe.panel.refresh_terms(force=True)
                self.assertEqual(probe.panel.shown_tag_count(), 1)
                self.assertEqual(probe.panel.visible_term_total(), 1)
            finally:
                probe.close()

    def test_unknown_page_shows_empty_list_not_the_whole_library(self):
        with self._open() as app:
            other = app.db.create_batch("上一页", "url:https://old.example.com/a")
            app.db.add_entry(batch_id=other, term="上一页的词")
            with self._reading_page(app):
                self.assertEqual(app.capture_service.current_page_scope(), (None, True),
                                 "新页面已知、但还没有主题")
                panel = app.reading_panel
                panel.refresh_terms(force=True)
                self.assertEqual(panel.shown_tag_count(), 0,
                                 "新页面必须显示空词表（不是全库）")
                self.assertEqual(panel.visible_term_total(), 0)
            self.assertEqual(app.db.count_entries(), 1, "别的页面的词只是不显示")

    def test_saved_page_shows_only_its_own_words(self):
        with self._open() as app:
            other = app.db.create_batch("别的页", "url:https://other.example.com/b")
            app.db.add_entry(batch_id=other, term="别的页的词")
            with self._reading_page(app) as src:
                entry_id, created = app.capture_service.manual_entry(
                    "这一页的词", source=src)
                self.assertTrue(created)
                panel = app.reading_panel
                panel.refresh_terms(force=True)
                shown = [str(card.cget("text")) for card in panel.tags_area.children]
                self.assertEqual(shown, ["这一页的词"])
                self.assertNotIn("别的页的词", shown)
                self.assertEqual(panel.visible_term_total(), 1)
                self.assertEqual(int(app.db.get_entry(entry_id)["batch_id"]),
                                 app.capture_service.current_page_scope()[0])

    def test_page_switch_and_selection_write_nothing(self):
        """划选 / 翻页只更新内存指针：不建主题、不落库、不写 settings。"""
        with self._open() as app:
            other = app.db.create_batch("上一页", "url:https://old.example.com/a")
            app.db.add_entry(batch_id=other, term="上一页的词")
            batches_before = len(app.db.list_batches())
            entries_before = app.db.count_entries()
            settings_before = app.db.all_settings()

            with self._reading_page(app):
                with mock.patch.object(app.config, "set",
                                       side_effect=AssertionError("不得写 settings")):
                    panel = app.reading_panel
                    show_selection(app, make_sel(term="新页面的词"))
                    panel.refresh_terms(force=True)
                    self.assertEqual(panel.shown_tag_count(), 0, "新页面空词表")
                    self.assertEqual(app.capture_service.current_page_scope(), (None, True))

            self.assertEqual(len(app.db.list_batches()), batches_before, "划选不得建主题")
            self.assertEqual(app.db.count_entries(), entries_before, "划选不得落库")
            self.assertEqual(app.db.all_settings(), settings_before, "不得写 settings")

    def test_page_switch_marks_main_window_to_follow_current_page(self):
        """换页面 → 主界面清掉浏览筛选；下次打开时列表呈新页面的词。"""
        with self._open() as app:
            main = app.main
            with self._reading_page(app, title="第一页") as src:
                first, _ = app.capture_service.manual_entry("第一页的词", source=src)
            first_topic = int(app.db.get_entry(first)["batch_id"])

            main._browse_batch_id = first_topic      # 用户之前点过这个主题浏览
            with self._reading_page(app, title="第二页", hwnd=60011) as src2:
                app.capture_service.manual_entry("第二页的词", source=src2)
            app._hide_overlays_for_foreground_change(app.capture_service.last_source(),
                                                     "window")
            self.assertIsNone(main._browse_batch_id, "换页面必须清掉手工浏览筛选")
            self.assertTrue(main._follow_pending, "主窗口收起时只记待办，不重建列表")

            app.open_main_window()                   # 用户打开主界面
            self.assertFalse(main._follow_pending, "打开时待办落地")
            shown = [str(row["term"]) for row in main._entry_rows]
            self.assertIn("第二页的词", shown)
            self.assertNotIn("第一页的词", shown, "列表跟当前页面，不跟旧页面")

    def test_visible_panel_keeps_its_content_across_page_switch(self):
        """A→B：可见浮窗**保持正在显示的内容** —— 换页不改主题/词表，划选才刷新。

        换页是用户翻页的动作，不是「要看新页面的词表」的指令：旧实现在换页时
        立刻改呈新页面的来源主题并退出详情页，浮窗于是在用户眼皮底下换了一屏。
        现在换页只交还手工浏览范围、收起主题选择，一个控件都不重建；页面范围
        要等用户**划选并记录**（``_record_snapshot_once`` → ``_refresh_panel_terms``）
        时才重新检测 —— 而页面身份本身在抬手时（``note_foreground``）就已采到。
        """
        from app.ui.reading_panel import PAGE_DETAIL, PAGE_LIST

        with self._open() as app:
            panel = app.reading_panel
            panel.show_dock()
            self.assertTrue(panel.toggle(), "先展开浮窗（用户看得见的那一个）")
            with self._reading_page(app, title="第一页") as src_a:
                first, _ = app.capture_service.manual_entry("第一页的词", source=src_a)
            topic_a = int(app.db.get_entry(first)["batch_id"])
            panel.refresh_terms(force=True)
            self.assertEqual([str(card.cget("text")) for card in panel.tags_area.children],
                             ["第一页的词"])

            # 用户正停在旧页的词详情，并且展开着主题选择
            self.assertTrue(panel.open_entry(first, expand=True))
            self.assertEqual(panel.page, PAGE_DETAIL)
            self.assertTrue(panel.toggle_topic_picker(True))
            batches_before = len(app.db.list_batches())
            geometry = list(panel.win.geometry_specs)
            maps = panel.win.events.count("deiconify")
            hides = panel.win.events.count("withdraw")
            visible_before, mode_before = panel.visible, panel.mode
            title_before = str(panel.term_title.cget("text"))
            body_before = panel.def_text.get("1.0", "end")

            # 切到 B 页（新页面，还没有保存过词）：前台变化事件到达
            with self._reading_page(app, title="第二页", hwnd=60011) as src_b:
                app._hide_overlays_for_foreground_change(src_b, "window")
                self.assertEqual(panel.page, PAGE_DETAIL,
                                 "换页不得打断用户正在看的词详情")
                self.assertEqual(panel.entry_id, first, "显示的仍是用户点开的那一条")
                self.assertEqual(str(panel.term_title.cget("text")), title_before)
                self.assertEqual(panel.def_text.get("1.0", "end"), body_before,
                                 "释义正文也保持原样")
                self.assertFalse(panel._topic_open, "瞬时控件：主题选择收起")
                self.assertEqual([str(card.cget("text")) for card in panel.tags_area.children],
                                 ["第一页的词"], "词表保持 A 页，不跟新页面")
                self.assertEqual(len(app.db.list_batches()), batches_before,
                                 "换页绝不建主题（落库只发生在记录之后）")

                # 用户在新页面上划第一个词并点「解释并记录」：**此刻**才重新检测页面
                # 真实链路：抬手时 capture_service.note_foreground 采到 B 页 →
                # 记录落库 → _record_snapshot_once → _refresh_panel_terms 才刷新词表
                sel_b = make_sel(term="第二页的词", hwnd=src_b.hwnd)
                sel_b.source = src_b          # 这一份选区的来源就是 B 页（真实捕获如此）
                self._select_on_page(app, sel_b)
                self.assertEqual([str(card.cget("text")) for card in panel.tags_area.children],
                                 ["第一页的词"], "只是划选：词表还是 A 页的")
                entry_b = app._record_snapshot_once(sel_b)
                self.assertIsNotNone(entry_b)
                topic_b = int(app.db.get_entry(entry_b)["batch_id"])
                self.assertNotEqual(topic_b, topic_a, "记下第一个词时才为 B 页建主题")
                self.assertEqual(panel.page_scope()[0], topic_b,
                                 "刷新时取的是**新页面**的范围")
                panel.show_list()
                self.assertEqual([str(card.cget("text")) for card in panel.tags_area.children],
                                 ["第二页的词"], "词表现在只有 B 页的词")
                self.assertEqual(panel.visible_term_total(), 1)

            # 回到 A 页：用户没划词 → 显示不动（不自动跳回 A 的词表）
            with self._reading_page(app, title="第一页"):
                app._hide_overlays_for_foreground_change(
                    app.capture_service.last_source(), "window")
            self.assertEqual([str(card.cget("text")) for card in panel.tags_area.children],
                             ["第二页的词"], "没有划选 / 记录就不刷新")
            self.assertEqual(list(panel.win.geometry_specs), geometry,
                             "换页 / 回到旧页都不得改窗口尺寸、位置")
            self.assertEqual((panel.visible, panel.mode), (visible_before, mode_before),
                             "换页不得改浮窗显隐形态")
            self.assertEqual(panel.win.events.count("deiconify"), maps,
                             "换页不得重新映射窗口")
            self.assertEqual(panel.win.events.count("withdraw"), hides,
                             "换页不得隐藏窗口")

    def test_late_foreground_event_keeps_the_new_selection_and_still_follows(self):
        """迟到的 A 页事件：不清 B 页的首选区，但视图仍跟**当前页**（B）。"""
        with self._open() as app:
            panel = app.reading_panel
            with self._reading_page(app, title="第一页") as src_a:
                app.capture_service.manual_entry("第一页的词", source=src_a)
            stale_epoch = app.capture_service.current_page_epoch()

            with self._reading_page(app, title="第二页", hwnd=60011):
                self._select_on_page(app, make_sel(term="新页面的词"))
            self.assertIsNotNone(app._current_selection, "前提：B 页已有首选区")

            app._hide_overlays_for_foreground_change(src_a, "window",
                                                     page_key=src_a.doc_key,
                                                     page_epoch=stale_epoch)
            self.assertIsNotNone(app._current_selection,
                                 "迟到的旧页事件绝不能清掉新页面的首选区")
            self.assertEqual(app._current_selection.term, "新页面的词")
            self.assertIsNotNone(panel.selection, "浮窗上的待处理词同样保留")
            self.assertIsNone(app.browse_scope(), "手工浏览筛选照旧清掉")
            batch_id, page_known = panel.page_scope()
            self.assertTrue(page_known)
            self.assertIsNone(batch_id, "视图跟当前页（B，还没有词），不是迟到的 A")
            self.assertEqual(panel.shown_tag_count(), 0, "B 页空词表")

    def test_self_foreground_never_switches_the_page_list(self):
        """自身窗口不是阅读来源：不产生换页事件，词表与来源主题保持不动。"""
        with self._open() as app:
            panel = app.reading_panel
            with self._reading_page(app, title="第一页") as src_a:
                first, _ = app.capture_service.manual_entry("第一页的词", source=src_a)
            topic_a = int(app.db.get_entry(first)["batch_id"])
            panel.refresh_terms(force=True)
            self.assertEqual(panel.visible_term_total(), 1)

            with mock.patch("app.capture_service.w32", app.fake_tk.w32):
                self.assertIsNone(app.capture_service.note_foreground(
                    {"hwnd": 999001, "pid": 111, "exe": "dict.exe", "app": "dict.exe",
                     "title": "探索词典", "is_self": True}),
                    "自身窗口不得成为「最近来源」")
            panel.follow_page_change()          # 即便被调用，也只跟可靠来源
            self.assertEqual([str(card.cget("text")) for card in panel.tags_area.children],
                             ["第一页的词"], "自身窗口不得把词表切到别处")
            self.assertEqual(panel.page_scope()[0], topic_a, "来源主题必须还是 A 页")

    def test_browsing_a_topic_does_not_change_the_save_target(self):
        """点选已保存主题只影响浏览；新词仍然写回**当前页面**自己的主题。"""
        with self._open() as app:
            with self._reading_page(app) as src:
                first, _ = app.capture_service.manual_entry("这一页的词", source=src)
                page_topic = int(app.db.get_entry(first)["batch_id"])
                other = app.db.create_batch("别的页", "url:https://other.example.com/b")

                main = app.main
                main._browse_batch_id = other          # 用户点选别的主题（纯内存）
                main.refresh_batches()
                main.refresh_entries()
                self.assertEqual(main.filter_batch_id(), other, "列表确实切到浏览的主题")

                second, _ = app.capture_service.manual_entry("第二个词", source=src)
                self.assertEqual(int(app.db.get_entry(second)["batch_id"]), page_topic,
                                 "浏览别的主题绝不改变新词的保存位置")
                self.assertEqual(app.db.count_entries(other), 0, "别的主题不得被写入")


class TestTopicChips(unittest.TestCase):
    """主题 chip 条：默认可选主题、进入标签看该主题的词、划选自动回到本页。

    用户本轮要求：「默认状态下可以选择不同的主题标签，进入主题标签后可以查看
    对应的关键词。用户开始刷选后自动打开对应的主题界面，如果是新的界面就创建
    一个新的主题标签界面。」——「新主题界面」= 标签条上的占位 chip，
    **不写库**：落库只发生在「解释并记录」。
    """

    def _reading_page(self, app, *args, **kwargs):
        """复用 :class:`TestPageScopedTermList` 的「当前阅读页面」（它要 self 兜住）。"""
        return TestPageScopedTermList._reading_page(self, app, *args, **kwargs)

    @staticmethod
    def _select_on_page(app, sel, point=(10, 10)) -> None:
        """复用真实的「带页面世代投递选区」路径（假 Win32，不读真实前台）。"""
        return TestPageScopedTermList._select_on_page(app, sel, point)

    @staticmethod
    def _open(**kwargs):
        return headless_app(overlays="panel", main_window="real", **kwargs)

    @staticmethod
    def _texts(panel) -> list:
        return [str(chip.cget("text")) for chip in panel._topic_chips]

    @staticmethod
    def _primary(chip) -> bool:
        """假 Tk 里的按钮只记 ``primary`` 这个选项，真实 FlatButton 记 ``_primary``。"""
        if hasattr(chip, "_primary"):
            return bool(chip._primary)
        return bool(getattr(chip, "primary", False))

    def _find(self, panel, text):
        for chip in panel._topic_chips:
            if str(chip.cget("text")) == text:
                return chip
        raise AssertionError(f"没有 {text!r} 这个 chip：{self._texts(panel)}")

    @staticmethod
    def _tags(panel) -> list:
        return [str(card.cget("text")) for card in panel.tags_area.children]

    def test_chips_show_the_page_topic_and_the_other_topics(self):
        with self._open() as app:
            other = app.db.create_batch("版本发布说明")
            app.db.add_entry(batch_id=other, term="beta")
            with self._reading_page(app) as src:
                app.capture_service.manual_entry("alpha", source=src)
                panel = app.reading_panel
                page_bid, known = panel.page_scope()
                self.assertTrue(known, "前提：面板认得出当前页面")
                name = str(app.db.get_batch(int(page_bid))["name"])
                panel.refresh_terms(force=True)
                self.assertEqual(self._texts(panel),
                                 [f"当前页 · {name}（1）", "版本发布说明（1）"],
                                 "标签条 = 本页主题 + 其它主题（同一个主题不重复）")
                self.assertTrue(self._primary(panel._topic_chips[0]), "跟随时本页主题高亮")
                self.assertFalse(self._primary(panel._topic_chips[1]))
                self.assertEqual(self._tags(panel), ["alpha"], "词表仍是本页的词")

    def test_empty_topics_are_left_out_of_the_strip(self):
        """0 条词的主题不进标签条：点进去只有空词表，那不是归纳。"""
        with self._open() as app:
            app.db.create_batch("空的主题")
            filled = app.db.create_batch("有词的主题")
            app.db.add_entry(batch_id=filled, term="alpha")
            with self._reading_page(app, title="当前页", hwnd=60041):
                panel = app.reading_panel
                panel.refresh_terms(force=True)
                self.assertEqual(self._texts(panel),
                                 ["当前页 · 新主题", "有词的主题（1）"])
                self.assertNotIn("空的主题（0）", self._texts(panel))
                self.assertNotIn("空的主题（0）",
                                 [text for _bid, text in panel.strip_topic_options()])
                self.assertIn("空的主题（0）",
                              [text for _bid, text in panel.topic_options()],
                              "内联清单是管理视图：空主题仍然列出来（能删能改名）")

    def test_new_page_shows_a_placeholder_chip_and_writes_nothing(self):
        with self._open() as app:
            old = app.db.create_batch("以前的页", "url:https://old.example.com/a")
            app.db.add_entry(batch_id=old, term="旧词")
            batches = len(app.db.list_batches())
            entries = app.db.count_entries()
            with self._reading_page(app, title="全新页", hwnd=60021):
                panel = app.reading_panel
                panel.refresh_terms(force=True)
                self.assertEqual(self._texts(panel), ["当前页 · 新主题", "以前的页（1）"],
                                 "新页面先给占位 chip（别的页面的主题只是可选，不算本页）")
                self.assertEqual(panel.shown_tag_count(), 0, "新页面是空词表")
                self.assertEqual(len(app.db.list_batches()), batches,
                                 "占位 chip 绝不建主题（写库只发生在「解释并记录」）")
                self.assertEqual(app.db.count_entries(), entries,
                                 "别的页面的词只是不显示，不动它")

    def test_clicking_a_topic_chip_browses_it_and_the_follow_chip_returns(self):
        with self._open() as app:
            other = app.db.create_batch("版本发布说明")
            app.db.add_entry(batch_id=other, term="beta")
            with self._reading_page(app) as src:
                app.capture_service.manual_entry("alpha", source=src)
                panel = app.reading_panel
                panel.refresh_terms(force=True)
                follow_text = self._texts(panel)[0]
                self._find(panel, "版本发布说明（1）").invoke()
                self.assertEqual(app.browse_scope(), int(other))
                self.assertEqual(self._tags(panel), ["beta"],
                                 "进入主题标签后看到该主题的关键词")
                self.assertTrue(self._primary(self._find(panel, "版本发布说明（1）")))
                self.assertFalse(self._primary(self._find(panel, follow_text)),
                                 "浏览历史主题时「当前页」chip 不再高亮")

                self._find(panel, follow_text).invoke()
                self.assertIsNone(app.browse_scope(), "点「当前页」回到跟随")
                self.assertEqual(self._tags(panel), ["alpha"], "词表回到本页的词")
                self.assertTrue(self._primary(self._find(panel, follow_text)))

    def test_overflow_topics_collapse_into_a_more_chip(self):
        with self._open() as app:
            for index in range(12):
                bid = app.db.create_batch(f"很长的主题名字第{index}号")
                app.db.add_entry(batch_id=bid, term=f"w{index}")
            with self._reading_page(app, title="当前页", hwnd=60031):
                panel = app.reading_panel
                panel.refresh_terms(force=True)
                texts = self._texts(panel)
                self.assertLessEqual(panel._topics_rows, max(1, int(geo.CHIP_ROWS)),
                                     "标签条最多 CHIP_ROWS 行（不抢词卡的地方）")
                self.assertTrue(texts and texts[-1].startswith("+")
                                and texts[-1].endswith(" 个主题"),
                                f"放不下的主题收进「+N 个主题」，实际 {texts[-1]!r}")
                self._find(panel, texts[-1]).invoke()
                self.assertTrue(panel._topic_open, "点「+N 个主题」展开完整清单")

    def test_selection_returns_to_the_page_topic_automatically(self):
        with self._open() as app:
            other = app.db.create_batch("版本发布说明")
            app.db.add_entry(batch_id=other, term="beta")
            with self._reading_page(app) as src:
                app.capture_service.manual_entry("alpha", source=src)
                panel = app.reading_panel
                app.set_browse_scope(other)
                panel.show_list(set_geometry=False)
                self.assertEqual(self._tags(panel), ["beta"], "前提：正在浏览别的主题")

                batches = len(app.db.list_batches())
                entries = app.db.count_entries()
                self._select_on_page(app, make_sel(term="新词"))
                self.assertIsNone(app.browse_scope(),
                                  "开始划选 = 看这一页：自动交还手工浏览范围")
                self.assertEqual(self._tags(panel), ["alpha"], "词表回到本页主题")
                self.assertEqual(len(app.db.list_batches()), batches,
                                 "划选本身不建主题（还没点「解释并记录」）")
                self.assertEqual(app.db.count_entries(), entries, "划选绝不写库")


class TestTagPaginationReachability(unittest.TestCase):
    """超过 MAX_TAGS 的标签必须能从浮窗访问（第 61 / 121 条）。"""

    @staticmethod
    def _seed(db, count: int) -> list[int]:
        bid = db.create_batch("大批次")
        ids = []
        for i in range(count):
            ids.append(int(db.add_entry(batch_id=bid, term=f"词{i:03d}")))
        return ids

    def test_61st_and_121st_terms_are_reachable(self):
        from app.ui.reading_panel import MAX_TAGS

        with temp_db() as db:
            ids = self._seed(db, 130)
            probe = PanelProbe(db=db)
            try:
                panel = probe.panel
                self.assertEqual(panel.shown_tag_count(), MAX_TAGS,
                                 "首页只渲染 MAX_TAGS 条（有界分批）")
                self.assertEqual(panel.visible_term_total(), 130, "必须显示真实总数")
                self.assertIn("显示更多", str(panel.btn_more.cget("text")))
                self.assertIn("已显示 60 / 共 130 条",
                              str(panel.more_hint.cget("text")))

                panel.show_more_terms()
                self.assertEqual(panel.shown_tag_count(), 120)
                self.assertIn("词060", [str(c.cget("text")) for c in panel.tags_area.children],
                              "第 61 条必须真的渲染成卡片")
                self.assertTrue(panel.open_entry(ids[60], expand=False),
                                "第 61 条详情可达")
                self.assertEqual(panel.entry_id, ids[60])
                panel.show_list()

                panel.show_more_terms()
                self.assertEqual(panel.shown_tag_count(), 130)
                self.assertIn("词120", [str(c.cget("text")) for c in panel.tags_area.children],
                              "第 121 条必须真的渲染成卡片")
                self.assertTrue(panel.open_entry(ids[120], expand=False))
                self.assertIn("词120", panel.term_title.cget("text"))
            finally:
                probe.close()

    def test_batch_change_resets_pagination(self):
        with temp_db() as db:
            self._seed(db, 70)
            probe = PanelProbe(db=db)
            try:
                panel = probe.panel
                panel.show_more_terms()
                self.assertEqual(panel.shown_tag_count(), 70)

                class _Svc:
                    def current_batch_id(self):
                        return 999

                probe.stub.capture_service = _Svc()
                panel.refresh_terms()
                self.assertEqual(panel.shown_tag_count(), 0,
                                 "换批次必须回到第一页（不能停在空的第二页）")
                self.assertEqual(panel.visible_term_total(), 0)
            finally:
                probe.close()


class TestGeometryAppliesSizeChanges(unittest.TestCase):
    """几何幂等的判据是**完整 geometry 串**，不是左上角。"""

    def test_dock_and_panel_sharing_topleft_still_resizes(self):
        """小方块 ↔ 面板共用左上角时，尺寸切换也必须真的落到 geometry。"""
        probe = PanelProbe()
        try:
            probe.panel.show_dock(explicit=True)
            self.assertEqual(probe.last_geometry(), "44x44+-56+518")
            # 面板**就在小方块的位置**（用户把窗口拖到那里 / 两种状态同坐标），
            # 此时从 44x44 切到 360x460 只比左上角是看不出来的 —— 必须比完整串。
            probe.panel._anchor = (-56, 518)
            probe.panel.mode = MODE_PANEL
            probe.panel._last_geometry = "44x44+-56+518"
            calls_before = len(probe.geometry_specs())
            probe.panel._show(MODE_PANEL)
            new_specs = probe.geometry_specs()[calls_before:]
            self.assertTrue(any(spec.startswith("360x460+") for spec in new_specs),
                            f"同坐标必须应用新宽高（不能停在 44x44）：{new_specs}")
            # 反向：360x460 → 44x44 也必须应用新尺寸
            probe.panel.mode = MODE_DOCK
            probe.panel._last_geometry = "360x460+-56+518"
            calls_before = len(probe.geometry_specs())
            probe.panel._show(MODE_DOCK)
            new_specs = probe.geometry_specs()[calls_before:]
            self.assertTrue(any(spec.startswith("44x44+") for spec in new_specs),
                            f"折叠回小方块同样必须应用 44x44：{new_specs}")
        finally:
            probe.close()

    def test_resize_at_same_coordinates_applies_new_size_once(self):
        probe = PanelProbe()
        try:
            probe.panel.show_dock()
            probe.panel.expand()
            specs_before = len(probe.geometry_specs())
            probe.panel._on_resize_start(FakeEvent(0, 0))
            probe.panel._on_resize_motion(FakeEvent(0, 50))     # 只改高度
            probe.panel._on_resize_end(FakeEvent(0, 50))
            new_specs = probe.geometry_specs()[specs_before:]
            self.assertTrue(new_specs, "同坐标改尺寸必须下发新的宽高")
            self.assertTrue(any("360x510" in spec for spec in new_specs),
                            f"新尺寸必须落到 geometry：{new_specs}")
            self.assertEqual(probe.last_geometry(), "360x510+-424+310")
            self.assertEqual(probe.panel.state.panel_rect[2:], (360, 510))

            # 值没变 → 一次 geometry 都不再调用
            again = len(probe.geometry_specs())
            probe.panel.show_list()
            self.assertEqual(len(probe.geometry_specs()), again,
                             "坐标与尺寸都没变时不得重复下发 geometry")
        finally:
            probe.close()

    def test_unchanged_show_does_no_geometry_call(self):
        probe = PanelProbe()
        try:
            probe.panel.show_dock(explicit=True)
            calls = len(probe.geometry_specs())
            probe.panel.show_dock(force=True)
            probe.panel.show_dock(force=True)
            self.assertEqual(len(probe.geometry_specs()), calls,
                             "小方块已可见时重复 show_dock 不得下发 geometry")
        finally:
            probe.close()


# ============================================================================
# A. 统一词条绑定：切词隔离（对话缓存 / 草稿 / 请求 token）+ 同词重开同步
# ============================================================================
class TestEntryBindingIsolation(unittest.TestCase):
    """审核发现的确定 bug 的回归。

    旧实现里 ``open_entry`` / ``show_recorded`` 换了 ``entry_id`` 却**不清**
    ``_chat_turns`` 缓存，而 ``_render_chat`` 只在 ``_chat_turns is None`` 时
    才重读 —— 于是 A 词的对话会显示在 B 词上，返回 A 也看不到迟到的回答。
    现在两条入口都走统一的 :meth:`ReadingPanel._bind_entry`。
    """

    @staticmethod
    def _seed(db):
        bid = db.create_batch("b")
        a = int(db.add_entry(batch_id=bid, term="alpha"))
        b = int(db.add_entry(batch_id=bid, term="beta"))
        db.add_chat_turn(entry_id=a, role="user", content="A 的问题")
        db.add_chat_turn(entry_id=a, role="assistant", content="A 的回答")
        db.add_chat_turn(entry_id=b, role="user", content="B 的问题")
        db.add_chat_turn(entry_id=b, role="assistant", content="B 的回答")
        return a, b

    def test_switching_entry_never_shows_previous_chat(self):
        with temp_db() as db:
            a, b = self._seed(db)
            probe = PanelProbe(db=db)
            try:
                panel = probe.panel
                panel.show_dock()
                self.assertTrue(panel.open_entry(a))
                self.assertIn("A 的回答", panel.chat_text.get("1.0", "end"))
                self.assertTrue(panel.open_entry(b))
                body = panel.chat_text.get("1.0", "end")
                self.assertIn("B 的回答", body, "B 词必须显示自己的历史")
                self.assertNotIn("A 的回答", body, "切到 B 后不得再显示 A 的对话")
                self.assertNotIn("A 的问题", body)
            finally:
                probe.close()

    def test_show_recorded_also_rebinds_chat_cache(self):
        with temp_db() as db:
            a, b = self._seed(db)
            probe = PanelProbe(db=db)
            try:
                panel = probe.panel
                panel.show_dock()
                panel.open_entry(a)
                panel.show_recorded(make_sel(term="beta"), b, request_token=1,
                                    status="pending")
                body = panel.chat_text.get("1.0", "end")
                self.assertIn("B 的回答", body)
                self.assertNotIn("A 的回答", body,
                                 "show_recorded 换词同样必须换掉 chat 缓存")
            finally:
                probe.close()

    def test_draft_is_never_sent_to_another_entry(self):
        with temp_db() as db:
            a, b = self._seed(db)
            probe = PanelProbe(db=db)
            try:
                panel = probe.panel
                panel.show_dock()
                panel.open_entry(a)
                panel.entry_input.insert(0, "A 的草稿")
                self.assertTrue(panel.open_entry(b))
                self.assertEqual(panel.current_question(), "",
                                 "切词必须清空输入框（草稿不得跟着换词）")
                self.assertEqual(probe.questions, [], "没有发送就没有任何请求")
                panel.entry_input.insert(0, "B 的问题")
                self.assertEqual(panel.send_question(), "sent")
                self.assertEqual(probe.questions, [(b, "B 的问题")],
                                 "发送的必须是 B 自己的输入")
            finally:
                probe.close()

    def test_same_entry_reopen_keeps_draft_and_syncs_real_inflight(self):
        with temp_db() as db:
            a, b = self._seed(db)
            probe = PanelProbe(db=db)
            try:
                panel = probe.panel
                panel.show_dock()
                panel.open_entry(a)
                panel.entry_input.insert(0, "还没发出去的草稿")
                panel.open_entry(a)                    # 同词重开：不清草稿
                self.assertEqual(panel.current_question(), "还没发出去的草稿")
                probe.inflight_entries.add(a)          # 宿主报告：A 真的还在回答
                panel.open_entry(a)
                self.assertTrue(panel._chat_busy, "同词重开必须同步真实在途状态")
                self.assertIn("正在回答…", panel.chat_text.get("1.0", "end"))
                self.assertEqual(panel.send_question(), "busy", "在途时不得重复发送")
                panel.open_entry(b)
                self.assertFalse(panel._chat_busy, "B 没有在途请求")
            finally:
                probe.close()

    def test_late_answer_of_a_is_visible_after_returning_to_a(self):
        """A 的回答在看 B 时到达：不刷新 B，返回 A 时可见（本地历史已落库）。"""
        with temp_db() as db:
            a, b = self._seed(db)
            probe = PanelProbe(db=db)
            try:
                panel = probe.panel
                panel.show_dock()
                panel.open_entry(a)
                panel.entry_input.insert(0, "问 A")
                self.assertEqual(panel.send_question(), "sent")
                token = panel._chat_request_token
                self.assertIsNotNone(token)

                panel.open_entry(b)                    # 用户切到 B
                self.assertFalse(panel.on_chat_result(token, a, "ok", "A 的迟到回答"),
                                 "别的词的迟到回答必须被拒绝")
                self.assertNotIn("A 的迟到回答", panel.chat_text.get("1.0", "end"))

                # ChatService 已按 A 落库；用户返回 A 时必须看到它
                db.add_chat_turn(entry_id=a, role="assistant", content="A 的迟到回答")
                probe.inflight_entries.discard(a)
                panel.open_entry(a)
                self.assertIn("A 的迟到回答", panel.chat_text.get("1.0", "end"),
                              "返回 A 必须看到迟到但已落库的回答")
                self.assertFalse(panel._chat_busy)
                self.assertEqual(panel.entry_id, a)
            finally:
                probe.close()

    def test_late_chat_result_never_re_expands(self):
        with temp_db() as db:
            a, _b = self._seed(db)
            probe = PanelProbe(db=db)
            try:
                panel = probe.panel
                panel.show_dock()
                panel.open_entry(a)
                panel.entry_input.insert(0, "问 A")
                panel.send_question()
                token = panel._chat_request_token
                panel.collapse_to_dock()
                maps = probe.deiconify_count()
                self.assertTrue(panel.on_chat_result(token, a, "ok", "回答"),
                                "自己的词：结果照常写进内存")
                self.assertEqual(panel.mode, MODE_DOCK, "迟到的追问回答不得重新展开面板")
                self.assertEqual(probe.deiconify_count(), maps)
            finally:
                probe.close()


# ============================================================================
# B. 详情页解释入口：**只在失败时出现的一个短「重试」**（无 Key 仍保存、重试不加词频）
# ============================================================================
class TestDetailExplainActions(unittest.TestCase):
    def _open(self, probe, db, entry_id):
        probe.panel.show_dock()
        probe.panel.open_entry(entry_id)
        return entry_id

    def test_detail_has_no_permanent_settings_or_reexplain_buttons(self):
        """常驻「设置 API / 重新解释」与选区上下文行都已删除；唯一入口按状态出现。"""
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = int(db.add_entry(batch_id=bid, term="alpha"))
            probe = PanelProbe(db=db)
            try:
                self._open(probe, db, eid)
                panel = probe.panel
                self.assertFalse(hasattr(panel, "btn_settings_api"),
                                 "常驻「设置 API」按钮必须随代码一起删掉")
                self.assertFalse(hasattr(panel, "_on_open_settings"))
                self.assertFalse(hasattr(panel, "quote_meta"),
                                 "选区上下文行（含控件与死引用）必须删掉")
                self.assertEqual(panel.status, "none",
                                 "库里 explain_status=none 必须原样保留，不得吞成空串")
                self.assertEqual(str(panel.btn_retry_explain.cget("text")), EXPLAIN_TEXT,
                                 "未解释的词条入口叫「解释」")
                self.assertTrue(getattr(panel, "_retry_packed", False),
                                "未解释时入口必须可见")
                self.assertEqual(probe.explain_settings_opened, 0)
            finally:
                probe.close()

    def test_failed_explain_shows_retry_row_then_hides_after_success(self):
        """失败 → 出现「重试」；解释成功 → 这一行整个消失。"""
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = int(db.add_entry(batch_id=bid, term="alpha"))
            db.update_entry(eid, explain_status="error", explain_error="连接超时")
            probe = PanelProbe(db=db)
            try:
                self._open(probe, db, eid)
                panel = probe.panel
                self.assertTrue(getattr(panel, "_retry_packed", False),
                                "解释失败后必须出现「重试」")
                self.assertEqual(probe.env.pack_kw(panel.retry_row).get("fill"), "x")

                probe.panel.status = "ok"
                probe.panel.result = make_explain_result("补上的释义")
                probe.panel._render_detail()
                self.assertFalse(getattr(panel, "_retry_packed", False),
                                 "成功之后重试行不得继续常驻")
            finally:
                probe.close()

    def test_retry_explain_uses_current_entry_only_and_never_records(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = int(db.add_entry(batch_id=bid, term="alpha"))
            probe = PanelProbe(db=db)
            try:
                self._open(probe, db, eid)
                self.assertTrue(probe.panel._on_retry_explain())
                self.assertEqual(probe.explain_retries, [eid],
                                 "重新解释必须针对当前词条")
                self.assertEqual(probe.actions, [],
                                 "重新解释绝不走「解释并记录」路径（不加词频）")
                self.assertEqual(probe.questions, [], "与追问无关，不得发请求")
            finally:
                probe.close()

    def test_retry_button_is_disabled_while_explaining(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = int(db.add_entry(batch_id=bid, term="alpha"))
            probe = PanelProbe(db=db)
            try:
                self._open(probe, db, eid)
                self.assertTrue(probe.panel.btn_retry_explain.is_enabled())
                self.assertTrue(probe.panel._on_retry_explain())
                self.assertFalse(probe.panel.btn_retry_explain.is_enabled(),
                                 "解释中必须禁用「重新解释」防重复")
                self.assertFalse(probe.panel._on_retry_explain())
                self.assertEqual(probe.explain_retries, [eid], "禁用期间不得再发请求")

                probe.explain_inflight.clear()          # 解释结束（成功 / 失败）
                probe.panel.show_result(eid, "error", None, "连接超时")
                self.assertTrue(probe.panel.btn_retry_explain.is_enabled(),
                                "解释结束后按钮必须恢复可用")
            finally:
                probe.close()

    def test_retry_failure_keeps_same_entry_and_reports_clearly(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = int(db.add_entry(batch_id=bid, term="alpha"))
            probe = PanelProbe(db=db)
            try:
                self._open(probe, db, eid)
                probe.retry_explain_result = False
                self.assertFalse(probe.panel._on_retry_explain())
                hint = str(probe.panel.hint_label.cget("text"))
                self.assertIn("主界面配置 API", hint)
                self.assertLessEqual(len(hint), 30, f"失败提示必须短：{hint}")
                self.assertEqual(probe.panel._hint_action, "main",
                                 "失败提示本身可点 → 直接打开主界面")
                self.assertEqual(probe.panel.entry_id, eid, "失败不得丢词条归属")
            finally:
                probe.close()

    def test_failure_text_is_short_and_points_to_the_retry_entry(self):
        """失败正文**只解释失败**：长操作说明与原始异常都不铺到浮窗。

        可执行的那一句在最底部短提示 / 唯一入口上；完整错误仍在库（主界面显示它）。
        """
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = int(db.add_entry(batch_id=bid, term="alpha"))
            raw = "连接超时\nTraceback (most recent call last):\n" + "x" * 400
            db.update_entry(eid, explain_status="error", explain_error=raw)
            probe = PanelProbe(db=db)
            try:
                self._open(probe, db, eid)
                panel = probe.panel
                body = panel.def_text.get("1.0", "end")
                self.assertIn("解释失败", body)
                self.assertIn("连接超时", body, "失败原因属于「只解释失败」的正文")
                self.assertNotIn("Traceback", body, "原始异常不得铺到浮窗")
                self.assertNotIn("主界面配置 API", body,
                                 "长操作说明不得铺到正文（它在最底部短提示里）")
                self.assertLessEqual(len(body.strip()), 60, f"正文必须短：{body!r}")
                self.assertEqual(str(panel.btn_retry_explain.cget("text")), RETRY_TEXT,
                                 "失败态的入口是「重试」")
                hint = str(panel.hint_label.cget("text"))
                self.assertIn(RETRY_TEXT, hint)
                self.assertLessEqual(len(hint), 20, f"失败提示必须短：{hint}")
                # 完整错误仍在库里（主界面显示的就是它）
                self.assertEqual(str(db.get_entry(eid)["explain_error"]), raw)
            finally:
                probe.close()


class TestDetailRetryOnRealAppChain(unittest.TestCase):
    """真实 App + 真实共享面板（假 Tk）：无 Key 先保存 → 配好 Key 重试同一条。"""

    @staticmethod
    def _open(**kwargs):
        return headless_app(overlays="panel", main_window="real", **kwargs)

    def test_no_key_saves_then_retry_explains_without_bumping_frequency(self):
        from tests.test_unified_action import (
            FakeClient, configure_key, make_sel, show_selection, wait_for,
        )

        with self._open() as app:
            panel = app.reading_panel
            show_selection(app, make_sel(term="卷积"))
            eid = panel._on_action()                    # 没有 Key：仍然保存
            self.assertIsNotNone(eid)
            self.assertEqual(app.db.count_entries(), 1)
            self.assertEqual(app.db.get_entry(eid)["repeat_count"], 1)
            self.assertEqual(panel.status, "pending_no_key")

            configure_key(app.config)                   # 用户去设置里配好 Key
            client = FakeClient("补上的释义")
            app.explain_service.make_client = lambda **kw: client
            self.assertTrue(panel._on_retry_explain())

            self.assertTrue(wait_for(
                app, lambda: app.db.get_entry(eid)["explain_status"] == "ok"),
                "重试必须写回同一条词条")
            # teardown 之前必须等 fake 解释 worker **真正退出**（不 sleep 猜测），
            # 否则关库之后还会有一条后台写库，日志里就会出现 ProgrammingError。
            self.assertTrue(wait_for(app, lambda: not app.explain_service.is_inflight(eid)),
                            "测试收尾前解释 worker 必须已经结束")
            row = app.db.get_entry(eid)
            self.assertEqual(app.db.count_entries(), 1, "重试绝不重复记词")
            self.assertEqual(row["repeat_count"], 1, "重试绝不加词频")
            self.assertEqual(row["one_line"], "补上的释义")
            self.assertIn("补上的释义", panel.def_text.get("1.0", "end"))

    def test_no_key_save_then_configured_key_button_really_explains(self):
        """无 Key 保存 → 配好（假）Key → 详情页「解释」按钮真的发起解释、不加词频。

        走的是产品按钮（``btn_retry_explain.invoke()``）→ 真实
        ``App.retry_explain_entry`` → 假 client（零联网、零真实 Key）。
        """
        from tests.test_unified_action import (
            FakeClient, configure_key, make_sel, show_selection, wait_for,
        )

        with self._open() as app:
            panel = app.reading_panel
            show_selection(app, make_sel(term="卷积", context="卷积的上下文"))
            eid = panel._on_action()                    # 没有 Key：仍然保存
            self.assertIsNotNone(eid)
            self.assertEqual(panel.status, "pending_no_key")
            self.assertEqual(app.db.get_entry(eid)["repeat_count"], 1)

            # 未解释词条：入口显示「解释」且可用（不是失败态的「重试」）
            self.assertEqual(str(panel.btn_retry_explain.cget("text")), EXPLAIN_TEXT)
            self.assertTrue(panel.btn_retry_explain.is_enabled())
            self.assertTrue(getattr(panel, "_retry_packed", False))

            configure_key(app.config)                   # 用户在设置里配好（假）Key
            client = FakeClient("补上的释义")
            app.explain_service.make_client = lambda **kw: client

            self.assertTrue(panel.btn_retry_explain.invoke(),
                            "配好 Key 后「解释」按钮必须真的发起解释")
            self.assertTrue(wait_for(
                app, lambda: app.db.get_entry(eid)["explain_status"] == "ok"),
                "解释结果必须写回同一条词条")
            self.assertTrue(wait_for(app, lambda: not app.explain_service.is_inflight(eid)))

            self.assertEqual(client.calls, [("卷积", "卷积的上下文")],
                             "提交的就是这条词的 term / context")
            self.assertEqual(app.db.count_entries(), 1, "解释入口绝不重复记词")
            self.assertEqual(app.db.get_entry(eid)["repeat_count"], 1, "绝不加词频")
            self.assertIn("补上的释义", panel.def_text.get("1.0", "end"))

    def test_selection_held_for_a_is_never_submitted_for_b(self):
        """面板还攥着 A 的选区时打开库里 B：B 的入口提交的必须是 B 的 term/context。

        ``App._selection_for_entry`` 优先用窗口手里那份快照 —— 如果 ``open_entry(B)``
        不丢掉 A 的选区，A 就会被当成 B 送出去；同一个契约还要覆盖**同词不同 context**
        （term 相同、语境是旧的），两份内容都一致才允许复用手里那份。这里用真实面板 +
        真实 App 钉死契约。
        """
        from tests.test_unified_action import (
            FakeClient, configure_key, make_sel, show_selection, wait_for,
        )

        with self._open() as app:
            configure_key(app.config)
            panel = app.reading_panel
            show_selection(app, make_sel(term="alpha", context="alpha 的上下文"))
            self.assertIsNotNone(panel.selection, "前提：面板手里握着 A 的选区快照")

            bid = int(app.db.create_batch("B 组"))
            eid_b = int(app.db.add_entry(batch_id=bid, term="beta",
                                         context="beta 的上下文"))
            before_repeat = int(app.db.get_entry(eid_b)["repeat_count"])

            panel.show_dock()
            self.assertTrue(panel.open_entry(eid_b))
            self.assertIsNone(panel.selection,
                              "打开已保存词条 B 后不得再攥着 A 的选区")
            snapshot = app._selection_for_entry(eid_b)
            self.assertEqual((snapshot.term, snapshot.context),
                             ("beta", "beta 的上下文"),
                             "快照必须按 B 的库内容重建，不能返回 A")

            client = FakeClient("beta 的释义")
            app.explain_service.make_client = lambda **kw: client
            self.assertTrue(panel.btn_retry_explain.invoke(),
                            "B 的解释入口必须真的发起请求")
            self.assertTrue(wait_for(
                app, lambda: app.db.get_entry(eid_b)["explain_status"] == "ok"))
            self.assertTrue(wait_for(app, lambda: not app.explain_service.is_inflight(eid_b)))

            self.assertEqual(client.calls, [("beta", "beta 的上下文")],
                             "提交的 term/context 必须是 B 的，绝不能是 A")
            self.assertNotIn("alpha", panel.def_text.get("1.0", "end"),
                             "B 的详情里不得出现 A 的内容")
            self.assertEqual(app.db.get_entry(eid_b)["repeat_count"], before_repeat,
                             "解释入口绝不加词频")
            self.assertEqual(app.db.count_entries(), 1, "也绝不重复记词")

            # ---- 同词不同 context：面板手里 term 与库里相同，context 已经不是库里的 ----
            # 同一个隔离契约的第二种形态：只比 entry_id、或只比 term，都会把面板
            # 攥着的**旧语境**当成这条词的内容复用。必须 term + context 同时一致。
            eid_c = int(app.db.add_entry(batch_id=bid, term="alpha",
                                         context="库里 alpha 的新语境"))
            stale = make_sel(term="alpha", context="面板手里的旧语境")
            panel.show_recorded(stale, eid_c, status="pending_no_key",
                                message="已记录，待解释。", explicit=False)
            self.assertEqual(panel.entry_id, eid_c)
            self.assertEqual(str(panel.selection.context), "面板手里的旧语境",
                             "前提：窗口绑定的就是这条词条，手里那份 term 相同但语境是旧的")

            rebuilt = app._selection_for_entry(eid_c)
            self.assertEqual((rebuilt.term, rebuilt.context),
                             ("alpha", "库里 alpha 的新语境"),
                             "同词但 context 与库不一致：必须按库重建，不得复用旧快照")

            client_c = FakeClient("alpha 的新释义")
            app.explain_service.make_client = lambda **kw: client_c
            self.assertTrue(panel.btn_retry_explain.invoke(),
                            "同名词条的解释入口必须真的发起请求")
            self.assertTrue(wait_for(
                app, lambda: app.db.get_entry(eid_c)["explain_status"] == "ok"))
            self.assertTrue(wait_for(app, lambda: not app.explain_service.is_inflight(eid_c)))

            self.assertEqual(client_c.calls, [("alpha", "库里 alpha 的新语境")],
                             "提交的 context 必须是库里的，绝不能是面板手里的旧语境")
            self.assertEqual(str(panel.selection.context), "库里 alpha 的新语境",
                             "重试后窗口绑定的快照也必须与库里的完整内容一致")
            self.assertEqual(app.db.get_entry(eid_c)["repeat_count"], 1,
                             "同词不同境走解释入口同样不加词频")

    def test_retry_button_disabled_while_inflight_on_real_chain(self):
        import threading

        from tests.test_unified_action import (
            FakeClient, configure_key, make_sel, show_selection, wait_for,
        )

        with self._open() as app:
            configure_key(app.config)
            app.explain_service.make_client = lambda **kw: FakeClient("第一次的释义")
            panel = app.reading_panel
            show_selection(app, make_sel(term="卷积"))
            eid = panel._on_action()
            self.assertTrue(wait_for(
                app, lambda: app.db.get_entry(eid)["explain_status"] == "ok"))
            self.assertTrue(wait_for(app, lambda: not app.explain_service.is_inflight(eid)))

            gate = threading.Event()

            class SlowClient(FakeClient):
                def explain(self, term, context=""):
                    gate.wait(5)
                    return super().explain(term, context)

            app.explain_service.make_client = lambda **kw: SlowClient("第二次的释义")
            self.assertTrue(panel._on_retry_explain())
            self.assertFalse(panel.btn_retry_explain.is_enabled(),
                             "解释在途时按钮必须禁用")
            self.assertFalse(panel._on_retry_explain(), "禁用期间不得再发一次请求")
            gate.set()
            self.assertTrue(wait_for(
                app, lambda: app.db.get_entry(eid)["one_line"] == "第二次的释义"))
            self.assertTrue(wait_for(app, lambda: not app.explain_service.is_inflight(eid)),
                            "测试收尾前解释 worker 必须已经结束")
            self.assertTrue(panel.btn_retry_explain.is_enabled())


# ============================================================================
# C. 小尺寸可用性：底部优先布局（纯结构验证，不创建任何真实 Tk 窗口）
# ============================================================================
class TestDetailLayoutSmallPanel(unittest.TestCase):
    """300x320 或长标题下，底部输入行/关键操作必须优先保留。"""

    def test_bottom_rows_are_packed_first_with_bottom_side(self):
        probe = PanelProbe()
        try:
            panel, env = probe.panel, probe.env
            i_hint = env.pack_index(panel.hint_label)
            i_input = env.pack_index(panel.input_row)
            i_def = env.pack_index(panel.def_area.frame)
            i_chat = env.pack_index(panel.chat_area.frame)
            for name, index in (("提示行", i_hint), ("输入行", i_input),
                                ("释义区", i_def), ("追问区", i_chat)):
                self.assertGreaterEqual(index, 0, f"{name} 必须被 pack 进详情页")
            self.assertEqual(env.pack_kw(panel.input_row).get("side"), "bottom")
            self.assertEqual(env.pack_kw(panel.hint_label).get("side"), "bottom")
            self.assertLess(i_hint, i_input, "提示行在输入行下方（先 pack 更靠底）")
            self.assertLess(i_input, i_def, "底部输入行必须先于可滚动释义区拿到空间")
            self.assertLess(i_input, i_chat, "底部输入行必须先于可滚动追问区拿到空间")
            self.assertLess(i_chat, i_def,
                            "追问区必须**先于**释义区拿到空间：旧布局把追问区放在最后，"
                            "pack 空间不足时把它压成 0 高度（用户看不到自己的提问与回答）")
            self.assertEqual(env.pack_kw(panel.chat_area.frame).get("side"), "bottom",
                             "追问区与输入行同侧（贴着底部），高度由产品显式分配")
            self.assertTrue(env.pack_kw(panel.def_area.frame).get("expand"),
                            "估算误差由可滚动的释义区吸收（expand）")
            self.assertFalse(env.pack_kw(panel.chat_area.frame).get("expand"),
                             "追问区高度显式给定，绝不参与压缩")
            # 列表页的「当前词 + 解释并记录」同样先 side="bottom" 预留位置
            self.assertEqual(env.pack_kw(panel.action_area).get("side"), "bottom")
            self.assertLess(env.pack_index(panel.action_area),
                            env.pack_index(panel.tags_area.frame),
                            "底部操作区必须先于可滚动词卡区拿到空间")
        finally:
            probe.close()

    def test_retry_row_only_exists_on_failure_and_sits_above_the_scroll_area(self):
        """解释入口按状态出现 / 隐藏：未解释「解释」、失败「重试」、有释义隐藏。"""
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = int(db.add_entry(batch_id=bid, term="alpha"))
            probe = PanelProbe(db=db)
            try:
                panel, env = probe.panel, probe.env
                panel.show_dock()
                panel.open_entry(eid)
                self.assertGreaterEqual(env.pack_index(panel.retry_row), 0,
                                        "未解释的词条必须有「解释」入口")
                self.assertEqual(str(panel.btn_retry_explain.cget("text")), EXPLAIN_TEXT)
                panel.status = "ok"
                panel._render_detail()
                self.assertFalse(getattr(panel, "_retry_packed", False),
                                 "已有释义时这一行必须隐藏（旧版常驻行已删除）")
                panel.status = "error"
                panel._render_detail()
                self.assertTrue(getattr(panel, "_retry_packed", False),
                                "失败后「重试」必须出现")
                self.assertEqual(str(panel.btn_retry_explain.cget("text")), RETRY_TEXT)
                last_pack = [kw for w, kw in env.pack_calls if w is panel.retry_row][-1]
                self.assertIs(last_pack.get("before"), panel.def_area.frame,
                              "「重试」必须插在滚动区之前（小窗里不会被挤掉）")
            finally:
                probe.close()

    def test_fixed_chrome_fits_the_minimum_panel_height(self):
        from app.ui import theme

        line = abs(theme.device_px(9)) * 1.6        # 正文行高（9pt）
        small = abs(theme.device_px(7)) * 1.6       # 辅助行高（7pt）
        chrome = (line * 1.4                        # 顶栏（主界面 + 标题 + 折叠/×）
                  + line * 1.2                      # 返回行
                  + line * 1.2                      # 失败时才出现的「重试」行
                  + abs(theme.device_px(14)) * 1.3  # 词条标题（已限 1 行）
                  + small * 2                       # 来源（限 1 行）+ 计数
                  + line * 1.2                      # 追问表头
                  + line * 1.6                      # 输入行
                  + small)                          # 提示行
        self.assertLess(
            chrome, theme.px(geo.PANEL_MIN_H) - theme.px(60),
            "固定区（含被限高的标题/来源）必须在小尺寸面板里放得下，并给滚动区留空间")

    def test_long_title_and_source_are_clipped(self):
        from app.ui.reading_panel import SOURCE_MAX_CHARS, TITLE_MAX_CHARS

        with temp_db() as db:
            bid = db.create_batch("b")
            long_term = "超长词条名称" * 30
            eid = int(db.add_entry(batch_id=bid, term=long_term,
                                   source_title="很长的来源标题" * 20,
                                   source_url="https://a.example.com/" + "x" * 200))
            probe = PanelProbe(db=db)
            try:
                probe.panel.show_dock()
                probe.panel.open_entry(eid)
                title = str(probe.panel.term_title.cget("text"))
                source = str(probe.panel.detail_source.cget("text"))
                self.assertLessEqual(len(title), TITLE_MAX_CHARS, "标题必须限字数")
                self.assertTrue(title.endswith("…"))
                for line in source.split("\n"):
                    self.assertLessEqual(len(line), SOURCE_MAX_CHARS,
                                         "来源 / URL 每行都必须限字数")
            finally:
                probe.close()

    def test_clip_text_helper(self):
        from app.ui.reading_panel import clip_text

        self.assertEqual(clip_text("卷积", 10), "卷积")
        self.assertEqual(clip_text("", 10), "")
        self.assertEqual(clip_text("abcde", 5), "abcde")
        self.assertEqual(clip_text("abcdef", 5), "abcd…")
        self.assertEqual(len(clip_text("x" * 500, 40)), 40)

    def test_long_term_is_readable_in_the_scrollable_body(self):
        """详情标题被限 40 字后，**完整词语必须能在可滚动正文里查到**。

        真实 ``ReadingPanel`` + 假 Tk 控件 + 临时库：一个真实窗口都不建、不联网。
        标题标签受固定区高度限制（小面板下不能被长词撑爆），正文在可滚动区里，
        因此长词条把完整 term 前置到正文是唯一「一定能看到完整词」的位置。
        """
        from app.ui.reading_panel import TITLE_MAX_CHARS

        with temp_db() as db:
            bid = db.create_batch("b")
            long_term = "超长词条名称" * 30          # 180 字，必然超过标题上限
            eid = int(db.add_entry(batch_id=bid, term=long_term))
            db.update_entry(eid, explain_status="ok", one_line="一句话解释",
                            detail="详细说明", examples='["例一"]')
            probe = PanelProbe(db=db)
            try:
                probe.panel.show_dock()
                self.assertTrue(probe.panel.open_entry(eid))
                title = str(probe.panel.term_title.cget("text"))
                self.assertLessEqual(len(title), TITLE_MAX_CHARS, "标题仍必须限字数")
                self.assertTrue(title.endswith("…"))
                self.assertNotIn(long_term, title)
                body = probe.panel.def_text.get("1.0", "end")
                self.assertIn(long_term, body,
                              "标题截断后，完整词语必须在详情可滚动正文里完整可查")
                self.assertIn("一句话解释", body)
                self.assertEqual(probe.questions, [], "读本地详情绝不联网")
                self.assertEqual(probe.actions, [], "也不得走「解释并记录」")
                self.assertEqual(db.count_entries(), 1, "打开详情不得写库")
            finally:
                probe.close()

    def test_layout_priority_survives_resize_to_minimum(self):
        probe = PanelProbe()
        try:
            panel, env = probe.panel, probe.env
            panel.show_dock()
            panel.expand()
            panel._on_resize_start(FakeEvent(0, 0))
            panel._on_resize_motion(FakeEvent(-9999, -9999))
            panel._on_resize_end(FakeEvent(-9999, -9999))
            self.assertEqual(panel.state.panel_rect[2:], (300, 320),
                             "缩到最小 300x320 仍然要保持底部优先布局")
            self.assertEqual(env.pack_kw(panel.input_row).get("side"), "bottom")
            self.assertLess(env.pack_index(panel.input_row),
                            env.pack_index(panel.def_area.frame))
        finally:
            probe.close()


class TestDpiScalingOnce(unittest.TestCase):
    """DPI 只缩放一次，字号语义是 **pt**：9pt 正文在 96/120/144/192 DPI = 12/15/18/24px。

    可读正文标准是 **9pt（96 DPI 下 12px）**，不是 9px：历史上把 pt 参数当像素用，
    导致全站字偏小，这里用显式期望值把正确的 pt → px 换算钉死。
    """

    def setUp(self):
        from app.ui import theme

        self.theme = theme
        self._saved = theme.scale()

    def tearDown(self):
        self.theme._scale = self._saved

    def _at(self, dpi: int):
        theme = self.theme
        theme._scale = max(1.0, min(2.0, dpi / 96.0))
        return theme

    def test_px_and_font_share_one_scale(self):
        from app.ui import panel_geometry as geo

        for dpi in (96, 120, 144, 192):
            theme = self._at(dpi)
            ratio = dpi / 96.0
            self.assertEqual(theme.px(geo.DOCK_SIZE),
                             max(1, round(geo.DOCK_SIZE * ratio)),
                             f"{dpi} DPI：小方块必须按 DPI 放大一次")
            self.assertEqual(theme.px(geo.PANEL_W),
                             max(1, round(geo.PANEL_W * ratio)))
            self.assertEqual(theme.px(geo.PANEL_H),
                             max(1, round(geo.PANEL_H * ratio)))

    def test_font_is_device_pixels_not_points(self):
        """字号必须是**负的设备像素** —— 否则 Tk 会再乘一次 ``tk scaling``。"""
        for dpi in (96, 120, 144, 192):
            theme = self._at(dpi)
            self.assertEqual(theme.font(9)[1], theme.device_px(9))
            self.assertLess(theme.font(9)[1], 0, "正数 = pt，会被 Tk 二次缩放")
            self.assertEqual(abs(theme.font(9)[1]),
                             max(1, round(9 * 96 / 72 * dpi / 96)),
                             "字号必须按 pt → px（96/72）换算后再乘一次 DPI")
            self.assertEqual(abs(theme.serif(12)[1]),
                             max(1, round(12 * 96 / 72 * dpi / 96)))
            self.assertEqual(abs(theme.tracking_label(7)[1]),
                             max(1, round(7 * 96 / 72 * dpi / 96)))
            self.assertEqual(theme.font(9, bold=True)[1], theme.device_px(9),
                             "粗体不能改变量纲")

    def test_body_text_is_twelve_px_at_96_dpi_not_nine(self):
        """9pt 正文 = 96/120/144/192 DPI 下 12/15/18/24px；7pt 辅助至少 9px。

        历史错误：把 pt 参数当像素用，9pt 正文只有 9px —— 测试**不允许**再拿
        9px 当可读正文标准。
        """
        expected = {96: 12, 120: 15, 144: 18, 192: 24}
        for dpi, want in expected.items():
            theme = self._at(dpi)
            self.assertEqual(abs(theme.device_px(9)), want,
                             f"{dpi} DPI：9pt 正文必须是 {want}px，不是 9px")
            self.assertGreaterEqual(abs(theme.device_px(7)), 9,
                                    f"{dpi} DPI：7pt 辅助文字不得小于 9px")
            self.assertGreater(abs(theme.device_px(9)), abs(theme.device_px(7)),
                               "正文必须比辅助字号大")
        self.assertEqual(self._at(96).font(9)[1], -12, "9pt 正文 = -12px（设备像素）")

    def test_text_grows_with_the_window_and_does_not_overflow(self):
        from app.ui import panel_geometry as geo

        base_px = abs(self._at(96).device_px(9))
        self.assertEqual(base_px, 12, "96 DPI 下 9pt 正文 = 12px")
        for dpi in (120, 144, 192):
            theme = self._at(dpi)
            # 与窗口同倍率（允许取整误差）：字不会相对窗口变小
            grew = abs(theme.device_px(9)) / base_px * 100
            self.assertAlmostEqual(grew, dpi / 96 * 100, delta=2,
                                   msg=f"{dpi} DPI 的字号倍率必须与窗口一致")
            # 正文（9pt = 12px@96，行高按 1.6 估）必须放得下 44 小方块与 360x460 面板
            line = abs(theme.device_px(9)) * 1.6
            self.assertLess(line, theme.px(geo.DOCK_SIZE),
                            f"{dpi} DPI：正文行高不该大过小方块")
            rows = theme.px(geo.PANEL_H) / line
            self.assertGreater(rows, 12, f"{dpi} DPI：面板可读行数过少（溢出风险）")
        self.assertEqual(self._at(96).device_px(1), -1, "最小字号不得为 0")


# ============================================================================
# A. 右上角「×」：真实假 Tk 按钮 → close_by_user（收起 + 后台继续，绝不 quit）
# ============================================================================
class TestTopbarCloseButton(unittest.TestCase):
    """顶栏必须有**看得见、点得到**的关闭入口，而不是只有一个方法。"""

    def test_close_button_is_rightmost_and_click_closes_panel(self):
        probe = PanelProbe()
        try:
            panel = probe.panel
            close_btn = probe.env.find_button(CLOSE_TEXT)
            self.assertIsNotNone(close_btn, "顶栏必须有「×」按钮")
            self.assertEqual(close_btn.pack_kw.get("side"), "right")
            self.assertIsNotNone(probe.env.find_button("主界面"), "主界面入口保留")
            self.assertIsNotNone(probe.env.find_button("折叠"), "折叠入口保留")
            self.assertIsNone(probe.env.find_button("菜单"), "顶栏不再有菜单入口")

            panel.show_dock(explicit=True)
            panel.expand(explicit=True)
            self.assertTrue(panel.is_open())
            maps = probe.deiconify_count()

            # 点的是**真实假 Tk 按钮**（走绑定的 command），不是直接调方法
            self.assertTrue(close_btn.invoke())
            self.assertEqual(panel.mode_name(), MODE_HIDDEN)
            self.assertFalse(panel.visible)
            self.assertTrue(panel.closed_by_user, "× 必须留下 closed_by_user 标记")
            self.assertEqual(probe.deiconify_count(), maps, "关闭只 withdraw，不 remap")
            self.assertTrue(probe.withdraw_count() >= 1)

            # 显式找回仍然有效（小方块），而不是被永久锁死
            self.assertTrue(panel.restore_dock(explicit=True))
            self.assertEqual(panel.mode_name(), MODE_DOCK)
            self.assertFalse(panel.closed_by_user)
        finally:
            probe.close()

    def test_closed_panel_never_revived_by_selection_result_or_polling(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = int(db.add_entry(batch_id=bid, term="alpha"))
            probe = PanelProbe(db=db)
            try:
                panel = probe.panel
                panel.show_dock(explicit=True)
                panel.show_recorded(make_sel(term="alpha"), eid, request_token=4,
                                    status="pending", explicit=True)
                self.assertTrue(probe.env.find_button(CLOSE_TEXT).invoke())
                maps = probe.deiconify_count()
                withdraws = probe.withdraw_count()

                # ① 新划选
                self.assertFalse(panel.show_selection(make_sel(term="新词"), 11, (10, 10)))
                # ② 迟到的匹配结果（内容写进内存，但不重开）
                self.assertFalse(panel.show_result(eid, "ok", make_explain_result("迟到释义"),
                                                   request_token=4))
                # ③ 门控轮询（allow → hard → allow）与 settle
                probe.gate.mode = "hard"
                self.assertFalse(panel.settle_after_gate())
                probe.gate.mode = "allow"
                self.assertFalse(panel.settle_after_gate())

                self.assertEqual(panel.mode_name(), MODE_HIDDEN)
                self.assertFalse(panel.visible, "关闭后任何自动路径都不得复活面板")
                self.assertEqual(probe.deiconify_count(), maps, "零 deiconify")
                self.assertEqual(probe.withdraw_count(), withdraws, "已经隐藏就零 withdraw")
                self.assertFalse(panel.restore(), "用户关闭过：restore 也不找回")

                # 用户显式找回才恢复
                self.assertTrue(panel.restore_dock(explicit=True))
                self.assertTrue(panel.visible)
            finally:
                probe.close()

    def test_close_button_never_quits_and_background_write_continues(self):
        with headless_app(overlays="panel", main_window="real") as app:
            from tests.test_unified_action import FakeClient, configure_key, wait_for

            configure_key(app.config)
            app.explain_service.make_client = lambda **kw: FakeClient("后台完成的释义")
            panel = app.reading_panel
            show_selection(app, make_sel(term="卷积"))
            panel._on_action()
            eid = panel.entry_id
            self.assertIsNotNone(eid)
            maps = app.fake_tk.deiconify_count()

            self.assertTrue(panel.btn_close.invoke(), "点 × 必须真的收起面板")
            self.assertTrue(panel.closed_by_user)
            self.assertFalse(app._closing, "× 只收起面板，绝不退出程序")

            self.assertTrue(wait_for(
                app, lambda: app.db.get_entry(eid)["explain_status"] == "ok"),
                "后台解释必须继续写库")
            self.assertTrue(wait_for(
                app, lambda: not app.explain_service.is_inflight(eid)))
            self.assertEqual(panel.mode_name(), MODE_HIDDEN)
            self.assertEqual(app.fake_tk.deiconify_count(), maps)

            # 进 / 出游戏的门控轮询同样不得复活
            app.gate.set_game_mode(True)
            app._apply_gate(force=True)
            app.gate.set_game_mode(False)
            app._apply_gate(force=True)
            self.assertFalse(panel.visible)
            self.assertEqual(app.fake_tk.deiconify_count(), maps)

            # 用户显式找回：只给小方块
            self.assertTrue(app.recover_panel())
            self.assertEqual(panel.mode_name(), MODE_DOCK)


# ============================================================================
# C. 后台更新只原地写内容；结果 / 追问在硬阻断下立即收起（零 remap）
# ============================================================================
class TestBackgroundUpdatesNeverResurrect(unittest.TestCase):
    def test_background_show_recorded_updates_in_place_without_expanding(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = int(db.add_entry(batch_id=bid, term="alpha"))
            probe = PanelProbe(db=db)
            try:
                panel = probe.panel
                panel.show_dock(explicit=True)
                maps = probe.deiconify_count()
                # 后台（非显式）更新：小方块保持小方块，只换内容
                self.assertTrue(panel.show_recorded(make_sel(term="alpha"), eid,
                                                    request_token=1, status="pending"))
                self.assertEqual(panel.mode_name(), MODE_DOCK,
                                 "后台更新不得把 dock 展开成面板")
                self.assertEqual(panel.entry_id, eid, "内容仍要绑到这条词条")
                self.assertEqual(probe.deiconify_count(), maps)

                # 用户显式动作才允许展开
                self.assertTrue(panel.show_recorded(make_sel(term="alpha"), eid,
                                                    request_token=1, status="pending",
                                                    explicit=True))
                self.assertEqual(panel.mode_name(), MODE_PANEL)
            finally:
                probe.close()

    def test_background_show_recorded_never_revives_closed_panel(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = int(db.add_entry(batch_id=bid, term="alpha"))
            probe = PanelProbe(db=db)
            try:
                panel = probe.panel
                panel.show_dock(explicit=True)
                panel.expand(explicit=True)
                panel.close_by_user()
                maps = probe.deiconify_count()
                self.assertFalse(panel.show_recorded(make_sel(term="alpha"), eid,
                                                     request_token=2, status="ok"),
                                 "隐藏 / 关闭状态下后台更新不显示")
                self.assertFalse(panel.visible)
                self.assertTrue(panel.closed_by_user, "后台更新不得清掉关闭标记")
                self.assertEqual(panel.mode_name(), MODE_HIDDEN)
                self.assertEqual(probe.deiconify_count(), maps)
            finally:
                probe.close()

    def test_show_result_under_hard_gate_hides_immediately_and_soft_keeps(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = int(db.add_entry(batch_id=bid, term="alpha"))
            probe = PanelProbe(db=db)
            try:
                panel = probe.panel
                panel.show_dock(explicit=True)
                panel.show_recorded(make_sel(term="alpha"), eid, request_token=7,
                                    status="pending", explicit=True)
                self.assertTrue(panel.is_open())
                maps = probe.deiconify_count()

                # 软受限：保留已显示面板，只更新内容
                probe.gate.mode = "soft"
                self.assertTrue(panel.show_result(eid, "ok", make_explain_result("软受限释义"),
                                                  request_token=7))
                self.assertTrue(panel.visible, "软受限不得隐藏面板")

                # 硬阻断（结果回来前用户切进游戏）：立即 hide，零 deiconify
                withdraws = probe.withdraw_count()
                probe.gate.mode = "hard"
                self.assertFalse(panel.show_result(eid, "ok", make_explain_result("游戏里的释义"),
                                                   request_token=7))
                self.assertFalse(panel.visible, "硬阻断下结果一到就必须立即收起")
                self.assertEqual(probe.deiconify_count(), maps, "零 deiconify")
                self.assertEqual(probe.withdraw_count(), withdraws + 1, "只 withdraw 一次")
            finally:
                probe.close()

    def test_chat_result_under_hard_gate_hides_immediately_and_soft_keeps(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = int(db.add_entry(batch_id=bid, term="alpha"))
            probe = PanelProbe(db=db)
            try:
                panel = probe.panel
                panel.show_dock(explicit=True)
                panel.show_recorded(make_sel(term="alpha"), eid, status="ok",
                                    explicit=True)
                panel.entry_input.insert(0, "它和互相关有什么区别？")
                self.assertEqual(panel.send_question(), "sent")
                token = panel._chat_request_token
                self.assertIsNotNone(token)
                maps = probe.deiconify_count()

                probe.gate.mode = "soft"
                self.assertTrue(panel.on_chat_result(token, eid, "ok", "软受限回答"))
                self.assertTrue(panel.visible, "软受限不得隐藏面板")

                probe.gate.mode = "hard"
                self.assertFalse(panel.on_chat_result(token, eid, "ok", "游戏里的回答"))
                self.assertFalse(panel.visible, "硬阻断下追问结果一到就必须立即收起")
                self.assertEqual(probe.deiconify_count(), maps, "零 deiconify")
                self.assertFalse(panel._chat_busy, "内存状态仍要更新（重新打开可见）")
            finally:
                probe.close()


# ============================================================================
# B. 置顶 / 呼出：普通环境零隐藏，软受限保留已展开窗口，硬阻断仍拒绝
# ============================================================================
class TestTopmostAndRecallPreserveWindows(unittest.TestCase):
    @staticmethod
    def _open(**kwargs):
        return headless_app(overlays="panel", main_window="real", **kwargs)

    def test_normal_topmost_off_keeps_panel_rect_visible_closed_pending(self):
        """普通阅读下浮窗**自动置顶**：旧的 ``ui.topmost`` 偏好不再驱动窗口层级。

        用户要求「主窗口始终普通层级、浮窗正常阅读自动置顶」，因此这里断言两件事：
        旧偏好开关改来改去都不会隐藏 / 抖动面板（零 geometry / deiconify /
        withdraw），而主窗口无论如何只被下发普通层级（``-topmost`` False）。
        """
        with self._open() as app:
            panel = app.reading_panel
            show_selection(app, make_sel(term="卷积"))
            self.assertTrue(panel.is_open())
            panel.entry_input.insert(0, "正在打字")
            specs = list(panel.win.geometry_specs)
            deicon = app.fake_tk.deiconify_count()
            withdraw = app.fake_tk.withdraw_count()

            app.set_topmost_preference(False)

            self.assertTrue(app.effective_topmost(),
                            "普通阅读前台：浮窗自动置顶，旧偏好开关不再关掉它")
            self.assertTrue(panel.is_open(), "普通环境改旧偏好不得隐藏面板")
            self.assertEqual(panel.mode_name(), MODE_PANEL)
            self.assertIsNotNone(panel.selection, "待处理词必须保留")
            self.assertFalse(panel.closed_by_user)
            self.assertEqual(panel.current_question(), "正在打字")
            self.assertEqual(panel.win.geometry_specs, specs, "零 geometry（矩形不变）")
            self.assertEqual(app.fake_tk.deiconify_count(), deicon, "零 deiconify")
            self.assertEqual(app.fake_tk.withdraw_count(), withdraw, "零 withdraw")
            self.assertIn(("-topmost", False),
                          [tuple(call) for call in app.fake_root.attributes_calls],
                          "主窗口只允许普通层级（绝不 -topmost True）")
            self.assertNotIn(("-topmost", True),
                             [tuple(call) for call in app.fake_root.attributes_calls])

            app.set_topmost_preference(True)          # 再打开旧偏好也不得隐藏 / 抖动
            self.assertTrue(panel.is_open())
            self.assertEqual(app.fake_tk.withdraw_count(), withdraw)

    def test_hard_gate_apply_topmost_still_hides(self):
        with self._open() as app:
            panel = app.reading_panel
            show_selection(app, make_sel())
            app.gate.set_game_mode(True)
            app._apply_gate(force=True)               # 进入硬阻断：收起浮层
            maps = app.fake_tk.deiconify_count()
            app.apply_topmost()                       # 硬阻断期间再同步一次置顶
            self.assertFalse(panel.visible, "硬阻断下置顶策略必须保持收起")
            self.assertFalse(app.effective_topmost())
            self.assertEqual(app.fake_tk.deiconify_count(), maps)

    def test_recall_ui_soft_foreground_keeps_expanded_panel(self):
        with self._open() as app:
            panel = app.reading_panel
            show_selection(app, make_sel(term="卷积"))
            self.assertTrue(panel.is_open())
            specs = list(panel.win.geometry_specs)
            deicon = app.fake_tk.deiconify_count()
            withdraw = app.fake_tk.withdraw_count()

            app.fake_tk.w32.foreground_hwnd = 999001     # 本程序另一个窗口（设置）
            app.fake_tk.w32.is_self_hwnds.add(999001)
            app.fake_tk.w32.root_map[999001] = 999001
            app.recall_ui()

            self.assertTrue(panel.is_open(), "软受限下显式呼出必须保留已展开窗口")
            self.assertEqual(panel.mode_name(), MODE_PANEL)
            self.assertEqual(panel.win.geometry_specs, specs,
                             "不得 _restore_dock 把它缩成 44x44")
            self.assertEqual(app.fake_tk.deiconify_count(), deicon)
            self.assertEqual(app.fake_tk.withdraw_count(), withdraw)

    def test_recall_ui_recovers_only_when_closed_or_hidden(self):
        with self._open() as app:
            panel = app.reading_panel
            panel.show_dock(explicit=True)
            panel.close_by_user()
            self.assertFalse(panel.visible)

            app.recall_ui()                              # 显式呼出 = 找回小方块
            self.assertEqual(panel.mode_name(), MODE_DOCK)
            self.assertTrue(panel.visible)
            self.assertFalse(panel.closed_by_user)

    def test_recall_ui_hard_gate_still_refuses(self):
        with self._open() as app:
            panel = app.reading_panel
            show_selection(app, make_sel())
            maps = app.fake_tk.deiconify_count()
            app.gate.set_game_mode(True)
            app.recall_ui()
            self.assertFalse(panel.visible, "游戏模式下呼出仍不得映射浮层")
            self.assertEqual(app.fake_tk.deiconify_count(), maps)


# ============================================================================
# E. 词卡摘要跟随解释结果刷新（一次），随后幂等且不 remap
# ============================================================================
class TestCardSummaryRefresh(unittest.TestCase):
    def test_summary_change_refreshes_cards_once_without_remap(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = int(db.add_entry(batch_id=bid, term="Transformer"))
            probe = PanelProbe(db=db)
            try:
                panel = probe.panel
                panel.show_dock(explicit=True)
                panel.expand(explicit=True)
                panel.show_list()
                card = self._card(panel, "Transformer")
                self.assertIsNotNone(card)
                self.assertEqual(card.summary, "", "库里还没有摘要")
                specs = list(panel.win.geometry_specs)
                deicon = probe.deiconify_count()

                db.update_entry(eid, explain_status="ok", one_line="加权求和的特征提取")
                self.assertTrue(panel.refresh_terms(), "摘要真的变了 → 更新一次")
                again = self._card(panel, "Transformer")
                self.assertIsNotNone(again)
                self.assertEqual(again.summary, "加权求和的特征提取",
                                 "卡片摘要必须跟着 one_line 更新")
                self.assertFalse(panel.refresh_terms(), "数据没变 → 幂等，不重建")
                self.assertEqual(panel.win.geometry_specs, specs, "刷新词卡不得 remap 窗口")
                self.assertEqual(probe.deiconify_count(), deicon)
            finally:
                probe.close()

    @staticmethod
    def _card(panel, term: str):
        """按词语找**真实卡片对象**（卡片不在 fake Tk 的控件工厂里）。"""
        for card in panel.tags_area.children:
            if getattr(card, "term", None) == term:
                return card
        return None

    def test_explanation_result_updates_summary_once_in_real_chain(self):
        with headless_app(overlays="panel", main_window="real") as app:
            from tests.test_unified_action import FakeClient, configure_key, wait_for

            configure_key(app.config)
            app.explain_service.make_client = lambda **kw: FakeClient("加权求和的特征提取")
            panel = app.reading_panel
            show_selection(app, make_sel(term="Transformer"))
            specs = list(panel.win.geometry_specs)
            panel._on_action()
            self.assertTrue(wait_for(
                app, lambda: panel.entry_id is not None
                and app.db.get_entry(panel.entry_id)["explain_status"] == "ok"))
            self.assertTrue(wait_for(
                app, lambda: self._card(panel, "Transformer") is not None
                and self._card(panel, "Transformer").summary == "加权求和的特征提取"),
                "解释结果必须让词卡摘要更新一次")
            self.assertEqual(panel.win.geometry_specs, specs,
                             "摘要刷新不得重新下发 geometry")
            self.assertEqual(panel.mode_name(), MODE_PANEL)


# ============================================================================
# G. 标签显示预算：常见短英文全称完整显示，超长仍省略，详情保留全文
# ============================================================================
class TestTagDisplayBudget(unittest.TestCase):
    def test_clip_tag_uses_halfwidth_budget(self):
        from app.ui.panel_geometry import TAG_MAX_CHARS, TAG_MAX_UNITS, char_units

        self.assertEqual(TAG_MAX_UNITS, 2 * TAG_MAX_CHARS - 2)
        self.assertEqual(char_units("T"), 1)
        self.assertEqual(char_units("注"), 2)
        self.assertEqual(clip_tag("Transformer"), "Transformer",
                         "11 个字母的常见英文全称不得被截成 Transforme…")
        self.assertEqual(clip_tag("Embedding"), "Embedding")
        self.assertEqual(clip_tag("注意力机制"), "注意力机制")
        long_term = "InternationalizationXYZ"
        shown = clip_tag(long_term)
        self.assertTrue(shown.endswith("…"), "超长仍然省略")
        self.assertLess(len(shown), len(long_term))
        self.assertEqual(clip_tag(""), "")
        self.assertEqual(clip_tag("abc", max_units=0), "abc")

    def test_cards_keep_size_and_detail_keeps_full_term(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            int(db.add_entry(batch_id=bid, term="Transformer"))
            probe = PanelProbe(db=db)
            try:
                panel = probe.panel
                panel.show_dock(explicit=True)
                panel.expand(explicit=True)
                panel.show_list()
                # 真超长（比面板两行预算还长）才省略：长度按面板自己的预算构造。
                long_term = "InternationalizationXYZ" + "W" * (term_budget(panel) + 4)
                long_id = int(db.add_entry(batch_id=bid, term=long_term))
                self.assertTrue(panel.refresh_terms(force=True))
                self.assertIsNotNone(probe.env.find("Transformer"),
                                     "短英文全称必须完整显示在卡片上")
                cards = [c for c in panel.tags_area.children
                         if isinstance(c, widgets.TermCard)]
                self.assertTrue(cards)
                self.assertEqual([c.term for c in cards if c.term == "Transformer"],
                                 ["Transformer"], "卡片上的词语不得被截断")
                self.assertTrue(any(c.term.endswith("…") for c in cards),
                                "超过两行的长词仍然省略")
                for card in cards:
                    self.assertEqual(card.height, theme.px(geo.CARD_H),
                                     "词卡尺寸不得变化")
                panel.open_entry(long_id)
                detail = str(panel.term_title.cget("text")) + "\n" + \
                    panel.def_text.get("1.0", "end")
                self.assertIn("InternationalizationXYZ", detail,
                              "详情页必须保留完整词语（卡片省略的只是显示）")
            finally:
                probe.close()

    def test_card_vertical_budget_fits_two_term_lines_and_one_summary(self):
        """卡片高度必须真的放得下「两行词语 + 一行摘要」——**行高 ≠ 字号**。

        真实 Tk 实测（``_check/card_metrics_probe.py``）：96 DPI 下 13px 词语一行
        21px、11px 摘要一行 18px；144 DPI 下 20px 词语 29px、16px 摘要 23px。
        旧实现按字号预留摘要高度，144 DPI 下把摘要下半截裁掉了 5px；这条断言把
        「卡片高 / 内边距 / 字号 / 行数」之间的关系钉死，改任何一项都会先在这里红。
        """
        for scale in (1.0, 1.5, 2.0):
            with self.subTest(scale=scale):
                def px(n: int, _s: float = scale) -> int:
                    return max(1, int(round(n * _s)))

                def font_px(pt: int, _s: float = scale) -> int:
                    return max(1, int(round(pt * 96 / 72 * _s)))

                body = px(geo.CARD_H) - 2 * px(geo.CARD_PAD_Y)
                summary_slot = geo.text_line_px(font_px(geo.CARD_SUMMARY_PT)) + 2
                term_slot = body - summary_slot - 2
                term_line = geo.text_line_px(font_px(geo.CARD_TERM_PT))
                self.assertGreaterEqual(
                    term_slot, term_line * geo.CARD_TERM_LINES,
                    f"scale={scale}：词语放不下 {geo.CARD_TERM_LINES} 行"
                    f"（留了 {term_slot}px，两行要 {term_line * geo.CARD_TERM_LINES}px）")

    def test_real_card_gives_the_term_two_lines_and_the_summary_its_line(self):
        """真控件上的同一件事：词语两行的位置 ≥ 两行高，摘要位置 ≥ 一行高。"""
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = int(db.add_entry(batch_id=bid, term="Transformer"))
            db.update_entry(eid, explain_status="ok", one_line="加权求和的特征提取")
            probe = PanelProbe(db=db)
            try:
                panel = probe.panel
                panel.show_dock(explicit=True)
                panel.expand(explicit=True)
                panel.show_list()
                panel.refresh_terms(force=True)
                cards = [c for c in panel.tags_area.children
                         if isinstance(c, widgets.TermCard)]
                card = next(c for c in cards if c.term == "Transformer")
                self.assertIsNotNone(card.summary_label, "库里有 one_line 就该有摘要行")
                term_line = widgets.line_height(geo.CARD_TERM_PT)
                self.assertGreaterEqual(card._term_height(), term_line * geo.CARD_TERM_LINES,
                                        "词语的位置必须容得下两行")
                self.assertGreaterEqual(card._summary_height(),
                                        widgets.line_height(geo.CARD_SUMMARY_PT),
                                        "摘要的位置必须容得下一整行（不能裁字）")
            finally:
                probe.close()


# ============================================================================
# F. 界面长文案精简后仍可定位错误（状态栏 / 正文都不出现内部编号与已删除按钮名）
# ============================================================================
class TestShortUserFacingStatus(unittest.TestCase):
    def test_no_key_status_is_short_and_has_no_internal_ids(self):
        with headless_app(overlays="panel", main_window="real") as app:
            panel = app.reading_panel
            show_selection(app, make_sel(term="卷积"))
            eid = panel._on_action()
            self.assertIsNotNone(eid)

            status = str(app.main.status_label.cget("text"))
            self.assertIn("待解释", status)
            self.assertNotIn("#", status, "状态栏不得显示词条内部编号")
            self.assertLessEqual(len(status), 30, f"状态栏太长：{status}")
            message = str(panel.message)
            self.assertIn("已记录", message)
            self.assertIn("待解释", message)
            self.assertNotIn("#", message)
            self.assertNotIn("重新解释", message, "正文不得再提已删除的按钮名")
            self.assertLessEqual(len(message), 40, f"待解释提示太长：{message}")
            # 可执行的那句话在**底部提示行**：文案还在，而且真的可点（打开主界面配置）
            self.assertEqual(str(panel.hint_label.cget("text")), NO_KEY_HINT)
            self.assertEqual(panel._hint_action, "main",
                             "底部配置提示必须是可点的那一句")

    def test_short_status_for_explaining_and_answering(self):
        with headless_app(overlays="panel", main_window="real") as app:
            from tests.test_unified_action import FakeClient, configure_key, wait_for

            configure_key(app.config)
            app.explain_service.make_client = lambda **kw: FakeClient("释义")
            panel = app.reading_panel
            show_selection(app, make_sel(term="卷积"))
            panel._on_action()
            self.assertIn("正在解释", str(app.main.status_label.cget("text")))
            self.assertTrue(wait_for(
                app, lambda: app.db.get_entry(panel.entry_id)["explain_status"] == "ok"))
            self.assertIn("解释完成", str(app.main.status_label.cget("text")))

            panel.entry_input.insert(0, "它和互相关有什么区别？")
            # 只验证界面文案：把 submit 换成纯内存替身，**绝不发真实请求**
            app.chat_service.submit = lambda entry_id, question: 4242
            self.assertEqual(panel.send_question(), "sent")
            self.assertEqual(str(app.main.status_label.cget("text")), "正在回答…")


# ============================================================================
# D. App 排队结果：服务**已经落库**、UI 还没消费时用户又编辑 / 移动词条
# ============================================================================
def _wait_persisted_without_pump(app, entry_id: int, timeout: float = 5.0) -> bool:
    """等「解释服务写完库 + 结果事件已入 UI 队列」，**绝不 pump UI 队列**。

    ``pump_app`` 会立刻把排队事件消费掉，那正是本组用例要卡住的时间窗口，
    所以这里只轮询数据库与队列状态，不驱动 ``App._pump``。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (app.db.get_entry(entry_id)["explain_status"] == "ok"
                and not app.explain_service.is_inflight(entry_id)
                and not app._ui_q.empty()):
            return True
        time.sleep(0.02)
    return False


class TestQueuedResultAfterUserEditOrMove(unittest.TestCase):
    """服务写回**之后**、UI 消费**之前**的窗口期：用户又编辑 / 移动了词条。

    已有的 ``FakeClient.on_call`` 用例只覆盖「服务写回**之前**」的改动；
    这里卡住「已经落库、结果事件还排在 UI 队列里」的时刻，验证 UI 消费结果时
    **不会按库里的现状再改一次名**：用户刚改的词 / 刚移入的主题原样保留。
    """

    def _arm(self, app, *, topic: str = ""):
        """走真实 App 链路：落库 → 异步解释 → 等结果落库入队（不消费）。"""
        from tests.test_unified_action import FakeClient, configure_key

        configure_key(app.config)
        app.explain_service.make_client = lambda **kw: FakeClient("队列里的释义",
                                                                  topic=topic)
        panel = app.reading_panel
        show_selection(app, make_sel(term="卷积", context="深度学习中的卷积"))
        entry_id = panel._on_action()
        self.assertIsNotNone(entry_id, "「解释并记录」必须先落库")
        self.assertTrue(_wait_persisted_without_pump(app, entry_id),
                        "解释服务必须先落库，结果事件此时还排在 UI 队列里")
        return int(entry_id)

    @staticmethod
    def _pending_explain_events(app) -> list:
        return [item for item in list(app._ui_q.queue)
                if item and item[0] == "explain_result"]

    def test_edit_between_writeback_and_consume_never_renames_again(self):
        with headless_app(overlays="panel", main_window="real") as app:
            entry_id = self._arm(app, topic="模型给的主题名")
            self.assertTrue(self._pending_explain_events(app),
                            "结果事件必须还没被 UI 消费（这正是被测的窗口期）")
            batch_id = int(app.db.get_entry(entry_id)["batch_id"])
            batch_name_before = str(app.db.get_batch(batch_id)["name"])

            # ---- 窗口期：用户在真实主界面保存里改了词 + 语境 ----
            app.main.select_entry(entry_id)
            app.main.var_term.set("卷积改名")
            app.main.txt_context.delete("1.0", "end")
            app.main.txt_context.insert("1.0", "用户改过的语境")
            app.main.save_detail()
            self.assertEqual(app.db.get_entry(entry_id)["term"], "卷积改名")

            pump_app(app)                     # UI 这时才消费那条排队结果

            row = app.db.get_entry(entry_id)
            self.assertEqual(row["term"], "卷积改名", "UI 消费排队结果时不得二次改名")
            self.assertEqual(row["context"], "用户改过的语境", "用户改的语境必须保留")
            self.assertEqual(str(app.db.get_batch(batch_id)["name"]), batch_name_before,
                             "UI 不得按排队结果里的 topic 再改一次主题名")
            self.assertEqual(str(app.main.var_term.get()), "卷积改名",
                             "主界面详情必须显示用户改后的词")
            self.assertEqual(str(app.main.status_label.cget("text")),
                             "解释完成：卷积改名", "状态栏按库里现状刷新，不按旧快照")

    def test_move_between_writeback_and_consume_never_touches_new_group(self):
        with headless_app(overlays="panel", main_window="real") as app:
            entry_id = self._arm(app, topic="模型给的主题名")
            self.assertTrue(self._pending_explain_events(app))
            old_batch = int(app.db.get_entry(entry_id)["batch_id"])
            self.assertEqual(str(app.db.get_batch(old_batch)["name"]), "模型给的主题名",
                             "服务写回时确实按 topic 改过名（这条路径是活的）")

            # ---- 窗口期：用户把这条词移到另一个「还能自动改名」的主题 ----
            other = app.db.create_batch("另一个自动主题", name_source="auto")
            app.db.update_entry(entry_id, batch_id=other)
            pump_app(app)

            self.assertEqual(int(app.db.get_entry(entry_id)["batch_id"]), other,
                             "UI 消费排队结果不得把词条移回旧主题")
            self.assertEqual(str(app.db.get_batch(other)["name"]), "另一个自动主题",
                             "旧结果里的 topic 绝不能按「词条现在的主题」再改一次名")
            self.assertEqual(str(app.db.get_batch(old_batch)["name"]), "模型给的主题名",
                             "原主题的名字保持服务写回时的结果")

    def test_queued_result_never_revives_panel_user_closed_in_the_window(self):
        """窗口期里用户点了「×」：排队结果消费时不得把面板重新弹开。"""
        with headless_app(overlays="panel", main_window="real") as app:
            entry_id = self._arm(app)
            self.assertTrue(self._pending_explain_events(app))
            panel = app.reading_panel
            maps = app.fake_tk.deiconify_count()

            self.assertTrue(panel.btn_close.invoke(), "窗口期里点 × 必须真的收起")
            self.assertTrue(panel.closed_by_user)
            pump_app(app)

            self.assertEqual(panel.mode_name(), MODE_HIDDEN)
            self.assertTrue(panel.closed_by_user, "UI 消费排队结果不得清掉关闭标记")
            self.assertEqual(app.fake_tk.deiconify_count(), maps, "零 deiconify")
            self.assertEqual(app.db.get_entry(entry_id)["explain_status"], "ok",
                             "窗口收起不影响已经完成的落库")


# ============================================================================
# G. 圆角安全留白 / 换行宽度 / region 同步时序（真实代码 + 假 Tk + 假 Win32）
# ============================================================================
class TestPanelPaddingAndWrap(unittest.TestCase):
    """``outer`` 是唯一承担圆角留白的一层；矩形内容绝不铺在圆弧上。"""

    @staticmethod
    def _pad_of(widget) -> tuple:
        kw = dict(getattr(widget, "pack_kw", {}) or {})
        return (int(kw.get("padx") or 0), int(kw.get("pady") or 0))

    def test_only_outer_carries_the_round_corner_padding(self):
        probe = PanelProbe()
        try:
            panel, env = probe.panel, probe.env
            dock_pad = theme.px(geo.border_safe_inset(geo.DOCK_RADIUS))
            panel_pad = theme.px(geo.border_safe_inset(geo.PANEL_RADIUS))
            self.assertEqual(self._pad_of(panel.outer), (dock_pad, dock_pad),
                             "初始（收起态）outer 必须自带小方块的圆角留白")

            panel.show_dock(explicit=True)
            panel.expand(explicit=True)
            self.assertEqual(self._pad_of(panel.outer), (panel_pad, panel_pad),
                             "展开后 outer 的留白 = 面板半径 + 描边")
            # 内层两个 body 铺满 outer，**没有**自己的 padx/pady（不重复留边）
            for name, body in (("panel_body", panel.panel_body), ("dock_body", panel.dock_body)):
                kw = dict(getattr(body, "pack_kw", {}) or {})
                self.assertNotIn("padx", kw, f"{name} 不得再留一份边距")
                self.assertNotIn("pady", kw, f"{name} 不得再留一份边距")

            # 留白 ≥ 半径 + 描边：矩形内容与整条圆弧**没有任何重叠**
            for mode_pad, radius in ((panel_pad, geo.PANEL_RADIUS),
                                     (dock_pad, geo.DOCK_RADIUS)):
                self.assertGreaterEqual(mode_pad,
                                        theme.px(radius) + theme.px(geo.WINDOW_BORDER),
                                        "内容内缩量必须覆盖整个角方块")

            # 模式切换只更新 outer 这一份边距：panel_body / dock_body 一次都没被
            # 重新 pack 过（它们的 pack 记录里没有 padx/pady），outer 只被重配 2 次
            # （build 一次 + dock→panel 一次）。
            outer_packs = [kw for w, kw in env.pack_calls if w is panel.outer]
            self.assertEqual(len(outer_packs), 2,
                             "只有模式切换允许重新 pack_configure outer")
            panel.collapse_to_dock(explicit=True)
            self.assertEqual(self._pad_of(panel.outer), (dock_pad, dock_pad),
                             "折叠回小方块后留白回到半径 10 的那一份")
            self.assertEqual(len([kw for w, kw in env.pack_calls if w is panel.outer]), 3)
        finally:
            probe.close()

    def test_wraplength_follows_the_real_content_width_at_300(self):
        """小窗缩到 300 宽：hint / quote / title 的 wraplength 必须一起变小。

        提示行还要再减掉右侧手柄净空（``GRIP_SIZE``），否则最后一段文字会算多
        20px、跑到手柄底下被 region 裁掉。
        """
        probe = PanelProbe()
        try:
            panel = probe.panel
            panel.show_dock(explicit=True)
            panel.expand(explicit=True)
            expected_360 = (theme.px(geo.PANEL_W)
                            - 2 * theme.px(geo.border_safe_inset(geo.PANEL_RADIUS))
                            - 2 * theme.px(geo.CONTENT_PAD))
            grip = theme.px(geo.GRIP_SIZE)
            for name, widget, want in (("提示行", panel.hint_label, expected_360 - grip),
                                       ("选词", panel.quote_label, expected_360),
                                       ("标题", panel.term_title, expected_360)):
                self.assertEqual(int(widget.cget("wraplength")), want,
                                 f"360 宽时 {name} 的 wraplength 必须按实际宽度算")

            # 真实路径：拖右下角手柄把面板缩到最小宽度 300
            panel._on_resize_start(FakeEvent(1000, 900))
            panel._on_resize_motion(FakeEvent(1000 - 60, 900))
            panel._on_resize_end(FakeEvent(1000 - 60, 900))
            self.assertEqual(panel._panel_w, theme.px(geo.PANEL_MIN_W))
            expected_300 = expected_360 - (theme.px(geo.PANEL_W) - theme.px(geo.PANEL_MIN_W))
            for name, widget, want in (("提示行", panel.hint_label, expected_300 - grip),
                                       ("选词", panel.quote_label, expected_300),
                                       ("标题", panel.term_title, expected_300)):
                got = int(widget.cget("wraplength"))
                self.assertEqual(got, want,
                                 f"300 宽时 {name} 的 wraplength 必须变成 {want}")
                self.assertNotEqual(got, theme.px(322), "不得再写死 322")
        finally:
            probe.close()

    def test_quote_display_stays_within_two_lines_at_300(self):
        """最窄 300 宽：长 CJK / 宽英文的「当前词」显示串都 ≤ 2 行。

        量的是 ``quote_label`` 里**真实渲染出来的那串文字**，用独立的贪心换行：
        每个字符按 1 em（``abs(theme.device_px(9))`` 像素 —— 中文全角字与最宽拉丁
        字母 ``W`` 的保守上界）逐个填满 ``wraplength``，再看它需要几行 —— 不是把
        ``quote_budget`` 的公式抄一遍。截断只允许发生在显示上：完整 term / context
        必须原样留在选区快照里。
        """
        probe = PanelProbe()
        try:
            panel = probe.panel
            panel.show_dock(explicit=True)
            panel.expand(explicit=True)
            panel._on_resize_start(FakeEvent(1000, 900))
            panel._on_resize_motion(FakeEvent(1000 - 60, 900))
            panel._on_resize_end(FakeEvent(1000 - 60, 900))
            self.assertEqual(panel._panel_w, theme.px(geo.PANEL_MIN_W))
            wrap = int(panel.quote_label.cget("wraplength"))
            self.assertEqual(wrap, panel._content_width(),
                             "选词行的换行宽度必须是 300 宽下的真实内容宽度")
            cell = abs(theme.device_px(9))

            def measure(text: str) -> tuple[int, int]:
                """贪心换行（每字符 ≤ 1 em 的保守上界）→ (行数, 最宽行像素)。"""
                lines, widest, used = 1, 0, 0
                for _ch in text:
                    if used + cell > wrap:
                        lines += 1
                        used = 0
                    used += cell
                    widest = max(widest, used)
                return lines, widest

            context = "上下文" * 40
            cases = ("卷" * 60, "W" * 60, "Internationalization" * 4,
                     "卷积" * 20 + "W" * 20)
            for token, term in enumerate(cases, start=1):
                sel = make_sel(term=term, context=context)
                panel.show_selection(sel, token, (10, 10))
                shown = str(panel.quote_label.cget("text"))
                self.assertTrue(shown.startswith("「") and shown.endswith("」"),
                                f"选词行必须带成对括号：{shown!r}")
                lines, widest = measure(shown)
                self.assertLessEqual(lines, QUOTE_LINES,
                                     f"{term[:8]}… 在 300 宽下显示成了 {lines} 行：{shown!r}")
                self.assertLessEqual(widest, QUOTE_LINES * wrap,
                                     "显示串的设备像素宽度上界必须 ≤ 两行内容宽度")
                self.assertIn(term[:6], shown, "开头必须原样显示（不是整串省略）")
                self.assertEqual(str(sel.term), term, "显示截断不得改动完整 term")
                self.assertEqual(str(sel.context), context, "显示截断不得改动完整 context")
                self.assertIs(panel.selection, sel,
                              "面板攥着的仍是完整快照（显示截断不落回快照）")
        finally:
            probe.close()

    def test_wraplength_syncs_across_dpi_scale(self):
        probe = PanelProbe()
        original = theme._scale
        try:
            panel = probe.panel
            panel.show_dock(explicit=True)
            panel.expand(explicit=True)
            big = int(panel.hint_label.cget("wraplength"))
            theme._scale = 1.5
            try:
                panel._panel_w = theme.px(geo.PANEL_MIN_W)
                expected = (theme.px(geo.PANEL_MIN_W)
                            - 2 * theme.px(geo.border_safe_inset(geo.PANEL_RADIUS))
                            - 2 * theme.px(geo.CONTENT_PAD)
                            - theme.px(geo.GRIP_SIZE))     # 提示行还要减手柄净空
                panel._move_window(0, 0)          # 真实入口：走 _set_geometry → 同步
                small = int(panel.hint_label.cget("wraplength"))
            finally:
                theme._scale = original
            self.assertEqual(small, expected)
            self.assertNotEqual(big, small, "换行宽度必须随窗口宽度变化")
        finally:
            theme._scale = original
            probe.close()

    def test_list_top_row_shows_only_the_short_topic(self):
        """列表页的主题归到 chip 条，顶栏来源行只在详情页出现。"""
        probe = PanelProbe()
        try:
            probe.stub.panel_topic_line = lambda: "默认批次"
            probe.stub.panel_context_line = lambda: "示例文档 · 《默认批次》"
            probe.panel._render_context()
            self.assertEqual(str(probe.panel.source_label.cget("text")), "默认批次",
                             "列表顶栏只放当前页的短主题名，不拼浏览器窗口标题")
            self.assertFalse(probe.panel._source_packed,
                             "列表页的来源行让位给主题 chip 条（同一个主题不显示两遍）")

            probe.panel.show_detail(set_geometry=False)   # 详情页仍然显示来源行
            probe.panel._render_context()
            self.assertTrue(probe.panel._source_packed, "详情页保留来源行")
            probe.panel.show_list(set_geometry=False)

            probe.stub.panel_topic_line = lambda: ""
            probe.panel._render_context()
            self.assertEqual(str(probe.panel.source_label.cget("text")), "")
            self.assertFalse(probe.panel._source_packed, "没有主题时整行 + 分隔线一起隐藏")

            del probe.stub.panel_topic_line       # 旧宿主只有 panel_context_line
            probe.panel._render_context()
            self.assertFalse(probe.panel._source_packed,
                             "列表页始终隐藏来源行（旧宿主也一样，不再回退显示）")
        finally:
            probe.close()

    def test_dock_glyph_uses_the_shared_size_and_zero_native_padding(self):
        probe = PanelProbe()
        try:
            label = probe.panel.dock_label
            self.assertEqual(str(label.cget("text")), geo.DOCK_GLYPH)
            font = tuple(label.cget("font"))
            self.assertEqual(font[1], theme.device_px(geo.DOCK_GLYPH_PT),
                             "小方块字号必须来自 geo.DOCK_GLYPH_PT（离线预览同源）")
            for key in ("padx", "pady", "bd", "highlightthickness"):
                self.assertEqual(int(label.cget(key) or 0), 0,
                                 f"小方块标签的 {key} 必须是 0（否则 22x22 会裁切）")
            self.assertEqual(geo.dock_content_box(), 22,
                             "44 的小方块四边各内缩 11 → 内容可用区 22x22")
        finally:
            probe.close()

    def test_hint_row_is_the_resize_grip_clearance(self):
        """右下角手柄（GRIP_SIZE）只压在最底部提示行里：行高 ≥ 手柄、右侧留空。"""
        probe = PanelProbe()
        try:
            panel = probe.panel
            kw = dict(panel.hint_label.pack_kw)
            padx = kw.get("padx")
            self.assertIsInstance(padx, tuple)
            self.assertEqual(int(padx[0]), theme.px(geo.CONTENT_PAD))
            self.assertGreaterEqual(int(padx[1]),
                                    theme.px(geo.CONTENT_PAD + geo.GRIP_SIZE),
                                    "提示文字右侧必须把手柄宽度留出来")
            pady = kw.get("pady")
            self.assertIsInstance(pady, tuple)
            row_h = abs(theme.device_px(8)) + sum(int(v) for v in pady)
            self.assertGreaterEqual(row_h, theme.px(geo.GRIP_SIZE),
                                    "提示行行高必须 ≥ 手柄边长（否则手柄会压到输入行）")
            self.assertEqual(int(panel.hint_label.cget("wraplength")),
                             panel._content_width() - theme.px(geo.GRIP_SIZE),
                             "wraplength 必须减掉手柄净空，不能和 quote 用同一个数")
        finally:
            probe.close()


class TestRegionSyncSequencing(unittest.TestCase):
    """region 同步的时序：请求尺寸当次生效、迟到事件被拒、idle 回执带请求序号。"""

    @staticmethod
    def _event(panel, width, height, widget=None):
        from types import SimpleNamespace

        return SimpleNamespace(widget=widget if widget is not None else panel.win,
                               width=int(width), height=int(height))

    def test_new_geometry_uses_the_requested_size_in_the_same_call(self):
        probe = PanelProbe()
        try:
            panel, w32 = probe.panel, probe.env.w32
            panel.show_dock(explicit=True)
            self.assertEqual(w32.region_calls[-1][1:], (theme.px(44), theme.px(44),
                                                        theme.px(geo.DOCK_RADIUS)))
            win = probe.env.toplevel()
            win.winfo_w, win.winfo_h = 44, 44   # winfo 还是旧值
            panel.expand(explicit=True)
            self.assertEqual(w32.region_calls[-1][1:],
                             (theme.px(360), theme.px(460), theme.px(geo.PANEL_RADIUS)),
                             "当次 region 必须按下发的请求尺寸走，而不是旧的 winfo 尺寸")
            self.assertEqual(panel._requested_size, (theme.px(360), theme.px(460)))
        finally:
            probe.close()

    def test_child_configure_is_ignored_and_stale_old_size_is_rejected(self):
        probe = PanelProbe()
        try:
            panel, env = probe.panel, probe.env
            panel.show_dock(explicit=True)
            panel.expand(explicit=True)
            win = env.toplevel()
            calls = len(env.w32.region_calls)
            events = len(win.events)

            panel._on_self_configure(self._event(panel, 44, 44, widget=panel.btn_close))
            self.assertEqual(panel._requested_size, (theme.px(360), theme.px(460)),
                             "子控件的 Configure 必须被过滤掉")

            panel._on_self_configure(self._event(panel, 44, 44))
            self.assertEqual(panel._requested_size, (theme.px(360), theme.px(460)),
                             "带着已作废旧尺寸的迟到事件不得清掉 pending")
            self.assertEqual(len(env.w32.region_calls), calls,
                             "迟到事件不得把 region 退回旧尺寸")

            panel._on_self_configure(self._event(panel, theme.px(360), theme.px(460)))
            self.assertIsNone(panel._requested_size)
            self.assertEqual(panel._measured_size, (theme.px(360), theme.px(460)))

            after = win.events[events:]
            for forbidden in ("deiconify", "withdraw", "update_idletasks"):
                self.assertNotIn(forbidden, after,
                                 f"同步 region 不得 {forbidden}（不改显隐、不 update）")
            self.assertFalse([e for e in after if e.startswith("geometry:")],
                             "同步 region 不得重新下发 geometry")
        finally:
            probe.close()

    def test_idle_ack_carries_request_seq_and_never_falls_back_to_old_size(self):
        probe = PanelProbe()
        try:
            panel, env = probe.panel, probe.env
            panel.show_dock(explicit=True)
            win = env.toplevel()
            win.winfo_w, win.winfo_h = 44, 44
            panel.expand(explicit=True)
            seq = panel._request_seq
            self.assertEqual(panel._ack_pending_seq, seq, "idle 回执必须带上请求序号")
            self.assertTrue(win.idle_callbacks, "必须真的排了一次 idle 回执")
            ack = win.idle_callbacks[-1]
            calls = len(env.w32.region_calls)

            ack()                                 # Tk 还没处理完：winfo 仍是 44x44
            self.assertEqual(panel._requested_size, (theme.px(360), theme.px(460)),
                             "读到旧 winfo 尺寸不得清掉 pending")
            self.assertEqual(len(env.w32.region_calls), calls,
                             "读到旧 winfo 尺寸不得退回旧 region")

            panel._on_idle_ack(seq - 1)           # 上一代的迟到回执
            self.assertEqual(panel._requested_size, (theme.px(360), theme.px(460)),
                             "迟到回执（序号不是当前一代）必须被丢弃")

            win.winfo_w, win.winfo_h = theme.px(360), theme.px(460)
            ack()
            self.assertIsNone(panel._requested_size)
            self.assertEqual(panel._measured_size, (theme.px(360), theme.px(460)))
            self.assertIsNone(panel._ack_pending_seq)
        finally:
            probe.close()


# ============================================================================
# H. 主界面工具条：真实按钮 → 真实 App 方法 → 假依赖（不是 callback 镜像）
# ============================================================================
class TestMainWindowToolbarBindings(unittest.TestCase):
    """逐个 ``button.invoke()``，每条都断言**真实 App 方法的真实副作用**。"""

    @staticmethod
    def _button(app, label: str):
        btn = app.fake_tk.find_button(label)
        assert btn is not None, f"主界面工具条缺「{label}」按钮"
        return btn

    def test_search_clear_pause_and_game_mode_buttons(self):
        with headless_app(overlays="panel", main_window="real") as app:
            bid = app.db.create_batch("默认批次")
            app.db.add_entry(batch_id=bid, term="卷积")
            app.db.add_entry(batch_id=bid, term="注意力机制")
            app.main.apply_pending_follow(force=True)
            self.assertEqual(str(app.main.count_label.cget("text")).split(" /")[0], "2")

            app.main.search_var.set("卷积")
            self._button(app, "搜索").invoke()
            self.assertEqual(str(app.main.count_label.cget("text")).split(" /")[0], "1",
                             "「搜索」必须真的按关键词重查词条列表")

            self._button(app, "清空").invoke()
            self.assertEqual(str(app.main.search_var.get()), "")
            self.assertEqual(str(app.main.count_label.cget("text")).split(" /")[0], "2")

            before = app.capture_enabled()
            self._button(app, "暂停取词").invoke()
            self.assertNotEqual(app.capture_enabled(), before,
                                "「暂停取词」必须真的翻转 App.capture_enabled()")
            self._button(app, "恢复取词").invoke()
            self.assertEqual(app.capture_enabled(), before)

            game = app.game_mode()
            self._button(app, "游戏模式：开" if game else "游戏模式：关").invoke()
            self.assertNotEqual(app.game_mode(), game,
                                "「游戏模式」必须真的翻转 App.game_mode()")

    def test_settings_button_lifts_main_first_then_the_settings_window(self):
        with headless_app(overlays="panel", main_window="real") as app:
            button = self._button(app, "设置")
            order: list = []
            real_lift = app.fake_root.lift

            def _lift():
                order.append("main-lift")
                real_lift()

            app.fake_root.lift = _lift
            created: list = []
            before_lift = app.fake_root.lift_calls
            before_deiconify = app.fake_root.deiconify_calls

            import app.main as app_main

            class _FakeDialog:
                """替身只代替**对话框本身**；被测的是 App.open_settings 的时序。"""

                def __init__(self, master, app_):
                    created.append((app.fake_root.lift_calls,
                                    app.fake_root.deiconify_calls))
                    from types import SimpleNamespace

                    self.win = SimpleNamespace(
                        winfo_exists=lambda: 1,
                        lift=lambda: order.append("settings-lift"),
                        focus_force=lambda: order.append("settings-focus"),
                    )

            with mock.patch.object(app_main, "SettingsDialog", _FakeDialog):
                button.invoke()
            self.assertEqual(order, ["main-lift", "settings-lift", "settings-focus"],
                             "必须先 lift 主窗 → 再建设置窗 → 最后 lift 设置窗")
            self.assertEqual(created, [(before_lift + 1, before_deiconify + 1)],
                             "建设置窗时主窗已经被抬起来过，且已 deiconify"
                             "（根窗口默认 withdraw：只 lift 不 deiconify 看不到设置）")

    def test_settings_and_map_buttons_build_real_dialogs_on_fake_tk(self):
        with headless_app(overlays="panel", main_window="real") as app:
            windows = len(app.fake_tk.windows)
            self._button(app, "设置").invoke()
            self.assertIsNotNone(app._settings_win, "「设置」必须真的建设置窗")
            self.assertEqual(len(app.fake_tk.windows), windows + 1)
            self._button(app, "导图").invoke()
            self.assertIsNotNone(app._map_win, "「导图」必须真的建概念关系图窗")
            self.assertEqual(len(app.fake_tk.windows), windows + 2)

    def test_manual_entry_and_clipboard_buttons_reach_the_real_service(self):
        import app.main as app_main

        with headless_app(overlays="panel", main_window="real") as app:
            created: list = []
            real_dialog = app_main.ManualEntryDialog

            def _factory(*args, **kwargs):
                dialog = real_dialog(*args, **kwargs)   # 真实对话框 + 假 Tk
                created.append(dialog)
                return dialog

            with mock.patch.object(app_main, "ManualEntryDialog", side_effect=_factory):
                self._button(app, "手动录入").invoke()
            self.assertEqual(len(created), 1, "「手动录入」必须真的打开录入对话框")
            dialog = created[0]
            dialog.var_term.set("手动录入的词")
            dialog.save(False)
            self.assertEqual(app.db.count_entries(), 1, "真实保存路径必须写库")

            with mock.patch("app.capture_service.w32.read_clipboard_text",
                            return_value="  来自剪贴板的词  "):
                self._button(app, "剪贴板导入").invoke()
            self.assertEqual(app.db.count_entries(), 2, "「剪贴板导入」必须真的落库")
            self.assertEqual(len(app.db.list_entries(query="来自剪贴板的词")), 1)

    def test_detail_explain_button_is_single_and_state_driven(self):
        """详情页只有**一个**解释入口，文案随状态变，动作也随状态变。"""
        with headless_app(overlays="panel", main_window="real") as app:
            main = app.main
            labels = [str(b.cget("text")) for b in app.fake_tk.buttons]
            self.assertEqual(labels.count("解释"), 1, "同一页不允许两个解释键同排")
            self.assertNotIn("强制重解释", labels, "旧的双按钮行必须删除")

            bid = app.db.create_batch("主题")
            eid = int(app.db.add_entry(batch_id=bid, term="alpha"))
            calls: list = []
            app.explain_selected = lambda force: calls.append(bool(force))

            main.select_entry(eid)
            self.assertEqual(str(main.btn_explain.cget("text")), "解释")
            main.btn_explain.invoke()
            self.assertEqual(calls, [False], "未解释 → 普通解释")

            app.db.update_entry(eid, explain_status="error", explain_error="连接超时")
            main.select_entry(eid)
            self.assertEqual(str(main.btn_explain.cget("text")), "重试")
            main.btn_explain.invoke()
            self.assertEqual(calls, [False, False], "失败 → 重试（同一条入口，不重复记词）")

            app.db.update_entry(eid, explain_status="ok", one_line="一句话解释")
            main.select_entry(eid)
            self.assertEqual(str(main.btn_explain.cget("text")), "重新解释")
            main.btn_explain.invoke()
            self.assertEqual(calls, [False, False, True], "已解释 → 重新解释（跳过缓存）")

    def test_toolbar_method_audit_every_entry_is_really_wired(self):
        """方法审计：工具条每个入口都绑在**真实对象的方法**上（不是 None / 镜像）。

        原生菜单栏已删除（用户明确要求主界面去 menubar），因此设置 / 暂停 / 游戏
        在界面上**只有工具条这一组**；「退出」也只剩工具条右端这一个清晰入口
        （主窗「×」只是收起、后台继续）。状态文案在窗口**底部的独立短状态行**
        （≤ 40 字），不占工具条宽度。
        """
        from app.ui.main_window import STATUS_MAX_CHARS

        app_bound = {"设置": "open_settings", "导图": "open_concept_map",
                     "暂停取词": "toggle_capture",
                     "手动录入": "open_manual_dialog",
                     "剪贴板导入": "import_clipboard",
                     "游戏模式：关": "toggle_game_mode",
                     "退出": "quit"}
        main_bound = {"搜索": "refresh_entries", "清空": "_clear_search",
                      "重命名": "rename_batch", "删除": "delete_batch",
                      # B 批入口也一并审计：合并 / 拆分 / 导出 / 标签区三个按钮。
                      "合并…": "merge_batches_dialog", "拆分…": "split_entries_dialog",
                      "导出": "export_entries", "标签改名": "rename_tag",
                      "标签删除": "delete_tag", "全部": "clear_tag_filter"}
        with headless_app(overlays="panel", main_window="real") as app:
            buttons = {str(b.cget("text")): b for b in app.fake_tk.buttons}
            toolbar_labels = [str(b.cget("text")) for b in app.fake_tk.buttons]
            for label in ("设置", "暂停取词", "游戏模式：关", "退出"):
                self.assertEqual(toolbar_labels.count(label), 1,
                                 f"「{label}」在界面上只允许出现一次（不重复功能键）")
            for label, name in app_bound.items():
                self.assertIn(label, buttons, f"主界面缺「{label}」按钮")
                command = buttons[label].command
                self.assertTrue(callable(command), f"「{label}」必须绑真实回调")
                self.assertIs(getattr(command, "__self__", None), app,
                              f"「{label}」必须绑在 App 上")
                self.assertEqual(getattr(command, "__name__", ""), name)
                self.assertTrue(callable(getattr(app, name, None)))
            for label, name in main_bound.items():
                self.assertIn(label, buttons, f"主界面左栏缺「{label}」按钮")
                command = buttons[label].command
                self.assertIs(getattr(command, "__self__", None), app.main,
                              f"「{label}」必须绑在主界面上")
                self.assertEqual(getattr(command, "__name__", ""), name)

            # ---- 主界面没有原生菜单栏，工具条也没有被删掉的原生复选框 ----
            self.assertIsNone(getattr(app.main, "topmost_check", None),
                              "「置顶」开关必须从工具条删除（浮窗普通阅读自动置顶）")
            self.assertNotIn("全部词语", buttons,
                             "「全部词语」复选框必须删除（主题选择 + 全库搜索完成检索）")

            # ---- 状态文案：底部独立短状态行（不占工具条宽度、最多 40 字）----
            main = app.main
            self.assertEqual(STATUS_MAX_CHARS, 40)
            self.assertIs(main.status_label.master, main.status_row,
                          "状态标签属于底部状态行，不再挂在工具条上")
            self.assertIsNot(main.status_label.master, main.toolbar)
            self.assertEqual(app.fake_tk.pack_kw(main.status_row).get("side"), "bottom",
                             "状态行必须独立在窗口底部")
            self.assertLess(app.fake_tk.pack_index(main.status_row),
                            app.fake_tk.pack_index(main.batch_list),
                            "状态行先于主体 pack（先拿到自己的高度，不抢工具条宽度）")
            main.update_status()
            self.assertLessEqual(len(str(main.status_label.cget("text"))),
                                 STATUS_MAX_CHARS)

    def test_rename_delete_and_quit_buttons_call_real_methods(self):
        from app.ui import app_menu

        with headless_app(overlays="panel", main_window="real") as app:
            bid = app.db.create_batch("旧名")
            app.main.refresh_batches()
            app.main.batch_list.curselection = lambda: (0,)
            with mock.patch("app.ui.main_window.simpledialog.askstring",
                            return_value="新名"):
                self._button(app, "重命名").invoke()
            self.assertEqual(str(app.db.get_batch(bid)["name"]), "新名",
                             "「重命名」必须真的改库")
            self.assertEqual(str(app.db.get_batch(bid)["name_source"]), "manual")

            with mock.patch("app.ui.main_window.messagebox") as box:
                box.askyesno.return_value = True
                self._button(app, "删除").invoke()
            self.assertIsNone(app.db.get_batch(bid), "「删除」必须真的删主题")

            # 退出：整程序**唯一**的清晰入口 = 工具条右端的「退出」按钮
            # （主窗「×」只收起、后台继续；原生菜单栏已删除）。
            quit_button = app.fake_tk.find_button("退出")
            self.assertIsNotNone(quit_button, "主界面必须保留唯一的「退出」入口")
            self.assertIs(getattr(quit_button.command, "__self__", None), app,
                          "「退出」必须绑在真实 App 上")
            self.assertEqual(getattr(quit_button.command, "__name__", ""), "quit")
            items = {item[0]: item for item in app_menu.menu_items(app)}
            self.assertIn(app_menu.MENU_QUIT, items, "菜单定义仍然保留「退出」兜底")
            self.assertFalse(app._closing)
            quit_button.command()           # 真的点工具条上的那一项
            self.assertTrue(app._closing, "「退出」必须走真实 App.quit()")
            self.assertEqual(app.fake_root.destroy_calls, 1, "退出必须销毁根窗口")


# ============================================================================
# C2. 删除后的右栏复位：左栏删了，右栏不许留残留（用户截图里的状态）
# ============================================================================
class TestDetailPaneClearsWhenItsEntryDisappears(unittest.TestCase):
    """用户报告：「为什么左侧删除了右侧还有残留？」

    真实 ``MainWindow``（假 Tk）+ 真实库。截图里的状态是：左栏主题空了、中间
    「0 / 0 条」+「这一页还没有词语」，右栏却还完整显示着那条词的词语 / 上下文
    / 来源 / 释义。根因是删除路径只重建列表、**从不碰详情右栏**：
    ``delete_batch`` 连释义框都没清，``delete_selected`` 只清了释义框。

    这一组把「让词条消失」的三条路径都钉住，并钉住**不能**误伤的反例：搜索与
    手工浏览只换列表范围，右栏那条词还在库里 → 不许被清空。
    """

    @staticmethod
    def _button(app, label: str):
        btn = app.fake_tk.find_button(label)
        assert btn is not None, f"主界面缺「{label}」按钮"
        return btn

    @staticmethod
    def _seed(app, *, terms=("卷积核",)) -> tuple[int, list[int]]:
        """造一个主题 + 词条，并让它**真的显示在右栏**（列表走「点主题」路径）。"""
        bid = int(app.db.create_batch("卷积网络"))
        ids = [int(app.db.add_entry(batch_id=bid, term=term, context=f"{term} 的上下文"))
               for term in terms]
        app.db.update_entry(ids[0], explain_status="ok", one_line="加权求和的特征提取",
                            detail="逐元素相乘再求和。")
        app.main.set_browse_scope(bid)          # 等价于左栏点了这个主题
        app.main.select_entry(ids[0])
        return bid, ids

    @staticmethod
    def _detail(main) -> dict:
        """一次读全右栏：五个字段 + 提示语 + 选中的词条 id。"""
        return {
            "id": main._selected_entry_id,
            "hint": str(main.detail_hint.cget("text")),
            "term": main.var_term.get(),
            "context": main.txt_context.get("1.0", "end"),
            "title": main.var_src_title.get(),
            "url": main.var_src_url.get(),
            "meta": main.var_meta.get(),
            "explain": main.exp_text.get("1.0", "end"),
        }

    def _assert_pane_shows(self, main, term: str) -> None:
        detail = self._detail(main)
        self.assertIsNotNone(detail["id"], "前置：右栏确实选中了一条词")
        self.assertEqual(detail["term"], term)
        self.assertIn(term, detail["context"])
        self.assertIn("加权求和", detail["explain"], "前置：释义也在右栏")

    def _assert_pane_empty(self, main) -> None:
        detail = self._detail(main)
        self.assertIsNone(detail["id"], "删除后不许再留着选中的词条 id")
        self.assertEqual(detail["hint"], EMPTY_DETAIL_HINT)
        self.assertEqual(
            (detail["term"], detail["context"], detail["title"], detail["url"],
             detail["meta"], detail["explain"]),
            ("", "", "", "", "", ""),
            "右栏必须整块复位：词语 / 上下文 / 来源 / meta / 释义一条都不许残留",
        )
        for eid, card in main._cards.items():
            self.assertEqual(card.cget("highlightbackground"), theme.BORDER,
                             f"词条 {eid} 的卡片必须回到未选中描边")

    def test_deleting_the_whole_topic_clears_the_detail_pane(self):
        """左栏「删除」（删整个主题）之后，右栏不许还显示已删词条的词语与释义。"""
        with headless_app(overlays="panel", main_window="real") as app:
            bid, ids = self._seed(app)
            main = app.main
            self._assert_pane_shows(main, "卷积核")

            main.batch_list.curselection = lambda: (0,)      # 左栏选中「卷积网络」
            with mock.patch("app.ui.main_window.messagebox") as box:
                box.askyesno.return_value = True
                self._button(app, "删除").invoke()

            self.assertIsNone(app.db.get_batch(bid), "前置：主题真的被删掉了")
            self.assertIsNone(app.db.get_entry(ids[0]), "前置：主题下的词条一并删掉")
            self._assert_pane_empty(main)
            self.assertIn("0 / 0", str(main.count_label.cget("text")),
                          "中间列表也必须是空的（用户截图里的 0 / 0 条）")

    def test_deleting_one_entry_clears_the_detail_pane(self):
        """右栏自己的「删除词条」：五个字段与高亮都要复位，不只清释义框。"""
        with headless_app(overlays="panel", main_window="real") as app:
            bid, ids = self._seed(app, terms=("卷积核", "池化"))
            main = app.main
            self._assert_pane_shows(main, "卷积核")

            with mock.patch("app.ui.main_window.messagebox") as box:
                box.askyesno.return_value = True
                main.delete_selected()

            self.assertIsNone(app.db.get_entry(ids[0]))
            self._assert_pane_empty(main)
            self.assertIn("池化", [str(card.cget("text")) for card in
                                   app.fake_tk.widgets], "另一条词必须还在列表里")

    def test_selecting_an_already_deleted_entry_clears_the_pane(self):
        """迟到路径：词条已不在库里时 ``select_entry`` 也必须整块复位。"""
        with headless_app(overlays="panel", main_window="real") as app:
            bid, ids = self._seed(app, terms=("卷积核", "池化"))
            main = app.main
            self._assert_pane_shows(main, "卷积核")
            card = main._cards[ids[0]]
            self.assertEqual(card.cget("highlightbackground"), theme.TEXT,
                             "前置：选中的卡片有高亮描边")

            app.db.delete_entry(ids[0])                      # 例如异步解释期间被删
            main.select_entry(ids[0])

            self._assert_pane_empty(main)
            self.assertEqual(card.cget("highlightbackground"), theme.BORDER,
                             "已删词条的卡片不能继续留着高亮")

    def test_search_and_browsing_never_clear_a_live_entry(self):
        """反例：搜索 / 换主题只是换列表范围，右栏那条词还在库里 → 不许清空。

        收口守卫只按**库内存在性**判断（不是「是否还在当前筛选结果里」），
        否则用户一搜别的词，右栏正在编辑的内容就被吞掉了。
        """
        with headless_app(overlays="panel", main_window="real") as app:
            bid, ids = self._seed(app, terms=("卷积核", "池化"))
            main = app.main
            main.var_term.set("卷积核（正在编辑）")           # 未保存的编辑

            main.search_var.set("完全不存在的词")
            main.refresh_entries()
            self.assertEqual(main._selected_entry_id, ids[0], "搜索不该丢选中")
            self.assertEqual(main.var_term.get(), "卷积核（正在编辑）")

            main.search_var.set("")
            main.set_browse_scope(None)                       # 回「跟随当前阅读页」
            main.refresh_entries()
            self.assertEqual(main._selected_entry_id, ids[0], "换浏览范围不该丢选中")
            self.assertEqual(main.var_term.get(), "卷积核（正在编辑）")


# ============================================================================
# D. 追问可见性：竖向空间分配 + 立即回显 + 滚到最新（真实产品调用 + 假几何）
# ============================================================================
class TestDetailAreasKeepVisibleHeight(unittest.TestCase):
    """用户报告的「追问发出去，自己的问题和回答都看不见」= **布局**问题。

    数据链路是好的（``ChatService.submit`` 同步落库），所以这里**不看**
    「假 Text 里有没有字符串」，而是核对两块可滚动区域的**真实高度**：
    Canvas 请求高度、内嵌 Frame 的宽高、最小面板下两块都还 > 0。
    """

    def _panel_ready(self, probe):
        panel = probe.panel
        panel.show_dock()
        self.assertTrue(panel.toggle(), "展开成 360x460 面板")
        return panel

    def test_empty_conversation_gives_the_whole_area_to_the_definition(self):
        """一句追问都没有时：对话区收成 0，释义区拿走**全部**剩余高度。

        用户报告的「解释的话像没说完一样」是布局问题（库里存的释义是完整的，
        探针 ``_check/entry_text_peek.py`` 已核对）：旧布局永远按 55/45 分，
        没有对话时那 45% 白空着，释义正文只好被裁成半句话。
        """
        probe = PanelProbe()
        try:
            panel = self._panel_ready(probe)
            self.assertFalse(panel._chat_has_content(), "刚展开、没有任何对话")
            def_h, chat_h = panel.detail_area_heights()
            self.assertEqual(chat_h, 0, "空对话区不占高度")
            avail = (panel._detail_window_height() - 2 * panel._body_padding(MODE_PANEL)
                     - theme.px(geo.PANEL_HEAD_H) - theme.px(geo.DETAIL_FIXED_H))
            self.assertEqual(def_h, max(theme.px(geo.DETAIL_AREA_MIN), avail),
                             "剩余高度全部给释义区（底线 DETAIL_AREA_MIN）")
            self.assertEqual(int(panel.chat_area.canvas.cget("height")), 0,
                             "算出来的 0 必须真的配置到对话区 Canvas 上")
            self.assertEqual(int(panel.def_area.canvas.cget("height")), int(def_h))
        finally:
            probe.close()

    def test_empty_conversation_renders_no_placeholder(self):
        """用户原话：不要显示「暂无对话」——空对话区里一个字都不写。"""
        probe = PanelProbe()
        try:
            panel = self._panel_ready(probe)
            panel._render_chat()
            self.assertEqual(str(panel.chat_text.get("1.0", "end")).strip(), "",
                             "空对话区不许再写「暂无对话」")
            self.assertEqual(int(panel.chat_area.canvas.cget("height")), 0)
        finally:
            probe.close()

    def test_definition_and_chat_split_the_remaining_height(self):
        probe = PanelProbe()
        try:
            panel = self._panel_ready(probe)
            # 有对话（历史 / 回显 / 回答中）时才按比例分，两块都必须活着
            panel._chat_turns = [{"role": "user", "status": "ok", "content": "它和下采样什么关系？"}]
            self.assertTrue(panel._chat_has_content())
            def_h, chat_h = panel.detail_area_heights()
            self.assertGreaterEqual(def_h, theme.px(geo.DETAIL_AREA_MIN))
            self.assertGreaterEqual(chat_h, theme.px(geo.DETAIL_AREA_MIN))
            self.assertGreaterEqual(chat_h, abs(theme.device_px(8)) * 2,
                                    "追问区至少要能显示两行 8pt 正文")
            panel._sync_detail_areas()
            self.assertEqual(int(panel.chat_area.canvas.cget("height")), int(chat_h),
                             "算出来的高度必须**真的**配置到对话区 Canvas 上")
            self.assertEqual(int(panel.def_area.canvas.cget("height")), int(def_h),
                             "释义区同理：高度是显式分配，不是 pack 的副产品")
            avail = (panel._detail_window_height() - 2 * panel._body_padding(MODE_PANEL)
                     - theme.px(geo.PANEL_HEAD_H) - theme.px(geo.DETAIL_FIXED_H))
            self.assertEqual(def_h + chat_h, max(2, avail),
                             "两块之和 = 可用高度（不留空白，也不互相挤没）")
        finally:
            probe.close()

    def test_minimum_panel_keeps_both_areas_alive(self):
        probe = PanelProbe()
        try:
            panel = self._panel_ready(probe)
            panel._chat_turns = [{"role": "assistant", "status": "ok", "content": "都是下采样。"}]
            panel._panel_h = theme.px(geo.PANEL_MIN_H)      # 拖到最小高度
            panel._move_window(0, 0)                        # 真实入口：重算并同步
            def_h, chat_h = panel.detail_area_heights()
            self.assertGreater(def_h, 0, "最小面板下释义区也不能是 0 高度")
            self.assertGreater(chat_h, 0, "最小面板下对话区也不能是 0 高度")
            self.assertEqual(int(panel.chat_area.canvas.cget("height")), int(chat_h))
            self.assertEqual(int(panel.def_area.canvas.cget("height")), int(def_h))
        finally:
            probe.close()

    def test_embedded_frame_and_text_follow_the_canvas_size(self):
        """``ScrollArea`` 内嵌 Frame 的宽高必须跟着 Canvas —— 否则文字被裁掉。"""
        probe = PanelProbe()
        try:
            area = probe.panel.chat_area
            event = mock.Mock(width=280, height=96)
            area._on_canvas(event)
            options = area.canvas.item_options[area._win]
            self.assertEqual(options.get("width"), 280, "内嵌 Frame 宽度 = 画布宽度")
            self.assertEqual(options.get("height"), 96, "fill_both：高度也跟随画布")
            self.assertTrue(probe.panel.chat_text.pack_kw.get("fill"),
                            "Text 必须铺满内嵌 Frame（否则文字只显示一行高度）")
            self.assertTrue(probe.panel.chat_text.pack_kw.get("expand"))
        finally:
            probe.close()


class TestChatQuestionIsImmediatelyVisible(unittest.TestCase):
    """发送后**立刻**看到自己的问题 + 「正在回答…」，结果到达后滚到最新。"""

    def _setup(self, **kw):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        db = stack.enter_context(temp_db())
        bid = db.create_batch("主题")
        eid = int(db.add_entry(batch_id=bid, term="卷积"))
        other = int(db.add_entry(batch_id=bid, term="池化"))
        probe = PanelProbe(db=db, **kw)
        self.addCleanup(probe.close)
        panel = probe.panel
        panel.show_dock()
        panel.toggle()
        panel.open_entry(eid)
        return db, probe, panel, eid, other

    @staticmethod
    def _watch_scroll(panel) -> list:
        calls: list = []
        panel.chat_text.see = lambda *a: calls.append(("see",) + tuple(a))
        panel.chat_text.yview_moveto = lambda f: calls.append(("moveto", f))
        return calls

    def test_send_shows_question_and_busy_then_the_answer(self):
        db, probe, panel, eid, _other = self._setup()
        scrolls = self._watch_scroll(panel)
        panel.entry_input.insert(0, "它和下采样什么关系？")
        self.assertEqual(panel.send_question(), "sent")
        text = str(panel.chat_text.get("1.0", "end"))
        self.assertIn("你：它和下采样什么关系？", text, "自己的问题必须立刻可见")
        self.assertIn("正在回答…", text, "回答中状态必须可见")
        self.assertEqual(panel.current_question(), "", "发送成功后输入框清空")
        self.assertTrue(any(call[0] == "moveto" and float(call[1]) == 1.0
                            for call in scrolls), "新消息在末尾：必须滚到最后一行")
        self.assertTrue(panel._chat_busy)
        self.assertFalse(panel.btn_send.is_enabled(), "忙态下「发送」禁用（回车仍有提示）")
        self.assertEqual(panel.send_question(), "busy", "忙态下不会重复发请求")
        self.assertEqual(len(probe.questions), 1, "守卫挡下的点击绝不产生第二次请求")

        db.add_chat_turn(entry_id=eid, role="assistant", content="都是下采样。",
                         request_id=1, status="ok")
        self.assertTrue(panel.on_chat_result(1, eid, "ok", "都是下采样。"))
        text = str(panel.chat_text.get("1.0", "end"))
        self.assertIn("你：它和下采样什么关系？", text, "回答到达后提问仍然在")
        self.assertIn("词典：都是下采样。", text, "回答必须可见")
        self.assertNotIn("正在回答…", text)
        self.assertFalse(panel._chat_busy)
        self.assertTrue(panel.btn_send.is_enabled())

    def test_no_key_keeps_the_input_and_gives_a_short_hint(self):
        db, probe, panel, _eid, _other = self._setup(config_values={"_has_key": "0"})
        panel.entry_input.insert(0, "为什么不发出去？")
        self.assertEqual(panel.send_question(), "unavailable")
        self.assertEqual(panel.current_question(), "为什么不发出去？",
                         "缺 Key 时必须保留用户输入（不静默丢弃、不伪造回答）")
        hint = str(panel.hint_label.cget("text"))
        self.assertIn("API Key", hint, "缺 Key 要给一句可见的短反馈")
        self.assertEqual(probe.questions[-1][1], "为什么不发出去？")

    def test_late_result_and_entry_switch_do_not_cross_talk(self):
        db, probe, panel, eid, other = self._setup()
        panel.entry_input.insert(0, "第一个词的问题")
        self.assertEqual(panel.send_question(), "sent")
        first_token = panel._chat_request_token
        panel.open_entry(other)                      # 切词
        self.assertNotIn("第一个词的问题", str(panel.chat_text.get("1.0", "end")),
                         "切词后绝不在新词上显示上一个词的对话")
        db.add_chat_turn(entry_id=eid, role="assistant", content="迟到的回答",
                         request_id=int(first_token), status="ok")
        self.assertFalse(panel.on_chat_result(int(first_token), eid, "ok", "迟到的回答"),
                         "迟到结果属于别的词条：必须被丢弃")
        self.assertNotIn("迟到的回答", str(panel.chat_text.get("1.0", "end")))


# ============================================================================
# E. 拖动面：白名单非交互面可拖，其它控件（词卡 / 滚动条 / 手柄 / 按钮）绝不
# ============================================================================
class TestDragSurfaces(unittest.TestCase):
    """拖动只绑**白名单里的非交互面**，绝不绑顶层窗口 / 祖先去「接冒泡」。

    旧实现的顶层绑定是子控件 bindtags 的成员，于是词卡文字 / 画布、滚动条
    Canvas、主题清单选项的按下全被当成空白拖动；改尺寸手柄更糟：手柄自己的
    ``_on_resize_start`` 先设好 ``_drag``，紧接着冒泡上来的 ``_on_drag_start``
    又把它清空，缩放彻底失效。这里把「哪些面有绑定、哪些面一个绑定都没有」
    钉死。
    """

    def test_drag_binds_only_the_whitelist_surfaces(self):
        def _holder(widget):
            """绑定的落点：组合控件（chevron）把绑定转给内部画布。"""
            return getattr(widget, "canvas", widget)

        probe = PanelProbe()
        try:
            panel = probe.panel
            panel.show_dock()
            panel.toggle()
            surfaces = [w for w in panel._drag_surfaces() if w is not None]
            self.assertTrue(surfaces, "拖动白名单不能为空")
            for widget in surfaces:
                binds = _holder(widget).binds
                self.assertEqual(binds.get("<Button-1>"), panel._on_drag_start,
                                 "白名单面必须能直接拖动")
                self.assertEqual(binds.get("<B1-Motion>"), panel._on_drag_motion)
                self.assertEqual(binds.get("<ButtonRelease-1>"), panel._on_drag_end,
                                 "拖动三条绑定一个都不能少")
            for widget in (panel.border_canvas, panel.outer, panel.panel_body,
                           panel.list_page, panel.detail_page, panel.empty_hint,
                           panel.tags_area.canvas, panel.tags_area.inner,
                           panel.dock_body, panel.dock_label,
                           panel.title_row, panel.title_label,
                           panel.source_row, panel.source_label):
                self.assertIn(widget, surfaces, "标题带 / 空白底 / 词表视口都要能拖")
            # chevron 是**组合**包装类（持有一个画布）：绑定必须真的落到那块画布上
            self.assertEqual(panel.topic_caret.canvas.binds.get("<Button-1>"),
                             panel._on_drag_start, "主题行箭头（画布）必须能直接拖动")
            self.assertIn(panel.topic_caret, surfaces)

            # 顶层窗口**没有**拖动绑定：它出现在每个子控件的 bindtags 里，
            # 绑在这里 = 词卡 / 滚动条 / 手柄的按下都会被当成空白拖动。
            for sequence in ("<Button-1>", "<B1-Motion>", "<ButtonRelease-1>"):
                self.assertNotIn(sequence, probe.env.toplevel().binds,
                                 "顶层窗口绝不能绑拖动（子控件事件会冒泡到这里）")

            # 非白名单面：按钮 / 输入框 / 滚动条 / 改尺寸手柄 / 提示行
            # （它们各自有自己的按下语义，但**都不是**面板的拖动三连）
            for widget in (panel.btn_main, panel.btn_close, panel.btn_collapse,
                           panel.btn_action, panel.btn_back, panel.entry_input,
                           panel.tags_area.sb.canvas, panel.resize_grip.canvas,
                           panel.hint_label):
                binds = widget.binds
                self.assertNotEqual(binds.get("<Button-1>"), panel._on_drag_start,
                                    f"{widget!r} 上不得有拖动按下绑定")
                self.assertNotEqual(binds.get("<B1-Motion>"), panel._on_drag_motion,
                                    f"{widget!r} 上不得有拖动位移绑定")
                self.assertNotEqual(binds.get("<ButtonRelease-1>"), panel._on_drag_end,
                                    f"{widget!r} 上不得有拖动松手绑定")
            # 改尺寸手柄自己那条按下绑定必须还在（缩放链路不能被我方白名单碰掉）
            self.assertEqual(panel.resize_grip.canvas.binds.get("<Button-1>"),
                             panel._on_resize_start)
        finally:
            probe.close()

    def test_word_cards_and_scroll_rail_are_not_drag_surfaces(self):
        """词卡（Canvas + Label）与滚动条滑轨都在拖动的**父容器内部**：

        Tk 只在事件真正落在的控件上触发绑定，父 Frame 不在子控件的 bindtags
        里，所以卡片文字上的按下只属于卡片自己（点卡片 = 进详情），绝不会挪窗。
        """
        with temp_db() as db:
            bid = db.create_batch("b")
            db.add_entry(batch_id=bid, term="卷积")
            probe = PanelProbe(db=db)
            try:
                panel = probe.panel
                panel.show_dock(explicit=True)
                panel.expand(explicit=True)
                panel.show_list()
                cards = [c for c in panel.tags_area.children
                         if isinstance(c, widgets.TermCard)]
                self.assertTrue(cards, "必须真的渲染出词卡")
                card = cards[0]
                self.assertEqual(card.canvas.binds.get("<Button-1>"), card._on_click)
                self.assertEqual(card.label.binds.get("<Button-1>"), card._on_click)
                self.assertNotEqual(card.canvas.binds.get("<Button-1>"),
                                    panel._on_drag_start)
                self.assertNotEqual(card.label.binds.get("<Button-1>"),
                                    panel._on_drag_start)
                self.assertIn(panel.tags_area.inner,
                              [w for w in panel._drag_surfaces() if w is not None],
                              "词卡视口本身仍是拖动面（卡片之间的空白）")
                # 卡片自己的点击只进详情，不挪窗口
                before = panel.last_geometry()
                card.canvas.binds["<Button-1>"](None)
                self.assertEqual(panel.page, PAGE_DETAIL)
                self.assertEqual(panel.last_geometry(), before, "点词卡绝不挪窗口")
            finally:
                probe.close()

    def test_click_and_far_release_on_drag_surfaces(self):
        """单击语义不变；**按下后直接抬到远处**（没有 Motion）算拖动、不误展开。"""
        probe = PanelProbe()
        try:
            panel = probe.panel
            panel.show_dock()
            panel.toggle()
            # 标题带拖动：真的挪窗口 + 持久化
            before = panel.last_geometry()
            panel._on_drag_start(FakeEvent(200, 200, widget=panel.title_label))
            panel._on_drag_motion(FakeEvent(240, 250, widget=panel.title_label))
            panel._on_drag_end(FakeEvent(240, 250, widget=panel.title_label))
            self.assertNotEqual(panel.last_geometry(), before, "拖动必须真的挪窗口")
            self.assertIsNotNone(panel.state.panel_rect, "松手必须持久化位置")

            # 单击（没有位移）落在小方块上 = 展开 / 折叠
            panel.collapse_to_dock(reason="test")
            panel._on_drag_start(FakeEvent(10, 10, widget=panel.dock_label))
            panel._on_drag_end(FakeEvent(10, 10, widget=panel.dock_label))
            self.assertEqual(panel.mode, MODE_PANEL, "点小方块必须展开")

            # 按下后**没有 Motion**、直接在小方块外远处松手 = 拖动：
            # 必须按松手点识别出来，绝不能误当成单击展开 / 折叠
            panel.collapse_to_dock(reason="test")
            start = panel._current_rect()
            panel._on_drag_start(FakeEvent(10, 10, widget=panel.dock_label))
            panel._on_drag_end(FakeEvent(240, 260, widget=panel.dock_label))
            self.assertEqual(panel.mode, MODE_DOCK,
                             "远处松手是拖动，不是单击：不得展开面板")
            # 小方块按**自身 44x44** 夹取。这里不能用 panel._min_size()
            # （300x320，展开面板的最小值）当期望 —— 那正是被修掉的错误公式。
            expected = geo.clamp_rect(start[0] + 230, start[1] + 250, 44, 44,
                                      panel._work_area(), min_w=44, min_h=44)
            self.assertEqual(panel._current_rect()[:2], expected[:2],
                             "必须按松手位移移动窗口（与 Motion 同一条公式）")
            self.assertEqual(panel.state.dock_pos, expected[:2], "松手必须持久化位置")
        finally:
            probe.close()

    def test_resize_grip_keeps_its_own_drag_and_resizes(self):
        """手柄的按下只走 ``_on_resize_start``：``_drag`` 不被拖动路径清空。

        旧实现里顶层 ``_on_drag_start`` 会把 ``_drag`` 置 None，
        ``_on_resize_motion`` 随即因为 ``self._drag is None`` 直接返回 —— 缩放失效。
        """
        probe = PanelProbe()
        try:
            panel = probe.panel
            panel.show_dock()
            panel.toggle()
            before = (int(panel._panel_w), int(panel._panel_h))
            grip = panel.resize_grip.canvas
            self.assertEqual(grip.binds.get("<Button-1>"), panel._on_resize_start)
            self.assertEqual(grip.binds.get("<B1-Motion>"), panel._on_resize_motion)
            self.assertEqual(grip.binds.get("<ButtonRelease-1>"), panel._on_resize_end)
            grip.binds["<Button-1>"](FakeEvent(0, 0))
            self.assertIsNotNone(panel._drag, "手柄按下后必须留下改尺寸状态")
            grip.binds["<B1-Motion>"](FakeEvent(60, 40))
            self.assertEqual((int(panel._panel_w), int(panel._panel_h)),
                             (before[0] + 60, before[1] + 40), "拖手柄必须改尺寸")
            grip.binds["<ButtonRelease-1>"](FakeEvent(60, 40))
            self.assertEqual(panel.state.panel_rect[2:], (before[0] + 60, before[1] + 40),
                             "松手必须持久化新尺寸")
        finally:
            probe.close()


# ============================================================================
# C. 浮窗顶栏主题：内联清单（不是 grab 菜单）+ 与主界面侧栏同步
# ============================================================================
class TestTopicPickerInline(unittest.TestCase):
    def test_clicking_the_short_topic_opens_an_inline_list(self):
        with temp_db() as db:
            db.create_batch("主题甲")
            db.create_batch("主题乙")
            probe = PanelProbe(db=db)
            try:
                panel = probe.panel
                panel.show_dock()
                panel.toggle()
                popups: list = []
                probe.stub.open_app_menu = lambda panel=None: popups.append(True)
                self.assertFalse(panel._topic_open)
                # 单击（未位移）：按在来源行上 → 展开清单
                panel._on_drag_start(FakeEvent(10, 10, widget=panel.source_label))
                panel._on_drag_end(FakeEvent(10, 10, widget=panel.source_label))
                self.assertTrue(panel._topic_open, "点短主题必须展开内联清单")
                labels = [str(w.cget("text")) for w in panel.topic_picker.winfo_children()]
                self.assertTrue(any("跟随当前阅读页" in t for t in labels))
                self.assertTrue(any("主题甲" in t for t in labels))
                self.assertTrue(any("主题乙" in t for t in labels))
                self.assertEqual(popups, [], "绝不使用 tk_popup / grab 菜单（NOACTIVATE 下失效）")

                row = [w for w in panel.topic_picker.winfo_children()
                       if "主题甲" in str(w.cget("text"))][0]
                row.binds["<Button-1>"](None)
                self.assertFalse(panel._topic_open, "选完自动收起")
                self.assertIsNotNone(probe.browse["id"], "选择必须落到宿主的浏览范围")
                self.assertIn("主题甲", str(panel.source_label.cget("text")),
                              "顶栏改为显示正在浏览的主题")
                self.assertTrue(str(panel.hint_label.cget("text")),
                                "切主题必须有短反馈")

                # 再点开 → 选「跟随当前阅读页」回到当前页
                panel.toggle_topic_picker(True)
                first = [w for w in panel.topic_picker.winfo_children()
                         if "跟随当前阅读页" in str(w.cget("text"))][0]
                first.binds["<Button-1>"](None)
                self.assertIsNone(probe.browse["id"])
            finally:
                probe.close()

    def test_clicking_the_chevron_canvas_toggles_the_picker(self):
        """chevron 是**组合控件**：真实 Tk 把 ``event.widget`` 报成内部画布。

        箭头必须和来源行一样算「主题行」—— 否则实机上点箭头毫无反应（假 Tk 里
        用包装对象当 event.widget 就会漏掉这种情况）。两种对象都要能开合清单，
        而且**拖动**路径也要认得出这行：按在箭头上拖 = 真的挪窗口，不误开清单。
        """
        probe = PanelProbe()
        try:
            panel = probe.panel
            panel.show_dock()
            panel.toggle()
            caret = panel.topic_caret
            self.assertFalse(panel._topic_open)
            for target in (caret, caret.canvas):
                panel._on_drag_start(FakeEvent(10, 10, widget=target))
                panel._on_drag_end(FakeEvent(10, 10, widget=target))
                self.assertTrue(panel._topic_open, f"点 {target!r} 必须展开清单")
                self.assertEqual(caret.direction(), "up", "展开时箭头朝上")
                panel._on_drag_start(FakeEvent(10, 10, widget=target))
                panel._on_drag_end(FakeEvent(10, 10, widget=target))
                self.assertFalse(panel._topic_open, "再点一次收起")
                self.assertEqual(caret.direction(), "down", "收起时箭头朝下")

            # 拖动路径（同一块组合画布）：真的挪窗口，松手绝不当成点击
            self.assertFalse(panel._topic_open)
            before = panel.last_geometry()
            panel._on_drag_start(FakeEvent(300, 300, widget=caret.canvas))
            panel._on_drag_motion(FakeEvent(330, 350, widget=caret.canvas))
            panel._on_drag_end(FakeEvent(330, 350, widget=caret.canvas))
            self.assertNotEqual(panel.last_geometry(), before,
                                "按在主题行箭头上拖动必须真的挪窗口（不能像点了没反应）")
            self.assertFalse(panel._topic_open, "拖动不是点击：不得展开 / 收起清单")
            self.assertEqual(caret.direction(), "down")
        finally:
            probe.close()

    def test_panel_and_main_sidebar_share_one_browse_scope(self):
        with headless_app(overlays="panel", main_window="real") as app:
            bid = app.db.create_batch("历史主题")
            app.main.refresh_batches()
            panel = app.reading_panel
            panel.show_dock()
            panel.toggle()
            self.assertTrue(panel.toggle_topic_picker(True))
            row = [w for w in panel.topic_picker.winfo_children()
                   if "历史主题" in str(w.cget("text"))]
            self.assertTrue(row, "清单里必须能看到历史主题")
            row[0].binds["<Button-1>"](None)
            self.assertEqual(app.browse_scope(), bid, "浮窗选主题 → 宿主浏览范围")
            self.assertEqual(app.main.browse_batch_id(), bid, "主界面侧栏同步同一主题")
            # 反向：主界面侧栏选主题 → 浮窗词表跟着换
            app.main.set_browse_scope(None)
            self.assertIsNone(panel.browse_scope())

    def test_picking_another_topic_leaves_the_detail_page_for_that_topics_list(self):
        """详情页停在旧主题词条时切主题：回到**所选主题**的词表页，而且只换页 ——
        不重新映射窗口、不自动收起（折叠成小方块）。"""
        with temp_db() as db:
            topic_a = int(db.create_batch("主题甲"))
            topic_b = int(db.create_batch("主题乙"))
            db.add_entry(batch_id=topic_a, term="甲词", context="甲的上下文")
            term_b = int(db.add_entry(batch_id=topic_b, term="乙词", context="乙的上下文"))
            probe = PanelProbe(db=db)
            try:
                panel = probe.panel
                panel.show_dock()
                self.assertTrue(panel.toggle(), "先展开面板")
                probe._set_browse(topic_b)
                self.assertTrue(panel.open_entry(term_b, expand=False), "打开乙词详情")
                self.assertEqual(panel.page, PAGE_DETAIL)
                self.assertIn("乙词", str(panel.term_title.cget("text")))

                maps = probe.deiconify_count()
                hides = probe.withdraw_count()
                geometry = probe.geometry_specs()
                self.assertTrue(panel.toggle_topic_picker(True))
                row = [w for w in panel.topic_picker.winfo_children()
                       if "主题甲" in str(w.cget("text"))]
                self.assertTrue(row, "清单里必须能看到主题甲")
                row[0].binds["<Button-1>"](None)

                self.assertEqual(panel.page, PAGE_LIST, "详情页必须回到所选主题词表")
                self.assertEqual([str(card.cget("text")) for card in panel.tags_area.children],
                                 ["甲词"], "词表必须是**所选主题**的词")
                self.assertEqual(probe.browse["id"], topic_a, "浏览范围确实切过去了")
                self.assertEqual(panel.mode, MODE_PANEL, "切主题不得把面板折叠成小方块")
                self.assertTrue(panel.visible)
                self.assertEqual(probe.deiconify_count(), maps, "不得重新映射窗口")
                self.assertEqual(probe.withdraw_count(), hides, "不得隐藏窗口")
                self.assertEqual(probe.geometry_specs(), geometry,
                                 "不得重设窗口位置 / 尺寸")
                self.assertFalse(panel._topic_open, "内联清单选完仍然收起")
                self.assertTrue(str(panel.hint_label.cget("text")), "切主题要有短反馈")
            finally:
                probe.close()

    def test_picking_a_topic_from_the_panel_keeps_the_pages_save_target(self):
        """浮窗清单切主题只影响浏览：新词仍然落回**当前阅读页**的主题。"""
        with headless_app(overlays="panel", main_window="real") as app:
            panel = app.reading_panel
            panel.show_dock()
            self.assertTrue(panel.toggle())
            with mock.patch("app.capture_service.w32", app.fake_tk.w32):
                app.capture_service.note_foreground(
                    dict(DEFAULT_HEADLESS_FG, title="Some Document", hwnd=55501))
            src = app.capture_service.last_source()
            first, _ = app.capture_service.manual_entry("这一页的词", source=src)
            page_topic = int(app.db.get_entry(first)["batch_id"])
            other = int(app.db.create_batch("别的主题"))
            app.db.add_entry(batch_id=other, term="别的词", context="别的上下文")

            panel.refresh_terms(force=True)
            self.assertTrue(panel.toggle_topic_picker(True))
            row = [w for w in panel.topic_picker.winfo_children()
                   if "别的主题" in str(w.cget("text"))]
            self.assertTrue(row, "清单里必须能看到别的主题")
            row[0].binds["<Button-1>"](None)
            self.assertEqual(app.browse_scope(), other, "清单选择落到宿主浏览范围")

            second, created = app.capture_service.manual_entry("第二个词", source=src)
            self.assertTrue(created)
            self.assertEqual(int(app.db.get_entry(second)["batch_id"]), page_topic,
                             "浏览别的主题绝不改变新词的保存位置")
            self.assertEqual(app.db.count_entries(other), 1, "别的主题不得被写入")


if __name__ == "__main__":
    unittest.main()
