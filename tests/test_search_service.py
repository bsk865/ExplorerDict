"""B5 搜索命中片段：命中哪个字段、高亮在哪、片段怎么截。"""
from __future__ import annotations

import json
import unittest

from app import search_service
from app.search_service import highlight, match_field, snippet, describe
from tests.support import temp_db


class TestHighlight(unittest.TestCase):
    def test_marks_every_hit_case_insensitively(self):
        self.assertEqual(highlight("Economics and economics", "econom"),
                         "【Econom】ics and 【econom】ics")
        self.assertEqual(highlight("边际效用递减", "递减"), "边际效用【递减】")
        self.assertEqual(highlight("没有命中", "xyz"), "没有命中")
        self.assertEqual(highlight("", "x"), "")
        self.assertEqual(highlight("abc", ""), "abc")

    def test_overlapping_queries_do_not_loop(self):
        self.assertEqual(highlight("aaaa", "aa"), "【aa】【aa】")


class TestSnippet(unittest.TestCase):
    def test_keeps_context_around_the_hit(self):
        text = "当消费者多消费一单位商品时，边际效用递减。"
        out = snippet(text, "递减")
        self.assertIn("【递减】", out)
        self.assertIn("边际效用", out)

    def test_ellipsis_on_both_ends(self):
        text = "前" * 60 + "命中" + "后" * 60
        out = snippet(text, "命中")
        self.assertTrue(out.startswith("…"))
        self.assertTrue(out.endswith("…"))
        self.assertIn("【命中】", out)

    def test_flattens_newlines(self):
        self.assertEqual(snippet("第一行\n第二行 命中", "命中"), "第一行 第二行 【命中】")

    def test_no_hit_shows_the_head(self):
        out = snippet("靠标签命中的词条", "经济学")
        self.assertEqual(out, "靠标签命中的词条")

    def test_empty_text(self):
        self.assertEqual(snippet("", "x"), "")


class TestMatchField(unittest.TestCase):
    def test_prefers_term_then_context(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = db.add_entry(batch_id=bid, term="边际效用", context="边际效用递减")
            row = db.get_entry(eid)
            self.assertEqual(match_field(row, "边际"), ("词语", "边际效用"))
            self.assertEqual(match_field(row, "递减"), ("上下文", "边际效用递减"))
            self.assertIsNone(match_field(row, "不存在"))
            self.assertIsNone(match_field(row, ""))

    def test_examples_and_source_and_tags(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = db.add_entry(batch_id=bid, term="t", context="ctx",
                               source_title="经济学原理", source_app="msedge.exe")
            db.update_entry(eid, examples=json.dumps(["稀缺性决定价格"], ensure_ascii=False))
            row = db.get_entry(eid)
            self.assertEqual(match_field(row, "稀缺性"), ("例子", "稀缺性决定价格"))
            self.assertEqual(match_field(row, "原理"), ("来源", "经济学原理"))
            self.assertEqual(match_field(row, "msedge"), ("来源应用", "msedge.exe"))
            self.assertEqual(match_field(row, "经济学", tags=["经济学"]), ("来源", "经济学原理"),
                             "字段顺序：来源比标签靠前")
            self.assertEqual(match_field(row, "博弈", tags=["博弈论"]), ("标签", "#博弈论"))
            self.assertIsNone(match_field(row, "博弈"))

    def test_describe_returns_a_one_line_hint(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = db.add_entry(batch_id=bid, term="供给", context="供给曲线向右上方倾斜")
            self.assertEqual(describe(db.get_entry(eid), "右上"),
                             "上下文：供给曲线向【右上】方倾斜")
            self.assertIsNone(describe(db.get_entry(eid), ""))

    def test_describe_uses_tags_when_nothing_else_matches(self):
        with temp_db() as db:
            bid = db.create_batch("b")
            eid = db.add_entry(batch_id=bid, term="供给")
            self.assertEqual(describe(db.get_entry(eid), "经济学", tags=["经济学"]),
                             "标签：#【经济学】")

    def test_field_order_is_documented(self):
        keys = [key for key, _label in search_service.FIELD_ORDER]
        self.assertEqual(keys[0], "term")
        self.assertIn("context", keys)
        self.assertIn("source_title", keys)


if __name__ == "__main__":
    unittest.main()
