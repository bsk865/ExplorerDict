"""设置对话框（只放用户要填、要开的东西：API、追问、取词与游戏）。

这里**没有**阅读应用清单、取词范围、长度、去重那类需要用户理解的参数 ——
默认就是「只要能选中文本就尝试读取；游戏与全屏自动避让」。

界面只保留：

* **解释与追问 API**：**服务商一键模板**（点一下只预填地址 + 模型名，Key 永远由用户
  自己粘）/ Base URL / 模型名（可手输 + 预设按钮）/ **推理强度**（单选按钮
  + 说明行，**档位跟着模型名变**：DeepSeek 是 low / high / max，其它模型是
  low / medium / high）/ 超时 / API Key（保存 / 清除），并显示一行
  「当前生效：<模型> · 推理强度 <x>」；
* **追问**：带上最近几轮 + 追问开关；
* **取词与游戏**：游戏模式 / 暂停取词。

两个按钮分工明确，别混：

* **检查配置**（`test_connection`）= 本地格式检查，**不联网**；
* **测试连接**（`test_connection_live`）= 发一条极轻量请求（``max_tokens=8`` 的一句
  ping），验证「地址 + Key + 网络」三件事，成功 / 失败原因直接写在反馈行里 ——
  用户不用靠「划一次词试试」来发现配置错了。

界面**没有**「面板与主界面置顶」开关：浮窗在普通阅读下自动置顶、主界面始终
普通层级（游戏 / 全屏 / 暂停的硬阻断照旧），那个开关既无用又和实际层级矛盾。
旧配置字段 ``ui.topmost`` 仍然读得出来（兼容老库），但保存时不再改写它。

诊断、路径、进程与服务状态之类的技术细节不上界面（只写日志）。API Key 只显示
「已保存 / 未配置」，界面永不回显明文，也不读取环境变量。

.. warning::
   **本模块绝不导入 ``tkinter.ttk``**：冻结运行时（``探索词典.exe`` 旁边的
   ``_runtime``）里**没有** ``tkinter.ttk``（打包时没有任何模块 import 它，
   PyInstaller 就不会收集），一旦导入就是 ``ImportError``，整个程序在
   ``import app.main`` 阶段就崩 —— 2026-10-03 真实发生过一次（下拉框版本）。
   需要「下拉」这类控件时用 :mod:`app.ui.widgets` 里的自绘控件或
   ``tk.Entry`` / ``tk.Radiobutton``。``tests.test_runtime_recovery`` 里有护栏测试。
"""
from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox

from ..config import (MODEL_PRESETS, PROVIDER_PRESETS, REASONING_LABELS,
                      REASONING_SHORT, effort_choices, effort_from_label)
from ..logging_setup import get_logger
from . import theme, widgets

log = get_logger("settings")


def _entry(parent, textvariable=None, **kw) -> tk.Entry:
    kw.setdefault("font", theme.font(9))
    return tk.Entry(
        parent, textvariable=textvariable, relief="flat", bd=0,
        highlightthickness=1, highlightbackground=theme.BORDER,
        highlightcolor=theme.TEXT, bg=theme.PANEL, fg=theme.TEXT,
        insertbackground=theme.TEXT, **kw,
    )


