"""统一风格控件（编辑杂志风）：扁平按钮、细滚动条、分隔线、状态标记。

为什么不用 ttk
--------------
``ttk`` 控件在 Windows 上带原生主题（蓝灰渐变、圆角、悬停高亮），与
「#F9F8F6 暖米色 + #1C1C1C 柔黑 + 细线无阴影 + 方角」的编辑杂志风冲突。
这里用 ``tk.Label`` 自绘按钮：

* 主按钮 = **柔黑底 + 米色字**（editorial 的 `bg-foreground text-background`）；
* 次按钮 = 透明底 + 细线方框，hover 时**只加细下划线并把字色加深**；
* 任何状态都不使用彩色，也不用阴影/渐变；
* ``configure(text=...)`` / ``cget("text")`` 与 ttk.Button 兼容，旧调用点不用改。

本文件另外提供两个**自绘**控件（Tk 原生控件做不到的圆角与拖动滑轨）：

* :class:`TermCard` —— 词表两列等宽卡片：Canvas 画 8px 圆角暖米白卡面，
  词语（+可选的一句话摘要）用普通 Label 叠在卡内（内缩 ≥ 圆角半径，
  所以文字不会盖住圆角）；
* :class:`ScrollRail` —— 右侧细滑轨：Canvas 画圆角灰色滑块，**无箭头、
  无说明字**，滚轮 / 拖滑块 / 点轨道三种方式都能滑动，``set(first, last)``
  与 ``command`` 协议与 ``tk.Scrollbar`` 完全一致，因此可以直接替换。
"""
from __future__ import annotations

import math
import tkinter as tk

from . import glyph_text
from . import panel_geometry as geo
from . import theme


class FlatButton(tk.Label):
    """无阴影、方角、单色的按钮（主/次两种）。

    文字**永远水平 + 垂直居中**，规则钉在字形图像里
    --------------------------------------------------
    * 文字由 :mod:`app.ui.glyph_text` 渲染成 RGBA 图像：紧致墨迹 + 字体排版单元
      + 调用点 ``padx`` / ``pady``（``theme.px`` 设备像素）对称烘焙，
      可见字形落在整张图像中心（偏差 ≤ 0.5px）；
    * 图像路径显式 ``super().configure(image=photo, compound="none",
      anchor="center", justify="center", padx=0, pady=0)`` —— Tk 那侧一个像素
      都不再加，内边距只加图像里那一次；
    * 调用点的视觉内边距原样留在 :attr:`padx` / :attr:`pady`（设备像素）；
      图像路径下 ``cget("padx")`` 是 0（Tk 侧不承担内边距）；
    * ``text`` / ``cget("text")`` / ``configure(state=..., text=...)`` /
      ``invoke()`` / ``set_enabled()`` 语义与旧实现一致（图像显示时
      ``text`` 仍写在控件上，只是被 ``compound="none"`` 隐藏）；
    * hover / 禁用 / 改文案都会**重建**图像并保留引用（``_glyph_photo``，
      否则 PhotoImage 被 GC 后按钮会变空白）；hover 的下划线只画在真实墨迹
      底边以下，图像尺寸与字形位置一格不动；
    * 位置只由 ``pack(anchor=...)`` / ``place(...)`` 决定 —— 那是**按钮整体**
      在父容器里的位置（例如底部主按钮靠左 ``pack(anchor="w")``），
      与按钮**内部**的文字居中互不影响，两者不要混为一谈。

    禁止给单个按钮加 ``anchor`` / ``justify`` / ``ipadx`` / ``ipady`` 这类
    「魔数偏移」：所有功能按钮共用这一条居中规则（``tests/test_ui_roundrect.py``
    有一条轻量契约测试守住它）。

    Pillow 缺失时（可选依赖）：清掉 image，退回原生文字居中 +
    调用点 ``padx`` / ``pady`` —— 这是**降级**，不是等价实现。
    """

    def __init__(self, master, text: str, command=None, *, primary: bool = False,
                 font_size: int = 9, padx: int = 12, pady: int = 5, width: int | None = None,
                 anchor: str = "center"):
        #: 调用点的视觉内边距（设备像素）：图像路径会把它烘焙进文字图像
        self.padx = theme.px(padx)
        self.pady = theme.px(pady)
        self._text = str(text or "")
        #: PhotoImage / PIL 图像的引用必须留住，否则会被 GC（按钮变空白）
        self._glyph_photo = None
        self._glyph_image = None
        self._glyph_meta: dict = {}
        self._glyph_ok = False
        super().__init__(
            master,
            text=self._text,
            cursor="hand2",
            font=theme.font(font_size),
            bg=theme.ACCENT if primary else theme.PANEL,
            fg=theme.BG if primary else theme.TEXT,
            padx=self.padx,
            pady=self.pady,
            anchor=anchor,
            justify="center",
            highlightthickness=1,
            highlightbackground=theme.ACCENT if primary else theme.BORDER,
            highlightcolor=theme.ACCENT,
            bd=0,
            state="normal",
        )
        if width is not None:
            self.configure(width=width)
        self.command = command
        self._primary = bool(primary)
        self._enabled = True
        self._font_size = font_size
        self._hover = False
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self._refresh_glyph()

    # ------------------------------------------------------------- 交互
    def _on_enter(self, _event=None) -> None:
        self._hover = True
        self._restyle()

    def _on_leave(self, _event=None) -> None:
        self._hover = False
        self._restyle()

    def _on_click(self, _event=None):
        """Label 的 ``state="disabled"`` 拦不住 ``<Button-1>``，所以自己守卫。"""
        if not self._enabled or self.command is None:
            return None
        return self.command()

    def invoke(self):
        return self._on_click()

    # ------------------------------------------------------------- 样式
    def _fg_now(self) -> str:
        if self._primary:
            return theme.BG if self._enabled else theme.PANEL
        return theme.TEXT if self._enabled else theme.TEXT_FAINT

    def _restyle(self) -> None:
        size = self._font_size
        underline = self._hover and self._enabled
        if self._primary:
            font = theme.font(size, underline=underline)
            bg = theme.ACCENT if self._enabled else theme.BORDER_STRONG
        else:
            font = theme.font(size, underline=underline)
            bg = theme.HOVER_BG if (self._hover and self._enabled) else theme.PANEL
        try:
            self.configure(font=font, bg=bg, fg=self._fg_now())
        except tk.TclError:  # pragma: no cover - 控件已销毁
            return
        if self._glyph_ok:
            self._refresh_glyph()

    # --------------------------------------------------------- 字形图像
    def _refresh_glyph(self) -> bool:
        """按当前文字 / 状态重建字形图像；Pillow 不可用就退回原生文字渲染。"""
        if not glyph_text.available():
            return self._use_native_text()
        try:
            image, meta = glyph_text.render_text_image(
                self._text, font_size=self._font_size,
                padx=self.padx, pady=self.pady, color=self._fg_now(),
                underline=self._hover and self._enabled,
            )
            photo = glyph_text.photo_image(image, master=self)
        except (RuntimeError, OSError, ImportError, tk.TclError):
            # 没有 Pillow / ImageTk / 拿不到解释器 → 退回原生文字（**降级**路径）。
            # 这里刻意不吞其它异常：真出 bug 时要炸在测试里，而不是悄悄变成另一种样子。
            return self._use_native_text()
        if photo is None:                       # pragma: no cover - 工厂没给图
            return self._use_native_text()
        self._glyph_image = image
        self._glyph_meta = dict(meta)
        self._glyph_photo = photo
        self._glyph_ok = True
        #: Tk 侧内边距显式归零：内边距已经在图像里，只允许加一次
        super().configure(image=photo, compound="none", anchor="center",
                          justify="center", padx=0, pady=0)
        return True

    def _use_native_text(self) -> bool:
        """降级路径：清 image，恢复原生居中与调用点 padx / pady。"""
        self._glyph_ok = False
        self._glyph_photo = None
        self._glyph_image = None
        try:
            super().configure(image="", compound="none", anchor="center",
                              justify="center", padx=self.padx, pady=self.pady)
        except tk.TclError:  # pragma: no cover - 控件已销毁
            pass
        return False

    # --------------------------------------------------------- 兼容接口
    def configure(self, cnf=None, **kw):  # noqa: D102 - 兼容 ttk 调用风格
        if isinstance(cnf, dict):
            kw = {**cnf, **kw}
            cnf = None
        if "state" in kw:
            self.set_enabled(str(kw.pop("state")) != "disabled")
        if "text" in kw:
            self._text = str(kw.pop("text"))
            super().configure(cnf, text=self._text, **kw)
            if self._glyph_ok:
                self._refresh_glyph()
            return None
        return super().configure(cnf, **kw)

    config = configure

    def set_enabled(self, value: bool) -> None:
        self._enabled = bool(value)
        self._restyle()

    def is_enabled(self) -> bool:
        return bool(self._enabled)


