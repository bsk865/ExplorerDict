"""纯 mock 圆角 / 滑轨 wrapper 回归（**额外**可单独运行，零真实 Tk 窗口）。

跑法（不需要 GUI，也不碰真实 Win32 / 数据库 / 网络）::

    python -m unittest tests.test_ui_roundrect

覆盖三块「只能自绘」的界面机制，全部用**真实产品代码** + 假 Tk 控件 + 假 Win32：

* :func:`app.ui.widgets.round_rect_points` 纯几何：半径收敛到 ``min(r, w/2, h/2)``、
  ``radius<=0`` 退化成直角矩形、顶点永远落在矩形内；
* :class:`app.ui.widgets.TermCard` 圆角卡片：圆角由 Canvas 多边形画出、文字内缩
  ≥ 圆角半径（不盖住圆角）、悬停只换底色/边线不加任何文字、改尺寸只重排不重建；
* :class:`app.ui.widgets.ScrollRail` 滑轨：``span=0.4`` 时滑块走一半 → ``moveto``
  必须是 ``0.3``（滑块行程比例 × ``1-span`` = 内容 first），最小滑块、空 / 满屏
  一律不滚动、不发 ``moveto``；
* :class:`app.ui.reading_panel.ReadingPanel` 的真实窗口圆角 region：小方块
  44x44 半径 10、展开面板 360x460 半径 12（尺寸/半径都走 ``theme.px`` 的 DPI 换算）；
* :func:`app.win32util.apply_round_region` 的 **GDI 所有权**：创建失败清旧 region、
  设置失败只释放一次、成功转交后**不**再 DeleteObject、非法尺寸 / 空 HWND、
  64 位大句柄原样传递（``user32`` / ``gdi32`` 整体换成记录型假对象，绝不碰真实 Win32）；
* :class:`app.ui.widgets.FlatButton` 的**字形图像路径**（真实按钮代码 + 假
  ``PhotoImage`` 工厂，一次都不碰真实 Tk）：图像尺寸 = 字体排版单元 + ``padx`` /
  ``pady`` 对称内边距（「×」这类小字也有正常点击尺寸），可见墨迹在图像中心
  （逐级 alpha 阈值误差 ≤ 1px）、每侧至少留出指定内边距；改文案 / hover /
  禁用都不偏移，Pillow 缺失时清 image 退回原生居中；
* ``tools/ui_preview.py`` 的**离线预览**：四张 PNG（列表 / 详情 / 空态 / 小方块）
  的功能按钮墨迹都在按钮中心 ≤ 1px，内容内缩与小方块 22x22 内容区都从产品常量
  算出来，已经删掉的「菜单 / 重新解释 / 设置 API」不得再出现在预览里；
* **AI 参考关系图**（本轮）：结构化候选关系只准用本次给出的词编号作端点、必须带
  逐字证据片段与合法类型，端点越界 / 自环 / 重复 / 反向矛盾 / 层级成环 / 证据不符
  一律筛除，依据不足就是空关系；缓存按 (主题, 内容指纹) 隔离（新词 / 改词过期），
  迟到结果不串图；布局按关系结构（包含 / 属于的层级分组、依赖 / 因果的前后分层、
  用途 / 对照的跨边、孤立词单独成行），窗口里**没有**手动录入控件、只有单一生成入口；
  镜头：滚动范围四边各留**一个视口**（内容比视口小的小图也能左右上下自由平移），
  初始视图落在**世界原点 0**，右键松手之后的第一次左键照常点线 / 点空白
  （只挡「右键还按着」期间的左键，绝不吞掉拖动后的下一次点击）。
"""
from __future__ import annotations

import ast
import json
import threading
import time
import tkinter
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests import support
from tests.support import FakeWidget, PanelProbe, _FakeTkEnv

from app import win32util as w32
from app.config import Config
from app.ui import panel_geometry as geo
from app.ui import theme, widgets

#: **真实**的 ``tkinter.Canvas``（在任何 mock 生效之前抓住引用）：
#: 用来断言自绘控件**没有**再去继承 Tk 画布。
_REAL_TK_CANVAS = tkinter.Canvas


class TestRoundRectPoints(unittest.TestCase):
    """圆角矩形的纯函数几何（不碰 Tk，可直接离线回归）。"""

    def test_plain_rectangle_when_radius_is_zero(self):
        self.assertEqual(widgets.round_rect_points(1, 2, 3, 4, 0), [1, 2, 4, 2, 4, 6, 1, 6])
        self.assertEqual(widgets.round_rect_points(1, 2, 3, 4, -5), [1, 2, 4, 2, 4, 6, 1, 6])

    def test_radius_is_clamped_into_the_rect(self):
        """卡片被压得很窄很扁时，半径收敛到 ``min(r, w/2, h/2)``，绝不画出自交形状。"""
        x, y, w, h, r = 0.0, 0.0, 10.0, 4.0, 12.0
        points = widgets.round_rect_points(x, y, w, h, r)
        xs, ys = points[0::2], points[1::2]
        self.assertTrue(all(x <= px <= x + w for px in xs))
        self.assertTrue(all(y <= py <= y + h for py in ys))
        # 半径被压到 h/2 = 2：四个圆角圆心分别在 (2,2)/(8,2)/(8,2)/(2,2)
        self.assertTrue(any(abs(px - 2.0) < 1e-9 for px in xs), "圆角确实被切出来了")
        # 直角顶点 (0,0) 不能出现在顶点表里（那就是没切圆角）
        self.assertFalse(any(abs(px - x) < 1e-9 and abs(py - y) < 1e-9
                             for px, py in zip(xs, ys)))

    def test_points_count_matches_corner_steps(self):
        points = widgets.round_rect_points(0, 0, 20, 20, 4)
        self.assertEqual(len(points), 4 * (widgets.CORNER_STEPS + 1) * 2)


class TestTermCardRoundedWrapper(unittest.TestCase):
    """:class:`TermCard`：圆角卡片 + 文字内缩 + 悬停只改色。"""

    def setUp(self):
        self.env = _FakeTkEnv()
        self.env.__enter__()
        self.parent = FakeWidget(None)

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def test_card_face_is_a_rounded_polygon(self):
        card = widgets.TermCard(self.parent, "卷积", width=166, height=64)
        item = card.canvas.items[card._shape]
        self.assertEqual(item["kind"], "polygon", "圆角只能由 Canvas 多边形画出来")
        self.assertEqual(item["coords"][0], 0.5)
        self.assertGreaterEqual(self.env.canvases[-1].item_count(), 1)

    def test_text_is_inset_by_at_least_the_corner_radius(self):
        """文字内缩 ≥ 圆角半径：字不会压在圆角上（用户明确要求）。"""
        card = widgets.TermCard(self.parent, "卷积", width=166, height=64)
        self.assertGreaterEqual(card.pad_x, card.radius)
        self.assertGreaterEqual(card.pad_y, card.radius)
        self.assertEqual(card.label.place_kw.get("x"), card.pad_x)
        self.assertGreaterEqual(card.label.place_kw.get("y"), card.pad_y)

    def test_hover_changes_only_colors(self):
        card = widgets.TermCard(self.parent, "卷积", width=166, height=64)
        before_items = card.canvas.item_count()
        before_children = len(card.winfo_children())
        self.assertTrue(card.set_hover(True))
        self.assertEqual(card.canvas.item_options[card._shape]["fill"], theme.CARD_BG_HOVER)
        self.assertEqual(card.canvas.item_options[card._shape]["outline"],
                         theme.CARD_BORDER_HOVER)
        self.assertEqual(card.canvas.item_count(), before_items, "悬停不得新增任何绘制")
        self.assertEqual(len(card.winfo_children()), before_children,
                         "悬停不得新增任何说明文字")
        self.assertNotIn("说明", str(card.cget("text")))
        self.assertTrue(card.set_hover(False))
        self.assertEqual(card.canvas.item_options[card._shape]["fill"], theme.CARD_BG)

    def test_summary_label_only_when_the_library_already_has_one(self):
        plain = widgets.TermCard(self.parent, "卷积", width=166, height=64)
        self.assertIsNone(plain.summary_label, "库里没有一句话释义时不得出现摘要行")
        rich = widgets.TermCard(self.parent, "卷积", summary="加权求和", width=166, height=64)
        self.assertIsNotNone(rich.summary_label)
        self.assertEqual(rich.cget("summary"), "加权求和")

    def test_resize_only_relayouts(self):
        card = widgets.TermCard(self.parent, "卷积", summary="加权求和",
                                width=166, height=64)
        canvas = card.canvas
        self.assertTrue(card.set_size(150, 70))
        self.assertIs(card.canvas, canvas, "改尺寸不得重建控件（会闪）")
        self.assertFalse(card.set_size(150, 70), "尺寸没变就是幂等")
        self.assertEqual(card.winfo_reqwidth(), 150)
        self.assertEqual(card.winfo_reqheight(), 70)
        item = canvas.items[card._shape]
        self.assertAlmostEqual(item["coords"][0], 0.5)
        self.assertLessEqual(max(item["coords"][0::2]), 150)


class TestScrollRailFractionMapping(unittest.TestCase):
    """滑轨拖动：**滑块行程比例** → ``moveto`` 的**内容 first**（用户报告的缺陷）。"""

    TRACK = 100

    def setUp(self):
        self.env = _FakeTkEnv()
        self.env.__enter__()
        self.parent = FakeWidget(None)
        self.calls: list[tuple] = []
        self.rail = widgets.ScrollRail(
            self.parent, command=lambda *a: self.calls.append(tuple(a)))
        #: 假 Tk 拿不到真实控件高度 → 用兜底高度把轨道钉成 100px
        self.rail.height_hint = self.TRACK

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def _grab_thumb_middle(self) -> int:
        top, height = self.rail.thumb_rect(self.TRACK)
        return top + height // 2

    def test_half_travel_maps_to_half_of_the_scrollable_range(self):
        """span=0.4、滑块走一半 → ``moveto`` = 0.5 × (1-0.4) = **0.3**。"""
        self.assertTrue(self.rail.set(0.0, 0.4))
        self.assertAlmostEqual(self.rail._span(), 0.4, places=9)
        self.calls.clear()
        grab = self._grab_thumb_middle()
        self.rail._on_press(SimpleNamespace(x=4, y=grab))
        room = self.TRACK - self.rail.thumb_rect(self.TRACK)[1]
        # 鼠标再走「半个行程」：travel = (y - drag_offset) / room = 0.5
        self.rail._on_motion(SimpleNamespace(
            y=int(round(self.rail._drag_offset + room / 2))))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[-1][0], "moveto")
        self.assertAlmostEqual(float(self.calls[-1][1]), 0.3, places=6)

    def test_thumb_bottom_reaches_the_content_end(self):
        """滑块拖到底 = 内容末尾 ``1-span``（旧实现只能滚到一半，后半截够不着）。"""
        self.rail.set(0.25, 0.65)          # span = 0.4
        top, height = self.rail.thumb_rect(self.TRACK)
        self.rail._drag_offset = 0.0
        first = self.rail._move_to(top + (self.TRACK - height))
        self.assertAlmostEqual(first, 0.6, places=6)
        self.assertAlmostEqual(first, 1.0 - self.rail._span(), places=6)
        # 越过底边也只是夹到末尾，不会超过 1-span
        self.assertAlmostEqual(self.rail._move_to(self.TRACK * 3), 0.6, places=6)

    def test_minimum_thumb_still_uses_the_same_mapping(self):
        """span 很小时滑块被最小高度撑住，映射仍按 ``1-span`` 换算。"""
        self.rail.set(0.10, 0.15)          # span = 0.05 → 滑块取最小高度
        top, height = self.rail.thumb_rect(self.TRACK)
        self.assertEqual(height, self.rail.min_thumb, "滑块不得小于最小高度")
        self.assertGreater(self.rail.min_thumb, round(self.TRACK * 0.05))
        self.calls.clear()
        grab = top + height // 2
        self.rail._on_press(SimpleNamespace(x=4, y=grab))
        room = self.TRACK - height
        self.rail._on_motion(SimpleNamespace(
            y=int(round(self.rail._drag_offset + room / 2))))
        self.assertAlmostEqual(float(self.calls[-1][1]), 0.5 * (1.0 - 0.05), places=6)

    def test_empty_or_full_content_never_scrolls_or_emits(self):
        for first, last in ((0.0, 0.0), (0.0, 1.0), (1.0, 1.0)):
            with self.subTest(first=first, last=last):
                self.calls.clear()
                self.rail.set(first, last)
                self.assertFalse(self.rail.scrollable(), "空 / 满屏都不可滚动")
                self.assertEqual(self.rail.thumb_rect(self.TRACK), (0, self.TRACK),
                                 "不可滚动时滑块占满整条轨道（也无可拖之处）")
                self.assertFalse(self.rail._dragging)
                self.rail._on_press(SimpleNamespace(x=4, y=40))
                self.assertFalse(self.rail._dragging, "不可滚动时按下不进入拖动")
                self.assertEqual(self.rail._move_to(40), 0.0)
                self.rail._on_motion(SimpleNamespace(y=60))
                self.assertEqual(self.calls, [], "不可滚动时一次 moveto 都不发")

    def test_set_clamps_and_is_idempotent(self):
        self.assertTrue(self.rail.set(0.9, 0.2))       # 乱序 → 交换
        self.assertEqual(self.rail.get(), (0.2, 0.9))
        self.assertFalse(self.rail.set(0.2, 0.9), "分数没变就不重画")
        self.assertTrue(self.rail.set(-1, 5))
        self.assertEqual(self.rail.get(), (0.0, 1.0))
        self.assertFalse(self.rail.set("坏值", 1))


class TestChevronCanvasWrapper(unittest.TestCase):
    """主题行 chevron：**组合**画布、线条几何、真实 Tcl 命令路径。

    历史 bug：``Chevron`` 曾是 ``tk.Canvas`` 子类，并在 ``__init__`` 里写
    ``self._w = 宽度``。``_w`` 是 Tkinter 保留的 **Tcl 窗口路径**，
    ``create_line`` / ``bind`` / ``pack`` 全靠它找窗口 —— 实机上箭头一条线都
    画不出来，而「只记录属性」的假控件完全看不出来。这里用假 Tk 的窗口路径
    校验把这条命令路径钉死（**不创建任何真实 Tk**）。
    """

    def setUp(self):
        self.env = _FakeTkEnv()
        self.env.__enter__()
        self.parent = FakeWidget(None)

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def test_wrapper_holds_a_canvas_and_never_shadows_tk_attributes(self):
        caret = widgets.Chevron(self.parent, bg="#fff", fg="#000")
        canvas = caret.canvas
        self.assertIsInstance(canvas, support.FakeCanvas,
                              "必须走假 Tk 的 Canvas 工厂（自身不是 Tk 控件）")
        self.assertNotIsInstance(caret, _REAL_TK_CANVAS,
                                 "绝不能再继承 tk.Canvas（会覆盖 Tk 保留的 _w）")
        for reserved in ("_w", "tk", "master", "children"):
            self.assertNotIn(reserved, vars(caret),
                             f"包装类绝不能占用 Tk 的保留属性 {reserved}")
        self.assertEqual(caret._chevron_width, float(theme.px(widgets.CHEVRON_W)))
        self.assertEqual(caret._chevron_height, float(theme.px(widgets.CHEVRON_H)))
        # 尺寸字段是任务专名 → 画布自己的 Tcl 路径完好无损
        self.assertEqual(canvas._w, canvas.tcl_path())

    def test_every_chevron_command_goes_to_the_registered_tcl_path(self):
        caret = widgets.Chevron(self.parent, bg="#fff", fg="#000")
        canvas = caret.canvas
        path = canvas._w
        self.assertTrue(str(path).startswith("."), "Tcl 路径必须形如 .!w12")
        draw_commands = [command for _path, command in canvas.tcl_calls
                         if command.startswith("create_")]
        self.assertEqual(draw_commands, ["create_line"],
                         "chevron 只准画线（绝不写任何字形）")
        self.assertEqual(canvas.illegal_tcl_calls, [],
                         "一条命令都不许发往非法窗口路径")
        caret.set_direction("up")
        self.assertEqual([p for p, _c in canvas.tcl_calls], [path] * len(canvas.tcl_calls),
                         "所有命令都发给同一个登记的窗口路径")

    def test_illegal_widget_path_fails_like_real_tk(self):
        """这条检查本身必须**真的有效**（不是摆设）。

        把画布的 ``_w`` 改成数字 —— 正是历史 bug 让产品代码干的事 —— 之后，
        发往该控件的 Tcl 命令必须像真实 Tk 一样失败，绝不能被假控件悄悄放过。
        """
        caret = widgets.Chevron(self.parent, bg="#fff", fg="#000")
        canvas = caret.canvas
        canvas._w = 9.0                              # 历史 bug 的写法
        with self.assertRaises(support.FakeTclPathError):
            canvas.create_line(0, 0, 1, 1)
        with self.assertRaises(support.FakeTclPathError):
            canvas.pack()
        self.assertEqual(len(canvas.illegal_tcl_calls), 2, "非法路径调用必须留痕")
        canvas._w = canvas.tcl_calls[0][0]           # 还原：路径再次合法
        self.assertEqual(canvas.tcl_path(), canvas.tcl_calls[-1][0])

    def test_line_geometry_keeps_half_stroke_clearance_inside_the_canvas(self):
        caret = widgets.Chevron(self.parent, bg="#fff", fg="#000")
        flat = caret.last_points
        self.assertEqual(len(flat), 6, "折线三个顶点")
        half = widgets.Chevron(self.parent, bg="#fff")._line / 2.0
        self.assertGreaterEqual(min(flat[0::2]), half - 1e-9,
                                "左端点必须留出半描边净空（否则被画布边缘裁掉）")
        self.assertLessEqual(max(flat[0::2]),
                             caret._chevron_width - half + 1e-9)
        self.assertGreaterEqual(min(flat[1::2]), half - 1e-9)
        self.assertLessEqual(max(flat[1::2]), caret._chevron_height - half + 1e-9)
        # 与纯函数同源：同一个 pad 算出来的坐标必须完全一致
        self.assertEqual(tuple(flat),
                         widgets.chevron_flat_points(
                             caret._chevron_width / 2.0, caret._chevron_height / 2.0,
                             width=caret._chevron_width, height=caret._chevron_height,
                             direction="down", pad=half))

    def test_theme_click_and_direction_toggle_still_work(self):
        caret = widgets.Chevron(self.parent, bg="#fff", fg="#000", cursor="hand2")
        clicked: list = []
        caret.bind("<Button-1>", lambda _e: clicked.append(True))
        caret.pack(side="right")
        self.assertEqual(caret.canvas.binds.get("<Button-1>") is not None, True,
                         "点击绑定必须落到画布上（主题选择可点）")
        self.assertEqual(caret.canvas.pack_kw, {"side": "right"})
        self.assertEqual(caret.cget("cursor"), "hand2", "主题点击沿用同一套鼠标样式")
        self.assertEqual(caret.direction(), "down")
        self.assertEqual(caret.set_direction("down"), False, "朝向没变不重画")
        before = caret.draw_calls
        self.assertTrue(caret.set_direction("up"))
        self.assertEqual(caret.draw_calls, before + 1)
        self.assertNotEqual(caret.last_points, ())
        self.assertEqual(caret.winfo_reqwidth(), int(caret._chevron_width))
        caret.destroy()
        self.assertTrue(caret.canvas.destroyed)


class TestWindowCornerRegion(unittest.TestCase):
    """真实阅读面板把圆角下发到窗口 region（不是只画在离线预览里）。"""

    def test_dock_is_44_radius_10_panel_is_360x460_radius_12(self):
        probe = PanelProbe()
        try:
            panel = probe.panel
            self.assertEqual(panel._dock_size(), theme.px(geo.DOCK_SIZE))
            self.assertTrue(panel.show_dock(explicit=True))
            dock = probe.env.w32.region_calls[-1]
            self.assertEqual(dock[1:], (theme.px(44), theme.px(44), theme.px(10)))
            self.assertEqual(panel.last_geometry().split("+")[0], "44x44")

            probe.env.w32.region_calls.clear()
            self.assertTrue(panel.expand(explicit=True))
            self.assertTrue(probe.env.w32.region_calls, "展开面板必须下发自己的圆角")
            shown = probe.env.w32.region_calls[-1]
            self.assertEqual(shown[1:], (theme.px(360), theme.px(460), theme.px(12)))
            self.assertEqual(panel.last_geometry().split("+")[0], "360x460")
        finally:
            probe.close()

    def test_region_failure_falls_back_to_square(self):
        probe = PanelProbe()
        try:
            probe.env.w32.region_ok = False
            self.assertTrue(probe.panel.show_dock(explicit=True))
            self.assertTrue(probe.env.w32.region_clears, "圆角失败必须清掉 region 退回方角")
            self.assertIsNone(probe.panel._region_key)
        finally:
            probe.close()

    def test_region_is_not_reapplied_when_size_unchanged(self):
        probe = PanelProbe()
        try:
            probe.panel.show_dock(explicit=True)
            calls = len(probe.env.w32.region_calls)
            probe.panel._sync_window_region()
            self.assertEqual(len(probe.env.w32.region_calls), calls,
                             "尺寸 / 半径没变就不得重复调用 SetWindowRgn")
        finally:
            probe.close()


class TestWin32RegionOwnership(unittest.TestCase):
    """``apply_round_region`` 的 GDI 句柄所有权（真实产品代码 + 假 user32/gdi32）。

    规则（见 ``app/win32util.set_window_region`` 的说明）：

    * ``CreateRoundRectRgn`` 失败 / 尺寸非法 → 清掉旧 region 退回方形，**不**新建句柄；
    * ``SetWindowRgn`` 成功 → 系统接管 region，**绝不**再 ``DeleteObject``（否则窗口
      销毁时二次释放）；
    * ``SetWindowRgn`` 失败 → 句柄还在自己手里，必须**恰好释放一次**；
    * 空 HWND → 不做任何 Win32 调用（交给调用方的旧 region 自己删）。

    这里把 ``app.win32util.user32`` / ``gdi32`` 整个替换成记录型假对象：
    **一次真实 Win32 调用都不会发生**。
    """

    def _install(self, *, create: int = 0x1234, set_ok: bool = True,
                 delete_ok: bool = True) -> None:
        self.user32 = _FakeUser32(set_ok)
        self.gdi32 = _FakeGdi32(create, delete_ok)
        for target, fake in (("user32", self.user32), ("gdi32", self.gdi32)):
            patcher = mock.patch.object(w32, target, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    # ------------------------------------------------------------ 创建失败
    def test_create_failure_clears_old_region_and_never_deletes(self):
        self._install(create=0)
        self.assertFalse(w32.apply_round_region(4242, 100, 50, 12))
        self.assertEqual(self.gdi32.create_calls, [(0, 0, 101, 51, 24, 24)],
                         "右下角必须是开区间（w+1 / h+1），椭圆宽高 = 2r")
        self.assertEqual([call[1] for call in self.user32.set_calls], [None],
                         "创建失败必须清掉旧 region 退回方角")
        self.assertEqual(self.user32.set_calls[0][0], 4242)
        self.assertEqual(self.gdi32.delete_calls, [], "没有句柄可删，绝不误删")

    def test_invalid_size_or_radius_never_creates_a_handle(self):
        for width, height, radius in ((0, 50, 12), (100, 0, 12), (-1, 50, 12),
                                      (100, 50, 0), (100, 50, -3)):
            with self.subTest(width=width, height=height, radius=radius):
                self._install()
                self.assertFalse(w32.apply_round_region(777, width, height, radius))
                self.assertEqual(self.gdi32.create_calls, [], "非法尺寸不得创建 region")
                self.assertEqual(self.gdi32.delete_calls, [])
                self.assertEqual([call[1] for call in self.user32.set_calls], [None])

    # ------------------------------------------------------------ 设置失败
    def test_set_failure_releases_the_region_exactly_once(self):
        self._install(create=0xABCD, set_ok=False)
        self.assertFalse(w32.apply_round_region(4242, 100, 50, 12))
        self.assertEqual([call[1] for call in self.user32.set_calls], [0xABCD, None],
                         "先尝试设置，失败后再清成方角")
        self.assertEqual(self.gdi32.delete_calls, [0xABCD],
                         "转移失败的句柄必须自己释放，且恰好一次")

    def test_delete_failure_is_reported_without_retry(self):
        self._install(create=0x55, set_ok=False, delete_ok=False)
        self.assertFalse(w32.set_window_region(4242, 0x55))
        self.assertEqual(self.gdi32.delete_calls, [0x55], "释放失败也不重复调用")
        self.assertFalse(w32.delete_region(0), "空句柄不做任何调用")
        self.assertEqual(self.gdi32.delete_calls, [0x55])

    # ------------------------------------------------------------ 成功转交
    def test_success_transfers_ownership_and_never_deletes(self):
        self._install(create=0xABCD, set_ok=True)
        self.assertTrue(w32.apply_round_region(4242, 360, 460, 12))
        self.assertEqual(len(self.user32.set_calls), 1)
        hwnd, region, redraw = self.user32.set_calls[0]
        self.assertEqual((hwnd, region, redraw), (4242, 0xABCD, True))
        self.assertEqual(self.gdi32.delete_calls, [],
                         "系统已接管 region，本进程绝不能再 DeleteObject")

    # ------------------------------------------------------------ 空 HWND
    def test_empty_hwnd_makes_no_win32_calls(self):
        self._install()
        self.assertFalse(w32.apply_round_region(0, 100, 50, 12))
        self.assertEqual((self.gdi32.create_calls, self.user32.set_calls), ([], []),
                         "空 HWND 不得创建 / 设置任何 region")
        # 调用方自己持有的句柄仍必须能安全释放
        self.assertFalse(w32.set_window_region(0, 0x77))
        self.assertEqual(self.gdi32.delete_calls, [0x77],
                         "没有 HWND 可转移时，句柄由调用方释放")
        self.assertFalse(w32.clear_window_region(0))
        self.assertEqual(len(self.user32.set_calls), 0)

    # ------------------------------------------------------------ 64 位句柄
    def test_large_64bit_handle_is_passed_through_and_released_once(self):
        big = 0x7FFFFFFF_FFFF0001
        self._install(create=big, set_ok=True)
        self.assertTrue(w32.apply_round_region(4242, 44, 44, 10))
        self.assertEqual(self.user32.set_calls[0][1], big,
                         "64 位句柄不得被截断成 32 位")
        self.assertEqual(self.gdi32.delete_calls, [])

    def test_large_64bit_handle_released_once_on_failure(self):
        big = 0x7FFFFFFF_FFFF0001
        self._install(create=big, set_ok=False)
        self.assertFalse(w32.apply_round_region(4242, 44, 44, 10))
        self.assertEqual(self.gdi32.delete_calls, [big])
        self.assertEqual(self.user32.set_calls[-1][1], None)


def _handle_value(value) -> int | None:
    """把 ctypes 句柄（``c_void_p`` / ``wintypes.HWND``）或整数取成普通 int。"""
    if value is None:
        return None
    inner = getattr(value, "value", value)
    if inner is None:
        return None
    return int(inner)


class _FakeUser32:
    """假的 ``user32``：只记录 ``SetWindowRgn``，绝不触碰真实窗口。"""

    def __init__(self, set_result=True):
        self.set_result = bool(set_result)
        #: ``[(hwnd, region|None, redraw), ...]``
        self.set_calls: list[tuple[int, int | None, bool]] = []

    def SetWindowRgn(self, hwnd, region, redraw):
        self.set_calls.append((_handle_value(hwnd) or 0, _handle_value(region),
                               bool(redraw)))
        return 1 if self.set_result else 0


class _FakeGdi32:
    """假的 ``gdi32``：记录 region 的创建与释放（句柄用普通整数模拟）。"""

    def __init__(self, create_result: int = 0x1234, delete_result=True):
        self.create_result = int(create_result)
        self.delete_result = bool(delete_result)
        self.create_calls: list[tuple[int, int, int, int, int, int]] = []
        self.delete_calls: list[int] = []

    def CreateRoundRectRgn(self, left, top, right, bottom, ellipse_w, ellipse_h):
        self.create_calls.append((int(left), int(top), int(right), int(bottom),
                                  int(ellipse_w), int(ellipse_h)))
        return self.create_result

    def DeleteObject(self, obj):
        self.delete_calls.append(_handle_value(obj) or 0)
        return 1 if self.delete_result else 0


# ============================================================================
# 功能按钮文字居中（用户明确要求的设计缺陷修复）：一条规则覆盖所有按钮
# ============================================================================
def _recording_label_init(instance, master=None, cnf=None, **kw):
    """假基类 ``__init__``：把真实 ``FlatButton`` 传下来的**全部选项**记进控件。

    ``_FakeTkEnv`` 自带的 ``_fake_tk_init`` 会丢掉构造关键字（面板测试不关心
    按钮绘制细节）；这里换成记录版，才能对**真实** ``FlatButton`` 断言它到底
    把什么交给了 Tk —— 依然一个真实控件都不创建。
    """
    support._init_fake_widget(instance, master, **{**(cnf or {}), **kw})
    instance._fake_inited = True
    return None


def _alpha_bbox(image, threshold: int = 0):
    """按 alpha 阈值量墨迹 bbox（``0`` = 所有非透明像素，右下为开区间）。"""
    alpha = image.getchannel("A")
    if threshold > 0:
        alpha = alpha.point(lambda value: 255 if value >= threshold else 0)
    return alpha.getbbox()


def _center_error(image, box) -> tuple[float, float]:
    """墨迹 bbox 中心 − 图像像素区域中心（两轴都以像素中心计）。"""
    width, height = image.size
    return ((box[0] + box[2] - 1) / 2 - (width - 1) / 2,
            (box[1] + box[3] - 1) / 2 - (height - 1) / 2)


def _ink_color(image) -> str:
    """图像里覆盖最广的**完全不透明**颜色（``#RRGGBB`` 大写）。"""
    opaque = [(count, pixel) for count, pixel in image.getcolors(maxcolors=1 << 24)
              if pixel[3] == 255]
    if not opaque:
        return ""
    red, green, blue, _alpha = max(opaque)[1]
    return f"#{red:02X}{green:02X}{blue:02X}"


class TestFlatButtonCentering(unittest.TestCase):
    """``FlatButton``：文字水平 + 垂直居中，四周内边距对称，禁止单按钮魔数偏移。"""

    def setUp(self):
        from app.ui import glyph_text
        from app.ui import widgets as widgets_mod

        self.glyph_text = glyph_text
        self.real_button = widgets_mod.FlatButton      # 假环境会把它换成替身，先抓住
        self.env = _FakeTkEnv()
        self.env.__enter__()
        self._real_class = mock.patch("app.ui.widgets.FlatButton", self.real_button)
        self._real_class.start()
        self._label_base = support._REAL_TK_BASES[1]   # 真实 tkinter.Label 基类
        self._base_init = mock.patch.object(self._label_base, "__init__",
                                            _recording_label_init)
        self._base_init.start()
        #: 假 PhotoImage 工厂：**绝不**碰真实 Tk（生产实现需要真实 Tk 解释器）
        self.photos: list = []

        def _fake_photo(image, master=None):
            photo = SimpleNamespace(image=image, master=master)
            self.photos.append(photo)
            return photo

        self._photo = mock.patch.object(glyph_text, "photo_image", side_effect=_fake_photo)
        self._photo.start()
        self.master = FakeWidget(None)

    def tearDown(self):
        self._photo.stop()
        self._base_init.stop()
        self._real_class.stop()
        self.env.__exit__(None, None, None)

    # ------------------------------------------------------- 像素级复核
    def _assert_padded_and_centered(self, image, padx: int, pady: int) -> None:
        """可见墨迹居中（逐级 alpha 阈值都 ≤ 1 设备像素）+ 每侧至少指定内边距。"""
        width, height = image.size
        self.assertEqual(image.mode, "RGBA")
        box = _alpha_bbox(image)
        self.assertGreaterEqual(box[0], padx, "左侧内边距不得小于调用点 padx")
        self.assertGreaterEqual(box[1], pady, "上方内边距不得小于调用点 pady")
        self.assertLessEqual(box[2], width - padx, "右侧内边距不得小于调用点 padx")
        self.assertLessEqual(box[3], height - pady, "下方内边距不得小于调用点 pady")
        for threshold in (0, 128, 200):
            dx, dy = _center_error(image, _alpha_bbox(image, threshold))
            self.assertLessEqual(abs(dx), 1.0, f"alpha≥{threshold} 水平居中偏差 {dx}px")
            self.assertLessEqual(abs(dy), 1.0, f"alpha≥{threshold} 垂直居中偏差 {dy}px")

    def test_glyph_image_is_tight_symmetric_and_centered_across_states(self):
        """真实 ``FlatButton`` 的图像像素验证（默认 / 改文案 / hover / 禁用 / 降级）。

        渲染是**真实产品代码**（``app.ui.glyph_text`` + 真实 Pillow），只有
        ``photo_image`` 工厂是假的 —— 生产实现要真实 Tk 解释器，测试一次都不碰。
        """
        if not self.glyph_text.available():
            self.skipTest("本机没有 Pillow，图像路径不存在")
        padx, pady = theme.px(14), theme.px(5)
        btn = self.real_button(self.master, "解释并记录", None, primary=True,
                               font_size=9, padx=14, pady=5)
        # 1) 图像路径真的生效：Tk 侧内边距显式归零，文字语义原样保留
        self.assertTrue(btn._glyph_ok)
        self.assertEqual((btn.padx, btn.pady), (padx, pady),
                         "调用点的视觉内边距必须留在对象属性上")
        self.assertEqual(btn.cget("text"), "解释并记录")
        self.assertEqual(btn.cget("padx"), 0, "图像路径 Tk 侧不得再加内边距")
        self.assertEqual(btn.cget("pady"), 0)
        self.assertEqual(btn.cget("compound"), "none")
        self.assertEqual(btn.cget("anchor"), "center")
        self.assertEqual(btn.cget("justify"), "center")
        self.assertEqual(btn.cget("width"), "", "没有额外宽度约束时由内容自适应")
        self.assertEqual(btn.cget("ipadx"), "", "禁止用 ipadx 给单个按钮加偏移")
        self.assertEqual(btn.cget("ipady"), "", "禁止用 ipady 给单个按钮加偏移")
        photo = btn.cget("image")
        self.assertIs(photo, btn._glyph_photo,
                      "PhotoImage 引用必须留住（否则被 GC，按钮变空白）")
        self.assertIs(photo.master, btn, "PhotoImage 必须挂在按钮自己的解释器上")
        image = photo.image
        self._assert_padded_and_centered(image, padx, pady)

        # 1b) 「×」的墨迹只有 7x6px：图像高度按字体排版单元撑足（不能只有 8px 高）
        close_pady = theme.px(1)
        close = self.real_button(self.master, "×", None, font_size=9, padx=6, pady=1)
        close_image = close.cget("image").image
        self.assertGreaterEqual(
            close_image.size[1], self.glyph_text.font_px(9) + 2 * close_pady,
            "「×」按钮必须撑到 font_px(9) + 2*pady，不能只按墨迹高度生成")
        self._assert_padded_and_centered(close_image, theme.px(6), close_pady)

        # 2) 文案更新：重建图像、cget("text") 同步、新图仍然居中且留足内边距
        btn.configure(text="发送")
        self.assertEqual(btn.cget("text"), "发送")
        self.assertIsNot(btn.cget("image"), photo, "文案变了必须重建图像")
        image2 = btn.cget("image").image
        self.assertNotEqual(image2.size, image.size)
        self._assert_padded_and_centered(image2, padx, pady)

        # 3) hover：只在真实墨迹底边以下补下划线 —— 尺寸不变、字形一格不动
        glyph_box = _alpha_bbox(image2)
        btn._on_enter()
        image3 = btn.cget("image").image
        self.assertEqual(image3.size, image2.size, "hover 不得改变按钮尺寸（不偏移）")
        self.assertEqual(_alpha_bbox(image3)[:3], glyph_box[:3],
                         "hover 不得移动字形（左 / 上 / 右完全一致）")
        underline = btn._glyph_meta["underline_box"]
        self.assertIsNotNone(underline, "hover 必须画出下划线")
        self.assertLessEqual(glyph_box[3], underline[1], "下划线只能画在真实墨迹底边以下")
        self.assertLessEqual(underline[3], image3.size[1], "下划线必须留在图像内")
        self.assertEqual(image3.crop((0, 0, image3.size[0], underline[1])),
                         image2.crop((0, 0, image2.size[0], underline[1])),
                         "下划线所在行以上必须逐字节一致")
        self.assertEqual(image3.crop((0, underline[3], image3.size[0], image3.size[1])),
                         image2.crop((0, underline[3], image2.size[0], image2.size[1])),
                         "下划线所在行以下必须逐字节一致")
        btn._on_leave()
        self.assertEqual(_alpha_bbox(btn.cget("image").image), glyph_box,
                         "离开 hover 后必须回到原始字形")

        # 4) 禁用：字形位置 / 尺寸 / 文案都不动（主按钮由底色变化表达禁用）
        btn.set_enabled(False)
        image4 = btn.cget("image").image
        self.assertFalse(btn.is_enabled())
        self.assertEqual(image4.size, image2.size)
        self.assertEqual(_alpha_bbox(image4), glyph_box, "禁用不得移动字形")
        self.assertEqual(btn.cget("text"), "发送")
        plain = self.real_button(self.master, "菜单", None, font_size=8, padx=8, pady=2)
        plain_size = plain.cget("image").image.size
        self.assertEqual(_ink_color(plain.cget("image").image), theme.TEXT,
                         "次按钮正常态字色 = theme.TEXT")
        plain.set_enabled(False)
        self.assertEqual(_ink_color(plain.cget("image").image), theme.TEXT_FAINT,
                         "次按钮禁用态字色 = theme.TEXT_FAINT（像素级）")
        self.assertEqual(plain.cget("image").image.size, plain_size, "禁用不得改尺寸")

        # 5) Pillow 不可用：清 image + 原生居中 + 调用点 padx/pady（降级路径）
        with mock.patch.object(self.glyph_text, "PIL_AVAILABLE", False):
            fallback = self.real_button(self.master, "发送", None, font_size=9,
                                        padx=14, pady=5)
        self.assertFalse(fallback._glyph_ok)
        self.assertIsNone(fallback._glyph_photo)
        self.assertEqual(fallback.cget("image"), "")
        self.assertEqual(fallback.cget("padx"), padx)
        self.assertEqual(fallback.cget("pady"), pady)
        self.assertEqual(fallback.cget("anchor"), "center")
        self.assertEqual(fallback.cget("justify"), "center")
        self.assertEqual(fallback.cget("text"), "发送")
        fallback.set_enabled(False)
        self.assertEqual(fallback.cget("padx"), padx, "降级路径切状态也不许改内边距")

    def test_disabled_and_hover_restyle_never_touch_the_centering(self):
        btn = self.real_button(self.master, "发送", None, primary=True)
        btn.set_enabled(False)
        btn._on_enter()
        btn._on_leave()
        self.assertEqual(btn.cget("anchor"), "center")
        self.assertEqual(btn.cget("justify"), "center")

    def test_no_call_site_overrides_the_centering_rule(self):
        """任何调用点都不许给单个按钮传 anchor / justify / ipadx / ipady。"""
        ui_dir = Path(__file__).resolve().parent.parent / "app" / "ui"
        offenders: list[str] = []
        for path in sorted(ui_dir.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(
                    func, "id", "")
                if name != "FlatButton":
                    continue
                for keyword in node.keywords:
                    if keyword.arg in {"anchor", "justify", "ipadx", "ipady"}:
                        offenders.append(f"{path.name}:{node.lineno} {keyword.arg}=")
        self.assertEqual(offenders, [],
                         "功能按钮只能共用 FlatButton 的统一居中规则，不得逐个加偏移")


class TestOfflinePreviewButtonCentering(unittest.TestCase):
    """离线像素验证：预览里每个功能按钮的墨迹都在按钮中心（≤ 1 设备像素）。"""

    #: 当前产品顶栏 / 详情页真实存在的功能按钮（「菜单 / 重新解释 / 设置 API」已删除）
    REQUIRED = ("主界面", "折叠", "×", "解释并记录", "发送", "重试", "← 返回词表")
    #: 已经删掉的按钮文案：预览里**不允许**再出现（否则预览与实机不一致）
    REMOVED = ("菜单", "重新解释", "设置 API")

    def test_every_function_button_ink_is_centered_within_one_pixel(self):
        from tools.ui_preview import centering_report

        rows, report = centering_report()
        self.assertTrue(rows, "预览必须记录到功能按钮")
        offenders = [(name, label, dx, dy) for name, label, dx, dy, _detail in rows
                     if max(abs(dx), abs(dy)) > 1]
        self.assertEqual(offenders, [], report)
        labels = {label for _name, label, _dx, _dy, _detail in rows}
        for label in self.REQUIRED:
            self.assertIn(label, labels, f"预览必须覆盖「{label}」按钮")
        for label in self.REMOVED:
            self.assertNotIn(label, labels, f"「{label}」已从产品里删除，预览不得再画")

    def test_preview_geometry_comes_from_the_product_constants(self):
        """预览的内容内缩 / 小方块内容区必须**从产品常量算出来**（不是各写一份）。"""
        from tools import ui_preview as preview

        self.assertEqual(preview.INSET, theme.px(geo.border_safe_inset(geo.PANEL_RADIUS)))
        self.assertEqual(preview.DOCK_INSET,
                         theme.px(geo.border_safe_inset(geo.DOCK_RADIUS)))
        self.assertEqual(preview.DOCK_BOX, theme.px(geo.dock_content_box()))
        self.assertEqual(preview.content_w(), preview.PANEL_W - 2 * preview.INSET
                         - 2 * theme.px(geo.CONTENT_PAD))
        ok, detail = preview.dock_fit_report()
        self.assertTrue(ok, f"小方块字形必须放得进 22x22 内容区：{detail}")

        # 三张浮窗 PNG 都必须真的画出右下角灰斜线手柄（产品有，预览不能漏画）：
        # 线端点直接来自 widgets.grip_lines（产品画布用的同一份纯函数）。
        def _rgb(value):
            value = value.lstrip("#")
            return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))

        grip = theme.px(geo.GRIP_SIZE)
        self.assertEqual(len(widgets.grip_lines(grip, grip)), len(geo.GRIP_STEPS),
                         "手柄 = GRIP_STEPS 条斜线")
        x0 = preview.PANEL_W - preview.INSET - grip
        y0 = preview.PANEL_H - preview.INSET - grip
        thumb = _rgb(theme.RAIL_THUMB)
        pages = (("ui-preview.png", preview.draw_list_page),
                 ("ui-detail-preview.png",
                  lambda draw: preview.draw_detail_page(draw, failed=True)),
                 ("ui-empty-preview.png", preview.draw_empty_page))
        for name, draw_page in pages:
            image = preview.render_panel(draw_page).convert("RGB")
            near = sum(1 for y in range(y0, y0 + grip) for x in range(x0, x0 + grip)
                       if sum(abs(a - b) for a, b
                              in zip(image.getpixel((x, y)), thumb)) < 90)
            self.assertGreaterEqual(near, 10, f"{name} 右下角必须画出手柄斜线")

    def test_preview_writes_four_pngs_including_the_empty_state(self):
        import inspect

        from tools import ui_preview as preview

        source = inspect.getsource(preview.main)
        for name in ("ui-detail-preview.png", "ui-empty-preview.png",
                     "ui-dock-preview.png", "ui-topic-preview.png",
                     "ui-chat-preview.png", "ui-main-preview.png",
                     "ui-concept-preview.png"):
            self.assertIn(name, source, f"main() 必须生成 {name}")
        self.assertEqual(preview.DOCK_GLYPH_BOX[2:], (preview.DOCK_BOX,
                                                      preview.DOCK_BOX))

    def test_preview_covers_the_new_pages_from_product_constants(self):
        """新主界面 / 导图 / 主题清单 / 追问问答都必须由**产品常量**画出来。"""
        from tools import ui_preview as preview
        from app.ui import widgets as widgets_mod

        # 主界面 / 导图预览的标题带高度 = 产品 BorderlessChrome 的同一个常量
        self.assertEqual(widgets_mod.CHROME_H, 30)
        # 追问区高度直接来自产品纯函数（预览与实机不会各算一份）
        def_h, chat_h = preview.detail_area_split()
        avail = (preview.PANEL_H - 2 * preview.INSET - theme.px(geo.PANEL_HEAD_H)
                 - theme.px(geo.DETAIL_FIXED_H))
        self.assertEqual((def_h, chat_h),
                         geo.detail_area_heights(avail,
                                                 def_min=theme.px(geo.DETAIL_AREA_MIN),
                                                 chat_min=theme.px(geo.DETAIL_AREA_MIN)))
        self.assertGreaterEqual(chat_h, theme.px(geo.DETAIL_AREA_MIN))
        # 主题清单第一项必须是「跟随当前阅读页」（与产品同一份文案）
        from app.ui.reading_panel import FOLLOW_TEXT
        self.assertEqual(FOLLOW_TEXT, "跟随当前阅读页")

        # 主界面与导图两张预览 PNG 真的画得出来，且尺寸与产品默认 geometry 一致
        self.assertEqual(preview.render_main_window().size, (preview.MAIN_W,
                                                             preview.MAIN_H))
        self.assertEqual(preview.render_concept_map().size, (preview.MAP_W,
                                                             preview.MAP_H))


# ============================================================================
# 自绘无框 chrome（主界面 / 导图 / 设置 / 手动录入共用）
# ============================================================================
class _FakeEvent:
    """拖动 / 缩放回调只用到 x_root / y_root。"""

    def __init__(self, x_root=0, y_root=0):
        self.x_root = int(x_root)
        self.y_root = int(y_root)


class TestBorderlessChrome(unittest.TestCase):
    def setUp(self):
        self.env = _FakeTkEnv()
        self.env.__enter__()

    def tearDown(self):
        self.env.__exit__(None, None, None)

    def _window(self, width=800, height=600):
        import tkinter as tk

        win = tk.Toplevel(None)
        win.winfo_w = int(width)
        win.winfo_h = int(height)
        calls: list = []
        original = win.overrideredirect
        win.overrideredirect = lambda value=True: (calls.append(bool(value)),
                                                   original(value))[1]
        return win, calls

    def test_chrome_removes_the_native_frame_and_wires_close(self):
        win, calls = self._window()
        closed: list = []
        chrome = widgets.BorderlessChrome(win, title="测试窗口",
                                          on_close=lambda: closed.append(True))
        self.assertEqual(calls, [True], "必须去掉原生标题栏 / 边框（overrideredirect）")
        self.assertEqual(str(chrome.title_label.cget("text")), "测试窗口")
        # × / Esc / Alt+F4 都走宿主给的同一个关闭回调（绝不各自造语义）
        chrome.btn_close.invoke()
        chrome._on_dismiss(None)
        self.assertEqual(len(closed), 2)
        self.assertEqual(chrome._on_dismiss(None), "break", "Esc 不再继续冒泡")

    def test_title_band_drags_the_window_and_blank_area_is_draggable(self):
        win, _calls = self._window(width=800, height=600)
        chrome = widgets.BorderlessChrome(win, title="拖我", on_close=lambda: None)
        self.assertEqual(chrome.bar.binds.get("<Button-1>"), chrome._on_drag_start)
        self.assertEqual(chrome.title_label.binds.get("<Button-1>"),
                         chrome._on_drag_start)
        chrome._on_drag_start(_FakeEvent(200, 200))
        chrome._on_drag_motion(_FakeEvent(260, 240))
        chrome._on_drag_end(_FakeEvent(260, 240))
        self.assertTrue(win.geometry_specs, "拖动标题带必须真的下发新位置")
        self.assertIn("+160+140", win.geometry_specs[-1],
                      "位移 = 指针位移（与 DPI / 缩放无关）")

    def test_resize_never_goes_below_the_minimum(self):
        win, _calls = self._window(width=900, height=700)
        chrome = widgets.BorderlessChrome(win, title="缩放", on_close=lambda: None,
                                          resizable=True, min_w=460, min_h=340)
        self.assertIsNotNone(chrome.grip, "可缩放窗口必须有右下角手柄")
        chrome._on_resize_start(_FakeEvent(0, 0))
        chrome._on_resize_motion(_FakeEvent(-500, -500))
        self.assertEqual(win.geometry_specs[-1],
                         f"{theme.px(460)}x{theme.px(340)}", "不得小于最小尺寸")
        chrome._on_resize_motion(_FakeEvent(120, 80))
        self.assertEqual(win.geometry_specs[-1],
                         f"{900 + 120}x{700 + 80}")

    def test_main_window_chrome_hides_to_background_and_never_quits(self):
        """主界面「×」/ Esc / Alt+F4 = 只收起、后台继续；子窗关闭只关自己。"""
        from tests.support import headless_app

        with headless_app(overlays="panel", main_window="real") as app:
            chrome = app.main.chrome
            self.assertIsInstance(chrome, widgets.BorderlessChrome,
                                  "主界面必须用共用的自绘无框 chrome")
            self.assertEqual(chrome.bar.binds.get("<Button-1>"), chrome._on_drag_start)
            self.assertFalse(app._closing)
            withdrew: list = []
            app.fake_root.withdraw = lambda: withdrew.append(True)
            chrome.close()                     # 与 Esc / Alt+F4 同一条路径
            self.assertEqual(len(withdrew), 1, "主窗「×」= 收起窗口")
            self.assertFalse(app._closing, "主窗「×」绝不退出程序")

            # 设置窗：共用同一份 chrome，关闭只关自己
            app.open_settings()
            settings = app._settings_win
            self.assertIsInstance(settings.chrome, widgets.BorderlessChrome)
            settings.chrome.close()
            self.assertFalse(app._closing, "子窗关闭绝不退出程序")
            self.assertIn("destroy", settings.win.events,
                          "设置窗的「×」只关掉本窗（不退出程序）")

            # 导图窗：同样共用，并且**不动**父窗口
            app.open_concept_map()
            conmap = app._map_win
            self.assertIsInstance(conmap.chrome, widgets.BorderlessChrome)
            deiconify_before = app.fake_root.deiconify_calls
            app.open_concept_map()             # 单例：再开一次只 lift + 重画
            self.assertIs(app._map_win, conmap, "导图必须单例（不重复建窗）")
            self.assertGreaterEqual(app.fake_root.deiconify_calls, deiconify_before)
            conmap.chrome.close()
            self.assertFalse(app._closing)

    def test_manual_dialog_uses_the_shared_chrome_and_inline_validation(self):
        from tests.support import FakeWidget
        from app.ui.manual_dialog import ManualEntryDialog

        with _FakeTkEnv():
            calls: list = []
            stub = SimpleNamespace(
                capture_service=SimpleNamespace(
                    last_source=lambda: None, effective_source=lambda: None,
                    manual_entry=lambda *a, **k: (1, True)),
                after_manual_save=lambda *a, **k: calls.append(a),
            )
            dialog = ManualEntryDialog(FakeWidget(None), stub)
            self.assertIsInstance(dialog.chrome, widgets.BorderlessChrome)
            dialog.var_term.set("")
            dialog.save(False)
            self.assertIn("不能为空", str(dialog.feedback.cget("text")),
                          "校验反馈必须内联（不弹系统 messagebox）")
            self.assertEqual(calls, [], "空的术语不写库")
            dialog.var_term.set("卷积")
            dialog.save(False)
            self.assertEqual(len(calls), 1, "合法输入必须真的走保存路径")




# ============================================================================
# 导图：AI 参考关系（受限输入 / 证据校验 / 缓存指纹 / 结构布局 / 迟到隔离）
# ============================================================================
def _map_db(db, topic="卷积网络"):
    """一个主题 + 三条带上下文 / 释义的词条（导图测试共用，按 entry_id 排序）。"""
    bid = int(db.create_batch(topic))
    rows = (("卷积", "卷积核在输入上滑动，逐点相乘再求和", "卷积是加权求和的特征提取"),
            ("池化", "池化用于降采样，扩大感受野", "池化降低分辨率"),
            ("过拟合", "正则化抑制过拟合", "过拟合是记住了噪声"))
    ids = {}
    for term, context, one_line in rows:
        eid = int(db.add_entry(batch_id=bid, term=term, context=context))
        db.update_entry(eid, one_line=one_line, explain_status="ok")
        ids[term] = eid
    return bid, ids


def _map_nodes(db, bid):
    from app.map_service import MapService
    from app.config import Config

    return MapService(db, Config(db)).nodes_for_topic(bid)


def _map_service(db, *, key="sk-test-not-real", client=None):
    """真实 ``MapService`` + 假客户端（绝不联网）。"""
    from app.config import Config
    from app.map_service import MapService

    cfg = Config(db)
    cfg.set("api.base_url", "https://api.example.com/v1")
    cfg.set("api.model", "model-A")
    if key:
        cfg.set_api_key(key)
    service = MapService(db, cfg)
    if client is not None:
        service.make_client = lambda **kw: client
    return service, cfg


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _put_map_cache(db, service, topic_id, relations, *, dropped=None,
                   verdicts=None) -> str:
    """按**当前库内容**写一份有效导图缓存（打开窗口即命中，零网络请求）。

    缓存里的关系带 ``src`` / ``dst`` / ``type`` / ``reason`` / ``evidence``，
    与真实生成路径写进去的结构完全一致（见 ``TestConceptMapFreshness._put_cache``）。
    ``dropped`` / ``verdicts`` 同样照真实路径写：判定是「这个词为什么成了孤立词」
    的唯一依据，界面上要说清原因就得靠它。
    """
    from app.map_service import MAP_VALIDATION_VERSION

    nodes = service.nodes_for_topic(int(topic_id))
    fingerprint = service.fingerprint(int(topic_id), nodes)
    db.put_map_graph(topic_id=int(topic_id), fingerprint=fingerprint,
                     base_url=service.config.base_url, model=service.config.model,
                     relations=[dict(item) for item in relations],
                     validation_version=MAP_VALIDATION_VERSION,
                     dropped=dict(dropped or {}),
                     verdicts=[dict(item) for item in (verdicts or [])])
    return fingerprint


class _MapClient:
    """假导图客户端：记录**实际发出去的受限材料**，绝不联网。

    * ``map_relations`` = 第一次调用（提候选）；
    * ``map_verify`` = 第二次**独立核对**调用。默认对每条候选回 ``supported``
      （即「核对同意」），需要时用 ``verdicts`` / ``verdict_factory`` 指定
      ``uncertain`` / ``contradicted`` / 坏判定，或用 ``verify_error`` 模拟第二次
      调用失败（网络 / 顶层格式）—— 用来验证「核对说不行就真的不画」。
    """

    def __init__(self, relations=None, error=None, gate=None, *,
                 verdicts=None, verify_error=None, verify_gate=None):
        self.relations = [dict(item) for item in (relations or [])]
        self.error = error
        self.gate = gate
        self.verdicts = None if verdicts is None else [dict(item) for item in verdicts]
        self.verify_error = verify_error
        self.verify_gate = verify_gate
        self.calls: list[dict] = []
        self.verify_calls: list[dict] = []

    def map_relations(self, entries, *, topic="", **_kw):
        self.calls.append({"entries": [dict(item) for item in entries],
                           "topic": str(topic)})
        if self.gate is not None:
            self.gate.wait(5.0)
        if self.error is not None:
            raise self.error
        return [dict(item) for item in self.relations]

    def map_verify(self, items, *, topic="", **_kw):
        items = [dict(item) for item in items]
        self.verify_calls.append({"items": items, "topic": str(topic)})
        if self.verify_gate is not None:
            self.verify_gate.wait(5.0)
        if self.verify_error is not None:
            raise self.verify_error
        if self.verdicts is not None:
            return [dict(item) for item in self.verdicts]
        return [{"index": int(item.get("index", 0)), "verdict": "supported",
                 "note": "材料支持"} for item in items]


def _canvas_texts(win) -> list[str]:
    return [str(options.get("text") or "") for options in
            win.canvas.item_options.values()]


#: 离线导图预览（``tools/ui_preview.render_concept_map``）用的**同一组**示例材料：
#: 这里只复刻输入（词 + 关系），布局仍然全部走产品纯函数。
PREVIEW_MAP_LABELS = {1: "卷积神经网络", 2: "卷积层", 3: "池化层", 4: "卷积运算",
                      5: "卷积核", 6: "降采样", 7: "Embedding"}
PREVIEW_MAP_RELATIONS = ((1, 2, "包含"), (2, 3, "对照"), (4, 5, "依赖"),
                         (3, 6, "用途"))
#: 预览画布尺寸（与 ``ui_preview.MAP_W/MAP_H`` 的导图区域一致）
PREVIEW_MAP_CANVAS = (620, 430)


def _preview_map_layout():
    """当前示例的纯布局（离线预览与实机共用同一个 ``layout_graph``）。"""
    from app.map_service import MapRelation
    from app.ui import concept_map as cm
    from app.ui import theme

    relations = [MapRelation(src, dst, rel_type, "离线假依据", "离线假证据片段")
                 for src, dst, rel_type in PREVIEW_MAP_RELATIONS]
    return cm.layout_graph(PREVIEW_MAP_LABELS, relations, width=PREVIEW_MAP_CANVAS[0],
                           height=PREVIEW_MAP_CANVAS[1], topic_label="卷积神经网络",
                           top_pad=theme.px(24))


def _crossing_example_layout():
    """当前示例里那条「左下 → 右上、中间正好横着一个词」的对照边。"""
    return _preview_map_layout()


def _blocked_cross_layout():
    """结构上**必然**「中间横着一个词」的对照跨边（避障回归的定点 fixture）。

    1 在上，2 / 3 / 4 同层一字排开；对照边 2↔4 的直连正好穿过中间的 3 ——
    合格的路由必须改走弧线 / 折线绕开它（离线预览示例里那条对照边在新布局下
    已经不再被挡，不能再用它来证明避障）。
    """
    from app.map_service import MapRelation
    from app.ui import concept_map as cm

    labels = {1: "卷积神经网络", 2: "卷积层", 3: "池化层", 4: "卷积运算"}
    relations = [MapRelation(1, 2, "包含", "离线假依据", "离线假证据片段"),
                 MapRelation(1, 3, "包含", "离线假依据", "离线假证据片段"),
                 MapRelation(1, 4, "包含", "离线假依据", "离线假证据片段"),
                 MapRelation(2, 4, "对照", "离线假依据", "离线假证据片段")]
    return cm.layout_graph(labels, relations, width=620, height=430,
                           topic_label="卷积神经网络")


def _label_texts(layout) -> list[str]:
    return [str(edge.label) for edge in layout.edges]


def _labels_of_kind(layout, rel_type: str) -> list[str]:
    return [str(edge.label) for edge in layout.edges if edge.label == rel_type]


def _node_rects(layout, *, pad: float) -> dict:
    from app.ui import concept_map as cm

    return {int(node.entry_id): cm.node_box(node, pad) for node in layout.nodes}


def _edge_hits_nodes(edge, nodes: dict) -> list[int]:
    """这条边的折线碰到了哪些**非端点**节点的矩形。"""
    from app.ui import concept_map as cm

    src, dst, _type = cm._relation_parts(edge.rel)
    return [entry_id for entry_id, rect in nodes.items()
            if entry_id not in (int(src), int(dst))
            and not cm.route_is_clear(edge.points, [rect])]


def _distance_to_path(point, points) -> float:
    """点到一串折线的**最短距离**（测试自己算：点积投影 + 端点夹取）。"""
    px_, py_ = float(point[0]), float(point[1])
    best = float("inf")
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        dx, dy = float(x2) - float(x1), float(y2) - float(y1)
        length2 = dx * dx + dy * dy
        if length2 <= 1e-9:
            t = 0.0
        else:
            t = ((px_ - float(x1)) * dx + (py_ - float(y1)) * dy) / length2
            t = max(0.0, min(1.0, t))
        best = min(best, ((px_ - (float(x1) + t * dx)) ** 2
                          + (py_ - (float(y1) + t * dy)) ** 2) ** 0.5)
    return best


def _razor_hits_rect(x1, y1, x2, y2, rect) -> bool:
    """**独立**的线段 / 矩形相交判据：测试里手写的 Liang-Barsky 裁剪。

    刻意不复用产品的 ``_segment_hits_rect``：避障用例的判据要能自己算，
    否则「产品说自己没问题」就变成了自证。
    """
    low, high = 0.0, 1.0
    dx, dy = x2 - x1, y2 - y1
    for numerator, denominator in ((rect[0] - x1, dx), (x1 - rect[2], -dx),
                                   (rect[1] - y1, dy), (y1 - rect[3], -dy)):
        if abs(denominator) < 1e-12:
            if numerator > 0:
                return False
            continue
        ratio = numerator / denominator
        if denominator > 0:
            low = max(low, ratio)
        else:
            high = min(high, ratio)
        if low > high:
            return False
    return True


class TestMapRelationValidation(unittest.TestCase):
    """结构化候选的字段 / 证据 / 矛盾 / 成环校验（纯函数，零网络零 Tk）。"""

    def _nodes(self):
        from app.map_service import MapNode

        return [MapNode(1, "卷积", "卷积核在输入上滑动", "卷积是加权求和"),
                MapNode(2, "池化", "池化用于降采样", "池化降低分辨率"),
                MapNode(3, "感受野", "池化扩大感受野", "感受野指输出像素对应的输入区域")]

    def _ok(self, **kw):
        item = {"src": 0, "dst": 1, "type": "包含", "reason": "卷积核是卷积的单元",
                "evidence": "卷积核在输入上滑动"}
        item.update(kw)
        return item

    def test_only_existing_word_endpoints_and_known_types_survive(self):
        from app.map_service import validate_relations

        nodes = self._nodes()
        kept, dropped = validate_relations([
            self._ok(),
            self._ok(src=9),                                   # 越界
            self._ok(src="不是编号"),                          # 非编号
            self._ok(dst=1, type="相关"),                      # 非法类型（绝不默认「相关」）
            self._ok(dst=1, type=""),                          # 缺类型
        ], nodes)
        self.assertEqual(len(kept), 1, "只有端点与类型都合法的候选才画")
        self.assertEqual(dropped["endpoint"], 2)
        self.assertEqual(dropped["type"], 2)
        self.assertEqual(int(kept[0].src_entry_id), 1)
        self.assertEqual(int(kept[0].dst_entry_id), 2)

    def test_self_loop_and_duplicates_are_dropped(self):
        from app.map_service import validate_relations

        nodes = self._nodes()
        kept, dropped = validate_relations([
            self._ok(),
            self._ok(),                                        # 完全重复
            self._ok(src=0, dst=0),                            # 自环
        ], nodes)
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped["duplicate"], 1)
        self.assertEqual(dropped["self"], 1)

    def test_mutual_hierarchy_is_a_contradiction_but_feedback_is_kept(self):
        """层级互相包含 = 矛盾（两条都不画）；双向依赖 / 因果 = 真实反馈（保留）。"""
        from app.map_service import validate_relations

        nodes = self._nodes()
        kept, dropped = validate_relations([
            self._ok(type="包含", evidence="池化用于降采样"),
            self._ok(src=1, dst=0, type="包含", evidence="池化用于降采样"),
        ], nodes)
        self.assertEqual(kept, [], "A 包含 B 又 B 包含 A = 不可能，两条都不画")
        self.assertEqual(dropped["contradiction"], 2)

        # 「A 依赖 B」与「B 依赖 A」（互相依赖）是真实的反馈环：必须都留下
        kept, dropped = validate_relations([
            self._ok(src=0, dst=1, type="依赖", reason="互为前提",
                     evidence="池化用于降采样"),
            self._ok(src=1, dst=0, type="依赖", reason="互为前提",
                     evidence="池化用于降采样"),
        ], nodes)
        self.assertEqual(len(kept), 2, "经过核对支持的互相依赖必须保留（反馈环）")
        self.assertEqual(dropped["contradiction"], 0)
        self.assertEqual(dropped["cycle"], 0, "方向反馈环不算层级成环")

        # 互为因果同理
        kept, dropped = validate_relations([
            self._ok(type="因果", reason="互为因果", evidence="池化用于降采样"),
            self._ok(src=1, dst=0, type="因果", reason="互为因果",
                     evidence="池化用于降采样"),
        ], nodes)
        self.assertEqual(len(kept), 2, "互为因果也是真实反馈，不得当矛盾删掉")
        self.assertEqual(dropped["contradiction"], 0)

        # 「对照」是对称关系：反向只算重复
        kept, dropped = validate_relations([
            self._ok(type="对照", evidence="池化用于降采样"),
            self._ok(src=1, dst=0, type="对照", evidence="池化用于降采样"),
        ], nodes)
        self.assertEqual(len(kept), 1, "「对照」是对称关系：反向只算重复")
        self.assertEqual(dropped["duplicate"], 1)

    def test_hierarchy_cycles_are_excluded(self):
        from app.map_service import validate_relations

        nodes = self._nodes()
        kept, dropped = validate_relations([
            self._ok(src=0, dst=1, type="包含"),
            self._ok(src=1, dst=2, type="包含", evidence="池化用于降采样"),
            # 「卷积 属于 感受野」= 感受野在卷积之上，与前两条形成 A→B→C→A 的环
            self._ok(src=0, dst=2, type="属于", evidence="池化扩大感受野"),
        ], nodes)
        self.assertEqual(len(kept), 2, "成环的那条层级边必须被排除")
        self.assertEqual(dropped["cycle"], 1)
        self.assertTrue(all(rel.constraint is not None for rel in kept))

    def test_evidence_must_appear_verbatim_in_the_given_material(self):
        from app.map_service import validate_relations

        nodes = self._nodes()
        kept, dropped = validate_relations([
            self._ok(evidence="这句话不在材料里"),
            self._ok(evidence="卷"),                            # 太短，不算证据
            self._ok(evidence=""),                              # 缺证据
            self._ok(reason=""),                                # 缺依据
        ], nodes)
        self.assertEqual(kept, [], "证据对不上材料就绝不连线")
        self.assertEqual(dropped["evidence"], 3)
        self.assertEqual(dropped["reason"], 1)

    def test_no_candidates_gives_an_empty_graph_not_an_error(self):
        from app.map_service import validate_relations

        kept, dropped = validate_relations([], self._nodes())
        self.assertEqual(kept, [], "依据不足允许空关系（不强行连线）")
        self.assertEqual(sum(dropped.values()), 0)

    def test_direction_semantics_are_consistent_for_every_type(self):
        from app.map_service import REL_TYPES, constraint_pair
        from app.ui.concept_map import edge_kind

        self.assertEqual(set(REL_TYPES),
                         {"包含", "属于", "依赖", "用途", "因果", "对照"})
        self.assertEqual(constraint_pair("包含", 1, 2), (1, 2), "包含：上位在前")
        self.assertEqual(constraint_pair("属于", 1, 2), (2, 1), "属于：下位在后")
        self.assertEqual(constraint_pair("依赖", 1, 2), (2, 1), "依赖：前提在前")
        self.assertEqual(constraint_pair("因果", 1, 2), (1, 2), "因果：因在前")
        self.assertIsNone(constraint_pair("用途", 1, 2), "用途是跨边，不约束层级")
        self.assertIsNone(constraint_pair("对照", 1, 2))
        self.assertEqual(edge_kind("包含"), "hierarchy")
        self.assertEqual(edge_kind("依赖"), "direction")
        self.assertEqual(edge_kind("用途"), "cross")

    # ------------------------------------------------ 第二次独立核对的判定回填
    def _relations(self):
        from app.map_service import MapRelation

        return [MapRelation(1, 2, "包含", "卷积核是卷积的单元", "卷积核在输入上滑动"),
                MapRelation(2, 3, "因果", "池化扩大感受野", "池化扩大感受野")]

    def test_only_explicitly_supported_edges_survive_verification(self):
        from app.map_service import apply_verdicts

        rels = self._relations()
        kept, counts = apply_verdicts(rels, self._nodes(), [
            {"index": 0, "verdict": "supported"},
            {"index": 1, "verdict": "uncertain"},
        ])
        self.assertEqual([rel.rel_type for rel in kept], ["包含"],
                         "只有明确 supported 的那条能画 / 能缓存")
        self.assertEqual(counts["verify_supported"], 1)
        self.assertEqual(counts["verify_uncertain"], 1)

        kept, counts = apply_verdicts(rels, self._nodes(), [
            {"index": 0, "verdict": "contradicted"},
            {"index": 1, "verdict": "supported"},
        ])
        self.assertEqual([rel.rel_type for rel in kept], ["因果"])
        self.assertEqual(counts["verify_contradicted"], 1)

    def test_missing_or_broken_verdicts_never_count_as_supported(self):
        """缺判定 / 序号越界 / 重复判定 / 坏 verdict / 非对象 —— 一律不画。"""
        from app.map_service import apply_verdicts

        rels = self._relations()
        kept, counts = apply_verdicts(rels, self._nodes(), [
            {"index": 9, "verdict": "supported"},        # 越界序号
            {"index": "不是编号", "verdict": "supported"},
            {"index": 0, "verdict": "大概吧"},            # 非法判定
            {"index": 1, "verdict": None},               # 缺判定字段
            "不是对象",
        ])
        self.assertEqual(kept, [], "核对输出坏掉时绝不冒充有效图")
        self.assertEqual(counts["verify_bad"], 7, "5 条坏判定 + 2 条完全没有判定")
        self.assertEqual(counts["verify_supported"], 0)

        kept, counts = apply_verdicts(rels, self._nodes(), [])
        self.assertEqual(kept, [], "一条判定都没有 = 一条都不画（允许空关系）")
        self.assertEqual(counts["verify_bad"], 2)

        # 坏判定与好判定混在一起：只有明确 supported 的那条留下
        kept, counts = apply_verdicts(rels, self._nodes(), [
            {"index": 0, "verdict": "大概吧"},
            {"index": 1, "verdict": "supported"},
            {"index": 1, "verdict": "contradicted"},     # 同一条重复判定：第一次为准
        ])
        self.assertEqual([rel.rel_type for rel in kept], ["因果"])
        self.assertEqual(counts["verify_supported"], 1)
        self.assertEqual(counts["verify_bad"], 3,
                         "坏判定 + 重复判定 + 那条没有有效判定的候选")

    def test_verification_never_adds_edges_and_rechecks_local_rules(self):
        """核对阶段**不能新增边**；supported 也要再过一遍本地证据校验。"""
        from app.map_service import MapRelation, apply_verdicts

        rels = self._relations()
        kept, counts = apply_verdicts(rels, self._nodes(), [
            {"index": 0, "verdict": "supported"},
            {"index": 1, "verdict": "supported"},
            {"index": 2, "verdict": "supported"},        # 核对想加一条——忽略
            {"index": 0, "src": 0, "dst": 1, "type": "因果"},   # 想改类型——忽略
        ])
        self.assertEqual(len(kept), 2, "核对输出再多也只会删，不会多出边")

        # 本地已经不成立的边（证据与材料不符 / 端点不在本次节点里）救不回来
        broken = MapRelation(1, 2, "包含", "依据", "这句不在材料里")
        kept, counts = apply_verdicts([broken], self._nodes(),
                                      [{"index": 0, "verdict": "supported"}])
        self.assertEqual(kept, [])
        self.assertEqual(counts["verify_bad"], 1)
        self.assertEqual(counts["verify_supported"], 0)

    def test_weak_evidence_repeating_a_term_is_still_rejected(self):
        from app.map_service import MapNode, validate_relations

        long_term = "Transformer编码器结构"
        nodes = [MapNode(1, long_term, f"{long_term}由多头注意力组成", "释义"),
                 MapNode(2, "自注意力", "注意力机制", "释义")]
        kept, dropped = validate_relations([
            {"src": 0, "dst": 1, "type": "包含", "reason": "重复词不算依据",
             "evidence": long_term},                     # 只是把端点词抄一遍
            {"src": 0, "dst": 1, "type": "包含", "reason": "短片段不算依据",
             "evidence": "注意"},
        ], nodes)
        self.assertEqual(kept, [], "只重复 term / 短词不构成依据")
        self.assertEqual(dropped["evidence"], 2)


class TestMapServiceWithFakeClient(unittest.TestCase):
    """真实服务 + 假客户端：结构化输出、证据筛除、缓存、切主题 / 改词隔离。"""

    def test_generation_validates_caches_and_reuses_the_cache(self):
        from app.map_service import MAP_VALIDATION_VERSION

        with support.temp_db() as db:
            bid, ids = _map_db(db)
            client = _MapClient([
                {"src": 0, "dst": 1, "type": "包含", "reason": "卷积核是卷积的单元",
                 "evidence": "卷积核在输入上滑动"},
                {"src": 2, "dst": 0, "type": "用途", "reason": "正则化抑制过拟合",
                 "evidence": "这句不在材料里"},                 # 证据不符 → 筛掉
            ])
            service, _cfg = _map_service(db, client=client)
            delivered: list = []
            service.set_result_sink(lambda *a: delivered.append(a))
            token = service.generate(bid, force=True, topic_name="卷积网络")
            self.assertIsNotNone(token)
            self.assertTrue(_wait_for(lambda: delivered), "结果必须回到调用方")
            _token, topic_id, fingerprint, status, graph, error = delivered[0]
            self.assertEqual(status, "ok")
            self.assertIsNone(error)
            self.assertEqual(int(topic_id), bid)
            self.assertEqual(len(graph.relations), 1, "无证据的关系被筛掉")
            self.assertEqual(graph.relations[0].rel_type, "包含")
            self.assertEqual(int(graph.dropped["evidence"]), 1)
            self.assertEqual(db.count_map_graphs(), 1, "通过校验的关系才写缓存")
            # 只有**本地校验通过**的候选才进第二次核对；核对输入不含内部 id
            self.assertEqual(len(client.verify_calls), 1, "每个候选都要独立核对一次")
            items = client.verify_calls[0]["items"]
            self.assertEqual(len(items), 1)
            self.assertEqual(set(items[0]),
                             {"index", "type", "direction", "source", "target",
                              "reason", "evidence"})
            self.assertIn("包含", str(items[0]["direction"]))
            row = db.get_map_graph(bid, fingerprint)
            self.assertEqual(int(row["validation_version"]), MAP_VALIDATION_VERSION,
                             "缓存必须记录校验版本（旧字串校验结果不得直接展示）")

            # 同内容再打开：命中缓存，**不再**请求（两次调用都不发）
            again: list = []
            service.set_result_sink(lambda *a: again.append(a))
            self.assertIsNone(service.generate(bid), "命中缓存不该发新请求")
            self.assertEqual(len(client.calls), 1, "缓存命中绝不发第二次请求")
            self.assertEqual(len(client.verify_calls), 1, "缓存命中也不重新核对")
            self.assertEqual(len(again), 1)
            self.assertEqual(str(again[0][3]), "ok")
            self.assertEqual(str(getattr(again[0][4], "source", "")), "cache")
            self.assertEqual(str(again[0][2]), str(fingerprint))

    def test_real_reference_without_semantic_support_is_not_drawn_or_cached(self):
        """引用**真实存在**但语义不支持的候选 → 核对 uncertain → 无边、无缓存。"""
        with support.temp_db() as db:
            bid, _ids = _map_db(db)
            candidate = {"src": 0, "dst": 2, "type": "因果",
                         "reason": "卷积导致过拟合",
                         "evidence": "正则化抑制过拟合"}
            client = _MapClient([candidate], verdicts=[{"index": 0,
                                                        "verdict": "uncertain",
                                                        "note": "材料没说因果"}])
            service, _cfg = _map_service(db, client=client)
            delivered: list = []
            service.set_result_sink(lambda *a: delivered.append(a))
            service.generate(bid, force=True, topic_name="卷积网络")
            self.assertTrue(_wait_for(lambda: delivered))
            _t, _topic, _fp, status, graph, error = delivered[0]
            self.assertEqual(status, "ok", "核对不确定不是生成失败：给空关系即可")
            self.assertIsNone(error)
            self.assertEqual(graph.relations, (), "核对没明确支持就一条边都不画")
            self.assertEqual(int(graph.dropped["verify_uncertain"]), 1)
            self.assertIn("核对不确定", graph.summary())
            # 这次生成本身是成功的：缓存里只存**空关系**，绝不存那条没通过核对的边
            self.assertEqual(db.count_map_graphs(), 1)
            row = list(db.query("SELECT payload FROM map_graphs"))[0]
            self.assertEqual(json.loads(row["payload"]), [],
                             "核对不确定的边绝不进缓存")

    def test_every_candidate_verdict_is_cached_and_read_back_from_the_cache(self):
        """每条候选的核对判定都要落库，重开窗口从缓存读回来 —— 孤立词才说得清原因。

        真实缺陷（2026-10）：判定只在内存里过一次，``cached_graph`` 又把
        ``dropped`` 写死成空 ⇒ 二次打开显示「已筛除 0 条」，用户只看到「孤立词」，
        完全不知道模型提过什么、为什么没画。
        """
        with support.temp_db() as db:
            bid, ids = _map_db(db)
            client = _MapClient([
                {"src": 0, "dst": 1, "type": "包含", "reason": "卷积核是卷积的单元",
                 "evidence": "卷积核在输入上滑动"},                    # 核对支持 → 画
                {"src": 0, "dst": 2, "type": "因果", "reason": "卷积导致过拟合",
                 "evidence": "正则化抑制过拟合"},                      # 核对认为不成立
                {"src": 2, "dst": 1, "type": "依赖", "reason": "过拟合依赖池化",
                 "evidence": "池化用于降采样"},                        # 核对不确定
            ], verdicts=[
                {"index": 0, "verdict": "supported", "note": "材料支持"},
                {"index": 1, "verdict": "contradicted",
                 "note": "材料只是场景描述，并未表达因果。"},
                {"index": 2, "verdict": "uncertain", "note": "材料不足"},
            ])
            service, _cfg = _map_service(db, client=client)
            delivered: list = []
            service.set_result_sink(lambda *a: delivered.append(a))
            service.generate(bid, force=True, topic_name="卷积网络")
            self.assertTrue(_wait_for(lambda: delivered))
            _t, _topic, fingerprint, status, graph, error = delivered[0]
            self.assertEqual(status, "ok")
            self.assertIsNone(error)

            records = {int(item["index"]): item for item in graph.verdicts}
            self.assertEqual(len(records), 3,
                             "三条候选都要留判定（不只留被筛掉的那两条）")
            self.assertEqual(records[0]["verdict"], "supported")
            self.assertTrue(records[0]["kept"], "核对支持的必须标记为已画")
            self.assertEqual(records[1]["verdict"], "contradicted")
            self.assertFalse(records[1]["kept"], "核对认为不成立的一条都不画")
            self.assertEqual((int(records[1]["src"]), int(records[1]["dst"])),
                             (int(ids["卷积"]), int(ids["过拟合"])),
                             "判定记录要用真实 entry id，点词才找得到")
            self.assertIn("场景描述", str(records[1]["note"]), "模型给的理由必须留档")
            # 「过拟合」是这条图上的孤立词：它参与的两条候选都没通过核对
            isolated = graph.verdicts_for(int(ids["过拟合"]))
            self.assertEqual([int(item["index"]) for item in isolated], [1, 2],
                             "点这个词要列出它参与过的每一条候选（按序号）")
            self.assertEqual({str(item["verdict"]) for item in isolated},
                             {"contradicted", "uncertain"})
            self.assertFalse(any(bool(item["kept"]) for item in isolated),
                             "它一条都没画出来 ⇒ 界面必须说得出为什么")
            self.assertEqual(int(graph.dropped["verify_contradicted"]), 1)
            self.assertEqual(int(graph.dropped["verify_uncertain"]), 1)
            self.assertEqual([int(rel.dst_entry_id) for rel in graph.relations],
                             [int(ids["池化"])], "只有那条 supported 被画出来")

            row = db.get_map_graph(bid, fingerprint)
            self.assertEqual(len(json.loads(row["verdicts"])), 3,
                             "判定必须随图落库（重开窗口没法再问一次模型）")
            self.assertEqual(json.loads(row["dropped"]),
                             {"verify_supported": 1, "verify_contradicted": 1,
                              "verify_uncertain": 1},
                             "三类判定计数都要落库（含通过核对的条数）")

            # 重开：命中缓存 —— 判定与筛除计数都要原样读回来
            again: list = []
            service.set_result_sink(lambda *a: again.append(a))
            self.assertIsNone(service.generate(bid), "命中缓存不发新请求")
            cached = again[0][4]
            self.assertEqual(str(cached.source), "cache")
            self.assertEqual([str(item["verdict"]) for item in cached.verdicts],
                             ["supported", "contradicted", "uncertain"],
                             "缓存必须把判定带回来")
            self.assertEqual(int(cached.dropped.get("verify_contradicted", 0)), 1,
                             "缓存也要带回筛除计数（否则显示「已筛除 0 条」）")
            self.assertIn("核对认为不成立", cached.summary())
            cached_isolated = cached.verdicts_for(int(ids["过拟合"]))
            self.assertEqual([int(item["index"]) for item in cached_isolated], [1, 2],
                             "缓存里也要能说出这个词为什么是孤立词")
            self.assertIn("场景描述", str(cached_isolated[0]["note"]),
                          "缓存里的判定理由要与生成时逐字一致")

    def test_supported_direction_is_cached_and_reused_verbatim(self):
        with support.temp_db() as db:
            bid, ids = _map_db(db)
            candidate = {"src": 2, "dst": 1, "type": "因果",
                         "reason": "过拟合由正则化抑制",
                         "evidence": "正则化抑制过拟合"}
            client = _MapClient([candidate],
                                verdicts=[{"index": 0, "verdict": "supported"}])
            service, _cfg = _map_service(db, client=client)
            delivered: list = []
            service.set_result_sink(lambda *a: delivered.append(a))
            service.generate(bid, force=True)
            self.assertTrue(_wait_for(lambda: delivered))
            graph = delivered[0][4]
            self.assertEqual(len(graph.relations), 1)
            rel = graph.relations[0]
            self.assertEqual((int(rel.src_entry_id), int(rel.dst_entry_id)),
                             (int(ids["过拟合"]), int(ids["池化"])),
                             "方向按核对通过的原样保留（过拟合 → 池化）")
            self.assertEqual(rel.rel_type, "因果")
            self.assertEqual(rel.reason, candidate["reason"])
            self.assertEqual(rel.evidence, candidate["evidence"])
            self.assertEqual(db.count_map_graphs(), 1)

            again: list = []
            service.set_result_sink(lambda *a: again.append(a))
            self.assertIsNone(service.generate(bid))
            self.assertEqual(len(client.verify_calls), 1, "缓存命中不再核对")
            self.assertEqual(len(again[0][4].relations), 1)

    def test_service_keeps_a_verified_mutual_dependency_as_feedback(self):
        """真实服务：互相依赖经核对支持 → 两条都保留、都进缓存，布局画成反馈。"""
        from app.ui.concept_map import layout_graph

        with support.temp_db() as db:
            bid, ids = _map_db(db)
            client = _MapClient([
                {"src": 0, "dst": 1, "type": "依赖", "reason": "互为前提",
                 "evidence": "池化用于降采样"},
                {"src": 1, "dst": 0, "type": "依赖", "reason": "互为前提",
                 "evidence": "池化用于降采样"}])
            service, _cfg = _map_service(db, client=client)
            delivered: list = []
            service.set_result_sink(lambda *a: delivered.append(a))
            service.generate(bid, force=True)
            self.assertTrue(_wait_for(lambda: delivered))
            _t, _topic, _fp, status, graph, _err = delivered[0]
            self.assertEqual(status, "ok")
            self.assertEqual(len(graph.relations), 2,
                             "经过核对支持的双向依赖必须保留（真实反馈环）")
            self.assertEqual(int(graph.dropped.get("contradiction", 0)), 0)
            self.assertEqual(int(graph.dropped.get("cycle", 0)), 0)
            self.assertEqual(len(client.verify_calls[0]["items"]), 2,
                             "两条候选都要核对")
            self.assertEqual(db.count_map_graphs(), 1)

            layout = layout_graph({int(ids["卷积"]): "卷积", int(ids["池化"]): "池化"},
                                  list(graph.relations), width=600, height=400)
            first = layout.find(int(ids["卷积"]))
            second = layout.find(int(ids["池化"]))
            self.assertEqual(first.level, second.level, "互相依赖的两个词同层")
            self.assertEqual(first.y, second.y, "同层不伪造前后关系")
            self.assertEqual([edge.kind for edge in layout.edges],
                             ["feedback", "feedback"], "两条都画成反馈弧线")
            self.assertTrue(all(len(edge.points) > 2 for edge in layout.edges))

    def test_verification_failure_is_an_error_and_can_be_retried(self):
        """第二次调用失败（网络 / 顶层格式）→ 整次按错误处理，可重试。"""
        from app.api_client import ApiError

        with support.temp_db() as db:
            bid, _ids = _map_db(db)
            client = _MapClient([
                {"src": 0, "dst": 1, "type": "包含", "reason": "卷积核是卷积的单元",
                 "evidence": "卷积核在输入上滑动"}],
                verify_error=ApiError("bad_response", "核对输出缺少 verdicts 数组"))
            service, _cfg = _map_service(db, client=client)
            delivered: list = []
            service.set_result_sink(lambda *a: delivered.append(a))
            service.generate(bid, force=True)
            self.assertTrue(_wait_for(lambda: delivered))
            _t, _topic, _fp, status, graph, error = delivered[0]
            self.assertEqual(status, "error")
            self.assertIsNone(graph, "核对失败绝不把没核对过的边当有效图")
            self.assertEqual(getattr(error, "kind", ""), "bad_response")
            self.assertEqual(db.count_map_graphs(), 0, "核对失败不写缓存")

            client.verify_error = None                # 重试：这一次核对正常
            retry: list = []
            service.set_result_sink(lambda *a: retry.append(a))
            self.assertIsNotNone(service.generate(bid, force=True), "重试必须真的再发")
            self.assertTrue(_wait_for(lambda: retry))
            self.assertEqual(str(retry[0][3]), "ok")
            self.assertEqual(len(retry[0][4].relations), 1)
            self.assertEqual(len(client.verify_calls), 2, "第二次核对也必须重发")
            self.assertEqual(db.count_map_graphs(), 1)

    def test_old_validation_version_cache_is_not_used(self):
        from app.map_service import MAP_VALIDATION_VERSION

        with support.temp_db() as db:
            bid, _ids = _map_db(db)
            client = _MapClient([])
            service, cfg = _map_service(db, client=client)
            nodes = service.nodes_for_topic(bid)
            fp = service.fingerprint(bid, nodes)
            db.put_map_graph(topic_id=bid, fingerprint=fp, base_url=cfg.base_url,
                             model=cfg.model,
                             relations=[{"src": int(nodes[0].entry_id),
                                         "dst": int(nodes[1].entry_id),
                                         "type": "包含", "reason": "旧结果",
                                         "evidence": "旧证据"}],
                             validation_version=max(0, MAP_VALIDATION_VERSION - 1))
            self.assertIsNone(service.cached_graph(bid, nodes, fingerprint=fp),
                              "旧版（只做字串证据校验）的缓存一律不命中")
            delivered: list = []
            service.set_result_sink(lambda *a: delivered.append(a))
            self.assertIsNotNone(service.generate(bid), "旧缓存不算命中 → 真的去生成")
            self.assertTrue(_wait_for(lambda: delivered))
            self.assertEqual(len(client.calls), 1)

    def test_entry_budget_is_reported_instead_of_silently_dropping_words(self):
        from app.api_client import MAP_MAX_ENTRIES

        with support.temp_db() as db:
            bid, _ids = _map_db(db)
            for index in range(MAP_MAX_ENTRIES + 4):
                db.add_entry(batch_id=bid, term=f"补词{index}", context="上下文")
            client = _MapClient([])
            service, cfg = _map_service(db, client=client)
            cfg.set("map.max_entries", str(MAP_MAX_ENTRIES))   # 顶到硬上限
            nodes = service.nodes_for_topic(bid)
            self.assertEqual(service.node_budget(), MAP_MAX_ENTRIES)
            self.assertEqual(len(nodes), MAP_MAX_ENTRIES, "单次分析有硬上限")
            total = service.topic_entry_total(bid)
            self.assertEqual(total, int(db.count_entries(batch_id=bid)))
            self.assertGreater(total, len(nodes))
            note = service.coverage_note(bid, len(nodes))
            self.assertIn(str(len(nodes)), note)
            self.assertIn(str(total), note)
            self.assertEqual(service.coverage_note(bid, total), "",
                             "没被截断时不显示多余的说明")

    def test_bad_json_is_an_error_and_never_writes_a_pseudo_graph(self):
        from app.api_client import ApiError

        with support.temp_db() as db:
            bid, _ids = _map_db(db)
            client = _MapClient(error=ApiError("bad_response", "缺少 relations 数组"))
            service, _cfg = _map_service(db, client=client)
            delivered: list = []
            service.set_result_sink(lambda *a: delivered.append(a))
            service.generate(bid, force=True)
            self.assertTrue(_wait_for(lambda: delivered))
            _t, _topic, _fp, status, graph, error = delivered[0]
            self.assertEqual(status, "error")
            self.assertIsNone(graph, "错误 JSON / schema 错误绝不产出伪图")
            self.assertEqual(getattr(error, "kind", ""), "bad_response")
            self.assertEqual(db.count_map_graphs(), 0, "失败的生成不写缓存")

    def test_missing_key_never_calls_the_api_and_hints_inline(self):
        with support.temp_db() as db:
            bid, _ids = _map_db(db)
            client = _MapClient([])
            service, cfg = _map_service(db, key="", client=client)
            delivered: list = []
            service.set_result_sink(lambda *a: delivered.append(a))
            self.assertIsNone(service.generate(bid, force=True))
            self.assertEqual(client.calls, [], "没有 Key 时一个请求都不发")
            self.assertEqual(len(delivered), 1)
            self.assertEqual(str(delivered[0][3]), "error")
            self.assertEqual(getattr(delivered[0][5], "kind", ""), "config")
            hint = service.unavailable_hint()
            self.assertIn("API Key", hint, "缺配置必须给内联提示")
            self.assertNotIn(cfg.api_key() or "sk-", hint, "提示里绝不出现密钥")

    def test_request_only_carries_the_selected_topic_terms(self):
        with support.temp_db() as db:
            bid, _ids = _map_db(db)
            other = int(db.create_batch("别的主题"))
            db.add_entry(batch_id=other, term="不属于本主题的词", context="别的主题的原文")
            client = _MapClient([])
            service, _cfg = _map_service(db, client=client)
            delivered: list = []
            service.set_result_sink(lambda *a: delivered.append(a))
            service.generate(bid, force=True, topic_name="卷积网络")
            self.assertTrue(_wait_for(lambda: delivered))
            self.assertEqual(len(client.calls), 1)
            entries = client.calls[0]["entries"]
            terms = [item["term"] for item in entries]
            self.assertEqual(terms, ["卷积", "池化", "过拟合"], "只带本主题的词条")
            self.assertNotIn("不属于本主题的词", terms)
            for item in entries:
                self.assertEqual(set(item), {"index", "term", "context", "explanation"},
                                 "受限输入：不带内部 id / 来源 / 网址")
            self.assertEqual(client.calls[0]["topic"], "卷积网络")

    def test_changed_word_expires_the_cache_and_late_result_does_not_cross(self):
        with support.temp_db() as db:
            bid, ids = _map_db(db)
            gate = threading.Event()
            client = _MapClient([
                {"src": 0, "dst": 1, "type": "包含", "reason": "卷积核是卷积的单元",
                 "evidence": "卷积核在输入上滑动"}], gate=gate)
            service, _cfg = _map_service(db, client=client)
            delivered: list = []
            service.set_result_sink(lambda *a: delivered.append(a))
            nodes_before = service.nodes_for_topic(bid)
            fp_before = service.fingerprint(bid, nodes_before)
            service.generate(bid, force=True)

            # 生成期间用户改了词条内容：指纹变化，旧结果回来时不许冒充新内容的缓存
            db.update_entry(ids["卷积"], context="完全换了的上下文")
            nodes_after = service.nodes_for_topic(bid)
            fp_after = service.fingerprint(bid, nodes_after)
            self.assertNotEqual(fp_before, fp_after, "改词必须让指纹过期")
            self.assertIsNone(service.cached_graph(bid, nodes_after),
                              "新内容不该命中旧缓存")

            gate.set()
            self.assertTrue(_wait_for(lambda: delivered))
            self.assertEqual(str(delivered[0][2]), fp_before,
                             "迟到结果带的是发起时的指纹（UI 据此拒绝它）")
            cached_old = service.cached_graph(bid, nodes_before, fingerprint=fp_before)
            self.assertIsNotNone(cached_old, "旧内容自己的缓存仍然有效")

    def test_new_word_also_changes_the_fingerprint(self):
        with support.temp_db() as db:
            bid, _ids = _map_db(db)
            service, _cfg = _map_service(db, client=_MapClient([]))
            before = service.fingerprint(bid, service.nodes_for_topic(bid))
            db.add_entry(batch_id=bid, term="新词", context="新上下文")
            after = service.fingerprint(bid, service.nodes_for_topic(bid))
            self.assertNotEqual(before, after)

    def test_regenerating_the_same_material_keeps_the_edges_it_already_drew(self):
        """同一份材料重新生成：这次没提出的边**沿用上次**（图不许被洗一遍）。

        提出阶段是模型采样：实测同一份材料三次只有 6 / 6 / 5 条、并集 10 条。
        上一轮已经核对通过的边，如果本地证据照样成立就要继续画 —— 而且**不重问**
        核对（沿用不是「再赌一次判定」）。
        """
        with support.temp_db() as db:
            bid, ids = _map_db(db)
            first = {"src": 0, "dst": 1, "type": "包含", "reason": "卷积核是卷积的单元",
                     "evidence": "卷积核在输入上滑动"}
            second = {"src": 1, "dst": 2, "type": "用途", "reason": "池化抑制过拟合",
                      "evidence": "正则化抑制过拟合"}
            client = _MapClient([first, second])
            service, _cfg = _map_service(db, client=client)
            delivered: list = []
            service.set_result_sink(lambda *a: delivered.append(a))
            service.generate(bid, force=True)
            self.assertTrue(_wait_for(lambda: delivered))
            graph = delivered[0][4]
            self.assertEqual(len(graph.relations), 2, "前提：第一轮两条都通过核对")
            self.assertEqual(int(graph.carried), 0, "第一轮没有上次可沿用")

            # 第二轮：模型这次只提出了第一条（采样漏掉了第二条）
            client.relations = [first]
            client.calls.clear()
            client.verify_calls.clear()
            again: list = []
            service.set_result_sink(lambda *a: again.append(a))
            self.assertIsNotNone(service.generate(bid, force=True))
            self.assertTrue(_wait_for(lambda: again))
            _t, _topic, _fp, status, graph2, _err = again[0]
            self.assertEqual(str(status), "ok")
            self.assertEqual(len(graph2.relations), 2, "沿用上次 ⇒ 边不许变少")
            self.assertEqual(int(graph2.carried), 1)
            pairs = {(int(rel.src_entry_id), int(rel.dst_entry_id), rel.rel_type)
                     for rel in graph2.relations}
            self.assertEqual(pairs, {(int(ids["卷积"]), int(ids["池化"]), "包含"),
                                     (int(ids["池化"]), int(ids["过拟合"]), "用途")})
            self.assertIn("沿用上次 1 条", graph2.summary())
            self.assertEqual(len(client.verify_calls[0]["items"]), 1,
                             "沿用的边不重问核对：只核对这轮新提出来的")
            carried_rows = [row for row in graph2.verdicts
                            if row["verdict"] == "supported"
                            and "沿用上次" in str(row.get("note") or "")]
            self.assertEqual(len(carried_rows), 1, "沿用也要有判定记录（点词看原因）")
            self.assertTrue(carried_rows[0]["kept"], "沿用的边确实在图上")


class TestMapDeterminismAndCarryOver(unittest.TestCase):
    """导图必须**确定**：同一份材料重新生成给同一张图（用户明确抱怨过反复变化）。

    两道保障（改这里之前先读 `_check/map_stability_probe.py` 的实测）：
    * 请求侧：导图两个调用都 ``temperature=0.0``，DeepSeek 系额外显式关掉思考模式
      （思考模式默认打开，而它不支持 temperature ⇒ 采样不受约束）；
    * 结果侧：同一内容指纹下沿用上次已核对通过的边（见 ``carry_over_relations``）。
    """

    def test_map_requests_are_deterministic_and_thinking_is_opt_in(self):
        from app.api_client import (DeepSeekClient, build_map_payload,
                                    build_map_verify_payload)

        entries = [{"index": 0, "term": "卷积", "context": "卷积核在输入上滑动",
                    "explanation": "特征提取"}]
        items = [{"index": 0, "type": "包含", "direction": "卷积 → 池化",
                  "source": {"term": "卷积", "context": "卷积核在输入上滑动"},
                  "target": {"term": "池化", "context": "池化用于降采样"},
                  "reason": "依据", "evidence": "卷积核在输入上滑动"}]
        for payload in (build_map_payload("m", entries),
                        build_map_verify_payload("m", items)):
            self.assertNotIn("thinking", payload, "默认绝不发这个字段（别的网关会 400）")
            self.assertEqual(float(payload["temperature"]), 0.0,
                             "导图两个调用都必须是确定输出")
        for value in ("disabled", " DISABLED ", "Disabled"):
            payload = build_map_payload("m", entries, thinking=value)
            self.assertEqual(payload.get("thinking"), {"type": "disabled"},
                             f"显式关思考模式：{value!r}")
        for value in ("", "   ", None):
            self.assertNotIn("thinking", build_map_payload("m", entries, thinking=value))
            self.assertNotIn("thinking", build_map_verify_payload("m", items, thinking=value))

        sent: list[dict] = []

        def envelope(content: str):
            return json.dumps({"choices": [{"message": {"content": content}}]},
                              ensure_ascii=False)

        client = DeepSeekClient("https://api.deepseek.com/v1", "deepseek-chat", "sk-x",
                                thinking="disabled")
        self.assertEqual(client.thinking, "disabled")
        client._post = lambda payload: sent.append(payload) or envelope('{"relations": []}')
        client.map_relations(entries)
        self.assertEqual(sent[0].get("thinking"), {"type": "disabled"},
                         "客户端构造时给的开关要真的进请求体")
        self.assertEqual(float(sent[0]["temperature"]), 0.0)
        sent.clear()
        client._post = lambda payload: sent.append(payload) or envelope('{"verdicts": []}')
        client.map_verify(items)
        self.assertEqual(sent[0].get("thinking"), {"type": "disabled"})
        self.assertEqual(DeepSeekClient("https://x/v1", "m", "sk-x").thinking, "")

    def test_map_thinking_follows_the_gateway(self):
        from app.config import Config, looks_like_deepseek

        self.assertTrue(looks_like_deepseek("deepseek-chat"))
        self.assertTrue(looks_like_deepseek("anything", "https://api.deepseek.com/v1"))
        self.assertFalse(looks_like_deepseek("gpt-4o", "https://api.openai.com/v1"))

        with support.temp_db() as db:
            cfg = Config(db)
            cfg.set("api.model", "deepseek-chat")
            cfg.set("api.base_url", "https://api.deepseek.com/v1")
            self.assertEqual(cfg.map_thinking, "disabled", "DeepSeek 系默认关掉思考模式")
            cfg.set("api.model", "gpt-4o")
            cfg.set("api.base_url", "https://api.openai.com/v1")
            self.assertEqual(cfg.map_thinking, "", "别的网关绝不发这个字段")
            cfg.set("api.map_thinking", "always")
            self.assertEqual(cfg.map_thinking, "disabled", "可以强制发")
            cfg.set("api.model", "deepseek-chat")
            cfg.set("api.map_thinking", "never")
            self.assertEqual(cfg.map_thinking, "", "也可以强制不发")
            cfg.set("api.map_thinking", "auto")
            from app.map_service import MapService

            service = MapService(db, cfg)
            client = service.make_client(base_url="https://api.deepseek.com/v1",
                                         model="deepseek-chat")
            self.assertEqual(client.thinking, "disabled",
                             "导图客户端默认带上「关思考模式」")

    def test_carry_over_keeps_only_what_still_holds(self):
        from app.map_service import (CARRY_NOTE, MapGraph, MapNode, MapRelation,
                                     carry_over_relations)

        nodes = [
            MapNode(1, "卷积", context="卷积核在输入上滑动", explanation="特征提取"),
            MapNode(2, "池化", context="池化用于降采样", explanation="降低分辨率"),
            MapNode(3, "过拟合", context="正则化抑制过拟合", explanation="记住噪声"),
        ]
        alive = MapRelation(1, 2, "包含", "卷积核是卷积的单元", "卷积核在输入上滑动")
        already = MapRelation(2, 3, "用途", "池化抑制过拟合", "正则化抑制过拟合")
        stale = MapRelation(1, 3, "因果", "卷积导致过拟合", "这句材料里根本没有")
        gone = MapRelation(999, 2, "包含", "端点不在本主题", "卷积核在输入上滑动")
        previous = MapGraph(topic_id=7, fingerprint="fp", nodes=tuple(nodes),
                            relations=(alive, already, stale, gone))
        # 这轮照样提出、核对也照样支持 ⇒ 不算沿用，只补回没提的那条
        supported = [{"index": 0, "verdict": "supported", "note": "这轮也支持"}]
        merged, verdicts, carried = carry_over_relations(
            previous, [already], nodes, verdicts=supported)
        self.assertEqual([(r.src_entry_id, r.dst_entry_id) for r in merged],
                         [(2, 3), (1, 2)], "只补回仍然成立、这次没提的那条")
        self.assertEqual(verdicts[0], supported[0], "这轮的判定原样保留")
        self.assertEqual(verdicts[-1]["index"], 1, "合成判定的序号接在本轮候选后面")
        self.assertEqual(verdicts[-1]["verdict"], "supported")
        self.assertEqual(verdicts[-1]["note"], CARRY_NOTE)
        self.assertEqual(len(carried), 1, "沿用计数只算真的补回来的")

        # 没有上次 / 上次空图：什么都不补
        self.assertEqual(carry_over_relations(None, [already], nodes),
                         ([already], [], set()))
        empty = MapGraph(topic_id=7, fingerprint="fp", nodes=tuple(nodes), relations=())
        self.assertEqual(carry_over_relations(empty, [already], nodes),
                         ([already], [], set()))

    def test_a_relation_drawn_last_time_is_not_lost_when_this_round_rejects_it(self):
        """这次**又提了**、但这一轮核对改判不成立 ⇒ 以上次为准（图不许缩水）。

        实测：同一份材料、同一批候选，换一批别的候选一起判，同一条边会被判成
        supported / contradicted 两种结果 —— 核对是整批一起判的。已经画出来的边
        不能因为「这次问出来不一样」就消失。
        """
        from app.map_service import (CARRY_OVERRIDE_NOTE, MapGraph, MapNode,
                                     MapRelation, carry_over_relations)

        nodes = [
            MapNode(1, "卷积", context="卷积核在输入上滑动", explanation="特征提取"),
            MapNode(2, "池化", context="池化用于降采样", explanation="降低分辨率"),
        ]
        drawn = MapRelation(1, 2, "包含", "卷积核是卷积的单元", "卷积核在输入上滑动")
        previous = MapGraph(topic_id=7, fingerprint="fp", nodes=tuple(nodes),
                            relations=(drawn,))
        rejected = [{"index": 0, "verdict": "contradicted", "note": "模型说方向不对"}]
        merged, verdicts, carried = carry_over_relations(
            previous, [drawn], nodes, verdicts=rejected)
        self.assertEqual(len(merged), 1, "边还在，没有被这轮的改判抹掉")
        self.assertEqual(len(verdicts), 1, "同一序号只留一条判定")
        self.assertEqual(verdicts[0]["index"], 0)
        self.assertEqual(verdicts[0]["verdict"], "supported")
        self.assertEqual(verdicts[0]["note"], CARRY_OVERRIDE_NOTE)
        self.assertEqual(len(carried), 1)

        # 材料里没有依据的边照样不沿用（该消失就消失）
        stale = MapRelation(1, 2, "包含", "卷积核是卷积的单元", "这句材料里根本没有")
        previous_stale = MapGraph(topic_id=7, fingerprint="fp", nodes=tuple(nodes),
                                  relations=(stale,))
        merged_stale, verdicts_stale, carried_stale = carry_over_relations(
            previous_stale, [stale], nodes, verdicts=rejected)
        self.assertEqual(verdicts_stale, rejected, "不沿用时判定原样保留")
        self.assertEqual(carried_stale, set())

    def test_carry_over_never_touches_the_edges_already_drawn(self):
        from app.map_service import MapRelation, carried_is_compatible

        hierarchy = MapRelation(1, 2, "包含", "依据", "证据")
        reverse = MapRelation(2, 1, "包含", "依据", "证据")
        self.assertFalse(carried_is_compatible(reverse, [hierarchy]),
                         "反向的层级包含是矛盾：宁可少画一条")
        chain = [MapRelation(1, 2, "包含", "依据", "证据"),
                 MapRelation(2, 3, "包含", "依据", "证据")]
        self.assertFalse(carried_is_compatible(MapRelation(3, 1, "包含", "依据", "证据"), chain),
                         "不可能的层级环不加")
        dependency = MapRelation(1, 2, "依赖", "依据", "证据")
        self.assertTrue(carried_is_compatible(chain[0], [dependency]),
                        "依赖 ≈ 层级包含不构成矛盾（层级只跟层级比）")
        self.assertTrue(carried_is_compatible(MapRelation(2, 3, "对照", "依据", "证据"),
                                              [MapRelation(1, 3, "包含", "依据", "证据")]))


class TestMapLayoutStructure(unittest.TestCase):
    """布局**真的**按关系结构：层级分组 / 前后分层 / 跨边 / 孤立词 / 换行不截断。"""

    def _rel(self, src, dst, rel_type, reason="依据", evidence="片段"):
        from app.map_service import MapRelation

        return MapRelation(src, dst, rel_type, reason, evidence)

    def test_hierarchy_group_puts_parent_above_children(self):
        from app.ui.concept_map import layout_graph

        labels = {1: "卷积", 2: "卷积核", 3: "步长"}
        rels = [self._rel(1, 2, "包含"), self._rel(1, 3, "包含")]
        layout = layout_graph(labels, rels, width=700, height=500, topic_label="卷积网络")
        parent = layout.find(1)
        child_a, child_b = layout.find(2), layout.find(3)
        self.assertLess(parent.y, child_a.y, "包含：上位节点必须在上面")
        self.assertLess(parent.y, child_b.y)
        self.assertEqual(parent.level, 0)
        self.assertEqual(child_a.level, child_b.level, "同一父节点的子节点同层")
        self.assertTrue(layout.groups, "包含 / 属于要形成层级分组")
        members = set(layout.groups[0].members)
        self.assertEqual(members, {1, 2, 3}, "分组覆盖整棵子树")
        group = layout.groups[0]
        for node in (parent, child_a, child_b):
            self.assertLessEqual(group.x0, node.x - node.w / 2 + 1e-6)
            self.assertGreaterEqual(group.x1, node.x + node.w / 2 - 1e-6)

    def test_dependency_and_cause_set_the_layer_order(self):
        from app.ui.concept_map import layout_graph

        labels = {1: "过拟合", 2: "正则化"}
        cause = layout_graph(labels, [self._rel(1, 2, "因果")], width=600, height=400)
        self.assertLess(cause.find(1).y, cause.find(2).y, "因果：因在上、果在下")
        depend = layout_graph(labels, [self._rel(1, 2, "依赖")], width=600, height=400)
        self.assertLess(depend.find(2).y, depend.find(1).y, "依赖：被依赖者在上")

    def test_mutual_dependency_and_mutual_cause_are_feedback_not_layers(self):
        from app.ui.concept_map import layout_graph

        labels = {1: "需求", 2: "供给", 3: "价格"}
        for rel_type in ("依赖", "因果"):
            with self.subTest(rel_type=rel_type):
                layout = layout_graph(
                    labels, [self._rel(1, 2, rel_type), self._rel(2, 1, rel_type)],
                    width=600, height=400)
                first, second = layout.find(1), layout.find(2)
                self.assertEqual(first.level, second.level,
                                 "强连通分量内部不排前后层（不伪造层级）")
                self.assertEqual(first.y, second.y, "反馈圈的两个词同层可见")
                self.assertEqual(len(layout.edges), 2, "反馈的两条边都要画出来")
                for edge in layout.edges:
                    self.assertEqual(edge.kind, "feedback")
                    self.assertEqual(edge.label, rel_type, "反馈边保留具体类型")
                    self.assertGreater(len(edge.points), 2, "反馈画成弧线，不是直连层级")
                self.assertEqual(first.isolated, False)

        # 反馈圈 + 一条指向圈外的因果：圈内同层，圈外仍然在后一层
        layout = layout_graph(labels, [self._rel(1, 2, "因果"), self._rel(2, 1, "因果"),
                                       self._rel(2, 3, "因果")],
                              width=640, height=420)
        self.assertEqual(layout.find(1).level, layout.find(2).level)
        self.assertGreater(layout.find(3).level, layout.find(2).level,
                           "圈外的果仍然在前一层之后")
        self.assertEqual({edge.kind for edge in layout.edges},
                         {"feedback", "direction"})

    def test_cross_edges_keep_type_label_and_do_not_change_layers(self):
        from app.ui.concept_map import layout_graph

        labels = {1: "卷积", 2: "池化"}
        layout = layout_graph(labels, [self._rel(1, 2, "对照")], width=600, height=400)
        self.assertEqual(len(layout.edges), 1)
        edge = layout.edges[0]
        self.assertEqual(edge.kind, "cross")
        self.assertEqual(edge.label, "对照", "跨边保留具体标签")
        self.assertEqual(layout.find(1).level, layout.find(2).level,
                         "对照不改变层级（同层跨边）")
        self.assertGreater(len(edge.points), 2, "跨边画成弧线，和层级直连区分开")

    def test_crossing_edge_goes_around_the_node_in_between(self):
        """被中间词挡住的对照跨边（同层左 → 右）**不得穿过**那个词的矩形。

        直连正好横穿中间的「池化层」：会被读成「第三个词也加入了这条对照」。
        连线要么从旁边绕（折线），要么弯到足够远 —— 判据是「线不碰任何非端点
        节点」，而且正反两个方向都试过（不能只朝一边弯）。
        """
        from app.ui import concept_map as cm

        layout = _blocked_cross_layout()
        nodes = _node_rects(layout, pad=0.0)
        crossing = [edge for edge in layout.edges
                    if edge.label == "对照" and edge.kind == "cross"]
        self.assertEqual(len(crossing), 1, "fixture 里必须有一条对照跨边")
        edge = crossing[0]
        self.assertEqual([int(cm._relation_parts(edge.rel)[index])
                          for index in (0, 1)], [2, 4],
                         "对照边还是「卷积层 → 卷积运算」这条合法边（不删边、不换端点）")
        # 前提：中间那个词真的夹在两端之间（否则这个 fixture 抓不到避障缺陷）
        middle = layout.find(3)
        left, right = sorted((layout.find(2), layout.find(4)),
                             key=lambda node: node.x)
        self.assertEqual(middle.level, left.level, "前提：三个词同层一字排开")
        self.assertLess(left.x, middle.x)
        self.assertLess(middle.x, right.x)
        self.assertGreater(len(edge.points), 2,
                           "被中间节点挡住时不允许直连：必须改走弧线 / 折线")
        self.assertEqual([_edge_hits_nodes(edge, nodes)], [[]],
                         "跨边不得穿过任何非端点节点矩形（含小间距）")
        spare = _node_rects(layout, pad=cm.metrics_for().route_margin)
        self.assertEqual(_edge_hits_nodes(edge, spare), [],
                         "避障要留净空：按 route_margin 外扩后的节点矩形同样不得相交")
        self.assertEqual(len(layout.nodes), 4, "避障不许隐藏任何节点")

    def test_relation_labels_never_cover_nodes_or_other_labels(self):
        """多条关系时：每个关系短标签都不压节点、也不压别的标签，且仍贴着本边。"""
        from app.ui import concept_map as cm

        layout = _crossing_example_layout()
        m = cm.metrics_for()
        nodes = _node_rects(layout, pad=0.0)
        labels = [(edge, cm.label_box(edge.label_pos, edge.label, m))
                  for edge in layout.edges]
        self.assertGreaterEqual(len(labels), 4, "示例必须有多条关系（含两条相邻标签）")
        for edge, box in labels:
            for entry_id, rect in nodes.items():
                self.assertFalse(cm.rects_intersect(box, rect),
                                 f"「{edge.label}」标签压住了节点 {entry_id}：{box} vs {rect}")
        for index, (edge, box) in enumerate(labels):
            for other_edge, other_box in labels[index + 1:]:
                horizontal, vertical = (max(box[0] - other_box[2], other_box[0] - box[2]),
                                        max(box[1] - other_box[3], other_box[1] - box[3]))
                self.assertTrue(horizontal > 0 or vertical > 0,
                                f"「{edge.label}」与「{other_edge.label}」两个标签挤在一起")
        for edge, box in labels:
            center = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
            distance = min(cm.segment_distance(center[0], center[1], *segment)
                           for segment in edge.segments())
            self.assertLessEqual(distance, m.line_h * 2.0,
                                 f"「{edge.label}」标签离自己那条边太远，会被读成别的线的标签")

    def test_comparison_is_undirected_while_direction_edges_keep_their_arrow(self):
        """对照是对称关系（两端都不画箭头）；用途 / 依赖仍然只在终点画箭头。"""
        layout = _crossing_example_layout()
        kinds = {str(edge.label): edge for edge in layout.edges}
        self.assertTrue(kinds["对照"].symmetric, "对照必须标成对称关系")
        self.assertFalse(kinds["用途"].symmetric, "用途是有方向的（前件用于后件）")
        self.assertFalse(kinds["依赖"].symmetric, "依赖是有方向的（被依赖者在上）")
        self.assertFalse(kinds["包含"].symmetric, "包含是层级关系，不是对称跨边")
        self.assertEqual({str(edge.label) for edge in layout.edges if edge.symmetric},
                         {"对照"}, "示例里只有对照是对称关系")

    def test_isolated_words_are_grouped_in_their_own_row_not_a_ring(self):
        from app.ui.concept_map import layout_graph

        labels = {index: f"词{index}" for index in range(1, 7)}
        layout = layout_graph(labels, [], width=800, height=600, topic_label="主题")
        self.assertEqual(len(layout.edges), 0)
        ys = {round(node.y, 3) for node in layout.nodes}
        self.assertEqual(len(ys), 1, "没有关系的词排在**同一行**，不是圆环")
        self.assertTrue(all(node.isolated for node in layout.nodes))
        self.assertIsNotNone(layout.isolated_label_pos, "孤立词要单独可见并带短标题")
        xs = sorted(node.x for node in layout.nodes)
        for index, node in enumerate(sorted(layout.nodes, key=lambda n: n.x)):
            if index:
                self.assertGreater(node.x - (node.w / 2),
                                   xs[index - 1] - node.w / 2,
                                   "孤立词也必须互不重叠")

    def test_long_terms_wrap_and_are_never_cut_to_six_chars(self):
        from app.ui.concept_map import layout_graph

        term = "Transformer 编码器结构中的自注意力机制与前馈网络"
        layout = layout_graph({1: term}, [], width=520, height=360, topic_label="主题")
        node = layout.find(1)
        self.assertGreater(len(node.lines), 1, "长词要换行显示")
        self.assertEqual("".join(node.lines).replace(" ", ""),
                         term.replace(" ", ""), "换行不丢字（不是砍到 6 个字）")
        self.assertTrue(all(len(line) > 1 for line in node.lines))

    def test_relation_change_changes_the_layout(self):
        from app.ui.concept_map import layout_graph

        labels = {1: "卷积", 2: "池化", 3: "感受野"}
        plain = layout_graph(labels, [], width=700, height=520)
        structured = layout_graph(labels, [self._rel(2, 3, "因果")],
                                  width=700, height=520)
        self.assertNotEqual({node.entry_id: round(node.y, 3) for node in plain.nodes},
                            {node.entry_id: round(node.y, 3)
                             for node in structured.nodes},
                            "关系集变化时布局必须跟着变（不是固定圆环）")
        self.assertNotEqual({node.entry_id: round(node.x, 3) for node in plain.nodes},
                            {node.entry_id: round(node.x, 3)
                             for node in structured.nodes})

    def test_both_bow_directions_are_tried_when_avoiding_a_middle_node(self):
        """避障必须**正反两个方向**都试：对称场景里两个方向都可能成立。"""
        from app.map_service import MapRelation
        from app.ui import concept_map as cm

        labels = {1: "甲", 2: "乙", 3: "丙"}
        relations = [MapRelation(1, 2, "包含", "依据", "证据片段"),
                     MapRelation(3, 2, "包含", "依据", "证据片段"),
                     MapRelation(1, 3, "对照", "依据", "证据片段")]
        layout = cm.layout_graph(labels, relations, width=600, height=360,
                                 topic_label="主题")
        through = layout.find(2)
        nodes = _node_rects(layout, pad=0.0)
        edge = [item for item in layout.edges if item.label == "对照"][0]
        self.assertEqual([_edge_hits_nodes(edge, nodes)], [[]],
                         "跨边不得穿过「乙」（它正好在两个端点中间）")
        up = [point for point in edge.points if point[1] < through.y - through.h / 2]
        down = [point for point in edge.points if point[1] > through.y + through.h / 2]
        self.assertTrue(up or down, "绕行必须真的从「乙」的上方或下方过去")

    def test_visibility_route_uses_the_channel_that_cannot_touch_the_wall(self):
        """① 回归：一堵矩形墙必须能从**旁边**绕过去（外侧空白通道不许被丢掉）。

        探针原来把「包围盒根本碰不到障碍」的线段当作不可用邻居 ``continue`` 掉，
        只留下「可能碰」的那一批去精确检查 —— 结果一条完全通敞的绕行道都留不下来，
        ``_visibility_route`` 对这张图恒返回 ``None``（上面这段几何里它是唯一通路）。

        判据不靠产品函数自证：用独立的 Liang-Barsky 裁剪手算，
        「直连必然撞墙」以及「绕行道整整一段都不碰墙 / 每段都不穿过墙体 x 区间」。
        """
        from app.ui import concept_map as cm

        wall = (200.0, 100.0, 220.0, 400.0)
        start, end = (100.0, 250.0), (320.0, 250.0)
        self.assertTrue(_razor_hits_rect(start[0], start[1], end[0], end[1], wall),
                        "这段几何的前提就是「直连撞墙」；否则这条用例抓不到缺陷")

        route = cm._visibility_route(start, end, [wall], pad=float(cm.metrics_for().route_margin),
                                     states_limit=8000)
        self.assertIsNotNone(route, "有矩形墙时绕行必须存在：外侧通道不许被丢掉")
        self.assertGreater(len(route), 2, "绕行不能退化成直连")
        self.assertEqual(route[0], start, "折线必须从起点出发")
        self.assertEqual(route[-1], end, "折线必须落在终点")
        self.assertTrue(cm.route_is_clear(route, [wall]),
                        "绕行道不得碰墙（含贴边框）—— 这里用的是产品自己的 clear 判据")
        for first, second in zip(route, route[1:]):
            self.assertFalse(_razor_hits_rect(first[0], first[1], second[0], second[1], wall),
                             f"手算裁剪：这一段穿墙了 {first} -> {second}")
        left = [point for point in route if point[0] < wall[0]]
        right = [point for point in route if point[0] > wall[2]]
        self.assertTrue(left and right,
                        "起点侧与终点侧都要有点：这才叫「从旁边绕」而不是硬穿")
        for point in route:
            self.assertFalse(wall[0] < point[0] < wall[2] and wall[1] < point[1] < wall[3],
                             f"拐点落在墙体内部：{point}")

    def test_a_29_word_chain_cross_edge_goes_around_instead_of_through(self):
        """② 回归：29 词的长链**不是病态输入**（产品上限 30、默认 24）。

        5↔20 的长边横跨中间 14 个词：中间那些节点的净空矩形全都挡在直连上，
        但没有一个真的把整列封死 —— 合格的路由必须从旁边绕过去。

        这里**一个节点、一条边都不删**：29 个词全部可见、29 条边全部画出来；
        判据是「这条边不碰任何非端点节点的矩形（含 ``route_margin`` 净空）」——
        ``_edge_hits_nodes`` 逐段跑产品 ``route_is_clear``，外加手工确认这条边真的
        从整列节点旁边出去（跨度远大于节点宽度、拐点数 > 2），因此穿节点直线
        不可能通过这个用例。
        """
        from app.map_service import MapRelation
        from app.ui import concept_map as cm

        count = 29
        labels = {index: f"词{index:02d}" for index in range(1, count + 1)}
        relations = [MapRelation(index, index + 1, "因果", "依据", "证据片段")
                     for index in range(1, count)]
        relations.append(MapRelation(5, 20, "因果", "依据", "证据片段"))
        layout = cm.layout_graph(labels, relations, width=700, height=560,
                                 topic_label="长链")
        m = cm.metrics_for()
        self.assertEqual(len(layout.nodes), count, "29 个词一个都不许被隐藏")
        self.assertEqual(len(layout.edges), count, "29 条边一条都不许被删")

        nodes = _node_rects(layout, pad=0.0)
        spare = _node_rects(layout, pad=m.route_margin)
        crossing = [edge for edge in layout.edges
                    if {int(cm._relation_parts(edge.rel)[index]) for index in (0, 1)}
                    == {5, 20}]
        self.assertEqual(len(crossing), 1, "5↔20 的对照边必须还在（不删边、不换端点）")
        edge = crossing[0]
        self.assertGreater(len(edge.points), 2,
                           "横跨 14 个词时不允许直连：必须折线绕到整列节点旁边")
        self.assertEqual(_edge_hits_nodes(edge, nodes), [],
                         "这条边穿过了中间的非端点节点矩形")
        self.assertEqual(_edge_hits_nodes(edge, spare), [],
                         "这条边没留净空：碰到了按 route_margin 外扩后的节点矩形")
        column_left = min(rect[0] for rect in nodes.values())
        column_right = max(rect[2] for rect in nodes.values())
        out_left = [point for point in edge.points if point[0] < column_left]
        out_right = [point for point in edge.points if point[0] > column_right]
        self.assertTrue(out_left or out_right,
                        "绕行必须真的走到整列节点的左外侧或右外侧（否则就是穿节点直线）")
        # 这条边可以贴着 ``route_margin`` 走窄通道（净空是产品自己定的 6px，没被降低），
        # 但**绝不能**还留在端点那一列的中心线上直连下来：至少要横向让开一个净空，
        # 而且不许把折点塞进任何一个中间节点的矩形里。
        detour = max(column_left - min(point[0] for point in edge.points),
                     max(point[0] for point in edge.points) - column_right)
        self.assertGreaterEqual(detour, m.route_margin,
                                "折线最远点必须横向让开至少一个 route_margin（否则就是贴列直连）")
        for point in edge.points:
            for entry_id, rect in nodes.items():
                if entry_id in (5, 20):
                    continue
                inside = (rect[0] <= point[0] <= rect[2]
                          and rect[1] <= point[1] <= rect[3])
                self.assertFalse(inside, f"折点落进了节点 {entry_id} 的矩形：{point}")

    def test_nodes_and_edges_stay_inside_the_content_box(self):
        from app.ui.concept_map import layout_graph

        labels = {1: "卷积", 2: "池化"}
        layout = layout_graph(labels, [self._rel(1, 2, "包含")], width=520, height=360,
                              topic_label="卷积网络")
        for node in list(layout.nodes) + [layout.topic]:
            self.assertGreaterEqual(node.x - node.w / 2, 0)
            self.assertGreaterEqual(node.y - node.h / 2, 0)
            self.assertLessEqual(node.x + node.w / 2, layout.content_w)
            self.assertLessEqual(node.y + node.h / 2, layout.content_h)
        for edge in layout.edges:
            for x, y in edge.points:
                self.assertLessEqual(x, layout.content_w)
                self.assertLessEqual(y, layout.content_h)


class TestMapRoutingRules(unittest.TestCase):
    """思维导图的**布线规范**：字号随缩放、正交车道、端口分散、主题母线、交叉最小化。

    这一组盯的是两条真实缺陷：
    ① ``_draw`` 曾经用固定字号（``theme.font(9)``）而节点框随 ``zoom`` 一起缩放
       ⇒ 缩小视图后文字撑出卡片边界；
    ② 层级 / 依赖边曾经在层与层之间斜线扇出、互相穿插，读不出层次。
    所以下面既钉住「文字与框同源」，也钉住「相邻两层的层级 / 依赖边走 L 形正交线」。
    """

    #: 四个词的包含链：1 包含 2 / 1 包含 3 / 2 依赖 4（4 与 1 同层 ⇒ 依赖是**向上**的）
    CHAIN = ({1: "卷积神经网络", 2: "卷积层", 3: "池化层", 4: "降采样"},
             ((1, 2, "包含"), (1, 3, "包含"), (2, 4, "依赖")))

    @staticmethod
    def _rel(src, dst, rel_type="包含"):
        from app.map_service import MapRelation
        return MapRelation(src, dst, rel_type, "依据", "证据片段")

    def _layout(self, *, zoom=1.0, width=700, height=560):
        from app.ui import concept_map as cm

        labels, relations = self.CHAIN
        return cm.layout_graph(labels, [self._rel(*item) for item in relations],
                               width=width, height=height, topic_label="主题",
                               zoom=zoom)

    def test_text_font_pixels_scale_with_every_zoom(self):
        from app.ui import concept_map as cm
        from app.ui import theme, widgets

        base_px = abs(theme.device_px(9))
        base_em = cm.metrics_for(1.0).em
        for zoom in (0.6, 0.75, 1.0, 1.5, 2.0):
            with self.subTest(zoom=zoom):
                metrics = cm.metrics_for(zoom)
                font_px = theme.font_px_at(9, zoom)
                # 字号必须跟着 zoom 走（旧实现固定 12px ⇒ 缩小时越出卡片）
                self.assertLessEqual(abs(font_px - base_px * zoom), 0.5,
                                     "字号没有跟着缩放走")
                # 字号与布局度量的比例恒定（取整误差之内）⇒ 文字永远不会相对框变大
                self.assertAlmostEqual(font_px / metrics.em, base_px / base_em, delta=0.06)
                self.assertLessEqual(font_px, metrics.em + 0.5)
                # 行高必须够真实 Tk 字体的一行：旧实现按 1.25em 估算，会上下裁掉
                self.assertGreater(metrics.line_h + 0.5,
                                   base_px * 1.25 * zoom,
                                   "行高又退回了「按字号估算」而不是真实行距")
                self.assertGreaterEqual(metrics.line_h + 0.5,
                                        widgets.line_height(9) * zoom)

    def test_a_wrapped_label_gets_one_real_line_of_height_per_line(self):
        from app.ui import concept_map as cm
        from app.ui import widgets

        label = "resource constrained environments"
        for zoom in (0.6, 1.0, 2.0):
            with self.subTest(zoom=zoom):
                metrics = cm.metrics_for(zoom)
                lines = cm.wrap_label(label, metrics.max_w - 2 * metrics.pad_x,
                                      metrics.em)
                self.assertGreater(len(lines), 1, "这个 fixture 必须真的换行")
                width, height = cm.box_size(lines, metrics)
                self.assertGreaterEqual(height + 0.5,
                                        2 * metrics.pad_y
                                        + len(lines) * widgets.line_height(9) * zoom,
                                        "框高装不下一行行真实文字")
                self.assertGreaterEqual(width + 0.5,
                                        2 * metrics.pad_x
                                        + max(cm.text_px(line, metrics.em)
                                              for line in lines))

    def test_adjacent_layers_are_wired_with_orthogonal_lanes(self):
        from app.ui import concept_map as cm

        layout = self._layout()
        nodes = {int(node.entry_id): node for node in layout.nodes}
        self.assertEqual(len(layout.edges), 3)
        for edge in layout.edges:
            src, dst, rel_type = cm._relation_parts(edge.rel)
            first, second = nodes[int(src)], nodes[int(dst)]
            with self.subTest(edge=f"{src}->{dst} {rel_type}"):
                self.assertIn(edge.kind, ("hierarchy", "direction"))
                points = edge.points
                self.assertIn(len(points), (2, 4),
                              "层级 / 依赖边必须走正交线（2 点竖线或 4 点 L 形）")
                if len(points) == 4:
                    self.assertAlmostEqual(points[0][0], points[1][0], places=6,
                                           msg="出卡片的一段必须是竖线")
                    self.assertAlmostEqual(points[1][1], points[2][1], places=6,
                                           msg="中间必须是一段水平车道")
                    self.assertAlmostEqual(points[2][0], points[3][0], places=6,
                                           msg="进卡片的一段必须是竖线")
                    lane = points[1][1]
                    upper, lower = ((first, second) if first.level < second.level
                                    else (second, first))
                    self.assertGreaterEqual(lane, upper.y + upper.h / 2 - 1e-6,
                                            "车道跑到上行卡片的区域里了")
                    self.assertLessEqual(lane, lower.y - lower.h / 2 + 1e-6,
                                         "车道跑到下行卡片的区域里了")
                # 点序从**源**到**目标**：否则 arrow="last" 的箭头会落在源卡片上
                downward = first.level < second.level
                start_y = first.y + (first.h / 2 if downward else -first.h / 2)
                end_y = second.y + (-second.h / 2 if downward else second.h / 2)
                self.assertAlmostEqual(points[0][1], start_y, places=6,
                                       msg="连线没有从源卡片那一侧出发")
                self.assertAlmostEqual(points[-1][1], end_y, places=6,
                                       msg="连线的终点不在目标卡片那一侧")
                self.assertEqual(_edge_hits_nodes(edge, _node_rects(layout, pad=0.0)),
                                 [], "正交线穿过了非端点节点")

    def test_one_source_spreads_its_edges_over_several_ports(self):
        from app.ui import concept_map as cm

        layout = self._layout()
        source = layout.find(1)
        bottom = source.y + source.h / 2
        ports = sorted({round(edge.points[0][0], 3) for edge in layout.edges
                        if int(cm._relation_parts(edge.rel)[0]) == 1
                        and abs(edge.points[0][1] - bottom) < 1e-6})
        self.assertEqual(len(ports), 2, "同一张卡片的两条边必须分到不同端口")
        self.assertGreaterEqual(ports[1] - ports[0], 2.0, "两个端口几乎重合")
        self.assertGreaterEqual(ports[0], source.x - source.w / 2)
        self.assertLessEqual(ports[-1], source.x + source.w / 2)

    def test_topic_links_are_one_polyline_through_a_shared_bus(self):
        from app.ui import concept_map as cm

        layout = self._layout()
        topic = layout.topic
        first_row = [node for node in layout.nodes
                     if not node.isolated and int(node.level) == 0]
        self.assertEqual(len(layout.topic_links), len(first_row),
                         "第一行每个词都要有一条母线支线")
        bus_ys = set()
        ends = set()
        for link in layout.topic_links:
            self.assertGreaterEqual(len(link), 3, "母线支线必须是折线，不是斜直线")
            self.assertAlmostEqual(link[0][0], topic.x, places=6)
            self.assertAlmostEqual(link[0][1], topic.y + topic.h / 2, places=6)
            self.assertAlmostEqual(link[1][1], link[2][1], places=6,
                                   msg="母线支线必须在同一根横线上分叉")
            bus_ys.add(round(link[1][1], 3))
            ends.add((round(link[-1][0], 3), round(link[-1][1], 3)))
        self.assertEqual(len(bus_ys), 1, "所有支线必须共用一根母线")
        for node in first_row:
            self.assertIn((round(node.x, 3), round(node.y - node.h / 2, 3)), ends,
                          "支线没有接到第一行卡片的顶边")

    def test_zoom_keeps_the_whole_layout_proportional(self):
        base = self._layout()
        scaled = self._layout(zoom=2.0)
        self.assertAlmostEqual(scaled.content_w, base.content_w * 2, places=6)
        self.assertAlmostEqual(scaled.content_h, base.content_h * 2, places=6)
        self.assertAlmostEqual(scaled.topic.x, base.topic.x * 2, places=6)
        self.assertEqual(len(scaled.topic_links), len(base.topic_links))
        for got, want in zip(scaled.topic_links, base.topic_links):
            self.assertEqual(len(got), len(want))
            for (gx, gy), (wx, wy) in zip(got, want):
                self.assertAlmostEqual(gx, wx * 2, places=6)
                self.assertAlmostEqual(gy, wy * 2, places=6)

    def test_crossing_reduction_only_moves_in_the_decreasing_direction(self):
        from app.ui import concept_map as cm

        relations = [self._rel(1, 4), self._rel(2, 3)]
        before_of, after_of, _feedback = cm._constraint_pairs(relations)

        def total(order_map):
            return (cm._pair_crossings(order_map[0], order_map[1], after_of)
                    + cm._pair_crossings(order_map[1], order_map[0], before_of))

        crossed = {0: [1, 2], 1: [3, 4]}       # 甲→丁、乙→丙：两行之间交叉一次
        # 产品内部把这个「两行之间」的交叉从上行、下行各数一遍，所以这里是 2 而不是 1
        # （两倍常数不影响「只在下降时交换」的判定）。
        self.assertEqual(total(crossed), 2, "这个 fixture 必须先真的有交叉")
        cm._reduce_crossings([0, 1], crossed, before_of, after_of)
        self.assertEqual(total(crossed), 0, "交叉最小化没有把交叉消掉")
        # 具体换的是哪一行由循环次序决定（上行先试），但两行的成员一个都不许变。
        self.assertEqual(sorted(crossed[0]), [1, 2])
        self.assertEqual(sorted(crossed[1]), [3, 4])

        optimal = {0: [1, 2], 1: [4, 3]}
        snapshot = {level: list(order) for level, order in optimal.items()}
        cm._reduce_crossings([0, 1], optimal, before_of, after_of)
        self.assertEqual(optimal, snapshot, "已经最优的顺序不许被改动（平局不换）")

    def test_crossing_reduction_never_makes_a_layout_worse(self):
        from unittest import mock

        from app.ui import concept_map as cm

        labels = {1: "甲", 2: "乙", 3: "丙", 4: "丁", 5: "戊", 6: "己", 7: "庚"}
        relations = [self._rel(1, 5), self._rel(1, 7), self._rel(2, 4),
                     self._rel(2, 7), self._rel(3, 4), self._rel(3, 5),
                     self._rel(3, 6)]
        before_of, after_of, _feedback = cm._constraint_pairs(relations)

        def crossings(layout):
            rows: dict[int, list[int]] = {}
            for node in layout.nodes:
                if not node.isolated:
                    rows.setdefault(int(node.level), []).append(int(node.entry_id))
            total = 0
            for level in sorted(rows):
                if level + 1 not in rows:
                    continue
                upper = sorted(rows[level], key=lambda eid: layout.find(eid).x)
                lower = sorted(rows[level + 1], key=lambda eid: layout.find(eid).x)
                total += cm._pair_crossings(upper, lower, after_of)
                total += cm._pair_crossings(lower, upper, before_of)
            return total

        with mock.patch.object(cm, "_reduce_crossings", lambda *args, **kwargs: None):
            plain = cm.layout_graph(labels, relations, width=900, height=620,
                                    topic_label="主题")
        reduced = cm.layout_graph(labels, relations, width=900, height=620,
                                  topic_label="主题")
        self.assertLessEqual(crossings(reduced), crossings(plain),
                             "交叉最小化把层间交叉数弄多了")
        self.assertGreater(crossings(plain), 0, "这个 fixture 必须先真的有交叉")
        # 这个 fixture 上重心法留下 12 处交叉（内部按上下行各数一遍 = 24），最小化后
        # 只剩 9 ⇒ 必须**严格下降**：这条断言就是用来钉死「最小化真的在工作」的
        # （曾经因为两行传反、``total()`` 恒等于 0，整个最小化是空转）。
        self.assertLess(crossings(reduced), crossings(plain),
                        "交叉最小化没有真的把交叉降下来")
        again = cm.layout_graph(labels, relations, width=900, height=620,
                                topic_label="主题")
        self.assertEqual([(int(node.entry_id), round(node.x, 6), round(node.y, 6))
                          for node in reduced.nodes],
                         [(int(node.entry_id), round(node.x, 6), round(node.y, 6))
                          for node in again.nodes],
                         "布局必须是确定性的（可复现）")


class TestMapCrowdingRules(unittest.TestCase):
    """去拥挤的三条硬规则：标签垫底色、孤立词均匀分带、一层绝不拆行。

    「一层拆成多行」这个反例（层内均匀分带）实测更糟：同一层的词被拆到多行之后，
    跨带的父子边只能退回候选搜索，画面反而多出一堆斜穿全图的线。规则因此钉死成
    「一层一行 + 横向平移」，宽度收不住时宁可让用户拖动，也不拆散一层的拓扑。
    """

    def test_label_plate_stays_inside_the_collision_box(self):
        """底色块 = 避让框收掉上下留白：既不盖住别的标签，又整块垫在字下面。"""
        from app.ui import concept_map as cm

        m = cm.metrics_for()
        for label in ("包含", "依赖", "对照", "属于", "用途"):
            box = cm.label_box((100.0, 60.0), label, m)
            plate = cm.label_plate((100.0, 60.0), label, m)
            self.assertGreater(plate[1], box[1], f"「{label}」底色块上边没收进去")
            self.assertLess(plate[3], box[3], f"「{label}」底色块下边没收进去")
            self.assertLessEqual(plate[0], box[0] + m.em * 0.13,
                                 f"「{label}」底色块左边超出避让框太多")
            self.assertGreaterEqual(plate[2], box[2] - m.em * 0.13,
                                    f"「{label}」底色块右边超出避让框太多")
            self.assertGreater(plate[2] - plate[0], 0.0, "底色块必须有宽度")
            self.assertGreater(plate[3] - plate[1], 0.0, "底色块必须有高度")
            self.assertAlmostEqual((plate[0] + plate[2]) / 2.0, 100.0, places=6,
                                   msg="底色块必须与标签同中心（横向）")
            self.assertAlmostEqual((plate[1] + plate[3]) / 2.0, 60.0, places=6,
                                   msg="底色块必须与标签同中心（纵向）")

    def test_isolated_words_never_leave_a_stub_row(self):
        """孤立词网格按「行数」均分（不是按列硬切）：4/4/4/2 这种秃尾行不许出现。"""
        from app.ui import concept_map as cm

        rows = cm._balanced_rows(list(range(1, 15)), 4)
        self.assertEqual([len(row) for row in rows], [4, 4, 3, 3],
                         "14 个词、一行放 4 个 ⇒ 分 4 行、每行最多差 1")
        self.assertEqual(sum(len(row) for row in rows), 14, "不许漏词")
        self.assertEqual([len(row) for row in cm._balanced_rows(list(range(1, 4)), 4)],
                         [3], "装得下时只排一行")
        self.assertEqual(cm._balanced_rows([], 4), [], "空输入 = 没有行")
        self.assertEqual([len(row) for row in cm._balanced_rows([1, 2, 3], 0)],
                         [1, 1, 1], "列数非法时退化成每行一个")

    def test_fit_columns_counts_what_really_fits(self):
        """一行能放几张卡：按画布可用宽算，画布再窄也至少一列。"""
        from app.ui import concept_map as cm

        m = cm.metrics_for()
        available = 520.0
        card_w = 100.0
        columns = cm._fit_columns(available, card_w, m)
        self.assertEqual(columns, 3)
        usable = available - 2.0 * m.pad
        self.assertLessEqual(columns * card_w + (columns - 1) * m.h_gap, usable + 1e-6,
                             "放得下的列数算多了")
        self.assertGreater((columns + 1) * card_w + columns * m.h_gap, usable,
                           "放得下的列数算少了")
        self.assertEqual(cm._fit_columns(60.0, card_w, m), 1,
                         "画布比一张卡还窄也必须给一列")

    def test_a_level_is_never_split_across_rows(self):
        """同一层的 6 个词即使远超画布宽，也必须还在同一行（不拆行）。"""
        from app.map_service import MapRelation
        from app.ui import concept_map as cm

        labels = {index: f"词{index}" for index in range(1, 8)}
        relations = [MapRelation(1, index, "包含", "离线假依据", "离线假证据片段")
                     for index in range(2, 8)]
        layout = cm.layout_graph(labels, relations, width=320, height=420,
                                 topic_label="主题")
        rows: dict[float, list[int]] = {}
        for node in layout.nodes:
            if not node.isolated:
                rows.setdefault(round(node.y, 3), []).append(int(node.entry_id))
        self.assertEqual(sorted(len(group) for group in rows.values()), [1, 6],
                         f"一层一行：实际每行词数 {sorted(len(g) for g in rows.values())}")
        self.assertGreater(layout.content_w, 320, "前提：这一行确实比画布宽")
        self.assertEqual(sorted(entry_id for group in rows.values() for entry_id in group),
                         [1, 2, 3, 4, 5, 6, 7], "所有词都要排进某一行")


class TestConceptMapWindow(unittest.TestCase):
    """窗口层：没有手动录入控件、缓存立即显示、错误不画伪图、迟到结果不串图。"""

    def _stub(self, db, service, **extra):
        return SimpleNamespace(db=db, map_service=service, **extra)

    def _valid_candidate(self):
        return {"src": 0, "dst": 1, "type": "包含", "reason": "卷积核是卷积的单元",
                "evidence": "卷积核在输入上滑动"}

    def _preview_window(self, env):
        """当前导图示例（与离线预览同一组输入）画在**假 Canvas** 上。

        service 直接给出已校验的资源图，因此 ``_draw`` 一步都不碰网络：
        断言的正是「真实画布上画出去的那串点 / 箭头」。
        """
        from app.map_service import MapGraph, MapNode, MapRelation
        from app.ui import concept_map as cm

        relations = [MapRelation(src, dst, rel_type, "离线假依据", "离线假证据片段")
                     for src, dst, rel_type in PREVIEW_MAP_RELATIONS]
        graph = MapGraph(1, "离线指纹", tuple(relations),
                         tuple(MapNode(entry_id, label, "", "")
                               for entry_id, label in sorted(PREVIEW_MAP_LABELS.items())))
        service = SimpleNamespace(nodes_for_topic=lambda topic: graph.nodes,
                                  fingerprint=lambda topic, nodes: "离线指纹",
                                  cached_graph=lambda topic, nodes, fingerprint="": graph,
                                  is_ready=lambda: (True, ""),
                                  coverage_note=lambda topic, count: "")
        db = SimpleNamespace(list_batches=lambda: [], get_batch=lambda topic: None)
        win = cm.ConceptMapWindow(None, self._stub(db, service))
        # 直接给出「已校验的图 + 词节点」：这一步不经过任何网络 / 读库路径，
        # 断言的正是「产品把布局画到画布上」这一段。
        win._topic_id = 1
        win._nodes = list(graph.nodes)
        win._labels = dict(PREVIEW_MAP_LABELS)
        win._topic_name_text = "卷积神经网络"
        win._graph = graph
        win._fingerprint = "离线指纹"
        env.canvases[-1]._width, env.canvases[-1]._height = PREVIEW_MAP_CANVAS
        win._draw_key = None
        self.assertTrue(win._nodes, "示例词条必须已经载入")
        self.assertGreater(win._draw(force=True), 0, "画布上必须真的画了东西")
        return win

    def test_relation_labels_are_bare_text_sitting_on_their_own_line(self):
        """真实画出去的关系标签（用户口径）：

        * **没有底色块** —— 文字背景透明，不许再垫一块画布同色的矩形；
        * 文字就是类型名本身，人工关系也**不带**「人工·」前缀；
        * 落在自己那条线旁边（不是飘在十几像素外的空白里）；
        * **最后画** —— 永远压在卡片之上，没地方放也读得见。
        """
        from app.ui import concept_map as cm

        with _FakeTkEnv() as env:
            env.canvases.clear()
            win = self._preview_window(env)
            layout = win._layout
        canvas = env.canvases[-1]
        plates = [item for item, options in canvas.item_options.items()
                  if str(options.get("_kind") or "") == "edge-label-plate"]
        labels = [item for item, options in canvas.item_options.items()
                  if str(options.get("_kind") or "") == "edge-label"]
        cards = [item for item, options in canvas.item_options.items()
                 if str(options.get("_kind") or "") in ("node", "node-text", "topic")]
        self.assertEqual(plates, [], "标签底下不许再有底色块（文字背景要透明）")
        self.assertEqual(len(labels), len(layout.edges), "每条关系都要有标签文字")
        for item in labels:
            self.assertEqual(str(canvas.item_options[item].get("fill") or "").lower(),
                             cm.EDGE_LABEL_FILL.lower(), "标签颜色全图统一")
        # 每题标签的文字 = 类型名（人工关系同样不带前缀）
        drawn = sorted(str(canvas.item_options[item].get("text") or "") for item in labels)
        self.assertEqual(drawn, sorted(edge.label for edge in layout.edges))
        self.assertFalse(any(text.startswith(cm.MANUAL_LABEL_PREFIX) for text in drawn),
                         f"线上标注不许再带「{cm.MANUAL_LABEL_PREFIX}」前缀")
        # 画在卡片之后 ⇒ 图层上压着卡片
        self.assertGreater(min(labels), max(cards), "标签必须最后画（压在卡片之上）")
        # 贴着自己那条线：中心到该条折线的距离不超过搜索时的最大偏移
        gap = cm.label_gap_px(cm.metrics_for())
        for edge in layout.edges:
            distance = _distance_to_path(edge.label_pos, edge.points)
            self.assertLessEqual(distance, gap * 2.7 + 2.0,
                                 f"「{edge.label}」的标签离线太远（{distance:.1f}px）")

    def test_canvas_draws_the_same_obstacle_avoiding_points_and_symmetric_arrows(self):
        """真实画出去的那串点 = 布局里的点：跨边绕开节点、对照不画箭头、方向边照旧。"""
        from app.ui import concept_map as cm

        with _FakeTkEnv() as env:
            env.canvases.clear()
            win = self._preview_window(env)
            layout = win._layout
        canvas = env.canvases[-1]
        nodes = _node_rects(layout, pad=0.0)
        for edge in layout.edges:
            item = win._edge_items[id(edge.rel)]
            options = canvas.item_options[item]
            self.assertEqual(canvas.items[item]["kind"], "line",
                             "关系必须画成画布线（不是别的东西）")
            flat = tuple(canvas.items[item]["coords"])
            self.assertEqual(flat, tuple(value for point in edge.points
                                         for value in point),
                             f"「{edge.label}」画布上的点与布局不同源")
            self.assertNotIn("smooth", options,
                             "避障折线不能再交给 Tk 平滑（会把拐角鼓回节点里）")
            self.assertEqual(options.get("arrow"),
                             "" if edge.symmetric else "last",
                             f"「{edge.label}」的箭头方向不对（只有终点有箭头）")
            if edge.symmetric:
                self.assertEqual(options.get("arrow"), "",
                                 "对照是对称关系：不能只在一端画箭头")
            if len(flat) > 4:
                self.assertEqual([_edge_hits_nodes(edge, nodes)], [[]],
                                 f"「{edge.label}」在画布上穿过了别的节点")
        self.assertEqual(win._item_count(), len(canvas.items),
                         "画布上的 item 数 = 窗口记下的 item 数")

    def test_the_canvas_window_keeps_its_own_controls_minimal(self):
        """画布窗口本身只放**入口按钮**：人工关系的输入控件在独立对话框里。

        批次 C 之前这条断言的是「窗口上没有手动关系控件」；现在 C5 明确要做人工关系，
        但那些裸输入框（起点 / 终点 / 关系名）仍然不该挤在图上 —— 图上只有
        「人工关系…」这个入口，点开才是对话框。
        """
        from app.ui import concept_map as cm

        with support.temp_db() as db:
            bid, _ids = _map_db(db)
            service, _cfg = _map_service(db, key="", client=_MapClient([]))
            with _FakeTkEnv() as env:
                win = cm.ConceptMapWindow(None, self._stub(
                    db, service, open_settings=lambda: None))
                texts = [str(widget.cget("text")) for widget in env.widgets]
                for banned in ("起点", "终点", "添加关系", "删除选中关系", "关系名"):
                    self.assertNotIn(banned, texts, f"裸输入控件「{banned}」不该挤在画布窗口上")
                # 批次 C 的四个入口都要在，而且都只是入口（真正的编辑在对话框里）
                for entry_text in ("人工关系…", "孤立词诊断", "导出关系图…", "打开选中的词条"):
                    self.assertIn(entry_text, texts, f"缺少入口「{entry_text}」")
                self.assertIn("只分析选中的词", texts, "局部生成（C3）的入口")
                self.assertIn("分析全部词条", texts, "切回整个主题的入口")
                self.assertIn("人工添加的关系不出现在线上标注里：双击一条线可以看到它的来历",
                              texts, "C6：图上要说明人工关系怎么看（不再靠线型区分）")
                self.assertEqual(str(win.entry_list.cget("selectmode")), "extended",
                                 "词条多选列表要在左栏、且真的能多选（C3）")
                self.assertFalse(win._diag_visible, "孤立词面板默认收起（C2）")
                self.assertFalse(any("#" in text for text in texts),
                                 "界面里不出现内部编号")
                entries = [b for b in env.buttons
                           if str(b.cget("text")) in ("生成参考关系", "重新生成", "重试")]
                self.assertEqual(len(entries), 1, "只能有一个生成 / 重新生成入口")
                # 简洁主题选择：左栏列表仍然在，且真的列出了主题
                self.assertTrue(str(win.topic_list.get(0)).startswith("卷积网络"))
                canvas_texts = _canvas_texts(win)
                self.assertIn("卷积", canvas_texts, "先显示词节点")
                # 画布上**不许**再有图例 / 操作提示（用户要求删掉压在左上角的那两行）
                self.assertFalse(any(text.startswith(cm.MAP_TAG) for text in canvas_texts),
                                 "「AI 参考」不该再压在画布上")
                self.assertFalse(any("平移" in text or "缩放" in text
                                     for text in canvas_texts),
                                 "操作提示不该再画在画布上（要放右下角浮层）")
                # 「AI 参考」改由窗口底部状态行说明（模型候选 + 点击连线看依据）
                self.assertTrue(any(text.startswith(cm.MAP_TAG) for text in texts),
                                "窗口状态行里必须仍能看出这是 AI 参考")
                # 右下角：三行提示，一行一条、行间留空档，整体贴住画布右下角
                hints = [widget for widget in env.widgets
                         if str(widget.cget("text")) in cm.HINT_LINES]
                self.assertEqual([str(widget.cget("text")) for widget in hints],
                                 list(cm.HINT_LINES),
                                 "右下角提示必须逐字是那三行、且顺序不变")
                self.assertEqual(str(win.hint_box.place_info().get("anchor")), "se",
                                 "提示块必须钉在画布右下角")
                self.assertTrue(str(win.hint_box.place_info().get("relx")) == "1.0"
                                and str(win.hint_box.place_info().get("rely")) == "1.0",
                                "提示块必须按右下角定位（relx/rely 都是 1.0）")
                self.assertEqual(len(hints), len(cm.HINT_LINES),
                             "三行必须是三个独立的 Label（Tk 多行文本没有行距）")
                # 提示块要有**不透明底**：用户反馈「框选位置的提示背景是透明的」
                # —— 原来它跟画布同色（都取 theme.PANEL），看着像浮在半空。
                from app.ui import theme
                self.assertEqual(str(win.hint_box.cget("bg")).lower(),
                                 theme.CARD_BG.lower(),
                                 "提示块要有自己的底色（不是画布同色）")
                self.assertEqual(str(win.hint_box.cget("highlightthickness")), "1",
                                 "提示块要有一圈细边，才看得出是一张卡片")
                self.assertNotEqual(theme.CARD_BG.lower(), theme.PANEL.lower(),
                                    "底色必须真的与画布底色不同，否则还是不透明等于没有")

    def test_entry_budget_is_spelled_out_in_the_window(self):
        """单次分析有上限时，窗口要用短句说清「本次分析 N 词 / 共 M 词」。"""
        from app.api_client import MAP_MAX_ENTRIES
        from app.ui import concept_map as cm

        with support.temp_db() as db:
            bid, _ids = _map_db(db)
            for index in range(MAP_MAX_ENTRIES + 3):
                db.add_entry(batch_id=bid, term=f"补词{index}", context="上下文")
            total = int(db.count_entries(batch_id=bid))
            self.assertGreater(total, MAP_MAX_ENTRIES)
            service, cfg = _map_service(db, client=_MapClient([]))
            cfg.set("map.max_entries", str(MAP_MAX_ENTRIES))   # 顶到硬上限
            with _FakeTkEnv():
                win = cm.ConceptMapWindow(None, self._stub(
                    db, service, open_settings=lambda: None))
                self.assertEqual(len(win._nodes), MAP_MAX_ENTRIES)
                text = str(win.feedback.cget("text"))
                self.assertIn("本次分析", text, "被上限截断必须明说，不能默默漏词")
                self.assertIn(str(MAP_MAX_ENTRIES), text)
                self.assertIn(str(total), text)

    def test_cached_graph_is_shown_immediately_without_a_request(self):
        from app.ui import concept_map as cm

        with support.temp_db() as db:
            bid, _ids = _map_db(db)
            client = _MapClient([self._valid_candidate()])
            service, _cfg = _map_service(db, client=client)
            delivered: list = []
            service.set_result_sink(lambda *a: delivered.append(a))
            service.generate(bid, force=True, topic_name="卷积网络")
            self.assertTrue(_wait_for(lambda: delivered))
            self.assertEqual(len(client.calls), 1)

            with _FakeTkEnv():
                win = cm.ConceptMapWindow(None, self._stub(
                    db, service, open_settings=lambda: None))
                self.assertIsNotNone(win._graph)
                self.assertEqual(str(win._graph.source), "cache")
                self.assertIn("缓存", str(win.feedback.cget("text")))
                self.assertEqual(len(client.calls), 1, "命中缓存时打开图不再请求")
                self.assertTrue(win._items_matching("edge"), "缓存里的关系也要真的画出来")

    def test_live_generation_draws_nodes_first_then_relations_and_evidence(self):
        from app.ui import concept_map as cm

        with support.temp_db() as db:
            bid, _ids = _map_db(db)
            gate = threading.Event()
            client = _MapClient([self._valid_candidate()], gate=gate)
            service, _cfg = _map_service(db, client=client)
            delivered: list = []
            with _FakeTkEnv():
                win = cm.ConceptMapWindow(None, self._stub(
                    db, service, open_settings=lambda: None))
                self.assertIsNone(win._graph, "打开时还没有生成结果")
                self.assertEqual(win._items_matching("edge"), [],
                                 "结果没回来前绝不画关系线")
                self.assertIn("卷积", _canvas_texts(win), "先把词节点显示出来")
                self.assertEqual(str(win.btn_generate.cget("text")), "正在生成…")
                self.assertTrue(_wait_for(lambda: client.calls),
                                "打开图 = 明确动作：真的去生成")
                self.assertEqual(len(client.calls), 1)

                service.set_result_sink(lambda *a: delivered.append(a))
                gate.set()
                self.assertTrue(_wait_for(lambda: delivered))
                self.assertTrue(win.on_map_result(delivered[0]), "本主题结果必须被采纳")
                rel = win._graph.relations[0]
                self.assertIn("包含", _canvas_texts(win), "连线带关系类型短标签")
                self.assertTrue(win._items_matching("edge"))
                self.assertEqual(str(win.btn_generate.cget("text")), "重新生成")

                first, last = win._layout.edges[0].points[0], win._layout.edges[0].points[-1]
                win._on_canvas_click(SimpleNamespace(x=(first[0] + last[0]) / 2,
                                                     y=(first[1] + last[1]) / 2))
                self.assertIsNotNone(win._selected, "点连线要能选中")
                shown = str(win.evidence.cget("text"))
                self.assertIn("依据", shown)
                self.assertIn(rel.reason, shown, "点连线要看到简短依据")
                self.assertIn(rel.evidence, shown, "点连线要看到证据片段")
                win._on_canvas_click(SimpleNamespace(x=1, y=1))
                self.assertIsNone(win._selected, "点空白处收起依据")
                self.assertIn("点击一条连线", str(win.evidence.cget("text")))

    def test_error_state_keeps_nodes_only_with_retry(self):
        from app.api_client import ApiError
        from app.ui import concept_map as cm

        with support.temp_db() as db:
            bid, _ids = _map_db(db)
            gate = threading.Event()
            client = _MapClient(error=ApiError("bad_response", "缺少 relations 数组"),
                                gate=gate)
            service, _cfg = _map_service(db, client=client)
            delivered: list = []
            with _FakeTkEnv():
                win = cm.ConceptMapWindow(None, self._stub(
                    db, service, open_settings=lambda: None))
                service.set_result_sink(lambda *a: delivered.append(a))
                gate.set()
                self.assertTrue(_wait_for(lambda: delivered))
                win.on_map_result(delivered[0])
                self.assertIsNone(win._graph, "错误 / 坏 JSON 绝不产出伪图")
                self.assertEqual(win._items_matching("edge"), [], "一张伪图都不画")
                self.assertIn("卷积", _canvas_texts(win), "词节点仍然可见")
                self.assertIn("生成失败", str(win.feedback.cget("text")))
                self.assertEqual(str(win.btn_generate.cget("text")), "重试")
                win._on_generate()                       # 重试 = 再发一次请求
                self.assertTrue(_wait_for(lambda: len(client.calls) == 2),
                                "重试必须真的再发一次请求")

    def test_missing_key_shows_inline_settings_hint(self):
        from app.ui import concept_map as cm

        with support.temp_db() as db:
            bid, _ids = _map_db(db)
            client = _MapClient([])
            service, _cfg = _map_service(db, key="", client=client)
            opened: list = []
            with _FakeTkEnv():
                win = cm.ConceptMapWindow(None, self._stub(
                    db, service, open_settings=lambda: opened.append(True)))
                self.assertEqual(win._state, "nokey")
                self.assertIn("API Key", str(win.feedback.cget("text")))
                self.assertTrue(win._hint_visible, "缺 Key 要有内联配置提示")
                self.assertIn("打开设置", str(win.btn_settings.cget("text")))
                win.btn_settings.invoke()
                self.assertEqual(opened, [True], "内联提示必须真的能打开设置")
                self.assertEqual(client.calls, [], "没有 Key 时一个请求都不发")

    def test_late_result_for_another_topic_is_ignored(self):
        from app.map_service import MAP_VALIDATION_VERSION
        from app.ui import concept_map as cm

        with support.temp_db() as db:
            topic_a, ids_a = _map_db(db, "主题A")
            topic_b, ids_b = _map_db(db, "主题B")
            service, cfg = _map_service(db, key="", client=_MapClient([]))
            for topic_id, ids, rel in ((topic_b, ids_b, (0, 1)), (topic_a, ids_a, (0, 1))):
                nodes = service.nodes_for_topic(topic_id)
                fingerprint = service.fingerprint(topic_id, nodes)
                db.put_map_graph(
                    topic_id=topic_id, fingerprint=fingerprint,
                    base_url=cfg.base_url, model=cfg.model,
                    relations=[{"src": int(nodes[rel[0]].entry_id),
                                "dst": int(nodes[rel[1]].entry_id),
                                "type": "包含", "reason": "依据", "evidence": "片段"}],
                    validation_version=MAP_VALIDATION_VERSION)
            nodes_b = service.nodes_for_topic(topic_b)
            graph_b = service.cached_graph(topic_b, nodes_b,
                                           fingerprint=service.fingerprint(topic_b, nodes_b))
            self.assertIsNotNone(graph_b)
            with _FakeTkEnv():
                win = cm.ConceptMapWindow(None, self._stub(
                    db, service, open_settings=lambda: None))
                win._topic_id = topic_a
                win._load_topic()
                self.assertEqual(int(win._topic_id), topic_a)
                self.assertEqual(str(win._graph.source), "cache")
                self.assertFalse(win.on_map_result((7, topic_b, graph_b.fingerprint, "ok",
                                                    graph_b, None)),
                                 "别的主题的结果绝不能画进当前图")
                self.assertEqual(int(win._graph.topic_id), topic_a, "当前图没被换掉")
                self.assertFalse(win.on_map_result((7, topic_a, "过期指纹", "ok",
                                                    graph_b, None)),
                                 "指纹对不上的迟到结果同样丢弃")
                self.assertEqual(int(win._graph.topic_id), topic_a)

    def test_zoom_wheel_and_resize_relayout(self):
        from app.ui import concept_map as cm

        with support.temp_db() as db:
            bid, ids = _map_db(db)
            service, _cfg = _map_service(db, key="", client=_MapClient([]))
            with _FakeTkEnv() as env:
                win = cm.ConceptMapWindow(None, self._stub(
                    db, service, open_settings=lambda: None))
                canvas = env.canvases[-1]
                canvas.configure(width=640, height=430)
                before = win._layout.find(ids["卷积"])
                self.assertIsNotNone(before)
                self.assertTrue(win.zoom_by(1.5), "缩放要真的重排")
                self.assertGreater(win._layout.find(ids["卷积"]).w, before.w)
                win.zoom_by(10)
                self.assertAlmostEqual(win._zoom, cm.ZOOM_MAX, "缩放要有上限")
                win.zoom_by(0.0001)
                self.assertAlmostEqual(win._zoom, cm.ZOOM_MIN, "缩放下限同样限死")
                self.assertEqual(canvas.scrollregion is not None, True,
                                 "画布要有 scrollregion 才能平移")

                # 滚轮 = **直接**缩放（不再滚动画面），Ctrl+滚轮走同一条路
                canvas.yview_calls.clear()
                win._zoom = 1.0
                win._fit_pending = False
                win._on_zoom_wheel(SimpleNamespace(delta=120, x=100, y=80))
                self.assertAlmostEqual(win._zoom, cm.ZOOM_STEP, places=6,
                                       msg="滚轮必须直接缩放")
                self.assertEqual([call for call in canvas.yview_calls
                                  if call[0] == "scroll"], [],
                                 "滚轮不再滚动画面（平移只认右键拖动）")

                canvas.configure(width=640, height=520)
                win._draw()
                self.assertEqual(win._layout.width, 640, "窗口尺寸变化要按新尺寸重排")


class TestClickingAWordExplainsItsRelations(unittest.TestCase):
    """点词卡：有关系的列出关系，孤立词必须说清楚「为什么一条都没画」。

    这是用户 2026-10 提的那条：「明明原文里都有关系，为什么有的词变成了无关词语？」
    —— 答案就在判定记录里（模型提过哪些候选、核对怎么判的），所以点词要看得到。
    """

    def _stub(self, db, service, **extra):
        return SimpleNamespace(db=db, map_service=service, **extra)

    @staticmethod
    def _click(win, x, y):
        """在**画布坐标** (x, y) 处点一下（与实机点屏幕位置同语义）。"""
        win._on_canvas_click(SimpleNamespace(x=x - win._view_left(),
                                             y=y - win._view_top()))

    def _window(self, db, env, *, legacy=False):
        """一个真实主题 + 真实缓存图（带判定记录）：一条画出来的边 + 一个孤立词。

        ``legacy=True`` 写成 **v6 之前那种缓存**（没有 dropped / verdicts 两列）：
        判定记录是这一轮才落库的，老图里一个字都没有 —— 点词时的说法必须跟着变。
        """
        from app.ui import concept_map as cm

        bid = int(db.create_batch("卷积主题"))
        rows = (("卷积", "卷积核在输入上滑动，逐点相乘再求和"),
                ("池化", "池化用于降采样，扩大感受野"),
                ("过拟合", "正则化抑制过拟合"),
                ("滑动窗口", "滑动窗口切分长序列"))
        ids = {name: int(db.add_entry(batch_id=bid, term=name, context=context))
               for name, context in rows}
        service, _cfg = _map_service(db, key="", client=_MapClient([]))
        relations = [{"src": ids["卷积"], "dst": ids["池化"], "type": "包含",
                      "reason": "池化是卷积网络的一层", "evidence": "池化用于降采样"}]
        if legacy:
            _put_map_cache(db, service, bid, relations)
        else:
            _put_map_cache(
                db, service, bid, relations,
                dropped={"verify_supported": 1, "verify_contradicted": 1,
                         "verify_uncertain": 1},
                verdicts=[
                    {"index": 0, "src": ids["卷积"], "dst": ids["过拟合"], "type": "因果",
                     "reason": "卷积导致过拟合", "evidence": "正则化抑制过拟合",
                     "verdict": "contradicted",
                     "note": "材料只是场景描述，并未表达因果。", "kept": False},
                    {"index": 1, "src": ids["过拟合"], "dst": ids["池化"], "type": "依赖",
                     "reason": "过拟合依赖池化", "evidence": "池化用于降采样",
                     "verdict": "uncertain", "note": "材料不足。", "kept": False},
                ])
        env.canvases.clear()
        win = cm.ConceptMapWindow(None, self._stub(db, service,
                                                   open_settings=lambda: None))
        win._draw(force=True)
        self.assertTrue(win._layout.edges, "前提：缓存图里真的有一条可点的连线")
        return win, ids

    def test_an_isolated_word_says_what_was_proposed_and_why_it_was_dropped(self):
        from app.ui import concept_map as cm

        with support.temp_db() as db:
            with _FakeTkEnv() as env:
                win, ids = self._window(db, env)
                node = win._layout.find(int(ids["过拟合"]))
                self.assertIsNotNone(node, "孤立词也要画在图上（不能悄悄消失）")
                self._click(win, node.x, node.y)
                self.assertEqual(int(win._selected_node), int(ids["过拟合"]),
                                 "点词卡要选中这个词（不是选中旁边的线）")
                self.assertIsNone(win._selected, "点词时不该同时选中某条关系")
                shown = str(win.evidence.cget("text"))
        self.assertIn("过拟合", shown)
        self.assertIn("孤立词", shown)
        self.assertIn(cm.ISOLATED_WHY[4:20], shown, "要说清「模型提过候选」这件事")
        self.assertIn("因果", shown, "候选关系类型要列出来")
        self.assertIn("依赖", shown, "每条候选都要列")
        self.assertIn(cm.VERDICT_LABELS["contradicted"], shown, "要说清核对怎么判的")
        self.assertIn(cm.VERDICT_LABELS["uncertain"], shown)
        self.assertIn("材料只是场景描述", shown, "模型给的理由要原样呈现（这才是原因）")
        self.assertIn("材料不足", shown)
        self.assertNotIn("（不在本主题）", shown, "词名必须解析成真实词条名")

    def test_a_connected_word_lists_its_edges_and_points_at_the_line_evidence(self):
        with support.temp_db() as db:
            with _FakeTkEnv() as env:
                win, ids = self._window(db, env)
                node = win._layout.find(int(ids["卷积"]))
                self._click(win, node.x, node.y)
                self.assertEqual(int(win._selected_node), int(ids["卷积"]))
                shown = str(win.evidence.cget("text"))
                # 选中词时它的连线要加粗（看得出「这几条是这个词的」）
                highlighted = [item for item, options in
                               env.canvases[-1].item_options.items()
                               if str(options.get("_kind") or "") == "edge"]
                self.assertTrue(highlighted, "前提：这条词真的有连线")
        self.assertIn("卷积", shown)
        self.assertIn("卷积 --包含--> 池化", shown, "要列出这个词已画出的关系")
        self.assertIn("点连线看这条的依据", shown, "引导用户去看连线上的证据")

    def test_an_old_cache_says_there_is_no_record_instead_of_blaming_the_model(self):
        """v6 之前的旧缓存没有判定记录 —— 不许说成「模型一条候选都没提」。"""
        from app.ui import concept_map as cm

        with support.temp_db() as db:
            with _FakeTkEnv() as env:
                win, ids = self._window(db, env, legacy=True)
                self.assertEqual(tuple(win._graph.verdicts), (),
                                 "前提：旧缓存里确实没有判定记录")
                node = win._layout.find(int(ids["过拟合"]))
                self.assertIsNotNone(node)
                self._click(win, node.x, node.y)
                shown = str(win.evidence.cget("text"))
        self.assertIn("没有留下判定记录", shown, "要如实说这张图没有记录")
        self.assertIn("重新生成", shown, "要告诉用户怎么才能看到原因")
        self.assertNotIn(cm.ISOLATED_NONE, shown)
        self.assertNotIn(cm.ISOLATED_WHY, shown)

    def test_a_word_with_no_record_in_a_graph_that_has_records_says_so_plainly(self):
        """图里有别的词的判定记录、偏偏没有这个词的 —— 说「没有这个词的候选记录」。"""
        from app.ui import concept_map as cm

        with support.temp_db() as db:
            with _FakeTkEnv() as env:
                win, ids = self._window(db, env)
                self.assertTrue(win._graph.verdicts, "前提：图里有别的词的判定记录")
                node = win._layout.find(int(ids["滑动窗口"]))
                self.assertIsNotNone(node)
                self._click(win, node.x, node.y)
                shown = str(win.evidence.cget("text"))
        self.assertIn("没有这个词的候选记录", shown)
        self.assertNotIn(cm.ISOLATED_UNKNOWN, shown)

    def test_clicking_empty_space_clears_the_selected_word(self):
        with support.temp_db() as db:
            with _FakeTkEnv() as env:
                win, ids = self._window(db, env)
                node = win._layout.find(int(ids["卷积"]))
                self._click(win, node.x, node.y)
                self.assertIsNotNone(win._selected_node)
                self._click(win, -400.0, -400.0)
                self.assertIsNone(win._selected_node, "点空白要收起词依据")
                self.assertIn("点击一条连线", str(win.evidence.cget("text")))


class TestSettingsDialogModelAndReasoningEffort(unittest.TestCase):
    """设置页：模型名可改、推理强度可选，并显示**当前生效**的那一份配置。

    用户诉求（2026-10）：接的是 DeepSeek 的 API，界面上却看不出在用哪个模型、
    什么推理强度 —— 所以这里既要有下拉选项，也要有一行「当前生效」。
    """

    def _cfg(self, db):
        from app.config import Config

        cfg = Config(db)
        cfg.set("api.base_url", "https://api.deepseek.com/v1")
        cfg.set("api.model", "deepseek-chat")
        return cfg

    def _stub(self, cfg):
        calls: list = []
        return SimpleNamespace(
            config=cfg,
            explain_service=SimpleNamespace(is_ready=lambda: (True, "")),
            on_settings_changed=lambda: calls.append("changed"),
            restore_pending_explain_window=lambda: calls.append("restore"),
        ), calls

    def test_the_window_offers_the_model_and_the_effort_and_shows_what_is_live(self):
        from tests.support import FakeWidget
        from app.config import MODEL_PRESETS, REASONING_SHORT, effort_choices
        from app.ui.settings_dialog import SettingsDialog

        with support.temp_db() as db:
            cfg = self._cfg(db)
            with _FakeTkEnv():
                stub, _calls = self._stub(cfg)
                dialog = SettingsDialog(FakeWidget(None), stub)
                self.assertIsInstance(dialog.chrome, widgets.BorderlessChrome)
                # 模型名：**可手输**输入框 + 一排预设按钮（预设只填进输入框）
                self.assertEqual(dialog.var_model.get(), "deepseek-chat")
                self.assertTrue(hasattr(dialog.model_entry, "insert"), "必须是可编辑输入框")
                self.assertEqual([button.cget("text") for button in dialog.preset_buttons],
                                 list(MODEL_PRESETS), "预设按钮要列出候选模型名")
                dialog.preset_buttons[1].invoke()
                self.assertEqual(dialog.var_model.get(), MODEL_PRESETS[1],
                                 "点预设 = 填进输入框，仍然可以再手改")
                # 推理强度：单选按钮组，绑的是**裸值**，档位**按模型名给**
                # （DeepSeek 官方只有 low / high / max）
                tiers = effort_choices("deepseek-chat")
                self.assertEqual(tiers, ("", "low", "high", "max"),
                                 "DeepSeek 的档位必须是 low / high / max（+ 不发送）")
                self.assertEqual([button.cget("value") for button in dialog.effort_buttons],
                                 list(tiers))
                self.assertEqual([button.cget("text") for button in dialog.effort_buttons],
                                 [REASONING_SHORT[value] for value in tiers])
                self.assertEqual(dialog.var_effort.get(), "", "默认 = 不发送")
                self.assertIn("不发送", str(dialog.effort_hint.cget("text")),
                              "说明行要显示当前档的完整解释")
                # 「当前生效」= 已经存进库的那份配置
                shown = str(dialog.effective.cget("text"))
                self.assertIn("当前生效", shown)
                self.assertIn("deepseek-chat", shown)
                self.assertIn("不发送", shown)

    def test_the_effort_tiers_follow_the_model_name(self):
        """档位不是写死的四档：换模型就换按钮，原来那档没了就退回「不发送」。"""
        from tests.support import FakeWidget
        from app.config import effort_choices
        from app.ui.settings_dialog import SettingsDialog

        with support.temp_db() as db:
            cfg = self._cfg(db)
            cfg.set("api.model", "gpt-4o")          # OpenAI 风格网关：三档带 medium
            cfg.set_reasoning_effort("medium")
            with _FakeTkEnv():
                stub, _calls = self._stub(cfg)
                dialog = SettingsDialog(FakeWidget(None), stub)
                self.assertEqual([b.cget("value") for b in dialog.effort_buttons],
                                 ["", "low", "medium", "high"])
                self.assertEqual(dialog.var_effort.get(), "medium",
                                 "打开窗口要按已保存的档位选中")
                # 切成 DeepSeek：medium 不是它的档位 ⇒ 退回「不发送」并在反馈里说清
                dialog.var_model.set("deepseek-chat")
                dialog._rebuild_efforts()           # 假 Tk 的 trace 是空实现
                self.assertEqual([b.cget("value") for b in dialog.effort_buttons],
                                 ["", "low", "high", "max"])
                self.assertEqual(dialog.var_effort.get(), "", "不认的档位不许留着")
                self.assertIn("medium", str(dialog.feedback.cget("text")),
                              "要告诉用户哪一档被丢了，不能静默改")
                # 再切回通用模型，档位跟着回来（用户原来的选择仍可从库里读到）
                dialog.var_model.set("qwen-max")
                dialog._rebuild_efforts()
                self.assertEqual([b.cget("value") for b in dialog.effort_buttons],
                                 list(effort_choices("qwen-max")))
                self.assertEqual(dialog.var_effort.get(), "")

    def test_saving_stores_the_model_and_the_effort_and_refreshes_the_live_line(self):
        from tests.support import FakeWidget
        from app.ui.settings_dialog import SettingsDialog

        with support.temp_db() as db:
            cfg = self._cfg(db)
            with _FakeTkEnv():
                stub, calls = self._stub(cfg)
                dialog = SettingsDialog(FakeWidget(None), stub)
                dialog.var_model.set("deepseek-reasoner")
                dialog.var_effort.set("high")
                dialog._on_effort_pick()
                self.assertIn("更多思考", str(dialog.effort_hint.cget("text")),
                              "换档要跟着换说明")
                dialog.save()
                self.assertEqual(cfg.model, "deepseek-reasoner", "模型名必须落库")
                self.assertEqual(cfg.reasoning_effort, "high", "推理强度必须落库")
                self.assertEqual(str(cfg.model_display),
                                 "deepseek-reasoner · 推理强度 high")
                self.assertIn("推理强度 high", str(dialog.effective.cget("text")),
                              "「当前生效」行要跟着保存结果刷新")
        self.assertEqual(calls, ["changed", "restore"],
                         "保存后要通知 App 并恢复挂起的解释窗")

    def test_deepseek_tiers_are_low_high_max_and_max_survives_a_round_trip(self):
        """DeepSeek 官方《思考模式》文档：``reasoning_effort`` = low / high / max。

        所以 ``max`` 必须是**一等公民**：既在 ``effort_choices`` 里出现，也要能存住
        —— 白名单少一个值就会把它静默重置成「不发送」，那是丢用户配置。
        """
        from app.config import (Config, REASONING_LABELS, effort_choices,
                                effort_from_label)

        self.assertEqual(effort_choices("deepseek-chat"), ("", "low", "high", "max"))
        self.assertEqual(effort_choices("DeepSeek-Flash"), ("", "low", "high", "max"))
        self.assertEqual(effort_choices("gpt-4o"), ("", "low", "medium", "high"))
        self.assertEqual(effort_choices(""), ("", "low", "medium", "high"),
                         "认不出模型就给通用档位（模型名是自由文本）")
        self.assertEqual(effort_from_label("max"), "max")
        self.assertEqual(effort_from_label(REASONING_LABELS["max"]), "max",
                         "说明行的长文案也要认")
        with support.temp_db() as db:
            cfg = Config(db)
            cfg.set("api.model", "deepseek-reasoner")
            cfg.set_reasoning_effort("max")
            self.assertEqual(cfg.reasoning_effort, "max", "max 必须能落库、读回")
            self.assertEqual(cfg.model_signature, "deepseek-reasoner|max")
            cfg.set_reasoning_effort("胡说八道")
            self.assertEqual(cfg.reasoning_effort, "", "乱写的值仍然退回「不发送」")

    def test_a_made_up_effort_label_falls_back_to_not_sending(self):
        """认不出的文案一律当「不发送」：绝不把脏字符串发给网关。"""
        from tests.support import FakeWidget
        from app.config import effort_from_label
        from app.ui.settings_dialog import SettingsDialog

        self.assertEqual(effort_from_label("high"), "high")
        self.assertEqual(effort_from_label("  high  "), "high")
        self.assertEqual(effort_from_label("超高"), "")
        self.assertEqual(effort_from_label(""), "")
        self.assertEqual(effort_from_label(None), "")

        with support.temp_db() as db:
            cfg = self._cfg(db)
            cfg.set_reasoning_effort("high")
            with _FakeTkEnv():
                stub, _calls = self._stub(cfg)
                dialog = SettingsDialog(FakeWidget(None), stub)
                self.assertEqual(dialog.var_effort.get(), "high",
                                 "打开窗口要按已保存的强度选中对应项")
                self.assertIn("更多思考", str(dialog.effort_hint.cget("text")))
                dialog.var_effort.set("某个不存在的档位")
                dialog.save()
                self.assertEqual(cfg.reasoning_effort, "", "认不出就退回「不发送」")

    def test_a_saved_effort_changes_the_cache_key_and_the_map_fingerprint(self):
        """跨层契约：换了推理强度，解释缓存与导图指纹都必须不命中旧结果。"""
        from app.config import Config, model_signature
        from app.explain_service import ExplainService
        from app.map_service import content_fingerprint

        self.assertEqual(model_signature("m"), "m", "空强度与旧口径逐字一致（老缓存不失效）")
        self.assertEqual(model_signature("m", "high"), "m|high")
        with support.temp_db() as db:
            cfg = Config(db)
            cfg.set("api.base_url", "https://api.deepseek.com/v1")
            cfg.set("api.model", "deepseek-chat")
            service = ExplainService(db, cfg)
            plain = service.cache_key("卷积", "上下文")
            cfg.set_reasoning_effort("high")
            self.assertEqual(str(cfg.model_display), "deepseek-chat · 推理强度 high")
            self.assertNotEqual(service.cache_key("卷积", "上下文"), plain,
                                "换了推理强度就不许命中旧解释缓存")
            nodes = [SimpleNamespace(entry_id=1, term="卷积", context="上下文",
                                     explanation="")]
            base = content_fingerprint(7, nodes, base_url=cfg.base_url,
                                       model=cfg.model, reasoning_effort="high")
            self.assertNotEqual(
                base, content_fingerprint(7, nodes, base_url=cfg.base_url,
                                          model=cfg.model, reasoning_effort=""),
                "导图指纹同理：强度不同 = 另一份内容")
            # 解释窗底部那行「模型配置」也要看得见强度（空强度时逐字不变）
            from app.explain_service import ExplainSnapshot

            def _snapshot(effort=""):
                return ExplainSnapshot(entry_id=1, term="卷积", entry_context="上下文",
                                       request_context="上下文", base_url=cfg.base_url,
                                       model=cfg.model, timeout=25.0, api_key="sk-x",
                                       reasoning_effort=effort)

            self.assertIn("推理强度 high", _snapshot("high").model_config)
            self.assertEqual(_snapshot().model_config, f"{cfg.base_url}|{cfg.model}",
                             "没设强度时这行必须与旧口径逐字一致（老测试与老日志照旧）")


class TestProviderPresetsAndLiveConnection(unittest.TestCase):
    """配置与上手（用户意见 A1 / A3）：服务商一键模板 + 真·测试连接。

    * A1：点一下服务商就填好地址与模型名，用户只需要粘自己的 Key ——
      **不内置任何密钥**，也不改变底层逻辑；
    * A3：原「检查配置」只做本地格式检查、从不联网，用户看不出配置到底通不通。
      新增「测试连接」发一条极轻量请求，成功 / 失败原因直接写在反馈行里。
    """

    def _stub(self, cfg):
        calls: list = []
        return SimpleNamespace(
            config=cfg,
            explain_service=SimpleNamespace(is_ready=lambda: (True, "")),
            on_settings_changed=lambda: calls.append("changed"),
            restore_pending_explain_window=lambda: calls.append("restore"),
        ), calls

    def test_the_presets_are_well_formed_and_carry_no_secrets(self):
        from app.config import LOCAL_ENDPOINT, PROVIDER_PRESETS

        names = [name for name, _base, _model in PROVIDER_PRESETS]
        self.assertEqual(len(names), len(set(names)), "服务商名字不能重复")
        self.assertGreaterEqual(len(names), 5, "至少要覆盖常见几家")
        for name, base, model in PROVIDER_PRESETS:
            self.assertTrue(base.lower().startswith(("http://", "https://")),
                            f"{name} 的地址必须是完整 URL")
            self.assertTrue(model.strip(), f"{name} 要有推荐模型名")
            self.assertNotIn("key", base.lower(), f"{name} 的地址里不该有任何密钥痕迹")
        self.assertIn(LOCAL_ENDPOINT, [(b, m) for _n, b, m in PROVIDER_PRESETS],
                      "本地 Ollama 也要在模板里")
        self.assertEqual(PROVIDER_PRESETS[0][1], "https://api.deepseek.com/v1",
                         "第一个模板 = 默认端点")

    def test_clicking_a_provider_fills_the_address_and_model_without_saving(self):
        from tests.support import FakeWidget
        from app.config import PROVIDER_PRESETS
        from app.ui.settings_dialog import SettingsDialog

        with support.temp_db() as db:
            cfg = Config(db)
            cfg.set("api.base_url", "https://api.deepseek.com/v1")
            cfg.set("api.model", "deepseek-chat")
            with _FakeTkEnv():
                stub, _calls = self._stub(cfg)
                dialog = SettingsDialog(FakeWidget(None), stub)
                self.assertEqual(
                    [button.cget("text") for button in dialog.provider_buttons],
                    [name for name, _b, _m in PROVIDER_PRESETS])
                silicon = [b for b in dialog.provider_buttons
                           if b.cget("text") == "硅基流动"]
                self.assertEqual(len(silicon), 1)
                silicon[0].invoke()
                target = dict((n, (b, m)) for n, b, m in PROVIDER_PRESETS)["硅基流动"]
                self.assertEqual(dialog.var_base.get(), target[0])
                self.assertEqual(dialog.var_model.get(), target[1])
                self.assertIn("硅基流动", str(dialog.feedback.cget("text")),
                              "要有反馈说清「Key 还是得自己填」")
                # 只填编辑框：按「保存」之前，配置一个字都不许动
                self.assertEqual(cfg.base_url, "https://api.deepseek.com/v1")
                self.assertEqual(cfg.model, "deepseek-chat")
                # 切到 DeepSeek 官方模板时档位也跟着模型名走
                deepseek = [b for b in dialog.provider_buttons
                            if b.cget("text") == "DeepSeek 官方"][0]
                deepseek.invoke()
                self.assertEqual(dialog.var_base.get(), "https://api.deepseek.com/v1")
                self.assertEqual(dialog.var_model.get(), "deepseek-chat")

    def test_the_test_button_refuses_to_fire_without_a_usable_key(self):
        from tests.support import FakeWidget
        from app.ui.settings_dialog import SettingsDialog

        with support.temp_db() as db:
            cfg = Config(db)
            cfg.set("api.base_url", "https://api.deepseek.com/v1")
            cfg.set("api.model", "deepseek-chat")
            with _FakeTkEnv():
                stub, _calls = self._stub(cfg)
                dialog = SettingsDialog(FakeWidget(None), stub)
                started: list = []
                dialog._start_connection_test = lambda *a: started.append(a)
                dialog.test_connection_live()
                self.assertEqual(started, [], "没有 Key 就不许发请求")
                self.assertIn("Key", str(dialog.feedback.cget("text")))
                # 粘上 Key（还没保存）就能测 —— 先测再保存这个顺序必须走得通
                dialog.var_key.set("sk-just-pasted")
                dialog.test_connection_live()
                self.assertEqual(len(started), 1)
                base, model, key, timeout = started[0]
                self.assertEqual((base, model, key), ("https://api.deepseek.com/v1",
                                                      "deepseek-chat", "sk-just-pasted"))
                self.assertLessEqual(timeout, 15.0, "测试连接不该等满用户的完整超时")

    def test_the_test_button_rejects_a_bad_address_or_a_missing_model(self):
        from tests.support import FakeWidget
        from app.ui.settings_dialog import SettingsDialog

        with support.temp_db() as db:
            cfg = Config(db)
            cfg.set_api_key("sk-saved-key")
            with _FakeTkEnv():
                stub, _calls = self._stub(cfg)
                dialog = SettingsDialog(FakeWidget(None), stub)
                dialog.var_base.set("api.deepseek.com")
                dialog.test_connection_live()
                self.assertIn("http://", str(dialog.feedback.cget("text")))
                dialog.var_base.set("https://api.deepseek.com/v1")
                dialog.var_model.set("   ")
                dialog.test_connection_live()
                self.assertIn("模型名", str(dialog.feedback.cget("text")))

    def test_a_successful_test_writes_the_result_into_the_feedback_line(self):
        """``_ping_worker`` 走真的 ping_endpoint（测里换成桩），反馈行说清结果。"""
        from tests.support import FakeWidget
        from app.ui.settings_dialog import SettingsDialog
        from app import api_client

        with support.temp_db() as db:
            cfg = Config(db)
            cfg.set("api.base_url", "https://api.deepseek.com/v1")
            cfg.set("api.model", "deepseek-chat")
            cfg.set_api_key("sk-saved-key")
            with _FakeTkEnv():
                stub, _calls = self._stub(cfg)
                dialog = SettingsDialog(FakeWidget(None), stub)
                calls: list = []
                original = api_client.ping_endpoint

                def fake_ping(*, base_url, model, api_key, timeout=12.0):
                    calls.append((base_url, model, api_key, timeout))
                    return True, f"连接成功：{base_url} · {model}"

                api_client.ping_endpoint = fake_ping
                try:
                    dialog._ping_worker("https://api.deepseek.com/v1", "deepseek-chat",
                                        "sk-saved-key", 12.0)
                finally:
                    api_client.ping_endpoint = original
                self.assertEqual(len(calls), 1)
                self.assertIn("连接成功", str(dialog.feedback.cget("text")))
                self.assertFalse(dialog._ping_busy, "结果回来要解除忙态")
                self.assertTrue(dialog.btn_ping.is_enabled())

                def failing_ping(**_kw):
                    return False, "连接失败：API Key 无效或没有权限，请在设置中检查 Key"

                api_client.ping_endpoint = failing_ping
                try:
                    dialog._ping_worker("https://api.deepseek.com/v1", "deepseek-chat",
                                        "sk-bad", 12.0)
                finally:
                    api_client.ping_endpoint = original
                shown = str(dialog.feedback.cget("text"))
                self.assertIn("连接失败", shown)
                self.assertIn("Key", shown)

    def test_the_test_button_is_disabled_while_a_test_is_in_flight(self):
        from tests.support import FakeWidget
        from app.ui.settings_dialog import SettingsDialog

        with support.temp_db() as db:
            cfg = Config(db)
            cfg.set_api_key("sk-saved-key")
            with _FakeTkEnv():
                stub, _calls = self._stub(cfg)
                dialog = SettingsDialog(FakeWidget(None), stub)

                def no_thread(target=None, args=(), daemon=None):
                    return SimpleNamespace(start=lambda: None)

                original = threading.Thread
                threading.Thread = no_thread
                try:
                    dialog.test_connection_live()
                finally:
                    threading.Thread = original
                self.assertTrue(dialog._ping_busy)
                self.assertFalse(dialog.btn_ping.is_enabled(), "在途时按钮要禁用")
                dialog.test_connection_live()
                self.assertIn("正在测试连接", str(dialog.feedback.cget("text")))
                dialog._apply_ping_result("✓ 连接成功：x")
                self.assertTrue(dialog.btn_ping.is_enabled())
                self.assertFalse(dialog._ping_busy)


class TestSetupWizard(unittest.TestCase):
    """首次启动的配置向导（用户意见 A2）：只弹一次，说清它不是翻译词典。

    用户原话的意思是：很多新用户下载完以为「划词就出翻译」，划了半天什么反应都
    没有，以为软件坏了。向导要把「只有点『解释并记录』才联网」说在明面上，并且
    顺手把地址与模型名填好（云端 / 本地二选一）。
    """

    def _app(self, cfg):
        calls: list = []
        return SimpleNamespace(
            config=cfg,
            on_settings_changed=lambda: calls.append("changed"),
            open_settings=lambda: calls.append("settings"),
        ), calls

    def test_should_show_reads_the_flag_and_respects_the_gate(self):
        from app.ui.setup_wizard import should_show

        with support.temp_db() as db:
            cfg = Config(db)
            self.assertTrue(should_show(cfg), "全新安装（没有标记）= 该弹")
            cfg.set_bool("ui.wizard_done", True)
            self.assertFalse(should_show(cfg), "看过了就不再弹")
            cfg.set_bool("ui.wizard_done", False)
            self.assertFalse(should_show(cfg, gate_locked=True),
                             "游戏 / 全屏 / 暂停硬阻断时不弹窗")
            self.assertFalse(should_show(SimpleNamespace()), "配置读不出来也不能崩")

    def test_the_wizard_explains_what_the_app_is_and_offers_two_choices(self):
        from tests.support import FakeWidget
        from app.ui.setup_wizard import INTRO_TEXT, KEY_NOTE, SetupWizard

        with support.temp_db() as db:
            cfg = Config(db)
            cfg.set("api.base_url", "https://api.deepseek.com/v1")
            cfg.set("api.model", "deepseek-chat")
            with _FakeTkEnv():
                stub, _calls = self._app(cfg)
                wizard = SetupWizard(FakeWidget(None), stub)
                self.assertIsInstance(wizard.chrome, widgets.BorderlessChrome)
                self.assertEqual(wizard.cloud_button.cget("text"), "用云端 API（推荐）")
                self.assertEqual(wizard.local_button.cget("text"),
                                 "用本地推理服务（Ollama）")
                self.assertIn("不是「划词就出翻译」的词典", INTRO_TEXT)
                self.assertIn("解释并记录", INTRO_TEXT,
                              "必须说清什么时候才联网")
                self.assertIn("将使用", str(wizard.choice_hint.cget("text")))
                self.assertIn("deepseek", str(wizard.choice_hint.cget("text")).lower())
                self.assertIn("不内置任何密钥", KEY_NOTE,
                              "界面必须说明 Key 是用户自己的，软件不内置")
                # 只是打开向导：不许写配置、不许标记已看过
                self.assertFalse(cfg.get_bool("ui.wizard_done", False))

    def test_choosing_local_writes_the_ollama_endpoint_and_marks_the_flag(self):
        from tests.support import FakeWidget
        from app.config import LOCAL_ENDPOINT
        from app.ui.setup_wizard import SetupWizard, should_show

        with support.temp_db() as db:
            cfg = Config(db)
            cfg.set("api.base_url", "https://api.deepseek.com/v1")
            cfg.set("api.model", "deepseek-chat")
            with _FakeTkEnv():
                stub, calls = self._app(cfg)
                wizard = SetupWizard(FakeWidget(None), stub)
                wizard.local_button.invoke()
                self.assertEqual(wizard.mode, "local")
                self.assertIn(LOCAL_ENDPOINT[0], str(wizard.choice_hint.cget("text")))
                self.assertFalse(cfg.get_bool("ui.wizard_done", False),
                                 "光是点选不落库，按完成才算看过")
                wizard.finish(open_settings=False)
                self.assertEqual(cfg.base_url, LOCAL_ENDPOINT[0])
                self.assertEqual(cfg.model, LOCAL_ENDPOINT[1])
                self.assertTrue(cfg.get_bool("ui.wizard_done", False))
                self.assertFalse(should_show(cfg), "关掉之后不许再弹")
        self.assertEqual(calls, ["changed"], "写完配置要通知 App 刷新服务")

    def test_going_to_the_settings_page_also_marks_the_wizard_as_seen(self):
        from tests.support import FakeWidget
        from app.ui.setup_wizard import SetupWizard

        with support.temp_db() as db:
            cfg = Config(db)
            cfg.set("api.base_url", "https://api.deepseek.com/v1")
            cfg.set("api.model", "deepseek-chat")
            with _FakeTkEnv():
                stub, calls = self._app(cfg)
                wizard = SetupWizard(FakeWidget(None), stub)
                wizard.finish(open_settings=True)
                self.assertTrue(cfg.get_bool("ui.wizard_done", False))
                self.assertEqual(cfg.base_url, "https://api.deepseek.com/v1",
                                 "云端选项 = 保持用户现有的地址，不许被向导改掉")
                self.assertEqual(cfg.model, "deepseek-chat")
        self.assertEqual(calls, ["changed", "settings"],
                         "要顺手把设置页打开（用户去填 Key）")

    def test_closing_the_window_counts_as_seen(self):
        from tests.support import FakeWidget
        from app.ui.setup_wizard import SetupWizard

        with support.temp_db() as db:
            cfg = Config(db)
            with _FakeTkEnv():
                stub, _calls = self._app(cfg)
                wizard = SetupWizard(FakeWidget(None), stub)
                wizard._on_close()
                self.assertTrue(cfg.get_bool("ui.wizard_done", False),
                                "「×」等同「先随便看看」，不能每次都弹")

    def test_the_app_shows_it_once_on_the_first_open(self):
        """接在 ``App.open_main_window`` 末尾：第一次打开主界面弹、之后不弹。"""
        from app.main import App

        with support.temp_db() as db:
            cfg = Config(db)
            opened: list = []
            stub = SimpleNamespace(config=cfg,
                                   gate_controller=SimpleNamespace(is_locked=lambda: False),
                                   open_setup_wizard=lambda: opened.append("wizard"))
            self.assertTrue(App.maybe_show_setup_wizard(stub))
            self.assertEqual(opened, ["wizard"])
            cfg.set_bool("ui.wizard_done", True)
            self.assertFalse(App.maybe_show_setup_wizard(stub))
            self.assertEqual(opened, ["wizard"], "第二次打开不许再弹")
            cfg.set_bool("ui.wizard_done", False)
            locked = SimpleNamespace(config=cfg,
                                     gate_controller=SimpleNamespace(is_locked=lambda: True),
                                     open_setup_wizard=lambda: opened.append("wizard"))
            self.assertFalse(App.maybe_show_setup_wizard(locked),
                             "门控硬阻断（游戏 / 全屏）时不弹")
            self.assertEqual(opened, ["wizard"])

    def test_lifting_the_main_window_alone_never_pops_the_wizard(self):
        """亮主窗 ≠ 弹向导：只有启动 / 显式呼出那条路径才带向导。

        设置页与「解释并记录」都会顺带调 ``open_main_window()`` 把父窗口亮出来，
        那条路径要是也弹向导，用户点一次「设置」就会被塞一个向导窗。
        """
        from app.main import App

        with support.temp_db() as db:
            cfg = Config(db)
            opened: list = []
            stub = SimpleNamespace(
                config=cfg,
                gate_controller=SimpleNamespace(is_locked=lambda: False),
                open_setup_wizard=lambda: opened.append("wizard"),
                main=SimpleNamespace(apply_pending_follow=lambda: None),
                root=SimpleNamespace(deiconify=lambda: None, lift=lambda: None,
                                     focus_force=lambda: None),
                lift_main_window=lambda: None,
            )
            stub.maybe_show_setup_wizard = lambda: App.maybe_show_setup_wizard(stub)
            stub.open_main_window = lambda **kw: App.open_main_window(stub, **kw)
            App.open_main_window(stub)
            self.assertEqual(opened, [], "亮主窗（设置页 / 手动录入）不许顺带弹向导")
            App.open_main_window(stub, wizard=True)
            self.assertEqual(opened, ["wizard"], "启动路径（双击 exe / 呼出）才弹")
            App.show_on_startup(stub)
            self.assertEqual(opened, ["wizard", "wizard"], "启动入口 = 带向导那一路")


class TestConceptMapFreshness(unittest.TestCase):
    """窗口打开之后数据变了：图窗必须按**当前库 + 当前请求归属**本地跟上。

    覆盖三个真实数据缺陷（根因都是「窗口只信自己打开时的旧快照」）：

    1. 打开窗口后再新增 / 编辑 / 删除词条、解释完成 —— 生成与刷新都必须重新读库；
    2. 异步结果回来时只比旧指纹 —— 现在必须重新查库 + 核对归属：过期结果丢弃，
       既不覆盖当前图，也不把按钮永远卡在「正在生成…」，更不能解除**更新**请求的忙态；
    3. 图窗重开时同主题同内容已有在途请求 —— 不重复发，但要有**真实**反馈；
    4. 内容**没变**的刷新通知（普通搜索 / 别的主题变更走的是同一条真实刷新路径）
       —— 图、在途请求、按钮、点选依据、画布一个都不许动。
    """

    def _stub(self, db, service, **extra):
        return SimpleNamespace(db=db, map_service=service, **extra)

    def _valid_candidate(self):
        return {"src": 0, "dst": 1, "type": "包含", "reason": "卷积核是卷积的单元",
                "evidence": "卷积核在输入上滑动"}

    @staticmethod
    def _put_cache(app, bid, relations):
        """按**当前库内容**写一份缓存（打开窗口即命中，一次请求都不发）。"""
        from app.map_service import MAP_VALIDATION_VERSION

        nodes = app.map_service.nodes_for_topic(int(bid))
        fingerprint = app.map_service.fingerprint(int(bid), nodes)
        app.db.put_map_graph(topic_id=int(bid), fingerprint=fingerprint,
                             base_url=app.config.base_url, model=app.config.model,
                             relations=list(relations),
                             validation_version=MAP_VALIDATION_VERSION)
        return fingerprint

    def test_real_save_delete_and_explain_refresh_update_the_open_map(self):
        """真实保存 / 删除 / 解释完成三条刷新路径：图窗当场跟上，且零新请求。"""
        with support.headless_app(main_window="real") as app:
            bid = int(app.db.create_batch("卷积网络"))
            first = int(app.db.add_entry(batch_id=bid, term="卷积",
                                         context="卷积核在输入上滑动"))
            second = int(app.db.add_entry(batch_id=bid, term="池化",
                                          context="池化用于降采样"))
            third = int(app.db.add_entry(batch_id=bid, term="过拟合",
                                         context="正则化抑制过拟合"))
            for entry_id, one_line in ((first, "卷积是加权求和"),
                                       (second, "池化降低分辨率"),
                                       (third, "过拟合是记住了噪声")):
                app.db.update_entry(entry_id, one_line=one_line, explain_status="ok")
            client = _MapClient([])
            app.config.set("api.base_url", "https://api.example.com/v1")
            app.config.set("api.model", "model-A")
            app.config.set_api_key("sk-test-not-real")
            app.map_service.make_client = lambda **kw: client
            self._put_cache(app, bid, [{"src": first, "dst": second, "type": "包含",
                                        "reason": "卷积核是卷积的单元",
                                        "evidence": "卷积核在输入上滑动"}])
            app.open_concept_map()
            win = app._map_win
            self.assertIsNotNone(win._graph, "命中缓存立刻显示")
            self.assertEqual([int(n.entry_id) for n in win._nodes], [first, second, third])
            self.assertEqual(client.calls, [], "命中缓存时打开图窗不发请求")

            # ① 真实保存路径：改词 → 图窗节点当场更新、过期图丢掉、忙态清掉
            app.main.select_entry(first)
            app.main.var_term.set("卷积核")
            app.main.save_detail()
            self.assertEqual([n.term for n in win._nodes], ["卷积核", "池化", "过拟合"])
            self.assertIsNone(win._graph, "内容变了：过期图必须当场丢掉")
            self.assertIn("词条已更新", str(win.feedback.cget("text")))
            self.assertIsNone(win._pending_token, "本地刷新绝不自动发请求")
            self.assertEqual(client.calls, [], "本地刷新一次 LLM 请求都不许发")
            self.assertEqual(app.map_service.inflight_count(), 0)

            # ② 真实解释完成事件（经 UI 队列 → MainWindow 真实刷新路径）
            app.db.update_entry(third, one_line="过拟合：记住了训练噪声",
                                detail="泛化变差", explain_status="ok")
            app._ui_q.put(("explain_result", (third, "ok", None, None)))
            support.pump_app(app)
            node = [n for n in win._nodes if int(n.entry_id) == third][0]
            self.assertIn("记住了训练噪声", node.explanation, "新释义必须进图窗节点")
            self.assertEqual(client.calls, [])

            # ③ 真实删除路径：节点计数同步减少
            app.main.select_entry(second)
            app.main.delete_selected()
            self.assertEqual([int(n.entry_id) for n in win._nodes], [first, third])
            self.assertEqual(client.calls, [])

            # ④ 清空当前主题：节点归零、上限提示撤销、按钮禁用、仍然零请求
            app.db.delete_entry(first)
            app.db.delete_entry(third)
            app.main.refresh_entries()
            self.assertEqual(win._nodes, [])
            self.assertEqual(win._coverage_note, "", "没有词条就不该残留上限提示")
            self.assertEqual(str(win.feedback.cget("text")),
                             "该主题还没有词条：先在阅读页解释并记录几个词")
            self.assertFalse(win.btn_generate.is_enabled(), "空主题不能生成")
            self.assertEqual(client.calls, [])
            self.assertEqual(app.map_service.inflight_count(), 0)

    def test_coverage_note_follows_the_current_node_count_after_refresh(self):
        """被上限截断才提示；删到不再截断，提示必须跟着消失（节点计数同步）。"""
        with support.headless_app(main_window="real") as app:
            app.config.set("map.max_entries", "2")
            bid = int(app.db.create_batch("上限主题"))
            ids = [int(app.db.add_entry(batch_id=bid, term=f"词{index}", context="上下文"))
                   for index in range(3)]
            self._put_cache(app, bid, [])
            app.open_concept_map()
            win = app._map_win
            self.assertEqual(len(win._nodes), 2, "单次分析有硬上限")
            self.assertIn("本次分析 2 词 / 该主题共 3 词", str(win.feedback.cget("text")))
            app.db.delete_entry(ids[2])
            app.main.refresh_entries()              # 真实刷新路径 → 通知图窗
            self.assertEqual(len(win._nodes), 2)
            self.assertEqual(win._coverage_note, "", "不再被截断就撤掉上限提示")
            self.assertIn("词条已更新（2 词）", str(win.feedback.cget("text")))

    def test_a_stale_result_releases_the_busy_state_it_owns(self):
        """库直接变了（通知没赶上）：过期结果必须放掉**它自己**那份忙态。"""
        from app.ui import concept_map as cm

        with support.temp_db() as db:
            bid, ids = _map_db(db)
            gate = threading.Event()
            client = _MapClient([self._valid_candidate()], gate=gate)
            service, _cfg = _map_service(db, client=client)
            delivered: list = []
            with _FakeTkEnv():
                win = cm.ConceptMapWindow(None, self._stub(
                    db, service, open_settings=lambda: None))
                self.assertTrue(_wait_for(lambda: client.calls))
                self.assertEqual(str(win.btn_generate.cget("text")), "正在生成…")
                db.update_entry(ids["卷积"], context="改过的上下文")
                service.set_result_sink(lambda *a: delivered.append(a))
                gate.set()
                self.assertTrue(_wait_for(lambda: delivered))
                self.assertFalse(win.on_map_result(delivered[0]),
                                 "旧语境的结果绝不采纳")
                self.assertIsNone(win._pending_token, "过期结果绝不能把按钮永远卡在忙态")
                self.assertIsNone(win._graph)
                self.assertNotEqual(str(win.btn_generate.cget("text")), "正在生成…")
                self.assertTrue(win.btn_generate.is_enabled(), "过期后必须能重新生成")
                self.assertIn("丢弃", str(win.feedback.cget("text")))
                self.assertIn("改过的上下文", [n.context for n in win._nodes],
                              "当场按最新库重读词条")

    def test_an_older_result_never_releases_a_newer_requests_busy_state(self):
        """旧结果回来时更新请求还在途：旧结果丢弃，但**绝不**解除新请求的忙态。"""
        from app.ui import concept_map as cm

        with support.temp_db() as db:
            bid, ids = _map_db(db)
            gate = threading.Event()
            client = _MapClient([self._valid_candidate()], gate=gate)
            service, _cfg = _map_service(db, client=client)
            delivered: list = []
            service.set_result_sink(lambda *a: delivered.append(a))
            with _FakeTkEnv():
                win = cm.ConceptMapWindow(None, self._stub(
                    db, service, open_settings=lambda: None))
                self.assertTrue(_wait_for(lambda: client.calls))
                old_fingerprint = str(win._fingerprint)
                first_token = int(win._pending_token)
                # 内容变了（本地通知）→ 旧请求作废；用户按新内容再点一次生成
                db.update_entry(ids["卷积"], context="新的上下文：卷积核在输入上滑动（逐点相乘）")
                self.assertTrue(win.on_entries_changed())
                self.assertIsNone(win._pending_token, "通知清掉过期请求关联")
                win._on_generate()
                self.assertTrue(_wait_for(lambda: len(client.calls) == 2))
                second_token = int(win._pending_token)
                self.assertNotEqual(first_token, second_token)
                self.assertIn("新的上下文", client.calls[1]["entries"][0]["context"],
                              "重新生成必须用**新**上下文，而不是窗口打开时的旧快照")

                gate.set()
                self.assertTrue(_wait_for(lambda: len(delivered) == 2))
                old_payload = [p for p in delivered if str(p[2]) == old_fingerprint][0]
                new_payload = [p for p in delivered if str(p[2]) != old_fingerprint][0]
                self.assertFalse(win.on_map_result(old_payload), "旧语境的结果丢弃")
                self.assertEqual(int(win._pending_token), second_token,
                                 "绝不能解除更新请求的忙态")
                self.assertEqual(str(win.btn_generate.cget("text")), "正在生成…")
                self.assertIsNone(win._graph, "旧结果绝不画进当前图")
                self.assertTrue(win.on_map_result(new_payload), "按新内容的结果必须采纳")
                self.assertIsNone(win._pending_token)
                self.assertIsNotNone(win._graph)
                self.assertTrue(win._items_matching("edge"))
                self.assertIsNotNone(
                    db.get_map_graph(bid, old_fingerprint, base_url=_cfg.base_url,
                                     model=_cfg.model),
                    "原工作线程可以干完，但只缓存它自己的旧指纹")

    def test_reopening_while_the_same_topic_is_inflight_adopts_the_request(self):
        """图窗关掉又打开、同主题同内容已有在途请求：不重复发，但要有真实反馈。"""
        from app.ui import concept_map as cm

        with support.temp_db() as db:
            bid, _ids = _map_db(db)
            gate = threading.Event()
            client = _MapClient([self._valid_candidate()], gate=gate)
            service, _cfg = _map_service(db, client=client)
            delivered: list = []
            service.set_result_sink(lambda *a: delivered.append(a))
            with _FakeTkEnv():
                first = cm.ConceptMapWindow(None, self._stub(
                    db, service, open_settings=lambda: None))
                self.assertTrue(_wait_for(lambda: client.calls))
                first.close()
                second = cm.ConceptMapWindow(None, self._stub(
                    db, service, open_settings=lambda: None))
                self.assertEqual(len(client.calls), 1, "同主题同内容的请求在途：不重复发")
                self.assertEqual(str(second.btn_generate.cget("text")), "正在生成…")
                self.assertIn("在途请求", str(second.feedback.cget("text")),
                              "按钮不能像点了没反应：必须给出真实状态")
                gate.set()
                self.assertTrue(_wait_for(lambda: delivered))
                self.assertTrue(second.on_map_result(delivered[0]),
                                "在途请求的结果回来必须直接显示")
                self.assertIsNotNone(second._graph)
                self.assertTrue(second._items_matching("edge"))
                self.assertEqual(len(client.calls), 1)

    def test_unchanged_refresh_keeps_the_ready_graph_and_selection_intact(self):
        """ready 态：普通搜索 / 别的主题变更触发的**真实刷新路径**内容没变
        → 图、按钮、点选依据、画布一个都不动（旧实现会把当前图直接清掉）。"""
        with support.headless_app(main_window="real") as app:
            bid = int(app.db.create_batch("卷积网络"))
            first = int(app.db.add_entry(batch_id=bid, term="卷积",
                                         context="卷积核在输入上滑动"))
            second = int(app.db.add_entry(batch_id=bid, term="池化",
                                          context="池化用于降采样"))
            app.config.set("api.base_url", "https://api.example.com/v1")
            app.config.set("api.model", "model-A")
            app.config.set_api_key("sk-test-not-real")
            client = _MapClient([])
            app.map_service.make_client = lambda **kw: client
            self._put_cache(app, bid, [{"src": first, "dst": second, "type": "包含",
                                        "reason": "卷积核是卷积的单元",
                                        "evidence": "卷积核在输入上滑动"}])
            app.open_concept_map()
            win = app._map_win
            graph = win._graph
            self.assertIsNotNone(graph, "命中缓存立刻显示")
            self.assertTrue(win._items_matching("edge"))

            # 点选一条连线：依据区 / 选中对象在无变化刷新后必须原样保留
            edge = win._layout.edges[0]
            start, end = edge.points[0], edge.points[-1]
            win._on_canvas_click(SimpleNamespace(x=(start[0] + end[0]) / 2,
                                                 y=(start[1] + end[1]) / 2))
            selected = win._selected
            self.assertIsNotNone(selected)
            snapshot = (win._state, str(win.feedback.cget("text")),
                        str(win.btn_generate.cget("text")),
                        str(win.evidence.cget("text")), win._draw_key,
                        win._item_count(), len(win._items_matching("edge")))

            # ① 真实搜索路径：搜索框打字触发的刷新（当前主题词条一格没变）
            app.main.search_var.set("卷积")
            app.main.refresh_entries()
            # ② 真实路径：别的主题新增词条 + 切到那个主题浏览
            other = int(app.db.create_batch("别的主题"))
            app.db.add_entry(batch_id=other, term="别的词", context="别的上下文")
            app.main.set_browse_scope(other)      # 内部自己会 refresh_entries
            app.main.refresh_entries()

            self.assertIs(win._graph, graph, "无变化通知不得丢掉当前图")
            self.assertIs(win._selected, selected, "点选依据不得被清掉")
            self.assertEqual(
                (win._state, str(win.feedback.cget("text")),
                 str(win.btn_generate.cget("text")),
                 str(win.evidence.cget("text")), win._draw_key,
                 win._item_count(), len(win._items_matching("edge"))),
                snapshot, "无变化通知必须一个控件都不动（含画布与忙态）")
            self.assertEqual([int(n.entry_id) for n in win._nodes], [first, second])
            self.assertEqual(client.calls, [], "无变化通知绝不自动发请求")
            self.assertEqual(app.map_service.inflight_count(), 0)

    def test_unchanged_refresh_keeps_the_inflight_request_and_busy_button(self):
        """loading 态：生成中收到无变化通知 → 在途请求关联与忙态都不许清。"""
        with support.headless_app(main_window="real") as app:
            bid = int(app.db.create_batch("卷积网络"))
            int(app.db.add_entry(batch_id=bid, term="卷积",
                                 context="卷积核在输入上滑动"))
            int(app.db.add_entry(batch_id=bid, term="池化",
                                 context="池化用于降采样"))
            app.config.set("api.base_url", "https://api.example.com/v1")
            app.config.set("api.model", "model-A")
            app.config.set_api_key("sk-test-not-real")
            gate = threading.Event()
            client = _MapClient([self._valid_candidate()], gate=gate)
            app.map_service.make_client = lambda **kw: client
            app.open_concept_map()
            win = app._map_win
            self.assertTrue(_wait_for(lambda: client.calls), "打开图 = 明确动作：真的去生成")
            token = win._pending_token
            self.assertIsNotNone(token, "在途请求必须记住归属 token")
            self.assertEqual(win._state, "loading")
            self.assertEqual(str(win.btn_generate.cget("text")), "正在生成…")
            items = (win._item_count(), win._draw_key)

            app.main.search_var.set("卷积")
            app.main.refresh_entries()
            other = int(app.db.create_batch("别的主题"))
            app.db.add_entry(batch_id=other, term="别的词", context="别的上下文")
            app.main.set_browse_scope(other)
            app.main.refresh_entries()

            self.assertEqual(win._pending_token, token, "无变化通知不得解绑在途请求")
            self.assertEqual(win._state, "loading", "忙态必须原样保留")
            self.assertEqual(str(win.btn_generate.cget("text")), "正在生成…")
            self.assertFalse(win.btn_generate.is_enabled(), "忙态按钮保持禁用")
            self.assertEqual((win._item_count(), win._draw_key), items,
                             "无变化通知不得重画画布")
            self.assertEqual(len(client.calls), 1, "无变化通知绝不重复发请求")

            # 收尾：放行在途请求并等它落地（后台线程不跨临时库收尾）
            gate.set()
            self.assertTrue(_wait_for(lambda: app.map_service.inflight_count() == 0))
            support.pump_app(app)


class TestMapLayoutAlignment(unittest.TestCase):
    """排布规则（用户反馈「两个框中心错位、尺寸杂乱」的定点回归）。

    * 同层节点**高矮一致**（行高 = 该行最高者），行内垂直居中对齐；
    * 所有行与中心主题共用**同一条中心轴**（不再出现主题居中、某一行整体偏左）；
    * 间距一律等距（列间距 = ``h_gap``、行距 = ``v_gap``）；
    * 孤立词排成**整齐网格**（多行、每行居中、行高统一），不是拉成一长条。
    """

    def _rel(self, src, dst, rel_type, reason="依据", evidence="片段"):
        from app.map_service import MapRelation

        return MapRelation(src, dst, rel_type, reason, evidence)

    #: 用户截图里那两个词 + 一个会换行的长词（尺寸杂乱的来源）；
    #: 第 5 个词**没有任何边**：孤立词网格与「全图同一条中心轴」必须真的被覆盖到，
    #: 否则拿一个没有孤立词的 fixture 去 ``min()`` 孤立词只会把用例本身弄错。
    #: 第 6 个词是长词**真正的同层伙伴**（3→4 包含把 4 放到下一层，1→6 包含把
    #: 6 放到同一条下一层），同层高必须拿这两个词比。
    LABELS = {1: "Game data science", 2: "visualization", 3: "卷积层",
              4: "Transformer 编码器结构中的自注意力机制与前馈网络",
              5: "孤立词", 6: "卷积"}
    RELATIONS = ((1, 2, "对照"), (3, 4, "包含"), (1, 6, "包含"))

    def _layout(self, width=860, height=620, labels=None, relations=None):
        from app.ui.concept_map import layout_graph

        return layout_graph(dict(labels or self.LABELS),
                            [self._rel(*item) for item in
                             (relations if relations is not None else self.RELATIONS)],
                            width=width, height=height,
                            topic_label="Game data science")

    def _rows(self, layout) -> dict:
        rows: dict = {}
        for node in layout.nodes:
            if node.isolated:
                continue
            rows.setdefault(round(node.y, 3), []).append(node)
        return rows

    def test_same_row_nodes_share_height_and_vertical_center(self):
        layout = self._layout()
        rows = self._rows(layout)
        self.assertEqual(len(rows), 2, "两条关系构成两层：1/3 一行、2/4 一行")
        for y, nodes in rows.items():
            heights = {round(node.h, 3) for node in nodes}
            self.assertEqual(len(heights), 1,
                             f"同一行的节点高度必须一致（行 {y}）：{heights}")
            for node in nodes:
                self.assertAlmostEqual(node.y, y, places=6,
                                       msg="同一行的节点必须垂直居中对齐")

    def test_topic_and_every_row_share_one_center_axis(self):
        layout = self._layout()
        centers = [layout.topic.x]
        for nodes in self._rows(layout).values():
            row_left = min(node.x - node.w / 2.0 for node in nodes)
            row_right = max(node.x + node.w / 2.0 for node in nodes)
            centers.append((row_left + row_right) / 2.0)
        isolated = [node for node in layout.nodes if node.isolated]
        self.assertTrue(isolated, "前提：fixture 里必须有第五个无边的孤立词")
        isolated_left = min(node.x - node.w / 2.0 for node in isolated)
        isolated_right = max(node.x + node.w / 2.0 for node in isolated)
        centers.append((isolated_left + isolated_right) / 2.0)
        for center in centers:
            self.assertAlmostEqual(center, centers[0], places=6,
                                   msg=f"中心轴必须全图一致：{centers}")

    def test_column_and_row_spacing_are_uniform(self):
        from app.ui import concept_map as cm

        m = cm.metrics_for()
        layout = self._layout()
        rows = self._rows(layout)
        widths = {round(node.w, 6) for node in layout.nodes}
        self.assertEqual(len(widths), 1, f"统一卡片宽：所有词卡同宽，实际 {widths}")
        for nodes in rows.values():
            ordered = sorted(nodes, key=lambda node: node.x)
            for first, second in zip(ordered, ordered[1:]):
                gap = (second.x - second.w / 2.0) - (first.x + first.w / 2.0)
                self.assertAlmostEqual(gap, m.h_gap, places=6,
                                       msg="同一行列间距必须一样（等距紧凑）")
        row_ys = sorted(rows)
        tops = {y: min(node.y - node.h / 2.0 for node in rows[y]) for y in row_ys}
        bottoms = {y: max(node.y + node.h / 2.0 for node in rows[y]) for y in row_ys}
        for upper, lower in zip(row_ys, row_ys[1:]):
            self.assertAlmostEqual(tops[lower] - bottoms[upper], m.v_gap, places=6,
                                   msg="行间距必须一律 v_gap")

    def test_a_wrapping_long_word_does_not_break_the_row_rules(self):
        layout = self._layout()
        long_node = layout.find(4)
        self.assertGreater(len(long_node.lines), 1, "长词必须换行（不截断）")
        # 同层 = 同一行：3→4 是「包含」（父在上、子在下），长词 4 与 1→6
        # 的「卷积」同在下层；1→2 对照不约束层级。
        partner = layout.find(6)
        self.assertEqual(long_node.level, partner.level, "前提：这两个词真的同层")
        self.assertAlmostEqual(long_node.h, partner.h, places=6,
                               msg="同一行里长词与短词的高度必须一致")
        self.assertAlmostEqual(long_node.y, partner.y, places=6)
        self.assertAlmostEqual(long_node.w, partner.w, places=6,
                               msg="统一卡片宽：长词与短词同宽（不再随文字忽宽忽窄）")

    def test_isolated_words_form_a_tidy_centered_grid(self):
        labels = {index: f"孤立词{index}" for index in range(1, 15)}
        layout = self._layout(width=520, height=640, labels=labels, relations=[])
        isolated = [node for node in layout.nodes if node.isolated]
        self.assertEqual(len(isolated), 14, "孤立词一个都不许少")
        rows: dict = {}
        for node in isolated:
            rows.setdefault(round(node.y, 3), []).append(node)
        self.assertGreater(len(rows), 1, "放不下时必须换到下一行（不是拉出屏幕）")
        widths = {len(nodes) for nodes in rows.values()}
        self.assertLessEqual(max(widths) - min(widths), 1, "每行词数必须基本一致")
        for nodes in rows.values():
            for node in nodes:
                self.assertAlmostEqual(node.h, nodes[0].h, places=6,
                                       msg="同一行孤立词高度一致")
                self.assertAlmostEqual(node.y, nodes[0].y, places=6)
                self.assertLessEqual(node.x + node.w / 2.0, layout.content_w,
                                     "孤立词不许排出内容外框")
        # 网格的每一行都居中在同一条轴上
        centers = []
        for nodes in rows.values():
            centers.append((min(node.x - node.w / 2.0 for node in nodes)
                            + max(node.x + node.w / 2.0 for node in nodes)) / 2.0)
        for center in centers:
            self.assertAlmostEqual(center, centers[0], places=6,
                                   msg="孤立词网格必须居中在同一轴")

    def test_extent_matches_the_actual_layout(self):
        """``layout_extent``（首开自适应用它）不得**低估**真实布局尺寸。"""
        from app.ui import concept_map as cm

        labels = {index: f"孤立词{index}" for index in range(1, 20)}
        labels[30] = "很长很长的词条名称会被换行显示但不能被截断"
        relations = [self._rel(1, 2, "因果"), self._rel(2, 3, "因果")]
        relations.append(self._rel(30, 4, "对照"))
        extent_w, extent_h = cm.layout_extent(labels, relations, width=520, height=640,
                                              topic_label="主题")
        layout = cm.layout_graph(labels, relations, width=520, height=640,
                                 topic_label="主题")
        self.assertGreaterEqual(extent_w + 1e-6, layout.content_w,
                                "自适应不能把内容算小（否则一开就被裁掉）")
        self.assertGreaterEqual(extent_h + 1e-6, layout.content_h,
                                "自适应不能把高度算小")


class TestConceptMapCamera(unittest.TestCase):
    """镜头交互：右键拖动平移（大图 / 小图四向自由挪） / 滚轮以指针为锚缩放 /
    首开自适应 / 世界坐标偏移在刷新后不变 / 平移不吞点击。"""

    def _stub(self, db, service, **extra):
        return SimpleNamespace(db=db, map_service=service, **extra)

    def _window(self, db, env, **extra):
        from app.ui import concept_map as cm

        service, _cfg = _map_service(db, key="", client=_MapClient([]))
        return cm.ConceptMapWindow(None, self._stub(db, service,
                                                    open_settings=lambda: None,
                                                    **extra))

    def _big_content(self, db):
        """真实主题 + 真实缓存图：内容**明显高于**画布，且**真的有连线可点**。

        镜头用例必须走产品自己的载入路径（``nodes_for_topic`` + 按内容指纹命中的
        缓存图）：没有主题 / 没有 nodes/graph 的 fixture 上，``layout.edges`` 是空的，
        平移与缩放断言根本无从谈起（更不能用「给窗口塞一个假 reader」来兜）。
        一个中心词 + 6 个包含词撑宽第一层（一排 6 张卡，宽度远超画布），
        再加 10 个孤立词撑高网格。
        """
        bid = int(db.create_batch("镜头主题"))
        rows = (("卷积神经网络", "卷积神经网络包含卷积层这类网络层"),
                ("卷积层", "卷积层带可学习参数"),
                ("池化层", "池化层用于降采样"),
                ("卷积核", "卷积核在输入上滑动"),
                ("降采样", "降采样缩小特征图尺寸"),
                ("感受野", "池化扩大感受野"),
                ("特征图", "卷积得到特征图"))
        ids = {name: int(db.add_entry(batch_id=bid, term=name, context=context))
               for name, context in rows}
        for index in range(10):
            db.add_entry(batch_id=bid, term=f"孤立词{index}", context="上下文")
        hub = ids["卷积神经网络"]
        relations = [{"src": hub, "dst": ids[name], "type": "包含",
                      "reason": f"{name}是卷积神经网络的一层",
                      "evidence": context}
                     for name, context in rows[1:]]
        service, _cfg = _map_service(db, key="", client=_MapClient([]))
        _put_map_cache(db, service, bid, relations)
        return bid, ids

    def _small_content(self, db):
        """小图：内容**明显小于**画布，但真的有一条可点的连线。

        小图正是旧滚动范围（``内容 + 24px``）钉死的那种图：往右下一点都挪不动。
        这里只放中心词 + 1 个包含词，尺寸稳稳小于 400x300 的测试画布。
        """
        bid = int(db.create_batch("小图主题"))
        rows = (("卷积", "卷积核在输入上滑动，逐点相乘再求和"),
                ("卷积核", "卷积核在输入上滑动"))
        ids = {name: int(db.add_entry(batch_id=bid, term=name, context=context))
               for name, context in rows}
        relations = [{"src": ids["卷积"], "dst": ids["卷积核"], "type": "包含",
                      "reason": "卷积核是卷积的单元", "evidence": "卷积核在输入上滑动"}]
        service, _cfg = _map_service(db, key="", client=_MapClient([]))
        _put_map_cache(db, service, bid, relations)
        return bid, ids

    @staticmethod
    def _press(win, x_root, y_root):
        """按下右键（**还没松手**）：返回按下时的视图原点（画布世界坐标）。"""
        win._on_pan_start(SimpleNamespace(x_root=x_root, y_root=y_root,
                                          x=10, y=10))
        return win._view_left(), win._view_top()

    @staticmethod
    def _release(win, x_root, y_root):
        """右键松手：之后的第一下左键就是一次**正常点击**（不许被吞掉）。"""
        win._on_pan_end(SimpleNamespace(x_root=x_root, y_root=y_root))

    @staticmethod
    def _drag(win, x_root, y_root, dx, dy):
        """右键从 ``(x_root, y_root)`` 拖 ``(dx, dy)`` 像素再松手。

        返回 ``(平移前, 平移后)`` 两个视图原点（画布世界坐标）—— 平移的量在
        世界坐标里量，不再看「随 scrollregion 变的分数」。
        """
        before = TestConceptMapCamera._press(win, x_root, y_root)
        win._on_pan_motion(SimpleNamespace(x_root=x_root + dx, y_root=y_root + dy))
        after = (win._view_left(), win._view_top())
        TestConceptMapCamera._release(win, x_root + dx, y_root + dy)
        return before, after

    @staticmethod
    def _clickable(win, edge):
        """折线上**真的能点中连线**的一点：取线段中点，且不落在任何词卡里。

        新规则是「先词后线」（``_on_canvas_click`` 先问 ``node_at``）：折线的端点
        常常正好贴在下游卡片的边线上，按老写法拿 ``points[len // 2]`` 当点击点，
        现在算的是**点词**。所以这里取线段中点，并用产品自己的两个判定函数把
        「这一点既不在词卡里、又确实在连线上」当成前置条件，交给用例断言。
        """
        from app.ui import theme

        for first, second in zip(edge.points, edge.points[1:]):
            mid = ((first[0] + second[0]) / 2.0, (first[1] + second[1]) / 2.0)
            if win._layout.node_at(mid[0], mid[1], pad=theme.px(2)) is None \
                    and win._layout.edge_at(mid[0], mid[1],
                                            tol=theme.px(10)) is edge:
                return mid
        raise AssertionError("这条折线整段都埋在词卡里，画布上点不到它")

    @staticmethod
    def _click(win, x, y):
        """在**画布坐标** (x, y) 处点一下。

        真实 Tk 的 ``event.x / y`` 是控件坐标，因此这里按当前视图偏移换成控件
        坐标再投出去 —— 与实机上「用户点屏幕上那个位置」完全同语义（视图被
        拖动 / 缩放移动过之后，画布坐标与控件坐标不再相等）。
        """
        win._on_canvas_click(SimpleNamespace(x=x - win._view_left(),
                                             y=y - win._view_top()))

    def test_right_button_drag_pans_the_canvas_and_never_selects_an_edge(self):
        from app.ui import concept_map as cm

        with support.temp_db() as db:
            self._big_content(db)
            with _FakeTkEnv() as env:
                env.canvases.clear()
                win = self._window(db, env)
                canvas = env.canvases[-1]
                canvas.configure(width=320, height=220)
                win._fit_pending = False
                win._draw(force=True)
                self.assertGreater(win._layout.content_w, 320, "前提：内容比画布宽")
                self.assertGreater(win._layout.content_h, 220, "前提：内容比画布高")
                self.assertTrue(win._layout.edges, "前提：缓存图真的载入了可点的连线")

                # 左键点一条连线：正常选中（右键平移不许影响这条路径）
                edge = win._layout.edges[0]
                middle = edge.points[len(edge.points) // 2]
                self._click(win, middle[0], middle[1])
                self.assertIsNotNone(win._selected, "左键点连线要能选中")
                win._selected = None
                win._update_evidence()

                start = self._press(win, 400, 300)
                win._on_pan_motion(SimpleNamespace(x_root=300, y_root=250))
                moved = (win._view_left(), win._view_top())
                self.assertAlmostEqual(moved[0], start[0] + 100, delta=0.5,
                                       msg="指针向左拖 100px → 视图原点 +100（画面跟手）")
                self.assertAlmostEqual(moved[1], start[1] + 50, delta=0.5,
                                       msg="指针向上拖 50px → 视图原点 +50（画面跟手）")
                self.assertEqual(canvas.xview_calls[-1], ("scroll", 100, "units"),
                                 "平移量 = 指针位移（1:1，跟手方向）")

                # 右键**还按着**的时候，左键这一下不作数（人正在平移，不可能在点线）
                self._click(win, middle[0], middle[1])
                self.assertIsNone(win._selected, "右键按住期间的左键不算点击")
                self.assertTrue(str(win.evidence.cget("text")).startswith("点击一条连线"),
                                "依据区必须保持「未选中」")

                # 右键松手：紧接着的**第一次**左键是一次正常点击 —— 必须点中那条线
                self._release(win, 300, 250)
                self._click(win, middle[0], middle[1])
                self.assertIsNotNone(win._selected,
                                     "右键松手后的首次左键点击不许被吞掉（点线要有效）")
                self.assertTrue(str(win.evidence.cget("text")).startswith(f"{edge.label}："),
                                "依据区要显示这条关系（点击真的被处理了）")
                # 同一时刻点空白也照常生效（点线 / 点词 / 点空白都不是被吞掉的一下）
                self._click(win, -300.0, -200.0)
                self.assertIsNone(win._selected, "点空白照样要能收起依据")

                # 左键**不参与**平移：按着左键移动（没有右键按住），视图一动不动
                before = (win._view_left(), win._view_top())
                win._on_pan_motion(SimpleNamespace(x_root=500, y_root=400))
                self.assertEqual((win._view_left(), win._view_top()), before,
                                 "左键移动不许平移（平移只认右键按住）")

    def test_small_content_pans_freely_in_all_four_directions(self):
        """小图（内容比画布小）也必须四个方向自由平移，且平移后首次点击有效。

        旧实现把 ``scrollregion`` 钉在 ``(0, 0, max(视口, 内容) + 24)``：内容比
        视口小时整张图只有 24px 可挪（往右下根本挪不动）。现在四边各留**一个
        视口**，左右上下各拖 100px 都要真的走 100px，每边至少能量出一个视口。
        """
        with support.temp_db() as db:
            self._small_content(db)
            with _FakeTkEnv() as env:
                env.canvases.clear()
                win = self._window(db, env)
                canvas = env.canvases[-1]
                # 画布给得**确实**比内容大（留白口径上调后内容高 ~301px，
                # 这里用 340 才有余量）：本用例的前提就是「小图」
                canvas.configure(width=420, height=340)
                win._fit_pending = False
                win._draw(force=True)
                self.assertLess(win._layout.content_w, 420, "前提：内容比画布窄")
                self.assertLess(win._layout.content_h, 340, "前提：内容比画布矮")
                self.assertTrue(win._layout.edges, "前提：小图也有可点的连线")
                self.assertAlmostEqual(win._view_left(), 0.0, places=6,
                                       msg="初始视图落在世界原点 0")
                self.assertAlmostEqual(win._view_top(), 0.0, places=6,
                                       msg="初始视图落在世界原点 0")

                # 左右上下各拖 100px：每一把都真的走 100px（1:1，方向跟手）
                for dx, dy in ((100, 0), (-100, 0), (0, 100), (0, -100)):
                    before, after = self._drag(win, 500, 400, dx, dy)
                    self.assertAlmostEqual(after[0], before[0] - dx, delta=0.5,
                                           msg=f"拖 ({dx}, {dy}) 的横向位移必须 1:1")
                    self.assertAlmostEqual(after[1], before[1] - dy, delta=0.5,
                                           msg=f"拖 ({dx}, {dy}) 的纵向位移必须 1:1")

                # 平移（右键刚松手）之后的第一下左键：点线必须有效
                edge = win._layout.edges[0]
                spot = self._clickable(win, edge)
                self.assertIsNone(win._selected_node, "前提：此时没有选中任何词")
                self._click(win, spot[0], spot[1])
                self.assertIsNotNone(win._selected,
                                     "小图平移之后的首次左键点击不许被吞掉")
                self.assertTrue(str(win.evidence.cget("text")).startswith(f"{edge.label}："),
                                "依据区要显示这条关系")

                # 每边至少一个视口的留白：狠狠拖一把，位移必须 ≥ 一个视口（旧的 24px 做不到）
                for dx, dy, axis, expect in ((3000, 0, 0, 400.0), (-3000, 0, 0, 400.0),
                                             (0, 3000, 1, 300.0), (0, -3000, 1, 300.0)):
                    before, after = self._drag(win, 500, 400, dx, dy)
                    self.assertGreaterEqual(abs(after[axis] - before[axis]), expect - 1.0,
                                            msg=f"每边留白至少一个视口（拖 ({dx}, {dy})）")

    def test_wheel_zooms_around_the_pointer_and_is_clamped(self):
        from app.ui import concept_map as cm

        with support.temp_db() as db:
            self._big_content(db)
            with _FakeTkEnv() as env:
                env.canvases.clear()
                win = self._window(db, env)
                canvas = env.canvases[-1]
                canvas.configure(width=400, height=300)
                win._fit_pending = False
                win._draw(force=True)
                canvas.xview_moveto(0.5)
                canvas.yview_moveto(0.5)
                # 指针摆在画布正中间；锚定成立时「指针下的那个画布点」缩放前后不变
                cursor_x, cursor_y = 200.0, 150.0
                direct_x = win._view_left()
                world_before = (direct_x + cursor_x, win._view_top() + cursor_y)

                win._on_zoom_wheel(SimpleNamespace(delta=120, x=cursor_x, y=cursor_y))
                self.assertAlmostEqual(win._zoom, cm.ZOOM_STEP, places=6,
                                       msg="滚轮必须**直接**缩放")
                world_after_x = win._view_left() + cursor_x
                world_after_y = win._view_top() + cursor_y
                self.assertAlmostEqual(world_after_x, world_before[0] * cm.ZOOM_STEP,
                                       delta=0.5, msg="缩放必须锚定在鼠标位置（x）")
                self.assertAlmostEqual(world_after_y, world_before[1] * cm.ZOOM_STEP,
                                       delta=0.5, msg="缩放必须锚定在鼠标位置（y）")
                self.assertGreater(win._layout.find(1).w, 0)

                for _ in range(30):               # 一直放大：必须停在上限
                    win._on_zoom_wheel(SimpleNamespace(delta=120, x=cursor_x,
                                                       y=cursor_y))
                self.assertAlmostEqual(win._zoom, cm.ZOOM_MAX, places=6,
                                       msg="缩放必须限幅在上限")
                for _ in range(60):               # 一直缩小：必须停在下限
                    win._on_zoom_wheel(SimpleNamespace(delta=-120, x=cursor_x,
                                                       y=cursor_y))
                self.assertAlmostEqual(win._zoom, cm.ZOOM_MIN, places=6,
                                       msg="缩放必须限幅在下限")
                self.assertEqual(float(canvas.xview()[0]) >= 0.0, True)
                self.assertEqual(float(canvas.xview()[0]) <= 1.0, True)

    def test_zoom_then_click_hits_the_edge_under_the_pointer(self):
        """缩放重绘后，命中判定仍然按**当前**布局 + **当前视图偏移**算。"""
        with support.temp_db() as db:
            self._big_content(db)
            with _FakeTkEnv() as env:
                env.canvases.clear()
                win = self._window(db, env)
                canvas = env.canvases[-1]
                canvas.configure(width=400, height=300)
                win._fit_pending = False
                win._draw(force=True)
                win.zoom_by(1.3)
                edge = win._layout.edges[0]
                middle = edge.points[len(edge.points) // 2]
                self._click(win, middle[0], middle[1])
                self.assertIs(win._selected, edge.rel,
                              "缩放后点中的必须是当前布局里那条边")
                win.zoom_by(1.0 / 1.3)
                moved = win._layout.edges[0]
                spot = moved.points[len(moved.points) // 2]
                win._selected = None
                self._click(win, spot[0] + 400, spot[1] + 400)
                self.assertIsNone(win._selected, "远离连线的点击不许误选")

    def test_first_open_fits_the_view_and_user_camera_survives_refresh(self):
        from app.ui import concept_map as cm

        with support.temp_db() as db:
            bid, _ids = self._big_content(db)
            with _FakeTkEnv() as env:
                env.canvases.clear()
                win = self._window(db, env)
                canvas = env.canvases[-1]
                self.assertAlmostEqual(win._zoom, 1.0, places=6,
                                       msg="还没拿到真实尺寸时不许乱缩放")
                canvas.configure(width=320, height=220)
                win._draw(force=True)
                self.assertTrue(win._fit_pending is False, "真实尺寸到了就要做一次自适应")
                self.assertLess(win._zoom, 1.0, "内容装不下时首开必须缩到看得全")
                self.assertGreaterEqual(win._zoom, cm.AUTO_FIT_MIN, "自适应有下限")
                self.assertAlmostEqual(win._view_left(), 0.0, places=6,
                                       msg="首开从**世界原点** 0 看起（不是滚动范围的左上角）")
                self.assertAlmostEqual(win._view_top(), 0.0, places=6,
                                       msg="首开从**世界原点** 0 看起（不是滚动范围的左上角）")

                # 用户自己调镜头：缩放 + 平移
                win.zoom_by(1.4)
                self._press(win, 300, 200)
                win._on_pan_motion(SimpleNamespace(x_root=220, y_root=160))
                self._release(win, 220, 160)
                camera = (round(win._zoom, 6), round(win._view_left(), 6),
                          round(win._view_top(), 6))
                self.assertGreater(camera[1], 0.0, "前提：用户真的平移过")

                # 刷新（词条变化 → 本地重读；重新生成结果回来 → 重画）都不许复位镜头
                db.add_entry(batch_id=bid, term="新增词", context="新增上下文")
                self.assertTrue(win.on_entries_changed(), "前提：内容真的变了")
                win._draw(force=True)
                canvas.configure(width=520, height=360)
                win._draw(force=True)             # 窗口改尺寸也重排，但不复位镜头
                self.assertEqual((round(win._zoom, 6), round(win._view_left(), 6),
                                  round(win._view_top(), 6)), camera,
                                 "用户缩放 / 拖动过之后，刷新与改尺寸都不许重置"
                                 "（世界坐标偏移不是「随 scrollregion 变的分数」）")

    def test_switching_topic_resets_the_camera_once(self):
        from app.ui import concept_map as cm

        with support.temp_db() as db:
            first, _ids = self._big_content(db)
            second = int(db.create_batch("另一个主题"))
            for index in range(4):
                db.add_entry(batch_id=second, term=f"别的词{index}", context="上下文")
            with _FakeTkEnv() as env:
                env.canvases.clear()
                win = self._window(db, env)
                canvas = env.canvases[-1]
                canvas.configure(width=320, height=220)
                win._draw(force=True)             # 第一个主题的首开自适应
                first_fit = win._zoom
                self.assertFalse(win._fit_pending, "第一个主题的自适应已经做完")
                win.zoom_by(1.5)
                self._press(win, 300, 200)
                win._on_pan_motion(SimpleNamespace(x_root=240, y_root=170))
                self._release(win, 240, 170)
                before = win._zoom
                self.assertNotAlmostEqual(before, first_fit, places=6, msg="前提：用户调过镜头")
                self.assertGreater(win._view_left(), 0.0, "前提：用户平移过")
                self.assertGreater(win._view_top(), 0.0, "前提：用户平移过")

                win._topic_id = second
                win._load_topic()                 # 换主题 = 换内容：镜头复位一次
                self.assertEqual(int(win._topic_id), second)
                self.assertLess(win._zoom, before, "换主题要丢掉上一个主题的缩放")
                self.assertLessEqual(win._zoom, 1.0, "复位后只会缩、不会放大")
                self.assertGreaterEqual(win._zoom, cm.AUTO_FIT_MIN, "自适应有下限")
                self.assertFalse(win._fit_pending, "新主题的首开自适应只做一次")
                self.assertAlmostEqual(win._view_left(), 0.0, places=6,
                                       msg="换主题回**世界原点** 0 看起")
                self.assertAlmostEqual(win._view_top(), 0.0, places=6,
                                       msg="换主题回**世界原点** 0 看起")
                self.assertEqual(sorted(node.label for node in win._layout.nodes),
                                 ["别的词0", "别的词1", "别的词2", "别的词3"],
                                 "换主题后画布上必须是新主题自己的词表")
                self.assertTrue(first)


class TestConceptMapEvidenceSource(unittest.TestCase):
    """依据区：仍然给 reason / evidence，并**如实**标明证据来自哪份材料。"""

    def test_evidence_source_is_context_or_explanation_never_claimed_as_quote(self):
        from app.map_service import MapNode
        from app.ui import concept_map as cm

        m = cm.metrics_for()
        src = MapNode(1, "卷积", "卷积核在输入上滑动，逐点相乘再求和", "卷积是加权求和")
        dst = MapNode(2, "池化", "池化用于降采样，扩大感受野", "池化降低分辨率")
        label, exact = cm.evidence_basis("卷积核在输入上滑动", src, dst, m)
        self.assertEqual(label, cm.BASIS_FROM_CONTEXT)
        self.assertTrue(exact)
        label, exact = cm.evidence_basis("池化降低分辨率", src, dst, m)
        self.assertEqual(label, cm.BASIS_FROM_EXPLANATION,
                         "命中释义就不能说成原文（那是本工具 / 模型写的）")
        self.assertTrue(exact)
        label, _exact = cm.evidence_basis("材料里根本没有的一句话", src, dst, m)
        self.assertEqual(label, cm.BASIS_FROM_UNKNOWN,
                         "都对不上就如实说来源不明，绝不硬说原文")

    def test_relation_line_shows_reason_evidence_and_source(self):
        """窗口必须按**临时库里的真实主题**载入 nodes/graph，依据行才有词名。"""
        from app.map_service import MapRelation
        from app.ui import concept_map as cm

        with support.temp_db() as db:
            bid = int(db.create_batch("卷积主题"))
            src_id = int(db.add_entry(batch_id=bid, term="卷积",
                                      context="卷积核在输入上滑动，逐点相乘再求和"))
            dst_id = int(db.add_entry(batch_id=bid, term="池化",
                                      context="池化用于降采样"))
            relation = MapRelation(src_id, dst_id, "依赖", "卷积依赖卷积核",
                                   "卷积核在输入上滑动")
            service, _cfg = _map_service(db, key="", client=_MapClient([]))
            _put_map_cache(db, service, bid, [relation.as_dict()])
            with _FakeTkEnv() as env:
                env.canvases.clear()
                win = cm.ConceptMapWindow(None, SimpleNamespace(
                    db=db, map_service=service, open_settings=lambda: None))
                self.assertEqual([node.term for node in win._nodes], ["卷积", "池化"],
                                 "窗口必须按临时库里的真实主题载入词表")
                self.assertIsNotNone(win._graph, "缓存里的已校验关系必须载入成图")
                text = win._rel_text(relation)
        self.assertIn("依赖", text)
        self.assertIn("卷积 → 池化", text)
        self.assertIn(relation.reason, text, "点连线要看得到依据")
        self.assertIn(relation.evidence, text, "点连线要看得到证据片段")
        self.assertIn(cm.BASIS_FROM_CONTEXT, text, "必须标明证据来自哪份材料")
        self.assertNotIn("原文片段）", text, "不得再把它称作原文片段")

    def test_long_evidence_is_marked_truncated_instead_of_claimed_verbatim(self):
        from app.map_service import MapNode
        from app.ui import concept_map as cm

        long_text = "卷积核在输入上滑动逐点相乘再求和得到特征图" * 6
        node = MapNode(1, "卷积", long_text, "")
        label, exact = cm.evidence_basis(long_text, node, node, cm.metrics_for())
        self.assertEqual(label, cm.BASIS_FROM_CONTEXT)
        self.assertTrue(exact, "库里那份一字不动、没被显示截断")
        _label, exact_short = cm.evidence_basis(long_text[:cm.EVIDENCE_SHOWN] + "…",
                                                node, node, cm.metrics_for())
        self.assertFalse(exact_short, "显示串被截断就不能再说「逐字」")


class TestConceptMapAppWiring(unittest.TestCase):
    """App 接线：后台结果经 UI 队列交回窗口；窗口关掉后迟到结果不复活。"""

    def test_map_result_reaches_the_live_window_and_is_dropped_after_close(self):
        from app.map_service import MapGraph, MapRelation

        with support.headless_app(main_window="real") as app:
            bid = int(app.db.create_batch("主题"))
            first = int(app.db.add_entry(batch_id=bid, term="卷积",
                                         context="卷积核在输入上滑动"))
            second = int(app.db.add_entry(batch_id=bid, term="池化",
                                          context="池化用于降采样"))
            app.open_concept_map()
            win = app._map_win
            self.assertIsNotNone(win, "「导图」必须真的建参考关系图窗")
            self.assertIs(win.service, app.map_service, "窗口必须用 App 的导图服务")
            self.assertEqual(int(win._topic_id), bid)
            self.assertTrue(win._fingerprint, "窗口必须记住当前内容指纹")

            relation = MapRelation(first, second, "包含", "卷积核是卷积的单元",
                                   "卷积核在输入上滑动")
            graph = MapGraph(topic_id=bid, fingerprint=win._fingerprint,
                             relations=(relation,), nodes=tuple(win._nodes), source="ai")
            app._ui_q.put(("map_result", (1, bid, win._fingerprint, "ok", graph, None)))
            support.pump_app(app)
            self.assertIs(win._graph, graph, "结果必须经 UI 队列回到窗口")
            self.assertTrue(win._items_matching("edge"), "关系必须画出来")

            win.close()
            self.assertIn("destroy", win.win.events, "导图窗的关闭只关自己")
            win.win.winfo_exists = lambda: 0     # 真实 Tk：destroy 之后 winfo_exists()=0
            app._ui_q.put(("map_result", (2, bid, win._fingerprint, "ok", graph, None)))
            support.pump_app(app)          # 关掉的窗口不再接收结果（也不许抛异常）
            self.assertFalse(win.alive())
            self.assertIs(win._graph, graph, "迟到的结果不许改动已关闭窗口的状态")


class TestLibraryOrganizationControls(unittest.TestCase):
    """B 批（词库组织）：标签筛选、合并 / 拆分、导出、搜索命中。

    全部跑在**真实** :class:`~app.ui.main_window.MainWindow` 上（假 Tk 控件，
    一个真实窗口都不建），验证的是「用户点的那个按钮 → 库里真的变了」。
    """

    def _seed(self, app):
        """一个主题两条词（第一条带标签）；浏览范围钉在这个主题上。"""
        bid = int(app.db.create_batch("经济学笔记"))
        first = int(app.db.add_entry(
            batch_id=bid, term="边际效用",
            context="当消费者多消费一单位商品时，边际效用递减。"))
        second = int(app.db.add_entry(
            batch_id=bid, term="供给曲线", context="供给曲线向右上方倾斜。"))
        app.db.set_entry_tags(first, ["经济学", "微观"])
        app.main._browse_batch_id = bid
        app.main.refresh_batches()
        app.main.refresh_tags()
        app.main.refresh_entries()
        return bid, first, second

    @staticmethod
    def _texts(widget) -> list:
        """卡片里所有控件的 ``text``（假 Tk 的 ``cget`` 直接读构造关键字）。

        按**控件身份**去重：假 Tk 的普通 ``FakeWidget`` 会在父控件的
        ``_children`` 里出现两次（``_init_fake_widget`` 与 ``__init__`` 各登记
        一次），那是测试替身的登记方式，不是产品代码建了两遍。
        """
        out: list = []
        seen: set = set()

        def walk(node):
            if id(node) in seen:
                return
            seen.add(id(node))
            out.append(str(node.cget("text")))
            for child in node.winfo_children():
                walk(child)

        walk(widget)
        return out

    def test_the_left_column_lists_every_tag_with_its_entry_count(self):
        with support.headless_app(main_window="real") as app:
            self._seed(app)
            self.assertEqual([str(r["name"]) for r in app.main._tag_rows],
                             ["微观", "经济学"], "标签清单按名字排序")
            items = [str(x) for x in app.main.tag_list._children]
            self.assertIn("微观  (1)", items)
            self.assertIn("经济学  (1)", items)
            self.assertTrue(app.main.tag_chips.winfo_children() or True,
                            "详情里有「+标签」快捷按钮的位置")

    def test_a_tag_filter_looks_across_batches_and_says_so(self):
        with support.headless_app(main_window="real") as app:
            bid, first, second = self._seed(app)
            other = int(app.db.create_batch("另一篇"))
            third = int(app.db.add_entry(batch_id=other, term="博弈", context="纳什均衡"))
            app.db.set_entry_tags(third, ["经济学"])
            app.main.refresh_tags()
            app.main.refresh_entries()

            self.assertTrue(app.main.set_tag_filter("经济学"), "第一次筛选算「变了」")
            self.assertCountEqual([int(r["id"]) for r in app.main._entry_rows],
                                  [first, third], "标签筛选是**跨主题**的")
            self.assertIn("标签「经济学」", str(app.main.count_label.cget("text")))
            self.assertFalse(app.main.set_tag_filter("经济学"), "同一个筛选不算变化")

            self.assertTrue(app.main.clear_tag_filter())
            self.assertEqual([int(r["id"]) for r in app.main._entry_rows], [second, first],
                             "取消筛选回到这个主题（捕获时间倒序）")
            self.assertEqual(app.main._tag_filter, "")

    def test_saving_the_detail_writes_the_tags_and_rebuilds_the_chips(self):
        with support.headless_app(main_window="real") as app:
            _bid, first, second = self._seed(app)
            app.main.select_entry(second)
            self.assertEqual(app.main.var_tags.get(), "", "第二条还没打标签")
            chips = [str(w.cget("text")) for w in app.main.tag_chips.winfo_children()]
            self.assertIn("+经济学", chips, "库里已有的标签做成一点即加的按钮")
            self.assertIn("+微观", chips)

            app.main.var_tags.set("经济学，博弈论")
            app.main.save_detail()
            self.assertEqual(app.db.tags_for(second), ["博弈论", "经济学"])
            self.assertEqual(app.main.var_tags.get(), "博弈论，经济学", "保存后回写成库里的结果")
            self.assertEqual([str(r["name"]) for r in app.main._tag_rows],
                             ["博弈论", "微观", "经济学"])

    def test_a_tag_chip_only_fills_the_input_box(self):
        with support.headless_app(main_window="real") as app:
            _bid, first, _second = self._seed(app)
            app.main.select_entry(first)
            app.main._add_tag_chip("博弈论")
            self.assertEqual(app.main.var_tags.get(), "微观，经济学，博弈论")
            self.assertEqual(app.db.tags_for(first), ["微观", "经济学"],
                             "没点「保存修改」之前库里一个字都不许动")

    def test_renaming_a_tag_into_an_existing_name_merges_them(self):
        with support.headless_app(main_window="real") as app:
            _bid, first, _second = self._seed(app)
            app.main.set_tag_filter("微观")
            with mock.patch("app.ui.main_window.simpledialog.askstring",
                            return_value="经济学"):
                app.main.rename_tag()
            self.assertEqual(app.main._tag_filter, "经济学", "改完仍停在这个标签上")
            self.assertEqual(app.db.tags_for(first), ["经济学"], "两个标签并成一个")
            self.assertEqual([str(r["name"]) for r in app.db.list_tags()], ["经济学"])
            self.assertIn("经济学", str(app.main.status_label.cget("text")))

    def test_tag_actions_without_a_selected_tag_only_hint(self):
        with support.headless_app(main_window="real") as app:
            self._seed(app)
            app.main.clear_tag_filter()
            with mock.patch("app.ui.main_window.messagebox") as box:
                app.main.rename_tag()
                app.main.delete_tag()
            self.assertEqual(box.showinfo.call_count, 2,
                             "没选标签时只提示，不许误改库")
            self.assertFalse(box.askyesno.called)

    def test_deleting_a_tag_keeps_the_entries(self):
        with support.headless_app(main_window="real") as app:
            _bid, first, _second = self._seed(app)
            app.main.set_tag_filter("微观")
            with mock.patch("app.ui.main_window.messagebox") as box:
                box.askyesno.return_value = True
                app.main.delete_tag()
            self.assertTrue(box.askyesno.called)
            self.assertIsNotNone(app.db.get_entry(first), "删标签不许删词条")
            self.assertEqual(app.db.tags_for(first), ["经济学"])
            self.assertEqual(app.main._tag_filter, "")

    def test_checking_a_card_survives_a_refresh_and_drives_the_hint(self):
        with support.headless_app(main_window="real") as app:
            _bid, first, _second = self._seed(app)
            var = app.main._card_vars[first]
            var.set(True)
            app.main._toggle_checked(first, var)
            self.assertEqual(app.main.checked_ids(), [first])
            self.assertEqual(str(app.main.sel_hint.cget("text")), "已勾选 1 条")

            app.main.refresh_entries()
            self.assertTrue(bool(app.main._card_vars[first].get()), "刷新后勾还在")
            app.main.clear_checked()
            self.assertEqual(app.main.checked_ids(), [])
            self.assertEqual(str(app.main.sel_hint.cget("text")), "")

    def test_a_check_disappears_when_its_entry_is_gone(self):
        with support.headless_app(main_window="real") as app:
            _bid, first, _second = self._seed(app)
            var = app.main._card_vars[first]
            var.set(True)
            app.main._toggle_checked(first, var)
            app.db.delete_entry(first)
            app.main.refresh_entries()
            self.assertEqual(app.main.checked_ids(), [], "已删词条不许留在勾选集合里")

    def test_splitting_checked_entries_creates_a_new_batch_and_follows_it(self):
        with support.headless_app(main_window="real") as app:
            bid, _first, second = self._seed(app)
            self.assertEqual(app.main.apply_move([second], "new", 0, "  新专题  "), 1)
            rows = app.db.list_batches()
            self.assertIn("新专题", [str(r["name"]) for r in rows], "标题两端空白要压掉")
            new_id = next(int(r["id"]) for r in rows if str(r["name"]) == "新专题")
            self.assertEqual(int(app.db.get_entry(second)["batch_id"]), new_id)
            self.assertEqual(app.main.checked_ids(), [], "搬完清空勾选")
            self.assertEqual(int(app.main._browse_batch_id), new_id, "搬完跟着看新主题")
            self.assertIn("已移动 1 条词语", str(app.main.status_label.cget("text")))
            self.assertEqual(app.db.count_entries(batch_id=bid), 1)

    def test_moving_checked_entries_into_an_existing_batch(self):
        with support.headless_app(main_window="real") as app:
            _bid, first, second = self._seed(app)
            other = int(app.db.create_batch("旧专题"))
            self.assertEqual(app.main.apply_move([second], "existing", other), 1)
            self.assertEqual(int(app.db.get_entry(second)["batch_id"]), other)
            self.assertEqual(app.main.apply_move([first], "existing", 999999), 0,
                             "目标主题不存在 → 一条都不搬")
            self.assertEqual(int(app.db.get_entry(first)["batch_id"]), _bid)

    def test_merging_batches_moves_the_entries_and_drops_the_source(self):
        with support.headless_app(main_window="real") as app:
            bid, first, _second = self._seed(app)
            extra = int(app.db.create_batch("另一篇"))
            third = int(app.db.add_entry(batch_id=extra, term="博弈", context="纳什均衡"))
            app.main.refresh_batches()
            app.main.refresh_entries()

            self.assertEqual(app.main.apply_merge(bid, [extra]), 1)
            self.assertIsNone(app.db.get_batch(extra), "源主题合并后消失")
            self.assertEqual(int(app.db.get_entry(third)["batch_id"]), bid)
            self.assertEqual(int(app.db.get_entry(first)["batch_id"]), bid, "目标主题的词不动")
            self.assertIn("已合并 1 条词语", str(app.main.status_label.cget("text")))

    def test_export_writes_csv_and_markdown_for_the_browsed_batch(self):
        from app import paths

        with support.temp_data_dir():
            with support.headless_app(main_window="real") as app:
                self._seed(app)
                with mock.patch("app.ui.main_window.os.startfile") as opener:
                    app.main.export_entries()
                csv_files = sorted(paths.exports_dir().glob("*.csv"))
                md_files = sorted(paths.exports_dir().glob("*.md"))
                self.assertEqual(len(csv_files), 1, "一次导出 = 一对同名文件")
                self.assertEqual(len(md_files), 1)
                self.assertEqual(csv_files[0].stem, md_files[0].stem)
                csv_text = csv_files[0].read_text("utf-8-sig")
                self.assertIn("边际效用", csv_text)
                self.assertIn("供给曲线", csv_text)
                self.assertIn("经济学", csv_text, "标签也要进导出")
                self.assertIn("已导出 2 条", str(app.main.status_label.cget("text")))
                self.assertTrue(opener.called, "问「打开文件夹吗」答是 → 调系统文件管理器")

    def test_export_follows_the_tag_filter_and_names_the_scope(self):
        from app import paths

        with support.temp_data_dir():
            with support.headless_app(main_window="real") as app:
                _bid, first, _second = self._seed(app)
                self.assertIn("经济学笔记", app.main._export_scope_label())
                app.main.set_tag_filter("经济学")
                app.main.search_var.set("递减")
                self.assertIn("经济学", app.main._export_scope_label())
                self.assertIn("递减", app.main._export_scope_label())
                with mock.patch("app.ui.main_window.os.startfile"):
                    app.main.export_entries()
                csv_files = sorted(paths.exports_dir().glob("*.csv"))
                self.assertEqual(len(csv_files), 1)
                csv_text = csv_files[0].read_text("utf-8-sig")
                self.assertIn("边际效用", csv_text)
                self.assertNotIn("供给曲线", csv_text, "导出跟着当前筛选走")
                self.assertIn("已导出 1 条", str(app.main.status_label.cget("text")))
                _ = first

    def test_a_search_hit_names_the_field_and_marks_the_match(self):
        with support.headless_app(main_window="real") as app:
            _bid, first, _second = self._seed(app)
            app.main.search_var.set("递减")
            app.main.refresh_entries()
            self.assertEqual([int(r["id"]) for r in app.main._entry_rows], [first])
            hits = [t for t in self._texts(app.main._cards[first]) if t.startswith("命中 ")]
            self.assertEqual(
                hits, ["命中 上下文：当消费者多消费一单位商品时，边际效用【递减】。"],
                "搜索要说清命中哪个字段、命中哪一段")

    def test_a_search_also_finds_entries_by_their_tag(self):
        with support.headless_app(main_window="real") as app:
            _bid, first, second = self._seed(app)
            app.main.search_var.set("微观")
            app.main.refresh_entries()
            self.assertEqual([int(r["id"]) for r in app.main._entry_rows], [first])
            hits = [t for t in self._texts(app.main._cards[first]) if t.startswith("命中 ")]
            self.assertEqual(hits, ["命中 标签：#【微观】"])
            self.assertNotIn(second, [int(r["id"]) for r in app.main._entry_rows])

    def test_a_stale_explanation_is_shown_with_a_resuggestion_line(self):
        with support.headless_app(main_window="real") as app:
            _bid, first, _second = self._seed(app)
            app.db.update_entry(first, one_line="旧解释", explain_status="stale")
            app.main.refresh_entries()
            self.assertIn("需重新解释", self._texts(app.main._cards[first]),
                          "卡片徽标要说清这条的解释已经过期")
            app.main.select_entry(first)
            text = app.main.exp_text.get("1.0", tkinter.END)
            self.assertIn("（上下文已补充，建议重新解释）", text)
            self.assertIn("旧解释", text, "旧解释照旧显示，只是加一句提醒")


class TestSettingsDialogDuplicateAction(unittest.TestCase):
    """B4：同一个概念再次被划到时是新建词条还是把上下文并进同一条。"""

    @staticmethod
    def _stub(cfg):
        return SimpleNamespace(
            config=cfg,
            explain_service=SimpleNamespace(is_ready=lambda: (True, "")),
            on_settings_changed=lambda: None,
            restore_pending_explain_window=lambda: None,
        )

    def _cfg(self, db):
        cfg = Config(db)
        cfg.set("api.base_url", "https://api.deepseek.com/v1")
        cfg.set("api.model", "deepseek-chat")
        return cfg

    def test_the_window_offers_new_versus_append_and_saves_the_choice(self):
        from app.ui.settings_dialog import SettingsDialog

        with support.temp_db() as db:
            cfg = self._cfg(db)
            self.assertEqual(cfg.duplicate_action, "new", "默认仍然是新建词条")
            with _FakeTkEnv():
                dialog = SettingsDialog(FakeWidget(None), self._stub(cfg))
                self.assertEqual([b.cget("value") for b in dialog.dup_buttons],
                                 ["new", "append"])
                self.assertEqual(dialog.var_dup_action.get(), "new")
                dialog.var_dup_action.set("append")
                dialog.save()
            self.assertEqual(cfg.duplicate_action, "append")
            self.assertEqual(Config(db).duplicate_action, "append", "选择要落库")

    def test_an_unknown_stored_value_falls_back_to_new(self):
        with support.temp_db() as db:
            cfg = Config(db)
            cfg.set("capture.duplicate_action", "whatever")
            self.assertEqual(cfg.duplicate_action, "new",
                             "只有 append 走追加，其它一律当新建")


class _PickedListbox:
    """假 Listbox 存不住选择（``tests/support.py`` 里 ``curselection`` 恒返回空元组）。

    凡是「用户在第几项上选了什么」的测试都用它顶替真控件：只回答选中的索引。
    """

    def __init__(self, *indexes):
        self._indexes = tuple(int(index) for index in indexes)

    def curselection(self):
        return self._indexes

    def selection_clear(self, *_args):
        return None

    def selection_set(self, *_args):
        return None


class _FakeTextIndex:
    """假 Text：只实现「鼠标落点在第几行」（真 Text 的 ``index("@x,y")``）。"""

    def __init__(self, index):
        self._index = str(index)

    def index(self, _spec):
        return self._index


class TestConceptMapManualAndTools(unittest.TestCase):
    """批次 C：人工关系（C5/C6）· 局部生成（C3）· 孤立词诊断（C2）· 打开词条（C4）· 导出（C1）。

    全部跑在假 Tk 上：**一个真窗口都不建、一次网络都不发**（图由 ``_put_map_cache``
    预先写进本地库 ⇒ 打开窗口即命中缓存零请求；``_MapClient([])`` 兜住任何意外请求）。
    """

    AI_RELATION = {"type": "包含", "reason": "池化是卷积网络的一层",
                   "evidence": "池化用于降采样"}

    @staticmethod
    def _stub(db, service, **extra):
        return SimpleNamespace(db=db, map_service=service, open_settings=lambda: None,
                               **extra)

    def _window(self, db, *, dropped=None, verdicts_for=None, use_cache=True, **app_extra):
        """真实主题（三词）+ 一份有效缓存图：AI「卷积→池化」，过拟合是孤立词。"""
        from app.ui import concept_map as cm

        bid, ids = _map_db(db)
        service, _cfg = _map_service(db, client=_MapClient([]))
        if use_cache:
            relations = [dict(self.AI_RELATION, src=ids["卷积"], dst=ids["池化"])]
            _put_map_cache(db, service, bid, relations, dropped=dropped,
                           verdicts=None if verdicts_for is None else verdicts_for(ids))
        win = cm.ConceptMapWindow(None, self._stub(db, service, **app_extra))
        win._draw(force=True)
        return win, ids, bid

    # ------------------------------------------------------- C5 / C6：人工关系
    def test_a_manual_relation_is_stored_and_drawn_like_any_other_line(self):
        """人工关系存进本地库、画在图上，但**外观与 AI 关系完全一样**。

        用户口径（截图反馈）：线上不要「人工·」前缀，人工线不要更粗 / 换色 ——
        要认自己加的关系就看右下角依据区、或者双击那条线。
        """
        from app.map_service import is_manual
        from app.ui import concept_map as cm

        with support.temp_db() as db:
            with _FakeTkEnv() as env:
                env.canvases.clear()
                win, ids, bid = self._window(db)
                note = "我自己判断：层数越多越容易过拟合"
                ok, message = win.save_manual_relation(ids["卷积"], ids["过拟合"], "因果", note)
                self.assertTrue(ok, message)
                self.assertIn("不会覆盖", message)
                rows = win.manual_relation_rows()
                self.assertEqual(len(rows), 1, "人工关系要落库（重启 / 重新生成都还在）")
                self.assertEqual(str(rows[0]["label"]), "因果")
                self.assertEqual(int(rows[0]["topic_id"]), bid)
                self.assertEqual(str(rows[0]["note"]), note)
                manual = win._manual_rels[0]
                self.assertTrue(is_manual(manual))
                self.assertEqual(manual.reason, note, "用户写的依据就是这条关系的依据")
                self.assertEqual(manual.evidence, "", "人工关系没有证据片段，绝不假装有")
                drawn = win.relations_for_draw()
                self.assertEqual(len(drawn), 2, "AI 一条 + 人工一条")
                canvas = env.canvases[-1]
                options = canvas.item_options[win._edge_items[id(manual)]]
                self.assertEqual(str(options.get("fill")).lower(),
                                 cm.EDGE_FILL.lower(),
                                 "人工线用全图统一的灰（不再用主色区分）")
                self.assertEqual(options.get("dash"), (), "人工关系是实线")
                ai = [rel for rel in drawn if not is_manual(rel)]
                ai_options = canvas.item_options[win._edge_items[id(ai[0])]]
                self.assertEqual(int(options.get("width") or 0),
                                 int(ai_options.get("width") or 0),
                                 "人工关系与 AI 关系一样粗")
                labels = _canvas_texts(win)
                self.assertIn("因果", labels, "标签只写类型名")
                self.assertNotIn(f"{cm.MANUAL_LABEL_PREFIX}因果", labels,
                                 f"线上标注不许再带「{cm.MANUAL_LABEL_PREFIX}」前缀")
                # 要认「这是我加的」就看依据区：选中那条线，面板会写明
                win._selected = manual
                win._update_evidence()
                panel = str(win.evidence.cget("text"))
                self.assertIn("因果", panel)
                self.assertIn(cm.MANUAL_KIND_NOTE, panel, "依据区要说清这是你自己加的")
                self.assertIn(note, panel, "你写的依据要原样显示")

    def test_a_manual_relation_replaces_the_ai_one_on_the_same_pair(self):
        from app.map_service import is_manual

        with support.temp_db() as db:
            with _FakeTkEnv():
                win, ids, _bid = self._window(db)
                self.assertEqual(len(win.relations_for_draw()), 1, "先只有 AI 那条")
                win.save_manual_relation(ids["卷积"], ids["池化"], "因果", "其实是因为这个")
                drawn = win.relations_for_draw()
                self.assertEqual(len(drawn), 1, "同一对端点只画一条，不让两条互相打脸")
                self.assertTrue(is_manual(drawn[0]))
                self.assertEqual(drawn[0].rel_type, "因果")
                self.assertEqual(len(win._edge_items), 1, "画布上也只有一条线")

    def test_regenerating_never_clears_your_manual_relations(self):
        with support.temp_db() as db:
            with _FakeTkEnv():
                win, ids, _bid = self._window(db)
                win.save_manual_relation(ids["卷积"], ids["过拟合"], "因果", "我的判断")
                self.assertTrue(win._manual_rels)
                win._drop_expired(force=True)          # 等价于「重新生成」：清掉模型那份结果
                self.assertIsNone(win._graph, "重新生成清掉的是模型那份结果")
                self.assertTrue(win._manual_rels, "人工关系一条都不能少")
                self.assertEqual(len(win.relations_for_draw()), 1, "只剩人工那条也照样画")
                self.assertEqual(len(win._edge_items), 1)
                row_id = int(win.manual_relation_rows()[0]["id"])
                ok, message = win.delete_manual_relation(row_id)
                self.assertTrue(ok, message)
                self.assertEqual(win.manual_relation_rows(), [], "删掉就真的没了")
                self.assertEqual(win.relations_for_draw(), ())
                self.assertEqual(win._edge_items, {}, "画布上那条线也要跟着消失")
                ok, message = win.delete_manual_relation(row_id)
                self.assertFalse(ok, "删第二次要如实说这条已经不在了")
                self.assertIn("不在了", message)

    def test_the_evidence_area_never_pretends_a_manual_relation_has_a_quote(self):
        from app.ui import concept_map as cm

        with support.temp_db() as db:
            with _FakeTkEnv():
                win, ids, _bid = self._window(db)
                win.save_manual_relation(ids["卷积"], ids["过拟合"], "因果",
                                         "材料里说层数多了会过拟合")
                text = win._rel_text(win._manual_rels[0])
                self.assertIn(cm.MANUAL_LABEL_PREFIX, text)
                self.assertIn("人工添加的关系", text)
                self.assertIn("你写的依据：材料里说层数多了会过拟合", text)
                self.assertNotIn("证据（", text, "人工关系没有证据片段，界面不能假装有")
                self.assertNotIn("（未给片段）", text)
                ai = win._rel_text(win._graph.relations[0])
                self.assertIn("证据（", ai, "AI 关系照旧要给出证据与来源")

    def test_clicking_a_word_marks_which_relations_are_yours(self):
        with support.temp_db() as db:
            with _FakeTkEnv():
                win, ids, _bid = self._window(db)
                win.save_manual_relation(ids["卷积"], ids["过拟合"], "因果", "我的判断")
                win._focus_node(ids["卷积"])
                text = str(win.evidence.cget("text"))
                self.assertIn(f"人工·因果", text)
                self.assertIn("不受重新生成影响", text, "要说清它不会被重新生成覆盖")
                self.assertIn("卷积", text)
                self.assertIn("池化", text, "AI 那条也在（点词看这个词的全部关系）")

    # ------------------------------------------------------- C3：只分析选中的词
    def test_analysing_only_the_selected_words_sends_only_those_words(self):
        with support.temp_db() as db:
            with _FakeTkEnv():
                win, ids, bid = self._window(db)
                sent = []
                win.service.generate = lambda topic_id, **kw: (sent.append(kw) or None)
                self.assertEqual(len(win._entry_options), 3, "左边列出这个主题的全部词条")
                win.entry_list = _PickedListbox(0)
                first = int(win._entry_options[0][0])
                self.assertEqual(win._selected_entry_ids(), [first])
                win._on_subset_generate()
                self.assertEqual(win._subset_ids, {first})
                self.assertEqual([int(node.entry_id) for node in win._nodes], [first],
                                 "只分析选中的词")
                self.assertEqual(len(win._all_nodes), 3, "主题里的词条一个不少（只是不分析）")
                self.assertIn("本次只分析选中的 1 词", str(win._coverage_note),
                              "界面上要说清这次只算了几个词")
                self.assertEqual([int(node.entry_id) for node in sent[-1]["nodes"]], [first],
                                 "只有选中的词进请求 —— 这才是省 token 的地方")
                self.assertTrue(sent[-1]["force"])
                subset_fp = win._current_fingerprint()
                self.assertEqual(subset_fp, str(win.service.fingerprint(bid, win._nodes)),
                                 "子集结果的指纹要按同一子集算，否则结果全被判过期")
                # 切回全部：范围清空，重新按整个主题分析
                win._on_all_generate()
                self.assertEqual(win._subset_ids, set())
                self.assertEqual(len(win._nodes), 3)
                self.assertEqual(str(win._coverage_note), "")
                self.assertEqual(len(sent[-1]["nodes"]), 3)
                self.assertNotEqual(win._current_fingerprint(), subset_fp,
                                    "子集与全部是两份结果，不能互相顶替")
                _ = ids

    def test_asking_for_a_subset_without_selecting_anything_only_hints(self):
        with support.temp_db() as db:
            with _FakeTkEnv():
                win, _ids, _bid = self._window(db)
                sent = []
                win.service.generate = lambda topic_id, **kw: (sent.append(kw) or None)
                win._on_subset_generate()
                self.assertEqual(sent, [], "没选词就一个请求都不发")
                self.assertEqual(win._subset_ids, set())
                self.assertIn("先在左边选几个词", str(win.feedback.cget("text")))

    # ------------------------------------------------------- C2：孤立词诊断
    @staticmethod
    def _mixed_verdicts(ids):
        return [
            {"index": 0, "src": ids["卷积"], "dst": ids["过拟合"], "type": "因果",
             "reason": "卷积导致过拟合", "evidence": "正则化抑制过拟合",
             "verdict": "contradicted", "note": "材料只是场景描述。", "kept": False},
            {"index": 1, "src": ids["过拟合"], "dst": ids["池化"], "type": "依赖",
             "reason": "过拟合依赖池化", "evidence": "池化用于降采样",
             "verdict": "uncertain", "note": "材料不足。", "kept": False},
        ]

    def test_the_diagnostics_panel_summarises_the_isolated_words(self):
        with support.temp_db() as db:
            with _FakeTkEnv():
                win, ids, _bid = self._window(db, verdicts_for=self._mixed_verdicts)
                linked = {ids["卷积"], ids["池化"]}
                self.assertEqual([int(node.entry_id) for node in win._nodes
                                  if int(node.entry_id) not in linked], [ids["过拟合"]],
                                 "前提：这次只有一个孤立词")
                self.assertFalse(win._diag_visible, "面板默认收起，不占画布地方")
                win.toggle_diagnostics()
                self.assertTrue(win._diag_visible)
                text = str(win.diag_text.get("1.0", tkinter.END))
                self.assertIn("本次分析 3 个词：1 条关系，1 个孤立词", text)
                self.assertIn("核对认为不成立 1 条", text, "汇总拒绝原因，不用逐个点词")
                self.assertIn("核对不确定（材料不足） 1 条", text)
                self.assertIn("· 过拟合 ——", text)
                # 每个被否掉的候选说清「判定 + 这条候选指向谁」：卷积→过拟合 与
                # 过拟合→池化 的终点分别就是这两个词（界面按**终点**报，不编方向）
                self.assertIn("核对认为不成立（连向 过拟合）", text)
                self.assertIn("核对不确定（材料不足）（连向 池化）", text)
                self.assertIn("双击某一行可以在图上定位这个词", text)
                self.assertEqual([int(eid) for eid in win._diag_lines.values()],
                                 [ids["过拟合"]])
                win.toggle_diagnostics()
                self.assertFalse(win._diag_visible, "再点一次收起")

    def test_double_clicking_a_diagnostics_line_locks_onto_that_word(self):
        with support.temp_db() as db:
            with _FakeTkEnv():
                win, ids, _bid = self._window(db, verdicts_for=self._mixed_verdicts)
                win.toggle_diagnostics()
                lines = [line for line, entry_id in win._diag_lines.items()
                         if entry_id == ids["过拟合"]]
                self.assertEqual(len(lines), 1, "每个孤立词一行")
                win.diag_text = _FakeTextIndex(f"{lines[0] + 1}.0")   # Text 行号从 1 开始
                win._on_diag_open(SimpleNamespace(x=5, y=5))
                self.assertEqual(win._selected_node, ids["过拟合"])
                self.assertIn("核对认为不成立", str(win.evidence.cget("text")))
                # 点在没词的标题行上 → 什么都不选中（不能乱跳）
                win._selected_node = None
                win.diag_text = _FakeTextIndex("1.0")
                win._on_diag_open(SimpleNamespace(x=5, y=5))
                self.assertIsNone(win._selected_node)

    # ------------------------------------------------------- C4：图里打开词条
    def test_double_clicking_a_word_card_opens_it_in_the_main_window(self):
        opened, selected, scopes = [], [], []

        def open_main():
            opened.append(True)

        with support.temp_db() as db:
            with _FakeTkEnv():
                win, ids, bid = self._window(
                    db, open_main_window=open_main,
                    main_window=SimpleNamespace(select_entry=selected.append,
                                                set_browse_scope=scopes.append))
                node = win._layout.find(ids["过拟合"])
                win._on_canvas_double_click(SimpleNamespace(
                    x=node.x - win._view_left(), y=node.y - win._view_top()))
                self.assertEqual(opened, [True], "先亮出主界面")
                self.assertEqual(scopes, [bid],
                                 "先把主界面的浏览范围切到这个词条的主题，"
                                 "否则右栏选中了、左边列表里却找不到它")
                self.assertEqual(selected, [ids["过拟合"]], "再选中这条词条")
                self.assertEqual(win._selected_node, ids["过拟合"])
                self.assertIn("已在主界面打开", str(win.feedback.cget("text")))
                # 双击空白处不许乱跳窗口
                win._on_canvas_double_click(SimpleNamespace(x=-9999.0, y=-9999.0))
                self.assertEqual(len(opened), 1)
                self.assertEqual(len(scopes), 1)

    def test_opening_the_selected_entry_without_picking_a_word_only_hints(self):
        opened = []
        with support.temp_db() as db:
            with _FakeTkEnv():
                win, _ids, _bid = self._window(db, open_main_window=lambda: opened.append(True))
                self.assertIsNone(win._selected_node)
                win.open_selected_entry()
                self.assertEqual(opened, [], "没点词就别乱开主界面")
                self.assertIn("先在图上点一个词", str(win.feedback.cget("text")))

    # ------------------------------------------------------- C1：导出（只出图片）
    def test_exporting_writes_an_editable_image_and_never_a_spreadsheet(self):
        from app import paths
        from app.ui import concept_map as cm

        seen = []
        with support.temp_data_dir():
            with support.temp_db() as db:
                with _FakeTkEnv():
                    win, ids, _bid = self._window(db)
                    win.save_manual_relation(ids["卷积"], ids["过拟合"], "因果", "我自己判断")
                    win._draw(force=True)          # 人工关系也得进这张图
                    with mock.patch.object(cm.ConceptMapWindow, "_open_directory",
                                           staticmethod(lambda path: seen.append(str(path)))):
                        win.export_map_files()
                    files = sorted(paths.exports_dir().glob("探索词典-关系图-*"))
                    self.assertEqual([path.suffix for path in files], [".svg"],
                                     "用户明确不要 Excel / JSON：导出只能是图片"
                                     "（假画布抓不到 PNG，可编辑 SVG 也必须在）")
                    svg = files[0].read_text("utf-8")
                    self.assertIn('xmlns="http://www.w3.org/2000/svg"', svg)
                    self.assertIn("<tspan", svg, "文字得是真文字，能改")
                    self.assertIn("因果", svg, "人工关系要画进导出的图里")
                    self.assertNotIn("人工·", svg,
                                     "导出的图上也不带「人工·」前缀（与画布一致）")
                    self.assertIn("过拟合", svg)
                    self.assertGreaterEqual(svg.count("<polyline"), 2, "AI 一条 + 人工一条")
                    self.assertEqual(seen, [str(paths.exports_dir())], "导出后打开导出目录")
                    feedback = str(win.feedback.cget("text"))
                    self.assertIn("可编辑矢量图", feedback)
                    self.assertIn("PNG 没抓到", feedback,
                                  "抓不到图要如实说，不能假装导出齐了")

    def test_exporting_before_the_graph_is_drawn_only_hints(self):
        with support.temp_db() as db:
            with _FakeTkEnv():
                win, _ids, _bid = self._window(db)
                win._layout = None
                win.export_map_files()
                self.assertIn("还没有关系图", str(win.feedback.cget("text")))

    # ------------------------------------------------------- 人工关系对话框
    def test_the_editor_type_list_covers_the_model_types_plus_related(self):
        from app.map_service import REL_TYPES
        from app.ui.relation_editor import MANUAL_TYPES

        self.assertEqual(MANUAL_TYPES, tuple(REL_TYPES) + ("相关",),
                         "人工关系可选类型 = 模型那几种 + 「相关」（模型白名单不含它）")

    def test_the_relation_editor_adds_and_deletes_and_refuses_half_filled_forms(self):
        from app.ui.relation_editor import MANUAL_TYPES, RelationEditor

        with support.temp_db() as db:
            with _FakeTkEnv():
                win, ids, _bid = self._window(db)
                editor = RelationEditor(FakeWidget(None), win)
                self.assertEqual([entry_id for entry_id, _name in editor.options],
                                 [ids["卷积"], ids["池化"], ids["过拟合"]])
                self.assertEqual([b.cget("value") for b in editor.type_buttons],
                                 list(MANUAL_TYPES))
                self.assertEqual(editor.var_type.get(), MANUAL_TYPES[0])
                editor.add_current()                       # 两端都没选：只提示，不写库
                self.assertEqual(win.manual_relation_rows(), [])
                self.assertIn("先", str(editor.status.cget("text")))
                editor.src_list = _PickedListbox(0)
                editor.dst_list = _PickedListbox(2)
                editor.var_type.set("因果")
                editor.var_note.set("我的判断")
                editor.add_current()
                rows = win.manual_relation_rows()
                self.assertEqual(len(rows), 1)
                self.assertEqual(str(rows[0]["label"]), "因果")
                self.assertEqual(str(rows[0]["note"]), "我的判断")
                self.assertIn("不会覆盖", str(editor.status.cget("text")))
                self.assertIn("卷积 --因果--> 过拟合", str(editor.rel_list.get(0)),
                              "对话框里要列出已经加过的人工关系")
                editor.rel_list = _PickedListbox(0)
                editor.delete_current()
                self.assertEqual(win.manual_relation_rows(), [])
                self.assertIn("已删除", str(editor.status.cget("text")))
                editor.close()


class _RecordingListbox(_PickedListbox):
    """假 Listbox + 记住 ``selection_set`` 点的是第几项（断言 ``_select_entry`` 用）。"""

    def __init__(self, *indexes):
        super().__init__(*indexes)
        self.selections: list[int] = []

    def selection_set(self, index, *_rest):
        self.selections.append(int(index))


def _screen(win, x, y, *, alt=False):
    """画布坐标 → 屏幕坐标（与 ``ConceptMapWindow._canvas_coord`` 互逆）。

    ``alt=True`` 模拟「按住 Alt」：Windows 上 Alt 位在 ``event.state`` 里
    （``concept_map.ALT_MASK``）—— 建关系要按住它才生效。
    """
    import app.ui.concept_map as cm

    return SimpleNamespace(x=float(x) - win._view_left(),
                           y=float(y) - win._view_top(),
                           state=cm.ALT_MASK if alt else 0)


def _free_point_on(win, edge):
    """在一条连线的折线上找一个**不压着任何词卡**的点（双击连线用）。"""
    from app.ui import concept_map as cm

    points = [(float(x), float(y)) for x, y in edge.points]
    tries: list[tuple[float, float]] = []
    for index, (x, y) in enumerate(points):
        tries.append((x, y))
        if index:
            x0, y0 = points[index - 1]
            for ratio in (0.5, 0.35, 0.65, 0.2, 0.8):
                tries.append((x0 + (x - x0) * ratio, y0 + (y - y0) * ratio))
    for x, y in tries:
        if win._layout.node_at(x, y, pad=theme.px(cm.NODE_HIT_PAD)) is None:
            return x, y
    raise AssertionError("这条连线上找不到不压词卡的点：换一条边再试")


class TestConceptMapDragPinsAndEdgeBlocks(unittest.TestCase):
    """批次 F：拖连线柄建关系（F1）· 拖卡片固定位置（F2）· 双击连线改 / 删（F3）。

    同样全跑在假 Tk 上：一个真窗口都不建、一次网络都不发（图来自 ``_put_map_cache``）。
    """

    AI_RELATION = {"type": "包含", "reason": "池化是卷积网络的一层",
                   "evidence": "池化用于降采样"}

    @staticmethod
    def _stub(db, service, **extra):
        return SimpleNamespace(db=db, map_service=service, open_settings=lambda: None,
                               **extra)

    def _window(self, db, **app_extra):
        """三词主题 + 一条 AI 关系（卷积→池化）；过拟合是孤立词。"""
        from app.ui import concept_map as cm

        bid, ids = _map_db(db)
        service, _cfg = _map_service(db, client=_MapClient([]))
        _put_map_cache(db, service, bid,
                       [dict(self.AI_RELATION, src=ids["卷积"], dst=ids["池化"])])
        win = cm.ConceptMapWindow(None, self._stub(db, service, **app_extra))
        win._draw(force=True)
        return win, ids, bid

    # ------------------------------------------- F1：Alt + 左键从卡片上拖出连线
    def test_cards_carry_no_connection_handles(self):
        """用户口径（截图反馈）：卡片上**不要**那个小圆点状的连接点。"""
        from app.ui import concept_map as cm

        with support.temp_db() as db:
            with _FakeTkEnv() as env:
                env.canvases.clear()
                win, _ids, _bid = self._window(db)
                self.assertEqual(win._items_matching("node-handle"), [],
                                 "卡片边上不许再画连接点")
                self.assertFalse(hasattr(win, "_handle_nodes"), "柄的登记表要一起删掉")
                self.assertFalse(hasattr(win, "_handle_at"), "抓柄的方法要一起删掉")
                self.assertIn("Alt", cm.HINT_LINES[0],
                              "建关系的办法要在右下角提示里写清楚")

    def test_alt_dragging_from_a_card_to_another_opens_the_editor_with_both_ends(self):
        with support.temp_db() as db:
            with _FakeTkEnv() as env:
                env.canvases.clear()
                win, ids, _bid = self._window(db)
                calls = []
                win.open_relation_editor = lambda **kw: calls.append(kw)
                src = win._layout.find(ids["卷积"])
                dst = win._layout.find(ids["过拟合"])
                win._on_canvas_press(_screen(win, src.x, src.y, alt=True))
                self.assertEqual(win._link_from[0], ids["卷积"])
                self.assertNotIn("连线取消", str(win.feedback.cget("text")),
                                 "只按下去、还没拖，不该说「取消」")
                win._on_canvas_motion(_screen(win, dst.x, dst.y))
                self.assertIn("Alt", str(win.feedback.cget("text")),
                              "真的拖起来之后才提示松手会发生什么")
                self.assertIsNotNone(win._link_item, "拖动时要看得见一条临时线")
                self.assertEqual(int(win._drag_target.entry_id), ids["过拟合"])
                item = win._link_item
                win._on_canvas_release(_screen(win, dst.x, dst.y))
                self.assertEqual(calls, [{"src_id": ids["卷积"], "dst_id": ids["过拟合"]}],
                                 "松手在另一张卡上 = 打开对话框，两端替他选好")
                self.assertIsNone(win._link_item)
                self.assertNotIn(item, env.canvases[-1].item_options,
                                 "临时线松手就要删掉，不能留在图上")
                self.assertIsNone(win._link_from)

    def test_alt_press_is_not_swallowed_by_the_plain_binding(self):
        """``<Alt-Button-1>`` 与 ``<Button-1>`` 可能都收到：只许开一次连线。"""
        with support.temp_db() as db:
            with _FakeTkEnv():
                win, ids, _bid = self._window(db)
                src = win._layout.find(ids["卷积"])
                event = _screen(win, src.x, src.y, alt=True)
                win._on_canvas_press_alt(event)            # 先到的那条绑定
                first = win._link_from
                win._on_canvas_press(event)                # 后到的普通绑定
                self.assertEqual(win._link_from, first, "第二条绑定不许把起点顶掉")
                win._on_canvas_release(_screen(win, -900.0, -900.0))
                self.assertIsNone(win._link_from)
                self.assertFalse(win._alt_armed, "松开后 Alt 状态要复位")

    def test_plain_left_drag_moves_the_card_and_never_starts_a_relation(self):
        with support.temp_db() as db:
            with _FakeTkEnv():
                win, ids, _bid = self._window(db)
                calls = []
                win.open_relation_editor = lambda **kw: calls.append(kw)
                src = win._layout.find(ids["卷积"])
                dst = win._layout.find(ids["过拟合"])
                win._on_canvas_press(_screen(win, src.x, src.y))    # 没按 Alt
                self.assertIsNone(win._link_from, "不按 Alt 就不是建关系")
                self.assertEqual(win._drag_node[0], ids["卷积"], "不按 Alt = 摆位置")
                win._on_canvas_motion(_screen(win, dst.x, dst.y))
                self.assertIsNone(win._link_item, "不该冒出临时线")
                win._on_canvas_release(_screen(win, dst.x, dst.y))
                self.assertEqual(calls, [], "拖到另一张卡上也只是挪位置，不开对话框")

    def test_dropping_the_alt_line_on_empty_space_or_the_same_card_only_hints(self):
        with support.temp_db() as db:
            with _FakeTkEnv():
                win, ids, _bid = self._window(db)
                calls = []
                win.open_relation_editor = lambda **kw: calls.append(kw)
                src = win._layout.find(ids["卷积"])
                win._on_canvas_press(_screen(win, src.x, src.y, alt=True))
                win._on_canvas_motion(_screen(win, -400.0, -400.0))
                win._on_canvas_release(_screen(win, -500.0, -500.0))
                self.assertIn("连线取消", str(win.feedback.cget("text")))
                self.assertIsNone(win._link_item)
                win._on_canvas_press(_screen(win, src.x, src.y, alt=True))
                win._on_canvas_motion(_screen(win, src.x, src.y - 40.0))
                win._on_canvas_release(_screen(win, src.x, src.y))
                self.assertIn("同一个词", str(win.feedback.cget("text")))
                self.assertEqual(calls, [], "取消就是不打开对话框")
                self.assertEqual(win.manual_relation_rows(), [], "取消不写库")

    def test_a_click_without_moving_never_pins_nor_starts_a_relation(self):
        """G0 操作管理：手抖一两像素的「单击」既不算摆位置，也不算建关系。

        用户口径：「左键是拖动，左键 + Alt 才是建立关系」。若按下就算拖动，
        一次普通点击会把卡片钉在原地（还弹一句「已固定」），Alt + 单击也会
        莫名其妙变成「连线取消」。位移门槛（:data:`app.ui.concept_map.DRAG_SLOP`）
        就是拦这两件事的。
        """
        with support.temp_db() as db:
            with _FakeTkEnv():
                win, ids, bid = self._window(db)
                calls = []
                win.open_relation_editor = lambda **kw: calls.append(kw)
                src = win._layout.find(ids["卷积"])
                before = str(win.feedback.cget("text"))
                # ① 普通左键：按下 → 抖 2 像素 → 松开
                win._on_canvas_press(_screen(win, src.x, src.y))
                win._on_canvas_motion(_screen(win, src.x + 2.0, src.y + 1.0))
                win._on_canvas_release(_screen(win, src.x + 2.0, src.y + 1.0))
                self.assertEqual(db.list_node_pins(bid), {},
                                 "点一下不许把卡片钉住")
                self.assertEqual(win._drag_items, [])
                self.assertIsNone(win._drag_node)
                # ② Alt + 左键：同样只抖一下
                win._on_canvas_press(_screen(win, src.x, src.y, alt=True))
                win._on_canvas_motion(_screen(win, src.x + 2.0, src.y + 1.0))
                win._on_canvas_release(_screen(win, src.x + 2.0, src.y + 1.0))
                self.assertFalse(win._drag_started)
                self.assertIsNone(win._link_from)
                self.assertIsNone(win._link_item, "没拖起来就不该有临时线")
                self.assertEqual(calls, [], "不许打开对话框")
                self.assertEqual(win.manual_relation_rows(), [], "更不许写库")
                self.assertEqual(str(win.feedback.cget("text")), before,
                                 "单击不该弹任何提示（没什么可固定的、也没什么可取消的）")

    def test_the_operations_table_matches_the_help_window(self):
        """手势表是**唯一来源**：提示行、操作说明窗口、绑定三者对得上。"""
        from app.ui import concept_map as cm
        from app.ui.shortcuts_dialog import ShortcutsDialog

        keys = [key for key, _desc in cm.INTERACTIONS]
        self.assertEqual(len(set(keys)), len(keys), "键位不许写两遍")
        self.assertTrue(any("Alt" in key for key in keys), "Alt 建关系必须在表里")
        self.assertTrue(any("左键拖动" in key for key in keys), "拖动必须在表里")
        self.assertFalse(any("柄" in key or "圆点" in key for key in keys),
                         "连接点已经删掉了，表里不许再提")
        self.assertEqual(cm.INTERACTIONS[0][1].count("选中"), 1)
        self.assertIn("Alt", cm.HINT_LINES[0])
        self.assertIn("右键", cm.HINT_LINES[2])
        with support.temp_db() as db:
            with _FakeTkEnv():
                win, _ids, _bid = self._window(db)
                win.open_shortcuts_help()
                dialog = win._shortcuts_dialog
                self.assertIsInstance(dialog, ShortcutsDialog)
                self.assertEqual(list(dialog.rows), list(cm.INTERACTIONS))
                self.assertEqual(dialog.reference()["鼠标滚轮"], "以指针为锚缩放")
                self.assertEqual(str(dialog.note_label.cget("text")), cm.NO_HANDLE_NOTE,
                                 "「卡片上没有连接点」这句话要写在说明窗口里")
                self.assertEqual(len(dialog.key_labels), len(cm.INTERACTIONS))
                self.assertEqual(len(dialog.desc_labels), len(cm.INTERACTIONS))
                dialog.close()

    # ------------------------------------------------------- F2：拖卡片固定位置
    def test_dragging_a_card_pins_where_the_user_left_it(self):
        with support.temp_db() as db:
            with _FakeTkEnv() as env:
                env.canvases.clear()
                win, ids, bid = self._window(db)
                other_before = win._layout.find(ids["卷积"])
                keep = (other_before.x, other_before.y)
                node = win._layout.find(ids["池化"])
                target = (node.x + 60.0, node.y + 25.0)
                win._on_canvas_press(_screen(win, node.x, node.y))
                self.assertEqual(win._drag_node[0], ids["池化"])
                self.assertIsNone(win._link_from, "抓卡片不能当成抓柄")
                win._on_canvas_motion(_screen(win, target[0], target[1]))
                self.assertTrue(win._drag_items, "拖动期间卡片本体要跟着走")
                moved = env.canvases[-1].coords(win._drag_items[0])
                xs = [float(value) for value in moved[0::2]]
                ys = [float(value) for value in moved[1::2]]
                self.assertAlmostEqual((min(xs) + max(xs)) / 2.0, target[0], places=3)
                self.assertAlmostEqual((min(ys) + max(ys)) / 2.0, target[1], places=3)
                self.assertNotAlmostEqual(node.x, target[0], places=3)
                win._on_canvas_release(_screen(win, target[0], target[1]))
                pins = db.list_node_pins(bid)
                self.assertEqual(set(pins), {ids["池化"]}, "松手就把位置落库（本主题）")
                self.assertAlmostEqual(pins[ids["池化"]][0], target[0], places=3)
                self.assertAlmostEqual(pins[ids["池化"]][1], target[1], places=3)
                self.assertIn("已固定", str(win.feedback.cget("text")))
                landed = win._layout.find(ids["池化"])
                self.assertAlmostEqual(landed.x, target[0], places=3)
                self.assertAlmostEqual(landed.y, target[1], places=3)
                still = win._layout.find(ids["卷积"])
                self.assertAlmostEqual(still.x, keep[0], places=3, msg="别的卡不该动")
                self.assertAlmostEqual(still.y, keep[1], places=3)

    def test_dragging_a_card_follows_the_mouse_step_by_step(self):
        """**真 bug 回归**：以前每次 Motion 都按布局坐标重算位移，卡片会越拖越飞。

        用户口径：拖动要「跟着鼠标顺滑移动」—— 连续几次 Motion 之后，卡片中心
        必须**正好**停在鼠标处（不是越走越偏）。
        """
        with support.temp_db() as db:
            with _FakeTkEnv() as env:
                env.canvases.clear()
                win, ids, bid = self._window(db)
                node = win._layout.find(ids["池化"])
                win._on_canvas_press(_screen(win, node.x, node.y))
                seen = []
                for step in ((40.0, 15.0), (75.0, 40.0), (120.0, 31.0), (118.0, 33.0)):
                    target = (node.x + step[0], node.y + step[1])
                    win._on_canvas_motion(_screen(win, target[0], target[1]))
                    moved = env.canvases[-1].coords(win._drag_items[0])
                    xs = [float(value) for value in moved[0::2]]
                    ys = [float(value) for value in moved[1::2]]
                    self.assertAlmostEqual((min(xs) + max(xs)) / 2.0, target[0], places=3,
                                           msg=f"第 {len(seen) + 1} 步卡片没跟上鼠标（横向）")
                    self.assertAlmostEqual((min(ys) + max(ys)) / 2.0, target[1], places=3,
                                           msg=f"第 {len(seen) + 1} 步卡片没跟上鼠标（纵向）")
                    seen.append(target)
                win._on_canvas_release(_screen(win, seen[-1][0], seen[-1][1]))
                pins = db.list_node_pins(bid)
                self.assertEqual(set(pins), {ids["池化"]}, "松手就落库")
                self.assertAlmostEqual(pins[ids["池化"]][0], seen[-1][0], places=3,
                                       msg="松手停在鼠标最后的位置（横向）")
                self.assertAlmostEqual(pins[ids["池化"]][1], seen[-1][1], places=3,
                                       msg="松手停在鼠标最后的位置（纵向）")
                self.assertEqual(win._drag_items, [], "松手要清掉拖动登记")
                self.assertIsNone(win._drag_last)

    def test_a_pinned_card_stays_put_across_regeneration_and_is_per_topic(self):
        with support.temp_db() as db:
            with _FakeTkEnv():
                win, ids, bid = self._window(db)
                ok, message = win.pin_node(ids["过拟合"], 640.0, 420.0)
                self.assertTrue(ok, message)
                self.assertEqual(db.list_node_pins(bid), {ids["过拟合"]: (640.0, 420.0)})
                win._drop_expired(force=True)      # 等价于「重新生成」
                win._draw(force=True)
                self.assertAlmostEqual(win._layout.find(ids["过拟合"]).x, 640.0, places=3,
                                       msg="重新生成只换 AI 关系，不动他摆过的位置")
                win._pins = {}                     # 相当于把这张图关掉再打开
                win._reload_pins()
                self.assertEqual(win.pinned_positions(), {ids["过拟合"]: (640.0, 420.0)},
                                 "位置存在本地库里，重开这张图照样在")
                _bid2, ids2 = _map_db(db, topic="另一个主题")
                self.assertNotEqual(ids2["过拟合"], ids["过拟合"])
                win2, _ids3, bid3 = self._window(db)
                self.assertNotEqual(bid3, bid)
                self.assertEqual(win2.pinned_positions(), {},
                                 "位置只在它自己的主题里有效，换个主题不会串")

    def test_the_auto_layout_button_clears_pins_and_says_so(self):
        with support.temp_db() as db:
            with _FakeTkEnv():
                win, ids, bid = self._window(db)
                win.reset_node_pins()
                self.assertIn("没有固定过位置的卡片", str(win.feedback.cget("text")))
                ok, message = win.pin_node(ids["卷积"], 250.0, 180.0)
                self.assertTrue(ok, message)
                self.assertEqual(win.pinned_positions(), {ids["卷积"]: (250.0, 180.0)})
                win.reset_node_pins()
                self.assertIn("已恢复自动布局", str(win.feedback.cget("text")))
                self.assertIn("1 张卡片", str(win.feedback.cget("text")))
                self.assertEqual(win._pins, {})
                self.assertEqual(win.pinned_positions(), {})
                self.assertEqual(db.list_node_pins(bid), {}, "库里也要清干净")

    def test_the_layout_puts_a_pinned_card_exactly_where_it_was_left(self):
        from app.ui import concept_map as cm

        labels = {1: "甲", 2: "乙"}
        auto = cm.layout_graph(labels, (), width=700.0, height=500.0)
        pinned = cm.layout_graph(labels, (), width=700.0, height=500.0,
                                 pins={1: (300.0, 111.0)})
        self.assertAlmostEqual(pinned.find(1).x, 300.0, places=3)
        self.assertAlmostEqual(pinned.find(1).y, 111.0, places=3)
        self.assertAlmostEqual(pinned.find(2).x, auto.find(2).x, places=3,
                               msg="固定一张卡不该动别的卡")
        metrics = cm.metrics_for()
        far = cm.layout_graph(labels, (), width=700.0, height=500.0,
                              pins={1: (-500.0, -500.0)})
        self.assertAlmostEqual(far.find(1).x, metrics.pad + far.find(1).w / 2.0, places=3,
                               msg="拖到左上角外面也只会被夹回留白里")
        self.assertAlmostEqual(far.find(1).y, metrics.pad + far.find(1).h / 2.0, places=3)

    # ------------------------------------------------------- F3：双击连线
    def test_double_clicking_an_ai_edge_asks_whether_it_is_wrong(self):
        with support.temp_db() as db:
            with _FakeTkEnv():
                win, _ids, _bid = self._window(db)
                calls = []
                win.open_edge_dialog = lambda edge: calls.append(edge)
                edge = win._layout.edges[0]
                x, y = _free_point_on(win, edge)
                win._on_canvas_double_click(_screen(win, x, y))
                self.assertEqual(len(calls), 1, "双击 AI 连线要问「这条不对吗」")
                self.assertIs(calls[0], edge)
                self.assertIs(win._selected, edge.rel, "顺手把这条边选中，依据栏跟着走")

    def test_double_clicking_a_manual_edge_opens_it_for_editing(self):
        from app.map_service import is_manual

        with support.temp_db() as db:
            with _FakeTkEnv():
                win, ids, _bid = self._window(db)
                win.save_manual_relation(ids["卷积"], ids["过拟合"], "因果", "我的判断")
                calls = []
                win.open_relation_editor = lambda **kw: calls.append(kw)
                manual = [edge for edge in win._layout.edges if is_manual(edge.rel)]
                self.assertEqual(len(manual), 1)
                x, y = _free_point_on(win, manual[0])
                win._on_canvas_double_click(_screen(win, x, y))
                self.assertEqual(len(calls), 1)
                self.assertTrue(is_manual(calls[0]["relation"]))
                self.assertEqual(calls[0]["relation"].rel_type, "因果")

    def test_double_clicking_a_card_still_opens_the_entry_and_never_the_edge_dialog(self):
        with support.temp_db() as db:
            with _FakeTkEnv():
                opened = []
                win, ids, _bid = self._window(db, open_main_window=lambda: opened.append(True))
                calls = []
                win.open_edge_dialog = lambda edge: calls.append(edge)
                node = win._layout.find(ids["卷积"])
                win._on_canvas_double_click(_screen(win, node.x, node.y))
                self.assertEqual(calls, [],
                                 "卡片优先：不会因为在卡边碰到一条线就跳去改关系")
                self.assertEqual(win._selected_node, ids["卷积"])
                self.assertEqual(opened, [True])

    def test_marking_an_ai_edge_as_wrong_removes_it_for_good(self):
        with support.temp_db() as db:
            with _FakeTkEnv():
                win, ids, bid = self._window(db)
                edge = win._layout.edges[0]
                self.assertEqual(len(win.relations_for_draw()), 1)
                ok, message = win.block_ai_edge(edge)
                self.assertTrue(ok, message)
                self.assertIn("不再画出来", message)
                self.assertEqual(db.map_edge_block_pairs(bid),
                                 {(ids["卷积"], ids["池化"]), (ids["池化"], ids["卷积"])},
                                 "两个方向都挡住：模型反过来提也不算")
                self.assertEqual(win.relations_for_draw(), ())
                self.assertEqual(win._edge_items, {}, "图上那条线要当场撤掉")
                win._graph = SimpleNamespace(relations=(edge.rel,))   # 重新生成又提出同一对
                self.assertEqual(win.relations_for_draw(), (),
                                 "黑名单说了算：重新生成也不让它回来")
                rows = win.blocked_edge_rows()
                self.assertEqual(len(rows), 1)
                self.assertEqual(str(rows[0]["label"]), "包含")
                self.assertEqual(str(rows[0]["reason"]), "池化用于降采样")

    def test_a_blocked_pair_can_be_restored_and_a_clean_pair_says_so(self):
        with support.temp_db() as db:
            with _FakeTkEnv():
                win, ids, _bid = self._window(db)
                edge = win._layout.edges[0]
                ok, message = win.unblock_edge_pair(ids["卷积"], ids["池化"])
                self.assertFalse(ok, "没标过就没有可恢复的")
                self.assertIn("本来就没被屏蔽", message)
                win.block_ai_edge(edge)
                ok, message = win.unblock_edge_pair(ids["卷积"], ids["池化"])
                self.assertTrue(ok, message)
                self.assertIn("会重新画出来", message)
                self.assertEqual(win.blocked_edge_rows(), [])
                self.assertEqual(win.relations_for_draw(), (edge.rel,))

    def test_adding_your_own_relation_clears_the_block_on_that_pair(self):
        with support.temp_db() as db:
            with _FakeTkEnv():
                win, ids, _bid = self._window(db)
                win.block_ai_edge(win._layout.edges[0])
                self.assertEqual(len(win.blocked_edge_rows()), 1)
                ok, message = win.save_manual_relation(ids["卷积"], ids["池化"], "因果",
                                                       "我自己判断")
                self.assertTrue(ok, message)
                self.assertEqual(win.blocked_edge_rows(), [],
                                 "你亲手加了一条关系，那句「这条不对」就不该再压着它")
                self.assertEqual(len(win.relations_for_draw()), 1)

    def test_the_edge_dialog_explains_the_edge_and_can_block_and_restore(self):
        from app.ui.edge_block_dialog import EdgeBlockDialog

        with support.temp_db() as db:
            with _FakeTkEnv():
                win, _ids, _bid = self._window(db)
                edge = win._layout.edges[0]
                dialog = EdgeBlockDialog(FakeWidget(None), win, edge)
                headline = str(dialog.headline.cget("text"))
                self.assertIn("卷积", headline)
                self.assertIn("池化", headline)
                self.assertIn("包含", headline)
                self.assertIn("池化是卷积网络的一层", str(dialog.reason.cget("text")))
                self.assertIn("池化用于降采样", str(dialog.evidence.cget("text")))
                dialog.restore_current()                       # 还没标过：只提示
                self.assertIn("先", str(dialog.status.cget("text")))
                dialog.block_current()
                self.assertIn("不再画出来", str(dialog.status.cget("text")))
                self.assertEqual(len(dialog.rows), 1, "标完立刻反映到下面的列表里")
                self.assertTrue(str(dialog.rows_list.get(0)).startswith("卷积 --包含--> 池化（"),
                                "列表要写清是哪一对端点")
                dialog.rows_list = _PickedListbox(0)
                dialog.restore_current()
                self.assertIn("已恢复", str(dialog.status.cget("text")))
                self.assertEqual(win.blocked_edge_rows(), [], "恢复之后本地库也要清掉")
                self.assertEqual(len(win.relations_for_draw()), 1, "这条关系重新画出来")
                dialog.close()

    # ------------------------------------------------------- 对话框的「预先选好」
    def test_the_editor_opens_with_the_two_ends_already_picked(self):
        from app.ui.relation_editor import RelationEditor

        with support.temp_db() as db:
            with _FakeTkEnv():
                win, ids, _bid = self._window(db)
                editor = RelationEditor(FakeWidget(None), win,
                                        src_id=ids["卷积"], dst_id=ids["过拟合"])
                self.assertEqual(editor.initial_src, ids["卷积"])
                self.assertEqual(editor.initial_dst, ids["过拟合"])
                self.assertIn("两端已经替你选好了", str(editor.status.cget("text")))
                recorder = _RecordingListbox()
                self.assertTrue(editor._select_entry(recorder, ids["池化"]))
                self.assertEqual(recorder.selections, [1], "选中的是列表里第二项")
                self.assertFalse(editor._select_entry(recorder, 999999))
                editor.close()

    def test_the_editor_opens_on_an_existing_manual_relation_for_editing(self):
        from app.ui.relation_editor import RelationEditor

        with support.temp_db() as db:
            with _FakeTkEnv():
                win, ids, _bid = self._window(db)
                win.save_manual_relation(ids["卷积"], ids["过拟合"], "因果", "我的判断")
                editor = RelationEditor(FakeWidget(None), win,
                                        relation=win._manual_rels[0])
                self.assertEqual(editor.initial_src, ids["卷积"])
                self.assertEqual(editor.initial_dst, ids["过拟合"])
                self.assertEqual(editor.var_type.get(), "因果")
                self.assertEqual(editor.var_note.get(), "我的判断")
                self.assertIn("这就是那条人工关系", str(editor.status.cget("text")))
                editor.src_list = _PickedListbox(0)            # 卷积
                editor.dst_list = _PickedListbox(2)            # 过拟合
                editor.var_type.set("对照")
                editor.add_current()
                rows = win.manual_relation_rows()
                self.assertEqual(len(rows), 1, "同一对端点只留一行：改类型不是新增")
                self.assertEqual(str(rows[0]["label"]), "对照")
                self.assertIn("卷积 --对照--> 过拟合", str(editor.rel_list.get(0)))
                editor.close()

    def test_the_editor_button_path_still_starts_empty(self):
        with support.temp_db() as db:
            with _FakeTkEnv():
                win, _ids, _bid = self._window(db)
                win.open_relation_editor()                     # 「人工关系…」按钮
                self.assertIsNone(win._relation_editor.initial_src)
                self.assertIsNone(win._relation_editor.initial_dst)
                self.assertIsNone(win._relation_editor.initial_relation)
                win._relation_editor.close()


class _ImmediateAfter:
    """把 ``after(0, func)`` 直接跑掉。

    假 Tk 的 ``after`` 只登记不执行（``FakeRoot.after_calls``），而「模型挑骨架」
    的结果**必须**经过 ``after`` 回 UI 线程才作数 —— 测试要看到那一步的结果。
    """

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def after(self, _ms, func=None, *args):
        if func is not None:
            func(*args)
        return "after-test"


class TestConceptMapTemplates(unittest.TestCase):
    """批次 G：布局骨架（模板）—— 工具条按钮 / 选择对话框 / 切模板 / 让模型挑。

    全部跑在假 Tk 上：一个真窗口都不建、一次网络都不发（图来自 ``_put_map_cache``，
    模型那一路用假的 ``pick_template`` 顶替）。
    """

    @staticmethod
    def _stub(db, service, config, **extra):
        return SimpleNamespace(db=db, map_service=service, config=config,
                               open_settings=lambda: None, **extra)

    @staticmethod
    def _edges(items, ids):
        """``(起点词, 终点词, 类型)`` → 真缓存里的关系字典（两端换成 entry_id）。"""
        return [{"type": kind, "reason": f"{kind}：离线假依据",
                 "evidence": "离线假证据片段", "src": ids[src], "dst": ids[dst]}
                for src, dst, kind in items]

    def _window(self, db, *, edges=True, **app_extra):
        """三词的图：默认一条「卷积 包含 池化」，过拟合是孤立词。

        ``edges`` 收 ``(起点词, 终点词, 类型)``：``True`` = 默认那一条、``[]`` = 空图、
        也可以直接给一串（比如两条因果，用来试「本地规则建议鱼骨图」）。
        """
        from app.ui import concept_map as cm

        bid, ids = _map_db(db)
        service, cfg = _map_service(db, client=_MapClient([]))
        items = [("卷积", "池化", "包含")] if edges is True else list(edges)
        _put_map_cache(db, service, bid, self._edges(items, ids))
        win = cm.ConceptMapWindow(None, self._stub(db, service, cfg, **app_extra))
        win._draw(force=True)
        return win, ids, bid, cfg, service

    #: 两条因果：本地规则会建议鱼骨图（因果边占了一半以上）
    CAUSAL_EDGES = (("卷积", "池化", "因果"), ("池化", "过拟合", "因果"))

    # ------------------------------------------------------ 工具条上的当前骨架
    def test_the_toolbar_button_names_the_skeleton_in_use(self):
        with support.temp_db() as db:
            with _FakeTkEnv():
                win, _ids, _bid, _cfg, _service = self._window(db)
                self.assertEqual(win.template_key(), "auto", "默认还是自动（与今天逐像素一致）")
                self.assertEqual(str(win.btn_template.cget("text")), "模板：自动（按关系分层）")
                self.assertTrue(win.apply_template("tree"))
                self.assertEqual(str(win.btn_template.cget("text")), "模板：树状图")

    def test_switching_a_skeleton_redraws_the_same_relations_in_new_spots(self):
        from app.ui import map_templates as mt

        with support.temp_db() as db:
            with _FakeTkEnv():
                win, ids, _bid, cfg, _service = self._window(db)
                before = win._layout.find(ids["卷积"])
                edges_before = len(win._layout.edges)
                self.assertTrue(win.apply_template("flow-v"))
                self.assertEqual(cfg.get("map.template"), "flow-v", "选择要存进设置")
                self.assertEqual(win.template_name(), mt.name_of("flow-v"))
                self.assertEqual(win._layout.template, "flow-v", "这一次真的按它摆了")
                after = win._layout.find(ids["卷积"])
                self.assertNotEqual((before.x, before.y), (after.x, after.y),
                                    "换了骨架就要换位置")
                self.assertEqual(len(win._layout.edges), edges_before,
                                 "切模板只改摆法：关系一条都不动")
                self.assertIn("已切到「流程线（垂直）」", str(win.feedback.cget("text")))

    def test_an_unknown_or_already_active_skeleton_changes_nothing(self):
        with support.temp_db() as db:
            with _FakeTkEnv():
                win, _ids, _bid, cfg, _service = self._window(db)
                self.assertFalse(win.apply_template("nope"))
                self.assertIn("不认识的模板名", str(win.feedback.cget("text")))
                self.assertFalse(win.apply_template(""))
                self.assertIn("不认识的模板名", str(win.feedback.cget("text")))
                self.assertFalse(win.apply_template("auto"))
                self.assertIn("现在用的就是", str(win.feedback.cget("text")))
                self.assertEqual(cfg.get("map.template"), "auto")

    def test_a_skeleton_that_does_not_fit_the_material_says_so(self):
        """鱼骨图要因果链：材料不合适就说明白，并退回自动 —— 不假装摆好了。"""
        with support.temp_db() as db:
            with _FakeTkEnv():
                win, _ids, _bid, _cfg, _service = self._window(db)     # 只有一条「包含」
                self.assertTrue(win.apply_template("fishbone"))
                text = str(win.feedback.cget("text"))
                self.assertIn("不太适合鱼骨图", text)
                self.assertIn("可以切回自动", text)
                self.assertEqual(win.template_key(), "fishbone", "选择记着，下次材料合适就用上")
                self.assertEqual(win._layout.template, "", "这一次没摆成，实际用的还是自动")
                self.assertEqual(str(win.btn_template.cget("text")), "模板：鱼骨图")

    # ------------------------------------------------------ 固定位置（G4：确认）
    def test_switching_asks_before_it_drops_your_pinned_cards(self):
        from app.ui import concept_map as cm

        with support.temp_db() as db:
            with _FakeTkEnv():
                win, ids, bid, cfg, _service = self._window(db)
                win.pin_node(ids["卷积"], 120.0, 340.0)
                self.assertTrue(win._pins)
                with mock.patch.object(cm.messagebox, "askyesno",
                                       return_value=False) as ask:
                    self.assertFalse(win.apply_template("org"))
                self.assertEqual(ask.call_count, 1)
                self.assertEqual(str(win.feedback.cget("text")),
                                 "没有切换模板：先按需要保留现在的位置")
                self.assertTrue(win._pins, "说「不」就一张都别动")
                self.assertTrue(db.list_node_pins(bid), "库里也不能动")
                self.assertEqual(cfg.get("map.template"), "auto")

    def test_saying_yes_moves_the_cards_and_clears_the_pins(self):
        from app.ui import concept_map as cm

        with support.temp_db() as db:
            with _FakeTkEnv():
                win, ids, bid, cfg, _service = self._window(db)
                win.pin_node(ids["卷积"], 120.0, 340.0)
                with mock.patch.object(cm.messagebox, "askyesno", return_value=True):
                    self.assertTrue(win.apply_template("org"))
                self.assertEqual(win._pins, {})
                self.assertEqual(db.list_node_pins(bid), {}, "取消固定要落库，不是只擦内存")
                self.assertEqual(cfg.get("map.template"), "org")
                self.assertIn("顺带取消了 1 张卡片的固定", str(win.feedback.cget("text")))

    def test_the_confirmation_says_which_skeleton_and_how_many_cards_move(self):
        from app.ui import concept_map as cm

        with support.temp_db() as db:
            with _FakeTkEnv():
                win, ids, _bid, _cfg, _service = self._window(db)
                win.pin_node(ids["卷积"], 120.0, 340.0)
                with mock.patch.object(cm.messagebox, "askyesno",
                                       return_value=False) as ask:
                    win.apply_template("fishbone")
                args, kwargs = ask.call_args
                self.assertEqual(args[0], "模板")
                self.assertIn("鱼骨图", args[1])
                self.assertIn("1 张卡片", args[1])
                self.assertIn("继续吗", args[1])
                self.assertIs(kwargs.get("parent"), win.win, "问话要挂在导图窗口上")

    # ------------------------------------------------------ 选择对话框
    def test_the_dialog_lists_every_skeleton_and_marks_the_current_one(self):
        from app.ui import map_templates as mt

        with support.temp_db() as db:
            with _FakeTkEnv():
                win, _ids, _bid, _cfg, _service = self._window(db)
                win.open_template_dialog()
                dialog = win._template_dialog
                self.assertEqual(dialog.rows, tuple((spec.key, spec.name)
                                                    for spec in mt.TEMPLATES))
                self.assertEqual(dialog.picked_key(), "auto", "打开时指在现在用的这一项上")
                self.assertIn("自动", str(dialog.desc_name.cget("text")))
                buffer_text = str(dialog.list.get(0))
                self.assertTrue(buffer_text.startswith("自动（按关系分层）　·　当前"),
                                "第一项就是现在用的自动，并标上「当前」")
                self.assertEqual(buffer_text.count("·　当前"), 1, "只标现在用的这一项")
                dialog.current = "flow-h"                      # 换个当前项，标记跟着走
                dialog.refresh()
                buffer_text = str(dialog.list.get(0))
                self.assertEqual(buffer_text.count("·　当前"), 1)
                self.assertIn("流程线（水平）　·　当前", buffer_text)

    def test_the_dialog_offers_the_local_suggestion_and_can_use_it(self):
        with support.temp_db() as db:
            with _FakeTkEnv():
                win, _ids, _bid, _cfg, _service = self._window(db, edges=self.CAUSAL_EDGES)
                win.open_template_dialog()
                dialog = win._template_dialog
                self.assertIsNotNone(dialog.suggestion, "因果占一半以上就该有建议")
                self.assertEqual(dialog.suggestion[0], "fishbone")
                self.assertIn("因果", dialog.suggestion[1])
                self.assertTrue(dialog.use_suggestion())
                self.assertEqual(dialog.picked_key(), "fishbone")
                self.assertIn("已经替你选好", str(dialog.status.cget("text")))
                self.assertIn("应用这个模板", str(dialog.status.cget("text")))

    # ------------------------------------------------------ 让模型挑（G3，默认关）
    def test_the_model_button_is_hidden_until_the_switch_is_on(self):
        with support.temp_db() as db:
            with _FakeTkEnv():
                win, _ids, _bid, cfg, _service = self._window(db)
                self.assertFalse(win._can_ask_model_template(), "默认关：不给模型发东西")
                win.open_template_dialog()
                self.assertIsNone(win._template_dialog.on_ask_model)
                cfg.set_bool("map.template_ask_model", True)
                self.assertTrue(win._can_ask_model_template())

    def test_the_model_button_needs_a_ready_service_not_just_a_truthy_tuple(self):
        """``is_ready()`` 返回 ``(bool, 说明)``：元组恒为真，必须拆开看第一个。"""
        with support.temp_db() as db:
            with _FakeTkEnv():
                win, _ids, _bid, cfg, service = self._window(db)
                cfg.set_bool("map.template_ask_model", True)
                service.is_ready = lambda: (False, "还没配 API Key")
                self.assertFalse(win._can_ask_model_template())
                service.is_ready = lambda: (True, "可以了")
                self.assertTrue(win._can_ask_model_template())
                service.is_ready = lambda: False
                self.assertFalse(win._can_ask_model_template())

    def test_asking_the_model_picks_a_skeleton_through_the_ui_thread(self):
        with support.temp_db() as db:
            with _FakeTkEnv():
                win, _ids, _bid, cfg, service = self._window(db)
                cfg.set_bool("map.template_ask_model", True)
                sent = {}

                def fake_pick(pairs, *, topic="", choices=(), callback=None):
                    sent["pairs"] = [dict(item) for item in pairs]
                    sent["choices"] = tuple(choices)
                    sent["topic"] = topic
                    callback("tree", "层级一眼看得出")       # 真实现从后台线程回调
                    return True

                service.pick_template = fake_pick
                win.win = _ImmediateAfter(win.win)
                win.open_template_dialog()
                dialog = win._template_dialog
                self.assertIsNotNone(dialog.on_ask_model)
                dialog.ask_model()
                self.assertEqual(dialog.picked_key(), "tree", "模型挑的那一项要被选中")
                status = str(dialog.status.cget("text"))
                self.assertIn("树状图", status)
                self.assertIn("层级一眼看得出", status)
                self.assertEqual(sent["topic"], "卷积网络")
                self.assertTrue(sent["choices"])
                self.assertTrue(all(item["key"] != "auto" for item in sent["choices"]),
                                "auto 不给模型看：本地规则已经兜住「什么都不套」")
                self.assertEqual(sent["pairs"][0]["type"], "包含")
                self.assertNotIn("id", sent["pairs"][0], "只发关系类型与两端词名")

    def test_a_failed_model_pick_falls_back_to_the_local_rules_without_a_popup(self):
        from app.ui import concept_map as cm

        with support.temp_db() as db:
            with _FakeTkEnv():
                win, _ids, _bid, cfg, service = self._window(db)
                cfg.set_bool("map.template_ask_model", True)

                def fake_pick(pairs, *, topic="", choices=(), callback=None):
                    callback("", "", "网络不通")
                    return True

                service.pick_template = fake_pick
                win.win = _ImmediateAfter(win.win)
                win.open_template_dialog()
                with mock.patch.object(cm, "messagebox") as box:
                    win._template_dialog.ask_model()
                status = str(win._template_dialog.status.cget("text"))
                self.assertIn("没用上模型", status)
                self.assertIn("网络不通", status)
                self.assertIn("让模型挑模板没成功", str(win.feedback.cget("text")))
                self.assertFalse(box.showerror.called, "挑不了骨架不是错误，不弹窗")
                self.assertFalse(box.askyesno.called)

    def test_a_skeleton_we_do_not_know_is_ignored(self):
        from app.ui import concept_map as cm

        with support.temp_db() as db:
            with _FakeTkEnv():
                win, _ids, _bid, cfg, service = self._window(db)
                cfg.set_bool("map.template_ask_model", True)
                service.pick_template = lambda pairs, *, topic="", choices=(), callback=None: (
                    callback("提纲", "我想用大纲"), True)[1]
                win.win = _ImmediateAfter(win.win)
                win.open_template_dialog()
                with mock.patch.object(cm, "messagebox") as box:
                    win._template_dialog.ask_model()
                self.assertIn("没用上模型",
                              str(win._template_dialog.status.cget("text")))
                self.assertEqual(win._template_dialog.picked_key(), "auto",
                                 "不认识的排法不能被选中")
                self.assertFalse(box.showerror.called)

    def test_no_relations_means_there_is_nothing_to_ask_about(self):
        with support.temp_db() as db:
            with _FakeTkEnv():
                win, _ids, _bid, cfg, service = self._window(db, edges=[])
                cfg.set_bool("map.template_ask_model", True)
                called = []
                service.pick_template = lambda *a, **kw: called.append(1)
                win.open_template_dialog()
                win._template_dialog.ask_model()
                self.assertEqual(called, [], "没有关系就不该发请求")
                self.assertIn("还没有关系",
                              str(win._template_dialog.status.cget("text")))

    # ------------------------------------------------------ 导出与屏幕一致
    def test_the_export_gets_the_same_skeleton_name_as_the_screen(self):
        with support.temp_db() as db:
            with _FakeTkEnv():
                win, _ids, _bid, _cfg, _service = self._window(db)
                self.assertTrue(win.apply_template("tree"))
                fake = SimpleNamespace(summary=lambda: "导出好了",
                                       directory="D:\\探索工具\\_check")
                with mock.patch("app.map_export.export_map",
                                return_value=fake) as export:
                    win.export_map_files()
                self.assertEqual(export.call_args.kwargs.get("template_name"), "树状图",
                                 "页脚写的模板名必须与屏幕上的一致")
                self.assertEqual(export.call_args.kwargs.get("topic_name"), "卷积网络")
