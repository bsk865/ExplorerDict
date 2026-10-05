"""B3 导出：CSV / Markdown 一对文件，绝不覆盖、Excel 不乱码、别把脏数据带崩。"""
from __future__ import annotations

import csv
import io
import json
import re
import unittest
from datetime import datetime
from unittest import mock
from pathlib import Path

from app import export_service
from app.export_service import ExportResult, export_entries, safe_stem
from app.db import Database
from tests.support import temp_db, tmp_dir

STAMP = datetime(2026, 3, 4, 5, 6, 7)


def _rows(db: Database, batch_id: int):
    return list(db.list_entries(batch_id=batch_id))


class TestExportService(unittest.TestCase):
    def test_default_format_writes_one_csv_and_names_it(self):
        """默认还是 CSV（老用户习惯不变），但一次只写**一个**文件。"""
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
            self.assertEqual(result.format_key, "csv")
            self.assertTrue(result.path.exists())
            self.assertEqual(result.directory, Path(tmp))
            self.assertIn("已导出 1 条", result.summary())
            self.assertEqual(result.path.name,
                             "探索词典-经济学入门-20260304-050607.csv")
            self.assertEqual(list(Path(tmp).iterdir()), [result.path],
                             "选了一种格式就只写这一个文件，不再顺手来一份 Markdown")
            self.assertEqual(result.csv_path, result.path, "旧调用点看到的还是它")
            self.assertEqual(result.md_path, result.path)

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
                                    now=STAMP, fmt="markdown")
            text = result.path.read_text(encoding="utf-8")
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
            self.assertNotEqual(first.path, second.path)
            self.assertEqual(second.path.name,
                             "探索词典-全部词语-20260304-050607-2.csv")
            self.assertTrue(first.path.exists(), "上一次的导出不能被覆盖")
            third = export_entries(_rows(db, bid), directory=tmp, now=STAMP,
                                   fmt="markdown")
            self.assertEqual(third.path.name,
                             "探索词典-全部词语-20260304-050607.md",
                             "换一种格式不跟 CSV 抢名字")

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


