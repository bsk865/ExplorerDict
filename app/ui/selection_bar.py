"""选区操作小浮条：**只有**一个按钮「解释并记录」。

.. note::
   **本轮起产品运行时不再使用本模块** —— 阅读面板 :class:`app.ui.reading_panel.
   ReadingPanel` 用**同一个窗口**同时承担「小方块 / 展开浮窗」两个角色
   （``app.selection_bar is app.explain_window``），用户只看到一个面板。
   本模块作为**保留的兼容实现**留在这里：它承载「唯一按钮」的交互契约，
   并且仍有回归覆盖（``tests.test_unified_action`` 的静态契约与门控探针），
   删除它会丢失这部分可审计的约束。

交互契约（用户明确要求，勿再拆成两个按钮）
------------------------------------------
* 位置：紧贴本次选区的鼠标抬起点附近（UIA 给了选区矩形就用它），
  贴边时自动翻到另一侧；窗口**真正置顶**且 ``SW_SHOWNOACTIVATE`` 不抢焦点。
* **划选本身**：只把内存快照挂到浮条上并在选区旁置顶显示 ——
  不写 SQLite、不建批次、不发任何网络请求。
* **唯一按钮「解释并记录」**：幂等落库（关键词 + 来源 / 上下文），
  然后异步解释并写回**同一个 entry_id**（见 ``App.explain_and_record_selection``）。
  同一个选区被连续点击只记录一次，不会重复记词、不会再加词频。
* 隐藏时机：普通鼠标单击其它区域（**不保存**）、点击本按钮、新选区为空、
  前台窗口 / 标签页切换、门控进入硬阻断（游戏 / 全屏 / 用户暂停）。
* **没有第三个「关闭」按钮**：浮条是 NOACTIVATE 无焦点窗口，键盘 Esc 送不到它这里，
  因此**不承诺** Esc 收起（收起靠「点别处」与上述自动时机）。
* 「点在浮层上」由**鼠标按下的那一刻**的 Win32 元信息判定
  （``CaptureService.handle_mouse_press`` 里的 ``is_overlay``），
  既不依赖浮条是否还在屏幕上，也不用任何时间宽限。
* 按钮回调自带**可见性 + 忙碌 + 已消费**三道守卫：浮条被门控/点别处收起之后
  迟到的 ``<Button-1>`` 回调、以及同一次选区的连续重复回调，都只会操作一次
  （真正的幂等落库在 ``App._record_snapshot_once``，这里是 UI 层的第二道闸）。
* 相同的选词/位置重复显示时，一次 Win32 调用都不做（不重建、不重复映射）。
"""
from __future__ import annotations

import tkinter as tk

from .. import win32util as w32
from . import theme
from .floating import FloatingWindow

#: 浮条出现在选区右侧/下方的偏移
OFFSET_X = 8
OFFSET_Y = 10

#: 浮条上唯一的按钮文案
ACTION_TEXT = "解释并记录"


