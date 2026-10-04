"""浮层窗口基类：真正置顶、不抢焦点、**幂等显示**（不每轮重建/映射/显隐）。

为什么需要这个基类
-----------------
用户报告的「页面不置顶 / 频闪」有两个直接原因：

1. 旧实现每次显示都走一遍 ``deiconify() → update_idletasks() →
   SetWindowPos(SWP_SHOWWINDOW) → ShowWindow(SW_SHOWNOACTIVATE)``，
   即使窗口已经显示着；对已经可见的窗口反复 SWP_SHOWWINDOW + ShowWindow
   会引起重绘闪烁。
2. 显示入口被门控轮询驱动 —— 状态没变也会重新映射窗口。

因此这里把「显示」做成**状态幂等**，并且**第一次显示之前就在隐藏状态准备就绪**：

* 内容、尺寸、坐标、``WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW`` 全部在窗口还
  ``withdraw`` 的时候准备好，然后一次 ``deiconify()`` 直接出现在目标位置 ——
  不存在「先在原点/旧位置闪一帧再跳过去」；
* 已经显示、且内容 token 与屏幕坐标都没变 → 直接返回，一次 Tk/Win32 调用都不做；
* 可见窗口只更新内容，**不重新映射**（``deiconify`` 只在隐藏态调用一次）；
* 显隐只用 Tk 机制（``deiconify`` / ``withdraw``）保持 Tk 与 Win32 的
  ``wm state`` 一致，不再混用 ``ShowWindow(SW_HIDE)`` 造成「Tk 以为还显示着」；
* 支持**负坐标**显示器：一律用 Tk 的**绝对坐标**写法 ``+-1920+0``
  （符号位 ``+`` 之后才是可能带负号的数字）。``-1920`` 在 Tk 里不是
  「x = -1920」，而是「距屏幕右边缘 1920 像素」—— 见 :func:`format_geometry`；
* **所有显示入口统一实时门控**：``show_at`` 是最底层也是唯一的映射入口，
  它先问 :meth:`App.overlay_display_allowed`（实时前台判定）；被拒绝时
  **只隐藏、绝不 ``deiconify``**，所以迟到的结果 / 延迟动作不可能在游戏前台上屏；
* ``-topmost`` 只在用户偏好/门控状态翻转时改一次；
* 从不 ``SetForegroundWindow``、从不抢键盘焦点。

本类只做窗口层的事；具体内容由子类实现 :meth:`_render`。
"""
from __future__ import annotations

import re
import time
import tkinter as tk

from .. import win32util as w32
from ..logging_setup import get_logger
from . import theme

log = get_logger("overlay")


def format_geometry(x: int, y: int) -> str:
    """把**虚拟屏幕绝对坐标**格式化成 Tk 的位置串。

    Tk 的位置串是 ``[=]宽x高±X±Y``，其中 ``±`` 是**位置模式**而不是数字符号：

    * ``+100``  → 绝对坐标 x = 100；
    * ``-1920`` → **距屏幕右边缘** 1920 像素（左边缘模式），
      在 1920 宽的屏幕上会落到 x = 0，**不是** x = -1920；
    * ``+-1920`` → 绝对坐标 x = -1920（符号位 ``+``，数字本身带负号）。

    左/上方显示器（Windows 虚拟屏幕负坐标）必须用 ``+-1920+0`` 这种写法，
    否则窗口会被摆到主屏右侧/下方去。因此这里固定输出「符号位 + 带符号数字」，
    绝不用 ``{:+d}`` 那种会把 ``+-`` 折叠成 ``-`` 的格式。y 同理（``+-`` 才是
    绝对负坐标，``-`` 是距下边缘）。
    """
    return f"+{int(x)}+{int(y)}"


def tk_position(spec: str) -> tuple[str, int, str, int]:
    """解析位置串，返回 ``(x_mode, x, y_mode, y)``（mode ∈ {"+", "-"}）。

    ``+`` 表示绝对坐标，``-`` 表示「距右/下边缘」。仅供测试 / 诊断做**语义**
    校验：一个位置串是否正确，取决于它解析出来的模式，而不只是「带不带负号」。
    """
    m = re.fullmatch(r"(\+|-)(-?\d+)(\+|-)(-?\d+)", str(spec).strip())
    if m is None:
        raise ValueError(f"无法解析 Tk 位置串: {spec!r}")
    return m.group(1), int(m.group(2)), m.group(3), int(m.group(4))


