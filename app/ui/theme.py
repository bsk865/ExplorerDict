"""统一主题：编辑杂志风（Editorial）palette 与字体。

风格约束（用户提供的设计参考，硬约束）
--------------------------------------
* 背景 **#F9F8F6 暖米色**（不是纯白 / #fafafa），文字 **#1C1C1C 柔黑**（不是 #000 / #0a0a0a）；
* **纯单色**：没有任何彩色强调色（红/蓝/绿一律不用），层次只靠灰度与透明度等价色；
* 细线、无阴影、方角（Tk 天然无圆角，这里保证不出现立体边框 / 粗边）；
* **正文 / 按钮 / 标题全部用无衬线**（Windows 上首选 ``Microsoft YaHei UI``）：
  产品界面里不再混排衬线标题 —— 中英文与数字混排时衬线的可读性明显更差，
  用户明确要求统一到微软雅黑 UI；:func:`serif` 只作为排版备选保留，界面不再调用；
* 标签用大写间距感的小号无衬线；
* hover 反馈优先用**细下划线 / 字色加深**，不用阴影、不用彩色高亮。

兼容性
------
变量名（``BG`` / ``PANEL`` / ``ACCENT`` / ``OK`` / ``ERR`` …）保持与旧代码一致，
但取值全部收敛到单色体系：``ACCENT`` 就是柔黑，``OK/WARN/ERR`` 只是正文色的
不同灰度 —— 这样旧调用点不会漂移出彩色，也不需要在各处改名字。

``font()`` 的签名向后兼容（新增 ``serif`` 关键字），旧的
``theme.font(9, bold=True)`` 调用照常工作。

字号语义：调用端是 **pt**，Tk 只吃像素（本轮修正）
-----------------------------------------------
``font(size)`` / ``serif(size)`` / ``tracking_label(size)`` 的 ``size`` 参数是
**pt** —— 与全部历史调用点 ``theme.font(9)``（9pt 正文）语义一致，调用端不需要
改成像素。Tk 的规则是：**正数**字号 = pt，会再乘 ``tk scaling``；而 ``init()``
已经把 ``tk scaling`` 设成 ``dpi/72``，所以只有**负数字号（像素）**才能保证
DPI 只缩放一次。

因此 pt → 设备像素的唯一换算点是 :func:`device_px`：

    device_px(n) = -max(1, round(n * 96/72 * scale))

* 96 DPI（scale = 1.0）：**9pt → 12px**（正文的默认可读标准），7pt 辅助 → 9px；
* 120 / 144 / 192 DPI：9pt → **15 / 18 / 24px**，与 44x44 小方块、360x460 面板
  （``px()``）按同一倍率一起变大。

旧实现把 ``size`` 当像素用（``-px(9)`` → 96 DPI 下只有 9px），等于把 9pt 正文
缩成了 9px，全站字都偏小 —— 这是本轮修掉的错误。

* **文字**：负的**设备像素**（Tk 不再乘 ``tk scaling``），由 :func:`device_px`
  按上面的公式换算；
* **布局**：``px()`` 继续返回已缩放的设备像素（Tk 对 padx/pady/width/height
  不做 DPI 换算，必须自己缩放）；
* 两者都只乘一次倍率，因此 100% / 125% / 150% / 200% 下「小方块 44px、
  面板 360px、正文 12px（9pt）」一起变大，字不会偏小、高 DPI 也不会溢出。
"""
from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont

# --------------------------------------------------------------- 核心调色板
BG = "#F9F8F6"            # 暖米色背景
PANEL = "#F9F8F6"         # 面板与背景同色（editorial 不用嵌套卡片底色）
PANEL_ALT = "#F2F1EC"     # 极浅的次级底（≈ #1C1C1C/4 混在米色上）
HOVER_BG = "#EFEEE9"      # hover 底色（仍然是无彩色）
BORDER = "#E3E1DC"        # ≈ #1C1C1C/10
BORDER_STRONG = "#C8C6C0"  # ≈ #1C1C1C/25
#: 词表卡片：暖米白底 + 浅灰边线；悬停只做柔和的底色/边线变化（不加任何说明文字）
CARD_BG = "#FCFBF8"
CARD_BG_HOVER = "#F1F0EA"
CARD_BORDER = "#E4E2DD"
CARD_BORDER_HOVER = "#C6C4BE"
#: 词表右侧滑轨的滑块（圆角灰色）与其悬停/拖动色
RAIL_THUMB = "#C8C6C0"
RAIL_THUMB_ACTIVE = "#A9A7A1"

TEXT = "#1C1C1C"          # 柔黑
TEXT_MUTED = "#6E6C67"    # ≈ /60 次要
TEXT_FAINT = "#9B9993"    # ≈ /40 辅助
TEXT_BODY = "#3A3936"     # 正文（比标题浅一档）

ACCENT = "#1C1C1C"        # 「强调」= 柔黑（主按钮用反白块）
ACCENT_SOFT = "#EDEBE6"
# 状态不再用颜色区分：一律只用灰度 + 文案本身（例如「解释中…」「解释失败」）
OK = "#1C1C1C"
OK_SOFT = "#F2F1EC"
WARN = "#1C1C1C"
WARN_SOFT = "#F2F1EC"
ERR = "#1C1C1C"
ERR_SOFT = "#F2F1EC"

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
