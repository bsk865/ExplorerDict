"""解释结果小窗：稳定、置顶、不抢焦点，**没有任何记录动作**。

.. note::
   **本轮起产品运行时不再使用本模块** —— 阅读面板 :class:`app.ui.reading_panel.
   ReadingPanel` 在同一个窗口里同时承担「已记录状态 + 释义 + 追问」的角色。
   本模块作为**保留的兼容实现**留在这里，并仍有回归覆盖
   （``tests.test_unified_action`` 的「结果窗没有记录动作」静态契约与
   entry + 请求 token 归属探针）。

与操作浮条的分工（最终交互）
----------------------------
* 浮条上唯一的按钮「解释并记录」会**先幂等落库**，再让本窗口显示状态；
* 本窗口只显示：**已记录状态 + 词条 / 释义 + 重试 / 设置 / 关闭**四个部分，
  「记录」按钮已经彻底移除 —— 落库动作只发生在浮条那一次点击里；
* 没有 API Key 时显示「已记录（待解释）」以及**真实可见**的
  「打开设置」/「重新解释」入口；重试沿用同一个 ``entry_id``，
  不会重复记词、不会再加词频；
* 同一个窗口复用（``show_at`` 幂等）：同一个 entry / token 反复显示不会重建/重映射；
* **结果归属 = entry_id + 解释请求 token 双重匹配**（:meth:`show_result`）：
  「重新解释」会生成**新的请求 token**，于是排队中的旧结果既不会覆盖新请求的
  「正在解释…」，也不会把旧释义写回窗口 —— 卡片/详情仍由 ``entry_id`` 单独刷新。
  这里的 token 是**解释服务自己的请求序号**，与选区序号（``selection token``）
  永远不互相比较；
* **绝不重开已隐藏的窗口**：迟到的解释结果只更新内容，不会把用户已经关掉
  或因为新选区 / 门控而收起的窗口重新弹出来；
* **所有显示都过实时门控**（``FloatingWindow.show_at``）：游戏/全屏/用户暂停
  期间一律只隐藏、不映射；本程序自己的窗口在前台时（例如用户点开了设置）
  窗口**保持可见**，不会被 soft gate 收掉 —— 否则「配好 Key 再点重新解释」
  的入口会消失；
* :meth:`restore` 是**显式恢复入口**：把同一个 entry 的待解释窗口找回来，
  只显示、绝不联网；
* 「关闭」是用户显式动作，之后只有**新的**「解释并记录」或者显式
  :meth:`restore` 才会再次显示本窗口。
"""
from __future__ import annotations

import tkinter as tk

from . import theme, widgets
from .floating import FloatingWindow

MAX_BODY_CHARS = 2200

#: 状态文案：窗口标题栏右侧显示的「已记录」状态
STATUS_TEXT = {
    "pending": "已记录 · 解释中…",
    "pending_no_key": "已记录 · 待解释",
    "ok": "已记录 · 已解释",
    "error": "已记录 · 解释失败",
    "stale": "已记录 · 解释已过期",
    "notice": "已记录",
}


