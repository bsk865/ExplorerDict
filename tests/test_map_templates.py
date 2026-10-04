"""布局骨架（模板，``app/ui/map_templates.py``）：批次 G 的纯几何测试。

用户口径（原话）
----------------
「我在想是否可以加入模板？LLM 可以挑选合适的模板进行生成，用户也可以自己选择
模板进行自定义画导图。」

这里测的是**骨架本身**：每个模板都要「一个词都不丢、卡片不叠、不出留白」，
形状还得真的是它自己宣称的那个样子（树状图父在上、架构图一层一行、单向图往右、
思维导图主题居中、鱼骨图有主脊、流程线成链）；再加上本地规则 ``suggest()`` 的
挑法与「摆不成时如实说、退回自动」的兜底。

硬约束
------
* **不创建任何 Tk 窗口**（本文件不 ``import tkinter``）：几何全是纯函数，
  窗口里的按钮 / 确认框 / 回调线程在 ``tests/test_ui_roundrect.py`` 里测。
* 任何模板都不许让图**画不出来**：算不出来就退回「自动」，只留一句说明。
"""
from __future__ import annotations

import unittest

from app.map_service import MapRelation
from app.ui import concept_map as cm
from app.ui import map_templates as mt

#: 夹具：八个词 + 八条混合关系（层级 / 因果 / 依赖 / 对照都有）
LABELS = {1: "卷积网络", 2: "卷积层", 3: "池化层", 4: "过拟合",
          5: "正则化", 6: "降采样", 7: "特征图", 8: "感受野"}
RELATIONS = ((1, 2, "包含"), (2, 3, "包含"), (1, 4, "包含"),
             (4, 5, "因果"), (5, 6, "因果"), (6, 7, "依赖"), (7, 8, "依赖"),
             (3, 6, "对照"))
#: 只用前六个词的那几棵树（7、8 不与它们相连，免得混进「孤立的根」）
TREE_LABELS = {key: LABELS[key] for key in (1, 2, 3, 4, 5, 6)}
#: 一张「全是因果」的图（鱼骨图的合格材料）
CAUSAL_RELATIONS = ((1, 2, "因果"), (2, 3, "因果"), (3, 4, "因果"))
#: 一张「全是层级」的图（树状图 / 架构图 / 思维导图的合格材料）
TREE_RELATIONS = ((1, 2, "包含"), (1, 3, "包含"), (2, 4, "包含"),
                  (2, 5, "包含"), (3, 6, "包含"))
#: 两个根的层级图（思维导图的左右分列要两支才看得出来）
TWO_ROOT_RELATIONS = ((1, 2, "包含"), (1, 3, "包含"), (4, 5, "包含"))
#: 全部会用到的骨架（``auto`` 不摆，单独测）
KEYS = tuple(spec.key for spec in mt.TEMPLATES if spec.placer is not None)

EPS = 1e-6


def _relations(items=RELATIONS) -> list:
    return [MapRelation(src, dst, rel_type, "离线假依据", "离线假证据片段")
            for src, dst, rel_type in items]


def _frame(**extra) -> mt.Frame:
    """一个与 ``_layout_core`` 里同量级的画布框（900×700、两侧留白 24）。"""
    pad = extra.pop("pad", 24.0)
    card_w = extra.pop("card_w", 140.0)
    card_h = extra.pop("card_h", 44.0)
    ids = extra.pop("ids", sorted(LABELS))
    values = dict(axis=(extra.pop("width", 900.0)) / 2.0, top=pad + 60.0,
                  card_w=card_w, card_h={int(item): float(card_h) for item in ids},
                  topic_w=180.0, topic_h=52.0, h_gap=28.0, v_gap=24.0, pad=pad)
    values.update(extra)
    return mt.Frame(**values)


def _levels(items=RELATIONS) -> dict:
    """关系 → 每个词的层号（模板只把它当参考，这里给一份合理的）。"""
    order = {node: index for index, node in enumerate(sorted(LABELS))}
    return dict(order)


def _spots(key, *, labels=LABELS, items=RELATIONS, frame=None):
    return mt.place(key, ids=sorted(labels), rels=_relations(items),
                    levels=_levels(items), frame=frame or _frame(ids=sorted(labels)))


