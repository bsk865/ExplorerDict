"""离线界面预览：用 Pillow 把阅读浮窗的**真实布局**画成 PNG。

* 只用 Pillow + 本机 Windows 字体：**不建 Tk 窗口、不截图、不读桌面像素、不联网**；
* 尺寸、圆角、边距、颜色直接取自 ``app.ui.panel_geometry`` 与 ``app.ui.theme``：
  窗口圆角半径、**圆角安全留白**（``border_safe_inset``）、小方块内容可用区
  （``dock_content_box``）、小方块字号（``DOCK_GLYPH_PT``）、卡片圆角 / 高度 /
  间距、滑轨宽度与滑块最小高度、行内留白（``CONTENT_PAD``）都是同一份常量；
* 字号与产品同一套 pt → px 换算（``theme.device_px``，DPI 只缩放一次），字体族
  与产品一样是**无衬线**（Microsoft YaHei UI，经 ``app.ui.glyph_text`` 选面）；
* 顶栏 = 产品里的「主界面 / 折叠 / ×」，**没有菜单**；来源行只有**短主题名**
  （没有主题时整块隐藏）；详情来源只有一行短来源，**不出现 URL / 完整窗口标题**；
* 图里**只有产品真实的控件与学习内容**（两列圆角词卡、当前词、释义、追问、
  空态提示），没有标题说明、状态示意框、列标题或任何开发文字；
* **功能按钮的文字一律按真实可见字形（墨迹 bbox）在按钮内居中**，尺寸按产品
  同一条规则算（渲染逻辑只有一份：``app.ui.glyph_text``）：**字体排版单元**
  （``text_cell`` = max(墨迹, 字宽 / 行高)）+ 两侧对称内边距；
* 生成图片后会把每个按钮在**最终 1x 像素**上的居中误差、以及小方块字形是否落在
  22x22 内容区里打印出来。

**这不是实机证据**：离线图只能证明「按产品同一份常量与同一套字号规则画出来是
什么样」，不能替代真实 Windows 上的观感 / 拖动 / DPI 验收。

输出（与主输出同目录）::

    ui-preview.png         360x460 词语列表浮窗（圆角 12px、**顶部主题标签条**、
                           两列等宽圆角卡片、右侧滑轨；主题行不再占一行）
    ui-topic-preview.png   360x460 主题选择浮窗（内联清单在标签条**上方**，不是菜单）
    ui-chat-preview.png    360x460 追问页（释义区 / 对话区高度取自产品纯函数）
    ui-detail-preview.png  360x460 正常释义 + 追问的浮窗（已解释：没有解释入口行）
    ui-empty-preview.png   360x460 空词表浮窗（新页面只有一个「当前页 · 新主题」占位 chip）
    ui-dock-preview.png    44x44 独立小方块（圆角 10px、透明背景，只有图标本身）
    ui-main-preview.png / ui-concept-preview.png  主界面 / 概念图窗口

四张浮窗都画产品真实的右下角灰斜线改尺寸手柄（``widgets.grip_lines`` 同源）。
``draw_detail_page(failed=True)`` 才画「解释失败 + 重试」那一版，只用于离线居中
复核（默认写出的 PNG 是正常释义）。标签条与词卡换行都**直接调产品的纯函数**
（``ReadingPanel._wrap_chips`` / ``panel_geometry.chip_width`` / ``.clip_units``），
所以预览与实机不会各排一份。

用法::

    python -X utf8 tools/ui_preview.py [输出路径]
"""
from __future__ import annotations

import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw                        # noqa: E402

from app.ui import glyph_text                         # noqa: E402
from app.ui import panel_geometry as geo              # noqa: E402
from app.ui import theme, widgets                     # noqa: E402
from app.ui.reading_panel import (CLOSE_TEXT, MAIN_TEXT, RETRY_TEXT,  # noqa: E402
                                  SUMMARY_MAX_CHARS, clip_tag)

PANEL_W, PANEL_H = geo.PANEL_W, geo.PANEL_H
DOCK_SIZE = geo.DOCK_SIZE
#: 新主界面 / 导图的预览尺寸（与产品默认 geometry 一致）
MAIN_W, MAIN_H = 1080, 680
MAP_W, MAP_H = 860, 620
SCALE = 2                       # 2x 超采样后再缩小，接近真实 DPI 观感

#: 圆角安全留白（设备像素）：产品里由 ``outer`` 的 pack padx/pady 承担
INSET = theme.px(geo.border_safe_inset(geo.PANEL_RADIUS))       # 面板 13
DOCK_INSET = theme.px(geo.border_safe_inset(geo.DOCK_RADIUS))   # 小方块 11
#: 行内留白（``CONTENT_PAD``，设备像素）
PAD = theme.px(geo.CONTENT_PAD)
#: 小方块内容可用区（22x22）：字号必须放得下这里
DOCK_BOX = theme.px(geo.dock_content_box())


def font_px(size_pt: float) -> int:
    """pt → 设备像素：与产品**同一个**换算点（``glyph_text.font_px``）。"""
    return glyph_text.font_px(size_pt)


def _font(size_pt: float):
    """字体族 / 字号都与产品同源（``app.ui.glyph_text``）；这里是 2x 超采样。

    产品界面（浮窗 + 主界面）**只用无衬线**（Microsoft YaHei UI），预览也一致。
    """
    return glyph_text.load_font(font_px(size_pt) * SCALE)


SANS = _font                                        # 历史别名（全部无衬线）


# ----------------------------------------------------------------- 基础绘制
def rect(draw, box, *, fill=None, outline=None, width=1):
    x, y, w, h = box
    draw.rectangle([x * SCALE, y * SCALE, (x + w) * SCALE - 1, (y + h) * SCALE - 1],
                   fill=fill, outline=outline, width=width * SCALE)


def rounded(draw, box, *, radius, fill=None, outline=None, width=1):
    """圆角矩形：卡片 8px 圆角、窗口 12px / 小方块 10px 都用它画。"""
    x, y, w, h = box
    draw.rounded_rectangle([x * SCALE, y * SCALE, (x + w) * SCALE - 1, (y + h) * SCALE - 1],
                           radius=max(0, int(radius)) * SCALE, fill=fill,
                           outline=outline, width=width * SCALE)


def text(draw, xy, content, *, font, fill=theme.TEXT, anchor="la"):
    draw.text((xy[0] * SCALE, xy[1] * SCALE), content, font=font, fill=fill, anchor=anchor)


# --------------------------------------------------------- 按钮文字居中（唯一规则）
#: 每个按钮的落点：``(名字, x, y, w, h, 是否主按钮)``；:func:`button` 自己记录，
#: :func:`centering_report` 用它在**最终 1x 像素**上复核居中误差。
BUTTON_BOXES: list[tuple[str, int, int, int, int, bool]] = []

#: 小方块里那个字的落点（= 内容可用区 22x22），同样按墨迹居中复核
DOCK_GLYPH_BOX: tuple[int, int, int, int] = (DOCK_INSET, DOCK_INSET, DOCK_BOX, DOCK_BOX)

#: 主题行右侧展开箭头**真正画出去**的折线坐标（与产品 ``widgets.Chevron``
#: 同一份纯函数几何；测试用它证明预览里是线条，不是 ``▾`` 字形）。
TOPIC_CHEVRON_POINTS: tuple[tuple[float, float], ...] = ()
#: 追问页**真正画出去**的对话行（默认必须是「问题 + 回答」的完成态）
CHAT_PAGE_LINES: tuple[str, ...] = ()


def ink_bbox(content: str, font) -> tuple[int, int, int, int]:
    """真实可见字形（含抗锯齿墨迹）的 tight bbox，相对 ``anchor="lt"`` 的原点。

    实现只有一份：``app.ui.glyph_text.ink_bbox`` —— 真实按钮的图像路径与这里的
    离线预览必须量同一件事。按钮文字要居中的是**看得见的墨迹**，不是排版单元
    （例如「×」在 9pt 下排版单元 9x17px，真实墨迹只有 7x6px）。
    """
    return glyph_text.ink_bbox(content, font)


def center_text(draw, box, content, *, font, fill) -> None:
    """把 ``content`` 的**可见墨迹**精确居中在 ``box``（x, y, w, h）里。

    与产品 ``FlatButton`` 的图像路径是同一条规则（``glyph_text.center_ink``）：
    居中的是墨迹中心，**没有任何 +1 之类的魔数位移**。误差在最终 1x 像素上复核
    ≤ 1 设备像素（见 :func:`centering_report`）。
    """
    glyph_text.center_ink(draw, box, content, font=font, fill=fill, scale=SCALE)


