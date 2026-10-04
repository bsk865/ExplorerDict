"""文字 → 字形图像（真实按钮与离线预览共用的**唯一**渲染规则）。

规则
----
1. 先用 Pillow 量出**紧致墨迹 bbox**（:func:`ink_bbox`，相对 ``anchor="lt"``
   的原点，右下为开区间）；
2. 图像内部按**字体排版单元**留白，而不是只留墨迹：
   ``cell_w = max(ink_w, ceil(font.getlength(content)))``、
   ``cell_h = max(ink_h, sum(font.getmetrics()))``；
   再加上调用点指定的设备像素内边距：``width = cell_w + 2 * padx``、
   ``height = cell_h + 2 * pady``；
3. 墨迹落点 ``ox = floor((width - ink_w) / 2)``、
   ``oy = floor((height - ink_h) / 2)``，绘制原点 ``(ox - x0, oy - y0)``：
   可见字形中心与图像中心偏差 ≤ 0.5px，按钮同时保有正常点击尺寸
   （「×」的墨迹在 9pt 下只有 7x6px，只按墨迹裁图会小到不好点）；
4. ``FlatButton`` 走图像路径时把 Tk 侧 ``padx`` / ``pady`` 显式归零
   （``compound="none"`` / ``anchor="center"``），内边距只加这一次；
5. hover 的下划线只画在**真实墨迹底边以下**、且高度放得下的区域里，
   不覆盖字形、不改图像尺寸、不动字形位置；
6. ``tools/ui_preview.py`` 用同一份 :func:`ink_bbox` / :func:`text_cell` /
   :func:`center_ink` 离线画图，产品与预览不会各说各话。

字号与 DPI
----------
``font_size`` 是 **pt**，经 :func:`font_px`（= ``theme.device_px`` 同一换算点）
换算成设备像素，与 ``theme.font()`` 完全一致：96/125/150/200% DPI 都**只缩放
一次**，图像里的字形与 Tk 原生文字一样大。

Pillow 是**可选**依赖
---------------------
导入失败时 :func:`available` 返回 ``False``，:func:`render_text_image` 会直接
抛错；``FlatButton`` 捕获后清掉 image 并退回原生文字居中（保留调用点
``padx`` / ``pady``）—— 这是**降级**，不是等价实现。
"""
from __future__ import annotations

import math
import os
from pathlib import Path

from . import theme

try:  # pragma: no cover - 分支由运行环境决定（本机 Pillow 12.3.0 可用）
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except Exception:  # noqa: BLE001 - 任何导入失败都按「没有 Pillow」处理
    Image = ImageDraw = ImageFont = None      # type: ignore[assignment]
    PIL_AVAILABLE = False


# ------------------------------------------------------------------ 字体解析
#: 主题字体族 → 本机字体文件（``theme.family()`` / ``serif_family()`` 的取值）
_SANS_FILES: dict[str, list[str]] = {
    "microsoft yahei ui": ["msyh.ttc", "msyhbd.ttc"],
    "微软雅黑": ["msyh.ttc", "msyhbd.ttc"],
    "microsoft yahei": ["msyh.ttc", "msyhbd.ttc"],
    "segoe ui": ["segoeui.ttf"],
    "arial": ["arial.ttf"],
}
_SERIF_FILES: dict[str, list[str]] = {
    "noto serif cjk sc": ["NotoSerifSC-VF.ttf", "simsun.ttc"],
    "noto serif sc": ["NotoSerifSC-VF.ttf", "simsun.ttc"],
    "source han serif sc": ["NotoSerifSC-VF.ttf", "simsun.ttc"],
    "思源宋体": ["NotoSerifSC-VF.ttf", "simsun.ttc"],
    "宋体": ["simsun.ttc"],
    "simsun": ["simsun.ttc"],
    "georgia": ["georgia.ttf"],
    "times new roman": ["times.ttf"],
}
#: 族名不认识时（例如尚未 ``theme.init()`` → "TkDefaultFont"）的兜底顺序。
#: 顺序与 ``tools/ui_preview.py`` 历史输出一致，保证离线预览不因重构而漂移。
_SANS_FALLBACK = ["msyh.ttc", "segoeui.ttf", "arial.ttf", "simsun.ttc"]
_SERIF_FALLBACK = ["NotoSerifSC-VF.ttf", "simsun.ttc", "georgia.ttf", "times.ttf"]