def _rect(placement, entry_id, frame) -> tuple[float, float, float, float]:
    x, y = placement.spots[entry_id]
    half_w = frame.card_w / 2.0
    half_h = frame.height_of(entry_id) / 2.0
    return (x - half_w, y - half_h, x + half_w, y + half_h)


def _overlaps(first, second, *, tolerance: float = 1.0) -> bool:
    return (first[0] < second[2] - tolerance and second[0] < first[2] - tolerance
            and first[1] < second[3] - tolerance and second[1] < first[3] - tolerance)


def _layout(key, *, labels=LABELS, items=RELATIONS, width=900.0, height=700.0, pins=None):
    return cm.layout_graph(labels, _relations(items), width=width, height=height,
                           topic_label="卷积网络", template=key, pins=pins)


def _rows(spots: dict) -> dict:
    """按 y 把卡片分行（同一行的 y 必须一模一样）。"""
    rows: dict[float, list[int]] = {}
    for entry_id, (_x, y) in spots.items():
        rows.setdefault(round(float(y), 6), []).append(entry_id)
    return rows


def _depths(labels, items) -> dict:
    """按「包含 / 属于」数出每个词的层号（环也安全，只为断言「谁比谁深」）。"""
    parent: dict[int, int] = {}
    for src, dst, kind in items:
        if kind == "包含":
            parent.setdefault(dst, src)
        elif kind == "属于":
            parent.setdefault(src, dst)
    depth: dict[int, int] = {}
    for node in sorted(labels):
        level, walker, guard = 0, node, 0
        while walker in parent and guard < len(labels):
            walker = parent[walker]
            level += 1
            guard += 1
        depth[node] = level
    return depth


class TestTemplateCatalog(unittest.TestCase):
    """下拉里有什么、每一项说清了什么（用户要自己挑，就得挑得明白）。"""

    def test_the_dropdown_starts_with_auto_and_then_lists_a_placer_each(self):
        self.assertEqual(mt.TEMPLATES[0].key, mt.TEMPLATE_AUTO,
                         "第一项永远是「自动」：出问题总得能切回来")
        self.assertIsNone(mt.TEMPLATES[0].placer, "「自动」不摆 —— 交给原布局")
        self.assertEqual(mt.TEMPLATE_KEYS, tuple(spec.key for spec in mt.TEMPLATES))
        self.assertEqual(len(set(mt.TEMPLATE_KEYS)), len(mt.TEMPLATE_KEYS), "key 不许重复")
        for spec in mt.TEMPLATES[1:]:
            self.assertIsNotNone(spec.placer, f"{spec.key} 没有摆放函数")
        names = [spec.name for spec in mt.TEMPLATES]
        self.assertEqual(len(set(names)), len(names), "显示名也不许重复")

    def test_every_spec_says_how_it_lays_out_and_when_to_use_it(self):
        for spec in mt.TEMPLATES:
            self.assertTrue(spec.summary.strip(), f"{spec.key} 没写「怎么摆」")
            self.assertTrue(spec.fit.strip(), f"{spec.key} 没写「适合什么图」")
            self.assertNotIn("见上", spec.summary, "说明要能独立看懂")

    def test_the_catalog_covers_the_six_requested_shapes(self):
        """用户勾的六项（思维导图 / 树状图 / 组织架构图 / 单向导图 / 鱼骨图 / 流程线）。"""
        for key in ("mindmap", "tree", "org", "oneway", "fishbone"):
            self.assertIn(key, mt.TEMPLATE_KEYS)
        for key in ("flow-h", "flow-v", "flow-s"):
            self.assertIn(key, mt.TEMPLATE_KEYS, "流程线三种走向都要有")
        self.assertNotIn("outline", mt.TEMPLATE_KEYS, "「大纲」这一版不做")

    def test_unknown_keys_fall_back_to_auto_without_saying_anything(self):
        self.assertIsNone(mt.spec_of("nope"))
        self.assertIsNone(mt.spec_of(""))
        self.assertEqual(mt.name_of("nope"), "nope", "脏配置照原样显示，便于查")
        self.assertEqual(mt.name_of(mt.TEMPLATE_AUTO), mt.TEMPLATES[0].name)
        self.assertIsNotNone(mt.spec_of(" tree "))
        for key in (mt.TEMPLATE_AUTO, "nope", ""):
            self.assertIsNone(_spots(key), f"{key} 不该摆")


