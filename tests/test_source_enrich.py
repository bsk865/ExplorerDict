"""B6 来源标题补全 + B4 同词追加：都在 `bookmark()` 落库那一刻发生。

隐私约束（改进清单 B6 的原文要求）在这一批里被固化成测试：
**只在用户点「解释并记录」那一刻处理一次字符串**，不后台采集、不联网。
"""
from __future__ import annotations

import unittest

from app import source_enrich
from app.config import Config
from app.models import CapturedSelection, SourceInfo, now_iso
from app.source_enrich import clean_document_title, document_kind
from tests.support import make_service, temp_db


def _sel(term="边际效用", context="当消费者多消费一单位商品时，边际效用递减。",
         title="经济学原理.pdf - Adobe Acrobat Reader", app="AcroRd32.exe",
         url="", doc_key="", hwnd=55501, pid=9999):
    src = SourceInfo(hwnd=hwnd, pid=pid, exe=f"C:\\Program Files\\{app}", app=app,
                     title=title, url=url,
                     confidence="url_document" if url else "window_title_only",
                     doc_key=doc_key or f"doc:{title}")
    return CapturedSelection(term=term, context=context, method="ui_textpattern",
                             source=src, captured_at=now_iso())


class TestCleanDocumentTitle(unittest.TestCase):
    def test_strips_browser_tails(self):
        self.assertEqual(clean_document_title("机器学习入门 - Google Chrome"),
                         "机器学习入门")
        self.assertEqual(clean_document_title("什么是边际效用？ | Microsoft Edge"),
                         "什么是边际效用？")
        self.assertEqual(
            clean_document_title("(12) 什么是边际效用？ - YouTube and 8 more pages - 个人 - Microsoft Edge"),
            "(12) 什么是边际效用？ - YouTube")

    def test_strips_reader_tails_and_pdf_extension(self):
        self.assertEqual(clean_document_title("第 3 章 概率论.pdf - Adobe Acrobat Reader"),
                         "第 3 章 概率论")
        self.assertEqual(clean_document_title("论文终稿.docx - Microsoft Word"), "论文终稿")
        self.assertEqual(clean_document_title("notes.md - Typora"), "notes")

    def test_keeps_multi_level_titles(self):
        self.assertEqual(clean_document_title("第 2 章 §2.3 弹性 - 微观经济学 - 微信读书"),
                         "第 2 章 §2.3 弹性 - 微观经济学")

    def test_falls_back_to_the_app_name(self):
        self.assertEqual(clean_document_title("记事本", app="notepad.exe"), "notepad.exe")
        self.assertEqual(clean_document_title("", app="msedge.exe"), "msedge.exe")
        self.assertEqual(clean_document_title("", app=""), "")
        self.assertEqual(clean_document_title("Word", app="WINWORD.EXE"), "WINWORD.EXE")

    def test_plain_titles_are_untouched(self):
        self.assertEqual(clean_document_title("宏观经济学讲义"), "宏观经济学讲义")
        self.assertEqual(clean_document_title("  多余   空白  "), "多余 空白")

    def test_document_kind(self):
        self.assertEqual(document_kind("x.pdf - Adobe", "file:///D:/a/b.pdf"), "pdf")
        self.assertEqual(document_kind("论文", "https://a.example.com/p"), "web")
        self.assertEqual(document_kind("论文", "https://a.example.com/x.pdf?x=1"), "pdf")
        self.assertEqual(document_kind("x.docx - Word"), "doc")
        self.assertEqual(document_kind("随便一个标题", ""), "")
        self.assertEqual(source_enrich._MORE_PAGES.search("a and 3 more pages b").group(0).strip(),
                         "and 3 more pages")


class TestBookmarkSourceTitle(unittest.TestCase):
    """`bookmark()` 落库时清洗标题，但**不动 doc_key**（主题归属不能因此改变）。"""

    def test_stored_title_is_cleaned(self):
        with temp_db() as db:
            svc, _cfg = make_service(db)
            eid, created = svc.bookmark(_sel())
            self.assertTrue(created)
            self.assertEqual(db.get_entry(eid)["source_title"], "经济学原理")

    def test_doc_key_is_untouched(self):
        with temp_db() as db:
            svc, _cfg = make_service(db)
            sel = _sel(title="经济学原理.pdf - Adobe Acrobat Reader", doc_key="doc:raw-title")
            eid, _created = svc.bookmark(sel)
            self.assertEqual(db.get_entry(eid)["doc_key"], "doc:raw-title")
            self.assertNotEqual(db.get_entry(eid)["source_title"], sel.source.title)


