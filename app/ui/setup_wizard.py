"""首次配置向导（纯 tk；**不发网络请求、不写 Key**）。

第一次打开主界面时弹一次（标记 ``ui.wizard_done`` 落库），解决两个真实问题：

* 新用户以为这是「划词就出翻译」的词典 —— 划了半天什么都没发生，以为坏了。
  这里明确说清：划选只弹一个浮条，**只有点「解释并记录」才会联网**，保存的是
  「术语 + 当时的原文上下文」；
* 新用户不知道 Base URL / 模型名该填什么 —— 这里让用户选「云端 API」还是
  「本地推理服务（Ollama）」，直接把地址与模型名写进配置，用户只需要粘自己的 Key。

两种选择的差别（都写进界面里）：

* 云端 API：解释质量最好，按量计费，需要自备 Key（只存本机，DPAPI 加密）；
* 本地 Ollama：完全离线、不花钱，但要先自己装好 Ollama 并拉过模型。

**向导本身不联网** —— 「通不通」由设置页的「测试连接」按钮负责（用户显式点击）。
「×」关闭等同「先随便看看」：标记为已看过，不再打扰。

.. warning::
   与其它界面模块同样的规矩：**绝不导入 ``tkinter.ttk``**（冻结运行时里没有），
   也**不用** ``filedialog`` / ``messagebox`` 之外的系统对话框。
"""
from __future__ import annotations

import tkinter as tk

from ..config import LOCAL_ENDPOINT, PROVIDER_PRESETS
from ..logging_setup import get_logger
from . import theme, widgets

log = get_logger("wizard")

#: 正文：先把「它不是什么」说清楚（用户明确要求消除「划词就出翻译」的错误预期）
INTRO_TEXT = (
    "这不是「划词就出翻译」的词典。\n"
    "划选一个词只会弹出一个小浮条；只有你点「解释并记录」，它才会把这个词和它出现"
    "的那句话发给模型解释，并存成一条能反复查的术语。\n"
    "它的价值在于把读到的术语连同当时的原文上下文攒起来 —— 之后翻主题、看关系图，"
    "靠的都是这份底账。"
)

#: Key 的归属（软件不内置、不代申请）
KEY_NOTE = ("API Key 只有你自己有：本软件不内置任何密钥，也不会代你申请。"
            "填好保存后，点设置页的「测试连接」可以立刻验证通不通。")

CLOUD_HINT = ("云端 API：解释质量最好，按量计费；需要自备 Key（只存在本机，"
              "用 Windows DPAPI 加密）。")
LOCAL_HINT = ("本地推理服务：完全离线、不花钱；但要先自己装好 Ollama（默认端口 "
              "11434）并拉过模型，模型能力弱一些。")


def should_show(cfg, *, gate_locked: bool = False) -> bool:
    """要不要弹首次配置向导：只看一个落库标记（外加门控没被硬阻断）。

    ``gate_locked`` 为真（游戏 / 全屏 / 暂停这类硬阻断）时**不弹** —— 那种场景下
    任何窗口都不该冒出来，等下次打开主界面再说。读配置出错也当「不弹」，
    绝不因为一个向导让主界面打不开。
    """
    if gate_locked:
        return False
    try:
        return not bool(cfg.get_bool("ui.wizard_done", False))
    except Exception:  # pragma: no cover - 配置替身 / 老库异常
        return False