class ExplainWindow(FloatingWindow):
    """解释结果小窗（纯展示 + 重试 / 设置 / 关闭）。"""

    def __init__(self, master: tk.Misc, app):
        super().__init__(master, app, name="explain_window")
        self.selection = None
        #: 本窗口正在跟随的**解释请求 token**（解释服务的请求序号）。
        #: 与 ``App._selection_token``（选区序号）是两套互不相干的计数器，
        #: 任何地方都不许拿它们互相比较。
        self.request_token: int | None = None
        #: 上次 ``show_recorded`` 的原文，供 :meth:`restore` 原样恢复
        self.message = ""
        self.result = None
        self.entry_id: int | None = None
        self.status = ""

        outer = tk.Frame(self.win, bg=theme.PANEL)
        outer.pack(fill="both", expand=True, padx=1, pady=1)
        self._body = outer

        header = tk.Frame(outer, bg=theme.ACCENT)
        header.pack(fill="x")
        self.title_label = tk.Label(
            header, text="解释", bg=theme.ACCENT, fg=theme.BG,
            font=theme.font(8, bold=True), padx=theme.px(7), pady=theme.px(3), anchor="w",
        )
        self.title_label.pack(side="left")
        self.close_btn = tk.Label(
            header, text="✕", bg=theme.ACCENT, fg=theme.BG, cursor="hand2",
            font=theme.font(9, bold=True), padx=theme.px(5),
        )
        self.close_btn.pack(side="right")
        self.close_btn.bind("<Button-1>", lambda _e: self._on_close())

        body = tk.Frame(outer, bg=theme.PANEL)
        body.pack(fill="both", expand=True)
        self.text = tk.Text(
            body, height=10, width=46, wrap="word", font=theme.font(9),
            bg=theme.PANEL, fg=theme.TEXT, relief="flat", bd=0, state="disabled",
            highlightthickness=0,
        )
        vsb = widgets.thin_scrollbar(body, command=self.text.yview)
        self.text.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True, padx=theme.px(6),
                       pady=(theme.px(4), 0))

        self.meta = tk.Label(
            outer, text="", bg=theme.PANEL, fg=theme.TEXT_FAINT, font=theme.font(8),
            anchor="w", justify="left", wraplength=theme.px(360),
        )
        self.meta.pack(fill="x", padx=theme.px(7), pady=(theme.px(2), theme.px(2)))

        btns = tk.Frame(outer, bg=theme.PANEL)
        btns.pack(fill="x")
        # 只有这三个动作：重试 / 设置 / 关闭。**没有**「记录」按钮。
        self.btn_retry = self._button(btns, "重新解释", self._on_retry, primary=True)
        self.btn_settings = self._button(btns, "打开设置", self._on_settings)
        self.btn_close = self._button(btns, "关闭", self._on_close)

    def _button(self, parent, text, command, primary: bool = False) -> tk.Label:
        b = tk.Label(
            parent, text=text, cursor="hand2", font=theme.font(9),
            bg=theme.ACCENT if primary else theme.PANEL_ALT,
            fg=theme.BG if primary else theme.TEXT,
            padx=theme.px(8), pady=theme.px(4),
            highlightthickness=1,
            highlightbackground=theme.ACCENT if primary else theme.BORDER_STRONG,
        )
        b.pack(side="left", padx=(theme.px(4), theme.px(2)), pady=theme.px(5))
        # 注意：tk.Label 是普通控件，`state="disabled"` **不会**拦住
        # <Button-1> 绑定，所以每个动作回调自己必须有守卫（见 _on_retry）。
        b.bind("<Button-1>", lambda _e: command())
        return b

    # ------------------------------------------------------------- 渲染
    def _render(self, text: str) -> None:
        self._set_text(text)

    def _set_text(self, text: str) -> None:
        body = text or ""
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS] + "\n…（内容较长，完整内容见词条详情）"
        try:
            self.text.configure(state="normal")
            self.text.delete("1.0", tk.END)
            self.text.insert("1.0", body)
            self.text.configure(state="disabled")
        except tk.TclError:  # pragma: no cover
            pass

    def _body_text(self) -> str:
        try:
            return self.text.get("1.0", tk.END).strip()
        except tk.TclError:  # pragma: no cover
            return ""

    def _set_title(self, status: str) -> None:
        tag = STATUS_TEXT.get(status, "已记录")
        term = ""
        if self.selection is not None:
            term = (getattr(self.selection, "term", "") or "").strip()
            if len(term) > 18:
                term = term[:18] + "…"
        try:
            self.title_label.configure(text=f"{term}　{tag}" if term else tag)
        except tk.TclError:  # pragma: no cover
            pass

    def _set_meta(self, selection, status: str) -> None:
        bits = []
        if self.entry_id:
            bits.append(f"词条 #{int(self.entry_id)}")
        if selection is not None:
            src = getattr(selection, "source", None)
            where = (getattr(src, "title", "") or getattr(src, "app", "") or "").strip()
            if where:
                bits.append(where[:60])
        if status == "pending":
            bits.append("正在解释，结果出来会写回这条词条")
        elif status == "pending_no_key":
            bits.append("已入库但未联网；点「打开设置」填 Key 后点「重新解释」即可")
        elif status == "ok":
            bits.append("已写回词条，可在主界面查看")
        elif status == "error":
            bits.append("解释失败，可点「重新解释」重试（不会重复记词）")
        try:
            self.meta.configure(text="　".join(bits))
        except tk.TclError:  # pragma: no cover
            pass

    @staticmethod
    def _default_anchor() -> tuple[int, int]:
        """没有锚点时的兜底位置：屏幕左上角附近（会被 clamp 到工作区内）。"""
        return (40, 120)

    # ------------------------------------------------------------- 显示
    def show_recorded(self, selection, entry_id: int, anchor=None, *,
                      request_token: int | None = None,
                      token: int | None = None,
                      status: str = "pending", message: str = "",
                      explicit: bool = False) -> bool:
        """显示「已记录 + 正在解释 / 待解释」状态（这是本窗口唯一的打开入口）。

        ``request_token``：本次解释请求的**服务 token**（``None`` = 还没有请求，
        例如未配置 Key 的「待解释」状态）。它只用于**结果归属**判定，
        **绝不与选区序号比较**。``token`` 是它的旧名（保留兼容）。

        ``explicit=True``：用户显式动作（见 :meth:`restore`），软受限前台下
        也允许显示（硬阻断仍然拒绝 —— 由 ``show_at`` 的统一门控决定）。
        """
        if request_token is None:
            request_token = token
        self.selection = selection
        self.entry_id = int(entry_id)
        self.request_token = None if request_token is None else int(request_token)
        self.result = None
        self.status = status
        self.closed_by_user = False
        if not message:
            message = (f"已记录到词典（词条 #{int(entry_id)}）。\n\n"
                       "正在解释…（网络请求在后台线程执行，不影响你继续操作）")
        self.message = message
        self._set_text(message)
        self._set_title(status)
        self._set_meta(selection, status)
        anchor = anchor or self._default_anchor()
        return self.show_at(anchor, self.request_token, explicit=explicit)

    def show_result(self, entry_id: int, status: str, result, error_text: str = "",
                    *, request_token: int | None = None) -> bool:
        """写回**属于本窗口本次请求**的结果。

        两道归属闸门（必须**同时**通过）：

        1. ``entry_id`` 必须与窗口归属一致 —— 迟到的、别的词条的结果一律拒绝；
        2. 带 ``request_token`` 时，必须与本窗口当前跟随的请求 token 完全一致
           —— 用户在旧结果还在排队时点了「重新解释」（生成了**新的请求 token**），
           旧结果就**不能**再覆盖新请求的窗口状态。``request_token=None`` 表示
           调用方没有 token 信息（旧的 4 元组回调），此时退回「只看 entry」的历史语义。

        这里的 token 是**解释服务的请求 token**，不是选区序号：两者永不互相比较。

        另外：**绝不重开已隐藏的窗口** —— 窗口不可见时只更新内存状态，
        不 ``deiconify``，也不改 ``closed_by_user``；用户关掉之后不会再被结果弹回来。
        所有显示路径都过 ``FloatingWindow.show_at`` 的实时门控：游戏/全屏/暂停
        期间只隐藏、不映射（结果照样已经写进数据库）。
        """
        if self.entry_id is None or int(self.entry_id) != int(entry_id):
            return False
        if request_token is not None:
            if self.request_token is None or int(request_token) != int(self.request_token):
                return False
        self.status = status
        if status == "ok" and result is not None:
            self.result = result
            self._set_text(f"{result.as_text()}\n\n[模型配置] {result.model_config}"
                           f"{'   （命中本地缓存，未联网）' if result.from_cache else ''}")
        elif status == "stale":
            self.result = None
            self._set_text("这次解释已被判为过期（词条在解释期间被修改或删除），"
                           "结果未写回。可以点「重新解释」再试一次。")
        else:
            self.result = None
            suffix = "\n\n请在「设置」里检查 Base URL / 模型名 / API Key，然后点「重新解释」。"
            self._set_text(f"已记录，但解释失败：{error_text or '未知错误'}{suffix}")
        self._set_title(status)
        self._set_meta(self.selection, status)
        if not self.visible or self.closed_by_user:
            return False
        return bool(self.show_at(self._anchor or self._default_anchor(), self.request_token))

    #: 值得「显式找回」的状态：用户还需要点一次「重新解释」的那些
    RESTORABLE_STATUS = ("pending_no_key", "pending", "error")

    def restore(self, anchor=None, *, explicit: bool = True) -> bool:
        """**显式恢复入口**：把同一个 entry 的待解释窗口重新显示出来。

        场景：没有 Key 时窗口显示「已记录（待解释）」→ 用户去设置里填好 Key
        （或窗口因前台切换被收起）→ 从设置保存后调用本方法把**同一条词条**
        的窗口找回来，接着点「重新解释」。

        **只显示，不联网、不写库**：真正的解释永远只由用户点按钮触发。
        用户已经「关闭」过的窗口（``closed_by_user``）不会被找回。
        """
        if self.entry_id is None or self.closed_by_user:
            return False
        if self.status not in self.RESTORABLE_STATUS or not self.message:
            return False
        if self.visible:
            return True
        return self.show_recorded(self.selection, int(self.entry_id),
                                  anchor or self._default_anchor(),
                                  request_token=self.request_token, status=self.status,
                                  message=self.message, explicit=explicit)

    # ------------------------------------------------------------- 动作
    def _on_close(self) -> None:
        self.close_by_user()

    def _on_retry(self) -> None:
        """重新解释**本窗口这一条已落库的词条**（绝不重新记词、绝不再加词频）。

        显式守卫：Label 的 disabled 拦不住 ``<Button-1>``，因此这里自己确认
        「窗口确实可见、且上面确实有一条已落库的词条」；否则什么都不做
        （门控收起 / 用户关闭后迟到的点击不得再次触发解释）。
        """
        entry_id = self.entry_id
        if not entry_id or not self.visible or self.closed_by_user:
            return
        self.app.retry_explain_entry(int(entry_id), anchor=self._anchor)

    def _on_settings(self) -> None:
        self.app.open_settings()

    def hide(self) -> bool:  # noqa: D102
        return super().hide()

    def clear(self) -> None:
        """收起并解除归属：新选区 / 词条被编辑时调用，旧结果不得再落到这里。"""
        self.hide()
        self.selection = None
        self.result = None
        self.entry_id = None
        self.request_token = None
        self.message = ""
        self.status = ""