class TestEverySkeletonIsSafe(unittest.TestCase):
    """共同底线：一个词都不丢、不叠、不出留白（摆不成就退回，不许硬来）。"""

    def test_every_skeleton_places_every_word(self):
        for key in KEYS:
            placement = _spots(key)
            self.assertIsNotNone(placement, key)
            self.assertEqual(set(placement.spots), set(LABELS),
                             f"{key} 漏了词：{sorted(set(LABELS) - set(placement.spots))}")

    def test_every_skeleton_keeps_the_cards_inside_the_padding(self):
        frame = _frame()
        for key in KEYS:
            placement = _spots(key, frame=frame)
            xs = [x for x, _y in placement.spots.values()]
            ys = [y for _x, y in placement.spots.values()]
            self.assertGreaterEqual(min(xs) - frame.card_w / 2.0, frame.pad - 1e-3,
                                    f"{key} 左边顶到画布外了")
            self.assertGreaterEqual(min(ys) - frame.height_of(1) / 2.0, frame.pad - 1e-3,
                                    f"{key} 上边顶到画布外了")

    def test_no_two_cards_overlap_in_any_skeleton(self):
        frame = _frame()
        for key in KEYS:
            placement = _spots(key, frame=frame)
            rects = {entry_id: _rect(placement, entry_id, frame)
                     for entry_id in placement.spots}
            for first in sorted(rects):
                for second in sorted(rects):
                    if second <= first:
                        continue
                    self.assertFalse(_overlaps(rects[first], rects[second]),
                                     f"{key}：{first} 和 {second} 叠在一起了")

    def test_no_word_is_ever_dropped_when_the_graph_is_weird(self):
        """环、自环、断链、只有一个词：一个都不能丢，也不许抛异常。"""
        weird = ((1, 1, "因果"), (1, 2, "包含"), (2, 1, "包含"),
                 (3, 3, "对照"), (5, 4, "依赖"))
        for key in KEYS:
            placement = _spots(key, items=weird)
            self.assertIsNotNone(placement, key)
            if key == "fishbone":
                continue          # 因果只有 1 条 → 它**故意**不摆（另有专门的测试）
            self.assertEqual(set(placement.spots), set(LABELS), key)
        single = _spots("tree", labels={7: "只有一个"}, items=())
        self.assertEqual(set(single.spots), {7})

    def test_a_cycle_inside_the_hierarchy_still_gets_laid_out(self):
        """1 包含 2、2 包含 1：环上的词不许因为「谁都不是根」而整棵子树消失。"""
        cycle = ((1, 2, "包含"), (2, 1, "包含"), (3, 4, "包含"))
        for key in ("tree", "org", "oneway", "mindmap"):
            placement = _spots(key, items=cycle)
            self.assertEqual(set(placement.spots), set(LABELS), key)
            head = {1, 2}
            self.assertTrue(head.issubset(set(placement.spots)), key)