#: 主题族（小写）→ 字体文件里应对应的 ``getname()[0]``（小写）。
#: ``msyh.ttc`` / ``msyhbd.ttc`` 都是 2 面：index 0 = Microsoft YaHei，
#: index 1 = Microsoft YaHei UI —— 必须按名字挑面，不能无条件用 index 0。
_FACE_BY_FAMILY: dict[str, str] = {
    "microsoft yahei ui": "microsoft yahei ui",
    "microsoft yahei": "microsoft yahei",
    "微软雅黑": "microsoft yahei",
}
#: TTC 里最多检查几面（本机 msyh*.ttc 只有 2 面，多试几面代价很小）
_FACE_SCAN_LIMIT = 8
#: 面名末尾可能被并进族名的字重 / 倾斜词
_FACE_STYLE_WORDS = frozenset({"bold", "italic", "oblique", "light", "semilight",
                               "semibold", "medium", "regular"})


def _font_dirs() -> list[Path]:
    dirs: list[Path] = [Path(os.environ.get("WINDIR") or r"C:\Windows") / "Fonts"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    return dirs


def _candidates(*, serif: bool, bold: bool) -> list[str]:
    """按主题字体族给出候选字体文件（先存在的绝对路径，再裸文件名兜底）。"""
    family = theme.serif_family() if serif else theme.family()
    key = str(family or "").strip().lower()
    table = _SERIF_FILES if serif else _SANS_FILES
    fallback = _SERIF_FALLBACK if serif else _SANS_FALLBACK
    names = list(table.get(key) or [])
    names += [name for name in fallback if name not in names]
    if bold:
        bold_files = {"msyh.ttc": "msyhbd.ttc", "segoeui.ttf": "segoeuib.ttf",
                      "arial.ttf": "arialbd.ttf", "simsun.ttc": "simsun.ttc"}
        names = [bold_files.get(name, name) for name in names]
    out: list[str] = []
    for name in names:
        for folder in _font_dirs():
            path = folder / name
            if path.exists():
                out.append(str(path))
                break
        out.append(name)          # Pillow 自己也会在系统字体目录里找裸文件名
    return out


def available() -> bool:
    """Pillow 是否可用（读的是**运行时**标志，便于测试打桩成「不可用」）。"""
    return bool(PIL_AVAILABLE)


def font_px(size_pt: float) -> int:
    """pt → **设备像素**：与 ``theme.font()`` 同一个换算点（DPI 只缩放一次）。"""
    return abs(theme.device_px(size_pt))


def _face_family(font) -> str:
    """``font.getname()`` 的族名（小写）；位图字体没有它时返回空串。"""
    try:
        return str(font.getname()[0] or "").strip().lower()
    except Exception:  # noqa: BLE001 - load_default 之类的位图字体
        return ""


def _base_face_name(name: str) -> str:
    """去掉面名末尾的字重 / 倾斜词（``Microsoft YaHei UI Bold`` → 族名）。"""
    parts = name.split()
    if len(parts) > 1 and parts[-1] in _FACE_STYLE_WORDS:
        return " ".join(parts[:-1])
    return name


def _open_candidate(path: str, px_size: int, want_face: str | None):
    """打开一个候选字体文件；TTC 多面时按 ``getname()`` 选与目标族匹配的那一面。

    ``want_face is None``（族名不认识，例如还没 ``theme.init()``）时沿用旧行为：
    取文件的第一面。全部面都不匹配时同样返回第一面。
    """
    first = None
    for index in range(_FACE_SCAN_LIMIT):
        try:
            font = ImageFont.truetype(path, px_size, index=index)
        except (OSError, ValueError):
            break
        if first is None:
            first = font
        if want_face is None:
            return font
        if _base_face_name(_face_family(font)) == want_face:
            return font
    return first


def load_font(px_size: int, *, serif: bool = False, bold: bool = False):
    """取一个 Pillow 字体：字体族跟随 theme，字号是**已经换算好的设备像素**。

    ``msyh.ttc`` 这类 TTC 里有多个面（index 0 = Microsoft YaHei、
    index 1 = Microsoft YaHei UI），按 ``getname()`` 选与 theme 目标族匹配的那一面。
    """
    if not available():
        raise RuntimeError("Pillow 不可用：无法渲染字形图像")
    px_size = max(1, int(round(px_size)))
    family = str(theme.serif_family() if serif else theme.family()).strip().lower()
    want_face = _FACE_BY_FAMILY.get(family)
    for candidate in _candidates(serif=serif, bold=bold):
        font = _open_candidate(candidate, px_size, want_face)
        if font is None:
            continue
        if bold:
            try:                      # 变量字体（Noto Serif SC VF）可以直接切字重
                font.set_variation_by_name("Bold")
            except Exception:         # noqa: BLE001 - 静态字重字体忽略
                pass
        return font
    try:                              # 最后兜底：Pillow 自带位图字体（≥10.1 支持 size）
        return ImageFont.load_default(size=px_size)
    except TypeError:                 # pragma: no cover - 老版本 Pillow
        return ImageFont.load_default()


# ------------------------------------------------------------------ 墨迹几何
def ink_bbox(content: str, font) -> tuple[int, int, int, int]:
    """真实可见字形（含抗锯齿墨迹）的 tight bbox，相对 ``anchor="lt"`` 的原点。

    真的渲染一遍再取 ``getbbox()``：``draw.textbbox()`` 给的是**排版盒**
    （含标点左边距），不能拿来当墨迹的落点。右下为开区间。
    """
    content = str(content or "")
    pad = max(4, int(getattr(font, "size", 12) or 12))
    width = int(font.getlength(content)) + pad * 2 + 4
    height = int(getattr(font, "size", 12) or 12) * 2 + pad * 2
    scratch = Image.new("L", (max(1, width), max(1, height)), 0)
    ImageDraw.Draw(scratch).text((pad, pad), content, font=font, fill=255, anchor="lt")
    box = scratch.getbbox()
    if box is None:                      # 空串 / 全空白
        return (0, 0, 0, 0)
    return (box[0] - pad, box[1] - pad, box[2] - pad, box[3] - pad)


def _cell_size(content: str, font, ink_w: int, ink_h: int) -> tuple[int, int]:
    """字体排版单元：宽 = ``max(墨迹宽, ceil(字宽))``，高 = ``max(墨迹高, ascent+descent)``。"""
    try:
        advance = int(math.ceil(float(font.getlength(content))))
    except Exception:                    # noqa: BLE001 - 没有 getlength 的字体
        advance = 0
    try:
        ascent, descent = font.getmetrics()
        line_h = int(ascent) + int(descent)
    except Exception:                    # noqa: BLE001 - 没有 getmetrics 的字体
        line_h = 0
    return (max(int(ink_w), advance), max(int(ink_h), line_h))


def text_cell(content: str, font) -> tuple[int, int]:
    """文字在 ``font`` 下的**排版单元**尺寸（≥ 紧致墨迹，单位 = font 的像素尺寸）。

    产品 :func:`render_text_image` 与离线预览 ``tools/ui_preview.py`` 的按钮尺寸
    共用这一份计算。
    """
    x0, y0, x1, y1 = ink_bbox(content, font)
    return _cell_size(str(content or ""), font, max(0, x1 - x0), max(0, y1 - y0))


def center_ink(draw, box, content, *, font, fill, scale: int = 1) -> None:
    """把 ``content`` 的**可见墨迹**精确居中在 ``box``（x, y, w, h）里。

    与图像路径（:func:`render_text_image`）是同一条规则：居中的是墨迹中心，
    不是排版盒中心，也没有任何 ``+1`` 之类的魔数位移。``scale`` 是离线预览的
    超采样倍率（1 = 最终像素）。
    """
    x, y, w, h = box
    x0, y0, x1, y1 = ink_bbox(content, font)
    cx = x * scale + (w * scale - 1) / 2          # 与 rect() 的像素区域同一中心
    cy = y * scale + (h * scale - 1) / 2
    draw.text((cx - (x0 + x1 - 1) / 2, cy - (y0 + y1 - 1) / 2), content,
              font=font, fill=fill, anchor="lt")


# ---------------------------------------------------------------- 图像渲染
def render_text_image(content: str, *, font_size: float = 9, padx: int = 0, pady: int = 0,
                      color: str = theme.TEXT, serif: bool = False, bold: bool = False,
                      underline: bool = False, font=None):
    """渲染「字体排版单元 + 对称内边距」的 RGBA 图像，返回 ``(image, meta)``。

    * 先量紧致墨迹 bbox，图像尺寸按排版单元算（设备像素）：
      ``cell_w = max(ink_w, ceil(font.getlength(content)))``、
      ``cell_h = max(ink_h, sum(font.getmetrics()))``；
      ``width = cell_w + 2 * padx``、``height = cell_h + 2 * pady``；
    * 墨迹落点 ``ox = floor((width - ink_w) / 2)``、
      ``oy = floor((height - ink_h) / 2)``，绘制原点 ``(ox - x0, oy - y0)``：
      图像比纯墨迹大（点击区正常），可见字形中心偏差 ≤ 0.5px；
    * ``underline=True`` 时在**真实墨迹底边以下**、且放得下的行里补一条下划线：
      不覆盖字形、不改图像尺寸、不动字形位置（hover 不会让文字跳）。

    ``meta`` 带字号 / 内边距 / 排版单元 / 墨迹框（图像坐标，右下开区间）/ 下划线框。
    """
    if not available():
        raise RuntimeError("Pillow 不可用：无法渲染字形图像")
    content = str(content or "")
    font = font if font is not None else load_font(font_px(font_size), serif=serif, bold=bold)
    padx, pady = max(0, int(padx)), max(0, int(pady))
    x0, y0, x1, y1 = ink_bbox(content, font)
    ink_w, ink_h = max(0, x1 - x0), max(0, y1 - y0)
    cell_w, cell_h = _cell_size(content, font, ink_w, ink_h)
    width, height = max(1, cell_w + 2 * padx), max(1, cell_h + 2 * pady)
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    ox = (width - ink_w) // 2
    oy = (height - ink_h) // 2
    if content:
        draw.text((ox - x0, oy - y0), content, font=font, fill=color, anchor="lt")
    meta = {
        "text": content,
        "font_px": int(getattr(font, "size", 0) or 0),
        "padx": padx,
        "pady": pady,
        "size": (width, height),
        "cell": (cell_w, cell_h),
        "ink_box": (ox, oy, ox + ink_w, oy + ink_h),
        "underline_box": None,
    }
    if underline and content and ink_w > 0:
        thickness = max(1, int(round(meta["font_px"] / 12.0)))
        # 下划线只画在**真实墨迹底边以下**、且高度够放的那几行里：
        # 不压字、不改图像尺寸、不动字形位置；放不下就不画。
        top = oy + ink_h
        left, right = ox, ox + ink_w - 1
        if left <= right and top + thickness <= height:
            draw.rectangle([left, top, right, top + thickness - 1], fill=color)
            meta["underline_box"] = (left, top, right + 1, top + thickness)
    return image, meta


def photo_image(image, master=None):
    """把 PIL 图像转成 Tk 可显示的 ``ImageTk.PhotoImage``（**可 patch 的工厂**）。

    * 延迟导入 ``PIL.ImageTk``：导入 ``app.ui.widgets`` 时**不碰 Tk**；
    * 真实实现需要一个真实 Tk 解释器（``master`` 就是那个按钮控件），
      因此测试一律 patch 掉这个工厂，绝不触发真实 Tk。
    """
    from PIL import ImageTk
    return ImageTk.PhotoImage(image, master=master)


__all__ = ["PIL_AVAILABLE", "available", "center_ink", "font_px",
           "ink_bbox", "load_font", "photo_image", "render_text_image", "text_cell"]
