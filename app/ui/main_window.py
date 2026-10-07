"""主界面（词典管理窗口）：主题列表、词条列表、词条详情。

风格
----
与阅读面板共用编辑杂志风 palette（见 :mod:`app.ui.theme`）：暖米色背景、柔黑文字、
细线方角、**全部无衬线（Windows 上首选 Microsoft YaHei UI）**、**没有任何彩色强调**
（ttk 的原生蓝灰与圆角会造成风格漂移，因此这里一律用 :mod:`app.ui.widgets` 里的
扁平控件）。主界面与浮窗的字体族 / 字号规则因此完全一致，不再混排衬线标题。

默认状态
--------
主窗口**默认 withdraw**（``App`` 启动后立刻收起）：阅读时屏幕上只留 44x44 的
小方块。菜单栏（打开词典 / 设置 / 暂停取词 / 游戏模式 / 退出）与工具条上的按钮
都能在需要时把它叫回来；**「退出」只在主菜单**（``app.ui.app_menu``），工具条不再放，
最小 860 宽时右侧按钮不会被挤掉。底部另有一条**独立短状态行**（≤ 40 字），
不占工具条宽度。关闭主窗口的「×」只是**收起窗口**，程序继续在后台运行。

工具条绑定（都是**真实 App 方法**）
----------------------------------
搜索 / 清空 / 设置 / 导图 / 手动录入 / 剪贴板导入 / 暂停取词 / 游戏模式，
逐项在 :meth:`MainWindow._build_toolbar` 与 :meth:`MainWindow._build_body` 里直接
绑定 ``App`` 的公开方法；面板顶栏不再有「菜单」（浮窗是 NOACTIVATE 窗口，
抓取式菜单拿不到焦点），主界面按钮与主菜单才是这些入口的归属。

主题（左侧列表）
----------------
* 分组**完全自动**：每个阅读页面按 ``doc_key`` 各自成组，标题默认取该页首个关键词；
* 左栏只做三件事：**看**（点一下浏览这个词表）、**重命名 / 删除**、**合并 / 拆分**；
* 点选主题**只影响浏览**（``_browse_batch_id``，纯内存），绝不改变新词的保存位置；
* 页面切换时列表自动跟着新页面走（``follow_current_page``）。

标签、搜索、导出（B 批）
------------------------
* **标签**是轻量的（一个词条几个标签、跨主题筛选），不做笔记 / 层级 / 颜色；
  左栏下方是标签清单，点一下 = 只留打了这个标签的词（全库范围）；
* 搜索命中会**指出命中的字段与片段**（``app.search_service``），不再只给一个词；
* 卡片左侧的勾选框用于**拆分主题**（把选中的词搬进新主题 / 另一个主题）；
* 「导出」先弹一个**格式选择器**（``app.ui.export_dialog``），八种格式可选：
  CSV / Markdown / JSON / JSONL / Anki 导入文本 / PDF / HTML 单页 / 纯文本 TXT
  （内容生成在 ``app.export_service``，PDF 见 ``app.pdf_writer``）；存到哪由
  「设置 → 导出」里的保存位置决定，留空就是 ``data/exports/``（冻结运行时
  没有「另存为」对话框，所以是设置里写死一个目录）。导图那边不受影响，
  仍然出 SVG + PNG。

文案
----
面向用户的部分只说人话，不出现「批次 / 归属 / 固定 / 跨天复用」这类内部说法，
也不显示模型配置、识别方式、进程与内部服务状态（那些只写日志）。
"""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import messagebox, simpledialog

from .. import export_service, search_service
from ..db import normalize_tags
from ..logging_setup import get_logger
from . import theme, widgets
from .batch_dialogs import MergeBatchesDialog, MoveEntriesDialog
from .export_dialog import ExportDialog

log = get_logger("ui")

STATUS_LABEL = {"none": "未解释", "ok": "已解释", "error": "解释失败",
                "pending": "解释中…", "stale": "需重新解释"}
#: 详情右栏「什么都没选中」时的提示语（初始状态与删除后的复位用同一句）
EMPTY_DETAIL_HINT = "在左侧选择一条词条"
MAX_CARDS = 300
#: 底部状态行最多显示多少字（超出省略）：状态行只放一句短状态，从不横向挤工具条
STATUS_MAX_CHARS = 40

# ---- 悬浮说明与搜索框占位（P0-4）-----------------------------------------
#: 搜索框空着且没聚焦时显示的那句提示。**只说人话**：讲清「能搜什么」，
#: 不讲内部字段名（``one_line`` / ``source_title``）。
SEARCH_PLACEHOLDER = "搜词语 / 上下文 / 来源"
#: 搜索框自己的悬浮说明（占位文字太短，装不下「全库」与命中说明）
SEARCH_TOOLTIP = ("搜索全部词条：词语、上下文、释义、来源、例子、标签都算命中；\n"
                  "有搜索词时按整库找，不再局限当前主题")
#: 卡片勾选框的悬浮说明（K1）：勾选**同时**决定「哪些词进参考关系图」与
#: 「拆分时搬走哪些词」。以前只服务后者，用户看不懂这个方框是干什么的。
CHECK_TOOLTIP = "勾上这个词：它才会进参考关系图；「拆分…」也只搬勾上的词"
#: 勾选状态那一行自己的说明（点它可选全部，所以要说清它能点）
SEL_HINT_TOOLTIP = "这一组里有多少词已经勾上（点这一行 = 全选）"

#: 每个按钮的悬浮说明（一句话说清「点了会怎样」）。
#:
#: 为什么集中在模块级而不是散在 ``_build_*`` 里：这些句子是**面向用户的口径**，
#: 集中一处才便于统一改口、也便于测试逐条读。键 = 按钮文案（与
#: ``tests/test_reading_panel.py`` 里那份「按钮 ↔ 回调」审计表用的是同一套文案）。
BUTTON_TOOLTIPS: dict[str, str] = {
    # 工具条
    "搜索": "按输入的内容筛出词条（回车同样生效）",
    "清空": "清掉搜索词，回到当前主题的全部词条",
    "导出": "选一种格式导出当前看到的词表：CSV / Markdown / JSON / JSONL / "
            "Anki / PDF / HTML / TXT（落到「设置 → 导出」里的保存位置，不发网络请求）",
    "导图": "把**勾选**的词画成参考关系图（图里的生成要单独点，会消耗额度）",
    "设置": "接口、取词、解释与外观设置",
    "游戏模式：关": "游戏模式：开会暂停取词并把浮窗压下去，全屏游戏时不再打扰",
    "暂停取词": "临时不记录新划的词（已存的词条不受影响）",
    "退出": "退出探索词典（主窗的 × 只是收起，后台继续取词）",
    # 主题区
    "重命名": "给当前选中的主题改个名字",
    "删除": "删除当前主题（里面的词条会一并处理，删前会再问一次）",
    "合并…": "把选中的另一个主题并进当前主题",
    "拆分…": "把勾选的词条搬进新主题 / 另一个主题（要拆的词在中栏勾选）",
    # 标签区
    "标签改名": "改掉这个标签在所有词条上的名字",
    "标签删除": "删掉这个标签（词条本身留着，只是不再挂这个标签）",
    "全部": "取消标签筛选，回到全部词条",
    # 词条列表
    "手动录入": "手动新建一条词条（不用划词）",
    "剪贴板导入": "把剪贴板里的文字当成一次划词收进来",
    "全选": "勾上当前这一组里的全部词条（包括屏幕外没画出来的）",
    "全不选": "全部取消勾选：导图会没有词可画，拆分也没有可搬的词",
    # 详情右栏
    "保存修改": "把右栏改过的词语 / 上下文 / 来源 / 标签保存回库里",
    "解释": "让模型解释这条词在上下文里的意思（会消耗额度）",
    "重试": "上一次解释失败了，再试一次（会消耗额度）",
    "重新解释": "重新问一次模型（上下文改过时用这个，会消耗额度）",
    "删除词条": "删除这一条词条（会再问一次）",
    "重试注册": "全局热键没注册上（被别的程序占用了），点这里再试一次",
}


