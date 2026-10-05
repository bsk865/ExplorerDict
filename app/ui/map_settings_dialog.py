"""「导图设置」对话框（本轮：把关系图设置从主界面搬进导图界面）。

用户口径
--------
「思维导图的导出和相关设置都需要在导图界面中，主界面不应该显示导图的相关设置。」

窗口里只有导图自己用得着的三件事：

* **布局骨架**：现在用的是哪个 + 「换模板…」（接力到 G1 的模板对话框）；
* **模型参与**：模板对话框里要不要显示「让模型也看一眼」。默认关；勾上后也只有
  用户按那个按钮时才会发一次请求（只发关系类型与起止词名，见
  :meth:`app.ui.concept_map.ConceptMapWindow._ask_model_for_template`）；
* **恢复自动布局**：与工具条上那个按钮同一条路（``reset_node_pins`` 回调）。

三条硬约束（与全程序一致）
------------------------
* **纯 tk**：冻结运行时没有 ``tkinter.ttk``；
* **不碰数据**：这里没有一处能改关系 / 改筛选 / 删词 —— 它只写两个设置键；
* **立即生效**：没有「保存」按钮要记 —— 勾一下就是勾了（写进 ``config``），
  状态行**如实说明**写没写成（配置写不进去时不会假装成功）。
"""
from __future__ import annotations

import tkinter as tk

from . import theme, widgets

#: 说明文字的折行宽度（设备像素）
NOTE_WRAP = 460
#: 窗口内容左右内缩（设备像素）
INSET = 16
#: 模型开关的设置键（默认关）
ASK_MODEL_KEY = "map.template_ask_model"


def _note(parent, text: str, *, muted: bool = True, size: int = 8,
          wrap: int = NOTE_WRAP):
    return tk.Label(parent, text=text, bg=theme.BG,
                    fg=theme.TEXT_MUTED if muted else theme.TEXT,
                    font=theme.font(size), justify="left", anchor="w",
                    wraplength=theme.px(wrap))


