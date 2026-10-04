"""T6 语境缓存区别：缓存键必须区分语境与模型配置；命中缓存不得联网。"""
from __future__ import annotations

import unittest

from tests.support import temp_db
from app.explain_service import ExplainService, make_cache_key
from app.models import ExplainResult


class TestCacheKey(unittest.TestCase):
    def test_same_term_different_context_differs(self):
        k1 = make_cache_key("bank", "river bank", "https://api.deepseek.com/v1", "deepseek-chat")
        k2 = make_cache_key("bank", "investment bank", "https://api.deepseek.com/v1",
                            "deepseek-chat")
        self.assertNotEqual(k1, k2, "同词不同语境必须是不同缓存条目")

    def test_same_term_context_different_model_differs(self):
        k1 = make_cache_key("t", "c", "https://api.deepseek.com/v1", "deepseek-chat")
        k2 = make_cache_key("t", "c", "https://api.deepseek.com/v1", "deepseek-reasoner")
        self.assertNotEqual(k1, k2, "不同模型配置必须是不同缓存条目")

    def test_same_term_context_different_base_url_differs(self):
        k1 = make_cache_key("t", "c", "https://api.deepseek.com/v1", "m")
        k2 = make_cache_key("t", "c", "http://127.0.0.1:8000/v1", "m")
        self.assertNotEqual(k1, k2)

    def test_trailing_slash_and_whitespace_normalized(self):
        k1 = make_cache_key("t ", " c ", "https://api.deepseek.com/v1/", " m ")
        k2 = make_cache_key("t", "c", "https://api.deepseek.com/v1", "m")
        self.assertEqual(k1, k2)

    def test_identical_inputs_same_key(self):
        k1 = make_cache_key("卷积", "深度学习", "https://x/v1", "m")
        k2 = make_cache_key("卷积", "深度学习", "https://x/v1", "m")
        self.assertEqual(k1, k2)


class TestCacheStoreAndHit(unittest.TestCase):
    def test_put_and_lookup(self):
        with temp_db() as db:
            from app.config import Config

            cfg = Config(db)
            svc = ExplainService(db, cfg)
            result = ExplainResult(one_line="一句话", detail="细节", examples=["e1"], raw="{}")
            svc.store_cache("term", "ctx", result)
            hit = svc.lookup_cache("term", "ctx")
            self.assertIsNotNone(hit)
            self.assertTrue(hit.from_cache)
            self.assertEqual(hit.one_line, "一句话")
            self.assertEqual(hit.examples, ["e1"])
            self.assertIsNone(svc.lookup_cache("term", "另一个语境"), "不同语境不应命中")

    def test_cache_hit_does_not_touch_network(self):
        """命中缓存时 explain_sync 必须直接返回，不发请求。"""
        with temp_db() as db:
            from app.config import Config

            cfg = Config(db)
            cfg.set("api.base_url", "http://127.0.0.1:1/v1")  # 一个必然连不上的地址
            svc = ExplainService(db, cfg)
            svc.store_cache("term", "ctx",
                            ExplainResult(one_line="缓存里的一句话", detail="d", examples=[]))
            out = svc.explain_sync("term", "ctx")
            self.assertTrue(out.from_cache)
            self.assertEqual(out.one_line, "缓存里的一句话")

    def test_no_api_key_blocks_network(self):
        """未配置 key 时不得发起任何网络请求。"""
        with temp_db() as db:
            from app.config import Config
            from app.api_client import ApiError

            cfg = Config(db)
            cfg.set("api.base_url", "http://127.0.0.1:1/v1")
            svc = ExplainService(db, cfg)
            ok, msg = svc.is_ready()
            self.assertFalse(ok)
            self.assertIn("API Key", msg)
            with self.assertRaises(ApiError) as ctx:
                svc.explain_sync("term", "ctx")
            self.assertEqual(ctx.exception.kind, "config")

    def test_explain_async_without_key_reports_error(self):
        with temp_db() as db:
            from app.config import Config

            cfg = Config(db)
            svc = ExplainService(db, cfg)
            bid = db.create_batch("b")
            eid = db.add_entry(batch_id=bid, term="t", context="c")
            results = []
            svc._on_result = lambda *a: results.append(a)
            started = svc.explain_entry_async(eid)
            self.assertFalse(started, "没有 key 时不应启动线程")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0][1], "error")
            self.assertEqual(results[0][3].kind, "config")
            self.assertEqual(db.get_entry(eid)["explain_status"], "error")


if __name__ == "__main__":
    unittest.main()
