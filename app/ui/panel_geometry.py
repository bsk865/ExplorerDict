"""面板几何：**纯函数**，不碰 Tk、不碰 Win32（因此可以零窗口回归测试）。

三种矩形
--------
* 小方块（Dock）：默认 44x44，贴着工作区**右边缘**、垂直居中；
* 展开面板：默认 360x460，出现在小方块左侧；最小 300x320，最大不超过工作区；
* 两者都被 :func:`clamp_rect` 夹进「该点所在显示器的工作区」，因此：

  - **负坐标显示器**（左侧 / 上方副屏）会被正确保留（``+-1920`` 语义）；
  - 保存的位置如果落在**已经不存在的显示器**上，重新打开时会被拉回最近的
    显示器里（``MONITOR_DEFAULTTONEAREST`` 语义），不会「窗口在屏幕外找不回来」；
  - 尺寸永远不小于最小值、不大于工作区。

持久化
------
只存**数字**（settings 表里就是文本），读取时逐项容错（缺项/脏数据 → 用默认值）。
``expanded`` **不持久化**：重启后一律回到「折叠成小方块」的后台状态。
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: 折叠后的小方块边长（逻辑像素，实际用 theme.px() 缩放）
DOCK_SIZE = 44
#: 展开面板默认尺寸（逻辑像素）
PANEL_W = 360
PANEL_H = 460
#: 展开面板最小尺寸
PANEL_MIN_W = 300
PANEL_MIN_H = 320
#: 面板与屏幕边缘 / 与小方块之间的留白
MARGIN = 12
GAP = 8

# ---------------------------------------------------------------- 词表卡片
#: 词表**两列等宽卡片**（逻辑像素）：卡片高度、卡片间距（横向 = 纵向）、圆角半径。
#: 这三个值同时被 ``app.ui.reading_panel``（真实控件）与 ``tools/ui_preview.py``
#: （离线预览）使用，预览与实际界面因此不会漂移。
#:
#: 卡片高度按「词语最多 :data:`CARD_TERM_LINES` 行 + 一行摘要」定：
#: ``2 * CARD_PAD_Y`` + 词语两行 + 2px 间距 + 摘要一行。
#: **行高不能用字号代替**（见 :func:`text_line_px`）：96 DPI 下最紧 ——
#: ``2*9 + 2*21 + 2 + (18+2) = 82px``，留 2px 余量取 84 逻辑像素；
#: 144 DPI（scale=1.5）实得 126px，两行词语 + 一行摘要 + 上下留白占 113px ✓。
CARD_H = 84
CARD_GAP = 10
CARD_RADIUS = 8
#: 卡片内文字距卡片边缘的留白（必须 ≥ CARD_RADIUS，文字才不会盖住圆角）
CARD_PAD_X = 11
CARD_PAD_Y = 9
#: 卡片内「词语」字号（pt）与可选的一句话摘要字号（pt）
CARD_TERM_PT = 10
CARD_SUMMARY_PT = 8
#: 词语最多占几行：宽度够时**按宽度换行**（``TermCard`` 的 ``wraplength``），
#: 只有超过这些行才省略 —— 旧实现按固定 10 个字截断，把
#: ``Machine learning (ML) algorithms`` 这种常见长词切成了 ``Machine learning (…``。
CARD_TERM_LINES = 2

# ---------------------------------------------------------------- 主题标签条
#: 顶部常驻主题标签条（逻辑像素）：chip 高度、内边距、chip 间距、字号、最多几行。
#: 与词卡同理，产品与离线预览共用这一份常量。
CHIP_H = 24
CHIP_PAD_X = 8
CHIP_GAP = 6
CHIP_PT = 8
#: 最多铺几行；放不下的主题收进末尾的「+N 个主题」chip（点开旧的完整清单）。
#: 标签条吃掉的高度必须**远小于**词卡区：两行 chip ≈ 54 逻辑像素，而词卡区
#: 在最小面板（300x320）里也要放得下一行卡片。
CHIP_ROWS = 2

#: 词卡标签的显示预算：CJK / 全角字算 2 个**半角单位**、ASCII 等算 1 个。
#: 常见短英文全称（``Transformer`` = 11 单位）因此能完整显示，CJK 仍约 9 个字宽；
#: 超长一律省略，完整词在详情页正文里可查。产品（``reading_panel``）与离线预览
#: （``tools/ui_preview``）共用同一份常量与同一个 :func:`clip_tag`，两边不会漂移。
TAG_MAX_CHARS = 10
TAG_MAX_UNITS = 2 * TAG_MAX_CHARS - 2


def char_units(ch: str) -> int:
    """一个字符占的**显示单位**：CJK / 全角字 2，ASCII 等 1（纯函数）。"""
    code = ord(ch)
    if 0x1100 <= code <= 0x115F or 0x2E80 <= code <= 0xA4CF or \
            0xAC00 <= code <= 0xD7A3 or 0xF900 <= code <= 0xFAFF or \
            0xFE30 <= code <= 0xFE4F or 0xFF00 <= code <= 0xFF60 or \
            0xFFE0 <= code <= 0xFFE6:
        return 2
    return 1


def clip_units(text: str, max_units: int) -> str:
    """按**显示预算（半角单位）**截断任意文本（超出加省略号）。

    :func:`char_units` 的折算规则：CJK / 全角字算 2 个单位、ASCII 等算 1 个，
    因此「一行能放几个单位」可以只用**字号像素**估算，不需要任何字体量测
    （假 Tk 环境里没有真实字体，产品与离线预览也就能共用同一套排布）。
    """
    body = str(text or "").strip()
    budget = int(max_units)
    if budget <= 0:
        return body
    used = 0
    out: list[str] = []
    for ch in body:
        used += char_units(ch)
        if used > budget:
            return "".join(out) + "…"
        out.append(ch)
    return "".join(out)


def text_units(text: str) -> int:
    """整段文本的显示宽度（半角单位，纯函数）。"""
    return sum(char_units(ch) for ch in str(text or ""))


def units_to_px(units: int, font_px: int) -> int:
    """半角单位 → 像素：一个单位 = **半个 em**（``font_px / 2``，纯函数）。"""
    return max(1, int(round(int(units) * max(1, int(font_px)) / 2.0)))


#: 一行文字的近似行高 / 字号：Tk 的 ``linespace`` 实测是字号的 1.45~1.62 倍
#: （微软雅黑 13px → 21px、20px → 29px、16px → 23px），取上限当**保守**估计。
TEXT_LINE_SPACING = 1.62


def text_line_px(font_px: int) -> int:
    """一行文字需要的高度（设备像素，纯函数）。

    卡片内部的高度是按行算的（词语 :data:`CARD_TERM_LINES` 行 + 摘要一行），
    用字号本身当行高会**低估**：16px 的摘要实际要 23px，只留 16px 会把字的
    下半截裁掉。真实控件走 ``widgets.line_height()``（直接问 Tk 字体度量），
    这里是与离线预览共用的近似值（假 Tk / Pillow 也能算）。
    """
    size = max(1, int(font_px))
    return max(size, int(round(size * TEXT_LINE_SPACING)))


def line_units(width_px: int, font_px: int, *, pad_x_px: int = 0) -> int:
    """一行能放几个半角单位（**保守**估计：一个字最多占一个 em）。

    ``width_px`` / ``pad_x_px`` / ``font_px`` 全部是**设备像素**（同一个坐标系）。
    """
    inner = max(1, int(width_px) - 2 * int(pad_x_px))
    cell = max(1, int(font_px))          # 一个 em = font_px 像素 = 2 个单位
    return max(2, (inner * 2) // cell)


def card_text_units(width_px: int, font_px: int, *, lines: int = 1,
                    pad_x_px: int = 0) -> int:
    """卡片里某段文字的显示预算（半角单位）：``lines`` 行、每行 :func:`line_units`。

    额外扣掉约 1/8 作为安全余量：``W`` / ``M`` 这类宽拉丁字母的 advance 接近
    2 个单位，而折算是按 1 个单位估的 —— 宁可早一点省略，也不让第 3 行冒出来
    （卡片高度是固定的，多出来的行只会被裁掉）。
    """
    per_line = line_units(width_px, font_px, pad_x_px=pad_x_px)
    budget = per_line * max(1, int(lines))
    return max(2, budget - max(2, budget // 8))


def chip_width(text: str, *, font_px: int, pad_x_px: int, border: int = 2) -> int:
    """主题 chip 的像素宽度（纯函数）：文字单位宽度 + 左右内边距 + 描边。

    文字居中，宽度**宁可多给几个像素**也不少给（少给会把主题名裁掉）。
    """
    return int(units_to_px(text_units(text), font_px)) + 2 * int(pad_x_px) + \
        2 * int(border) + 4


def clip_tag(text: str, max_units: int = TAG_MAX_UNITS) -> str:
    """按**显示预算（半角单位）**截断词卡标签（超出加省略号）。

    旧实现按固定 10 个字符截断，把 ``Transformer`` 这种 11 个字母的常见短
    英文全称切成了 ``Transforme…``；按半角单位折算后常见英文全称可以完整显示，
    超长仍然省略。只影响**显示**：完整词在详情页正文里始终可查
    （``reading_panel._render_detail`` 的「完整词语：」一行）。

    :data:`TAG_MAX_UNITS` 是**不看宽度**的一行预算，因此它只是兜底：
    面板实际用 :func:`card_text_units` 按卡片宽度与 :data:`CARD_TERM_LINES`
    算预算（宽面板能显示更多字），本函数保留给离线预览与回归断言。
    """
    return clip_units(text, max_units)

# ---------------------------------------------------------------- 滚动滑轨
#: 词表右侧细滑轨宽度（逻辑像素）与滑块最小高度、滑块圆角半径
RAIL_W = 11
RAIL_MIN_THUMB = 18
RAIL_RADIUS = 5

# ---------------------------------------------------------------- 窗口圆角
#: 真实 Windows 窗口的圆角半径（逻辑像素）：小方块 10px、展开浮窗 12px
DOCK_RADIUS = 10
PANEL_RADIUS = 12
#: 窗口最外圈的 1px 描边（由 ``ReadingPanel`` 的圆角画布绘制，**不是**方形 Frame
#: 的 highlight —— 方形边线会被圆角 region 裁掉，看起来就像「圆角缺一块」）
WINDOW_BORDER = 1
#: 内容行的内部留白（逻辑像素）：圆角安全留白已经把内容从窗口边缘推进来
#: ``半径 + 描边``，内部行**不需要**再重复一次 ``padx=10`` 那种边距。
#: 产品（``reading_panel``）与离线预览（``tools/ui_preview``）共用这一个常量。
CONTENT_PAD = 2


def border_safe_inset(radius: int, border: int = WINDOW_BORDER) -> int:
    """圆角安全留白（逻辑像素）：**内容四边内缩 = 该模式圆角半径 + 描边宽度**。

    为什么必须内缩这么多
    --------------------
    窗口的圆角只由 Win32 region 裁出来，1px 描边是画在 region **以内**的圆角
    多边形。不透明的内容 Frame 是一个**矩形**：只要它铺到窗口边缘，圆角那一段
    圆弧（半径 12 的圆弧大部分落在 x/y > 1 的位置，45° 处约在 (3.5, 3.5)）
    就会被它整段盖住 —— 用户看到的就是「边线有、圆角没有」。内缩 ``半径 + 描边``
    是**保守但可证明**的上界：圆弧的任意一点都在 ``radius x radius`` 的角方块内，
    从边缘退开 ``radius + 描边`` 之后，矩形内容与整条描边**没有任何重叠**。

    本函数同时是离线预览的唯一来源：预览用同一个内缩画内容区，因此「预览里
    圆角可见」与「实机圆角可见」是同一套几何（离线画完整圆角本身**不构成**
    实机可见的证明，这里只是保证两边几何一致）。
    """
    return max(0, int(radius)) + max(0, int(border))

# ------------------------------------------------------- 改尺寸手柄（右下角）
#: 手柄控件的边长（逻辑像素，整个方块都是拖拽命中区）
GRIP_SIZE = 20
#: 可见斜线到窗口边缘的内缩：必须 ≥ ``WINDOW_BORDER``，且整组线都在圆角半径以内
GRIP_INSET = 3
#: 三条斜线距右下角的偏移（越大越靠内）；最大偏移 + 内缩必须 ≤ 圆角半径，
#: 否则斜线的端点会落到圆角之外被 region 裁掉（旧版方形手柄的「黑角」就是这个）
GRIP_STEPS = (4, 8, 11)


# ------------------------------------------------------- 小方块里的唯一图标
#: 小方块中央那个字（收起态唯一可见的图标）
DOCK_GLYPH = "探"
#: 小方块字号的**唯一来源**（pt）：产品（``reading_panel`` 的原生 Label）与离线
#: 预览（``tools/ui_preview.py`` 的 Pillow 绘制）共用它，两边不会漂移。
#:
#: 为什么不再是 13pt：小方块边长 44，四边各内缩 ``半径 + 描边`` = 11 之后，
#: 留给内容的只有 **22x22 设备像素**。13pt 在 96 DPI 下是 17px 字号、排版行高
#: ≈22px，再叠上 ``tk.Label`` 默认的 ``padx``/``pady``（各 1px）就会被窗口
#: region 裁掉一截。11pt（96 DPI → 15px，行高 ≈20px）配合
#: ``padx=0 / pady=0 / bd=0 / highlightthickness=0`` 才稳稳放得下，
#: ``tools/ui_preview.py`` 会在离线像素上复核墨迹确实落在 22x22 里。
DOCK_GLYPH_PT = 11


def dock_content_box(size: int = DOCK_SIZE, radius: int = DOCK_RADIUS,
                     border: int = WINDOW_BORDER) -> int:
    """小方块**内容可用区**边长（逻辑像素）：``边长 − 2 × (半径 + 描边)``。

    44 的小方块 → 22x22。字号（:data:`DOCK_GLYPH_PT`）、离线预览的居中框与
    回归断言都用这一个函数算，不各写一份数字。
    """
    return max(1, int(size) - 2 * border_safe_inset(radius, border))


def rounded_contains(x: float, y: float, w: float, h: float, radius: float) -> bool:
    """点是否落在 ``w x h``、圆角 ``radius`` 的圆角矩形内（**纯函数**）。

    与 ``CreateRoundRectRgn(0, 0, w+1, h+1, 2r, 2r)`` 的判定一致（右下角开区间），
    用来离线验证「手柄斜线 / 描边是否会被 region 裁掉」——不需要真实窗口。
    """
    x, y, w, h = float(x), float(y), float(w), float(h)
    if x < 0 or y < 0 or x > w or y > h:
        return False
    r = max(0.0, min(float(radius), w / 2.0, h / 2.0))
    if r <= 0.0:
        return True
    cx = min(max(x, r), w - r)
    cy = min(max(y, r), h - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r

# ------------------------------------------------- 详情页竖向空间分配（纯函数）
#: 详情页里两块可滚动区域（释义 / 追问对话）各自的最小可见高度（逻辑像素）：
#: 约 2.5 行 9pt 正文 —— 比这更矮就等于「看不见」，用户报告的「发了追问什么都
#: 看不到」正是旧布局把最后 pack 的对话区压成 0 高度造成的。
DETAIL_AREA_MIN = 34
#: 详情页**固定行**的高度预算（逻辑像素，含行距）：返回行 + 标题 + 来源 +
#: 底部输入行 + 「追问」标题 + 分隔线。取保守偏大的值：估多了只是少给滚动区
#: 几十像素（可滚动区仍然可见），估少了就会重新把某一块压没。
DETAIL_FIXED_H = 138
#: 面板层（顶栏 + 来源行 + 分隔线 + 底部反馈行）的高度预算（逻辑像素）
PANEL_HEAD_H = 88
#: 剩余空间里释义区占的比例（对话区拿剩下的）：释义通常更长，但对话必须有位置
DETAIL_DEF_SHARE = 0.55


def detail_area_heights(avail_h: int, *, def_min: int = DETAIL_AREA_MIN,
                        chat_min: int = DETAIL_AREA_MIN,
                        def_share: float = DETAIL_DEF_SHARE) -> tuple[int, int]:
    """把详情页中部**剩余高度**分给「释义区」与「对话区」（纯函数）。

    返回的 ``(def_h, chat_h)`` **恒等于** ``avail_h``（尽量不留空白），且两块都
    至少 1px；空间不足时按比例缩，但不会把任何一块直接压成 0 —— 旧布局靠 pack
    的隐式压缩，最后 pack 的对话区会被前面的控件吃光（高度 0 = 用户看不到自己
    的提问与回答）。两边各自都是可滚动 Canvas，所以「变小」只是少显示几行，
    不会丢内容。
    """
    avail = max(2, int(avail_h))
    dmin = max(1, int(def_min))
    cmin = max(1, int(chat_min))
    if avail <= dmin + cmin:
        if avail == 1:
            return 1, 1
        d = int(round(avail * float(def_share)))
        d = max(1, min(d, avail - 1))
        return d, avail - d
    extra = avail - dmin - cmin
    d = dmin + int(round(extra * float(def_share)))
    d = max(dmin, min(d, avail - cmin))
    return d, avail - d


#: settings 键（全部在 ui.panel.* 下）
K_DOCK_X = "ui.panel.dock_x"
K_DOCK_Y = "ui.panel.dock_y"
K_PANEL_X = "ui.panel.x"
K_PANEL_Y = "ui.panel.y"
K_PANEL_W = "ui.panel.w"
K_PANEL_H = "ui.panel.h"

Rect = tuple[int, int, int, int]


def parse_int(value, default: int | None = None) -> int | None:
    """宽松解析 settings 里的整数（空串 / 脏数据 → default）。"""
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return default


def clamp_rect(x, y, w, h, work_area: Rect, *, min_w: int, min_h: int) -> Rect:
    """把矩形夹进工作区：尺寸先收敛到 [min, 工作区]，再保证**完全可见**。"""
    left, top, right, bottom = (int(v) for v in work_area)
    avail_w = max(1, right - left)
    avail_h = max(1, bottom - top)
    ww = min(max(int(parse_int(w, min_w)), int(min_w)), avail_w)
    hh = min(max(int(parse_int(h, min_h)), int(min_h)), avail_h)
    xx = min(max(int(parse_int(x, left)), left), max(left, right - ww))
    yy = min(max(int(parse_int(y, top)), top), max(top, bottom - hh))
    return (int(xx), int(yy), int(ww), int(hh))


def default_dock_pos(work_area: Rect, *, size: int = DOCK_SIZE, margin: int = MARGIN) -> tuple[int, int]:
    """默认小方块位置：工作区右边缘、垂直居中。"""
    left, top, right, bottom = (int(v) for v in work_area)
    x = right - int(size) - int(margin)
    y = top + max(0, (bottom - top - int(size)) // 2)
    return (x, y)


def default_panel_rect(work_area: Rect, dock_pos: tuple[int, int], *,
                       size: tuple[int, int] = (PANEL_W, PANEL_H),
                       dock_size: int = DOCK_SIZE, margin: int = MARGIN,
                       gap: int = GAP, min_w: int = PANEL_MIN_W,
                       min_h: int = PANEL_MIN_H) -> Rect:
    """默认展开位置：出现在小方块**左侧**，两者垂直中心对齐。"""
    left, top, right, bottom = (int(v) for v in work_area)
    w, h = int(size[0]), int(size[1])
    x = int(dock_pos[0]) - w - int(gap)
    if x < left + int(margin):
        # 小方块贴在屏幕左缘时（例如左副屏），改为出现在它右侧
        x = int(dock_pos[0]) + int(dock_size) + int(gap)
    y = int(dock_pos[1]) + int(dock_size) // 2 - h // 2
    return clamp_rect(x, y, w, h, work_area, min_w=min_w, min_h=min_h)


@dataclass
class PanelState:
    """面板的**可视状态**（位置 + 尺寸），可持久化。"""

    dock_pos: tuple[int, int] | None = None
    panel_rect: Rect | None = None
    #: 本次会话是否已经从小方块展开过（不持久化）
    expanded: bool = False

    # ------------------------------------------------------------- 读取
    @classmethod
    def load(cls, get) -> "PanelState":
        """:param get: ``key -> str|None``（通常是 ``Config.get``）。"""
        state = cls()
        dx = parse_int(get(K_DOCK_X))
        dy = parse_int(get(K_DOCK_Y))
        if dx is not None and dy is not None:
            state.dock_pos = (dx, dy)
        px_, py = parse_int(get(K_PANEL_X)), parse_int(get(K_PANEL_Y))
        pw, ph = parse_int(get(K_PANEL_W)), parse_int(get(K_PANEL_H))
        if None not in (px_, py, pw, ph):
            state.panel_rect = (px_, py, pw, ph)
        return state

    def save(self, set_) -> None:
        """:param set_: ``(key, value) -> None``（通常是 ``Config.set``）。"""
        if self.dock_pos is not None:
            set_(K_DOCK_X, str(int(self.dock_pos[0])))
            set_(K_DOCK_Y, str(int(self.dock_pos[1])))
        if self.panel_rect is not None:
            x, y, w, h = (int(v) for v in self.panel_rect)
            set_(K_PANEL_X, str(x))
            set_(K_PANEL_Y, str(y))
            set_(K_PANEL_W, str(w))
            set_(K_PANEL_H, str(h))

    # ------------------------------------------------------------- 派生
    def dock(self, work_area: Rect, *, size: int = DOCK_SIZE, margin: int = MARGIN) -> Rect:
        """小方块矩形（无保存值 / 越界时回落到默认右边缘位置）。"""
        sx = sy = None
        if self.dock_pos is not None:
            sx, sy = self.dock_pos
        if sx is None or sy is None:
            sx, sy = default_dock_pos(work_area, size=size, margin=margin)
        x, y, w, h = clamp_rect(sx, sy, size, size, work_area, min_w=size, min_h=size)
        return (x, y, w, h)

    def panel(self, work_area: Rect, *, size: tuple[int, int] = (PANEL_W, PANEL_H),
              dock_size: int = DOCK_SIZE, margin: int = MARGIN, gap: int = GAP,
              min_w: int = PANEL_MIN_W, min_h: int = PANEL_MIN_H) -> Rect:
        """展开面板矩形（无保存值 → 由小方块位置推出；有保存值 → 夹回可见范围）。"""
        dock_rect = self.dock(work_area, size=dock_size, margin=margin)
        if self.panel_rect is None:
            return default_panel_rect(work_area, (dock_rect[0], dock_rect[1]), size=size,
                                      dock_size=dock_size, margin=margin, gap=gap,
                                      min_w=min_w, min_h=min_h)
        x, y, w, h = self.panel_rect
        return clamp_rect(x, y, w, h, work_area, min_w=min_w, min_h=min_h)


@dataclass
class DragState:
    """一次拖动 / 改尺寸的中间状态（用于区分「点击」与「拖动」）。"""

    origin: tuple[int, int]
    start_rect: Rect
    moved: bool = False
    #: 拖动阈值（像素）：小于它算点击
    threshold: int = 4
    samples: list = field(default_factory=list)

    def update(self, x: int, y: int) -> tuple[int, int]:
        """返回目标左上角坐标；累计位移超过阈值才置 ``moved``。"""
        dx = int(x) - int(self.origin[0])
        dy = int(y) - int(self.origin[1])
        if abs(dx) >= self.threshold or abs(dy) >= self.threshold:
            self.moved = True
        return (int(self.start_rect[0]) + dx, int(self.start_rect[1]) + dy)

    def resize(self, x: int, y: int, *, min_w: int, min_h: int,
               max_w: int, max_h: int) -> tuple[int, int]:
        """返回目标 ``(w, h)``（改尺寸手柄在右下角）。"""
        dx = int(x) - int(self.origin[0])
        dy = int(y) - int(self.origin[1])
        if abs(dx) >= self.threshold or abs(dy) >= self.threshold:
            self.moved = True
        w = max(int(min_w), min(int(self.start_rect[2]) + dx, int(max_w)))
        h = max(int(min_h), min(int(self.start_rect[3]) + dy, int(max_h)))
        return (w, h)