class ScrollFrame(tk.Frame):
    """一个可垂直滚动的容器（细线滚动条、无立体边框）。"""

    def __init__(self, master, bg=theme.BG):
        super().__init__(master, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.vsb = widgets.thin_scrollbar(self, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        for w in (self.canvas, self.inner):
            w.bind("<MouseWheel>", self._on_wheel)

    def _on_inner(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, event):
        self.canvas.itemconfigure(self._window, width=event.width)

    def _on_wheel(self, event):
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def bind_wheel(self, widget) -> None:
        widget.bind("<MouseWheel>", self._on_wheel)

    def clear(self) -> None:
        for child in self.inner.winfo_children():
            child.destroy()


def _entry(parent, textvariable=None, **kw) -> tk.Entry:
    """底线输入框（editorial：细线、无立体边框、聚焦只加深字色与线色）。"""
    kw.setdefault("font", theme.font(9))
    return tk.Entry(
        parent, textvariable=textvariable, relief="flat", bd=0,
        highlightthickness=1, highlightbackground=theme.BORDER,
        highlightcolor=theme.TEXT, bg=theme.PANEL, fg=theme.TEXT,
        insertbackground=theme.TEXT, **kw,
    )


def _small_button(parent, text: str, command, *, side: str = "left"):
    """左栏那种小号功能键（主题区 / 标签区两排共用），**顺带挂上悬浮说明**。

    两排按钮各自一行文案、名字又都只有两三个字（「合并…」合的是主题还是词条？），
    所以统一走这个工厂：建立 + 排版 + 说明一次做完，新增按钮时不会漏挂说明。
    """
    btn = widgets.FlatButton(parent, text, command, font_size=8, padx=6, pady=2)
    btn.pack(side=side, padx=theme.px(2))
    hint = BUTTON_TOOLTIPS.get(text)
    if hint:
        widgets.tooltip(btn, hint)
    return btn


class MainWindow:
    def __init__(self, root: tk.Tk, app):
        self.root = root
        self.app = app
        self.db = app.db
        self.cfg = app.config
        self._selected_entry_id: int | None = None
        self._cards: dict[int, tk.Frame] = {}
        self._alerts: list[tuple[str, str]] = []
        self._gate_text = ""
        self._batch_rows: list = []
        self._entry_rows: list = []
        #: 用户点选的主题（**纯内存的浏览筛选**）：只决定「列表里显示哪一组词」，
        #: 绝不参与新词的保存位置（保存目标永远由页面自动归属决定）。
        self._browse_batch_id: int | None = None
        #: 页面切换时置位：主窗口收起期间只记标记，打开时才真正重建列表
        self._follow_pending = False
        #: 标签筛选（B1）：**跨主题**的轻量筛选，非空时列表改呈全库中打了该标签的词
        self._tag_filter = ""
        self._tag_rows: list = []
        #: 卡片上勾选的词条（B2 拆分 / K1 导图用）：只存 id，刷新列表时保留 ——
        #: 用户勾的就是这些词，切搜索词、滚动、刷新都不该把他的勾选抖掉。
        self._checked: set[int] = set()
        #: 卡片勾选框的 BooleanVar（刷新时随卡片一起重建）
        self._card_vars: dict[int, object] = {}
        #: 当前这一组里的**全部**词条 id（顺序 = 列表顺序）。它按库算，不是按
        #: 画出来的卡片算 —— 中栏只画 ``MAX_CARDS`` 张，拿卡片算范围会让「全选」
        #: 悄悄漏掉没画出来的词，而用户以为整个主题都进图了（K1 的默认全勾同理）。
        self._checked_scope: list[int] = []
        #: 上一次做「默认全勾」判断时的**视野键** ``(batch_id, show_all)``。
        #: 只认视野：搜索词 / 标签筛选变化**不重置**勾选（用户正在挑词，不能抖）。
        self._checked_scope_key: tuple | None = None

        root.title("探索词典")
        root.configure(bg=theme.BG)
        root.geometry(f"{theme.px(1080)}x{theme.px(680)}")
        root.minsize(theme.px(860), theme.px(520))
        #: 自绘无框 chrome（**没有**原生标题栏 / 边框 / 菜单栏）：标题、拖动、
        #: 关闭、右下角缩放都在这里；「×」= 只收起主界面、后台继续运行。
        self.chrome = widgets.BorderlessChrome(
            root, title="探索词典", on_close=self.app.on_main_close,
            resizable=True, min_w=860, min_h=520, bg=theme.PANEL,
            on_minimize=lambda: widgets.minimize_window(root),
            on_maximize=lambda: widgets.toggle_maximize_window(root),
        )

        self._build_alerts(root)
        self._build_toolbar(root)
        self._build_status_row(root)
        self._build_body(root)
        self.refresh_batches()
        self.refresh_tags()
        self.refresh_entries()
        self.update_status()

    # ------------------------------------------------------------- 提醒条
    def _build_alerts(self, root) -> None:
        """顶部提醒条：真实的失败与需要用户处理的暂停在这里明说（单色，不靠颜色）。"""
        self.alert_bar = tk.Frame(root, bg=theme.PANEL_ALT, highlightthickness=1,
                                  highlightbackground=theme.BORDER_STRONG)
        self.alert_label = tk.Label(
            self.alert_bar, text="", bg=theme.PANEL_ALT, fg=theme.TEXT,
            font=theme.font(9), justify="left", anchor="w", wraplength=theme.px(880),
        )
        self.alert_label.pack(side="left", fill="x", expand=True,
                              padx=theme.px(10), pady=theme.px(5))
        self.alert_btn = widgets.FlatButton(self.alert_bar, "重试注册",
                                            self.app.retry_hotkeys, font_size=8,
                                            padx=10, pady=3)
        self.alert_btn.pack(side="right", padx=theme.px(8), pady=theme.px(4))
        widgets.tooltip(self.alert_btn, BUTTON_TOOLTIPS["重试注册"])

    def _render_alerts(self) -> None:
        if not self._alerts:
            self.alert_bar.pack_forget()
            return
        text = "\n".join(t for _, t in self._alerts)
        self.alert_label.configure(text=text)
        show_retry = any("重试注册" in t for _, t in self._alerts)
        if show_retry:
            self.alert_btn.pack(side="right", padx=theme.px(8), pady=theme.px(4))
        else:
            self.alert_btn.pack_forget()
        self.alert_bar.pack(fill="x", side="top", before=self.toolbar)

    def on_gate_changed(self, decision) -> None:
        self._gate_text = decision.status_text() if decision else ""
        self.update_status()

    def refresh_alerts(self, decision) -> None:
        alerts: list[tuple[str, str]] = []
        hotkey_warning = self.app.hotkey_warning()
        if hotkey_warning:
            alerts.append(("err", hotkey_warning))
        if decision is not None and not decision.allowed:
            if decision.blocked_by_user:
                alerts.append(("warn", f"⏸ {decision.label}。"
                                       "按 Ctrl+Alt+Shift+P / G 可恢复取词。"))
            else:
                alerts.append(("warn", f"⏸ {decision.label}。当前前台不取词，"
                                       "切回阅读窗口后自动恢复。"))
        self._alerts = alerts
        self._render_alerts()

    # ------------------------------------------------------------- 工具条
    def _build_toolbar(self, root) -> None:
        bar = tk.Frame(root, bg=theme.PANEL, height=theme.px(46))
        self.toolbar = bar
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)
        widgets.hairline(root).pack(fill="x", side="top")

        tk.Label(bar, text="探索词典", bg=theme.PANEL, fg=theme.TEXT,
                 font=theme.font(13)).pack(side="left",
                                           padx=(theme.px(14), theme.px(12)))

        self.search_var = tk.StringVar()
        entry = _entry(bar, textvariable=self.search_var, width=20)
        #: 搜索输入框本身（占位提示挂在它上面；测试也按这个名字找它）
        self.search_entry = entry
        entry.pack(side="left", pady=theme.px(10), ipady=theme.px(3))
        entry.bind("<Return>", lambda e: self.refresh_entries())
        entry.bind("<KeyRelease>", self._on_search_key)
        # 占位提示（只动**显示文本**，绝不写进 search_var，否则真会拿提示去查库）
        widgets.placeholder(entry, SEARCH_PLACEHOLDER, variable=self.search_var)
        widgets.tooltip(entry, SEARCH_TOOLTIP)

        #: 工具条上每个按钮的说明（P0-4）：这些入口的名字都很短，「导出」导到哪、
        #: 「导图」点了到底是出图还是要花钱，光看字看不出来 —— 存进列表是为了
        #: 建完统一挂悬浮说明，新增按钮时**不会漏**（列表里没登记的文案测试会报）。
        toolbar_buttons: list[tk.Widget] = []

        def _toolbar_button(text: str, command) -> tk.Widget:
            btn = widgets.FlatButton(bar, text, command, font_size=8, padx=10, pady=3)
            toolbar_buttons.append(btn)
            return btn

        _toolbar_button("搜索", self.refresh_entries).pack(side="left",
                                                           padx=theme.px(5))
        _toolbar_button("清空", self._clear_search).pack(side="left")

        # 「全部词语」复选框已按用户要求删除：检索由**主题选择 + 全库搜索**完成。
        # 这里保留 ``scope_var`` 只是兼容 ``entry_scope`` 的既有判据，值恒为 False
        # —— 它不再有界面入口，也就不能被历史配置重新点亮（否则侧栏点主题会失效）。
        self.scope_var = tk.BooleanVar(value=False)

        # ---- 右侧动作组（右→左依次 pack）：**退出 / 设置 / 导图 / 游戏 / 暂停** ----
        # 全部功能键**每组只留一份**：旧版工具条与原生菜单栏各有一份设置 / 暂停 /
        # 游戏，正是用户截图里「同一页重复功能键」的问题。原生菜单栏已删除，
        # 这里就是唯一的入口组；「退出」全程序只有这一个清晰入口（主窗「×」只是
        # 收起、后台继续）。
        _toolbar_button("退出", self.app.quit).pack(
            side="right", padx=(theme.px(10), theme.px(4)))
        _toolbar_button("设置", self.app.open_settings).pack(side="right",
                                                             padx=theme.px(4))
        _toolbar_button("导图", self.app.open_concept_map).pack(side="right",
                                                                padx=theme.px(4))
        #: 导出（B3）：把**当前看到的**词表写成 CSV + Markdown，落在 data/exports/。
        #: 冻结运行时没有「另存为」对话框，所以按钮只负责写文件 + 报路径。
        _toolbar_button("导出", self.export_entries).pack(side="right",
                                                         padx=theme.px(4))
        self.game_btn = _toolbar_button("游戏模式：关", self.app.toggle_game_mode)
        self.game_btn.pack(side="right", padx=theme.px(4))
        self.pause_btn = _toolbar_button("暂停取词", self.app.toggle_capture)
        self.pause_btn.pack(side="right", padx=theme.px(4))

        # 工具条这一排统一挂悬浮说明（P0-4）：入口名字都很短，「导出」导到哪、
        # 「导图」点了到底是出图还是要花钱，光看字看不出来。文案统一在
        # :data:`BUTTON_TOOLTIPS`（测试会逐条核对**每个按钮都有说明**）。
        for btn in toolbar_buttons:
            hint = BUTTON_TOOLTIPS.get(str(btn.cget("text")))
            if hint:
                widgets.tooltip(btn, hint)

        # 「全部词语」与「置顶」开关已删除：检索由**主题选择 + 全库搜索**完成，
        # 浮窗在普通阅读下自动置顶、主窗口保持普通层级（游戏 / 全屏硬阻断照旧）。
        # ``scope_var`` 只保留成内部状态（配置项 ``ui.batch_scope_all`` 仍在读），
        # 界面上不再出现第二个入口。
        self.topmost_check = None

    # ------------------------------------------------------- 底部短状态行
    def _build_status_row(self, root) -> None:
        """主界面**底部独立短状态行**（≤ ``STATUS_MAX_CHARS`` 字）。

        状态文案不参与工具条的横向分配：窗口缩到最小 860 宽时，它不会把搜索 /
        暂停 / 游戏挤掉；一行短句也永远不会自己换行撑高界面。
        """
        row = tk.Frame(root, bg=theme.PANEL)
        self.status_row = row
        row.pack(fill="x", side="bottom")
        widgets.hairline(root).pack(fill="x", side="bottom")   # 状态行上方的细线
        self.status_label = tk.Label(row, text="", bg=theme.PANEL, fg=theme.TEXT_MUTED,
                                     font=theme.font(8), anchor="w")
        self.status_label.pack(side="left", fill="x", expand=True,
                               padx=theme.px(12), pady=theme.px(3))

    def _on_topmost_toggle(self) -> None:
        """兼容旧调用点：界面上**没有**置顶开关，这里只是把内部偏好缓存对齐配置。

        浮窗在普通阅读下自动置顶、主窗口始终普通层级（游戏 / 全屏硬阻断照旧），
        因此这个方法不再改变任何窗口层级，也不会被任何可见控件调用。
        """
        self.sync_topmost_check()

    def sync_topmost_check(self) -> None:
        """把内部「旧置顶偏好」缓存同步成配置值（界面无开关 → 没有可见控件）。"""
        want = bool(self.cfg.topmost)
        var = getattr(self, "topmost_var", None)
        if var is None:
            return
        try:
            if bool(var.get()) != want:
                var.set(want)
        except tk.TclError:  # pragma: no cover
            pass

    # ------------------------------------------------------------- 主体
    def _build_body(self, root) -> None:
        body = tk.Frame(root, bg=theme.BG)
        body.pack(fill="both", expand=True)

        # ---- 主题（左）----
        left = tk.Frame(body, bg=theme.BG, width=theme.px(220))
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        tk.Label(left, text="主题", bg=theme.BG, fg=theme.TEXT_MUTED,
                 font=theme.tracking_label(7)).pack(anchor="w", padx=theme.px(12),
                                                    pady=(theme.px(10), theme.px(4)))
        self.batch_list = tk.Listbox(
            left, font=theme.font(9), bg=theme.PANEL, fg=theme.TEXT,
            highlightthickness=1, highlightbackground=theme.BORDER, bd=0,
            activestyle="none", exportselection=False, selectmode="browse",
            selectbackground=theme.ACCENT, selectforeground=theme.BG,
        )
        self.batch_list.pack(fill="both", expand=True, padx=theme.px(12))
        self.batch_list.bind("<<ListboxSelect>>", self._on_batch_select)

        # 只保留「重命名 / 删除」：分组是全自动的，没有新建、没有固定。
        bb = tk.Frame(left, bg=theme.BG)
        bb.pack(fill="x", padx=theme.px(10), pady=theme.px(8))
        for text, cmd in (("重命名", self.rename_batch), ("删除", self.delete_batch),
                          ("合并…", self.merge_batches_dialog),
                          ("拆分…", self.split_entries_dialog)):
            _small_button(bb, text, cmd)

        # ---- 标签（左下，B1）----
        # 标签是**跨主题**的：点一下就把全库里打了这个标签的词聚到一起看，
        # 这正是「同一术语分散在不同文档里」的用法。
        widgets.hairline(left).pack(fill="x", pady=(theme.px(2), 0))
        self.tag_head = tk.Label(left, text="标签", bg=theme.BG, fg=theme.TEXT_MUTED,
                                 font=theme.tracking_label(7))
        self.tag_head.pack(anchor="w", padx=theme.px(12),
                           pady=(theme.px(8), theme.px(4)))
        self.tag_list = tk.Listbox(
            left, font=theme.font(8), bg=theme.PANEL, fg=theme.TEXT, height=6,
            highlightthickness=1, highlightbackground=theme.BORDER, bd=0,
            activestyle="none", exportselection=False, selectmode="browse",
            selectbackground=theme.ACCENT, selectforeground=theme.BG,
        )
        self.tag_list.pack(fill="both", expand=True, padx=theme.px(12))
        self.tag_list.bind("<<ListboxSelect>>", self._on_tag_select)
        tb = tk.Frame(left, bg=theme.BG)
        tb.pack(fill="x", padx=theme.px(10), pady=theme.px(8))
        for text, cmd in (("标签改名", self.rename_tag), ("标签删除", self.delete_tag),
                          ("全部", self.clear_tag_filter)):
            _small_button(tb, text, cmd)

        # ---- 词条列表（中）----
        center = tk.Frame(body, bg=theme.BG)
        center.pack(side="left", fill="both", expand=True)
        head = tk.Frame(center, bg=theme.BG)
        head.pack(fill="x", padx=theme.px(10), pady=(theme.px(10), theme.px(4)))
        self.count_label = tk.Label(head, text="0 条", bg=theme.BG, fg=theme.TEXT_MUTED,
                                    font=theme.font(8))
        self.count_label.pack(side="left")
        #: 勾选状态（B2 / K1）：显示「已勾选 N / M 条」。**点它可选全部** ——
        #: 全不选之后它就是屏幕上唯一还提这件事的地方（留着词还有用的人要能一键找回）。
        self.sel_hint = tk.Label(head, text="", bg=theme.BG, fg=theme.TEXT_MUTED,
                                 font=theme.font(8), cursor="hand2")
        self.sel_hint.pack(side="left", padx=theme.px(8))
        self.sel_hint.bind("<Button-1>", self._on_sel_hint_click)
        widgets.tooltip(self.sel_hint, SEL_HINT_TOOLTIP)
        #: 全选 / 全不选（K1）：一次改一整组（含屏幕外没画出来的词条）
        _small_button(head, "全选", lambda: self._set_all_checked(True))
        _small_button(head, "全不选", lambda: self._set_all_checked(False))
        for text, cmd in (("手动录入", self.app.open_manual_dialog),
                          ("剪贴板导入", self.app.import_clipboard)):
            btn = widgets.FlatButton(head, text, cmd, font_size=8, padx=10, pady=3)
            btn.pack(side="right", padx=theme.px(3))
            widgets.tooltip(btn, BUTTON_TOOLTIPS[text])

        self.cards = ScrollFrame(center, bg=theme.BG)
        self.cards.pack(fill="both", expand=True, padx=theme.px(10), pady=(0, theme.px(10)))

        # ---- 详情（右）----
        right = tk.Frame(body, bg=theme.PANEL, width=theme.px(340))
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        widgets.hairline(body).pack(side="right", fill="y")
        self._build_detail(right)

    def _build_detail(self, parent) -> None:
        tk.Label(parent, text="词条详情", bg=theme.PANEL, fg=theme.TEXT_MUTED,
                 font=theme.tracking_label(7)).pack(anchor="w", padx=theme.px(12),
                                                    pady=(theme.px(10), theme.px(2)))
        self.detail_hint = tk.Label(parent, text=EMPTY_DETAIL_HINT, bg=theme.PANEL,
                                    fg=theme.TEXT_FAINT, font=theme.font(7))
        self.detail_hint.pack(anchor="w", padx=theme.px(12))

        box = tk.Frame(parent, bg=theme.PANEL)
        box.pack(fill="both", expand=True, padx=theme.px(12), pady=theme.px(6))
        box.columnconfigure(0, weight=1)
        box.rowconfigure(3, weight=1)
        box.rowconfigure(11, weight=2)

        tk.Label(box, text="词语", bg=theme.PANEL, fg=theme.TEXT_FAINT,
                 font=theme.font(7)).grid(row=0, column=0, sticky="w", pady=(theme.px(4), 0))
        self.var_term = tk.StringVar()
        tk.Entry(box, textvariable=self.var_term, font=theme.font(11), relief="flat",
                 bd=0, highlightthickness=1, highlightbackground=theme.BORDER,
                 highlightcolor=theme.TEXT, bg=theme.PANEL, fg=theme.TEXT,
                 insertbackground=theme.TEXT).grid(row=1, column=0, sticky="we",
                                                   ipady=theme.px(2))

        tk.Label(box, text="上下文", bg=theme.PANEL, fg=theme.TEXT_FAINT,
                 font=theme.font(7)).grid(row=2, column=0, sticky="nw", pady=(theme.px(6), 0))
        self.txt_context = tk.Text(box, height=4, font=theme.font(8), wrap="word",
                                   relief="flat", bd=0, highlightthickness=1,
                                   highlightbackground=theme.BORDER, highlightcolor=theme.TEXT,
                                   bg=theme.PANEL, fg=theme.TEXT_BODY)
        self.txt_context.grid(row=3, column=0, sticky="nwe")

        tk.Label(box, text="来源标题", bg=theme.PANEL, fg=theme.TEXT_FAINT,
                 font=theme.font(7)).grid(row=4, column=0, sticky="w", pady=(theme.px(6), 0))
        self.var_src_title = tk.StringVar()
        _entry(box, textvariable=self.var_src_title).grid(row=5, column=0, sticky="we",
                                                          ipady=theme.px(2))

        tk.Label(box, text="来源链接（可留空）",
                 bg=theme.PANEL, fg=theme.TEXT_FAINT, font=theme.font(7)).grid(
            row=6, column=0, sticky="w", pady=(theme.px(6), 0))
        self.var_src_url = tk.StringVar()
        _entry(box, textvariable=self.var_src_url).grid(row=7, column=0, sticky="we",
                                                        ipady=theme.px(2))

        # ---- 标签（B1）：轻量、可跨主题筛选 ----
        tk.Label(box, text="标签（逗号分隔，可留空）", bg=theme.PANEL, fg=theme.TEXT_FAINT,
                 font=theme.font(7)).grid(row=8, column=0, sticky="w",
                                          pady=(theme.px(6), 0))
        self.var_tags = tk.StringVar()
        _entry(box, textvariable=self.var_tags).grid(row=9, column=0, sticky="we",
                                                     ipady=theme.px(2))
        #: 「库里已有的标签」快捷按钮：点一下追加到输入框（省得手打错字）
        self.tag_chips = tk.Frame(box, bg=theme.PANEL)
        self.tag_chips.grid(row=10, column=0, sticky="we", pady=(theme.px(4), 0))

        self.var_meta = tk.StringVar()
        tk.Label(box, textvariable=self.var_meta, bg=theme.PANEL, fg=theme.TEXT_FAINT,
                 font=theme.font(7), justify="left", anchor="w",
                 wraplength=theme.px(300)).grid(row=11, column=0, sticky="we",
                                                pady=(theme.px(6), theme.px(2)))

        expo = tk.Frame(box, bg=theme.PANEL, highlightthickness=1,
                        highlightbackground=theme.BORDER)
        expo.grid(row=12, column=0, sticky="nsew", pady=(theme.px(4), 0))
        tk.Label(expo, text="释义", bg=theme.PANEL,
                 fg=theme.TEXT_FAINT, font=theme.font(7)).pack(anchor="w",
                                                               padx=theme.px(8),
                                                               pady=(theme.px(6), 0))
        self.exp_text = tk.Text(expo, height=10, font=theme.font(8), wrap="word",
                                bg=theme.PANEL, fg=theme.TEXT_BODY, relief="flat", bd=0,
                                highlightthickness=0, state="disabled")
        self.exp_text.pack(fill="both", expand=True, padx=theme.px(8),
                           pady=(0, theme.px(6)))

        btns = tk.Frame(parent, bg=theme.PANEL)
        btns.pack(fill="x", padx=theme.px(12), pady=theme.px(10))
        btn_save = widgets.FlatButton(btns, "保存修改", self.save_detail, font_size=8,
                                      padx=10, pady=3)
        btn_save.pack(side="left", padx=(0, theme.px(4)))
        widgets.tooltip(btn_save, BUTTON_TOOLTIPS["保存修改"])
        #: 解释入口**只有一个**（用户明确要求：同一页不出现两个功能键）。
        #: 文案随状态变：未解释 → 「解释」；失败 / 过期 → 「重试」；已解释 →「重新解释」。
        self.btn_explain = widgets.FlatButton(btns, self.EXPLAIN_LABEL_NEW,
                                              self._on_explain_button, font_size=8,
                                              padx=10, pady=3)
        self.btn_explain.pack(side="left", padx=theme.px(3))
        # 说明跟着按钮文案走：这个按钮会在三种文案之间切换，硬写死一句就会与当前
        # 状态对不上，所以传 callable、每次弹出重新取（见 widgets.Tooltip.text）。
        widgets.tooltip(self.btn_explain,
                        lambda: BUTTON_TOOLTIPS.get(self.explain_button_label(),
                                                    BUTTON_TOOLTIPS["解释"]))
        btn_del = widgets.FlatButton(btns, "删除词条", self.delete_selected, font_size=8,
                                     padx=10, pady=3)
        btn_del.pack(side="right")
        widgets.tooltip(btn_del, BUTTON_TOOLTIPS["删除词条"])

    #: 详情页唯一解释入口的三种文案（按状态切换，绝不并排两个按钮）
    EXPLAIN_LABEL_NEW = "解释"
    EXPLAIN_LABEL_RETRY = "重试"
    EXPLAIN_LABEL_AGAIN = "重新解释"

    def explain_button_label(self) -> str:
        """当前状态该显示哪个解释入口（未解释 / 失败 / 已解释）。"""
        entry_id = self._selected_entry_id
        row = self.db.get_entry(entry_id) if entry_id else None
        status = str(row["explain_status"] or "none") if row is not None else "none"
        if status == "ok":
            return self.EXPLAIN_LABEL_AGAIN
        if status in ("error", "stale"):
            return self.EXPLAIN_LABEL_RETRY
        return self.EXPLAIN_LABEL_NEW

    def update_explain_button(self) -> bool:
        """按状态刷新唯一解释按钮的文案（幂等；没有选中词条时保持默认文案）。"""
        label = self.explain_button_label()
        button = getattr(self, "btn_explain", None)
        if button is None:
            return False
        try:
            if str(button.cget("text")) == label:
                return False
            button.configure(text=label)
            return True
        except tk.TclError:  # pragma: no cover
            return False

    def _on_explain_button(self) -> None:
        """唯一解释入口：执行有区别，但**只有一个按钮**。

        未解释 / 失败 → 正常解释（失败时走同一入口重试，不重复记词、不加词频）；
        已解释 → 强制重新解释（跳过缓存）。
        """
        label = self.explain_button_label()
        self.app.explain_selected(label == self.EXPLAIN_LABEL_AGAIN)

    # ------------------------------------------------------------- 主题
    def browse_batch_id(self) -> int | None:
        """当前用来**筛选列表**的主题 id（``None`` = 跟随当前阅读页面）。"""
        return self._browse_batch_id

    def page_scope(self) -> tuple[int | None, bool]:
        """当前阅读页面的范围（只读）：``(batch_id, page_known)``。

        与阅读面板用**同一个**判据（``CaptureService.current_page_scope``）：
        页面已知但还没保存过词时返回 ``(None, True)`` —— 列表必须是空的，
        不能拿全库顶上。
        """
        from ..capture_service import resolve_page_scope

        return resolve_page_scope(getattr(self.app, "capture_service", None))

    def entry_scope(self) -> tuple[int | None, bool]:
        """词条列表实际显示的范围：``(batch_id, show_all)``。

        * 搜索框里有内容 → ``(None, True)``：**全库搜索**（用户明确在找词，
          不该被「当前这一页」的范围藏起来）；
        * 用户点过某个主题 → 那个主题（纯内存浏览筛选）；
        * 否则跟随当前阅读页面：有主题就用它；**已知页面但还没有词** →
          ``(None, False)``（空列表）；完全不知道来源（例如面板探针）→ 全库。
        """
        if self.scope_var.get():
            return None, True
        if self.search_var.get().strip():
            return None, True
        if self._browse_batch_id is not None and self.db.get_batch(self._browse_batch_id):
            return int(self._browse_batch_id), False
        batch_id, page_known = self.page_scope()
        if batch_id is not None:
            return batch_id, False
        return None, not page_known

    def filter_batch_id(self) -> int | None:
        """列表实际使用的筛选 id（``None`` = 全库或「这一页还没有词」）。"""
        return self.entry_scope()[0]

    def refresh_batches(self) -> None:
        self._batch_rows = list(self.db.list_batches())
        self.batch_list.delete(0, tk.END)
        #: 标签筛选生效时列表是**全库**范围，此时高亮某个主题会误导用户
        selected = None if self._tag_filter else self.filter_batch_id()
        #: 只有真的在按某个主题筛选时才高亮它：跟随当前页面（``selected is None``）
        #: 时不高亮任何一行 —— 否则会误示「正在浏览某个主题」。
        sel_index: int | None = None
        for i, row in enumerate(self._batch_rows):
            self.batch_list.insert(tk.END, f"{row['name']}  ({row['entry_count']})")
            if selected is not None and int(row["id"]) == selected:
                sel_index = i
        if self._batch_rows and sel_index is not None:
            self.batch_list.selection_clear(0, tk.END)
            self.batch_list.selection_set(sel_index)
            self.batch_list.see(sel_index)

    def _selected_batch_id(self) -> int | None:
        sel = self.batch_list.curselection()
        if not sel or sel[0] >= len(self._batch_rows):
            return None
        return int(self._batch_rows[sel[0]]["id"])

    def _on_batch_select(self, _event=None) -> None:
        """点主题 = **只看**这一组词（纯内存筛选）。

        绝不调用 ``set_current_batch(explicit=True)``：手工浏览不能改变新词的
        保存位置 —— 保存目标永远由页面自动归属决定。
        """
        bid = self._selected_batch_id()
        self.set_browse_scope(bid)

    def set_browse_scope(self, batch_id: int | None) -> bool:
        """设置**浏览**主题（+ 刷新侧栏 / 列表），返回是否真的变了。

        浮窗顶栏的主题清单与这里共用同一份状态（``App.set_browse_scope``），
        因此两边永远同步；浏览范围**只影响显示**，不改变新词的保存位置。
        """
        bid = None if batch_id in (None, "") else int(batch_id)
        if bid is not None and self.db.get_batch(bid) is None:
            bid = None                       # 主题已被删除：回到跟随当前页
        self.scope_var.set(False)            # 「全部词语」入口已删除，浏览永远按主题
        changed = self._browse_batch_id != bid
        self._browse_batch_id = bid
        self._follow_pending = False
        if self._tag_filter:
            # 点主题 = 结束标签筛选（否则列表还是全库的标签结果，点了像没反应）
            self._tag_filter = ""
            self.refresh_tags()
        self.refresh_batches()
        self.refresh_entries()
        app = getattr(self, "app", None)
        notifier = getattr(app, "on_browse_scope_changed", None)
        if callable(notifier):
            notifier()
        return changed

    def follow_current_page(self) -> bool:
        """页面切换：清掉手工浏览筛选，列表自动改呈**新页面**的词。

        主窗口阅读时是收起的（屏幕上只留 44x44 小方块），所以这里默认**延迟**：
        先记下「回来后要跟到新页面」，真正重建列表等窗口被打开时
        （:meth:`apply_pending_follow`）再做 —— 浏览器里每切一个标签页就重建
        最多 300 张卡片纯属浪费，而且用户根本看不见。窗口此刻真的在屏幕上时
        立即执行。

        只动**浏览筛选**（纯内存）与列表显示：新词的保存位置永远由页面自动
        归属决定，绝不因为「跟着页面走」而改变。
        """
        self._browse_batch_id = None
        self._follow_pending = True
        if self._window_mapped():
            return self.apply_pending_follow()
        return False

    def release_browse_scope(self) -> bool:
        """把「浏览主题」交还「跟随当前阅读页」——**纯内存**，不重建任何控件。

        浮窗在前台换页时走这里（换页等于结束手工浏览，用户开始划选时的那次刷新
        就取新页面的范围）；主界面此刻多半收起着，它自己的列表仍然按
        :meth:`follow_current_page` 的延迟策略（下次打开时落地），这里只清共享的
        那一个浏览指针，绝不 ``refresh_batches`` / ``refresh_entries``、绝不重建卡片。
        """
        changed = self._browse_batch_id is not None
        self._browse_batch_id = None
        return changed

    def apply_pending_follow(self, *, force: bool = False) -> bool:
        """把「跟到当前页面」真正落地（没有待办时零开销，不查库不重建控件）。"""
        if not self._follow_pending and not force:
            return False
        self._follow_pending = False
        self._browse_batch_id = None
        self.refresh_batches()
        self.refresh_entries()
        return True

    def _window_mapped(self) -> bool:
        """主窗口此刻是否真的显示着（拿不到可见性时按「收起」处理 → 走延迟路径）。"""
        try:
            is_mapped = getattr(self.root, "winfo_ismapped", None)
            if callable(is_mapped):
                return bool(is_mapped())
            state = getattr(self.root, "state", None)
            if callable(state):
                return str(state()) != "withdrawn"
        except Exception:  # pragma: no cover - 窗口已销毁
            return False
        return False

    def rename_batch(self) -> None:
        """用户改名：写入 ``name_source='manual'``，此后自动命名永不覆盖它。"""
        bid = self._selected_batch_id()
        if bid is None:
            return
        row = self.db.get_batch(bid)
        name = simpledialog.askstring("修改名称", "名称：", parent=self.root,
                                      initialvalue=row["name"] if row else "")
        if not name or not name.strip():
            return
        self.db.rename_batch(bid, name.strip())
        self.refresh_batches()

    def delete_batch(self) -> None:
        bid = self._selected_batch_id()
        if bid is None:
            return
        row = self.db.get_batch(bid)
        if not messagebox.askyesno("删除主题",
                                   f"确定删除主题《{row['name'] if row else bid}》？\n"
                                   "该主题下的所有词语会一并删除，且不可恢复。",
                                   parent=self.root):
            return
        self.db.delete_batch(bid)
        if self._browse_batch_id == bid:
            self._browse_batch_id = None
        if self.app.capture_service.current_batch_id() == bid:
            self.app.capture_service.set_current_batch(None)
        self.refresh_batches()
        self.refresh_entries()

    # ------------------------------------------------------------- 标签（B1）
    def refresh_tags(self) -> None:
        """重建左栏标签清单（按名字排序，带使用条数）。"""
        self._tag_rows = list(self.db.list_tags())
        self.tag_list.delete(0, tk.END)
        for row in self._tag_rows:
            self.tag_list.insert(tk.END, f"{row['name']}  ({row['entry_count']})")
        self.tag_list.selection_clear(0, tk.END)
        if self._tag_filter:
            for i, row in enumerate(self._tag_rows):
                if str(row["name"]) == self._tag_filter:
                    self.tag_list.selection_set(i)
                    break

    def _on_tag_select(self, _event=None) -> None:
        sel = self.tag_list.curselection()
        if not sel or sel[0] >= len(self._tag_rows):
            return
        self.set_tag_filter(str(self._tag_rows[sel[0]]["name"]))

    def set_tag_filter(self, name: str) -> bool:
        """按标签筛选（``""`` = 取消筛选）。返回是否真的变了。"""
        text = " ".join(str(name or "").split()).strip()
        changed = text != self._tag_filter
        self._tag_filter = text
        self.refresh_batches()
        self.refresh_entries()
        return changed

    def clear_tag_filter(self) -> bool:
        return self.set_tag_filter("")

    def rename_tag(self) -> None:
        """标签改名；改成已有名字就是**并成一条**（同类标签合并不是删数据）。"""
        if not self._tag_filter:
            messagebox.showinfo("重命名标签", "先在左下角点一个标签。", parent=self.root)
            return
        row = next((r for r in self._tag_rows if str(r["name"]) == self._tag_filter), None)
        name = simpledialog.askstring(
            "重命名标签", f"标签《{self._tag_filter}》改名为：", parent=self.root,
            initialvalue=str(row["name"]) if row else self._tag_filter)
        if not name or not name.strip():
            return
        final = self.db.rename_tag(self._tag_filter, name)
        if final:
            self._tag_filter = final
        self.refresh_tags()
        self.refresh_entries()
        self.app.set_status(f"标签已改名为「{final}」" if final else "标签不存在")

    def delete_tag(self) -> None:
        """删标签 = **只解开标签**，词条本身一条都不会少。"""
        if not self._tag_filter:
            messagebox.showinfo("删除标签", "先在左下角点一个标签。", parent=self.root)
            return
        if not messagebox.askyesno(
                "删除标签",
                f"删除标签《{self._tag_filter}》？\n词条本身不会删除，只是不再带这个标签。",
                parent=self.root):
            return
        self.db.delete_tag(self._tag_filter)
        self._tag_filter = ""
        self.refresh_tags()
        self.refresh_entries()
        self.app.set_status("标签已删除（词条保留）")

    def apply_tags(self, names) -> list[str]:
        """把详情里的标签写进库（返回最终标签）。"""
        if not self._selected_entry_id:
            return []
        return self.db.set_entry_tags(self._selected_entry_id, names)

    # ---------------------------------------------------- 合并 / 拆分（B2）
    def checked_ids(self) -> list[int]:
        """当前勾选的词条 id（升序，便于测试与提示）。"""
        return sorted(self._checked)

    def checked_scope_ids(self) -> list[int]:
        """当前这一组里的全部词条 id（列表顺序，**不受卡片绘制上限影响**）。"""
        return list(self._checked_scope)

    def _checked_view_key(self) -> tuple[int | None, bool]:
        """「在浏览哪一组词」的键：``(batch_id, 看全库)``。

        与 :meth:`entry_scope` 的区别只有一个、但很关键：**搜索不算换组**。
        ``entry_scope()`` 一见搜索词就返回 ``(None, True)``（搜索走全库），拿它当键
        的话「按提示搜了一下」就会被当成换主题 → 把用户点掉的勾全补回来。所以这里
        明确用 :attr:`_browse_batch_id` 当「当前这一组」，标签筛选与搜索都只换范围、
        不换组（这条键里根本没有标签与搜索词）。
        """
        if self._tag_filter or self.search_var.get().strip():
            #: 跨主题筛选 / 全库搜索：组还是主界面正在浏览的那个主题（没有就全库）
            browse = self._browse_batch_id
            if browse is not None and self.db.get_batch(browse):
                return int(browse), False
            return None, True
        return self.entry_scope()

    def _refresh_checked_scope(self) -> None:
        """按当前视野重算「这一组有哪些词」，并在**换主题**时把它们默认全勾上。

        口径（用户 2026-10-05 确认）：新进一个主题 = 该主题的词条**默认全部勾选**
        ——「好多词都要，只有几个不要」时，取消几个比一条条勾快得多；切换主题
        重新全勾；搜索词 / 标签筛选变化只换范围，**不动**用户已经手动改过的勾选。
        """
        tag = self._tag_filter
        query = self.search_var.get().strip()
        #: 范围（查库）与「组」（要不要重置全勾）必须用**同一条键**算：
        #: 之前左边用 ``view_all``、右边重新调一次 ``entry_scope()``，
        #: 两者在「有搜索词且正在浏览某个主题」时并不相等（``(1, True)`` vs
        #: ``(1, False)``），于是每搜一次都被当成换主题、把勾全补回来。
        view_key = self._checked_view_key()
        batch_id = view_key[0]
        try:
            self._checked_scope = list(self.db.list_entry_ids(
                batch_id=batch_id, query=query, tag=tag))
        except Exception:  # pragma: no cover - 读库失败不该炸列表
            log.exception("读取勾选范围失败")
            self._checked_scope = []
        if view_key != self._checked_scope_key:
            #: 进一个主题 = 默认全勾（用户 2026-10-05 确认）。只在**视野键真的变了**
            #: 时重置：搜索 / 标签筛选换的是范围，不重置；「全不选」之后刷新也不许
            #: 自动勾回来（早先这里有一条 ``elif not self._checked`` 的兜底，正好
            #: 会把用户刚点掉的勾全部补回来，已删）。
            self._checked_scope_key = view_key
            self._checked = set(self._checked_scope)

    def _set_all_checked(self, checked: bool) -> None:
        """「全选 / 全不选」：整组一起改，屏幕外的词条也算在内。"""
        if checked:
            self._checked = set(self._checked_scope)
        else:
            self._checked.clear()
        self._sync_card_checks()
        self._update_sel_hint()

    def _sync_card_checks(self) -> None:
        """把可见卡片的方框拨到与 ``self._checked`` 一致（不能只改数据不改界面）。"""
        for eid, var in list(self._card_vars.items()):
            try:
                var.set(int(eid) in self._checked)
            except tk.TclError:  # pragma: no cover - 控件正在销毁
                continue

    def _toggle_checked(self, entry_id: int, var=None) -> None:
        try:
            checked = bool(var.get()) if var is not None else True
        except tk.TclError:  # pragma: no cover
            checked = False
        #: 手动改过 = 记住这个视野，别再自动「补全勾」（否则取消一条会立刻被勾回来）
        self._checked_scope_key = self._checked_view_key()
        if checked:
            self._checked.add(int(entry_id))
        else:
            self._checked.discard(int(entry_id))
        self._update_sel_hint()

    def _update_sel_hint(self) -> None:
        """中栏顶部那一行勾选状态：**点了会全选**，所以空着时也给一句话。"""
        count = len(self._checked)
        total = len(self._checked_scope)
        if not count:
            text = "点这里全选" if total else ""
        elif total and count >= total:
            text = f"本组 {total} 条已全勾"
        else:
            text = f"已勾选 {count} / {total} 条"
        hint = getattr(self, "sel_hint", None)
        if hint is None:
            return
        try:
            hint.configure(text=text)
        except tk.TclError:  # pragma: no cover
            pass

    def _on_sel_hint_click(self, _event=None) -> None:
        """点勾选状态那一行 = 全选（全不选之后它是屏幕上唯一还提这件事的地方）。"""
        self._set_all_checked(True)

    def _drop_missing_checks(self) -> None:
        """勾选的词条被删掉之后（别处删的也算）从勾选集合里清掉。"""
        if not self._checked:
            return
        alive = {int(r["id"]) for r in self._entry_rows}
        stale = {i for i in self._checked if self.db.get_entry(i) is None}
        if not stale:
            return
        self._checked -= stale
        for i in stale:
            var = self._card_vars.get(i)
            if var is not None:
                try:
                    var.set(False)
                except tk.TclError:  # pragma: no cover
                    pass
        self._update_sel_hint()
        _ = alive                                # 保留变量，便于将来只清「当前不可见」的勾选

    def clear_checked(self) -> None:
        """清空勾选（同时把可见卡片的勾去掉）。"""
        for eid, var in list(self._card_vars.items()):
            try:
                var.set(False)
            except tk.TclError:  # pragma: no cover
                pass
            _ = eid
        self._checked.clear()
        self._update_sel_hint()

    def merge_batches_dialog(self) -> None:
        """「合并…」：把多个主题并进一个（源主题消失）。"""
        rows = list(self.db.list_batches())
        if len(rows) < 2:
            messagebox.showinfo("合并主题", "现在只有一个主题，没有可合并的对象。",
                                parent=self.root)
            return
        target = self._selected_batch_id() or self.filter_batch_id() or int(rows[0]["id"])
        MergeBatchesDialog(self.root, self.app, rows, target, on_merge=self.apply_merge)

    def apply_merge(self, target_id: int, source_ids) -> int:
        """执行合并（对话框回调，也供测试直接调用）。返回搬动的词条数。"""
        moved = self.db.merge_batches(int(target_id), list(source_ids or []))
        if self._browse_batch_id in set(int(i) for i in (source_ids or [])):
            self._browse_batch_id = int(target_id)
        if self.app.capture_service.current_batch_id() in set(int(i) for i in (source_ids or [])):
            self.app.capture_service.set_current_batch(int(target_id))
        self.refresh_batches()
        self.refresh_tags()
        self.refresh_entries()
        self.app.set_status(f"已合并 {moved} 条词语")
        return moved

    def split_entries_dialog(self) -> None:
        """「拆分…」：把勾选的词条搬进新主题 / 另一个已有主题。"""
        ids = self.checked_ids()
        if not ids:
            messagebox.showinfo("拆分主题",
                                "先在中栏词条卡片上勾选要搬走的词条（可多选）。",
                                parent=self.root)
            return
        #: 安全阀（K1）：进主题时**默认全勾**，于是「点开拆分就搬走整个主题」只要
        #: 两下。整组都被勾上时先问一句 —— 搬家本身可逆，但用户往往没意识到自己
        #: 搬的是全部（当年这条操作得一条条勾，风险不一样）。
        if self._checked_scope and set(ids) >= set(self._checked_scope):
            if not messagebox.askyesno(
                    "拆分主题",
                    f"当前这一组的 {len(self._checked_scope)} 条词全部被勾选，"
                    "继续就会把整个主题搬走。\n确定要搬吗？",
                    parent=self.root):
                return
        MoveEntriesDialog(self.root, self.app, ids, list(self.db.list_batches()),
                          on_move=self.apply_move)

    def apply_move(self, entry_ids, mode: str, target_id: int, name: str = "") -> int:
        """执行搬家（对话框回调，也供测试直接调用）。返回搬动的条数。"""
        ids = [int(i) for i in (entry_ids or [])]
        if not ids:
            return 0
        if mode == MoveEntriesDialog.MODE_NEW:
            title = " ".join(str(name or "").split()).strip()
            if not title:
                return 0
            bid = self.db.create_batch(title)
        else:
            bid = int(target_id)
            if self.db.get_batch(bid) is None:
                return 0
        moved = self.db.move_entries(ids, bid)
        self.clear_checked()
        if self._browse_batch_id is not None:
            self._browse_batch_id = bid            # 跟着搬过去看结果
            #: 搬到哪儿就是「换了组」这件事，用户自己已经做完了：先把视野键提前
            #: 对齐，否则下面这次刷新会把「换主题 = 默认全勾」再执行一遍，
            #: 刚被 ``clear_checked()`` 清掉的勾会立刻按新主题全勾回来。
            self._checked_scope_key = self._checked_view_key()
        self.refresh_batches()
        self.refresh_tags()
        self.refresh_entries()
        self.app.set_status(f"已移动 {moved} 条词语")
        return moved

    # ------------------------------------------------------------- 导出（B3）
    def _export_scope_label(self) -> str:
        """给导出文件起个能看懂的范围名（主题名 / 搜索词 / 标签 / 全部）。"""
        parts: list[str] = []
        if self._tag_filter:
            parts.append(self._tag_filter)
        batch_id = self.filter_batch_id()
        row = self.db.get_batch(batch_id) if batch_id else None
        if row is not None:
            parts.append(str(row["name"]))
        query = self.search_var.get().strip()
        if query:
            parts.append(query)
        return " ".join(parts) if parts else "全部词语"

    def export_entries(self) -> None:
        """导出当前列表：先弹格式选择器，选完再写**一个**文件，最后问要不要开文件夹。

        口径（K1，用户本轮确认）：导出**不按勾选筛**，导的是当前列表范围 ——
        勾选只决定「哪些词进参考关系图」。

        口径（M，用户本轮确认）：格式有八种（CSV / Markdown / JSON / JSONL /
        Anki / PDF / HTML / TXT），由用户在 :class:`app.ui.export_dialog.ExportDialog`
        里选；存到哪看「设置 → 导出」的保存位置（留空 = ``data/exports/``）。
        """
        try:
            rows = list(self.db.list_entries(
                batch_id=None if (self._tag_filter or self.search_var.get().strip())
                else self.filter_batch_id(),
                query=self.search_var.get().strip(), limit=MAX_CARDS,
                tag=self._tag_filter))
            tags_map = self.db.tags_for_entries([int(r["id"]) for r in rows])
        except Exception as exc:                      # pragma: no cover - 查询异常兜底
            log.warning("导出前查词失败: %s", exc)
            messagebox.showerror("导出失败", f"读取词条失败：{exc}", parent=self.root)
            return
        scope_label = self._export_scope_label()
        directory = export_service.resolve_directory(self.cfg.export_directory)
        #: 选择器**不阻塞**：按「导出」时才回调这里（选过的格式会记进 export.format）
        ExportDialog(
            self.root, cfg=self.cfg, scope_label=scope_label, count=len(rows),
            directory=directory, default_format=self.cfg.export_format,
            on_confirm=lambda key: self._write_export(rows, tags_map, scope_label,
                                                      directory, key))

    def _write_export(self, rows, tags_map, scope_label: str, directory, fmt: str) -> None:
        """真正写文件（格式选择器按「导出」之后才走到这儿）。"""
        try:
            result = export_service.export_entries(
                rows, scope_label=scope_label, tags_map=tags_map,
                directory=directory, fmt=fmt)
        except OSError as exc:
            log.warning("导出失败: %s", exc)
            messagebox.showerror("导出失败", f"写文件失败：{exc}", parent=self.root)
            return
        except Exception as exc:                      # pragma: no cover - 生成异常兜底
            log.error("导出失败（%s）: %s", fmt, exc)
            messagebox.showerror("导出失败", f"生成 {fmt} 失败：{exc}", parent=self.root)
            return
        self.app.set_status(result.summary()[:STATUS_MAX_CHARS])
        chosen = result.format
        if messagebox.askyesno(
                "导出完成",
                f"{result.summary()}\n\n格式：{chosen.label}（{chosen.target}）\n"
                f"存到：{result.directory}\n\n现在打开文件夹吗？",
                parent=self.root):
            self._open_directory(result.directory)

    @staticmethod
    def _open_directory(path) -> bool:
        """用系统文件管理器打开目录（**没有** filedialog 的替代做法）。"""
        try:
            os.startfile(str(path))          # noqa: S606 - Windows 专用，路径在 data/ 内
            return True
        except (OSError, AttributeError) as exc:  # pragma: no cover - 非 Windows / 无关联
            log.warning("打开目录失败: %s", exc)
            return False

    # ------------------------------------------------------------- 搜索/卡片
    def _on_scope_change(self) -> None:
        self.cfg.set_bool("ui.batch_scope_all", self.scope_var.get())
        self.refresh_entries()

    def _clear_search(self) -> None:
        self.search_var.set("")
        self.refresh_entries()

    def _on_search_key(self, event) -> None:
        if event.keysym in ("Return", "Escape"):
            return
        self.root.after(250, self._debounced_search)

    def _debounced_search(self) -> None:
        self.refresh_entries()

    def refresh_entries(self) -> None:
        query = self.search_var.get().strip()
        tag = self._tag_filter
        batch_id, show_all = self.entry_scope()
        if tag:
            # 标签是**跨主题**的筛选：一旦按标签看，就按全库列（与搜索同一套判据）
            rows = self.db.list_entries(query=query, limit=MAX_CARDS, tag=tag)
            total = self.db.count_entries(query=query, tag=tag)
            show_all = True
        elif show_all:
            rows = self.db.list_entries(query=query, limit=MAX_CARDS)
            total = self.db.count_entries()
        elif batch_id is not None:
            rows = self.db.list_entries(batch_id=batch_id, query=query, limit=MAX_CARDS)
            total = self.db.count_entries(batch_id)
        else:
            # 这一页还没有保存过任何词：空列表，**不拿全库顶上**
            rows = []
            total = 0
        self._entry_rows = rows
        #: 卡片要显示标签与「命中片段」，两条都在这里批量查好（每条一次查询太浪费）
        self._tag_map = self.db.tags_for_entries([int(r["id"]) for r in rows])
        self.cards.clear()
        self._cards.clear()
        self._card_vars.clear()

        if not rows:
            hint = "暂无词语" if (show_all or batch_id is not None) else "这一页还没有词语"
            tk.Label(self.cards.inner, text=hint, bg=theme.BG, fg=theme.TEXT_FAINT,
                     font=theme.font(9), justify="left").pack(anchor="w",
                                                              padx=theme.px(10),
                                                              pady=theme.px(14))
        for row in rows:
            self._make_card(row)

        browsing = self._browse_batch_id is not None and batch_id == self._browse_batch_id
        scope = "全部词语" if show_all else ("本主题" if browsing else "本页")
        if tag:
            scope += f" · 标签「{tag}」"
        self.count_label.configure(text=f"{len(rows)} / {total} 条（{scope}）")
        self._refresh_checked_scope()
        self._update_sel_hint()
        self._drop_missing_checks()
        # 选中的词条可能刚刚消失（单条删除 / **整个主题被删** / 面板里删掉）：
        # 这里是所有增删改之后都会走的收口，所以复位放在这儿 —— 左栏空了右栏
        # 还显示着已删词条的词语与释义，就是用户截图里那个「残留」。
        # 只按**库里的存在性**判断，不看它是否还在当前筛选结果里：搜索 / 切主题
        # 只是换了列表范围，右栏显示的那条词并没有被删，不该被清空。
        if self._selected_entry_id is not None and \
                self.db.get_entry(self._selected_entry_id) is None:
            self.clear_detail()
        # 词条新增 / 编辑 / 删除 / 解释完成之后都会走到这里：让**已经打开的**
        # 参考关系图按当前库本地跟上（只读库 + 丢过期图，绝不自动发 LLM 请求）。
        notifier = getattr(getattr(self, "app", None), "notify_map_entries_changed", None)
        if callable(notifier):
            notifier()

    def _make_card(self, row, *, tags=None, query=None) -> None:
        eid = int(row["id"])
        if tags is None:
            tags = self.db.tags_for(eid)
        if query is None:
            query = self.search_var.get().strip()
        card = tk.Frame(self.cards.inner, bg=theme.PANEL, highlightthickness=1,
                        highlightbackground=theme.BORDER)
        card.pack(fill="x", pady=theme.px(4), padx=theme.px(2))

        top = tk.Frame(card, bg=theme.PANEL)
        top.pack(fill="x", padx=theme.px(10), pady=(theme.px(7), 0))

        #: 勾选框（B2 拆分 / K1 导图）：勾上的词才进参考关系图，拆分也只搬勾上的。
        #: 选中的 id 存在 ``self._checked`` 里（刷新列表后仍然保留 —— 用户勾的
        #: 就是这些词）；进一个主题时默认**全勾**，见 :meth:`_refresh_checked_scope`。
        var = tk.BooleanVar(value=eid in self._checked)
        check = tk.Checkbutton(
            top, text="", variable=var, command=lambda i=eid, v=var: self._toggle_checked(i, v),
            bg=theme.PANEL, activebackground=theme.PANEL, selectcolor=theme.BG,
            highlightthickness=0, bd=0, takefocus=0,
        )
        check.pack(side="left", padx=(0, theme.px(4)))
        widgets.tooltip(check, CHECK_TOOLTIP)
        self._card_vars[eid] = var

        tk.Label(top, text=row["term"], bg=theme.PANEL, fg=theme.TEXT,
                 font=theme.font(12), justify="left", anchor="w",
                 wraplength=theme.px(420)).pack(side="left")

        status = str(row["explain_status"] or "none")
        badge = tk.Label(top, text=STATUS_LABEL.get(status, "未解释"), bg=theme.PANEL,
                         fg=theme.TEXT_FAINT, font=theme.font(7))
        badge.pack(side="right")

        meta = f"{row['source_title'] or row['source_app'] or '(未知来源)'}  ·  {row['captured_at']}"
        if row["repeat_count"] > 1:
            meta += f"  ·  重复 {row['repeat_count']} 次"
        tk.Label(card, text=meta, bg=theme.PANEL, fg=theme.TEXT_MUTED, font=theme.font(7),
                 justify="left", anchor="w", wraplength=theme.px(460)).pack(
            fill="x", padx=theme.px(10), pady=(theme.px(2), 0))

        if tags:
            tk.Label(card, text="  ".join(f"#{t}" for t in tags), bg=theme.PANEL,
                     fg=theme.TEXT_MUTED, font=theme.font(7), justify="left", anchor="w",
                     wraplength=theme.px(460)).pack(fill="x", padx=theme.px(10),
                                                    pady=(theme.px(2), 0))

        #: 搜索命中说明（B5）：告诉用户**为什么**这条会出现在结果里
        #: （命中字段 + 片段，命中处已用【】圈出）。
        if query:
            hit = search_service.describe(row, query, tags=tags)
            if hit:
                tk.Label(card, text=f"命中 {hit}", bg=theme.PANEL, fg=theme.TEXT_BODY,
                         font=theme.font(8), justify="left", anchor="w",
                         wraplength=theme.px(460)).pack(fill="x", padx=theme.px(10),
                                                        pady=(theme.px(2), 0))

        if row["one_line"]:
            tk.Label(card, text=row["one_line"], bg=theme.PANEL, fg=theme.TEXT_BODY,
                     font=theme.font(9), justify="left", anchor="w",
                     wraplength=theme.px(460)).pack(fill="x", padx=theme.px(10),
                                                    pady=(theme.px(3), theme.px(7)))
        else:
            tk.Frame(card, bg=theme.PANEL, height=theme.px(6)).pack()

        for w in (card, top):
            w.bind("<Button-1>", lambda e, i=eid: self.select_entry(i))
            self.cards.bind_wheel(w)
        for child in card.winfo_children():
            child.bind("<Button-1>", lambda e, i=eid: self.select_entry(i))
            self.cards.bind_wheel(child)
            for sub in child.winfo_children():
                sub.bind("<Button-1>", lambda e, i=eid: self.select_entry(i))
                self.cards.bind_wheel(sub)

        self._cards[eid] = card
        if self._selected_entry_id == eid:
            self._highlight(eid)

    def _highlight(self, entry_id: int) -> None:
        for eid, card in self._cards.items():
            card.configure(highlightbackground=theme.TEXT if eid == entry_id else theme.BORDER)

    def select_entry(self, entry_id: int) -> None:
        row = self.db.get_entry(entry_id)
        if row is None:
            # 已被删除（例如异步解释回来时词条已经不在了）→ 右栏一起复位，
            # 不能只把 id 清掉：那会留下上一条词条的词语与释义当「残留」。
            self.clear_detail()
            return
        self._selected_entry_id = entry_id
        self._highlight(entry_id)
        batch = self.db.get_batch(int(row["batch_id"]))
        self.detail_hint.configure(text=str(batch["name"]) if batch else "")
        self.var_term.set(row["term"])
        self.txt_context.delete("1.0", tk.END)
        self.txt_context.insert("1.0", row["context"] or "")
        self.var_src_title.set(row["source_title"])
        self.var_src_url.set(row["source_url"])
        tags = self.db.tags_for(entry_id)
        self.var_tags.set("，".join(tags))
        self.refresh_tags()
        self._render_tag_chips(tags)
        self.var_meta.set(
            f"{row['source_app'] or ''}   {row['captured_at']}"
            + (f"   重复 {row['repeat_count']} 次" if row["repeat_count"] > 1 else "")
        )
        self._set_explanation_text(row)
        self.update_explain_button()

    #: 详情里最多显示几个「库里已有的标签」快捷按钮
    TAG_CHIP_LIMIT = 8

    def _render_tag_chips(self, current) -> None:
        """把库里已有、但这条词还没有的标签做成小按钮（点一下加上去）。"""
        frame = getattr(self, "tag_chips", None)
        if frame is None:
            return
        for child in frame.winfo_children():
            child.destroy()
        have = {str(t).casefold() for t in (current or ())}
        names = [str(r["name"]) for r in self._tag_rows
                 if str(r["name"]).casefold() not in have]
        for name in names[: self.TAG_CHIP_LIMIT]:
            chip = widgets.FlatButton(frame, f"+{name}",
                                      command=lambda n=name: self._add_tag_chip(n),
                                      font_size=7, padx=5, pady=1)
            chip.pack(side="left", padx=(0, theme.px(3)))
            # 说明要把「这是往输入框里填字、还没存」说清楚 —— 光看「+标签」会以为
            # 点一下就已经加上了。
            widgets.tooltip(chip, f"把「{name}」填进标签框（还要点「保存修改」才生效）")

    def _add_tag_chip(self, name: str) -> None:
        """把标签按钮的名字并进输入框（不落库 —— 还要点「保存修改」）。"""
        try:
            current = str(self.var_tags.get() or "")
        except tk.TclError:  # pragma: no cover
            current = ""
        names = normalize_tags([*[t for t in current.replace("，", ",").split(",")], name])
        self.var_tags.set("，".join(names))
        self._render_tag_chips(names)

    def _set_explanation_text(self, row) -> None:
        import json

        self.exp_text.configure(state="normal")
        self.exp_text.delete("1.0", tk.END)
        if row["explain_status"] == "error":
            self.exp_text.insert("1.0", f"解释失败：{row['explain_error'] or '未知错误'}\n\n"
                                        "可以点下方「解释」重试。")
        elif row["explain_status"] in ("ok", "stale"):
            #: ``stale``（B4）：上下文在解释之后被追加过 —— 旧解释还在，但已经不
            #: 覆盖新的上下文，所以照旧显示 + 明说一句「建议重新解释」。
            if row["explain_status"] == "stale":
                self.exp_text.insert("1.0", "（上下文已补充，建议重新解释）\n\n")
            self.exp_text.insert(tk.END, row["one_line"] + "\n")
            if row["detail"]:
                self.exp_text.insert(tk.END, f"\n{row['detail']}\n")
            try:
                examples = json.loads(row["examples"] or "[]")
            except Exception:
                examples = []
            if examples:
                self.exp_text.insert(tk.END, "\n例子：\n")
                for ex in examples:
                    self.exp_text.insert(tk.END, f"  · {ex}\n")
        else:
            self.exp_text.insert("1.0", "尚未解释。\n\n点下方「解释」获取解释。")
        self.exp_text.configure(state="disabled")

    def clear_detail(self) -> None:
        """把右侧详情复位成「还没选中词条」——**删除路径必须走这里**。

        词条被删（单条删除 / 整个主题被删 / 异步解释回来时词条已不在）之后，如果
        右栏还留着它的词语、上下文、来源与释义，用户看到的是「左边已经删了、右边
        还有残留」，会以为这条词还在库里（用户反馈的截图就是这个状态）。这里把
        :data:`EMPTY_DETAIL_HINT` 与五个字段一起复位，并**取消卡片高亮**、
        把解释按钮按「没有选中」重置文案；全程只碰控件，不写库、不联网。
        """
        self._selected_entry_id = None
        self._highlight(-1)                    # -1 不匹配任何词条 → 全部回到未选中描边
        self.detail_hint.configure(text=EMPTY_DETAIL_HINT)
        self.var_term.set("")
        self.txt_context.delete("1.0", tk.END)
        self.var_src_title.set("")
        self.var_src_url.set("")
        self.var_tags.set("")
        self._render_tag_chips(())
        self.var_meta.set("")
        self.exp_text.configure(state="normal")
        self.exp_text.delete("1.0", tk.END)
        self.exp_text.configure(state="disabled")
        self.update_explain_button()

    def refresh_selected(self) -> None:
        if self._selected_entry_id:
            self.select_entry(self._selected_entry_id)

    # ------------------------------------------------------------- 详情动作
    def save_detail(self) -> None:
        if not self._selected_entry_id:
            return
        term = self.var_term.get().strip()
        if not term:
            messagebox.showwarning("保存", "词语不能为空", parent=self.root)
            return
        ctx = self.txt_context.get("1.0", tk.END).strip()
        title = self.var_src_title.get().strip()
        url = self.var_src_url.get().strip()

        row = self.db.get_entry(self._selected_entry_id)
        changed_source = (title != (row["source_title"] or "")) or (url != (row["source_url"] or ""))
        fields = {"term": term, "context": ctx, "source_title": title, "source_url": url}
        if changed_source:
            # 人工修正来源 → 可信度升级为 manual（SPEC.md FR-2.3）
            fields["source_confidence"] = "manual"
        # 词语或语境被改动 → 原解释已不对应当前内容，就地作废（不删缓存：
        # 缓存键含 term+context，不会被新内容误命中，也不影响别的词条）。
        if term != (row["term"] or "") or ctx != (row["context"] or ""):
            fields.update({
                "explain_status": "none",
                "one_line": "",
                "detail": "",
                "examples": "[]",
                "explain_error": "",
                "explained_at": "",
            })
            self.app.invalidate_snapshot_explanation(self._selected_entry_id)
        self.db.update_entry(self._selected_entry_id, **fields)
        # 标签单独走关系表（entries 表里没有标签列），写的是**整体替换**后的结果
        final_tags = self.apply_tags(self.var_tags.get())
        self.var_tags.set("，".join(final_tags))
        self.refresh_entries()
        self.refresh_tags()
        self.select_entry(self._selected_entry_id)
        self.app.set_status("已保存词条修改")

    def delete_selected(self) -> None:
        if not self._selected_entry_id:
            return
        if not messagebox.askyesno("删除词条", "确定删除这条词条？（它的追问历史会一并删除）",
                                   parent=self.root):
            return
        self.db.delete_entry(self._selected_entry_id)
        self.clear_detail()                    # 右栏当场复位，不留已删词条的残留
        self.refresh_batches()
        self.refresh_tags()
        self.refresh_entries()

    # ------------------------------------------------------------- 状态栏
    def update_status(self) -> None:
        enabled = self.app.capture_enabled()
        self.pause_btn.configure(text="暂停取词" if enabled else "恢复取词")
        self.game_btn.configure(text="游戏模式：开" if self.app.game_mode() else "游戏模式：关")
        self.sync_topmost_check()
        self.update_explain_button()
        decision = self.app.gate_decision()
        self._gate_text = decision.status_text() if decision else ""
        # 状态栏只说用户关心的三件事：取词开着 / 取词暂停 / 游戏模式。
        if self.app.game_mode():
            plain = "游戏模式"
        elif not enabled or (decision is not None and not decision.allowed):
            plain = "取词已暂停"
        else:
            plain = "取词已开启"
        self.status_label.configure(text=str(plain)[:STATUS_MAX_CHARS])
        self.refresh_alerts(decision)
        # 注意：这里**不**重建批次列表。批次行只在增删/重命名/固定/记录时变化，
        # 那些路径会显式调用 refresh_batches()；每次状态更新都重建会让列表可视闪动。
