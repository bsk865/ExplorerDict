"""B3 导出：CSV / Markdown 一对文件，绝不覆盖、Excel 不乱码、别把脏数据带崩。"""
from __future__ import annotations

import csv
import io
import json
import unittest
from datetime import datetime
from pathlib import Path

from app import export_service
from app.export_service import ExportResult, export_entries, safe_stem
from app.db import Database
from tests.support import temp_db, tmp_dir

STAMP = datetime(2026, 3, 4, 5, 6, 7)


def _rows(db: Database, batch_id: int):
    return list(db.list_entries(batch_id=batch_id))


class TestExportService(unittest.TestCase):
    def test_files_are_written_as_a_pair(self):
        with temp_db() as db, tmp_dir("exp_") as tmp:
            bid = db.create_batch("经济学入门")
            eid = db.add_entry(batch_id=bid, term="边际效用",
                               context="当消费者多消费一单位商品时，边际效用递减。",
                               source_title="经济学原理", source_url="https://x.example.com/p",
                               source_app="msedge.exe")
            db.update_entry(eid, one_line="每多消费一单位带来的额外满足感",
                            examples=json.dumps(["吃第一个包子", "吃第五个包子"],
                                                ensure_ascii=False))
            db.set_entry_tags(eid, ["经济学"])

            result = export_entries(_rows(db, bid), scope_label="经济学入门",
                                    directory=tmp, tags_map=db.tags_for_entries([eid]),
                                    now=STAMP)
            self.assertIsInstance(result, ExportResult)
            self.assertEqual(result.count, 1)
            self.assertTrue(result.csv_path.exists())
            self.assertTrue(result.md_path.exists())
            self.assertEqual(result.directory, Path(tmp))
            self.assertIn("已导出 1 条", result.summary())
            self.assertEqual(result.csv_path.name,
                             "探索词典-经济学入门-20260304-050607.csv")

    def test_csv_has_bom_and_crlf_and_all_columns(self):
        with temp_db() as db, tmp_dir("exp_") as tmp:
            bid = db.create_batch("b")
            eid = db.add_entry(batch_id=bid, term="t", context="ctx")
            result = export_entries(_rows(db, bid), directory=tmp, now=STAMP)
            raw = result.csv_path.read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "CSV 必须带 BOM（Excel 中文）")
            text = raw.decode("utf-8-sig")
            self.assertIn("\r\n", text)
            rows = list(csv.reader(io.StringIO(text)))
            self.assertEqual(rows[0], [label for _k, label in export_service.COLUMNS])
            self.assertEqual(len(rows[1]), len(export_service.COLUMNS))
            self.assertEqual(rows[1][0], "t")
            self.assertEqual(rows[1][1], "ctx")
            self.assertEqual(eid, int(eid))

    def test_examples_are_flattened_and_tags_rendered(self):
        with temp_db() as db, tmp_dir("exp_") as tmp:
            bid = db.create_batch("b")
            eid = db.add_entry(batch_id=bid, term="t")
            db.update_entry(eid, examples=json.dumps(["e1", "e2"], ensure_ascii=False))
            db.set_entry_tags(eid, ["a", "b"])
            result = export_entries(_rows(db, bid), directory=tmp,
                                    tags_map=db.tags_for_entries([eid]), now=STAMP)
            rows = list(csv.reader(io.StringIO(result.csv_path.read_text("utf-8-sig"))))
            index = {label: i for i, label in enumerate(rows[0])}
            self.assertEqual(rows[1][index["例子"]], "e1；e2")
            self.assertEqual(rows[1][index["标签"]], "#a  #b")
            self.assertEqual(rows[1][index["重复次数"]], "1")

    def test_markdown_table_escapes_pipes(self):
        with temp_db() as db, tmp_dir("exp_") as tmp:
            bid = db.create_batch("b")
            db.add_entry(batch_id=bid, term="a|b", context="line1\nline2")
            result = export_entries(_rows(db, bid), scope_label="范围", directory=tmp,
                                    now=STAMP)
            text = result.md_path.read_text(encoding="utf-8")
            self.assertIn("| 词语 | 上下文 |", text)
            self.assertIn("a\\|b", text)
            self.assertIn("line1 line2", text)
            self.assertIn("- 范围：范围", text)
            self.assertIn("- 条数：1", text)
            self.assertIn("未做后台采集", text, "导出文件里要写清隐私边界")

    def test_second_export_never_overwrites(self):
        with temp_db() as db, tmp_dir("exp_") as tmp:
            bid = db.create_batch("b")
            db.add_entry(batch_id=bid, term="t")
            first = export_entries(_rows(db, bid), directory=tmp, now=STAMP)
            second = export_entries(_rows(db, bid), directory=tmp, now=STAMP)
            self.assertNotEqual(first.csv_path, second.csv_path)
            self.assertEqual(second.csv_path.name,
                             "探索词典-全部词语-20260304-050607-2.csv")
            self.assertTrue(first.csv_path.exists(), "上一次的导出不能被覆盖")
            self.assertEqual(second.md_path.name,
                             "探索词典-全部词语-20260304-050607-2.md")

    def test_safe_stem_strips_illegal_characters(self):
        self.assertEqual(safe_stem('主题: 经济学/入门?*'), "主题 经济学 入门")
        self.assertEqual(safe_stem("   "), "导出")
        self.assertEqual(safe_stem(""), "导出")
        self.assertEqual(len(safe_stem("长" * 100)), export_service.MAX_STEM_CHARS)

    def test_broken_examples_do_not_break_the_export(self):
        with temp_db() as db, tmp_dir("exp_") as tmp:
            bid = db.create_batch("b")
            eid = db.add_entry(batch_id=bid, term="t")
            db.update_entry(eid, examples="{不是 JSON")
            result = export_entries(_rows(db, bid), directory=tmp, now=STAMP)
            rows = list(csv.reader(io.StringIO(result.csv_path.read_text("utf-8-sig"))))
            index = {label: i for i, label in enumerate(rows[0])}
            self.assertEqual(rows[1][index["例子"]], "{不是 JSON", "脏数据原样导出，不丢行")

    def test_empty_batch_still_writes_headers(self):
        with temp_db() as db, tmp_dir("exp_") as tmp:
            bid = db.create_batch("空主题")
            result = export_entries(_rows(db, bid), scope_label="空主题", directory=tmp,
                                    now=STAMP)
            self.assertEqual(result.count, 0)
            rows = list(csv.reader(io.StringIO(result.csv_path.read_text("utf-8-sig"))))
            self.assertEqual(len(rows), 1)
            self.assertEqual(len(rows[0]), len(export_service.COLUMNS))

    def test_formula_like_cells_get_an_apostrophe_prefix(self):
        """划词内容可能以 = + - @ 开头（网页公式、列表符号）：CSV 里要挡掉公式注入。"""
        self.assertEqual(export_service.csv_cell("=SUM(A1)"), "'=SUM(A1)")
        self.assertEqual(export_service.csv_cell("+1"), "'+1")
        self.assertEqual(export_service.csv_cell("- 列表项"), "'- 列表项")
        self.assertEqual(export_service.csv_cell("@名字"), "'@名字")
        self.assertEqual(export_service.csv_cell("正常词语"), "正常词语")
        self.assertEqual(export_service.csv_cell(""), "")

        text = export_service.csv_text(["词语"], [["=1+1"], ["普通"]])
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(rows[1], ["'=1+1"])
        self.assertEqual(rows[2], ["普通"])

    def test_markdown_does_not_add_the_csv_prefix(self):
        """Markdown 表格不执行公式，别把单引号带进 Obsidian。"""
        text = export_service.markdown_text(["词语"], [["=1+1"]], scope_label="x")
        self.assertIn("| =1+1 |", text)


if __name__ == "__main__":
    unittest.main()
