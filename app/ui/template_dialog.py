"""「选一个布局骨架」对话框（批次 G：模板）。

用户口径
--------
「我在想是否可以加入模板？LLM 可以挑选合适的模板进行生成，用户也可以自己选择模板
进行自定义画导图。」

这个窗口就是「用户也可以自己选择」那一半：列出全部骨架，每条写清**怎么摆**与
**适合什么图**，选中即预览说明，按「应用」才真正切。模型建议（G3）只是同一张列表
上面的一行提示 —— 挑不挑、挑哪个，最后都按用户手里这一下算。

三条硬约束（与全程序一致）
-------------------------
* **纯 tk**：冻结运行时没有 ``tkinter.ttk``；
* **只改骨架**：这里没有一处能改关系 / 改筛选 / 删词 —— 切模板只让卡片换个摆法；
* **永远留一条退路**：第一项永远是「自动」，摆不成时也会退回它（见
  :func:`app.ui.map_templates.place`）。
"""
from __future__ import annotations

import tkinter as tk

from . import map_templates, theme, widgets

#: 模板列表的宽度（字符）与高度（行）
LIST_WIDTH = 24
LIST_HEIGHT = 9
#: 说明栏的折行宽度（设备像素）
DESC_WRAP = 380


def _label(parent, text: str, *, muted: bool = False, size: int = 9, wrap: int = DESC_WRAP):
    return tk.Label(parent, text=text, bg=theme.BG,
                    fg=theme.TEXT_MUTED if muted else theme.TEXT,
                    font=theme.font(size), justify="left", anchor="w",
                    wraplength=theme.px(wrap))


