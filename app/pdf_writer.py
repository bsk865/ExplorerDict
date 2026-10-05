"""手写精简 PDF 写入器：只干「把词条排成一份能看的 PDF」这一件事。

为什么自己写
------------
冻结运行时里没有 reportlab / fpdf / pypdf，也不该为导出功能联网装东西。
PDF 的「写字」部分本身并不复杂：一份 Type0/Identity-H 的 CID 字体 + 嵌一份
TTF 子集（:mod:`app.ttf_subset`）+ 每行一句 ``BT … Tj ET``。于是自绘。

排版分工
--------
- 本模块负责：画字、折行、分页、行高、图形（色块 / 分隔线）、把字体内嵌；
- 谁负责内容：:func:`app.export_service.pdf_bytes` 决定先写什么后写什么。

两条容易踩的坑（都真的踩过）
----------------------------
1. **宽度数组 ``/W`` 必须一个字形一条**。它原本被写成「连号字形按 100 个一组
   打包」，而 ``/W`` 的语法是「从这个 CID 起**连续**这么多个宽度」⇒ 只有前
   60 个字形拿到正确的 0.5em，后面全掉进 ``/DW 1000``（1em）⇒ 每行写到第 60
   个字就被推出纸边裁掉，看起来就是「乱码」。
2. **字体流写进文件 ≠ 被引用**。``/FontDescriptor`` 里漏掉 ``/FontFile2`` 时，
   文件里确实躺着一段字体，但没人指着它，换台电脑打开就是方块。
"""

from __future__ import annotations

import logging
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import ttf_subset

log = logging.getLogger(__name__)

# --------------------------------------------------------------------- 纸张
PAGE_WIDTH = 595.28          # A4 纵向，单位是 pt（1pt = 1/72 英寸）
PAGE_HEIGHT = 841.89
MARGIN_X = 46.0
MARGIN_TOP = 52.0
MARGIN_BOTTOM = 54.0

# --------------------------------------------------------------------- 字号
SIZE_TITLE = 17.0
SIZE_HEADING = 12.5          # 词条标题（序号 + 词语）
SIZE_FIELD = 8.6             # 字段名（「上下文」「释义」这些小标题）
SIZE_BODY = 9.5              # 正文
SIZE_VALUE = 9.0             # 字段的值
SIZE_FOOT = 7.6              # 元信息 / 页脚（来源链接、捕获时间这些）

# ----------------------------------------------------- 行高 = 字号 × 一个系数
# 每个字号配一个固定行高，系数彼此接近（1.16–1.5），这样「这段文字自己密不密」
# 是一致的；行与行之间「留多少空」则一律走 :data:`GAP_AFTER_FIELD`（见下）。
#   实测老版式的问题：卡片里的相邻行出现了 12.20 / 12.60 / 15.60 / 16.60 /
#   19.60 / 20.60 七种间距，看着就是不齐 —— 现在字段名→值 全是 13.6、
#   值→下一个字段名 全是 15.6（量了 80 + 40 处），只剩两个字号各自的行高差。
LEADING_TITLE = 25.5         # 17.0 × 1.5
LEADING_HEADING = 16.8       # 12.5 × 1.34
LEADING_BODY = 13.3          # 9.5 × 1.4
LEADING_VALUE = 12.0         # 9.0 × 1.33
LEADING_FIELD = 10.0         # 8.6 × 1.16（下面紧跟的是值，不需要整行行高）
LEADING_FOOT = 10.26         # 7.6 × 1.35

# --------------------------------------------------------------- 块间距
# 一个「模数」管三件事：字段名 → 它自己的值、值 → 下一个字段名、标题 → 第一个
# 字段。所以整张卡片里任意两组内容之间的空隙都是同一个数。
GAP_AFTER_FIELD = 3.6        # 字段名→值、值→下一个字段名、标题→第一个字段（都是它）
GAP_LABEL_TO_VALUE = GAP_AFTER_FIELD   # 名字留着，老代码读它
GAP_AFTER_HEADING = GAP_AFTER_FIELD    # 同上
GAP_AFTER_TITLE = 6.0        # 大标题 → 元信息那一行（两者基线差 25.5）
GAP_AFTER_META = 2.0         # 元信息 → 下面那行小字说明（基线差 13.9）
GAP_AFTER_NOTE = 2.6         # 小字说明 → 抬头那条横线（基线差 14.0）
GAP_AFTER_RULE = 2.0         # 抬头横线 → 第一条词条（和词条间的分隔线同一个数）
GAP_BEFORE_ENTRY = 15.1      # 词条与词条之间（上一条最后一行 → 下一条标题 30.0）

