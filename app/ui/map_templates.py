"""关系图模板（批次 G）：**只决定卡片怎么摆**，不动一个关系数据。

用户口径
--------
* 「是否可以加入模板？LLM 可以挑选合适的模板进行生成，用户也可以自己选择模板进行
  自定义画导图。」
* 模板 = **布局骨架**：把同一批关系画成思维导图 / 树状图 / 组织架构图 / 单向导图 /
  鱼骨图 / 流程线。关系本身、筛选口径、点选 / 双击 / 拖拽 / 人工关系 / 导出全部不变，
  随时能切回「自动」。

为什么单独一个模块
------------------
摆放是**纯几何**（进去一堆 ``entry_id`` 与关系，出来一堆坐标），与 Tk 无关，
可以单独喂数据断言，不必开窗口。:func:`app.ui.concept_map._layout_core` 只把
「决定位置」这一步交给这里，后面的布线 / 标签让位 / 分组底衬 / 内容外框原样复用 ——
所以模板图同样有点选、双击、拖拽、固定位置、黑名单、导出。

三条硬约束
----------
* **纯函数**：同样的输入给同样的坐标（可断言、可重现），不用随机数；
* **不 import ``map_service``**：那条链会拉起 DPAPI / Win32（见 ``app/map_export.py``
  的同款理由）；层级与方向类型的字面量与 ``map_service`` 的一致性由
  ``tests\\test_map_templates.py`` 的一条断言守着；
* **不确定就退回自动**：骨架和数据不搭（例如鱼骨图却没有因果边）时返回一条
  ``note`` 并给出空坐标，由调用方退回默认布局 —— 绝不硬套出一个读不懂的图。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping

from ..logging_setup import get_logger

log = get_logger(__name__)

#: 「自动」：沿用改进清单 A–F 批那套「一层一行」的布局（默认值）
TEMPLATE_AUTO = "auto"

#: 关系类型字面量（与 ``app.map_service.REL_TYPES`` 对齐；故意不 import，见模块 docstring）
CAUSAL = "因果"
DEPEND = "依赖"
CONTAINS = "包含"
BELONGS = "属于"
#: 层级型（父 → 子）
HIERARCHY_TYPES = (CONTAINS, BELONGS)
#: 方向型（有前后，但不一定是父子）
DIRECTIONAL_TYPES = (DEPEND, CAUSAL)

#: 鱼骨图至少要几条因果边才摆得出来（少于此数退回自动，并如实说明）
FISHBONE_MIN_CAUSAL = 2


def _relation_parts(rel) -> tuple[int, int, str]:
    """关系行 → ``(起点, 终点, 类型)``（支持三元组 / 字典 / ``MapRelation``）。"""
    if isinstance(rel, (tuple, list)) and len(rel) >= 3:
        return int(rel[0]), int(rel[1]), str(rel[2])
    if isinstance(rel, Mapping):
        return (int(rel.get("src_entry_id") or rel.get("src") or 0),
                int(rel.get("dst_entry_id") or rel.get("dst") or 0),
                str(rel.get("rel_type") or rel.get("type") or ""))
    return (int(getattr(rel, "src_entry_id", 0) or 0),
            int(getattr(rel, "dst_entry_id", 0) or 0),
            str(getattr(rel, "rel_type", "") or ""))


@dataclass(frozen=True)
class Frame:
    """摆放要用的几何（全部来自 :func:`app.ui.concept_map._layout_core`）。

    ``axis`` 是全图中心轴、``top`` 是主题下方第一行可以用的最上边；模板只在
    ``axis`` / ``top`` 附近排，**不改画布尺寸、不改卡片尺寸**（统一卡片宽由调用方
    算好，模板只许用 ``card_w``）。
    """

    axis: float
    top: float
    card_w: float
    card_h: dict[int, float] = field(default_factory=dict)
    topic_w: float = 0.0
    topic_h: float = 0.0
    h_gap: float = 0.0
    v_gap: float = 0.0
    pad: float = 0.0

    def height_of(self, entry_id: int) -> float:
        """这张卡的高度（查不到就退化成「一张单行卡」，绝不返回 0）。"""
        try:
            height = float(self.card_h.get(int(entry_id), 0.0) or 0.0)
        except (TypeError, ValueError):  # pragma: no cover - 脏数据
            height = 0.0
        return height if height > 0 else max(1.0, float(self.card_w) * 0.4)


@dataclass(frozen=True)
class Placement:
    """一次骨架摆放的结果（``spots`` 为空 = 没摆成，调用方退回自动布局）。"""

    spots: dict[int, tuple[float, float]] = field(default_factory=dict)
    #: 中心主题要不要换个地方（思维导图把它挪到正中；其余模板留在顶上）
    topic_pos: tuple[float, float] | None = None
    #: 内容整体右移了多少（宽图贴左边留白时，主题要跟着右移才对齐）
    axis_shift: float = 0.0
    #: 主题母线直接连到哪几个词（空 = 用默认那条水平母线）
    hubs: tuple[int, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class TemplateSpec:
    """下拉里的一个模板：``key`` 稳定、``name`` 给人看。"""

    key: str
    name: str
    summary: str
    fit: str
    placer: Callable[..., Placement] | None = None
    #: 这段骨架的**走线主轴**：``"y"`` = 同一层的词左右排成一条**横带**
    #: （车道横着走，直接复用默认规则）；``"x"`` = 同一层的词上下排成一**列**
    #: （把整张图转置 90° 再复用同一套规则，车道就变成竖着走）。
    #: 排线规则跟着骨架朝向走，用户要求的「不同模板各自的关系线排布规则」
    #: 就是这一格 —— 见 ``concept_map._orthogonal_routes``。
    route_axis: str = "y"
    #: 骨架和数据不搭时的退路（永远是自动布局）
    fallback: str = TEMPLATE_AUTO


# --------------------------------------------------------------------- 结构
@dataclass(frozen=True)
class _Structure:
    """关系 → 树上的结构（谁是谁的父、谁在谁前面、哪些是无向邻居）。"""

    order: tuple[int, ...]
    kids: dict[int, tuple[int, ...]]          # 包含 / 属于给出的父子
    before: dict[int, tuple[int, ...]]        # 「在谁后面」（依赖 / 因果）
    after: dict[int, tuple[int, ...]]         # 「在谁前面」（依赖 / 因果）
    near: dict[int, tuple[int, ...]]          # 无向邻居（用途 / 对照）
    roots: tuple[int, ...]
    children: dict[int, tuple[int, ...]]      # 生成树（每个节点最多一个父）
    depth: dict[int, int]
    causal: tuple[tuple[int, int], ...]       # 只留因果（鱼骨图用）


def _structure(ids: Iterable[int], rels: Iterable, levels: Mapping[int, int]) -> _Structure:
    order = tuple(sorted({int(item) for item in ids}))
    known = set(order)
    kids: dict[int, list[int]] = {}
    before: dict[int, list[int]] = {}
    after: dict[int, list[int]] = {}
    near: dict[int, list[int]] = {}
    causal: list[tuple[int, int]] = []
    for rel in rels or ():
        src, dst, kind = _relation_parts(rel)
        if src not in known or dst not in known or src == dst:
            continue
        if kind == CONTAINS:
            up, down = src, dst
        elif kind == BELONGS:
            up, down = dst, src
        elif kind == CAUSAL:
            up, down = src, dst
            causal.append((src, dst))
        elif kind == DEPEND:
            up, down = dst, src
        else:
            near.setdefault(src, []).append(dst)
            near.setdefault(dst, []).append(src)
            continue
        after.setdefault(up, []).append(down)
        before.setdefault(down, []).append(up)
        if kind in HIERARCHY_TYPES:
            kids.setdefault(up, []).append(down)

    def _key(node: int) -> tuple[int, int]:
        return (int(levels.get(node, 0) or 0), int(node))

    def _sorted(values) -> tuple[int, ...]:
        return tuple(sorted(set(values), key=_key))

    #: 「包含 / 属于」说出来的父子关系：一个词有层级父时，**层级优先** ——
    #: 依赖 / 因果只用来给没有层级归属的词排前后，不许把别人的孩子抢走。
    hier_parent: dict[int, int] = {}
    for up, downs in kids.items():
        for down in downs:
            hier_parent.setdefault(down, up)

    children: dict[int, list[int]] = {}
    depth: dict[int, int] = {}
    seen: set[int] = set()

    def walk(node: int, level: int) -> None:
        seen.add(node)
        depth[node] = level
        candidates = _sorted(list(kids.get(node, ())) + list(after.get(node, ()))
                             + list(near.get(node, ())))
        for item in candidates:
            if item in seen:
                continue
            parent = hier_parent.get(item)
            if parent is not None and parent != node:
                continue                     # 它另有层级父，跟着那边走
            children.setdefault(node, []).append(item)
            walk(item, level + 1)

    #: 从哪儿开始走 = 既没有层级父、也没有「谁在它前面」的词；空图 / 全是环时才退回第一个词。
    start = tuple(node for node in order
                  if node not in hier_parent and node not in before)
    for root in start or order:
        if root not in seen:
            walk(root, 0)
    for node in order:                       # 环 / 断链：随便挑一个当根，一个词都不丢
        if node not in seen:
            walk(node, 0)
    #: 真正的根 = 生成树里**没有父节点**的词。不能只看 ``start``：环（1 包含 2、
    #: 2 包含 1）里的词谁都不是 start，只能从兜底那一步长出来，但它的子树必须
    #: 有人摆 —— 否则思维导图 / 单向导图 / 树状图会**漏掉整个环**。
    has_parent = {child for kids_of_node in children.values() for child in kids_of_node}
    roots = tuple(node for node in order if node not in has_parent)
    return _Structure(
        order=order,
        kids={key: _sorted(value) for key, value in kids.items()},
        before={key: _sorted(value) for key, value in before.items()},
        after={key: _sorted(value) for key, value in after.items()},
        near={key: _sorted(value) for key, value in near.items()},
        roots=roots or (order[:1] if order else ()),
        children={key: tuple(value) for key, value in children.items()},
        depth=depth,
        causal=tuple(dict.fromkeys(causal)),
    )


def _shift(spots: dict[int, tuple[float, float]], frame: Frame,
           topic_pos: tuple[float, float] | None = None
           ) -> tuple[dict[int, tuple[float, float]], tuple[float, float] | None, float]:
    """整体平移，保证**卡片本身**（不是它的中心）不越过留白。

    思维导图会往左边长、水平流程线会横着长出一屏，都靠这一步推回留白里。
    返回 ``(新坐标, 新主题位置, 右移量)`` —— 右移量交给调用方去挪主题：宽图贴到
    左边留白之后，主题必须跟着右移，否则它不再压在内容中心上。
    """
    half_w = float(frame.card_w) / 2.0
    xs = [spot[0] - half_w for spot in spots.values()]
    ys = [spot[1] - frame.height_of(node) / 2.0
          for node, spot in spots.items()]
    if topic_pos is not None:
        xs.append(float(topic_pos[0]) - float(frame.topic_w) / 2.0)
        ys.append(float(topic_pos[1]) - float(frame.topic_h) / 2.0)
    if not xs:
        return spots, topic_pos, 0.0
    dx = max(0.0, float(frame.pad) - min(xs))
    dy = max(0.0, float(frame.pad) - min(ys))
    if dx == 0.0 and dy == 0.0:
        return spots, topic_pos, 0.0
    moved = {key: (value[0] + dx, value[1] + dy) for key, value in spots.items()}
    return moved, (None if topic_pos is None else (topic_pos[0] + dx, topic_pos[1] + dy)), dx


def _rows_of(structure: _Structure) -> dict[int, list[int]]:
    rows: dict[int, list[int]] = {}
    for node in structure.order:
        if node in structure.depth:
            rows.setdefault(structure.depth[node], []).append(node)
    return rows


# ------------------------------------------------------------- 树状 / 组织架构
def _place_tree(ids, rels, levels, frame: Frame, *,
                strict_rows: bool = False, orientation: str = "down") -> Placement:
    """树状图：根在上，父节点居中于自己的子树。

    ``strict_rows=True``（组织架构图）：**每一层是一条完整的行**，层内等距铺开、
    整行居中，父节点再自下而上挪到子节点的重心 —— 就是那张金字塔。
    """
    structure = _structure(ids, rels, levels)
    if not structure.order:
        return Placement()
    if orientation == "right":
        return _place_oneway(structure, frame)
    rows = _rows_of(structure)
    level_h = {depth: max(frame.height_of(node) for node in nodes)
               for depth, nodes in rows.items()}
    level_top: dict[int, float] = {}
    cursor = float(frame.top)
    for depth in sorted(level_h):
        level_top[depth] = cursor
        cursor += level_h[depth] + float(frame.v_gap)

    if strict_rows:
        return _place_rows_pyramid(structure, rows, level_h, level_top, frame)

    width_cache: dict[int, float] = {}
    height_cache: dict[int, float] = {}

    def block_w(node: int) -> float:
        if node in width_cache:
            return width_cache[node]
        kids = structure.children.get(node, ())
        total = (sum(block_w(kid) for kid in kids)
                 + float(frame.h_gap) * max(0, len(kids) - 1))
        width_cache[node] = max(float(frame.card_w), total)
        return width_cache[node]

    def block_h(node: int) -> float:
        if node in height_cache:
            return height_cache[node]
        kids = structure.children.get(node, ())
        own = level_h.get(structure.depth.get(node, 0), frame.height_of(node))
        if not kids:
            height_cache[node] = own
            return own
        height_cache[node] = own + float(frame.v_gap) + max(block_h(kid) for kid in kids)
        return height_cache[node]

    spots: dict[int, tuple[float, float]] = {}

    def assign(node: int, left: float) -> None:
        width = block_w(node)
        depth = structure.depth.get(node, 0)
        height = level_h.get(depth, frame.height_of(node))
        spots[node] = (left + width / 2.0, level_top.get(depth, frame.top) + height / 2.0)
        cursor_x = left
        for kid in structure.children.get(node, ()):
            assign(kid, cursor_x)
            cursor_x += block_w(kid) + float(frame.h_gap)

    total_w = (sum(block_w(root) for root in structure.roots)
               + float(frame.h_gap) * max(0, len(structure.roots) - 1))
    left = float(frame.axis) - total_w / 2.0
    for root in structure.roots:
        assign(root, left)
        left += block_w(root) + float(frame.h_gap)
    spots, _topic, dx = _shift(spots, frame)
    return Placement(spots=spots, axis_shift=dx)


def _place_rows_pyramid(structure: _Structure, rows, level_h, level_top,
                        frame: Frame) -> Placement:
    """组织架构图：一层一行、层内等距，父节点居中于子节点（自下而上）。"""
    order_in_row: dict[int, list[int]] = {}
    for depth in sorted(rows):
        if depth == 0:
            order_in_row[depth] = sorted(rows[depth])
            continue
        above = order_in_row.get(depth - 1, [])
        rank = {node: index for index, node in enumerate(above)}

        def key(node: int, _rank=rank) -> tuple:
            parents = [p for p in (structure.before.get(node, ())
                                   + structure.kids.get(node, ())) if p in _rank]
            return (min((_rank[p] for p in parents), default=len(_rank)), int(node))

        order_in_row[depth] = sorted(rows[depth], key=key)

    x_of: dict[int, float] = {}
    for depth in sorted(order_in_row):
        row = order_in_row[depth]
        width = (len(row) * float(frame.card_w)
                 + float(frame.h_gap) * max(0, len(row) - 1))
        left = float(frame.axis) - width / 2.0
        for index, node in enumerate(row):
            x_of[node] = (left + float(frame.card_w) / 2.0
                          + index * (float(frame.card_w) + float(frame.h_gap)))
    # 自下而上：父节点挪到子节点重心（只改 x，层不动）
    for depth in sorted(order_in_row, reverse=True)[1:]:
        for node in order_in_row.get(depth - 1, []):
            kids = [kid for kid in structure.children.get(node, ()) if kid in x_of]
            if kids:
                x_of[node] = sum(x_of[kid] for kid in kids) / len(kids)
    # 重心挪动会把同一行的两个词撞在一起（各自往自己的孩子收）：每行按 x 拉开
    # 到一个卡片宽 + 间距，再整行挪回去 —— 保住这一行原本的重心，只消除重叠。
    gap = float(frame.card_w) + float(frame.h_gap)
    for depth in sorted(order_in_row):
        row = order_in_row[depth]
        if len(row) < 2:
            continue
        ordered = sorted(row, key=lambda node: (x_of[node], node))
        before = sum(x_of[node] for node in ordered)
        spread: list[float] = []
        for node in ordered:
            x = x_of[node]
            if spread and x - spread[-1] < gap:
                x = spread[-1] + gap
            spread.append(x)
        shift = (before - sum(spread)) / len(ordered)
        for node, x in zip(ordered, spread):
            x_of[node] = x + shift
    spots = {}
    for node in structure.order:
        if node not in x_of:
            continue
        depth = structure.depth.get(node, 0)
        spots[node] = (x_of[node],
                       level_top.get(depth, frame.top)
                       + level_h.get(depth, frame.height_of(node)) / 2.0)
    spots, _topic, dx = _shift(spots, frame)
    return Placement(spots=spots, axis_shift=dx)


def _place_oneway(structure: _Structure, frame: Frame) -> Placement:
    """单向导图：关系**向右**长 —— 层变成列，同一父的子节点在列内上下排开。"""
    height_cache: dict[int, float] = {}

    def block_h(node: int) -> float:
        if node in height_cache:
            return height_cache[node]
        kids = structure.children.get(node, ())
        total = (sum(block_h(kid) for kid in kids)
                 + float(frame.v_gap) * max(0, len(kids) - 1))
        height_cache[node] = max(frame.height_of(node), total)
        return height_cache[node]

    depths = sorted({structure.depth.get(node, 0) for node in structure.order})
    total_w = (float(frame.card_w) + float(frame.h_gap) * max(0, max(depths or [0])))
    left = float(frame.axis) - total_w / 2.0
    spots: dict[int, tuple[float, float]] = {}

    def assign(node: int, top: float) -> None:
        height = block_h(node)
        depth = structure.depth.get(node, 0)
        spots[node] = (left + float(frame.card_w) / 2.0
                       + depth * (float(frame.card_w) + float(frame.h_gap)),
                       top + height / 2.0)
        kids = structure.children.get(node, ())
        if not kids:
            return
        inner = (sum(block_h(kid) for kid in kids)
                 + float(frame.v_gap) * max(0, len(kids) - 1))
        cursor = top + max(0.0, (height - inner) / 2.0)
        for kid in kids:
            assign(kid, cursor)
            cursor += block_h(kid) + float(frame.v_gap)

    cursor = float(frame.top)
    for root in structure.roots:
        assign(root, cursor)
        cursor += block_h(root) + float(frame.v_gap)
    spots, _topic, dx = _shift(spots, frame)
    return Placement(spots=spots, axis_shift=dx)


# ----------------------------------------------------------------- 思维导图
def _place_mindmap(ids, rels, levels, frame: Frame) -> Placement:
    """思维导图：主题在**正中**，一级分支左右分列，子树向外横排、父居中于子。"""
    structure = _structure(ids, rels, levels)
    if not structure.order:
        return Placement()
    branches = list(structure.roots) or list(structure.order[:1])
    right = branches[0::2]
    left = branches[1::2]
    span_cache: dict[int, float] = {}

    def span(node: int) -> float:
        if node in span_cache:
            return span_cache[node]
        kids = structure.children.get(node, ())
        if not kids:
            span_cache[node] = frame.height_of(node)
            return span_cache[node]
        span_cache[node] = max(frame.height_of(node),
                               sum(span(kid) for kid in kids)
                               + float(frame.v_gap) * max(0, len(kids) - 1))
        return span_cache[node]

    spots: dict[int, tuple[float, float]] = {}
    column = float(frame.card_w) + float(frame.h_gap)
    side_gap = float(frame.topic_w) / 2.0 + float(frame.h_gap)

    def assign(node: int, depth: int, top: float, *, to_right: bool) -> None:
        height = span(node)
        offset = side_gap + float(frame.card_w) / 2.0 + depth * column
        x = float(frame.axis) + offset if to_right else float(frame.axis) - offset
        kids = structure.children.get(node, ())
        if not kids:
            spots[node] = (x, top + height / 2.0)
            return
        inner = (sum(span(kid) for kid in kids)
                 + float(frame.v_gap) * max(0, len(kids) - 1))
        cursor = top + max(0.0, (height - inner) / 2.0)
        centers: list[float] = []
        for kid in kids:
            assign(kid, depth + 1, cursor, to_right=to_right)
            centers.append(spots[kid][1])
            cursor += span(kid) + float(frame.v_gap)
        spots[node] = (x, (centers[0] + centers[-1]) / 2.0)

    heights = {
        "right": (sum(span(node) for node in right)
                  + float(frame.v_gap) * max(0, len(right) - 1)),
        "left": (sum(span(node) for node in left)
                 + float(frame.v_gap) * max(0, len(left) - 1)),
    }
    total_h = max(heights.values(), default=0.0)
    top = float(frame.pad)          # 主题挪到中间，上面这块空间正好还给内容
    for side, nodes in (("right", right), ("left", left)):
        cursor = top + max(0.0, (total_h - heights[side]) / 2.0)
        for node in nodes:
            assign(node, 0, cursor, to_right=(side == "right"))
            cursor += span(node) + float(frame.v_gap)
    topic_pos = (float(frame.axis), top + total_h / 2.0)
    spots, topic_pos, _dx = _shift(spots, frame, topic_pos)
    return Placement(spots=spots, topic_pos=topic_pos, hubs=tuple(branches))


# ------------------------------------------------------------------- 鱼骨图
def _place_fishbone(ids, rels, levels, frame: Frame) -> Placement:
    """鱼骨图：水平主脊 = 因果链，其余相关词斜挂在主脊两侧。"""
    structure = _structure(ids, rels, levels)
    if not structure.order:
        return Placement()
    causal = structure.causal
    if len(causal) < FISHBONE_MIN_CAUSAL:
        return Placement(note="这张图不太适合鱼骨图：因果边太少"
                              f"（只有 {len(causal)} 条），先按自动排。")
    parents_of: dict[int, set[int]] = {}
    kids_of: dict[int, list[int]] = {}
    for src, dst in causal:
        kids_of.setdefault(src, []).append(dst)
        parents_of.setdefault(dst, set()).add(src)
        parents_of.setdefault(src, set())
    indegree = {node: len(values) for node, values in parents_of.items()}
    level_of: dict[int, int] = {node: 0 for node in parents_of}
    queue = sorted([node for node, degree in indegree.items() if degree == 0])
    settled: set[int] = set()
    while queue:
        node = queue.pop(0)
        if node in settled:
            continue
        settled.add(node)
        for kid in kids_of.get(node, ()):
            level_of[kid] = max(level_of.get(kid, 0), level_of[node] + 1)
            indegree[kid] -= 1
            if indegree[kid] <= 0 and kid not in settled:
                queue.append(kid)
    for node in parents_of:                            # 环里的词也要上脊
        if node in settled:
            continue
        level_of[node] = max((level_of[p] + 1 for p in parents_of[node] if p in settled),
                             default=0)
        settled.add(node)
    spine = sorted(level_of, key=lambda node: (level_of[node], int(node)))
    if not spine:
        return Placement(note="这张图不太适合鱼骨图：找不到因果链，先按自动排。")
    spine_set = set(spine)
    others = [node for node in structure.order if node not in spine_set]
    spine_h = max(frame.height_of(node) for node in spine)
    branch_h = max([frame.height_of(node) for node in others], default=spine_h)
    spine_y = float(frame.top) + branch_h + float(frame.v_gap) + spine_h / 2.0
    step = float(frame.card_w) + float(frame.h_gap)
    total = step * max(0, len(spine) - 1) + float(frame.card_w)
    left = float(frame.axis) - total / 2.0
    spots: dict[int, tuple[float, float]] = {}
    for index, node in enumerate(spine):
        spots[node] = (left + float(frame.card_w) / 2.0 + index * step, spine_y)
    attach: dict[int, list[int]] = {}
    stray: list[int] = []
    for node in others:
        host = None
        for candidate in (structure.before.get(node, ()) + structure.kids.get(node, ())
                          + structure.near.get(node, ()) + structure.after.get(node, ())):
            if candidate in spots:
                host = candidate
                break
        if host is None:
            stray.append(node)
        else:
            attach.setdefault(host, []).append(node)
    index = 0
    for host in sorted(attach, key=lambda node: (spots[node][0], int(node))):
        for node in attach[host]:
            above = index % 2 == 0
            offset = branch_h / 2.0 + float(frame.v_gap) + frame.height_of(node) / 2.0
            spots[node] = (spots[host][0] + step * 0.55,
                           spine_y - offset if above else spine_y + offset)
            index += 1
    if stray:                                          # 挂不上主脊的：主脊下面排一行
        bottom = max(spot[1] for spot in spots.values()) + float(frame.v_gap) * 2.0
        width = (len(stray) * float(frame.card_w)
                 + float(frame.h_gap) * max(0, len(stray) - 1))
        start = float(frame.axis) - width / 2.0
        for position, node in enumerate(stray):
            spots[node] = (start + float(frame.card_w) / 2.0
                           + position * (float(frame.card_w) + float(frame.h_gap)),
                           bottom + frame.height_of(node) / 2.0)
    spots, _topic, dx = _shift(spots, frame)
    return Placement(spots=spots, axis_shift=dx, hubs=tuple(spine[:1]))


# ------------------------------------------------------------------- 流程线
def _place_flow(ids, rels, levels, frame: Frame, *, mode: str = "h") -> Placement:
    """流程线：按层级排一条链（水平 / 垂直 / S 型折行），与「谁连谁」无关。"""
    order = sorted({int(item) for item in ids},
                   key=lambda node: (int(levels.get(node, 0) or 0), int(node)))
    if not order:
        return Placement()
    step_x = float(frame.card_w) + float(frame.h_gap)
    spots: dict[int, tuple[float, float]] = {}

    def row_height(nodes) -> float:
        return max(frame.height_of(node) for node in nodes)

    if mode == "h":
        height = row_height(order)
        spots = {node: (float(frame.axis) - (len(order) - 1) * step_x / 2.0 + index * step_x,
                        float(frame.top) + height / 2.0)
                 for index, node in enumerate(order)}
    elif mode == "v":
        cursor = float(frame.top)
        for node in order:
            height = frame.height_of(node)
            spots[node] = (float(frame.axis), cursor + height / 2.0)
            cursor += height + float(frame.v_gap)
    else:                                              # S 型：折行 + 来回走
        per_row = max(2, int(round(math.sqrt(len(order)))))
        rows = [order[index:index + per_row] for index in range(0, len(order), per_row)]
        cursor = float(frame.top)
        for index, row in enumerate(rows):
            height = row_height(row)
            width = (len(row) - 1) * step_x
            start = float(frame.axis) - width / 2.0
            for position, node in enumerate(row):
                slot = position if index % 2 == 0 else len(row) - 1 - position
                spots[node] = (start + slot * step_x, cursor + height / 2.0)
            cursor += height + float(frame.v_gap) * 1.6
    spots, _topic, dx = _shift(spots, frame)
    return Placement(spots=spots, axis_shift=dx)


# --------------------------------------------------------------------- 注册表
def _tree_down(ids, rels, levels, frame):
    return _place_tree(ids, rels, levels, frame, orientation="down")


def _org(ids, rels, levels, frame):
    return _place_tree(ids, rels, levels, frame, strict_rows=True)


def _oneway(ids, rels, levels, frame):
    structure = _structure(ids, rels, levels)
    if not structure.order:
        return Placement()
    return _place_oneway(structure, frame)


def _flow_h(ids, rels, levels, frame):
    return _place_flow(ids, rels, levels, frame, mode="h")


def _flow_v(ids, rels, levels, frame):
    return _place_flow(ids, rels, levels, frame, mode="v")


def _flow_s(ids, rels, levels, frame):
    return _place_flow(ids, rels, levels, frame, mode="s")


#: 下拉里的全部模板（第一项是「自动」，永不删除：出问题永远能切回来）
TEMPLATES: tuple[TemplateSpec, ...] = (
    TemplateSpec(TEMPLATE_AUTO, "自动（按关系分层）",
                 "一层一行，关系越密越清楚",
                 "大多数图；不确定就用它"),
    TemplateSpec("mindmap", "思维导图",
                 "主题在正中，一级分支左右分列",
                 "围绕一个中心词发散、分支不多的图", placer=_place_mindmap),
    TemplateSpec("tree", "树状图",
                 "根在上，父节点居中于自己的子树",
                 "包含 / 属于这类父子关系清楚、层次深的图", placer=_tree_down),
    TemplateSpec("org", "组织架构图",
                 "一层一行、层内等距的金字塔",
                 "层级整齐、同一层有好几个词的图", placer=_org),
    TemplateSpec("oneway", "单向导图（向右）",
                 "关系一律向右长，子树上下排开",
                 "有明确起点、想看「一圈圈扩散」的图", placer=_oneway, route_axis="x"),
    TemplateSpec("fishbone", "鱼骨图",
                 "水平主脊 = 因果链，其余词斜挂在两侧",
                 f"因果边不少于 {FISHBONE_MIN_CAUSAL} 条的图", placer=_place_fishbone),
    TemplateSpec("flow-h", "流程线（水平）",
                 "按层级从左到右排一条链",
                 "大致是一条直线、步骤分明的图", placer=_flow_h),
    TemplateSpec("flow-v", "流程线（垂直）",
                 "按层级从上到下排一条链",
                 "步骤多、横向放不下的图", placer=_flow_v),
    TemplateSpec("flow-s", "流程线（S 型）",
                 "折行、来回走的链",
                 "步骤很多、想一屏看完的图", placer=_flow_s),
)
#: 供下拉直接用的顺序（与 ``TEMPLATES`` 一致）
TEMPLATE_KEYS: tuple[str, ...] = tuple(spec.key for spec in TEMPLATES)
_BY_KEY: dict[str, TemplateSpec] = {spec.key: spec for spec in TEMPLATES}


def route_axis_of(key: str) -> str:
    """这个模板的**走线主轴**（未知 key 按上下流处理）。

    ``"y"`` = 上下流（行是横带，车道横着走）；``"x"`` = 左右流（整张图转置
    90° 后用同一套规则算，车道竖着走）。左右长的模板不转置的话，「行」会
    退化成一整条 —— 实测 ``flow-h`` 里四种 row 挤在同一条 y 上，排线规则
    于是时灵时不灵。
    """
    spec = spec_of(key)
    return spec.route_axis if spec is not None else "y"


def spec_of(key: str) -> TemplateSpec | None:
    """按 key 取模板说明（未知 key → None）。"""
    return _BY_KEY.get(str(key or "").strip())


def name_of(key: str) -> str:
    """模板的中文名（未知 key 原样返回，便于把脏配置显示出来）。"""
    spec = spec_of(key)
    return spec.name if spec is not None else str(key or "")


def place(key: str, *, ids: Iterable[int], rels: Iterable, levels: Mapping[int, int],
          frame: Frame) -> Placement | None:
    """按模板摆一遍（``auto`` / 未知 key → None = 让调用方走默认布局）。

    绝不让一张图因为摆放失败而画不出来：任何异常都退回 None（并写一条日志）。
    """
    spec = spec_of(key)
    if spec is None or spec.placer is None:
        return None
    try:
        result = spec.placer(list(ids), list(rels or ()), dict(levels or {}), frame)
    except Exception:                                  # pragma: no cover - 兜底
        log.exception("模板 %s 摆放失败，退回自动布局", key)
        return None
    if result is None:
        return None
    if not isinstance(result, Placement):              # pragma: no cover - 自检
        log.warning("模板 %s 返回了意料之外的结果：%r", key, type(result))
        return None
    return result


# ------------------------------------------------------------------ 本地建议
def suggest(rels: Iterable, *, total: int = 0) -> tuple[str, str]:
    """**本地规则**挑一个模板（零成本、可解释）：``(key, 理由)``。

    这是「LLM 挑模板」的第一层：模型没配 / 没开开关 / 失败时都用它，而且理由
    永远是数据本身（数了几条什么类型的边），不是模型的自由发挥。
    """
    counts: dict[str, int] = {}
    pairs: set[tuple[int, int, str]] = set()
    for rel in rels or ():
        src, dst, kind = _relation_parts(rel)
        if src == dst:
            continue
        pairs.add((src, dst, kind))
        counts[kind] = counts.get(kind, 0) + 1
    edges = len(pairs)
    if edges < 2:
        return TEMPLATE_AUTO, ""
    causal = counts.get(CAUSAL, 0)
    hierarchy = counts.get(CONTAINS, 0) + counts.get(BELONGS, 0)
    if causal >= FISHBONE_MIN_CAUSAL and causal * 2 >= edges:
        return "fishbone", f"因果边占了一半以上（{causal}/{edges}）"
    if hierarchy >= 2 and hierarchy * 2 >= edges:
        return "org", f"包含 / 属于这类层级边占了一半以上（{hierarchy}/{edges}）"
    if counts.get(DEPEND, 0) >= 2 and counts.get(DEPEND, 0) * 2 >= edges:
        return "flow-v", f"依赖边为主（{counts.get(DEPEND, 0)}/{edges}），像是在讲先后"
    if edges >= 4 and len({src for src, _dst, _kind in pairs}) == 1:
        return "mindmap", "几乎所有关系都从同一个词发散出去"
    if edges >= 3:
        return "flow-h", f"关系不算密（{edges} 条），先按一条链看"
    return TEMPLATE_AUTO, ""