class TemplateDialog:
    """「模板…」：选布局骨架（纯展示 + 一次回调，不碰数据库）。"""

    def __init__(self, master, *, current: str = "", suggestion=None,
                 on_pick=None, on_ask_model=None):
        #: ``(key, 显示名)``，顺序就是下拉顺序（第一项永远是「自动」）
        self.rows: tuple[tuple[str, str], ...] = tuple(
            (spec.key, spec.name) for spec in map_templates.TEMPLATES)
        self.names = {key: name for key, name in self.rows}
        #: 当前用的骨架（用来在列表里标一个「当前」）
        self.current = str(current or map_templates.TEMPLATE_AUTO)
        #: 本地规则的建议：``(key, 理由)`` 或 ``None``
        self.suggestion = None
        if suggestion:
            key, reason = suggestion
            if map_templates.spec_of(key) is not None and key != self.current:
                self.suggestion = (str(key), str(reason or ""))
        self.on_pick = on_pick
        #: 给「让模型建议一个」用（``None`` = 不显示那个按钮，例如没配模型）
        self.on_ask_model = on_ask_model
        #: 程序化选中（测试与「用这个建议」都走它，不依赖 Listbox 的选中项）
        self._selected: str | None = None
        self.status_text = ""

        self.win = tk.Toplevel(master)
        self.win.title("模板 — 探索词典")
        self.win.configure(bg=theme.BG)
        try:
            self.win.transient(master)
        except tk.TclError:  # pragma: no cover - 极简替身
            pass
        self.chrome = widgets.BorderlessChrome(self.win, title="模板", on_close=self.close,
                                               resizable=True, min_w=620, min_h=400,
                                               bg=theme.PANEL)

        wrap = tk.Frame(self.win, bg=theme.BG)
        wrap.pack(fill="both", expand=True, padx=theme.px(16), pady=theme.px(12))
        _label(wrap, "换一个布局骨架：只改「卡片摆在哪」，关系、筛选、人工关系、"
                     "导出的图都跟着同一份骨架走。随时可以切回「自动」。",
               muted=True, size=8, wrap=DESC_WRAP + 180).pack(anchor="w",
                                                              pady=(0, theme.px(8)))

        columns = tk.Frame(wrap, bg=theme.BG)
        columns.pack(fill="both", expand=True)

        left = tk.Frame(columns, bg=theme.BG)
        left.pack(side="left", fill="y")
        self.list = tk.Listbox(
            left, height=LIST_HEIGHT, width=LIST_WIDTH, activestyle="none",
            exportselection=False, selectmode="browse", bg=theme.PANEL, fg=theme.TEXT,
            font=theme.font(9), highlightthickness=1, highlightbackground=theme.BORDER,
            borderwidth=0, selectbackground=theme.ACCENT, selectforeground=theme.BG)
        self.list.pack(fill="y")
        try:
            self.list.bind("<<ListboxSelect>>", self._on_select)
        except tk.TclError:  # pragma: no cover - 极简替身
            pass

        right = tk.Frame(columns, bg=theme.BG)
        right.pack(side="left", fill="both", expand=True, padx=(theme.px(12), 0))
        self.desc_name = tk.Label(right, text="", bg=theme.BG, fg=theme.TEXT,
                                  font=theme.font(11), anchor="w")
        self.desc_name.pack(fill="x")
        self.desc_summary = _label(right, "", size=9)
        self.desc_summary.pack(fill="x", pady=(theme.px(4), 0))
        self.desc_fit = _label(right, "", muted=True, size=8)
        self.desc_fit.pack(fill="x", pady=(theme.px(2), 0))

        self.suggest_label = _label(right, "", muted=True, size=8)
        self.suggest_label.pack(fill="x", pady=(theme.px(10), 0))
        self.btn_suggest = widgets.FlatButton(right, "用这个建议", self.use_suggestion,
                                              font_size=8, padx=10, pady=4)
        if self.suggestion:
            self.suggest_label.configure(
                text=f"建议：{self.names.get(self.suggestion[0], self.suggestion[0])}"
                     f"（{self.suggestion[1]}）")
            self.btn_suggest.pack(anchor="w", pady=(theme.px(4), 0))
        else:
            self.suggest_label.configure(text="这张图暂时没有明显更合适的骨架，"
                                              "用「自动」就行。")

        self.btn_ask_model = widgets.FlatButton(right, "让模型也看一眼", self.ask_model,
                                                font_size=8, padx=10, pady=4)
        if on_ask_model is not None:
            self.btn_ask_model.pack(anchor="w", pady=(theme.px(6), 0))

        self.status = tk.Label(wrap, text="", bg=theme.BG, fg=theme.TEXT_MUTED,
                               font=theme.font(8), anchor="w", justify="left",
                               wraplength=theme.px(DESC_WRAP + 180))
        self.status.pack(fill="x", pady=(theme.px(8), 0))

        buttons = tk.Frame(wrap, bg=theme.BG)
        buttons.pack(fill="x", pady=(theme.px(6), 0))
        self.btn_apply = widgets.FlatButton(buttons, "应用这个模板", self.apply_current,
                                            primary=True, font_size=8, padx=12, pady=5)
        self.btn_apply.pack(side="left")
        self.btn_cancel = widgets.FlatButton(buttons, "取消", self.close, font_size=8,
                                             padx=10, pady=4)
        self.btn_cancel.pack(side="right")

        self.refresh()
        self.select(self.current)

    # ------------------------------------------------------------------ 列表
    def refresh(self) -> None:
        """重填列表（``当前`` 标在正在用的那一项上）。"""
        try:
            self.list.delete(0, tk.END)
        except tk.TclError:  # pragma: no cover - 极简替身
            pass
        for key, name in self.rows:
            suffix = "　·　当前" if key == self.current else ""
            self.list.insert(tk.END, f"{name}{suffix}")

    def select(self, key: str) -> bool:
        """选中某一项（程序化）；未知 key → ``False``。"""
        wanted = str(key or "")
        for index, (row_key, _name) in enumerate(self.rows):
            if row_key != wanted:
                continue
            self._selected = row_key
            try:
                self.list.selection_clear(0, tk.END)
                self.list.selection_set(index)
                self.list.activate(index)
                self.list.see(index)
            except (AttributeError, tk.TclError):  # pragma: no cover - 极简替身
                pass
            self._describe(row_key)
            return True
        return False

    def picked_key(self) -> str | None:
        """当前选中的模板 key（没选 → ``None``）。"""
        try:
            picked = self.list.curselection()
        except (AttributeError, tk.TclError):  # pragma: no cover - 极简替身
            picked = ()
        if picked:
            index = int(picked[0])
            if 0 <= index < len(self.rows):
                return self.rows[index][0]
        return self._selected

    def _on_select(self, _event=None) -> None:
        key = self.picked_key()
        if key:
            self._describe(key)

    def _describe(self, key: str) -> None:
        spec = map_templates.spec_of(key)
        if spec is None:
            return
        self.desc_name.configure(text=spec.name)
        self.desc_summary.configure(text=spec.summary)
        self.desc_fit.configure(text=f"适合：{spec.fit}")

    # ------------------------------------------------------------------ 动作
    def _set_status(self, text: str, *, error: bool = False) -> None:
        self.status_text = str(text)
        try:
            self.status.configure(text=self.status_text,
                                  fg=theme.ERR if error else theme.TEXT_MUTED)
        except tk.TclError:  # pragma: no cover - 极简替身
            pass

    def use_suggestion(self) -> bool:
        """把本地规则建议的那一项选中（不直接应用 —— 用户还得按一下）。"""
        if not self.suggestion:
            self._set_status("这张图没有更合适的骨架，用「自动」就行。")
            return False
        key, reason = self.suggestion
        if not self.select(key):
            return False
        self._set_status(f"已经替你选好「{self.names.get(key, key)}」（{reason}），"
                         "确认就按「应用这个模板」。")
        return True

    def ask_model(self) -> None:
        """把「让模型看一眼」交给调用方（这里只转发 + 说一句在等）。"""
        if self.on_ask_model is None:
            return
        self._set_status("已经去问模型了，结果出来会显示在这一行上面……")
        try:
            self.on_ask_model(self)
        except Exception as exc:  # pragma: no cover - 调用方自己兜底
            self._set_status(f"问模型失败：{exc}", error=True)

    def apply_model_suggestion(self, key: str, reason: str = "") -> bool:
        """模型给出建议后由调用方回调进来：选中它并写一行说明。"""
        if not key or map_templates.spec_of(key) is None:
            self.model_unavailable("模型没有给出可用的排法")
            return False
        if not self.select(key):
            return False
        self._set_status(f"模型建议「{self.names.get(key, key)}」"
                         f"{f'（{reason}）' if reason else ''}：确认就按「应用这个模板」。")
        return True

    def model_unavailable(self, text: str = "") -> None:
        """没问到模型（没配 Key / 超时 / 输出坏了 / 没勾开关）：说清一句 + 退回本地建议。

        **不让它变成错误弹窗**：挑不了骨架只是「少一条路」，本地规则与手动下拉
        都还在，用户按「用这个建议」或自己点一项就行。
        """
        why = str(text or "").strip() or "这次没问成"
        if self.suggestion:
            key, reason = self.suggestion
            self._set_status(f"没用上模型（{why}）；本地规则建议"
                             f"「{self.names.get(key, key)}」（{reason}），"
                             "按「用这个建议」就能选上它。")
        else:
            self._set_status(f"没用上模型（{why}）：这张图没有更合适的骨架，用「自动」就行。")

    def apply_current(self) -> bool:
        """应用选中的骨架（回调给调用方；成功 → 关窗）。"""
        key = self.picked_key()
        if not key:
            self._set_status("先在左边点一个模板，再按「应用这个模板」。")
            return False
        if key == self.current:
            self._set_status("现在用的就是这个模板。")
            return False
        if self.on_pick is not None:
            try:
                self.on_pick(key)
            except Exception as exc:  # pragma: no cover - 调用方自己兜底
                self._set_status(f"切换失败：{exc}", error=True)
                return False
        self.current = key
        self.close()
        return True

    def close(self) -> None:
        try:
            self.win.destroy()
        except tk.TclError:  # pragma: no cover - 已经关掉了
            pass