class TestExportFormats(unittest.TestCase):
    """M：八种格式各写各的、后缀分明、内容对得上。"""

    def _batch(self, db):
        bid = db.create_batch("经济学入门")
        eid = db.add_entry(batch_id=bid, term="边际效用",
                           context="多消费一单位带来的满足感")
        db.update_entry(eid, one_line="额外一单位带来的额外满足",
                        examples=json.dumps(["包子一", "包子五"], ensure_ascii=False))
        db.set_entry_tags(eid, ["经济学"])
        return bid, eid

    def test_every_format_writes_exactly_one_file_with_its_own_suffix(self):
        with temp_db() as db, tmp_dir("exp_") as tmp:
            bid, eid = self._batch(db)
            rows = _rows(db, bid)
            tags = db.tags_for_entries([eid])
            for fmt in export_service.FORMATS:
                with self.subTest(fmt=fmt.key):
                    out = Path(tmp) / fmt.key
                    result = export_entries(rows, scope_label="经济学入门",
                                            directory=out, tags_map=tags,
                                            now=STAMP, fmt=fmt.key)
                    files = sorted(p for p in out.iterdir() if p.is_file())
                    self.assertEqual(len(files), 1, f"{fmt.key}：只该有一个文件")
                    self.assertEqual(result.path.suffix, fmt.suffix)
                    self.assertEqual(result.format, fmt)
                    raw = result.path.read_bytes()
                    if fmt.suffix == ".pdf":
                        self.assertTrue(raw.startswith(b"%PDF-1.4"))
                        self.assertIn(b"/FontFile2", raw, "PDF 得把字体带上")
                        continue
                    text = raw.decode("utf-8-sig")
                    self.assertIn("边际效用", text, f"{fmt.key}：词条要写进去")
                    if fmt.key == "csv":
                        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "CSV 带 BOM")
                        self.assertIn("词语,上下文", text)
                    elif fmt.key == "markdown":
                        self.assertIn("| 词语 | 上下文 |", text)
                    elif fmt.key == "json":
                        self.assertEqual(json.loads(text)["count"], 1)
                    elif fmt.key == "jsonl":
                        self.assertEqual(json.loads(text.strip())["term"], "边际效用")
                    elif fmt.key == "anki":
                        self.assertIn("\t", text)
                        self.assertNotIn("词语\t", text, "Anki 不带表头")
                    elif fmt.key == "html":
                        self.assertTrue(text.startswith("<!DOCTYPE html>"))
                    elif fmt.key == "txt":
                        self.assertNotIn("│", text)

    def test_the_formats_carry_what_each_tool_needs(self):
        """HTML 要能双击就看、Markdown 是表格、Anki 是制表符、TXT 不画框线。"""
        with temp_db() as db, tmp_dir("exp_") as tmp:
            bid, eid = self._batch(db)
            rows = _rows(db, bid)
            tags = db.tags_for_entries([eid])

            def pick(key):
                return export_entries(rows, scope_label="经济学入门", directory=tmp,
                                      tags_map=tags, now=STAMP, fmt=key).path

            html = pick("html").read_text(encoding="utf-8")
            self.assertIn("<!DOCTYPE html>", html)
            self.assertIn("charset", html, "中文单页必须有 charset，不然双击就是乱码")
            self.assertIn("<style", html, "样式要带在身上，离线也能看")
            self.assertNotIn("http://", html.replace("http://www.w3.org", ""),
                             "单页不该往网上要资源")

            md = pick("markdown").read_text(encoding="utf-8")
            self.assertIn("| 词语 | 上下文 |", md)

            anki = pick("anki").read_text(encoding="utf-8")
            lines = anki.strip().splitlines()
            self.assertEqual(len(lines), 1, "Anki 导入文本不带表头")
            cells = lines[0].split("	")
            self.assertEqual(cells[0], "边际效用")
            self.assertIn("#经济学", cells[-1], "末列认标签")

            txt = pick("txt").read_text(encoding="utf-8")
            self.assertIn("边际效用", txt)
            self.assertNotIn("│", txt, "纯文本不画框线表格")

    def test_json_is_structured_and_jsonl_is_one_line_per_entry(self):
        with temp_db() as db, tmp_dir("exp_") as tmp:
            bid, eid = self._batch(db)
            rows = _rows(db, bid)
            tags = db.tags_for_entries([eid])
            data = json.loads(export_entries(rows, scope_label="经济学入门",
                                             directory=tmp, tags_map=tags, now=STAMP,
                                             fmt="json").path.read_text("utf-8"))
            self.assertEqual(data["count"], 1)
            self.assertIn("exported_at", data)
            item = data["entries"][0]
            self.assertEqual(item["term"], "边际效用")
            self.assertEqual(item["tags"], ["经济学"])
            self.assertEqual(item["examples"], ["包子一", "包子五"])
            lines = export_entries(rows, scope_label="经济学入门", directory=tmp,
                                   tags_map=tags, now=STAMP,
                                   fmt="jsonl").path.read_text("utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["term"], "边际效用")

    def test_pdf_is_a_real_pdf_with_the_font_embedded(self):
        """PDF 换台电脑打开不能变方块 —— 字体子集必须真的带在身上。"""
        import re
        import zlib

        from app import pdf_writer, ttf_subset

        with temp_db() as db, tmp_dir("exp_") as tmp:
            bid, eid = self._batch(db)
            result = export_entries(_rows(db, bid), scope_label="经济学入门",
                                    directory=tmp, tags_map=db.tags_for_entries([eid]),
                                    now=STAMP, fmt="pdf")
            data = result.path.read_bytes()
            self.assertTrue(data.startswith(b"%PDF-1.4"))
            self.assertTrue(data.rstrip().endswith(b"%%EOF"))
            self.assertIn(b"/Type /Catalog", data)
            self.assertIn(b"/Identity-H", data)
            text = data.decode("latin-1")
            descriptor = re.search(r"/Type /FontDescriptor.*?>>", text, re.S)
            self.assertIsNotNone(descriptor, "PDF 里得有字体描述")
            found = re.search(r"/FontFile2 (\d+) 0 R", descriptor.group(0))
            self.assertIsNotNone(found,
                                 "字体描述必须引用 /FontFile2，否则 PDF 没带上字体")
            number = int(found.group(1))
            head = re.search(rf"(?m)^{number} 0 obj\n(.*?)stream\r?\n".encode(),
                             data, re.S)
            length = int(re.search(r"/Length\s+(\d+)",
                                   head.group(1).decode("latin-1")).group(1))
            blob = zlib.decompress(data[head.end():head.end() + length])
            self.assertEqual(blob[:4], b"\x00\x01\x00\x00", "解出来要是真 TrueType")
            out = Path(tmp) / "embedded.ttf"
            out.write_bytes(blob)
            font = ttf_subset.load_ttf(str(out))
            self.assertGreater(font.num_glyphs, 10)
            self.assertIsNotNone(pdf_writer.find_font(), "这台机器上找得到中文字体")

    def test_pdf_width_array_has_one_entry_per_glyph(self):
        """``/W`` 得一个字形一条。

        曾经把连号的 100 个字形打包成一个 ``起始CID [w1 w2 …]`` —— 而 PDF 的
        语法是「从这个 CID 起**连续**这么多个」，中间没列到的字形会掉进 ``/DW``
        （1000 = 1em），于是第 61 个字形之后按两倍宽度推进，**每行写到第 60 个
        字就被推出纸边裁掉**。这条盯数组本身，下一条盯「行有没有越界」。
        """
        from app import pdf_writer
        with temp_db() as db, tmp_dir("exp_") as tmp:
            bid = db.create_batch("宽度数组")
            eid = db.add_entry(batch_id=bid, term="卷积神经网络与池化层",
                               context="".join(chr(0x4E00 + i) for i in range(210)),
                               source_app="msedge.exe")
            result = export_entries(_rows(db, bid), scope_label="宽度数组",
                                    directory=tmp, tags_map=db.tags_for_entries([eid]),
                                    now=STAMP, fmt="pdf")
            raw = result.path.read_bytes()

        match = re.search(rb"/W\s*\[(.*?)\]\s*/CIDToGIDMap", raw, re.S)
        self.assertIsNotNone(match, "CIDFont 里没有 /W 数组")
        body = match.group(1).decode("latin-1")
        groups = re.findall(r"\d+\s*\[([^\]]*)\]", body)
        self.assertGreater(len(groups), 50, "这条用例得用到足够多的字形才有意义")
        for group in groups:
            self.assertEqual(len(group.split()), 1,
                             "一个 /W 条目只能带一个宽度，出现了范围组：%r" % group)

    def test_no_pdf_line_runs_past_the_right_margin(self):
        """照阅读器的方式（按 /W 推进）算一遍：没有一行该越过右边界。"""
        from app import pdf_writer
        with temp_db() as db, tmp_dir("exp_") as tmp:
            bid = db.create_batch("版式")
            eid = db.add_entry(batch_id=bid, term="边际效用",
                               context="Existing solutions often depend on "
                                       "drift-detection methods that produce high "
                                       "computational overhead for resource-constrained "
                                       "environments, and fail to provide strict "
                                       "guarantees on resource usage or theoretical "
                                       "performance assurances.",
                               source_app="msedge.exe")
            result = export_entries(_rows(db, bid), scope_label="版式",
                                    directory=tmp, tags_map=db.tags_for_entries([eid]),
                                    now=STAMP, fmt="pdf")
            raw = result.path.read_bytes()

        widths = {int(a): int(b) for a, b in
                  re.findall(rb"(\d+)\s*\[\s*(\d+)\s*\]", raw)}
        self.assertGreater(len(widths), 20, "没读到 /W 宽度表")
        right = pdf_writer.PAGE_WIDTH - pdf_writer.MARGIN_X
        checked = 0
        for match in re.finditer(rb"/F1 ([\d.]+) Tf 1 0 0 1 ([\d.-]+) ([\d.-]+) Tm "
                                 rb"<([0-9A-Fa-f]*)> Tj", raw):
            size, x = float(match.group(1)), float(match.group(2))
            codes = bytes.fromhex(match.group(4).decode())
            end = x + sum(widths.get(int.from_bytes(codes[i:i + 2], "big"), 1000)
                          / 1000.0 * size for i in range(0, len(codes), 2))
            self.assertLessEqual(round(end, 1), round(right, 1),
                                 "这一行越过了右边界：%r" % match.group(4)[:60])
            checked += 1
        self.assertGreater(checked, 3, "没检查到几行，这条用例可能失效了")

    def test_pdf_falls_back_to_latin1_when_no_font_is_available(self):
        """找不到中文字体也不能崩：退化成只写拉丁字母，文件照样能开。"""
        from app import pdf_writer

        with temp_db() as db, tmp_dir("exp_") as tmp:
            bid = db.create_batch("b")
            db.add_entry(batch_id=bid, term="plain")
            with mock.patch.object(pdf_writer, "find_font", return_value=None):
                result = export_entries(_rows(db, bid), directory=tmp, now=STAMP,
                                        fmt="pdf")
            data = result.path.read_bytes()
            self.assertTrue(data.startswith(b"%PDF-1.4"))
            self.assertIn(b"/Helvetica", data)
            self.assertNotIn(b"/FontFile2", data)

    def test_unknown_format_falls_back_to_csv_and_build_text_complains(self):
        with temp_db() as db, tmp_dir("exp_") as tmp:
            bid = db.create_batch("b")
            db.add_entry(batch_id=bid, term="t")
            result = export_entries(_rows(db, bid), directory=tmp, now=STAMP,
                                    fmt="docx")
            self.assertEqual(result.format_key, "csv")
            self.assertEqual(result.path.suffix, ".csv")
            self.assertEqual(export_service.format_for(None).key, "csv")
            self.assertEqual(export_service.format_for("MARKDOWN").key, "markdown",
                             "大小写不敏感")
            self.assertEqual(export_service.format_for("docx").key, "csv",
                             "认不出来的 key 一律当 CSV")
            stray = export_service.ExportFormat("docx", "Word", ".docx", "给 Word 用")
            with self.assertRaises(ValueError):
                export_service.build_text(stray, ["词语"], [["t"]], _rows(db, bid),
                                          {}, scope_label="x", created_at=STAMP)

    def test_resolve_directory_expands_env_vars_and_defaults_to_exports(self):
        from app import paths

        with temp_db() as db:
            del db
            default = export_service.resolve_directory("")
            self.assertEqual(default, Path(paths.exports_dir()))
            self.assertEqual(export_service.resolve_directory("   "), default)
            self.assertEqual(export_service.resolve_directory('"C:\\某个文件夹"'),
                             Path("C:/某个文件夹"), "手输的引号要能忍")
            fallback = Path("D:/别处")
            self.assertEqual(export_service.resolve_directory("", fallback=fallback),
                             fallback)



if __name__ == "__main__":
    unittest.main()
