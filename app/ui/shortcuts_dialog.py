"""操作说明窗口（批次 G0：「请你做好操作管理」）。

为什么要单独一个窗口
--------------------
画布上的手势已经不是三行提示装得下的了：点选 / 双击 / 拖动卡片 / ``Alt`` + 拖动 /
右键平移 / 滚轮缩放，还要说清「哪两个手势长得很像、怎么区分」。三行提示只够放
**最高频**的三条，剩下的必须有一个地方能查 —— 就是这里。

为什么把表格**从外面传进来**
----------------------------
手势表的唯一来源是 :data:`app.ui.concept_map.INTERACTIONS`（定义在画布旁边，
改了绑定就不容易忘记改表）。这个模块只负责**排版**，不 import 画布模块（否则
``concept_map`` ⇄ ``shortcuts_dialog`` 互相 import，冻结运行时容易踩循环导入），
界面与测试都只依赖 ``rows`` 这一个入参。

三条硬约束（与全程序一致）
------------------------
* **纯 tk**：冻结运行时没有 ``tkinter.ttk``（也没有 ``scrolledtext``）；
* **零网络、零写库**：纯粹给用户看的一张表；
* **逐字来自传进来的表**：这里不新增、不改写任何一条手势说明。
"""
from __future__ import annotations

import tkinter as tk

from . import theme, widgets

#: 键位列的宽度（字符）：够放下「Alt + 左键：从一张词卡拖到另一张」这种长键位
KEY_COLUMN_WIDTH = 34
#: 说明列的折行宽度（设备像素）
DESC_WRAP = 430


class ShortcutsDialog:
    """「操作说明」：键位 → 做什么，一行一条。"""

    def __init__(self, master, rows, *, title: str = "操作说明",
                 note: str = "", on_close=None):
        #: 传进来的手势表（``((键位, 说明), ...)``），原样保存一份便于测试核对
        self.rows = tuple((str(key), str(desc)) for key, desc in rows)
        self.key_labels: list = []
        self.desc_labels: list = []
        self.note_label = None

        self.win = tk.Toplevel(master)
        self.win.title(f"{title} — 探索词典")
        self.win.configure(bg=theme.BG)
        try:
            self.win.transient(master)
        except tk.TclError:  # pragma: no cover - 极简替身
            pass
        self.chrome = widgets.BorderlessChrome(
            self.win, title=title, on_close=on_close or self.close, resizable=True,
            min_w=520, min_h=380, bg=theme.PANEL)

        wrap = tk.Frame(self.win, bg=theme.BG)
        wrap.pack(fill="both", expand=True, padx=theme.px(16), pady=theme.px(12))
        head = tk.Label(wrap, text="鼠标怎么用", bg=theme.BG, fg=theme.TEXT,
                        font=theme.font(11), anchor="w")
        head.pack(fill="x")
        tip = tk.Label(wrap, text="画布上的操作就这么几条，改起来都在这一张表里。",
                       bg=theme.BG, fg=theme.TEXT_FAINT, font=theme.font(8),
                       anchor="w", justify="left")
        tip.pack(fill="x", pady=(theme.px(4), theme.px(8)))

        grid = tk.Frame(wrap, bg=theme.BG)
        grid.pack(fill="both", expand=True)
        for index, (key, desc) in enumerate(self.rows):
            key_label = tk.Label(grid, text=key, bg=theme.BG, fg=theme.TEXT,
                                 font=theme.font(9), anchor="nw", justify="left",
                                 width=KEY_COLUMN_WIDTH)
            key_label.grid(row=index, column=0, sticky="nw",
                           pady=(0, theme.px(6)))
            desc_label = tk.Label(grid, text=desc, bg=theme.BG, fg=theme.TEXT_MUTED,
                                  font=theme.font(9), anchor="w", justify="left",
                                  wraplength=theme.px(DESC_WRAP))
            desc_label.grid(row=index, column=1, sticky="w",
                            pady=(0, theme.px(6)))
            self.key_labels.append(key_label)
            self.desc_labels.append(desc_label)

        if note:
            self.note_label = tk.Label(wrap, text=note, bg=theme.CARD_BG,
                                       fg=theme.TEXT_MUTED, font=theme.font(8),
                                       anchor="w", justify="left",
                                       wraplength=theme.px(DESC_WRAP + 120),
                                       highlightthickness=1,
                                       highlightbackground=theme.BORDER)
            self.note_label.pack(fill="x", pady=(theme.px(8), 0),
                                 ipady=theme.px(6), ipadx=theme.px(8))

        footer = tk.Frame(wrap, bg=theme.BG)
        footer.pack(fill="x", pady=(theme.px(10), 0))
        self.btn_close = widgets.FlatButton(footer, "知道了", self.close,
                                            font_size=8, padx=12, pady=5,
                                            primary=True)
        self.btn_close.pack(side="right")

    def texts(self) -> tuple[str, ...]:
        """窗口里出现的所有键位与说明（测试与文档核对用）。"""
        out: list[str] = []
        for key, desc in self.rows:
            out.append(key)
            out.append(desc)
        return tuple(out)

    def reference(self) -> dict:
        """``{键位: 说明}``（一条键位写两遍时后写的赢，与界面显示顺序一致）。"""
        return {key: desc for key, desc in self.rows}

    def close(self) -> None:
        try:
            self.win.destroy()
        except tk.TclError:  # pragma: no cover - 已经关掉了
            pass
