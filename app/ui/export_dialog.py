"""「导出」格式选择器（用户选格式 → 再写文件）。

用户口径（2026-10-05）
--------------------
「导出内容要支持 CSV / JSON / JSONL / Anki 导入文本 / PDF / HTML 单页 / 纯文本 TXT，
用户可以进行选择」，并且**主界面的导出**与**导图的导出**要分开看 —— 导图那边维持
出 SVG + PNG，这里是词表那一类。

窗口里只有三件事
---------------
* **选格式**：单选框，每行一个格式，右边一句话说清「给谁用、别人拿到长什么样」。
  清单来自 :data:`app.export_service.FORMATS`（**单一来源** —— 加格式只改那一处）；
* **存到哪**：把实际目录写出来（来源是「设置 → 导出」里那个保存位置，留空就是默认
  的 ``<data>/exports``）—— 用户不用猜文件落哪儿；
* **导出 / 取消**：按「导出」才写文件、才关窗。

两条硬约束（与全程序一致）
-------------------------
* **纯 tk**：冻结运行时没有 ``tkinter.ttk``，也没有 ``tkinter.filedialog``
  —— 所以是这样一个自绘小窗，而不是系统下拉框 / 另存为；
* **不写数据**：这个窗只回一个格式 key，谁写文件由调用方决定（见
  :meth:`app.ui.main_window.MainWindow.export_entries`）。选过的格式会记进
  ``export.format``，下次打开还是那个（只记偏好，不算数据改动）。
"""
from __future__ import annotations

import tkinter as tk

from .. import export_service
from . import theme, widgets

#: 说明文字的折行宽度（设备像素）
NOTE_WRAP = 520
#: 窗口内容左右内缩（设备像素）
INSET = 16


def _note(parent, text: str, *, muted: bool = True, size: int = 8,
          wrap: int = NOTE_WRAP):
    return tk.Label(parent, text=text, bg=theme.BG,
                    fg=theme.TEXT_MUTED if muted else theme.TEXT,
                    font=theme.font(size), justify="left", anchor="w",
                    wraplength=theme.px(wrap))


def _short_path(path, keep: int = 3) -> str:
    """目录太长时只留最后几段（``…\\data\\exports``），避免把窗口撑宽。"""
    text = str(path)
    parts = [p for p in text.replace("/", "\\").split("\\") if p]
    if len(parts) <= keep:
        return text
    return "…\\" + "\\".join(parts[-keep:])


