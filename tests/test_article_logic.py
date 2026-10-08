"""「按原文归纳」的用例（**零联网、零真实模型**：假 client 直接回 JSON）。

这条流水线的关键是**第四步的校验不许靠模型**：模型说「我归纳好了」不算数，
必须由 :func:`app.map_service.validate_logic` 拿代码逐条算 —— 分支够不够、
实体是不是原文里真有的、有先后的边有没有被讲反。所以这里的主力用例是
**构造一份「看起来很像样、其实不合格」的输出，断言它必须被判不合格**：

* 时序文章被归纳成并列从属（用户要的正是这个不许发生）；
* 实体在原文里找不到（编词）；
* 没有原文却照样归纳（凭空造图）；
* 分支只有一个 / 分支名叫「其它」。

以及流水线的留痕行为：不合格要带着**差异清单**重试，重试用尽要如实报错，
模型调用失败不能抛出去炸界面。

覆盖：
* ``parse_article_logic`` 的形状整理与四种失败（空 / 非 JSON / 非对象 / 没有分支）；
* ``build_article_material`` 的段落边界截断 + 如实标注；
* ``build_article_logic_payload``：temperature=0、反馈接在 user 末尾、材料不含密钥；
* ``validate_logic`` 的分支 / 关系 / 逆行 / 编词 / 无原文五类判定；
* ``logic_flow`` / ``logic_levels`` / ``logic_to_map_relations`` 的纯函数行为；
* ``run_logic_graph`` 的重试、上限、失败兜底。
"""
from __future__ import annotations

import json
import unittest

from app.api_client import (
    ARTICLE_ENTITY_CHARS, ARTICLE_LOGIC_TYPES, ARTICLE_VARY_MAX,
    ARTICLE_VARY_STEPS, ApiError, article_vary, build_article_logic_payload,
    build_article_material, build_article_vary_note, parse_article_logic,
)
from app.map_service import (
    LOGIC_MAX_ATTEMPTS, LOGIC_MIN_BRANCHES, LOGIC_TEXT_MATCH_MIN, LogicRelation,
    MapNode, content_fingerprint, logic_feedback, logic_levels, logic_rel_pairs,
    logic_flow, logic_text_of, logic_to_map_relations, logic_vary_label,
    run_logic_graph, validate_logic, vary_plan,
)

#: 一篇「先讲问题、再讲方案、最后实测」的时序文章（测试用的最小原文）。
ARTICLE = (
    "很多团队都遇到过检索质量差的问题，回答里经常出现与问题无关的内容。"
    "为了解决这个问题，我们提出了重排方案：先召回一批候选段落，再用重排模型重新打分。"
    "重排模型只看问题与段落的配对，因此比向量召回更准。"
    "实测结果显示，重排把准确率从 61% 提升到 78%，但延迟也增加了一倍。"
)


def branches(*pairs) -> list[dict]:
    """``(("问题提出", ("检索质量差",)), …)`` → ``[{"name": …, "entities": […]}]``。"""
    return [{"name": name, "entities": list(members)} for name, members in pairs]


def relation(src: str, dst: str, rel_type: str = "时序", reason: str = "文章里这么写的"):
    return {"src": src, "dst": dst, "type": rel_type, "reason": reason}


#: 一份**合格**的归纳（下面所有用例都从它改一处出来）。
GOOD = {
    "main_logic": "时序",
    "branches": branches(
        ("问题提出", ("检索质量差", "向量召回")),
        ("方案设计", ("重排方案", "重排模型")),
        ("实测结果", ("准确率", "延迟")),
    ),
    "relations": [
        relation("检索质量差", "重排方案", "对策"),
        relation("重排方案", "重排模型", "时序"),
        relation("重排模型", "准确率", "因果"),
    ],
}


def good_copy(**overrides) -> dict:
    """深拷一份 GOOD 再覆盖顶层字段（免得用例之间互相污染）。"""
    data = json.loads(json.dumps(GOOD))
    data.update(overrides)
    return data