INDENT = 0.0
INDENT_VALUE = 0.0           # 卡片里的字段名与值同一条缩进线上
INDENT_CARD = 15.0           # 词条内容相对页面边距的缩进（卡片的内边距）
INDENT_META = 0.0

CARD_INSET = 15.0            # 词条内容整体往右收一点
CONTENT_WIDTH = PAGE_WIDTH - MARGIN_X * 2             # 版心宽度
CARD_WIDTH = CONTENT_WIDTH - CARD_INSET               # 卡片里一行最多能多宽

# --------------------------------------------------------------- 颜色
# 和软件界面同一套（app/ui/theme.py）：暖米底、柔黑字、无彩色系。
COLOR_TEXT = (0.110, 0.110, 0.110)      # #1C1C1C
COLOR_BODY = (0.227, 0.224, 0.212)      # #3A3936
COLOR_MUTED = (0.431, 0.424, 0.404)     # #6E6C67
COLOR_FAINT = (0.608, 0.600, 0.576)     # #9B9993
COLOR_RULE = (0.890, 0.882, 0.863)      # #E3E1DC
COLOR_RULE_STRONG = (0.784, 0.776, 0.753)   # #C8C6C0
COLOR_BAND = (0.929, 0.922, 0.902)      # #EDEBE6

BAND_WIDTH = 2.4             # 词条左侧那条小竖条的宽度
# 没有粗体字面可嵌，靠「错开一点点画两遍」加粗会糊成一团（实测试过，标题会
# 看着像重影），所以干脆不加粗 —— 层级靠字号和颜色拉开，和 HTML 那版一致。
FAUX_BOLD_SHIFT = 0.0

