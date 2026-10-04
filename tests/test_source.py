"""T5 持久化 / T2 自身过滤 / T1 来源切换 / T3 文档键 / T4 固定批次。

最终交互：**划选本身不落库**（``handle_gesture`` 只发内存快照事件），
只有浮条上唯一的按钮「解释并记录」才会写 SQLite。
因此这些「来源 / 批次归属」用例统一用 :func:`capture_and_record`
模拟「划选 → 点按钮落库」这一条真实路径。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest import mock

from tests.support import FakeBridge, foreground, make_service, ok_response, temp_db, fail_response
from app.capture_service import (complete_word_tail, make_doc_key, normalize_url_key,
                                 strip_page_marker)


def capture_and_record(svc, x, y, kind="drag"):
    """模拟最终交互：划选取内存快照 → 用户点唯一按钮「解释并记录」才落库。"""
    sel, reason = svc.attempt_capture(x, y, kind)
    if sel is None:
        return None, reason
    return svc.record_snapshot(sel)


class TestSourceSwitchAndSelfFilter(unittest.TestCase):
    def test_source_switch_creates_distinct_batches(self):
        """T1：A 文档 → B 文档 → 回到 A，批次归属正确。"""
        with temp_db() as db:
            bridge = FakeBridge(lambda i: ok_response(
                text=["alpha", "beta", "gamma"][i],
                url=["https://news.example.com/a",
                     "https://other.example.com/b",
                     "https://news.example.com/a"][i],
            ))
            svc, _ = make_service(db, bridge)

            with foreground(title="Site A"):
                capture_and_record(svc, 10, 10)
            with foreground(title="Site B", app="firefox.exe", hwnd=55502):
                capture_and_record(svc, 20, 20)
            with foreground(title="Site A"):
                capture_and_record(svc, 30, 30)

            batches = db.list_batches()
            self.assertEqual(len(batches), 2, "A/B 两个文档应该只有两个主题")
            names = {b["name"] for b in batches}
            # 自动主题名 = 该页**首个关键词**的简短名（不再拼时间戳/长标题）
            self.assertEqual(names, {"alpha", "beta"})
            self.assertTrue(all(b["name_source"] == "auto" for b in batches))

            by_key = {b["source_key"]: b for b in batches}
            key_a = "url:https://news.example.com/a"
            key_b = "url:https://other.example.com/b"
            self.assertIn(key_a, by_key)
            self.assertIn(key_b, by_key)
            # 回到 A 仍然进 A 批次（同一来源跨会话复用，不因时间窗口新建）
            terms_a = [r["term"] for r in db.list_entries(int(by_key[key_a]["id"]))]
            self.assertEqual(sorted(terms_a), ["alpha", "gamma"])

    def test_same_source_reused_across_days(self):
        """同一可靠来源跨天复用同一批次（不再有 120 分钟窗口）。"""
        with temp_db() as db:
            bridge = FakeBridge(lambda i: ok_response(
                text=f"t{i}", url="https://book.example.com/novel"))
            svc, _ = make_service(db, bridge)
            base = datetime(2026, 9, 30, 23, 50)
            for i in range(4):
                with foreground(title="在线小说"):
                    capture_and_record(svc, 10 + i, 10)
                # 每次推进 12 小时（跨天），仍然必须是同一个批次
                svc.db.execute("UPDATE batches SET updated_at=?",
                               ((base + timedelta(hours=12 * i)).astimezone()
                                .replace(microsecond=0).isoformat(),))
            self.assertEqual(len(db.list_batches()), 1, "跨天也必须复用同一批次")
            self.assertEqual(db.count_entries(), 4)

    def test_manual_selection_never_redirects_new_captures(self):
        """最新契约：手工选中某个主题只影响**浏览**，绝不改变新捕获的归属。"""
        with temp_db() as db:
            bridge = FakeBridge(lambda i: ok_response(
                text=f"e{i}", url="https://book.example.com/novel"))
            svc, _ = make_service(db, bridge)
            with foreground(title="在线小说"):
                capture_and_record(svc, 1, 1)
            first = svc.current_batch_id()

            # 旧接口仍可调用（兼容），但归属不再受它影响
            other = db.create_batch("别处主题", "url:https://other.example.com/x",
                                    "url_document", name_source="manual")
            svc.set_current_batch(other, explicit=True)

            with foreground(title="在线小说"):
                entry_id, _ = capture_and_record(svc, 2, 2)

            self.assertEqual(len(db.list_batches()), 2)
            self.assertEqual(db.count_entries(first), 2, "新捕获必须回到它自己那一页的主题")
            self.assertEqual(db.count_entries(other), 0, "手工选中不得把词写进别的主题")
            self.assertEqual(int(db.get_entry(entry_id)["batch_id"]), first)

    def test_self_window_is_ignored(self):
        """T2：前台是本进程窗口时不捕获、不建批次、不污染来源。"""
        with temp_db() as db:
            bridge = FakeBridge(ok_response(text="should_not_appear"))
            svc, _ = make_service(db, bridge)

            with foreground(title="真实文档"):
                capture_and_record(svc, 1, 1)
            self.assertEqual(db.count_entries(), 1)

            before_source = svc.last_source()
            before_batches = len(db.list_batches())

            with foreground(title="探索词典 — 主界面", app="python.exe", pid=1234,
                            hwnd=55599, is_self=True):
                svc.handle_gesture(2, 2, "drag", delay=0)
                self.assertIsNone(svc.note_foreground(), "自身窗口不应更新来源")
                self.assertEqual(svc.attempt_capture(2, 2, "drag")[1], "self_window")

            self.assertEqual(db.count_entries(), 1, "自身窗口不应产生词条")
            self.assertEqual(len(db.list_batches()), before_batches)
            self.assertEqual(svc.last_source().title, before_source.title,
                             "切到自身不得污染来源")

    def test_uia_failure_is_silent_and_does_not_write(self):
        with temp_db() as db:
            bridge = FakeBridge(fail_response("no_textpattern"))
            svc, _ = make_service(db, bridge)
            with foreground(title="Word 文档", app="WINWORD.EXE"):
                svc.handle_gesture(3, 3, "drag", delay=0)
            self.assertEqual(db.count_entries(), 0)
            self.assertEqual(svc.last_failure_reason, "no_textpattern")

    def test_term_length_filter(self):
        with temp_db() as db:
            bridge = FakeBridge(ok_response(text="x" * 200))
            svc, _ = make_service(db, bridge, capture__max_len=80)
            with foreground(title="Doc"):
                svc.handle_gesture(4, 4, "drag", delay=0)
            self.assertEqual(db.count_entries(), 0)
            self.assertIn("length_out_of_range", svc.last_failure_reason)

    def test_paused_capture_does_nothing(self):
        with temp_db() as db:
            bridge = FakeBridge(ok_response(text="alpha"))
            svc, cfg = make_service(db, bridge)
            cfg.set_bool("capture.enabled", False)
            with foreground(title="Doc"):
                svc.handle_gesture(5, 5, "drag", delay=0)
            self.assertEqual(db.count_entries(), 0)
            self.assertEqual(len(bridge.calls), 0, "暂停时不应调用 UIA")


class TestDocKey(unittest.TestCase):
    def test_document_identity_params_are_kept(self):
        """Codex 审阅：?id=1 与 ?id=2 是不同文档，不能混在一起。"""
        a = normalize_url_key("https://x.com/article?id=1")
        b = normalize_url_key("https://x.com/article?id=2")
        self.assertNotEqual(a, b)
        self.assertEqual(a, "https://x.com/article?id=1")

    def test_spa_routes_are_kept(self):
        a = normalize_url_key("https://x.com/app/#/doc/1")
        b = normalize_url_key("https://x.com/app/#/doc/2")
        self.assertNotEqual(a, b, "SPA 路由属于文档身份，必须保留")
        self.assertEqual(a, "https://x.com/app#/doc/1")

    def test_only_tracking_params_dropped(self):
        a = normalize_url_key("https://x.com/a?id=7&utm_source=wx&utm_campaign=t&fbclid=zz")
        b = normalize_url_key("https://x.com/a?id=7")
        self.assertEqual(a, b)
        self.assertEqual(a, "https://x.com/a?id=7")

    def test_param_order_and_default_port_normalized(self):
        a = normalize_url_key("https://x.com:443/a?b=2&a=1")
        b = normalize_url_key("https://x.com/a?a=1&b=2")
        self.assertEqual(a, b)

    def test_pdf_page_anchor_ignored(self):
        """PDF 页码锚点特例：同一 PDF 翻页不算新文档。"""
        a = normalize_url_key("https://x.com/paper.pdf#page=3&zoom=100,0,0")
        b = normalize_url_key("https://x.com/paper.pdf")
        self.assertEqual(a, b)
        # 但其它锚点（章节）仍算文档身份
        self.assertNotEqual(normalize_url_key("https://x.com/paper.pdf#sec3"), b)

    def test_trailing_slash_normalized(self):
        self.assertEqual(normalize_url_key("https://x.com/a/"), "https://x.com/a")

    def test_title_page_markers_stripped(self):
        cases = [
            ("论文标题 - 3 / 10", "论文标题"),
            ("Paper Title - Page 3 of 10", "Paper Title"),
            ("说明书 第 5 页", "说明书"),
            ("Report (3/10)", "Report"),
            ("Slide - P. 4 of 12", "Slide"),
        ]
        for raw, expect in cases:
            self.assertEqual(strip_page_marker(raw), expect, raw)

    def test_single_trailing_number_is_not_stripped(self):
        """Codex 审阅：标题末尾的单独数字/括号数字可能是不同文档，不能一律删。"""
        cases = [
            ("Report (7)", "Report (7)"),
            ("Deck | 12", "Deck | 12"),
            ("ISO 标准 9001", "ISO 标准 9001"),
        ]
        for raw, expect in cases:
            self.assertEqual(strip_page_marker(raw), expect, raw)
        self.assertNotEqual(make_doc_key("WINWORD.EXE", "", "季度报告 (7)"),
                            make_doc_key("WINWORD.EXE", "", "季度报告 (8)"))

    def test_make_doc_key_falls_back_to_title(self):
        k1 = make_doc_key("WINWORD.EXE", "", "报告 - 1 / 9")
        k2 = make_doc_key("WINWORD.EXE", "", "报告 - 2 / 9")
        self.assertEqual(k1, k2)
        self.assertTrue(k1.startswith("app:winword.exe"))

    def test_same_url_reuses_batch(self):
        with temp_db() as db:
            bridge = FakeBridge(lambda i: ok_response(
                text=f"t{i}", url="https://site.example.com/book#page=2"))
            svc, _ = make_service(db, bridge)
            with foreground(title="在线书"):
                for i in range(4):
                    capture_and_record(svc, 10 + i, 10)
            self.assertEqual(len(db.list_batches()), 1, "同一文档不应创建新批次")
            self.assertEqual(db.count_entries(), 4)


class TestPinnedBatchLegacy(unittest.TestCase):
    """最新契约：旧的「固定批次」标记**不再**影响新捕获的归属（数据保留、兼容可读）。"""

    def test_pinned_batch_does_not_absorb_new_sources(self):
        with temp_db() as db:
            bridge = FakeBridge(lambda i: ok_response(
                text=f"w{i}", url=f"https://site{i}.example.com/x"))
            svc, _ = make_service(db, bridge)
            with foreground(title="Doc 0"):
                capture_and_record(svc, 1, 1)
            pinned_id = svc.current_batch_id()
            self.assertIsNotNone(pinned_id)

            db.set_pinned(pinned_id)
            svc.set_current_batch(pinned_id)

            with foreground(title="Doc 1", app="firefox.exe", hwnd=60001):
                capture_and_record(svc, 2, 2)
            with foreground(title="Doc 2", app="WINWORD.EXE", hwnd=60002):
                capture_and_record(svc, 3, 3)

            self.assertEqual(len(db.list_batches()), 3, "每个页面各自成组，固定标记不得再吞并")
            self.assertEqual(db.count_entries(pinned_id), 1)
            # 旧数据/旧接口保留：标记还在，只是不再干预归属
            self.assertEqual(db.pinned_batch()["id"], pinned_id)

    def test_unpin_keeps_page_ownership(self):
        with temp_db() as db:
            bridge = FakeBridge(lambda i: ok_response(
                text=f"u{i}", url=f"https://s{i}.example.com/p"))
            svc, _ = make_service(db, bridge)
            with foreground(title="Doc 0"):
                capture_and_record(svc, 1, 1)
            pinned_id = svc.current_batch_id()
            db.set_pinned(pinned_id)
            with foreground(title="Doc 1", app="firefox.exe", hwnd=61001):
                capture_and_record(svc, 2, 2)
            self.assertEqual(len(db.list_batches()), 2, "固定期间也必须按页面分组")

            db.set_pinned(None)
            with foreground(title="Doc 2", app="firefox.exe", hwnd=61002, pid=7777):
                capture_and_record(svc, 3, 3)
            self.assertEqual(len(db.list_batches()), 3)


class TestAutoTopicNaming(unittest.TestCase):
    """自动主题命名：首个关键词的简短名；无 Key 可用；不同来源不因同名合并。"""

    def test_first_keyword_names_the_topic(self):
        with temp_db() as db:
            bridge = FakeBridge(ok_response(text="边际效用", url="https://e.example.com/x"))
            svc, _ = make_service(db, bridge)
            with foreground(title="经济学讲义"):
                capture_and_record(svc, 1, 1)
            batch = db.list_batches()[0]
            self.assertEqual(batch["name"], "边际效用")
            self.assertEqual(batch["name_source"], "auto")

    def test_long_first_term_is_shortened(self):
        with temp_db() as db:
            term = "这是一个非常非常长的术语名称用于测试截断行为"
            bridge = FakeBridge(ok_response(text=term, url="https://e.example.com/long"))
            svc, _ = make_service(db, bridge)
            with foreground(title="长词页"):
                capture_and_record(svc, 1, 1)
            name = db.list_batches()[0]["name"]
            self.assertLessEqual(len(name), 17)
            self.assertTrue(name.endswith("…"))

    def test_same_topic_name_from_different_sources_is_not_merged(self):
        """两个不同页面恰好都以同一个词开头 → 仍是两个独立主题。"""
        with temp_db() as db:
            bridge = FakeBridge(lambda i: ok_response(
                text="模型", url=["https://a.example.com/p1",
                                  "https://b.example.com/p2"][i]))
            svc, _ = make_service(db, bridge)
            with foreground(title="A 站"):
                capture_and_record(svc, 1, 1)
            with foreground(title="B 站", app="firefox.exe", hwnd=62001):
                capture_and_record(svc, 2, 2)
            batches = db.list_batches()
            self.assertEqual(len(batches), 2)
            self.assertEqual({b["name"] for b in batches}, {"模型"})
            self.assertEqual(len({b["source_key"] for b in batches}), 2)

    def test_name_falls_back_to_page_title_without_term(self):
        from app.capture_service import auto_topic_name
        from app.models import SourceInfo

        self.assertEqual(auto_topic_name("", SourceInfo(title="论文 - 3 / 9",
                                                        app="chrome.exe")), "论文")
        self.assertEqual(auto_topic_name("", SourceInfo()), "未命名主题")

    def test_auto_rename_never_overwrites_user_or_legacy_names(self):
        with temp_db() as db:
            auto_id = db.create_batch("旧自动名", "k1", name_source="auto")
            manual_id = db.create_batch("用户改名", "k2", name_source="manual")
            legacy_id = db.create_batch("旧库名字", "k3")
            db.execute("UPDATE batches SET name_source='' WHERE id=?", (legacy_id,))

            self.assertTrue(db.rename_batch_auto(auto_id, "新主题名"))
            self.assertEqual(db.get_batch(auto_id)["name"], "新主题名")
            self.assertFalse(db.rename_batch_auto(manual_id, "被覆盖"))
            self.assertEqual(db.get_batch(manual_id)["name"], "用户改名")
            self.assertFalse(db.rename_batch_auto(legacy_id, "被覆盖"))
            self.assertEqual(db.get_batch(legacy_id)["name"], "旧库名字")

    def test_manual_rename_marks_name_source(self):
        with temp_db() as db:
            bid = db.create_batch("自动名", "k", name_source="auto")
            db.rename_batch(bid, "我的名字")
            self.assertEqual(db.get_batch(bid)["name"], "我的名字")
            self.assertEqual(db.batch_name_source(bid), "manual")
            self.assertFalse(db.rename_batch_auto(bid, "晚到的主题名"))
            self.assertEqual(db.get_batch(bid)["name"], "我的名字")


class TestCurrentPageScope(unittest.TestCase):
    """只读的「当前页面 → 已有主题」查找：划选 / 翻页 / 轮询都不得写库或建主题。"""

    @staticmethod
    def _poll(title: str, hwnd: int = 55501, app: str = "msedge.exe") -> dict:
        return {"hwnd": hwnd, "pid": 9999, "title": title, "app": app,
                "exe": f"C:\\Program Files\\{app}", "is_self": False}

    def test_unknown_page_is_known_but_has_no_topic(self):
        with temp_db() as db:
            svc, _ = make_service(db, FakeBridge(ok_response(text="alpha")))
            self.assertEqual(svc.current_page_scope(), (None, False),
                             "还没有任何来源信息 → 未知（调用方可保持全部词语）")
            svc.note_foreground(self._poll("新页面"))
            self.assertEqual(svc.current_page_scope(), (None, True),
                             "新页面已知但还没有主题 → 空词表（不是全库）")
            self.assertEqual(db.list_batches(), [], "只读查找不得创建主题")
            self.assertEqual(db.count_entries(), 0)
            self.assertIsNone(db.get_setting("ui.current_batch_id"),
                              "只读查找不得写 settings")

    def test_existing_page_topic_is_found_without_any_write(self):
        with temp_db() as db:
            bridge = FakeBridge(lambda i: ok_response(
                text=f"t{i}", url=f"https://site{i}.example.com/p"))
            svc, cfg = make_service(db, bridge)
            with foreground(title="Doc 0"):
                capture_and_record(svc, 1, 1)
            topic = svc.current_batch_id()
            self.assertIsNotNone(topic)

            writes: list = []
            with mock.patch.object(cfg, "set",
                                   side_effect=lambda k, v: writes.append((k, v))), \
                    mock.patch.object(db, "execute",
                                      side_effect=AssertionError("只读查找不得写库")), \
                    mock.patch.object(db, "create_batch",
                                      side_effect=AssertionError("只读查找不得建主题")):
                with foreground(title="Doc 1", app="firefox.exe", hwnd=60001):
                    svc.note_foreground()
                    self.assertEqual(svc.current_page_scope(), (None, True))
                with foreground(title="Doc 0"):        # 回到第一页
                    svc.note_foreground()
                    self.assertEqual(svc.current_page_scope(), (topic, True),
                                     "回到已保存过的页面必须找回它的主题")
            self.assertEqual(writes, [], "只读查找不得写 settings")
            self.assertEqual(len(db.list_batches()), 1, "全程只有一个主题")

    def test_url_identity_is_not_flipped_back_by_title_polling(self):
        """同页：轮询只有标题回退键，UIA 补出的 URL 身份不得被来回顶掉。"""
        from app.models import SourceInfo

        with temp_db() as db:
            bridge = FakeBridge(ok_response(
                text="卷积", url="https://news.example.com/a",
                top_title="深度学习入门 · 第 3 章 卷积"))
            svc, _ = make_service(db, bridge)
            with foreground(title="深度学习入门"):
                sel, reason = svc.attempt_capture(1, 1, "drag")
            self.assertIsNotNone(sel, reason)
            url_key = svc.page_doc_key(sel.source)
            self.assertTrue(url_key.startswith("url:"), url_key)

            # 轮询（只有窗口标题）连来三次：身份必须一直是 URL
            for _ in range(3):
                svc.note_foreground(self._poll("深度学习入门"))
                self.assertEqual(svc.page_doc_key(svc.last_source()), url_key,
                                 "轮询不得把已确认的 URL 身份翻转成标题回退键")
            # 即使收到一个**只有标题回退键**的来源（旧调用点 / 手工构造），
            # 同一个 (HWND, 标题) 也必须换回已确认的 URL 身份
            plain = SourceInfo(
                hwnd=55501, pid=9999, app="msedge.exe", title="深度学习入门",
                confidence="window_title_only",
                doc_key=make_doc_key("msedge.exe", "", "深度学习入门"))
            self.assertTrue(plain.doc_key.startswith("app:"))
            self.assertEqual(svc.page_doc_key(plain), url_key)
            # 真的换了页面（HWND + 标题都变）→ 身份必须跟着变
            svc.note_foreground(self._poll("另一篇文档", hwnd=60009))
            self.assertNotEqual(svc.page_doc_key(svc.last_source()), url_key)


class TestConfidenceAndManual(unittest.TestCase):
    def test_no_url_means_window_title_only(self):
        with temp_db() as db:
            bridge = FakeBridge(ok_response(text="alpha", url=""))
            svc, _ = make_service(db, bridge)
            with foreground(title="本地 PDF.pdf"):
                capture_and_record(svc, 1, 1)
            row = db.list_entries()[0]
            self.assertEqual(row["source_confidence"], "window_title_only")
            self.assertEqual(row["source_url"], "")

    def test_url_marks_url_document(self):
        with temp_db() as db:
            bridge = FakeBridge(ok_response(text="alpha", url="https://a.example.com/p?q=1"))
            svc, _ = make_service(db, bridge)
            with foreground(title="网页"):
                capture_and_record(svc, 1, 1)
            row = db.list_entries()[0]
            self.assertEqual(row["source_confidence"], "url_document")
            self.assertIn("a.example.com", row["source_url"])

    def test_manual_entry_uses_last_source_not_self(self):
        """手工录入时前台是本进程窗口，来源应沿用最近一次阅读来源。"""
        with temp_db() as db:
            bridge = FakeBridge(ok_response(text="alpha", url="https://a.example.com/p"))
            svc, _ = make_service(db, bridge)
            with foreground(title="真实网页"):
                capture_and_record(svc, 1, 1)

            with foreground(title="探索词典", app="python.exe", is_self=True):
                entry_id, created = svc.manual_entry("手工词", "手工上下文")
            self.assertTrue(created)
            row = db.get_entry(entry_id)
            self.assertEqual(row["capture_method"], "manual_input")
            self.assertEqual(row["source_title"], "真实网页")
            self.assertEqual(row["term"], "手工词")


class TestSelectionTailCompletion(unittest.TestCase):
    """选区尾部少字符：``complete_word_tail`` **只补到词尾**，补不了就一个字不动。

    取证（用户报告「选了 Shifting，面板显示 shiftin」）：本机库里最近 10 条词语有
    **8 条**正好是 ``context`` 里紧跟其后那个字母的缺格版本 —— ``shiftin`` /
    ``shifting``、``adherenc`` / ``adherence``、``high computationa`` /
    ``computational``（探针 ``_check/term_offbyone_peek.py``）。少字符发生在 UIA
    读取侧（Chromium/Edge 的 PDF 阅读器），应用侧只做规范化、从不截断词语。
    补齐的判据不是猜，而是 helper 回传的 ``context_prefix``：选区起点在
    ``context`` 里的字符偏移 —— ``context.startswith(term, prefix)`` 对不上就不补。
    """

    CTX = "…maintained under adherence to strict resource constraints…"

    def _complete(self, term, context=None, prefix=None, **kw):
        context = self.CTX if context is None else context
        if prefix is None:
            prefix = context.index(term) if term in context else -1
        return complete_word_tail(term, context, prefix, **kw)

    def test_missing_last_letter_is_completed(self):
        self.assertEqual(self._complete("adherenc"), "adherence")
        self.assertEqual(self._complete("shiftin", "…are shifting over time…"), "shifting")
        self.assertEqual(self._complete("computationa", "…high computational overhead…"),
                         "computational")

    def test_an_already_complete_word_is_untouched(self):
        self.assertEqual(self._complete("adherence"), "adherence")
        self.assertEqual(self._complete("alpha", "…alpha beta…"), "alpha")

    def test_unknown_or_impossible_offset_completes_nothing(self):
        for prefix in (None, -1, 0, 999, "abc"):
            with self.subTest(prefix=prefix):
                ctx = self.CTX
                self.assertEqual(complete_word_tail("adherenc", ctx, prefix), "adherenc",
                                 "偏移不可用/越界时必须 fail-closed")

    def test_an_offset_that_does_not_match_the_context_completes_nothing(self):
        start = self.CTX.index("adherence")
        for prefix in (start + 1, start - 1, start + 4):
            with self.subTest(prefix=prefix):
                self.assertEqual(complete_word_tail("adherenc", self.CTX, prefix),
                                 "adherenc",
                                 "位置对不上就不是「少一格」，一个字都不补")

    def test_a_tail_that_is_too_long_is_left_alone(self):
        ctx = "…adherenc" + "x" * 9 + " tail…"
        self.assertEqual(complete_word_tail("adherenc", ctx, ctx.index("adherenc")),
                         "adherenc",
                         "补到上限还在词里 ⇒ 不是「少一格」，按原样返回")

    def test_a_tail_that_reaches_the_limit_is_still_completed(self):
        ctx = "…adherenc" + "e" * 8 + " to strict…"
        self.assertEqual(complete_word_tail("adherenc", ctx, ctx.index("adherenc")),
                         "adherenc" + "e" * 8)

    def test_it_never_crosses_a_space_or_touches_chinese(self):
        ctx = "…shifting, over time…"
        # 词后就是标点：本词已经完整
        self.assertEqual(complete_word_tail("shifting", ctx, ctx.index("shifting")),
                         "shifting")
        # 中文没有词边界：绝不越过一个汉字去补
        zh = "概念漂移很重要"
        self.assertEqual(complete_word_tail("概念", zh, 0), "概念")
        # 词语末尾不是 ASCII 字母/数字 ⇒ 不补
        self.assertEqual(complete_word_tail("shifting!", "…shifting!over…", 1), "shifting!")

    def test_a_partial_selection_is_completed_to_the_word_end(self):
        """划选到词的中间时也会补到词尾 —— **有意为之**（词典查的是词）。

        上限 + 「补到真正的边界」两条同时成立才补，所以不会一路吞掉后面的词。
        """
        self.assertEqual(self._complete("adher", self.CTX), "adherence")

    # ---------------------------------------------------------------- 真实链路
    def test_capture_completes_the_term_from_the_helper_offset(self):
        with temp_db() as db:
            ctx = "the task data distributions are shifting over time"
            bridge = FakeBridge(ok_response(text="shiftin", context=ctx,
                                            context_prefix=ctx.index("shiftin"),
                                            url="https://arxiv.org/abs/2509.1"))
            svc, _ = make_service(db, bridge)
            with foreground(title="RCCDA 论文"):
                sel, reason = svc.attempt_capture(10, 10, "double_click")
            self.assertIsNotNone(sel, reason)
            self.assertEqual(sel.term, "shifting", "少的那一格必须补回来")
            self.assertEqual(sel.context, ctx, "上下文本身不许被改动")

    def test_capture_keeps_the_raw_term_when_the_offset_is_unknown(self):
        """旧 helper / 多选区 / 偏移未知：行为与修复前完全一致（一个字不补）。"""
        with temp_db() as db:
            ctx = "the task data distributions are shifting over time"
            bridge = FakeBridge(ok_response(text="shiftin", context=ctx))
            svc, _ = make_service(db, bridge)
            with foreground(title="RCCDA 论文"):
                sel, reason = svc.attempt_capture(10, 10, "double_click")
            self.assertIsNotNone(sel, reason)
            self.assertEqual(sel.term, "shiftin")

    def test_capture_keeps_the_raw_term_when_the_context_does_not_match(self):
        with temp_db() as db:
            ctx = "the task data distributions are shifting over time"
            bridge = FakeBridge(ok_response(text="shiftin", context=ctx, context_prefix=0))
            svc, _ = make_service(db, bridge)
            with foreground(title="RCCDA 论文"):
                sel, reason = svc.attempt_capture(10, 10, "double_click")
            self.assertIsNotNone(sel, reason)
            self.assertEqual(sel.term, "shiftin", "位置对不上：不许凭空补字母")


if __name__ == "__main__":
    unittest.main()
