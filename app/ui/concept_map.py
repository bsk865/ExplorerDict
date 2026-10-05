"""概念关系图：**AI 生成的参考关系图**（每条关系都带依据与证据片段，点线可查）。

用户最新要求（优先于 SPEC.md 旧 FR-11）
--------------------------------------
* 图上**没有**内部编号（``#id``）与长说明：左边只挑主题，右边是图，底部只有一个
  「生成参考关系 / 重新生成」入口与必要状态。**人工关系是例外**：按改进清单 C5，
  用户可以手动新增 / 删除 / 改类型（人工关系用加粗实线 + 「人工」前缀与 AI 关系
  区分，重新生成**绝不覆盖**它）—— 这是后来明确要求的，覆盖早先「图上不放录入控件」；
* 「打开图」本身就是用户的明确动作：命中按 ``(主题, 内容指纹)`` 的有效缓存就
  **立刻**显示；没有缓存时**先画出该主题的词节点**，再异步生成参考关系；
* AI 关系必须有依据：端点只能是本次给出的词、类型只能是
  包含 / 属于 / 依赖 / 用途 / 因果 / 对照，并带简短依据 + 逐字证据片段；
  端点越界、自环、重复、层级互相包含、包含·属于成环、证据与材料不符的一律筛除，
  并且还要过第二次**独立核对**（只有明确 supported 才画）；依据不足就允许空关系
  （**绝不**强行连线、**绝不**默认「相关」）；
* 布局**真正按关系结构**：包含 / 属于形成层级分组，依赖 / 因果分前后层，
  用途 / 对照画成带方向与标签的跨边；互相依赖 / 互为因果（强连通分量）同层分组并
  画成反馈弧线 —— 既不把反馈当错误删掉，也绝不用它伪造层级；
  成环的不可信**层级**边被排除（不计入层级）；孤立词单独成行可见。
  **禁止**把全部节点摆成一个固定圆环，也**禁止**按词数随机摆放；
* 主题 → 顶层词的细线是**分类结构**，明确标注为非 AI 判断，绝不混充语义关系；
* 「AI 参考」只在窗口**底部状态行**里说明（画布上不再压图例 / 提示文字：
  用户要求删掉左上角那两行）；操作提示（右键平移 / 滚轮缩放 / 点击连线看依据）
  三行排在画布**右下角**的浮层上；点击任意连线在底部看到该关系的依据与证据片段。

窗口层：与主界面 / 设置 / 手动录入共用 :class:`app.ui.widgets.BorderlessChrome`
（自绘标题带、可拖动、可缩放、Esc / Alt+F4 / × 都只关本窗）。
"""
from __future__ import annotations

import math
import os
import tkinter as tk
from tkinter import messagebox
from dataclasses import dataclass, replace

from ..logging_setup import get_logger
from ..map_service import (DIRECTIONAL_TYPES, HIERARCHY_TYPES, REL_TYPES,
                           SYMMETRIC_TYPES, is_manual, manual_relations,
                           normalize_material)
from . import map_templates, theme, widgets

log = get_logger("conceptmap")

#: 画布还没被 Tk 映射时（首次 ``<Configure>`` 之前 ``winfo_width`` = 1）的兜底尺寸：
#: 直接用真实尺寸会把整张图挤成 1x1，用户看到的就是「打开是空的」。
FALLBACK_W = 520
FALLBACK_H = 320
#: 缩放范围与步长（滚轮 / Ctrl+滚轮）
ZOOM_MIN = 0.6
ZOOM_MAX = 2.0
ZOOM_STEP = 1.1
#: 首开自适应缩放的**下限**：内容比视口大得多时可以缩小（保证一开就能看全），
#: 但不缩到读不清字；用户自己滚过轮之后**一次都不再自动改**（见 ``_auto_fit``）。
AUTO_FIT_MIN = 0.75
#: 「这些线是模型猜的」短标注（用户要求：图里要能看出这是 AI 参考）。
#: 它现在只出现在窗口**底部状态行**里 —— 画布上原来那两行图例（图例 + 操作提示）
#: 压在图左上角、挡住内容，用户明确要求删掉，操作提示改放画布**右下角**。
MAP_TAG = "AI 参考"
#: 画布右下角的操作提示（**一行一条**，行与行之间留空档）。用户原来给的三行
#: （右键平移 / 滚轮缩放 / 点连线看依据）仍在，前三行改成把 F1–F3 与本轮的新
#: 操作也写进去 —— 拖拽连线、拖动卡片、左键拖空白平移，全靠手感发现不了，
#: 必须写在提示里（用户口径：「只需要简单说明和操作提示」）。
#: **左键 = 拖；Alt + 左键 = 建关系**（用户口径 2026-10-04）：卡片上不再放小圆点
#: 状的连接点，免得抓卡片时被它抢走手势；左键拖**空白处**也参与平移
#: （ProjectGraph 式：抓边框就能挪整张图，按住 Alt 才切到建关系）。
HINT_LINES = ("按住 Alt + 左键从一张卡拖到另一张 = 建关系 · 左键拖空白处 = 平移整张图",
              "左键拖卡片 = 摆位置（拖过就固定）· 鼠标滚轮 = 缩放 · 点连线 = 看依据",
              "双击连线 = 改类型 / 删掉 · 双击词卡 = 打开词条 · 鼠标右键拖动 = 平移")
#: 提示行之间的空档（设备像素，随 DPI 换算）
HINT_ROW_GAP = 6
#: 提示块离画布右下角的距离（设备像素）
HINT_INSET = 10
#: 展开后多久自动淡出折叠（毫秒）——用户口径：「用户刚刚打开时展开进行提示，
#: 过 20 秒之后淡出折叠」。**这是提示的存在时长，不是可配项**：不写进 settings。
HINT_EXPAND_MS = 20000
#: 淡出分成几步、每步多少毫秒（``steps × step_ms`` = 淡出总时长）。
#: tk 没有真正的透明度（``-alpha`` 是整窗属性，套在 ``Toplevel`` 上会把整个导图窗
#: 一起变半透明），所以「淡出」只能这样模拟：把文字色从 ``TEXT_MUTED`` 一格一格
#: 混向画布底色，最后收成一行小字。
HINT_FADE_STEPS = 6
HINT_FADE_STEP_MS = 90
#: 折叠后那一行的文案（可点，点开重新展开）——「操作说明」与工具条上那个按钮同名，
#: 用户一眼知道点它会看到什么。
HINT_COLLAPSED_TEXT = "操作说明"
#: 点选连线的命中半径（逻辑像素）
HIT_TOL = 10
#: 点选词卡的**外扩**半径（逻辑像素）：贴着卡片边缘点一下也算点中这个词
NODE_HIT_PAD = 2
#: 画布上「一张词卡」的 **Tk tag** 前缀：这张卡的框与文字共用同一个 tag（``node-24``）。
#: 拖动靠它认图元（``canvas.find_withtag``）与搬图元（``canvas.move``）。
#:
#: ★ 为什么必须是**真 Tk 的 tag**：旧实现拿 ``self.canvas.item_options`` 里记的
#: ``_kind`` 去找图元 —— 那个字典**只有测试替身有**，真 ``tk.Canvas`` 上根本没有，
#: 于是 ``getattr(..., {})`` 静默返回空字典、``_node_items()`` 永远返回 ``[]``、
#: ``_move_node_items()`` 一路 early-return：**实机上左键压根拖不动卡片**，
#: 只有松手那一下 ``pin_node()`` 把卡片「啪」地瞬移到鼠标处（用户 2026-10-05 报的
#: 「无法拖动 / 拖动不准确」）。假 Tk 因为自带 ``item_options`` 一直是绿的。
NODE_TAG_PREFIX = "node-"
#: 手势期间被推迟的重画：隔多久回头看一眼「手离开了没」（毫秒）。
DRAW_DEFER_MS = 120


def node_tag(entry_id: int) -> str:
    """一张词卡在画布上的 tag 名（框与文字共用）。"""
    return f"{NODE_TAG_PREFIX}{int(entry_id)}"
#: 「按住 Alt」在 Tk ``event.state`` 里的位（Windows 真鼠标实测：按住 Alt 时
#: ``state = 0x20008``；不按任何键时 ``state = 0x8``）。
#:
#: ★★ ``0x8`` 那一位是**普通左键本身**贡献的，而它正好就是 Tk 的 ``Mod1Mask``。
#: 曾经为了兜底「Alt 不进 state」给 ``<Mod1-Button-1>`` 多绑了一条，结果那条
#: 绑定把**每一次普通左键**都抢走了（Tk 只触发最具体的那一条），左键于是永远
#: 在连线、永远拖不动卡片。现在只认这个位，绝不再绑 ``<Mod1-*``。
ALT_MASK = 0x00020000
#: **手势认定阈值**（屏幕像素）：左键按下后，鼠标离开按下点不足这个距离就**当单击**处理 ——
#: 不固定卡片、也不建关系。理由：手抖一两像素是常事，若按下就算拖动，一次普通点击会把
#: 卡片钉死在原地（还会弹一句「已固定…」），Alt + 单击也会变成「连线取消：起点和终点是
#: 同一个词」。用户口径（2026-10-04）：**「左键是拖动，左键 + Alt 才是建立关系，
#: 请你做好操作管理」** —— 这条阈值就是「操作管理」的第一层保险。
DRAG_SLOP = 4.0
#: 画布上的**全部**操作，一处定义、多处引用（提示行、操作说明窗口、测试、文档都从这里来）。
#: 顺序 = 用户最可能先摸到的顺序。改手势只改这一张表，别的地方自动跟上（防止
#: 「提示里写着拖柄、代码里早删了」这类漂移）。
INTERACTIONS: tuple[tuple[str, str], ...] = (
    ("左键单击词卡", "选中这个词：右栏写它的关系与孤立原因"),
    ("左键单击连线", "选中这条线：右栏写它的依据与证据片段"),
    ("左键单击空白", "取消选中"),
    ("左键拖动词卡", "摆位置（拖过就固定；「恢复自动布局」一键放回去）"),
    ("左键拖动空白处", "平移整张图（project-graph 式：抓空白就能挪；右键拖动同样平移）"),
    ("Alt + 左键：从一张词卡拖到另一张", "建立一条人工关系（松开在空白处 = 取消）"),
    ("左键双击词卡", "回主界面打开这条词条"),
    ("左键双击连线", "人工关系 = 改类型 / 改依据 / 删掉；AI 关系 = 标为不对（以后不再画）"),
    ("左键双击「孤立词…」那一行", "展开 / 收起孤立词诊断（展开后双击某行跳到那个词）"),
    ("鼠标右键拖动 / 中键拖动", "平移画布"),
    ("鼠标滚轮", "以指针为锚缩放"),
)
#: 操作提示里那句「卡片上没有连接点」的说明（用户要求删掉柄，这里把原因讲清楚）
NO_HANDLE_NOTE = "卡片上没有连接点：抓卡片永远是「摆位置」，按住 Alt 才是建关系。"
#: 滚动范围**四边**的留白（视口尺寸的倍数）：左边 / 上边从一个视口的**负坐标**
#: 开始，右边 / 下边到「内容 + 一个视口」。留白必须按视口给 —— 只留几十像素时，
#: 内容比视口小的小图只能往左上挪那么一点点（往右下根本挪不动），右键平移形同
#: 虚设。视图原点 (0, 0) 因此落在滚动范围**内部**：Tk 新建画布的视图原点就是 0，
#: 一打开看到的正是世界原点（见 ``_region_box`` / ``_view_left`` / ``_move_view_to``）。
PAN_MARGIN_VIEWPORTS = 1.0
#: 依据区里证据片段的显示上限（超出只截断显示，库里与缓存里的原文一字不动）
EVIDENCE_SHOWN = 80
#: 显示被截断时补的省略号：带它的串**一律不算**「逐字来自材料」
#: （显示截断过的片段绝不能被说成原文引文）
EVIDENCE_ELLIPSIS = "…"
#: 证据片段的**来源**短标注：这两句只说明「模型抄的是哪一份材料」，
#: **不是**「已核对过的原文引文」——材料本身（上下文 / 释义）可能已被截断。
BASIS_FROM_CONTEXT = "来源：阅读页上下文"
BASIS_FROM_EXPLANATION = "来源：本工具已存释义"
BASIS_FROM_UNKNOWN = "来源：端点材料"
#: 孤立词那一行的短标题（**必须说准**：不是「AI 没给关系」，而是「模型给的候选
#: 一条都没通过独立核对」—— 点这一行能看到每条候选被判定为不成立的理由）。
#: 本轮（用户口径：功能要内化进实际操作、不要一排单独的功能按钮）：工具条上原来
#: 那个「孤立词诊断」按钮删掉了，改成**点画布上这一行**展开 / 收起诊断 ——
#: 所以文案必须把「点这里」说出来（「点词看原因」会被读成点**词卡**）。
ISOLATED_TEXT = "孤立词（候选关系都没通过核对 —— 点这一行看原因）"
#: 孤立词说明里的一句话（点词之后的依据区抬头）
ISOLATED_WHY = "这个词没有画出关系：模型提过下面这些候选，但没有一条通过独立核对"
#: 孤立词**没有留下候选记录**时的说明（说准但不编：模型可能没提，也可能是被本地
#: 校验挡在核对之前 —— 这两种情况这张图里确实分不出来）
ISOLATED_NONE = ("没有这个词的候选记录：模型可能没为它提候选，也可能提的候选在证据 / "
                 "格式校验那一关就被筛掉了（那些不会进入核对）")
#: 整张图**一条判定记录都没有**时的说明（v6 之前的旧缓存，或这次生成时模型一条候选都没提）
ISOLATED_UNKNOWN = ("这张图没有留下判定记录（旧缓存，或这次生成时模型一条候选都没提）："
                    "点「重新生成」按当前设置再问一次，就能逐条看到候选与核对结果")
#: 核对判定的中文短标注（点词看原因时逐条列出来）
VERDICT_LABELS = {
    "supported": "核对支持",
    "uncertain": "核对不确定（材料不足）",
    "contradicted": "核对认为不成立",
    "": "核对没有给出判定",
}
#: 布局侧的**短间距**（设备像素）：连线避障 / 关系标签让位都按它留净空 ——
#: 「小间距」不是零：连线贴着别的节点边框走一样会被读成压在上面。
LAYOUT_EPS = 0.5
#: 折线绕行的**候选点外扩**（设备像素）：候选拐点必须严格落在碰撞障碍之外，
#: 否则它自己就贴着碰撞边界 —— ``route_is_clear`` 连碰边框都算失败，那样生成的
#: 角点 / 投影一个都连不出去。碰撞判据仍用传进来的障碍（净空不降）。
CANDIDATE_OUTSET = LAYOUT_EPS
#: 关系短标签（类型名，最多 3 个字）的让位搜索参数：沿路径的取样比例 ×
#: 垂直方向的候选距离（× 标签基准净空；**负号 = 连线的另一侧**）。
#: 顺序就是优先级：先「贴着线、靠中间」，再往另一侧 / 更远找空位。
LABEL_SLIDE_STEPS = (0.0, 0.08, -0.08, 0.16, -0.16, 0.24, -0.24)
LABEL_OFFSET_STEPS = (1.0, -1.0, 1.8, -1.8, 2.6, -2.6)
#: 关系短标签自己的字号（逻辑像素）：比卡片文字（9）小一档
LABEL_FONT_SIZE = 7
#: 折线明暗：采样段数（每条边一致，真实画布与预览都是同一串点）
CURVE_SEGMENTS = 12
#: 跨层**正交三段线**（见 :func:`_orthogonal_routes`）的三个规则常量：
#: 端口离卡片角的最小内缩 = ``min(pad_x, 卡片宽 × ORTHO_PORT_INSET)``；同一条层间
#: 空档里相邻车道的**最小**间距；车道距上下两行的净空（在 ``route_margin`` 之上
#: 再加这 1px，正好躲开按 ``route_margin`` 外扩的节点矩形）。
ORTHO_PORT_INSET = 0.22
ORTHO_LANE_MIN = 2.5
ORTHO_LANE_CLEAR = 1.0
#: 连线的**统一颜色与线宽**（用户口径：以前层级 / 方向 / 交叉 / 人工四种线各自
#: 一个灰度、人工还更粗，图上一眼看去「粗细颜色都不同」，很乱）——
#: 现在所有连线只有**一种颜色、一种线宽**，类型只靠**虚线**与线上的短标签区分。
EDGE_FILL = theme.TEXT_FAINT
#: 连线线宽（逻辑像素，随 DPI 换算）
EDGE_WIDTH = 1
#: 连线标签的文字颜色（比线略深一档，压在浅底上读得清；**没有底色块**）
EDGE_LABEL_FILL = theme.TEXT_MUTED
#: 4 种线型（单色主题下只靠线型 / 标签区分）：
#: ``feedback`` = 互相依赖 / 互为因果的**反馈弧**（同层两点之间），绝不画成前后层级。
#: ``manual``（人工添加 / 修正的关系）沿用同一套画法：图上与 AI 边**同款**，
#: 靠依据区与双击对话框区分（用户要求：不要「人工·」这种线标注）。
EDGE_STYLES = {
    "hierarchy": {"fill": EDGE_FILL, "dash": ()},
    "direction": {"fill": EDGE_FILL, "dash": ()},
    "cross": {"fill": EDGE_FILL, "dash": (theme.px(5), theme.px(3))},
    "feedback": {"fill": EDGE_FILL, "dash": (theme.px(2), theme.px(2))},
    "manual": {"fill": EDGE_FILL, "dash": ()},
}

#: 人工关系在连线标签与依据区里的前缀 / 说明（C6：机器推导与用户判断不能混）。
MANUAL_LABEL_PREFIX = "人工·"
MANUAL_KIND_NOTE = "人工添加的关系（你自己的判断，重新生成不会覆盖）"


def unique_labels(names: list[str]) -> list[str]:
    """把可能重名的词条名变成**唯一**显示名（纯函数，绝不出现 ``#id``）。

    ``["Transformer", "Transformer"]`` → ``["Transformer", "Transformer（2）"]``。
    空名回退成「未命名」，仍然保证互不相同（``未命名（2）``）。
    """
    seen: dict[str, int] = {}
    out: list[str] = []
    for raw in names:
        base = str(raw or "").strip() or "未命名"
        count = seen.get(base, 0) + 1
        seen[base] = count
        out.append(base if count == 1 else f"{base}（{count}）")
    return out


# ===================================================================== 布局
def char_units(text: str) -> float:
    """估算显示宽度（汉字 / 全角 = 1.0 个字宽，半角 = 0.55；纯函数）。"""
    return sum(1.0 if ord(ch) > 0x2E7F else 0.55 for ch in str(text or ""))


def text_px(text: str, em: float) -> float:
    return char_units(text) * float(em)


def wrap_label(label: str, max_px: float, em: float) -> list[str]:
    """按估算宽度换行（**不截断**）：词条文字要能读全，不是砍到 6 个字。"""
    text = str(label or "").strip() or "—"
    limit = max(float(em) * 2.0, float(max_px))
    lines: list[str] = []
    for raw_line in text.splitlines() or [text]:
        tokens = raw_line.split(" ")
        current = ""
        for token in tokens:
            candidate = token if not current else f"{current} {token}"
            if text_px(candidate, em) <= limit:
                current = candidate
                continue
            if current:
                lines.append(current)
                current = ""
            # 单个词本身就超宽：按字符硬拆（中文长词 / 长英文术语）
            piece = ""
            for ch in token:
                if piece and text_px(piece + ch, em) > limit:
                    lines.append(piece)
                    piece = ""
                piece += ch
            current = piece
        if current:
            lines.append(current)
    return lines or ["—"]


def box_size(lines, m: "MapMetrics") -> tuple[float, float]:
    width = 2.0 * m.pad_x + max((text_px(line, m.em) for line in lines), default=0.0)
    height = 2.0 * m.pad_y + max(1, len(lines)) * m.line_h
    return (max(m.min_w, width), max(m.min_h, height))


def evidence_basis(evidence, src_node, dst_node, m: "MapMetrics") -> tuple[str, bool]:
    """证据片段来自哪一份材料（纯函数，返回 ``(短标注, 是否逐字来自材料)``）。

    接口契约（见 :mod:`app.api_client` 的提示词）是「evidence 必须逐字来自本次给出的
    上下文或释义」，因此这里只做**来源区分**，不冒充权威：

    * 命中阅读页上下文 → :data:`BASIS_FROM_CONTEXT`（那是取词时读到的页面文字）；
    * 只命中本工具已存释义 → :data:`BASIS_FROM_EXPLANATION`（那是本工具 / 模型写的，
      **不是**原文）；
    * 都不命中（例如材料在送模型前就被截断过）→ :data:`BASIS_FROM_UNKNOWN`，
      绝不硬说成原文。

    第二个返回值 = 「这串字**逐字**出现在端点材料里」：显示时按
    :data:`EVIDENCE_SHOWN` 截断过的串（带 :data:`EVIDENCE_ELLIPSIS`）一定返回
    ``False`` —— 规范化会把标点抹掉，只看子串匹配的话截断串会蒙混过关。
    """
    text = str(evidence or "").strip()
    probe = normalize_material(text)
    if not text:
        return BASIS_FROM_UNKNOWN, False
    truncated = text.endswith(EVIDENCE_ELLIPSIS)
    candidates = []
    for node in (src_node, dst_node):
        if node is None:
            continue
        candidates.append(normalize_material(getattr(node, "context", "") or ""))
        candidates.append(normalize_material(getattr(node, "explanation", "") or ""))
    contexts = tuple(normalize_material(getattr(node, "context", "") or "")
                     for node in (src_node, dst_node) if node is not None)
    explanations = tuple(normalize_material(getattr(node, "explanation", "") or "")
                         for node in (src_node, dst_node) if node is not None)
    if probe and any(probe in item for item in contexts if item):
        label = BASIS_FROM_CONTEXT
    elif probe and any(probe in item for item in explanations if item):
        label = BASIS_FROM_EXPLANATION
    else:
        label = BASIS_FROM_UNKNOWN
    verbatim = bool(probe) and not truncated and any(
        probe in item for item in candidates if item)
    return label, verbatim


def box_edge_point(cx: float, cy: float, w: float, h: float,
                   tx: float, ty: float) -> tuple[float, float]:
    """从矩形中心朝目标点射出，与矩形**边界**的交点（边端落在节点边界上）。"""
    dx, dy = float(tx) - float(cx), float(ty) - float(cy)
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return (float(cx), float(cy))
    hw, hh = max(1.0, float(w)) / 2.0, max(1.0, float(h)) / 2.0
    scale = []
    if abs(dx) > 1e-9:
        scale.append(hw / abs(dx))
    if abs(dy) > 1e-9:
        scale.append(hh / abs(dy))
    k = min(scale)
    return (float(cx) + dx * k, float(cy) + dy * k)


def segment_distance(px: float, py: float, x1: float, y1: float,
                     x2: float, y2: float) -> float:
    """点到线段的距离（点选连线用；纯函数）。"""
    vx, vy = float(x2) - float(x1), float(y2) - float(y1)
    length_sq = vx * vx + vy * vy
    if length_sq <= 1e-12:
        return math.hypot(float(px) - float(x1), float(py) - float(y1))
    t = ((float(px) - float(x1)) * vx + (float(py) - float(y1)) * vy) / length_sq
    t = max(0.0, min(1.0, t))
    return math.hypot(float(px) - (float(x1) + t * vx), float(py) - (float(y1) + t * vy))


@dataclass(frozen=True)
class MapMetrics:
    """布局度量（全部是设备像素）：实机用 :func:`metrics_for`，预览共用同一份。

    留白口径（用户报「太拥挤了」之后统一上调，**改布局时必须保持这套比例**）：

    * 卡片内边距 ``pad_x/pad_y`` 与最小高度 ``min_h`` 一起放大，让两三行文字
      不再贴着边框（配 ``line_h`` 的真实行高）；
    * 卡片**之间**的列间距 ``h_gap`` 明显大于卡片内边距、**层与层之间**的
      ``v_gap`` 又明显大于 ``h_gap``（层间空档同时是正交车道的走线带）；
    * 内容四周 ``pad``、主题母线空档 ``topic_gap``、分组底板 ``group_pad``
      同步放大，整张图四周都有呼吸区。
    """

    em: float = 12.0
    pad_x: float = 14.0
    pad_y: float = 10.0
    line_h: float = 15.0
    min_w: float = 72.0
    max_w: float = 182.0
    min_h: float = 42.0
    h_gap: float = 32.0
    v_gap: float = 56.0
    pad: float = 26.0
    topic_em: float = 13.0
    topic_pad_x: float = 20.0
    topic_pad_y: float = 12.0
    group_pad: float = 12.0
    cross_bow: float = 34.0
    topic_gap: float = 66.0
    passes: int = 4
    #: 连线避障净空（关系线离非端点节点的最小距离；标签命中检查外加
    #: :data:`LABEL_HIT_PAD` 的余量 —— 几何检查本身只用**画笔真实宽度**的裕度）
    route_margin: float = 6.0
    #: 关系标签的净空（标签边框到节点 / 其它标签的最小空档）
    label_gap: float = 3.0
    #: 避障折线搜索上限（保证纯函数一定收敛，不随节点数爆炸）
    route_obstacles: int = 10
    route_states: int = 8000

    def scaled(self, factor: float) -> "MapMetrics":
        f = max(0.4, min(2.5, float(factor or 1.0)))
        if abs(f - 1.0) < 1e-9:
            return self
        return replace(
            self,
            em=self.em * f, pad_x=self.pad_x * f, pad_y=self.pad_y * f,
            line_h=self.line_h * f, min_w=self.min_w * f, max_w=self.max_w * f,
            min_h=self.min_h * f, h_gap=self.h_gap * f, v_gap=self.v_gap * f,
            pad=self.pad * f, topic_em=self.topic_em * f,
            topic_pad_x=self.topic_pad_x * f, topic_pad_y=self.topic_pad_y * f,
            group_pad=self.group_pad * f, cross_bow=self.cross_bow * f,
            topic_gap=self.topic_gap * f,
            route_margin=self.route_margin * f, label_gap=self.label_gap * f,
        )


def metrics_for(zoom: float = 1.0) -> MapMetrics:
    """按当前 DPI 与缩放构造布局度量（文档化：只在这里做 DPI 换算一次）。

    ``line_h`` 用 :func:`app.ui.widgets.line_height` 的**真实行高**（而不是
    ``字号 × 1.25`` 的估算）：节点文字是一整块多行 Text（``"\\n".join(lines)``），
    Tk 按字体 ``linespace`` 排行，比估算行距高得多（实测 12px 字号要 19px 行距），
    行高估小就会把两行词语上下挤出卡片边框（用户报的「文字越出标签栏边界」）。
    """
    base = MapMetrics(
        em=abs(theme.device_px(9)) * 1.05,
        pad_x=theme.px(14), pad_y=theme.px(10), line_h=widgets.line_height(9),
        min_w=theme.px(72), max_w=theme.px(182), min_h=theme.px(42),
        h_gap=theme.px(32), v_gap=theme.px(56), pad=theme.px(26),
        topic_em=abs(theme.device_px(10)) * 1.05, topic_pad_x=theme.px(20),
        topic_pad_y=theme.px(12), group_pad=theme.px(12), cross_bow=theme.px(34),
        topic_gap=theme.px(66),
    )
    return base.scaled(zoom)


@dataclass(frozen=True)
class LayoutNode:
    """画布上的一个节点（词条；``entry_id == 0`` 是中心主题）。

    ``level`` 是关系分层给的层号（**可能不连续**，例如某层没有词）；
    ``row`` 是**实际排在第几行**（自上而下从 0 数，孤立词网格接在最后）。
    布线要看「谁在谁上面一行」时必须用 ``row``：层号跳号时 ``level + 1`` 并不
    指向下面那一行，而 ``row + 1`` 一定指。
    """

    entry_id: int
    label: str
    lines: tuple[str, ...]
    x: float
    y: float
    w: float
    h: float
    level: int = 0
    isolated: bool = False
    row: int = 0


@dataclass(frozen=True)
class LayoutEdge:
    """一条**已通过证据校验**的 AI 参考关系（带折线与标签位置）。

    ``points`` 是**真实画出去**的那串点（弧线在这里就已经采样成折线）：真实画布与
    离线预览画同一串点，不会一条是平滑曲线、另一条是折线。
    ``symmetric=True`` 表示这条关系对称（对照）：两端都不画箭头（无向线），
    方向关系仍然只在终点画箭头。
    """

    rel: object
    kind: str                      # hierarchy / direction / cross / feedback
    points: tuple[tuple[float, float], ...]
    label: str
    label_pos: tuple[float, float]
    symmetric: bool = False

    def segments(self):
        for first, second in zip(self.points, self.points[1:]):
            yield (first[0], first[1], second[0], second[1])


@dataclass(frozen=True)
class LayoutGroup:
    """包含 / 属于形成的层级分组（画在节点后面的浅色底板）。"""

    root_id: int
    members: tuple[int, ...]
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(frozen=True)
class MapLayout:
    """一次布局的完整结果（纯数据；绘制与点选都在 UI 层）。"""

    width: int
    height: int
    topic: LayoutNode | None = None
    nodes: tuple[LayoutNode, ...] = ()
    edges: tuple[LayoutEdge, ...] = ()
    groups: tuple[LayoutGroup, ...] = ()
    topic_links: tuple[tuple[tuple[float, float], ...], ...] = ()
    content_w: float = 0.0
    content_h: float = 0.0
    isolated_label_pos: tuple[float, float] | None = None
    #: 这一张图实际用的**布局骨架**（G1）：``""`` / ``"auto"`` = 默认的分层布局。
    #: 导出页脚要写它，窗口状态行也要写它（用户切了模板总得看见自己切成了什么）。
    template: str = ""
    #: 模板没摆成时的说明（例如「这张图不太适合鱼骨图…」）：如实说，不硬套。
    template_note: str = ""

    def find(self, entry_id: int) -> LayoutNode | None:
        for node in self.nodes:
            if int(node.entry_id) == int(entry_id):
                return node
        return None

    def edge_at(self, x: float, y: float, *, tol: float) -> LayoutEdge | None:
        """点选：返回距离指针最近的连线（阈值内），支持折线。"""
        best, best_distance = None, float(tol)
        for edge in self.edges:
            distance = min(segment_distance(x, y, *seg) for seg in edge.segments())
            if distance <= best_distance:
                best, best_distance = edge, distance
        return best

    def node_at(self, x: float, y: float, *, pad: float = 0.0) -> LayoutNode | None:
        """点选：指针落在哪个词卡上（含中心词；外扩 ``pad`` 便于点中）。

        词卡比连线「粗」，所以点击判定**先问节点、再问连线** —— 否则一条贴着
        卡片边缘走的折线会把点词卡的手势抢走。同一位置有多个命中时取面积最小
        （最贴合）的那个，保证结果与遍历顺序无关。
        """
        best, best_area = None, None
        for node in ([self.topic] if self.topic is not None else []) + list(self.nodes):
            x0, y0, x1, y1 = node_box(node, pad)
            if x0 <= float(x) <= x1 and y0 <= float(y) <= y1:
                area = max(0.0, node.w) * max(0.0, node.h)
                if best is None or area < best_area:
                    best, best_area = node, area
        return best


