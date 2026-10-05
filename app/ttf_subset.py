"""TrueType 子集化（只为 PDF 导出服务，**只用标准库**）。

为什么要有这个模块
------------------
PDF 要显示中文，必须**把字体嵌进文件**（PDF 的 14 个标准字体没有一个带汉字）。
冻结运行时里没有 Pillow / reportlab / fontTools，所以：

* 解析 TrueType 二进制（表目录 / ``head`` / ``hhea`` / ``maxp`` / ``hmtx`` /
  ``cmap`` / ``loca`` / ``glyf``）由本模块自己干；
* 只保留**这次真正用到的字形**再重新拼一份字体（子集化）——SimHei 源文件
  9.7 MB，直接整个嵌进 PDF 会让每份导出都变成 9 MB 起步；只带用到的字形时
  通常只有几十 KB。

两条关键取舍（都是为了「让 PDF 阅读器认」）
------------------------------------------
1. **字形编号（glyph id）保持原样，不重新编号**。子集化常见的做法是压缩编号，
   但那样必须改写复合字形（composite glyph）里引用的部件编号，一旦漏改就是
   花屏或崩；保持原编号则复合字形可以逐字节照抄。代价是 ``loca`` 里留了很多
   空洞（每条字形数据只占几十字节，空洞不占文件体积）——完全值得。
2. **``cmap`` 只重建 format 4 一条子表**（BMP 字符够用），并且只保留本次用到的
   字符。format 4 每个段要求「码点连续且 glyph id 连续」，所以按
   「code - gid 恒定」切段（同一段里 ``idDelta`` 一样，``idRangeOffset`` 全为 0）。

对外只暴露三样东西：:func:`load_ttf`（解析源字体并缓存）、
:class:`TTFont`（``glyph_for`` / ``advance`` / ``units_per_em``）、
:func:`subset_ttf`（产出子集字节）。
"""
from __future__ import annotations

import struct
from functools import lru_cache
from pathlib import Path

#: 拼字体时按这个顺序写表（顺序无强制要求，固定下来便于比对）
KEEP_ORDER = ("head", "hhea", "maxp", "hmtx", "cmap", "loca", "glyf")
#: 即使没用到也一定保留的表（``head`` 里的 loca 格式标记、``hhea`` 的字形数等）
REQUIRED = ("head", "hhea", "maxp", "hmtx", "cmap", "loca", "glyf")
#: TrueType 表目录里这些标签必须 4 字节对齐
_PAD4 = b"\x00\x00\x00\x00"
#: 段数上限：正常文档用不到这么多；纯粹是防一份超长文档把 cmap 撑爆
MAX_CMAP_SEGMENTS = 4000


class FontError(RuntimeError):
    """字体读不了 / 结构不认识（不是 ``TclError`` 那类，是数据问题）。"""


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from(">H", data, offset)[0]


