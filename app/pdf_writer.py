"""手写一个精简 PDF（导出用，**只用标准库**）。

为什么不是 reportlab / fpdf
---------------------------
冻结运行时（``探索词典.exe`` 旁边的 ``_runtime``）里只有标准库 + Pillow，
装不了第三方库；而 PDF 要显示中文，就必须**自己嵌字体**。所以这个文件把一个
「够用的 PDF 1.4 写入器」从头写出来：

* A4 纵向，左右 46pt 边距；标题 / 小标题 / 正文三种字号，正文自动换行 +
  自动分页，页脚印「第 n / N 页」；
* 字体走 **Identity-H 编码 + CIDFontType2 嵌入子集**（见 :mod:`app.ttf_subset`）：
  每个字符写成一个 2 字节的字形编号，再配一张 ``ToUnicode`` CMap —— 于是
  文字**能选中、能搜索、能复制**，不是把字画成图形；
* 颜色用 ``rg``（默认近黑 + 灰色小字），不做线条 / 方框 / 表格（版式朴素是
  刻意的：这是「把词表排出来」的出口，不是排版软件）。

写出来的文件结构（对象编号固定，便于测试与排查）::

    1 Catalog → 2 Pages → 每页一个 Page 对象
    页 = Page + Content 流 + 共享的字体资源
    字体 = Type0 Font / CIDFontType2 DescendantFont / FontDescriptor
           / FontFile2（嵌入子集）/ ToUnicode 流

字体找不到时（非 Windows / 字体文件缺失）不抛异常：:class:`PdfDoc` 退化成
「只画得出拉丁字符」——调用方拿到 :attr:`PdfDoc.embedded` 就知道有没有嵌成。
"""
from __future__ import annotations

import os
import zlib
from dataclasses import dataclass
from pathlib import Path

from . import ttf_subset
from .logging_setup import get_logger

log = get_logger("pdf")

#: A4 纵向（点）
PAGE_WIDTH = 595.28
PAGE_HEIGHT = 841.89
MARGIN_X = 46.0
MARGIN_TOP = 52.0
MARGIN_BOTTOM = 54.0

#: 字号（点）：标题 / 小标题 / 正文 / 页脚
SIZE_TITLE = 17.0
SIZE_HEADING = 11.5
SIZE_BODY = 9.5
SIZE_FOOT = 8.0
#: 正文行高
LEADING_BODY = 14.0
#: 段间距
GAP_AFTER_TITLE = 10.0
GAP_BEFORE_HEADING = 12.0
GAP_AFTER_HEADING = 4.0
GAP_AFTER_BLOCK = 6.0
#: 左缩进（正文第二行起的悬挂缩进用不上，这里只做整体缩进）
INDENT = 0.0

#: 颜色（近黑 / 灰 / 浅灰）
COLOR_TEXT = (0.12, 0.12, 0.12)
COLOR_MUTED = (0.42, 0.42, 0.40)
COLOR_FAINT = (0.62, 0.62, 0.60)