def edge_kind(rel_type: str) -> str:
    """关系类型 → 线型分类（层级 / 方向 / 跨边）。"""
    if rel_type in HIERARCHY_TYPES:
        return "hierarchy"
    if rel_type in DIRECTIONAL_TYPES:
        return "direction"
    return "cross"


def boundary_point(node: LayoutNode, toward: tuple[float, float]) -> tuple[float, float]:
    """从 ``node`` 中心朝 ``toward`` 射出与节点矩形的交点（绝对画布坐标）。"""
    return box_edge_point(node.x, node.y, node.w, node.h, toward[0], toward[1])


def node_box(node: LayoutNode, pad: float = 0.0) -> tuple[float, float, float, float]:
    """节点的矩形 ``(x0, y0, x1, y1)``（可外扩 ``pad``）。"""
    return (node.x - node.w / 2.0 - float(pad), node.y - node.h / 2.0 - float(pad),
            node.x + node.w / 2.0 + float(pad), node.y + node.h / 2.0 + float(pad))


def label_em(m: MapMetrics) -> float:
    """关系短标签自己的**字宽**（设备像素）：跟着卡片字号与缩放走（纯函数）。

    以前标签沿用了卡片的 ``m.em`` / ``m.line_h`` 来算避让框与净空，标签中心被顶到
    离连线十几像素开外，看上去「飘」在空白里（用户报「线标注的文本位置也不对」）。
    标签比卡片文字小一档（:data:`LABEL_FONT_SIZE`），这里就按它自己的字号算。
    """
    return max(6.0, float(m.em) * (float(LABEL_FONT_SIZE) / 9.0))


def label_gap_px(m: MapMetrics) -> float:
    """标签中心离**它自己那条连线**的净空：文字半高 + 一条小缝（纯函数）。"""
    return label_em(m) * 0.78 + max(1.0, float(m.label_gap) * 0.5)


def label_box(pos: tuple[float, float], label: str, m: MapMetrics
              ) -> tuple[float, float, float, float]:
    """关系短标签在画布上的矩形（纯估算宽度，与绘制的 ``font(7)`` 同量级）。

    标签绘制用 ``anchor="center"``，这里给出同一个中心的估算外框：避让检查与
    回归断言量的是同一件事。宽高都按**标签自己的字号**（:func:`label_em`）算。
    """
    em = label_em(m)
    width = max(em * 0.5, text_px(label, em))
    height = max(em * 1.3, em)
    return (float(pos[0]) - width / 2.0, float(pos[1]) - height / 2.0,
            float(pos[0]) + width / 2.0, float(pos[1]) + height / 2.0)


def label_plate(pos: tuple[float, float], label: str, m: MapMetrics
                ) -> tuple[float, float, float, float]:
    """关系短标签的**底色块**：盖掉从标签底下穿过去的连线（纯估算宽度）。

    就是 :func:`label_box` 的矩形上下各收一点、左右各放宽一点 —— 避让检查仍然
    用更宽的 ``label_box``，所以这里的底色块一定不会盖到别人的标签，而字号 7 的
    短标签又确实整块压在底色上（这是「连线穿过标签」这个观感问题的直接修法）。
    """
    x0, y0, x1, y1 = label_box(pos, label, m)
    inset = max(1.0, (y1 - y0) * 0.22)
    return (x0 - m.em * 0.12, y0 + inset, x1 + m.em * 0.12, y1 - inset)


def rects_intersect(first, second, *, tol: float = 0.0) -> bool:
    """两个矩形是否相交（``tol`` 是允许贴合的净空；纯函数）。"""
    gap = max(0.0, float(tol))
    return not (first[2] + gap <= second[0] or second[2] + gap <= first[0]
                or first[3] + gap <= second[1] or second[3] + gap <= first[1])


def _segment_hits_rect(x1, y1, x2, y2, rect) -> bool:
    """线段与矩形的相交（Liang-Barsky 裁剪；纯函数，无随机、无 Tk）。"""
    x0, y0, rx1, ry1 = (float(rect[0]), float(rect[1]),
                        float(rect[2]), float(rect[3]))
    if not _segment_may_hit(x1, y1, x2, y2, (rect,)):
        return False
    dx, dy = x2 - x1, y2 - y1
    low, high = 0.0, 1.0
    for numerator, denominator in ((x0 - x1, dx), (x1 - rx1, -dx),
                                   (y0 - y1, dy), (y1 - ry1, -dy)):
        if abs(denominator) < 1e-12:
            if numerator > 0:
                return False
            continue
        ratio = numerator / denominator
        if denominator > 0:
            low = max(low, ratio)
        else:
            high = min(high, ratio)
        if low > high:
            return False
    return True


def _segment_may_hit(x1, y1, x2, y2, rects) -> bool:
    """线段的包围盒是否可能碰到任一矩形（廉价预筛；纯函数）。"""
    for rect in rects:
        if max(x1, x2) < rect[0] or min(x1, x2) > rect[2] \
                or max(y1, y2) < rect[1] or min(y1, y2) > rect[3]:
            continue
        return True
    return False


def route_is_clear(points, obstacles, *, tol: float = 0.0) -> bool:
    """整条折线（``((x, y), …)``）是否避开全部障碍矩形。

    线宽 / 净空都算在 ``obstacles`` 的外扩里；折线的每个端点都要**严格**落在
    障碍外（只碰到边框不算避开）。
    """
    for first, second in zip(points, points[1:]):
        for rect in obstacles:
            if _segment_hits_rect(first[0], first[1], second[0], second[1], rect):
                return False
    return True


def _expand(rect, margin: float):
    return (rect[0] - margin, rect[1] - margin, rect[2] + margin, rect[3] + margin)


def _gap_to_box(rect, point) -> float:
    """点到矩形的空档（点在矩形里 = 0；纯函数）。"""
    dx = max(rect[0] - float(point[0]), 0.0, float(point[0]) - rect[2])
    dy = max(rect[1] - float(point[1]), 0.0, float(point[1]) - rect[3])
    return math.hypot(dx, dy)


def _quadratic_points(start, control, end, *, samples: int = CURVE_SEGMENTS):
    """二次贝塞尔采样成折线（真实画布与预览因此画的是同一串点；纯函数）。"""
    steps = max(2, int(samples))
    points = []
    for index in range(steps + 1):
        t = index / float(steps)
        u = 1.0 - t
        points.append((u * u * start[0] + 2.0 * u * t * control[0] + t * t * end[0],
                       u * u * start[1] + 2.0 * u * t * control[1] + t * t * end[1]))
    return tuple(points)


def _route_candidate_points(x1, y1, x2, y2, bow: float,
                            *, samples: int = CURVE_SEGMENTS):
    """沿路径法线鼓出 ``bow`` 的弧线（正负号 = 法线的两个方向）。"""
    mid_x, mid_y = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    vx, vy = x2 - x1, y2 - y1
    length = math.hypot(vx, vy) or 1.0
    control = (mid_x - vy / length * bow, mid_y + vx / length * bow)
    return _quadratic_points((x1, y1), control, (x2, y2), samples=samples), control


def _simplify_route(points, obstacles):
    """折线瘦身：跳过「直连也不碰任何障碍」的中间点（路线不变量不变）。"""
    keep: list = [points[0]]
    index = 1
    total = len(points)
    while index < total:
        if index == total - 1 or not route_is_clear((keep[-1], points[index + 1]),
                                                    obstacles):
            keep.append(points[index])
        index += 1
    return tuple(keep)


def _segment_clearance(x1, y1, x2, y2, rect) -> float:
    """线段到矩形的间隙（相交 / 穿过 = 0；纯函数）。"""
    if _segment_hits_rect(x1, y1, x2, y2, rect):
        return 0.0
    corners = ((rect[0], rect[1]), (rect[2], rect[1]),
               (rect[2], rect[3]), (rect[0], rect[3]))
    best = min(segment_distance(px, py, x1, y1, x2, y2) for px, py in corners)
    for cx, cy, dx, dy in ((rect[0], rect[1], rect[2], rect[1]),
                           (rect[2], rect[1], rect[2], rect[3]),
                           (rect[2], rect[3], rect[0], rect[3]),
                           (rect[0], rect[3], rect[0], rect[1])):
        best = min(best, segment_distance(x1, y1, cx, cy, dx, dy),
                   segment_distance(x2, y2, cx, cy, dx, dy))
    return best


