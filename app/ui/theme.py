"""Warm paper and terracotta palette shared with the reading workspace.

Font sizes remain device-scaled pixels through device_px.
"""
from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont

# --------------------------------------------------------------- 核心调色板
BG = "#F7F2EA"            # 暖米色背景
PANEL = "#F7F2EA"         # 面板与背景同色（editorial 不用嵌套卡片底色）
PANEL_ALT = "#F1E8DC"     # 极浅的次级底（≈ #493C32/4 混在米色上）
HOVER_BG = "#EFDFD1"      # hover 底色（仍然是无彩色）
BORDER = "#E5D8CA"        # ≈ #493C32/10
BORDER_STRONG = "#CFB8A4"  # ≈ #493C32/25
#: 词表卡片：暖米白底 + 浅灰边线；悬停只做柔和的底色/边线变化（不加任何说明文字）
CARD_BG = "#FFFCF7"
CARD_BG_HOVER = "#F4E7DA"
CARD_BORDER = "#E8D9CA"
CARD_BORDER_HOVER = "#C89D81"
#: 词表右侧滑轨的滑块（圆角灰色）与其悬停/拖动色
RAIL_THUMB = "#CFB8A4"
RAIL_THUMB_ACTIVE = "#A9A7A1"

TEXT = "#493C32"          # 柔黑
TEXT_MUTED = "#806F61"    # ≈ /60 次要
TEXT_FAINT = "#9C8C7D"    # ≈ /40 辅助
TEXT_BODY = "#53463B"     # 正文（比标题浅一档）

ACCENT = "#9D5C43"        # 「强调」= 柔黑（主按钮用反白块）
ACCENT_SOFT = "#F0DFD0"
# 状态不再用颜色区分：一律只用灰度 + 文案本身（例如「解释中…」「解释失败」）
OK = "#493C32"
OK_SOFT = "#F1E8DC"
WARN = "#493C32"
WARN_SOFT = "#F1E8DC"
ERR = "#493C32"
ERR_SOFT = "#F1E8DC"

#: 选区/面板的 1px 外框颜色
OUTLINE = BORDER_STRONG

# ------------------------------------------------------------------- 字体
_SANS_FAMILIES = ["Microsoft YaHei UI", "微软雅黑", "Microsoft YaHei", "Segoe UI", "Arial"]
#: 衬线优先 Noto Serif CJK / 思源宋体，其次系统宋体，最后西文衬线
_SERIF_FAMILIES = [
    "Noto Serif CJK SC", "Noto Serif SC", "Source Han Serif SC", "思源宋体",
    "宋体", "SimSun", "Georgia", "Times New Roman",
]

_sans: str | None = None
_serif: str | None = None
_scale: float = 1.0


def _pick(families, available: set[str], fallback: str) -> str:
    for name in families:
        if name in available:
            return name
    return fallback


def init(root: tk.Misc, dpi: int = 96) -> None:
    """选字体族并设置 DPI 缩放（在创建任何窗口之前调用一次）。"""
    global _sans, _serif, _scale
    try:
        families = set(tkfont.families(root))
    except tk.TclError:  # pragma: no cover
        families = set()
    _sans = _pick(_SANS_FAMILIES, families, "TkDefaultFont")
    # 没有系统衬线时退回无衬线，避免 Tk 找不到字体而报错
    _serif = _pick(_SERIF_FAMILIES, families, _sans)
    _scale = max(1.0, min(2.0, dpi / 96.0))
    try:
        root.tk.call("tk", "scaling", dpi / 72.0)
    except tk.TclError:  # pragma: no cover
        pass


def family() -> str:
    return _sans or "TkDefaultFont"


def serif_family() -> str:
    return _serif or family()


def scale() -> float:
    return _scale


def px(n: int) -> int:
    """布局尺寸（逻辑像素）→ 设备像素：Tk 不对 padx/pady/width/height 做 DPI 换算。"""
    return max(1, int(round(n * _scale)))


#: 1pt = 96/72 px（Tk 的 ``tk scaling`` 也用同一个基准：dpi/72）
PT_TO_PX = 96.0 / 72.0


def device_px(n: int) -> int:
    """字号（**pt**）→ Tk 字体用的**负**设备像素：``-N`` 表示 N 设备像素。

    Tk 的规则：正数 = pt（会乘 ``tk scaling``），负数 = 像素（原样使用）。
    这里按 ``n * 96/72 * scale`` 换算后再取负，因此 9pt 在 96/120/144/192 DPI
    下分别是 12/15/18/24px，且与 44 / 360 这些布局尺寸**只被 DPI 缩放一次**。
    """
    return -max(1, int(round(n * PT_TO_PX * _scale)))


def font_px_at(pt: float, zoom: float = 1.0) -> int:
    """字号（**pt**）+ 视图缩放 → **设备像素**（DPI 与缩放各只算一次）。

    画布上的文字（思维导图等自绘内容）用它取字号：字号必须与**同一缩放下**
    的布局度量（``concept_map.metrics_for(zoom).em``）同源，否则缩小视图时
    卡片框跟着缩小、文字却保持原大，字就会越出框外。
    """
    return max(1, int(round(abs(device_px(pt)) * float(zoom or 1.0))))


def font_at(pt: float, zoom: float = 1.0) -> tuple:
    """**随视图缩放**的字体元组（自绘画布上的文字用；负 = 设备像素）。"""
    return (family(), -font_px_at(pt, zoom))


def font(size: int = 9, bold: bool = False, italic: bool = False,
         serif: bool = False, underline: bool = False) -> tuple:
    """正文字体元组；``size`` 是 **pt**（默认 9pt = 96 DPI 下 12px）。

    ``serif=True`` 用衬线（标题）；``underline`` 用于 hover。
    返回的字号是**负的设备像素**（见 :func:`device_px`：DPI 只缩放一次）。
    """
    style = []
    if bold:
        style.append("bold")
    if italic:
        style.append("italic")
    if underline:
        style.append("underline")
    fam = serif_family() if serif else family()
    px_size = device_px(size)
    return (fam, px_size, " ".join(style)) if style else (fam, px_size)


def serif(size: int = 12, italic: bool = False, underline: bool = False) -> tuple:
    """标题字体：衬线 + **常规字重**（editorial 规则：标题绝不加粗）。"""
    return font(size, italic=italic, serif=True, underline=underline)


def tracking_label(size: int = 7) -> tuple:
    """标签字体（小号无衬线；Tk 无字距，靠字号与灰度做出克制感）。"""
    return font(size)
