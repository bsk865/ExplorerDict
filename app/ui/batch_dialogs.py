"""主题合并 / 词条拆分的小对话框（B2）。

为什么单独一个模块
------------------
主窗口（``app/ui/main_window.py``）已经很长，而这两个对话框有各自的校验与
回调协议；放在这里，主窗口只留一个方法调它们，回归测试也能单独构造。

两条硬约束（与全程序一致）
--------------------------
* **纯 tk**：冻结运行时没有 ``tkinter.ttk``，所以勾选用 ``tk.Checkbutton``、
  单选目标用 ``tk.Radiobutton``，没有下拉框；
* **只改「浏览」与数据库**：合并 = 把源主题的词搬进目标主题 + 删源主题；
  拆分 = 把选中的词搬进新主题 / 另一个主题。两者都不碰「新词存在哪里」的规则。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox

from . import theme, widgets


def _label(parent, text: str, *, muted: bool = False, size: int = 9, wrap: int = 380):
    return tk.Label(parent, text=text, bg=theme.BG,
                    fg=theme.TEXT_MUTED if muted else theme.TEXT,
                    font=theme.font(size), justify="left", anchor="w",
                    wraplength=theme.px(wrap))


class MergeBatchesDialog:
    """把若干主题**合并进**一个目标主题。

    * 目标主题：单选按钮（默认 = 左栏当前选中的那个）；
    * 来源主题：复选框（勾谁就把谁并进目标，随后源主题消失）；
    * 「合并」→ 二次确认 → ``on_merge(target_id, source_ids)``。
    """

    def __init__(self, master, app, batches, target_id, *, on_merge=None):
        self.app = app
        self.db = app.db
        self.batches = list(batches or [])
        self.on_merge = on_merge
        self.result = 0

        self.win = tk.Toplevel(master)
        self.win.title("合并主题 — 探索词典")
        self.win.configure(bg=theme.BG)
        self.win.transient(master)
        self.win.resizable(False, False)
        self.chrome = widgets.BorderlessChrome(self.win, title="合并主题",
                                               on_close=self.win.destroy, bg=theme.PANEL)
        wrap = tk.Frame(self.win, bg=theme.BG)
        wrap.pack(fill="both", expand=True, padx=theme.px(16), pady=theme.px(12))
        _label(wrap, "把勾选的主题合并进目标主题：词语会全部搬过去，"
                     "源主题随后消失（词条、上下文、来源、标签都不会丢）。",
               muted=True, size=8).pack(anchor="w", pady=(0, theme.px(8)))

        #: 目标用 StringVar 存 id 的字符串（假 Tk 环境里没有 IntVar）
        default = str(int(target_id)) if target_id else ""
        if not any(str(r["id"]) == default for r in self.batches) and self.batches:
            default = str(self.batches[0]["id"])
        self.var_target = tk.StringVar(value=default)

        _label(wrap, "目标主题", muted=True, size=7).pack(anchor="w")
        self.target_buttons: list = []
        for row in self.batches:
            button = tk.Radiobutton(
                wrap, text=self._title(row), variable=self.var_target,
                value=str(int(row["id"])), command=self._refresh,
                bg=theme.BG, fg=theme.TEXT, font=theme.font(9),
                activebackground=theme.BG, activeforeground=theme.TEXT,
                selectcolor=theme.PANEL, highlightthickness=0, bd=0, anchor="w",
            )
            button.pack(anchor="w", padx=theme.px(6))
            self.target_buttons.append(button)

        _label(wrap, "要合并的主题", muted=True, size=7).pack(anchor="w",
                                                              pady=(theme.px(8), 0))
        self.var_sources: dict[int, tk.BooleanVar] = {}
        self.source_checks: dict[int, tk.Checkbutton] = {}
        for row in self.batches:
            bid = int(row["id"])
            var = tk.BooleanVar(value=False)
            check = tk.Checkbutton(
                wrap, text=self._title(row), variable=var, command=self._refresh,
                bg=theme.BG, fg=theme.TEXT, font=theme.font(9),
                activebackground=theme.BG, activeforeground=theme.TEXT,
                selectcolor=theme.PANEL, highlightthickness=0, bd=0, anchor="w",
            )
            check.pack(anchor="w", padx=theme.px(6))
            self.var_sources[bid] = var
            self.source_checks[bid] = check

        self.hint = _label(wrap, "", muted=True, size=8)
        self.hint.pack(anchor="w", pady=(theme.px(8), 0))

        btns = tk.Frame(wrap, bg=theme.BG)
        btns.pack(fill="x", pady=(theme.px(10), 0))
        widgets.FlatButton(btns, "取消", self.win.destroy, font_size=8, padx=10,
                           pady=3).pack(side="right")
        self.btn_merge = widgets.FlatButton(btns, "合并", self.merge, primary=True,
                                            font_size=8, padx=12, pady=3)
        self.btn_merge.pack(side="right", padx=(0, theme.px(6)))
        self._refresh()

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _title(row) -> str:
        return f"{row['name']}  ({row['entry_count']})"

    def target_id(self) -> int:
        try:
            return int(self.var_target.get() or 0)
        except (TypeError, ValueError):  # pragma: no cover - 只可能是空串
            return 0

    def sources(self) -> list[int]:
        """勾选且**不等于目标**的主题 id（目标自己不能被当成来源）。"""
        target = self.target_id()
        out = []
        for bid, var in self.var_sources.items():
            try:
                checked = bool(var.get())
            except tk.TclError:  # pragma: no cover
                checked = False
            if checked and bid != target:
                out.append(bid)
        return out

    def _refresh(self) -> None:
        rows = {int(r["id"]): r for r in self.batches}
        target = rows.get(self.target_id())
        picked = self.sources()
        moved = sum(int(rows[i]["entry_count"] or 0) for i in picked if i in rows)
        parts = [f"合并进：《{target['name']}》" if target else "请选择目标主题"]
        if picked:
            parts.append(f"共 {len(picked)} 个主题、约 {moved} 条词语会被搬过来")
        else:
            parts.append("还没有勾选要合并的主题")
        text = "；".join(parts)
        try:
            self.hint.configure(text=text)
        except tk.TclError:  # pragma: no cover
            pass
        enable = getattr(self.btn_merge, "set_enabled", None)
        if callable(enable):
            enable(bool(picked))

    def merge(self) -> int:
        """执行合并（先确认）。返回搬动的词条数；条件不满足返回 0。"""
        target = self.target_id()
        picked = self.sources()
        if not target or not picked:
            messagebox.showinfo("合并主题", "请先选择目标主题，并勾选至少一个要合并的主题。",
                                parent=self.win)
            return 0
        rows = {int(r["id"]): str(r["name"]) for r in self.batches}
        names = "、".join(rows.get(i, str(i)) for i in picked)
        if not messagebox.askyesno(
                "合并主题",
                f"把《{names}》合并进《{rows.get(target, target)}》？\n"
                "词语会全部搬到目标主题，源主题随即删除（此操作不可撤销）。",
                parent=self.win):
            return 0
        moved = 0
        if callable(self.on_merge):
            moved = int(self.on_merge(target, picked) or 0)
        self.result = moved
        self.win.destroy()
        return moved


class MoveEntriesDialog:
    """把**勾选的词条**搬进一个新主题或另一个已有主题（主题拆分）。"""

    MODE_NEW = "new"
    MODE_EXISTING = "existing"

    def __init__(self, master, app, entry_ids, batches, *, on_move=None):
        self.app = app
        self.db = app.db
        self.entry_ids = [int(i) for i in (entry_ids or [])]
        self.batches = list(batches or [])
        self.on_move = on_move
        self.result = 0

        self.win = tk.Toplevel(master)
        self.win.title("拆分主题 — 探索词典")
        self.win.configure(bg=theme.BG)
        self.win.transient(master)
        self.win.resizable(False, False)
        self.chrome = widgets.BorderlessChrome(self.win, title="拆分 / 移动词条",
                                               on_close=self.win.destroy, bg=theme.PANEL)
        wrap = tk.Frame(self.win, bg=theme.BG)
        wrap.pack(fill="both", expand=True, padx=theme.px(16), pady=theme.px(12))
        _label(wrap, f"已勾选 {len(self.entry_ids)} 条词条，搬到：",
               muted=True, size=8).pack(anchor="w", pady=(0, theme.px(8)))

        self.var_mode = tk.StringVar(value=self.MODE_NEW)
        self.var_name = tk.StringVar(value="")
        self.var_target = tk.StringVar(value="")

        tk.Radiobutton(
            wrap, text="新建主题", variable=self.var_mode, value=self.MODE_NEW,
            command=self._refresh, bg=theme.BG, fg=theme.TEXT, font=theme.font(9),
            activebackground=theme.BG, activeforeground=theme.TEXT,
            selectcolor=theme.PANEL, highlightthickness=0, bd=0, anchor="w",
        ).pack(anchor="w", padx=theme.px(6))
        self.name_entry = tk.Entry(
            wrap, textvariable=self.var_name, font=theme.font(9), relief="flat", bd=0,
            highlightthickness=1, highlightbackground=theme.BORDER,
            highlightcolor=theme.TEXT, bg=theme.PANEL, fg=theme.TEXT,
            insertbackground=theme.TEXT,
        )
        self.name_entry.pack(fill="x", padx=theme.px(24), pady=(0, theme.px(6)),
                             ipady=theme.px(2))
        self.name_entry.bind("<KeyRelease>", lambda e: self._refresh())

        tk.Radiobutton(
            wrap, text="搬到已有主题", variable=self.var_mode, value=self.MODE_EXISTING,
            command=self._refresh, bg=theme.BG, fg=theme.TEXT, font=theme.font(9),
            activebackground=theme.BG, activeforeground=theme.TEXT,
            selectcolor=theme.PANEL, highlightthickness=0, bd=0, anchor="w",
        ).pack(anchor="w", padx=theme.px(6))
        self.target_buttons: list = []
        for row in self.batches:
            button = tk.Radiobutton(
                wrap, text=self._title(row), variable=self.var_target,
                value=str(int(row["id"])), command=self._refresh,
                bg=theme.BG, fg=theme.TEXT, font=theme.font(9),
                activebackground=theme.BG, activeforeground=theme.TEXT,
                selectcolor=theme.PANEL, highlightthickness=0, bd=0, anchor="w",
            )
            button.pack(anchor="w", padx=theme.px(24))
            self.target_buttons.append(button)
        if self.batches:
            self.var_target.set(str(int(self.batches[0]["id"])))

        self.hint = _label(wrap, "", muted=True, size=8)
        self.hint.pack(anchor="w", pady=(theme.px(8), 0))

        btns = tk.Frame(wrap, bg=theme.BG)
        btns.pack(fill="x", pady=(theme.px(10), 0))
        widgets.FlatButton(btns, "取消", self.win.destroy, font_size=8, padx=10,
                           pady=3).pack(side="right")
        self.btn_move = widgets.FlatButton(btns, "移动", self.move, primary=True,
                                           font_size=8, padx=12, pady=3)
        self.btn_move.pack(side="right", padx=(0, theme.px(6)))
        self._refresh()

    @staticmethod
    def _title(row) -> str:
        return f"{row['name']}  ({row['entry_count']})"

    def mode(self) -> str:
        try:
            value = str(self.var_mode.get() or "")
        except tk.TclError:  # pragma: no cover
            value = ""
        return self.MODE_EXISTING if value == self.MODE_EXISTING else self.MODE_NEW

    def target_id(self) -> int:
        try:
            return int(self.var_target.get() or 0)
        except (TypeError, ValueError):
            return 0

    def target_name(self) -> str:
        try:
            return str(self.var_name.get() or "").strip()
        except tk.TclError:  # pragma: no cover
            return ""

    def _refresh(self) -> None:
        if self.mode() == self.MODE_NEW:
            text = (f"新建主题《{self.target_name()}》"
                    if self.target_name() else "请输入新主题的名称")
        else:
            rows = {int(r["id"]): str(r["name"]) for r in self.batches}
            name = rows.get(self.target_id())
            text = f"搬到《{name}》" if name else "请选择一个已有主题"
        try:
            self.hint.configure(text=text)
        except tk.TclError:  # pragma: no cover
            pass

    def move(self) -> int:
        """执行搬家；返回搬动的条数（条件不满足返回 0）。"""
        if not self.entry_ids:
            messagebox.showinfo("拆分主题", "还没有勾选词条。", parent=self.win)
            return 0
        if self.mode() == self.MODE_NEW:
            name = self.target_name()
            if not name:
                messagebox.showinfo("拆分主题", "请输入新主题的名称。", parent=self.win)
                return 0
            payload = ("new", 0, name)
        else:
            target = self.target_id()
            if not target:
                messagebox.showinfo("拆分主题", "请选择一个已有主题。", parent=self.win)
                return 0
            payload = ("existing", target, "")
        moved = 0
        if callable(self.on_move):
            moved = int(self.on_move(self.entry_ids, *payload) or 0)
        self.result = moved
        self.win.destroy()
        return moved