class FakeClient:
    """假模型客户端：按顺序把预置的响应吐出来，并记下每次收到的 logic。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.titles: list[str] = []
        #: 每次调用的「换思路」参数（批次 M21-B）：vary / temperature / effort。
        self.vary: list[dict] = []

    def article_logic(self, logic, *, title="", temperature=0.0, vary=0,
                      reasoning_effort=None):
        self.calls.append(dict(logic))
        self.titles.append(title)
        self.vary.append({"vary": int(vary), "temperature": float(temperature),
                          "effort": reasoning_effort})
        if not self.responses:
            raise AssertionError("假 client 的响应不够用了（用例设计错了）")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# ------------------------------------------------------------ 材料与请求体
class TestArticleMaterial(unittest.TestCase):
    def test_short_article_passes_through(self):
        self.assertEqual(build_article_material("第一段。\n\n\n\n第二段。"),
                         "第一段。\n\n第二段。", "连续空行要压成一个，省 token")

    def test_empty_article_is_admitted_not_faked(self):
        self.assertEqual(build_article_material(""), "（没有取到原文）")
        self.assertEqual(build_article_material(None), "（没有取到原文）")

    def test_long_article_is_cut_at_a_paragraph_and_says_so(self):
        text = "\n\n".join(f"第{i}段：" + "内容" * 200 for i in range(20))
        out = build_article_material(text, limit=600)
        self.assertLessEqual(len(out), 600 + len("\n（正文过长，以下省略）"))
        self.assertTrue(out.endswith("（正文过长，以下省略）"),
                        "截断了就必须如实写，不许假装这是全文")
        self.assertNotIn("第19段", out)

    def test_payload_pins_temperature_and_carries_terms(self):
        payload = build_article_logic_payload(
            "deepseek-chat", {"text": ARTICLE, "terms": ["重排模型", "延迟"]},
            title="重排方案实测")
        self.assertEqual(payload["model"], "deepseek-chat")
        self.assertEqual(payload["temperature"], 0.0,
                         "同一篇文章重新生成必须给同一张图")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        user = payload["messages"][1]["content"]
        self.assertIn("重排方案实测", user)
        self.assertIn("重排模型、延迟", user)
        self.assertIn("重排把准确率从 61% 提升到 78%", user)

    def test_payload_without_terms_or_feedback_stays_clean(self):
        payload = build_article_logic_payload("m", {"text": ARTICLE, "terms": []})
        user = payload["messages"][1]["content"]
        self.assertIn("（没有划过词）", user)
        self.assertIn("（未知标题）", user)
        self.assertNotIn("上一轮你的输出", user)

    def test_feedback_is_appended_verbatim_for_the_retry(self):
        payload = build_article_logic_payload(
            "m", {"text": ARTICLE, "terms": [], "feedback": "- 分支只有一个"})
        user = payload["messages"][1]["content"]
        self.assertIn("上一轮你的输出有这样一些问题", user)
        self.assertIn("- 分支只有一个", user)

    def test_system_prompt_keeps_the_hard_lines(self):
        payload = build_article_logic_payload("m", {"text": ARTICLE})
        system = payload["messages"][0]["content"]
        for line in ("不许补充常识", "不许写成并列从属", "不要输出「其它」",
                     "尽量不超过 10 个字"):
            self.assertIn(line, system, "红线被改掉了：这条提示词的护栏不能松")


# ------------------------------------------------------------ 响应解析
class TestParseArticleLogic(unittest.TestCase):
    def test_reads_the_documented_shape(self):
        data = parse_article_logic(json.dumps(GOOD))
        self.assertEqual(data["main_logic"], "时序")
        self.assertEqual([b["name"] for b in data["branches"]],
                         ["问题提出", "方案设计", "实测结果"])
        self.assertEqual(data["relations"][0]["type"], "对策")

    def test_accepts_alternative_field_names(self):
        data = parse_article_logic(json.dumps({
            "branches": [{"name": "一", "members": ["A"]}, "二"],
            "relations": [{"source": "A", "target": "B", "rel": "因果",
                           "evidence": "因为 A 所以 B"}],
        }, ensure_ascii=False))
        self.assertEqual(data["branches"], [{"name": "一", "entities": ["A"]},
                                            {"name": "二", "entities": []}])
        self.assertEqual(data["relations"][0],
                         {"src": "A", "dst": "B", "type": "因果",
                          "reason": "因为 A 所以 B"},
                         "字段别名要照收，别让一次改名废掉整轮归纳")

    def test_duplicate_entities_in_one_branch_are_folded(self):
        data = parse_article_logic(json.dumps(
            {"branches": [{"name": "一", "entities": ["A", "A", " B ", ""]}]},
            ensure_ascii=False))
        self.assertEqual(data["branches"][0]["entities"], ["A", "B"])

    def test_fenced_json_is_unwrapped(self):
        body = "```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```"
        self.assertEqual(parse_article_logic(body)["main_logic"], "时序")

    def test_four_ways_the_contract_is_broken(self):
        for content, why in (("", "空响应"),
                             ("   ", "全空白"),
                             ("{不是 JSON", "不是 JSON"),
                             ("[1, 2, 3]", "JSON 但不是对象"),
                             (json.dumps({"relations": []}), "没有 branches"),
                             (json.dumps({"branches": []}), "branches 是空数组"),
                             (json.dumps({"branches": [{"entities": ["A"]}]}),
                              "分支没有名字")):
            with self.assertRaises(ApiError, msg=f"{why} 必须抛 ApiError"):
                parse_article_logic(content)

    def test_missing_branches_error_says_why(self):
        with self.assertRaises(ApiError) as ctx:
            parse_article_logic(json.dumps({"relations": []}))
        self.assertIn("branches", str(ctx.exception),
                      "一张没有分支的图不是归纳，报错要说清楚")


# ------------------------------------------------------------ 确定性校验
class TestValidateLogic(unittest.TestCase):
    def test_good_result_passes_and_reports_match_ratio(self):
        result = validate_logic(GOOD, text=ARTICLE, terms=("重排模型", "延迟"))
        self.assertTrue(result["ok"], f"合格的归纳被判不合格：{result['problems']}")
        self.assertEqual(result["problems"], [])
        self.assertEqual(result["main_logic"], "时序")
        self.assertEqual(len(result["branches"]), 3)
        self.assertEqual(len(result["relations"]), 3)
        self.assertAlmostEqual(result["matches"], 1.0)

    def test_one_branch_is_not_an_induction(self):
        data = good_copy(branches=branches(("全部", ("检索质量差", "重排方案"))))
        result = validate_logic(data, text=ARTICLE)
        self.assertFalse(result["ok"])
        self.assertTrue(any(f"至少要 {LOGIC_MIN_BRANCHES} 个" in p
                            for p in result["problems"]), result["problems"])

    def test_vague_branch_names_are_rejected(self):
        for name in ("其它", "其他", "杂项", "补充"):
            data = good_copy(branches=branches((name, ("检索质量差",)),
                                               ("方案设计", ("重排方案",))))
            result = validate_logic(data, text=ARTICLE)
            self.assertFalse(result["ok"], f"「{name}」这种分支名必须被拒")
            self.assertTrue(any("太笼统" in p for p in result["problems"]),
                            result["problems"])

    def test_duplicate_branch_names_are_rejected(self):
        data = good_copy(branches=branches(("方案设计", ("检索质量差",)),
                                           ("方案设计", ("重排方案",))))
        self.assertFalse(validate_logic(data, text=ARTICLE)["ok"])

    def test_entity_in_two_branches_keeps_the_first(self):
        data = good_copy(branches=branches(("问题提出", ("检索质量差", "重排方案")),
                                           ("方案设计", ("重排方案", "重排模型"))))
        result = validate_logic(data, text=ARTICLE)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["branches"][1]["entities"], ["重排模型"])
        self.assertTrue(any("重复出现" in w for w in result["warnings"]),
                        result["warnings"])

    def test_empty_branch_is_dropped_with_a_warning(self):
        data = good_copy(branches=branches(("问题提出", ("检索质量差",)),
                                           ("空分支", ()),
                                           ("方案设计", ("重排方案",))))
        result = validate_logic(data, text=ARTICLE)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual([b["name"] for b in result["branches"]],
                         ["问题提出", "方案设计"])
        self.assertTrue(any("是空的" in w for w in result["warnings"]))

    def test_entity_that_is_not_in_the_article_is_a_problem(self):
        data = good_copy(branches=branches(("问题提出", ("检索质量差", "知识图谱")),
                                           ("方案设计", ("重排方案", "重排模型"))),
                         relations=[relation("检索质量差", "重排方案", "对策")])
        result = validate_logic(data, text=ARTICLE)
        self.assertFalse(result["ok"], "「知识图谱」原文里没有，怎么能画上去")
        self.assertTrue(any("原文里找不到" in p for p in result["problems"]),
                        result["problems"])

    def test_too_many_fabricated_entities_fails_the_ratio_check(self):
        data = good_copy(branches=branches(("一", ("检索质量差", "甲", "乙", "丙")),
                                           ("二", ("重排方案", "丁", "戊"))))
        result = validate_logic(data, text=ARTICLE)
        self.assertFalse(result["ok"])
        self.assertLess(result["matches"], LOGIC_TEXT_MATCH_MIN)
        self.assertTrue(any("像是在编造" in p for p in result["problems"]),
                        result["problems"])

    def test_without_the_article_it_refuses_to_induct(self):
        result = validate_logic(GOOD, text="")
        self.assertFalse(result["ok"], "没有原文就不能归纳，否则就是凭空造图")
        self.assertTrue(any("没有原文可比对" in p for p in result["problems"]),
                        result["problems"])

    def test_temporal_article_written_as_parallel_subordination_is_rejected(self):
        """用户要的核心红线：时序文章被写成并列从属 —— 必须判不合格。"""
        data = good_copy(relations=[relation("重排模型", "重排方案", "时序")],
                         main_logic="时序")
        result = validate_logic(data, text=ARTICLE)
        self.assertFalse(result["ok"], "先做方案、再训模型，反过来写就是把脉络讲反了")
        self.assertTrue(any("与分支顺序相反" in p for p in result["problems"]),
                        result["problems"])

    def test_reverse_edge_across_branches_is_caught(self):
        data = good_copy(relations=[relation("准确率", "检索质量差", "因果")])
        result = validate_logic(data, text=ARTICLE)
        self.assertFalse(result["ok"])
        self.assertTrue(any("与分支顺序相反" in p for p in result["problems"]))

    def test_parallel_relations_are_allowed(self):
        """「对比」「层级」没有先后，逆着分支顺序也不该报错。"""
        data = good_copy(relations=[relation("重排模型", "向量召回", "对比", "文章把两者对比")])
        result = validate_logic(data, text=ARTICLE)
        self.assertTrue(result["ok"], result["problems"])

    def test_bad_relation_types_and_shapes(self):
        cases = {
            "类型不在六种里": [relation("检索质量差", "重排方案", "相关")],
            "起点为空": [relation("", "重排方案", "对策")],
            "终点为空": [relation("检索质量差", "", "对策")],
            "自己连自己": [relation("重排方案", "重排方案", "时序")],
        }
        for why, rels in cases.items():
            with self.subTest(why=why):
                result = validate_logic(good_copy(relations=rels), text=ARTICLE)
                self.assertFalse(result["ok"], why)

    def test_relation_endpoint_outside_every_branch_is_a_problem(self):
        data = good_copy(relations=[relation("检索质量差", "召回率", "因果")])
        result = validate_logic(data, text=ARTICLE)
        self.assertFalse(result["ok"], "「召回率」原文里没有，也不能当端点")
        self.assertTrue(any("原文里找不到" in p for p in result["problems"]))

    def test_missing_reason_is_only_a_warning(self):
        data = good_copy(relations=[relation("检索质量差", "重排方案", "对策", "")])
        result = validate_logic(data, text=ARTICLE)
        self.assertTrue(result["ok"], "没写依据只提醒，不挡画图")
        self.assertTrue(any("没有写依据" in w for w in result["warnings"]))

    def test_duplicate_edge_is_folded_with_a_warning(self):
        data = good_copy(relations=[relation("检索质量差", "重排方案", "对策"),
                                    relation("检索质量差", "重排方案", "对策")])
        result = validate_logic(data, text=ARTICLE)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(len(result["relations"]), 1)
        self.assertTrue(any("重复" in w for w in result["warnings"]))

    def test_garbage_relation_items_are_reported_not_crashed(self):
        result = validate_logic(good_copy(relations=["不是对象", 42, None]),
                                text=ARTICLE)
        self.assertFalse(result["ok"])
        self.assertTrue(any("不是对象" in p for p in result["problems"]))

    def test_unknown_main_logic_is_dropped_with_a_warning(self):
        result = validate_logic(good_copy(main_logic="总分总"), text=ARTICLE)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["main_logic"], "")
        self.assertTrue(any("不在六种里" in w for w in result["warnings"]))

    def test_entity_longer_than_the_soft_limit_only_warns(self):
        # 逐字出自原文、只是长（14 字 > ARTICLE_ENTITY_CHARS）：**长不挡画图**。
        # 注意「原文里找不到」是另一条**硬**红线（见上一个用例），别把两者混起来。
        long_name = "重排模型只看问题与段落的配对"
        self.assertGreater(len(long_name), ARTICLE_ENTITY_CHARS)
        self.assertIn(long_name, ARTICLE, "夹具本身要逐字出自原文，否则测的就不是长度")
        data = good_copy(branches=branches(("问题提出", ("检索质量差",)),
                                           ("方案设计", (long_name,))),
                         relations=[])
        result = validate_logic(data, text=ARTICLE)
        self.assertTrue(result["ok"], f"实体名过长是软约束，不该挡画图：{result['problems']}")
        self.assertTrue(any("超过" in w for w in result["warnings"]))
        self.assertFalse(any("原文里找不到" in p for p in result["problems"]),
                         "逐字出自原文的长名字不该被当成编造")

    def test_empty_input_is_not_a_pass(self):
        result = validate_logic({}, text=ARTICLE)
        self.assertFalse(result["ok"])
        result = validate_logic(None, text=ARTICLE)
        self.assertFalse(result["ok"])
        self.assertTrue(result["problems"])

    def test_logic_types_are_the_six_documented_ones(self):
        self.assertEqual(len(ARTICLE_LOGIC_TYPES), 6)
        self.assertEqual(ARTICLE_LOGIC_TYPES,
                         ("时序", "因果", "对策", "层级", "依赖", "对比"))

    def test_feedback_lists_problems_then_warnings(self):
        result = validate_logic(good_copy(main_logic="总分总"), text="")
        text = logic_feedback(result)
        self.assertIn("- ", text)
        self.assertIn("（提醒）", text)
        self.assertLess(text.index("没有原文可比对"), text.index("（提醒）"))


# ------------------------------------------------------------ 主线与落位
class TestFlowAndLevels(unittest.TestCase):
    def test_flow_walks_branches_in_order(self):
        self.assertEqual(logic_flow(GOOD["branches"]),
                         ["检索质量差", "向量召回", "重排方案", "重排模型",
                          "准确率", "延迟"])

    def test_terms_come_first_inside_their_branch(self):
        """落位用的顺序：划过、且被归纳进分支的词排在本分支前面（按划词顺序）。

        这条顺序**只**给落位 / 分行用（``prefer_terms=True``）。校验「有没有把先后
        讲反」必须用模型自己给的先后（默认 ``prefer_terms=False``），否则拿划词顺序
        去判逆行就会误伤 —— 划词顺序与文章顺序本来就可以不一致。
        """
        flow = logic_flow(GOOD["branches"], ("重排模型", "向量召回"), prefer_terms=True)
        self.assertEqual(flow[:4], ["向量召回", "检索质量差", "重排模型", "重排方案"],
                         "用户划过的词要排在本分支前面（按划词顺序）")
        self.assertLess(flow.index("向量召回"), flow.index("检索质量差"),
                        "「向量召回」划过，必须排在它没划过的同分支邻居前面")
        self.assertLess(flow.index("重排模型"), flow.index("重排方案"),
                        "「重排模型」划过，必须排在它没划过的同分支邻居前面")
        self.assertEqual(sorted(flow), sorted(logic_flow(GOOD["branches"])),
                         "只换顺序，不许多一个也不许少一个")
        self.assertEqual(logic_flow(GOOD["branches"], ("重排模型", "向量召回")),
                         logic_flow(GOOD["branches"]),
                         "不给 prefer_terms 时，划词顺序一个字都不许改主线")
        self.assertEqual(len(flow), len(set(flow)), "一个词只许出现一次")
        self.assertEqual(set(flow), {"检索质量差", "向量召回", "重排方案",
                                     "重排模型", "准确率", "延迟"})

    def test_flow_skips_blank_and_missing_members(self):
        self.assertEqual(logic_flow([{"entities": ["A", " ", ""]}, {"name": "空"}]),
                         ["A"])
        self.assertEqual(logic_flow(None), [])

    def test_levels_map_entry_ids_to_branch_index(self):
        nodes = [MapNode(entry_id=1, term="检索质量差", context="", explanation=""),
                 MapNode(entry_id=2, term="重排方案", context="", explanation=""),
                 MapNode(entry_id=3, term="没归纳到的词", context="", explanation=""),
                 MapNode(entry_id=4, term="延迟", context="", explanation="")]
        levels = logic_levels(GOOD, nodes)
        self.assertEqual(levels[1], 0)
        self.assertEqual(levels[2], 1)
        self.assertEqual(levels[4], 2)
        self.assertEqual(levels[3], -1, "不在任何分支里 = -1（界面据此单独摆放）")

    def test_relations_outside_the_topic_are_reported_not_swallowed(self):
        nodes = [MapNode(entry_id=1, term="检索质量差", context="", explanation=""),
                 MapNode(entry_id=2, term="重排方案", context="", explanation="")]
        relations, skipped = logic_to_map_relations(GOOD, nodes)
        pairs = {(r.src_entry_id, r.dst_entry_id, r.rel_type) for r in relations}
        self.assertIn((1, 2, "对策"), pairs)
        self.assertEqual(len(relations), 1, "两端不在词库里的边不能画")
        self.assertEqual(len(skipped), 2, "丢掉的边要列出来给用户看")
        self.assertTrue(any("重排模型" in s for s in skipped))

    def test_relation_pairs_keep_type_and_reason(self):
        graph = type("G", (), {"relations": (LogicRelation("A", "B", "时序", "先 A"),
                                             LogicRelation("B", "C", "因果", ""))})()
        self.assertEqual(logic_rel_pairs(graph),
                         [{"src": "A", "dst": "B", "type": "时序", "reason": "先 A"},
                          {"src": "B", "dst": "C", "type": "因果", "reason": ""}])

    def test_logic_text_of_accepts_row_string_and_none(self):
        self.assertEqual(logic_text_of("正文"), "正文")
        self.assertEqual(logic_text_of(None), "")
        self.assertEqual(logic_text_of({"text": "正文"}), "正文")
        self.assertEqual(logic_text_of({"其它列": 1}), "")


# ------------------------------------------------------------ 流水线
class TestRunLogicGraph(unittest.TestCase):
    def test_first_round_ok_needs_only_one_call(self):
        client = FakeClient(GOOD)
        graph = run_logic_graph(client, doc_key="url:http://x/1", title="重排方案实测",
                                text=ARTICLE, terms=("重排模型",), model_config="m")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(graph.doc_key, "url:http://x/1")
        self.assertEqual(graph.title, "重排方案实测")
        self.assertEqual(graph.main_logic, "时序")
        self.assertEqual(len(graph.branches), 3)
        self.assertEqual(graph.model_config, "m")
        self.assertEqual(graph.warnings, ())

    def test_second_round_gets_the_feedback_from_the_first(self):
        bad = good_copy(relations=[relation("重排模型", "重排方案", "时序")])
        client = FakeClient(bad, GOOD)
        graph = run_logic_graph(client, doc_key="k", text=ARTICLE)
        self.assertEqual(len(client.calls), LOGIC_MAX_ATTEMPTS)
        self.assertEqual(client.calls[0]["feedback"], "")
        self.assertIn("与分支顺序相反", client.calls[1]["feedback"],
                      "第二轮必须把差异清单原样告诉模型")
        self.assertEqual(len(graph.relations), 3)
        self.assertIn("重排模型", [r.src for r in graph.relations])

    def test_giving_up_keeps_the_last_result_and_says_unverified(self):
        bad = good_copy(relations=[relation("重排模型", "重排方案", "时序")])
        client = FakeClient(bad, bad)
        graph = run_logic_graph(client, doc_key="k", text=ARTICLE)
        self.assertEqual(len(client.calls), LOGIC_MAX_ATTEMPTS,
                         "重试要在上限处停住（每轮花的都是用户的钱）")
        self.assertEqual(len(graph.relations), 1, "最后一版的关系要留着给界面显示")
        self.assertEqual(logic_feedback({"problems": ["与分支顺序相反"],
                                         "warnings": []}),
                         "- 与分支顺序相反")

    def test_broken_contract_also_retries(self):
        client = FakeClient(ApiError("bad_response", "归纳输出缺少 branches 数组"),
                            GOOD)
        graph = run_logic_graph(client, doc_key="k", text=ARTICLE)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(graph.branches), 3)

    def test_api_failure_is_reported_not_raised(self):
        client = FakeClient(ApiError("network", "连不上服务端"))
        graph = run_logic_graph(client, doc_key="k", text=ARTICLE)
        self.assertEqual(len(client.calls), 1, "网络失败不重试，直接如实报错")
        self.assertEqual(graph.branches, ())
        self.assertEqual(graph.relations, ())

    def test_max_attempts_is_clamped_to_at_least_one(self):
        client = FakeClient(GOOD)
        run_logic_graph(client, doc_key="k", text=ARTICLE, max_attempts=0)
        self.assertEqual(len(client.calls), 1)


# ------------------------------------------------------------ 重新生成 = 换思路
class TestVaryLadder(unittest.TestCase):
    """批次 M21-B：用户口径「没有思考等级方式的变化，重新生成毫无意义」。

    这一组用例守的是三件事：

    * **第 0 档必须与批次 M20 的老口径逐字一致** —— 否则升级一次，用户已有的
      缓存全失效、每张老图都要重新花钱归纳；
    * **每一档必须真的不一样**（提示词多一段、temperature 不同、推理强度更高），
      否则「换思路」只是嘴上说说；
    * 每一档**各有各的指纹**（各缓存一张图），且档位随图落库、读得回来。
    """

    def test_step_zero_keeps_the_old_prompt_verbatim(self):
        payload = build_article_logic_payload("m", {"text": ARTICLE, "terms": ["重排模型"]},
                                              title="重排方案实测")
        self.assertEqual(build_article_vary_note(0), "")
        self.assertNotIn("这一遍的额外要求", payload["messages"][1]["content"],
                         "第 0 档不许出现换思路的话，否则老缓存全部作废")
        self.assertNotIn("额外要求", payload["messages"][0]["content"])

    def test_every_step_adds_its_own_instruction_and_heat(self):
        seen_temperature = set()
        seen_note = set()
        for step in range(ARTICLE_VARY_MAX + 1):
            label, approach, temperature = article_vary(step)
            note = build_article_vary_note(step)
            seen_temperature.add(temperature)
            seen_note.add(note)
            self.assertTrue(label, "每一档都要有给人看的名字")
            if step:
                self.assertIn(label, note)
                self.assertIn(approach, note)
                self.assertGreater(temperature, 0.0,
                                   "换思路那一遍不该还是 temperature=0（那样同一档必然复读）")
        self.assertEqual(len(seen_temperature), ARTICLE_VARY_MAX + 1)
        self.assertEqual(len(seen_note), ARTICLE_VARY_MAX + 1, "每一档的话必须各不相同")

    def test_step_is_clamped_instead_of_exploding(self):
        self.assertEqual(article_vary(-3), article_vary(0))
        self.assertEqual(article_vary(99), article_vary(ARTICLE_VARY_MAX))
        self.assertEqual(article_vary(1.0), ARTICLE_VARY_STEPS[1])

    def test_payload_carries_the_step_instruction_and_its_temperature(self):
        payload = build_article_logic_payload(
            "m", {"text": ARTICLE, "terms": []}, title="t", vary=2)
        body = payload["messages"][1]["content"]
        label, approach, temperature = article_vary(2)
        self.assertIn(label, body)
        self.assertIn(approach, body)
        self.assertEqual(payload["temperature"], temperature)
        self.assertEqual(payload["messages"][1]["content"].count("这一遍的额外要求"), 1,
                         "换思路那段只能加一次，别在重试时叠上去")

    def test_zero_step_fingerprint_matches_the_old_payload(self):
        nodes = [MapNode(entry_id=1, term="重排模型"),
                 MapNode(entry_id=2, term="向量召回")]
        old = content_fingerprint(7, nodes, base_url="http://x", model="m",
                                  reasoning_effort="", source="article:k#ok:9")
        self.assertEqual(content_fingerprint(7, nodes, base_url="http://x", model="m",
                                             reasoning_effort="", source="article:k#ok:9",
                                             vary=0),
                         old, "第 0 档必须与老口径逐字相同，否则老缓存全失效")

    def test_each_step_has_its_own_fingerprint(self):
        nodes = [MapNode(entry_id=1, term="重排模型")]
        prints = {content_fingerprint(7, nodes, base_url="http://x", model="m",
                                      reasoning_effort="", source="a", vary=step)
                  for step in range(ARTICLE_VARY_MAX + 1)}
        self.assertEqual(len(prints), ARTICLE_VARY_MAX + 1,
                         "每一档各是一张图、各有自己的缓存，不许互相顶掉")

    def test_plan_walks_up_the_effort_ladder(self):
        tiers = ["", "low", "high", "max"]
        got = [vary_plan(step, reasoning_effort="", model="deepseek-chat")["effort"]
               for step in range(ARTICLE_VARY_MAX + 1)]
        self.assertEqual(got, ["", "low", "high", "max"],
                         "设置里是「不发送」时，四档要正好走完四档推理强度")
        self.assertEqual(got, tiers)

    def test_plan_never_goes_past_the_last_tier(self):
        plan = vary_plan(ARTICLE_VARY_MAX, reasoning_effort="max",
                         model="deepseek-chat")
        self.assertEqual(plan["effort"], "max", "已经在最高档就停在最高档")
        self.assertEqual(plan["vary"], ARTICLE_VARY_MAX)

    def test_plan_keeps_the_setting_when_the_model_has_no_tiers(self):
        """没列出档位的模型走兜底目录 —— 用户在设置里选的那一档必须还在。"""
        plan = vary_plan(1, reasoning_effort="medium", model="某个没列档位的模型")
        self.assertEqual(plan["effort"], "high",
                         "兜底目录是 ('', low, medium, high)：medium 往上抬一级 = high")
        self.assertEqual(vary_plan(1, reasoning_effort="high",
                                   model="某个没列档位的模型")["effort"], "high",
                         "已经在顶就停在顶，不许越界")

    def test_graph_records_which_step_it_came_from(self):
        client = FakeClient(GOOD)
        graph = run_logic_graph(client, doc_key="k", text=ARTICLE,
                                plan=vary_plan(1, reasoning_effort="", model="deepseek-chat"))
        self.assertEqual(graph.vary, 1)
        self.assertEqual(graph.vary_label, article_vary(1)[0])
        self.assertEqual(client.vary[0]["vary"], 1)
        self.assertEqual(client.vary[0]["effort"], "low")
        self.assertEqual(client.vary[0]["temperature"], article_vary(1)[2])
        self.assertIn("vary", graph.as_dict(), "档位要随图落库，读缓存才认得回来")
        self.assertEqual(len(client.calls), 1)

    def test_no_plan_means_the_old_behaviour(self):
        client = FakeClient(GOOD)
        graph = run_logic_graph(client, doc_key="k", text=ARTICLE)
        self.assertEqual(graph.vary, 0)
        self.assertEqual(graph.vary_label, "")
        self.assertEqual(client.vary[0], {"vary": 0, "temperature": 0.0, "effort": ""})

    def test_label_is_silent_for_the_first_version(self):
        self.assertEqual(logic_vary_label({"vary": 0, "vary_label": "标准"}), "")
        self.assertEqual(logic_vary_label(None), "")
        self.assertEqual(logic_vary_label({"vary": 2, "vary_label": "换一种结构"}),
                         "第 3 档 · 换一种结构")
        self.assertEqual(logic_vary_label({"vary": 1, "vary_label": ""}), "第 2 档")

    def test_retry_stays_inside_the_same_step(self):
        bad = good_copy(relations=[relation("重排模型", "重排方案", "时序")])
        client = FakeClient(bad, GOOD)
        run_logic_graph(client, doc_key="k", text=ARTICLE,
                        plan=vary_plan(2, reasoning_effort="", model="deepseek-chat"))
        self.assertEqual(client.vary[0], client.vary[1],
                         "重试是「同一档再想一次」，不许在重试里偷偷换档")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