class TestTreeShapes(unittest.TestCase):
    """树状图 / 组织架构图 / 单向导图：三种都建立在同一份父子结构上。"""

    def _tree_spots(self, key, items=TREE_RELATIONS):
        return _spots(key, labels=TREE_LABELS, items=items,
                      frame=_frame(ids=sorted(TREE_LABELS)))

    def test_the_tree_puts_a_parent_above_its_own_children(self):
        placement = self._tree_spots("tree")
        above = {src: dst for src, dst, _kind in TREE_RELATIONS}
        for parent, child in above.items():
            self.assertLess(placement.spots[parent][1], placement.spots[child][1],
                            f"{parent} 应该在自己孩子 {child} 上面")

    def test_the_tree_centers_a_parent_over_its_subtree(self):
        """父节点的 x = **子树块的几何中心**（不是子节点 x 的算术平均）。"""
        frame = _frame(ids=sorted(TREE_LABELS))
        placement = self._tree_spots("tree")
        for parent, descendants in ((1, (2, 3, 4, 5, 6)), (2, (4, 5)), (3, (6,))):
            left = min(placement.spots[node][0] for node in descendants) - frame.card_w / 2.0
            right = max(placement.spots[node][0] for node in descendants) + frame.card_w / 2.0
            self.assertAlmostEqual(placement.spots[parent][0], (left + right) / 2.0,
                                   places=3, msg=f"{parent} 没有居中于自己的子树")

    def test_the_tree_keeps_each_subtree_in_its_own_block(self):
        """2 的两个孩子（4、5）必须在 2 的子树块里，不能跑到 3 那边去。"""
        placement = self._tree_spots("tree")
        left_two = max(placement.spots[4][0], placement.spots[5][0])
        self.assertLess(left_two, placement.spots[3][0] - 1.0,
                        "子树块之间不许交错")

    def test_the_org_chart_keeps_one_height_per_row(self):
        placement = self._tree_spots("org")
        rows = _rows(placement.spots)
        self.assertEqual(sorted(rows), sorted({round(y, 6) for _x, y in
                                               placement.spots.values()}))
        for y, members in rows.items():
            xs = sorted(placement.spots[entry_id][0] for entry_id in members)
            gaps = [round(b - a, 6) for a, b in zip(xs, xs[1:])]
            self.assertLessEqual(len(set(gaps)), 1,
                                 f"第 {y} 行没有等距（{gaps}）")

    def test_the_org_chart_deepens_row_by_row(self):
        """深度越大的词，y 只能更大 —— 金字塔不许倒过来。"""
        placement = self._tree_spots("org")
        depth = _depths(TREE_LABELS, TREE_RELATIONS)
        for node, level in depth.items():
            for other, other_level in depth.items():
                if level < other_level:
                    self.assertLess(placement.spots[node][1], placement.spots[other][1],
                                    f"{node} 比 {other} 层浅，应该在上面")

    def test_the_oneway_map_always_grows_to_the_right(self):
        placement = self._tree_spots("oneway")
        depth = _depths(TREE_LABELS, TREE_RELATIONS)
        for parent, child in ((src, dst) for src, dst, _kind in TREE_RELATIONS):
            self.assertLess(placement.spots[parent][0], placement.spots[child][0],
                            f"{parent} → {child} 应该向右长")
        for node, level in depth.items():
            for other, other_level in depth.items():
                if level < other_level:
                    self.assertLessEqual(placement.spots[node][0],
                                         placement.spots[other][0] + EPS)

    def test_the_oneway_map_stacks_a_layer_into_one_column(self):
        placement = self._tree_spots("oneway")
        column = {2: {4, 5}}
        for parent, kids in column.items():
            xs = {round(placement.spots[kid][0], 6) for kid in kids}
            self.assertEqual(len(xs), 1, "同一层的兄弟必须对齐成一列")
            self.assertNotAlmostEqual(placement.spots[2][0], placement.spots[4][0], places=3)


class TestMindMapShape(unittest.TestCase):
    """思维导图：主题在正中，一级分支左右分列，子树跟着自己那一支走。"""

    def _mindmap(self, *, labels=LABELS, items=RELATIONS):
        frame = _frame(ids=sorted(labels))
        return _spots("mindmap", labels=labels, items=items, frame=frame), frame

    def test_the_mindmap_puts_the_topic_in_the_middle_of_the_cards(self):
        placement, frame = self._mindmap()
        self.assertIsNotNone(placement.topic_pos)
        topic_x, topic_y = placement.topic_pos
        ys = [y for _x, y in placement.spots.values()]
        self.assertAlmostEqual(topic_y, (min(ys) + max(ys)) / 2.0, places=3,
                               msg="主题要落在卡片带的中间")
        self.assertAlmostEqual(topic_x, frame.axis + placement.axis_shift, places=3,
                               msg="主题留在中轴上（左右长出去的部分由 axis_shift 说明）")

    def test_the_mindmap_splits_the_branches_left_and_right(self):
        placement, frame = self._mindmap(labels=TREE_LABELS, items=TWO_ROOT_RELATIONS)
        axis = frame.axis + placement.axis_shift
        left = [entry_id for entry_id, (x, _y) in placement.spots.items() if x < axis]
        right = [entry_id for entry_id, (x, _y) in placement.spots.items() if x > axis]
        self.assertTrue(left, "至少要有一支往左（否则就成单向图了）")
        self.assertTrue(right, "至少要有一支往右")
        self.assertEqual(len(left) + len(right), len(TREE_LABELS))

    def test_the_mindmap_connects_the_topic_to_its_first_level(self):
        placement, _frame_used = self._mindmap(labels=TREE_LABELS, items=TREE_RELATIONS)
        self.assertTrue(placement.hubs, "思维导图要走直线母线（主题 → 一级分支）")
        self.assertEqual(set(placement.hubs), {1}, "一级分支就是没有父节点的那些词")
        two = self._mindmap(labels=TREE_LABELS, items=TWO_ROOT_RELATIONS)[0]
        self.assertEqual(set(two.hubs), {1, 4, 6},
                         "两支都要连到主题；6 谁也不连，自己也是一支")

    def test_a_single_root_mindmap_stays_honestly_on_one_side(self):
        """只有一个根时不假装对称：主题 + 一向排开的子树，一个词都不许挪到对面。"""
        placement, frame = self._mindmap(labels=TREE_LABELS, items=TREE_RELATIONS)
        axis = frame.axis + placement.axis_shift
        xs = [x for x, _y in placement.spots.values()]
        self.assertTrue(all(x > axis for x in xs), "单根时全在右边，不做假对称")
        self.assertEqual(set(placement.hubs), {1})

    def test_an_isolated_word_is_its_own_branch(self):
        """只靠依赖 / 因果挂着的词，自己就是一支（也要连到主题）。"""
        placement, _frame_used = self._mindmap()
        # 6 依赖 7、7 依赖 8 ⇒ 8 在最前面（``依赖`` 的方向是「被依赖的先」）
        self.assertEqual(set(placement.hubs), {1, 8})
        self.assertEqual(set(placement.spots), set(LABELS))

    def test_every_branch_keeps_its_own_subtree_on_its_own_side(self):
        placement, frame = self._mindmap(labels=TREE_LABELS, items=TWO_ROOT_RELATIONS)
        axis = frame.axis + placement.axis_shift
        depth = _depths(TREE_LABELS, TWO_ROOT_RELATIONS)
        parents = {dst: src for src, dst, _kind in TWO_ROOT_RELATIONS}
        for node, level in depth.items():
            if level == 0:
                continue
            side = 1.0 if placement.spots[node][0] > axis else -1.0
            parent = parents[node]
            if parent in (1, 4):
                continue              # 一级分支自己决定左右，子节点跟着它就行
            parent_side = 1.0 if placement.spots[parent][0] > axis else -1.0
            self.assertEqual(side, parent_side, f"{node} 跑到父节点对面去了")