class FloatingWindow:
    """一个不抢焦点、可置顶、显示幂等的 Toplevel 包装。"""

    def __init__(self, master: tk.Misc, app, *, name: str = "overlay",
                 border_color: str | None = None):
        self.app = app
        self.name = name
        self.visible = False
        self._hwnd = 0
        self._no_activate_ok = False
        self._topmost_attr = None          # Tk -topmost 的当前值缓存
        self._body: tk.Widget | None = None
        self._token: object = None
        self._anchor: tuple[int, int] | None = None
        self._last_shown_at = 0.0
        #: 是否已经「在隐藏状态准备过一次」（尺寸/坐标/样式）
        self._prepared = False
        #: 最近一次真正下发的 geometry 串（位置 + 尺寸，用于避免重复调用）
        self._last_geometry: str | None = None
        #: 内容是否已经写入过（决定是否需要 update_idletasks 量尺寸）
        self._rendered = False
        #: 用户是否显式关闭过：关闭后不得被迟到的结果重新 show 出来
        self.closed_by_user = False
        #: 真正执行了多少次「显示」调用（测试用来断言没有重复显示）
        self.show_calls = 0
        #: 真正执行了多少次「重新定位」
        self.move_calls = 0

        self.win = tk.Toplevel(master)
        self.win.withdraw()
        self.win.overrideredirect(True)
        # 窗口底色 = 面板底色（**不是**描边色）：圆角 region 之外被裁掉的边缘
        # 只会露出同一种底色，不会在四个角上留下一条被切断的深色边（缺口）。
        # 真正的 1px 描边由子类用圆角画布画在 region 以内。
        self.win.configure(bg=border_color or theme.PANEL)
        # 浮窗层级 = **当前环境**决定的自动置顶（普通阅读 → 置顶；游戏 / 全屏 /
        # 暂停的硬阻断 → 不置顶）。旧实现读 ``ui.topmost`` 偏好开关，而那个开关
        # 已经从界面上移除；字段仍兼容保留，但不再决定窗口层级。
        want = None
        effective = getattr(app, "effective_topmost", None)
        if callable(effective):
            try:
                want = bool(effective())
            except Exception:  # pragma: no cover - 极简替身
                want = None
        if want is None:
            want = bool(getattr(app.config, "topmost", True))
        try:
            self.win.attributes("-topmost", bool(want))
            self._topmost_attr = bool(want)
        except tk.TclError:  # pragma: no cover
            pass
        self.win.bind("<Map>", self._on_map)

    # ------------------------------------------------------------- 窗口句柄
    def hwnd(self) -> int:
        if not self._hwnd:
            try:
                self._hwnd = w32.toplevel_hwnd(self.win.winfo_id())
            except tk.TclError:  # pragma: no cover
                return 0
        return self._hwnd

    def destroy(self) -> None:
        try:
            self.win.destroy()
        except tk.TclError:  # pragma: no cover
            pass
        self.visible = False
        self._hwnd = 0

    # --------------------------------------------------------- 无激活 / 置顶
    def _apply_no_activate(self) -> None:
        """补 ``WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW`` —— 只在确实缺失时动样式。"""
        hwnd = self.hwnd()
        if not hwnd:
            return
        if self._no_activate_ok and w32.is_no_activate(hwnd):
            return
        w32.make_no_activate(hwnd)
        self._no_activate_ok = w32.is_no_activate(hwnd)

    def set_topmost(self, value: bool) -> bool:
        """设置置顶。返回偏好是否发生了变化（未变化时一次 Win32 调用都不做）。

        ``_topmost_attr`` 只是**偏好**缓存；真实生效状态以 ``WS_EX_TOPMOST`` 为准。
        因为 :func:`app.win32util.set_topmost` 会绕过 Tk 直接改扩展样式，
        只信缓存会出现「以为已经置顶、其实早被取消」的漂移。
        """
        value = bool(value)
        changed = self._topmost_attr != value
        self._topmost_attr = value
        if changed:
            try:
                self.win.attributes("-topmost", value)
            except tk.TclError:  # pragma: no cover
                pass
        hwnd = self.hwnd()
        if hwnd and w32.is_topmost(hwnd) != value:
            # 只改 Z 序：不显示、不移动、不激活、不重新映射
            w32.set_topmost(hwnd, value)
        return changed

    def topmost(self) -> bool:
        return bool(self._topmost_attr)

    def _on_map(self, _event=None) -> None:
        # 窗口被系统/Tk 重新映射时补一次样式；绝不在这里改动可见性
        self._apply_no_activate()

    # ------------------------------------------------------- 尺寸 / 几何串
    def _geometry_spec(self, x: int, y: int) -> str:
        """Tk geometry 串。默认只写位置（尺寸由内容决定）。

        需要**固定尺寸**的子类（例如阅读面板：小方块 44x44 / 展开 360x460）
        覆写本方法，把尺寸一起写进 geometry 串 —— 位置部分仍然必须使用
        :func:`format_geometry` 的「绝对坐标」写法（负坐标显示器）。
        """
        return format_geometry(x, y)

    def _set_geometry(self, spec: str, x: int, y: int) -> bool:
        """应用一次 geometry 并记录**完整串**（位置 + 尺寸）。

        ``_last_geometry`` 是 :meth:`show_at` 判断「要不要真的调用 Tk」的依据：
        只比较左上角是不够的 —— 小方块 44x44 与展开面板 360x460 完全可能
        共享同一个左上角（默认布局就是垂直居中对齐），只比坐标会让窗口
        停在旧尺寸上（44x44 的「面板」或者 360x460 的「小方块」）。

        串与坐标都没变时**不调用** Tk；返回值表示是否真的下发了一次 geometry。
        """
        spec = str(spec)
        if spec == self._last_geometry and self._anchor == (int(x), int(y)):
            return False
        self.win.geometry(spec)
        self._anchor = (int(x), int(y))
        self._last_geometry = spec
        self.move_calls += 1
        return True

    def last_geometry(self) -> str:
        """最近一次真正下发的 geometry 串（诊断 / 回归断言用）。"""
        return str(self._last_geometry or "")

    # ------------------------------------------------- 显式激活（键盘输入）
    def can_activate(self) -> bool:
        """本窗口是否允许「用户明确点击后激活」的路径（子类可覆写）。"""
        return True

    def activate_for_input(self, widget=None) -> bool:
        """**只有用户明确点击输入框**时才调用：临时允许窗口激活并给键盘焦点。

        浮层默认带 ``WS_EX_NOACTIVATE``（不抢阅读焦点），因此点击不会激活，
        键盘输入也进不来。用户点进追问输入框时，这里显式：

        1. 去掉 ``WS_EX_NOACTIVATE``（一次 Win32 调用，之后 :meth:`_apply_no_activate`
           会在下一次**自动**显示时补回来）；
        2. ``SetForegroundWindow`` 让本窗口成为前台（只针对这次点击）；
        3. 把 Tk 焦点交给输入控件。

        自动展开（划选触发）**绝不**走这条路径。

        成功后记下 ``_input_active``：前台门控的软受限回调据此知道
        「用户正在我们的面板里打字」，不会把面板折叠掉（见
        :meth:`ReadingPanel` 一侧的处理）。
        """
        if not self.can_activate():
            return False
        hwnd = self.hwnd()
        if hwnd:
            w32.clear_no_activate(hwnd)
            if w32.is_no_activate(hwnd):  # pragma: no cover - 极端情况下清不掉
                return False
            self._no_activate_ok = False
            w32.activate_window(hwnd)
        try:
            self.win.focus_force()
        except tk.TclError:  # pragma: no cover
            pass
        if widget is not None:
            try:
                widget.focus_set()
            except tk.TclError:  # pragma: no cover
                pass
        self._input_active = True
        return True

    def input_active(self) -> bool:
        """用户是否正把键盘交给本窗口（点过输入框、还没点走）。"""
        return bool(getattr(self, "_input_active", False))

    def release_activation(self) -> None:
        """结束键盘输入：恢复 ``WS_EX_NOACTIVATE``（下一次自动显示不抢焦点）。"""
        self._no_activate_ok = False
        self._apply_no_activate()

    # ---------------------------------------------------------------- 位置
    def _clamp_to(self, x: int, y: int, w: int, h: int) -> tuple[int, int]:
        """把矩形夹进**该点所在显示器**的工作区（支持负坐标显示器）。"""
        try:
            left, top, right, bottom = w32.work_area_for_point(x, y)
        except OSError:  # pragma: no cover
            left, top = 0, 0
            right, bottom = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        if right - left < w:
            x = left
        else:
            x = max(left + 2, min(int(x), right - w - 2))
        if bottom - top < h:
            y = top
        else:
            y = max(top + 2, min(int(y), bottom - h - 2))
        return x, y

    def _apply_geometry(self, x: int, y: int) -> tuple[int, int]:
        """设置窗口位置 / 尺寸（隐藏时也生效）；返回实际使用的左上方坐标。"""
        self._ensure_measured()
        self._set_geometry(self._geometry_spec(int(x), int(y)), int(x), int(y))
        return int(x), int(y)

    def _ensure_measured(self) -> None:
        """让 Tk 立刻按**当前**内容算好 requested size。

        内容会从「正在解释…」变成完整释义，尺寸随之变化，所以这里不能只做
        一次：每次要在隐藏态定位 / 显示之前都重新量一遍。
        ``update_idletasks`` 只跑空闲任务，不会重入用户事件回调。
        """
        try:
            self.win.update_idletasks()
        except tk.TclError:  # pragma: no cover
            return

    def _size(self) -> tuple[int, int]:
        w = max(theme.px(40), int(self.win.winfo_reqwidth()))
        h = max(theme.px(20), int(self.win.winfo_reqheight()))
        return w, h

    def prepare_hidden(self, anchor: tuple[int, int] | None = None,
                       token=None, *, text: str | None = None) -> bool:
        """**首次显示之前**在隐藏状态把内容 / 尺寸 / 坐标 / 样式准备好。

        这是「不先在原点闪现再移动」的关键：``winfo_reqwidth`` 在窗口
        ``withdraw`` 时依然可用（Tk 已按内容算好 requested size），
        因此可以在窗口还没被映射时就把 geometry 定到目标位置。
        """
        if text is not None:
            self._render(text)
            self._rendered = True
        self._ensure_measured()
        hwnd = self.hwnd()
        if hwnd:
            self._apply_no_activate()
            if w32.is_topmost(hwnd) != self.topmost():
                w32.set_topmost(hwnd, self.topmost())
        if anchor is not None:
            w, h = self._size()
            self._apply_geometry(*self._clamp_to(int(anchor[0]), int(anchor[1]), w, h))
        if token is not None:
            self._token = token
        self._prepared = True
        return True

    # ---------------------------------------------------------------- 显示门控
    def display_allowed(self, *, already_visible: bool = False,
                        explicit: bool = False) -> bool:
        """**实时可显示判定** —— 所有浮层映射的唯一闸门（fail-closed）。

        宿主（``App``）提供 ``overlay_display_allowed(window, already_visible=…,
        explicit=…)``：它每次都重新读前台（不看轮询缓存），因此**迟到的动作 /
        迟到的结果**也不可能在游戏/全屏前台上屏。

        * 没有该钩子的宿主（纯窗口层测试替身）视为允许；
        * 钩子抛异常 → **拒绝**（宁可不显示，也不在不确定的前台上弹窗）。
        """
        fn = getattr(self.app, "overlay_display_allowed", None)
        if not callable(fn):
            return True
        try:
            return bool(fn(self, already_visible=bool(already_visible),
                           explicit=bool(explicit)))
        except Exception:  # pragma: no cover - 门控自身异常必须 fail-closed
            log.exception("浮层显示门控失败，按拒绝处理")
            return False

    def can_show_now(self, *, explicit: bool = False) -> bool:
        """当下是否允许「新映射」本窗口（实时判定，fail-closed）。

        ``already_visible`` 传**真实**可见性：软受限（本程序窗口在前台）下
        已经映射的窗口可以保留、未映射的不许冒出来。
        """
        visible_now = bool(self.visible and w32.is_window_visible(self.hwnd()))
        return self.display_allowed(already_visible=visible_now, explicit=explicit)

    # ---------------------------------------------------------------- 显示
    def show_at(self, anchor: tuple[int, int], token=None, *, text: str | None = None,
                reopen: bool = True, explicit: bool = False) -> bool:
        """在 ``anchor`` 附近显示（已显示且 token/位置未变则什么都不做）。

        修闪烁的三个关键点：

        1. 窗口还不可见时**先把内容/尺寸/坐标/样式在隐藏状态准备好，再
           ``deiconify``** —— 反过来会让窗口先在旧位置（首次是 Tk 默认位置）
           映射一帧再跳过去；
        2. 已可见且位置没变时不再重复 ``deiconify``（可见窗口只更新内容）；
        3. ``text=None`` 表示「内容由子类自己写好，别在这里重画」——
           否则子类刚设置的「正在解释…」会被空串覆盖成白板。

        门控（**第一步**，先于任何渲染 / 定位 / 映射）：实时判定不允许显示时
        **只隐藏并返回 False，一次 ``deiconify`` 都不做**。游戏/全屏/用户暂停
        下哪怕结果迟到、动作延迟，都不可能把浮层映射出来。

        ``reopen=False``：**已经隐藏 / 被用户关闭**的窗口不得被重新显示
        （迟到的结果回来了也只能写内容，不弹窗）。这是「解释结果窗不会自己
        弹回来」的硬保证。

        ``explicit=True``：用户**显式动作**（例如配好 Key 后从设置里把同一条
        待解释窗口找回来）。软受限（本程序窗口在前台）下它允许解释结果窗出现，
        但**硬阻断（游戏/全屏/暂停）依旧一律拒绝**。

        位置 / 尺寸的幂等判据是**完整 geometry 串**（``WxH+X+Y``），不是左上角：
        小方块与展开面板经常共享同一个左上角（默认布局垂直居中对齐），
        只比坐标会让窗口停在 44x44（该展开却还是小方块）或 360x460
        （该折叠却还是大面板）。值真的没变时一次 Tk 调用都不做。
        """
        ax, ay = int(anchor[0]), int(anchor[1])
        visible_now = bool(self.visible and w32.is_window_visible(self.hwnd()))
        if not self.display_allowed(already_visible=visible_now, explicit=explicit):
            self.hide()          # 幂等；不做 deiconify、不改 closed_by_user
            return False
        if not reopen and (self.closed_by_user or not self.visible):
            if text is not None:
                self._render(text)
                self._rendered = True
            self._token = token
            return False
        self.closed_by_user = False

        visible = self.visible and w32.is_window_visible(self.hwnd())
        if visible and text is None and self._token == token \
                and self._token is not None and self._anchor == (ax, ay) \
                and self._last_geometry == self._geometry_spec(ax, ay):
            # 位置 + 尺寸 + 内容 token 全都没变：一次 Tk 调用都不做（门控轮询不闪）
            return True
        if text is not None:
            self._render(text)
            self._rendered = True

        if not self._prepared:
            # 首次显示：先在隐藏态把一切准备好，再一次性映射到目标位置
            self.prepare_hidden((ax, ay), token)

        # 目标矩形（夹进工作区后）与目标 geometry 串：先算清楚，再决定要不要动 Tk
        self._ensure_measured()
        w, h = self._size()
        x, y = self._clamp_to(ax, ay, w, h)
        spec = self._geometry_spec(x, y)
        need_geometry = (self._last_geometry != spec) or (self._anchor != (x, y))

        if not visible:
            # 隐藏 → 显示：只有这一条路径会映射窗口
            if need_geometry:
                self._set_geometry(spec, x, y)
            self._apply_no_activate()
            self.win.deiconify()
            self.show_calls += 1
            self._last_shown_at = time.monotonic()
        else:
            # 已可见：只更新内容与（必要时）位置 / 尺寸，绝不重复映射
            if need_geometry:
                self._set_geometry(spec, x, y)
        self._token = token
        self.visible = True
        return True

    def hide(self) -> bool:
        """隐藏（幂等：已经隐藏时不做任何调用）。返回是否发生了变化。

        显隐统一走 Tk（``withdraw``），不再混用 ``ShowWindow(SW_HIDE)``：
        混用会让 Tk 的 ``wm state`` 以为窗口还显示着，之后 ``deiconify`` 不生效，
        只能靠重建窗口「复活」—— 那正是用户看到的「关掉又自己弹回来」。

        ``closed_by_user`` **不在这里**改动：门控/新选区导致的自动收起不是
        「用户关闭」，后续结果仍然可以正常显示。
        """
        if not self.visible:
            return False
        try:
            self.win.withdraw()
        except tk.TclError:  # pragma: no cover
            pass
        self.visible = False
        self._token = None
        self._anchor = None
        # 下次显示必须重新下发完整 geometry（隐藏期间尺寸/状态可能变了）
        self._last_geometry = None
        return True

    def close_by_user(self) -> bool:
        """用户显式关闭：收起并记住「别再自己弹回来」。"""
        changed = self.hide()
        self.closed_by_user = True
        return changed

    def _render(self, text: str) -> None:  # pragma: no cover - 子类实现
        raise NotImplementedError