class TestDuplicateActionAppend(unittest.TestCase):
    """B4：`capture.duplicate_action = append` 时，同词不同上下文并进一条词条。"""

    def test_default_creates_a_second_entry(self):
        with temp_db() as db:
            svc, cfg = make_service(db)
            self.assertEqual(cfg.duplicate_action, "new")
            first, _ = svc.bookmark(_sel(context="第一处语境"))
            second, created = svc.bookmark(_sel(context="第二处语境"))
            self.assertNotEqual(first, second)
            self.assertTrue(created)
            self.assertEqual(db.count_entries(), 2)

    def test_append_merges_the_context(self):
        from app.db import CONTEXT_JOINER

        with temp_db() as db:
            svc, _cfg = make_service(db, **{"capture__duplicate_action": "append"})
            first, _ = svc.bookmark(_sel(context="第一处语境"))
            second, created = svc.bookmark(_sel(context="第二处语境"))
            self.assertEqual(first, second, "追加模式不该再建新词条")
            self.assertFalse(created)
            self.assertEqual(db.count_entries(), 1)
            self.assertEqual(db.get_entry(first)["context"],
                             f"第一处语境{CONTEXT_JOINER}第二处语境")

    def test_append_emits_an_appended_event(self):
        with temp_db() as db:
            svc, _cfg = make_service(db, **{"capture__duplicate_action": "append"})
            seen = []
            svc._on_event = lambda event, payload: seen.append((event, payload))
            svc.bookmark(_sel(context="第一处语境"))
            svc.bookmark(_sel(context="第二处语境"))
            self.assertEqual(len(seen), 2)
            event, payload = seen[-1]
            self.assertEqual(event, "bookmarked")
            self.assertTrue(payload.get("appended"))
            self.assertFalse(payload.get("created"))
            self.assertIn("第二处语境", payload.get("context", ""))

    def test_append_marks_the_old_explanation_stale(self):
        with temp_db() as db:
            svc, _cfg = make_service(db, **{"capture__duplicate_action": "append"})
            first, _ = svc.bookmark(_sel(context="第一处语境"))
            db.update_entry(first, one_line="旧解释", explain_status="ok")
            svc.bookmark(_sel(context="第二处语境"))
            self.assertEqual(db.get_entry(first)["explain_status"], "stale")

    def test_append_falls_back_to_a_new_entry_for_other_documents(self):
        with temp_db() as db:
            svc, _cfg = make_service(db, **{"capture__duplicate_action": "append"})
            first, _ = svc.bookmark(_sel(context="第一处语境", doc_key="doc:a"))
            second, created = svc.bookmark(_sel(context="第二处语境", doc_key="doc:b",
                                                title="另一篇文章 - Google Chrome"))
            self.assertNotEqual(first, second, "不同文档仍然是两条（主题也不同）")
            self.assertTrue(created)

    def test_append_ignores_an_empty_context(self):
        with temp_db() as db:
            svc, _cfg = make_service(db, **{"capture__duplicate_action": "append"})
            first, _ = svc.bookmark(_sel(context="第一处语境"))
            second, created = svc.bookmark(_sel(context=""))
            self.assertNotEqual(first, second, "没有上下文就没什么可追加的")
            self.assertTrue(created)

    def test_dedupe_still_wins_over_append(self):
        """同一句话又划到（上下文也一样）→ 还是走去重计数，不追加。"""
        with temp_db() as db:
            svc, _cfg = make_service(db, **{"capture__duplicate_action": "append"})
            first, _ = svc.bookmark(_sel(context="同一句话"))
            second, created = svc.bookmark(_sel(context="同一句话"))
            self.assertEqual(first, second)
            self.assertFalse(created)
            self.assertEqual(db.get_entry(first)["repeat_count"], 2)
            self.assertEqual(db.get_entry(first)["context"], "同一句话")


class TestConfigDefault(unittest.TestCase):
    def test_defaults_are_compatible(self):
        with temp_db() as db:
            cfg = Config(db)
            self.assertEqual(cfg.duplicate_action, "new", "默认行为必须与旧版本一致")
            cfg.set("capture.duplicate_action", "APPEND")
            self.assertEqual(cfg.duplicate_action, "append")
            cfg.set("capture.duplicate_action", "写错了")
            self.assertEqual(cfg.duplicate_action, "new", "非法值必须退回默认")


if __name__ == "__main__":
    unittest.main()