class TestFishboneShape(unittest.TestCase):
    def test_the_fishbone_asks_for_at_least_two_causal_edges(self):
        placement = _spots("fishbone", items=((1, 2, "因果"), (2, 3, "包含")))
        self.assertEqual(placement.spots, {}, "摆不成就不许乱摆")
        self.assertIn("鱼骨图", placement.note)
        self.assertIn("1", placement.note, "说清数到了几条因果边")

    def test_the_fishbone_runs_the_causal_chain_along_one_spine(self):
        placement = _spots("fishbone", items=CAUSAL_RELATIONS)
        self.assertTrue(placement.hubs, "主题要连到主脊的第一节")
        self.assertEqual(set(placement.hubs), {1})
        positions = [placement.spots[node] for node in (1, 2, 3, 4)]
        xs = [x for x, _y in positions]
        self.assertEqual(xs, sorted(xs), "主脊上的词要按因果顺序排开")
        ys = [y for _x, y in positions]
        self.assertLessEqual(max(ys) - min(ys), 1e-6, "主脊是一条水平线")

    def test_the_fishbone_hangs_the_other_words_on_both_sides(self):
        items = CAUSAL_RELATIONS + ((1, 5, "包含"), (2, 6, "依赖"), (3, 7, "用途"))
        placement = _spots("fishbone", items=items)
        self.assertEqual(set(placement.spots), set(LABELS))
        spine_y = placement.spots[1][1]
        above = [node for node in (5, 6, 7, 8) if placement.spots[node][1] < spine_y - EPS]
        below = [node for node in (5, 6, 7, 8) if placement.spots[node][1] > spine_y + EPS]
        self.assertTrue(above, "挂在主脊上面的骨头不见了")
        self.assertTrue(below, "挂在主脊下面的骨头不见了")

    def test_a_word_with_no_host_lands_under_the_spine_instead_of_vanishing(self):
        items = CAUSAL_RELATIONS + ((8, 8, "对照"),)
        placement = _spots("fishbone", items=items)
        self.assertEqual(set(placement.spots), set(LABELS))