def thin_scrollbar(parent, command=None) -> tk.Scrollbar:
    """细线滚动条（单色、无立体边框）。主界面等旧容器仍在用。"""
    sb = tk.Scrollbar(
        parent, orient="vertical", command=command,
        width=theme.px(9), troughcolor=theme.BG, bg=theme.BORDER_STRONG,
        activebackground=theme.TEXT_MUTED, highlightthickness=0, bd=0,
        relief="flat", elementborderwidth=0,
    )
    return sb


# =============================================================== 圆角几何
#: 每个圆角用几段折线近似（4 段在 8px 半径下已经看不出棱角）
CORNER_STEPS = 4


def round_rect_points(x: float, y: float, w: float, h: float, radius: float) -> list[float]:
    """圆角矩形的多边形顶点（**纯函数**，不碰 Tk，可离线回归）。

    半径自动收敛到 ``min(radius, w/2, h/2)``：卡片被压得很窄时也不会画出
    自交的怪形状；``radius <= 0`` 时退化成普通矩形。
    """
    x, y, w, h = float(x), float(y), float(w), float(h)
    r = max(0.0, min(float(radius), w / 2.0, h / 2.0))
    if r <= 0.0:
        return [x, y, x + w, y, x + w, y + h, x, y + h]
    corners = (
        (x + r, y + r, 180.0, 270.0),          # 左上
        (x + w - r, y + r, 270.0, 360.0),      # 右上
        (x + w - r, y + h - r, 0.0, 90.0),     # 右下
        (x + r, y + h - r, 90.0, 180.0),       # 左下
    )
    points: list[float] = []
    steps = max(1, int(CORNER_STEPS))
    for cx, cy, start, end in corners:
        for index in range(steps + 1):
            angle = math.radians(start + (end - start) * index / steps)
            points.extend([cx + r * math.cos(angle), cy + r * math.sin(angle)])
    return points


def draw_round_rect(canvas, x, y, w, h, radius, **kw):
    """在 Canvas 上画一个圆角矩形（返回 item id，便于悬停改色）。"""
    return canvas.create_polygon(round_rect_points(x, y, w, h, radius), **kw)


def line_height(size: int) -> int:
    """一行文字的**真实**高度（设备像素）：直接问 Tk 字体度量 ``linespace``。

    字号本身**不等于**行高：16px 的微软雅黑要 23px、20px 要 29px。卡片里词语
    与摘要的高度都是按行预留的，用字号当行高会裁掉字的下半截（实测 144 DPI 下
    摘要被裁 5px）。拿不到 Tk 字体（假 Tk 环境、离线预览）时退回
    :func:`panel_geometry.text_line_px` 的纯计算，两边同一套近似值。
    """
    fallback = geo.text_line_px(abs(theme.device_px(size)))
    try:
        import tkinter.font as tkfont
        metrics = tkfont.Font(font=theme.font(size)).metrics("linespace")
        return max(fallback, int(metrics))
    except Exception:                        # pragma: no cover - 假 Tk / 无字体
        return fallback


