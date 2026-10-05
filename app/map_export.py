"""参考关系图的**图片导出**：一张完整的 PNG + 一份可编辑的 SVG。

定位
----
用户点「导出关系图…」要的是**图**，不是数据表（要数据去主界面用「导出」）。
所以这里只产出图片，同一个名字成对出现：

* **SVG**（主产物）：手写矢量图 —— 方框、连线都是真实图形，文字是**真文字**，
  在浏览器 / Inkscape / Illustrator / Figma 里都能直接改（纯字符串拼装，
  见 :func:`svg_document`）；
* **PNG**（附赠）：把画布上的**整张图**抓成位图。内容比视口大时按视口**分块
  抓取再拼起来**（见 :func:`capture_canvas_png`），不是只抓看得见的那一块。

三条硬约束
----------
1. **只依赖标准库**：冻结运行时里没有 Pillow / numpy，所以 PNG 由 :mod:`zlib`
   + :mod:`struct` 手写（见 :func:`write_png`），抓图走 :mod:`ctypes`，不引入
   任何第三方截图库。
2. **抓图不建窗口**：:func:`capture_canvas_png` 只读**已经存在**的 Tk 画布
   句柄（``winfo_id()``），自己绝不 ``create_window``；模块 import 时也不调用
   任何 Win32 API —— 没有窗口的环境（测试 / 无头）照样能 import 本模块。
3. **图片抓不到也不丢图**：SVG 先落地；PNG 失败只把原因写进
   :attr:`MapExportResult.png_error`，用户在 RDP / 无桌面会话下点导出，
   仍然拿到一份完整的可编辑矢量图。

导出的**口径**（与界面一致，别改写）
------------------------------------
* 连线只是 **AI 参考**：已按材料证据筛过 + 独立核对过，但不是原文事实；
* 连线的**颜色与粗细全图统一**（用户口径）：人工添加的关系**不在线上**做标记，
  也不给标签加前缀 —— 想分辨哪条是自己加的，看界面的依据区、双击这条线，
  或者看页脚里的人工条数；
* 页脚写明主题、词数 / 关系数、模型与导出时间，图离开软件也看得懂。
"""
from __future__ import annotations

import ctypes
import math
import os
import struct
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .logging_setup import get_logger
from . import export_service, paths

log = get_logger("map_export")

#: 人工关系的来源标记（与 :data:`app.map_service.MANUAL_ORIGIN` 一致）。
#: 这里**故意不 import map_service** —— 那个模块会连锁拉进 config → db →
#: crypto_dpapi（进程内一开始就 WinDLL("crypt32")），而导出模块要能在
#: 没有数据库、没有 Win32 的环境里被单独 import（有测试守着这条）。
MANUAL_ORIGIN = "user"

#: 文件名前缀（与 :mod:`app.export_service` 同一套命名）
FILE_PREFIX = "探索词典"
#: 文件名里的标签（``探索词典-关系图-<标签>-<时间戳>``）
SCOPE_LABEL = "关系图"
#: 主题名为空时用的标签
FALLBACK_STEM = "全部"
#: SVG 命名空间
SVG_NAMESPACE = "http://www.w3.org/2000/svg"

#: 分块抓图时相邻两块的**重叠像素**：边界上有一条缝比多抓两列难看得多
TILE_OVERLAP = 2
#: 一张 PNG 的像素上限（超过就按整数倍**抽稀**，图仍然完整，只是小一点）
MAX_CAPTURE_PIXELS = 8_000_000
#: 抓图位图的像素上限（超过就直接说清楚：图太大，先缩小视图）
MAX_NATIVE_PIXELS = 40_000_000
#: 内容外框四周留的白边（像素）
CONTENT_MARGIN = 6.0
#: 页脚图例（与界面上那条灰字同口径：人工关系不在线上做标记）
LEGEND_LINE = "连线为模型候选、已按材料证据筛过；人工添加的关系不另做标记，看页脚条数"

#: SVG 的默认配色 / 字号（与 ``app.ui.theme`` 同色；界面会把自己的那份传进来）
DEFAULT_STYLE: dict = {
    "font": "Microsoft YaHei UI, PingFang SC, Noto Sans CJK SC, sans-serif",
    "bg": "#F9F8F6",
    "card": "#FCFBF8",
    "panel": "#F9F8F6",
    "panel_alt": "#F2F1EC",
    "border": "#E3E1DC",
    "border_strong": "#C8C6C0",
    "text": "#1C1C1C",
    "text_body": "#3A3936",
    "muted": "#6E6C67",
    "faint": "#9B9993",
    "accent": "#1C1C1C",
    "accent_soft": "#EDEBE6",
    "node_font": 12.0,
    "topic_font": 13.4,
    "edge_font": 9.5,
    "label_font": 9.5,
    "footer_font": 10.5,
    "node_radius": 9.0,
    "topic_radius": 12.0,
    "group_radius": 10.0,
    #: 组名字号（批次 M13：分组画成「细框 + 组名」，不再是褐色底衬）
    "group_font": 8.0,
    "line_width": 1.0,
    "edge_fill": "#9B9993",
    "label_fill": "#6E6C67",
    "margin": CONTENT_MARGIN,
    "isolated_text": "孤立词",
    "title": "参考关系图",
    #: ``{kind: {"stroke": ..., "dash": (...)}}``，由界面用 ``EDGE_STYLES`` 填
    "edges": {},
}

#: ``PrintWindow`` 的 ``PW_RENDERFULLCONTENT``（Win 8.1+：能抓到 DirectComposition
#: 渲染的内容，否则 Tk 画布可能是一片空白）
_PW_RENDERFULLCONTENT = 0x00000002
#: ``BitBlt`` 的 ``SRCCOPY``
_SRCCOPY = 0x00CC0020
#: ``DIB_RGB_COLORS``
_DIB_RGB_COLORS = 0


class MapExportError(RuntimeError):
    """参考关系图导出失败（消息里说明**是哪一步**失败，方便用户复述）。"""


@dataclass(frozen=True)
class MapExportResult:
    """一次关系图导出的结果（界面据此显示反馈 / 打开文件夹）。

    ``svg_path`` 一定存在（写不出来就是失败，直接抛异常）；``png_path`` 为
    ``None`` 表示「矢量图写好了，但位图没抓到」—— 这**不是**失败：界面应该把
    ``png_error`` 如实说给用户，而不是报「导出失败」。
    """

    svg_path: Path
    png_path: Path | None
    node_count: int
    count: int
    width: int = 0
    height: int = 0
    step: int = 1
    png_error: str = ""

    @property
    def directory(self) -> Path:
        return self.svg_path.parent

    def summary(self) -> str:
        text = (f"已导出完整关系图（{self.node_count} 个词 / {self.count} 条关系）："
                f"{self.svg_path.name}（可编辑矢量图）")
        if self.png_path is not None:
            text += f" + {self.png_path.name}（{self.width}×{self.height} 图片"
            if self.step > 1:
                text += f"，图很大已按 1/{self.step} 抽稀"
            text += "）"
        elif self.png_error:
            text += f"；PNG 没抓到：{self.png_error}"
        return text