class ExportDialog:
    """「导出词表」：选格式（默认上次那个）→ 按导出。"""

    def __init__(self, master, *, cfg=None, scope_label: str = "全部词语",
                 count: int = 0, directory=None, default_format: str | None = None,
                 on_confirm=None):
        #: 配置对象（``app.config.Config``；测试替身可以不给 —— 只是记不住偏好）
        self.cfg = cfg
        self.scope_label = str(scope_label or "全部词语")
        self.count = int(count or 0)
        #: 落盘目录（调用方算好传进来，这里只显示 + 转发）
        self.directory = directory
        #: 按「导出」时把选中的 key 交给调用方（写文件不在这里做）
        self.on_confirm = on_confirm
        #: 已经按过导出了（防连点写两份）
        self.confirmed = False

        start = str(default_format or "").strip().lower()
        if start not in export_service.FORMATS_BY_KEY:
            start = export_service.DEFAULT_FORMAT
        self.var_format = tk.StringVar(value=start)

        self.win = tk.Toplevel(master)
        self.win.title("导出词表 — 探索词典")
        self.win.configure(bg=theme.BG)
        try:
            self.win.transient(master)
        except tk.TclError:                                     # pragma: no cover
            pass
        self.chrome = widgets.BorderlessChrome(self.win, title="导出词表",
                                               on_close=self.close, resizable=False,
                                               min_w=520, min_h=320, bg=theme.PANEL)

        wrap = tk.Frame(self.win, bg=theme.BG)
        wrap.pack(fill="both", expand=True, padx=theme.px(INSET), pady=theme.px(12))

        tk.Label(wrap, text=f"导出范围：{self.scope_label} · {self.count} 条",
                 bg=theme.BG, fg=theme.TEXT, font=theme.font(10),
                 anchor="w").pack(fill="x")
        _note(wrap, "先选文件格式，再按「导出」—— 导出的是上面这个范围的全部词条"
                    "（和勾选无关）。", size=8).pack(anchor="w", pady=(theme.px(2), 0))

        # ------------------------------------------------------- 格式（单一来源）
        grid = tk.Frame(wrap, bg=theme.BG)
        grid.pack(fill="x", pady=(theme.px(10), 0))
        self.format_buttons: list = []
        self.radio_buttons: dict[str, tk.Radiobutton] = {}
        for index, fmt in enumerate(export_service.FORMATS):
            radio = tk.Radiobutton(
                grid, text=fmt.label, variable=self.var_format, value=fmt.key,
                command=self._on_pick, bg=theme.BG, fg=theme.TEXT,
                font=theme.font(9), activebackground=theme.BG,
                activeforeground=theme.TEXT, selectcolor=theme.PANEL,
                highlightthickness=0, bd=0, anchor="w", width=14,
            )
            radio.grid(row=index * 2, column=0, sticky="nw", padx=(theme.px(2), 0))
            grid.columnconfigure(1, weight=1)
            _note(grid, f"{fmt.note}（{fmt.target}）" if fmt.target else fmt.note,
                  size=8, wrap=NOTE_WRAP).grid(row=index * 2, column=1, sticky="w")
            self.radio_buttons[fmt.key] = radio
            self.format_buttons.append(radio)

        # ------------------------------------------------------- 存到哪
        tk.Frame(wrap, bg=theme.BORDER, height=1).pack(fill="x", pady=theme.px(12))
        tk.Label(wrap, text="保存到：" + _short_path(self.directory),
                 bg=theme.BG, fg=theme.TEXT, font=theme.font(9),
                 anchor="w", justify="left",
                 wraplength=theme.px(NOTE_WRAP)).pack(fill="x")
        _note(wrap, "想换个地方存，就去主界面「设置 → 导出」里改保存位置"
                    "（不填就存到默认目录）。").pack(anchor="w", pady=(theme.px(2), 0))

        # ------------------------------------------------------- 按钮
        btns = tk.Frame(wrap, bg=theme.BG)
        btns.pack(fill="x", pady=(theme.px(14), 0))
        self.btn_export = widgets.FlatButton(btns, "导出", self.confirm, primary=True,
                                             font_size=9, padx=16, pady=5)
        self.btn_export.pack(side="right", padx=theme.px(4))
        self.btn_cancel = widgets.FlatButton(btns, "取消", self.close, font_size=9,
                                             padx=14, pady=5)
        self.btn_cancel.pack(side="right")
        for sequence in ("<Return>", "<KP_Enter>"):
            try:
                self.win.bind(sequence, lambda _e: self.confirm())
            except tk.TclError:                                 # pragma: no cover
                pass

    # ------------------------------------------------------------------ 对外
    def selected_format(self) -> str:
        """当前选中的格式 key（认不出来就退回默认）。"""
        return export_service.format_for(self.var_format.get()).key

    def selected(self) -> export_service.ExportFormat:
        return export_service.format_for(self.var_format.get())

    def confirm(self) -> None:
        """按「导出」：把选中的 key 交给调用方，然后关窗。"""
        if self.confirmed:
            return
        self.confirmed = True
        key = self.selected_format()
        if self.cfg is not None:
            # 只记偏好（下次打开还是这个格式）；写不进去也不该拦住导出
            try:
                self.cfg.set("export.format", key)
            except Exception:                                   # pragma: no cover
                pass
        self.close()
        if callable(self.on_confirm):
            self.on_confirm(key)

    def close(self) -> None:
        try:
            self.win.destroy()
        except tk.TclError:                                     # pragma: no cover
            pass

    # ------------------------------------------------------------------ 内部
    def _on_pick(self) -> None:
        """点单选框：只改选中值（导出要再按一下「导出」）。"""
        if self.cfg is not None:
            try:
                self.cfg.set("export.format", self.selected_format())
            except Exception:                                   # pragma: no cover
                pass


def ask_format(master, *, cfg=None, scope_label: str = "全部词语", count: int = 0,
               directory=None, default_format: str | None = None, on_confirm=None
               ) -> ExportDialog:
    """打开格式选择器（**非阻塞**：按「导出」时走 ``on_confirm(key)``）。"""
    return ExportDialog(master, cfg=cfg, scope_label=scope_label, count=count,
                        directory=directory, default_format=default_format,
                        on_confirm=on_confirm)
