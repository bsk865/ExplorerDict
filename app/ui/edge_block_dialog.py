"""AI 参考关系对话框（批次 F3：「这条不对吗？」）。

用户要的能力
------------
* **看懂这条边**：起点 / 类型 / 终点 / 依据 / 证据一次摆出来（都是模型给的原话，
  这里**不加工**、也不重新请求模型）；
* **标为不对**：模型推错的那条边，以后不再画出来 —— 包括**重新生成之后**，
  只要两端还是这两个词，它就不会回来；
* **反悔**：下面列出这个主题里所有被标为「不对」的端点对，选一条按「恢复」，
  它就会重新参与绘制（下次重新生成时模型再提出同样的一对，也会照常画）。

三条硬约束（与全程序一致）
-------------------------
* **纯 tk**：冻结运行时没有 ``tkinter.ttk``，类型 / 列表一律 ``tk.Listbox``、``tk.Label``；
* **零 token**：只读写本地库，一个网络请求都不发；
* **不重复实现存储**：屏蔽走 :meth:`app.ui.concept_map.ConceptMapWindow.block_ai_edge`，
  恢复走 ``unblock_edge_row`` / ``blocked_edge_rows``（它们负责落库 + 重画）。
"""
from __future__ import annotations

import tkinter as tk

from . import theme, widgets

#: 列表宽度（字符）与高度（行）
LIST_WIDTH = 46
LIST_HEIGHT = 6

#: 对话框里对「没有材料时」的说明：模型常常给不出证据，这不是错误
NO_EVIDENCE = "（模型这次没有给证据片段）"
NO_REASON = "（模型这次没有给一句依据）"


def _label(parent, text: str, *, muted: bool = False, size: int = 9, wrap: int = 560):
    return tk.Label(parent, text=text, bg=theme.BG,
                    fg=theme.TEXT_MUTED if muted else theme.TEXT,
                    font=theme.font(size), justify="left", anchor="w",
                    wraplength=theme.px(wrap))


def _listbox(parent, height: int = LIST_HEIGHT, width: int = LIST_WIDTH) -> tk.Listbox:
    return tk.Listbox(
        parent, height=height, width=width, activestyle="none",
        exportselection=False, selectmode="browse", bg=theme.PANEL, fg=theme.TEXT,
        font=theme.font(9), highlightthickness=1, highlightbackground=theme.BORDER,
        borderwidth=0, selectbackground=theme.ACCENT, selectforeground=theme.BG,
    )