def _s16(data: bytes, offset: int) -> int:
    return struct.unpack_from(">h", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from(">I", data, offset)[0]


def _checksum(data: bytes) -> int:
    """TrueType 的校验和：按 4 字节大端累加（不足补零），结果取低 32 位。

    ``head`` 表的 ``checkSumAdjustment`` 必须按这个口径算，否则 FreeType 会
    报 ``broken font``（实测：PIL 打开时报 ``OSError: unrecognized data``）。
    """
    pad = (-len(data)) % 4
    if pad:
        data = data + b"\x00" * pad
    total = 0
    for i in range(0, len(data), 4):
        total = (total + _u32(data, i)) & 0xFFFFFFFF
    return total


class TTFont:
    """一个已解析的 TrueType 字体（只保留子集化用得上的那几张表）。"""

    def __init__(self, data: bytes, face_index: int = 0):
        self.raw = data
        self.tables: dict[str, tuple[int, int]] = {}
        self.face_index = int(face_index)
        self._parse_directory()
        missing = [tag for tag in REQUIRED if tag not in self.tables]
        if missing:
            raise FontError(f"字体缺少必需的表：{', '.join(missing)}")

        head = self._table("head")
        if len(head) < 54:
            raise FontError("head 表长度异常")
        self.units_per_em = _u16(head, 18) or 1000
        self._index_to_loc_format = _s16(head, 50)
        self._head = head

        hhea = self._table("hhea")
        self.number_of_h_metrics = _u16(hhea, 34)
        maxp = self._table("maxp")
        self.num_glyphs = _u16(maxp, 4)

        self._loca = self._parse_loca()
        self._hmtx = self._table("hmtx")
        self._glyf = self._table("glyf")
        self._cmap = self._parse_cmap()

    # ----------------------------------------------------------- 解析
    def _parse_directory(self) -> None:
        data = self.raw
        if len(data) < 12:
            raise FontError("文件太短")
        tag = data[:4]
        if tag == b"ttcf":
            if len(data) < 16:
                raise FontError("TTC 头部不完整")
            count = _u32(data, 8)
            if self.face_index >= count:
                raise FontError(f"TTC 里没有第 {self.face_index} 张字体")
            offset = _u32(data, 12 + 4 * self.face_index)
        elif tag in (b"\x00\x01\x00\x00", b"true", b"OTTO"):
            offset = 0
        else:
            raise FontError(f"不认识的字体签名：{tag!r}")
        if offset + 12 > len(data):
            raise FontError("表目录越界")
        num_tables = _u16(data, offset + 4)
        for i in range(num_tables):
            entry = offset + 12 + 16 * i
            if entry + 16 > len(data):
                raise FontError("表目录越界")
            name = data[entry:entry + 4].decode("latin-1")
            start = _u32(data, entry + 8)
            length = _u32(data, entry + 12)
            if start + length > len(data):
                raise FontError(f"表 {name} 越界")
            self.tables[name] = (start, length)

    def _table(self, tag: str) -> bytes:
        start, length = self.tables[tag]
        return self.raw[start:start + length]

    def _parse_loca(self) -> list[int]:
        loca = self._table("loca")
        count = self.num_glyphs + 1
        if self._index_to_loc_format == 0:
            need = count * 2
            if len(loca) < need:
                raise FontError("loca 表比字形数短")
            return [_u16(loca, 2 * i) * 2 for i in range(count)]
        need = count * 4
        if len(loca) < need:
            raise FontError("loca 表比字形数短")
        return [_u32(loca, 4 * i) for i in range(count)]

    def _parse_cmap(self) -> dict[int, int]:
        """取一条可用的 Unicode 子表（优先 format 12，其次 format 4）。"""
        start, length = self.tables["cmap"]
        data = self.raw[start:start + length]
        if len(data) < 4:
            raise FontError("cmap 表太短")
        count = _u16(data, 2)
        best: tuple[int, int, int] | None = None   # (优先级, 偏移, format)
        for i in range(count):
            entry = 4 + 8 * i
            if entry + 8 > len(data):
                break
            platform = _u16(data, entry)
            encoding = _u16(data, entry + 2)
            offset = _u32(data, entry + 4)
            if offset + 2 > len(data):
                continue
            fmt = _u16(data, offset)
            if fmt == 12 and platform in (0, 3):
                rank = 0
            elif fmt == 4 and platform == 3 and encoding in (1, 10):
                rank = 1
            elif fmt == 4 and platform == 0:
                rank = 2
            else:
                continue
            if best is None or rank < best[0]:
                best = (rank, offset, fmt)
        if best is None:
            raise FontError("cmap 里没有可用的 Unicode 子表")
        _rank, offset, fmt = best
        if fmt == 12:
            return self._cmap_format12(data, offset)
        return self._cmap_format4(data, offset)

    @staticmethod
    def _cmap_format4(data: bytes, offset: int) -> dict[int, int]:
        seg_x2 = _u16(data, offset + 6)
        segs = seg_x2 // 2
        ends = offset + 14
        starts = ends + seg_x2 + 2
        deltas = starts + seg_x2
        ranges = deltas + seg_x2
        out: dict[int, int] = {}
        for i in range(segs):
            if ranges + 2 * i + 2 > len(data):
                break
            end = _u16(data, ends + 2 * i)
            start_code = _u16(data, starts + 2 * i)
            delta = _s16(data, deltas + 2 * i)
            range_offset = _u16(data, ranges + 2 * i)
            if start_code > end:
                continue
            for code in range(start_code, min(end, 0xFFFF) + 1):
                if range_offset == 0:
                    glyph = (code + delta) & 0xFFFF
                else:
                    addr = ranges + 2 * i + range_offset + 2 * (code - start_code)
                    if addr + 2 > len(data):
                        continue
                    glyph = _u16(data, addr)
                    if glyph:
                        glyph = (glyph + delta) & 0xFFFF
                if glyph:
                    out[code] = glyph
        return out

    @staticmethod
    def _cmap_format12(data: bytes, offset: int) -> dict[int, int]:
        groups = _u32(data, offset + 12)
        out: dict[int, int] = {}
        base = offset + 16
        for i in range(groups):
            addr = base + 12 * i
            if addr + 12 > len(data):
                break
            start_code = _u32(data, addr)
            end_code = _u32(data, addr + 4)
            start_glyph = _u32(data, addr + 8)
            if end_code - start_code > 0x10000:      # 防御：异常大组直接跳过
                continue
            for code in range(start_code, end_code + 1):
                if code > 0xFFFF:                     # 本模块只做 BMP
                    break
                out[code] = start_glyph + (code - start_code)
        return out

    # ----------------------------------------------------------- 查询
    def glyph_for(self, char: str) -> int:
        """字符 → glyph id（**0 = 缺字形**，由调用方决定怎么显示）。"""
        if not char:
            return 0
        code = ord(char)
        if code > 0xFFFF:
            return 0
        return int(self._cmap.get(code, 0))

    def advance(self, glyph: int) -> int:
        """字形的前进宽度（字体单位）。最后一个字形重复最后一个 hMetric。"""
        count = max(1, self.number_of_h_metrics)
        index = min(int(glyph), count - 1)
        offset = 4 * index
        if offset + 2 > len(self._hmtx):
            return self.units_per_em
        return _u16(self._hmtx, offset)

    def text_width(self, text: str, size: float) -> float:
        """一段文字在 ``size`` 磅下的宽度（点）——换行算宽度就靠它。"""
        scale = float(size) / float(self.units_per_em)
        total = 0
        for char in text:
            total += self.advance(self.glyph_for(char))
        return total * scale

    def glyph_bytes(self, glyph: int) -> bytes:
        if glyph < 0 or glyph + 1 >= len(self._loca):
            return b""
        start, end = self._loca[glyph], self._loca[glyph + 1]
        if end <= start or end > len(self._glyf):
            return b""
        return self._glyf[start:end]

    def has_glyph(self, char: str) -> bool:
        return self.glyph_for(char) != 0


@lru_cache(maxsize=4)
def _read_file(path: str) -> bytes:
    return Path(path).read_bytes()


def load_ttf(path, face_index: int = 0) -> TTFont:
    """读并解析一个字体文件（同一路径只读一次；解析结果不缓存，几毫秒的事）。"""
    return TTFont(_read_file(str(path)), face_index=face_index)


# --------------------------------------------------------------------- 子集化
def _build_cmap4(pairs: list[tuple[int, int]]) -> bytes:
    """按 ``(码点, glyph id)`` 造一条 format 4 子表。

    每个段要求「码点连续 + glyph id 连续」（这样 ``idDelta`` 恒定、
    ``idRangeOffset`` 全 0）。同段内 ``idDelta`` 取第一个码点的差值，
    之后靠「码点每 +1、glyph id 也 +1」自然对上 —— 所以切段条件是
    「码点差 == glyph id 差」。
    """
    pairs = sorted((c, g) for c, g in pairs if 0 < c < 0xFFFF and g)
    segments: list[list[tuple[int, int]]] = []
    for code, glyph in pairs:
        if segments:
            last = segments[-1][-1]
            if code == last[0] + 1 and glyph == last[1] + 1:
                segments[-1].append((code, glyph))
                continue
        segments.append([(code, glyph)])
    if not segments:
        segments = [[(0xFFFF, 0)]]

    seg_count = len(segments)
    end_codes, start_codes, deltas, range_offsets = [], [], [], []
    for seg in segments:
        first_code, first_glyph = seg[0]
        end_codes.append(seg[-1][0])
        start_codes.append(first_code)
        deltas.append((first_glyph - first_code) & 0xFFFF)
        range_offsets.append(0)

    end_codes.append(0xFFFF)
    start_codes.append(0xFFFF)
    deltas.append(1)          # 0xFFFF 映射到 glyph 0（约定的收尾段）
    range_offsets.append(0)
    seg_count += 1

    seg_x2 = seg_count * 2
    search_range = 2 * (2 ** (seg_count.bit_length() - 1))
    entry_selector = max(0, seg_count.bit_length() - 1) if seg_count else 0
    range_shift = seg_x2 - search_range

    body = bytearray()
    body += struct.pack(">HHHHHHH", 4, 16 + 8 * seg_count + seg_x2, 0,
                        seg_x2, search_range, entry_selector, range_shift)
    body += struct.pack(f">{seg_count}H", *end_codes)
    body += b"\x00\x00"
    body += struct.pack(f">{seg_count}H", *start_codes)
    body += struct.pack(f">{seg_count}H", *deltas)
    body += struct.pack(f">{seg_count}H", *range_offsets)
    return bytes(body)


def _build_cmap(pairs: list[tuple[int, int]]) -> bytes:
    """``cmap`` 表：版本 + 一条 (3,1) format 4 子表。"""
    sub = _build_cmap4(pairs)
    header = struct.pack(">HHHHI", 0, 1, 3, 1, 12)
    return header + sub


def subset_ttf(path, text: str, face_index: int = 0) -> bytes:
    """把 ``text`` 里出现的字符做成一份子集字体，返回字体文件的字节。

    只带用到的字形（外加 ``.notdef``）；字形编号保持不变（见模块文档）。
    字体里一个字符都没有时只带 ``.notdef`` —— 调用方应当先检查
    :meth:`TTFont.glyph_for` 再决定要不要嵌字体。
    """
    font = load_ttf(path, face_index=face_index)
    return subset_from(font, text)


def _is_embed_char(char: str) -> bool:
    """哪些字符进 ``cmap``：可打印字符 + 空格（换行 / 制表符不进 —— 它们只做布局）。"""
    return char == " " or char.isprintable()


def subset_from(font: TTFont, text: str) -> bytes:
    """:func:`subset_ttf` 的已解析版本（同一份字体要连出多页时用它省一次解析）。"""
    used: dict[int, int] = {0: 0}         # glyph id -> 码点（取最先出现的那个）
    for char in text:
        if not _is_embed_char(char):
            continue
        glyph = font.glyph_for(char)
        if glyph and glyph not in used:
            used[glyph] = ord(char)

    # --- glyf + loca：逐字形照抄，按 2 字节对齐（短 loca 要求），
    #     能塞进短格式就写短格式（体积差一半）---
    glyf = bytearray()
    offsets = [0]
    for glyph in range(font.num_glyphs):
        blob = font.glyph_bytes(glyph) if (glyph in used or glyph == 0) else b""
        if blob:
            glyf += blob
            pad = (-len(blob)) % 2
            if pad:
                glyf += b"\x00" * pad
        offsets.append(len(glyf))
    if len(glyf) <= 0x1FFFE:                       # 短格式每格 2 字节（值 = 真实偏移 / 2）
        loca = b"".join(struct.pack(">H", value // 2) for value in offsets)
        loca_format = 0
    else:
        loca = b"".join(struct.pack(">I", value) for value in offsets)
        loca_format = 1

    # --- head：写 loca 格式，并清空 checkSumAdjustment ---
    head = bytearray(font._head)
    struct.pack_into(">h", head, 50, loca_format)
    struct.pack_into(">I", head, 8, 0)

    tables: dict[str, bytes] = {
        "head": bytes(head),
        "hhea": font._table("hhea"),
        "maxp": font._table("maxp"),
        "hmtx": font._table("hmtx"),
        "cmap": _build_cmap(sorted((code, glyph) for glyph, code in used.items())),
        "loca": loca,
        "glyf": bytes(glyf),
    }

    # --- 拼文件：表目录必须先排好，head 的校验和最后回头补 ---
    names = [tag for tag in KEEP_ORDER if tag in tables]
    num_tables = len(names)
    entry_selector = max(0, num_tables.bit_length() - 1)
    search_range = 16 * (2 ** entry_selector)
    header = struct.pack(">IHHHH", 0x00010000, num_tables, search_range,
                         entry_selector, num_tables * 16 - search_range)
    offset = len(header) + 16 * num_tables
    entries = bytearray()
    blobs = bytearray()
    head_offset = 0
    for tag in names:
        blob = tables[tag]
        if tag == "head":
            head_offset = offset
        entries += struct.pack(">4sIII", tag.encode("latin-1"), _checksum(blob),
                               offset, len(blob))
        blobs += blob
        pad = (-len(blob)) % 4
        if pad:
            blobs += _PAD4[:pad]
        offset += len(blob) + pad

    font_bytes = header + bytes(entries) + bytes(blobs)
    adjustment = (0xB1B0AFBA - _checksum(font_bytes)) & 0xFFFFFFFF
    out = bytearray(font_bytes)
    struct.pack_into(">I", out, head_offset + 8, adjustment)
    return bytes(out)