class SetupWizard:
    """首次配置向导窗口。构造即显示；关窗请用 :meth:`finish` / ``×``。"""

    def __init__(self, master: tk.Misc, app):
        self.app = app
        self.cfg = app.config
        self.mode = self._default_mode()
        self.win = tk.Toplevel(master)
        self.win.title("首次配置向导 — 探索词典")
        self.win.configure(bg=theme.BG)
        self.win.transient(master)
        self.win.resizable(False, False)
        #: 与主界面 / 设置页共用自绘无框 chrome：Esc / Alt+F4 / × 只关本窗。
        self.chrome = widgets.BorderlessChrome(self.win, title="首次配置向导",
                                               on_close=self._on_close, bg=theme.PANEL)

        wrap = tk.Frame(self.win, bg=theme.BG)
        wrap.pack(fill="both", expand=True, padx=theme.px(18), pady=theme.px(14))

        tk.Label(wrap, text="先花一分钟，把「解释」这一步配好", bg=theme.BG,
                 fg=theme.TEXT, font=theme.font(12), anchor="w").pack(anchor="w")
        tk.Label(wrap, text=INTRO_TEXT, bg=theme.BG, fg=theme.TEXT_MUTED,
                 font=theme.font(9), anchor="w", justify="left",
                 wraplength=theme.px(520)).pack(anchor="w", pady=(theme.px(8), theme.px(12)))

        tk.Label(wrap, text="解释用哪个模型？", bg=theme.BG, fg=theme.TEXT,
                 font=theme.font(10), anchor="w").pack(anchor="w")
        row = tk.Frame(wrap, bg=theme.BG)
        row.pack(anchor="w", pady=(theme.px(6), theme.px(6)))
        self.cloud_button = widgets.FlatButton(row, "用云端 API（推荐）",
                                               lambda: self.choose("cloud"),
                                               primary=True, font_size=9, padx=12, pady=6)
        self.cloud_button.pack(side="left")
        self.local_button = widgets.FlatButton(row, "用本地推理服务（Ollama）",
                                               lambda: self.choose("local"),
                                               font_size=9, padx=12, pady=6)
        self.local_button.pack(side="left", padx=(theme.px(8), 0))

        #: 当前选择会写进配置的地址与模型名（**只显示，不联网**）
        self.choice_hint = tk.Label(wrap, text="", bg=theme.BG, fg=theme.TEXT,
                                    font=theme.font(9), anchor="w", justify="left",
                                    wraplength=theme.px(520))
        self.choice_hint.pack(anchor="w", pady=(theme.px(4), theme.px(2)))
        self.mode_hint = tk.Label(wrap, text="", bg=theme.BG, fg=theme.TEXT_FAINT,
                                  font=theme.font(8), anchor="w", justify="left",
                                  wraplength=theme.px(520))
        self.mode_hint.pack(anchor="w", pady=(0, theme.px(8)))
        tk.Label(wrap, text=KEY_NOTE, bg=theme.BG, fg=theme.TEXT_FAINT,
                 font=theme.font(8), anchor="w", justify="left",
                 wraplength=theme.px(520)).pack(anchor="w", pady=(0, theme.px(10)))

        btns = tk.Frame(wrap, bg=theme.BG)
        btns.pack(fill="x", pady=(theme.px(4), 0))
        widgets.FlatButton(btns, "去填 API Key（打开设置）",
                           lambda: self.finish(open_settings=True), primary=True,
                           font_size=9, padx=14, pady=5).pack(side="right")
        widgets.FlatButton(btns, "先随便看看", lambda: self.finish(open_settings=False),
                           font_size=9, padx=12, pady=5).pack(side="right",
                                                              padx=(0, theme.px(6)))

        self._refresh_hint()
        self.win.update_idletasks()
        self._center(master)

    # ------------------------------------------------------------- 选择
    def _default_mode(self) -> str:
        """当前配置是不是已经指向本机（用来决定默认选中哪一项）。"""
        try:
            base = (self.cfg.base_url or "").lower()
        except Exception:  # pragma: no cover - 配置替身
            base = ""
        return "local" if ("127.0.0.1" in base or "localhost" in base) else "cloud"

    def choice_target(self, mode: str) -> tuple[str, str]:
        """该选项要写进配置的 ``(Base URL, 模型名)``。

        * ``local``：:data:`app.config.LOCAL_ENDPOINT`（Ollama 默认端点）；
        * ``cloud``：**保持用户现有的地址与模型名**（默认就是官方 DeepSeek），
          绝不用向导去覆盖用户已经填好的第三方网关。
        """
        if mode == "local":
            return LOCAL_ENDPOINT
        base = ""
        model = ""
        try:
            base = (self.cfg.base_url or "").strip()
            model = (self.cfg.model or "").strip()
        except Exception:  # pragma: no cover - 配置替身
            pass
        return (base or PROVIDER_PRESETS[0][1], model or PROVIDER_PRESETS[0][2])

    def choose(self, mode: str) -> None:
        """点选一种：只改界面上的说明，**先不落库**（按「完成」/「去填 Key」才写）。"""
        self.mode = "local" if mode == "local" else "cloud"
        self._refresh_hint()

    def _refresh_hint(self) -> None:
        base, model = self.choice_target(self.mode)
        hint = LOCAL_HINT if self.mode == "local" else CLOUD_HINT
        try:
            self.choice_hint.configure(text=f"将使用：{base} · {model}")
            self.mode_hint.configure(text=hint)
        except tk.TclError:  # pragma: no cover - 窗口正在销毁
            pass

    # ------------------------------------------------------------- 落库 / 收尾
    def apply_choice(self, mode: str | None = None) -> tuple[str, str]:
        """把当前选择写进配置（只写 ``api.base_url`` / ``api.model``，不碰 Key）。"""
        mode = mode or self.mode
        base, model = self.choice_target(mode)
        self.mode = "local" if mode == "local" else "cloud"
        try:
            self.cfg.set("api.base_url", base)
            self.cfg.set("api.model", model)
        except Exception:  # pragma: no cover - 配置替身 / 数据库异常
            log.warning("向导写入地址与模型名失败", exc_info=True)
        return base, model

    def finish(self, *, open_settings: bool = False) -> None:
        """收尾：写配置 + 标记「已看过」+ 刷新服务 + 关窗（可选打开设置页）。"""
        base, model = self.apply_choice()
        try:
            self.cfg.set_bool("ui.wizard_done", True)
        except Exception:  # pragma: no cover
            log.warning("向导标记写入失败", exc_info=True)
        log.info("首次配置向导完成：%s · %s（打开设置=%s）", base, model, open_settings)
        try:
            self.app.on_settings_changed()
        except Exception:  # pragma: no cover - 刷新服务失败不能挡住关窗
            log.warning("向导完成后刷新服务失败", exc_info=True)
        self.destroy()
        if open_settings:
            try:
                self.app.open_settings()
            except Exception:  # pragma: no cover
                log.warning("向导无法打开设置页", exc_info=True)

    def destroy(self) -> None:
        try:
            self.win.destroy()
        except tk.TclError:  # pragma: no cover - 已经关了
            pass

    def _on_close(self) -> None:
        """「×」/ Esc：等同「先随便看看」（看过就不再弹）。"""
        self.finish(open_settings=False)

    def _center(self, master) -> None:
        try:
            x = master.winfo_rootx() + max(0, (master.winfo_width() - self.win.winfo_width()) // 2)
            y = master.winfo_rooty() + theme.px(80)
            self.win.geometry(f"+{x}+{y}")
        except (tk.TclError, AttributeError):
            # 无窗口测试里的假 master 没有 winfo_rootx：干脆不摆位置，
            # 但绝不吞掉窗口构造本身（与 SettingsDialog._center 同样的写法）。
            pass