class TermCard:
    """词表卡片（两列等宽布局里的一张卡）。

    结构（都是真实 Tk 控件，假 Tk 环境里同样成立）::

        Canvas(卡片底：圆角矩形)        ← 圆角只可能由 Canvas 画出来
          ├─ Label(词语)               ← 内缩 ≥ 圆角半径，不盖住圆角
          └─ Label(一句话摘要，可选)    ← 只有库里**已经有** one_line 时才创建

    词语**最多两行**（:data:`panel_geometry.CARD_TERM_LINES`）：``wraplength``
    跟着卡片宽度走，所以 ``Machine learning (ML) algorithms`` 这种长词是**换行**
    显示，而不是被字数截断成 ``Machine learning (…``；真正超过两行才由调用点
    （:meth:`reading_panel.ReadingPanel._render_tags`）按宽度预算省略。

    对外接口与旧 ``tag_label`` 保持兼容：``cget("text")`` 返回当前显示的词语、
    ``place()`` / ``bind()`` / ``winfo_reqwidth()`` 都能用，因此面板的
    reflow / 滚轮 / 回归断言不需要改语义。
    """

    def __init__(self, parent, term: str, *, summary: str = "", width: int, height: int,
                 radius: int | None = None, command=None, pad_x: int | None = None,
                 pad_y: int | None = None, bg: str | None = None):
        self.term = str(term or "")
        self.summary = str(summary or "")
        self.command = command
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self.radius = int(radius if radius is not None else theme.px(geo.CARD_RADIUS))
        self.pad_x = int(pad_x if pad_x is not None else theme.px(geo.CARD_PAD_X))
        self.pad_y = int(pad_y if pad_y is not None else theme.px(geo.CARD_PAD_Y))
        self._card_bg = bg or theme.CARD_BG
        self._hover = False

        self.canvas = tk.Canvas(parent, bg=theme.PANEL, highlightthickness=0, bd=0,
                                width=self.width, height=self.height, cursor="hand2")
        self._shape = draw_round_rect(self.canvas, 0.5, 0.5, self.width - 1, self.height - 1,
                                      self.radius, fill=self._card_bg,
                                      outline=theme.CARD_BORDER, width=1)
        self.label = tk.Label(self.canvas, text=self.term, bg=self._card_bg, fg=theme.TEXT,
                              font=theme.font(geo.CARD_TERM_PT), anchor="nw", justify="left",
                              bd=0, highlightthickness=0,
                              wraplength=self._inner_width())
        self.summary_label: tk.Label | None = None
        if self.summary:
            self.summary_label = tk.Label(
                self.canvas, text=self.summary, bg=self._card_bg, fg=theme.TEXT_MUTED,
                font=theme.font(geo.CARD_SUMMARY_PT), anchor="nw", justify="left",
                bd=0, highlightthickness=0,
            )
        self._layout()
        self._bind_events()

    # ------------------------------------------------------------ 布局
    def _summary_height(self) -> int:
        """摘要一行占的高度（**真实行高** + 2px 余量，见 :func:`line_height`）。"""
        return line_height(geo.CARD_SUMMARY_PT) + 2

    def _term_height(self) -> int:
        """词语占据的高度：有摘要时给它两行以内，没摘要时铺满卡片。"""
        body = max(1, self.height - 2 * self.pad_y)
        if self.summary_label is None:
            return body
        return max(1, body - self._summary_height() - 2)

    def _layout(self) -> None:
        inner = self._inner_width()
        # ``wraplength`` = 卡片内宽（设备像素）：宽度变了就跟着换行，不是截断。
        try:
            self.label.configure(wraplength=inner)
        except tk.TclError:  # pragma: no cover - 控件已销毁
            return
        self.label.place(x=self.pad_x, y=self.pad_y, width=inner, height=self._term_height())
        if self.summary_label is not None:
            summary_h = self._summary_height()
            self.summary_label.place(x=self.pad_x, y=self.height - self.pad_y - summary_h,
                                     width=inner, height=summary_h)

    def _inner_width(self) -> int:
        return max(theme.px(16), self.width - 2 * self.pad_x)

    def set_size(self, width: int, height: int) -> bool:
        """改卡片尺寸（窗口缩放时调用）：只重排、**不重建**控件（不闪）。"""
        w, h = max(1, int(width)), max(1, int(height))
        if (w, h) == (self.width, self.height):
            return False
        self.width, self.height = w, h
        try:
            self.canvas.configure(width=w, height=h)
            self.canvas.coords(self._shape,
                               *round_rect_points(0.5, 0.5, w - 1, h - 1, self.radius))
        except tk.TclError:  # pragma: no cover - 控件已销毁
            return False
        self._layout()
        return True

    # ------------------------------------------------------------ 交互
    def _bind_events(self) -> None:
        widgets = [self.canvas, self.label]
        if self.summary_label is not None:
            widgets.append(self.summary_label)
        self._widgets = widgets
        for widget in widgets:
            widget.bind("<Button-1>", self._on_click)
            widget.bind("<Enter>", self._on_enter)
            widget.bind("<Leave>", self._on_leave)

    def _on_click(self, _event=None):
        if self.command is None:
            return None
        return self.command()

    def _on_enter(self, _event=None) -> None:
        self.set_hover(True)

    def _on_leave(self, _event=None) -> None:
        self.set_hover(False)

    def set_hover(self, value: bool) -> bool:
        """悬停：**只**把底色/边线换成柔和的浅色，不加任何说明文字。"""
        value = bool(value)
        if value == self._hover:
            return False
        self._hover = value
        bg = theme.CARD_BG_HOVER if value else self._card_bg
        border = theme.CARD_BORDER_HOVER if value else theme.CARD_BORDER
        try:
            self.canvas.itemconfigure(self._shape, fill=bg, outline=border)
            self.label.configure(bg=bg)
            if self.summary_label is not None:
                self.summary_label.configure(bg=bg)
        except tk.TclError:  # pragma: no cover
            return False
        return True

    def is_hovered(self) -> bool:
        return bool(self._hover)

    # ------------------------------------------------------- 兼容接口
    def place(self, **kw) -> None:
        self.canvas.place(**kw)

    place_configure = place

    def place_forget(self) -> None:
        self.canvas.place_forget()

    def pack(self, **kw) -> None:  # pragma: no cover - 卡片一律用 place 排版
        self.canvas.pack(**kw)

    def bind(self, sequence=None, func=None, **_kw) -> None:
        """把事件绑到卡片的每一层（点在文字上也算点在卡片上）。"""
        for widget in getattr(self, "_widgets", [self.canvas]):
            widget.bind(sequence, func)

    def cget(self, key):
        if str(key) == "text":
            return self.term
        if str(key) == "summary":
            return self.summary
        return self.canvas.cget(key)

    def configure(self, cnf=None, **kw):
        if isinstance(cnf, dict):
            kw = {**cnf, **kw}
        text = kw.pop("text", None)
        if text is not None:
            self.set_text(str(text))
        if kw:
            self.canvas.configure(**kw)
        return None

    config = configure

    def set_text(self, text: str) -> None:
        self.term = str(text or "")
        try:
            self.label.configure(text=self.term)
        except tk.TclError:  # pragma: no cover
            pass

    def winfo_reqwidth(self) -> int:
        return int(self.width)

    def winfo_reqheight(self) -> int:
        return int(self.height)

    def winfo_children(self) -> list:
        kids: list = [self.label]
        if self.summary_label is not None:
            kids.append(self.summary_label)
        return kids

    def destroy(self) -> None:
        try:
            self.canvas.destroy()
        except tk.TclError:  # pragma: no cover
            pass