def _luma(pixel) -> int:
    r, g, b = pixel[0], pixel[1], pixel[2]
    return (r * 299 + g * 587 + b * 114) // 1000


def measure_ink_center(image: Image.Image, box, *, primary: bool, label: str = ""):
    """在最终 1x 图上量 ``box`` 内文字墨迹 bbox 的中心，返回 ``(dx, dy, 结果)``。

    * 只统计**可见**像素（``alpha >= 200``，圆角外的透明像素不算）；
    * 主按钮 = 米色字压柔黑底 → 找亮像素；次按钮 = 柔黑字压米色底 → 找暗像素；
    * 误差 = 墨迹中心 − 按钮像素区域中心（``x..x+w-1`` / ``y..y+h-1``）。
    """
    x, y, w, h = box
    rgba = image.convert("RGBA")
    pixels, alpha = rgba.load(), rgba.getchannel("A").load()
    xs: list[int] = []
    ys: list[int] = []
    for yy in range(y + 1, y + h - 1):            # 去掉 1px 细线外框
        for xx in range(x + 1, x + w - 1):
            if alpha[xx, yy] < 200:
                continue
            value = _luma(pixels[xx, yy])
            if (value > 150) if primary else (value < 110):
                xs.append(xx)
                ys.append(yy)
    if not xs:
        return (None, None, f"{label or box}: 没找到墨迹像素")
    ink_cx = (min(xs) + max(xs)) / 2
    ink_cy = (min(ys) + max(ys)) / 2
    box_cx = x + (w - 1) / 2
    box_cy = y + (h - 1) / 2
    dx, dy = ink_cx - box_cx, ink_cy - box_cy
    detail = (f"{label or '?'}: 框 {w}x{h}@({x},{y}) 墨迹 "
              f"x[{min(xs)}..{max(xs)}] y[{min(ys)}..{max(ys)}] "
              f"中心偏移 dx={dx:+.1f}px dy={dy:+.1f}px")
    return (dx, dy, detail)


def dock_fit_report() -> tuple[bool, str]:
    """小方块字形必须落在 ``DOCK_BOX``（22x22）内容区里（离线像素复核）。"""
    font = SANS(geo.DOCK_GLYPH_PT)
    x0, y0, x1, y1 = ink_bbox(geo.DOCK_GLYPH, font)
    ink_w = (x1 - x0) / SCALE
    ink_h = (y1 - y0) / SCALE
    fits = ink_w <= DOCK_BOX and ink_h <= DOCK_BOX
    detail = (f"小方块「{geo.DOCK_GLYPH}」{geo.DOCK_GLYPH_PT}pt：墨迹 "
              f"{ink_w:.1f}x{ink_h:.1f}px，内容可用区 {DOCK_BOX}x{DOCK_BOX}px")
    return fits, detail


def centering_report() -> tuple[list[tuple[str, str, float, float, str]], str]:
    """渲染五张浮窗 + 小方块，逐个按钮复核居中；返回 ``(行, 文本报告)``。

    每行 = ``(预览文件, 按钮文字, dx, dy, 明细)``。这是**离线**检查：
    只用 Pillow 画出来的像素，不建 Tk 窗口、不截图。
    """
    rows: list[tuple[str, str, float, float, str]] = []
    for draw_page, name in ((draw_list_page, "ui-preview.png"),
                            (lambda draw: draw_detail_page(draw, failed=True),
                             "ui-detail-preview.png"),
                            (draw_empty_page, "ui-empty-preview.png"),
                            (draw_topic_picker_page, "ui-topic-preview.png"),
                            (draw_chat_page, "ui-chat-preview.png")):
        image = render_panel(draw_page)
        for label, x, y, w, h, primary in BUTTON_BOXES:
            dx, dy, detail = measure_ink_center(image, (x, y, w, h), primary=primary,
                                                label=label)
            rows.append((name, label, dx if dx is not None else float("nan"),
                         dy if dy is not None else float("nan"), detail))
    image = render_dock()
    dock_label = f"小方块「{geo.DOCK_GLYPH}」"
    dx, dy, detail = measure_ink_center(image, DOCK_GLYPH_BOX, primary=False,
                                        label=dock_label)
    rows.append(("ui-dock-preview.png", dock_label,
                 dx if dx is not None else float("nan"),
                 dy if dy is not None else float("nan"), detail))
    lines = ["按键文字居中复核（Pillow 最终 1x 像素；阈值：上下左右 |偏移| ≤ 1px）"]
    worst = 0.0
    for name, _label, dx, dy, detail in rows:
        worst = max(worst, abs(dx), abs(dy))
        lines.append(f"  [{'OK ' if max(abs(dx), abs(dy)) <= 1 else 'BAD'}] {name:<22} {detail}")
    lines.append(f"  最大偏移 {worst:.1f}px —— "
                 f"{'全部 ≤ 1 设备像素' if worst <= 1 else '存在超差（必须修）'}")
    ok, detail = dock_fit_report()
    lines.append(f"  [{'OK ' if ok else 'BAD'}] 小方块字形是否放得下：{detail}")
    return rows, "\n".join(lines)


def measure(draw, content: str, font) -> int:
    return int(draw.textlength(content, font=font) / SCALE)


def clip(content: str, limit: int) -> str:
    text_value = str(content or "")
    return text_value if len(text_value) <= limit else text_value[:limit] + "…"


def hairline(draw, x, y, w, color=theme.BORDER):
    draw.rectangle([x * SCALE, y * SCALE, (x + w) * SCALE, y * SCALE + SCALE - 1], fill=color)


_TOKEN_RE = re.compile(r"[A-Za-z0-9@._:/#+\-]+|\s+|.")


def wrap(draw, content: str, *, font, max_w: int) -> list[str]:
    """按**实际像素宽度**换行（CJK 逐字断行、西文按词断行），文字变大后不溢出。"""
    lines: list[str] = []
    for para in str(content).split("\n"):
        if not para:
            lines.append("")
            continue
        cur = ""
        for token in _TOKEN_RE.findall(para):
            if token.isspace() and not cur:
                continue
            trial = cur + token
            if cur and measure(draw, trial, font) > max_w:
                lines.append(cur.rstrip())
                cur = "" if token.isspace() else token
            else:
                cur = trial
        lines.append(cur.rstrip())
    return lines


def draw_lines(draw, x, y, lines, *, font, fill=theme.TEXT_BODY, line_h=None):
    """逐行绘制，返回下一行的 y。行高按字号 1.5 估（中文正文的常规行距）。"""
    step = line_h or int(font_px(9) * 1.5)
    for i, line in enumerate(lines):
        if line:
            text(draw, (x, y + i * step), line, font=font, fill=fill)
    return y + max(1, len(lines)) * step


# ------------------------------------------------------------- 内容区（与产品同源）
def content_x() -> int:
    """内容左边界：窗口左边缘 + 圆角安全留白 + 行内留白（= 15）。"""
    return INSET + PAD


def content_w() -> int:
    """内容可用宽度：与 ``ReadingPanel._content_width()`` 同一个算式（= 330）。"""
    return PANEL_W - 2 * INSET - 2 * PAD


def button_size(label, *, font_pt=8, padx=8, pady=4) -> tuple[int, int]:
    """按钮的**逻辑像素**尺寸：排版单元（按 ``SCALE`` 折回 1x）+ 两侧对称内边距。

    与产品 ``glyph_text.render_text_image`` 是同一条规则、同一份 ``text_cell``
    计算，因此预览里的按钮不会比真实按钮小（也不会只按墨迹高度缩成一条）。
    """
    cell_w, cell_h = glyph_text.text_cell(label, SANS(font_pt))
    w = int(round(cell_w / SCALE)) + theme.px(padx) * 2
    h = int(round(cell_h / SCALE)) + theme.px(pady) * 2
    return max(1, w), max(1, h)