class SelectionBar(FloatingWindow):
    """选词后出现在选区旁的单个按钮的小浮条。"""

    def __init__(self, master: tk.Misc, app):
        super().__init__(master, app, name="selection_bar")
        self.selection = None          # 当前这份内存快照（未落库）
        self.token: int = 0            # 这份快照的编号（选区序号）
        #: 已经点过按钮的选区序号：同一个 token 的连续重复回调只操作一次
        self._consumed_token: int | None = None
        #: 是否正在处理这一次点击（防重入）
        self._busy = False

        outer = tk.Frame(self.win, bg=theme.PANEL)
        outer.pack(fill="both", expand=True, padx=1, pady=1)
        self._body = outer

        row = tk.Frame(outer, bg=theme.PANEL)
        row.pack(fill="x")

        self.term_label = tk.Label(
            row, text="", bg=theme.PANEL, fg=theme.TEXT, font=theme.font(8, bold=True),
            padx=theme.px(7), pady=theme.px(4), anchor="w",
        )
        self.term_label.pack(side="left")

        self.btn_action = self._button(row, ACTION_TEXT, self._on_action, primary=True)

    # ------------------------------------------------------------- 构建
    def _button(self, parent, text, command, primary: bool = False) -> tk.Label:
        """用 Label 当按钮：NOACTIVATE 窗口里不涉及键盘焦点，点击即可触发。

        注意：``tk.Label`` 的 ``state="disabled"`` **不会**拦住 ``<Button-1>``
        绑定，所以回调自己必须有显式守卫（见 :meth:`_on_action`）。
        """
        b = tk.Label(
            parent, text=text, cursor="hand2", font=theme.font(9),
            bg=theme.ACCENT if primary else theme.PANEL_ALT,
            fg=theme.BG if primary else theme.TEXT,
            padx=theme.px(8), pady=theme.px(4),
            highlightthickness=1,
            highlightbackground=theme.ACCENT if primary else theme.BORDER_STRONG,
        )
        b.pack(side="left", padx=(theme.px(2), theme.px(4)), pady=theme.px(3))
        b.bind("<Button-1>", lambda _e: command())
        return b

    def _render(self, text: str) -> None:
        shown = text or (self.selection.term if self.selection else "")
        if len(shown) > 24:
            shown = shown[:24] + "…"
        self.term_label.configure(text=shown)

    # ------------------------------------------------------------- 显示
    def anchor_for(self, selection, point) -> tuple[int, int]:
        """算出浮条左上角：优先选区矩形右下角，否则鼠标抬起点。"""
        rect = getattr(selection, "rect", None)
        if rect:
            left, top, right, bottom = rect
            return (int(right) + OFFSET_X, int(bottom) + OFFSET_Y)
        x, y = point
        return (int(x) + OFFSET_X, int(y) + OFFSET_Y)

    def show_selection(self, selection, token: int, point=(0, 0)) -> bool:
        """显示浮条。**不落库、不发网络**：只把这份内存快照挂上去。

        显示门控只有一处（``FloatingWindow.show_at`` → ``App.overlay_display_allowed``）：
        游戏/全屏/暂停一律拒绝，本程序自己的窗口在前台时也**不显示选区条**
        （用户正在操作界面，浮条冒出来只会碍事）。被拒绝时浮条保持隐藏。
        """
        self.selection = selection
        self.token = int(token)
        if self._consumed_token != self.token:
            self._consumed_token = None       # 新选区：允许再次点击
        try:
            self.btn_action.configure(text=ACTION_TEXT, state="normal")
        except tk.TclError:  # pragma: no cover
            pass
        return self.show_at(self.anchor_for(selection, point), self.token,
                            text=self._term_text(selection))

    @staticmethod
    def _term_text(selection) -> str:
        term = (selection.term or "").strip().replace("\n", " ")
        return term

    def hide(self) -> bool:  # noqa: D102 - 语义同基类，保留快照便于重试
        return super().hide()

    def clear(self) -> None:
        """彻底清空（新选区为空 / 来源切换）：连快照一起丢弃。"""
        self.hide()
        self.selection = None
        self._consumed_token = None

    # ------------------------------------------------------------- 动作
    def _on_action(self) -> None:
        """唯一按钮：解释并记录。

        三道显式守卫（``tk.Label`` 的 disabled 状态拦不住 ``<Button-1>``）：

        1. 必须还挂着一份内存快照；
        2. **浮条必须仍然可见** —— 已经被门控 / 点别处收起的浮条，迟到的按钮
           回调不得再触发落库与解释；
        3. **同一次选区只处理一次**（``_consumed_token`` + ``_busy`` 防重入）——
           连续重复回调（双击、Tk 重复派发）只操作一次。
        """
        selection = self.selection
        if selection is None:
            return
        if not self.visible:
            return
        if self._consumed_token == self.token or self._busy:
            return
        self._busy = True
        try:
            self._consumed_token = int(self.token)
            self.app.explain_and_record_selection(selection, anchor=self._anchor)
        finally:
            self._busy = False

    # ------------------------------------------------------------- 状态
    def reset_action_text(self) -> None:
        try:
            self.btn_action.configure(text=ACTION_TEXT)
        except tk.TclError:  # pragma: no cover
            pass

    def owns_point(self, x: int, y: int) -> bool:
        """屏幕点 (x, y) 是否落在浮条自己身上（诊断 / 旧调用点用）。

        运行时**不**用它做收起判定：那必须用鼠标按下瞬间的 ``is_overlay``。
        """
        hwnd = w32.window_root_from_point(x, y)
        return bool(hwnd) and hwnd == self.hwnd()