#: 可以嵌进 PDF 的字体（按优先级找；SimHei 是**独立 TTF**，最好解析）
FONT_CANDIDATES: tuple[str, ...] = (
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simkai.ttf",
    r"C:\Windows\Fonts\Deng.ttf",
    r"C:\Windows\Fonts\msyh.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)
#: 环境变量可以指定（留了口子给「系统里没有上面这些字体」的机器）
FONT_ENV_VAR = "EXPLORER_DICT_PDF_FONT"

#: 这些字符在 PDF 内容流里必须转义
_ESCAPE = {"\\": r"\\", "(": r"\(", ")": r"\)", "\r": r"\r", "\n": r"\n"}


def find_font() -> tuple[Path, int] | None:
    """找一份能嵌进 PDF 的中文字体，返回 ``(路径, 集合内序号)``；找不到返回 None。

    ``.ttc`` 是字体集合，这里固定取第 0 张（SimHei 是独立 ``.ttf``，优先）。
    """
    override = os.environ.get(FONT_ENV_VAR, "").strip()
    candidates = ((override,) if override else ()) + FONT_CANDIDATES
    for item in candidates:
        if not item:
            continue
        path = Path(item)
        try:
            if path.is_file():
                return path, 0
        except OSError:                                       # pragma: no cover
            continue
    return None


def _escape(text: str) -> str:
    return "".join(_ESCAPE.get(char, char) for char in text)


def _break_opportunities(text: str) -> list[int]:
    """可以断行的位置（在 ``i`` 之后断），按优先级：空格 > 中文标点 > 字符之间。"""
    spaces, punctuation = [], []
    for i, char in enumerate(text[:-1]):
        if char in " \t":
            spaces.append(i)
        elif char in "，。；：！？、）」』】…—·,.;:!?)]}":
            punctuation.append(i)
    return spaces or punctuation or list(range(len(text) - 1))


def wrap_text(text: str, font: ttf_subset.TTFont | None, size: float,
              width: float) -> list[str]:
    """把 ``text`` 按 ``width`` 磅宽折行（贪心；没字体时按「一个汉字 = 1em」估）。

    折行位置优先空格，其次中文标点，最后才是字符之间 —— 中文没有空格，
    所以「哪儿都能断」是必须的；英文单词尽量保持完整。
    """
    text = str(text or "")
    if not text:
        return [""]
    unit = size if font is None else float(font.units_per_em)

    def measure(part: str) -> float:
        if font is None:
            return len(part) * size * 0.62
        return font.text_width(part, size)

    lines: list[str] = []
    rest = text
    while rest:
        if measure(rest) <= width:
            lines.append(rest)
            break
        # 二分找最长能放下的前缀
        low, high = 1, len(rest)
        while low < high:
            mid = (low + high + 1) // 2
            if measure(rest[:mid]) <= width:
                low = mid
            else:
                high = mid - 1
        cut = low
        candidates = [pos + 1 for pos in _break_opportunities(rest[:cut])
                      if 0 < pos + 1 <= cut]
        if candidates:
            cut = max(candidates)
        line = rest[:cut].rstrip()
        lines.append(line)
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
    align_right: bool = False


class PdfDoc:
    """极简 PDF 写入器：加页、写字、自动分页，最后 :meth:`build`。"""

    def __init__(self, title: str = "", *, font_path=None, face_index: int = 0):
        self.doc_title = str(title or "")
        self.pages: list[list[_Line]] = []        # 每页的「待画文字行」
        self._current: list[_Line] | None = None
        self._y = 0.0
        self.font: ttf_subset.TTFont | None = None
        self.embedded = False
        self.font_path = ""
        self._used_glyphs: dict[int, int] = {}    # glyph id -> 字符码点
        self._subset: bytes | None = None

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

    def new_page(self, *, keep_empty: bool = False) -> None:
        """开新页（当前页一个字都没写时，``keep_empty=False`` 会把它丢掉）。"""
        if self._current is not None:
            if keep_empty or self._current:
                self.pages.append(self._current)
        self._current = []
        self._y = PAGE_HEIGHT - MARGIN_TOP

    def _ensure(self) -> list[_Line]:
        if self._current is None:
            self.new_page()
        return self._current

    # ------------------------------------------------------------- 写字
    def text_width(self, text: str, size: float) -> float:
        """一段文字在 ``size`` 磅下的宽度（点）——页脚靠右就靠它。"""
        if self.font is None:
            return len(text) * size * 0.62
        return self.font.text_width(text, size)

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

    def _content_stream(self, lines: list[_Line], *, footer: str) -> bytes:
        """一页的内容流：逐行写字（页脚靠右下角）。"""
        y = PAGE_HEIGHT - MARGIN_TOP
        commands: list[str] = []
        for item in lines:
            codes = self._glyph_codes(item.text)
            if item.align_right:
                x = PAGE_WIDTH - MARGIN_X - self.text_width(item.text, item.size)
            else:
                x = MARGIN_X + item.indent
            commands.append(
                "BT %.3f %.3f %.3f rg /F1 %.2f Tf 1 0 0 1 %.2f %.2f Tm <%s> Tj ET" % (
                    item.color[0], item.color[1], item.color[2], item.size, x, y, codes))
            y -= LEADING_BODY if item.size <= SIZE_BODY else item.size * 1.35
        footer_codes = self._glyph_codes(footer)
        footer_x = PAGE_WIDTH - MARGIN_X - self.text_width(footer, SIZE_FOOT)
        commands.append(
            "BT %.3f %.3f %.3f rg /F1 %.2f Tf 1 0 0 1 %.2f %.2f Tm <%s> Tj ET" % (
                COLOR_FAINT[0], COLOR_FAINT[1], COLOR_FAINT[2], SIZE_FOOT,
                footer_x, 30.0, footer_codes))
        return "\n".join(commands).encode("latin-1", "replace")

    def line(self, text: str, *, size: float = SIZE_BODY, indent: float = INDENT,
             color=COLOR_TEXT, align_right: bool = False) -> None:
        """写一行（不换行、不分页；超宽也照写）。"""
        target = self._ensure()
        target.append(_Line(str(text or ""), float(size), float(indent),
                            tuple(color), bool(align_right)))
        self._y -= LEADING_BODY if size <= SIZE_BODY else size * 1.35

    def paragraph(self, text: str, *, size: float = SIZE_BODY, color=COLOR_TEXT,
                  indent: float = INDENT, gap_after: float = GAP_AFTER_BLOCK) -> None:
        """写一段（自动折行 + 自动分页）。空行只吃掉一个行高。"""
        text = str(text or "")
        if not text.strip():
            self._ensure()
            self._y -= LEADING_BODY
            return
        width = PAGE_WIDTH - MARGIN_X * 2 - indent
        lines = wrap_text(text, self.font, size, width)
        for line_text in lines:
            if self._y < MARGIN_BOTTOM + LEADING_BODY:
                self.new_page()
            self.line(line_text, size=size, indent=indent, color=color)
        self._y -= gap_after

    def heading(self, text: str, *, size: float = SIZE_HEADING) -> None:
        self.space(GAP_BEFORE_HEADING)
        self.line(text, size=size, color=COLOR_TEXT)
        self._y -= GAP_AFTER_HEADING

    def title(self, text: str, *, size: float = SIZE_TITLE) -> None:
        self.line(text, size=size, color=COLOR_TEXT)
        self._y -= GAP_AFTER_TITLE

    def space(self, amount: float) -> None:
        self._ensure()
        self._y -= float(amount)

    # ------------------------------------------------------------- 产出
    def _subset_bytes(self) -> bytes:
        """把「整份文档用到的字符」做成本次嵌入的子集字体（只做一次）。"""
        if self.font is None:
            return b""
        if self._subset is None:
            chars = "".join(chr(code) for code in sorted(set(self._used_glyphs.values())))
            self._subset = ttf_subset.subset_from(self.font, chars)
        return self._subset

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

        # 宽度数组：只有一种字体的连续区间，按 100 个一组分开写
        glyphs = sorted(self._used_glyphs)
        widths_parts: list[bytes] = []
        for start in range(0, len(glyphs), 100):
            chunk = glyphs[start:start + 100]
            values = " ".join(f"{self.font.advance(g) * 1000 // self.font.units_per_em}"
                              for g in chunk)
            widths_parts.append(b"%d [%s]" % (chunk[0], values.encode("ascii")))
        widths = b" ".join(widths_parts) or b"0 []"

        scale = 1000.0 / self.font.units_per_em
        upem = int(self.font.units_per_em * scale)
        # /FontFile2 必须指到那个字体流：漏了它，PDF 里就只剩一个「名字」，
        # 换台机器打开会变成方块或者干脆不显示（浏览器/打印机自己的字体
        # 里可没有我们这些字形）。
        descriptor = (
            b"<< /Type /FontDescriptor /FontName /EmbeddedCJK /Flags 4 "
            b"/FontBBox [%d %d %d %d] /ItalicAngle 0 /Ascent %d /Descent %d "
            b"/CapHeight %d /StemV 80 /FontFile2 %d 0 R >>" % (
                int(-200 * scale), int(-200 * scale), upem, upem,
                int(880 * scale), int(-120 * scale), int(700 * scale), base + 3)
        )
        cid_font = (
            b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /EmbeddedCJK "
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
            b"/FontDescriptor %d 0 R /DW 1000 /W [%s] /CIDToGIDMap /Identity >>"
            % (base + 2, widths)
        )
        type0 = (
            b"<< /Type /Font /Subtype /Type0 /BaseFont /EmbeddedCJK "
            b"/Encoding /Identity-H /DescendantFonts [%d 0 R] /ToUnicode %d 0 R >>"
            % (base + 1, base + 4)
        )
        objects = {
            base: type0,
            base + 1: cid_font,
            base + 2: descriptor,
            base + 3: (b"<< /Length %d /Filter /FlateDecode /Length1 %d >>\nstream\n"
                       % (len(compressed), len(blob))) + compressed + b"\nendstream",
            base + 4: (b"<< /Length %d >>\nstream\n" % len(cmap_stream))
                      + cmap_stream + b"\nendstream",
        }
        return objects, base + 5

    def build(self, body_text: str = "") -> bytes:
        """产出 PDF 字节。``body_text`` 是文档里出现过的所有文字（正文用）。

        内容流里写的是**字形编号**（Identity-H），字体子集只带用到的字形；
        所以顺序是「先把每一行都编码一遍收齐字形 → 再做子集 → 再组对象」。
        ``body_text`` 只用于「没有走 :meth:`paragraph` 的那些文字」的兜底。
        """
        if self._current is not None:
            self.pages.append(self._current)
            self._current = None
        if not self.pages:
            self.new_page()

        total_pages = len(self.pages)
        # 先把所有文字编码一遍（收齐字形 + 算出页脚位置），再做字体子集
        page_blobs = [self._content_stream(page, footer=f"{index + 1} / {total_pages}")
                      for index, page in enumerate(self.pages)]
        for char in str(body_text or "") + "0123456789 /第页":
            self._glyph_codes(char)

        # 对象编号从 3 开始连续排（1 = Catalog，2 = Pages）：稀疏编号会让 xref
        # 表里插一堆空闲项，排查时不好看，也没必要。
        font_base = 3
        font_objects, next_id = self._font_objects(font_base)
        page_ids = [next_id + 2 * i for i in range(total_pages)]
        content_ids = [pid + 1 for pid in page_ids]
        resources = (b"<< /Font << /F1 %d 0 R >> >>" % font_base)

        objects: dict[int, bytes] = dict(font_objects)
        for page_id, content_id, stream in zip(page_ids, content_ids, page_blobs):
            objects[content_id] = (b"<< /Length %d >>\nstream\n" % len(stream)
                                   + stream + b"\nendstream")
            objects[page_id] = (
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f] "
                b"/Resources %s /Contents %d 0 R >>"
                % (PAGE_WIDTH, PAGE_HEIGHT, resources, content_id))
        objects[2] = (b"<< /Type /Pages /Count %d /Kids [%s] >>"
                      % (total_pages,
                         b" ".join(b"%d 0 R" % pid for pid in page_ids)))
        objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets: dict[int, int] = {}
        for number in sorted(objects):
            offsets[number] = len(out)
            out += b"%d 0 obj\n" % number
            out += objects[number]
            out += b"\nendobj\n"
        xref_at = len(out)
        size = max(objects) + 1
        out += b"xref\n0 %d\n" % size
        out += b"0000000000 65535 f \n"
        for number in range(1, size):
            if number in offsets:
                out += b"%010d 00000 n \n" % offsets[number]
            else:
                out += b"0000000000 65535 f \n"
        out += b"trailer\n<< /Size %d /Root 1 0 R >>\n" % size
        out += b"startxref\n%d\n%%%%EOF\n" % xref_at
        return bytes(out)


def sample_pdf(title: str, blocks: list[str], *, body_text: str = "") -> bytes:
    """调试用：把一堆段落拼成一份最小 PDF（测试也用它）。"""
    doc = PdfDoc(title)
    doc.new_page()
    if title:
        doc.title(title)
    for block in blocks:
        doc.paragraph(block)
    return doc.build(body_text or (title + "".join(blocks)))