def button(draw, x, y, label, *, font_pt=8, padx=8, pady=4, primary=False, right=None):
    """扁平按钮（细线方角、无阴影）；``right`` 给右边缘时自动右对齐。返回 (w, h)。

    尺寸与文字位置都走产品**图像路径**的同一条规则（``app.ui.glyph_text``）：
    宽 / 高 = :func:`button_size`；文字用 :func:`center_text` 把**真实可见字形**
    的墨迹居中在按钮像素区域里，**没有任何针对单个按钮的偏移量**。
    """
    font = SANS(font_pt)
    w, h = button_size(label, font_pt=font_pt, padx=padx, pady=pady)
    if right is not None:
        x = right - w
    x, y, w, h = int(x), int(y), int(w), int(h)
    rect(draw, (x, y, w, h), fill=theme.ACCENT if primary else theme.PANEL,
         outline=theme.ACCENT if primary else theme.BORDER)
    center_text(draw, (x, y, w, h), label, font=font,
                fill=theme.BG if primary else theme.TEXT)
    BUTTON_BOXES.append((label, x, y, w, h, bool(primary)))
    return w, h


def input_box(draw, x, y, w, h):
    """输入框（细线方角；产品里的输入框本来就是空的，不画占位文案）。"""
    rect(draw, (x, y, w, h), fill=theme.PANEL, outline=theme.BORDER)


# --------------------------------------------------- 示例学习内容（非界面文案）
TOPIC = "卷积"                      # 当前这一页的**短主题名**
CAPTURED_TOPICS = [("卷积神经网络", 12), ("注意力机制", 8), ("优化器", 5)]
#: 标签条：第一个 chip 永远是**当前页**（跟随时显示本页主题名 + 条数），
#: 其余 = 有词的历史主题；``STRIP_ACTIVE = 0`` 表示当前高亮的是「跟随当前页」。
STRIP_FOLLOW = f"当前页 · {TOPIC}（12）"
STRIP_ACTIVE = 0
CURRENT_TERM = "卷积"
#: 词卡 = (词语, 库里**已经有的**一句话释义或空串)：没有释义的卡片不画摘要行。
#: 第一条刻意用长英文短语：词语在卡里**最多两行**（按真实宽度换行、真超长才省略）。
CARDS = [("Machine learning (ML) algorithms", "机器学习算法：让计算机从数据里学规律"),
         ("注意力机制", "按相关度加权"),
         ("Transformer", ""), ("梯度消失", "深层网络难训练"),
         ("过拟合", "记住了噪声"), ("正则化", "约束模型复杂度"),
         ("Embedding", ""), ("残差连接", "跨层直连"),
         ("批量归一化", "稳定每层分布"), ("反向传播", "链式法则求梯度"),
         ("学习率", "每步走多远"), ("Dropout", "随机丢弃神经元")]
DEF_BODY = [
    "一句话解释：在信号 / 图像上做加权求和的特征提取运算。",
    "",
    "详细说明：卷积核在输入上滑动，逐点相乘再求和；在深度学习里，",
    "卷积核参数由训练得到。",
    "",
    "例子：",
    "  · 3×3 卷积核提取图像边缘。",
]
#: 失败正文**只解释失败**：长操作说明不铺进来（底部一句短提示 + 唯一入口）
FAIL_BODY = [
    "解释失败：连接超时",
]
CHAT_TURNS = [("你", "它和互相关有什么区别？"),
              ("词典", "互相关不做核翻转，卷积会翻转。")]


# ----------------------------------------------------------------- 浮窗绘制
def draw_chrome(draw) -> int:
    """顶栏：最左「主界面」+ 标题，最右「×」与「折叠」（**没有菜单**）。

    与产品 ``ReadingPanel._build_panel`` 同一套控件与顺序：最右是「×」
    （``close_by_user``：收起并在后台继续，绝不退出程序），它左边是「折叠」。
    返回内容区起点 y。
    """
    y = INSET + theme.px(6)
    bw, bh = button(draw, content_x(), y, MAIN_TEXT, font_pt=8, padx=8, pady=2)
    text(draw, (content_x() + bw + theme.px(4) + theme.px(2), y + 1), "探索词典",
         font=SANS(11), fill=theme.TEXT)
    xw, _xh = button(draw, 0, y, CLOSE_TEXT, font_pt=9, padx=6, pady=1,
                     right=PANEL_W - INSET - PAD)
    button(draw, 0, y, "折叠", font_pt=8, padx=8, pady=2,
           right=PANEL_W - INSET - PAD - xw - theme.px(2))
    return y + max(bh, theme.px(10)) + theme.px(4)


def draw_source_row(draw, y: int, topic: str) -> int:
    """来源行：**只有当前这一页的短主题名**；没有主题时整块（含分隔线）隐藏。

    与产品 ``ReadingPanel._sync_source_row`` 一致：浏览器窗口标题 / URL 不进浮窗。
    右侧的展开箭头由 :func:`draw_topic_chevron` 画成**线条**（不是字形）。
    """
    if not topic:
        return y
    text(draw, (content_x(), y), topic, font=SANS(8), fill=theme.TEXT_MUTED)
    line_y = y + font_px(8) + theme.px(4)
    hairline(draw, content_x(), line_y, content_w())
    return line_y + theme.px(4)


def draw_topic_chevron(draw, row_top: int) -> tuple[tuple[float, float], ...]:
    """主题行右侧的展开箭头：**与产品 ``widgets.Chevron`` 同一份几何**。

    返回真正画出去的折线坐标（``TOPIC_CHEVRON_POINTS`` 同步记录一份）。旧预览
    直接排 ``▾`` 字形，Pillow 缺字时画成方框；现在两边都只画线。
    """
    global TOPIC_CHEVRON_POINTS
    from app.ui import widgets as widgets_mod

    width = float(theme.px(widgets_mod.CHEVRON_W))
    height = float(theme.px(widgets_mod.CHEVRON_H))
    cx = PANEL_W - INSET - PAD - width / 2.0
    cy = row_top + font_px(8) / 2.0
    flat = widgets_mod.chevron_flat_points(cx, cy, width=width, height=height,
                                           direction="down")
    points = tuple((flat[index], flat[index + 1]) for index in range(0, len(flat), 2))
    draw.line([(x * SCALE, y * SCALE) for x, y in points],
              fill=theme.TEXT_FAINT, width=max(1, theme.px(1)) * SCALE)
    TOPIC_CHEVRON_POINTS = points
    return points