def _visibility_route(start, end, obstacles, *, pad: float, states_limit: int):
    """折线兜底：拐点候选 + 视线检查 + Dijkstra（纯函数，有状态上限，必定收敛）。

    拐点 = 障碍外扩后的角点与边中点 + 起终点到附近障碍的投影 + 「外扩 x / y」交叉
    成的格子 + **层间空行**（行距本身就是最好走的通道）。代价 = 长度 + 「贴着障碍
    走」的罚（贴边越近罚越大）——因此选出来的绕行既躲开节点，也不会紧贴边框走。
    """
    if not obstacles:
        return None
    # 这里有两层矩形，**不能混用**：
    #
    # * ``collision`` = 传进来的障碍（``_routing_obstacles`` 已经把 ``route_margin``
    #   算进去了，也就是产品要求的净空）：**碰撞与罚项只用它** —— 邻居的视线检查、
    #   penalty、以及最后 ``route_is_clear`` 全都拿它当「撞上去了」的判据；
    # * ``inflated`` = 在 ``collision`` 上再外扩 ``CANDIDATE_OUTSET``：**只用来生成
    #   候选拐点**（角点 / 边中点 / 投影 / 交叉格子 / 空行 lane）。候选点因此严格落在
    #   碰撞障碍之外 —— 而 ``route_is_clear`` 的契约是「连碰边框也算失败」，候选点
    #   若正好落在碰撞边界上，它自己就永远连不出去。
    #
    # 离这条边太远的节点不可能挡路：先按「起终点包围盒 + 余量」裁一遍，
    # 每次视线检查的代价就与整张图的大小无关了（纯函数必须收敛）。
    room = max(100.0, float(pad) * 12.0)
    window = (min(start[0], end[0]) - room, min(start[1], end[1]) - room,
              max(start[0], end[0]) + room, max(start[1], end[1]) + room)
    nearby = [rect for rect in obstacles
              if not (rect[2] < window[0] or rect[0] > window[2]
                      or rect[3] < window[1] or rect[1] > window[3])]
    if not nearby:
        return None
    collision = nearby
    effective = [_expand(rect, pad * 0.9) for rect in nearby]
    # 候选点按「离起终点连线的远近」排序后取前一批（纯函数必须收敛：候选数封顶）
    ax0, ax1 = min(start[0], end[0]), max(start[0], end[0])
    ay0, ay1 = min(start[1], end[1]), max(start[1], end[1])
    ranked = sorted(effective, key=lambda rect: (
        max(0.0, rect[0] - ax1) + max(0.0, ax0 - rect[2])
        + max(0.0, rect[1] - ay1) + max(0.0, ay0 - rect[3]),
        rect[0], rect[1]))
    ranked = ranked[:max(12, max(1, int(states_limit)) // 40)]
    candidates: list[tuple[float, float]] = [start, end]

    def add(point) -> None:
        key = (round(float(point[0]), 3), round(float(point[1]), 3))
        if all(abs(key[0] - round(p[0], 3)) > 1e-6 or abs(key[1] - round(p[1], 3)) > 1e-6
               for p in candidates):
            candidates.append((float(point[0]), float(point[1])))

    for rect in ranked:
        for x in (rect[0], (rect[0] + rect[2]) / 2.0, rect[2]):
            add((x, rect[1]))
            add((x, rect[3]))
    # 起终点可能落在某个节点的净空里（这条边的两个词挨得很近）：把起终点**投影**
    # 到附近障碍的边界上，否则它们在图里会是孤点，折线永远搜不出来。
    for rect in effective:
        for point in (start, end):
            if max(rect[0] - point[0], point[0] - rect[2], 0.0) > pad * 8.0 \
                    and max(rect[1] - point[1], point[1] - rect[3], 0.0) > pad * 8.0:
                continue
            add((rect[0], point[1]))
            add((rect[2], point[1]))
            add((point[0], rect[1]))
            add((point[0], rect[3]))
    # 折线要能从**被别的节点夹住的缝**里穿过去：在「外扩 x / y」交叉处再布一层
    # 候选点（拐弯只会落在这些缝里）。点数仍然固定封顶。
    xs = sorted({round(rect[0], 3) for rect in ranked}
                | {round(rect[2], 3) for rect in ranked})
    ys = sorted({round(rect[1], 3) for rect in ranked}
                | {round(rect[3], 3) for rect in ranked})
    if len(xs) * len(ys) <= 400:
        for x in xs:
            for y in ys:
                add((x, y))
    # 层与层之间的**空行**（布局自己的行距）是最好走的通道：在「所有节点的 y 中心」
    # 之间铺横向 lane，再与障碍的竖直边交叉成候选点。
    centers = sorted({round((rect[1] + rect[3]) / 2.0, 3) for rect in effective})
    lanes = {round(rect[1], 3) for rect in effective}
    lanes |= {round(rect[3], 3) for rect in effective}
    for first, second in zip(centers, centers[1:]):
        if second - first > 1.0:
            lanes.add(round((first + second) / 2.0, 3))
    if centers:                              # 最上一层的上方 / 最下一层的下方
        lanes.add(round(centers[0] - max(pad * 3.0, 12.0), 3))
        lanes.add(round(centers[-1] + max(pad * 3.0, 12.0), 3))
    if len(xs) * max(1, len(lanes)) <= 600:
        for lane in sorted(lanes):
            for x in xs:
                add((x, lane))
    source, target = 0, 1
    budget = max(1, int(states_limit))
    # 视线检查是这里唯一的开销：给它一个硬上限（纯函数必须**一定**收敛，
    # 而且不能因为图很大就把整次布局拖慢 —— 超预算就放弃折线、退回弧线 / 直连）。
    lookups = [0]

    def neighbours(index):
        """惰性视线邻居：只算**真正被弹出**的节点（稠密图里省掉大量无用检查）。

        ``_segment_may_hit`` 是包围盒预筛：它说「不可能碰」时线段**一定**是通的
        —— 那种线段直接就是可用邻居（外侧的空白通道全在这一类里，丢掉它们等于
        把绕行通道整片删掉）。只有预筛说「可能碰」时才做精确的视线检查。
        """
        origin = candidates[index]
        for other in range(len(candidates)):
            if other == index:
                continue
            lookups[0] += 1
            if lookups[0] > budget * 12:
                return
            point = candidates[other]
            if _segment_may_hit(point[0], point[1], origin[0], origin[1], collision) \
                    and not route_is_clear((origin, point), collision):
                continue
            yield (other, origin, point)

    best = {source: 0.0}
    previous: dict[int, int] = {}
    seen: set[int] = set()
    steps = 0
    while len(seen) < len(candidates):
        steps += 1
        if steps > budget:
            return None
        pending = [(cost, node) for node, cost in best.items() if node not in seen]
        if not pending:
            break
        cost, node = min(pending, key=lambda item: (item[0], item[1]))
        if node == target:
            break
        seen.add(node)
        for other, point_a, point_b in neighbours(node):
            if other in seen:
                continue
            length = math.hypot(point_b[0] - point_a[0], point_b[1] - point_a[1])
            penalty = 0.0
            for rect in collision:
                if not _segment_may_hit(point_a[0], point_a[1], point_b[0], point_b[1],
                                        (rect,)):
                    continue
                clearance = _segment_clearance(point_a[0], point_a[1],
                                               point_b[0], point_b[1], rect)
                if clearance <= pad:
                    penalty += (pad - clearance) * 4.0
            total = cost + length + penalty
            if total < best.get(other, float("inf")):
                best[other] = total
                previous[other] = node
    if target not in best or (target not in previous and target != source):
        return None
    chain, node = [], target
    while True:
        chain.append(candidates[node])
        if node == source:
            break
        node = previous.get(node)
        if node is None:
            return None
    chain.reverse()
    if len(chain) < 2 or not route_is_clear(tuple(chain), obstacles):
        return None
    return _simplify_route(tuple(chain), effective)


def _route_candidates(start, end, obstacles, m: MapMetrics, *, prefer_curve: bool):
    """一条边所有**可画且避障**的候选路由（按偏好排序）+ 直连兜底（纯函数）。

    返回 ``(候选, 兜底)``：候选 = 弧线（正反两个方向 × 多档弯曲 × 两种采样密度）
    → 折线绕行，全都**已经避障**；兜底 = 直连（哪怕贴着节点也要画出来，
    绝不隐藏节点、绝不删合法边）。顺序固定，不含随机。

    ``prefer_curve``（跨边 / 反馈）会把弧线排在直连之前：跨边要能一眼看出不是
    层级直连，只有弧线全都不合适时才退回直连。
    """
    seen: list[tuple] = []
    keys: set = set()

    def push(points, control) -> None:
        key = (len(points), tuple((round(x, 3), round(y, 3)) for x, y in points))
        if key in keys:
            return
        keys.add(key)
        seen.append((tuple(points), control))

    line = (start, end)
    span = math.hypot(end[0] - start[0], end[1] - start[1])
    straight = (line, None)
    if span <= 1e-9:
        return [straight], straight
    base = max(float(m.cross_bow), float(m.em))
    # 弯曲半径随边长收敛（邻接的同层节点之间仍能画出弧），但太短的边不硬弯
    # （那时弯曲只会贴到端点节点自己身上）。
    bow_base = min(base, max(base * 0.45, span * 0.5))
    if span >= max(m.em, 12.0):
        for sign in (1.0, -1.0):
            for factor in (1.0, 1.6, 2.4, 3.2):
                bow = sign * bow_base * factor
                for samples in (CURVE_SEGMENTS, CURVE_SEGMENTS * 2):
                    points, control = _route_candidate_points(start[0], start[1],
                                                              end[0], end[1], bow,
                                                              samples=samples)
                    if route_is_clear(points, obstacles):
                        push(points, control)
    detour = _visibility_route(start, end, obstacles, pad=float(m.route_margin),
                               states_limit=int(m.route_states))
    if detour is not None and len(detour) > 2:
        push(detour, None)
    if not prefer_curve and route_is_clear(line, obstacles):
        push(line, None)
    return seen, straight


def _route_midpoint(points):
    """折线中点（按弧长走到一半）与切向（纯函数）。"""
    lengths = [math.hypot(second[0] - first[0], second[1] - first[1])
               for first, second in zip(points, points[1:])]
    total = sum(lengths)
    if total <= 1e-9:
        return points[0], (1.0, 0.0)
    target, walked = total / 2.0, 0.0
    for index, span in enumerate(lengths):
        if walked + span >= target and span > 1e-9:
            ratio = (target - walked) / span
            first, second = points[index], points[index + 1]
            point = (first[0] + (second[0] - first[0]) * ratio,
                     first[1] + (second[1] - first[1]) * ratio)
            return point, ((second[0] - first[0]) / span, (second[1] - first[1]) / span)
        walked += span
    first, second = points[-2], points[-1]
    length = math.hypot(second[0] - first[0], second[1] - first[1]) or 1.0
    return points[-1], ((second[0] - first[0]) / length, (second[1] - first[1]) / length)


def _point_along(points, fraction: float):
    """折线上按弧长比例取点 + 该处切向（纯函数）。"""
    lengths = [math.hypot(second[0] - first[0], second[1] - first[1])
               for first, second in zip(points, points[1:])]
    total = sum(lengths)
    if total <= 1e-9:
        return points[0], (1.0, 0.0)
    target = max(0.0, min(1.0, float(fraction))) * total
    walked = 0.0
    for index, span in enumerate(lengths):
        if walked + span >= target and span > 1e-9:
            ratio = (target - walked) / span
            first, second = points[index], points[index + 1]
            point = (first[0] + (second[0] - first[0]) * ratio,
                     first[1] + (second[1] - first[1]) * ratio)
            return point, ((second[0] - first[0]) / span, (second[1] - first[1]) / span)
        walked += span
    return points[-1], (1.0, 0.0)


def _normal(point, tangent, offset: float) -> tuple[float, float]:
    """把点沿法线挪 ``offset``：正号 = 朝画布上方（标签默认落在线上方）。"""
    upward = (-tangent[1], tangent[0])
    if upward[1] > 0:
        upward = (tangent[1], -tangent[0])
    return (point[0] + upward[0] * offset, point[1] + upward[1] * offset)


def _label_clearance(m: MapMetrics) -> float:
    """关系短标签的净空（设备像素）：只要**不压上去**就够了。

    标签之间 / 标签与节点之间按半个笔画宽度留缝（约 0.5 设备像素），远比连线的
    避障裕度小：标签不需要像线那样离节点很远，但**绝不能**压住节点或别的标签
    （压住就会被读成第三个词参与了这条关系）。
    """
    return max(0.5, min(float(m.label_gap), abs(theme.px(1)) / 2.0))


def _label_fits(box, blockers, *, pad: float) -> bool:
    return not any(rects_intersect(box, other, tol=pad) for other in blockers)


def _label_cost(box, blockers) -> float:
    """候选位置的代价：与障碍的重叠面积（越小越好；纯函数）。"""
    cost = 0.0
    for other in blockers:
        width = min(box[2], other[2]) - max(box[0], other[0])
        height = min(box[3], other[3]) - max(box[1], other[1])
        if width > 0 and height > 0:
            cost += width * height
    return cost


def _label_position(points, label: str, m: MapMetrics, *, base: tuple[float, float],
                    blockers, offset: float) -> tuple[tuple[float, float], float]:
    """关系标签落点：贴着**它自己那条线**，并且不压卡片 / 不压别的标签。

    搜索顺序是固定的（先近后远、先上后下、先中间后两边），**第一个放得下的候选就是
    结果** —— 这比「取代价最小」可预测：实机上标签不会忽左忽右地跳到整条线的另一头
    （用户报「线标注的文本位置不对」）。真的哪儿都放不下时，才退回「压得最少」的那个，
    此时它仍然贴着自己的线。
    """
    gap = _label_clearance(m)
    center, tangent = _route_midpoint(points)
    normal_offset = max(0.5, float(offset))
    base = (float(base[0]), float(base[1]))
    seen: list = []

    def spots():
        """候选落点：**先试调用方给的基准点**，再中间优先 / 另一侧 / 更远，最后沿路径挪。"""
        yield base
        for fraction in LABEL_SLIDE_STEPS:
            point, turn = (center, tangent) if not fraction else _point_along(points, 0.5 + fraction)
            for scale in LABEL_OFFSET_STEPS:
                spot = _normal(point, turn, normal_offset * float(scale))
                if math.hypot(spot[0] - base[0], spot[1] - base[1]) <= 1e-6:
                    continue            # 和基准点重合的候选不用试第二遍
                yield spot

    for spot in spots():
        box = label_box(spot, label, m)
        if not _label_fits(box, blockers, pad=0.0):
            continue
        if any(rects_intersect(box, other, tol=gap) for other in seen):
            continue
        seen.append(box)
        return spot, math.hypot(spot[0] - base[0], spot[1] - base[1])

    # 一个都不干净：取「压得最少」的那个（面积最小，其次离基准点最近）
    best_cost, best_spot, best_box = None, base, label_box(base, label, m)
    for spot in spots():
        box = label_box(spot, label, m)
        cost = (_label_cost(box, blockers) + _label_cost(box, seen),
                math.hypot(spot[0] - base[0], spot[1] - base[1]))
        if best_cost is None or cost < best_cost:
            best_cost, best_spot, best_box = cost, spot, box
    seen.append(best_box)
    return (best_spot,
            math.hypot(best_spot[0] - base[0], best_spot[1] - base[1]))


def _reachable(adjacency: dict, start: int, target: int) -> bool:
    """邻接表可达性（布局侧的**防御性**成环排除，纯函数）。"""
    stack, seen = [int(start)], set()
    while stack:
        node = stack.pop()
        if node == int(target):
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(adjacency.get(node, ()))
    return False


def _subtree(root: int, children: dict) -> list[int]:
    out, stack, seen = [], [int(root)], set()
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        out.append(node)
        stack.extend(children.get(node, ()))
    return out


def _relation_parts(rel) -> tuple[int, int, str]:
    return (int(getattr(rel, "src_entry_id", 0) or 0),
            int(getattr(rel, "dst_entry_id", 0) or 0),
            str(getattr(rel, "rel_type", "") or ""))


def _strongly_connected(adjacency: dict) -> list[list[int]]:
    """强连通分组（Kosaraju，**标准库之外的确定性纯函数**，无随机、无 Tk）。

    依赖 / 因果里「A 依赖 B、B 又依赖 A」这种反馈是真实存在的：它不能当层级，
    也不能删掉。这里把反馈圈里的节点归成一个分量，布局让整个分量待在同一层，
    分量内部的边画成反馈弧线（见 :func:`layout_graph`）。
    """
    nodes = sorted({int(node) for node in adjacency}
                   | {int(node) for outs in adjacency.values() for node in outs})
    reverse: dict[int, set] = {}
    for src, outs in adjacency.items():
        for dst in outs:
            reverse.setdefault(int(dst), set()).add(int(src))

    order: list[int] = []
    seen: set[int] = set()
    for start in nodes:
        if start in seen:
            continue
        seen.add(start)
        stack = [(start, iter(sorted(adjacency.get(start, ()))))]
        while stack:
            node, pending = stack[-1]
            advanced = False
            for nxt in pending:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append((nxt, iter(sorted(adjacency.get(nxt, ())))))
                    advanced = True
                    break
            if not advanced:
                order.append(node)
                stack.pop()

    groups: list[list[int]] = []
    assigned: set[int] = set()
    for node in reversed(order):
        if node in assigned:
            continue
        assigned.add(node)
        component, stack = [], [node]
        while stack:
            current = stack.pop()
            component.append(current)
            for prev in sorted(reverse.get(current, ())):
                if prev not in assigned:
                    assigned.add(prev)
                    stack.append(prev)
        groups.append(sorted(component))
    groups.sort(key=lambda component: component[0])
    return groups


def _constraint_pairs(rels) -> tuple[dict, dict, set]:
    """把关系集翻成布局用的有向约束 —— 返回 ``(before_of, after_of, feedback)``。

    * **包含 / 属于**：层级约束；成环的层级边在这里被排除（绝不画成假层级）；
    * **依赖 / 因果**：全部保留。同一个强连通分量内部的边不进 ``before_of`` /
      ``after_of``（它们不参与分层，改由反馈跨边表达），但一定出现在 ``feedback``
      里，绝不因为「成环」被丢掉；
    * 用途 / 对照：不产生层级约束。
    """
    hierarchy_adjacency: dict[int, set] = {}
    constraints: list[tuple[int, int, str]] = []
    for rel in rels:
        src, dst, rel_type = _relation_parts(rel)
        if rel_type in HIERARCHY_TYPES:
            pair = (src, dst) if rel_type == "包含" else (dst, src)
            before, after = pair
            if before == after or _reachable(hierarchy_adjacency, after, before):
                continue                  # 成环的层级边绝不参与分层
            hierarchy_adjacency.setdefault(before, set()).add(after)
            constraints.append((src, dst, rel_type))
        elif rel_type in DIRECTIONAL_TYPES:
            constraints.append((src, dst, rel_type))

    adjacency: dict[int, set] = {}
    for src, dst, rel_type in constraints:
        if rel_type not in DIRECTIONAL_TYPES:
            continue
        pair = (dst, src) if rel_type == "依赖" else (src, dst)
        adjacency.setdefault(int(pair[0]), set()).add(int(pair[1]))
    component_of: dict[int, int] = {}
    for index, component in enumerate(_strongly_connected(adjacency)):
        for node in component:
            component_of[node] = index

    before_of: dict[int, list] = {}
    after_of: dict[int, list] = {}
    feedback: set[tuple[int, int]] = set()
    seen_pairs: set[tuple[int, int]] = set()
    for src, dst, rel_type in constraints:
        if rel_type in DIRECTIONAL_TYPES:
            before, after = (dst, src) if rel_type == "依赖" else (src, dst)
            if component_of.get(int(before)) == component_of.get(int(after)):
                feedback.add((int(before), int(after)))
                continue                  # 反馈圈内部：同层 + 反馈弧，不做前后分层
        else:
            before, after = (src, dst) if rel_type == "包含" else (dst, src)
        if (before, after) in seen_pairs:
            continue                      # 同一对只连一次
        seen_pairs.add((before, after))
        before_of.setdefault(after, []).append(before)
        after_of.setdefault(before, []).append(after)
    return before_of, after_of, feedback


def _layer_levels(active, before_of, after_of) -> tuple[dict, set]:
    """前后分层（拓扑最长路径；纯函数）。

    ``active`` 是参与分层的词条；``before_of`` / ``after_of`` 是
    :func:`_constraint_pairs` 给出的前后约束。返回 ``(level, settled)``：
    ``level`` 是每个词的层号（被依赖 / 因在前），``settled`` 是被正常结算的词 ——
    没结算的（防御性兜底）回到第 0 层，绝不丢掉节点。布局与
    :func:`layout_extent` 共用这一份分层，两者的行宽才是同一个数。
    """
    level = {int(entry_id): 0 for entry_id in active}
    indegree = {int(entry_id): len(before_of.get(int(entry_id), ()))
                for entry_id in active}
    queue = sorted(entry_id for entry_id in level if indegree[entry_id] == 0)
    settled: set = set()
    while queue:
        node = queue.pop(0)
        settled.add(node)
        for child in sorted(after_of.get(node, ())):
            level[child] = max(level.get(child, 0), level[node] + 1)
            indegree[child] -= 1
            if indegree[child] <= 0 and child not in settled:
                queue.append(child)
                queue.sort()
    for entry_id in level:                # 防御：未结算的节点不参与分层
        if entry_id not in settled:
            level[entry_id] = 0
    return level, settled


def _pair_crossings(first, second, links) -> int:
    """两层之间的**交叉数**（纯函数）：把 ``links`` 展开成序列后数逆序对。

    ``links[entry]`` 是 ``first`` 层里某个词在 ``second`` 层的邻居。逆序对数量
    就是「两条线在两行之间交叉了多少次」——同层词之间的次序由列表下标给出。
    """
    position = {int(entry_id): index for index, entry_id in enumerate(second)}
    sequence: list[int] = []
    for entry_id in first:
        for other in sorted(links.get(int(entry_id), ())):
            if int(other) in position:
                sequence.append(position[int(other)])
    crossings = 0
    for index, value in enumerate(sequence):
        crossings += sum(1 for later in sequence[index + 1:] if later < value)
    return crossings


def _reduce_crossings(levels, order_map, before_of, after_of) -> None:
    """层间**交叉最小化**：同层相邻两项只在交叉数下降时交换（纯函数，原地改）。

    重心法（见 ``_layout_core`` 的 ``passes``）只让同层的词靠近自己的父 / 子，
    两行之间仍会留下「甲连丙、乙连丁」这种交叉。这里对每一对相邻层做一遍确定性
    下降：交换**严格减少**交叉数才保留（平局不换），因此结果只由结构决定、可复现。
    每个交换都严格减小交叉数，所以循环一定终止。
    """
    for upper, lower in zip(levels, levels[1:]):
        # ``levels`` 是升序的层号 ⇒ 前一个是**上行**。行内次序必须按这个方向配对：
        # ``after_of`` 由父指向子（键在上行），``before_of`` 由子指向父（键在下行）；
        # 两边都要数（各自覆盖一批交叉），而且**两边都要非空** —— 一旦把两行传反，
        # 两次调用的 ``links`` 都取不到键，``total()`` 会恒等于 0、整个最小化变成空转。
        rows = (order_map[upper], order_map[lower])

        def total() -> int:
            return (_pair_crossings(rows[0], rows[1], after_of)
                    + _pair_crossings(rows[1], rows[0], before_of))

        for lvl in (upper, lower):
            changed = True
            while changed and len(order_map[lvl]) > 1:
                changed = False
                for index in range(len(order_map[lvl]) - 1):
                    before = total()
                    order_map[lvl][index], order_map[lvl][index + 1] = (
                        order_map[lvl][index + 1], order_map[lvl][index])
                    if total() < before:
                        changed = True
                    else:
                        order_map[lvl][index], order_map[lvl][index + 1] = (
                            order_map[lvl][index + 1], order_map[lvl][index])


def _routing_obstacles(nodes, exclude, m: MapMetrics):
    """避障用的节点矩形（外扩 ``route_margin``）：排除这条边自己的两个端点。"""
    margin = float(m.route_margin)
    return tuple(_expand(node_box(node), margin) for node in nodes
                 if int(node.entry_id) not in exclude)


def _same_path(first, second) -> bool:
    """两条线是不是**同一串点**（容差 :data:`LAYOUT_EPS`；纯函数）。

    用来发现「同一对词之间的关系被画了两遍」：正向 + 反向的一对边、或同一对词之间
    的两条不同类型的关系，如果几何完全重合就会被读成一条粗线。
    """
    if len(first) != len(second):
        return False
    return all(abs(a[0] - b[0]) <= LAYOUT_EPS and abs(a[1] - b[1]) <= LAYOUT_EPS
               for a, b in zip(first, second))


def _orthogonal_routes(base_edges, nodes, m: MapMetrics):
    """跨层连线统一排成**正交三段线**（上层卡片底边 → 车道 → 下层卡片顶边）。

    返回与 ``base_edges`` 等长的列表（``None`` = 这条边仍交给 :func:`route_edges`
    的候选搜索）。规范：

    * 只管 ``hierarchy`` / ``direction`` 边（它们表达「上下层级」）：相邻**两行**走两行
      之间的空档，**跨行**的边走目标行上方那条空档（垂直段从源卡片底边直下，
      中间隔着别的行时由净空检查兜底退回候选搜索）；``cross`` / ``feedback`` 保持
      弧线 —— 它们是「横跨 / 反馈」的视觉语言，必须和层级直连一眼分得开；
      键是**行号**而不是层号：一层太宽时会被均匀分带（见 ``_layout_core``），
      带与带互为相邻行，同层跨带的边因此**不**走这条规则（竖线会穿过中间的带）；
    * **端口分散**：同一张卡片同一侧的多条边按对端 x 排序后等距分端口，不再挤在
      同一个点上；
    * **车道分道**：同一层间空档里的水平段按中点 x 排序排进不同车道，同层的线互不
      压住；车道全部落在 ``v_gap`` 空档里（那里没有任何卡片）；
    * 生成的点仍要过 :func:`route_is_clear` 才会被采用（拿不准就退回候选搜索）；
    * 点序**从源卡片到目标卡片**（反向依赖也在翻回来），箭头才会落在目标上。
    """
    by_id = {int(node.entry_id): node for node in nodes}
    row_of: dict[int, int] = {}
    band: dict[int, list[float]] = {}
    for node in nodes:
        if node.isolated:
            continue                      # 孤立词没有任何关系边
        entry_id = int(node.entry_id)
        row_of[entry_id] = int(node.row)
        row = band.setdefault(int(node.row), [node.y - node.h / 2.0,
                                              node.y + node.h / 2.0])
        row[0] = min(row[0], node.y - node.h / 2.0)
        row[1] = max(row[1], node.y + node.h / 2.0)

    clear = float(m.route_margin) + ORTHO_LANE_CLEAR
    plans: list[dict | None] = []
    for base in base_edges:
        src, dst, _type = _relation_parts(base.rel)
        upper, lower = by_id.get(src), by_id.get(dst)
        plan = None
        if (upper is not None and lower is not None
                and base.kind in ("hierarchy", "direction")):
            a, b = row_of.get(int(src)), row_of.get(int(dst))
            flipped = False
            if a is not None and b is not None and a != b:
                if a > b:
                    # 源卡片在目标卡片**下面**（例如「降采样 依赖 算子」这种反向依赖）：
                    # 几何排布仍然按「上面的卡片 → 下面的卡片」算，但点序必须
                    # 从**源**到**目标**，否则 ``arrow="last"`` 的箭头会画在源卡片上。
                    upper, lower, a, b = lower, upper, b, a
                    flipped = True
                # 相邻两行用它们之间的空档；**跨行**的边退而使用「目标行上方」那条
                # 空档（垂直段照旧从源卡片底边直下 —— 中间隔着别的行时，只要那条
                # 竖线撞到卡片，下面 ``route_edges`` 的净空检查就会把它退回候选搜索）。
                near, far = (a, b) if b - a == 1 else (b - 1, b)
                gap_top = band.get(near, [0.0, 0.0])[1]
                gap_bottom = band.get(far, [0.0, 0.0])[0]
                if gap_bottom - gap_top > 2.0 * clear + ORTHO_LANE_MIN:
                    plan = {"upper": int(upper.entry_id), "lower": int(lower.entry_id),
                            "key": (near, far), "top": gap_top, "bottom": gap_bottom,
                            "y0": upper.y + upper.h / 2.0,
                            "y1": lower.y - lower.h / 2.0,
                            "src_x": upper.x, "dst_x": lower.x,
                            "flipped": flipped}
        plans.append(plan)

    def far_x(plan: dict, entry_id: int) -> float:
        """这条边**另一端**的默认 x（端口排序用，与最终端口无关）。"""
        return plan["dst_x"] if plan["upper"] == entry_id else plan["src_x"]

    # 端口分散：同一张卡片同一侧的多条边按对端 x 排序，在边宽内等距分开
    ends: dict[tuple[int, str], list[int]] = {}
    for index, plan in enumerate(plans):
        if plan is None:
            continue
        ends.setdefault((plan["upper"], "bottom"), []).append(index)
        ends.setdefault((plan["lower"], "top"), []).append(index)
    ports: dict[int, list[float]] = {}
    for (entry_id, side), members in ends.items():
        node = by_id[entry_id]
        inset = max(2.0, min(float(m.pad_x), node.w * ORTHO_PORT_INSET))
        half = max(0.0, node.w / 2.0 - inset)
        ordered = sorted(members, key=lambda index: (far_x(plans[index], entry_id),
                                                     index))
        step = (2.0 * half) / float(len(ordered) - 1) if len(ordered) > 1 else 0.0
        for position, index in enumerate(ordered):
            x = node.x - half + step * position if len(ordered) > 1 else node.x
            ports.setdefault(index, [node.x, node.x])[0 if side == "bottom" else 1] = x

    def source_x(index: int) -> float:
        plan = plans[index]
        return ports.get(index, [plan["src_x"], plan["dst_x"]])[0]

    # 车道：同一层间空档里的水平段分道，在空档正中居中。
    # **车道的上下次序按「起点 x 从右到左」排**：两条不交叉的线（起点与终点同序）里，
    # 起点偏左的那条放到**下面的车道** ⇒ 它的垂直短段不会穿过另一条的水平段。
    # （反过来排会让每条线的垂直段都从别的车道上穿过去，凭空多出一批交叉。）
    lanes: dict[int, float] = {}
    groups: dict[tuple[int, int], list[int]] = {}
    for index, plan in enumerate(plans):
        if plan is not None:
            groups.setdefault(plan["key"], []).append(index)
    for members in groups.values():
        plan = plans[members[0]]
        top, bottom = plan["top"], plan["bottom"]
        span = max(0.0, (bottom - clear) - (top + clear))
        center = (top + bottom) / 2.0
        ordered = sorted(members, key=lambda index: (-source_x(index), index))
        count = len(ordered)
        spacing = 0.0
        if count > 1:
            spacing = max(ORTHO_LANE_MIN,
                          min(float(m.line_h) * 0.6, span / float(count - 1)))
        for position, index in enumerate(ordered):
            lanes[index] = center + (position - (count - 1) / 2.0) * spacing

    routes: list[list[tuple[float, float]] | None] = [None] * len(base_edges)
    for index, plan in enumerate(plans):
        if plan is None or index not in lanes:
            continue
        port = ports.get(index, [plan["src_x"], plan["dst_x"]])
        start_x, end_x = port[0], port[1]
        if abs(start_x - end_x) < LAYOUT_EPS:
            points = [(start_x, plan["y0"]), (start_x, plan["y1"])]
        else:
            lane = lanes[index]
            points = [(start_x, plan["y0"]), (start_x, lane),
                      (end_x, lane), (end_x, plan["y1"])]
        if plan["flipped"]:
            # 反向依赖（源在下面）：翻回「源 → 目标」的点序，箭头才落在目标上。
            points.reverse()
        routes[index] = points
    return routes


def route_edges(base_edges, nodes, m: MapMetrics, *, ortho=None):
    """给每条关系**选一条真实画得出来的线**，并放好它的类型标签（纯函数）。

    * 一条边绝不穿过**非端点**节点的矩形（含小间距：障碍按 ``route_margin`` 外扩）；
    * 相邻两层的层级 / 方向边走**正交三段线**（``ortho`` 参数给出，见
      :func:`_orthogonal_routes`）：这是排版规范，只要它本身干净就一定采用 ——
      脏了就退回候选搜索，**绝不硬穿**；
    * 跨边 / 反馈先试弧线（正反两个方向 + 多档弯曲），不行再折线绕行；
    * 关系标签避开所有节点矩形与已放好的标签，同时仍然贴着它自己的那条边；
    * 同一对词之间的第二条线**不许和第一条重合**（重合会被读成一条粗线）；
    * ``对照`` 是对称关系：``symmetric=True``（两端都不画箭头）。
    节点位置与层级**一个字都不动** —— 这里只改连线怎么走。
    """
    by_id = {int(node.entry_id): node for node in nodes}
    routed: list[LayoutEdge] = []
    label_blockers = [node_box(node, m.label_gap) for node in nodes]
    twins: dict[frozenset, list] = {}
    for order, base in enumerate(base_edges):
        src, dst, rel_type = _relation_parts(base.rel)
        src_node, dst_node = by_id.get(src), by_id.get(dst)
        if src_node is None or dst_node is None:
            continue
        start = boundary_point(src_node, (dst_node.x, dst_node.y))
        end = boundary_point(dst_node, (src_node.x, src_node.y))
        obstacles = _routing_obstacles(nodes, {int(src), int(dst)}, m)
        prefer_curve = base.kind in ("cross", "feedback")
        forced = None if ortho is None else (ortho[order] if order < len(ortho) else None)
        if forced is not None and not route_is_clear(forced, obstacles):
            forced = None                   # 正交线不干净 ⇒ 退回候选搜索
        if forced is not None:
            candidates = [(forced, None)]
        else:
            preferred, fallback = _route_candidates(start, end, obstacles, m,
                                                    prefer_curve=prefer_curve)
            candidates = list(preferred) + [fallback]
        key = frozenset((int(src), int(dst)))
        chosen = None
        for index, (points, control) in enumerate(candidates):
            # 标签净空：**标签自己的**半高 + 一条小缝（见 ``label_offset``）——
            # 以前拿卡片行高算，标签被顶到离连线十几像素开外，看着像飘在空白里。
            label_offset = label_gap_px(m)
            if forced is not None:
                # 正交线：标签落在**水平段中点上方**（基准点的法线就是画布上方）
                if len(points) >= 4:
                    base_pos = ((points[1][0] + points[2][0]) / 2.0,
                                points[1][1] - label_offset)
                else:
                    center, tangent = _route_midpoint(points)
                    base_pos = _normal(center, tangent, label_offset)
            elif control is not None:
                base_pos = (control[0], control[1] - label_offset)
            else:
                center, tangent = _route_midpoint(points)
                base_pos = _normal(center, tangent, label_offset)
            label_pos, drift = _label_position(points, str(base.label), m,
                                               base=base_pos, blockers=label_blockers,
                                               offset=label_offset)
            box = label_box(label_pos, str(base.label), m)
            label_overlap = 0.0 if _label_fits(box, label_blockers, pad=0.0) else 1.0
            twin_overlap = 1.0 if any(_same_path(points, other)
                                      for other in twins.get(key, ())) else 0.0
            # 选线代价（严格优先级）：
            #   ① **穿过非端点节点**（兜底直连才会发生）—— 那是会被读成「第三个词
            #      也在这条关系里」的误读，权重远高于其它项；
            #   ② 与同一对词的既有连线**完全重合**（会被读成一条粗线）；
            #   ③ **跨边 / 反馈退化成直连**（那是层级边的视觉语言，见
            #      :func:`_route_candidates`）：2 点直连只剩「弧线全都被挡住」这
            #      最后一次机会。权重必须压过「拐点惩罚 + 标签位移」这些几十像素的
            #      小账 —— 实测反馈边弧线 22.0 vs 直连 18.8，只差 3px 就会把反馈
            #      画成一条直线（用户报的「布线杂乱」正是这类退化）；但必须低于
            #      「标签压叠」（1e4）：弧线的标签真的没地方放时，宁可退回直连，
            #      也不要把两个关系标签叠在一起；
            #   ④ 关系文字被挤走的距离（保持原位最好）；
            #   ⑤ 拐了几个弯（每点 2px，同层相邻的层级边因此优先直连）；
            #   ⑥ 文字压到节点 / 别的标签（只有极端拥挤时才允许折衷）。
            curve_last = 1e3 * (1.0 if prefer_curve and len(points) <= 2 else 0.0)
            route_cost = (1e6 * (0.0 if route_is_clear(points, obstacles) else 1.0)
                          + 1e5 * twin_overlap
                          + curve_last
                          + drift + 2.0 * max(0, len(points) - 2)
                          + 1e4 * label_overlap + index * 1e-3)
            if chosen is None or route_cost < chosen[0]:
                chosen = (route_cost, points, label_pos)
        if chosen is None:                 # 理论上不会发生（兜底直连一定在）
            continue
        _cost, points, label_pos = chosen
        twins.setdefault(key, []).append(list(points))
        label_blockers.append(label_box(label_pos, str(base.label), m))
        routed.append(LayoutEdge(base.rel, base.kind, tuple(points), base.label,
                                 (float(label_pos[0]), float(label_pos[1])),
                                 bool(base.symmetric)))
    return tuple(routed)


def _fit_columns(canvas_w: float, card_w: float, m: MapMetrics) -> int:
    """一行最多放几张卡：由画布宽度 + 统一卡片宽决定（绝不排出屏幕右边，纯函数）。"""
    return max(1, int((max(float(canvas_w) - 2.0 * m.pad + m.h_gap, card_w))
                      // max(card_w + m.h_gap, 1.0)))


def _balanced_rows(items, columns: int) -> list[list[int]]:
    """把一行的词**均匀分带**（每带词数最多差 1，纯函数）：一条带就是一行。

    按 ``columns`` 硬切会在末尾留一条只有一两个词的秃带（和孤立词网格同一个毛病）；
    分带数定下来之后均匀分，每带依旧「等距列 + 整行居中同一轴」。
    装得下时只返回一条带 ⇒ 排布与「一层一行」逐像素一致。
    """
    ordered = list(items)
    if not ordered:
        return []
    columns = min(max(1, int(columns)), len(ordered))
    count = max(1, -(-len(ordered) // columns))
    per, extra = divmod(len(ordered), count)
    out: list[list[int]] = []
    start = 0
    for index in range(count):
        size = per + (1 if index < extra else 0)
        out.append(ordered[start:start + size])
        start += size
    return out


def _layout_core(labels, relations, *, width: int, height: int, topic_label: str = "",
                 metrics: MapMetrics | None = None,
                 top_pad: float | None = None,
                 pins: dict[int, tuple[float, float]] | None = None,
                 template: str = "") -> MapLayout:
    """按**关系结构**排布（zoom = 1 的**逻辑布局**；纯函数，无 Tk、无随机）。

    这份逻辑布局是唯一的权威：:func:`layout_graph` 只在它上面做一次仿射缩放
    （所有坐标 × zoom），:func:`layout_extent` 直接取它的内容外框 —— 两处
    绝不会各算一份、更不会一个把另一个算小。

    排布规则（用户最新要求）：

    * 包含 / 属于 → 父子层级；依赖 / 因果 → 前后分层（被依赖 / 因在前）；
    * **每一个层就是一条视觉行**：绝不把两层并排塞进同一条行（那会让「谁在前」
      读不出来）；同层高 = 该行最高那个词的高度，行内垂直居中；
    * **统一卡片宽**：同一张图里所有词卡同宽（= 最宽那张），列间距一律 ``h_gap``；
    * 所有行（含孤立词每一行）与中心主题**共用同一条中心轴**：行各自居中，
      不再出现「主题居中、某一行整体偏左」；
    * 没有任何关系的词排成**整齐网格**：每行放得下的列数由画布宽度与统一卡片宽
      决定（绝不排出屏幕右边），行内等距、整行居中；
    * 互相依赖 / 互为因果（强连通分量）同层 + 反馈弧线，不伪造层级；
    * 连线绕开非端点节点（:func:`route_edges`），标签不压节点 / 别的标签；
    * 内容外框把节点、连线、标签、孤立词标题全部包住（加左右 / 上下留白），
      ``scrollregion`` 与「节点不出界」都用它；
    * ``pins``（F2）：用户把某几张卡拖到过的地方 —— 这几张卡直接落在他摆的位置
      （画布坐标里的卡片中心，只夹左上留白的下限），其余卡片照上面的规则自动排。
    * ``template``（G1）：布局骨架（思维导图 / 树状图 / 组织架构图 / 单向导图 /
      鱼骨图 / 流程线，见 :mod:`app.ui.map_templates`）。**只替换「卡片摆在哪」这一步**，
      布线 / 标签 / 分组底衬 / 内容外框照旧；骨架和数据不搭时坐标保持默认，并留下
      ``template_note`` 如实说明（例如「因果边太少，先按自动排」）。
    """
    m = metrics or metrics_for()
    canvas_w, canvas_h = max(1, int(width)), max(1, int(height))
    top = float(m.pad if top_pad is None else max(m.pad, float(top_pad)))
    ids = sorted(int(key) for key in (labels or {}))
    if not ids:
        return MapLayout(width=canvas_w, height=canvas_h,
                         content_w=2.0 * m.pad, content_h=top + m.pad)

    # ---- 换行 + **统一卡片宽**：同一张图里每个词卡同宽（最宽那张的文字宽度） ----
    wrapped: dict[int, tuple[str, ...]] = {}
    sizes: dict[int, tuple[float, float]] = {}
    for entry_id in ids:
        lines = tuple(wrap_label(str(labels.get(entry_id) or ""), m.max_w - 2 * m.pad_x,
                                 m.em))
        wrapped[entry_id] = lines
        sizes[entry_id] = box_size(lines, m)
    card_w = max(m.min_w, max(size[0] for size in sizes.values()))
    card_h = {entry_id: sizes[entry_id][1] for entry_id in ids}
    topic_lines = tuple(wrap_label(str(topic_label or ""), m.max_w, m.topic_em))
    topic_w = max(m.min_w, 2 * m.topic_pad_x + max(
        (text_px(line, m.topic_em) for line in topic_lines), default=0.0))
    topic_h = max(m.min_h, 2 * m.topic_pad_y + max(1, len(topic_lines)) * m.line_h)

    valid = set(ids)
    rels = [rel for rel in (relations or ())
            if _relation_parts(rel)[0] in valid and _relation_parts(rel)[1] in valid
            and _relation_parts(rel)[0] != _relation_parts(rel)[1]
            and _relation_parts(rel)[2] in REL_TYPES]
    touched = set()
    for rel in rels:
        src, dst, _type = _relation_parts(rel)
        touched.update((src, dst))
    active = [entry_id for entry_id in ids if entry_id in touched]
    isolated = [entry_id for entry_id in ids if entry_id not in touched]

    before_of, after_of, feedback = _constraint_pairs(rels)
    level, _settled = _layer_levels(active, before_of, after_of)

    rows: dict[int, list[int]] = {}
    for entry_id in active:
        rows.setdefault(level[entry_id], []).append(entry_id)
    levels = sorted(rows)

    # ---- 行内顺序：按「父在上 / 子在下」把同层词的左右次序微调（间距始终等距）--
    slot: dict[int, float] = {entry_id: 0.0 for entry_id in ids}
    order_map = {lvl: sorted(rows[lvl]) for lvl in levels}
    iso_order = sorted(isolated)

    def assign() -> None:
        """按 ``order_map`` 把每行**等距**排好（统一卡片宽 → 列间距一律 h_gap）。"""
        for lvl in levels:
            cursor = 0.0
            for entry_id in order_map[lvl]:
                slot[entry_id] = cursor + card_w / 2.0
                cursor += card_w + m.h_gap

    assign()
    for _ in range(max(0, int(m.passes))):
        desired = dict(slot)
        for lvl in levels[1:]:
            for entry_id in order_map[lvl]:
                parents = before_of.get(entry_id, ())
                if parents:
                    desired[entry_id] = sum(slot[p] for p in parents) / len(parents)
        for lvl in reversed(levels[:-1]):
            for entry_id in order_map[lvl]:
                kids = after_of.get(entry_id, ())
                if kids:
                    desired[entry_id] = sum(slot[k] for k in kids) / len(kids)
        for lvl in levels:
            order_map[lvl] = sorted(order_map[lvl],
                                    key=lambda item: (desired[item], item))
        assign()
    # 重心法之后再做一遍**层间交叉最小化**：两行之间「甲连丙、乙连丁」的交叉
    # 只靠重心法消不掉（它只看同层的相对次序，不看线到底交不交）。
    _reduce_crossings(levels, order_map, before_of, after_of)
    assign()

    def row_width(count: int) -> float:
        return float(count) * card_w + m.h_gap * max(0, int(count) - 1)

    max_cols = _fit_columns(canvas_w, card_w, m)
    iso_rows = _balanced_rows(iso_order, max_cols)
    # 关系分层**一层一行**：同一层的词排在同一行、共用一条中心轴。
    # （试过「一层太宽就均匀分带成多行」，实测更糟：分带把同一层的词拆到多行之后，
    # 跨带的父子边只能退回候选搜索，画面反而多出一堆斜穿全图的线 —— 见
    # ``_check/crowding_probe.py`` 的 crowding-old/new.png 对比。宽度收不住时宁可
    # 让用户横向平移，也不拆散一层的拓扑。）
    bands = {lvl: [order_map[lvl]] for lvl in levels}

    blocks = [topic_w] + [row_width(len(band)) for lvl in levels
                          for band in bands[lvl]]
    blocks += [row_width(len(group)) for group in iso_rows]
    inner_w = max([max(blocks, default=m.min_w), m.min_w])
    content_left = float(m.pad)
    axis = content_left + inner_w / 2.0          # 全图唯一的中心轴

    nodes: list[LayoutNode] = []

    def place(entry_id: int, row_center: float, row_h: float, lvl: int, index_row: int,
              isolate: bool, row_left: float, index: int) -> None:
        """把一个词放进它那一行的统一高度里（等距列 + 行整体居中）。"""
        height_px = row_h if row_h > 0 else card_h[entry_id]
        nodes.append(LayoutNode(
            entry_id, str(labels.get(entry_id) or ""), wrapped[entry_id],
            row_left + card_w / 2.0 + index * (card_w + m.h_gap),
            row_center, card_w, height_px, lvl, bool(isolate), index_row))

    topic = LayoutNode(0, str(topic_label or ""), topic_lines, axis,
                       top + topic_h / 2.0, topic_w, topic_h, -1, False, -1)
    topic_links: list[tuple[tuple[float, float], ...]] = []
    y = top + topic_h + m.topic_gap
    index_row = 0
    for lvl in levels:                     # ①② 关系分层：**一层一行**
        for band in bands[lvl]:
            row_h = max(card_h[entry_id] for entry_id in band)
            row_left = axis - row_width(len(band)) / 2.0
            for index, entry_id in enumerate(band):
                place(entry_id, y + row_h / 2.0, row_h, lvl, index_row, False,
                      row_left, index)
            y += row_h + m.v_gap
            index_row += 1
    isolated_level = (max(levels) + 1) if levels else 0
    isolated_label_pos = None
    if iso_order:
        isolated_label_pos = (content_left, y - m.v_gap / 2.0)
        y += m.line_h * 1.5
        for group in iso_rows:             # ③ 孤立词：整齐网格（每行等距 + 整行居中）
            row_h = max(card_h[entry_id] for entry_id in group)
            row_left = axis - row_width(len(group)) / 2.0
            for index, entry_id in enumerate(group):
                place(entry_id, y + row_h / 2.0, row_h, isolated_level, index_row,
                      True,
                      row_left, index)
            y += row_h + m.v_gap
            index_row += 1

    # ---- G1 模板骨架：把上面这份「一层一行」的坐标换掉，其余原封不动 ----------
    # 模板只决定**卡片摆在哪**；连线（route_edges 避障 + 标签让位）、分组底衬、
    # 内容外框、点选 / 双击 / 拖拽 / 固定位置 / 黑名单 / 导出全部沿用下面的代码。
    # 摆不成（鱼骨图却没有因果边等）→ 坐标不动、只留下一条 note 如实说明。
    template_used = ""
    template_note = ""
    hubs: tuple[int, ...] = ()
    if template and template != map_templates.TEMPLATE_AUTO:
        frame = map_templates.Frame(
            axis=axis, top=y, card_w=card_w,
            card_h={int(item): card_h[int(item)] for item in ids},
            topic_w=topic_w, topic_h=topic_h,
            h_gap=m.h_gap, v_gap=m.v_gap, pad=m.pad)
        placement = map_templates.place(template, ids=ids, rels=rels,
                                       levels=level, frame=frame)
        if placement is not None:
            template_note = str(placement.note or "")
        if placement is not None and placement.spots and not template_note:
            spots = {int(key): (float(value[0]), float(value[1]))
                     for key, value in placement.spots.items()}
            moved: list[LayoutNode] = []
            for node in nodes:
                spot = spots.get(int(node.entry_id))
                moved.append(node if spot is None
                             else replace(node, x=spot[0], y=spot[1]))
            nodes = moved
            template_used = template
            if placement.axis_shift:
                topic = replace(topic, x=topic.x + float(placement.axis_shift))
            if placement.topic_pos is not None:
                topic = replace(topic, x=float(placement.topic_pos[0]),
                                y=float(placement.topic_pos[1]))
            hubs = tuple(int(item) for item in placement.hubs)
            if isolated_label_pos is not None:
                bottom_of_nodes = max(node.y + node.h / 2.0 for node in nodes)
                isolated_label_pos = (content_left, bottom_of_nodes + m.v_gap / 2.0)

    by_id = {int(node.entry_id): node for node in nodes}
    if pins:
        # ---- 用户固定过位置的卡片（F2）：把这几张卡搬到他摆的地方，剩下的仍然
        #      自动排。位置是**画布坐标里的卡片中心**；只夹一个下限（别越出左上角
        #      的留白），不夹上限 —— 他想摆多远就摆多远，内容外框会跟着长。
        #      连线、分组底衬、内容外框都在下面才计算，所以全都跟着新位置走。
        wanted = {int(key): (float(value[0]), float(value[1]))
                  for key, value in pins.items()}
        for index, node in enumerate(nodes):
            spot = wanted.get(int(node.entry_id))
            if spot is None:
                continue
            nodes[index] = replace(
                node,
                x=max(m.pad + node.w / 2.0, spot[0]),
                y=max(top + node.h / 2.0, spot[1]),
            )
        by_id = {int(node.entry_id): node for node in nodes}
    # 主题 → 顶层词的**母线**：主题底边先垂直下到母线，再水平走，最后垂直落进
    # 每张卡片顶边的**正中**。所有连线共用同一条母线（``topic_gap`` 正中），
    # 因此不再是从主题斜射出去的扇形 —— 那是用户说的「布线杂乱」的一半。
    topic_bottom = topic.y + topic.h / 2.0
    bus_y = topic_bottom + m.topic_gap / 2.0
    top_level = levels[0] if levels else None
    if hubs:
        # 思维导图 / 鱼骨图：主题不在顶上、内容也不排在一条水平行里，母线没有意义
        # —— 直接从主题的边界拉一条直线到这几个「一级节点」。
        for entry_id in sorted(set(hubs)):
            node = by_id.get(int(entry_id))
            if node is None or node.isolated:
                continue
            topic_links.append((boundary_point(topic, (node.x, node.y)),
                                boundary_point(node, (topic.x, topic.y))))
    else:
        for entry_id in sorted({n.entry_id for n in nodes if n.level == top_level}):
            node = by_id[entry_id]
            if node.isolated:
                continue
            topic_links.append(((topic.x, topic_bottom), (topic.x, bus_y),
                                (node.x, bus_y), (node.x, node.y - node.h / 2.0)))

    # ---- 关系连线：只在这里决定「线怎么走」（避障 + 标签让位，节点位置不动） ----
    base_edges: list[LayoutEdge] = []
    for rel in rels:
        src, dst, rel_type = _relation_parts(rel)
        kind = edge_kind(rel_type)
        if kind == "direction":
            before, after = (dst, src) if rel_type == "依赖" else (src, dst)
            if (int(before), int(after)) in feedback:
                kind = "feedback"         # 同一条强连通分量内部：反馈弧，不是前后层
        base_edges.append(LayoutEdge(rel, kind, (), rel_type, (0.0, 0.0),
                                     rel_type in SYMMETRIC_TYPES))
    # 布线规范都在 ``_orthogonal_routes`` 里：相邻两层的层级 / 方向边走正交三段线
    # （端口分散 + 车道分道），跨层 / 跨边 / 反馈仍走候选搜索。节点位置一个字不动。
    edges = route_edges(base_edges, nodes, m,
                        ortho=_orthogonal_routes(base_edges, nodes, m))

    # ---- 内容外框：节点 / 连线 / 标签 / 孤立词标题全部包住，再加一圈留白 ----
    right = content_left + inner_w
    bottom = max(top + topic_h, y - m.v_gap)
    for node in [topic, *nodes]:
        right = max(right, node.x + node.w / 2.0)
        bottom = max(bottom, node.y + node.h / 2.0)
    for edge in edges:
        for x, py in edge.points:
            right = max(right, float(x))
            bottom = max(bottom, float(py))
        box = label_box(edge.label_pos, edge.label, m)
        right = max(right, box[2])
        bottom = max(bottom, box[3])
    if isolated_label_pos is not None:
        right = max(right, float(isolated_label_pos[0])
                    + text_px(ISOLATED_TEXT, m.em * 7.0 / 9.0))
    content_w = right + m.pad
    content_h = bottom + m.pad

    groups: list[LayoutGroup] = []
    children: dict[int, list[int]] = {}
    parent_of: dict[int, int] = {}
    for rel in rels:
        src, dst, rel_type = _relation_parts(rel)
        if rel_type == "包含":
            parent, child = src, dst
        elif rel_type == "属于":
            parent, child = dst, src
        else:
            continue
        if child in parent_of and parent_of[child] != parent:
            continue                      # 一个节点只归一个父分组（多父时不重复画）
        parent_of[child] = parent
        children.setdefault(parent, [])
        if child not in children[parent]:
            children[parent].append(child)
    for root in sorted(children):
        members = _subtree(root, children)
        if len(members) < 2 or root not in by_id:
            continue
        member_nodes = [by_id[member] for member in members if member in by_id]
        if len(member_nodes) < 2:
            continue
        groups.append(LayoutGroup(
            root_id=int(root), members=tuple(sorted(int(x.entry_id) for x in member_nodes)),
            x0=min(node.x - node.w / 2.0 for node in member_nodes) - m.group_pad,
            y0=min(node.y - node.h / 2.0 for node in member_nodes) - m.group_pad,
            x1=max(node.x + node.w / 2.0 for node in member_nodes) + m.group_pad,
            y1=max(node.y + node.h / 2.0 for node in member_nodes) + m.group_pad,
        ))

    return MapLayout(
        width=canvas_w, height=canvas_h, topic=topic, nodes=tuple(nodes),
        edges=tuple(edges), groups=tuple(groups), topic_links=tuple(topic_links),
        content_w=content_w, content_h=content_h, isolated_label_pos=isolated_label_pos,
        template=template_used, template_note=template_note,
    )


def _scaled_layout(layout: MapLayout, factor: float) -> MapLayout:
    """把 zoom = 1 的逻辑布局**整体仿射缩放**（所有坐标 × factor；纯函数）。

    缩放**只改坐标，不改结构**：列数 / 行数 / 绕行折线的形状一个都不变
    （所以滚轮缩放不会换列、不会把某条边重新绕一次），指针锚定也因此是
    严格的仿射关系：``画布坐标_缩放后 = 画布坐标_缩放前 × ratio``。
    """
    f = float(factor or 1.0)
    if abs(f - 1.0) < 1e-9:
        return layout

    def point(value) -> tuple[float, float]:
        return (float(value[0]) * f, float(value[1]) * f)

    def node(value: LayoutNode) -> LayoutNode:
        return replace(value, x=value.x * f, y=value.y * f,
                       w=value.w * f, h=value.h * f)

    return replace(
        layout,
        topic=None if layout.topic is None else node(layout.topic),
        nodes=tuple(node(item) for item in layout.nodes),
        edges=tuple(replace(edge, points=tuple(point(item) for item in edge.points),
                            label_pos=point(edge.label_pos))
                    for edge in layout.edges),
        groups=tuple(replace(group, x0=group.x0 * f, y0=group.y0 * f,
                             x1=group.x1 * f, y1=group.y1 * f)
                     for group in layout.groups),
        topic_links=tuple(tuple(point(item) for item in link)
                          for link in layout.topic_links),
        content_w=layout.content_w * f,
        content_h=layout.content_h * f,
        isolated_label_pos=(None if layout.isolated_label_pos is None
                            else point(layout.isolated_label_pos)),
    )


def layout_graph(labels, relations, *, width: int, height: int, topic_label: str = "",
                 metrics: MapMetrics | None = None, zoom: float = 1.0,
                 top_pad: float | None = None,
                 pins: dict[int, tuple[float, float]] | None = None,
                 template: str = "") -> MapLayout:
    """**关系结构布局**（层一行 / 统一卡片宽 / 居中等距 / 孤立词网格）+ 仿射缩放。

    先在 zoom = 1 下算出唯一的逻辑布局（:func:`_layout_core`），再按 ``zoom``
    把所有坐标整体缩放 —— 因此滚轮缩放**不换列**、不重排、不改绕行形状，
    而首开自适应（:func:`layout_extent`）用的就是同一份逻辑布局的尺寸。
    ``pins`` 是用户固定过位置的卡片（见 :func:`_layout_core`）。
    """
    core = _layout_core(labels, relations, width=width, height=height,
                        topic_label=topic_label, metrics=metrics, top_pad=top_pad,
                        pins=pins, template=template)
    return _scaled_layout(core, zoom)


def layout_extent(labels, relations, *, width: int, height: int, topic_label: str = "",
                  metrics: MapMetrics | None = None,
                  top_pad: float | None = None,
                  pins: dict[int, tuple[float, float]] | None = None,
                  template: str = "") -> tuple[float, float]:
    """**在 zoom = 1 下**这张图自然需要多少画布（纯函数）。

    直接取 :func:`_layout_core` 的内容外框 —— 与 :func:`layout_graph` 同源，
    因此**不可能低估**真实布局（低估会让首开自适应算小、一开就被裁掉）。
    只用来选初始缩放，不参与绘制。``pins`` 必须与绘制时传同一份，否则
    自适应会按「卡片还在自动位置」算，把用户拖远的卡裁到画面外。
    ``template`` 同理，必须与绘制时一致 —— 换了模板，外框跟着变。
    """
    core = _layout_core(labels, relations, width=width, height=height,
                        topic_label=topic_label, metrics=metrics, top_pad=top_pad,
                        pins=pins, template=template)
    return (float(core.content_w), float(core.content_h))

# ===================================================================== 窗口
class ConceptMapWindow:
    """参考关系图窗口（单例由 ``App`` 保证；本类只管自己这一扇窗）。"""

    def __init__(self, master: tk.Misc, app, only_ids=None, full_ids=None):
        self.app = app
        self.db = app.db
        self.service = getattr(app, "map_service", None)
        self.config = getattr(app, "config", None)
        self._topic_id: int | None = None
        self._topic_ids: list[int] = []
        self._topic_name_text = ""
        self._nodes: list = []
        self._labels: dict[int, str] = {}      # entry_id → 唯一显示名
        self._graph = None                     # MapGraph | None（**已校验**的关系）
        self._fingerprint = ""
        self._pending_token: int | None = None
        self._selected = None                  # 当前点选的 MapRelation
        self._selected_node = None             # 当前点选的词（entry_id；与连线二选一）
        self._state = "empty"                  # empty / idle / loading / ready / error / nokey
        self._zoom = 1.0
        self._layout: MapLayout | None = None
        self._items: list[int] = []
        self._edge_items: dict[int, int] = {}  # id(rel) → canvas item
        self._edge_rels: dict[int, object] = {}
        self._draw_key = None
        self._scheduled = False
        #: 「手离开画布后补画一次」的定时器 id。手势期间 :meth:`_draw` **拒绝重画**：
        #: 重画会把卡片按布局位置摆回去（手指底下的卡片会「跳」），更会把
        #: :meth:`_clear` 里的手势状态一起擦掉 —— 拖动当场失效。
        self._draw_retry = None
        #: 首开自适应缩放是否还没做过（换主题 / 首次拿到真实画布尺寸时才做一次；
        #: 用户一旦自己滚过轮就再也不自动改缩放与位置）
        self._fit_pending = True
        #: 右键拖动平移的状态：``_pan_last`` = 上一次指针位置（屏幕坐标）。
        #: 它**只在右键按住期间非 None**：:meth:`_pan_guard` 据此判断「这一下左键
        #: 是不是发生在平移当中」；右键一松手（:meth:`_on_pan_end`）立刻清掉，
        #: 因此松手之后的**第一次**正常左键点击照常点线 / 点词，绝不被吞掉。
        self._pan_last: tuple[int, int] | None = None
        #: 「本次分析 N 词 / 该主题共 M 词」的短句（只有被上限截断时非空）
        self._coverage_note = ""
        #: 该主题的**全部**词节点（``self._nodes`` 可能是它的子集，见 ``_subset_ids``）
        self._all_nodes: list = []
        #: 只分析选中的这些词条（空集合 = 分析全部）。改它必须重算指纹并重新生成 ——
        #: 子集与全部是**两份不同的分析结果**，指纹不同、缓存行也不同（改进清单 C3）。
        self._subset_ids: set[int] = set()
        #: 主界面勾选进来的范围（K1）：非空 = 这张图只画/只分析这些词。与窗口内
        #: 自己挑的子集共用 ``_subset_ids``（同一套裁剪、指纹与缓存口径），只是
        #: 入口不同 —— 一个是主界面卡片上的勾选框，一个是图窗左边那张列表。
        #: ``_only_topic_id`` 记「这个子集是为哪个主题设的」：:meth:`_load_topic`
        #: 只放行那一次，之后用户自己换主题照旧清空（子集不跨主题沿用）。
        self._only_topic_id: int | None = None
        #: 构造期还没定主题时，:meth:`apply_only_ids` 把范围寄存在这里
        #: （``(勾选的 id 集合, 那一组的全部 id 集合)``），:meth:`_load_topic` 取用。
        self._pending_only_ids: tuple[set[int], set[int]] | None = None
        self.apply_only_ids(only_ids, full_ids=full_ids)
        #: 用户人工添加的关系（:class:`app.map_service.ManualRelation`）。每次读库都
        #: 重新算一遍：人工关系是**用户自己的判断**，AI 重新生成绝不覆盖它（C5）。
        self._manual_rels: tuple = ()
        #: 孤立词诊断面板的行 → entry_id（供双击定位用），见 :meth:`_fill_diagnostics`
        self._diag_lines: dict[int, int] = {}
        #: 用户把卡片拖到过的地方（F2）：``entry_id → (x, y)``，画布坐标里的卡片中心。
        #: 只在**本主题**下有意义（换主题要重读；见 :meth:`_reload_pins`）。
        self._pins: dict[int, tuple[float, float]] = {}
        #: AI 关系黑名单（F3）：被用户判定「这条不对」的端点对，**两个方向**都在集合里
        #: （AI 边的方向常常本身就是错的，只按单向过滤会漏掉反向那条）。
        self._blocks: set[tuple[int, int]] = set()
        #: 正在拖的连线（Alt+左键）：``(起点 entry_id, 起点 x, 起点 y)``；None = 没在拖
        self._link_from: tuple[int, float, float] | None = None
        #: 正在拖的卡片（左键摆位置）：``(entry_id, 抓取点相对卡片中心的偏移)``
        self._drag_node: tuple[int, float, float] | None = None
        #: 画布上的**拖拽临时线**：松手 / 取消时删掉，绝不留在图上
        self._link_item: int | None = None
        #: 拖动卡片时跟着鼠标走的 item（卡片本体 + 文字；松手重画整张图）
        self._drag_items: list[int] = []
        #: 上一次搬到的位置（拖动期间按它算增量，卡片才会严格跟着鼠标走）
        self._drag_last: tuple[float, float] | None = None
        #: 松手前这一刻鼠标下的卡片（供状态行说明「要连到哪儿」）
        self._drag_target: int | None = None
        #: 这一下按下时的**屏幕**坐标（``event.x/y``）与「是否真的拖起来了」——
        #: 见 :data:`DRAG_SLOP`：不算够位移就当单击，**既不固定卡片也不建关系**。
        self._press_xy: tuple[float, float] | None = None
        self._drag_started: bool = False
        #: 左键拖**空白处** = 平移整张图（本轮，ProjectGraph 式）：按下点在空白上时
        #: 记这里，之后每次 ``<B1-Motion>`` 按它算增量。不用 ``_pan_last``（那是右键
        #: 平移的状态，``_pan_guard`` 拿它判「正在平移」，混用会吃掉松手那一下）。
        self._bg_pan_last: tuple[float, float] | None = None
        #: 布局骨架（G1）：``"auto"`` = 一层一行的老布局；其余见
        #: :mod:`app.ui.map_templates`。存在设置里（``map.template``），换主题不变。
        self._template: str = self._read_template()
        #: 上一次模板没摆成时的说明（拿来写状态行；摆成了就是空串）
        self._template_note: str = ""
        #: 模板对话框（同一时间只开一个）
        self._template_dialog = None
        #: 「导图设置…」窗口（同一时间只开一个）：模板 / 让模型参与 / 恢复自动布局 ——
        #: 本轮从主界面「设置」搬进导图界面（用户口径）。
        self._map_settings_dialog = None
        #: 右下角操作提示（用户口径 2026-10-04）：**展开 20 秒后自动淡出折叠**。
        #: ``_hint_expanded`` = 现在是不是三行全展开；``_hint_user_collapsed`` =
        #: 这一次折叠是用户自己点掉的（点掉的就**不再自动展开**，否则手一快想收起来、
        #: 又被定时器弹回来，等于这个折叠键坏了）；``_hint_fade_id`` = 待执行的定时器
        #: id（关窗 / 重新展开时必须 ``after_cancel``，否则回调会打到已销毁的窗口上）。
        self._hint_expanded: bool = True
        self._hint_user_collapsed: bool = False
        self._hint_fade_id = None
        self._hint_fade_base: str | None = None
        #: 淡出时的「起点色」怎么取（见 :meth:`_hint_fade_color`）：真实 Tk 的
        #: ``Canvas`` 有 ``-bg``，画布就是它上面那块底色；取到了就按「画布底色」
        #: 精确混色，取不到再用窗口底色兜底（本主题两者同色，肉眼无差）。
        self._hint_tint_override: str | None = None

        self.win = tk.Toplevel(master)
        self.win.title("参考关系图 — 探索词典")
        self.win.configure(bg=theme.BG)
        self.win.geometry(f"{theme.px(880)}x{theme.px(640)}")
        self.chrome = widgets.BorderlessChrome(
            self.win, title="参考关系图", on_close=self.close, resizable=True,
            min_w=640, min_h=460, bg=theme.PANEL, on_resized=self._on_resized,
        )

        head = tk.Frame(self.win, bg=theme.BG)
        head.pack(fill="x", padx=theme.px(10), pady=(theme.px(6), theme.px(2)))
        tk.Label(head, text="主题", bg=theme.BG, fg=theme.TEXT_MUTED,
                 font=theme.tracking_label(7)).pack(side="left")
        self.feedback = tk.Label(head, text="", bg=theme.BG, fg=theme.TEXT_MUTED,
                                 font=theme.font(8), anchor="e")
        self.feedback.pack(side="right")

        body = tk.Frame(self.win, bg=theme.BG)
        body.pack(fill="both", expand=True, padx=theme.px(10), pady=theme.px(4))

        # ---- 左：主题列表（点一下 = 换中心主题；这里就是「简洁主题选择」）----
        left = tk.Frame(body, bg=theme.BG, width=theme.px(190))
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        self.topic_list = tk.Listbox(
            left, font=theme.font(9), bg=theme.PANEL, fg=theme.TEXT,
            highlightthickness=1, highlightbackground=theme.BORDER, bd=0,
            activestyle="none", exportselection=False, selectmode="browse",
            selectbackground=theme.ACCENT, selectforeground=theme.BG,
        )
        self.topic_list.pack(fill="both", expand=True)
        self.topic_list.bind("<<ListboxSelect>>", self._on_topic_select)

        # ---- 左：本主题的词条（可多选 = 只分析子集，改进清单 C3）----
        # 全部生成在词条几十上百条时很贵：这里让用户勾出关心的那一小批，只对子集
        # 发一次请求。double-click 一条 = 回主界面打开这条词条（改进清单 C4）。
        tk.Label(left, text="词条（可多选，只分析选中的）", bg=theme.BG,
                 fg=theme.TEXT_FAINT, font=theme.tracking_label(7),
                 anchor="w").pack(fill="x", pady=(theme.px(8), theme.px(2)))
        self.entry_list = tk.Listbox(
            left, height=9, activestyle="none", exportselection=False,
            selectmode="extended", bg=theme.PANEL, fg=theme.TEXT,
            font=theme.font(9), highlightthickness=1,
            highlightbackground=theme.BORDER, borderwidth=0,
            selectbackground=theme.ACCENT, selectforeground=theme.BG,
        )
        self.entry_list.pack(fill="x")
        self.entry_list.bind("<Double-Button-1>", self._on_entry_list_open)
        self.btn_subset = widgets.FlatButton(left, "只分析选中的词",
                                             self._on_subset_generate,
                                             font_size=8, padx=8, pady=3)
        self.btn_subset.pack(fill="x", pady=(theme.px(4), 0))
        self.btn_all = widgets.FlatButton(left, "分析全部词条", self._on_all_generate,
                                          font_size=8, padx=8, pady=3)
        self.btn_all.pack(fill="x", pady=(theme.px(2), 0))

        # ---- 右：画布（右键拖动平移 / 滚轮以指针为锚缩放）----
        self.canvas = tk.Canvas(body, bg=theme.PANEL, highlightthickness=1,
                                highlightbackground=theme.BORDER)
        self.canvas.pack(side="left", fill="both", expand=True, padx=(theme.px(8), 0))
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Button-1>", self._on_canvas_click)
        #: 双击词卡 = 回主界面打开这条词条（改进清单 C4：不用回列表里翻）
        self.canvas.bind("<Double-Button-1>", self._on_canvas_double_click)
        #: 左键拖拽（F1 建关系 / F2 摆位置）：**按住 Alt** = 从这张卡拖一条关系到
        #: 别的卡，不按 Alt = 抓起这张卡摆位置。拖动期间只画临时线或搬动卡片
        #: （**不动任何数据**），松手才落库。与上面的 ``<Button-1>`` 是两条独立
        #: 绑定（``add="+"``），因此「点一下 = 选中 / 看依据」的老行为一个字都不变。
        for sequence, handler in (("<Button-1>", self._on_canvas_press),
                                  ("<B1-Motion>", self._on_canvas_motion),
                                  ("<ButtonRelease-1>", self._on_canvas_release),
                                  # 按住 Alt + 左键 = 建关系。
                                  #
                                  # ★★ 这里**不许**再绑 ``<Mod1-Button-1>``
                                  # （2026-10-05 的血案）：Windows 上 Tk 给「不按
                                  # 任何键的真实左键」报的 ``event.state`` 就是
                                  # ``0x8``，而 ``0x8`` 正是 Tk 的 ``Mod1Mask``
                                  # 那一位 —— 于是 ``<Mod1-Button-1>`` **匹配每一次
                                  # 普通左键**；Tk 对同一个控件只触发**最具体的
                                  # 那一条**绑定，它比 ``<Button-1>`` 具体，普通
                                  # 左键因此全被它抢走，``_on_canvas_press``
                                  # （拖动那条路）根本不执行。用户两次报障
                                  # （「左键拖不动」「左键怎么还是连线」）都是这一条
                                  # 造成的，橡皮筋线还会留在画布上。
                                  # 真鼠标实测：只绑 ``<Button-1>`` 时普通左键
                                  # ``state=0x8``、按住 Alt ``state=0x20008``；
                                  # ``<Alt-Button-1>`` 单绑时能正常命中。
                                  ("<Alt-Button-1>", self._on_canvas_press_alt)):
            try:
                self.canvas.bind(sequence, handler, add="+")
            except (tk.TclError, TypeError):  # pragma: no cover - 极简替身
                continue
        #: 松手兜底：鼠标在**画布外**松开时画布收不到 ``<ButtonRelease-1>``，那一轮
        #: 手势会一直挂着（橡皮筋线留在画布上、下一次按下被当成「上一轮还没结束」——
        #: 用户 2026-10-05 拍屏里那两条没有终点的长线就是这么来的）。绑在顶层窗口上，
        #: 只要松手发生在这个窗口里就一定收得到；``<Escape>`` 是显式放弃当前手势。
        for sequence, handler in (("<ButtonRelease-1>", self._on_foreign_release),
                                  ("<Escape>", self._on_escape)):
            try:
                self.win.bind(sequence, handler, add="+")
            except (tk.TclError, TypeError, AttributeError):  # pragma: no cover
                continue
        # Windows 的右键 = Button-3；Button-2 是 X11 / 触控板的中间键兜底。
        # 左键**不参与**平移：平移只在右键按下时开始，节点点击仍然只认左键。
        for sequence, handler in (
                ("<MouseWheel>", self._on_zoom_wheel),
                ("<Control-MouseWheel>", self._on_zoom_wheel),
                ("<Button-3>", self._on_pan_start),
                ("<B3-Motion>", self._on_pan_motion),
                ("<ButtonRelease-3>", self._on_pan_end),
                ("<Button-2>", self._on_pan_start),
                ("<B2-Motion>", self._on_pan_motion),
                ("<ButtonRelease-2>", self._on_pan_end)):
            try:
                self.canvas.bind(sequence, handler, add="+")
            except (tk.TclError, TypeError):  # pragma: no cover - 极简替身
                continue

        # ---- 右下角操作提示：**可折叠**（用户口径 2026-10-04）----
        # 原话：「右下角的提示你改成可折叠起来的，用户刚刚打开时展开进行提示，过 20 秒
        # 之后淡出折叠。这个背景要是透明的，不要给提示背景板设置颜色。」
        # 三件事对应三处：
        # ① 刚打开 = 三行全展开，右对齐排在画布右下角（``place`` 的浮层，不是画布
        #    图元 —— 平移 / 缩放时视图在动，提示必须钉在窗口角上不动）；
        # ② 20 秒后**淡出**收成一行 :data:`HINT_COLLAPSED_TEXT`（见
        #    :meth:`_schedule_hint_fade` / :meth:`_show_hint`，点那一行可再展开）；
        # ③ 背景**透明**：底板与文字都用画布自己的底色（``self.hint_tint()``），
        #    不加 ``highlightthickness``、不设 ``highlightbackground``，皮上就只剩
        #    文字，没有「提示背景板」。
        # **与 F7 那次反馈的出入**：早先用户说过提示「压在卡片上看着像透明的、字读
        # 不清」，当时才加上 CARD_BG 底板 + 1px 边框（``改进清单.md`` / ``README.md``
        # 有记）。这次口径明确要求去掉背景板，按**新口径**实现；压在卡片上时略挤是
        # 可接受的代价，折叠后只剩一行小字，影响也小。
        self.hint_box = tk.Frame(self.canvas, bg=self.hint_tint())
        self.hint_inner = tk.Frame(self.hint_box, bg=self.hint_tint())
        self.hint_inner.pack(padx=theme.px(9), pady=theme.px(7))
        self._build_hint_rows(expanded=self._hint_expanded)
        self.hint_box.place(relx=1.0, rely=1.0, anchor="se",
                            x=-theme.px(HINT_INSET), y=-theme.px(HINT_INSET))
        # 刚打开就开始计时：20 秒后自动淡出折叠（用户口径「刚刚打开时展开」）。
        self._schedule_hint_fade(expanded=self._hint_expanded)

        # ---- 底：唯一生成入口 + 依据区（没有起点 / 终点 / 关系名输入框）----
        bar = tk.Frame(self.win, bg=theme.BG)
        bar.pack(fill="x", padx=theme.px(10), pady=(0, theme.px(2)))
        self.btn_generate = widgets.FlatButton(bar, "生成参考关系", self._on_generate,
                                               primary=True, font_size=9, padx=14, pady=5)
        self.btn_generate.pack(side="left")
        self.btn_settings = widgets.FlatButton(bar, "打开设置配置 API Key",
                                               self._open_settings, font_size=8,
                                               padx=10, pady=4)
        self._hint_visible = False
        tk.Label(bar, text=f"{MAP_TAG}：连线为模型候选，已按材料证据筛过；点连线看依据、点词看关系",
                 bg=theme.BG, fg=theme.TEXT_FAINT,
                 font=theme.font(8)).pack(side="right")

        # ---- 底：导出 / 模板 / 恢复布局 / 操作说明 / 导图设置 ----
        # 本轮（用户口径）：原来那排功能按钮里的「人工关系…」「孤立词诊断」
        # 「打开选中的词条」三个**内化进操作本身**，不再各占一个按钮 ——
        #   · 人工关系 = 按住 Alt 从一张卡拖到另一张（提示行与手势表都写着）；
        #   · 孤立词诊断 = 点画布上「孤立词…」那一行（见 ``_bind_isolated_label``）；
        #   · 打开词条 = 双击词卡（``<Double-Button-1>``）。
        # 三个方法本身都留着（图上的入口、其它调用点与测试都还在用），只是不再
        # 出现在工具条上：用户要的是「通过操作来实现」，不是一排功能框。
        bar2 = tk.Frame(self.win, bg=theme.BG)
        bar2.pack(fill="x", padx=theme.px(10), pady=(0, theme.px(2)))
        self.btn_export = widgets.FlatButton(bar2, "导出关系图…", self.export_map_files,
                                             font_size=8, padx=10, pady=4)
        self.btn_export.pack(side="left")
        # 「模板…」（G1·G2）：换一个布局骨架（思维导图 / 树状图 / 组织架构图 /
        # 单向导图 / 鱼骨图 / 流程线）。按钮上写着当前用的是哪个，一眼能看见。
        self.btn_template = widgets.FlatButton(bar2, "模板…", self.open_template_dialog,
                                               font_size=8, padx=10, pady=4)
        self.btn_template.pack(side="left", padx=(theme.px(6), 0))
        # 「恢复自动布局」（F2）：把本主题下被拖过的卡片一次性放回自动位置。
        self.btn_unpin = widgets.FlatButton(bar2, "恢复自动布局", self.reset_node_pins,
                                            font_size=8, padx=10, pady=4)
        self.btn_unpin.pack(side="left", padx=(theme.px(6), 0))
        # 「操作说明…」（G0）：手势表 INTERACTIONS 的完整版 —— 三行提示只够放最常用的，
        # 拖动卡片 / Alt 建关系这种「靠猜就会误操作」的手势要有一个能查的地方。
        self.btn_help = widgets.FlatButton(bar2, "操作说明…", self.open_shortcuts_help,
                                           font_size=8, padx=10, pady=4)
        self.btn_help.pack(side="left", padx=(theme.px(6), 0))
        # 「导图设置…」（本轮）：模板 / 让模型参与挑模板 / 恢复自动布局都在导图自己的
        # 设置窗里 —— 用户口径「导图的相关设置需要在导图界面中，主界面不应该显示导图
        # 的相关设置」，所以主界面「设置」里那一节已经搬走（见 MapSettingsDialog）。
        self.btn_map_settings = widgets.FlatButton(bar2, "导图设置…", self.open_map_settings,
                                                   font_size=8, padx=10, pady=4)
        self.btn_map_settings.pack(side="left", padx=(theme.px(6), 0))
        # 图例（用户口径）：连线外观全图统一，人工关系**不在图上**另做标记 ——
        # 想分辨哪条是自己加的，看右下角依据区或双击这条线。
        tk.Label(bar2, text="人工添加的关系不出现在线上标注里：双击一条线可以看到它的来历",
                 bg=theme.BG, fg=theme.TEXT_FAINT, font=theme.font(8)).pack(side="right")

        # 孤立词诊断：默认藏起来，点「孤立词诊断」才展开（C2：不用逐条点词）。
        self.diag_text = tk.Text(self.win, height=5, wrap="none", bg=theme.PANEL,
                                 fg=theme.TEXT_MUTED, font=theme.font(8),
                                 highlightthickness=1,
                                 highlightbackground=theme.BORDER, borderwidth=0)
        self.diag_text.configure(state="disabled")
        self.diag_text.bind("<Double-Button-1>", self._on_diag_open)
        self._diag_visible = False

        self.evidence = tk.Label(self.win, text="", bg=theme.BG, fg=theme.TEXT_MUTED,
                                 font=theme.font(8), anchor="w", justify="left",
                                 wraplength=theme.px(620))
        self.evidence.pack(fill="x", padx=theme.px(10), pady=(0, theme.px(8)))

        self.refresh()

    # ------------------------------------------------------------- 生命周期
    def alive(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:  # pragma: no cover
            return False

    def lift(self) -> bool:
        """抬到最前并给焦点。**不动父窗口**。"""
        try:
            self.win.deiconify()
            self.win.lift()
            self.win.focus_force()
        except tk.TclError:  # pragma: no cover
            return False
        return True

    def close(self) -> None:
        """关闭本窗：只关自己，**不退出程序**（迟到的结果也不会再进这个窗口）。"""
        # 提示的淡出定时器必须先取消：它排在 ``self.win`` 上，窗口一销毁再触发，
        # 回调就会去打已经没了的控件（真实 Tk 抛 TclError）。
        self._cancel_hint_fade()
        # 补画定时器同理：手势中途关窗，它会比窗口活得久，Tk 会打
        # ``invalid command name "..._draw_after_gesture"``。
        self._cancel_deferred_draw()
        self.chrome.enable(False)
        self._pending_token = None
        try:
            self.win.destroy()
        except tk.TclError:  # pragma: no cover
            pass

    # ------------------------------------------------------------- 数据
    def topic_rows(self) -> list:
        try:
            return list(self.db.list_batches())
        except Exception:  # pragma: no cover - 读库失败不该炸窗口
            log.exception("读取主题失败")
            return []

    def _topic_name(self) -> str:
        if self._topic_id is None:
            return ""
        try:
            row = self.db.get_batch(int(self._topic_id))
        except Exception:  # pragma: no cover
            return ""
        return str(row["name"] or "").strip() if row is not None else ""

    def _set_feedback(self, text: str) -> None:
        """窗口内**内联**短反馈（不弹系统 messagebox）。"""
        try:
            if str(self.feedback.cget("text")) == str(text):
                return
            self.feedback.configure(text=str(text))
        except tk.TclError:  # pragma: no cover
            pass

    def _with_coverage(self, text: str) -> str:
        """给短状态补上「本次分析 N 词 / 共 M 词」（只有被上限截断时才补）。"""
        note = str(getattr(self, "_coverage_note", "") or "")
        text = str(text or "")
        return f"{text}（{note}）" if note and note not in text else text

    def _model_note(self, graph=None) -> str:
        """反馈行里的「模型 + 推理强度」短标注（用户要求：界面要能看出用了什么模型）。

        指纹里已经包含模型与推理强度（两者任一变化都不命中缓存），所以能显示到
        这里的结果一定出自同一份配置。配置读不到时退回图自己记的模型名（``base|model``
        只取模型名那一半），**绝不猜一个模型名出来**。
        """
        text = ""
        cfg = self.config
        if cfg is not None:
            try:
                text = str(cfg.model_display or "")
            except Exception:  # pragma: no cover - 配置替身
                text = ""
        if not text:
            text = str(getattr(graph, "model_config", "") or "").rsplit("|", 1)[-1].strip()
        return f" · {text}" if text else ""

    def refresh(self, *, topic_id: int | None = None) -> None:
        """重建主题列表并重新载入当前主题的图（幂等，只读本地库）。

        ``topic_id``：指定就载入这个主题（主界面点「导图」时把自己正在看的主题
        报过来 —— 勾选是按那个主题勾的，图却跑去画「最近更新的那个主题」，
        勾了半天等于没勾）。不给就沿用当前主题，没有就挑第一个。
        """
        rows = self.topic_rows()
        selected = self._topic_id
        if topic_id is not None:
            selected = int(topic_id)
        self.topic_list.delete(0, tk.END)
        self._topic_ids = []
        sel_index = None
        for row in rows:
            try:
                batch_id = int(row["id"])
                name = str(row["name"] or "").strip()
                count = int(row["entry_count"] or 0)
            except (IndexError, KeyError, TypeError, ValueError):  # pragma: no cover
                continue
            self._topic_ids.append(batch_id)
            self.topic_list.insert(tk.END, f"{name or '未命名主题'}  ({count})")
            if selected is not None and batch_id == selected:
                sel_index = len(self._topic_ids) - 1
        if sel_index is None and self._topic_ids:
            sel_index = 0
        if sel_index is not None:
            self._topic_id = int(self._topic_ids[sel_index])
            try:
                self.topic_list.selection_clear(0, tk.END)
                self.topic_list.selection_set(sel_index)
                self.topic_list.see(sel_index)
            except tk.TclError:  # pragma: no cover
                pass
        else:
            self._topic_id = None
        self._load_topic()

    def _reload_nodes(self) -> bool:
        """按当前主题从服务重读**最新**词节点 / 总数 / 内容指纹（只读本地库）。

        ``self._nodes`` 只是窗口里的显示副本：每次点击生成、每次核对异步结果、
        每次收到「词条变了」的通知都**当场重新读库**，绝不把陈旧快照发给模型。
        返回窗口可见内容是否真的变了：词条集合 / 内容指纹 / 「本次分析 N 词」的
        上限提示任一变化都算（词条集合变化必然改变指纹）。
        """
        service = self.service
        topic_id = self._topic_id
        if service is None or topic_id is None:
            return False
        try:
            all_nodes, nodes = self._scope_nodes(service, int(topic_id))
            fingerprint = str(service.fingerprint(int(topic_id), nodes))
        except Exception:  # pragma: no cover - 读库失败不该炸窗口，保留当前显示
            log.exception("重读主题词条失败")
            return False
        # 单次分析有硬上限（``MAP_MAX_ENTRIES``）：被截断时必须**明说**
        # 「本次分析 N 词 / 该主题共 M 词」，绝不默默漏掉用户的词表。
        # 用户自己选了子集（C3）时说明的是**子集**：两者都要说清，别让人以为漏了词。
        note_fn = getattr(service, "coverage_note", None)
        try:
            note = str(note_fn(int(topic_id), len(nodes)) or "") if callable(note_fn) else ""
        except Exception:  # pragma: no cover - 统计失败不影响出图
            note = ""
        if self._subset_ids and len(nodes) != len(all_nodes):
            note = f"本次只分析选中的 {len(nodes)} 词（该主题共 {len(all_nodes)} 词）"
        changed = (fingerprint != str(self._fingerprint)
                   or tuple(int(n.entry_id) for n in nodes)
                   != tuple(int(n.entry_id) for n in self._nodes)
                   or note != str(getattr(self, "_coverage_note", "") or ""))
        self._all_nodes = all_nodes
        self._nodes = nodes
        self._labels = {
            int(node.entry_id): label
            for node, label in zip(nodes, unique_labels([n.term for n in nodes]))
        }
        self._fingerprint = fingerprint
        self._coverage_note = note
        self._reload_manual_relations()
        # 固定位置（F2）与 AI 边黑名单（F3）都跟着主题走：换主题 / 词条变了
        # 一律重读，免得把上一个主题的固定位置或屏蔽记录带到这一张图上。
        self._reload_pins()
        self._reload_blocks()
        self._fill_entry_list(all_nodes)
        return changed

    def _scope_nodes(self, service, topic_id: int) -> tuple[list, list]:
        """按当前主题读**全部**词节点，再按 ``self._subset_ids`` 裁出本次分析的范围。

        子集是窗口自己的状态（不入库）：AI 结果按子集算指纹，与「全部」是两份不同的
        分析结果 —— 切回全部会重新生成（数据不丢，只是缓存指纹不同）。
        """
        all_nodes = list(service.nodes_for_topic(int(topic_id)))
        if not self._subset_ids:
            return all_nodes, all_nodes
        keep = {int(eid) for eid in self._subset_ids}
        nodes = [node for node in all_nodes if int(node.entry_id) in keep]
        #: 库里已经不存在的词条自动从子集里掉出去（删词之后不留幽灵选中）
        self._subset_ids = {int(node.entry_id) for node in nodes}
        return all_nodes, nodes

    def apply_only_ids(self, only_ids, *, full_ids=None) -> bool:
        """把「只分析这些词」的范围定为主界面勾选的那些词（K1，空 = 全部）。

        返回范围是否真的变了。**只在这一刻定范围**：之后用户在图上点「只分析选中的
        词 / 整个主题」都是他自己的选择，不会被反复覆盖；下一次在主界面重新点
        「导图」时再按当时的勾选重定一次。

        ``full_ids`` = 主界面那一组词的全部 id：**全勾与不勾是一回事**（都画整个
        主题），不合并的话「一个字都没取消」也会被当成子集，白白多算一份缓存、
        还在图上多一句「本次只分析选中的 N 词」的废话。
        """
        wanted = {int(i) for i in (only_ids or []) if int(i)}
        every = {int(i) for i in (full_ids or []) if int(i)}
        if self._topic_id is None:
            #: 窗口刚建、主题还没挑（:meth:`refresh` → :meth:`_load_topic` 还没跑）：
            #: 现在算不出交集，先记下来，等 :meth:`_load_topic` 定下主题再套用。
            self._pending_only_ids = (wanted, every)
            return False
        changed = self._set_subset(wanted, full_ids=full_ids)
        self._only_topic_id = self._topic_id if self._subset_ids else None
        return changed

    def _set_subset(self, wanted, *, full_ids=None, empty_note: str = "") -> bool:
        """改子集范围（唯一的写入口）：与当前库的节点求交集，空交集绝不生效。

        三条守卫：①库里已经没有的词条自动掉出去（不留幽灵选中）；
        ②勾选的是**别的主题**的词时交集为空 —— 这时必须退回「全部」，
        否则图上会一个词都没有，看起来像这个主题坏了；
        ③``full_ids`` 全被选中时也算「全部」（见 :meth:`apply_only_ids`）。

        主题还没定时**直接返回 False**（不写、不读库）：交集是跟「当前主题的节点」
        求的，连主题都还不知道就没有交集可言 —— 调用方（:meth:`_load_topic`）
        会带着已知的主题再来一次。
        """
        if self._topic_id is None:
            return False
        ids = {int(i) for i in (wanted or []) if int(i)}
        if full_ids:
            every = {int(i) for i in full_ids if int(i)}
            if every and ids >= every:
                ids = set()
        if ids:
            try:
                alive = {int(node.entry_id)
                         for node in self.service.nodes_for_topic(int(self._topic_id))}
            except Exception:  # pragma: no cover - 读库失败就当交集为空（退回全部）
                log.exception("读取主题词条失败")
                alive = set()
            ids &= alive
        new = ids
        if new == self._subset_ids:
            return False
        self._subset_ids = new
        if empty_note and wanted and not new:
            self._set_feedback(empty_note)
        return True

    def _current_fingerprint(self) -> str:
        """**此刻**按当前库 + 当前配置算出的内容指纹（核对异步结果时重新读库）。

        子集分析时指纹必须按**同样的子集**算，否则每份结果都会被判成过期。
        """
        service = self.service
        topic_id = self._topic_id
        if service is None or topic_id is None:
            return str(self._fingerprint)
        try:
            _all_nodes, nodes = self._scope_nodes(service, int(topic_id))
            return str(service.fingerprint(int(topic_id), nodes))
        except Exception:  # pragma: no cover - 读库失败按原指纹处理
            log.exception("核对主题指纹失败")
            return str(self._fingerprint)

    def _drop_expired(self, *, force: bool = False) -> bool:
        """数据 / 配置已变：本地跟上最新词条，清掉过期图与请求关联（零 LLM 请求）。

        ``force=False``（默认，词条变化通知走这条）**只对真实变化生效**：
        :meth:`_reload_nodes` 报告词条 / 内容指纹 / 上限提示都没变时就一个控件都
        不动 —— 普通搜索、别的主题变更触发的列表刷新绝不能把当前图和忙态清掉。
        ``force=True`` 是**已确认过期**的强制作废路径（``_on_generate`` 按新内容
        重新生成、``_reject_stale`` 丢弃迟到结果）：不管重读结果如何都清干净，
        保证旧图不会留在新内容上、旧忙态不会卡住按钮。
        """
        changed = self._reload_nodes()
        if not changed and not force:
            return False
        self._graph = None
        self._selected = None
        self._selected_node = None
        self._pending_token = None
        self._state = "empty" if not self._nodes else "idle"
        self._sync_button()
        self._draw()
        return changed

    def on_entries_changed(self) -> bool:
        """词条新增 / 编辑 / 删除 / 解释完成后的**本地**通知（App 在真实刷新路径里调用）。

        只做三件事：按当前主题重读词条与内容指纹、丢掉过期图与请求关联、给一句
        短状态提示重新生成。**绝不**自动发起任何 LLM 请求；内容没变（例如只是
        搜索框打字 / 别的主题变更触发的列表刷新）时**一个控件都不动**：图、
        在途请求、按钮、点选依据与画布全部原样保留。
        """
        if self._topic_id is None or self.service is None:
            return False
        if not self._drop_expired():
            return False
        if not self._nodes:
            self._set_feedback("该主题还没有词条：先在阅读页解释并记录几个词")
        else:
            self._set_feedback(self._with_coverage(
                f"词条已更新（{len(self._nodes)} 词），点「生成参考关系」按最新内容重新分析"))
        return True

    def _inflight(self, topic_id: int) -> bool:
        """同主题 + **同当前内容指纹**的请求是否已经在途（不重复发、也不误判）。"""
        checker = getattr(self.service, "is_inflight", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(int(topic_id), str(self._fingerprint)))
        except TypeError:  # pragma: no cover - 只接受主题参数的旧实现
            return bool(checker(int(topic_id)))

    def _load_topic(self) -> None:
        """换主题：先看有效缓存 → 立刻显示；否则先画词节点再去生成。"""
        self._selected = None
        self._selected_node = None
        self._pending_token = None
        self._graph = None
        self._nodes = []
        self._labels = {}
        self._fingerprint = ""
        self._coverage_note = ""
        #: 换主题 = 换范围：词条列表不跨主题沿用（人工关系按主题各自读库）。
        #: 子集**通常也清空** —— 例外只有一个：主界面刚把「只画勾选的词」设在本主题上
        #: （见 :meth:`apply_only_ids`），那一次的载入要保住这个子集，否则一打开就被
        #: 这里清成「全部」，用户勾了半天等于没勾。用户在图窗里自己换主题时照旧清空。
        self._all_nodes = []
        pending = getattr(self, "_pending_only_ids", None)
        if pending is not None:
            #: 主界面点「导图」时窗口还没定下主题（构造期），范围先寄存在
            #: :meth:`apply_only_ids` 里 —— 到这里主题已知，套用它。此后这个子集
            #: 就是「本主题的子集」，用户在图窗自己换主题时照旧清空。
            self._pending_only_ids = None
            self._only_topic_id = None
            self._subset_ids = set()
            wanted, every = pending
            self._set_subset(wanted, full_ids=every)
            if self._subset_ids:
                self._only_topic_id = self._topic_id
        elif self._only_topic_id == self._topic_id and self._subset_ids:
            #: 这个子集是**本主题的**（主界面勾选带进来的）：重读主题时保住它。
            #: 注意还要 ``_subset_ids`` 非空：删词之后子集可能已经被裁成空集合了，
            #: 那正是「回到全部」——不拦住的话会被这一支当成「有子集」保住，
            #: 下一轮又变回子集，来回抖。
            self._only_topic_id = self._topic_id
        else:
            self._only_topic_id = None
            self._subset_ids = set()
        self._manual_rels = ()
        self._entry_list_ids = ()
        self._entry_options = []
        self._diag_lines = {}
        # 换主题 = 换内容：镜头状态重置一次（缩放回到 1.0，首开重新自适应一次）。
        # 「刷新」不走这里 —— 词条变化 / 重新生成都保留用户自己调好的缩放与位置。
        self._zoom = 1.0
        self._fit_pending = True
        self._topic_name_text = self._topic_name()
        self._update_evidence()
        if self._topic_id is None:
            self._state = "empty"
            self._sync_button()
            self._draw()
            self._set_feedback("还没有主题：先在阅读页解释并记录几个词")
            return
        service = self.service
        if service is None:
            self._state = "empty"
            self._sync_button()
            self._draw()
            self._set_feedback("关系图服务不可用")
            return
        # 每次打开 / 换主题都**当场读库**：绝不用任何陈旧快照
        self._reload_nodes()
        nodes = self._nodes
        if not nodes:
            self._state = "empty"
            self._sync_button()
            self._draw()
            self._set_feedback("该主题还没有词条：先在阅读页解释并记录几个词")
            return
        cached = service.cached_graph(int(self._topic_id), nodes,
                                      fingerprint=self._fingerprint)
        if cached is not None:
            self._graph = cached
            self._state = "ready"
            self._sync_button()
            self._draw()
            self._set_feedback(self._with_coverage(
                f"已显示缓存：{cached.summary()}{self._model_note(cached)}"))
            return
        self._state = "idle"
        self._sync_button()
        self._draw()                       # 先显示词节点（不是空白画布）
        self._request(force=False)

    # ------------------------------------------------------------- 生成
    def _request(self, *, force: bool) -> None:
        """发起一次生成（唯一入口：打开 / 切主题 / 点按钮）。

        发请求前**再按当前主题读一次库**：``self._nodes`` 只是显示副本，绝不把
        陈旧快照发给模型。同主题 + 同内容的请求已经在途时（例如窗口关掉又打开）
        不重复发，但按钮必须给出**真实**反馈 —— 那份结果回来会直接显示。
        """
        service = self.service
        if service is None or self._topic_id is None:
            return
        if self._pending_token is not None:
            return
        topic_id = int(self._topic_id)
        self._reload_nodes()
        if not self._nodes:
            self._state = "empty"
            self._sync_button()
            self._draw()
            self._set_feedback("该主题还没有词条：先在阅读页解释并记录几个词")
            return
        ok, _msg = service.is_ready()
        if not ok:
            self._state = "nokey"
            self._show_hint(True)
            self._sync_button()
            self._set_feedback(service.unavailable_hint())
            return
        if self._inflight(topic_id):
            # 同主题、同内容的请求已经在途：不重复发网络请求，但按钮不能像
            # 「点了没反应」—— 给出真实状态，它自己的结果回来就直接显示。
            self._state = "loading"
            self._sync_button()
            self._set_feedback(self._with_coverage(
                "正在生成参考关系…（同主题已有在途请求，完成后自动显示）"))
            return
        self._show_hint(False)
        token = service.generate(topic_id, nodes=self._nodes, force=bool(force),
                                 topic_name=self._topic_name_text)
        if token is None:
            return                          # 命中缓存 / 没有可生成内容（结果走回调）
        self._pending_token = int(token)
        self._state = "loading"
        self._sync_button()
        self._set_feedback(self._with_coverage("正在生成参考关系…（先按当前词节点显示）"))

    def _reject_stale(self, token: int, pending: int | None) -> None:
        """过期 / 迟到结果：只清理**它自己**那份忙态，绝不动更新请求的忙态。"""
        if pending is not None and int(token) != int(pending):
            return
        released = self._pending_token is not None or self._state == "loading"
        if released:
            # 这份结果就是「正在生成…」在等的那个：它已经过期，忙态必须放掉，
            # 否则按钮永远卡在「正在生成…」。
            self._pending_token = None
            self._state = "empty" if not self._nodes else "idle"
            self._sync_button()
        if str(self._fingerprint) != self._current_fingerprint():
            # 本地显示确实过期（窗口打开之后库 / 配置变了，通知没赶上）：
            # 当场按最新词条重读并丢掉过期图（已确认过期 → 强制作废）。
            self._drop_expired(force=True)
            self._set_feedback(self._with_coverage(
                "词条或配置已变化，旧结果已丢弃；请点「重新生成」按最新内容分析"))
        elif released:
            self._set_feedback(self._with_coverage(
                "这份生成结果已过期，已丢弃；请点「重新生成」按当前内容分析"))

    def on_map_result(self, payload) -> bool:
        """后台结果回到 UI 线程：按**当前库 + 当前配置 + 请求归属**重新核对后决定。

        返回是否被采纳。三层核对缺一不可：

        1. **主题**：不是当前主题 → 丢弃；
        2. **指纹**：与**此刻重新读库算出来的**指纹不一致（窗口打开后新增 / 编辑 /
           删除词条、改了释义、换了模型配置）→ 丢弃，绝不画进当前图；若这份结果
           正好属于我们**正在等**的那个请求，顺手解除忙态并提示重新生成 ——
           绝不让按钮永远卡在「正在生成…」；
        3. **归属**：还有更新的请求在途时，旧 token 的结果一律不采纳，也**绝不**
           解除那份新请求的忙态（只有它自己的结果回来才作数）。

        窗口已经关掉时结果根本到不了这里（``App._handle_map_result`` 只投给活着的
        窗口），因此迟到的结果既不会串图、也不会复活旧窗口。
        """
        try:
            token, topic_id, fingerprint, status, graph, error = payload
        except (TypeError, ValueError):
            return False
        try:
            token = int(token)
        except (TypeError, ValueError):  # pragma: no cover - 非法 token
            return False
        if self._topic_id is None or int(topic_id) != int(self._topic_id):
            return False
        pending = self._pending_token
        if str(fingerprint) != self._current_fingerprint():
            self._reject_stale(token, pending)
            return False
        if pending is not None and token != int(pending) and token != 0:
            # 更新请求还在途：这份（内容虽旧的）结果与它的忙态无关，一动不动。
            return False
        self._pending_token = None
        self._selected = None
        self._selected_node = None
        if str(status) == "ok" and graph is not None:
            self._graph = graph
            self._state = "ready"
            note = "（缓存）" if str(getattr(graph, "source", "")) == "cache" else ""
            summary = getattr(graph, "summary", None)
            text = summary() if callable(summary) else ""
            self._set_feedback(self._with_coverage(f"{text}{note}{self._model_note(graph)}"))
        else:
            # 错误 / 非法 JSON / schema 错误：**不画伪图**，只保留词节点 + 短状态 + 重试
            self._graph = None
            self._state = "error"
            detail = error.display() if hasattr(error, "display") else str(error or "生成失败")
            self._set_feedback(f"生成失败：{detail}")
        self._update_evidence()
        self._sync_button()
        self._draw()
        return True

    def _on_generate(self) -> None:
        """底部**唯一**入口：没有 Key 时变成内联配置提示，其余情况都是重新生成。

        用户点击 = 明确动作：先按当前主题从服务重读最新词条 / 总数 / 内容指纹，
        再发请求 —— 窗口打开期间新增 / 编辑 / 删除过词条时，用的必须是新内容，
        绝不是窗口刚打开时的旧快照。
        """
        if self._pending_token is not None:
            self._set_feedback(self._with_coverage("正在生成参考关系…（完成后自动显示）"))
            return
        if self._topic_id is None:
            self._set_feedback("先在阅读页解释并记录几个词，再回来生成参考关系")
            return
        service = self.service
        if service is None:
            self._set_feedback("关系图服务不可用")
            return
        if self._reload_nodes():
            self._drop_expired(force=True)  # 内容变了：先丢掉过期图，再按新内容生成
        if not self._nodes:
            self._state = "empty"
            self._sync_button()
            self._draw()
            self._set_feedback("该主题还没有词条：先在阅读页解释并记录几个词，再回来生成")
            return
        ok, _msg = service.is_ready()
        if not ok:
            self._show_hint(True)
            self._sync_button()
            self._set_feedback(service.unavailable_hint())
            self._open_settings()
            return
        self._request(force=True)

    def _open_settings(self) -> None:
        opener = getattr(self.app, "open_settings", None)
        if callable(opener):
            try:
                opener()
                return
            except Exception:  # pragma: no cover - 打开设置失败不影响本窗
                log.exception("打开设置失败")
        self._set_feedback(self.service.unavailable_hint() if self.service else "请在设置里配置 API Key")

    def _show_hint(self, show: bool) -> None:
        """内联配置提示按钮的显示 / 收起（同一个按钮，不新增弹窗）。

        .. note:: 与右下角「操作提示」不是一回事：那个看 :meth:`_build_hint_rows`，
           本方法里的 ``_hint_visible`` 只管缺 Key 时那个内联「去设置」按钮。
        """
        show = bool(show)
        if show == self._hint_visible:
            return
        self._hint_visible = show
        try:
            if show:
                self.btn_settings.pack(side="left", padx=(theme.px(8), 0))
            else:
                self.btn_settings.pack_forget()
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass

    # ------------------------------------------------- 右下角操作提示（可折叠）
    # 用户口径 2026-10-04：「改成可折叠起来的，用户刚刚打开时展开进行提示，过 20 秒
    # 之后淡出折叠。这个背景要是透明的，不要给提示背景板设置颜色。」
    #
    # 三行全展开 → :data:`HINT_EXPAND_MS` 后淡出 → 收成一行 :data:`HINT_COLLAPSED_TEXT`
    # （可点开）。用户自己点开的那次不再自动收（见 :meth:`_show_hint` 的说明）。
    def hint_tint(self) -> str:
        """提示底板 / 文字的「透明」底色 = **画布自己的底色**。

        真透明做不到（见 :meth:`_show_hint` 上面那段说明），于是让提示与画布同色，
        皮上就只剩文字。底色直接问画布要（``cget("bg")``），这样万一以后画布换了
        颜色、提示也不用跟着改；问不到再退回 :data:`theme.BG`（测试替身 / 极简
        Tk 上 ``cget`` 可能不在）。
        """
        if self._hint_tint_override:
            return self._hint_tint_override
        try:
            got = str(self.canvas.cget("bg") or "").strip()
        except Exception:  # pragma: no cover - 极简替身没有 cget
            got = ""
        return got or theme.BG

    def _hint_fade_color(self, color: str, progress: float) -> str:
        """把 ``color`` 按 ``progress``（0 = 原色、1 = 画布底色）混向画布底色。

        tk 没有透明度：淡出只能靠**逐格换文字色**模拟（把字色一格一格挪向底色），
        所以这一步就是那个「一格」。色值自己解析（``theme`` 只给 ``"#rrggbb"``
        字符串，没有拆通道的公开函数），解析不出来就原样返回，绝不抛。
        """
        p = max(0.0, min(1.0, float(progress)))
        try:
            start = (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16))
            end = (int(self.hint_tint()[1:3], 16), int(self.hint_tint()[3:5], 16),
                   int(self.hint_tint()[5:7], 16))
        except (ValueError, IndexError, TypeError):  # pragma: no cover - 非 #rrggbb
            return color
        return "#%02X%02X%02X" % tuple(
            max(0, min(255, round(a + (b - a) * p))) for a, b in zip(start, end))

    def _cancel_hint_fade(self) -> None:
        """取消待执行的淡出 / 折叠定时器（关窗与重新展开都要先取消）。"""
        timer, self._hint_fade_id = self._hint_fade_id, None
        self._hint_fade_base = None
        cancel = getattr(self.win, "after_cancel", None)
        if timer is not None and callable(cancel):
            try:
                cancel(timer)
            except Exception:  # pragma: no cover - 定时器已跑完 / 窗已销毁
                pass

    def _schedule_hint_fade(self, *, expanded: bool) -> None:
        """排下一次淡出（``expanded`` 告诉回调**从什么状态开始淡**）。"""
        self._cancel_hint_fade()
        if self._hint_user_collapsed:
            return
        after = getattr(self.win, "after", None)
        if not callable(after):  # pragma: no cover - 极简替身
            return
        delay = HINT_EXPAND_MS if expanded else HINT_FADE_STEP_MS
        try:
            self._hint_fade_id = after(delay, self._on_hint_fade_tick)
        except Exception:  # pragma: no cover - 窗已销毁
            self._hint_fade_id = None

    def _on_hint_fade_tick(self) -> None:
        """定时器回调：把展开的三行逐格混向底色，最后一格收成一行。

        第一次进来是「已经显示满 :data:`HINT_EXPAND_MS`」那一拍（回调是
        :meth:`_schedule_hint_fade` 按 ``expanded=True`` 排的），之后就每
        :data:`HINT_FADE_STEP_MS` 一拍，共 :data:`HINT_FADE_STEPS` 拍。
        """
        if not self.win.winfo_exists():  # pragma: no cover - 关窗后不许再碰控件
            return
        rows = list(getattr(self, "hint_rows", ()) or ())
        if not rows:  # pragma: no cover - 还没建出来
            self._hint_fade_id = None
            return
        base = self._hint_fade_base or rows[0].cget("fg")
        self._hint_fade_base = base
        p = min(1.0, self._hint_fade_progress() + 1.0 / float(HINT_FADE_STEPS))
        self._hint_fade_set_progress(p)
        for row in rows:
            try:
                row.configure(fg=self._hint_fade_color(base, p))
            except Exception:  # pragma: no cover - 控件已销毁
                pass
        if p >= 1.0:
            self._hint_fade_id = None
            self._hint_expanded = False
            self._build_hint_rows(expanded=False)
            return
        self._hint_fade_id = self.win.after(HINT_FADE_STEP_MS, self._on_hint_fade_tick)

    def _hint_fade_progress(self) -> float:
        """当前淡出进度（0 = 全展开、1 = 已收成一行）。"""
        return float(getattr(self, "_hint_fade_level", 0.0) or 0.0)

    def _hint_fade_set_progress(self, value: float) -> None:
        self._hint_fade_level = max(0.0, min(1.0, float(value)))

    def _restart_hint_fade(self) -> None:
        """回到「刚打开」的样子：三行全展开、计时重来（用户点折叠行时用）。

        **不碰** :attr:`_hint_user_collapsed`：那个闩是「用户自己要看，别再自动收」，
        正是 :meth:`_on_hint_rows_click` 刚设上的那一个 —— 这里若把它清掉，
        就等于「用户一点开、定时器又把它收走」，点了像没点。
        """
        if not self._hint_expanded:
            self._build_hint_rows(expanded=True)
        self._schedule_hint_fade(expanded=True)

    def _build_hint_rows(self, *, expanded: bool) -> None:
        """画提示的行：展开 = 三行说明（右对齐），折叠 = 一行 :data:`HINT_COLLAPSED_TEXT`。

        两种状态用同一套 Label（先 ``pack_forget`` 再重建），折叠那一行绑左键点开。
        """
        for child in list(self.hint_inner.winfo_children()):
            try:
                child.destroy()
            except tk.TclError:  # pragma: no cover - 控件已销毁
                pass
        self._hint_expanded = bool(expanded)
        self._hint_fade_base = None
        self._hint_fade_set_progress(0.0)
        tint = self.hint_tint()
        self.hint_rows: list = []
        #: 每一行的「原色」：淡出中会被改掉，重新展开时要按它还原。
        self.hint_row_fg: list = []
        if expanded:
            fonts_fg = (theme.TEXT_MUTED,) + (theme.TEXT_FAINT,) * (len(HINT_LINES) - 1)
            for index, line in enumerate(HINT_LINES):
                last = index == len(HINT_LINES) - 1
                label = tk.Label(self.hint_inner, text=line, bg=tint,
                                 fg=fonts_fg[index], font=theme.font(8),
                                 anchor="e", justify="right")
                label.pack(side="top", anchor="e",
                           pady=(0, 0 if last else theme.px(HINT_ROW_GAP)))
                self.hint_rows.append(label)
                self.hint_row_fg.append(fonts_fg[index])
            return
        label = tk.Label(self.hint_inner, text=HINT_COLLAPSED_TEXT, bg=tint,
                         fg=theme.TEXT_FAINT, font=theme.font(8),
                         anchor="e", justify="right", cursor="hand2")
        label.pack(side="top", anchor="e")
        try:
            label.bind("<Button-1>", self._on_hint_rows_click)
        except (tk.TclError, TypeError):  # pragma: no cover - 极简替身没有 bind
            pass
        self.hint_rows.append(label)
        self.hint_row_fg.append(theme.TEXT_FAINT)

    def _on_hint_rows_click(self, _event=None) -> None:
        """点折叠后那一行 = 再展开一次；这是用户自己要看的，**不再自动收**。"""
        self._hint_user_collapsed = True
        self._restart_hint_fade()

    def _sync_button(self) -> None:
        """单一按钮的文案与可用性（生成 / 重新生成 / 重试 / 正在生成）。"""
        if self._pending_token is not None or self._state == "loading":
            text, enabled = "正在生成…", False
        elif self._state == "error":
            text, enabled = "重试", True
        elif self._graph is not None and getattr(self._graph, "relations", ()):
            text, enabled = "重新生成", True
        else:
            text, enabled = "生成参考关系", True
        if self._topic_id is None or not self._nodes:
            text, enabled = "生成参考关系", False
        try:
            self.btn_generate.configure(text=text)
            self.btn_generate.set_enabled(enabled)
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass

    # ------------------------------------------------------------- 主题切换
    def _on_topic_select(self, _event=None) -> None:
        index = self.topic_list.curselection()
        if not index:
            return
        try:
            topic_id = int(self._topic_ids[index[0]])
        except (IndexError, AttributeError, ValueError):  # pragma: no cover
            return
        if topic_id == self._topic_id:
            return
        self._topic_id = topic_id
        self._load_topic()

    # ------------------------------------------------------------- 依据
    def _rel_text(self, rel) -> str:
        """点选连线后的依据行：类型 / 方向 / 依据 + 证据片段与**它的来源**。

        来源标注只区分「模型抄的是哪一份材料」：阅读页上下文（取词时读到的页面文字）
        还是本工具已存释义（本工具 / 模型自己写的）。材料送模型前会被截断，因此这里
        **不**把它称作「权威原文引文」；显示按 :data:`EVIDENCE_SHOWN` 截断时补一句
        「显示截断」，绝不把截断过的串说成逐字全文。
        """
        src = str(self._labels.get(int(rel.src_entry_id), "（不在本主题）"))
        dst = str(self._labels.get(int(rel.dst_entry_id), "（不在本主题）"))
        if is_manual(rel):
            #: 人工关系没有「证据片段」也不该假装有：说明它是你自己的判断（C6）
            note = str(getattr(rel, "note", "") or getattr(rel, "reason", "") or "").strip()
            return (f"{MANUAL_LABEL_PREFIX}{rel.rel_type}：{src} → {dst}\n"
                    f"{MANUAL_KIND_NOTE}"
                    + (f"\n你写的依据：{note}" if note else ""))
        reason = str(getattr(rel, "reason", "") or "").strip() or "（未给依据）"
        evidence = str(getattr(rel, "evidence", "") or "").strip() or "（未给片段）"
        nodes = {int(getattr(node, "entry_id", 0) or 0): node for node in self._nodes}
        label, verbatim = evidence_basis(evidence, nodes.get(int(rel.src_entry_id)),
                                         nodes.get(int(rel.dst_entry_id)), metrics_for())
        truncated = len(evidence) > max(1, int(EVIDENCE_SHOWN))
        shown = (evidence[:EVIDENCE_SHOWN] + EVIDENCE_ELLIPSIS if truncated
                 else evidence)
        note = "" if verbatim else "（未能逐字对上材料）"
        if truncated:
            note += f"（显示截断，库里存的是完整 {len(evidence)} 字）"
        return (f"{rel.rel_type}：{src} → {dst}\n"
                f"依据：{reason}　证据（{label}）：「{shown}」{note}")

    def _node_text(self, entry_id: int) -> str:
        """点词卡后的依据行：这个词的关系 / 为什么它是孤立词。

        图里出现「孤立词」时，用户最想知道的就是**为什么**。判定记录（每条候选的
        结果 + 核对给的理由）随图一起落库，所以这里能逐条说清；老缓存没有判定记录
        时如实说「没有留下判定记录」，绝不编一个理由。三种情况分别对应
        ``ISOLATED_WHY``（有该词的候选）/ ``ISOLATED_NONE``（有记录但没这个词）/
        ``ISOLATED_UNKNOWN``（整张图都没有记录）。
        """
        entry_id = int(entry_id)
        name = str(self._labels.get(entry_id, "（不在本主题）"))
        rels = [rel for rel in self.relations_for_draw()
                if int(rel.src_entry_id) == entry_id or int(rel.dst_entry_id) == entry_id]
        lines = [f"{name}：{len(rels)} 条已画出的参考关系" if rels else f"{name}：孤立词"]
        for rel in rels:
            src = str(self._labels.get(int(rel.src_entry_id), "（不在本主题）"))
            dst = str(self._labels.get(int(rel.dst_entry_id), "（不在本主题）"))
            mark = MANUAL_LABEL_PREFIX if is_manual(rel) else ""
            lines.append(f"　· {src} --{mark}{rel.rel_type}--> {dst}")
        if rels:
            if any(is_manual(rel) for rel in rels):
                lines.append(f"（带「{MANUAL_LABEL_PREFIX}」的是你自己加的关系，"
                             f"不受重新生成影响；其余关系都过了独立核对）")
            else:
                lines.append("（点连线看这条的依据与证据片段；上面的关系都过了独立核对）")
            return "\n".join(lines)

        graph = self._graph
        rows = tuple(getattr(graph, "verdicts_for", lambda _i: ())(entry_id)) if graph else ()
        if not rows:
            #: 三种情况说三种话，**绝不编理由**：图里根本没有判定记录（旧缓存 / 这次
            #: 一条候选都没提）说「没有留下判定记录」；有记录但这个词一条都没有，
            #: 就如实说「可能是没提，也可能是本地校验筛掉了」。
            has_records = bool(getattr(graph, "verdicts", ()) or ()) if graph is not None else False
            lines.append(ISOLATED_NONE if has_records else ISOLATED_UNKNOWN)
            return "\n".join(lines)
        lines.append(ISOLATED_WHY)
        for row in rows:
            src = str(self._labels.get(int(row.get("src", 0) or 0), "（不在本主题）"))
            dst = str(self._labels.get(int(row.get("dst", 0) or 0), "（不在本主题）"))
            label = VERDICT_LABELS.get(str(row.get("verdict") or ""), "核对没有给出判定")
            note = str(row.get("note") or "").strip()
            lines.append(f"　· {src} --{row.get('type', '')}--> {dst}　{label}"
                         + (f"：{note}" if note else ""))
        lines.append("（这些候选不画进图：核对不确定或认为不成立的关系，只能当 AI 参考）")
        return "\n".join(lines)

    def _update_evidence(self) -> None:
        if self._selected_node is not None:
            text = self._node_text(int(self._selected_node))
        elif self._selected is None:
            text = (f"点击一条连线看依据，或点一个词看它的关系 / 为什么是孤立词"
                    f"（{MAP_TAG}，仅供参考）")
        else:
            text = self._rel_text(self._selected)
        try:
            self.evidence.configure(text=text)
        except tk.TclError:  # pragma: no cover
            pass

    def _highlight_edges(self) -> None:
        """加粗**当前选中**的连线；选中一个词时加粗它自己的那些连线。

        基准线宽对每一条线都是同一个值（用户口径：连线的粗细 / 颜色全图统一，
        人工关系不再更粗），选中时在这个基准上加两像素。
        """
        base = max(1, theme.px(EDGE_WIDTH))
        node_id = self._selected_node
        for key, item in list(self._edge_items.items()):
            rel = self._edge_rels.get(key)
            picked = rel is self._selected
            if not picked and node_id is not None and rel is not None:
                picked = (int(getattr(rel, "src_entry_id", 0)) == int(node_id)
                          or int(getattr(rel, "dst_entry_id", 0)) == int(node_id))
            width = (base + 2) if picked else base
            try:
                self.canvas.itemconfigure(item, width=width)
            except (tk.TclError, AttributeError):  # pragma: no cover - 已重画 / 替身
                continue

    def _on_canvas_click(self, event) -> None:
        """**左键**点词卡 / 连线 → 显示依据；点空白 → 收起依据。

        判定顺序是「**先词后线**」：词卡比连线粗，一条贴着卡片边缘绕行的折线不该
        把点词的手势抢走。点词显示这个词已画出的关系，孤立词则逐条列出被核对否掉
        的候选与理由（这才是「为什么它是孤立词」的答案）。

        右键拖动平移走的是 Button-3 / B3-Motion，根本不会进这里；只有**右键还
        按着**的时候左键才被 :meth:`_pan_guard` 挡掉。右键松手之后的第一下左键
        就是一次正常点击：点线要能选中、点空白要能收起，**绝不吞掉**。
        """
        if self._pan_guard():
            return
        try:
            screen_x, screen_y = float(event.x), float(event.y)
        except (AttributeError, TypeError, ValueError):  # pragma: no cover
            return
        layout = self._layout
        if layout is None:
            return
        # 真实 Tk 的 ``event.x / y`` 是**控件坐标**：画布滚过 / 拖过之后必须经
        # ``canvasx`` / ``canvasy``（已含视图偏移）换算成画布坐标再命中判定，
        # 否则平移之后点连线会点到别处。
        x = self._canvas_coord("canvasx", screen_x)
        y = self._canvas_coord("canvasy", screen_y)
        node = layout.node_at(x, y, pad=theme.px(NODE_HIT_PAD))
        edge = None if node is not None else layout.edge_at(x, y, tol=theme.px(HIT_TOL))
        self._selected_node = int(node.entry_id) if node is not None else None
        self._selected = edge.rel if edge is not None else None
        self._update_evidence()
        self._highlight_edges()

    def _on_canvas_double_click(self, event) -> None:
        """双击**词卡** = 回主界面打开这条词条（C4）；双击**连线** = 改 / 删（F3）。

        判定顺序仍是「先词后线」：鼠标压在一张卡上时，双击永远算「打开这个词」，
        绝不会因为在卡片边缘碰到一条线就跳去改关系。点在空白处什么都不做 ——
        「双击」是个明确的动作，不该在没点中东西的时候乱开窗口。
        """
        if self._pan_guard():
            return
        layout = self._layout
        if layout is None:
            return
        try:
            x = self._canvas_coord("canvasx", float(event.x))
            y = self._canvas_coord("canvasy", float(event.y))
        except (AttributeError, TypeError, ValueError):  # pragma: no cover
            return
        node = layout.node_at(x, y, pad=theme.px(NODE_HIT_PAD))
        if node is None:
            edge = layout.edge_at(x, y, tol=theme.px(HIT_TOL))
            if edge is not None:
                self._edit_edge(edge)
            return
        self._selected = None
        self._selected_node = int(node.entry_id)
        self._update_evidence()
        self._highlight_edges()
        self._open_entry(int(node.entry_id))

    def _edit_edge(self, edge) -> None:
        """双击一条连线：人工关系 → 打开编辑；AI 关系 → 打开「这条不对吗？」（F3）。"""
        rel = getattr(edge, "rel", edge)
        if is_manual(rel):
            self.open_relation_editor(relation=rel)
            return
        self._selected = rel
        self._selected_node = None
        self._update_evidence()
        self._highlight_edges()
        self.open_edge_dialog(edge)

    # ------------------------------------ 拖拽（Alt+左键建关系 / 左键摆位置）
    def _forget_gesture(self) -> None:
        """把「正在进行中的手势」整个忘掉：不写库、不提示、卡片留在原处。

        三处用它：①一轮没收到松手就结束之后，下一次按下时自愈；②松手落在画布**外**
        （只有顶层窗口那条兜底绑定收得到）；③按 ``<Escape>`` 主动放弃。
        """
        self._clear_link_line()
        self._link_from = None
        self._drag_node = None
        self._drag_items = []
        self._drag_last = None
        self._drag_target = None
        self._bg_pan_last = None
        self._press_xy = None
        self._drag_started = False

    def _on_foreign_release(self, event) -> None:
        """顶层窗口上的松手兜底：画布自己没收到的那次松手，在这里收尾。

        松手落在画布**里**时，画布那条 ``<ButtonRelease-1>`` 先把状态清干净了，
        这里再跑一次是幂等的（手上没有手势就什么都不做）。落在画布**外**时不补
        完手势，直接放弃：位置已经不在画布坐标系里，硬落库只会把卡片钉到莫名其
        妙的地方（「松在画布外 = 取消」）。
        """
        if (self._link_from is None and self._drag_node is None
                and self._bg_pan_last is None):
            return
        self._forget_gesture()
        self._set_feedback("手势已取消：鼠标松开的位置不在图上")
        self._draw(force=True)

    def _on_escape(self, _event=None) -> None:
        """``<Escape>``：放弃正在进行的手势（临时线删掉、卡片留在原处）。"""
        if self._link_from is None and self._drag_node is None:
            return
        self._forget_gesture()
        self._set_feedback("已取消")
        self._draw(force=True)

    def _alt_held(self, event) -> bool:
        """这一下是不是「按住 Alt」：**只看** ``event.state`` 的 Alt 位（见 :data:`ALT_MASK`）。

        真鼠标实测（本机 Tk）：不按任何键 ``state=0x8``、按住 Alt ``state=0x20008``。
        这里**不留**任何跨事件的标志位 —— 标志位要靠松手事件复位，而松手事件可能
        根本收不到，一残留就把之后的普通左键统统读成建关系（画布随之彻底不响应）。
        """
        try:
            return bool(int(getattr(event, "state", 0) or 0) & ALT_MASK)
        except (TypeError, ValueError):  # pragma: no cover - 替身事件
            return False

    def _over_slop(self, event) -> bool:
        """鼠标离按下点够不够远（屏幕像素，见 :data:`DRAG_SLOP`）。

        比的是**屏幕**坐标而不是画布坐标：画布坐标被缩放乘过，缩到 40% 时
        「4 个画布单位」只有一两个屏幕像素，阈值会形同虚设。
        """
        if self._press_xy is None:
            return True
        try:
            dx = float(event.x) - float(self._press_xy[0])
            dy = float(event.y) - float(self._press_xy[1])
        except (AttributeError, TypeError, ValueError):  # pragma: no cover - 替身事件
            return True
        return (dx * dx + dy * dy) >= (DRAG_SLOP * DRAG_SLOP)

    def _on_canvas_press_alt(self, event) -> None:
        """``<Alt-Button-1>``：按着 Alt 按下的左键。

        这条绑定**命中本身就是证据**（用户确实按着 Alt 按下的左键），所以直接把
        「这一下是建关系」交给 :meth:`_on_canvas_press` —— 这里**不留任何跨事件的
        标志位**（标志位要等松手才清，松手收不到就永久残留，之后不按 Alt 的左键
        全被读成建关系）。
        """
        self._on_canvas_press(event, alt=True)

    def _on_canvas_press(self, event, *, alt: bool | None = None) -> None:
        """左键按下：**按住 Alt** = 从这张卡拖一条关系；空白处 = 平移整张图；否则 = 抓卡片。

        用户口径（2026-10-04，照 ProjectGraph 的手感）：**左键就是拖** —— 抓卡片
        挪卡片、抓空白挪整张图；**按住 Alt 才切换成建关系**。卡片上**没有**任何小
        圆点状的连接点，要不要建关系只看 Alt。

        ``alt``：由 :meth:`_on_canvas_press_alt` 显式传入 True（那条绑定命中 =
        按着 Alt）；为 None 时按 :meth:`_alt_held` 判（看 ``event.state`` 的 Alt 位）。

        三条路径都只在松手时才写库，而且**都要先真的拖动**（:data:`DRAG_SLOP`）——
        按一下不动就是一次普通单击（选中 / 看依据由 ``<Button-1>`` 那条绑定负责），
        不会误固定卡片、也不会误建关系、更不会把图挪走。「点空白收起依据」的老行为
        因此原样保留（:meth:`_on_canvas_click` 是另一条独立绑定）。
        """
        if self._pan_guard():
            return
        if self._link_from is not None or self._drag_node is not None:
            # 上一轮手势没有正常收尾（松手落在画布外、窗口失焦、菜单弹走…）：**自愈**。
            # 新一轮左键按下本身就说明上一轮早就结束了 —— 在这里 early-return 的话
            # 画布会永久卡死：用户 2026-10-05 拍屏里那两条「没有终点的长线」就是
            # 这么留在画布上的。
            self._forget_gesture()
        layout = self._layout
        if layout is None:
            return
        try:
            x = self._canvas_coord("canvasx", float(event.x))
            y = self._canvas_coord("canvasy", float(event.y))
        except (AttributeError, TypeError, ValueError):  # pragma: no cover
            return
        node = layout.node_at(x, y, pad=theme.px(NODE_HIT_PAD))
        self._press_xy = self._event_xy(event)
        self._drag_started = False
        held = bool(alt) if alt is not None else self._alt_held(event)
        if node is None:
            # 空白处按下：不碰卡片、不建关系 —— 左键拖动 = 平移整张图（本轮新增）。
            # 「按住 Alt 在空白处按下」不算平移：那多半是想建关系（起点选错了），
            # 让它什么都不做比偷偷把图挪走更不容易出事。
            self._drag_node = None
            self._link_from = None
            self._bg_pan_last = None if held else self._event_xy(event)
            return
        self._bg_pan_last = None
        if held:
            self._link_from = (int(node.entry_id), float(x), float(y))
            self._drag_node = None
            self._link_item = None
            return
        self._drag_node = (int(node.entry_id), float(x) - float(node.x), float(y) - float(node.y))
        #: 要搬的图元**在这一刻认好**：拖动期间按上一次落点算增量，卡片才会
        #: 严格跟着鼠标走（见 :meth:`_move_node_items`）。
        self._drag_items = self._node_items(int(node.entry_id))
        self._drag_last = (float(node.x), float(node.y))

    @staticmethod
    def _event_xy(event) -> tuple[float, float] | None:
        """事件的屏幕坐标（取不到就 None：极简替身只给 x / y 时也不会炸）。"""
        try:
            return (float(event.x), float(event.y))
        except (AttributeError, TypeError, ValueError):  # pragma: no cover
            return None

    def _on_canvas_motion(self, event) -> None:
        """左键拖动中：平移整张图（抓的是空白）/ 画临时线（Alt 建关系）/ 搬卡片。

        拖动期间**只动画面，不动数据**：平移直接滚视图，卡片本体与文字跟着鼠标走，
        贴着这张卡的连线端头也跟着走（:meth:`_follow_edges`，只写 ``coords``、不重排队），
        整张图的重画与走线重算留到松手（半路重画整张图要 15 ms 一帧，会卡、也会一直闪）。
        位移没过 :data:`DRAG_SLOP` 之前什么都不做 —— 那样单击才不会被当成拖动。
        """
        if self._pan_guard():
            return
        if self._bg_pan_last is not None:
            self._pan_by_drag(event)
            return
        if self._link_from is None and self._drag_node is None:
            return
        if not self._drag_started:
            if not self._over_slop(event):
                return
            self._drag_started = True
            if self._link_from is not None:
                self._set_feedback(
                    "按住 Alt 拖到另一张词卡上松开 = 建立关系（松开在空白处 = 取消）")
        try:
            x = self._canvas_coord("canvasx", float(event.x))
            y = self._canvas_coord("canvasy", float(event.y))
        except (AttributeError, TypeError, ValueError):  # pragma: no cover
            return
        if self._link_from is not None:
            start_x, start_y = self._link_from[1], self._link_from[2]
            # 先探测 ``find_withtag``（测试替身画布没有这个方法）：有就确认那条线还在，
            # 没有就直接挪位置 —— 否则替身上会一直新建不出线来。
            needs_new = self._link_item is None
            finder = getattr(self.canvas, "find_withtag", None)
            if not needs_new and callable(finder):
                try:
                    needs_new = not finder(self._link_item)
                except (tk.TclError, TypeError):  # pragma: no cover - 替身画布
                    needs_new = False
            try:
                if needs_new:
                    self._link_item = self.canvas.create_line(
                        start_x, start_y, x, y, fill=EDGE_LABEL_FILL,
                        width=max(1, theme.px(EDGE_WIDTH)),
                        dash=(theme.px(4), theme.px(3)))
                else:
                    self.canvas.coords(self._link_item, start_x, start_y, x, y)
            except (tk.TclError, AttributeError):  # pragma: no cover - 替身画布
                self._link_item = None
            self._drag_target = self._node_under(x, y)
            return
        if self._drag_node is not None:
            entry_id, off_x, off_y = self._drag_node
            self._move_node_items(int(entry_id), x - off_x, y - off_y)

    def _pan_by_drag(self, event) -> None:
        """左键拖**空白处** = 平移整张图（本轮新增，ProjectGraph 式的手感）。

        与右键平移走同一套滚动调用（``xscrollincrement = 1``，1 unit = 1 设备像素），
        区别只在起点：右键用 ``x_root / y_root``，这条用按下那一刻的 ``event.x / y``
        —— 两者都是**屏幕**坐标，因此缩放多少都跟手 1:1。

        没过 :data:`DRAG_SLOP` 之前一动不动：空白处按一下仍然只是「取消选中」。
        """
        if not self._drag_started:
            if not self._over_slop(event):
                return
            self._drag_started = True
            self._set_feedback("平移整张图：松手就停（滚轮缩放 · 右键拖动也一样）")
        point = self._event_xy(event)
        if point is None or self._bg_pan_last is None:  # pragma: no cover - 替身事件
            return
        dx = point[0] - self._bg_pan_last[0]
        dy = point[1] - self._bg_pan_last[1]
        self._bg_pan_last = point
        if dx == 0 and dy == 0:
            return
        try:
            self.canvas.xview_scroll(-int(round(dx)), "units")
            self.canvas.yview_scroll(-int(round(dy)), "units")
        except (tk.TclError, AttributeError):  # pragma: no cover - 极简替身
            pass

    def _on_canvas_release(self, event) -> None:
        """左键松手：落库（建立关系 / 固定位置 / 结束平移），然后重画一次。

        **没拖起来就是一次单击**：清掉内部状态直接返回 —— 不写库、不弹「已固定」，
        也不说「连线取消」（用户只是点了一下，没什么可取消的）。
        """
        if self._pan_guard():
            return
        started = bool(self._drag_started)
        self._press_xy = None
        self._drag_started = False
        if self._bg_pan_last is not None:
            # 左键拖空白 = 平移：松手就停（画面上的东西一个都没动过，没有要写库的）
            self._bg_pan_last = None
            return
        try:
            x = self._canvas_coord("canvasx", float(event.x))
            y = self._canvas_coord("canvasy", float(event.y))
        except (AttributeError, TypeError, ValueError):  # pragma: no cover
            x = y = 0.0
        if self._link_from is not None:
            src_id = int(self._link_from[0])
            self._clear_link_line()
            self._link_from = None
            target = self._node_under(x, y) if started else None
            self._drag_target = None
            if not started:
                return
            if target is None:
                self._set_feedback("连线取消：把线拖到另一张词卡上松开才会建立关系")
                return
            if int(target.entry_id) == src_id:
                self._set_feedback("连线取消：起点和终点是同一个词")
                return
            self._open_relation_editor_for(int(src_id), int(target.entry_id))
            return
        if self._drag_node is None:
            self._drag_last = None
            return
        entry_id, off_x, off_y = self._drag_node
        self._drag_node = None
        self._drag_items = []
        self._drag_last = None
        if not started:
            # 手抖一两像素的「单击」：不算摆位置，也不固定。
            return
        node = self._node_by_id(int(entry_id))
        if node is None:  # pragma: no cover - 卡片已经被删掉了
            return
        # 拖到哪儿就固定在哪儿（相对抓取点的偏移保持住，手指下的位置不跳）
        want_x = x - off_x
        want_y = y - off_y
        ok, message = self.pin_node(int(entry_id), want_x, want_y)
        if not ok:
            self._set_feedback(message)
            self._draw(force=True)
            return
        self._set_feedback("已固定这张卡片的位置（「恢复自动布局」可一键放回去）")
        self._draw(force=True)

    def _node_under(self, x: float, y: float):
        """画布坐标 ``(x, y)`` 下的词卡（没有就 None）。"""
        layout = self._layout
        if layout is None:
            return None
        return layout.node_at(x, y, pad=theme.px(NODE_HIT_PAD))

    def _node_by_id(self, entry_id: int):
        layout = self._layout
        if layout is None:
            return None
        for node in layout.nodes:
            if int(node.entry_id) == int(entry_id):
                return node
        return None

    def _node_items(self, entry_id: int) -> list:
        """这张卡在画布上的图元 id（框 + 文字）：**按下时认一次**，拖动期间照单搬。

        认法是 **Tk tag**（:func:`node_tag` → ``node-24``）：:meth:`_draw` 给框和文字
        打的就是这一个 tag，``canvas.find_withtag`` 一次全取回。

        ★ 2026-10-05 修的真 bug（用户口径：「左键的时候无法拖动 / 拖动不准确」）：
        旧实现遍历 ``self.canvas.item_options`` 按 ``_kind`` 找图元 —— 那个字典
        **只有测试替身有**（``tests/support.py`` 的 ``FakeCanvas``），真 ``tk.Canvas``
        上根本没有，``getattr(..., {})`` 静默给个空字典 ⇒ 这里永远返回 ``[]``
        ⇒ :meth:`_move_node_items` 一路 early-return：**实机上拖动期间卡片纹丝不动**，
        只有松手那一下 :meth:`pin_node` 把卡片瞬移到鼠标处。替身画布自带
        ``item_options``，所以两条拖动测试一直是绿的。
        """
        finder = getattr(self.canvas, "find_withtag", None)
        if not callable(finder):  # pragma: no cover - 没有 find_withtag 就不是画布
            return []
        try:
            return [int(item) for item in finder(node_tag(entry_id))]
        except (tk.TclError, TypeError, ValueError):  # pragma: no cover - 坏图元
            return []

    def _move_node_items(self, entry_id: int, cx: float, cy: float) -> None:
        """把一张卡（框 + 文字）**平滑地**搬到 ``(cx, cy)``；不动数据。

        三条纪律（前两条是历史真 bug，第三条是 2026-10-05 的实机反馈）：

        * 要搬的图元在**按下时**就认好了（:meth:`_node_items`）—— 不能每次按
          「图元中心 == 布局坐标」重新认图元：图元已经被搬走了，第二下就再也认不出来，
          卡片只会动一下；
        * 位移按**上一次落点**算增量 —— 不能每次拿 ``self._layout`` 里的坐标重算位移
          （图元位置已经变了，那样位移会被重复累加，卡片越拖飞得越远）；
        * 搬用 ``canvas.move``，**带上那个 tag**：一次 Tcl 调用同时挪框和文字。
          不读回 ``coords`` 再写回 —— 省掉两趟往返（实测搬 2 个图元：``coords`` 读+写
          0.021 ms，``move`` 0.003 ms），也让「只挪了框、文字留在原地」无从发生。

        拖动期间**不重算连线走线**：整张重画的实测成本是 17.9 ms（10 个节点），
        按 60 Hz 拖就是每秒一千毫秒，必然「一卡一卡的」。连线只做最便宜的跟随，
        见 :meth:`_follow_edges`；松手后由 :meth:`_draw` 恢复正规走线。
        """
        if not self._drag_items:
            return
        last_x, last_y = self._drag_last or (float(cx), float(cy))
        dx, dy = float(cx) - float(last_x), float(cy) - float(last_y)
        if dx == 0.0 and dy == 0.0:
            return
        mover = getattr(self.canvas, "move", None)
        if not callable(mover):  # pragma: no cover - 没有 move 就不是画布
            return
        try:
            mover(node_tag(entry_id), dx, dy)
        except tk.TclError:  # pragma: no cover - 图元已被删掉
            return
        self._drag_last = (float(cx), float(cy))
        self._follow_edges(entry_id, float(cx), float(cy))

    def _follow_edges(self, entry_id: int, cx: float, cy: float) -> None:
        """拖动期间让**这张卡自己的连线**端头跟着走：一头贴在它边上、一头贴在对面边上。

        只做这件事，**不重算走线**（避障绕行那套要重排整张图，17.9 ms 一次）。
        拖动时线被拉成直的、松手后 :meth:`_draw` 恢复绕行 —— 这是常见的做法，
        关键是「线不能留在原地不动」，否则卡片一挪、线就像挂空了一样。
        线段端点顺序按**原来的 src→dst** 给，箭头方向才不会翻过来。
        """
        layout = self._layout
        node = layout.find(entry_id)
        if node is None:  # pragma: no cover - 正在拖的卡一定在布局里
            return
        for edge in layout.edges:
            src, dst, _type = _relation_parts(edge.rel)
            if entry_id not in (src, dst):
                continue
            item = self._edge_items.get(id(edge.rel))
            other = layout.find(dst if entry_id == src else src)
            if item is None or other is None:
                continue
            here = box_edge_point(cx, cy, node.w, node.h, other.x, other.y)
            there = box_edge_point(other.x, other.y, other.w, other.h, cx, cy)
            x1, y1, x2, y2 = (here + there) if entry_id == src else (there + here)
            try:
                self.canvas.coords(item, x1, y1, x2, y2)
            except (tk.TclError, AttributeError):  # pragma: no cover - 图元已删
                continue

    def _clear_link_line(self) -> None:
        """删掉拖拽期间那条临时线（松手 / 取消都要删，绝不留在图上）。"""
        item = self._link_item
        self._link_item = None
        if item is None:
            return
        try:
            self.canvas.delete(item)
        except (tk.TclError, AttributeError):  # pragma: no cover
            return

    def _open_relation_editor_for(self, src_id: int, dst_id: int) -> None:
        """拖拽连线松手：打开人工关系对话框，**两端已经替你选好**。"""
        self.open_relation_editor(src_id=src_id, dst_id=dst_id)

    # ------------------------------------ 人工关系（改进清单 C5 · C6）
    def manual_relation_rows(self) -> list:
        """当前主题已经存下的**人工关系行**（对话框用来列出来；读库失败给空列表）。"""
        topic_id = self._topic_id
        if topic_id is None:
            return []
        try:
            return list(self.db.list_manual_relations(int(topic_id)))
        except Exception:  # pragma: no cover - 老库 / 读失败都不该炸窗口
            log.exception("读取人工关系失败")
            return []

    def _reload_manual_relations(self) -> bool:
        """从本地库重读这个主题的人工关系（不发任何网络请求）。

        人工关系是**用户自己的判断**：它不参与模型输出、也绝不被重新生成覆盖，
        所以每次读库都单独把它捞出来，与 AI 关系区分开。
        """
        manual = manual_relations(self.manual_relation_rows(),
                                  self._all_nodes or self._nodes)
        changed = tuple(manual) != tuple(getattr(self, "_manual_rels", ()) or ())
        self._manual_rels = tuple(manual)
        return changed

    def relations_for_draw(self) -> tuple:
        """画布上要画的关系 = 过了校验的 AI 关系 + 人工关系（**人工优先**）。

        同一对词条既有 AI 判断又有人工标注时，只画人工那条：用户的手动判断优先，
        绝不让同一对端点在图上出现两条互相打脸的关系。人工关系是「你自己加的」，
        不参与模型核对（它没有 evidence，也不该被当成 AI 候选）。

        F3 的**黑名单**在 AI 这边生效：被用户判定「这条不对」的端点对（两个方向）
        一律不画，包括重新生成之后的新候选 —— 否则「删掉不再回来」就是空话。
        """
        ai = tuple(getattr(self._graph, "relations", ()) or ())
        manual = tuple(getattr(self, "_manual_rels", ()) or ())
        blocks = set(getattr(self, "_blocks", set()) or set())
        if blocks:
            ai = tuple(rel for rel in ai
                       if (int(rel.src_entry_id), int(rel.dst_entry_id)) not in blocks)
        if not manual:
            return ai
        blocked: set[tuple[int, int]] = set()
        for rel in manual:
            blocked.add((int(rel.src_entry_id), int(rel.dst_entry_id)))
            if rel.symmetric:
                blocked.add((int(rel.dst_entry_id), int(rel.src_entry_id)))
        kept = tuple(rel for rel in ai
                     if (int(rel.src_entry_id), int(rel.dst_entry_id)) not in blocked)
        return kept + manual

    # ------------------------------------ 卡片位置固定（F2）/ AI 边黑名单（F3）
    def _reload_pins(self) -> bool:
        """从本地库重读「被拖过的卡片位置」（零网络）。

        位置只在**本主题**下有意义；读库失败当「一张都没固定过」——
        自动布局照常画，绝不因为读不到位置就把整张图弄崩。
        """
        pins: dict[int, tuple[float, float]] = {}
        topic_id = self._topic_id
        if topic_id is not None:
            try:
                pins = dict(self.db.list_node_pins(int(topic_id)))
            except Exception:  # pragma: no cover - 老库 / 读失败都不该炸窗口
                log.exception("读取卡片固定位置失败")
                pins = {}
        changed = pins != dict(getattr(self, "_pins", {}) or {})
        self._pins = pins
        return changed

    def _reload_blocks(self) -> bool:
        """重读 AI 关系黑名单（用户判定「这条不对」的端点对，两个方向都在集合里）。"""
        pairs: set[tuple[int, int]] = set()
        topic_id = self._topic_id
        if topic_id is not None:
            try:
                pairs = set(self.db.map_edge_block_pairs(int(topic_id)))
            except Exception:  # pragma: no cover - 老库 / 读失败
                log.exception("读取 AI 关系黑名单失败")
                pairs = set()
        changed = pairs != set(getattr(self, "_blocks", set()) or set())
        self._blocks = pairs
        return changed

    def pinned_positions(self) -> dict[int, tuple[float, float]]:
        """交给布局的固定位置（一份拷贝）。布局只认自己有的词条，多余的自然被忽略。"""
        return dict(getattr(self, "_pins", {}) or {})

    def pin_node(self, entry_id, x, y) -> tuple[bool, str]:
        """把一张词卡固定到 ``(x, y)``（画布坐标里的**卡片中心**）。

        拖动松手时调用：写库 + 记住新位置；下一次绘制（以及下次打开这张图）
        这张卡就落在这里，重新生成只换 AI 关系、不动它。
        """
        if self._topic_id is None:
            return False, "还没有选中主题"
        try:
            eid = int(entry_id)
            self.db.set_node_pin(int(self._topic_id), eid, float(x), float(y))
        except ValueError as exc:
            return False, str(exc)
        except Exception:
            log.exception("固定卡片位置失败")
            return False, "固定卡片位置失败，详见日志"
        self._pins[eid] = (float(x), float(y))
        return True, ""

    def reset_node_pins(self) -> None:
        """「恢复自动布局」（F2）：取消本主题全部固定位置，然后重画一次。"""
        if self._topic_id is None:
            self._set_feedback("还没有选中主题")
            return
        try:
            count = int(self.db.clear_node_pins(int(self._topic_id)))
        except Exception:
            log.exception("恢复自动布局失败")
            self._set_feedback("恢复自动布局失败，详见日志")
            return
        self._pins = {}
        self._drag_node = None
        if count <= 0:
            self._set_feedback("没有固定过位置的卡片：现在就是自动布局")
            return
        self._set_feedback(f"已恢复自动布局（取消固定 {count} 张卡片）")
        self._draw(force=True)

    # ============================================== 模板 / 布局骨架（G1·G2·G4）
    def _read_template(self) -> str:
        """从设置里读布局骨架（未知值一律当「自动」—— 脏配置不该让图画不出来）。"""
        config = getattr(self, "config", None)
        try:
            key = str(config.get("map.template", map_templates.TEMPLATE_AUTO) or "").strip()
        except Exception:  # pragma: no cover - 极简替身 / 坏配置
            key = ""
        return key if map_templates.spec_of(key) is not None else map_templates.TEMPLATE_AUTO

    def template_key(self) -> str:
        """当前用的骨架 key（``auto`` = 默认布局）。"""
        return self._template

    def template_name(self) -> str:
        """当前骨架的中文名（按钮与状态行都写它）。"""
        return map_templates.name_of(self._template)

    def template_hint(self) -> str:
        """状态行用的半句话：模板名 + （配置里存过就提一句）。"""
        if self._template == map_templates.TEMPLATE_AUTO:
            return "模板：自动"
        return f"模板：{self.template_name()}"

    def _sync_template_button(self) -> None:
        """按钮上写着当前骨架（切完立刻能看见自己切成了什么）。"""
        button = getattr(self, "btn_template", None)
        if button is None:  # pragma: no cover - 极简替身
            return
        text = f"模板：{self.template_name()}"
        try:
            if str(button.cget("text") or "") != text:
                button.configure(text=text)
        except (tk.TclError, AttributeError):  # pragma: no cover - 极简替身
            pass
        # 「导图设置…」窗口若开着，顺手把「当前的布局骨架」那一行也刷新 ——
        # 同一份状态在开关两边显示，绝不能一边写着「树状图」、另一边还是「自动」。
        dialog = getattr(self, "_map_settings_dialog", None)
        setter = getattr(dialog, "set_template", None)
        if callable(setter):
            try:
                setter(self._template, self.template_name())
            except Exception:  # pragma: no cover - 窗口已经关了
                log.exception("刷新导图设置窗口失败")

    def local_template_suggestion(self) -> tuple[str, str]:
        """本地规则（零成本）给的模板建议：``(key, 理由)``；没有建议 → ``("", "")``。"""
        try:
            relations = self.relations_for_draw()
        except Exception:  # pragma: no cover - 画布还没建好
            relations = ()
        try:
            return map_templates.suggest(relations)
        except Exception:  # pragma: no cover - 兜底
            log.exception("本地模板建议失败")
            return "", ""

    def open_template_dialog(self) -> None:
        """「模板…」：选一个布局骨架（用户自己挑的那一半）。"""
        key, reason = self.local_template_suggestion()
        suggestion = (key, reason) if key else None
        ask_model = self._ask_model_for_template if self._can_ask_model_template() else None
        from .template_dialog import TemplateDialog      # 懒导入：避免循环导入
        try:
            self._template_dialog = TemplateDialog(
                self.win, current=self._template, suggestion=suggestion,
                on_pick=self.apply_template, on_ask_model=ask_model)
        except Exception as exc:  # pragma: no cover - 极简替身
            log.exception("打开模板对话框失败")
            self._set_feedback(f"打开模板对话框失败：{exc}")

    def _can_ask_model_template(self) -> bool:
        """要不要显示「让模型也看一眼」：设置开着、且服务/模型可用。

        ``is_ready()`` 返回的是 ``(bool, 说明)`` —— 元组恒为真，**必须先拆开**，
        否则没配 Key 时按钮也会冒出来。
        """
        config = getattr(self, "config", None)
        try:
            enabled = bool(config.get_bool("map.template_ask_model", False))
        except Exception:  # pragma: no cover - 极简替身
            enabled = False
        if not enabled:
            return False
        service = getattr(self, "service", None)
        ready = getattr(service, "is_ready", None)
        if not callable(ready):
            return False
        try:
            ok = ready()
        except Exception:  # pragma: no cover - 极简替身
            return False
        if isinstance(ok, tuple):
            ok = bool(ok[0])
        return bool(ok)

    def _model_template_choices(self) -> tuple[dict, ...]:
        """给模型看的骨架清单（不含 auto：本地规则已经兜住「什么都不套」）。"""
        out: list[dict] = []
        for spec in map_templates.TEMPLATES:
            if spec.key == map_templates.TEMPLATE_AUTO or spec.placer is None:
                continue
            out.append({"key": spec.key, "name": spec.name,
                        "summary": spec.summary, "fit": spec.fit})
        return tuple(out)

    def _ask_model_for_template(self, dialog) -> None:
        """把关系摘要交给模型，让它回一个模板 id（失败就静默用本地规则）。

        ``pick_template`` 的回调在**后台线程**里跑：这里用 ``after(0, …)`` 送回
        UI 线程再碰控件 —— worker 线程直接动 Tk 会崩。
        """
        service = getattr(self, "service", None)
        picker = getattr(service, "pick_template", None)
        if not callable(picker):
            dialog.model_unavailable("这个版本没有接上模型挑模板")
            return
        labels = dict(self._labels or {})
        for node in list(self._nodes or ()):       # 兜底：子集里也要有词名
            labels.setdefault(int(getattr(node, "entry_id", 0) or 0),
                              str(getattr(node, "term", "") or ""))
        pairs_builder = getattr(service, "template_pairs", None)
        if callable(pairs_builder):
            pairs = pairs_builder(self.relations_for_draw(), labels)
        else:  # pragma: no cover - 极简替身
            pairs = [{"source": str(labels.get(int(r.src_entry_id), r.src_entry_id)),
                      "target": str(labels.get(int(r.dst_entry_id), r.dst_entry_id)),
                      "type": str(r.rel_type or "")} for r in self.relations_for_draw()]
        if not pairs:
            dialog.model_unavailable("这张图还没有关系，没什么可挑的")
            return

        def show(key, reason="", error="") -> None:
            """后台线程 → UI 线程：只有这里才许碰控件。"""
            def apply() -> None:
                if error or not key:
                    dialog.model_unavailable(error or "模型没有给出可用的排法")
                    if error:
                        self._set_feedback(f"让模型挑模板没成功（已用本地规则）：{error}")
                    return
                dialog.apply_model_suggestion(str(key), str(reason or ""))
            try:
                self.win.after(0, apply)
            except (tk.TclError, AttributeError):  # pragma: no cover - 窗口没了
                log.debug("导图窗口已销毁，丢弃模板建议", exc_info=True)

        try:
            started = picker(pairs, topic=self._topic_name_text,
                             choices=self._model_template_choices(), callback=show)
        except Exception as exc:  # pragma: no cover - 调用方自己兜底
            log.exception("让模型挑模板失败")
            self._set_feedback(f"让模型挑模板失败：{exc}")
            return
        if not started:
            dialog.model_unavailable("没配好 API Key，或者服务正忙")

    def apply_template(self, key) -> bool:
        """切到一个模板（G4：有固定位置就先问一句，确认后取消固定再重画）。

        切模板**只改摆法**：关系、筛选、人工关系、黑名单、导出格式都不动 ——
        所以这里既不碰缓存也不重新请求模型，直接按同一份关系重排一次。
        """
        wanted = str(key or "").strip()
        spec = map_templates.spec_of(wanted)
        if spec is None:
            self._set_feedback(f"不认识的模板名：{wanted or '（空）'}")
            return False
        if wanted == self._template:
            self._set_feedback(f"现在用的就是「{spec.name}」")
            return False
        cleared = 0
        if self._pins:
            cleared = len(self._pins)
            try:
                yes = messagebox.askyesno(
                    "模板",
                    f"切到「{spec.name}」会取消你固定过的 {cleared} 张卡片，"
                    "它们会按新骨架重新排。\n继续吗？",
                    parent=self.win)
            except tk.TclError:  # pragma: no cover - 极简替身
                yes = True
            if not yes:
                self._set_feedback("没有切换模板：先按需要保留现在的位置")
                return False
            try:
                if self._topic_id is not None:
                    self.db.clear_node_pins(int(self._topic_id))
            except Exception:  # pragma: no cover - 读失败也不该挡住切换
                log.exception("切换模板时取消固定位置失败")
            self._pins = {}
        self._template = wanted
        config = getattr(self, "config", None)
        try:
            config.set("map.template", wanted)
        except Exception:  # pragma: no cover - 存不下也要能看这一次
            log.exception("保存模板设置失败")
        tail = f"（顺带取消了 {cleared} 张卡片的固定）" if cleared else ""
        self._set_feedback(f"已切到「{spec.name}」：{spec.summary}{tail}")
        self._draw(force=True)
        return True

    def block_ai_edge(self, edge) -> tuple[bool, str]:
        """把这条 AI 关系标成「不对」（F3）：以后不再画它，重新生成也不会回来。"""
        if self._topic_id is None:
            return False, "还没有选中主题"
        rel = getattr(edge, "rel", edge)
        try:
            src, dst = int(rel.src_entry_id), int(rel.dst_entry_id)
        except (AttributeError, TypeError, ValueError):
            return False, "这条关系没有可用的两端"
        if src == dst:
            return False, "关系两端不能是同一个词条"
        # 备注只给界面看：类型名 + 模型给的证据片段（**不改判定**）。
        label = str(getattr(rel, "rel_type", "") or getattr(rel, "label", "") or "")
        reason = str(getattr(rel, "evidence", "") or "")
        try:
            self.db.block_map_edge(int(self._topic_id), src, dst, label=label, reason=reason)
        except ValueError as exc:
            return False, str(exc)
        except Exception:
            log.exception("屏蔽 AI 关系失败")
            return False, "屏蔽 AI 关系失败，详见日志"
        self._reload_blocks()
        self._selected = None
        self._draw(force=True)
        return True, "已标为不对：这条关系不再画出来（重新生成也不会回来）"

    def unblock_edge_pair(self, src, dst) -> tuple[bool, str]:
        """恢复这一对端点的 AI 关系（黑名单里**两个方向**一起删）。"""
        if self._topic_id is None:
            return False, "还没有选中主题"
        try:
            removed = int(self.db.clear_map_edge_blocks(int(self._topic_id), int(src), int(dst)))
        except Exception:
            log.exception("恢复 AI 关系失败")
            return False, "恢复 AI 关系失败，详见日志"
        self._reload_blocks()
        self._draw(force=True)
        if removed <= 0:
            return False, "这条关系本来就没被屏蔽"
        return True, "已恢复：这条关系会重新画出来"

    def blocked_edge_rows(self) -> list:
        """当前主题被标为「不对」的 AI 关系行（对话框列出来用；读库失败给空列表）。"""
        topic_id = self._topic_id
        if topic_id is None:
            return []
        try:
            return list(self.db.list_map_edge_blocks(int(topic_id)))
        except Exception:  # pragma: no cover - 老库 / 读失败都不该炸窗口
            log.exception("读取 AI 关系黑名单失败")
            return []

    def unblock_edge_row(self, block_id) -> tuple[bool, str]:
        """按行 id 恢复一条被标为「不对」的 AI 关系（对话框列表里点的那一行）。"""
        if self._topic_id is None:
            return False, "还没有选中主题"
        try:
            removed = bool(self.db.delete_map_edge_block(int(block_id)))
        except Exception:
            log.exception("恢复 AI 关系失败")
            return False, "恢复 AI 关系失败，详见日志"
        self._reload_blocks()
        self._draw(force=True)
        if not removed:
            return False, "这条关系本来就没被标为不对"
        return True, "已恢复：这条关系会重新画出来"

    def open_edge_dialog(self, edge) -> None:
        """双击一条 **AI** 连线：打开「这条不对吗？」对话框（纯本地，零 token）。"""
        try:
            from .edge_block_dialog import EdgeBlockDialog
        except Exception:  # pragma: no cover - 打包缺文件时给出可读反馈
            log.exception("导入 AI 关系对话框失败")
            self._set_feedback("AI 关系对话框不可用，详见日志")
            return
        try:
            self._edge_dialog = EdgeBlockDialog(self.win, self, edge)
        except Exception:
            log.exception("打开 AI 关系对话框失败")
            self._set_feedback("打开 AI 关系对话框失败，详见日志")

    def open_shortcuts_help(self) -> None:
        """打开「操作说明」窗口（G0）：把手势表 :data:`INTERACTIONS` 原样列一遍。

        用户口径「左键是拖动，Alt + 左键才是建立关系，请你做好操作管理」——
        一个手势一旦靠猜就会误操作，所以除了三行提示，再给一张能随时查的表。
        """
        try:
            from .shortcuts_dialog import ShortcutsDialog
        except Exception:  # pragma: no cover - 打包缺文件时给出可读反馈
            log.exception("导入操作说明窗口失败")
            self._set_feedback("操作说明窗口不可用，详见日志")
            return
        try:
            self._shortcuts_dialog = ShortcutsDialog(self.win, INTERACTIONS,
                                                     note=NO_HANDLE_NOTE)
        except Exception:
            log.exception("打开操作说明窗口失败")
            self._set_feedback("打开操作说明窗口失败，详见日志")

    def open_map_settings(self) -> None:
        """打开「导图设置」窗口（本轮）：模板 / 让模型参与挑模板 / 恢复自动布局。

        用户口径（2026-10-04）：「思维导图的导出和相关设置都需要在导图界面中，
        主界面不应该显示导图的相关设置」—— 主界面「设置」里原来那一节「关系图」
        已经删掉，全部收进这个小窗口。窗口本身只写两个设置键，**不碰任何关系数据**。
        """
        try:
            from .map_settings_dialog import MapSettingsDialog
        except Exception:  # pragma: no cover - 打包缺文件时给出可读反馈
            log.exception("导入导图设置窗口失败")
            self._set_feedback("导图设置窗口不可用，详见日志")
            return
        try:
            self._map_settings_dialog = MapSettingsDialog(
                self.win, cfg=self.config, template_key=self._template,
                template_name=self.template_name(),
                on_open_templates=self.open_template_dialog,
                on_reset_layout=self.reset_node_pins)
        except Exception:
            log.exception("打开导图设置窗口失败")
            self._set_feedback("打开导图设置窗口失败，详见日志")

    def manual_relation_options(self) -> list[tuple[int, str]]:
        """给人工关系对话框用的 ``(entry_id, 显示名)`` 列表（本主题全部词条）。"""
        return [(int(node.entry_id),
                 str(self._labels.get(int(node.entry_id)) or node.term))
                for node in (self._all_nodes or self._nodes)]

    def open_relation_editor(self, *, src_id=None, dst_id=None, relation=None) -> None:
        """打开「人工关系」对话框（新增 / 删除 / 改类型）—— 纯 tk，不发网络请求。

        三种打开方式：

        * 不带参数（按钮「人工关系…」）：空表单，行为与以前完全一样；
        * ``src_id`` / ``dst_id``（F1 拖拽连线松手）：两端**已经替你选好**；
        * ``relation``（F3 双击一条人工连线）：打开就是「改这一条」——
          类型、依据、列表里那一行都预先选中。
        """
        if not (self._all_nodes or self._nodes):
            self._set_feedback("该主题还没有词条：先在阅读页解释并记录几个词")
            return
        try:
            from .relation_editor import RelationEditor
        except Exception:  # pragma: no cover - 打包缺文件时给出可读反馈
            log.exception("导入人工关系对话框失败")
            self._set_feedback("人工关系对话框不可用，详见日志")
            return
        try:
            self._relation_editor = RelationEditor(self.win, self, src_id=src_id,
                                                   dst_id=dst_id, relation=relation)
        except Exception:
            log.exception("打开人工关系对话框失败")
            self._set_feedback("打开人工关系对话框失败，详见日志")

    def save_manual_relation(self, src, dst, rel_type: str, note: str = "") -> tuple[bool, str]:
        """写一条人工关系（同一对端点的旧行会被替换 = 改类型 / 改依据）。"""
        if self._topic_id is None:
            return False, "还没有选中主题"
        try:
            src_id, dst_id = int(src), int(dst)
        except (TypeError, ValueError):
            return False, "请先选起点和终点"
        if src_id == dst_id:
            return False, "关系两端不能是同一个词条"
        label = str(rel_type or "").strip()
        try:
            self.db.set_manual_relation(int(self._topic_id), src_id, dst_id, label, str(note or ""))
        except ValueError as exc:
            return False, str(exc)
        except Exception:
            log.exception("保存人工关系失败")
            return False, "保存人工关系失败，详见日志"
        # 用户自己给这一对词加了关系 ⇒ 他显然要这条边：顺手把这对端点的 AI
        # 黑名单（F3，两个方向）删掉，别让「已屏蔽」在人工关系下面继续挡着。
        try:
            self.db.clear_map_edge_blocks(int(self._topic_id), src_id, dst_id)
        except Exception:  # pragma: no cover - 清黑名单失败不该让保存白做
            log.exception("清理 AI 关系黑名单失败")
        self._reload_blocks()
        self.on_manual_relations_changed()
        return True, "已保存人工关系（重新生成不会覆盖它）"

    def delete_manual_relation(self, relation_id) -> tuple[bool, str]:
        """删掉一条人工关系（AI 关系不在这里删 —— 它们只能靠重新生成变化）。"""
        try:
            removed = bool(self.db.delete_manual_relation(int(relation_id)))
        except Exception:
            log.exception("删除人工关系失败")
            return False, "删除人工关系失败，详见日志"
        if not removed:
            return False, "这条人工关系已经不在了"
        self.on_manual_relations_changed()
        return True, "已删除人工关系"

    def on_manual_relations_changed(self) -> None:
        """人工关系变了：重读 + 重画 + 刷新依据区。**不**重新请求模型（零 token）。"""
        self._reload_manual_relations()
        selected = self._selected
        if selected is not None and is_manual(selected):
            live = {int(rel.relation_id) for rel in self._manual_rels}
            if int(getattr(selected, "relation_id", 0) or 0) not in live:
                self._selected = None
        self._draw(force=True)

    # ------------------------------------ 子集生成（C3）/ 词条列表
    def _fill_entry_list(self, nodes=None) -> None:
        """把本主题的词条填进左边的多选列表。

        **只在词条集合真的变了时重建**：列表每重建一次就会清掉用户正在进行的多选，
        而 :meth:`_reload_nodes` 在每次生成 / 核对 / 词条变化时都会被调用。
        """
        nodes = list(self._all_nodes if nodes is None else nodes)
        ids = tuple(int(node.entry_id) for node in nodes)
        if ids == tuple(getattr(self, "_entry_list_ids", ()) or ()):
            return
        self._entry_list_ids = ids
        self._entry_options = [
            (int(node.entry_id), str(self._labels.get(int(node.entry_id)) or node.term))
            for node in nodes]
        widget = getattr(self, "entry_list", None)
        if widget is None:  # pragma: no cover - 极简替身
            return
        try:
            widget.delete(0, tk.END)
            for _eid, label in self._entry_options:
                widget.insert(tk.END, label)
        except (AttributeError, tk.TclError):  # pragma: no cover
            return
        self._apply_entry_list_selection()

    def _apply_entry_list_selection(self) -> None:
        widget = getattr(self, "entry_list", None)
        if widget is None:  # pragma: no cover
            return
        picks = [index for index, (eid, _label) in enumerate(getattr(self, "_entry_options", []))
                 if int(eid) in self._subset_ids]
        try:
            widget.selection_clear(0, tk.END)
            for index in picks:
                widget.selection_set(index)
        except (AttributeError, tk.TclError):  # pragma: no cover
            return

    def _selected_entry_ids(self) -> list[int]:
        """左边列表里**用户当前选中**的那些词条（空格 / 没选中 = 空列表）。"""
        widget = getattr(self, "entry_list", None)
        if widget is None:  # pragma: no cover
            return []
        try:
            picks = [int(index) for index in widget.curselection()]
        except (AttributeError, tk.TclError):  # pragma: no cover
            return []
        options = list(getattr(self, "_entry_options", []))
        return [int(options[index][0]) for index in picks if 0 <= index < len(options)]

    def _on_subset_generate(self) -> None:
        """只分析选中的词（C3）：省 token 的核心入口。"""
        picks = self._selected_entry_ids()
        if not picks:
            self._set_feedback("先在左边选几个词（Ctrl / Shift 可多选），再点「只分析选中的词」")
            return
        self._set_subset({int(eid) for eid in picks})
        self._drop_expired(force=True)
        self._set_feedback(f"只分析选中的 {len(self._subset_ids)} 个词……")
        self._request(force=True)

    def _on_all_generate(self) -> None:
        """切回「整个主题」：子集范围清空后重新生成（子集与全部是两份结果）。"""
        if not self._subset_ids:
            self._on_generate()
            return
        self._set_subset(set())
        self._drop_expired(force=True)
        self._set_feedback("已切回全部词条，正在按整个主题重新分析……")
        self._request(force=True)

    # ------------------------------------ 孤立词诊断（C2）/ 打开词条（C4）
    def _diag_summary(self) -> str:
        """一次生成的孤立词汇总：哪些词被孤立、候选各被判成什么（不用逐条点）。"""
        nodes = list(self._nodes)
        if not nodes:
            return "该主题还没有词条。"
        drawn = self.relations_for_draw()
        linked: set[int] = set()
        for rel in drawn:
            linked.add(int(rel.src_entry_id))
            linked.add(int(rel.dst_entry_id))
        isolated = [node for node in nodes if int(node.entry_id) not in linked]
        manual_count = sum(1 for rel in drawn if is_manual(rel))
        graph = self._graph
        verdicts = tuple(getattr(graph, "verdicts", ()) or ()) if graph is not None else ()
        head = (f"本次分析 {len(nodes)} 个词：{len(drawn)} 条关系，"
                f"{len(isolated)} 个孤立词"
                + (f"（其中人工添加 {manual_count} 条）" if manual_count else ""))
        lines = [head]
        if not isolated:
            lines.append("没有孤立词：每个词都至少有一条画出来的关系。")
            return "\n".join(lines)
        counts: dict[str, int] = {}
        for row in verdicts:
            label = VERDICT_LABELS.get(str(row.get("verdict") or ""), "")
            if label:
                counts[label] = counts.get(label, 0) + 1
        if counts:
            lines.append("被否掉的候选：" + "　".join(
                f"{label} {count} 条" for label, count in sorted(counts.items())))
        else:
            lines.append("这次没有留下判定记录（旧缓存 / 模型一条候选都没提）。")
        self._diag_lines = {}
        for node in isolated:
            entry_id = int(node.entry_id)
            name = str(self._labels.get(entry_id) or node.term)
            rows = tuple(getattr(graph, "verdicts_for", lambda _i: ())(entry_id)) if graph else ()
            if not rows:
                why = "这次没有它的候选记录（可能是没提，也可能是本地校验就筛掉了）"
            else:
                parts = []
                for row in rows:
                    label = VERDICT_LABELS.get(str(row.get("verdict") or ""), "没有判定")
                    dst = str(self._labels.get(int(row.get("dst", 0) or 0), "（不在本主题）"))
                    parts.append(f"{label}（连向 {dst}）")
                why = "；".join(parts)
            self._diag_lines[len(lines)] = entry_id
            lines.append(f"· {name} —— {why}")
        lines.append("（双击某一行可以在图上定位这个词；人工关系不算 AI 判断）")
        return "\n".join(lines)

    def _fill_diagnostics(self) -> None:
        widget = getattr(self, "diag_text", None)
        if widget is None:  # pragma: no cover
            return
        try:
            widget.configure(state="normal")
            widget.delete("1.0", tk.END)
            widget.insert("1.0", self._diag_summary())
            widget.configure(state="disabled")
        except (AttributeError, tk.TclError):  # pragma: no cover
            return

    def toggle_diagnostics(self) -> None:
        """展开 / 收起孤立词诊断面板（C2）。"""
        widget = getattr(self, "diag_text", None)
        if widget is None:  # pragma: no cover
            return
        visible = bool(getattr(self, "_diag_visible", False))
        try:
            if visible:
                widget.pack_forget()
            else:
                widget.pack(fill="x", padx=theme.px(10), pady=(0, theme.px(2)),
                            before=self.evidence)
        except (AttributeError, tk.TclError):  # pragma: no cover
            return
        self._diag_visible = not visible
        if self._diag_visible:
            self._fill_diagnostics()

    def _bind_isolated_label(self, item) -> None:
        """把画布上「孤立词…」那一行接上诊断开关（本轮：按钮内化成图上的入口）。

        用户口径：这几个功能「本身就是功能，用户通过操作来完成……不需要单独作为
        一个功能框去展示」。所以入口挂在图上那句话上，而不是再排一个按钮。

        测试替身画布没有 ``tag_bind``（见 ``tests\\support.py`` 的 FakeCanvas），
        因此先探测再绑：替身上只是少一个入口，不会让 ``_draw`` 抛错。
        """
        binder = getattr(self.canvas, "tag_bind", None)
        if not callable(binder):
            return
        try:
            binder(item, "<Button-1>", self._on_isolated_label_click)
        except (tk.TclError, TypeError):  # pragma: no cover - 替身画布
            pass

    def _on_isolated_label_click(self, _event=None) -> None:
        """点那一行 = 展开 / 收起孤立词诊断（原来工具条按钮的入口，C2）。"""
        self.toggle_diagnostics()

    def _on_diag_open(self, event=None) -> None:
        """双击诊断面板的一行 → 在图上定位并选中那个词。"""
        widget = getattr(self, "diag_text", None)
        if widget is None or event is None:  # pragma: no cover
            return
        try:
            index = widget.index(f"@{int(event.x)},{int(event.y)}")
            line = int(str(index).split(".")[0]) - 1        # Text 行号从 1 开始
        except (AttributeError, TypeError, ValueError, tk.TclError):  # pragma: no cover
            return
        entry_id = getattr(self, "_diag_lines", {}).get(line)
        if entry_id is None:
            return
        self._focus_node(int(entry_id))

    def _focus_node(self, entry_id: int) -> bool:
        """选中某个词并把视图拉到它附近（诊断面板 / 词条列表共用）。"""
        self._selected = None
        self._selected_node = int(entry_id)
        self._update_evidence()
        self._highlight_edges()
        layout = self._layout
        target = None
        if layout is not None:
            for node in layout.nodes:
                if int(node.entry_id) == int(entry_id):
                    target = node
                    break
        if target is None:
            return False
        try:
            self.canvas.xview_moveto(max(0.0, (target.x - self._canvas_size()[0] / 2.0)
                                        / max(1.0, self._layout_size()[0])))
            self.canvas.yview_moveto(max(0.0, (target.y - self._canvas_size()[1] / 2.0)
                                        / max(1.0, self._layout_size()[1])))
        except (AttributeError, tk.TclError, TypeError, ZeroDivisionError):  # pragma: no cover
            return True
        return True

    def _layout_size(self) -> tuple[float, float]:
        """画布滚动区域的世界尺寸（定位用；取不到时退回画布尺寸）。"""
        try:
            region = self.canvas.cget("scrollregion").split()
            if len(region) == 4:
                return (max(1.0, float(region[2]) - float(region[0])),
                        max(1.0, float(region[3]) - float(region[1])))
        except (AttributeError, TypeError, ValueError, tk.TclError):  # pragma: no cover
            pass
        return self._canvas_size()

    def _on_entry_list_open(self, _event=None) -> None:
        """左边列表里双击一条词条 = 打开它（C4 的另一条路径）。"""
        picks = self._selected_entry_ids()
        if not picks:
            return
        self._open_entry(picks[0])

    def open_selected_entry(self) -> None:
        """打开**当前选中**的那条词条（画布点词之后按这里）。"""
        if self._selected_node is None:
            self._set_feedback("先在图上点一个词，再点这里打开它的词条详情")
            return
        self._open_entry(int(self._selected_node))

    def _open_entry(self, entry_id: int) -> None:
        """在主界面里打开并选中这条词条（C4：不用回列表里翻）。"""
        opener = getattr(self.app, "open_main_window", None)
        if callable(opener):
            try:
                opener()
            except Exception:  # pragma: no cover - 主界面打不开也不该炸图窗
                log.exception("打开主界面失败")
        #: 主窗口在 App 上叫 ``self.main``（``self.main_window`` 不存在 —— 早先这里
        #: 拿错了名字，于是「双击词卡回主界面并选中」那一步的**切主题**静默失效，
        #: 主界面停在别的主题时看起来就像「跳过去没反应」）。
        main = getattr(self.app, "main", None)
        # 先把主界面的浏览范围切到这个词条所属的主题：只调 select_entry 的话，
        # 主界面若正停在别的主题（或跟着阅读页面走），右栏会显示这条词，
        # 左边列表里却找不到它 —— 看起来像「跳过去没反应」。
        scope = getattr(main, "set_browse_scope", None)
        if callable(scope) and self._topic_id:
            try:
                scope(int(self._topic_id))
            except Exception:  # pragma: no cover - 切范围失败也要把词条选上
                log.exception("切换主界面浏览主题失败")
        selector = getattr(main, "select_entry", None)
        if callable(selector):
            try:
                selector(int(entry_id))
            except Exception:  # pragma: no cover
                log.exception("在主界面里选中词条失败")
        self._set_feedback("已在主界面打开这条词条（改完回这里点「重新生成」）")

    # ------------------------------------ 导出（C1）
    def export_style(self) -> dict:
        """导出图片用的样式：把 theme 的颜色 / 字号 / 圆角 / 线型一次交给 ``map_export``。

        为什么不让 ``map_export`` 自己读 ``theme``：那个模块必须能在**没有窗口**的
        环境里被 import 与测试（它是纯渲染 + Win32 抓图），而 ``theme`` 的度量
        （``font_px_at`` / ``px``）与 Tk 缩放绑定；界面这边才是唯一知道「当前缩放
        和这套配色」的人，所以由界面把「这一张图长什么样」说清楚。
        """
        zoom = float(self._zoom or 1.0)
        style = {
            "font": theme.family(),
            "bg": theme.BG, "card": theme.CARD_BG, "panel": theme.PANEL,
            "panel_alt": theme.PANEL_ALT, "border": theme.BORDER,
            "border_strong": theme.BORDER_STRONG, "text": theme.TEXT,
            "text_body": theme.TEXT_BODY, "muted": theme.TEXT_MUTED,
            "faint": theme.TEXT_FAINT, "accent": theme.ACCENT,
            "accent_soft": theme.ACCENT_SOFT,
            "node_font": theme.font_px_at(9, zoom),
            "topic_font": theme.font_px_at(10, zoom),
            "edge_font": theme.font_px_at(7, zoom),
            "label_font": theme.font_px_at(7, zoom),
            "footer_font": theme.font_px_at(8, zoom),
            "node_radius": theme.px(9), "topic_radius": theme.px(12),
            "group_radius": theme.px(10),
            # 连线外观全图统一（用户口径）：一个线宽、一个颜色，人工关系不特殊；
            # 线型（虚实）由 ``edges`` 里各自的 dash 决定。
            "line_width": max(1, theme.px(EDGE_WIDTH)),
            "edge_fill": EDGE_FILL, "label_fill": EDGE_LABEL_FILL,
            "isolated_text": ISOLATED_TEXT,
            "edges": {kind: {"stroke": spec.get("fill"), "dash": tuple(spec.get("dash") or ())}
                      for kind, spec in EDGE_STYLES.items()},
        }
        name = str(self._topic_name_text or "").strip()
        if name:
            style["title"] = f"{name} —— 参考关系图"
        return style

    def export_map_files(self) -> None:
        """导出当前关系图：一张**完整**的 PNG + 一份可编辑的 SVG（C1）。

        用户要的是**图**，不是数据表：导出的两个文件同名成对，PNG 是整张图
        （内容比视口大就分块抓取拼起来），SVG 是矢量图、能拿去继续改。
        """
        if self._layout is None or not self._nodes:
            self._set_feedback("画布上还没有关系图，先等它画出来（或点「重新生成」）再导出")
            return
        try:
            from .. import map_export
        except Exception:  # pragma: no cover
            log.exception("导入导出模块失败")
            self._set_feedback("导出模块不可用，详见日志")
            return
        try:
            result = map_export.export_map(
                layout=self._layout, style=self.export_style(), canvas=self.canvas,
                topic_name=self._topic_name_text,
                model_config=str(getattr(self.config, "model_display", "") or ""),
                template_name=self.template_name(),
            )
        except Exception as exc:
            log.exception("导出关系图失败")
            self._set_feedback(f"导出关系图失败：{exc}")
            return
        self._set_feedback(f"{result.summary()}　→　{result.directory}")
        self._open_directory(result.directory)

    @staticmethod
    def _open_directory(path) -> None:
        """打开导出目录（失败只记日志：打开文件夹不是导出的必要条件）。"""
        try:
            os.startfile(str(path))       # noqa: S606 - Windows 桌面程序，用户可见
        except Exception:  # pragma: no cover - 无 shell / 被策略挡住
            log.exception("打开导出目录失败")

    # ------------------------------------------------------- 平移（右键拖动）
    def _pan_guard(self) -> bool:
        """左键点击是否发生在平移**当中**（右键还按着）→ 是就不作数。

        判据只有一条：``_pan_last`` 是否非 None（右键按住期间才非 None）。
        :meth:`_on_pan_end` 一松手就清掉标记，因此右键松手之后的第一次正常左键
        点击必须照常点线 / 点词 —— 「刚平移过」绝不能成为吃掉下一次点击的理由。
        """
        return self._pan_last is not None

    def _on_pan_start(self, event) -> None:
        """右键按下：记住指针位置，开始平移（不改选中、不触发任何点击语义）。"""
        try:
            point = (int(event.x_root), int(event.y_root))
        except (AttributeError, TypeError, ValueError):  # pragma: no cover
            self._pan_last = None
            return
        self._pan_last = point

    def _on_pan_motion(self, event) -> None:
        """右键按住移动：画布视图跟着指针走（方向与手一致，1:1）。

        只有右键按下过（``_pan_last`` 非 None）才动视图 —— 左键拖动绝不参与平移；
        任何位移都照常跟随（手抖不该让画面粘住）。
        """
        if self._pan_last is None:
            return
        try:
            x_root, y_root = int(event.x_root), int(event.y_root)
        except (AttributeError, TypeError, ValueError):  # pragma: no cover
            return
        dx = x_root - self._pan_last[0]
        dy = y_root - self._pan_last[1]
        if dx == 0 and dy == 0:
            return
        self._pan_last = (x_root, y_root)
        try:
            # ``xscrollincrement`` = 1 → 1 unit = 1 设备像素：指针挪多少，视图挪多少
            self.canvas.xview_scroll(-dx, "units")
            self.canvas.yview_scroll(-dy, "units")
        except (tk.TclError, AttributeError):  # pragma: no cover - 极简替身
            pass

    def _on_pan_end(self, _event=None) -> None:
        """右键松手：结束平移并**清掉**标记。

        紧接着的那一次左键点击是一次**正常点击**（用户在拖完手之后松开右键，
        下一次左键往往就是要点线 / 点词）—— 标记在这里清掉，绝不留给
        :meth:`_pan_guard` 去「消费」掉那一下。
        """
        self._pan_last = None

    # ------------------------------------------------------------- 缩放
    def _region_box(self) -> tuple[float, float, float, float]:
        """画布滚动范围 ``(left, top, right, bottom)``（设备像素，与 Tk 同一个数）。

        四边各留**一个视口**的空白：左 / 上从一个视口的**负坐标**开始，右 / 下到
        「内容右边 / 下边 + 一个视口」。留白必须按视口给 —— 只留几十像素时，内容
        比视口小的小图只能往左上挪那么一点点，右键平移形同虚设。

        视图原点 ``(0, 0)`` 落在滚动范围**内部**（Tk 新建画布的视图原点就是 0）：
        于是视图边缘 = ``左界 + 分数 × 总宽``（:meth:`_view_left`），``moveto``
        分数 = ``(视图边缘 - 左界) / 总宽``（:meth:`_move_view_to`）—— 「原点 0」、
        「允许负边缘」与「四边留白」都由同一个式子表达，不再有第二个口径。
        """
        layout = self._layout
        width, height = self._canvas_size()
        view_w, view_h = float(width), float(height)
        margin_x = view_w * PAN_MARGIN_VIEWPORTS
        margin_y = view_h * PAN_MARGIN_VIEWPORTS
        if layout is None:
            return (0.0, 0.0, view_w, view_h)   # 还没有内容：范围就是一屏
        left = -margin_x
        top = -margin_y
        # 内容比视口小时也要留够一个视口的余量（小图同样能往四个方向自由挪）
        right = max(float(layout.content_w), view_w) + margin_x
        bottom = max(float(layout.content_h), view_h) + margin_y
        # 取整：``scrollregion`` 落到 Tk 里是整数像素，视图偏移与 ``canvasx``
        # 必须用**同一个数**，否则锚定会差出亚像素（见 ``_view_left``）。
        return (float(int(round(left))), float(int(round(top))),
                float(int(round(right))), float(int(round(bottom))))

    def content_right(self) -> int:
        """画布滚动范围的右边界（设备像素，与 ``scrollregion`` 同一个数）。"""
        return int(self._region_box()[2])

    def content_bottom(self) -> int:
        """画布滚动范围的下边界（设备像素，与 ``scrollregion`` 同一个数）。"""
        return int(self._region_box()[3])

    def _canvas_coord(self, axis: str, value: float) -> float:
        """屏幕坐标 → 画布坐标（真实 Tk 用 ``canvasx`` / ``canvasy``）。

        **返回值已经含视图偏移**：绝不能再加一次 ``_view_left`` / ``_view_top``。
        """
        method = getattr(self.canvas, axis, None)
        if callable(method):
            try:
                return float(method(value))
            except (tk.TclError, TypeError, ValueError):  # pragma: no cover
                pass
        return float(value)

    def _view_left(self) -> float:
        """视图左边缘在**画布坐标**里的位置（= ``canvasx(0)``）。

        真实 Tk 的 ``xview()[0]`` 是**相对滚动范围**的分数，所以必须算
        ``左界 + 分数 × 总宽``：滚动范围从负坐标开始时，这个分数**不是**画布坐标
        （原点 0 对应 ``视口宽 / 总宽``），直接拿分数当坐标会整体偏移一个视口。
        口径与 :meth:`_region_box` 一致（``_draw`` 每次都把那个范围配到画布上）。
        """
        left, _top, right, _bottom = self._region_box()
        try:
            start = float(self.canvas.xview()[0])
        except (tk.TclError, AttributeError, TypeError, ValueError, IndexError):
            start = 0.0
        return float(left) + start * max(1.0, float(right) - float(left))

    def _view_top(self) -> float:
        """视图上边缘在**画布坐标**里的位置（= ``canvasy(0)``；与左边缘同构）。"""
        _left, top, _right, bottom = self._region_box()
        try:
            start = float(self.canvas.yview()[0])
        except (tk.TclError, AttributeError, TypeError, ValueError, IndexError):
            start = 0.0
        return float(top) + start * max(1.0, float(bottom) - float(top))

    def _move_view_to(self, view_left: float, view_top: float) -> None:
        """把视图左 / 上边缘挪到**画布坐标** ``view_left`` / ``view_top``。

        ``moveto`` 要的是**相对滚动范围**的分数 ``(边缘 - 左界) / 总宽``（Tk 的
        ``moveto`` 本来就是相对整个 ``scrollregion``，不是相对可滚距离）。允许负
        边缘 / 越界分数（缩放锚定算出来的左缘本来就可能为负）—— **限幅交给 Tk**：
        它会按滚动范围把视图夹回可见范围，这里绝不自己先夹一次。
        """
        left, top, right, bottom = self._region_box()
        total_w = max(1.0, float(right) - float(left))
        total_h = max(1.0, float(bottom) - float(top))
        try:
            self.canvas.xview_moveto((float(view_left) - float(left)) / total_w)
            self.canvas.yview_moveto((float(view_top) - float(top)) / total_h)
        except (tk.TclError, AttributeError, TypeError, ValueError):  # pragma: no cover
            pass

    def _apply_anchor(self, anchor_px: float, anchor_py: float,
                      world_x: float, world_y: float, ratio: float) -> None:
        """把「缩放前指针下的那个画布点」重新放回指针下面。

        布局是 zoom = 1 的逻辑布局的仿射缩放，因此那个点的**新**画布坐标就是
        ``world × ratio``；要让它落在屏幕 ``anchor`` 处，视图左边缘必须是
        ``world × ratio - anchor``（可能为负，例如内容本来就在原点左上方）。
        分数换算与限幅统一走 :meth:`_move_view_to` 与 Tk。
        """
        self._move_view_to(float(world_x) * float(ratio) - float(anchor_px),
                           float(world_y) * float(ratio) - float(anchor_py))

    def zoom_by(self, factor: float, *, anchor: tuple[float, float] | None = None) -> bool:
        """缩放（滚轮 / Ctrl+滚轮 / 程序调用）：**仿射缩放整张逻辑布局**。

        逻辑布局只在 zoom = 1 下算一次（列数 / 行数 / 绕行形状都不变），缩放
        只把所有坐标乘以 ``ratio``。``anchor`` 是**指针的屏幕坐标**：给出时，
        缩放前后指针底下还是同一个画布点；不给就锚在画布中心。缩放被限幅在
        :data:`ZOOM_MIN` – :data:`ZOOM_MAX`，并在同一处停掉首开自适应。
        """
        try:
            current = float(self._zoom)
            zoomed = max(ZOOM_MIN, min(ZOOM_MAX, current * float(factor)))
            ratio = zoomed / current
        except (TypeError, ValueError, ZeroDivisionError):  # pragma: no cover
            return False
        if abs(ratio - 1.0) < 1e-6:
            return False
        width, height = self._canvas_size()
        spot = ((width / 2.0, height / 2.0) if anchor is None
                else (float(anchor[0]), float(anchor[1])))
        # ``canvasx`` 已经含偏移：这里拿到的是完整的画布坐标，不再加视图偏移。
        world_x = self._canvas_coord("canvasx", spot[0])
        world_y = self._canvas_coord("canvasy", spot[1])
        self._fit_pending = False               # 用户自己动过镜头：不再自动复位
        self._zoom = zoomed
        self._draw(force=True)
        self._apply_anchor(spot[0], spot[1], world_x, world_y, ratio)
        self._set_feedback(f"缩放 {int(round(self._zoom * 100))}%（滚轮）")
        return True

    def _wheel_delta(self, event) -> int:
        try:
            return int(getattr(event, "delta", 0) or 0)
        except (TypeError, ValueError):  # pragma: no cover
            return 0

    def _on_zoom_wheel(self, event) -> None:
        """滚轮 = **直接**缩放（以鼠标所在位置为锚）；Ctrl+滚轮走同一条路。"""
        delta = self._wheel_delta(event)
        if not delta:
            return
        anchor = None
        try:
            anchor = (float(event.x), float(event.y))
        except (AttributeError, TypeError, ValueError):  # pragma: no cover
            anchor = None
        self.zoom_by(ZOOM_STEP if delta > 0 else 1.0 / ZOOM_STEP, anchor=anchor)

    def _cancel_deferred_draw(self) -> None:
        """取消还没到点的「手离开后补画」——这次已经在画了。"""
        item = self._draw_retry
        self._draw_retry = None
        if item is None:
            return
        cancel = getattr(self.canvas, "after_cancel", None)
        if not callable(cancel):  # pragma: no cover - 极简替身
            return
        try:
            cancel(item)
        except tk.TclError:  # pragma: no cover
            pass

    def _gesture_live(self) -> bool:
        """手还在画布上吗（拖着卡片 / 拉着关系线 / 拖空白平移）。"""
        return (self._drag_node is not None or self._link_from is not None
                or self._bg_pan_last is not None)

    def _draw_after_gesture(self) -> None:
        """推迟的那次重画：手一离开画布就补上；手还在就再等一轮。"""
        self._draw_retry = None
        if self._gesture_live():
            self._defer_draw()
            return
        self._draw(force=True)

    def _defer_draw(self) -> None:
        """排一次「等手离开画布再补画」；已经排过就不重复排。"""
        if self._draw_retry is not None:
            return
        after = getattr(self.canvas, "after", None)
        if not callable(after):  # pragma: no cover - 极简替身
            return
        try:
            self._draw_retry = after(DRAW_DEFER_MS, self._draw_after_gesture)
        except tk.TclError:  # pragma: no cover
            self._draw_retry = None

    # ------------------------------------------------------------- 绘制
    def _on_canvas_configure(self, _event=None) -> None:
        """画布尺寸确定 / 变化 → 重画（首次未映射时的 1x1 不画，等真实尺寸）。"""
        if self._scheduled:
            return
        self._scheduled = True
        drawer = self._draw                        # 幂等；尺寸相同不会重画
        after = getattr(self.canvas, "after_idle", None)
        if not callable(after):
            after = getattr(self.canvas, "after", None)
        try:
            if callable(after):
                after(drawer)
            else:  # pragma: no cover - 极简替身
                drawer()
        except tk.TclError:  # pragma: no cover
            self._scheduled = False

    def _on_resized(self, _w: int, _h: int) -> None:
        self._draw(force=True)
        self._set_feedback("已按新窗口尺寸重排")

    def _canvas_size(self) -> tuple[int, int]:
        try:
            width = int(self.canvas.winfo_width())
            height = int(self.canvas.winfo_height())
        except (tk.TclError, TypeError, ValueError):  # pragma: no cover
            width = height = 0
        if width <= 1 or height <= 1:
            return (theme.px(FALLBACK_W), theme.px(FALLBACK_H))
        return (width, height)

    def _push(self, item) -> int:
        try:
            self._items.append(int(item))
        except (TypeError, ValueError):  # pragma: no cover
            pass
        return item

    def _clear(self) -> None:
        for item in self._items:
            try:
                self.canvas.delete(item)
            except (tk.TclError, AttributeError):  # pragma: no cover
                break
        self._items = []
        self._edge_items = {}
        self._edge_rels = {}
        self._drag_items = []
        self._drag_last = None
        # 重画时把「半路的拖动」也一并忘掉：卡片 / 连线都不在了，留着状态只会
        # 让下一次 Motion 去搬一个已经删掉的图元。
        self._press_xy = None
        self._drag_started = False
        self._link_from = None
        self._drag_node = None
        self._drag_target = None
        self._link_item = None
        #: 左键拖空白平移（本轮）：重画时也要忘掉，否则下一次 Motion 会按一个
        #: 早已不存在的按下点继续滚视图。
        self._bg_pan_last = None

    def _items_matching(self, kind: str) -> list:
        """当前画布上的某类 item（回归断言用；真实 / 假 Canvas 都可用）。"""
        options = getattr(self.canvas, "item_options", None)
        if not isinstance(options, dict):
            return []
        return [dict(value) for value in options.values()
                if str(value.get("_kind") or "") == kind]

    def _mapped_size(self) -> tuple[int, int]:
        """画布**真实**尺寸（还没被 Tk 映射时 ``winfo_*`` = 1 → 返回 ``(0, 0)``）。

        与 :meth:`_canvas_size` 的区别：后者给未映射的画布一个兜底尺寸（免得
        整张图挤成 1x1），但**首开自适应绝不能用兜底尺寸** —— 那会在窗口还没
        显示时就把缩放定死（用户一打开就是缩小过的图）。
        """
        try:
            width = int(self.canvas.winfo_width())
            height = int(self.canvas.winfo_height())
        except (tk.TclError, TypeError, ValueError):  # pragma: no cover
            return (0, 0)
        if width <= 1 or height <= 1:
            return (0, 0)
        return (width, height)

    def _auto_fit(self, width: int, height: int, relations, top_pad: float) -> bool:
        """首开自适应：内容比视口大就整体缩到看得全（只缩不放，且**每个主题只做一次**）。

        * **只缩不放**：内容本来就装得下就保持 1.0（不放大 = 不把字撑到发虚）；
        * **下限** :data:`AUTO_FIT_MIN`：再挤也不缩到读不清字，剩下的交给右键拖动；
        * **只做一次**：做完就把 :attr:`_fit_pending` 清掉 —— 用户之后自己滚轮缩放 /
          右键平移，刷新（词条变化、重新生成结果回来、窗口改尺寸）**一律原样保留**；
        * 画布还没有真实尺寸（Tk 尚未映射）时**不做也不清**，等真正的
          ``<Configure>`` 拿到尺寸后再算。

        缩放本身由 :func:`layout_graph` 的仿射缩放完成（列数 / 行数不变），
        尺寸取自 :func:`layout_extent`（与布局同一份逻辑布局，绝不低估）。

        返回是否**真的改了缩放**（改了才需要把视图复位到左上角）。
        """
        if not self._fit_pending or self._topic_id is None or not self._nodes:
            return False
        real_w, real_h = int(width), int(height)
        if real_w <= 1 or real_h <= 1:
            return False                      # 还没映射：等真实尺寸
        extent_w, extent_h = layout_extent(self._labels, relations, width=real_w,
                                          height=real_h,
                                          topic_label=self._topic_name_text,
                                          top_pad=top_pad,
                                          pins=self.pinned_positions(),
                                          template=self._template)
        if extent_w <= 0 or extent_h <= 0:
            return False
        factor = min(1.0, (real_w - theme.px(4)) / extent_w,
                     (real_h - theme.px(4)) / extent_h)
        self._fit_pending = False
        if factor >= 0.999:
            return False                      # 装得下：保持 1.0，一个像素都不动
        fitted = max(AUTO_FIT_MIN, factor)
        if abs(fitted - self._zoom) < 1e-6:
            return False
        self._zoom = fitted
        return True

    def _reset_view(self) -> None:
        """把视图拉回**世界原点** (0, 0)（首开 / 换主题：主题与第一层可见）。

        滚动范围从负的一个视口开始，所以这里的 ``moveto`` 分数**不是** 0，而是
        ``(0 - 左界) / 总宽``（见 :meth:`_move_view_to`）。
        """
        self._move_view_to(0.0, 0.0)
        self._pan_last = None

    def _draw(self, *, force: bool = False) -> int:
        """重画画布：层级分组 + 分层节点 + AI 关系边 + 孤立词 + 短标注。

        内容（尺寸 / 缩放 / 主题 / 词条 / 关系）没变就不重画：``<Configure>``
        在拖动 / 缩放窗口时会反复触发，幂等才能不闪。

        **手还在画布上时（拖动 / 拉线 / 平移）直接不画**：见 :meth:`_gesture_live`。
        """
        self._scheduled = False
        self._cancel_deferred_draw()
        if not force and self._gesture_live():
            # ★ 手还在画布上：**这时绝不重画**。重画会把卡片按布局位置重新摆一遍
            # （手指底下的卡片「跳」回原位），紧接着的 :meth:`_clear` 又会把手势
            # 状态（`_drag_node` / `_link_from` / `_bg_pan_last` …）全部擦掉 ——
            # 拖动当场失效。真鼠标探针抓到的就是这个：按下明明设好了 `_drag_node`，
            # 一次 `after_idle` 重画之后手势就没了。改成排一次「手离开后补画」。
            self._defer_draw()
            return self._item_count()
        width, height = self._canvas_size()
        relations = self.relations_for_draw()
        manual = tuple(rel for rel in relations if is_manual(rel))
        key = (width, height, round(self._zoom, 3), self._topic_id,
               self._topic_name_text, self._fingerprint,
               str(self._template),
               tuple(sorted(self._subset_ids)),
               tuple(sorted((int(r.src_entry_id), int(r.dst_entry_id), str(r.rel_type))
                            for r in relations)))
        if not force and key == self._draw_key:
            return self._item_count()
        self._draw_key = key
        self._clear()
        # 画布上的**每一个字号都随缩放走**（``theme.font_at(pt, self._zoom)``）：
        # 卡片框 / 坐标来自 ``layout_graph(..., zoom=self._zoom)``，字号必须与
        # ``metrics_for(zoom).em`` 同源 —— 否则滚轮缩小视图时框缩了、字没缩，
        # 文字就会越出标签框（用户报的那条）。
        if self._topic_id is None:
            self._text(theme.px(14), theme.px(12), anchor="nw", fill=theme.TEXT_FAINT,
                       font=theme.font_at(9, self._zoom),
                       text="还没有主题。先在阅读页解释并记录几个词，再回来看关系图。")
            return self._item_count()
        if not self._nodes:
            self._text(theme.px(14), theme.px(12), anchor="nw", fill=theme.TEXT_FAINT,
                       font=theme.font_at(9, self._zoom),
                       text="该主题还没有词条：先在阅读页解释并记录几个词。")
            return self._item_count()

        # 画布顶上不再留图例带（那两行图例已按用户要求删掉，操作提示移到右下角
        # 的浮层上）：这里只给一个下限，真正的顶部留白用布局自己的 ``pad``
        # （``_layout_core`` 取 ``max(pad, top_pad)``）。
        top_pad = theme.px(6)
        fitted = self._auto_fit(*self._mapped_size(), relations, top_pad)
        layout = layout_graph(self._labels, relations, width=width, height=height,
                              topic_label=self._topic_name_text,
                              zoom=self._zoom, top_pad=top_pad,
                              pins=self.pinned_positions(),
                              template=self._template)
        self._layout = layout
        self._template_note = str(getattr(layout, "template_note", "") or "")
        self._sync_template_button()
        if self._template_note:
            # 骨架和数据不搭：**如实说**，并提示一条退路（用户永远能切回自动）。
            self._set_feedback(self._template_note + "（「模板…」里可以切回自动）")
        region_left, region_top, region_right, region_bottom = self._region_box()
        try:
            # ``xscrollincrement / yscrollincrement`` = 1：``*view_scroll(n, "units")``
            # 的 1 unit 就是 1 设备像素 —— 右键平移与指针位移严格 1:1。
            # ``scrollregion`` 四边各留一个视口（见 :meth:`_region_box`）：小图也能
            # 往四个方向自由平移；视图原点 (0, 0) 落在范围**内部**。改范围时 Tk
            # 保留视图原点、只按新范围限幅 —— 刷新因此保持用户实际的世界坐标偏移
            # （而不是「按新范围重算的分数」）。
            self.canvas.configure(xscrollincrement=1, yscrollincrement=1,
                                  scrollregion=(region_left, region_top,
                                                region_right, region_bottom))
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass
        if fitted:
            self._reset_view()               # 首开缩过：回到世界原点（主题与第一层可见）

        # ① 层级分组底板（包含 / 属于的子树）——只是结构提示，不含语义判断
        for group in layout.groups:
            self._round(group.x0, group.y0, group.x1 - group.x0, group.y1 - group.y0,
                        theme.px(10), fill=theme.PANEL_ALT, outline="", _kind="group")
        # ② 主题 → 顶层词的细线：分类结构（**不是** AI 语义关系）。
        #    走线是**折线**：主题底边 → 母线（``topic_gap`` 正中）→ 水平 → 下层卡片
        #    顶边；所有连线共用同一条母线，因此不再是从主题斜射出去的扇形交叉。
        for link in layout.topic_links:
            self._line([value for point in link for value in point],
                       fill=theme.BORDER, width=max(1, theme.px(1)),
                       _kind="topic-link")
        # ③ 参考关系：颜色与线宽**全图统一**（用户口径：不要每条线一个灰度、不要人工
        #    更粗），只靠虚线与线上的类型短标签区分 —— 四种线型见 ``EDGE_STYLES``。
        #    画的就是 ``edge.points``（布局里已经采样 / 绕开节点的**同一串点**）：
        #    不开 Tk 的 smooth —— 平滑会把折线的拐角鼓回被绕开的节点里，
        #    而这里的折线本来就是按避障净空算出来的，必须原样画出去。
        for edge in layout.edges:
            style = EDGE_STYLES.get(edge.kind, EDGE_STYLES["cross"])
            points = [value for point in edge.points for value in point]
            item = self._line(points, fill=style["fill"],
                              width=max(1, theme.px(EDGE_WIDTH)),
                              dash=style.get("dash") or (),
                              arrow="" if edge.symmetric else "last",
                              arrowshape=(theme.px(8), theme.px(9), theme.px(3)),
                              _kind="edge")
            self._edge_items[id(edge.rel)] = item
            self._edge_rels[id(edge.rel)] = edge.rel
        # ④ 节点：圆角矩形 + 完整词语（换行，不截断）。卡片上**没有**小圆点状的
        #    连接点（用户要求）：建关系改成「按住 Alt + 左键拖」。
        for node in layout.nodes:
            fill = theme.CARD_BG if not node.isolated else theme.PANEL
            #: 框与文字**打同一个 tag**（``node-24``）：拖动时按这个 tag 认图元、搬图元。
            #: 见 :data:`NODE_TAG_PREFIX` —— 不能靠「图元中心 == 布局坐标」去找，
            #: 那套比对只认得出「此刻正好落在布局坐标上」的图元，卡片一搬走就失联。
            tag = node_tag(node.entry_id)
            self._round(node.x - node.w / 2.0, node.y - node.h / 2.0, node.w, node.h,
                        theme.px(9), fill=fill, outline=theme.BORDER_STRONG,
                        width=max(1, theme.px(1)), tags=(tag,), _kind="node")
            self._text(node.x, node.y, text="\n".join(node.lines), fill=theme.TEXT,
                       font=theme.font_at(9, self._zoom), anchor="center",
                       justify="center", tags=(tag,), _kind="node-text")
        # ⑤ 中心主题节点
        if layout.topic is not None and layout.topic.w > 0:
            topic = layout.topic
            self._round(topic.x - topic.w / 2.0, topic.y - topic.h / 2.0, topic.w, topic.h,
                        theme.px(12), fill=theme.ACCENT_SOFT, outline=theme.ACCENT,
                        width=max(1, theme.px(1)), _kind="topic")
            self._text(topic.x, topic.y, text="\n".join(topic.lines), fill=theme.TEXT,
                       font=theme.font_at(10, self._zoom), anchor="center",
                       justify="center", _kind="topic-text")
        # ⑥ 孤立词那一行的短标题 —— 本轮它同时是**孤立词诊断的入口**：点这一行
        #    展开 / 收起下方那本账（工具条上原来那个「孤立词诊断」按钮已按用户口径
        #    内化掉，见 :meth:`_bind_isolated_label`）。
        if layout.isolated_label_pos is not None:
            item = self._text(layout.isolated_label_pos[0], layout.isolated_label_pos[1],
                              text=ISOLATED_TEXT, fill=theme.TEXT_FAINT,
                              font=theme.font_at(7, self._zoom), anchor="w",
                              tags=("isolated-label",), _kind="isolated-label")
            self._bind_isolated_label(item)
        # ⑦ **关系短标签最后画**：直接从线上方的空白里落字，没有底板（用户要求：
        #    文字背景透明），也不带「人工·」前缀（用户要求）—— 人工关系与 AI 关系
        #    在图上完全同款，要区分就看依据区或者双击这条线。放在最后是为了让标签
        #    永远压在卡片之上：万一某一个真的没地方放，也还读得见，不会被卡片盖掉。
        for edge in layout.edges:
            self._text(edge.label_pos[0], edge.label_pos[1],
                       text=edge.label,
                       fill=EDGE_LABEL_FILL,
                       font=theme.font_at(LABEL_FONT_SIZE, self._zoom),
                       anchor="center", _kind="edge-label")
        # ⑧ 图上不再画固定的图例 / 操作提示（用户要求删掉左上角那两行）：
        #    操作提示改成画布右下角的浮层（见 ``__init__`` 的 ``hint_box``），
        #    「AI 参考」那句留在窗口底部的状态行里。
        self._update_evidence()
        self._highlight_edges()
        if self._diag_visible:
            self._fill_diagnostics()
        return self._item_count()

    def _line(self, points, **kw):
        kind = kw.pop("_kind", "")
        item = self.canvas.create_line(points, **kw)
        if kind and isinstance(getattr(self.canvas, "item_options", None), dict):
            self.canvas.item_options.setdefault(int(item), {})["_kind"] = kind
        return self._push(item)

    def _text(self, x, y, **kw):
        kind = kw.pop("_kind", "")
        item = self.canvas.create_text(x, y, **kw)
        if kind and isinstance(getattr(self.canvas, "item_options", None), dict):
            self.canvas.item_options.setdefault(int(item), {})["_kind"] = kind
        return self._push(item)

    def _round(self, x, y, w, h, radius, **kw):
        kind = kw.pop("_kind", "")
        item = widgets.draw_round_rect(self.canvas, x, y, w, h, radius, **kw)
        if kind and isinstance(getattr(self.canvas, "item_options", None), dict):
            self.canvas.item_options.setdefault(int(item), {})["_kind"] = kind
        return self._push(item)

    def _item_count(self) -> int:
        counter = getattr(self.canvas, "item_count", None)
        if callable(counter):
            try:
                return int(counter())
            except Exception:  # pragma: no cover
                return 0
        try:
            return len(self.canvas.find_all())
        except Exception:  # pragma: no cover
            return 0
