"""阅读面板：**一个窗口**同时承担「小方块（Dock）」与「紧凑浮窗（展开态）」。

用户只看到一个面板
------------------
旧的 ``SelectionBar``（选区小浮条）+ ``ExplainWindow``（解释结果窗）在本轮被
**同一个 :class:`ReadingPanel` 实例**取代（``app.selection_bar is app.explain_window``）：
两个角色合到一个窗口上，因此不会出现「两个浮层互相抢位置 / 一个收起一个还在」。
本类同时实现了旧两类的对外方法（``show_selection`` / ``show_recorded`` /
``show_result`` / ``restore`` / ``hide`` / ``clear`` …），所以 ``App`` 的
token / 门控 / 幂等逻辑**一行都不用改**（``tests.test_unified_action`` 全绿）。

三种可视状态
------------
* ``dock``：44x44 小方块，默认贴工作区右边缘、垂直居中。**阅读时后台只留它**；
* ``panel``：默认 360x460（DPI aware）的紧凑浮窗，最小 300x320，最大不超过工作区；
  可拖标题栏移动、可拖右下角改尺寸，松手后把位置/尺寸写进 settings；
* ``hidden``：完全收起（门控硬阻断 / 用户关闭 / 程序退出）。

真实窗口的圆角由 Win32 region 实现（不是只画在预览里）：小方块 10px、展开浮窗
12px，在 geometry 真正变化时与首次映射前更新，失败一律退回方形 —— 详见
:meth:`ReadingPanel._sync_window_region`。旧浮层（SelectionBar / ExplainWindow）
不走这条路径，保持原样。

交互契约（用户明确要求）
------------------------
* **划选**：只把内存快照挂到面板上并**自动展开**（零写库、零联网、NOACTIVATE
  不抢阅读焦点）；用户点过 × 关闭后**不自动展开**（找回只能靠用户显式动作，
  快照仍然保留）；
* **点外部**：只**作废当前待处理选区**（清内存快照），窗口保持原样 ——
  面板的显隐只由用户（点小方块 / 标题栏「折叠」/ 右上角「×」）与硬阻断决定；
  点在面板/小方块上不算外部（钩子按下瞬间的 ``is_overlay`` 判定）；
* **小方块**：显式点击 = 展开；面板标题栏的「折叠」= 收成小方块；拖动 = 移动；
* **可见功能只剩这些**：主界面入口 / 折叠 / ×、词卡与「返回词表」、
  「解释并记录」、释义、追问输入 + 发送（词表超过一页时才出现
  「显示更多 / 收起列表」）。设置 API / 搜索 / 主题管理 / 导入 / 导图 /
  暂停取词 / 游戏模式 / 退出**全部归主界面**，浮窗一个都不放 ——
  旧版常驻的「菜单」（弹系统式 grab 菜单）、常驻「重新解释」「设置 API」
  三个按钮已按这条契约删除（不是隐藏：控件与对应代码一起删掉）；
* **顶栏「主界面」**：**直接**调 ``App.open_main_window`` 打开词典主窗口，
  **不再**弹 ``tk_popup`` 的抢占式菜单 —— 浮窗带 ``WS_EX_NOACTIVATE``，
  抓取式菜单在实机上拿不到焦点，用户点了像没反应；
* **右上角「×」**：唯一的显式关闭入口，接 :meth:`close_by_user` —— 连小方块
  一起收起、标记 ``closed_by_user`` 并在**后台继续**（取词 / 在途解释都不受影响），
  **绝不** ``quit``；关闭后迟到结果 / 新划选 / 门控轮询都不会复活它，
  只有用户显式动作（小方块点击 / 快捷键 / 找回入口）能重新打开；
* **「解释并记录」**：先在 `App` 里幂等落库 → 再异步解释 → 结果写回
  同一个 ``entry_id``；同一个选区连续点击只操作一次（可见性 + token + 忙三道守卫），
  被守卫挡下时**给出可见提示**（绝不留下「点了没反应」的死点击）；
* **解释失败**：详情页**条件显示**一处简短的「重试」（只解释这条词，
  不重复记词、不加词频）；没配置 Key 时只显示一句「请在主界面配置 API」，
  **这句话本身可点**，点它或点顶栏「主界面」都能直接打开主界面并给状态反馈；
* **迟到结果**：只在 `entry_id + 请求 token` 同时匹配时更新内容；
  **绝不**重新展开已经折叠 / 隐藏的窗口，也不会覆盖新词；
* **标签**：已保存的词按**两列等宽卡片**排列（``CARD_H``=64、``CARD_GAP``=10、
  8px 圆角、暖米白底 + 浅灰边线；词语 10pt，长词卡内省略或换行、详情页全文可查；
  库里**已经有**一句话释义时卡片带一行短摘要，**不新增任何网络请求**），
  **点卡片任意位置只做本地读取**（词条 + 释义 + 该词的追问历史），不联网；
  悬停只做柔和的底色/边线变化，不弹任何说明文字；词表为空时提示居中放在
  **词卡视口正中**（不压在底部操作区、也不占一行把卡片区顶掉）；
* **顶部来源行**：只显示**当前这一页的简短主题名**（≤ ``TOPIC_MAX_CHARS``），
  没有主题就整行隐藏（连分隔线一起），**不显示浏览器整串窗口标题**；
* **详情来源行**：只有**一行**简短可读来源（标题或应用名，超长省略），
  没有就隐藏；URL / 完整窗口标题只在**主界面**的「来源链接」里看；
* **选词区**：只显示**当前词**（最多 2 行，按当前内容宽度省略）；选区上下文
  **不占浮窗的任何一行** —— 完整 term / context 仍原样在内存快照与库里，
  落库与解释一个字符都不少（截断只影响显示）；
* **滑轨**：词表右侧是 Canvas 自绘的细滑轨（圆角灰色滑块、无箭头无文字），
  滚轮 / 拖滑块 / 点轨道都能滑动，滑块大小与位置随 viewport 与内容变化，
  内容不足一屏时不滚动也不画滑块；缩放与滚动**不重建控件**（只重排 + 重画滑块）；
* **追问**：详情页底部输入 + 「发送」；**只有点发送才联网**，纯异步；
  用户明确点输入框时才允许窗口激活拿键盘（其余自动路径保持 NOACTIVATE）；
  空问题 / 没有词条 / 上一条还在回答都给一句可见提示，**不静默丢弃**；
  追问失败的「重试」只在**真的失败之后**才出现（不是常驻按钮行）；
* **无 Key**：保留输入、给出可配置提示，不伪造答案。

窗口外观（圆角相关，别改回去）
------------------------------
* 窗口底色 = 面板底色（``theme.PANEL``）：**没有**方形 ``highlight`` 描边 ——
  方形边线会被圆角 region 裁断，看起来就是「圆角缺口」；
* 1px 圆角描边由 :meth:`ReadingPanel._render_border` 画在 region **以内**，
  四个角跟随同一段圆弧；
* **圆角安全留白**：只有 ``outer`` 这一层带 ``padx`` / ``pady`` =
  ``半径 + 描边``（:func:`app.ui.panel_geometry.border_safe_inset`），
  ``panel_body`` / ``dock_body`` 铺满它、**不再重复留边**；模式切换只
  ``pack_configure`` 更新 ``outer`` 那一份边距。不透明的矩形内容只要贴到窗口
  边缘，就会把 x/y > 1 的那段圆弧整段盖住 —— 离线用 Pillow 画一个完整圆角
  **不能**证明实机上圆角可见；内缩是能证明的上界；
* **小方块图标**：内容可用区只有 ``dock_content_box()`` = 22x22，字号取
  ``DOCK_GLYPH_PT``（11pt）并**零原生内边距**（旧的 13pt + Label 默认内边距
  会被 region 裁掉一截）；字号常量与离线预览共用同一份；
* **换行宽度**：``hint`` / ``quote`` / ``title`` 的 ``wraplength`` 随**实际
  内容宽度**变化（:meth:`ReadingPanel._sync_wraplengths`），小窗拖到 300 宽时
  不再是写死的 322；
* 内部行不再重复一份 ``padx=10``：留白由 :data:`app.ui.panel_geometry.CONTENT_PAD`
  统一给（否则「安全留白 + 内部边距」会叠加成两倍缩进）；
* 右下角改尺寸手柄是 :class:`app.ui.widgets.ResizeGrip`：**没有**方形边框，
  只有 3 条向内收的灰色斜线 + 整块命中区，整组线都落在圆角半径以内；
* region 用**有效**窗口设备尺寸下发：刚下发 geometry 时用**请求尺寸**
  （``winfo_width`` 这时可能还是旧的 44x44，跟着它会把展开窗裁成 44x44），
  等 Tk 自己的 ``<Configure>`` / idle 回执到了再按实测尺寸幂等同步；
  ``<Configure>`` 先按 ``event.widget is self.win`` 过滤掉子控件事件，再**拒绝
  带着已作废旧尺寸的迟到事件**；idle 回执带**请求序号**，读到旧 ``winfo`` 尺寸
  时绝不清掉 pending、也绝不把 region 退回旧尺寸；同步过程不 ``update``、
  不 ``lift``、不改显隐；参数 ``(w, h, radius)`` 没变就**不重复** ``SetWindowRgn``；
  不使用任何透明颜色键（那会在正文上打洞）。

线程与门控
----------
所有 Tk 调用都在 UI 线程；本类不读 UIA、不装钩子、不轮询前台。显示统一走
``FloatingWindow.show_at`` 的实时门控（``App.overlay_display_allowed``）：
游戏 / 全屏 / 手动游戏模式 / 用户暂停下**只隐藏、零 deiconify**。
门控轮询**不会**重建界面或标签（``_render_*`` 全部按签名做幂等更新）。
"""
from __future__ import annotations

import json
import tkinter as tk

from .. import win32util as w32
from ..logging_setup import get_logger
from . import panel_geometry as geo
from . import theme, widgets
from .floating import FloatingWindow

log = get_logger("panel")

# ---------------------------------------------------------------- 常量
MODE_HIDDEN = "hidden"
MODE_DOCK = "dock"
MODE_PANEL = "panel"

PAGE_LIST = "list"
PAGE_DETAIL = "detail"

#: 唯一按钮文案（与旧 SelectionBar 保持一致，勿再拆成两个按钮）
ACTION_TEXT = "解释并记录"
#: 顶栏最左的入口：直接打开词典主窗口（**不是**菜单，绝不弹 grab 菜单）
MAIN_TEXT = "主界面"
#: 标题栏右上角「×」：收起面板并在后台继续（**绝不退出程序**）
CLOSE_TEXT = "×"
#: 详情页返回入口：回到**词表**（不是「全部词语」—— 范围由当前页面 / 浏览主题决定）
BACK_TEXT = "← 返回词表"
#: 详情页唯一解释入口的两种文案：还没有释义 = 「解释」，失败 / 过期 = 「重试」
RETRY_TEXT = "重试"
EXPLAIN_TEXT = "解释"
#: 没有 API Key 时的提示：点这一句 = 打开主界面（提示本身可点，绝不无反馈）
NO_KEY_HINT = "请在主界面配置 API"
#: 主界面入口不可用时的兜底提示（仍然给用户一句可执行的话，短）
NO_MAIN_HINT = "暂时打不开主界面，可再点一次"

#: 内联授权卡（浮窗置顶）：一句问句 + 两个按钮，**不用**系统 messagebox 弹窗
CONSENT_TEXT = "允许浮窗置顶？"
CONSENT_ALLOW_TEXT = "允许"
CONSENT_DENY_TEXT = "暂不"

#: 词卡标签显示预算（CJK 全角字 / 半角单位）：与离线预览共用同一份纯常量与
#: 同一个 :func:`clip_tag`（定义在 ``panel_geometry``，这里只是转发，方便调用点）。
TAG_MAX_CHARS = geo.TAG_MAX_CHARS
TAG_MAX_UNITS = geo.TAG_MAX_UNITS
char_units = geo.char_units
clip_tag = geo.clip_tag
#: 卡片里「一句话摘要」的字数上限（只在库里**已经有** one_line 时显示，零联网）
SUMMARY_MAX_CHARS = 14
#: 标签列表**每页**显示多少个（超过就分批渲染，见 ``_tag_page``）
MAX_TAGS = 60

#: 详情页标题 / 来源的**显示字数上限**：长标题 / 长来源不得无限占高度
#: （小尺寸 300x320 下必须给底部输入行与可滚动区域留出空间）
TITLE_MAX_CHARS = 40
#: 来源**只显示一行**（标题或应用名），超长省略；URL 不在浮窗里出现
SOURCE_MAX_CHARS = 40
#: 顶部来源行的预算：只放「这一页的主题名」，不拼浏览器整串窗口标题
TOPIC_MAX_CHARS = 22
#: 内联主题清单最多列几个历史主题（再多就该去主界面侧栏里找了）
TOPIC_MAX_ITEMS = 8
#: 内联主题清单第一项：回到「跟随当前阅读页」的浏览范围
FOLLOW_TEXT = "跟随当前阅读页"
#: 选词区：`「词」` 最多 **2 行**（预算按当前内容宽度算，见 :func:`quote_budget`）。
#: 选区上下文**不再占浮窗任何一行**；这只是显示预算，完整 term / context 原样在
#: 内存快照与库里，落库 / 送 LLM 不受影响。
QUOTE_LINES = 2
#: 选词区里**词本身之外**的固定占位（单位 = 一个字号像素的格子）：一对括号
#: ``「`` / ``」`` + 截断省略号 ``…`` + 1 格安全余量（吸收字体实际 advance 与
#: 取整误差）。旧预算没扣这几格，最窄 300 宽下 `「词」` 会挤到第 3 行。
QUOTE_DECOR = 4
#: 失败正文里那行库内原因的字数上限（只取第一行）：完整错误仍在库与主界面。
FAILURE_MAX_CHARS = 40
#: 反馈行的上下留白（逻辑像素）：行高 = 8pt 字高 + 2×它，必须 ≥ ``GRIP_SIZE``，
#: 右下角改尺寸手柄才会**只**压在这一行里（不挡输入行 / 发送按钮）。
HINT_PAD_Y = 5


def clip_text(text: str, limit: int) -> str:
    """按字数截断（超出加省略号）；只影响**显示**，完整内容仍在库/提示里。"""
    body = str(text or "").strip()
    if int(limit) > 0 and len(body) > int(limit):
        return body[: max(1, int(limit) - 1)] + "…"
    return body