def draw_rail(draw, top: int, bottom: int, *, first: float = 0.18,
              span: float = 0.62) -> int:
    """右侧可拖滑轨：**只画圆角滑块**（无箭头、无刻度、无说明字）。

    位置 / 高度与产品同一套几何：滑块最小高度 ``RAIL_MIN_THUMB``、圆角
    ``RAIL_RADIUS``，顶端 = ``可走高度 × first / (1 - span)``（与
    ``widgets.ScrollRail.fraction()`` 同一换算）。返回滑块顶端 y。
    """
    width = theme.px(geo.RAIL_W)
    radius = theme.px(geo.RAIL_RADIUS)
    track = max(1, int(bottom) - int(top))
    thumb = max(theme.px(geo.RAIL_MIN_THUMB), int(round(track * span)))
    thumb = max(1, min(thumb, track))
    room = track - thumb
    ratio = 0.0 if span >= 1 else min(max(first / (1.0 - span), 0.0), 1.0)
    y = int(top) + int(round(room * ratio))
    inset = max(1, (width - radius * 2) // 2)
    rounded(draw, (PANEL_W - INSET - PAD - width + inset, y,
                   max(2, width - 2 * inset), max(2, thumb - 2)),
            radius=radius, fill=theme.RAIL_THUMB)
    return y


def draw_hint_row(draw, message: str) -> int:
    """最底部反馈行（产品里同时是右下角手柄的净空带）：返回它的顶端 y。

    行高 = 文字实际行数 × 行高 + 2 × ``HINT_PAD_Y``；换行宽度 = 内容宽度减去手柄
    宽度（与产品 ``hint_label`` 的 ``wraplength`` / 右侧 ``padx`` 一致）。
    """
    from app.ui.reading_panel import HINT_PAD_Y

    pad = theme.px(HINT_PAD_Y)
    line_h = font_px(8) + 3
    lines = wrap(draw, message, font=SANS(8),
                 max_w=content_w() - theme.px(geo.GRIP_SIZE)) if message else []
    row_h = max(font_px(8), len(lines) * line_h) + 2 * pad
    top = PANEL_H - INSET - row_h
    if lines:
        draw_lines(draw, content_x(), top + pad, lines, font=SANS(8),
                   fill=theme.TEXT_MUTED, line_h=line_h)
    return top


def draw_grip(draw) -> None:
    """右下角改尺寸手柄：与产品 ``widgets.ResizeGrip`` **同源**的 3 条内缩灰斜线。

    落点 = ``panel_body`` 的右下角（窗口四边各内缩 ``INSET``），线端点直接来自
    :func:`app.ui.widgets.grip_lines`（产品画布用的同一份纯函数），不是另写一套。
    """
    size = theme.px(geo.GRIP_SIZE)
    x0 = PANEL_W - INSET - size
    y0 = PANEL_H - INSET - size
    color = theme.RAIL_THUMB
    for x1, y1, x2, y2 in widgets.grip_lines(size, size):
        draw.line([(x0 + x1) * SCALE, (y0 + y1) * SCALE,
                   (x0 + x2) * SCALE, (y0 + y2) * SCALE],
                  fill=color, width=max(1, theme.px(1)) * SCALE)


def draw_action_area(draw, bottom: int, *, term: str):
    """底部「当前词（最多两行）+ 解释并记录」；``bottom`` 是它的下边界。

    与产品 ``_build_list_page`` 一致：这块**先**按 ``side="bottom"`` 预留位置，
    可滚动的词卡区只吃剩下的空间；选区 **context 不在这里显示**（完整快照仍在
    内存与库里），当前词按**当前内容宽度**最多两行。
    """
    quote_lines = wrap(draw, f"「{term}」", font=SANS(9), max_w=content_w())[:2] \
        if term else []
    btn_h = button_size("解释并记录", font_pt=9, padx=14, pady=5)[1]
    line_h9 = font_px(9) + 4
    block_h = (theme.px(8) + len(quote_lines) * line_h9
               + theme.px(6) + btn_h + theme.px(6))
    top = bottom - block_h
    hairline(draw, content_x(), top, content_w())
    yy = top + theme.px(8)
    if quote_lines:
        yy = draw_lines(draw, content_x(), yy, quote_lines, font=SANS(9),
                        fill=theme.TEXT_BODY, line_h=line_h9)
    button(draw, content_x(), yy + theme.px(6), "解释并记录", font_pt=9, padx=14, pady=5,
           primary=True)
    return top


def draw_cards(draw, top: int, bottom: int, cards) -> None:
    """两列等宽圆角词卡（真实面板是 Canvas 自绘 + 可滚动）。

    词语最多 ``geo.CARD_TERM_LINES`` 行：**按真实像素宽度换行**（不是按字数截断），
    真的放不下才在第二行末尾补「…」；摘要一行、8pt、``TEXT_MUTED``（不再贴边发灰）。
    """
    rail_w = theme.px(geo.RAIL_W)
    gap = theme.px(geo.CARD_GAP)
    card_h = theme.px(geo.CARD_H)
    radius = theme.px(geo.CARD_RADIUS)
    pad_x, pad_y = theme.px(geo.CARD_PAD_X), theme.px(geo.CARD_PAD_Y)
    area_x = content_x()
    area_w = content_w() - rail_w - theme.px(4)
    col_w = max(theme.px(90), (max(theme.px(120), area_w) - gap) // 2)
    inner = max(theme.px(16), col_w - 2 * pad_x)
    term_font, sum_font = SANS(geo.CARD_TERM_PT), SANS(geo.CARD_SUMMARY_PT)
    term_lines_max = max(1, int(geo.CARD_TERM_LINES))
    # 行高与产品同源（``geo.text_line_px`` = Tk ``linespace`` 的保守估计）：
    # 用字号当行高会低估，画出来的两行词语会比真实界面挤。
    term_line_h = geo.text_line_px(font_px(geo.CARD_TERM_PT))
    sum_line_h = geo.text_line_px(font_px(geo.CARD_SUMMARY_PT))
    for index, (term, summary) in enumerate(cards):
        row, col = divmod(index, 2)
        cy = top + row * (card_h + gap)
        if cy + card_h > bottom:
            break
        cx = area_x + col * (col_w + gap)
        rounded(draw, (cx, cy, col_w, card_h), radius=radius,
                fill=theme.CARD_BG, outline=theme.CARD_BORDER)
        lines = wrap(draw, term, font=term_font, max_w=inner)
        if len(lines) > term_lines_max:
            lines = lines[:term_lines_max]
            lines[-1] = lines[-1].rstrip() + "…"
        draw_lines(draw, cx + pad_x, cy + pad_y, lines, font=term_font,
                   fill=theme.TEXT, line_h=term_line_h)
        if summary:
            body = str(summary)
            if len(body) > 2 * SUMMARY_MAX_CHARS:
                body = body[: 2 * SUMMARY_MAX_CHARS] + "…"
            sum_lines = wrap(draw, body, font=sum_font, max_w=inner)[:1]
            draw_lines(draw, cx + pad_x,
                       cy + card_h - pad_y - sum_line_h,
                       sum_lines or [""], font=sum_font, fill=theme.TEXT_MUTED,
                       line_h=sum_line_h)
    draw_rail(draw, top, bottom)


def draw_topic_chips(draw, y: int, *, follow: str, topics, active: int = 0) -> int:
    """主题 chip 条（常驻）：第一个永远是「当前页 · …」，其余是历史主题。

    排布**直接调产品的纯函数** ``ReadingPanel._wrap_chips``（同一份贪心换行 + 同一份
    ``panel_geometry.chip_width``），所以预览与实机不会各排一份；放不下的主题收进
    末尾「+N 个主题」chip。返回内容区下一行的 y。``active`` = 高亮的主题序号
    （``0`` = 「当前页」）。
    """
    from app.ui.reading_panel import ReadingPanel

    pad_x = theme.px(geo.CHIP_PAD_X)
    gap = theme.px(geo.CHIP_GAP)
    row_h = theme.px(geo.CHIP_H)
    chip_px = abs(theme.device_px(geo.CHIP_PT))
    row_w = max(theme.px(60), content_w() - theme.px(geo.RAIL_W) - theme.px(4))
    labels = [(index + 1, f"{name}（{count}）") for index, (name, count) in enumerate(topics)]
    rows: list[list[tuple]] = []
    for keep in range(len(labels), -1, -1):
        rest = len(labels) - keep
        chips = [("follow", follow)] + labels[:keep]
        if rest:
            chips.append(("more", f"+{rest} 个主题"))
        rows = ReadingPanel._wrap_chips(chips, row_w=row_w, chip_px=chip_px,
                                       pad_x=pad_x, gap=gap)
        if len(rows) <= max(1, int(geo.CHIP_ROWS)) or keep == 0:
            break
    rows = rows[:max(1, int(geo.CHIP_ROWS))]
    for index, row in enumerate(rows):
        x = content_x()
        yy = y + index * (row_h + gap)
        for action, label, width in row:
            primary = (action == "follow" and active == 0) or (
                isinstance(action, int) and int(action) == int(active))
            rect(draw, (x, yy, width, row_h),
                 fill=theme.ACCENT if primary else theme.PANEL,
                 outline=theme.ACCENT if primary else theme.BORDER)
            center_text(draw, (x, yy, width, row_h), label, font=SANS(geo.CHIP_PT),
                        fill=theme.BG if primary else theme.TEXT)
            BUTTON_BOXES.append((label, x, yy, width, row_h, bool(primary)))
            x += width + gap
    if not rows:
        return y
    return y + len(rows) * row_h + max(0, len(rows) - 1) * gap


def draw_list_page(draw) -> None:
    """列表页：主题标签条 + 两列等宽圆角词卡 + 右侧滑轨 + 底部「当前词 / 解释并记录」。

    主题**不再靠顶栏那一行**：常驻 chip 条的第一个 chip 写着本页主题（跟随时高亮），
    后面的 chip 是历史主题（点一个就看那一组词），放不下收进「+N 个主题」。
    """
    y = draw_chrome(draw)
    y = draw_topic_chips(draw, y + theme.px(2), follow=STRIP_FOLLOW,
                         topics=CAPTURED_TOPICS, active=STRIP_ACTIVE)
    y += theme.px(2)
    hint_top = draw_hint_row(draw, "")
    action_top = draw_action_area(draw, hint_top, term=CURRENT_TERM)
    text(draw, (PANEL_W - INSET - PAD, y + theme.px(4)), f"{len(CARDS)} 条", font=SANS(7),
         fill=theme.TEXT_FAINT, anchor="ra")
    draw_cards(draw, y + theme.px(16), action_top - theme.px(6), CARDS)
    draw_grip(draw)


def draw_empty_page(draw) -> None:
    """空词表：提示**居中在词卡视口正中**（不占一行、不压到底部操作区）。

    新页面（这一页还没保存过词）的标签条只有一个占位 chip「当前页 · 新主题」——
    **只显示、不写库**，真正的主题在用户点「解释并记录」时才建。
    """
    y = draw_chrome(draw)
    y = draw_topic_chips(draw, y + theme.px(2), follow="当前页 · 新主题", topics=[])
    y += theme.px(2)
    hint_top = draw_hint_row(draw, "")
    action_top = draw_action_area(draw, hint_top, term="")
    text(draw, (PANEL_W - INSET - PAD, y + theme.px(4)), "0 条", font=SANS(7),
         fill=theme.TEXT_FAINT, anchor="ra")
    view_top = y + theme.px(16)
    view_bottom = action_top - theme.px(6)
    text(draw, (content_x() + content_w() // 2, (view_top + view_bottom) // 2),
         "暂无词语", font=SANS(9), fill=theme.TEXT_FAINT, anchor="mm")
    draw_grip(draw)


def draw_detail_page(draw, *, failed: bool = False) -> None:
    """详情页：词语 + 一行来源 + 释义 + 追问（+ 失败时才有的唯一「重试」）。

    默认画**正常释义 + 追问学习内容**（已解释：没有解释入口行）；``failed=True``
    才画「解释失败 + 重试」那一版，只用于离线按钮居中复核，写出的 PNG 仍是正常版。
    """
    y = draw_chrome(draw)
    row_top = y
    y = draw_source_row(draw, y, TOPIC)
    draw_topic_chevron(draw, row_top)
    hint_top = draw_hint_row(
        draw, f"解释失败，请点「{RETRY_TEXT}」" if failed else "")
    row_h = font_px(9) + theme.px(6)
    row_y = hint_top - theme.px(2) - row_h
    send_w, _sh = button(draw, 0, row_y, "发送", font_pt=8, padx=10, pady=3, primary=True,
                         right=PANEL_W - INSET - PAD)
    input_box(draw, content_x(), row_y, content_w() - send_w - theme.px(4), row_h)

    # ---- 顶部：返回 +（失败态）唯一「重试」；已解释态没有解释入口行 ----
    from app.ui.reading_panel import BACK_TEXT

    _bw, bh = button(draw, content_x(), y + theme.px(2), BACK_TEXT, font_pt=8,
                     padx=8, pady=2)
    text(draw, (PANEL_W - INSET - PAD, y + theme.px(4)),
         "解释失败" if failed else "已解释", font=SANS(7),
         fill=theme.TEXT_FAINT, anchor="ra")
    yy = y + theme.px(2) + bh + theme.px(4)
    if failed:
        _rw, rh = button(draw, content_x(), yy, RETRY_TEXT, font_pt=8, padx=10, pady=2)
        yy += rh + theme.px(6)
    else:
        yy += theme.px(2)

    # ---- 词语 + 只有一行的短来源（URL / 完整标题只在主界面看）----
    yy = draw_lines(draw, content_x(), yy, wrap(draw, CURRENT_TERM, font=SANS(13),
                                                max_w=content_w()),
                    font=SANS(13), fill=theme.TEXT, line_h=font_px(13) + 4)
    yy = draw_lines(draw, content_x(), yy, ["深度学习入门"], font=SANS(7),
                    fill=theme.TEXT_FAINT, line_h=font_px(7) + 3)

    # ---- 释义正文（可滚动区）：正常态画真实学习内容，失败态只解释失败 ----
    body: list[str] = []
    for para in (FAIL_BODY if failed else DEF_BODY):
        body.extend(wrap(draw, para, font=SANS(9), max_w=content_w()) if para else [""])
    yy = draw_lines(draw, content_x(), yy + theme.px(6), body, font=SANS(9),
                    fill=theme.TEXT_BODY, line_h=font_px(9) + 4)

    # ---- 追问（表头 + 历史）----
    yy += theme.px(6)
    hairline(draw, content_x(), yy, content_w())
    text(draw, (content_x(), yy + theme.px(4)), "追问", font=SANS(7), fill=theme.TEXT_MUTED)
    yy += font_px(7) + theme.px(8)
    chat: list[str] = []
    for role, content in CHAT_TURNS:
        chat.extend(wrap(draw, f"{role}：{content}", font=SANS(8), max_w=content_w()))
        chat.append("")
    draw_lines(draw, content_x(), yy, chat, font=SANS(8), fill=theme.TEXT_BODY,
               line_h=font_px(8) + 4)
    draw_grip(draw)


# ------------------------------------ 主题内联清单 / 追问问答（新页，同一浮窗尺寸）
def detail_area_split() -> tuple[int, int]:
    """释义区 / 对话区的高度：**直接调产品纯函数**（预览与实机不会各算一份）。"""
    avail = (PANEL_H - 2 * INSET - theme.px(geo.PANEL_HEAD_H)
             - theme.px(geo.DETAIL_FIXED_H))
    return geo.detail_area_heights(avail, def_min=theme.px(geo.DETAIL_AREA_MIN),
                                   chat_min=theme.px(geo.DETAIL_AREA_MIN))


def draw_topic_picker_page(draw) -> None:
    """主题选择：点标签条末尾「+N 个主题」展开**内联清单**（不是 grab 菜单）。

    第一项永远是「跟随当前阅读页」，其余是历史主题（含 0 条词的空主题：这里是
    「管理视图」，标签条才是只列有词主题的「归纳」）。清单画在标签条**上方**，
    与产品里的 pack 顺序一致（``topic_picker`` 属于 ``panel_body``，整页词表在它下面）。
    """
    from app.ui.reading_panel import FOLLOW_TEXT, TOPIC_MAX_ITEMS, TOPIC_MAX_CHARS

    y = draw_chrome(draw)
    rows = [FOLLOW_TEXT] + [f"{name}（{count}）" for name, count in CAPTURED_TOPICS][
        :TOPIC_MAX_ITEMS]
    row_h = font_px(8) + theme.px(4)
    for index, label in enumerate(rows):
        top = y + index * row_h
        rect(draw, (content_x(), top, content_x() + content_w(), top + row_h),
             fill=theme.PANEL_ALT)
        marker = "· " if index == 1 else "   "
        text(draw, (content_x() + theme.px(2), top + theme.px(2)), marker + label,
             font=SANS(8), fill=theme.TEXT if index == 1 else theme.TEXT_MUTED)
    y += len(rows) * row_h + theme.px(4)
    y = draw_topic_chips(draw, y, follow=STRIP_FOLLOW, topics=CAPTURED_TOPICS,
                         active=1) + theme.px(2)
    draw_cards(draw, y, PANEL_H - INSET - theme.px(30), CARDS[:4])
    draw_hint_row(draw, f"正在浏览《{CAPTURED_TOPICS[0][0]}》")
    draw_grip(draw)


def draw_chat_page(draw, *, pending: bool = False) -> None:
    """追问页默认画**完成态**：用户的问题 + 词典的回答都在可见高度里。

    旧预览在回答后面又多画一行「正在回答…」，看起来像「回答完还在回答」。现在
    默认只有一问一答；``pending=True`` 才追加那一行，专供「回答中」单状态检查，
    **不新增 PNG**。真正画出去的行记录在 :data:`CHAT_PAGE_LINES`。
    """
    global CHAT_PAGE_LINES
    from app.ui.reading_panel import BACK_TEXT

    y = draw_chrome(draw)
    row_top = y
    y = draw_source_row(draw, y, TOPIC)
    draw_topic_chevron(draw, row_top)
    hint_top = draw_hint_row(draw, "")
    row_h = font_px(9) + theme.px(6)
    row_y = hint_top - theme.px(2) - row_h
    send_w, _sh = button(draw, 0, row_y, "发送", font_pt=8, padx=10, pady=3, primary=True,
                         right=PANEL_W - INSET - PAD)
    input_box(draw, content_x(), row_y, content_w() - send_w - theme.px(4), row_h)

    _bw, bh = button(draw, content_x(), y + theme.px(2), BACK_TEXT, font_pt=8,
                     padx=8, pady=2)
    text(draw, (PANEL_W - INSET - PAD, y + theme.px(4)), "已解释", font=SANS(7),
         fill=theme.TEXT_FAINT, anchor="ra")
    yy = y + theme.px(2) + bh + theme.px(6)
    yy = draw_lines(draw, content_x(), yy, wrap(draw, CURRENT_TERM, font=SANS(13),
                                                max_w=content_w()),
                    font=SANS(13), fill=theme.TEXT, line_h=font_px(13) + 4)
    def_h, chat_h = detail_area_split()
    # 释义区（可滚动）：只画前两行，右下角标出它分到的高度
    body: list[str] = []
    for para in DEF_BODY[:2]:
        body.extend(wrap(draw, para, font=SANS(9), max_w=content_w()))
    draw_lines(draw, content_x(), yy + theme.px(2), body[:2], font=SANS(9),
               fill=theme.TEXT_BODY, line_h=font_px(9) + 4)
    chat_top = yy + def_h
    hairline(draw, content_x(), chat_top - theme.px(4), content_w())
    text(draw, (content_x(), chat_top), "追问", font=SANS(7), fill=theme.TEXT_MUTED)
    chat_y = chat_top + font_px(7) + theme.px(4)
    chat: list[str] = []
    for role, content in CHAT_TURNS + [("你", "它和下采样什么关系？")]:
        chat.extend(wrap(draw, f"{role}：{content}", font=SANS(8), max_w=content_w()))
        chat.append("")
    chat.extend(wrap(draw, "词典：都是把局部信息汇总成一个值。", font=SANS(8),
                     max_w=content_w()))
    if pending:
        chat.extend(["", "词典：正在回答…"])
    CHAT_PAGE_LINES = tuple(line for line in chat if line)
    #: 只画**最新**的几行：产品在更新后会把对话滚到最后一条（新消息必须可见）
    per_line = font_px(8) + 4
    room = max(1, chat_h // per_line)
    draw_lines(draw, content_x(), chat_y, chat[-room:],
               font=SANS(8), fill=theme.TEXT_BODY, line_h=per_line)
    draw_grip(draw)


def render_panel(draw_page) -> Image.Image:
    """一个 360x460 的实际浮窗：**圆角 12px**（与窗口 region 一致，角外透明）。"""
    img = Image.new("RGBA", (PANEL_W * SCALE, PANEL_H * SCALE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    rounded(draw, (0, 0, PANEL_W, PANEL_H), radius=theme.px(geo.PANEL_RADIUS),
            fill=theme.PANEL, outline=theme.OUTLINE)
    BUTTON_BOXES.clear()                 # 只记录本页按钮，供居中复核使用
    draw_page(draw)
    return img.resize((PANEL_W, PANEL_H), Image.LANCZOS)


def _blank(size: tuple[int, int]) -> tuple[Image.Image, "ImageDraw.ImageDraw"]:
    img = Image.new("RGBA", (size[0] * SCALE, size[1] * SCALE), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def _chrome_bar(draw, width: int, title: str) -> int:
    """自绘标题带：**没有原生标题栏 / 边框 / 菜单栏**，只有一条标题 + 「×」。

    高度取产品的 :data:`app.ui.widgets.CHROME_H`：主界面 / 导图 / 设置 / 手动录入
    共用同一份 chrome，预览与实机不会各画一个高度。
    """
    bar_h = theme.px(widgets.CHROME_H)
    rect(draw, (0, 0, width, bar_h), fill=theme.PANEL)
    text(draw, (theme.px(12), theme.px(8)), title, font=SANS(9), fill=theme.TEXT)
    button(draw, 0, theme.px(3), "×", font_pt=9, padx=7, pady=1,
           right=width - theme.px(6))
    hairline(draw, 0, bar_h, width)
    return bar_h


def render_main_window() -> Image.Image:
    """新主界面：无框 chrome + **一组**功能键 + 唯一「退出」+ 单按钮解释入口。"""
    width, height = MAIN_W, MAIN_H
    img, draw = _blank((width, height))
    rect(draw, (0, 0, width, height), fill=theme.BG, outline=theme.OUTLINE)
    y = _chrome_bar(draw, width, "探索词典")
    bar_h = theme.px(46)
    rect(draw, (0, y, width, y + bar_h), fill=theme.PANEL)
    hairline(draw, 0, y + bar_h, width)
    # 左：搜索（主题选择在左栏 + 全库搜索完成检索：没有「全部词语」复选框）
    input_box(draw, theme.px(16), y + theme.px(10), theme.px(230), theme.px(24))
    text(draw, (theme.px(22), y + theme.px(15)), "搜索词语…", font=SANS(9),
         fill=theme.TEXT_FAINT)
    x = theme.px(254)
    for label in ("搜索", "清空"):
        bw, _bh = button(draw, x, y + theme.px(11), label, font_pt=8, padx=10, pady=3)
        x += bw + theme.px(6)
    # 右：暂停取词 / 游戏模式 / 导图 / 设置 / 退出 —— 每项只出现一次
    right = width - theme.px(10)
    for label in ("退出", "设置", "导图", "游戏模式：关", "暂停取词"):
        bw, bh = button_size(label, font_pt=8, padx=10, pady=3)
        button(draw, right - bw, y + theme.px(11), label, font_pt=8, padx=10, pady=3)
        right -= bw + theme.px(8)
    # 主体三栏：主题 / 词条卡片 / 词条详情
    top = y + bar_h
    bottom = height - theme.px(30)
    left_w = theme.px(220)
    rect(draw, (0, top, left_w, bottom), fill=theme.BG)
    text(draw, (theme.px(12), top + theme.px(10)), "主题", font=SANS(7),
         fill=theme.TEXT_MUTED)
    for index, (name, count) in enumerate(CAPTURED_TOPICS):
        row_y = top + theme.px(28) + index * theme.px(24)
        if index == 0:
            rect(draw, (theme.px(12), row_y, left_w - theme.px(12),
                        row_y + theme.px(20)), fill=theme.ACCENT_SOFT)
        text(draw, (theme.px(16), row_y + theme.px(4)), f"{name}  ({count})",
             font=SANS(9), fill=theme.TEXT if index == 0 else theme.TEXT_MUTED)
    right_w = theme.px(340)
    rect(draw, (width - right_w, top, width, bottom), fill=theme.PANEL)
    draw.line([((width - right_w) * SCALE, top * SCALE),
               ((width - right_w) * SCALE, bottom * SCALE)],
              fill=theme.BORDER, width=max(1, theme.px(1)) * SCALE)
    center_x = left_w + theme.px(12)
    text(draw, (center_x, top + theme.px(8)), f"{len(CARDS)} 条（本主题）", font=SANS(8),
         fill=theme.TEXT_MUTED)
    card_y = top + theme.px(28)
    for index, (term, summary) in enumerate(CARDS[:8]):
        row, col = divmod(index, 2)
        cw = theme.px(240)
        cx = center_x + col * (cw + theme.px(10))
        cy = card_y + row * theme.px(58)
        if cy + theme.px(50) > bottom:
            break
        rounded(draw, (cx, cy, cw, theme.px(50)), radius=theme.px(geo.CARD_RADIUS),
                fill=theme.CARD_BG, outline=theme.CARD_BORDER)
        text(draw, (cx + theme.px(10), cy + theme.px(8)), clip_tag(term),
             font=SANS(10), fill=theme.TEXT)
        if summary:
            text(draw, (cx + theme.px(10), cy + theme.px(28)), clip(summary, 14),
                 font=SANS(7), fill=theme.TEXT_FAINT)
    # 右栏详情：**一个**解释按钮（文案按状态），没有「强制重解释」第二颗
    dx0 = width - right_w + theme.px(12)
    text(draw, (dx0, top + theme.px(10)), "词条详情", font=SANS(7), fill=theme.TEXT_MUTED)
    text(draw, (dx0, top + theme.px(28)), "卷积神经网络", font=SANS(11), fill=theme.TEXT)
    yy = draw_lines(draw, dx0, top + theme.px(58),
                    wrap(draw, "一句话解释：在信号 / 图像上做加权求和的特征提取运算。",
                         font=SANS(8), max_w=right_w - theme.px(24))[:3],
                    font=SANS(8), fill=theme.TEXT_BODY, line_h=font_px(8) + 4)
    buttons = (("保存修改", False), ("解释", False))
    bx = dx0
    for label, _ in buttons:
        bw, _bh = button(draw, bx, yy + theme.px(8), label, font_pt=8, padx=10, pady=3)
        bx += bw + theme.px(6)
    bw, _bh = button_size("删除词条", font_pt=8, padx=10, pady=3)
    button(draw, width - theme.px(12) - bw, yy + theme.px(8), "删除词条", font_pt=8,
           padx=10, pady=3)
    # 底部状态行（独立一行短状态，不占工具条宽度）
    rect(draw, (0, bottom, width, height), fill=theme.PANEL)
    hairline(draw, 0, bottom, width)
    text(draw, (theme.px(12), bottom + theme.px(7)), "取词已开启", font=SANS(8),
         fill=theme.TEXT_MUTED)
    return img.resize((width, height), Image.LANCZOS)


def render_concept_map(labels=None, relations=None, topic_label: str = "") -> Image.Image:
    """导图：AI 参考关系（层级分组 / 前后分层 / 跨边 + 孤立词），**无手动录入控件**。

    位置**不是**这里另算一套：直接调用产品的纯布局函数
    ``app.ui.concept_map.layout_graph``（同一份层级 / 分层 / 跨边规则），
    只把画布坐标平移进预览的画布区域，因此预览与实机的结构一致。
    连线也画 ``edge.points`` 那份**已经避障采样好的折线**（与真实画布同一串点），
    对称关系（``edge.symmetric``，如对照）不画箭头，其余方向关系只画终点箭头。
    """
    from app.map_service import MapRelation
    from app.ui import concept_map as cm

    width, height = MAP_W, MAP_H
    img, draw = _blank((width, height))
    rect(draw, (0, 0, width, height), fill=theme.BG, outline=theme.OUTLINE)
    y = _chrome_bar(draw, width, "参考关系图")
    head_y = y + theme.px(6)
    text(draw, (theme.px(10), head_y), "主题", font=SANS(7), fill=theme.TEXT_MUTED)
    text(draw, (width - theme.px(10), head_y),
         f"{len(relations) if relations else 4} 条参考关系（已筛除 3 条无依据候选）",
         font=SANS(8), fill=theme.TEXT_MUTED, anchor="ra")
    body_top = head_y + theme.px(18)
    body_bottom = height - theme.px(76)
    # 左：简洁主题选择（列表只有主题名与词数）
    left_w = theme.px(190)
    rect(draw, (theme.px(10), body_top, theme.px(10) + left_w, body_bottom),
         fill=theme.PANEL, outline=theme.BORDER)
    for index, (name, count) in enumerate(CAPTURED_TOPICS):
        row_y = body_top + theme.px(6) + index * theme.px(22)
        if index == 0:
            rect(draw, (theme.px(12), row_y, theme.px(10) + left_w - theme.px(2),
                        row_y + theme.px(20)), fill=theme.ACCENT_SOFT)
        text(draw, (theme.px(18), row_y + theme.px(4)), f"{name}  ({count})",
             font=SANS(9), fill=theme.TEXT if index == 0 else theme.TEXT_MUTED)

    canvas_x0 = theme.px(10) + left_w + theme.px(8)
    canvas_x1 = width - theme.px(10)
    rect(draw, (canvas_x0, body_top, canvas_x1, body_bottom), fill=theme.PANEL,
         outline=theme.BORDER)

    # ---- 用**产品的**布局函数算这张图（同一份层级 / 分层 / 跨边规则）----
    # 示例边逐条自洽：evidence 原文片段本身就表达这条关系与方向（离线假材料，
    # 不是从真实库 / 真实 API 抄来的示例）。
    # 传了 ``labels`` / ``relations`` 就画那份材料（离线探针拿真实密度的大图看
    # 分带与母线，不必建窗），默认仍是下面这套小示例。
    labels = dict(labels) if labels else {
        1: "卷积神经网络", 2: "卷积层", 3: "池化层",
        4: "卷积运算", 5: "卷积核", 6: "降采样", 7: "Embedding"}
    relations = list(relations) if relations else [
        MapRelation(1, 2, "包含", "卷积神经网络包含卷积层这类网络层",
                    "卷积神经网络包含卷积层"),
        MapRelation(2, 3, "对照", "卷积层带可学习参数，池化层不带：两者形成对照",
                    "卷积层有可学习参数，池化层没有可学习参数，两者形成对照"),
        MapRelation(4, 5, "依赖", "卷积运算依赖卷积核完成逐点相乘",
                    "卷积运算依赖卷积核"),
        MapRelation(3, 6, "用途", "池化层用于降采样",
                    "池化层用于降采样，缩小特征图尺寸"),
    ]
    canvas_w = canvas_x1 - canvas_x0
    canvas_h = body_bottom - body_top
    layout = cm.layout_graph(labels, relations, width=canvas_w, height=canvas_h,
                             topic_label=topic_label or str(labels.get(1) or ""),
                             top_pad=theme.px(24))
    ox, oy = canvas_x0, body_top

    def px_(value: float) -> float:
        return (value + ox) * SCALE

    def py_(value: float) -> float:
        return (value + oy) * SCALE

    # ① 包含 / 属于的层级分组底板（结构提示，不是语义判断）
    for group in layout.groups:
        rounded(draw, (group.x0 + ox, group.y0 + oy, group.x1 - group.x0,
                       group.y1 - group.y0), radius=theme.px(10), fill=theme.PANEL_ALT)
    # ② 主题 → 顶层词的细线：分类结构（图例里明确写了「非 AI 判断」）。
    #    产品和这里一样画的是**母线折线**（主题底边 → 母线 → 词顶边），不是扇形斜线。
    for link in layout.topic_links:
        draw.line([(px_(x), py_(y)) for x, y in link], fill=theme.BORDER,
                  width=max(1, theme.px(1)) * SCALE, joint="curve")
    # ③ AI 参考关系：层级 / 方向实线、跨边虚线，末端箭头 + 类型标签
    #    画的就是 ``edge.points``（产品布局里已经按避障采样好的同一串点）；
    #    对照是对称关系（``edge.symmetric``）→ 不画箭头（两端都不画 = 无向线），
    #    其余方向关系仍然只在终点画箭头。
    for edge in layout.edges:
        style = cm.EDGE_STYLES.get(edge.kind, cm.EDGE_STYLES["cross"])
        points = [(px_(x), py_(y)) for x, y in edge.points]
        dash = style.get("dash") or ()
        if dash:
            _dashed(draw, points, fill=style["fill"], dash=dash)
        else:
            draw.line(points, fill=style["fill"], width=max(1, theme.px(1)) * SCALE,
                      joint="curve")
        if not edge.symmetric:
            _arrow(draw, edge.points[-2], edge.points[-1], offset=(ox, oy),
                   fill=style["fill"])
        # 标签**没有底色块**（用户口径：文字背景透明），颜色与线统一用一个灰
        text(draw, (edge.label_pos[0] + ox, edge.label_pos[1] + oy), edge.label,
             font=SANS(7), fill=cm.EDGE_LABEL_FILL, anchor="mm")
    # ④ 节点：圆角矩形 + **完整词语**（换行，不砍成 6 个字）
    for node in layout.nodes:
        rounded(draw, (node.x - node.w / 2 + ox, node.y - node.h / 2 + oy,
                       node.w, node.h), radius=theme.px(9),
                fill=theme.PANEL if node.isolated else theme.CARD_BG,
                outline=theme.BORDER_STRONG)
        draw.text((px_(node.x), py_(node.y)), "\n".join(node.lines), font=SANS(9),
                  fill=theme.TEXT, anchor="mm", align="center")
    # ⑤ 中心主题
    topic = layout.topic
    if topic is not None:
        rounded(draw, (topic.x - topic.w / 2 + ox, topic.y - topic.h / 2 + oy,
                       topic.w, topic.h), radius=theme.px(12),
                fill=theme.ACCENT_SOFT, outline=theme.ACCENT)
        draw.text((px_(topic.x), py_(topic.y)), "\n".join(topic.lines), font=SANS(10),
                  fill=theme.TEXT, anchor="mm", align="center")
    # ⑥ 孤立词标题（画布上**不再**画图例 / 操作提示：用户要求删掉左上角那两行，
    #    操作提示改排到画布右下角的三行浮层，见 ⑦）
    if layout.isolated_label_pos is not None:
        text(draw, (layout.isolated_label_pos[0] + ox,
                    layout.isolated_label_pos[1] + oy), cm.ISOLATED_TEXT,
             font=SANS(7), fill=theme.TEXT_FAINT)
    # ⑦ 画布右下角：三行操作提示（逐字取产品常量，行间留空档 HINT_ROW_GAP）
    hint_gap = theme.px(cm.HINT_ROW_GAP)
    hint_inset = theme.px(cm.HINT_INSET)
    hint_y = body_bottom - hint_inset
    for line in reversed(cm.HINT_LINES):
        text(draw, (canvas_x1 - hint_inset, hint_y), line, font=SANS(8),
             fill=theme.TEXT_FAINT, anchor="rs")
        hint_y -= (abs(theme.device_px(8)) + hint_gap)

    # 底：**唯一**入口（生成 / 重新生成）+ 依据行（点连线后显示的那一条）
    bar_y = body_bottom + theme.px(10)
    button(draw, theme.px(10), bar_y, "重新生成", font_pt=9, padx=14, pady=5,
           primary=True)
    text(draw, (width - theme.px(10), bar_y + theme.px(5)),
         f"{cm.MAP_TAG}：连线为模型候选，已按材料证据筛过；点击连线看依据",
         font=SANS(8), fill=theme.TEXT_FAINT, anchor="ra")
    text(draw, (theme.px(10), bar_y + theme.px(30)),
         _basis_line(relations, labels),
         font=SANS(8), fill=theme.TEXT_MUTED)
    return img.resize((width, height), Image.LANCZOS)


def _basis_line(relations, labels) -> str:
    """底部依据行 = **真实示例边**照产品格式渲染（与图上那条同源，不手写）。

    来源标注与图窗点选连线后完全一致（``concept_map.evidence_basis`` 的
    「来源：阅读页上下文 / 本工具已存释义 / 端点材料」三档）：示例边的证据
    逐字来自下面这份离线假材料 → 「来源：阅读页上下文」，**不再**把它称作
    「原文片段」（本工具不做全文查证，材料送模型前也可能被截断）。
    """
    from app.map_service import MapNode
    from app.ui import concept_map as cm

    chosen = [rel for rel in relations if rel.rel_type == "用途"][0]
    #: 预览示例的离线假材料（与关系证据同源，不是从真实库 / 真实 API 抄来的）
    materials = {
        1: "卷积神经网络包含卷积层这类网络层",
        2: "卷积层有可学习参数，池化层没有可学习参数，两者形成对照",
        3: "池化层用于降采样，缩小特征图尺寸",
        4: "卷积运算依赖卷积核完成逐点相乘",
        5: "卷积运算依赖卷积核完成逐点相乘",
        6: "池化层用于降采样，缩小特征图尺寸",
        7: "",
    }
    src = MapNode(int(chosen.src_entry_id), labels[chosen.src_entry_id],
                  materials.get(int(chosen.src_entry_id), ""), "")
    dst = MapNode(int(chosen.dst_entry_id), labels[chosen.dst_entry_id],
                  materials.get(int(chosen.dst_entry_id), ""), "")
    label, verbatim = cm.evidence_basis(chosen.evidence, src, dst, cm.metrics_for())
    note = "" if verbatim else "（未能逐字对上材料）"
    return (f"{chosen.rel_type}：{labels[chosen.src_entry_id]} → "
            f"{labels[chosen.dst_entry_id]}　依据：{chosen.reason}　"
            f"证据（{label}）：「{chosen.evidence}」{note}")


def _dashed(draw, points, *, fill, dash, width: int = 1) -> None:
    """虚线折线（跨边用；单色主题下与实线区分）。"""
    on, off = max(1, int(dash[0])), max(1, int(dash[1]))
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        length = math.hypot(x2 - x1, y2 - y1)
        if length <= 0:
            continue
        ux, uy = (x2 - x1) / length, (y2 - y1) / length
        travelled = 0.0
        while travelled < length:
            end = min(length, travelled + on)
            draw.line([(x1 + ux * travelled, y1 + uy * travelled),
                       (x1 + ux * end, y1 + uy * end)],
                      fill=fill, width=max(1, width) * SCALE)
            travelled = end + off


def _arrow(draw, start, end, *, offset, fill, size: int = 7) -> None:
    """关系线末端的箭头（方向一致的可见证据；预览专用的小三角）。"""
    x1, y1 = start[0] + offset[0], start[1] + offset[1]
    x2, y2 = end[0] + offset[0], end[1] + offset[1]
    angle = math.atan2(y2 - y1, x2 - x1)
    length = theme.px(size)
    left = (x2 - length * math.cos(angle - 0.4), y2 - length * math.sin(angle - 0.4))
    right = (x2 - length * math.cos(angle + 0.4), y2 - length * math.sin(angle + 0.4))
    draw.polygon([(x2 * SCALE, y2 * SCALE), (left[0] * SCALE, left[1] * SCALE),
                  (right[0] * SCALE, right[1] * SCALE)], fill=fill)


def render_dock() -> Image.Image:
    """独立 44x44 小方块：**圆角 10px**、透明背景，只有方块本身与图标。"""
    global DOCK_GLYPH_BOX
    size = DOCK_SIZE * SCALE
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    rounded(draw, (0, 0, DOCK_SIZE, DOCK_SIZE), radius=theme.px(geo.DOCK_RADIUS),
            fill=theme.PANEL, outline=theme.OUTLINE)
    DOCK_GLYPH_BOX = (DOCK_INSET, DOCK_INSET, DOCK_BOX, DOCK_BOX)
    center_text(draw, DOCK_GLYPH_BOX, geo.DOCK_GLYPH, font=SANS(geo.DOCK_GLYPH_PT),
                fill=theme.TEXT)
    return img.resize((DOCK_SIZE, DOCK_SIZE), Image.LANCZOS)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    out = Path(argv[0]) if argv else (Path(__file__).resolve().parent.parent
                                      / "artifacts" / "ui-preview.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    detail = out.with_name("ui-detail-preview.png")
    empty = out.with_name("ui-empty-preview.png")
    dock = out.with_name("ui-dock-preview.png")
    topic = out.with_name("ui-topic-preview.png")
    chat = out.with_name("ui-chat-preview.png")
    main_win = out.with_name("ui-main-preview.png")
    conmap = out.with_name("ui-concept-preview.png")

    render_panel(draw_list_page).save(out)
    render_panel(draw_detail_page).save(detail)
    render_panel(draw_empty_page).save(empty)
    render_dock().save(dock)
    render_panel(draw_topic_picker_page).save(topic)
    render_panel(draw_chat_page).save(chat)
    render_main_window().save(main_win)
    render_concept_map().save(conmap)

    print(f"已生成浮窗预览：{out}  ({PANEL_W}x{PANEL_H} 圆角 {geo.PANEL_RADIUS}px、"
          f"内容内缩 {INSET}px)")
    print(f"已生成详情预览：{detail}  ({PANEL_W}x{PANEL_H} 圆角 {geo.PANEL_RADIUS}px、"
          f"正常释义 + 追问；失败态的「{RETRY_TEXT}」只在居中复核时绘制)")
    print(f"已生成空态预览：{empty}  ({PANEL_W}x{PANEL_H} 圆角 {geo.PANEL_RADIUS}px、"
          f"「暂无词语」居中在词卡视口)")
    print(f"已生成独立小方块：{dock}  ({DOCK_SIZE}x{DOCK_SIZE} 圆角 "
          f"{geo.DOCK_RADIUS}px 透明背景、字形 {geo.DOCK_GLYPH_PT}pt 落在 "
          f"{DOCK_BOX}x{DOCK_BOX} 内容区)")
    def_h, chat_h = detail_area_split()
    print(f"已生成主题选择预览：{topic}  (顶栏短主题 → 右侧线条 chevron + 内联清单："
          f"「跟随当前阅读页」+ 历史主题，不用 grab 菜单)")
    print(f"已生成追问问答预览：{chat}  (完成态：问题 + 回答；释义区 {def_h}px + "
          f"对话区 {chat_h}px，两块高度都来自产品纯函数 detail_area_heights；"
          f"「正在回答…」只作为 draw_chat_page(pending=True) 的单状态检查，不单独出图)")
    print(f"已生成主界面预览：{main_win}  ({MAIN_W}x{MAIN_H}：无框自绘 chrome、"
          f"没有原生菜单栏、功能键各一组、唯一「退出」、详情只有一个解释按钮)")
    print(f"已生成导图预览：{conmap}  ({MAP_W}x{MAP_H}：AI 参考关系图 —— 包含/属于的层级"
          f"分组、依赖/因果的前后分层、用途/对照的跨边 + 孤立词；没有手动录入控件，"
          f"位置来自产品同一份 layout_graph)")
    print("注意：离线预览只用 Pillow 按产品同一份常量绘制，**不是实机证据**。")
    _rows, report = centering_report()
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