class SettingsDialog:
    def __init__(self, master: tk.Misc, app):
        self.app = app
        self.cfg = app.config
        self.win = tk.Toplevel(master)
        self.win.title("设置 — 探索词典")
        self.win.configure(bg=theme.BG)
        self.win.transient(master)
        self.win.resizable(False, False)
        #: 自绘无框 chrome（与主界面 / 导图 / 手动录入共用）：Esc / Alt+F4 / ×
        #: 都只**关掉本窗**，绝不退出程序。
        self.chrome = widgets.BorderlessChrome(self.win, title="设置",
                                               on_close=self.win.destroy,
                                               bg=theme.PANEL)

        wrap = tk.Frame(self.win, bg=theme.BG)
        wrap.pack(fill="both", expand=True, padx=theme.px(16), pady=theme.px(12))

        #: 校验 / 结果反馈**内联**在窗口里（不弹系统 messagebox）
        self.feedback = tk.Label(wrap, text="", bg=theme.BG, fg=theme.TEXT_MUTED,
                                 font=theme.font(8), anchor="w", justify="left",
                                 wraplength=theme.px(520))

        # ---------------------------------------------------------- API
        #: 「测试连接」是否在途（在途时按钮禁用、反馈行说明，避免连点发多条请求）
        self._ping_busy = False
        self._section(wrap, "解释与追问 API")

        grid = tk.Frame(wrap, bg=theme.BG)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        self.var_base = tk.StringVar(value=self.cfg.base_url)
        self.var_model = tk.StringVar(value=self.cfg.model)
        #: 推理强度存的是**字段裸值**（``""`` / low / high / max …），不是界面文案：
        #: 单选框直接绑裸值，界面文案只出现在按钮与说明行里。可选的档位由模型名决定，
        #: 见 :meth:`_rebuild_efforts`。
        self.var_effort = tk.StringVar(value=self.cfg.reasoning_effort)
        self.var_timeout = tk.StringVar(value=str(self.cfg.get_int("api.timeout", 25)))
        self.var_key = tk.StringVar(value="")

        #: 服务商一键模板：点一下只**预填**地址与模型名（不落库、不联网、不含任何
        #: 密钥）—— 用户只需要粘自己的 Key。手输地址 / 模型名照样可以。
        self.provider_buttons: list = []
        provider_row = tk.Frame(grid, bg=theme.BG)
        for name, base, model in PROVIDER_PRESETS:
            button = widgets.FlatButton(provider_row, name,
                                         command=lambda n=name: self._pick_provider(n),
                                         font_size=8, padx=6, pady=3)
            button.pack(side="left", padx=(0, theme.px(4)))
            self.provider_buttons.append(button)
        self._row(grid, 0, "服务商", provider_row)
        self._row(grid, 1, "Base URL", _entry(grid, textvariable=self.var_base, width=46))
        #: 模型名 = **可手输**输入框（预设只是候选，第三方网关的模型名照样手输）。
        #: 这里刻意**不用** ``ttk.Combobox`` —— 冻结运行时里没有 ``tkinter.ttk``。
        self.model_entry = _entry(grid, textvariable=self.var_model, width=34)
        self._row(grid, 2, "模型名", self.model_entry)
        preset_row = tk.Frame(grid, bg=theme.BG)
        self.preset_buttons: list = []
        for name in MODEL_PRESETS:
            button = widgets.FlatButton(preset_row, name,
                                        command=lambda n=name: self._pick_model(n),
                                        font_size=8, padx=6, pady=3)
            button.pack(side="left", padx=(0, theme.px(4)))
            self.preset_buttons.append(button)
        self._row(grid, 3, "预设", preset_row)
        #: 推理强度 = 单选按钮组：空串 = 请求体里根本不带 ``reasoning_effort``。
        #: **档位随模型变化**（DeepSeek 只有 low / high / max），所以模型名一改就要
        #: 重画这排按钮 —— 见 :meth:`_rebuild_efforts`。
        self.effort_row = tk.Frame(grid, bg=theme.BG)
        self.effort_buttons: list = []
        self._row(grid, 4, "推理强度", self.effort_row)
        #: 选中那一档的**完整**解释（按钮上只放得下短文案）
        self.effort_hint = tk.Label(grid, text="", bg=theme.BG, fg=theme.TEXT_FAINT,
                                    font=theme.font(8), anchor="w", justify="left",
                                    wraplength=theme.px(360))
        self._row(grid, 5, "", self.effort_hint)
        self._rebuild_efforts()
        #: 模型名一变就换档位（预设按钮与手输都走这里）
        self.var_model.trace_add("write", lambda *_a: self._rebuild_efforts())
        self._row(grid, 6, "超时(秒)", _entry(grid, textvariable=self.var_timeout, width=10))

        key_frame = tk.Frame(grid, bg=theme.BG)
        key_entry = _entry(key_frame, textvariable=self.var_key, width=34, show="●")
        key_entry.pack(side="left")
        tk.Label(key_frame, text="留空=不修改", bg=theme.BG, fg=theme.TEXT_FAINT,
                 font=theme.font(8)).pack(side="left", padx=theme.px(6))
        self._row(grid, 7, "API Key", key_frame)

        #: 「当前生效」= 已经**存进库**的那份配置（不是编辑框里还没保存的内容）。
        #: 用户明确要求：界面上要能看出正在用哪个模型、什么推理强度。
        self.effective = tk.Label(wrap, text="", bg=theme.BG, fg=theme.TEXT_MUTED,
                                  font=theme.font(8), anchor="w", justify="left",
                                  wraplength=theme.px(520))
        self.effective.pack(anchor="w", padx=theme.px(10), pady=(theme.px(4), 0))
        self._sync_effective()

        tk.Label(wrap, text="模型名可手输（预设按钮只是候选）；推理强度档位按模型名给出 ——"
                           "DeepSeek 是 low / high / max，其它模型是 low / medium / high，"
                           "「不发送」最兼容。改模型或推理强度后，旧的解释缓存与关系图"
                           "不会命中，会重新问一次。\n"
                           "「检查配置」只做本地格式检查（不联网）；「测试连接」会真的发"
                           "一条极轻量请求，验证地址 / Key / 网络是否通。",
                 bg=theme.BG, fg=theme.TEXT_FAINT, font=theme.font(8), anchor="w",
                 justify="left", wraplength=theme.px(520)).pack(
            anchor="w", padx=theme.px(10), pady=(theme.px(2), 0))

        status = "已保存" if self.cfg.has_api_key() else "未配置"
        self.key_status = tk.Label(wrap, text=f"Key 状态：{status}", bg=theme.BG,
                                   fg=theme.TEXT_MUTED, font=theme.font(8))
        self.key_status.pack(anchor="w", padx=theme.px(10), pady=(theme.px(3), 0))

        # ---------------------------------------------------------- 追问
        self._section(wrap, "追问")
        chat = tk.Frame(wrap, bg=theme.BG)
        chat.pack(fill="x")
        chat.columnconfigure(1, weight=1)
        self.var_chat_turns = tk.StringVar(value=str(self.cfg.chat_history_turns))
        self._row(chat, 0, "带上最近几轮", _entry(chat, textvariable=self.var_chat_turns,
                                                  width=8))
        # 旧配置里的「未配置 Key 的提示」不再提供输入框，但保存时仍沿用已存的值，
        # 老设置不会因为界面精简而失效（面板仍按原文案提示）。
        self.var_chat_hint = tk.StringVar(value=self.cfg.chat_no_key_hint)
        self.var_chat_enabled = tk.BooleanVar(value=self.cfg.chat_enabled)
        tk.Checkbutton(chat, text="允许在面板里追问",
                       variable=self.var_chat_enabled, bg=theme.BG, fg=theme.TEXT,
                       font=theme.font(9), activebackground=theme.BG,
                       activeforeground=theme.TEXT, selectcolor=theme.BG,
                       highlightthickness=0, bd=0, anchor="w").grid(
            row=1, column=0, columnspan=2, sticky="w", padx=theme.px(10), pady=theme.px(3))

        # ---------------------------------------------------------- 取词行为
        self._section(wrap, "取词与游戏")

        self.var_game = tk.BooleanVar(value=self.cfg.game_mode)
        self.var_paused = tk.BooleanVar(value=not self.cfg.capture_enabled)

        opts = tk.Frame(wrap, bg=theme.BG)
        opts.pack(fill="x", padx=theme.px(10), pady=(theme.px(6), 0))
        for text, var in (
            ("游戏模式", self.var_game),
            ("暂停取词（Ctrl+Alt+Shift+P）", self.var_paused),
        ):
            tk.Checkbutton(opts, text=text, variable=var, bg=theme.BG, fg=theme.TEXT,
                           font=theme.font(9), activebackground=theme.BG,
                           activeforeground=theme.TEXT, selectcolor=theme.BG,
                           highlightthickness=0, bd=0, anchor="w").pack(fill="x")

        # ---------------------------------------------------------- 关系图（搬走了）
        # 本轮（用户口径 2026-10-04）：「思维导图的导出和相关设置都需要在导图界面中，
        # 主界面不应该显示导图的相关设置」—— 原来这里的「关系图」一节（模板是否让模型
        # 也看一眼）已搬进导图窗口的「导图设置…」，主界面不再显示，也不在这里保存。
        # 设置键仍是 ``map.template_ask_model``（默认关），代码见
        # :mod:`app.ui.map_settings_dialog`。

        # ---- 同一个概念再次被划到（B4）：建新词条 / 追加上下文 ----
        tk.Label(wrap, text="同一个概念再次被划到（上下文不同）：", bg=theme.BG,
                 fg=theme.TEXT_MUTED, font=theme.font(8), justify="left",
                 anchor="w").pack(anchor="w", padx=theme.px(10), pady=(theme.px(10), 0))
        self.var_dup_action = tk.StringVar(value=self.cfg.duplicate_action)
        self.dup_buttons: list = []
        dup_row = tk.Frame(wrap, bg=theme.BG)
        dup_row.pack(fill="x", padx=theme.px(10))
        for value, text in (("new", "新建词条"),
                            ("append", "追加上下文（同一词条收齐各处语境）")):
            button = tk.Radiobutton(
                dup_row, text=text, variable=self.var_dup_action, value=value,
                bg=theme.BG, fg=theme.TEXT, font=theme.font(9),
                activebackground=theme.BG, activeforeground=theme.TEXT,
                selectcolor=theme.PANEL, highlightthickness=0, bd=0, anchor="w",
            )
            button.pack(anchor="w")
            self.dup_buttons.append(button)
        tk.Label(wrap, text="选「追加上下文」时，词条的解释会被标成「需重新解释」"
                            "（内容变了，旧解释不再对应）。",
                 bg=theme.BG, fg=theme.TEXT_MUTED, font=theme.font(8), justify="left",
                 wraplength=theme.px(520)).pack(anchor="w", padx=theme.px(10))

        tk.Label(wrap, text="快捷键：" + self._shortcut_line(),
                 bg=theme.BG, fg=theme.TEXT_MUTED, font=theme.font(8), justify="left",
                 wraplength=theme.px(520)).pack(anchor="w", padx=theme.px(10),
                                                pady=(theme.px(8), 0))

        # ---------------------------------------------------------- 反馈 + 按钮
        self.feedback.pack(fill="x", padx=theme.px(10), pady=(theme.px(10), 0))
        btns = tk.Frame(wrap, bg=theme.BG)
        btns.pack(fill="x", pady=(theme.px(8), 0))
        widgets.FlatButton(btns, "保存", self.save, primary=True, font_size=9,
                           padx=16, pady=5).pack(side="right", padx=theme.px(4))
        widgets.FlatButton(btns, "取消", self.win.destroy, font_size=9, padx=14,
                           pady=5).pack(side="right")
        widgets.FlatButton(btns, "清除已保存的 Key", self.clear_key, font_size=8,
                           padx=10, pady=4).pack(side="left", padx=theme.px(4))
        widgets.FlatButton(btns, "检查配置", self.test_connection, font_size=8,
                           padx=10, pady=4).pack(side="left", padx=theme.px(4))
        #: 真联网的连通性测试（用户明确要求的「测试连接」）：只在点击时才发请求。
        self.btn_ping = widgets.FlatButton(btns, "测试连接", self.test_connection_live,
                                           font_size=8, padx=10, pady=4)
        self.btn_ping.pack(side="left", padx=theme.px(4))

        self.win.update_idletasks()
        self._center(master)

    def _set_feedback(self, text: str) -> bool:
        """窗口内**内联**反馈（校验失败 / 检查结果都在这里说一句）。"""
        try:
            if str(self.feedback.cget("text")) == str(text):
                return False
            self.feedback.configure(text=str(text))
            return True
        except tk.TclError:  # pragma: no cover
            return False

    def _pick_model(self, name: str) -> None:
        """预设按钮：只把名字填进**可手输**的输入框，落库仍要用户点「保存」。"""
        self.var_model.set(str(name))

    def _rebuild_efforts(self) -> None:
        """按当前模型名重画推理强度单选按钮。

        DeepSeek 的 ``reasoning_effort`` 只有 low / high / max，别的模型是 OpenAI 风格
        的三档（见 ``config.effort_choices``）。换模型后原来那一档可能不存在（例如从
        OpenAI 式网关切到 DeepSeek 时的 ``medium``）—— 此时**退回「不发送」并在说明行
        里讲清楚**，绝不静默发一个模型不认的值。
        """
        tiers = effort_choices(self.var_model.get())
        current = str(self.var_effort.get() or "").strip().lower()
        dropped = current if current not in tiers else ""
        if dropped:
            self.var_effort.set("")
        for button in self.effort_buttons:
            try:
                button.destroy()
            except tk.TclError:  # pragma: no cover - 窗口正在销毁
                pass
        self.effort_buttons = []
        for value in tiers:
            button = tk.Radiobutton(
                self.effort_row, text=REASONING_SHORT[value], variable=self.var_effort,
                value=value, command=self._on_effort_pick, bg=theme.BG, fg=theme.TEXT,
                font=theme.font(9), activebackground=theme.BG,
                activeforeground=theme.TEXT, selectcolor=theme.PANEL,
                highlightthickness=0, bd=0, anchor="w")
            button.pack(side="left", padx=(0, theme.px(8)))
            self.effort_buttons.append(button)
        self._on_effort_pick()
        if dropped:
            self._set_feedback(
                f"「{dropped}」不在 {self.var_model.get() or '该模型'} 的档位里，"
                f"推理强度已回到「不发送」（原来的设置要保存才会改）。")

    def _on_effort_pick(self) -> None:
        """刷新推理强度的完整说明（单选按钮上只放得下短文案）。"""
        effort = effort_from_label(self.var_effort.get())
        text = REASONING_LABELS.get(effort, REASONING_LABELS[""])
        try:
            self.effort_hint.configure(text=text)
        except tk.TclError:  # pragma: no cover
            pass

    def _sync_effective(self) -> None:
        """刷新「当前生效」行：显示已经保存的那份模型 + 推理强度。"""
        try:
            text = f"当前生效：{self.cfg.model_display}"
        except Exception:  # pragma: no cover - 配置替身
            return
        try:
            self.effective.configure(text=text)
        except tk.TclError:  # pragma: no cover
            pass

    # ------------------------------------------------------------- 构建辅助
    @staticmethod
    def _shortcut_line() -> str:
        """可用快捷键的**清单**（不含注册状态等技术细节）。"""
        from ..hotkeys import HOTKEYS, labels_for

        return labels_for(sorted(HOTKEYS)).replace("  ", " ")

    def _section(self, parent, text: str) -> None:
        tk.Label(parent, text=text, bg=theme.BG, fg=theme.TEXT_MUTED,
                 font=theme.tracking_label(7)).pack(anchor="w", padx=theme.px(10),
                                                    pady=(theme.px(12), theme.px(3)))

    def _row(self, grid, row: int, label: str, widget: tk.Widget) -> None:
        tk.Label(grid, text=label, bg=theme.BG, fg=theme.TEXT_FAINT, font=theme.font(8),
                 width=18, anchor="w").grid(row=row, column=0, sticky="w",
                                            padx=theme.px(10), pady=theme.px(3))
        widget.grid(row=row, column=1, sticky="we", padx=theme.px(4), pady=theme.px(3),
                    ipady=theme.px(2))

    def diagnostics_text(self) -> str:
        """诊断文本（路径 / 计数 / 服务状态）：**只写日志 / 排查用，不上界面**。"""
        from .. import paths

        db = self.app.db
        lines = [
            f"数据库：{paths.db_path()}",
            f"数据目录：{paths.data_dir()}",
            f"模型配置：{self.cfg.base_url} | {self.cfg.model} | 超时 {self.cfg.timeout:g}s",
            f"当前生效：{self.cfg.model_display}",
            f"Key：{'已配置' if self.cfg.has_api_key() else '未配置'}",
            f"词条 {db.count_entries()} 条 · 主题 {len(db.list_batches())} 个 · "
            f"解释缓存 {db.count_cache()} 条 · 追问轮次 {db.count_chat_turns()} 条",
            f"取词：{'开启' if self.cfg.capture_enabled else '已暂停'} · "
            f"游戏模式：{'开' if self.cfg.game_mode else '关'} · "
            f"浮窗层级：{'普通阅读自动置顶' if not self.app.gate_controller.is_locked() else '硬阻断（不置顶）'}",
            f"热键：{self.app.hotkey_status_text()} · UIA：{self.app.uia_status_text()}",
            f"门控：{self.app.gate_decision().status_text()}",
        ]
        return "\n".join(lines)

    def _center(self, master) -> None:
        try:
            x = master.winfo_rootx() + max(0, (master.winfo_width() - self.win.winfo_width()) // 2)
            y = master.winfo_rooty() + theme.px(60)
            self.win.geometry(f"+{x}+{y}")
        except (tk.TclError, AttributeError):
            # 真实 Tk 不该走到这里；无窗口测试里的假 master 没有 winfo_rootx，
            # 那就干脆不摆位置 —— 但绝不吞掉窗口构造本身。
            pass

    # ------------------------------------------------------------- 动作
    def save(self) -> None:
        try:
            timeout = max(3, int(self.var_timeout.get().strip() or "25"))
        except ValueError:
            self._set_feedback("超时必须是整数")
            return
        try:
            turns = max(0, min(50, int(self.var_chat_turns.get().strip() or "6")))
        except ValueError:
            self._set_feedback("「带上最近几轮」必须是整数")
            return

        base = self.var_base.get().strip()
        if base and not base.lower().startswith(("http://", "https://")):
            self._set_feedback("Base URL 必须以 http:// 或 https:// 开头")
            return

        self.cfg.set("api.base_url", base)
        self.cfg.set("api.model", self.var_model.get().strip())
        self.cfg.set_reasoning_effort(effort_from_label(self.var_effort.get()))
        self.cfg.set_int("api.timeout", timeout)
        self.cfg.set_int("chat.history_turns", turns)
        hint = self.var_chat_hint.get().strip()
        if hint:
            self.cfg.set("chat.no_key_hint", hint)
        self.cfg.set_bool("chat.enabled", self.var_chat_enabled.get())
        self.cfg.set_bool("gate.game_mode", self.var_game.get())
        self.cfg.set_bool("capture.enabled", not self.var_paused.get())
        #: 关系图那一节（``map.template_ask_model``）本轮搬进「导图设置…」——
        #: 主界面不再显示、也不再写这个键（见 app\ui\map_settings_dialog.py）
        #: 「追加上下文」只认 "append"，其它都是 "new"（见 Config.duplicate_action）
        self.cfg.set("capture.duplicate_action",
                     "append" if str(self.var_dup_action.get()) == "append" else "new")

        key = self.var_key.get().strip()
        if key:
            self.cfg.set_api_key(key)
            self.var_key.set("")
            self.key_status.configure(text="Key 状态：已保存")
            log.info("已更新 API Key")

        self.app.on_settings_changed()
        self._sync_effective()
        #: 关窗本身就是「已保存」的反馈；主界面状态行还会再说一句（不弹 messagebox）
        self.app.restore_pending_explain_window()
        self.win.destroy()

    def clear_key(self) -> None:
        if not self.cfg.has_api_key():
            self._set_feedback("当前没有保存的 Key")
            return
        if messagebox.askyesno("设置", "确定清除已保存的 API Key？", parent=self.win):
            self.cfg.db.clear_secret("api_key")
            self.key_status.configure(text="Key 状态：未配置")
            self.app.on_settings_changed()
            self._set_feedback("已清除 Key")
            log.info("已清除 API Key")

    def test_connection(self) -> None:
        """只做「配置是否完整」的本地检查，不发起收费调用。"""
        ok, msg = self.app.explain_service.is_ready()
        base = self.var_base.get().strip()
        if not base.lower().startswith(("http://", "https://")):
            self._set_feedback("Base URL 不合法")
            return
        if not ok and not self.var_key.get().strip():
            self._set_feedback(f"配置不完整：{msg}")
            return
        effort = effort_from_label(self.var_effort.get())
        note = f"；推理强度：{REASONING_LABELS.get(effort, REASONING_LABELS[''])}"
        self._set_feedback("配置格式检查通过" + note +
                           "（保存后点浮窗「解释并记录」即可验证；想现在就验证请点"
                           "「测试连接」）")

    # ------------------------------------------------------------- 服务商模板
    def _pick_provider(self, name: str) -> None:
        """点服务商模板：只把地址与模型名填进编辑框（**不落库、不联网、不填 Key**）。

        用户按「保存」才写进配置；Key 永远由用户自己粘 —— 模板里没有任何密钥。
        """
        for preset_name, base, model in PROVIDER_PRESETS:
            if preset_name != name:
                continue
            self.var_base.set(base)
            self.var_model.set(model)  # 经 trace 触发 _rebuild_efforts()
            self._set_feedback(f"已填入「{name}」的地址与模型名。API Key 请自己粘贴，"
                               f"保存后点「测试连接」可以立刻验证通不通。")
            return
        self._set_feedback(f"没有名为「{name}」的服务商模板")

    # ------------------------------------------------------------- 测试连接
    def _connection_targets(self) -> tuple[str, str, str, float] | None:
        """准备「测试连接」要用的四个值；任何一项不合格就反馈一句并返回 ``None``。

        Key 优先取**上面刚粘的**（可能还没保存），为空才用已保存的那把 ——
        「先粘 Key → 点测试 → 再保存」这个顺序最顺手。
        """
        base = self.var_base.get().strip()
        model = self.var_model.get().strip()
        key = self.var_key.get().strip()
        if not key:
            try:
                key = self.cfg.api_key() or ""
            except Exception:  # pragma: no cover - 配置替身
                key = ""
        if not base.lower().startswith(("http://", "https://")):
            self._set_feedback("Base URL 必须以 http:// 或 https:// 开头")
            return None
        if not model:
            self._set_feedback("请先填模型名（服务商模板或预设按钮可以一键填）")
            return None
        if not key:
            self._set_feedback("还没有可用的 Key：请在上面填入 API Key 并保存，再点测试")
            return None
        try:
            timeout = float(self.var_timeout.get().strip() or "25")
        except ValueError:
            timeout = 25.0
        # 测试连接只问「通不通」，不该等满用户的完整超时：最长 15s。
        return base, model, key, max(3.0, min(timeout, 15.0))

    def test_connection_live(self) -> None:
        """真·测试连接：发一条极轻量请求验证 **地址 / Key / 网络**。

        这是设置页里**唯一**会联网的按钮，只在用户点击时发生；请求体里只有一句
        ``ping``（``max_tokens=8``），成功或失败原因都写在反馈行里。
        """
        if self._ping_busy:
            self._set_feedback("正在测试连接，请稍候…")
            return
        targets = self._connection_targets()
        if targets is None:
            return
        base, model, _key, _timeout = targets
        self._set_feedback(f"正在测试连接：{base} · {model} …")
        self._start_connection_test(*targets)

    def _start_connection_test(self, base: str, model: str, key: str,
                               timeout: float) -> None:
        """在后台线程里发这条请求（界面线程绝不阻塞）；测试里可替换成同步桩。"""
        self._ping_busy = True
        try:
            self.btn_ping.set_enabled(False)
        except Exception:  # pragma: no cover - 假控件 / 窗口已销毁
            pass
        threading.Thread(target=self._ping_worker,
                         args=(base, model, key, timeout), daemon=True).start()

    def _ping_worker(self, base: str, model: str, key: str, timeout: float) -> None:
        """后台线程：只调 :func:`app.api_client.ping_endpoint`，结果交回界面线程。"""
        from .. import api_client

        try:
            ok, message = api_client.ping_endpoint(base_url=base, model=model,
                                                   api_key=key, timeout=timeout)
        except Exception as exc:  # pragma: no cover - ping_endpoint 自己已兜底
            ok, message = False, f"连接失败：{exc}"
        self._finish_connection_test(bool(ok), str(message))

    def _finish_connection_test(self, ok: bool, message: str) -> None:
        """回到界面线程更新反馈。

        无窗口测试里的假窗口**没有** ``after``（AttributeError）或窗口已销毁
        （``TclError``）：那就直接同步调用 —— 反馈照样写得出来。
        """
        text = ("✓ " if ok else "✗ ") + str(message)
        try:
            self.win.after(0, lambda: self._apply_ping_result(text))
        except Exception:  # pragma: no cover - 假 Tk / 窗口已销毁
            self._apply_ping_result(text)

    def _apply_ping_result(self, text: str) -> None:
        self._ping_busy = False
        try:
            self.btn_ping.set_enabled(True)
        except Exception:  # pragma: no cover
            pass
        self._set_feedback(text)
        log.info("测试连接：%s", text)