class EdgeBlockDialog:
    """「这条关系不对吗？」：看清楚它，然后决定留、还是标为不对。"""

    def __init__(self, master, owner, edge):
        self.owner = owner
        self.db = owner.db
        self.edge = edge
        self.rel = getattr(edge, "rel", edge)
        #: ``entry_id → 显示名``（本主题全部词条；不在本主题的两端显示成括号说明）
        self.names = {int(eid): str(name) for eid, name in owner.manual_relation_options()}
        #: 库里当前被标为「不对」的行（与下面那个列表一一对应）
        self.rows: list = []

        self.win = tk.Toplevel(master)
        self.win.title("这条关系不对吗 — 探索词典")
        self.win.configure(bg=theme.BG)
        try:
            self.win.transient(master)
        except tk.TclError:  # pragma: no cover - 极简替身
            pass
        self.chrome = widgets.BorderlessChrome(self.win, title="这条关系不对吗",
                                               on_close=self.close, resizable=True,
                                               min_w=560, min_h=400, bg=theme.PANEL)

        wrap = tk.Frame(self.win, bg=theme.BG)
        wrap.pack(fill="both", expand=True, padx=theme.px(16), pady=theme.px(12))
        _label(wrap, "这是模型提出的一条参考关系（不是事实）：如果它明显不对，"
                     "就标成「不对」，以后这张图上不会再出现它 —— 重新生成也不会回来。",
               muted=True, size=8).pack(anchor="w", pady=(0, theme.px(8)))

        self.headline = _label(wrap, self._headline_text(), size=10)
        self.headline.pack(anchor="w")
        _label(wrap, "模型的依据", muted=True, size=7).pack(anchor="w",
                                                          pady=(theme.px(8), theme.px(2)))
        self.reason = _label(wrap, self._block_text(self.rel, "reason", NO_REASON),
                             size=9, wrap=540)
        self.reason.pack(anchor="w")
        _label(wrap, "模型的证据片段", muted=True, size=7).pack(anchor="w",
                                                          pady=(theme.px(8), theme.px(2)))
        self.evidence = _label(wrap, self._block_text(self.rel, "evidence", NO_EVIDENCE),
                               muted=True, size=8, wrap=540)
        self.evidence.pack(anchor="w")

        _label(wrap, "这个主题里已经标为「不对」的关系（点一条再按「恢复这条关系」）",
               muted=True, size=7).pack(anchor="w", pady=(theme.px(12), theme.px(2)))
        self.rows_list = _listbox(wrap)
        self.rows_list.pack(fill="x")

        self.status = tk.Label(wrap, text="", bg=theme.BG, fg=theme.TEXT_MUTED,
                               font=theme.font(8), anchor="w", justify="left",
                               wraplength=theme.px(540))
        self.status.pack(fill="x", pady=(theme.px(4), 0))

        buttons = tk.Frame(wrap, bg=theme.BG)
        buttons.pack(fill="x", pady=(theme.px(6), 0))
        self.btn_block = widgets.FlatButton(buttons, "标为不对（以后不再出现）",
                                            self.block_current, primary=True,
                                            font_size=8, padx=12, pady=5)
        self.btn_block.pack(side="left")
        self.btn_restore = widgets.FlatButton(buttons, "恢复这条关系", self.restore_current,
                                              font_size=8, padx=10, pady=4)
        self.btn_restore.pack(side="left", padx=(theme.px(6), 0))
        self.btn_close = widgets.FlatButton(buttons, "关闭", self.close, font_size=8,
                                            padx=10, pady=4)
        self.btn_close.pack(side="right")

        self.refresh()
        self._set_status("觉得不对就按「标为不对」；标错了随时可以在下面恢复。")

    # ------------------------------------------------------------- 文案
    def _name(self, entry_id) -> str:
        try:
            return self.names.get(int(entry_id), "（不在本主题）")
        except (TypeError, ValueError):  # pragma: no cover - 坏数据
            return "（不在本主题）"

    def _headline_text(self) -> str:
        src = self._name(getattr(self.rel, "src_entry_id", None))
        dst = self._name(getattr(self.rel, "dst_entry_id", None))
        kind = str(getattr(self.rel, "rel_type", "") or getattr(self.rel, "label", "") or "相关")
        return f"{src}　--{kind}-->　{dst}"

    def _block_text(self, rel, field: str, fallback: str) -> str:
        text = str(getattr(rel, field, "") or "").strip()
        return text or fallback

    def _row_text(self, row) -> str:
        src = self._name(row["src_entry_id"])
        dst = self._name(row["dst_entry_id"])
        kind = str(row["label"] or "").strip() or "相关"
        when = str(row["created_at"] or "").strip()
        text = f"{src} --{kind}--> {dst}"
        return f"{text}（{when}）" if when else text

    # ------------------------------------------------------------- 数据
    def refresh(self) -> None:
        """重读「已标为不对」的列表（**只读本地库**）。"""
        self.rows = list(self.owner.blocked_edge_rows())
        try:
            self.rows_list.delete(0, tk.END)
            for row in self.rows:
                self.rows_list.insert(tk.END, self._row_text(row))
        except (AttributeError, tk.TclError):  # pragma: no cover - 极简替身
            return

    def _picked_row(self):
        try:
            picks = self.rows_list.curselection()
        except (AttributeError, tk.TclError):  # pragma: no cover
            return None
        if not picks:
            return None
        index = int(picks[0])
        if not 0 <= index < len(self.rows):
            return None
        return self.rows[index]

    # ------------------------------------------------------------- 动作
    def _set_status(self, text: str, *, error: bool = False) -> None:
        try:
            self.status.configure(text=str(text),
                                  fg=theme.ERR if error else theme.TEXT_MUTED)
        except (AttributeError, tk.TclError):  # pragma: no cover
            return

    def block_current(self) -> None:
        """把这条 AI 关系标成「不对」（写库 + 从图上撤掉）。"""
        ok, message = self.owner.block_ai_edge(self.edge)
        self._set_status(message, error=not ok)
        if ok:
            self.refresh()

    def restore_current(self) -> None:
        """恢复列表里选中的那一条（它会重新参与绘制）。"""
        row = self._picked_row()
        if row is None:
            self._set_status("先在下面点一条已经标为「不对」的关系，再按「恢复这条关系」。",
                             error=True)
            return
        ok, message = self.owner.unblock_edge_row(row["id"])
        self._set_status(message, error=not ok)
        if ok:
            self.refresh()

    def close(self) -> None:
        try:
            self.win.destroy()
        except (AttributeError, tk.TclError):  # pragma: no cover
            return