# --------------------------------------------------------------------- 字体
FONT_CANDIDATES: tuple[str, ...] = (
    r"C:\Windows\Fonts\simhei.ttf",      # 黑体：无衬线，标题正文都撑得住
    r"C:\Windows\Fonts\simkai.ttf",      # 楷体（黑体缺失时的替补）
    r"C:\Windows\Fonts\Deng.ttf",        # 等线
    r"C:\Windows\Fonts\msyh.ttf",        # 微软雅黑
    r"C:\Windows\Fonts\simsun.ttc",      # 宋体
    r"C:\Windows\Fonts\msyh.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)
FONT_ENV_VAR = "EXPLORER_DICT_PDF_FONT"   # 想换字体就设这个环境变量

_ESCAPE = {"\\": r"\\", "(": r"\(", ")": r"\)", "\r": r"\r", "\n": r"\n"}


def find_font() -> tuple[Path, int] | None:
    """找一个能画中文的字体，返回 (路径, 第几张脸)；找不到返回 None。

    环境变量 ``EXPLORER_DICT_PDF_FONT`` 优先（可以是 .ttf 或 .ttc）。
    """
    import os

    override = os.environ.get(FONT_ENV_VAR, "").strip().strip('"')
    candidates = ((override,) if override else ()) + FONT_CANDIDATES
    for item in candidates:
        path = Path(item)
        try:
            if path.is_file() and path.stat().st_size > 4096:
                return (path, 0)          # .ttc 固定取第 0 张脸
        except OSError:                    # pragma: no cover
            continue
    return None


def _escape(text: str) -> str:
    return "".join(_ESCAPE.get(char, char) for char in text)


def _break_opportunities(text: str) -> list[int]:
    """能断行的位置（越大越优先）：空格 > 中文标点后 > 字符之间。"""
    return [index + 1 for index, char in enumerate(text)
            if char.isspace() or char in "，。；：！？、）】》」』"]


def wrap_text(text: str, font: ttf_subset.TTFont | None, size: float,
              width: float) -> list[str]:
    """把 ``text`` 折成不超过 ``width`` 的行（贪心 + 二分找最长前缀）。"""
    text = str(text or "")
    if not text:
        return [""]
    if font is None:                       # 没字体就按「一个字 0.62em」粗算
        per_line = max(1, int(width / (size * 0.62)))
        return [text[i:i + per_line] for i in range(0, len(text), per_line)] or [""]

    lines: list[str] = []
    rest = text
    while rest:
        if font.text_width(rest, size) <= width:
            lines.append(rest)
            break
        low, high = 1, len(rest)
        while low < high:
            mid = (low + high + 1) // 2
            if font.text_width(rest[:mid], size) <= width:
                low = mid
            else:
                high = mid - 1
        cut = low
        breaks = [spot for spot in _break_opportunities(rest[:cut]) if spot > 0]
        if breaks:
            cut = breaks[-1]
        lines.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip() if rest[cut:cut + 1] == " " else rest[cut:]
    return lines or [""]


@dataclass
class _Line:
    """一页里即将画出的一行字（**先攒文字、最后统一编码**：字形子集要在

    「所有用到的字都收齐」之后才做得出来，见 :meth:`PdfDoc.build`）。"""

    text: str
    size: float
    indent: float
    color: tuple[float, float, float]
    leading: float = 0.0
    align_right: bool = False
    letter_spacing: float = 0.0
    bold: bool = False
    band: bool = False          # 这一行左边要不要画一条小竖条


@dataclass
class _Mark:
    """页面上的一小块图形（实体矩形 / 细线）。"""

    kind: str                   # "rect" | "line"
    x: float
    y: float
    w: float
    h: float
    color: tuple[float, float, float]


@dataclass
class _Page:
    lines: list[_Line] = field(default_factory=list)
    marks: list[_Mark] = field(default_factory=list)
    _orphans: list[float] = field(default_factory=list)   # 记「掉在页底」的那几行


class PdfDoc:
    """极简 PDF 写入器：加页、写字、画线、自动分页，最后 :meth:`build`。

    坐标用 PDF 的常规坐标系（原点左下、y 向上），排版时内部维护一个
    「下一条内容画在哪」的游标 ``_y``。
    """

    def __init__(self, title: str = "", *, font_path=None, face_index: int = 0):
        self.doc_title = str(title or "")
        self.pages: list[_Page] = []
        self._current: _Page | None = None
        self._y = 0.0
        self.font: ttf_subset.TTFont | None = None
        self.embedded = False
        self.font_path = ""
        self._used_glyphs: dict[int, int] = {}    # glyph id -> 字符码点
        self._subset: bytes | None = None
        self.dry_run = False                      # True = 只量高度，不落内容
        self._measured_pages = 0                  # dry_run 下「用掉几页」
        self._measured_used = 0.0                 # dry_run 下「内容一共多高」

        if font_path is None:
            found = find_font()
            font_path, face_index = (found if found else (None, 0))
        if font_path is not None:
            try:
                self.font = ttf_subset.load_ttf(font_path, face_index=face_index)
                self.embedded = True
                self.font_path = str(font_path)
            except Exception as exc:                          # pragma: no cover
                log.warning("PDF 字体读不了，改成不嵌字体：%s", exc)
                self.font = None
                self.embedded = False
        else:
            log.info("系统里没找到可嵌入 PDF 的中文字体，PDF 里的中文会是空白")

    # ------------------------------------------------------------- 页面
    @property
    def page_count(self) -> int:
        return len(self.pages) + (1 if self._current is not None else 0)

    @property
    def content_width(self) -> float:
        """一行字最多能有多宽（左右各让出一个页边距）。"""
        return PAGE_WIDTH - MARGIN_X * 2

    def new_page(self, *, keep_empty: bool = False) -> None:
        """开新页（当前页一个字都没写时，``keep_empty=False`` 会把它丢掉）。

        量高度时（``dry_run``）不真的分页，只把游标拉回页顶、把页数记在
        ``_measured_pages`` 里 —— 因为量出来的页本身就是空的。
        """
        if self.dry_run:
            self._measured_pages += 1
            # 翻页前这一页用掉的高度 = 游标离页底还有多远（**不是** _y 减页顶，
            # 那样写出来是负的 —— 踩过这个坑，量出来的高度成了 -1153）
            self._measured_used += max(0.0, self._y - MARGIN_BOTTOM)
            self._current = self._current or _Page()
            self._y = PAGE_HEIGHT - MARGIN_TOP
            return
        if self._current is not None:
            if keep_empty or self._current.lines or self._current.marks:
                self.pages.append(self._current)
        self._current = _Page()
        self._y = PAGE_HEIGHT - MARGIN_TOP

    def _ensure(self) -> _Page:
        if self._current is None:
            self.new_page()
        return self._current

    def y(self) -> float:
        """当前游标（下一条内容画在哪）。"""
        return self._y

    def set_y(self, value: float) -> None:
        self._ensure()
        self._y = float(value)

    # ------------------------------------------------------------- 量
    def text_width(self, text: str, size: float, *,
                   letter_spacing: float = 0.0) -> float:
        """一段文字在 ``size`` 磅下的宽度（点）—— 靠右排版就靠它。"""
        if self.font is None:
            base = len(text) * size * 0.62
        else:
            base = self.font.text_width(text, size)
        return base + max(0.0, letter_spacing) * max(0, len(text) - 1)

    # ------------------------------------------------------------- 落笔
    def _glyph_codes(self, text: str) -> str:
        """文字 → Identity-H 的十六进制串（每个字符 2 字节字形编号）。

        ``0000`` 表示字体里没有这个字形（PDF 阅读器画空白，不会崩）。
        没有嵌入字体时退化成 Latin-1 单字节（只能画西方字符）。
        """
        codes: list[str] = []
        for char in text:
            if self.font is not None:
                glyph = self.font.glyph_for(char)
                if glyph:
                    self._used_glyphs.setdefault(glyph, ord(char))
                    codes.append(f"{glyph:04X}")
                else:
                    codes.append("0000")
            else:
                code = ord(char)
                codes.append(f"{code:02X}" if code < 0x100 else "3F")
        return "".join(codes)

    def _leading(self, item: _Line) -> float:
        if item.leading:
            return float(item.leading)
        if item.size <= SIZE_VALUE + 0.4:
            return LEADING_VALUE
        if item.size <= SIZE_BODY + 0.4:
            return LEADING_BODY
        if item.size <= SIZE_HEADING + 0.4:
            return LEADING_HEADING
        return item.size * 1.4

    def _emit(self, item: _Line) -> None:
        """把一行放上去；放不下就翻页，**并且把上一行一起带走**。

        「带走上一行」是为了不让一个词条标题孤零零留在上一页末尾。
        量高度时（``dry_run``）只走 ``_y`` 和翻页，不往页上落东西。
        """
        page = self._ensure()
        if self._y - self._leading(item) < MARGIN_BOTTOM:
            self.new_page()
            page = self._ensure()
            busy = self.pages[-1] if self.pages else None
            if busy is not None:
                # 把上一页**末尾连着的那几行**搬到新页开头，免得一个词条标题
                # 孤零零留在上一页最后一行（_orphans 目前恒为空，留给正文里
                # 需要「标题 + 首行」绑在一起的场景）。
                keep = busy._orphans.pop() if busy._orphans else 0
                if keep:
                    cut = len(busy.lines) - keep
                    moved = busy.lines[cut:]
                    del busy.lines[cut:]
                    page.lines.extend(moved)
        if not self.dry_run:
            if item.band:
                self._band(page, item.indent)
            page.lines.append(item)
        self._y -= self._leading(item)

    def _band(self, page: _Page, indent: float = INDENT) -> None:
        """词条左边那个小色块（和界面上的「药丸」是同一个灰）。"""
        x = MARGIN_X + float(indent) - BAND_WIDTH - 5.0
        page.marks.append(_Mark("rect", x, self._y - 2.0, BAND_WIDTH, 14.0,
                                COLOR_BAND))

    # ------------------------------------------------------------- 写字
    def line(self, text: str, *, size: float = SIZE_BODY, indent: float = INDENT,
             color=COLOR_TEXT, leading: float = 0.0, align_right: bool = False,
             letter_spacing: float = 0.0, bold: bool = False,
             band: bool = False) -> None:
        """写一行（不换行、不分页判断之外不做别的）。"""
        self._emit(_Line(str(text or ""), float(size), float(indent), tuple(color),
                         float(leading), bool(align_right), float(letter_spacing),
                         bool(bold), bool(band)))

    def paragraph(self, text: str, *, size: float = SIZE_BODY, color=COLOR_TEXT,
                  indent: float = INDENT, leading: float = 0.0,
                  gap_after: float = GAP_AFTER_FIELD, bold: bool = False,
                  width: float | None = None) -> None:
        """写一段（自动折行 + 自动分页）。空字符串只吃掉一个行高。

        ``width`` 给定了就按它折行（默认铺满版心）—— 卡片式排版靠它把正文
        收在卡片里。
        """
        text = str(text or "")
        if not text.strip():
            self._ensure()
            self._y -= LEADING_BODY
            return
        usable = float(width) if width else self.content_width - indent
        usable = max(24.0, usable - indent)
        lines = wrap_text(text, self.font, size, usable)
        for line_text in lines:
            self.line(line_text, size=size, indent=indent, color=color,
                      leading=leading, bold=bold)
        self._y -= gap_after

    def field(self, label: str, value: str, *, size: float = SIZE_VALUE,
              label_size: float = SIZE_FIELD, label_color=COLOR_MUTED,
              color=COLOR_BODY, indent: float = INDENT_VALUE,
              gap_after: float = GAP_AFTER_FIELD, width: float | None = None,
              gap_before: float = 0.0) -> None:
        """一个字段：**字段名单独一行，值缩进挂在下面**。

        这样排的原因：字段名和值同处一行时，长句折行后会顶到页边、读起来
        分不清哪儿是名哪儿是值；分开排一眼就能扫到「释义」「例子」这些锚点。

        间距全部走同一个模数（:data:`GAP_AFTER_FIELD`）：字段名 → 值、值 →
        下一个字段名、标题 → 第一个字段，都是它 —— 这样一列字段看下来是
        等距的，不会有的一行挤、有的隔得远。
        """
        value = str(value or "").strip()
        if not value:
            return
        self._ensure()
        self._y -= float(gap_before)
        self.line(label, size=label_size, color=label_color, indent=indent,
                  leading=LEADING_FIELD)
        self._y -= GAP_LABEL_TO_VALUE
        self.paragraph(value, size=size, color=color, indent=indent,
                       leading=LEADING_VALUE if size <= SIZE_VALUE + 0.4 else LEADING_BODY,
                       gap_after=gap_after, width=width)

    def chip(self, text: str, x: float, *, size: float = SIZE_FIELD,
             pad_x: float = 5.0, height: float = 12.4,
             bg=COLOR_BAND, fg=COLOR_BODY, gap: float = 4.0) -> float:
        """在游标那一行画一个圆角小标签（标签药丸），返回下一个该放的 x。

        PDF 的 ``re`` 操作符没有圆角，这里近似成两条竖线 + 一个圆头矩形，
        远看和界面上的药丸是一个东西。
        """
        text = str(text or "").strip()
        if not text:
            return x
        width = self.text_width(text, size) + pad_x * 2
        page = self._ensure()
        if not self.dry_run:
            page.marks.append(_Mark("pill", x, self._y - 2.6, width, height, tuple(bg)))
        self.line(text, size=size, indent=x - MARGIN_X + pad_x, color=fg,
                  leading=0.0)
        self._y += self._leading(_Line("", size, 0.0, fg))   # 抵消 line() 吃掉的行高
        return x + width + gap

    def tagline(self, tags, *, x: float, size: float = SIZE_FIELD,
                gap_after: float = GAP_AFTER_FIELD) -> None:
        """一行放若干个标签药丸（放不下就换行）。"""
        cursor = float(x)
        right = MARGIN_X + self.content_width
        self._ensure()
        for tag in tags:
            text = str(tag or "").strip()
            if not text:
                continue
            width = self.text_width(text, size) + 10.0
            if cursor + width > right and cursor > x:
                self._y -= 15.0
                cursor = float(x)
            cursor = self.chip(text, cursor, size=size)
        self._y -= gap_after

    def footer_line(self, text: str) -> None:
        """页脚那一行（右对齐、浅灰）。"""
        self.line(text, size=SIZE_FOOT, color=COLOR_FAINT, align_right=True,
                  leading=LEADING_FOOT)

    def heading(self, text: str, *, size: float = SIZE_HEADING,
                indent: float = INDENT) -> None:
        """词条标题：带左侧小色块，翻页时会把标题一起带到下一页。"""
        self._emit(_Line(str(text or ""), float(size), float(indent), COLOR_TEXT,
                         LEADING_HEADING, False, 0.0, False, True))

    def title(self, text: str, *, size: float = SIZE_TITLE) -> None:
        self.line(text, size=size, color=COLOR_TEXT, leading=LEADING_TITLE)

    # ------------------------------------------------------------- 画图
    def rect(self, x: float, y: float, w: float, h: float, *,
             color=COLOR_RULE) -> None:
        """实心矩形（细线也用它：高度给很小即可）。"""
        page = self._ensure()
        if self.dry_run:
            return
        page.marks.append(_Mark("rect", float(x), float(y), float(w), float(h),
                                tuple(color)))

    def rule(self, *, color=COLOR_RULE, width: float = 0.8,
             x: float = MARGIN_X, span: float | None = None,
             keep_with_next: float = 0.0) -> None:
        """一条横线，画在当前游标稍下方一点点。

        ``keep_with_next`` 给正数时表示「这条线后面至少还要留这么多空间」——
        不够就先把线挪到下一页去，免得一条分隔线孤零零掉在页底（判断在画线
        **之前**做，否则线已经画在上一页了，再翻页就成了一条莫名其妙的孤线）。
        """
        self._ensure()
        if keep_with_next and self._y - keep_with_next - 8.0 < MARGIN_BOTTOM:
            self.new_page()
        page = self._ensure()
        self._y -= 4.0
        if not self.dry_run:
            page.marks.append(_Mark("rect", float(x), self._y,
                                    float(span if span is not None else self.content_width),
                                    max(0.4, float(width)), tuple(color)))
        self._y -= 4.0

    def space(self, amount: float) -> None:
        self._ensure()
        self._y -= float(amount)

    # ------------------------------------------------------------- 量身
    @classmethod
    def measure(cls, render, *, start_y: float | None = None,
                 height: float | None = None, pad_top: float = 0.0) -> tuple[float, int]:
        """量一段内容从**页顶**铺下来要多高、要几页，但**什么都不画**。

        用法是「先量、再决定要不要翻页、最后真的画一遍」：一个词条如果放到
        页底会从中间劈开，就先把整条挪到下一页去（HTML 那版是靠
        ``break-inside: avoid`` 做这件事的，PDF 这边得自己算）。

        量的时候**固定从页顶开始**，这样同一个词条量多少次都是同一个数
        （拿 ``start_y`` 当起点去量的话，折行会随剩下的高度变化，量出来的
        高度也跟着变 —— 298 / 366 / 673 三个数都量出来过，判断就乱了）。
        返回 ``(高度, 页数)``；``height`` 给「一新页有多少空间」，默认一整页。

        ``pad_top`` 补的是**第一条线落笔之前**那一格行高：游标指的是「第一条线
        的基线」，可这段内容真正的上边界在基线之上一个行高。不补的话量出来的
        高度会比实际占地少一格 —— 判定「刚刚好放得下」的内容真画时会从页底
        溢出一行（踩过：词条的标题那格 18pt 没算，10 条词平白多出 4 个只有
        一行字的页）。谁量谁负责把这一格报出来。
        """
        full = (height if height is not None else 0.0) or (PAGE_HEIGHT - MARGIN_TOP - MARGIN_BOTTOM)
        doc = cls("")
        doc.dry_run = True
        doc._current = _Page()     # 量的时候不落内容，这页只是给游标一个起点
        doc._measured_pages = 0    # 上面这句不算翻页，别用 new_page() 免得记成 1 页
        doc._measured_used = 0.0
        doc._y = MARGIN_BOTTOM + float(full)
        top = doc._y
        render(doc)
        # 内容从页顶铺下来多高：翻过去的那几页是整页，最后这页看游标走到哪儿。
        # 不能写成「起点减终点」—— 中途翻页后游标已经回到页顶，减出来是负数
        # （踩过：-466.99，于是「放不下」的判断全失灵）。
        used = doc._measured_pages * float(full) + (top - doc._y) + float(pad_top)
        pages = 1 + doc._measured_pages
        return used, pages

    # ------------------------------------------------------------- 产出
    def _subset_bytes(self) -> bytes:
        """把「整份文档用到的字符」做成本次嵌入的子集字体（只做一次）。"""
        if self.font is None:
            return b""
        if self._subset is None:
            chars = "".join(chr(code) for code in sorted(set(self._used_glyphs.values())))
            self._subset = ttf_subset.subset_from(self.font, chars)
        return self._subset

    def _content_stream(self, page: _Page, *, footer: str) -> bytes:
        """一页的内容流：先画图形，再逐行写字（页脚靠右下角）。"""
        commands: list[str] = []
        for mark in page.marks:
            if mark.kind == "rect":
                commands.append("%.3f %.3f %.3f rg %.2f %.2f %.2f %.2f re f" % (
                    mark.color[0], mark.color[1], mark.color[2],
                    mark.x, mark.y, mark.w, mark.h))
            elif mark.kind == "pill":
                commands.extend(self._pill_ops(mark))
            else:                                   # pragma: no cover - 备用
                commands.append("%.3f %.3f %.3f RG %.2f w %.2f %.2f m %.2f %.2f l S" % (
                    mark.color[0], mark.color[1], mark.color[2],
                    max(0.4, mark.h), mark.x, mark.y, mark.x + mark.w, mark.y))

        y = PAGE_HEIGHT - MARGIN_TOP
        for item in page.lines:
            codes = self._glyph_codes(item.text)
            if item.align_right:
                x = PAGE_WIDTH - MARGIN_X - self.text_width(
                    item.text, item.size, letter_spacing=item.letter_spacing)
            else:
                x = MARGIN_X + item.indent
            commands.append(self._text_op(item, x, y, codes))
            if item.bold:
                # 没有粗体字面：错开一点点再画一遍，远看就是「加粗」
                commands.append(self._text_op(item, x + FAUX_BOLD_SHIFT, y, codes))
            if item.letter_spacing:
                # 有字距时必须一个字一个字画，否则 Tj 会自己挤在一起
                commands.pop()
                if item.bold:
                    commands.pop()
                for index in range(len(codes) // 4):
                    commands.append(self._text_op(
                        item, x + index * (item.size + item.letter_spacing), y,
                        codes[index * 4:index * 4 + 4]))
                    if item.bold:
                        commands.append(self._text_op(
                            item, x + index * (item.size + item.letter_spacing)
                            + FAUX_BOLD_SHIFT, y, codes[index * 4:index * 4 + 4]))
            y -= self._leading(item)

        footer_codes = self._glyph_codes(footer)
        footer_x = PAGE_WIDTH - MARGIN_X - self.text_width(footer, SIZE_FOOT)
        commands.append(
            "BT %.3f %.3f %.3f rg /F1 %.2f Tf 1 0 0 1 %.2f %.2f Tm <%s> Tj ET" % (
                COLOR_FAINT[0], COLOR_FAINT[1], COLOR_FAINT[2], SIZE_FOOT,
                footer_x, 30.0, footer_codes))
        return "\n".join(commands).encode("latin-1", "replace")

    @staticmethod
    def _pill_ops(mark: _Mark) -> list[str]:
        """圆角小标签：中间一个矩形、两端各一个满圆（近似圆角）。"""
        r = min(mark.h / 2.0, (mark.w - 0.4) / 2.0)
        if r <= 0.3:
            return ["%.3f %.3f %.3f rg %.2f %.2f %.2f %.2f re f" % (
                mark.color[0], mark.color[1], mark.color[2],
                mark.x, mark.y, mark.w, mark.h)]
        ops = ["%.3f %.3f %.3f rg" % mark.color]
        ops.append("%.2f %.2f %.2f %.2f re f" % (mark.x + r, mark.y,
                                                 max(0.2, mark.w - 2 * r), mark.h))
        for cx in (mark.x + r, mark.x + mark.w - r):
            ops.append("%.2f %.2f m %.2f %.2f %.2f %.2f %.2f %.2f c "
                       "%.2f %.2f %.2f %.2f %.2f %.2f c f" % (
                           cx, mark.y,
                           cx + r * 0.5523, mark.y, cx + r, mark.y + r * 0.4477,
                           cx + r, mark.y + r,
                           cx + r, mark.y + r + r * 0.5523, cx + r * 0.5523,
                           mark.y + mark.h, cx, mark.y + mark.h))
        return ops

    @staticmethod
    def _text_op(item: _Line, x: float, y: float, codes: str) -> str:
        return "BT %.3f %.3f %.3f rg /F1 %.2f Tf 1 0 0 1 %.2f %.2f Tm <%s> Tj ET" % (
            item.color[0], item.color[1], item.color[2], item.size, x, y, codes)

    def _font_objects(self, base: int) -> tuple[dict[int, bytes], int]:
        """字体那 5 个对象（Type0 / CIDFont / Descriptor / FontFile2 / ToUnicode）。

        **调用前**必须把整份文档都编码过一遍（:meth:`_glyph_codes`），否则
        ``_used_glyphs`` 是空的 —— 子集字体里会一个字都没有。
        """
        if self.font is None:
            helvetica = (b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                         b"/Encoding /WinAnsiEncoding >>")
            return {base: helvetica}, base + 1

        blob = self._subset_bytes()
        compressed = zlib.compress(blob, 9)

        # ToUnicode：2 字节字形编号 → Unicode
        to_unicode = [
            b"/CIDInit /ProcSet findresource begin",
            b"12 dict begin",
            b"begincmap",
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
            b"/CMapName /Adobe-Identity-UCS def",
            b"/CMapType 2 def",
            b"1 begincodespacerange",
            b"<0000> <FFFF>",
            b"endcodespacerange",
        ]
        items = sorted(self._used_glyphs.items())
        for start in range(0, len(items), 100):
            chunk = items[start:start + 100]
            to_unicode.append(b"%d beginbfchar" % len(chunk))
            for glyph, code in chunk:
                to_unicode.append(b"<%04X> <%04X>" % (glyph, code))
            to_unicode.append(b"endbfchar")
        to_unicode += [
            b"endcmap",
            b"CMapName currentdict /CMap defineresource pop",
            b"end",
            b"end",
        ]
        cmap_stream = b"\n".join(to_unicode)

        # 宽度数组：**一个字形一条** `gid [宽度]`。
        #
        # 别图省事把连号的字形打包成 ``起始CID [w1 w2 …]`` —— PDF 的语法是
        # 「从这个 CID 开始**连续**这么多个宽度」，中间没被列到的 CID 会掉进
        # ``/DW``（1000 = 1em）。一开始就是这么写的：60 个宽度塞进一个组里，
        # 第 61 个字形之后全部按两倍宽度推进，**每行写到第 60 个字就被推出
        # 纸边裁掉**（2026-10-05 用户看到的「乱码」就是它）。
        glyphs = sorted(self._used_glyphs)
        widths = b" ".join(
            b"%d [%d]" % (g, self.font.advance(g) * 1000 // self.font.units_per_em)
            for g in glyphs
        ) or b"0 []"

        scale = 1000.0 / self.font.units_per_em
        upem = int(self.font.units_per_em * scale)
        # /FontFile2 必须指到那个字体流：漏了它，PDF 里就只剩一个「名字」，
        # 换台电脑打开就是方块（这也是踩过的坑，见模块 docstring）。
        descriptor = (
            b"<< /Type /FontDescriptor /FontName /Embedded /Flags 4 "
            b"/FontBBox [%d %d %d %d] /ItalicAngle 0 /Ascent %d /Descent %d "
            b"/CapHeight %d /StemV 80 /FontFile2 %d 0 R >>" % (
                0, int(-0.22 * upem), upem, upem, int(0.86 * upem),
                int(-0.22 * upem), int(0.72 * upem), base + 3))
        cid_font = (
            b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /Embedded "
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
            b"/FontDescriptor %d 0 R /W [%s] /DW 1000 /CIDToGIDMap /Identity >>" % (
                base + 2, widths))
        type0 = (
            b"<< /Type /Font /Subtype /Type0 /BaseFont /Embedded "
            b"/Encoding /Identity-H /DescendantFonts [%d 0 R] "
            b"/ToUnicode %d 0 R >>" % (base + 1, base + 4))

        font_file = (b"<< /Length %d /Length1 %d /Filter /FlateDecode >>\nstream\n"
                     % (len(compressed), len(blob))) + compressed + b"\nendstream"
        cmap_obj = (b"<< /Length %d >>\nstream\n" % len(cmap_stream)
                    ) + cmap_stream + b"\nendstream"

        return {
            base: type0,
            base + 1: cid_font,
            base + 2: descriptor,
            base + 3: font_file,
            base + 4: cmap_obj,
        }, base + 5

    def build(self, body_text: str = "") -> bytes:
        """收尾并产出 PDF 字节。"""
        if self._current is not None:
            page = self._current
            if page.lines or page.marks:
                self.pages.append(page)
            self._current = None
        if not self.pages:
            self.new_page()

        total_pages = len(self.pages)
        page_blobs = [self._content_stream(page, footer=f"{index + 1} / {total_pages}")
                      for index, page in enumerate(self.pages)]
        # 页脚 / 页码里的字也要进子集
        for char in str(body_text or "") + "0123456789 /第页":
            self._glyph_codes(char)

        font_base = 3
        objects, next_id = self._font_objects(font_base)

        kids = []
        for index, blob in enumerate(page_blobs):
            page_id = next_id
            content_id = next_id + 1
            next_id += 2
            kids.append(page_id)
            objects[page_id] = (
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f] "
                b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>" % (
                    PAGE_WIDTH, PAGE_HEIGHT, font_base, content_id))
            objects[content_id] = (
                b"<< /Length %d >>\nstream\n" % len(blob)) + blob + b"\nendstream"

        objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
        objects[2] = b"<< /Type /Pages /Count %d /Kids [%s] >>" % (
            len(kids), b" ".join(b"%d 0 R" % kid for kid in kids))

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets: dict[int, int] = {}
        for obj_id in sorted(objects):
            offsets[obj_id] = len(out)
            out += b"%d 0 obj\n" % obj_id
            out += objects[obj_id]
            out += b"\nendobj\n"

        max_id = max(objects)
        xref_at = len(out)
        out += b"xref\n0 %d\n" % (max_id + 1)
        out += b"0000000000 65535 f \n"
        for obj_id in range(1, max_id + 1):
            offset = offsets.get(obj_id)
            if offset is None:
                out += b"0000000000 65535 f \n"
            else:
                out += b"%010d 00000 n \n" % offset
        out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
            max_id + 1, xref_at)
        return bytes(out)


def sample_pdf(title: str, blocks: list[str], *, body_text: str = "") -> bytes:
    """调试用：把几段文字排成一份 PDF（``tools`` 里的自检脚本会用到）。"""
    doc = PdfDoc(title)
    doc.new_page()
    doc.title(title or "样例")
    doc.rule(color=COLOR_RULE_STRONG)
    doc.space(6.0)
    for index, block in enumerate(blocks, start=1):
        doc.heading(f"{index}. {str(block).splitlines()[0][:40]}")
        doc.field("正文", str(block))
    return doc.build(body_text)
