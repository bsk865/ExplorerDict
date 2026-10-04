"""手动录入对话框（UIA 取不到选区时的明确兜底）。"""
from __future__ import annotations

import tkinter as tk

from . import theme, widgets


class ManualEntryDialog:
    """用户主动打开的输入窗口 —— 这里允许抢焦点，因为交互是用户发起的。"""

    def __init__(self, master: tk.Misc, app, *, preset_term: str = "", preset_context: str = ""):
        self.app = app
        self.win = tk.Toplevel(master)
        self.win.title("手动录入 — 探索词典")
        self.win.configure(bg=theme.BG)
        self.win.geometry(f"{theme.px(520)}x{theme.px(400)}")
        self.win.transient(master)
        self.saved_entry_id: int | None = None
        #: 自绘无框 chrome（与主界面 / 导图 / 设置共用）：Esc / Alt+F4 / × 只关本窗
        self.chrome = widgets.BorderlessChrome(self.win, title="手动录入",
                                               on_close=self.win.destroy,
                                               resizable=True, min_w=420, min_h=320,
                                               bg=theme.PANEL)

        self.feedback = tk.Label(self.win, text="", bg=theme.BG, fg=theme.TEXT_MUTED,
                                 font=theme.font(8), anchor="w", justify="left",
                                 wraplength=theme.px(480))

        tk.Label(self.win, text="术语 / 词语", bg=theme.BG, fg=theme.TEXT_FAINT,
                 font=theme.font(8)).pack(anchor="w", padx=theme.px(12),
                                          pady=(theme.px(10), theme.px(2)))
        self.var_term = tk.StringVar(value=preset_term)
        entry = tk.Entry(self.win, textvariable=self.var_term, font=theme.font(12),
                         relief="flat", bd=0, highlightthickness=1,
                         highlightbackground=theme.BORDER, highlightcolor=theme.TEXT,
                         bg=theme.PANEL, fg=theme.TEXT, insertbackground=theme.TEXT)
        entry.pack(fill="x", padx=theme.px(12), ipady=theme.px(3))
        entry.focus_set()

        tk.Label(self.win, text="上下文（可选，用于解释消歧）", bg=theme.BG, fg=theme.TEXT_FAINT,
                 font=theme.font(8)).pack(anchor="w", padx=theme.px(12),
                                          pady=(theme.px(8), theme.px(2)))
        self.txt_context = tk.Text(self.win, height=6, font=theme.font(9), wrap="word",
                                   relief="flat", bd=0, highlightthickness=1,
                                   highlightbackground=theme.BORDER, highlightcolor=theme.TEXT,
                                   bg=theme.PANEL, fg=theme.TEXT_BODY)
        self.txt_context.pack(fill="both", expand=True, padx=theme.px(12))
        if preset_context:
            self.txt_context.insert("1.0", preset_context)

        self.var_use_current = tk.BooleanVar(value=False)
        tk.Checkbutton(
            self.win, text="以当前前台窗口作为来源（不勾选则沿用最近一次阅读来源）",
            variable=self.var_use_current, bg=theme.BG, fg=theme.TEXT, font=theme.font(8),
            activebackground=theme.BG, activeforeground=theme.TEXT,
            selectcolor=theme.BG, highlightthickness=0, bd=0, anchor="w",
        ).pack(fill="x", padx=theme.px(12), pady=(theme.px(6), 0))

        self.src_label = tk.Label(self.win, text="", bg=theme.BG, fg=theme.TEXT_FAINT,
                                  font=theme.font(8), justify="left", anchor="w",
                                  wraplength=theme.px(490))
        self.src_label.pack(fill="x", padx=theme.px(12), pady=(theme.px(2), theme.px(6)))
        self._refresh_source()
        self.var_use_current.trace_add("write", lambda *_: self._refresh_source())

        btns = tk.Frame(self.win, bg=theme.BG)
        btns.pack(fill="x", padx=theme.px(12), pady=(theme.px(6), theme.px(10)))
        self.feedback.pack(fill="x", padx=theme.px(12), pady=(theme.px(6), 0),
                           before=btns)
        widgets.FlatButton(btns, "保存并解释", lambda: self.save(True), primary=True,
                           font_size=9, padx=14, pady=5).pack(side="right", padx=theme.px(4))
        widgets.FlatButton(btns, "仅保存", lambda: self.save(False), font_size=9,
                           padx=12, pady=5).pack(side="right", padx=theme.px(4))
        widgets.FlatButton(btns, "取消", self.win.destroy, font_size=9, padx=12,
                           pady=5).pack(side="left")

        self.win.bind("<Escape>", lambda e: self.win.destroy())
        self.win.bind("<Control-Return>", lambda e: self.save(False))

    def _refresh_source(self) -> None:
        if self.var_use_current.get():
            src = self.app.capture_service.effective_source()
            text = f"将记录来源：{src.title or src.app or '(未知)'}"
        else:
            src = self.app.capture_service.last_source()
            text = ("将记录来源：" + (src.title or src.app or "(未知来源)")) if src else \
                "将记录来源：(未知来源)"
        self.src_label.configure(text=text)

    def _set_feedback(self, text: str) -> bool:
        """窗口内**内联**校验反馈（不弹系统 messagebox）。"""
        try:
            if str(self.feedback.cget("text")) == str(text):
                return False
            self.feedback.configure(text=str(text))
            return True
        except tk.TclError:  # pragma: no cover
            return False

    def save(self, then_explain: bool) -> None:
        term = self.var_term.get().strip()
        if not term:
            self._set_feedback("术语不能为空")
            return
        context = self.txt_context.get("1.0", tk.END).strip()
        src = None
        if not self.var_use_current.get():
            src = self.app.capture_service.last_source()
        try:
            entry_id, created = self.app.capture_service.manual_entry(
                term, context, source=src, refresh_source=self.var_use_current.get()
            )
        except ValueError as exc:
            self._set_feedback(str(exc))
            return
        self.saved_entry_id = entry_id
        self.app.after_manual_save(entry_id, created, then_explain)
        self.win.destroy()