class TestFlowLineShape(unittest.TestCase):
    def test_the_horizontal_flow_is_one_row_read_left_to_right(self):
        placement = _spots("flow-h", items=TREE_RELATIONS)
        ys = {round(y, 6) for _x, y in placement.spots.values()}
        self.assertEqual(len(ys), 1, "水平流程线只有一个高度")
        xs = [placement.spots[node][0] for node in sorted(LABELS)]
        self.assertEqual(xs, sorted(xs), "从左到右必须是有序的")

    def test_the_vertical_flow_is_one_column_read_top_to_bottom(self):
        placement = _spots("flow-v", items=TREE_RELATIONS)
        xs = {round(x, 6) for x, _y in placement.spots.values()}
        self.assertEqual(len(xs), 1, "垂直流程线只有一列")
        ys = [placement.spots[node][1] for node in sorted(LABELS)]
        self.assertEqual(ys, sorted(ys), "从上到下必须是有序的")

    def test_the_s_flow_folds_back_on_itself(self):
        """折行后，**链序**在偶数行从左往右、奇数行从右往左（S 型，不是 Z 型）。"""
        placement = _spots("flow-s", items=TREE_RELATIONS)
        rows = _rows(placement.spots)
        self.assertGreaterEqual(len(rows), 2, "S 型至少要折一次")
        levels = _levels(TREE_RELATIONS)
        chain = sorted(LABELS, key=lambda node: (levels.get(node, 0), node))
        for index, (_y, members) in enumerate(sorted(rows.items())):
            line = [node for node in chain if node in members]
            xs = [placement.spots[node][0] for node in line]
            if index % 2 == 0:
                self.assertEqual(xs, sorted(xs), f"第 {index + 1} 行应该从左往右")
            else:
                self.assertEqual(xs, sorted(xs, reverse=True),
                                 f"第 {index + 1} 行应该折回来（从右往左）")

    def test_every_flow_mode_keeps_every_word(self):
        for key in ("flow-h", "flow-v", "flow-s"):
            placement = _spots(key, items=TREE_RELATIONS)
            self.assertEqual(set(placement.spots), set(LABELS), key)


class TestLocalSuggestion(unittest.TestCase):
    """本地规则（零成本、可解释）：先按关系类型数数，再决定推不推。"""

    def _suggest(self, items, *, total=None):
        return mt.suggest(_relations(items), total=total or len(LABELS))

    def test_a_causal_heavy_graph_suggests_the_fishbone(self):
        key, reason = self._suggest(CAUSAL_RELATIONS)
        self.assertEqual(key, "fishbone")
        self.assertIn("因果", reason)

    def test_a_hierarchy_heavy_graph_suggests_the_org_chart(self):
        key, reason = self._suggest(TREE_RELATIONS)
        self.assertEqual(key, "org")
        self.assertIn("层级", reason)

    def test_a_dependency_chain_suggests_the_vertical_flow(self):
        key, reason = self._suggest(((1, 2, "依赖"), (2, 3, "依赖"), (3, 4, "依赖")))
        self.assertEqual(key, "flow-v")
        self.assertIn("先后", reason)

    def test_a_sparse_graph_suggests_a_plain_chain(self):
        key, reason = self._suggest(((1, 2, "用途"), (2, 3, "对照"), (3, 4, "用途")))
        self.assertEqual(key, "flow-h")
        self.assertIn("3", reason, "理由要说清数了几条")

    def test_a_graph_with_a_single_source_suggests_the_mindmap(self):
        items = ((1, 2, "用途"), (1, 3, "对照"), (1, 4, "用途"), (1, 5, "对照"),
                 (2, 3, "用途"))
        key, reason = self._suggest(items)
        self.assertEqual(key, "flow-h", "层级 / 因果 / 依赖都不占多数时按链看")
        dense = ((1, 2, "用途"), (1, 3, "对照"), (1, 4, "用途"), (1, 5, "对照"))
        key, reason = self._suggest(dense)
        self.assertEqual(key, "mindmap")
        self.assertIn("发散", reason)

    def test_too_few_relations_means_keep_auto(self):
        for items in ((), ((1, 2, "包含"),)):
            key, reason = self._suggest(items)
            self.assertEqual(key, mt.TEMPLATE_AUTO)
            self.assertEqual(reason, "", "没建议就不写理由（界面上不留空话）")

    def test_a_self_loop_does_not_count_as_a_relation(self):
        key, _reason = self._suggest(((1, 1, "因果"),))
        self.assertEqual(key, mt.TEMPLATE_AUTO)

    def test_the_suggestion_always_names_a_real_template(self):
        for items in ((), ((1, 2, "包含"),), CAUSAL_RELATIONS, TREE_RELATIONS,
                      ((1, 2, "依赖"), (2, 3, "依赖"))):
            key, _reason = self._suggest(items)
            self.assertIsNotNone(mt.spec_of(key), key)