def quote_budget(width: int) -> int:
    """底部「当前词」的显示预算：按**当前内容宽度**算 ``QUOTE_LINES`` 行字。

    上界依据是**保守估计**（不依赖任何真实字体量测）：9pt 正文的字号像素
    ``cell = abs(theme.device_px(9))`` 就是一个 em，中文全角字与最宽的拉丁
    字母（``W``）的 advance 都**不超过**一个 em，所以「一个字符最多占 ``cell``
    像素」成立。``QUOTE_LINES`` 行最多放 ``QUOTE_LINES * (width // cell)`` 个
    字符，再扣掉 ``QUOTE_DECOR``（``「`` / ``」`` + 省略号 + 安全余量）才是词
    本身能显示的格数 —— 于是显示串 `「词」` 无论 CJK 还是宽英文都**不会超过**
    ``QUOTE_LINES`` 行（``_set_quote`` 里的 ``wraplength`` 也是同一个宽度）。

    这里截断的只是**显示**：完整 term / context 仍在内存快照与库里。
    """
    cell = max(1, abs(theme.device_px(9)))
    per_line = max(1, int(width) // cell)
    return max(1, per_line * QUOTE_LINES - QUOTE_DECOR)


def local_examples(raw) -> list[str]:
    """把库里 ``examples`` 字段（JSON 文本）**安全**解析成例子列表。

    与 ``MainWindow._set_explanation_text`` 同一套语义（``json.loads(... or "[]")``），
    但额外兜住脏数据：坏 JSON、非数组、数组里混进非字符串 —— 一律忽略而不是抛异常
    （详情页渲染绝不能因为库里一条脏数据炸掉）。空串元素也过滤掉。
    """
    if isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        try:
            items = json.loads(str(raw or "") or "[]")
        except Exception:
            return []
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for item in items:
        text = str(item if item is not None else "").strip()
        if text:
            out.append(text)
    return out


def _local_body(row) -> str:
    """本地词条的释义正文：one_line + detail + 例子（与 ``ExplainResult.as_text`` 同排版）。

    **实时结果**走 ``self.result.as_text()``（它自己已含例子），这条路径只用于
    「从库里重开已解释词条」—— 旧实现只拼 one_line/detail，DB 里的 examples 丢了。
    """
    lines = [str(row["one_line"] or "")]
    detail = str(row["detail"] or "")
    if detail:
        lines.extend(["", detail])
    examples = local_examples(row["examples"])
    if examples:
        lines.extend(["", "例子："])
        lines.extend(f"  · {ex}" for ex in examples)
    return "\n".join(lines).strip()

#: 状态文案（只描述这条词此刻的状态，不带任何内部标记）。
#: ``none`` = 库里从未解释过（``explain_status`` 原值，打开词条时不再被吞成空串）。
STATUS_TEXT = {
    "none": "未解释",
    "pending": "解释中…",
    "pending_no_key": "待解释",
    "ok": "已解释",
    "error": "解释失败",
    "stale": "需要重新解释",
    "notice": "已记录",
}


class ScrollArea:
    """细线滚动容器（Canvas + 内嵌 Frame + 右侧自绘滑轨），标签区与长文本共用。

    ``fill_both=True``：内嵌 Frame 跟随画布尺寸（长回答 / 追问历史用，
    滚动交给内嵌 Text 自己）；
    ``fill_both=False``：内嵌 Frame 高度由内容决定（词表卡片用，
    滚动交给画布）。

    滚动条是 :class:`app.ui.widgets.ScrollRail`（Canvas 自绘圆角滑块、无箭头、
    无说明字），``set`` / ``command`` 协议与 ``tk.Scrollbar`` 相同，因此宿主
    代码（``canvas.yview`` / ``Text.yview``）一行都不用改。
    """

    def __init__(self, parent, *, bg: str = theme.PANEL, height: int | None = None,
                 fill_both: bool = False):
        self.frame = tk.Frame(parent, bg=bg)
        self.fill_both = bool(fill_both)
        self.canvas = tk.Canvas(self.frame, bg=bg, highlightthickness=0, bd=0,
                                height=height or 0)
        self.sb = widgets.ScrollRail(self.frame, command=self._on_scrollbar, bg=bg)
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.bind("<Configure>", self._on_canvas)
        self._children: list = []
        #: 滚轮作用对象：None = 画布自己；由 :meth:`add_text` 换成 Text 控件
        self._wheel_target = None

    # -------------------------------------------------------------- 布局
    def pack(self, **kw) -> None:
        self.frame.pack(**kw)

    def pack_forget(self) -> None:
        self.frame.pack_forget()

    def bind_wheel(self, widget) -> None:
        widget.bind("<MouseWheel>", self._on_wheel)

    def clear(self) -> None:
        widgets.clear_children(self.inner)
        self._children = []

    def add(self, widget):
        self._children.append(widget)
        self.bind_wheel(widget)
        return widget

    @property
    def children(self) -> list:
        """已放置的子控件（标签换行布局用）。"""
        return list(self._children)

    def add_text(self, widget):
        """长文本控件：滚轮直接滚它自己（不能用画布滚，否则文字不动）。"""
        self._wheel_target = widget
        self._children.append(widget)
        self.bind_wheel(widget)
        return widget

    def _on_scrollbar(self, *args) -> None:
        target = self._wheel_target
        if target is not None:
            target.yview(*args)
            return
        self.canvas.yview(*args)

    def set_extent(self, width: int, height: int) -> None:
        """放置（place）布局不参与几何传播，所以尺寸由调用方显式设置。"""
        try:
            self.inner.configure(width=max(1, int(width)), height=max(1, int(height)))
            self.canvas.configure(scrollregion=(0, 0, max(1, int(width)),
                                               max(1, int(height))))
        except tk.TclError:  # pragma: no cover
            pass

    # -------------------------------------------------------------- 事件
    def _on_canvas(self, event) -> None:
        try:
            if self.fill_both:
                self.canvas.itemconfigure(self._win, width=event.width,
                                          height=event.height)
            else:
                self.canvas.itemconfigure(self._win, width=event.width)
        except tk.TclError:  # pragma: no cover
            pass
        on_resize = getattr(self, "on_resize", None)
        if callable(on_resize):
            on_resize(int(getattr(event, "width", 0) or 0))

    def _on_wheel(self, event) -> None:
        delta = int(-getattr(event, "delta", 0) / 120) or 0
        try:
            target = self._wheel_target
            if target is not None:
                target.yview_scroll(delta, "units")
            else:
                self.canvas.yview_scroll(delta, "units")
        except tk.TclError:  # pragma: no cover
            pass


class ReadingPanel(FloatingWindow):
    """Dock + 紧凑浮窗（列表 / 详情 / 追问）合一。"""

    def __init__(self, master: tk.Misc, app):
        super().__init__(master, app, name="reading_panel", border_color=theme.OUTLINE)
        self.app = app
        # ---- 可视状态 ----
        self.mode = MODE_HIDDEN
        self.page = PAGE_LIST
        self.state = self._load_state()
        self._panel_w, self._panel_h = self._initial_panel_size()
        # ---- 选区角色（原 SelectionBar）----
        self.selection = None
        self.token = 0
        self._consumed_token: int | None = None
        self._busy = False
        # ---- 词条 / 结果角色（原 ExplainWindow）----
        self.entry_id: int | None = None
        self.request_token: int | None = None
        self.result = None
        self.message = ""
        self.status = ""
        # ---- 追问 ----
        self._chat_busy = False
        self._chat_request_token: int | None = None
        self._chat_error = ""
        self._last_question = ""
        #: 当前词条的追问历史缓存；``None`` = 还没读过（``_reload_chat`` 会填）。
        #: **切词时必须失效**（见 ``_bind_entry``），否则会串词。
        self._chat_turns: list | None = None
        #: 刚发出、库里可能还没被**重新读到**的用户提问（本地回显，见 ``_render_chat``）
        self._chat_echo = ""
        #: 用户是否正把键盘交给我们（点过输入框且还没点走）：软受限门控据此
        #: 不折叠面板，否则输入框会在下一次门控轮询里被收走
        self._input_active = False
        # ---- 渲染缓存（门控轮询 / 重复显示时零重建）----
        self._terms_sig = None
        self._detail_sig = None
        self._drag: geo.DragState | None = None
        #: 本次拖动起点的控件（松手没移动时判断「点的是哪个面」：小方块 = 展开，
        #: 来源行 / 箭头 = 开合主题清单）
        self._drag_origin = None
        #: 开始拖动时窗口处于哪个模式（dock / panel）：拖动期间窗口被别处
        #: 展开 / 折叠过 → 这次拖动已经不属于当前界面，松手不得再切换形态
        self._drag_start_mode: str | None = None
        #: 拖动中**最后一次真正下发过**的矩形：:meth:`_safe_move` 用它做
        #: 「同一位置零重复下发」的判据（松手位置由 :meth:`_on_drag_end`
        #: 按事件坐标重算，不再依赖它）。
        self._drag_end_rect: geo.Rect | None = None
        #: 标签分页：本页要显示多少条 + 当前范围（批次）下**真实**总数
        self._tag_page = MAX_TAGS
        self._tag_total = 0
        self._tag_scope = None
        #: 已经下发到 Win32 的圆角 region 参数 ``(w, h, radius)``：
        #: 值没变就**不重复**调用 SetWindowRgn（切 dock / 面板 / 改尺寸只更新必要的）
        self._region_key: tuple[int, int, int] | None = None
        #: 圆角描边画布的幂等键 ``(w, h, radius)``：没变就不重画
        self._border_key: tuple[int, int, int] | None = None
        #: **刚下发**的 geometry 请求尺寸（设备像素）：Tk 还没处理完之前
        #: ``winfo_width`` 可能仍是旧值（44x44），region 必须先按请求尺寸走
        self._requested_size: tuple[int, int] | None = None
        #: **请求序号**：每次真正下发 geometry 递增；idle 回执带着它回来，
        #: 迟到的回执（序号已经不是当前一代）一律丢弃
        self._request_seq = 0
        #: 已经**作废**的历史窗口尺寸（上一个请求 / 上一次实测）：迟到的
        #: ``<Configure>`` 常常正好带着这些尺寸，必须拒绝，否则会把刚按请求
        #: 尺寸设好的 region 又退回旧的
        self._stale_sizes: list[tuple[int, int]] = []
        #: Tk 通过自己的 ``<Configure>`` / idle 回执确认的**实测**窗口尺寸
        self._measured_size: tuple[int, int] | None = None
        #: 已经安排了 idle 回执的请求序号（拖动时不要堆一堆重复回调）
        self._ack_pending_seq: int | None = None
        #: 当前已下发的 ``outer`` 边距（设备像素）：模式切换**只**更新这一份
        self._outer_pad: int | None = None
        #: 当前已经 pack 好的模式 / 页面（幂等：状态没变就不重复 pack）
        self._packed_mode: str | None = None
        self._packed_page: str | None = None
        #: 最近一次同步过的 ``wraplength``（设备像素）
        self._wrap_width: int | None = None
        #: 提示行当前点击后要做什么（"" = 纯文字，不可点）
        self._hint_action = ""
        #: 内联授权卡（浮窗置顶）：授权未知时显示，用户点「允许 / 暂不」后立即移除
        self.consent_card: tk.Frame | None = None
        self._consent_card_packed = False
        #: 守卫路径写下的可见反馈是否还被「钉住」（见 :meth:`_hint_auto`）
        self._hint_hold = False
        #: 钉住时那条词的状态：同状态下的状态刷新不许把它清掉 / 覆盖
        self._hint_hold_status: str | None = None

        self._build()
        # 窗口**自己**的 Configure：子控件的 Configure 会沿着 bindtags 冒泡到
        # Toplevel，必须按 ``event.widget is self.win`` 过滤（见 _on_self_configure）
        try:
            self.win.bind("<Configure>", self._on_self_configure, add="+")
        except tk.TclError:  # pragma: no cover - 控件已销毁
            pass
        self._render_mode()
        self._render_list()
        self._render_detail()

    # ================================================================= 构建
    def _build(self) -> None:
        """窗口内容结构：**圆角描边画布**在最底层，内容整体内缩「半径 + 描边」。

        为什么不是 ``Frame(highlightthickness=1)``：方形边线会被窗口圆角 region
        裁断（四个角上的线突然消失），用户看到的就是「圆角缺口」。这里改成
        一张铺满窗口的 Canvas，用**圆角多边形**画那 1px 描边 —— 描边形状与
        region 的圆弧一致，角上不会断。底色三层（win / canvas / outer）全部
        用 ``theme.PANEL``，被裁掉的边缘露出同一种颜色。

        内容为什么要退开**整个半径**：描边的圆弧在 45° 附近已经深入到
        (≈3.5, ≈3.5)，矩形内容只要还铺在 x/y > 1 的位置就会把圆弧盖住
        （只剩四条直边可见 = 用户看到的「圆角缺口」）。内缩量由
        :func:`app.ui.panel_geometry.border_safe_inset` 统一给，dock / panel
        两种半径各自算，并且**只加在 ``outer`` 这一层**（``padx`` / ``pady``）：
        模式切换时只 :meth:`tkinter.Widget.pack_configure` 更新这一份边距，
        ``panel_body`` / ``dock_body`` 铺满 ``outer``、不再重复留边 ——
        内层再加一次就成了双倍缩进，而且矩形内容会重新贴到窗口边缘上。
        """
        self.win.configure(bg=theme.PANEL)
        self.border_canvas = tk.Canvas(self.win, bg=theme.PANEL, highlightthickness=0,
                                       bd=0, takefocus=0)
        # 先铺满整窗（place 不参与几何传播），再 pack 内容：内容内缩「半径+描边」，
        # 那一圈正好露出描边画布的**整条圆弧**。
        self.border_canvas.place(x=0, y=0, relwidth=1.0, relheight=1.0)
        self._border_shape = None

        # ``outer`` 自身就带一份**圆角安全留白**（padx/pady = 半径 + 描边）：
        # 它铺满窗口时是一个**不透明矩形**，不留边距就会把 ``border_canvas``
        # 画的整条圆弧盖住（用户看到的「有直边、没圆角」就是这么来的）。
        # 边距只由本控件承担；``panel_body`` / ``dock_body`` 再也不重复留边。
        self.outer = tk.Frame(self.win, bg=theme.PANEL)
        pad = self._body_padding()
        self.outer.pack(fill="both", expand=True, padx=pad, pady=pad)
        self._outer_pad: int | None = pad
        self._body = self.outer

        self._build_dock()
        self._build_panel()
        # 拖动按**白名单**逐个绑到非交互面：Tk 的绑定只对「事件真正落在的那个
        # 控件」生效（父 Frame 不在子控件的 bindtags 里），所以绝不需要、也绝不
        # 允许绑窗口 / 祖先来「接冒泡」—— 那会把词卡、滚动条、主题清单选项甚至
        # 改尺寸手柄的按下都当成空白拖动。
        for surface in self._drag_surfaces():
            self._bind_drag(surface)

    def _body_padding(self, mode: str | None = None) -> int:
        """当前模式的**圆角安全留白**（设备像素）：内容四边内缩半径 + 描边。

        dock 半径 10 → 内缩 11；展开面板半径 12 → 内缩 13（都随 DPI 一起缩放）。
        内部行只需要 :data:`app.ui.panel_geometry.CONTENT_PAD` 那一点点留白，
        可见边距因此仍然和旧版（1px 描边 + 10px 内距）接近，不会双倍缩进。
        """
        current = self.mode if mode is None else mode
        radius = geo.PANEL_RADIUS if current == MODE_PANEL else geo.DOCK_RADIUS
        return theme.px(geo.border_safe_inset(radius))

    def _render_border(self, *, force: bool = False) -> bool:
        """重画窗口的 1px 圆角描边（幂等：``(w, h, radius)`` 没变就什么都不做）。"""
        width, height = self._window_device_size()
        radius = self._window_radius()
        key = (int(width), int(height), int(radius))
        if not force and key == self._border_key and self._border_shape is not None:
            return False
        self._border_key = None
        try:
            self.border_canvas.delete("border")
            x, y, w, h, r = widgets.window_border_box(width, height, radius)
            self._border_shape = widgets.draw_round_rect(
                self.border_canvas, x, y, w, h, r, fill="", outline=theme.OUTLINE,
                width=max(1, theme.px(geo.WINDOW_BORDER)), tags="border")
        except tk.TclError:  # pragma: no cover - 控件已销毁
            return False
        self._border_key = key
        return True

    # ---------------------------------------------------------- 小方块
    def _build_dock(self) -> None:
        # **没有** highlight 边框：小方块只有 44x44，方形边线一定压在圆角 region
        # 的裁剪边缘上（四个角各缺一块）。窗口描边由 ``border_canvas`` 统一画。
        self.dock_body = tk.Frame(self.outer, bg=theme.PANEL, cursor="hand2")
        # 内容可用区只有 ``dock_content_box()`` = 22x22（四边各内缩半径 + 描边）：
        # 字号取 :data:`app.ui.panel_geometry.DOCK_GLYPH_PT`（11pt → 96 DPI 15px），
        # 并且 **零原生内边距**（``padx``/``pady``/``bd``/``highlightthickness`` 全 0）
        # —— 13pt + Label 默认内边距的排版单元 ≈22px，会被裁掉一截。
        # 字号是产品与离线预览共用的同一个常量，两边不会各画一个样。
        self.dock_label = tk.Label(
            self.dock_body, text=geo.DOCK_GLYPH, bg=theme.PANEL, fg=theme.TEXT,
            font=theme.font(geo.DOCK_GLYPH_PT), cursor="hand2",
            bd=0, highlightthickness=0, padx=0, pady=0, anchor="center",
        )
        self.dock_label.pack(fill="both", expand=True)

    # ------------------------------------------------------------ 拖动（B）
    def _drag_surfaces(self) -> tuple:
        """可拖动面的**白名单**（顺序即绑定顺序；未创建 / 为 None 的会被跳过）。

        只列非交互面：窗口描边画布、内容容器、两种模式的页面 Frame、词表滚动
        容器（画布 + 内嵌 Frame）、空词表提示、小方块、标题带与来源行。
        **不列**按钮、输入框、词卡、滚动条滑轨、主题清单选项、改尺寸手柄 ——
        这些控件自己处理按下，事件也不会冒泡到父 Frame。
        """
        tags = getattr(self, "tags_area", None)
        return (
            getattr(self, "border_canvas", None),
            getattr(self, "outer", None),
            getattr(self, "panel_body", None),
            getattr(self, "list_page", None),
            getattr(self, "detail_page", None),
            getattr(tags, "canvas", None),
            getattr(tags, "inner", None),
            getattr(self, "empty_hint", None),
            getattr(self, "dock_body", None),
            getattr(self, "dock_label", None),
            getattr(self, "title_row", None),
            getattr(self, "title_label", None),
            getattr(self, "source_row", None),
            getattr(self, "source_label", None),
            getattr(self, "topic_caret", None),
        )

    def _bind_drag(self, widget) -> None:
        """把「按住拖动窗口」三连绑到**一个非交互面**（见 :meth:`_drag_surfaces`）。

        真实 Tk 在按下时建立隐式抓取：Motion / Release 始终回到按下的那个控件，
        所以这三条必须绑在**同一个**面上，拖动中移出窗口也不会丢事件。
        """
        if widget is None:  # pragma: no cover - 防御
            return
        for sequence, handler in (("<Button-1>", self._on_drag_start),
                                  ("<B1-Motion>", self._on_drag_motion),
                                  ("<ButtonRelease-1>", self._on_drag_end)):
            try:
                widget.bind(sequence, handler)
            except (tk.TclError, AttributeError):  # pragma: no cover - 已销毁
                continue

    # ------------------------------------------------------------ 展开态
    def _build_panel(self) -> None:
        self.panel_body = tk.Frame(self.outer, bg=theme.PANEL)

        # ---- 顶栏：标题 + 右侧「×」/「折叠」，最左「主界面」 ----
        top = tk.Frame(self.panel_body, bg=theme.PANEL, cursor="hand2")
        top.pack(fill="x")
        #: 标题带（按钮之间的空白也能拖动；按钮自己**没有**拖动绑定）
        self.title_row = top
        #: **直接**打开词典主窗口（``App.open_main_window``）：没有菜单、没有
        #: ``tk_popup``、没有 grab —— 浮窗是 NOACTIVATE 窗口，抓取式菜单拿不到
        #: 焦点，实机上点了像没反应。
        self.btn_main = widgets.FlatButton(top, MAIN_TEXT, self._on_open_main, font_size=8,
                                           padx=8, pady=2)
        self.btn_main.pack(side="left", padx=(theme.px(geo.CONTENT_PAD), theme.px(4)),
                           pady=(theme.px(6), theme.px(4)))
        self.title_label = tk.Label(top, text="探索词典", bg=theme.PANEL, fg=theme.TEXT,
                                    font=theme.font(11), anchor="w", cursor="hand2")
        self.title_label.pack(side="left", padx=(theme.px(2), theme.px(4)),
                              pady=(theme.px(6), theme.px(4)))
        #: 右上角「×」：最右、最简洁的显式关闭入口（用户不再需要靠菜单退出面板）。
        #: 语义 = ``close_by_user``：连小方块一起收起并在后台继续，**绝不 quit**。
        self.btn_close = widgets.FlatButton(top, CLOSE_TEXT, self._on_close, font_size=9,
                                            padx=6, pady=1)
        self.btn_close.pack(side="right", padx=(theme.px(2), theme.px(geo.CONTENT_PAD)),
                            pady=(theme.px(6), theme.px(4)))
        self.btn_collapse = widgets.FlatButton(top, "折叠", self._on_collapse, font_size=8,
                                               padx=8, pady=2)
        self.btn_collapse.pack(side="right", padx=theme.px(2),
                               pady=(theme.px(6), theme.px(4)))
        # 标题带（含标题左边的空白、标题与按钮之间的空白）与来源行都可直接拖动；
        # 按钮是交互控件，不在拖动白名单里（见 _drag_surfaces）。
        # ---- 来源：只放「当前这一页的简短主题名」，没有主题时整块隐藏 ----
        # 这一行同时是**主题入口**：点它展开内联主题清单（见 _on_drag_end /
        # toggle_topic_picker）—— 不复用 tk_popup / grab 菜单：浮窗是
        # WS_EX_NOACTIVATE 窗口，抓取式菜单拿不到焦点，实机上点了像没反应。
        self.source_row = tk.Frame(self.panel_body, bg=theme.PANEL, cursor="hand2")
        self.source_row.pack(fill="x", padx=theme.px(geo.CONTENT_PAD))
        self.source_label = tk.Label(self.source_row, text="", bg=theme.PANEL,
                                     fg=theme.TEXT_MUTED, font=theme.font(8), anchor="w",
                                     justify="left", cursor="hand2")
        self.source_label.pack(side="left", fill="x", expand=True)
        # 展开箭头用**同源 Canvas 线条**画（``widgets.Chevron`` → 纯函数
        # ``widgets.chevron_points``）：不再用 ``▾`` 这类特殊字形 —— 离线预览用
        # Pillow 渲染时那种字形会变成缺字方框，而线条在 Tk 与预览里完全一致。
        self.topic_caret = widgets.Chevron(self.source_row, bg=theme.PANEL,
                                           fg=theme.TEXT_FAINT, cursor="hand2")
        self.topic_caret.pack(side="right", padx=(theme.px(2), 0))
        self.source_divider = widgets.divider(self.panel_body, pady=4)
        self._source_packed = True

        # ---- 主题内联清单（默认隐藏；点顶栏短主题展开）----
        self.topic_picker = tk.Frame(self.panel_body, bg=theme.PANEL_ALT)
        self._topic_open = False

        # ---- 反馈行（**草稿页共用**，永远在最底部）----
        #: 无 Key / 失败 / 上一条还在回答时的**唯一**反馈位置；需要用户去主界面时
        #: 这句话本身可点（``_hint_action == "main"``），绝不出现「点了没反应」。
        #: 放在 ``panel_body`` 而不是详情页里：列表页上的守卫（例如没选中词就点
        #: 「解释并记录」）也必须看得见反馈。
        #:
        #: 这一行同时是右下角改尺寸手柄（``GRIP_SIZE``）的**净空带**：
        #: * 左右 ``pady`` 让行高 ≥ 手柄边长 —— 手柄只压在这一行里，
        #:   永远不会盖到上面的「发送」按钮或输入行；
        #: * 右侧 ``padx`` 多留出手柄宽度 —— 提示文字不会跑到手柄底下。
        self.hint_label = tk.Label(self.panel_body, text="", bg=theme.PANEL,
                                   fg=theme.TEXT_MUTED, font=theme.font(8), anchor="w",
                                   justify="left")
        self.hint_label.pack(side="bottom", fill="x",
                             padx=(theme.px(geo.CONTENT_PAD),
                                   theme.px(geo.CONTENT_PAD + geo.GRIP_SIZE)),
                             pady=(theme.px(HINT_PAD_Y), theme.px(HINT_PAD_Y)))
        self.hint_label.bind("<Button-1>", self._on_hint_click)

        self._build_list_page()
        self._build_detail_page()

    # ---------------------------------------------------------- 置顶授权卡
    def show_consent_card(self) -> bool:
        """显示内联授权卡（**只**由 ``App`` 在「授权未知」时调用，幂等）。

        卡片紧跟在 ``title_row`` 之后 pack：不弹系统对话框、不加长说明，
        用户点「允许 / 暂不」后立刻移除（见 :meth:`set_overlay_topmost_consent`）。
        """
        if self.consent_card is None:
            self.consent_card = self._build_consent_card()
        if self.consent_card is None:
            return False
        if self._consent_card_packed:
            return True
        try:
            self.consent_card.pack(fill="x", after=self.title_row,
                                   padx=theme.px(geo.CONTENT_PAD),
                                   pady=(0, theme.px(4)))
        except tk.TclError:  # pragma: no cover - 控件已销毁
            return False
        self._consent_card_packed = True
        return True

    def _build_consent_card(self):
        """授权卡：一句问句 + 「允许」/「暂不」两个按钮。"""
        card = tk.Frame(self.panel_body, bg=theme.PANEL_ALT)
        tk.Label(card, text=CONSENT_TEXT, bg=theme.PANEL_ALT, fg=theme.TEXT,
                 font=theme.font(8), anchor="w").pack(
                     side="left", padx=(theme.px(geo.CONTENT_PAD), 0), pady=theme.px(4))
        widgets.FlatButton(card, CONSENT_ALLOW_TEXT, self._on_consent_allow, font_size=8,
                           padx=8, pady=2, primary=True).pack(
                               side="right", padx=(theme.px(2), theme.px(geo.CONTENT_PAD)),
                               pady=theme.px(3))
        widgets.FlatButton(card, CONSENT_DENY_TEXT, self._on_consent_deny, font_size=8,
                           padx=8, pady=2).pack(side="right", padx=theme.px(2),
                                                pady=theme.px(3))
        return card

    def consent_card_visible(self) -> bool:
        """授权卡此刻是否在界面上。"""
        return bool(self._consent_card_packed)

    def hide_consent_card(self) -> bool:
        """移除授权卡（幂等；控件保留，之后「补询问」可直接复用）。"""
        if not self._consent_card_packed or self.consent_card is None:
            return False
        try:
            self.consent_card.pack_forget()
        except tk.TclError:  # pragma: no cover - 控件已销毁
            return False
        self._consent_card_packed = False
        return True

    def set_overlay_topmost_consent(self, allow: bool) -> bool:
        """用户可以点「允许 / 暂不」：只调 ``App.set_overlay_topmost_consent`` 并移除卡片。

        面板显隐、位置、尺寸、待处理词一个都不动。
        """
        setter = getattr(self.app, "set_overlay_topmost_consent", None)
        if callable(setter):
            try:
                setter(bool(allow))
            except Exception:  # pragma: no cover - 保存失败也不该卡住界面
                log.exception("保存浮窗置顶授权失败")
        self.hide_consent_card()
        return True

    def _on_consent_allow(self) -> bool:
        return self.set_overlay_topmost_consent(True)

    def _on_consent_deny(self) -> bool:
        return self.set_overlay_topmost_consent(False)

    # -------------------------------------------------------------- 列表页
    def _build_list_page(self) -> None:
        self.list_page = tk.Frame(self.panel_body, bg=theme.PANEL)
        self.list_page.pack(fill="both", expand=True)

        # ---- ① 底部操作区：**最先 pack**（``side="bottom"``）把位置钉住 ----
        # pack 的分配顺序 = pack 调用顺序：先放底部，后面 ``expand`` 的词卡区
        # 才只会吃掉**剩下**的空间。旧实现把这一块放在最后 pack，窗口被压到
        # 最小尺寸（300x320）时词卡区先按 expand 分走空间，底部「当前词 +
        # 解释并记录」会被挤到可视范围之外。
        self.action_area = tk.Frame(self.list_page, bg=theme.PANEL)
        self.action_area.pack(side="bottom", fill="x")
        widgets.divider(self.action_area, pady=4)
        quote_box = tk.Frame(self.action_area, bg=theme.PANEL)
        quote_box.pack(fill="x", padx=theme.px(geo.CONTENT_PAD), pady=(0, theme.px(6)))
        #: `「词」`：最多两行（超出按**当前内容宽度**省略，见 :func:`quote_budget`）；
        #: ``wraplength`` 由 :meth:`_sync_wraplengths` 按**窗口实际内容宽度**设置
        #: （小窗 300 宽时不再是写死的 322 —— 那会让文字横着溢出被裁）。
        #: 选区 context **不在这里显示**（完整上下文在内存快照与库里）。
        self.quote_label = tk.Label(
            quote_box, text="", bg=theme.PANEL, fg=theme.TEXT_BODY,
            font=theme.font(9), anchor="w", justify="left",
            wraplength=theme.px(geo.PANEL_W),
        )
        self.quote_label.pack(fill="x")
        self.btn_action = widgets.FlatButton(quote_box, ACTION_TEXT, self._on_action,
                                             primary=True, font_size=9, padx=14, pady=5)
        self.btn_action.pack(anchor="w")

        # ---- ② 主题 chip 条：常驻在词卡上方，一眼看出「这一屏在看哪个主题」----
        # 第一个 chip 永远是「当前页」（跟随时显示本页的主题名 + 条数；这一页
        # 还没入库时显示占位「当前页 · 新主题」——**只显示，绝不写库**），后面
        # 是最近用过的主题（带条数），当前项 primary 高亮。chip 用 ``place``
        # 排在固定高度的宿主 Frame 里：placed 子控件不参与几何传播，所以行数
        # 变化只改宿主高度，不会把词卡区顶下去。
        self.topics_row = tk.Frame(self.list_page, bg=theme.PANEL)
        self.topics_row.pack(fill="x", padx=theme.px(geo.CONTENT_PAD),
                             pady=(theme.px(2), 0))
        self.topics_host = tk.Frame(self.topics_row, bg=theme.PANEL,
                                    height=theme.px(geo.CHIP_H))
        self.topics_host.pack(fill="x")
        self._topics_sig: tuple | None = None       # 上次铺 chip 的输入签名（幂等）
        self._topics_options: list | None = None    # 缓存主题清单（拖窗口不读库）
        self._topics_choices: list = []             # 完整清单（含排在 8 个名额外的）
        self._topics_page_scope: tuple | None = None  # 上次渲染时的 (batch_id, 已知?)
        self._topics_rows = 0                       # 上次占了几行
        self._topic_chips: list = []

        # ---- ③ 词卡区（吃掉剩余空间；不够时先压缩它）----
        head = tk.Frame(self.list_page, bg=theme.PANEL)
        head.pack(fill="x", padx=theme.px(geo.CONTENT_PAD), pady=(theme.px(2), 0))
        self.count_label = tk.Label(head, text="", bg=theme.PANEL, fg=theme.TEXT_FAINT,
                                    font=theme.font(7), anchor="e")
        self.count_label.pack(side="right")

        self.tags_area = ScrollArea(self.list_page, bg=theme.PANEL)
        self.tags_area.on_resize = lambda width: self._reflow_tags(width)
        self.tags_area.pack(fill="both", expand=True, padx=theme.px(geo.CONTENT_PAD),
                            pady=(theme.px(4), theme.px(2)))
        self.tags_area.bind_wheel(self.tags_area.canvas)
        self.tags_area.bind_wheel(self.tags_area.inner)

        #: 「显示更多」：MAX_TAGS 之后的分批渲染入口（第 61、121… 条也要能点到）
        self.more_row = tk.Frame(self.list_page, bg=theme.PANEL)
        self.btn_more = widgets.FlatButton(self.more_row, "显示更多", self.show_more_terms,
                                           font_size=8, padx=10, pady=2)
        self.btn_more.pack(side="left")
        self.btn_show_less = widgets.FlatButton(self.more_row, "收起列表",
                                                self.show_first_page, font_size=8,
                                                padx=8, pady=2)
        self.btn_show_less.pack(side="left", padx=(theme.px(4), 0))
        self.more_hint = tk.Label(self.more_row, text="", bg=theme.PANEL,
                                  fg=theme.TEXT_FAINT, font=theme.font(7), anchor="w")
        self.more_hint.pack(side="left", padx=(theme.px(6), 0))

        #: 空词表提示：**居中放在词卡视口正中**（``place(in_=…canvas)``），
        #: 既不占列表页的一行（旧实现会顶掉卡片区高度），也不会压到底部操作区。
        self.empty_hint = tk.Label(
            self.list_page, text="暂无词语", bg=theme.PANEL, fg=theme.TEXT_FAINT,
            font=theme.font(9), justify="center", anchor="center",
        )
        self._empty_placed = False

    # -------------------------------------------------------------- 详情页
    def _build_detail_page(self) -> None:
        """详情页布局：**底部优先**。

        pack 的分配顺序 = pack 调用顺序，所以先把「输入行 + 提示」按
        ``side="bottom"`` 放下去（小尺寸 300x320 / 长标题下优先保留），
        中间只留可滚动的释义与追问区域 —— 窗口被挤小时先压缩可滚动区，
        绝不会把输入框和发送按钮挤出可视范围。
        """
        self.detail_page = tk.Frame(self.panel_body, bg=theme.PANEL)

        # ---- ① 底部固定区（最先 pack：拿不到空间时宁可压缩中间区域）----
        self.input_row = tk.Frame(self.detail_page, bg=theme.PANEL)
        self.input_row.pack(side="bottom", fill="x", padx=theme.px(geo.CONTENT_PAD),
                            pady=(0, theme.px(2)))
        self.entry_input = tk.Entry(
            self.input_row, font=theme.font(9), bg=theme.PANEL, fg=theme.TEXT, bd=0,
            relief="flat", highlightthickness=1, highlightbackground=theme.BORDER,
            highlightcolor=theme.TEXT, insertbackground=theme.TEXT,
        )
        self.entry_input.pack(side="left", fill="x", expand=True, ipady=theme.px(3))
        self.btn_send = widgets.FlatButton(self.input_row, "发送", self._on_send,
                                           primary=True, font_size=8, padx=10, pady=3)
        self.btn_send.pack(side="left", padx=(theme.px(4), 0))
        # 用户**明确点击**输入框才激活窗口（拿键盘）；自动展开永远 NOACTIVATE
        self.entry_input.bind("<Button-1>", self._on_input_click)
        self.entry_input.bind("<Return>", lambda _e: self._on_send())
        self.entry_input.bind("<Escape>", lambda _e: self._on_collapse())
        self.entry_input.bind("<FocusOut>", lambda _e: self._on_input_blur())

        # ---- ② 追问块（**在释义区之前 pack**：空间不够时先压缩释义区）----
        # 旧顺序里对话区排在最后，pack 在空间不足时把它压成 0 高度 —— 用户发完
        # 追问「自己和模型的回答都看不见」就是这个原因（数据一直在库里）。
        # 现在对话块先拿到自己的高度，释义区兜住剩余空间（它同样是可滚动的）。
        self.chat_divider = widgets.divider(self.detail_page, pady=2)
        self.chat_divider.pack(side="bottom", fill="x")
        self.chat_head = tk.Frame(self.detail_page, bg=theme.PANEL)
        self.chat_head.pack(side="bottom", fill="x", padx=theme.px(geo.CONTENT_PAD))
        tk.Label(self.chat_head, text="追问", bg=theme.PANEL, fg=theme.TEXT_MUTED,
                 font=theme.tracking_label(8), anchor="w").pack(side="left")
        #: 追问的「重试」**只在回答真的失败之后**出现（不是常驻按钮行）：
        #: 重发最后一次提问，不重复记录那一问。
        self.btn_retry_chat = widgets.FlatButton(self.chat_head, "重试", self._on_retry_chat,
                                                font_size=8, padx=8, pady=1)
        self._chat_retry_packed = False

        self.chat_area = ScrollArea(self.detail_page, bg=theme.PANEL,
                                    height=theme.px(geo.DETAIL_AREA_MIN), fill_both=True)
        self.chat_area.pack(side="bottom", fill="x", padx=theme.px(geo.CONTENT_PAD),
                            pady=(theme.px(2), theme.px(2)))
        self.chat_text = tk.Text(
            self.chat_area.inner, wrap="word", font=theme.font(8), bg=theme.PANEL,
            fg=theme.TEXT_BODY, relief="flat", bd=0, highlightthickness=0,
            state="disabled", height=3, spacing1=1, spacing3=2,
        )
        self.chat_text.pack(fill="both", expand=True)
        self.chat_area.add_text(self.chat_text)

        # ---- ③ 顶部固定区：返回 + **条件出现**的唯一重试 ----
        self.detail_head = tk.Frame(self.detail_page, bg=theme.PANEL)
        self.detail_head.pack(side="top", fill="x", padx=theme.px(geo.CONTENT_PAD),
                              pady=(0, theme.px(2)))
        self.btn_back = widgets.FlatButton(self.detail_head, BACK_TEXT, self.show_list,
                                           font_size=8, padx=8, pady=2)
        self.btn_back.pack(side="left")
        self.detail_status = tk.Label(self.detail_head, text="", bg=theme.PANEL,
                                      fg=theme.TEXT_FAINT, font=theme.font(7), anchor="e")
        self.detail_status.pack(side="right")

        #: 解释失败 / 过期时才出现的一行：**只有一个**「重试」按钮。
        #: 常态不占位（旧版常驻的「重新解释 / 设置 API」两连排已删除）：
        #: 只解释这条已保存的词条，绝不重复记词、不加词频；解释在途时禁用。
        self.retry_row = tk.Frame(self.detail_page, bg=theme.PANEL)
        self.btn_retry_explain = widgets.FlatButton(self.retry_row, RETRY_TEXT,
                                                    self._on_retry_explain, font_size=8,
                                                    padx=10, pady=2)
        self.btn_retry_explain.pack(side="left")
        self._retry_packed = False

        # ---- ④ 中部可滚动区（pack 顺序最后 → 空间不足时先被压缩）----
        # ``wraplength`` 同样交给 :meth:`_sync_wraplengths`（随窗口宽度变化）
        self.term_title = tk.Label(self.detail_page, text="", bg=theme.PANEL, fg=theme.TEXT,
                                   font=theme.font(13), anchor="w", justify="left",
                                   wraplength=theme.px(geo.PANEL_W))
        self.term_title.pack(side="top", fill="x", padx=theme.px(geo.CONTENT_PAD),
                             pady=(theme.px(2), 0))
        #: 来源行：**只有一行**简短可读来源（标题或应用名）；没有就隐藏。
        #: URL / 完整窗口标题只在主界面看得到，浮窗里不再出现长串。
        self.detail_source = tk.Label(self.detail_page, text="", bg=theme.PANEL,
                                      fg=theme.TEXT_FAINT, font=theme.font(7), anchor="w",
                                      justify="left")
        self.detail_source.pack(side="top", fill="x", padx=theme.px(geo.CONTENT_PAD))
        self._detail_source_packed = True

        self.def_area = ScrollArea(self.detail_page, bg=theme.PANEL,
                                   height=theme.px(geo.DETAIL_AREA_MIN), fill_both=True)
        self.def_area.pack(side="top", fill="both", expand=True,
                           padx=theme.px(geo.CONTENT_PAD), pady=(theme.px(4), theme.px(2)))
        self.def_text = tk.Text(
            self.def_area.inner, wrap="word", font=theme.font(9), bg=theme.PANEL,
            fg=theme.TEXT_BODY, relief="flat", bd=0, highlightthickness=0,
            state="disabled", height=4, spacing1=1, spacing3=2,
        )
        self.def_text.pack(fill="both", expand=True)
        self.def_area.add_text(self.def_text)

        # ---- 右下角改尺寸手柄：向内收的 3 条灰斜线（**没有方形边框**）----
        # 旧实现是 ``Frame(highlightthickness=1)`` 贴在角上，方形边线正好压在
        # 圆角 region 的裁剪边缘，被裁掉之后就是用户看到的「右下黑角 / 缺口」。
        self.resize_grip = widgets.ResizeGrip(
            self.panel_body, size=geo.GRIP_SIZE,
            on_start=self._on_resize_start, on_motion=self._on_resize_motion,
            on_end=self._on_resize_end,
        )
        #: 兼容旧名字（回归断言 / 外部探针仍可用 ``panel.resize_handle``）
        self.resize_handle = self.resize_grip

    # ============================================================ 几何状态
    def _load_state(self) -> geo.PanelState:
        cfg = getattr(self.app, "config", None)
        loader = getattr(cfg, "get", None)
        if callable(loader):
            try:
                return geo.PanelState.load(loader)
            except Exception:  # pragma: no cover - 脏设置不该阻塞启动
                log.exception("读取面板位置失败，使用默认值")
        return geo.PanelState()

    def _initial_panel_size(self) -> tuple[int, int]:
        if self.state.panel_rect is not None:
            return (max(theme.px(geo.PANEL_MIN_W), int(self.state.panel_rect[2])),
                    max(theme.px(geo.PANEL_MIN_H), int(self.state.panel_rect[3])))
        return (theme.px(geo.PANEL_W), theme.px(geo.PANEL_H))

    def _dock_size(self) -> int:
        return theme.px(geo.DOCK_SIZE)

    def _min_size(self) -> tuple[int, int]:
        return (theme.px(geo.PANEL_MIN_W), theme.px(geo.PANEL_MIN_H))

    def _margin(self) -> int:
        return theme.px(geo.MARGIN)

    def _work_area(self, x: int | None = None, y: int | None = None) -> geo.Rect:
        """目标点所在显示器的工作区（支持负坐标副屏；查不到就退回主屏）。"""
        if x is None or y is None:
            anchor = self._anchor or self.state.dock_pos or (0, 0)
            x, y = int(anchor[0]), int(anchor[1])
        try:
            return tuple(int(v) for v in w32.work_area_for_point(int(x), int(y)))
        except OSError:  # pragma: no cover
            return (0, 0, int(self.win.winfo_screenwidth()),
                    int(self.win.winfo_screenheight()))

    def _panel_rect_at(self, x: int, y: int) -> geo.Rect:
        """以 ``(x, y)`` 为左上角、按当前面板尺寸夹进工作区。"""
        work = self._work_area(x, y)
        min_w, min_h = self._min_size()
        return geo.clamp_rect(x, y, max(min_w, int(self._panel_w)),
                              max(min_h, int(self._panel_h)), work,
                              min_w=min_w, min_h=min_h)

    def _visible_rect(self) -> geo.Rect:
        """窗口此刻的矩形；已经完全在**当前**工作区内就原样返回。

        「已经可见」不等于「还在屏幕里」：副屏被拔掉 / 分辨率变化之后，窗口可能
        停在已不存在的位置上。这里用 ``clamp_rect`` 做一次判定，越界才夹回来，
        位置本来就合法时绝不改变一个像素（否则每次换页都会挪动窗口）。
        """
        x, y, w, h = self._current_rect()
        work = self._work_area(x, y)
        clamped = geo.clamp_rect(x, y, w, h, work, min_w=self._min_size()[0],
                                 min_h=self._min_size()[1])
        return clamped if clamped != (x, y, w, h) else (x, y, w, h)

    def rect_for(self, mode: str, *, from_mode: str | None = None) -> geo.Rect:
        """当前状态下应该使用的矩形（已夹进工作区）。

        ``from_mode``：切换**之前**的状态（``_show`` 会先改 ``self.mode``；
        省略时按当前状态推断）。规则：

        * **状态没变** → 用窗口此刻真实占据的矩形（``_visible_rect``）：
          拖动 / 改尺寸之后 ``state.panel_rect`` 可能还是旧值，按保存值重算会
          让窗口突然跳回去；同时会检查它是否还在当前工作区内（拔掉副屏 /
          改分辨率后要能拉回来）；
        * 小方块 → 展开态 → 按小方块位置重新摆位（锚点是小方块的位置，
          直接当面板左上角会错位）；
        * 还没有锚点（首次显示）→ 用保存值 / 由小方块推导的默认位置。
        """
        was = self.mode if from_mode is None else from_mode
        if self._anchor is not None and was == mode:
            return self._visible_rect()
        work = self._work_area()
        if mode == MODE_PANEL:
            min_w, min_h = self._min_size()
            size = (max(min_w, int(self._panel_w)), max(min_h, int(self._panel_h)))
            return self.state.panel(work, size=size, dock_size=self._dock_size(),
                                    margin=self._margin(), gap=theme.px(geo.GAP),
                                    min_w=min_w, min_h=min_h)
        return self.state.dock(work, size=self._dock_size(), margin=self._margin())

    def _size(self) -> tuple[int, int]:
        if self.mode == MODE_PANEL:
            return (max(self._min_size()[0], int(self._panel_w)),
                    max(self._min_size()[1], int(self._panel_h)))
        return (self._dock_size(), self._dock_size())

    def _geometry_spec(self, x: int, y: int) -> str:
        """固定尺寸 + **绝对坐标**（负坐标显示器写 ``+-1920``）。"""
        w, h = self._size()
        return f"{int(w)}x{int(h)}+{int(x)}+{int(y)}"

    def _set_geometry(self, spec: str, x: int, y: int) -> bool:
        """下发 geometry，并**当次**按请求尺寸同步圆角 region / 描边。

        ``show_at`` 在 ``deiconify`` 之前调用本方法，所以**首次映射前**窗口
        形状就已经是圆角（不会先方角闪一帧）。region 只改形状：不映射、不激活、
        不移动，因此门控拒绝时（``show_at`` 提前 return，一次 geometry 都不下发）
        也不会有任何窗口动作。

        **为什么用请求尺寸而不是 ``winfo_width``**：刚请求 360x460 时
        ``winfo_width`` 还可能是上一次的 44 —— 直接跟着它走会把展开窗裁成
        44x44（用户看到的「一片空白 / 只剩角」）。这里先把请求尺寸记为待确认，
        region / 描边立刻按它走；Tk 随后用自己的 ``<Configure>``（或 idle 回执）
        确认实测尺寸，再由 :meth:`_on_self_configure` 幂等校正一次。

        ``_last_geometry`` 没变时 ``super()`` 返回 False：既不下发、也不动 region
        —— 门控轮询 / 重复显示因此是零 Win32 调用的。
        """
        changed = super()._set_geometry(spec, x, y)
        if changed:
            size = self._size()
            self._remember_stale(self._requested_size)
            self._remember_stale(self._measured_size)
            # 当前目标尺寸永远不算「旧尺寸」（dock ↔ panel 来回切时会重新变成目标）
            self._stale_sizes = [s for s in self._stale_sizes if s != tuple(size)]
            self._request_seq += 1
            self._requested_size = size
            self._measured_size = None
            self._render_border()
            self._sync_window_region()
            self._schedule_region_ack()
            self._sync_wraplengths()
            self._sync_detail_areas()
        return changed

    def _remember_stale(self, size: tuple[int, int] | None) -> None:
        """记下一个**已经作废**的窗口尺寸（迟到的 Configure / idle 会带着它回来）。

        与**当前目标尺寸**相同的值不算「旧尺寸」（纯移动 / 同尺寸重下发时
        ``_requested_size`` 就是新值）—— 否则会把当前这一代自己判成迟到事件。
        只保留最近 4 个，避免无限增长。
        """
        if not size:
            return
        item = (int(size[0]), int(size[1]))
        if item == self._size():
            return
        self._stale_sizes = [s for s in self._stale_sizes if s != item][-3:]
        self._stale_sizes.append(item)

    # ------------------------------------------------------------ 窗口圆角
    def _window_radius(self) -> int:
        """小方块 10px / 展开浮窗 12px（逻辑像素 → 设备像素）。"""
        if self.mode == MODE_PANEL:
            return theme.px(geo.PANEL_RADIUS)
        return theme.px(geo.DOCK_RADIUS)

    def _window_device_size(self) -> tuple[int, int]:
        """窗口**有效**的设备像素尺寸（region 与描边都必须用这一份）。

        判据顺序（修掉「刚请求 360x460、``winfo_width`` 还是旧的 44」）：

        1. 刚下发过 geometry 且 Tk 还没确认（``_requested_size``）→ 用**请求尺寸**：
           ``winfo_width`` 这时可能还是旧值，跟着它会把展开窗裁成 44x44；
        2. Tk 已通过自己 ``<Configure>`` / idle 确认过（``_measured_size``）→ 用实测值
           （系统钳制最小尺寸 / DPI 变化时就以它为准）；
        3. 其它情况（首次显示前）→ ``winfo_*`` 可用就用它，拿不到再用 :meth:`_size`。

        两种尺寸都用 ``theme.px`` 缩放后的**设备像素**，因此 region 不会在高 DPI 下
        切在错误的半径上。
        """
        if self._requested_size is not None:
            return self._requested_size
        measured = self._measured_size
        if measured is not None:
            return measured
        try:
            w = int(self.win.winfo_width())
            h = int(self.win.winfo_height())
        except (tk.TclError, TypeError, ValueError):  # pragma: no cover
            w = h = 0
        if w <= 1 or h <= 1:
            return self._size()
        return (w, h)

    def _on_self_configure(self, event) -> None:
        """Tk 确认了窗口**自己**的新尺寸（Configure）：按实测尺寸幂等同步。

        * ``event.widget is not self.win`` 一律忽略：子控件的 ``<Configure>``
          会沿 bindtags 冒泡到 Toplevel，不过滤的话面板里任何一个标签改尺寸
          都会被当成「窗口尺寸变了」；
        * **拒绝迟到事件**：尺寸正好等于「上一个请求 / 上一次实测」（即已经作废
          的 :attr:`_stale_sizes`）时直接返回 —— 那说明 Tk 还在处理旧请求，
          跟着它走会把刚按**请求尺寸**设好的 region 退回旧的；
        * 只做「记录实测尺寸 + 幂等重画描边 / 同步 region / 同步换行宽度」：
          **不改显隐**、不 ``update``、不 ``lift``、不重新映射（防频闪的硬约束）；
        * 尺寸与上一次实测一致时直接返回 —— 稳定后**零** Win32 调用。
        """
        if getattr(event, "widget", None) is not self.win:
            return
        try:
            w, h = int(event.width), int(event.height)
        except (TypeError, ValueError):  # pragma: no cover - 假事件没有尺寸
            return
        if w <= 1 or h <= 1:  # pragma: no cover - 未映射窗口的报告
            return
        size = (w, h)
        if size == self._measured_size:
            return
        if size in self._stale_sizes:
            return                       # 迟到事件：带着已作废的旧尺寸，保留 pending
        self._requested_size = None
        self._measured_size = size
        self._render_border()
        self._sync_window_region()
        self._sync_wraplengths()
        self._sync_detail_areas()

    def _schedule_region_ack(self) -> None:
        """请 Tk 在**空闲**时回执一次真实尺寸（Configure 之外的兜底）。

        窗口还没映射时 Configure 不一定来；这里只安排一次**只读**回执：
        不 ``update``、不 ``lift``、不改显隐。回执带上**请求序号**，同一代只排
        一次（拖动时每次 motion 都下发 geometry，不能堆出一串回调）。
        """
        seq = int(self._request_seq)
        if self._ack_pending_seq == seq:
            return
        after_idle = getattr(self.win, "after_idle", None)
        if not callable(after_idle):  # pragma: no cover - 极简假窗口
            return
        try:
            after_idle(lambda: self._on_idle_ack(seq))
        except tk.TclError:  # pragma: no cover - 控件已销毁
            return
        self._ack_pending_seq = seq

    def _on_idle_ack(self, seq: int | None = None) -> None:
        """idle 回执：Tk 已经处理过**这一代** geometry 才接受实测值。

        * 回执带着下发时的请求序号；序号已经不是当前一代 → 直接丢弃
          （迟到的回执绝不允许动这一代的 pending 与 region）；
        * 读到的尺寸仍然停在**旧尺寸**（已作废的历史尺寸）上 → 什么都不做：
          保留 ``_requested_size``，下一次 Configure / 回执再来确认；
          这正是「idle 读到旧 winfo 尺寸就退回旧 region」那个缺陷的反面。
        """
        if seq is not None and int(seq) == self._ack_pending_seq:
            self._ack_pending_seq = None
        if seq is not None and int(seq) != int(self._request_seq):
            return                       # 迟到回执：请求已经换了一代
        pending = self._requested_size
        if pending is None:
            return
        try:
            size = (int(self.win.winfo_width()), int(self.win.winfo_height()))
        except (tk.TclError, TypeError, ValueError):  # pragma: no cover
            return
        if size[0] <= 1 or size[1] <= 1:  # pragma: no cover - 未映射窗口
            return
        if size != pending and (size in self._stale_sizes or size == self._measured_size):
            return                       # Tk 还停在旧尺寸上：等 Configure，别清 pending
        self._requested_size = None
        self._measured_size = size
        self._render_border()
        self._sync_window_region()
        self._sync_wraplengths()
        self._sync_detail_areas()

    def _sync_window_region(self, *, force: bool = False) -> bool:
        """把当前模式/尺寸对应的圆角 region 应用到真实 HWND（幂等、失败安全）。

        * **幂等**：``(w, h, radius)`` 没变就一次 Win32 调用都不做
          （``show_at`` / ``_after_show`` / Configure 回执 / 门控轮询都会调到这里，
          值没变时零开销）；
        * **有效尺寸**：用 :meth:`_window_device_size`（待确认时是**请求尺寸**，
          Tk 确认后是实测尺寸），绝不用可能过期的 ``winfo_width`` 硬算；
        * **失败安全**：创建 / 设置失败一律**清掉 region 退回方形**，绝不因为
          圆角失败影响可用性；失败后清空缓存，下次几何变化会再试一次；
        * 不使用透明颜色键，正文不可能被打洞；
        * 旧浮层（SelectionBar / ExplainWindow）完全不碰这条路径。
        """
        try:
            hwnd = self.hwnd()
        except Exception:  # pragma: no cover - 窗口已销毁
            return False
        if not hwnd:
            return False
        width, height = self._window_device_size()
        radius = self._window_radius()
        key = (int(width), int(height), int(radius))
        if not force and key == self._region_key:
            return True
        apply_region = getattr(w32, "apply_round_region", None)
        if not callable(apply_region):
            return False
        try:
            ok = bool(apply_region(hwnd, key[0], key[1], key[2]))
        except Exception:
            log.exception("设置窗口圆角失败，退回方角")
            ok = False
        if ok:
            self._region_key = key
            return True
        self._region_key = None
        clear_region = getattr(w32, "clear_window_region", None)
        if callable(clear_region):
            try:
                clear_region(hwnd)
            except Exception:  # pragma: no cover
                log.exception("清空窗口 region 失败")
        return False

    def _token_value(self):
        return (self.mode, self.page, self._size(), self.entry_id, self.status,
                bool(self._chat_busy))

    # ------------------------------------------------------------ 持久化
    def save_geometry(self) -> bool:
        """把当前位置 / 尺寸写进 settings（拖动或改尺寸结束后调用）。"""
        cfg = getattr(self.app, "config", None)
        saver = getattr(cfg, "save_panel_state", None)
        try:
            if callable(saver):
                saver(self.state)
                return True
            setter = getattr(cfg, "set", None)
            if callable(setter):
                self.state.save(setter)
                return True
        except Exception:  # pragma: no cover - 设置写失败不影响交互
            log.exception("保存面板位置失败")
        return False

    # ============================================================ 显示 / 状态
    def display_allowed(self, *, already_visible: bool = False,
                        explicit: bool = False) -> bool:
        """面板的显示门控：**已映射的面板在软受限前台下必须保留**。

        宿主 ``App.overlay_display_allowed`` 对「新浮层」在软受限（本程序窗口
        在前台 / 桌面 / 无前台）下一律拒绝 —— 对旧的独立选区条是对的，但本面板
        是**同一个窗口**同时承担小方块与浮层：用户在面板里点标签、点输入框时
        前台正是它自己，如果这里也拒绝，就会先隐藏再显示（频闪），
        输入框的焦点也一起被收走（用户根本没法打字）。

        因此「窗口**已经映射**」时按 ``already_visible=True`` 询问宿主：
        硬阻断（游戏 / 全屏 / 暂停）依旧一律拒绝并收起，软受限下保留现状。
        """
        return super().display_allowed(
            already_visible=bool(already_visible or self.visible), explicit=explicit)

    def _show(self, mode: str, *, explicit: bool = False) -> bool:
        previous = self.mode
        self.mode = mode
        self._render_mode()
        rect = self.rect_for(mode, from_mode=previous)
        ok = self.show_at((rect[0], rect[1]), token=self._token_value(), explicit=explicit)
        if ok:
            self._after_show()
        return ok

    def _after_show(self) -> None:
        """显示之后的幂等刷新：位置/尺寸变了就同步回内存状态，并补齐窗口圆角。"""
        # 已经映射之后再确认一次 region / 描边（幂等：参数没变就零调用）
        self._render_border()
        self._sync_window_region()
        if self._anchor is None:
            return
        if self.mode == MODE_PANEL:
            self.state.panel_rect = (self._anchor[0], self._anchor[1],
                                     int(self._panel_w), int(self._panel_h))
        elif self.mode == MODE_DOCK:
            self.state.dock_pos = (self._anchor[0], self._anchor[1])

    def show_dock(self, *, explicit: bool = False, force: bool = False) -> bool:
        """显示/保持小方块。**绝不**自动展开面板（离开游戏恢复时用这条）。

        「已经可见」也**必须**过一遍实时门控：否则门控轮询 / 恢复路径会以为
        「小方块还在」，而它其实早该在游戏前台消失 —— 已显示的窗口要按
        **当下**判定保留或收起，不能凭上一次的状态想当然。
        """
        if self.closed_by_user and not (explicit or force):
            return False
        if explicit or force:
            self.closed_by_user = False
        if self.visible and self.mode == MODE_DOCK:
            if not self.display_allowed(already_visible=True, explicit=explicit):
                self.hide()          # 实时门控拒绝：只隐藏，零 deiconify
                return False
            return True
        return self._show(MODE_DOCK, explicit=explicit)

    def expand(self, *, explicit: bool = False, force: bool = False) -> bool:
        """展开面板（划选自动展开 / 用户点击 / 显式恢复）。"""
        if self.closed_by_user and not (explicit or force):
            return False
        if explicit or force:
            self.closed_by_user = False
        self.state.expanded = True
        return self._show(MODE_PANEL, explicit=explicit)

    def collapse_to_dock(self, *, reason: str = "", drop_selection: bool = False,
                         explicit: bool = False) -> bool:
        """折叠成小方块（用户点标题栏「折叠」/ 点小方块切换时调用）。

        .. note:: ``App`` 的软事件（点外部 / 前台切换 / 本程序窗口在前台）
           **不再**调用本方法：面板的显隐只由用户与硬阻断决定。

        * **保留**当前待处理词与已保存标签（用户可再展开继续操作）；
        * ``drop_selection=True``：前台窗口/标签页真的换了 → 旧选区作废；
        * 已经隐藏（门控 / 用户关闭）时不强行显示：门控优先。
        """
        if drop_selection:
            self._drop_pending_selection()
        if self.closed_by_user and not explicit:
            return False
        # 折叠 = 结束键盘输入：恢复 NOACTIVATE，下一次自动展开仍然不抢阅读焦点
        self._end_input()
        # 记住「现在是折叠态」：硬阻断避让结束后按这个状态恢复（不再一律展开 / 折叠）
        self.state.expanded = False
        if not self.visible:
            self.mode = MODE_DOCK
            self._render_mode()
            return False
        if self.mode == MODE_DOCK:
            return True
        return self._show(MODE_DOCK)

    def hide(self) -> bool:  # noqa: D102 - 语义同基类 + 记住「已隐藏」
        changed = super().hide()
        self.mode = MODE_HIDDEN
        self._end_input()
        self._render_mode()
        return changed

    def close_by_user(self) -> bool:
        """用户显式关闭（右上角「×」）：连小方块一起收起并在后台继续。

        * 只 ``withdraw`` + 记住 ``closed_by_user``：取词、在途解释与追问、
          门控轮询全部照常，**绝不**调用 ``quit`` / 销毁窗口；
        * 之后迟到结果 / 新划选 / 门控翻转都不会复活它（见 ``show_selection``
          / ``show_recorded`` / ``show_result`` / ``settle_after_gate``）；
        * 找回只能靠用户显式动作（``restore_dock(explicit=True)`` /
          ``restore()`` / 点小方块 / 全局快捷键）。
        """
        changed = super().close_by_user()
        self.mode = MODE_HIDDEN
        self._end_input()
        self._render_mode()
        return changed

    def restore_dock(self, *, explicit: bool = False) -> bool:
        """门控恢复 / 启动时确保小方块在屏幕上（用户关闭过的不自动找回）。"""
        return self.show_dock(explicit=explicit)

    def settle_after_gate(self) -> bool:
        """门控轮询结束后的**唯一**窗口动作（幂等；由 ``App`` 在翻转进允许时调用）。

        用户契约：「面板的显隐只由用户与硬阻断决定」。所以这里既不展开也不折叠：

        * **已经映射**（展开面板或小方块）→ 只做一次**实时**门控复核：被硬阻断
          （游戏 / 全屏 / 暂停）就收起；否则**原样保留** —— 展开的继续保持展开，
          不会因为「从设置 / 桌面回到普通窗口」被折叠成小方块（零 deiconify /
          withdraw / geometry）；
        * **还没有映射**且用户没有主动关闭过 → 恢复**之前**的状态：展开过
          （``state.expanded``）就重新展开（沿用保存的矩形，**位置尺寸不变**），
          否则只补回小方块（**绝不**把展开过的面板缩成 44x44）；
        * 用户关闭过（``closed_by_user``）→ 什么都不做，**不复活**。

        返回是否真的做了一次显示动作（只有「未映射 → 恢复」为 True）。
        """
        if self.visible:
            if not self.display_allowed(already_visible=True):
                self.hide()          # 硬阻断：只隐藏，零 deiconify
            return False
        if self.closed_by_user:
            return False
        if self.state.expanded:
            return self.expand()
        return self.show_dock()

    def mode_name(self) -> str:
        return self.mode

    def is_open(self) -> bool:
        return bool(self.visible and self.mode == MODE_PANEL)

    def is_dock(self) -> bool:
        return bool(self.visible and self.mode == MODE_DOCK)

    def toggle(self) -> bool:
        """小方块显式点击：展开 / 折叠。"""
        if self.visible and self.mode == MODE_PANEL:
            return self.collapse_to_dock(reason="toggle", explicit=True)
        return self.expand(explicit=True)

    # ------------------------------------------------------------ 选区角色
    def show_selection(self, selection, token: int, point=(0, 0)) -> bool:
        """一次新的划选：只挂内存快照 → 本地读标签 → **自动展开**（零写库零联网）。

        用户点过「×」关闭（``closed_by_user``）时**不自动展开**：× 的语义是
        「先收起来、继续在后台待命」，只有用户自己的显式动作（点小方块 /
        全局快捷键 / 找回入口）能把它找回来。快照仍然挂上去，所以用户一旦找回
        面板，这个词与「解释并记录」都还在。

        用户**开始划选** = 明确的「看我这一页」：手工浏览的历史主题在这里交还
        （:meth:`_follow_current_page`），面板自动切到**当前阅读页的主题视图**
        ——这一页还没有主题时 chip 条显示占位「当前页 · 新主题」（只显示、不写库，
        真正的主题等用户点「解释并记录」）。**零写库、零请求**。
        """
        self.selection = selection
        self.token = int(token)
        self._hint_hold = False          # 新选区：上一条守卫反馈让位（状态真的换了）
        self._hint_hold_status = None
        if self._consumed_token != self.token:
            self._consumed_token = None
        self._follow_current_page()      # 划选自动回到「这一页」的范围
        self._set_quote(selection)
        self.show_list(set_geometry=False)
        self.refresh_terms(force=True)   # 范围刚变过：强制重排（含主题 chip 条）
        return self.expand()

    def clear(self) -> None:
        """解除词条归属（新选区 / 词条被编辑）：只清内存状态，不写库、不联网。

        注意：与旧的 ``ExplainWindow.clear()`` 不同，这里**不隐藏窗口** ——
        面板是同一个窗口，隐藏再展开会闪一帧。
        """
        self.entry_id = None
        self.request_token = None
        self.result = None
        self.message = ""
        self.status = ""
        self._hint_hold = False
        self._hint_hold_status = None
        self._chat_busy = False
        self._chat_request_token = None
        self._chat_error = ""
        self._last_question = ""
        self._chat_turns = None          # 下一次渲染会按新词条重读（不再复用旧缓存）
        self._clear_input()
        self.page = PAGE_LIST
        self._render_mode()
        self._render_detail()

    def _drop_pending_selection(self) -> None:
        self.selection = None
        self._consumed_token = None
        self._set_quote(None)

    def _on_action(self) -> int | None:
        """唯一按钮：解释并记录（三道守卫与旧 SelectionBar 完全一致）。

        守卫挡下时**必须留下可见反馈**：早先这些分支直接 ``return None``，
        用户看到的就是「点了没反应」。
        """
        selection = self.selection
        if selection is None:
            self._set_hint("先划选一个词", guard=True)
            self._render_detail()
            return None
        if not self.visible or self.mode != MODE_PANEL:
            # 折叠成小方块 / 已被门控收起后，迟到的按钮回调不得再触发落库与解释
            self._set_hint("面板已收起，点小方块展开", guard=True)
            self._render_detail()
            return None
        if self._consumed_token == self.token or self._busy:
            self._set_hint("这次选词已经记录过了", guard=True)
            self._render_detail()
            return None
        self._busy = True
        try:
            self._consumed_token = int(self.token)
            return self.app.explain_and_record_selection(selection, anchor=self._anchor)
        finally:
            self._busy = False

    def reset_action_text(self) -> None:
        try:
            self.btn_action.configure(text=ACTION_TEXT)
        except tk.TclError:  # pragma: no cover
            pass

    def finish_action(self, *, page: str | None = None, reason: str = "action") -> bool:
        """唯一按钮消费掉当前选区之后的收尾（**共享窗口专用**）。

        旧的 ``SelectionBar`` 在这里隐藏自己；同一个窗口承担两个角色时**不能**
        这么做：隐藏 / 折叠再展开会让面板先缩成 44x44 又弹回 360x460 —— 频闪。

        因此本方法只做「消费」与「换页」，**一次 geometry 调用都不做**：

        * 当前逻辑窗口矩形（``_last_geometry``）保持原样；
        * 不 ``withdraw``、不 ``deiconify``、不折叠成小方块；
        * 待处理选区标记为已消费（迟到的重复回调不会再落库）；
        * 已经 ``_dismiss_selection_bar()`` 折叠过之后再被调用 → 什么都不做
          （不会把用户已经收起的窗口重新弹开）。
        """
        token = int(self.token) if self.selection is not None else None
        self._drop_pending_selection()
        # 顺序要紧：``_drop_pending_selection`` 会清掉 ``_consumed_token``，
        # 所以「已消费」必须在它之后重新写回，否则守卫就失效了。
        self._consumed_token = token
        if page is not None and self.page != page:
            self.page = page
            self._render_mode()
            self._render_detail()
        return True

    def consume_action(self, *, page: str | None = None) -> bool:
        """:meth:`finish_action` 的别名（调用方按语义二选一，行为完全一致）。"""
        return self.finish_action(page=page, reason="consume")

    def is_action_consumed(self) -> bool:
        """当前选区是否已经被「解释并记录」消费过。"""
        return self.selection is None or self._consumed_token == self.token

    def owns_point(self, x: int, y: int) -> bool:
        hwnd = w32.window_root_from_point(x, y)
        return bool(hwnd) and hwnd == self.hwnd()

    # ------------------------------------------------------------ 结果角色
    def show_recorded(self, selection, entry_id: int, anchor=None, *,
                      request_token: int | None = None, token: int | None = None,
                      status: str = "pending", message: str = "",
                      explicit: bool = False) -> bool:
        """「已记录 + 解释中 / 待解释」：把内容绑到该词条。

        展开与否**只由调用方是不是用户显式动作决定**：

        * ``explicit=True``（用户点了「解释并记录」/「重新解释」/找回入口）→
          切到详情页并展开，同时清除 ``closed_by_user``；
        * ``explicit=False``（后台更新）→ **只原地写内容**：已经展开的保持展开、
          小方块保持小方块、隐藏或被用户「×」关闭的**绝不复活**，
          ``closed_by_user`` 也不在这里清除。

        旧实现在这里无条件 ``closed_by_user = False`` + ``force=True`` 展开，
        后台调用因此能把用户刚关掉 / 折叠的窗口重新弹出来。
        """
        if request_token is None:
            request_token = token
        self.selection = selection
        if not message:
            message = "已记录，正在解释…"
        # 与 open_entry 同一条绑定路径：换词就清掉上一个词的对话现场与草稿
        self._bind_entry(int(entry_id), request_token=request_token, status=status,
                         message=message)
        self.page = PAGE_DETAIL
        self.refresh_terms()
        self._render_detail()
        if not explicit:
            return bool(self.visible)
        self.closed_by_user = False
        return self.expand(explicit=True, force=True)

    def show_result(self, entry_id: int, status: str, result, error_text: str = "",
                    *, request_token: int | None = None) -> bool:
        """写回属于本窗口本次请求的结果（entry_id + 请求 token 双闸门）。

        门控分两档，先于任何窗口动作：

        * **硬阻断**（游戏 / 全屏 / 用户暂停）→ 结果一到就**立即** ``hide()``
          （等不到下一次门控轮询），一次 ``deiconify`` 都不做；
        * **软受限**（本程序窗口 / 桌面 / 无前台）→ **不隐藏**，已显示的面板
          原样保留，只刷新内容。

        **绝不重新展开**：折叠 / 隐藏 / 用户关闭时只更新内存内容，
        下次展开时才看到 —— 迟到的结果不会把窗口弹回来。
        """
        blocked = not self.display_allowed(already_visible=True)
        if blocked:
            self.hide()          # 实时硬阻断：立即收起，零 deiconify
        if self.entry_id is None or int(self.entry_id) != int(entry_id):
            return False
        if request_token is not None:
            if self.request_token is None or int(request_token) != int(self.request_token):
                return False
        self.status = status
        if status == "ok" and result is not None:
            self.result = result
        elif status == "stale":
            self.result = None
        else:
            self.result = None
        self._render_detail()
        if blocked:
            return False
        if not self.visible or self.mode != MODE_PANEL or self.closed_by_user:
            return False
        return True

    #: 值得显式找回的待解释状态
    RESTORABLE_STATUS = ("pending_no_key", "pending", "error")

    def restore(self, anchor=None, *, explicit: bool = True) -> bool:
        """显式恢复：把同一条待解释词条的面板找回来（只显示，不联网、不写库）。"""
        if self.entry_id is None or self.closed_by_user:
            return False
        if self.status not in self.RESTORABLE_STATUS or not self.message:
            return False
        if self.visible and self.mode == MODE_PANEL:
            return True
        self.page = PAGE_DETAIL
        return self.expand(explicit=explicit, force=True)

    # ============================================================ 页面切换
    def show_list(self, *, set_geometry: bool = True) -> bool:
        """切到列表页（同一个窗口，**不重建、不重新映射**）。

        ``set_geometry`` 只表示「允许顺带校正位置」；页面切换本身从不隐藏 /
        重新映射，因此默认不会造成频闪。
        """
        self.page = PAGE_LIST
        self._render_mode()
        if set_geometry and self.visible and self.mode == MODE_PANEL:
            return self._show(self.mode)
        return True

    def show_detail(self, *, set_geometry: bool = True) -> bool:
        """切到详情页（同上，纯换页）。"""
        self.page = PAGE_DETAIL
        self._render_mode()
        if set_geometry and self.visible and self.mode == MODE_PANEL:
            return self._show(self.mode)
        return True

    def open_entry(self, entry_id: int, *, expand: bool = True) -> bool:
        """点标签：**纯本地读取**词条 + 该词的追问历史（绝不联网）。

        两个必须一起做的动作：

        * ``explain_status`` 原样映射成面板状态（``none`` 保留为 ``"none"``，
          不再被吞成空串），详情页据此决定解释入口显示「解释」还是「重试」；
        * **丢掉上一个选区**：面板手里可能还攥着 A 的快照，而 ``App._selection_for_entry``
          优先用窗口手里那份 —— 留着就会把 A 的 term / context 当成 B 提交。
          词条绑定本身只认 ``entry_id``，不需要旧选区。
        """
        db = getattr(self.app, "db", None)
        if db is None:
            return False
        row = db.get_entry(int(entry_id))
        if row is None:
            return False
        raw = str(row["explain_status"] or "").strip()
        status = raw if raw in STATUS_TEXT else "none"
        self._drop_pending_selection()
        # 切词统一走 _bind_entry：清掉上一个词的对话缓存 / 请求 token / 草稿，
        # 再按**当前词**重读历史与真实在途状态（否则 A 的对话会显示在 B 上）
        self._bind_entry(int(entry_id), request_token=None, status=status, message="")
        self.page = PAGE_DETAIL
        self._render_mode()
        self._render_detail()
        if expand:
            return self.expand(explicit=True)
        return True

    # ======================================================== 词条绑定（唯一入口）
    def _bind_entry(self, entry_id: int, *, request_token: int | None = None,
                    status: str | None = None, message: str | None = None) -> bool:
        """把面板绑定到某条词条：**open_entry / show_recorded / clear 共用**。

        这条路径要解决的缺陷是「``entry_id`` 换了、聊天缓存没换」：

        * **切词**（``entry_id`` 变化）→ 清空 ``_chat_turns`` / 请求 token /
          忙标记 / 错误 / 最后一次提问，并**清空输入框**（不做跨词草稿，
          简洁优先：A 的草稿绝不会被发送到 B）；然后再按新词重读历史；
        * **同词重开**（例如「重新解释」/ 恢复展开）→ 保留输入框内容，
          只把历史与**真实在途状态**同步回来（``App.chat_inflight``）。

        ``_render_chat`` 只在 ``_chat_turns is None`` 时才重读，所以这里必须
        **无条件**重读一次，否则 A 词的历史会一直挂在 B 词上。
        """
        entry_id = int(entry_id)
        changed = self.entry_id is None or int(self.entry_id) != entry_id
        self.entry_id = entry_id
        self._hint_hold = False         # 换 / 重开词条：守卫反馈让位给新状态
        self._hint_hold_status = None
        self.request_token = None if request_token is None else int(request_token)
        self.result = None
        if status is not None:
            self.status = str(status)
        if message is not None:
            self.message = str(message)
        self._chat_request_token = None
        self._chat_error = ""
        self._last_question = ""
        self._chat_echo = ""
        if changed:
            self._chat_turns = []          # 绝不把上一个词的对话显示在新词上
            self._clear_input()            # 上一个词的草稿不得发到这个词上
        self._chat_busy = self._entry_inflight(entry_id)
        self._reload_chat()
        self._detail_sig = None
        return changed

    def _entry_inflight(self, entry_id: int) -> bool:
        """这条词**真实**是否还有追问在途（宿主报告；不可用时保守 False）。"""
        probe = getattr(self.app, "chat_inflight", None)
        if not callable(probe):
            return False
        try:
            return bool(probe(int(entry_id)))
        except Exception:  # pragma: no cover - 宿主读库失败不该炸面板
            return False

    def _clear_input(self) -> None:
        try:
            self.entry_input.delete(0, tk.END)
        except tk.TclError:  # pragma: no cover - 控件已销毁
            pass

    # ================================================== 详情页的解释入口（按钮）
    def _explain_inflight(self) -> bool:
        """这条词是否**正在解释**（用于禁用「重新解释」，防重复请求）。

        优先问宿主的解释服务（``App.explain_service.is_inflight``）；拿不到服务
        时退回面板自己的状态（``pending`` = 已经发起、结果还没回来）。
        """
        if self.entry_id is None:
            return False
        service = getattr(self.app, "explain_service", None)
        probe = getattr(service, "is_inflight", None)
        if callable(probe):
            try:
                return bool(probe(int(self.entry_id)))
            except Exception:  # pragma: no cover - 服务内部异常不该炸面板
                return self.status == "pending"
        return self.status == "pending"

    def _on_retry_explain(self) -> bool:
        """详情页唯一解释入口：只解释**这条已保存的词条**。

        文案按状态是「解释」（还没有释义）或「重试」（失败 / 过期），两种都走
        ``App.retry_explain_entry``：**不重新记词、不加词频**（无 Key 时保存过的
        词条配好 Key 后直接点它）。解释在途时按钮禁用（``_render_detail`` 同步
        状态），这里再兜一层守卫；守卫反馈都是短句且被钉住，不会被状态刷新清掉。
        """
        if self.entry_id is None:
            self._set_hint("这条词已不在列表里", guard=True)
            self._render_detail()
            return False
        if self._explain_inflight():
            self._set_hint("正在解释中，请稍候…", guard=True)
            self._render_detail()
            return False
        retry = getattr(self.app, "retry_explain_entry", None)
        if not callable(retry):
            self._set_hint("暂时无法解释", guard=True)
            self._render_detail()
            return False
        try:
            started = bool(retry(int(self.entry_id), anchor=self._anchor))
        except TypeError:  # pragma: no cover - 宿主签名不带 anchor
            started = bool(retry(int(self.entry_id)))
        except Exception:
            log.exception("重新解释失败 #%s", self.entry_id)
            started = False
        if not started:
            self._set_hint("未能发起解释：可先在主界面配置 API", action="main", guard=True)
            self._render_detail()
            return False
        self.result = None
        self.status = "pending"
        self.message = "正在解释…"
        self._set_hint("正在解释，结果会显示在这里")
        self._render_detail()
        return True

    @staticmethod
    def _set_enabled(widget, value: bool) -> bool:
        """按钮禁用 / 启用（控件不支持时静默跳过；状态没变就不重复 configure）。"""
        try:
            if bool(widget.is_enabled()) == bool(value):
                return False
            widget.set_enabled(bool(value))
            return True
        except Exception:  # pragma: no cover - 假控件 / 已销毁
            return False

    # ============================================================ 追问
    def _on_input_click(self, _event=None) -> bool:
        """**显式路径**：用户点了追问输入框 → 允许窗口激活、拿键盘输入。"""
        return self.activate_for_input(self.entry_input)

    def _on_input_blur(self, _event=None) -> None:
        self._end_input()

    def _end_input(self) -> None:
        """结束键盘输入：恢复 NOACTIVATE（下一次自动展开仍然不抢焦点）。"""
        self._input_active = False
        try:
            self.release_activation()
        except Exception:  # pragma: no cover - 窗口可能已销毁
            pass

    def _on_send(self, _event=None) -> str:
        """用户点「发送」：只有这里才会发起追问请求（纯异步）。"""
        return self.send_question()

    def send_question(self) -> str:
        """返回 ``sent / busy / unavailable / empty / no_entry``（便于回归断言）。

        每一条**没有发出去**的分支都会给一句可见提示：按钮点下去必须有反馈，
        不允许「点了没反应」。
        """
        if not self.entry_id:
            self._set_hint("先选一个词再提问", guard=True)
            self._render_detail()
            return "no_entry"
        question = self.current_question()
        if self._chat_busy:
            # 忙态优先于空输入：用户连按回车时先知道「上一条还在回答中」
            self._set_hint("上一条还在回答中", guard=True)
            self._render_detail()
            return "busy"
        if not question:
            self._set_hint("请先输入问题", guard=True)
            self._render_detail()
            return "empty"
        ask = getattr(self.app, "ask_entry_question", None)
        if not callable(ask):
            self._set_hint("追问不可用", guard=True)
            return "unavailable"
        token = ask(int(self.entry_id), question)
        if token is None:
            inflight = getattr(self.app, "chat_inflight", None)
            self._chat_busy = bool(inflight(int(self.entry_id))) if callable(inflight) else False
            if self._chat_busy:
                self._set_hint("上一条还在回答中", guard=True)
                self._render_detail()
                return "busy"
            hint = ""
            getter = getattr(self.app, "chat_unavailable_hint", None)
            if callable(getter):
                hint = str(getter() or "")
            # 无 Key：**保留用户输入**，不伪造答案、不联网
            self._set_hint(hint or "这次追问没能发出")
            self._render_detail()
            return "unavailable"
        self._chat_busy = True
        self._chat_request_token = int(token)
        self._last_question = question
        self._chat_error = ""
        #: **本地回显**：请求已被接受 → 立刻把用户的问题画进对话区（库里的写入
        #: 是同步的，但 UI 不该依赖「下一次读库」才显示；读到了会自动去重）。
        self._chat_echo = question
        try:
            self.entry_input.delete(0, tk.END)
        except tk.TclError:  # pragma: no cover
            pass
        self._set_hint("")
        self._reload_chat()
        self._render_detail()
        return "sent"

    def current_question(self) -> str:
        try:
            return str(self.entry_input.get() or "").strip()
        except tk.TclError:  # pragma: no cover
            return ""

    def _on_retry_chat(self) -> bool:
        """追问失败后的「重试」：重发**最后一次**提问，不重复记录那一问。"""
        if not self.entry_id:
            self._set_hint("这条词已不在列表里", guard=True)
            self._render_detail()
            return False
        if self._chat_busy:
            self._set_hint("上一条还在回答中", guard=True)
            self._render_detail()
            return False
        retry = getattr(self.app, "retry_entry_question", None)
        if not callable(retry):
            return False
        token = retry(int(self.entry_id))
        if token is None:
            self._set_hint("没有可重试的提问", guard=True)
            self._render_detail()
            return False
        self._chat_busy = True
        self._chat_request_token = int(token)
        self._chat_error = ""
        self._reload_chat()
        self._render_detail()
        return True

    def on_chat_result(self, request_id: int, entry_id: int, status: str, content: str = "",
                       error_text: str = "") -> bool:
        """追问结果回到 UI 线程：只认「当前显示的词 + 当前请求 token」。

        与 :meth:`show_result` 同一条实时门控：硬阻断（游戏 / 全屏 / 暂停）下
        结果一到就立即 ``hide()``（零 deiconify），内容仍写进内存 / 库；
        软受限**不隐藏**，已显示的面板原样保留。
        """
        blocked = not self.display_allowed(already_visible=True)
        if blocked:
            self.hide()          # 实时硬阻断：立即收起，零 deiconify
        if self.entry_id is None or int(entry_id) != int(self.entry_id):
            return False
        if self._chat_request_token is not None and int(request_id) != int(self._chat_request_token):
            return False
        self._chat_busy = False
        self._chat_error = "" if status == "ok" else (error_text or "回答失败")
        self._chat_echo = ""          # 库里已经有用户那一问了，本地回显退场
        self._reload_chat()
        self._render_detail()
        return not blocked

    def _reload_chat(self) -> None:
        db = getattr(self.app, "db", None)
        if db is None or not self.entry_id:
            self._chat_turns = []
            return
        try:
            self._chat_turns = list(db.list_chat_turns(int(self.entry_id), limit=40))
        except Exception:  # pragma: no cover
            log.exception("读取追问历史失败")
            self._chat_turns = []

    # ============================================================ 渲染
    def _grip_place_kwargs(self) -> dict:
        """手柄的落点：贴右下角，但**整块**都在窗口内（不越过 1px 描边）。"""
        return {"relx": 1.0, "rely": 1.0, "anchor": "se",
                "width": theme.px(geo.GRIP_SIZE), "height": theme.px(geo.GRIP_SIZE)}

    def _render_mode(self) -> None:
        """按状态显隐「小方块 / 面板」并摆好改尺寸手柄（幂等）。

        ``outer`` 是唯一承担圆角安全留白的一层：dock 用半径 10 的留白（11），
        展开面板用半径 12 的留白（13）。**模式切换只更新这一份边距**
        （``pack_configure``），``panel_body`` / ``dock_body`` 铺满 ``outer``、
        不再重复留边 —— 内层再加一次就是双倍缩进，而且不透明的矩形内容会重新
        贴到窗口边缘、把 ``border_canvas`` 的圆弧盖住。
        """
        try:
            self._render_border()
            pad = self._body_padding()
            if pad != self._outer_pad:
                self.outer.pack_configure(padx=pad, pady=pad)
                self._outer_pad = pad
            if self.mode != self._packed_mode:
                if self.mode == MODE_PANEL:
                    self.dock_body.pack_forget()
                    self.panel_body.pack(fill="both", expand=True)
                    # 手柄是**无边框**的圆角内缩斜线（ResizeGrip），贴角摆放即可：
                    # 底色与面板一致，被 region 裁掉的部分看不出来。
                    self.resize_grip.place(**self._grip_place_kwargs())
                elif self.mode == MODE_DOCK:
                    self.panel_body.pack_forget()
                    self.resize_grip.place_forget()
                    self.dock_body.pack(fill="both", expand=True)
                else:
                    self.panel_body.pack_forget()
                    self.dock_body.pack_forget()
                    self.resize_grip.place_forget()
                self._packed_mode = self.mode
                if self.mode != MODE_PANEL:
                    self._packed_page = None
            self._render_pages()
            self._sync_wraplengths()
        except tk.TclError:  # pragma: no cover
            log.exception("切换面板状态失败")

    def _render_pages(self) -> None:
        """列表页 / 详情页二选一（同一个窗口里换页，幂等：状态没变不重复 pack）。"""
        if self.mode != MODE_PANEL:
            return
        if self.page == self._packed_page:
            return
        if self.page == PAGE_DETAIL:
            self.list_page.pack_forget()
            self.detail_page.pack(fill="both", expand=True)
        else:
            self.detail_page.pack_forget()
            self.list_page.pack(fill="both", expand=True)
        self._packed_page = self.page
        # 来源行只在**详情页**出现：列表页的「在看哪个主题」由常驻 chip 条表达
        # （能一眼换主题），同一件事不再说两遍。
        self._sync_source_row(self._source_wanted())
        self._sync_detail_areas()

    # ------------------------------------------------------- 实际内容宽度
    def _content_width(self) -> int:
        """内容区**实际**可用宽度（设备像素）：窗口宽 − 两侧圆角留白 − 行内留白。

        判据与 region 完全相同（优先**请求尺寸**，其次 Tk 确认过的实测尺寸，
        最后才是 ``winfo_width`` / 面板默认尺寸）—— 小窗被拖到 300 宽时，
        ``hint`` / ``quote`` / ``title`` 的 ``wraplength`` 必须跟着变成 ≈270，
        写死 322 会让文字横着溢出、右端被 region 裁掉。
        """
        width = 0
        for candidate in (self._requested_size, self._measured_size):
            if candidate:
                width = int(candidate[0])
                break
        if width <= 1:
            try:
                width = int(self.win.winfo_width())
            except (tk.TclError, TypeError, ValueError):  # pragma: no cover
                width = 0
        if width <= 1:
            width = self._size()[0] if self.mode == MODE_PANEL else theme.px(geo.PANEL_W)
        return max(theme.px(80),
                   width - 2 * self._body_padding() - 2 * theme.px(geo.CONTENT_PAD))

    def _sync_wraplengths(self) -> int:
        """按**实际内容宽度**同步三个多行标签的 ``wraplength``（幂等）。

        * ``hint_label`` 还要再减掉右侧 ``GRIP_SIZE`` 净空 —— 它的右边距已经把
          手柄宽度让出来了（见 ``_build_panel``），换行宽度不减就会算多 20px，
          文字最后一段跑到手柄底下 / 被 region 裁掉；
        * ``quote_label`` 的显示预算也跟着当前宽度重算（窗口拉宽后长词能多显示）；
        * 只改属性、且值没变就不 configure：门控轮询 / 重复显示不会造成重排闪动。
        """
        width = self._content_width()
        if width == self._wrap_width:
            return width
        self._wrap_width = width
        targets = {
            "hint_label": max(theme.px(40), width - theme.px(geo.GRIP_SIZE)),
            "quote_label": width,
            "term_title": width,
        }
        for name, value in targets.items():
            widget = getattr(self, name, None)
            if widget is None:
                continue
            try:
                if int(widget.cget("wraplength") or 0) == value:
                    continue
                widget.configure(wraplength=value)
            except (tk.TclError, TypeError, ValueError):  # pragma: no cover
                continue
        if self.selection is not None:
            self._set_quote(self.selection)      # 显示预算随宽度重算（只影响显示）
        # 主题 chip 条：chip 宽度按内容宽度算，窗口拉宽 / 缩窄后要重排
        # （``reload=False``：只重排不读库，拖动改尺寸不会一直查库；页面范围用
        #  上一次真渲染时记下的那份 —— 这里**不重新问**页面身份，否则 chip 会
        #  在拖窗口时莫名从「当前页 · X（n）」退化成「当前页 · 新主题」）。
        if getattr(self, "topics_host", None) is not None:
            scope = self._topics_page_scope
            if scope is not None:               # 还没铺过 chip：没有可重排的东西
                self._render_topics(scope[0], scope[1], reload=False)
        return width

    # --------------------------------------------- 详情页：释义 / 对话可见高度
    def _detail_window_height(self) -> int:
        """面板窗口的**有效**设备像素高度（判据与 region / 横向宽度完全一致）。"""
        for candidate in (self._requested_size, self._measured_size):
            if candidate:
                return max(1, int(candidate[1]))
        try:
            height = int(self.win.winfo_height())
        except (tk.TclError, TypeError, ValueError):  # pragma: no cover
            height = 0
        if height <= 1:
            height = self._size()[1] if self.mode == MODE_PANEL else theme.px(geo.PANEL_H)
        return max(1, height)

    def detail_area_heights(self) -> tuple[int, int]:
        """当前窗口高度下「释义区 / 对话区」各自的可见高度（设备像素，纯计算）。

        用户报告的「追问发出去，自己的问题和回答都看不见」不是数据问题
        （提问在 ``ChatService.submit`` 里已经**同步落库**、历史也读得到），
        而是竖向空间分配问题：旧布局把对话区放在 pack 顺序的最后，空间不够时
        它被前面的控件吃成 **0 高度**（320 高的最小面板必然如此，460 的默认面板
        在标题换行 / 出现「重试」行时也会如此）。这里改成**显式分配**：
        窗口高度 → 减圆角安全留白 → 减面板固定行预算 → 减详情页固定行预算 →
        :func:`app.ui.panel_geometry.detail_area_heights` 把剩余高度**全部**
        分给两块可滚动区，两块都保证至少 :data:`app.ui.panel_geometry.DETAIL_AREA_MIN`
        （空间实在不够时按比例缩，但绝不出现 0）。

        另一条（用户报告「解释的话像没说完一样」）：**一句追问都没有时不留空
        对话区**。旧布局永远按 55/45 分，没有对话时那 45% 白空着，释义正文
        只好挤在上半块里被裁掉半句话。现在 ``_chat_has_content()`` 为假时把
        整块剩余高度都给释义区、对话区收成 0；一旦有了问答再接回比例分配。
        """
        content = self._detail_window_height() - 2 * self._body_padding(MODE_PANEL)
        avail = content - theme.px(geo.PANEL_HEAD_H) - theme.px(geo.DETAIL_FIXED_H)
        if not self._chat_has_content():
            # 空对话区不占一个像素；释义区拿走**全部**剩余高度（但仍守
            # ``DETAIL_AREA_MIN`` 这条底线：面板被压到极小时也要看得见正文）。
            return max(theme.px(geo.DETAIL_AREA_MIN), int(avail)), 0
        return geo.detail_area_heights(avail, def_min=theme.px(geo.DETAIL_AREA_MIN),
                                       chat_min=theme.px(geo.DETAIL_AREA_MIN))

    def _chat_has_content(self) -> bool:
        """对话区里是否**真有**东西：历史 / 本地回显 / 正在回答。

        空的时候对话区被收成 0 高度（见 :meth:`detail_area_heights`）：
        用户明确不要「暂无对话」这种占位字，空区域也不该占着释义正文的高度。
        """
        if self._chat_busy or self._chat_turns:
            return True
        return bool(str(getattr(self, "_chat_echo", "") or "").strip())

    def _sync_detail_areas(self) -> bool:
        """把算出来的高度**显式**配置给两块 Canvas（幂等：值没变就不 configure）。

        为什么配置 Canvas 的 ``height`` 而不是给 Frame 加 ``minsize``：这两块都是
        :class:`ScrollArea`（Canvas + 内嵌 Frame + 自绘滑轨），Canvas 的请求高度
        就是整个区域的请求高度；显式写在 Canvas 上，pack 才会**按算好的值**分配，
        而不是按「谁先 pack 谁吃光」。释义区仍然 ``expand=True``（吃估算误差），
        对话区固定高度（永远不会被压没）。
        """
        if self.mode != MODE_PANEL:
            return False
        def_h, chat_h = self.detail_area_heights()
        changed = False
        for area, value in ((self.def_area, def_h), (self.chat_area, chat_h)):
            canvas = getattr(area, "canvas", None)
            if canvas is None:  # pragma: no cover - 旧探针没有 ScrollArea
                continue
            try:
                if int(canvas.cget("height") or 0) == int(value):
                    continue
                canvas.configure(height=int(value))
                changed = True
            except (tk.TclError, TypeError, ValueError):  # pragma: no cover
                continue
        return changed

    @staticmethod
    def _set_label(widget, text: str) -> bool:
        """只在文本变化时 configure（门控轮询不会造成闪动）。"""
        try:
            if str(widget.cget("text")) == str(text):
                return False
            widget.configure(text=text)
            return True
        except tk.TclError:  # pragma: no cover
            return False

    @staticmethod
    def _set_text_widget(widget, text: str) -> bool:
        body = str(text or "")
        try:
            if str(widget.get("1.0", tk.END)).rstrip("\n") == body:
                return False
            widget.configure(state="normal")
            widget.delete("1.0", tk.END)
            widget.insert("1.0", body)
            widget.configure(state="disabled")
            return True
        except tk.TclError:  # pragma: no cover
            return False

    # ------------------------------------------------------------ 列表渲染
    def page_scope(self) -> tuple[int | None, bool]:
        """当前阅读页面的词表范围：``(batch_id, page_known)``（**只读**，零写库）。

        * ``page_known=False``（没有 capture_service / 还没有任何来源信息，
          例如只读的面板探针）→ 保持「全部词语」的历史行为；
        * ``page_known=True, batch_id=None``（这一页还没保存过词）→ **空词表**，
          绝不退回全库（否则新页面里会看到上一篇文章的词）；
        * 否则显示这一页已有主题里的词。

        划选、翻页、门控轮询都会经过这里：全都只有内存读取，不建主题、
        不写 settings —— 真正的落库只发生在用户点「解释并记录」之后。
        """
        from ..capture_service import resolve_page_scope

        browsed = self.browse_scope()
        if browsed is not None:
            # 用户在这个浮窗里选了历史主题 / 让主界面侧栏选了主题 → 看那一组词。
            # 这**只影响浏览**：新词保存位置永远由来源页面自动归属决定。
            return int(browsed), True
        return self.page_topic_scope()

    def page_topic_scope(self) -> tuple[int | None, bool]:
        """**这一页自己**的主题（``(batch_id, page_known)``，无视手工浏览范围）。

        主题 chip 条的「当前页 · …」用它：浏览历史主题时，第一个 chip 仍然必须
        诚实地写着**本页**的主题，否则点开别的主题后它会被读成「本页 = 别的主题」。
        """
        from ..capture_service import resolve_page_scope

        return resolve_page_scope(getattr(self.app, "capture_service", None))

    # ------------------------------------------------------- 主题选择（C）
    def follow_page_change(self) -> bool:
        """前台换页（A→B→A）：浮窗**只交还状态，不改正在显示的内容**。

        换页是用户翻页的动作，不是「我要看新页面的词」的指令：屏幕上那个浮窗
        （词表，或某个词的详情 + 追问）保持原样。刷新的真实时机是用户**划选并
        记录**（``App._record_snapshot_once`` → ``_refresh_panel_terms`` →
        :meth:`refresh_terms`，以及显示某词详情时的 :meth:`show_recorded`）——
        页面身份在抬手那一刻就已采到（``note_foreground``），所以那次刷新取到的
        永远是划词时所在的页面。因此这里**一个控件都不重建**，只做状态准备：

        * **不再退出详情页 / 不再重排词表 / 不换顶栏主题** —— 用户没划词之前
          浮窗显示什么就一直是什么（旧实现「立刻改呈新页面词表」已废弃）；
        * **交还手工浏览范围**（``browse_scope`` 非 None 时）：换页等于结束手工
          浏览，下一次刷新的范围就是新页面；这一步纯内存，不改任何显示；
        * 收起展开着的主题选择（瞬时控件，留着会显示过期的「当前项」）；
        * **显隐 / 尺寸 / 位置一个像素都不动**（不 ``withdraw`` / ``deiconify`` /
          ``geometry``，也不折叠成小方块）；
        * **不动待处理选区**：迟到的换页事件绝不能清掉新页面的首选区 ——
          世代判定在 :meth:`App._hide_overlays_for_foreground_change` 里；
        * **只读**：零写库、零请求、不建主题（落库仍然只发生在用户点
          「解释并记录」之后的 ``resolve_batch``）。
        """
        changed = False
        if self._topic_open:
            self.toggle_topic_picker(False)
            changed = True
        if self._follow_current_page():
            changed = True
        if changed:
            # 只重排**控件**（标签条的高亮 / 「当前页 · …」的文案），词卡与详情
            # 一个像素不动：换页不改用户正在看的内容，这是本方法的核心契约。
            try:
                batch_id, page_known = self.page_topic_scope()
            except Exception:  # pragma: no cover - 页面身份读不到就退回占位
                batch_id, page_known = None, False
            self._render_topics(batch_id, page_known)
        return changed

    def browse_scope(self) -> int | None:
        """当前**浏览**的主题 id（纯内存；``None`` = 跟随当前阅读页）。

        与主窗口左侧栏共用宿主的同一份状态（``App.browse_scope``），因此两边
        永远显示同一组词：不管是在浮窗顶栏点的，还是在主界面侧栏点的。
        """
        getter = getattr(self.app, "browse_scope", None)
        if not callable(getter):
            return None
        try:
            value = getter()
        except Exception:  # pragma: no cover - 宿主状态读取失败不该炸面板
            return None
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):  # pragma: no cover
            return None

    def browse_topic_name(self) -> str:
        """正在浏览的主题名（没有 / 已删除 → 空串）。"""
        batch_id = self.browse_scope()
        db = getattr(self.app, "db", None)
        if batch_id is None or db is None:
            return ""
        try:
            row = db.get_batch(int(batch_id))
        except Exception:  # pragma: no cover
            return ""
        return str(row["name"] or "").strip() if row is not None else ""

    def topic_options(self) -> list[tuple[int | None, str]]:
        """内联主题清单的数据（**只读本地库**）：第一项永远是「跟随当前阅读页」。

        这是「管理视图」：**空主题（0 条词）也要列出来**（用户可能刚把一个主题
        的词删光，仍然要能点进去确认），所以这里不按条数过滤 —— 标签条要的是
        「归纳」，那份过滤在 :meth:`strip_topic_options` 里。
        """
        options: list[tuple[int | None, str]] = [(None, FOLLOW_TEXT)]
        for batch_id, label, _count in self._batch_choices():
            options.append((batch_id, label))
            if len(options) > TOPIC_MAX_ITEMS:
                break
        return options

    def strip_topic_options(self) -> list[tuple[int, str]]:
        """标签条用的主题清单：**跳过 0 条词的主题**（点进去只有空词表 = 噪声）。

        与 :meth:`topic_options` 的差别只有这一条；上限同样是 ``TOPIC_MAX_ITEMS``，
        但这里是「前 N 个**有词**的主题」，不会被空主题挤掉名额。
        """
        return self._capped_choices(self._batch_choices())

    @staticmethod
    def _capped_choices(choices: list[tuple[int, str, int]]) -> list[tuple[int, str]]:
        """从完整清单里挑出标签条要显示的（**只读纯函数**，便于测试与离线预览）。"""
        options: list[tuple[int, str]] = []
        for batch_id, label, count in choices:
            if count <= 0:
                continue
            options.append((batch_id, label))
            if len(options) >= TOPIC_MAX_ITEMS:
                break
        return options

    def _batch_choices(self) -> list[tuple[int, str, int]]:
        """本地库里的主题 → ``(batch_id, "名字（条数）", 条数)``（**只读**）。

        不过滤、不截断：调用方自己决定要不要空主题、要不要限量。
        """
        choices: list[tuple[int, str, int]] = []
        db = getattr(self.app, "db", None)
        lister = getattr(db, "list_batches", None)
        if not callable(lister):
            return choices
        try:
            rows = list(lister())
        except Exception:  # pragma: no cover - 读库失败不该炸面板
            log.exception("读取主题清单失败")
            return choices
        for row in rows:
            try:
                name = str(row["name"] or "").strip()
                count = int(row["entry_count"] or 0)
                batch_id = int(row["id"])
            except (IndexError, KeyError, TypeError, ValueError):  # pragma: no cover
                continue
            choices.append((batch_id, f"{name}（{count}）" if name else str(batch_id), count))
        return choices

    # ------------------------------------------------------- 主题 chip 条（常驻）
    def _render_topics(self, batch_id: int | None = None, page_known: bool = True,
                       *, reload: bool = True, row_w: int | None = None) -> int:
        """把主题归纳成常驻 chip 条（**幂等**；返回占了几行）。

        * 第一个 chip 永远是**当前页**：跟随时就是本页的主题名 + 条数；这一页
          还没入库时显示占位「当前页 · 新主题」—— 占位**只显示、绝不写库**，
          真正的主题只在用户点「解释并记录」时落库，之后这里自然换成真名；
        * 其余 chip = 最近用过的主题（:meth:`topic_options`，带条数），当前浏览
          的那个 ``primary`` 高亮；放不下的收进末尾「+N 个主题」chip（点开旧的
          完整清单，:meth:`toggle_topic_picker`）；
        * ``reload=False`` 表示**不读库**，用缓存的主题清单只做重排（拖窗口、
          改尺寸时会走这条路）：布局是纯计算，所以不存在「拖一下读一次库」；
        * ``row_w`` 显式给出可用宽度时用它排版（画布 resize 回调就是这么做的：
          回调参数才是**新**宽度，此时 ``winfo_width()`` 可能仍是旧值）。

        只影响显示：切主题走 :meth:`_on_topic_pick`（宿主那份唯一状态），
        **零写库、零请求**。
        """
        host = getattr(self, "topics_host", None)
        if host is None:                      # 面板还没建好（__init__ 早期）
            return 0
        if reload or self._topics_options is None:
            # 一次读库拿到**完整**清单：``self._topics_choices`` 供第一个 chip
            # 认名字（本页主题可能排在 8 个名额之外），``self._topics_options``
            # 是标签条真正显示的（只含有词主题 + 上限）。
            self._topics_choices = self._batch_choices()
            self._topics_options = self._capped_choices(self._topics_choices)
            self._topics_page_scope = (batch_id, bool(page_known))
        options = list(self._topics_options or [])
        current = self.browse_scope()
        if row_w is None:
            row_w = self._tags_width()
        row_w = max(theme.px(60), int(row_w))
        chip_px = abs(theme.device_px(geo.CHIP_PT))
        pad_x = theme.px(geo.CHIP_PAD_X)
        gap = theme.px(geo.CHIP_GAP)
        row_h = theme.px(geo.CHIP_H)

        follow = ("follow", self._follow_chip_text(
            batch_id, page_known, self._topics_choices or options))
        # ``strip_topic_options()`` 只含有词的主题（**没有**「跟随当前页」哨兵项），
        # 所以这里全部都是可点的主题 chip。
        topics = [(int(bid), str(text)) for bid, text in options]
        if page_known and batch_id is not None:
            # 当前页自己的主题**不再单列一个 chip**：第一个 chip 已经是它了
            # （避免同一个主题在标签条里出现两次），点它就是「从手工浏览回到跟随」。
            topics = [(bid, text) for bid, text in topics if bid != int(batch_id)]
        # 从「全铺」往回收：直到能在 CHIP_ROWS 行里放下，剩下的收进「+N 个主题」
        rows: list[list[tuple]] = []
        for keep in range(len(topics), -1, -1):
            rest = len(topics) - keep
            chips = [follow] + topics[:keep]
            if rest:
                chips.append(("more", f"+{rest} 个主题"))
            rows = self._wrap_chips(chips, row_w=row_w, chip_px=chip_px,
                                    pad_x=pad_x, gap=gap)
            if len(rows) <= max(1, int(geo.CHIP_ROWS)) or keep == 0:
                break
        rows = rows[:max(1, int(geo.CHIP_ROWS))]

        sig = (row_w, batch_id, bool(page_known), current,
               tuple((action, text) for row in rows for action, text, _w in row))
        if sig == self._topics_sig:
            return self._topics_rows
        self._topics_sig = sig
        self._topics_rows = len(rows)

        for chip in self._topic_chips:
            try:
                chip.destroy()
            except tk.TclError:  # pragma: no cover - 已销毁
                pass
        self._topic_chips = []
        try:
            host.configure(height=len(rows) * row_h + max(0, len(rows) - 1) * gap)
        except tk.TclError:  # pragma: no cover - 控件已销毁
            return 0
        for index, row in enumerate(rows):
            x = 0
            y = index * (row_h + gap)
            for action, text, width in row:
                active = (action == "follow" and current is None) or (
                    isinstance(action, int) and current is not None
                    and int(action) == int(current))
                chip = widgets.FlatButton(
                    host, text, (lambda a=action: self._on_topic_chip(a)),
                    primary=bool(active), font_size=geo.CHIP_PT,
                    padx=geo.CHIP_PAD_X, pady=2,
                )
                chip.place(x=x, y=y, width=width, height=row_h)
                self._topic_chips.append(chip)
                x += width + gap
        return len(rows)

    @staticmethod
    def _wrap_chips(chips: list[tuple], *, row_w: int, chip_px: int, pad_x: int,
                    gap: int) -> list[list[tuple]]:
        """贪心换行（**纯计算**，不建控件）：返回每行的 ``(action, text, width)``。

        宽度用 :func:`panel_geometry.chip_width` 估算（不量真实字体：假 Tk 环境
        没有字体，产品与离线预览共用同一套排布），另加一点余量，宁可早换行也
        不让 chip 互相压住。
        """
        rows: list[list[tuple]] = []
        for action, text in chips:
            width = geo.chip_width(str(text), font_px=chip_px, pad_x_px=pad_x, border=3)
            width = min(width + theme.px(3), max(theme.px(40), int(row_w)))
            if rows and sum(w for _a, _t, w in rows[-1]) + gap + width > int(row_w):
                rows.append([])
            if not rows:
                rows.append([])
            rows[-1].append((action, str(text), width))
        return rows

    def _follow_chip_text(self, batch_id: int | None, page_known: bool,
                          options: list[tuple]) -> str:
        """第一个 chip 的文字：这一页现在属于哪个主题（**只读**，不建主题）。

        ``options`` 可以是 ``(batch_id, 文案)`` 或 ``(batch_id, 文案, 条数)`` ——
        调用方传的是**完整**清单（本页主题排在 8 个名额之外时也要认得出名字）。
        """
        if not page_known:
            # 拿不到页面身份（例如只读探针）：退回「全库」的历史行为。
            # 文字刻意**不叫**「全部词语」：那个复选框已按用户要求删除，
            # 工具条契约测试（TestMainWindowToolbarBindings）禁止它复现。
            return "全部词条"
        if batch_id is None:
            # 这一页还没保存过词：占位。**不写库** —— 落库只发生在「解释并记录」
            return "当前页 · 新主题"
        for item in options:
            bid, text = item[0], item[1]
            if bid is not None and int(bid) == int(batch_id):
                return f"当前页 · {text}"
        return "当前页 · 本页主题"

    def _on_topic_chip(self, action) -> bool:
        """chip 点击：``"follow"`` 回跟随当前页、``"more"`` 开完整清单、int 切主题。"""
        if action == "follow":
            return self._on_follow_current_page()
        if action == "more":
            return self.toggle_topic_picker(True)
        try:
            return self._on_topic_pick(int(action))
        except (TypeError, ValueError):  # pragma: no cover - 不该发生
            return False

    def _on_follow_current_page(self) -> bool:
        """回到「跟随当前阅读页」（纯内存：零写库、零请求、零新建主题）。"""
        switched = self._follow_current_page()
        self._tag_scope = None
        self.toggle_topic_picker(False)
        self.refresh_terms(force=True)
        self._set_hint("已跟随当前阅读页" if switched else "正在跟随当前阅读页")
        return True

    def _follow_current_page(self) -> bool:
        """交还手工浏览范围（宿主那份唯一状态），返回**是否真的切换过**。

        刷选（:meth:`show_selection`）与换页（:meth:`follow_page_change`）都走
        这里：用户开始划词 = 明确的「看我这一页」，此时必须回到页面范围，否则
        词表还停在上次手工点的历史主题上。纯内存，不重建控件、不刷新界面
        （调用方决定要不要 :meth:`refresh_terms`）。
        """
        if self.browse_scope() is None:
            return False
        releaser = getattr(self.app, "release_browse_scope", None)
        if not callable(releaser):
            return False
        try:
            releaser()
        except Exception:  # pragma: no cover - 宿主状态异常
            log.exception("交还浏览范围失败")
            return False
        return True

    def toggle_topic_picker(self, want: bool | None = None) -> bool:
        """展开 / 收起内联主题清单（**不是**弹出菜单：不 popup、不 grab）。"""
        show = (not self._topic_open) if want is None else bool(want)
        if show == bool(self._topic_open):
            return bool(self._topic_open)
        self._topic_open = show
        if not show:
            self._sync_topic_caret()
            try:
                self.topic_picker.pack_forget()
            except tk.TclError:  # pragma: no cover
                pass
            return False
        self._render_topic_picker()
        self._sync_topic_caret()
        try:
            # 插在来源行**下面**、页面区上面：pack 顺序 = 空间优先级，
            # 清单拿到自己的高度，不会把下面的词表挤没。
            self.topic_picker.pack(fill="x", pady=(0, theme.px(2)),
                                   after=self.source_divider)
        except tk.TclError:  # pragma: no cover - 来源行已收起
            try:
                self.topic_picker.pack(fill="x", pady=(0, theme.px(2)),
                                       before=self.hint_label)
            except tk.TclError:
                self._topic_open = False
                return False
        return True

    def _sync_topic_caret(self) -> None:
        """主题行右侧 chevron 的朝向 = 清单是否展开（纯视觉，不改任何状态）。"""
        caret = getattr(self, "topic_caret", None)
        setter = getattr(caret, "set_direction", None)
        if callable(setter):
            try:
                setter("up" if self._topic_open else "down")
            except Exception:  # pragma: no cover - 窗口销毁 / 极简替身
                pass

    def _render_topic_picker(self) -> int:
        """重建清单行（本地数据，点一行 = 切浏览范围 + 一句短反馈）。"""
        widgets.clear_children(self.topic_picker)
        current = self.browse_scope()
        options = self.topic_options()
        for batch_id, label in options:
            active = (batch_id is None and current is None) or \
                (batch_id is not None and current == int(batch_id))
            item = tk.Label(
                self.topic_picker, text=("· " if active else "   ") + str(label),
                bg=theme.PANEL_ALT, fg=theme.TEXT if active else theme.TEXT_MUTED,
                font=theme.font(8), anchor="w", cursor="hand2", justify="left",
                wraplength=max(theme.px(80), self._content_width()),
            )
            item.pack(fill="x", padx=theme.px(geo.CONTENT_PAD), pady=theme.px(1))
            item.bind("<Button-1>", lambda _e, b=batch_id: self._on_topic_pick(b))
        return len(options)

    def _on_topic_pick(self, batch_id) -> bool:
        """点清单里的一行：切换浏览范围（**只影响显示**）、回到所选主题的词表页，给一句短反馈。"""
        setter = getattr(self.app, "set_browse_scope", None)
        if not callable(setter):
            self._set_hint("暂时无法切换主题", guard=True)
            return False
        try:
            setter(None if batch_id is None else int(batch_id))
        except Exception:
            log.exception("切换浏览主题失败")
        self._tag_scope = None            # 强制重算范围与分页
        self.refresh_terms(force=True)
        # 详情页可能还停在**上一个**主题的词条：换主题 = 看这一组词，回到词表页。
        # ``set_geometry=False``：只换页，不隐藏 / 不重新映射窗口、也不把面板
        # 自动折叠成小方块（展开态与位置原样保留）。
        self.show_list(set_geometry=False)
        self.toggle_topic_picker(False)
        name = self.browse_topic_name()
        self._set_hint(f"正在浏览《{name}》" if name else "已跟随当前阅读页")
        return True

    def _render_list(self, *, force: bool = False) -> bool:
        """刷新列表页（标签 + 来源/计数）：**只读本地库**，数据没变就一个控件都不重建。"""
        return self.refresh_terms(force=force)

    def refresh_terms(self, *, force: bool = False) -> bool:
        """读**本地**已保存词条并刷新标签（不联网）。数据没变就一个控件都不重建。

        标签**分页**：本页要 ``_tag_page`` 条（首次 = :data:`MAX_TAGS`），
        多取一条只为判断「还有没有更多」。范围（当前页面主题 / 浏览的主题 /
        全部词语）换了就把分页重置回第一页，因此范围变化不会让用户停在空的第二页。

        第 61、121 条这些超过一页的词条，靠列表底部的「显示更多」（每次 +60）
        分批渲染 —— 不是一次性把成千上万个控件塞进画布，滚动与换行不受影响。

        **判据包含卡片上真正显示的内容**（id + 词语 + 词频 + 一句话摘要）：
        解释完成后 ``one_line`` 变了，摘要行才会更新一次；数据没变时依旧
        一个控件都不重建（幂等），也**不重新映射窗口**（只重排卡片）。
        """
        rows: list = []
        total = 0
        batch_id, page_known = self.page_scope()
        #: 已知页面但还没有主题 → 空词表（**不查全库**）；没有来源信息才退回全库
        empty_page = bool(page_known and batch_id is None)
        scope = (batch_id, page_known)
        db = getattr(self.app, "db", None)
        if scope != self._tag_scope:
            # 换了范围（翻页 / 用户点了别的主题 / 首次知道页面）→ 分页回到第一页
            self._tag_scope = scope
            self._tag_page = MAX_TAGS
            force = True
        if db is not None and not empty_page:
            try:
                rows = list(db.list_entries(batch_id=batch_id, limit=int(self._tag_page) + 1))
            except Exception:  # pragma: no cover
                log.exception("读取本地词条失败")
                rows = []
            try:
                total = int(db.count_entries(batch_id))
            except Exception:  # pragma: no cover
                total = len(rows)
        has_more = len(rows) > int(self._tag_page)
        if has_more:
            rows = rows[: int(self._tag_page)]
        self._tag_total = max(int(total), len(rows))
        shown = len(rows)

        # 主题 chip 条：词表刷新必然可能改变「本页主题 / 主题条数」，所以放在
        # sig 早退**之前**（早退也要重排 chip；它自己还有一层幂等）。
        # chip 条的「当前页 · …」必须写**本页自己**的主题：浏览历史主题时
        # `page_scope()` 会返回被浏览的批次，那是 chip 高亮的事，不是本页的身份。
        page_batch, page_topic_known = self.page_topic_scope()
        self._render_topics(page_batch, page_topic_known)
        sig = tuple((int(r["id"]), str(r["term"]), int(r["repeat_count"]),
                     self._row_summary(r)) for r in rows)
        if not force and sig == self._terms_sig:
            self._render_more(shown)
            self._render_context(shown)
            return False
        self._terms_sig = sig
        self._render_tags(rows)
        self._render_more(shown)
        self._render_context(shown)
        return True

    def show_more_terms(self, *, step: int | None = None) -> int:
        """「显示更多」：本页 +step 条（有界分批渲染，一次只多建 step 个标签）。"""
        self._tag_page = int(self._tag_page) + int(step or MAX_TAGS)
        self.refresh_terms(force=True)
        return self._tag_page

    def show_first_page(self) -> int:
        """「收起列表」：回到第一页。"""
        self._tag_page = MAX_TAGS
        self.refresh_terms(force=True)
        return self._tag_page

    def shown_tag_count(self) -> int:
        """当前已经渲染出来的标签数（回归断言用）。"""
        return len(self.tags_area.children)

    def visible_term_total(self) -> int:
        """当前范围（当前页面主题 / 浏览的主题 / 全部词语）下**真实**的词条总数。"""
        return int(self._tag_total)

    def _render_more(self, shown: int) -> None:
        """「显示更多 / 收起列表 + 真实总数」这一行（幂等）。"""
        total = int(self._tag_total)
        has_more = shown < total
        need_row = has_more or shown > MAX_TAGS
        if has_more:
            self._set_label(self.more_hint, f"已显示 {shown} / 共 {total} 条")
        else:
            self._set_label(self.more_hint, f"已显示全部 {shown} 条")
        packed = getattr(self, "_more_packed", False)
        if need_row and not packed:
            self.more_row.pack(fill="x", padx=theme.px(geo.CONTENT_PAD), pady=(0, theme.px(2)))
            self._more_packed = True
        elif not need_row and packed:
            self.more_row.pack_forget()
            self._more_packed = False

    def _render_tags(self, rows) -> None:
        """把本地词条渲染成**两列等宽卡片**（只画**已有**的词，不画空占位框）。

        每张卡：Canvas 画的 8px 圆角暖米白卡面 + 词语 + （库里**已经有**
        ``one_line`` 时）一句短摘要 —— 摘要只读本地库，绝不额外发请求。
        点卡片任意位置 = 进同一个窗口的详情页；悬停只做柔和底色/边线。

        文字预算**按卡片实际宽度算**（:func:`panel_geometry.card_text_units`）：
        词语最多 :data:`panel_geometry.CARD_TERM_LINES` 行（宽度够就换行，只有
        真超长才省略），摘要固定一行 —— 面板拉宽时能显示更多字，缩窄时先省略，
        不会出现「明明有两行空间却被按字数切掉」。
        """
        self.tags_area.clear()
        width = self._tags_width()
        col_w = self._card_width(width)
        card_h = theme.px(geo.CARD_H)
        radius = theme.px(geo.CARD_RADIUS)
        pad_x = theme.px(geo.CARD_PAD_X)
        term_px = abs(theme.device_px(geo.CARD_TERM_PT))
        summary_px = abs(theme.device_px(geo.CARD_SUMMARY_PT))
        term_budget = geo.card_text_units(col_w, term_px, lines=geo.CARD_TERM_LINES,
                                         pad_x_px=pad_x)
        #: 摘要：一行放得下多少字由宽度决定，再兜一个字数上限（``SUMMARY_MAX_CHARS``
        #: 按「一个 CJK 字 = 2 单位」折算），避免窄面板上挤出第二行被裁。
        summary_budget = min(
            geo.card_text_units(col_w, summary_px, lines=1, pad_x_px=pad_x),
            2 * SUMMARY_MAX_CHARS,
        )
        for row in rows:
            eid = int(row["id"])
            full = str(row["term"] or "")
            shown = geo.clip_units(full, term_budget)
            card = widgets.TermCard(
                self.tags_area.inner, shown,
                summary=geo.clip_units(self._row_summary(row), summary_budget),
                width=col_w, height=card_h, radius=radius,
                command=(lambda i=eid: self.open_entry(i)),
            )
            self.tags_area.add(card)
        self._reflow_tags(width)

    @staticmethod
    def _row_summary(row) -> str:
        """卡片摘要：只取库里**已有**的一句话释义（没有就返回空串）。

        摘要只占一行，所以内部换行 / 连续空白一律折成单个空格：否则一个多行
        ``one_line`` 会在卡片上显示成「第一行 + 半行」。
        """
        try:
            raw = str(row["one_line"] or "")
        except (IndexError, KeyError, TypeError):  # pragma: no cover - 旧行结构
            return ""
        return " ".join(raw.split())

    def _tags_width(self) -> int:
        try:
            width = int(self.tags_area.canvas.winfo_width())
        except Exception:
            width = 0
        if width <= 1:
            width = max(theme.px(120), int(self._panel_w) - theme.px(30))
        return max(theme.px(80), width)

    def _card_width(self, width: int) -> int:
        """两列等宽卡片的列宽：``2 * 列宽 + 列间距 <= 可用宽度``（绝不重叠）。"""
        gap = theme.px(geo.CARD_GAP)
        return max(theme.px(90), (max(theme.px(120), int(width)) - gap) // 2)

    def _reflow_tags(self, width: int | None = None) -> int:
        """两列卡片排版（place 布局不参与几何传播，尺寸显式设置）。

        * 两列**等宽**、间距 ``CARD_GAP``（横向 = 纵向）、卡片高 ``CARD_H``；
        * 窗口缩放时只是重新 ``place`` + 改卡片尺寸（**不重建控件**，不闪）；
        * 内容高度驱动 ``scrollregion``（``set_extent``），滑轨随之更新；
        * 卡片数量少于 2 时右侧留空，不画任何空占位框。
        """
        width = int(width or 0) or self._tags_width()
        gap = theme.px(geo.CARD_GAP)
        card_h = theme.px(geo.CARD_H)
        col_w = self._card_width(width)
        cards = list(self.tags_area.children)
        for index, card in enumerate(cards):
            row, col = divmod(index, 2)
            try:
                card.set_size(col_w, card_h)
                card.place(x=col * (col_w + gap), y=row * (card_h + gap))
            except (tk.TclError, AttributeError):  # pragma: no cover - 已销毁的旧卡
                continue
        rows = (len(cards) + 1) // 2
        total = rows * card_h + max(0, rows - 1) * gap
        # 主题 chip 条同样按**这份**宽度重排：画布 resize 回调给的 width 才是新
        # 宽度（此刻 ``winfo_width()`` 可能还是旧值），chip 条换行必须跟着它，
        # 否则窗口拖窄后会留着上一档宽度的排布、chip 溢出右边界。
        host = getattr(self, "topics_host", None)
        scope = self._topics_page_scope
        if host is not None and scope is not None:
            self._render_topics(scope[0], scope[1], reload=False, row_w=width)
        self.tags_area.set_extent(width, total)
        return total

    def _render_context(self, count: int | None = None) -> None:
        """来源 / 计数（全部本地信息，**只有短主题名**）。

        * 顶部来源行：只显示「当前这一页的主题名」（宿主 ``panel_topic_line``，
          拿不到时才退回旧的 ``panel_context_line``），没有主题就把标签**和分隔线
          一起隐藏**（不留一行空白）—— 浏览器那串窗口标题 / 完整上下文不再进列表，
          它们只在主界面看得到；
        * 计数显示**真实总数**（``_tag_total``），不是本页渲染出来的控件数 ——
          用户必须能看出「一共 130 条，现在显示 60 条」，否则超过一页的词条
          看起来就像被丢掉了。
        """
        line = self._topic_line()
        self._set_label(self.source_label, line)
        self._sync_source_row(self._source_wanted(line))
        if self._topic_open:
            self._render_topic_picker()      # 清单开着：同步「当前项」标记
        total = int(self._tag_total) if count is None else max(int(count), int(self._tag_total))
        if total:
            self._set_label(self.count_label, f"{total} 条")
        else:
            self._set_label(self.count_label, "")
        self._sync_empty_hint(not total)

    def _topic_line(self) -> str:
        """来源行文字：正在浏览的主题名优先，其次宿主给的「当前页主题」。

        * 浏览历史主题时顶栏显示**它**（否则用户会以为看的是当前页）；
        拿不到主题名时回退 ``panel_context_line``；都没有 = 空串（整行隐藏）。
        """
        browsed = self.browse_topic_name()
        if browsed:
            return clip_text(browsed, TOPIC_MAX_CHARS)
        getter = getattr(self.app, "panel_topic_line", None) or \
            getattr(self.app, "panel_context_line", None)
        if not callable(getter):
            return ""
        try:
            return clip_text(str(getter() or ""), TOPIC_MAX_CHARS)
        except Exception:  # pragma: no cover
            return ""

    def _source_wanted(self, line: str | None = None) -> bool:
        """来源行要不要显示：**有内容 + 不在列表页**（列表页交给 chip 条）。

        列表页与详情页在同一个窗口里切换，所以这一行会跟着 :meth:`_render_pages`
        显隐；判断集中在这里，两条路径永远一致。
        """
        if self.page == PAGE_LIST:
            return False
        return bool(self._topic_line() if line is None else line)

    def _sync_source_row(self, want: bool) -> None:
        """顶部来源行（标签 + 分隔线）按需显隐：没有主题时**整块**消失。"""
        want = bool(want)
        packed = bool(getattr(self, "_source_packed", False))
        if want == packed:
            return
        try:
            if want:
                # ``before=hint_label``：反馈行永远在最后 pack（side=bottom），
                # 把来源行插回它前面，pack 顺序仍然是「顶栏 → 来源 → 页面」。
                self.source_row.pack(fill="x", padx=theme.px(geo.CONTENT_PAD),
                                     before=self.hint_label)
                self.source_divider.pack(fill="x", pady=theme.px(4),
                                         before=self.hint_label)
            else:
                # 没有主题就没有可切换的目标：清单一起收起（不留悬空入口）
                self.toggle_topic_picker(False)
                self.source_row.pack_forget()
                self.source_divider.pack_forget()
        except tk.TclError:  # pragma: no cover - 控件已销毁
            return
        self._source_packed = want

    def _sync_empty_hint(self, want: bool) -> None:
        """空词表提示：``place`` 到**词卡视口正中**（不占列表页的一行）。"""
        want = bool(want)
        if want == bool(getattr(self, "_empty_placed", False)):
            return
        try:
            if want:
                self._set_label(self.empty_hint, "暂无词语")
                self.empty_hint.place(in_=self.tags_area.canvas, relx=0.5, rely=0.5,
                                      anchor="center")
            else:
                self.empty_hint.place_forget()
        except tk.TclError:  # pragma: no cover - 控件已销毁
            return
        self._empty_placed = want

    def _set_quote(self, selection) -> None:
        """把当前选区的**当前词**写进底部（选区上下文不再出现在浮窗里）。

        * `「词」` 最多 **2 行**：预算按**当前内容宽度**算（:func:`quote_budget`），
          并且已经扣掉括号 / 省略号那几格 —— 显示串整体（含 ``「`` ``」``）都在
          预算内，超出按字数省略；``wraplength`` 由 :meth:`_sync_wraplengths` 同步；
        * 选区 context 零占位（既不是标签也不是提示行）；
        * 这里截断的**只是显示**：完整 term / context 仍在内存快照与库里，
          落库与送给 LLM 的内容一个字符都不少。
        """
        if selection is None:
            self._set_label(self.quote_label, "")
            return
        term = str(getattr(selection, "term", "") or "").strip()
        shown = clip_text(term, quote_budget(self._content_width())) if term else ""
        self._set_label(self.quote_label, f"「{shown}」" if shown else "")

    # ------------------------------------------------------------ 详情渲染
    def _render_detail(self) -> None:
        self._render_retry_row()
        self._render_chat_retry()
        # 忙态可见：上一条还在回答时「发送」直接禁用（回车路径仍有 busy 提示兜底）
        self._set_enabled(self.btn_send, not self._chat_busy)
        self._sync_detail_areas()
        if self.entry_id is None:
            self._set_label(self.term_title, "")
            self._set_label(self.detail_source, "")
            self._sync_detail_source(False)
            self._set_label(self.detail_status, "")
            self._set_text_widget(self.def_text, "")
            self._set_text_widget(self.chat_text, "")
            self._hint_auto("")
            return
        db = getattr(self.app, "db", None)
        row = db.get_entry(int(self.entry_id)) if db is not None else None
        if row is None:
            self._set_label(self.term_title, "词条已删除")
            self._set_label(self.detail_source, "")
            self._sync_detail_source(False)
            self._set_label(self.detail_status, "")
            self._set_text_widget(self.def_text, "词条已删除。")
            self._set_text_widget(self.chat_text, "")
            self._hint_auto("")
            return

        term = str(row["term"] or "")
        full_term = term.strip()
        # 来源只显示**一行**简短可读来源（标题优先，其次应用名）：
        # URL / 完整窗口标题不在浮窗里出现（主界面的「来源链接」才是它们的去处）
        source = str(row["source_title"] or row["source_app"] or "").strip()
        title_line = clip_text(term, TITLE_MAX_CHARS)
        self._set_label(self.term_title, title_line)
        self._set_label(self.detail_source, clip_text(source, SOURCE_MAX_CHARS))
        self._sync_detail_source(bool(source))
        self._set_label(self.detail_status, STATUS_TEXT.get(self.status, ""))
        # 标题被限字数时，**完整词语必须能在可滚动正文里查到**（长词条不能被吃掉）。
        # 正文本身是可滚动的，不受固定区高度限制，所以放在这里最稳妥。
        prefix = f"完整词语：{full_term}\n\n" if title_line != full_term else ""

        if self.status == "ok" and self.result is not None:
            body = self.result.as_text()
        elif self.status == "error":
            # 正文**只解释失败**：长操作说明不铺到这里（可执行的那一句在最底部
            # 提示行 / 唯一入口按钮上），完整错误仍在库与主界面。
            body = f"解释失败：{self._stored_error(row)}"
        elif self.status in ("pending", "pending_no_key"):
            body = self.message or "正在解释…"
        elif self.status == "stale":
            body = "词条改动过，需要重新解释。"
        elif str(row["explain_status"]) == "ok":
            # 本地重开：one_line + detail + **DB 里的例子**（不联网、不重复请求）
            body = _local_body(row)
        elif str(row["explain_status"]) == "error":
            body = f"上次解释失败：{self._stored_error(row)}"
        else:
            body = "尚未解释。"
        self._set_text_widget(self.def_text, prefix + body)
        self._render_chat()
        if self.status in ("error", "stale") and not self._explain_inflight():
            self._hint_auto(self._failure_hint(), action="")
        elif self.status == "pending_no_key":
            # 缺 API：只留一句「请在主界面配置 API」，**这句话本身可点**
            self._hint_auto(NO_KEY_HINT, action="main")

    @staticmethod
    def _stored_error(row) -> str:
        """库里的失败原因：只取第一行、按显示预算省略。

        完整错误（可能很长 / 带技术细节）留在库里与主界面，浮窗只放一句人话。
        """
        raw = str(row["explain_error"] or "").strip()
        first = raw.splitlines()[0].strip() if raw else ""
        return clip_text(first, FAILURE_MAX_CHARS) or "未知错误"

    def _failure_hint(self) -> str:
        """解释失败时提示行里那句话（短、可执行：点唯一的「重试」）。"""
        return f"解释失败，请点「{RETRY_TEXT}」"

    #: 还没有释义（库里 ``explain_status=none`` / 空串 / 无 Key 待解释）→ 显示「解释」
    EXPLAIN_NEEDED_STATUS = ("none", "", "pending_no_key")
    #: 解释失败 / 结果过期 → 显示「重试」
    RETRY_NEEDED_STATUS = ("error", "stale")

    def _explain_entry_label(self) -> str:
        """当前状态该显示哪个解释入口；空串 = 这一行不出现（已有释义）。"""
        if not self.entry_id:
            return ""
        if self.status in self.RETRY_NEEDED_STATUS:
            return RETRY_TEXT
        if self.status == "pending":
            return EXPLAIN_TEXT          # 解释在途：显示但禁用（防重复请求）
        if self.status in self.EXPLAIN_NEEDED_STATUS and not self._has_explanation():
            return EXPLAIN_TEXT
        return ""

    def _has_explanation(self) -> bool:
        """当前词条库里是否已经有释义（有就隐藏解释入口，不必重复请求）。"""
        db = getattr(self.app, "db", None)
        getter = getattr(db, "get_entry", None)
        if not callable(getter) or not self.entry_id:
            return False
        try:
            row = getter(int(self.entry_id))
        except Exception:  # pragma: no cover - 读库失败不该炸渲染
            return False
        if row is None:
            return False
        return bool(str(row["explain_status"] or "") == "ok"
                    and (str(row["one_line"] or "").strip()
                         or str(row["detail"] or "").strip()))

    def _render_retry_row(self) -> None:
        """唯一的解释入口按**状态**出现（常态不占位、不重复请求）。

        * 已有释义 → 整行隐藏；
        * 未解释（``none`` / 空 / ``pending_no_key``）→ 「解释」；
        * 失败 / 过期 → 「重试」；
        * 解释在途（``pending``）→ 显示但**禁用**，再点也不会重复发请求。

        两种文案走同一个 :meth:`_on_retry_explain` → ``App.retry_explain_entry``：
        只解释这条**已保存**的词条，绝不重复落库、不加词频。
        """
        label = self._explain_entry_label()
        need = bool(label)
        if need:
            self._set_label(self.btn_retry_explain, label)
        self._set_enabled(self.btn_retry_explain, not self._explain_inflight())
        packed = bool(getattr(self, "_retry_packed", False))
        if need == packed:
            return
        try:
            if need:
                self.retry_row.pack(fill="x", padx=theme.px(geo.CONTENT_PAD), pady=(0, theme.px(2)),
                                    before=self.def_area.frame)
            else:
                self.retry_row.pack_forget()
        except tk.TclError:  # pragma: no cover - 控件已销毁
            return
        self._retry_packed = need

    def _sync_detail_source(self, want: bool) -> None:
        """详情来源行按需显隐：没有来源就整行隐藏（不留空行）。"""
        want = bool(want)
        packed = bool(getattr(self, "_detail_source_packed", True))
        if want == packed:
            return
        try:
            if want:
                self.detail_source.pack(fill="x", padx=theme.px(geo.CONTENT_PAD),
                                        after=self.term_title)
            else:
                self.detail_source.pack_forget()
        except tk.TclError:  # pragma: no cover
            return
        self._detail_source_packed = want

    def _render_chat_retry(self) -> None:
        """追问的「重试」只在**上一次回答失败**之后出现（不是常驻按钮）。"""
        want = bool(self._chat_error) and bool(self.entry_id)
        packed = bool(getattr(self, "_chat_retry_packed", False))
        if want == packed:
            return
        try:
            if want:
                self.btn_retry_chat.pack(side="right")
            else:
                self.btn_retry_chat.pack_forget()
        except tk.TclError:  # pragma: no cover
            return
        self._chat_retry_packed = want

    def _render_chat(self) -> bool:
        """把**本地**追问历史 + 在途状态画进对话区（零联网、零写库）。

        三条硬要求：

        * 请求刚被接受（``ChatService.submit`` 已把用户那一问同步落库）时，
          即使历史读取还没跟上，也要用 ``_chat_echo`` **本地回显**用户的问题，
          并紧跟着一行「词典：正在回答…」——用户点完发送必须立刻看到东西；
        * 回答到达后重新读历史并**滚到最新一条**（新消息在末尾，不滚动就看不见）；
        * 失败 / 切词 / 迟到结果都不会把别人的问答画到当前词上（``_bind_entry``
          切词时清缓存，``on_chat_result`` 只认当前词 + 当前请求号）；
        * 一条都没有时**不写任何占位字**（用户明确不要「暂无对话」）：对话区
          被收成 0 高度，那部分高度全部让给释义正文。
        """
        turns = getattr(self, "_chat_turns", None)
        if turns is None and self.entry_id:
            self._reload_chat()
            turns = getattr(self, "_chat_turns", [])
        lines: list[str] = []
        asked: list[str] = []
        for row in (turns or []):
            role = str(row["role"] or "")
            status = str(row["status"] or "ok")
            content = str(row["content"] or "").strip()
            if role == "user":
                asked.append(content)
                lines.append(f"你：{content}")
            elif status == "ok":
                lines.append(f"词典：{content}")
            elif status == "error":
                lines.append(f"词典（回答失败）：{content or '请点「重试」'}")
            else:
                lines.append(f"词典：{content}")
            lines.append("")
        echo = str(getattr(self, "_chat_echo", "") or "").strip()
        if echo and echo not in asked:
            # 库里还读不到（或读到了但内容一致）时，本地补一条当前提问
            lines.append(f"你：{echo}")
            lines.append("")
        if self._chat_busy:
            lines.append("词典：正在回答…")
            lines.append("")
        changed = self._set_text_widget(self.chat_text, "\n".join(lines).strip())
        if changed:
            self._scroll_chat_to_end()
        # 有 / 没有对话决定对话区的高度（没有就收成 0，把高度让给释义正文）。
        self._sync_detail_areas()
        return changed

    def _scroll_chat_to_end(self) -> bool:
        """把对话区滚到**最新一条**（新问题 / 新回答必须立刻可见）。"""
        moved = False
        try:
            self.chat_text.see(tk.END)
            moved = True
        except (tk.TclError, AttributeError):  # pragma: no cover - 控件已销毁
            pass
        for target in (self.chat_text, getattr(self.chat_area, "canvas", None)):
            mover = getattr(target, "yview_moveto", None)
            if not callable(mover):
                continue
            try:
                mover(1.0)
                moved = True
            except tk.TclError:  # pragma: no cover
                continue
        return moved

    def _set_hint(self, text: str, *, action: str = "", guard: bool = False) -> bool:
        """写反馈行；``action="main"`` 表示**这句话本身可点**（打开主界面）。

        提示行是浮窗里唯一的「系统反馈」位置：按钮被守卫挡下、没配 Key、
        上一条还在回答 —— 都往这里写一句人话。可点时必须换手型光标并把
        字色提起，用户才知道这里能点（点了真的会打开主界面）。

        ``guard=True``：这是**守卫挡下**时写下的反馈（「先划选一个词」这类）。
        紧随其后的状态刷新常常会把提示清空 —— 那样用户看到的就是「点了没反应」。
        守卫反馈因此被**钉住**（见 :meth:`_hint_auto`），直到下一次真正的状态
        变化（新选区 / 新词条 / 状态翻转 / 有非空提示要写）才让位。
        守卫文案一律是短句，不铺操作说明。
        """
        self._hint_action = str(action or "")
        self._hint_hold = bool(guard) and bool(str(text or "").strip())
        self._hint_hold_status = str(self.status) if self._hint_hold else None
        changed = self._set_label(self.hint_label, str(text or ""))
        actionable = bool(self._hint_action) and bool(str(text or "").strip())
        try:
            self.hint_label.configure(cursor="hand2" if actionable else "",
                                      fg=theme.TEXT if actionable else theme.TEXT_MUTED)
        except tk.TclError:  # pragma: no cover - 控件已销毁
            return changed
        return changed

    def _hint_auto(self, text: str, *, action: str = "") -> bool:
        """状态渲染驱动的提示更新：**不把刚写下的守卫反馈立刻清掉 / 覆盖**。

        渲染想写空串（= 清空）而守卫反馈还钉着 → 跳过这一次；同一条词的**状态
        没变**时，渲染想写状态提示（例如「请在主界面配置 API」）也不许盖掉用户
        刚看到的那句守卫反馈。只有状态真的翻转（失败 / 过期 / 待解释…）或下一次
        真正的状态变化（新选区 / 新词条）才让位。
        """
        body = str(text or "").strip()
        if self._hint_hold:
            if not body or str(self.status) == str(self._hint_hold_status):
                return False
        self._hint_hold = False
        self._hint_hold_status = None
        return self._set_hint(text, action=action)

    def _on_hint_click(self, _event=None) -> bool:
        """点了可点的提示行：执行它承诺的动作（目前只有「打开主界面」）。"""
        if self._hint_action != "main":
            return False
        return self._on_open_main()

    def _on_open_main(self) -> bool:
        """顶栏「主界面」/ 可点提示行：**直接**调 ``App.open_main_window``。

        为什么不是菜单：浮窗是 ``WS_EX_NOACTIVATE`` 窗口，``tk_popup`` 那种
        抓取式菜单拿不到焦点，实机上点了像没反应（用户报告的「按钮没反馈」）。
        直接调宿主的公开方法：不 popup、不 grab、不动本窗口的显隐。

        拿不到宿主方法 / 宿主抛异常时给一句**可见**反馈（:data:`NO_MAIN_HINT`），
        绝不静默返回。
        """
        opener = getattr(self.app, "open_main_window", None)
        if not callable(opener):
            self._set_hint(NO_MAIN_HINT, action="main", guard=True)
            return False
        try:
            opener()
        except Exception:
            log.exception("打开主界面失败")
            self._set_hint(NO_MAIN_HINT, action="main", guard=True)
            return False
        return True

    def _is_topic_surface(self, widget) -> bool:
        """这一按是不是落在「顶部主题行」上（松手没移动 = 展开 / 收起主题清单）。

        chevron 是**组合控件**（普通 Python 包装类 + 内部 ``tk.Canvas``）：真实 Tk
        把 ``event.widget`` 报成那**块画布**，假 Tk 测试里则可能是包装对象本身。
        两者都算「主题行」，否则实机上点箭头不会展开清单。
        """
        if widget is None:
            return False
        caret = getattr(self, "topic_caret", None)
        return widget in (getattr(self, "source_row", None),
                          getattr(self, "source_label", None),
                          caret,
                          getattr(caret, "canvas", None))

    def _current_rect(self) -> geo.Rect:
        """面板**当前**占据的矩形（优先用真实 anchor，避免拖动时跳位）。"""
        if self._anchor is None:
            return self.rect_for(self.mode)
        w, h = self._size()
        return (int(self._anchor[0]), int(self._anchor[1]), int(w), int(h))

    def _drag_target(self, x: int, y: int, cursor: tuple[int, int] | None = None) -> geo.Rect:
        """拖动 / 改尺寸的目标矩形：窗口左上角 ``(x, y)`` 夹进**光标**所在工作区。

        ``cursor`` 必须传事件里的 ``x_root / y_root``：拖动时窗口左上角与光标
        隔着抓取偏移，用左上角判显示器会让跨屏拖动被上一块屏的边界挡住
        （见 :meth:`_work_area_for`）。省略时退回 ``(x, y)``（仅改尺寸等
        「光标就在角上」的场景）。

        为什么拖动过程中也要夹（旧实现只在松手时夹）：
        ``_current_rect`` 在拖动期间仍然报「上一次真正下发过的位置」，所以
        「拖出工作区 → 松手被猛地拽回来」会表现为**窗口跟着鼠标跑出屏幕、
        松手又跳回**，用户感觉就是「拖不动 / 不自由」。这里在 Motion 阶段就用
        同一个 :meth:`_work_area_for` + ``clamp_rect``，加上 :meth:`_safe_move`
        的**同位置零下发**，拖动全程稳定、松手不再跳。
        """
        cx, cy = (int(x), int(y)) if cursor is None else (int(cursor[0]), int(cursor[1]))
        if self.mode == MODE_DOCK:
            # 小方块只有 ``_dock_size()``（44）：夹取必须按**它自己**的尺寸。
            # 用面板最小值（300x320）会把它当成展开窗来夹，可停范围被压成
            # (工作区 - 300x320)：右下角永远够不到（贴不了屏幕右边 / 下边）。
            # 与 :meth:`PanelState.dock`（``min_w=size``）保持同一条公式。
            min_w = min_h = self._dock_size()
        else:
            min_w, min_h = self._min_size()
        w, h = self._size()
        return geo.clamp_rect(int(x), int(y), w, h, self._work_area_for(cx, cy),
                              min_w=min_w, min_h=min_h)

    def _work_area_for(self, x: int, y: int) -> geo.Rect:
        """目标点所在显示器的工作区（拖动期间按**光标**判显示器，不按窗口左上角）。

        旧实现用窗口左上角判显示器：把窗口从主屏拖到左副屏时，只要左上角还留在
        主屏里，夹取用的就是主屏工作区 —— 窗口一进入副屏就被主屏左边界挡住，
        表现为「拖不过去」。用光标位置判，跨屏拖动即时切换工作区。
        """
        try:
            return tuple(int(v) for v in w32.work_area_for_point(int(x), int(y)))
        except OSError:  # pragma: no cover
            return (0, 0, int(self.win.winfo_screenwidth()),
                    int(self.win.winfo_screenheight()))

    def _safe_move(self, x: int, y: int) -> bool:
        """移动窗口；**同一个矩形绝不重复下发**（拖动期间每个像素一次调用即可）。

        直接 ``_move_window`` 会在鼠标没动（或夹取后位置没变）时反复下发同样的
        geometry 串：每次都要走一遍 region / 描边 / 换行同步，拖动时既费又闪。
        """
        if self._drag_end_rect is not None and self._drag_end_rect[:2] == (int(x), int(y)):
            return False
        self._move_window(int(x), int(y))
        self._drag_end_rect = (int(x), int(y), self._size()[0], self._size()[1])
        return True

    def _on_drag_start(self, event) -> None:
        """拖动白名单上的按下：只有这些面绑了本方法（见 :meth:`_drag_surfaces`）。

        按钮 / 输入框 / 词卡 / 滚动条 / 主题清单选项 / 改尺寸手柄既没有这条绑定，
        事件也不会从它们冒泡到父 Frame —— 因此这里不需要任何「是不是交互控件」的
        闸门，点它们永远只是点它们自己。
        """
        self._drag_origin = getattr(event, "widget", None)
        self._drag_start_mode = self.mode
        self._drag_end_rect = None
        rect = self._current_rect()
        self._drag = geo.DragState(origin=(int(event.x_root), int(event.y_root)),
                                   start_rect=(rect[0], rect[1], rect[2], rect[3]))

    def _on_drag_motion(self, event) -> None:
        drag = self._drag
        if drag is None:
            return                       # 没有拖动在进行：忽略
        x, y = drag.update(int(event.x_root), int(event.y_root))
        if not drag.moved:
            return
        self._safe_move(*self._drag_target(x, y, (event.x_root, event.y_root))[:2])

    def _drag_idle(self, drag) -> bool:
        """这次按下算「点击」还是「拖动」。

        * 位移过阈值（``drag.moved``，含松手点的位移）→ 是拖动；
        * 窗口被别处展开了（旧实现会在用户按着标题带时被划选自动展开打断，
          拖动被当成点击）→ 也算拖动，**绝不**再触发展开 / 折叠；
        * 窗口在按住期间**改过尺寸 / 被收进小方块**（区域已经变了）→ 不当作点击，
          否则 360x460 的按下会配上 44x44 的松手，误折叠用户刚打开的面板。
        """
        if drag.moved:
            return False
        if self._drag_start_mode is not None and self.mode != self._drag_start_mode:
            return False
        if not self.visible:
            return False
        w, h = self._size()
        return (int(drag.start_rect[2]), int(drag.start_rect[3])) == (int(w), int(h))

    def _on_drag_end(self, event) -> None:
        drag, self._drag = self._drag, None
        origin, self._drag_origin = self._drag_origin, None
        start_mode, self._drag_start_mode = self._drag_start_mode, None
        _, self._drag_end_rect = self._drag_end_rect, None
        if drag is None:
            return
        # 松手点也喂给 DragState：**没有 Motion 的「按下 → 远处松手」**只有这一个
        # 样本，不更新就会被当成单击（小方块误展开、来源行误开主题清单）。
        cursor = (int(event.x_root), int(event.y_root))
        x, y = drag.update(*cursor)
        if self._drag_idle(drag):
            # 小方块：单击 = 展开；标题带：单击不做事（避免误触）；
            # 来源行 / 箭头：单击 = 展开 / 收起内联主题清单（不弹 grab 菜单）。
            # 拖动期间窗口形态被别处改过（start_mode != 当前模式）则什么都不做。
            if start_mode is not None and self.mode != start_mode:
                return
            if self.mode == MODE_DOCK:
                self.toggle()
            elif self.mode == MODE_PANEL and self._is_topic_surface(origin):
                self.toggle_topic_picker()
            return
        # 目标 = 按下时的矩形 + 松手位移（与 Motion 同一条公式），并按**光标**
        # 所在显示器夹取：松手不跳位、跨屏拖动即时换工作区。
        clamped = self._drag_target(x, y, cursor)
        self._move_window(clamped[0], clamped[1])
        if self.mode == MODE_PANEL:
            self.state.panel_rect = (clamped[0], clamped[1],
                                     int(self._panel_w), int(self._panel_h))
        else:
            self.state.dock_pos = (clamped[0], clamped[1])
        self.save_geometry()

    def _move_window(self, x: int, y: int) -> None:
        """移动 / 改尺寸的唯一入口：走 ``_set_geometry``，保持「已下发的完整串」。

        直接调 ``win.geometry()`` 会绕过 ``_last_geometry`` 记账，下一次
        ``show_at`` 就会以为尺寸没同步过而重复下发一次同样的 geometry
        （拖动 / 改尺寸之后的第一次换页会多一次无谓的窗口调用）。
        """
        try:
            self._set_geometry(self._geometry_spec(int(x), int(y)), int(x), int(y))
        except tk.TclError:  # pragma: no cover
            return

    def _on_resize_start(self, event) -> None:
        # 手柄的按下只属于改尺寸（手柄不在拖动白名单里，按下不会触发窗口拖动）
        self._drag_origin = None
        self._drag_start_mode = None
        self._drag_end_rect = None
        rect = self.rect_for(MODE_PANEL)
        self._drag = geo.DragState(origin=(int(event.x_root), int(event.y_root)),
                                   start_rect=rect)

    def _on_resize_motion(self, event) -> None:
        if self._drag is None or self.mode != MODE_PANEL:
            return
        work = self._work_area()
        area_w = max(1, work[2] - work[0])
        area_h = max(1, work[3] - work[1])
        w, h = self._drag.resize(int(event.x_root), int(event.y_root),
                                 min_w=self._min_size()[0], min_h=self._min_size()[1],
                                 max_w=area_w, max_h=area_h)
        self._panel_w, self._panel_h = int(w), int(h)
        rect = self.rect_for(MODE_PANEL)
        self._move_window(rect[0], rect[1])

    def _on_resize_end(self, event) -> None:
        drag, self._drag = self._drag, None
        if drag is None:
            return
        rect = self._current_rect()
        work = self._work_area(rect[0], rect[1])
        x, y, w, h = geo.clamp_rect(rect[0], rect[1], self._panel_w, self._panel_h, work,
                                    min_w=self._min_size()[0], min_h=self._min_size()[1])
        self._panel_w, self._panel_h = int(w), int(h)
        self._move_window(x, y)
        self.state.panel_rect = (x, y, int(w), int(h))
        self.save_geometry()
        self._reflow_tags()

    # ============================================================ 折叠 / 关闭
    def _on_collapse(self) -> bool:
        return self.collapse_to_dock(reason="user", explicit=True)

    def _on_close(self) -> bool:
        """标题栏右上角「×」：收起面板并在后台继续（**绝不退出程序**）。"""
        return self.close_by_user()

    def _render(self, text: str) -> None:  # noqa: D102 - FloatingWindow 抽象方法
        """本面板不接收 ``show_at(text=…)`` 的文本通道（内容由 ``_render_*`` 自己写）。

        保留这个空实现只为满足基类契约：面板的每一次显示都传 ``text=None``，
        因此这里**没有**任何控件被写坏的可能，也不再有旧的 ``tag_hint`` 死引用。
        """