class ScrollRail:
    """右侧细滑轨（Canvas 自绘圆角滑块；无箭头、无说明字）。

    与 ``tk.Scrollbar`` 的协议一致：``set(first, last)`` 接收 viewport 分数，
    拖动/点击时用 ``command("moveto", fraction)`` 通知宿主（``Canvas.yview`` /
    ``Text.yview`` 都支持 ``moveto``）。这里的 ``fraction`` 是**内容坐标**
    （0..1 的 ``first``），由滑块行程比例换算得到 —— 滑块走到底 = 内容末尾，
    详见 :meth:`_move_to`。

    三条滑动路径：宿主绑定的滚轮、拖滑块、点击轨道。内容不足一屏
    （``last - first >= 1``）时**不画滑块、也不接受拖动**，并且所有分数换算
    都对 ``span == 0`` / ``span == 1`` 做了除零保护。
    """

    def __init__(self, parent, *, command=None, width: int | None = None,
                 bg: str | None = None, thumb: str | None = None,
                 active: str | None = None, radius: int | None = None,
                 min_thumb: int | None = None):
        self.command = command
        self.width = theme.px(width or geo.RAIL_W)
        self.min_thumb = theme.px(min_thumb or geo.RAIL_MIN_THUMB)
        self.radius = int(radius if radius is not None else theme.px(geo.RAIL_RADIUS))
        self._thumb_color = thumb or theme.RAIL_THUMB
        self._active_color = active or theme.RAIL_THUMB_ACTIVE
        self._first = 0.0
        self._last = 1.0
        self._drag_offset = 0.0
        self._dragging = False
        self._active_now = False
        #: 假 Tk / 未映射时 ``winfo_height`` 拿不到真实高度，用这个兜底
        self.height_hint = 0

        # ``height=0`` 必须显式写死：Tk Canvas 的**默认高度是 7cm**，在 144 DPI
        # （``tk scaling = 2.0``）下等于 **397 设备像素**。滑轨自己总是纵向填充
        # （``pack(side="right", fill="y")``），真实高度由容器决定，但它那 397px 的
        # *请求*高度会经 pack 传播成整个 ``ScrollArea.frame`` 的请求高度：
        # 详情页的对话区是 ``fill="x"``（**不** expand），于是它被撑到 397px 高，
        # 把最后 pack 的释义区挤成 0、词条标题挤成几像素 —— 正是用户报告的
        # 「浮窗文字被边框遮挡 / 释义不显示」。写成 ``height=0`` 后请求高度是 1px，
        # 容器高度只由正文 Canvas（``_sync_detail_areas`` 设定）决定。
        self.canvas = tk.Canvas(parent, width=self.width, height=0,
                                bg=bg or theme.PANEL,
                                highlightthickness=0, bd=0, cursor="arrow")
        self.canvas.bind("<Configure>", lambda _e: self.redraw())
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Enter>", lambda _e: self._set_active(True))
        self.canvas.bind("<Leave>", lambda _e: self._set_active(False))

    # ------------------------------------------------------------ 容器接口
    def pack(self, **kw) -> None:
        self.canvas.pack(**kw)

    def pack_forget(self) -> None:
        self.canvas.pack_forget()

    def bind(self, sequence=None, func=None, **_kw) -> None:
        self.canvas.bind(sequence, func)

    def winfo_height(self) -> int:
        return int(self.track_height())

    def cget(self, key):
        return self.canvas.cget(key)

    def destroy(self) -> None:
        try:
            self.canvas.destroy()
        except tk.TclError:  # pragma: no cover
            pass

    # ------------------------------------------------------------ 分数状态
    def set(self, first, last) -> bool:
        """接收 ``yscrollcommand`` 的 viewport 分数（幂等：没变就不重画）。"""
        try:
            f, l = float(first), float(last)
        except (TypeError, ValueError):
            return False
        if f != f or l != l:              # NaN 兜底
            return False
        f = min(max(f, 0.0), 1.0)
        l = min(max(l, 0.0), 1.0)
        if l < f:
            f, l = l, f
        if (f, l) == (self._first, self._last):
            return False
        self._first, self._last = f, l
        self.redraw()
        return True

    def get(self) -> tuple[float, float]:
        return (self._first, self._last)

    def fraction(self) -> float:
        """滑块当前的**顶端分数**（拖动时用它换算 ``moveto``）。"""
        span = self._span()
        if span >= 1.0 or span <= 0.0:
            return 0.0
        return min(max(self._first / (1.0 - span), 0.0), 1.0)

    def _span(self) -> float:
        return max(0.0, min(1.0, self._last - self._first))

    def scrollable(self) -> bool:
        """内容是否**真的**超出一屏：只有 ``0 < span < 1`` 才可滚动。

        * ``span >= 1``（内容不足一屏 / 正好一屏）→ 不滚动、不画滑块；
        * ``span <= 0``（空内容：有些宿主会给出 ``set(0, 0)``）→ 同样不滚动。

        与 :meth:`thumb_rect` / :meth:`_move_to` 的判据保持一致，
        三种滑动路径（滚轮 / 拖滑块 / 点轨道）因此都不会在空内容上乱动。
        """
        span = self._span()
        return 0.0 < span < 0.999

    # ------------------------------------------------------------ 几何
    def track_height(self) -> int:
        """滑轨像素高度（真实 Tk 取控件高度；假 Tk 用 height_hint 兜底）。"""
        height = 0
        try:
            height = int(self.canvas.winfo_height())
        except (tk.TclError, TypeError, ValueError):  # pragma: no cover
            height = 0
        if height <= 1:
            height = int(self.height_hint or 0)
        if height <= 1:
            height = theme.px(120)
        return max(1, height)

    def thumb_rect(self, track: int | None = None) -> tuple[int, int]:
        """返回 ``(滑块顶端 y, 滑块高度)``：随 viewport 与内容变化。"""
        height = int(track if track is not None else self.track_height())
        height = max(1, height)
        span = self._span()
        if span >= 0.999 or span <= 0.0:
            # 不足一屏 / 空内容：滑块占满整条轨道（也可视为「无处可拖」）
            return (0, height)
        thumb = max(self.min_thumb, int(round(height * span)))
        thumb = max(1, min(thumb, height))
        room = height - thumb
        top = int(round(room * self.fraction()))
        return (max(0, min(top, room)), thumb)

    def redraw(self) -> None:
        """只重画滑块 item（不重建控件，滚动/缩放都不会闪）。"""
        try:
            self.canvas.delete("rail")
        except tk.TclError:  # pragma: no cover
            return
        if not self.scrollable():
            return
        track = self.track_height()
        top, height = self.thumb_rect(track)
        color = self._active_color if (self._active_now or self._dragging) \
            else self._thumb_color
        inset = max(1, (self.width - self.radius * 2) // 2)
        self.canvas.create_polygon(
            round_rect_points(inset, top + 1, max(2, self.width - 2 * inset),
                              max(2, height - 2), self.radius),
            fill=color, outline=color, tags="rail",
        )

    # ------------------------------------------------------------ 事件
    def _set_active(self, value: bool) -> None:
        value = bool(value)
        if value == self._active_now:
            return
        self._active_now = value
        self.redraw()

    def _on_press(self, event):
        if not self.scrollable():
            return None
        y = int(getattr(event, "y", 0) or 0)
        top, height = self.thumb_rect()
        self._dragging = True
        if top <= y <= top + height:
            self._drag_offset = y - top
        else:
            # 点轨道：滑块中心直接落到点击处（滚轮/拖动之外的第三条路径）
            self._drag_offset = height / 2.0
            self._move_to(y)
        self.redraw()
        return "break"

    def _on_motion(self, event):
        if not self._dragging or not self.scrollable():
            return None
        self._move_to(int(getattr(event, "y", 0) or 0))
        return "break"

    def _on_release(self, _event=None):
        self._dragging = False
        self.redraw()
        return "break"

    def _move_to(self, y: int) -> float:
        """把像素位置映射成 ``moveto`` 分数并发给宿主（含除零保护）。

        两套分数必须分清（旧实现把两者当成同一个，滑块拖到一半只能滚到内容
        一半，长列表的下半截永远够不着）：

        * **滑块行程比例** ``travel`` = 滑块走过的像素 / 轨道里可走的像素；
        * ``yview("moveto", first)`` 要的是**内容坐标** ``first``。

        两者的换算是 ``first = travel * (1 - span)``，其中
        ``span = last - first`` 是视口占内容的比例：内容不足一屏 / 为空
        （``span >= 1`` 或 ``span <= 0``）时**没有任何可滚动的量**，
        这里一次 ``moveto`` 都不发（拖动 / 点击本来也被 :meth:`scrollable`
        挡住），返回 ``0.0``。
        """
        span = self._span()
        if span <= 0.0 or span >= 0.999:
            return 0.0
        track = self.track_height()
        _top, height = self.thumb_rect(track)
        room = max(1.0, float(track - height))
        travel = (float(y) - float(self._drag_offset)) / room
        travel = min(max(travel, 0.0), 1.0)
        first = travel * (1.0 - span)
        emit = self.command
        if callable(emit):
            emit("moveto", first)
        return first


def divider(parent, *, pady: int = 0) -> tk.Frame:
    """1px 分隔线（editorial：border = #1C1C1C/10）。"""
    line = tk.Frame(parent, bg=theme.BORDER, height=1)
    line.pack(fill="x", pady=theme.px(pady))
    return line


def hairline(parent, **pack_kw) -> tk.Frame:
    """不自动布局的 1px 细线（调用方自己 pack）。"""
    return tk.Frame(parent, bg=theme.BORDER, height=1)


# =========================================================== 窗口描边 / 手柄
def window_border_box(width: int, height: int, radius: int, inset: int = geo.WINDOW_BORDER):
    """窗口 1px 圆角描边的落点 ``(x, y, w, h, radius)``（**纯函数**）。

    描边必须画成**圆角**、而不是方形 Frame 的 ``highlight``：方形边线被窗口
    region 裁掉之后，四个角上的线会突然断掉 —— 用户看到的就是「圆角像缺口」。
    """
    inset = max(0, int(inset))
    w = max(1, int(width) - 2 * inset)
    h = max(1, int(height) - 2 * inset)
    r = max(0, min(int(radius) - inset, w // 2, h // 2))
    return (inset + 0.5, inset + 0.5, w - 1, h - 1, r)


def grip_lines(width: int, height: int, *, inset: int | None = None,
               steps=geo.GRIP_STEPS) -> list[tuple[float, float, float, float]]:
    """右下角改尺寸手柄的 3 条灰色斜线端点（相对控件左上角，**纯函数**）。

    端点的连线与窗口对角线平行、整体**向内收** ``inset``，因此全部落在圆角
    半径以内（见 ``tests/test_ui_roundrect.py`` 的 ``rounded_contains`` 断言）：
    旧版那个贴角的方形边框手柄会被 region 裁成「黑角 / 缺口」，这里不会再出现。
    """
    pad = geo.GRIP_INSET if inset is None else int(inset)
    right = float(width) - float(pad)
    bottom = float(height) - float(pad)
    out: list[tuple[float, float, float, float]] = []
    for step in steps:
        k = float(step)
        out.append((right - k, bottom, right, bottom - k))
    return out


class ResizeGrip:
    """右下角改尺寸手柄：向内收的 3 条灰色斜线 + 整块拖拽命中区。

    为什么不用 ``tk.Frame(highlightthickness=1)``
    --------------------------------------------
    方形边框正好压在窗口圆角 region 的裁剪边缘上，被裁掉之后剩下的直角线就是
    用户看到的「右下黑角 / 圆角缺口」。这里：

    * 控件**没有边框**、底色与窗口一致（``bg=theme.PANEL``），被 region 裁掉的
      部分和面板同色，看不出任何接缝；
    * 可见墨迹是 3 条向内收的斜线，最外一条距边缘 ``GRIP_INSET``，整组线都在
      圆角半径以内；
    * 整个 ``GRIP_SIZE x GRIP_SIZE`` 方块都是拖拽命中区（边缘拖拽仍然好点）。
    """

    def __init__(self, parent, *, on_start=None, on_motion=None, on_end=None,
                 size: int | None = None, bg: str | None = None):
        self.size = theme.px(size or geo.GRIP_SIZE)
        self._bg = bg or theme.PANEL
        self.canvas = tk.Canvas(parent, width=self.size, height=self.size, bg=self._bg,
                                highlightthickness=0, bd=0, cursor="size_nw_se")
        if on_start is not None:
            self.canvas.bind("<Button-1>", on_start)
        if on_motion is not None:
            self.canvas.bind("<B1-Motion>", on_motion)
        if on_end is not None:
            self.canvas.bind("<ButtonRelease-1>", on_end)
        self._color = theme.RAIL_THUMB
        self._lines: list = []
        self.redraw()

    # ------------------------------------------------------------ 几何
    def redraw(self) -> bool:
        """重画 3 条斜线（幂等；控件尺寸没变时结果完全一致）。"""
        try:
            self.canvas.delete("grip")
        except tk.TclError:  # pragma: no cover - 控件已销毁
            return False
        self._lines = grip_lines(self.size, self.size)
        for x1, y1, x2, y2 in self._lines:
            try:
                self.canvas.create_line(x1, y1, x2, y2, fill=self._color,
                                        width=max(1, theme.px(1)), tags="grip")
            except tk.TclError:  # pragma: no cover
                return False
        return True

    def line_count(self) -> int:
        """当前画了几条斜线（回归断言用）。"""
        return len(self._lines)

    # ------------------------------------------------------------ 容器接口
    def place(self, **kw) -> None:
        self.canvas.place(**kw)

    place_configure = place

    def place_forget(self) -> None:
        self.canvas.place_forget()

    def bind(self, sequence=None, func=None, **_kw) -> None:
        self.canvas.bind(sequence, func)

    def cget(self, key):
        return self.canvas.cget(key)

    def destroy(self) -> None:
        try:
            self.canvas.destroy()
        except tk.TclError:  # pragma: no cover
            pass


def clear_children(widget) -> None:
    """销毁子控件（重建列表 / 聊天区之前调用）。"""
    for child in widget.winfo_children():
        try:
            child.destroy()
        except tk.TclError:  # pragma: no cover
            pass


#: 自绘标题带高度（逻辑像素）
CHROME_H = 30
#: 可缩放的无框窗口最小尺寸（逻辑像素）
CHROME_MIN_W = 460
CHROME_MIN_H = 340

#: 真正接受键盘输入的 Tk 控件类名（``winfo_class()`` 的取值）。
#: 点这些控件时键盘焦点必须落在控件本身，绝不能被窗口级绑定抢回顶层窗口。
INPUT_WIDGET_CLASSES = frozenset({
    "Entry", "Text", "Spinbox", "TEntry", "TCombobox", "TSpinbox",
})
#: 按钮类控件：点按钮只干活，不迁移键盘焦点（否则会从输入框抢走焦点）。
BUTTON_WIDGET_CLASSES = frozenset({"Button", "TButton"})

#: 主题行右侧 chevron 的默认尺寸（设备像素）
CHEVRON_W = 9
CHEVRON_H = 5


def chevron_points(cx: float, cy: float, *, width: float | None = None,
                   height: float | None = None, direction: str = "down",
                   pad: float = 0.0) -> tuple[tuple[float, float], ...]:
    """chevron（展开箭头）折线的三个顶点 —— **纯函数**。

    产品 Canvas（:class:`Chevron`）与 ``tools/ui_preview.py`` 的离线预览共用
    这一份几何：不再用 ``▾`` 这类特殊字形，Pillow 与 Tk 都不会画出缺字方框。
    ``direction="down"`` 是「可展开」，``"up"`` 是「可收起」。

    ``pad`` 是**半描边净空**：端点若正好落在画布边缘，线宽的一半会被裁掉，
    箭头看起来像缺了角。传 ``线宽 / 2`` 就能把整条折线收进画布内部。
    """
    half_w = max(1.0, float(CHEVRON_W if width is None else width)) / 2.0
    half_h = max(1.0, float(CHEVRON_H if height is None else height)) / 2.0
    inset = max(0.0, float(pad))
    half_w = max(0.5, half_w - inset)
    half_h = max(0.5, half_h - inset)
    sign = 1.0 if str(direction) == "down" else -1.0
    return ((float(cx) - half_w, float(cy) - sign * half_h / 2.0),
            (float(cx), float(cy) + sign * half_h / 2.0),
            (float(cx) + half_w, float(cy) - sign * half_h / 2.0))


def chevron_flat_points(cx: float, cy: float, *, width: float | None = None,
                        height: float | None = None, direction: str = "down",
                        pad: float = 0.0) -> tuple[float, ...]:
    """:func:`chevron_points` 的摊平形式（``Canvas.create_line`` 直接吃这串）。"""
    flat: list[float] = []
    for x, y in chevron_points(cx, cy, width=width, height=height,
                               direction=direction, pad=pad):
        flat.extend((x, y))
    return tuple(flat)


class Chevron:
    """自绘 chevron 小控件（**线条**，不依赖任何字体字形）。

    尺寸固定为 ``width × height``（设备像素），位置由 ``pack`` / ``place`` 决定；
    ``bind("<Button-1>", ...)`` 与普通控件完全一致，因此主题行既能拖动也能点击。

    为什么是**组合**而不是 ``tk.Canvas`` 子类
    ----------------------------------------
    Tk 把控件自己的 Tcl 窗口路径记在 ``_w`` 上，``create_line`` / ``bind`` /
    ``pack`` 全部靠它去找窗口。子类里写 ``self._w = 尺寸`` 会把这个路径覆盖成
    一个浮点数：实机上后面每一条画线命令都在找不存在的窗口（箭头根本不出现），
    而假 Tk 测试只记录属性、看不出这条命令路径是坏的。因此这里与
    :class:`ScrollRail` / :class:`ResizeGrip` 同一套写法：普通 Python 包装类持有
    一个 ``tk.Canvas``，只代理需要的 ``pack`` / ``bind`` / ``configure`` /
    ``cget``；尺寸字段用**任务专名** ``_chevron_width`` / ``_chevron_height``，
    绝不碰 Tk 的 ``_w`` / ``tk`` / ``master`` / ``children``。
    """

    def __init__(self, parent, *, width: int = CHEVRON_W, height: int = CHEVRON_H,
                 bg: str | None = None, fg: str | None = None, cursor: str | None = None):
        #: 画布逻辑尺寸（设备像素）—— 任务专名，绝不复用 Tk 保留属性
        self._chevron_width = float(theme.px(width))
        self._chevron_height = float(theme.px(height))
        self._fg = fg or theme.TEXT_FAINT
        self._direction = "down"
        self._line = max(1, theme.px(1))
        self.canvas = tk.Canvas(parent, width=theme.px(width), height=theme.px(height),
                                bg=bg or theme.PANEL, highlightthickness=0, bd=0,
                                **({"cursor": cursor} if cursor else {}))
        #: 最近一次真正画出去的折线坐标（回归断言用：产品真的走画线路径）
        self.last_points: tuple[float, ...] = ()
        self.draw_calls = 0
        self.draw()

    # ------------------------------------------------------------ 容器接口
    def pack(self, **kw) -> None:
        self.canvas.pack(**kw)

    pack_configure = pack

    def pack_forget(self) -> None:
        self.canvas.pack_forget()

    def place(self, **kw) -> None:
        self.canvas.place(**kw)

    place_configure = place

    def place_forget(self) -> None:
        self.canvas.place_forget()

    def bind(self, sequence=None, func=None, **_kw) -> None:
        self.canvas.bind(sequence, func)

    def configure(self, cnf=None, **kw):
        if isinstance(cnf, dict):
            kw = {**cnf, **kw}
        return self.canvas.configure(**kw) if kw else None

    config = configure

    def cget(self, key):
        return self.canvas.cget(key)

    def winfo_width(self) -> int:
        return int(self._chevron_width)

    def winfo_height(self) -> int:
        return int(self._chevron_height)

    def winfo_reqwidth(self) -> int:
        return int(self._chevron_width)

    def winfo_reqheight(self) -> int:
        return int(self._chevron_height)

    def destroy(self) -> None:
        try:
            self.canvas.destroy()
        except tk.TclError:  # pragma: no cover
            pass

    # ------------------------------------------------------------ 绘制
    def set_direction(self, direction: str) -> bool:
        """切换朝向（展开 ↓ / 收起 ↑）；朝向没变就不重画。"""
        want = "down" if str(direction) != "up" else "up"
        if want == self._direction:
            return False
        self._direction = want
        self.draw()
        return True

    def direction(self) -> str:
        return self._direction

    def draw(self) -> None:
        """按同一份 :func:`chevron_points` 几何重画折线（不写任何字形）。"""
        pad = self._line / 2.0            # 半描边净空：端点不贴画布边缘
        flat = chevron_flat_points(self._chevron_width / 2.0, self._chevron_height / 2.0,
                                   width=self._chevron_width,
                                   height=self._chevron_height,
                                   direction=self._direction, pad=pad)
        try:
            self.canvas.delete("chevron")
            self.canvas.create_line(list(flat), fill=self._fg, width=self._line,
                                    capstyle="round", joinstyle="round",
                                    tags=("chevron",))
        except tk.TclError:  # pragma: no cover - 窗口已销毁
            return
        self.last_points = tuple(flat)
        self.draw_calls += 1


class BorderlessChrome:
    """自绘无框 chrome：主界面 / 导图 / 设置 / 手动录入**共用同一份**。

    原生 Windows 标题栏、边框与菜单栏一律不要（用户明确要求「简约统一」）：
    ``overrideredirect(True)`` 之后由本类自己提供标题带与关闭入口。语义统一：

    * **拖动**：标题带本体、标题文字、标题带上的空白区域（按钮不绑拖动）；
    * **×**：调用宿主给的 ``on_close`` —— 主窗口是「只收起、后台继续」，
      子窗口是「关掉本窗」，**都不退出程序**；
    * **Esc / Alt+F4**：与「×」同一语义（``on_close`` 由宿主决定）；
    * **缩放**（可选）：右下角 :class:`ResizeGrip`，不小于 ``min_w × min_h``；
    * 拿不到 ``overrideredirect`` 的替身窗口（假 Tk 测试环境）自动跳过这一步，
      因此本类可以在零真实窗口的回归测试里跑。
    """

    def __init__(self, win, *, title: str, on_close, on_minimize=None,
                 resizable: bool = False, min_w: int = CHROME_MIN_W,
                 min_h: int = CHROME_MIN_H, bg: str | None = None,
                 on_resized=None):
        self.win = win
        self.on_close = on_close
        self.on_minimize = on_minimize
        self.on_resized = on_resized
        self.title_text = str(title or "")
        self.min_w = theme.px(min_w)
        self.min_h = theme.px(min_h)
        self._resize: tuple[int, int, int, int] | None = None
        self._enabled = True
        bar_bg = bg or theme.PANEL

        self.frame = tk.Frame(win, bg=bar_bg)
        self.frame.pack(fill="x", side="top")
        self.bar = tk.Frame(self.frame, bg=bar_bg, height=theme.px(CHROME_H),
                            cursor="hand2")
        self.bar.pack(fill="x")
        self.bar.pack_propagate(False)      # 固定行高：标题带不被内容撑高 / 压扁
        self.title_label = tk.Label(self.bar, text=self.title_text, bg=bar_bg,
                                    fg=theme.TEXT, font=theme.font(9), anchor="w",
                                    cursor="hand2")
        self.title_label.pack(side="left", padx=(theme.px(12), theme.px(6)))
        self.btn_close = FlatButton(self.bar, "×", self.close, font_size=9, padx=7, pady=1)
        self.btn_close.pack(side="right", padx=(theme.px(2), theme.px(6)),
                            pady=theme.px(3))
        self.btn_min = None
        if callable(on_minimize):
            self.btn_min = FlatButton(self.bar, "—", self.minimize, font_size=9,
                                      padx=7, pady=1)
            self.btn_min.pack(side="right", padx=theme.px(2), pady=theme.px(3))
        hairline(self.frame).pack(fill="x", side="bottom")

        for widget in (self.frame, self.bar, self.title_label):
            self._bind_drag(widget)
        for sequence in ("<Escape>", "<Alt-F4>"):
            try:
                win.bind(sequence, self._on_dismiss, add="+")
            except (tk.TclError, AttributeError, TypeError):  # pragma: no cover
                continue
        # 无框窗口在 Windows 上不会自动被「点一下就激活」：点窗口任意处补一次
        # 窗口激活，但**键盘焦点落在真正被点的输入控件上**（见 focus_click）——
        # 窗口级绑定在 Tk 的类绑定之后执行，早先无条件 ``win.focus_force()``
        # 会把 Entry 刚拿到的焦点又抢回顶层窗口，实机表现就是「搜索框点进去
        # 打不了字」。按钮不迁移焦点，空白处只在窗口确实没焦点时激活一次。
        try:
            win.bind("<Button-1>", self._on_any_click, add="+")
        except (tk.TclError, AttributeError, TypeError):  # pragma: no cover
            pass

        self.grip = None
        if resizable:
            self.grip = ResizeGrip(win, on_start=self._on_resize_start,
                                   on_motion=self._on_resize_motion,
                                   on_end=self._on_resize_end, bg=bar_bg)
            self.grip.place(relx=1.0, rely=1.0, anchor="se",
                            width=theme.px(geo.GRIP_SIZE),
                            height=theme.px(geo.GRIP_SIZE))

        remover = getattr(win, "overrideredirect", None)
        if callable(remover):
            try:
                remover(True)               # 去掉原生标题栏 / 边框
            except tk.TclError:  # pragma: no cover
                pass

    # ---------------------------------------------------------------- 交互
    def _bind_drag(self, widget) -> None:
        for sequence, handler in (("<Button-1>", self._on_drag_start),
                                  ("<B1-Motion>", self._on_drag_motion),
                                  ("<ButtonRelease-1>", self._on_drag_end)):
            try:
                widget.bind(sequence, handler)
            except (tk.TclError, AttributeError):  # pragma: no cover
                continue

    def enable(self, value: bool = True) -> None:
        """窗口被销毁 / 正在关闭时关掉交互（幂等）。"""
        self._enabled = bool(value)

    def _on_drag_start(self, event) -> None:
        if not self._enabled:
            return
        self._drag = (int(event.x_root), int(event.y_root),
                      int(self._root_x()), int(self._root_y()))

    def _on_drag_motion(self, event) -> None:
        drag = getattr(self, "_drag", None)
        if not self._enabled or drag is None:
            return
        ox, oy, x0, y0 = drag
        x = x0 + int(event.x_root) - ox
        y = y0 + int(event.y_root) - oy
        try:
            self.win.geometry(f"+{int(x)}+{int(y)}")
        except tk.TclError:  # pragma: no cover
            return

    def _on_drag_end(self, _event=None) -> None:
        self._drag = None

    def _root_x(self) -> int:
        try:
            return int(self.win.winfo_rootx())
        except (tk.TclError, TypeError, ValueError):  # pragma: no cover
            return 0

    def _root_y(self) -> int:
        try:
            return int(self.win.winfo_rooty())
        except (tk.TclError, TypeError, ValueError):  # pragma: no cover
            return 0

    def _on_dismiss(self, _event=None) -> str:
        """Esc / Alt+F4：与「×」同一语义（宿主决定收起还是关掉本窗）。"""
        if self._enabled:
            self.close()
        return "break"

    # ---------------------------------------------------------- 点击 / 焦点
    @staticmethod
    def widget_class(widget) -> str:
        """控件的 Tk 类名（``Entry`` / ``Text`` / ``TButton`` …）；拿不到就当空串。"""
        try:
            return str(widget.winfo_class() or "")
        except Exception:  # noqa: BLE001 - 已销毁 / 极简替身
            return ""

    @classmethod
    def is_input_widget(cls, widget) -> bool:
        """是不是**可输入**控件（Entry / Text / 下拉框 …）。

        先按真实 Tk 类型判断，再按 ``winfo_class()`` 兜底：假 Tk 环境里的替身
        不是真的 ``tk.Entry``（``tkinter.Entry`` 那时已被换成工厂），但会如实
        报告类名，因此产品代码与回归用的是同一条判据。
        """
        if widget is None:
            return False
        for tk_type in (tk.Entry, tk.Text):
            try:
                if isinstance(widget, tk_type):
                    return True
            except TypeError:  # pragma: no cover - 被换掉的工厂不是类型
                continue
        return cls.widget_class(widget) in INPUT_WIDGET_CLASSES

    @classmethod
    def is_button_widget(cls, widget) -> bool:
        """是不是按钮（点它只干活：**不得**把键盘焦点抢到窗口上）。"""
        if widget is None:
            return False
        if cls.widget_class(widget) in BUTTON_WIDGET_CLASSES:
            return True
        return callable(getattr(widget, "invoke", None))

    @classmethod
    def click_target(cls, event):
        """这次点击真正该拿键盘焦点的**可输入控件**；否则 ``None``。

        从被点控件沿 ``master`` 链向上找：先碰到输入控件 → 返回它；先碰到按钮
        → 返回 ``None``（按钮不迁移键盘焦点）；空白 / 标题带也返回 ``None``。
        """
        widget = getattr(event, "widget", None)
        for _ in range(64):                     # 链上环状 self 引用时也不会死循环
            if widget is None:
                return None
            if cls.is_input_widget(widget):
                return widget
            if cls.is_button_widget(widget):
                return None
            widget = getattr(widget, "master", None)
        return None                          # pragma: no cover - 理论到不了

    def window_has_focus(self) -> bool:
        """键盘焦点是否已经落在**本窗内的控件**上（含子控件 / 已销毁的窗口）。"""
        try:
            focused = self.win.focus_get()
        except Exception:  # noqa: BLE001 - 假 Tk / 已销毁
            return False
        if focused is None:
            return False
        widget = focused
        for _ in range(64):
            if widget is self.win:
                return True
            if widget is None:
                return False
            widget = getattr(widget, "master", None)
        return False                         # pragma: no cover - 理论到不了

    def _focus_widget(self, widget) -> None:
        setter = getattr(widget, "focus_set", None)
        if not callable(setter):
            setter = getattr(widget, "focus_force", None)
        if not callable(setter):  # pragma: no cover - 极简替身
            return
        try:
            setter()
        except tk.TclError:  # pragma: no cover - 控件已销毁
            pass

    def _activate_window(self) -> None:
        """无框窗口不会自动激活：这里只补一次窗口级的键盘焦点。"""
        try:
            self.win.focus_force()
        except tk.TclError:  # pragma: no cover - 窗口已销毁
            pass

    def focus_click(self, event=None):
        """点窗口任意处的**焦点落点**（返回这次拿焦点的控件；``None`` = 没迁移）。

        语义（用户要求，别改回去）：

        * 点 ``Entry`` / ``Text`` 这类可输入控件：窗口照样激活，但键盘焦点
          **最终落在被点的输入控件上** —— Tk 的类绑定先把焦点给输入框，窗口级
          绑定（本方法）再跑，所以这里必须重新把焦点放回输入框，否则搜索 / API
          字段点进去打不了字；
        * 点按钮：只让按钮干活，**不**把键盘焦点抢到窗口 root；
        * 点空白 / 标题带：只在窗口还没有键盘焦点时才激活一次（不重复
          ``focus_force``，避免抢走别处的输入焦点）。
        """
        if not self._enabled:
            return None
        target = self.click_target(event)
        if target is not None:
            self._activate_window()
            self._focus_widget(target)
            return target
        if self.is_button_widget(getattr(event, "widget", None)):
            return None
        if not self.window_has_focus():
            self._activate_window()
        return None

    def _on_any_click(self, event=None):
        """``<Button-1>`` 的窗口级兜底（见 :meth:`focus_click`）。"""
        return self.focus_click(event)

    def close(self, _event=None) -> None:
        if not self._enabled:
            return
        callback = self.on_close
        if callable(callback):
            try:
                callback()
            except Exception:  # pragma: no cover - 关闭失败不该抛给 Tk
                pass

    def minimize(self, _event=None) -> None:
        if not self._enabled:
            return
        callback = self.on_minimize
        if callable(callback):
            try:
                callback()
            except Exception:  # pragma: no cover
                pass

    def set_title(self, text: str) -> None:
        self.title_text = str(text or "")
        try:
            self.title_label.configure(text=self.title_text)
        except tk.TclError:  # pragma: no cover
            pass

    # ---------------------------------------------------------------- 缩放
    def _size_now(self) -> tuple[int, int]:
        try:
            return (max(1, int(self.win.winfo_width())),
                    max(1, int(self.win.winfo_height())))
        except (tk.TclError, TypeError, ValueError):  # pragma: no cover
            return (self.min_w, self.min_h)

    def _on_resize_start(self, event) -> None:
        w, h = self._size_now()
        self._resize = (int(event.x_root), int(event.y_root), w, h)

    def _on_resize_motion(self, event) -> None:
        if self._resize is None:
            return
        ox, oy, w0, h0 = self._resize
        w = max(self.min_w, w0 + int(event.x_root) - ox)
        h = max(self.min_h, h0 + int(event.y_root) - oy)
        try:
            self.win.geometry(f"{int(w)}x{int(h)}")
        except tk.TclError:  # pragma: no cover
            return

    def _on_resize_end(self, _event=None) -> None:
        if self._resize is None:
            return
        self._resize = None
        callback = self.on_resized
        if callable(callback):
            w, h = self._size_now()
            try:
                callback(w, h)
            except Exception:  # pragma: no cover - 重排失败不该抛给 Tk
                pass