class TestSkeletonsInTheRealLayout(unittest.TestCase):
    """走真正的 ``layout_graph`` / ``layout_extent``（连线、外框、固定位置都在里面）。"""

    def test_the_layout_reports_which_skeleton_it_used(self):
        for key in KEYS:
            layout = _layout(key)
            self.assertEqual(layout.template, key)
            self.assertEqual(layout.template_note, "", f"{key} 不该有失败说明")
        auto = _layout(mt.TEMPLATE_AUTO)
        self.assertEqual(auto.template, "", "「自动」不算模板（导出页脚不写它）")
        self.assertEqual(auto.template_note, "")

    def test_every_word_survives_every_skeleton_in_the_real_layout(self):
        for key in KEYS:
            layout = _layout(key)
            self.assertEqual({int(node.entry_id) for node in layout.nodes}, set(LABELS), key)
            for node in layout.nodes:
                self.assertIsNotNone(layout.find(int(node.entry_id)))

    def test_the_skeleton_does_not_move_a_card_the_user_pinned(self):
        pinned = (777.0, 555.0)
        layout = _layout("tree", pins={2: pinned})
        self.assertAlmostEqual(layout.find(2).x, pinned[0], places=3)
        self.assertAlmostEqual(layout.find(2).y, pinned[1], places=3)
        plain = _layout("tree")
        for entry_id in (3, 4, 5, 6):
            self.assertAlmostEqual(layout.find(entry_id).x, plain.find(entry_id).x, places=3,
                                   msg="固定一张卡不该动别的卡")

    def test_a_skeleton_that_cannot_fit_says_so_and_falls_back_to_auto(self):
        """鱼骨图却没有因果边：**坐标不动**、如实说一句、随时能切回自动。"""
        items = ((1, 2, "包含"), (2, 3, "包含"), (3, 4, "包含"))
        layout = _layout("fishbone", items=items)
        self.assertEqual(layout.template, "", "摆不成就不算用了模板")
        self.assertIn("鱼骨图", layout.template_note)
        self.assertIn("自动", layout.template_note)
        auto = _layout(mt.TEMPLATE_AUTO, items=items)
        for node in layout.nodes:
            twin = auto.find(int(node.entry_id))
            self.assertAlmostEqual(node.x, twin.x, places=6, msg="失败时要一字不动")
            self.assertAlmostEqual(node.y, twin.y, places=6)
        self.assertEqual(auto.find(1).x, layout.find(1).x)

    def test_the_extent_never_underestimates_a_skeleton(self):
        """首开自适应用的是 ``layout_extent``：算小了就会一开图就被裁。"""
        for key in KEYS:
            extent_w, extent_h = cm.layout_extent(LABELS, _relations(), width=900.0,
                                                  height=700.0, topic_label="卷积网络",
                                                  template=key)
            layout = _layout(key)
            self.assertGreaterEqual(extent_w + 1e-6, layout.content_w, key)
            self.assertGreaterEqual(extent_h + 1e-6, layout.content_h, key)

    def test_switching_back_to_auto_gives_the_original_layout(self):
        """「随时切回自动」不是空话：切回去必须与从没用过模板时逐像素一致。"""
        plain = cm.layout_graph(LABELS, _relations(), width=900.0, height=700.0,
                                topic_label="卷积网络")
        for key in ("tree", "flow-v"):
            layout = _layout(key)
            back = cm.layout_graph(LABELS, _relations(), width=900.0, height=700.0,
                                   topic_label="卷积网络", template=mt.TEMPLATE_AUTO)
            for node in back.nodes:
                twin = plain.find(int(node.entry_id))
                self.assertAlmostEqual(node.x, twin.x, places=6, msg=key)
                self.assertAlmostEqual(node.y, twin.y, places=6, msg=key)
            self.assertNotEqual(layout.template, "")
        self.assertEqual(plain.template, "")
        self.assertEqual(len(plain.edges), len(back.edges), "切回来连线也一模一样")
        self.assertAlmostEqual(plain.content_w, back.content_w, places=6)
        self.assertAlmostEqual(plain.content_h, back.content_h, places=6)


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