# ---------------------------------------------------------------- 纯函数：文字量
def char_units(text: str) -> float:
    """估算显示宽度（汉字 / 全角 = 1.0 个字宽，半角 = 0.55；纯函数）。

    与 ``app.ui.concept_map.char_units`` **同一口径**：两处都按「一个 em 放一个
    汉字」估宽，导出的卡片与标签才不会比画布上宽窄不一（那里的实现依赖 tkinter 度量，
    这里不能引，所以各留一份、口径写死在这里）。
    """
    return sum(1.0 if ord(ch) > 0x2E7F else 0.55 for ch in str(text or ""))


def text_width(text: str, font_px: float) -> float:
    """一段文字在 ``font_px`` 字号下大约多宽（像素）。"""
    return char_units(text) * float(font_px)


def _num(value) -> str:
    """SVG 里的紧凑数字（``12.50`` → ``12.5``，``12.00`` → ``12``）。"""
    text = f"{float(value):.2f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-") else "0"


def _esc(text) -> str:
    """XML 文本转义（``&`` 必须第一个换，否则会二次转义）。"""
    return (str(text if text is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def _style(style=None) -> dict:
    """默认样式 + 调用方覆盖（只做一层合并，``edges`` 单独深一层）。"""
    out = dict(DEFAULT_STYLE)
    if style:
        override = dict(style)
        edges = override.pop("edges", None)
        out.update({key: value for key, value in override.items() if value is not None})
        merged = dict(DEFAULT_STYLE.get("edges") or {})
        merged.update(dict(edges or {}))
        out["edges"] = merged
    return out


def is_manual_edge(edge) -> bool:
    """这条连线是不是**人工添加**的（``edge.rel`` 上的 ``origin`` 说了算）。"""
    rel = getattr(edge, "rel", edge)
    return str(getattr(rel, "origin", "") or "") == MANUAL_ORIGIN


def edge_style(edge, style=None) -> tuple[str, float, tuple[float, ...]]:
    """一条连线的 ``(描边色, 线宽, 虚线)``。

    用户口径：连线的颜色与粗细**全图统一**，人工添加的关系不再特殊对待 ——
    所以这里不区分 ``edge`` 是不是人工的，只按线型取 stroke / dash，
    线宽永远是 ``style["line_width"]``。
    """
    st = _style(style)
    spec = dict((st.get("edges") or {}).get(getattr(edge, "kind", "")) or {})
    stroke = str(spec.get("stroke") or st.get("edge_fill") or st["text_body"])
    dash = tuple(float(v) for v in (spec.get("dash") or ()))
    return stroke, float(st["line_width"]), dash


# ---------------------------------------------------------------- 纯函数：分块
def tile_origins(extent: int, tile: int, *, overlap: int = TILE_OVERLAP) -> list[int]:
    """一维分块：返回每块的**请求起点**（0 基，相对内容左上角；纯函数）。

    规则：每块最多 ``tile`` 长，相邻两块重叠 ``overlap``；**最后一块向右贴齐
    边界**（起点 = ``extent - tile``），所以右 / 下边一定被覆盖。``extent <= tile``
    时只有一块。返回的起点严格递增且都 ``<= extent - tile``（不会请求一个会被
    滚动位置夹回去的坐标）。
    """
    extent = max(0, int(extent))
    tile = max(1, int(tile))
    if extent <= tile:
        return [0]
    limit = extent - tile                       # 最后一个可用起点（贴右 / 下边界）
    step = max(1, tile - 2 * max(0, int(overlap)))
    out = [0]
    pos = 0
    while pos < limit:
        pos = min(pos + step, limit)            # 最后一块直接贴齐，别多走一步
        out.append(pos)
    return sorted(set(out))


def plan_tiles(width: int, height: int, tile_w: int, tile_h: int,
               *, overlap: int = TILE_OVERLAP) -> list[tuple[int, int]]:
    """整张图 → 若干「抓图起点」坐标（纯函数，顺序为从上到下、从左到右）。"""
    xs = tile_origins(width, tile_w, overlap=overlap)
    ys = tile_origins(height, tile_h, overlap=overlap)
    return [(x, y) for y in ys for x in xs]


def sample_step(width: int, height: int, *, max_pixels: int = MAX_CAPTURE_PIXELS) -> int:
    """图片太大时用的**整数抽稀倍数**（1 = 原尺寸；纯函数）。

    ``max_pixels <= 0``（设成没有上限）直接返回 1 —— 唯一不设上限的合法解释。
    """
    width = max(1, int(width))
    height = max(1, int(height))
    limit = _int_or_zero(max_pixels)
    if limit <= 0:
        return 1
    step = 1
    while ((width + step - 1) // step) * ((height + step - 1) // step) > limit:
        step += 1
    return step


# ---------------------------------------------------------------- 纯函数：内容外框
def content_box(layout, *, style=None, margin=None) -> tuple[float, float, float, float]:
    """整张图（不只是视口）在画布坐标里的外框，四边留 ``margin`` 像素白边。

    把分组底衬、主题细线、连线（含弧线拐点）、关系标签（**没有底板**，所以要按
    文字本身的外框算）、节点卡片、词条标题、孤立词说明**全部**并进来 —— 既然用户要的是
    「完整」，只按节点算就会把绕在外面的连线和标签裁掉。``x0`` / ``y0`` 夹到 ≥ 0
    （画布坐标原点就是内容的左上角，抓图时请求负坐标会被滚动位置悄悄夹住）。

    空图抛 :class:`MapExportError`（没东西可导）。
    """
    st = _style(style)
    pad = float(st["margin"]) if margin is None else float(margin)
    xs: list[float] = []
    ys: list[float] = []

    def add(x, y) -> None:
        xs.append(float(x))
        ys.append(float(y))

    def add_rect(x0, y0, x1, y1) -> None:
        add(x0, y0)
        add(x1, y1)

    for group in getattr(layout, "groups", ()) or ():
        add_rect(group.x0, group.y0, group.x1, group.y1)
    for link in getattr(layout, "topic_links", ()) or ():
        for point in link or ():
            add(point[0], point[1])
    for edge in getattr(layout, "edges", ()) or ():
        for point in getattr(edge, "points", ()) or ():
            add(point[0], point[1])
        label = str(getattr(edge, "label", "") or "")
        pos = getattr(edge, "label_pos", None)
        if label and pos:
            # 标签就是线型短名（「依赖」这种），人工关系的标签不再加前缀。
            half_w = text_width(label, float(st["label_font"])) * 0.5 + float(st["label_font"]) * 0.4
            half_h = max(float(st["label_font"]) * 1.1, 1.0)
            add(float(pos[0]) - half_w, float(pos[1]) - half_h)
            add(float(pos[0]) + half_w, float(pos[1]) + half_h)

    boxes = list(getattr(layout, "nodes", ()) or ())
    topic = getattr(layout, "topic", None)
    if topic is not None and float(getattr(topic, "w", 0) or 0) > 0:
        boxes.append(topic)
    for node in boxes:
        half_w = abs(float(getattr(node, "w", 0) or 0)) * 0.5
        half_h = abs(float(getattr(node, "h", 0) or 0)) * 0.5
        add(float(node.x) - half_w, float(node.y) - half_h)
        add(float(node.x) + half_w, float(node.y) + half_h)

    text = str(st.get("isolated_text") or "")
    pos = getattr(layout, "isolated_label_pos", None)
    if text and pos:
        half_h = float(st["edge_font"]) * 1.2
        add(float(pos[0]), float(pos[1]) - half_h)
        add(float(pos[0]) + text_width(text, float(st["edge_font"])), float(pos[1]) + half_h)

    if not xs or not ys:
        raise MapExportError("画布上还没有可以导出的内容：先在阅读页解释并记录几个词，等关系图画出来再导出")
    return (max(0.0, min(xs) - pad), max(0.0, min(ys) - pad),
            max(xs) + pad, max(ys) + pad)


def layout_counts(layout) -> tuple[int, int, int]:
    """``(词条数, 关系数, 人工关系数)``（纯函数；标题不算词条）。"""
    nodes = list(getattr(layout, "nodes", ()) or ())
    edges = list(getattr(layout, "edges", ()) or ())
    manual = sum(1 for edge in edges if is_manual_edge(edge))
    return len(nodes), len(edges), manual


# ---------------------------------------------------------------- SVG 渲染
def _svg_text(x, y, lines, *, fill: str, font_px: float, anchor: str = "middle",
              line_h: float | None = None) -> str:
    """一段（可多行）文字 —— 每行一个 ``<tspan>``，行距按字号算。"""
    items = [str(line) for line in (lines or ())] or [""]
    font_px = float(font_px)
    line_h = float(line_h if line_h else font_px * 1.28)
    first = float(y) - (len(items) - 1) * line_h / 2.0 + font_px * 0.34
    head = (f'<text x="{_num(x)}" y="{_num(first)}" fill="{fill}"'
            f' font-size="{_num(font_px)}" text-anchor="{anchor}">')
    body = "".join(f'<tspan x="{_num(x)}" y="{_num(first + index * line_h)}">'
                   f"{_esc(line)}</tspan>" for index, line in enumerate(items))
    return f"{head}{body}</text>"


def _svg_rect(x0, y0, x1, y1, *, fill: str, stroke: str = "", width: float = 0.0,
              radius: float = 0.0) -> str:
    """矩形（``x0,y0,x1,y1`` 对角坐标；``radius > 0`` 就是圆角）。"""
    stroke_part = f' stroke="{stroke}" stroke-width="{_num(width)}"' if stroke else ""
    radius_part = f' rx="{_num(radius)}"' if radius else ""
    return (f'<rect x="{_num(x0)}" y="{_num(y0)}" width="{_num(float(x1) - float(x0))}"'
            f' height="{_num(float(y1) - float(y0))}" fill="{fill}"{stroke_part}{radius_part}/>')


def _svg_polyline(points, *, stroke: str, width: float, dash=(), marker: str = "",
                  marker_id: str = "") -> str:
    """折线（连线就是折线 + 末端箭头；虚线用 ``stroke-dasharray``）。"""
    coords = " ".join(f"{_num(x)},{_num(y)}" for x, y in (points or ()))
    dash_part = ""
    if dash:
        dash_part = f' stroke-dasharray="{" ".join(_num(v) for v in dash)}"'
    marker_part = f' marker-end="url(#{marker_id})"' if (marker and marker_id) else ""
    return (f'<polyline points="{coords}" fill="none" stroke="{stroke}"'
            f' stroke-width="{_num(width)}" stroke-linecap="round"'
            f' stroke-linejoin="round"{dash_part}{marker_part}/>')


def _arrow_defs(edges, st: dict) -> tuple[str, dict[str, str]]:
    """按出现过的描边色各生成一个箭头 marker，返回 ``(defs文本, 颜色→id)``。"""
    colors: list[str] = []
    for edge in edges:
        if getattr(edge, "symmetric", False):
            continue        # 对称关系（对照）不画箭头，与画布一致
        stroke = edge_style(edge, st)[0]
        if stroke not in colors:
            colors.append(stroke)
    if not colors:
        return "", {}
    mapping: dict[str, str] = {}
    lines = ["<defs>"]
    for index, color in enumerate(colors, start=1):
        marker_id = f"arrow-{index}"
        mapping[color] = marker_id
        lines.append(
            f'<marker id="{marker_id}" viewBox="0 0 10 10" refX="9.5" refY="5"'
            f' markerWidth="9" markerHeight="8" markerUnits="userSpaceOnUse"'
            f' orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z"'
            f' fill="{color}"/></marker>')
    lines.append("</defs>")
    return "\n".join(lines), mapping


def _footer_lines(meta, layout, st: dict) -> list[str]:
    """页脚三行（主题 / 数量与模型 / 图例）—— 图离开软件也要看得懂。"""
    if not meta:
        return []
    nodes, edges, manual = layout_counts(layout)
    topic = str((meta or {}).get("topic_name") or "").strip() or FALLBACK_STEM
    first = f"{FILE_PREFIX} · {SCOPE_LABEL} —— {topic}"
    second = f"{nodes} 个词 / {edges} 条关系"
    if manual:
        second += f"（其中人工 {manual} 条）"
    model = str((meta or {}).get("model_config") or "").strip()
    if model:
        second += f"　模型：{model}"
    template = str((meta or {}).get("template_name") or "").strip()
    if template:
        second += f"　模板：{template}"
    stamp = str((meta or {}).get("exported_at") or "").strip()
    if stamp:
        second += f"　导出：{stamp}"
    return [first, second, LEGEND_LINE]


def _footer_height(st: dict, footer: list[str]) -> float:
    if not footer:
        return 0.0
    font = float(st["footer_font"])
    return font * (1.65 * len(footer) + 0.9)


def svg_document(*, layout, style=None, box=None, meta=None) -> str:
    """把一次布局渲染成**完整、可编辑**的 SVG 文本（纯函数，不碰文件）。

    画法与 ``app.ui.concept_map._draw`` 一一对应（分组底衬 → 主题细线 → 连线 →
    标签 → 卡片 → 词条标题 → 孤立词说明 → 页脚），所以导出的 SVG 和屏幕上是
    同一张图；区别只有两点：坐标是画布坐标（整张图，不是视口），以及矢量文字
    可以被再次编辑。
    """
    st = _style(style)
    if box is not None:
        x0, y0, x1, y1 = (float(v) for v in box)
    else:
        x0, y0, x1, y1 = content_box(layout, style=st)
    width = max(1.0, x1 - x0)
    height = max(1.0, y1 - y0)

    groups = list(getattr(layout, "groups", ()) or ())
    links = list(getattr(layout, "topic_links", ()) or ())
    edges = list(getattr(layout, "edges", ()) or ())
    nodes = list(getattr(layout, "nodes", ()) or ())
    topic = getattr(layout, "topic", None)

    footer = _footer_lines(meta, layout, st)
    footer_h = _footer_height(st, footer)
    total_h = height + footer_h

    defs, arrows = _arrow_defs(edges, st)
    parts = ['<?xml version="1.0" encoding="UTF-8"?>']
    parts.append(
        f'<svg xmlns="{SVG_NAMESPACE}" version="1.1" width="{_num(width)}"'
        f' height="{_num(total_h)}" viewBox="{_num(x0)} {_num(y0)} {_num(width)} {_num(total_h)}"'
        f' font-family="{_esc(st["font"])}">')
    title = str(st.get("title") or "参考关系图")
    node_count, edge_count, manual_count = layout_counts(layout)
    desc = (f"{title}：{node_count} 个词、{edge_count} 条关系"
            f"（其中人工 {manual_count} 条），矢量图可直接编辑")
    parts.append(f"  <title>{_esc(title)}</title>")
    parts.append(f"  <desc>{_esc(desc)}</desc>")
    if defs:
        parts.append(defs)
    parts.append(f'  <rect x="{_num(x0)}" y="{_num(y0)}" width="{_num(width)}"'
                 f' height="{_num(total_h)}" fill="{st["bg"]}"/>')

    # ① 分组：画成**组标签**（细边框 + 组名），不再是实心底衬（批次 M13）——
    #    用户口径「这个褐色背景应该是作为组标签」。框里有别人的卡片时
    #    （``framed=False``）只画组名、不画框，免得把不相干的词也圈进来。
    for group in groups:
        framed = bool(getattr(group, "framed", True))
        if framed:
            parts.append("  " + _svg_rect(group.x0, group.y0, group.x1, group.y1,
                                          fill="none", stroke=str(st["border"]),
                                          width=float(st["line_width"]),
                                          radius=float(st["group_radius"])))
        label = str(getattr(group, "label", "") or "")
        if not label:
            continue
        if framed:
            left, top = group.x0 + 5.0, group.y0 + 4.0
        else:
            finder = getattr(layout, "find", None)
            node = finder(group.root_id) if callable(finder) else None
            left = (node.x - node.w / 2.0) if node is not None else group.x0
            top = (node.y - node.h / 2.0 - 9.0) if node is not None else group.y0
        group_px = float(st.get("group_font", 8.0))
        parts.append("  " + _svg_text(left, top + group_px * 0.5, [label],
                                      fill=str(st["muted"]), font_px=group_px,
                                      anchor="start"))
    # ② 主题细线（词条标题 → 各分组）
    for link in links:
        parts.append("  " + _svg_polyline(link, stroke=str(st["border"]),
                                          width=float(st["line_width"])))
    # ③ 连线 + 标签
    labels: list[str] = []
    for edge in edges:
        stroke, line_w, dash = edge_style(edge, st)
        parts.append("  " + _svg_polyline(edge.points, stroke=stroke, width=line_w, dash=dash,
                                          marker="" if getattr(edge, "symmetric", False) else "arrow",
                                          marker_id=arrows.get(stroke, "")))
        label = str(getattr(edge, "label", "") or "")
        pos = getattr(edge, "label_pos", None)
        if not label or not pos:
            continue
        # 标签**没有底板**（用户要求：文字背景透明），也不带「人工·」前缀：
        # 人工关系与 AI 关系在图上完全同款，颜色统一用一个 ``label_fill``。
        font = float(st["label_font"])
        labels.append("  " + _svg_text(pos[0], pos[1], [label],
                                       fill=str(st.get("label_fill") or st["muted"]),
                                       font_px=font))
    parts.extend(labels)
    # ④ 词条卡片
    for node in nodes:
        half_w = abs(float(getattr(node, "w", 0) or 0)) * 0.5
        half_h = abs(float(getattr(node, "h", 0) or 0)) * 0.5
        parts.append("  " + _svg_rect(
            float(node.x) - half_w, float(node.y) - half_h,
            float(node.x) + half_w, float(node.y) + half_h,
            fill=str(st["panel"] if getattr(node, "isolated", False) else st["card"]),
            stroke=str(st["border_strong"]), width=float(st["line_width"]),
            radius=float(st["node_radius"])))
        lines = list(getattr(node, "lines", ()) or ()) or [str(getattr(node, "label", "") or "")]
        parts.append("  " + _svg_text(node.x, node.y, lines, fill=str(st["text"]),
                                      font_px=float(st["node_font"])))
    # ⑤ 词条标题
    if topic is not None and float(getattr(topic, "w", 0) or 0) > 0:
        half_w = abs(float(topic.w)) * 0.5
        half_h = abs(float(topic.h)) * 0.5
        parts.append("  " + _svg_rect(
            float(topic.x) - half_w, float(topic.y) - half_h,
            float(topic.x) + half_w, float(topic.y) + half_h,
            fill=str(st["accent_soft"]), stroke=str(st["accent"]),
            width=float(st["line_width"]), radius=float(st["topic_radius"])))
        lines = list(getattr(topic, "lines", ()) or ()) or [str(getattr(topic, "label", "") or "")]
        parts.append("  " + _svg_text(topic.x, topic.y, lines, fill=str(st["text"]),
                                      font_px=float(st["topic_font"])))
    # ⑥ 孤立词说明
    iso_text = str(st.get("isolated_text") or "")
    iso_pos = getattr(layout, "isolated_label_pos", None)
    if iso_text and iso_pos:
        parts.append("  " + _svg_text(iso_pos[0], iso_pos[1], [iso_text],
                                      fill=str(st["faint"]), font_px=float(st["edge_font"]),
                                      anchor="start"))
    # ⑦ 页脚
    if footer:
        top = y1
        font = float(st["footer_font"])
        parts.append("  " + _svg_rect(x0, top, x0 + width, top + footer_h, fill=str(st["panel_alt"])))
        parts.append("  " + _svg_polyline([(x0, top), (x0 + width, top)],
                                          stroke=str(st["border"]), width=float(st["line_width"])))
        for index, line in enumerate(footer):
            y = top + font * (0.95 + index * 1.65)
            fill = str(st["muted"] if index == 0 else st["faint"])
            parts.append("  " + _svg_text(x0 + font * 0.8, y, [line], fill=fill,
                                          font_px=font, anchor="start"))
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def write_svg(path, text) -> Path:
    """把 SVG 文本原子写出（临时文件 + ``os.replace``，绝不留下半截文件）。"""
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    try:
        tmp.write_text(str(text), encoding="utf-8", newline="")
        os.replace(tmp, target)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - 文件被占用
            pass
        raise MapExportError(f"写入 SVG 失败（{target.name}）：{exc}") from exc
    return target


# ---------------------------------------------------------------- PNG 写出
def _png_chunk(tag: bytes, data: bytes) -> bytes:
    """一个 PNG chunk：长度 + 标签 + 数据 + ``CRC32(标签 + 数据)``。"""
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _int_or_zero(value) -> int:
    """把任意值转成 int，转不了就当 0（尺寸参数来自外部，必须稳）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def write_png(path, width: int, height: int, rows) -> None:
    """把像素写成 8 位 RGB 的 PNG（纯标准库：``zlib`` + ``struct``）。

    ``rows`` 两种形式：

    * ``bytes``：长度必须是 ``width * height * 3``，按 RGB、**自上而下**；
    * 可迭代对象：每行一段 ``bytes``（每行 ``width * 3``）。

    每个扫描行用 filter type 0（None）—— 不做 filter 优化，简单正确优先。
    本来就不需要优化：这张图是画布快照，几 MB 的东西 zlib 的默认压缩足够，
    而「手写 filter 试探」是这类代码最容易写错的地方。

    写文件走**临时文件 + 原子替换**：任何一步失败都把临时文件删掉，绝不留下
    半截 PNG 让用户以为导出成功了。

    :raises MapExportError: ``width`` / ``height`` < 1，或行数据长度不对。
    """
    width = _int_or_zero(width)
    height = _int_or_zero(height)
    if width < 1 or height < 1:
        raise MapExportError(f"PNG 尺寸非法：{width}x{height}（宽高都必须 ≥ 1）")
    stride = width * 3

    if isinstance(rows, (bytes, bytearray, memoryview)):
        raw = bytes(rows)
        if len(raw) != stride * height:
            raise MapExportError(
                f"PNG 像素数据长度不符：期望 {stride * height} 字节"
                f"（{width}x{height} RGB），实际 {len(raw)} 字节")
        lines = (raw[offset:offset + stride] for offset in range(0, len(raw), stride))
    else:
        lines = rows

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    payload = bytearray()
    seen = 0
    for line in lines:
        data = bytes(line)
        if len(data) != stride:
            raise MapExportError(
                f"PNG 第 {seen + 1} 行长度不符：期望 {stride} 字节，实际 {len(data)} 字节")
        payload += b"\x00"          # filter type 0（None）
        payload += data
        seen += 1
    if seen != height:
        raise MapExportError(f"PNG 行数不符：期望 {height} 行，实际 {seen} 行")

    blob = (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(bytes(payload)))
            + _png_chunk(b"IEND", b""))
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    try:
        tmp.write_bytes(blob)
        os.replace(tmp, target)
    except Exception as exc:  # pragma: no cover - 磁盘满 / 权限
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise MapExportError(f"写入 PNG 失败（{target.name}）：{exc}") from exc


# ---------------------------------------------------------------- 画布抓图
class _BitmapInfoHeader(ctypes.Structure):
    """``BITMAPINFOHEADER``（``GetDIBits`` 要求 ``biSize`` 先填好）。"""

    _fields_ = (
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    )


class _BitmapInfo(ctypes.Structure):
    """``BITMAPINFO`` = 头 + 一张调色板（``biBitCount=32`` 时用不到）。"""

    _fields_ = (("bmiHeader", _BitmapInfoHeader), ("bmiColors", ctypes.c_uint32 * 3))


class _Rect(ctypes.Structure):
    _fields_ = (("left", ctypes.c_int32), ("top", ctypes.c_int32),
                ("right", ctypes.c_int32), ("bottom", ctypes.c_int32))


def _gdi_libraries():
    """延迟加载 ``user32`` / ``gdi32`` 并**显式声明句柄类型**（只在调用时加载）。

    为什么必须声明 ``restype``：64 位下 ``HDC`` / ``HBITMAP`` 是 64 位句柄，
    不声明的话 ``ctypes`` 默认按 ``c_int``（32 位）返回，句柄高位会被截断 ——
    后续 ``GetDIBits`` 直接失败或写坏内存。这是本项目里唯一一处必须写
    ``argtypes`` 的地方，删掉它抓图在别人机器上就会间歇性失败。

    另外这里**不在模块 import 时执行**（函数内局部导入）：测试要在没有窗口 /
    没有桌面的环境里 import 本模块，import 期不得碰任何 Win32 API。
    """
    import ctypes.wintypes as wintypes  # noqa: F401 - 确保 wintypes 已加载

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    except (AttributeError, OSError) as exc:  # pragma: no cover - 非 Windows
        raise MapExportError(f"当前环境没有 Windows 图形接口（{exc}）") from exc

    hwnd_t = ctypes.c_void_p
    hdc_t = ctypes.c_void_p
    hbitmap_t = ctypes.c_void_p
    hgdiobj_t = ctypes.c_void_p
    user32.GetClientRect.argtypes = [hwnd_t, ctypes.POINTER(_Rect)]
    user32.GetClientRect.restype = ctypes.c_int
    user32.GetDC.argtypes = [hwnd_t]
    user32.GetDC.restype = hdc_t
    user32.ReleaseDC.argtypes = [hwnd_t, hdc_t]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.PrintWindow.argtypes = [hwnd_t, hdc_t, ctypes.c_uint]
    user32.PrintWindow.restype = ctypes.c_int
    gdi32.CreateCompatibleDC.argtypes = [hdc_t]
    gdi32.CreateCompatibleDC.restype = hdc_t
    gdi32.CreateCompatibleBitmap.argtypes = [hdc_t, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = hbitmap_t
    gdi32.SelectObject.argtypes = [hdc_t, hgdiobj_t]
    gdi32.SelectObject.restype = hgdiobj_t
    gdi32.DeleteObject.argtypes = [hgdiobj_t]
    gdi32.DeleteObject.restype = ctypes.c_int
    gdi32.DeleteDC.argtypes = [hdc_t]
    gdi32.DeleteDC.restype = ctypes.c_int
    gdi32.BitBlt.argtypes = [hdc_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             hdc_t, ctypes.c_int, ctypes.c_int, ctypes.c_uint32]
    gdi32.BitBlt.restype = ctypes.c_int
    gdi32.GetDIBits.argtypes = [hdc_t, hbitmap_t, ctypes.c_uint, ctypes.c_uint,
                                ctypes.c_void_p, ctypes.POINTER(_BitmapInfo), ctypes.c_uint]
    gdi32.GetDIBits.restype = ctypes.c_int
    return user32, gdi32


def bgra_to_rgb(buffer, width: int, height: int, *, step: int = 1) -> bytes:
    """``GetDIBits`` 拿到的 BGRA 缓冲 → 自上而下的 RGB（纯函数，便于单测）。

    ``biHeight = -height`` 时 DIB 是**自顶向下**存的，所以这里不翻转行序，
    只把每像素的 B / R 交换，并丢掉 alpha（``biBitCount=32`` 一定有 4 字节/像素）。
    ``step > 1`` 时每隔 ``step`` 个像素取一个（超大图抽稀，输出尺寸取上整）。
    """
    width = _int_or_zero(width)
    height = _int_or_zero(height)
    step = max(1, _int_or_zero(step) or 1)
    raw = bytes(buffer)
    stride = width * 4
    if width < 1 or height < 1 or len(raw) < stride * height:
        raise MapExportError(
            f"抓到的像素数据不完整：{width}x{height} 需要 {stride * height} 字节，"
            f"实际 {len(raw)} 字节")
    out_w = (width + step - 1) // step
    out_h = (height + step - 1) // step
    out = bytearray(out_w * out_h * 3)
    for row in range(out_h):
        base = (row * step) * stride
        target = row * out_w * 3
        for col in range(out_w):
            off = base + (col * step) * 4
            dst = target + col * 3
            out[dst] = raw[off + 2]        # R
            out[dst + 1] = raw[off + 1]    # G
            out[dst + 2] = raw[off]        # B
    return bytes(out)


def _release_dc(gdi32, dc, bitmap, previous) -> None:
    """放掉一组「离屏 DC + 位图」（位图必须先选出去才能删；清理阶段不抛）。"""
    if gdi32 is None or not dc:
        return
    if previous:
        try:
            gdi32.SelectObject(dc, previous)
        except Exception:  # pragma: no cover
            pass
    if bitmap:
        try:
            gdi32.DeleteObject(bitmap)
        except Exception:  # pragma: no cover
            pass
    try:
        gdi32.DeleteDC(dc)
    except Exception:  # pragma: no cover
        pass


def _cleanup_gdi(user32, gdi32, hwnd, hdc, contexts) -> None:
    """按正确顺序释放 GDI 对象（任何一步失败都不影响其余清理）。"""
    for dc, bitmap, previous in contexts or ():
        _release_dc(gdi32, dc, bitmap, previous)
    if user32 is not None and hdc and hwnd:
        try:
            user32.ReleaseDC(hwnd, hdc)
        except Exception:  # pragma: no cover - 清理阶段不抛
            pass


def _remove_partial(path) -> None:
    """把可能已经写了一半的图片删掉（绝不留下半截 PNG）。"""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:  # pragma: no cover - 文件不存在 / 被占用
        pass


# ---------------------------------------------------------------- 视口滚动
def _view_origin(canvas) -> tuple[float, float]:
    """画布左上角对应的**画布坐标**（= 当前滚动位置；拿不到就当 0）。"""
    out: list[float] = []
    for name in ("canvasx", "canvasy"):
        func = getattr(canvas, name, None)
        try:
            out.append(float(func(0)) if callable(func) else 0.0)
        except Exception:  # pragma: no cover - 假画布 / 窗口已销毁
            out.append(0.0)
    return out[0], out[1]


def _scroll_to(canvas, x: float, y: float) -> None:
    """把视口滚到画布坐标 ``(x, y)`` 处（``xview_moveto`` 用比例，所以要读回
    ``scrollregion``；没有滚动范围 / 假画布就什么都不做）。"""
    try:
        parts = [float(value) for value in str(canvas.cget("scrollregion")).split()]
    except Exception:
        return
    if len(parts) != 4:
        return
    left, top, right, bottom = parts
    for name, value, lo, hi in (("xview_moveto", x, left, right),
                                ("yview_moveto", y, top, bottom)):
        func = getattr(canvas, name, None)
        if not callable(func):
            continue
        span = max(1.0, hi - lo)
        fraction = min(1.0, max(0.0, (float(value) - lo) / span))
        try:
            func(fraction)
        except Exception:  # pragma: no cover - 假画布
            pass


def _settle(canvas) -> None:
    """让 Tk 把滚动后的界面真正重画出来（假画布上没有这个方法）。"""
    func = getattr(canvas, "update_idletasks", None)
    if callable(func):
        try:
            func()
        except Exception:  # pragma: no cover - 窗口已销毁
            pass


def _saved_view(canvas):
    """记下当前滚动位置（抓完要还回去）。"""
    out: list[float] = []
    for name in ("xview", "yview"):
        func = getattr(canvas, name, None)
        try:
            out.append(float(func()[0]) if callable(func) else 0.0)
        except Exception:  # pragma: no cover
            return None
    return tuple(out)


def _restore_view(canvas, saved) -> None:
    if not saved:
        return
    for name, value in (("xview_moveto", saved[0]), ("yview_moveto", saved[1])):
        func = getattr(canvas, name, None)
        if callable(func):
            try:
                func(value)
            except Exception:  # pragma: no cover
                pass


def _grab_tile(user32, gdi32, hwnd, hdc, tile_dc, width: int, height: int) -> bool:
    """把画布**当前视口**抓到 ``tile_dc`` 的位图里（PrintWindow 优先，BitBlt 兜底）。"""
    printed = False
    try:
        printed = bool(user32.PrintWindow(hwnd, tile_dc, _PW_RENDERFULLCONTENT))
    except Exception:  # pragma: no cover - 旧系统没有这个导出
        printed = False
    if printed:
        return True
    try:
        return bool(gdi32.BitBlt(tile_dc, 0, 0, width, height, hdc, 0, 0, _SRCCOPY))
    except Exception:  # pragma: no cover - 兜底也不通
        return False


def capture_canvas_png(canvas, path, *, box=None,
                       max_pixels: int = MAX_CAPTURE_PIXELS) -> tuple[int, int, int]:
    """把画布上的**整张图**抓成一张 PNG，返回 ``(宽, 高, 抽稀倍数)``。

    为什么不是直接 ``PrintWindow`` 一次：那次抓到的只是**控件客户区**（= 当前
    视口），图比窗口大时导出的 PNG 只有看得见的那一块 —— 用户说「不完整」就是
    这里。现在的做法：

    1. ``box``（默认 = 当前视口，调用方传整张图的外框）算出要出的图尺寸；
    2. 按视口大小 :func:`plan_tiles` 分块，逐块 ``xview_moveto`` / ``yview_moveto``
       滚过去，PrintWindow 抓进**可复用的**小位图；
    3. 每块用 ``BitBlt`` 拼进目标大位图，落点按 ``canvasx(0) / canvasy(0)``
       **实际**算（Tk 会把请求的滚动位置夹在边界内，按请求值拼会错位）；
    4. 抓完把滚动位置**还给用户**；图片超过 :data:`MAX_CAPTURE_PIXELS` 就抽稀；
    5. ``GetDIBits``（``biBitCount=32`` / ``BI_RGB`` / ``biHeight = -h`` 顶向下）
       取 BGRA → RGB → :func:`write_png`。

    失败一律抛 :class:`MapExportError`，消息里写清**是哪一步**失败，并且
    **不留半截文件**：先写到 ``<名字>.<pid>.part``，全部成功后才 ``os.replace``
    成 ``path``（GDI 对象在 :func:`_cleanup_gdi` 里释放，异常路径也走）。

    不做「像素全黑 = 失败」的判断：深色主题的画布本来就接近全黑，
    误报会逼用户去查一个不存在的问题。抓不到就是抓不到（API 报错）。

    代价（说清楚）：抓图期间画布会**快速滚动一遍**（每块一次），抓完自动回到
    原位 —— 这是「不建窗口也能拿到整张图」的唯一办法。
    """
    try:
        hwnd = int(canvas.winfo_id())
    except Exception as exc:
        raise MapExportError(f"拿不到画布窗口句柄：{exc}") from exc
    if not hwnd:
        raise MapExportError("拿不到画布窗口句柄：winfo_id() 返回 0（窗口可能已经销毁）")

    user32, gdi32 = _gdi_libraries()
    target = Path(path)
    # 先抓到临时文件、成功后再原子替换：抓图失败时**path 上原有的文件保持原样**，
    # 既不留半截 PNG，也不会把用户上一次的导出删掉（早期版本在这里踩过坑：
    # 失败路径直接 _remove_partial(path)，结果连已有的好文件一起删了）。
    partial = target.with_name(f"{target.name}.{os.getpid()}.part")
    hdc = dest_dc = dest_bmp = dest_previous = None
    tile_dc = tile_bmp = tile_previous = None
    try:
        rect = _Rect()
        if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
            raise MapExportError(f"GetClientRect 失败（hwnd={hwnd}），读不到画布尺寸")
        view_w = int(rect.right - rect.left)
        view_h = int(rect.bottom - rect.top)
        if view_w < 1 or view_h < 1:
            raise MapExportError(f"画布客户区尺寸非法：{view_w}x{view_h}")

        if box is None:                       # 没给范围就抓「看得见的那一块」
            left, top = _view_origin(canvas)
            box = (left, top, left + view_w, top + view_h)
        box_left, box_top, box_right, box_bottom = (float(value) for value in box)
        out_w = int(math.ceil(box_right - box_left))
        out_h = int(math.ceil(box_bottom - box_top))
        if out_w < 1 or out_h < 1:
            raise MapExportError(
                f"要导出的范围非法：{_num(box_left)},{_num(box_top)} → "
                f"{_num(box_right)},{_num(box_bottom)}")
        if out_w * out_h > MAX_NATIVE_PIXELS:
            raise MapExportError(
                f"关系图太大（{out_w}×{out_h} 像素），先缩小视图或减少词条再导出")
        step = sample_step(out_w, out_h, max_pixels=max_pixels)

        hdc = user32.GetDC(hwnd)
        if not hdc:
            raise MapExportError(f"GetDC 失败（hwnd={hwnd}），拿不到画布的设备上下文")
        dest_dc = gdi32.CreateCompatibleDC(hdc)
        if not dest_dc:
            raise MapExportError("CreateCompatibleDC 失败，无法建立离屏绘图上下文")
        dest_bmp = gdi32.CreateCompatibleBitmap(hdc, out_w, out_h)
        if not dest_bmp:
            raise MapExportError(f"CreateCompatibleBitmap 失败（{out_w}x{out_h}）")
        dest_previous = gdi32.SelectObject(dest_dc, dest_bmp)
        if not dest_previous:
            raise MapExportError("SelectObject 失败，目标位图没能选进离屏上下文")

        tile_dc = gdi32.CreateCompatibleDC(hdc)
        if not tile_dc:
            raise MapExportError("CreateCompatibleDC 失败（分块抓图用）")
        tile_bmp = gdi32.CreateCompatibleBitmap(hdc, view_w, view_h)
        if not tile_bmp:
            raise MapExportError(f"CreateCompatibleBitmap 失败（{view_w}x{view_h}）")
        tile_previous = gdi32.SelectObject(tile_dc, tile_bmp)
        if not tile_previous:
            raise MapExportError("SelectObject 失败，分块位图没能选进离屏上下文")

        saved = _saved_view(canvas)
        painted = 0
        last = None
        try:
            for offset_x, offset_y in plan_tiles(out_w, out_h, view_w, view_h):
                _scroll_to(canvas, box_left + offset_x, box_top + offset_y)
                _settle(canvas)
                origin_x, origin_y = _view_origin(canvas)
                key = (int(round(origin_x)), int(round(origin_y)))
                if key == last:            # Tk 把滚动夹在边界内：这一块和上一块重合
                    continue
                last = key
                if not _grab_tile(user32, gdi32, hwnd, hdc, tile_dc, view_w, view_h):
                    raise MapExportError(
                        f"抓图失败：PrintWindow 与 BitBlt 都没能取到画布像素（hwnd={hwnd}）")
                if not gdi32.BitBlt(dest_dc, key[0] - int(round(box_left)),
                                    key[1] - int(round(box_top)), view_w, view_h,
                                    tile_dc, 0, 0, _SRCCOPY):
                    raise MapExportError(
                        f"拼接画布图片失败（第 {offset_x},{offset_y} 块）")
                painted += 1
        finally:
            _restore_view(canvas, saved)
            _settle(canvas)
        if not painted:
            raise MapExportError("抓图失败：一块像素都没取到（窗口可能已经销毁）")

        stride = out_w * 4
        info = _BitmapInfo()
        info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
        info.bmiHeader.biWidth = out_w
        info.bmiHeader.biHeight = -out_h        # 负数 = 自顶向下，省一次翻转
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0        # BI_RGB
        buffer = ctypes.create_string_buffer(stride * out_h)
        got = gdi32.GetDIBits(dest_dc, dest_bmp, 0, out_h, buffer, ctypes.byref(info),
                              _DIB_RGB_COLORS)
        if int(got) != out_h:
            raise MapExportError(
                f"GetDIBits 只取到 {int(got)} 行（期望 {out_h} 行），像素不完整")

        png_w = (out_w + step - 1) // step
        png_h = (out_h + step - 1) // step
        write_png(partial, png_w, png_h, bgra_to_rgb(buffer.raw, out_w, out_h, step=step))
        os.replace(partial, target)
        return (out_w, out_h, step)
    except MapExportError:
        raise
    except OSError as exc:
        raise MapExportError(f"写入画布图片失败：{exc}") from exc
    except Exception as exc:  # pragma: no cover - ctypes / 其它意外
        raise MapExportError(f"抓取画布图片时出错：{exc}") from exc
    finally:
        _cleanup_gdi(user32, gdi32, hwnd, hdc,
                     ((tile_dc, tile_bmp, tile_previous), (dest_dc, dest_bmp, dest_previous)))
        _remove_partial(partial)


# ---------------------------------------------------------------- 一次导出
def default_directory() -> Path:
    """默认导出目录 = :func:`app.paths.exports_dir`（与词条导出同一个文件夹）。"""
    return paths.exports_dir()


def unique_stem(directory, base: str) -> str:
    """两个文件（SVG / PNG）共用的**可用 stem**，重名整体退避。

    为什么不是逐个后缀 :func:`app.export_service.unique_path`：那样两次判定各看
    各的，会出现 ``x.svg`` / ``x-2.png`` 这种**不同名**的两件套，用户根本认不出
    是一次的产物。这里探测「``base`` 的两个后缀是否都空着」，占用就整体退到
    ``base-2``、``base-3``…（同一个数字，两个文件同名）。
    """
    target = Path(directory)
    suffixes = (".svg", ".png")
    for index in range(1, 1000):
        stem = base if index == 1 else f"{base}-{index}"
        if not any((target / f"{stem}{suffix}").exists() for suffix in suffixes):
            return stem
    return f"{base}-{int(datetime.now().timestamp())}"


def _stamp_text(now, *, footer: bool = False) -> str:
    """时间戳：文件名用 ``20260101-120000``，页脚用给人看的形式。"""
    moment = now or datetime.now()
    return moment.strftime("%Y-%m-%d %H:%M") if footer else moment.strftime("%Y%m%d-%H%M%S")


def stem_label(topic_name: str) -> str:
    """文件名里的主题标签：空主题写 ``全部``，其余交给 ``export_service.safe_stem``。

    不能直接写 ``safe_stem(topic_name) or FALLBACK_STEM``：``safe_stem`` 对空串返回
    的是它自己的兜底值 ``"导出"``（那是词条导出的口径），于是「全部主题」会变成
    「导出」这种看不懂的名字。这里先判空，再让 safe_stem 管非法字符与截断。
    """
    text = str(topic_name or "").strip()
    if not text:
        return FALLBACK_STEM
    return export_service.safe_stem(text) or FALLBACK_STEM


def export_map(*, layout, style=None, canvas=None, topic_name="", model_config="",
               template_name="", directory=None, now=None) -> MapExportResult:
    """把一张参考关系图导出成**图片**：同名的 ``.svg``（主） + ``.png``（附赠）。

    * 文件名：``探索词典-关系图-<safe_stem(主题) 或 全部>-YYYYmmdd-HHMMSS``；
    * SVG 一定写出来（写不出来就是失败，抛 :class:`MapExportError`）；
    * ``canvas is not None`` 时再抓一整张 PNG：抓不到只写
      :attr:`MapExportResult.png_error`，**绝不影响** SVG —— 用户要的是图，
      矢量图还在，位图是附赠；
    * ``template_name``（G1）：这次用的布局骨架（「自动」/「思维导图」…），
      只写进页脚 —— 图换了个摆法，拿到图的人得知道它是哪一种；
    * 目录不存在就建；重名不覆盖（见 :func:`unique_stem`）。
    """
    if layout is None:
        raise MapExportError("还没有关系图可以导出：先在阅读页解释并记录几个词，等图画出来再导出")
    st = _style(style)
    box = content_box(layout, style=st)     # 空图在这里就抛，别写出一个空文件
    out_w = int(math.ceil(box[2] - box[0]))
    out_h = int(math.ceil(box[3] - box[1]))

    stamp = _stamp_text(now)
    target = Path(directory) if directory else default_directory()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MapExportError(f"建不了导出目录（{target}）：{exc}") from exc
    stem = unique_stem(target, f"{FILE_PREFIX}-{SCOPE_LABEL}-{stem_label(topic_name)}-{stamp}")
    svg_path = target / f"{stem}.svg"
    png_path = target / f"{stem}.png"

    meta = {"topic_name": str(topic_name or "").strip(),
            "model_config": str(model_config or "").strip(),
            "template_name": str(template_name or "").strip(),
            "exported_at": _stamp_text(now, footer=True)}
    write_svg(svg_path, svg_document(layout=layout, style=st, box=box, meta=meta))

    result_png: Path | None = None
    png_error = ""
    step = 1
    if canvas is not None:
        try:
            _cap_w, _cap_h, step = capture_canvas_png(canvas, png_path, box=box)
            result_png = png_path
        except MapExportError as exc:
            png_error = str(exc)
            log.warning("关系图 PNG 导出失败（SVG 已写出）：%s", png_error)
        except Exception as exc:  # pragma: no cover - 抓图不该抛出别的东西
            png_error = f"抓取画布图片时出错：{exc}"
            log.exception("关系图 PNG 导出意外失败（SVG 已写出）")

    nodes, edges, _manual = layout_counts(layout)
    log.info("导出关系图 %d 个词 / %d 条关系 → %s", nodes, edges, svg_path.name)
    return MapExportResult(svg_path=svg_path, png_path=result_png, node_count=nodes,
                           count=edges, width=out_w, height=out_h, step=step,
                           png_error=png_error)