class MapSettingsDialog:
    """「导图设置…」：看当前骨架 / 换模板 / 模型开关 / 恢复自动布局（都立即生效）。"""

    def __init__(self, master, *, cfg=None, template_key: str = "auto",
                 template_name: str = "", on_open_templates=None,
                 on_reset_layout=None):
        #: 配置对象（``app.config.Config``；测试替身可以不给）
        self.cfg = cfg
        self.template_key = str(template_key or "")
        self.template_name = str(template_name or self.template_key or "自动")
        #: 「换模板…」交给导图窗口自己去做（这里只转发 —— 不重复实现一遍切换逻辑）
        self.on_open_templates = on_open_templates
        #: 「恢复自动布局」同上
        self.on_reset_layout = on_reset_layout
        self.status_text = ""

        self.win = tk.Toplevel(master)
        self.win.title("导图设置 — 探索词典")
        self.win.configure(bg=theme.BG)
        try:
            self.win.transient(master)
        except tk.TclError:  # pragma: no cover - 极简替身
            pass
        self.chrome = widgets.BorderlessChrome(self.win, title="导图设置",
                                               on_close=self.close, resizable=False,
                                               min_w=540, min_h=300, bg=theme.PANEL)

        wrap = tk.Frame(self.win, bg=theme.BG)
        wrap.pack(fill="both", expand=True, padx=theme.px(INSET), pady=theme.px(12))
        _note(wrap, "导图自己的设置都在这里 —— 主界面「设置」里不再显示导图相关项。",
              size=8).pack(anchor="w", pady=(0, theme.px(10)))

        # ------------------------------------------------------- ① 布局骨架
        self.template_label = tk.Label(wrap, text="", bg=theme.BG, fg=theme.TEXT,
                                       font=theme.font(10), anchor="w")
        self.template_label.pack(fill="x")
        row = tk.Frame(wrap, bg=theme.BG)
        row.pack(fill="x", pady=(theme.px(6), 0))
        self.btn_templates = widgets.FlatButton(row, "换模板…", self.open_templates,
                                                font_size=8, padx=10, pady=4)
        self.btn_templates.pack(side="left")
        self.btn_reset = widgets.FlatButton(row, "恢复自动布局", self.reset_layout,
                                            font_size=8, padx=10, pady=4)
        self.btn_reset.pack(side="left", padx=(theme.px(6), 0))
        _note(wrap, "模板只改「卡片摆在哪」：关系、筛选、人工关系、导出的图都跟着同一份"
                    "骨架走，随时可以切回「自动」。").pack(
            anchor="w", pady=(theme.px(6), 0))
        self.set_template(template_key, template_name)

        # ------------------------------------------------------- ② 模型参与
        tk.Frame(wrap, bg=theme.BORDER, height=1).pack(fill="x", pady=theme.px(12))
        self.var_ask_model = tk.BooleanVar(value=self.read_ask_model())
        self.chk_ask_model = tk.Checkbutton(
            wrap, text="打开模板对话框时，也让模型看一眼该用哪种排法",
            variable=self.var_ask_model, command=self.toggle_ask_model,
            bg=theme.BG, fg=theme.TEXT, font=theme.font(9),
            activebackground=theme.BG, activeforeground=theme.TEXT,
            selectcolor=theme.BG, highlightthickness=0, bd=0, anchor="w")
        self.chk_ask_model.pack(anchor="w")
        _note(wrap, "默认关。勾上后，只有你在模板对话框里按「让模型也看一眼」时才会发一次"
                    "请求 —— 发出去的是这张图的关系类型与起止词名（例如「卷积 包含 池化」），"
                    "不含上下文、释义、依据与证据，也不含其它主题；模型只回一个排法名字与"
                    "一句理由，不会改动任何关系。关着时一直用本地规则，零额外花费。").pack(
            anchor="w", pady=(theme.px(4), 0))

        self.status = tk.Label(wrap, text="", bg=theme.BG, fg=theme.TEXT_MUTED,
                               font=theme.font(8), anchor="w", justify="left",
                               wraplength=theme.px(NOTE_WRAP))
        self.status.pack(fill="x", pady=(theme.px(12), 0))

        buttons = tk.Frame(wrap, bg=theme.BG)
        buttons.pack(fill="x", pady=(theme.px(6), 0))
        self.btn_close = widgets.FlatButton(buttons, "关闭", self.close, primary=True,
                                            font_size=8, padx=12, pady=5)
        self.btn_close.pack(side="right")

    # ---------------------------------------------------------------- 状态
    def _set_status(self, text: str, *, error: bool = False) -> None:
        self.status_text = str(text)
        label = getattr(self, "status", None)
        if label is None:  # pragma: no cover - 构造中途失败
            return
        try:
            label.configure(text=self.status_text,
                            fg=theme.ERR if error else theme.TEXT_MUTED)
        except tk.TclError:  # pragma: no cover - 极简替身
            pass

    def set_template(self, key: str, name: str = "") -> None:
        """刷新「当前的布局骨架」那一行（导图窗口切完模板会回调这里）。"""
        self.template_key = str(key or "")
        self.template_name = str(name or self.template_key or "自动")
        label = getattr(self, "template_label", None)
        if label is None:  # pragma: no cover - 构造中途失败
            return
        try:
            label.configure(text=f"当前的布局骨架：{self.template_name}")
        except tk.TclError:  # pragma: no cover - 极简替身
            pass

    # ------------------------------------------------------------ 模型开关
    def read_ask_model(self) -> bool:
        """设置里存着的开关值（读不到一律当「关」）。"""
        cfg = self.cfg
        if cfg is None:
            return False
        try:
            return bool(cfg.get_bool(ASK_MODEL_KEY, False))
        except Exception:  # pragma: no cover - 极简替身 / 坏配置
            return False

    def set_ask_model(self, value: bool, *, note: bool = True) -> bool:
        """写开关（立即生效）。返回**真正存进去**的值 —— 写不进去就如实返回旧值。"""
        wanted = bool(value)
        stored = wanted
        cfg = self.cfg
        if cfg is not None:
            try:
                cfg.set_bool(ASK_MODEL_KEY, wanted)
            except Exception:  # pragma: no cover - 极简替身
                stored = self.read_ask_model()
        var = getattr(self, "var_ask_model", None)
        if var is not None:
            try:
                var.set(wanted)
            except tk.TclError:  # pragma: no cover - 极简替身
                pass
        if note:
            if stored:
                self._set_status("已保存：模板对话框里会显示「让模型也看一眼」。")
            else:
                self._set_status("已保存：只用本地规则挑模板（不发任何请求）。")
        return bool(stored)

    def toggle_ask_model(self) -> bool:
        """勾选框的回调（Tk 在调用前已经翻好变量，这里只负责落盘 + 说明）。"""
        var = getattr(self, "var_ask_model", None)
        try:
            value = bool(var.get())
        except (AttributeError, tk.TclError):  # pragma: no cover - 极简替身
            value = not self.read_ask_model()
        return self.set_ask_model(value)

    # ---------------------------------------------------------------- 动作
    def open_templates(self) -> bool:
        """「换模板…」：转发给导图窗口（切换逻辑只有一处）。"""
        if self.on_open_templates is None:  # pragma: no cover - 独立打开时
            self._set_status("这里打不开模板列表：用工具条上的「模板…」。")
            return False
        try:
            self.on_open_templates()
        except Exception as exc:  # pragma: no cover - 调用方自己兜底
            self._set_status(f"打开模板列表失败：{exc}", error=True)
            return False
        return True

    def reset_layout(self) -> bool:
        """「恢复自动布局」：交给导图窗口（它知道怎么清固定位置 + 重画）。"""
        if self.on_reset_layout is None:  # pragma: no cover - 独立打开时
            return False
        try:
            self.on_reset_layout()
        except Exception as exc:  # pragma: no cover - 调用方自己兜底
            self._set_status(f"恢复自动布局失败：{exc}", error=True)
            return False
        self._set_status("已按骨架重排：手动固定过的卡片都放回去了。")
        return True

    def close(self) -> None:
        try:
            self.win.destroy()
        except tk.TclError:  # pragma: no cover - 已经关掉了
            pass
