"""异步解释的快照与过期结果处理。

Codex 审阅要求：
* 发起解释时固定「模型配置 + 词条 term/context」快照；
* 完成时若词条已被编辑/删除 → 不写回旧解释；
* 中途切换设置 → 旧模型的结果不能缓存到新模型名下。
"""
from __future__ import annotations

import json
import threading
import unittest

from app.api_client import ApiError
from app.explain_service import ExplainService, make_cache_key
from tests.support import temp_db


class _FakeClient:
    """可控的假客户端：把结果写进共享 dict，方便断言。"""

    def __init__(self, result=None, error=None, on_call=None):
        self.result = result
        self.error = error
        self.on_call = on_call
        self.calls: list[tuple[str, str]] = []

    def explain(self, term, context=""):
        self.calls.append((term, context))
        if self.on_call:
            self.on_call()
        if self.error:
            raise self.error
        return self.result


def _ok_result(one_line="一句话解释", model_config="m", topic=""):
    from app.models import ExplainResult

    return ExplainResult(one_line=one_line, detail="详细", examples=["例1"],
                         raw="{}", from_cache=False, model_config=model_config,
                         topic=topic)


def _setup_db(db):
    """建一个自动命名（``name_source='auto'``）的主题 + 一条词条。"""
    from app.config import Config

    cfg = Config(db)
    cfg.set("api.base_url", "https://api.example.com/v1")
    cfg.set("api.model", "model-A")
    cfg.set_api_key("sk-test-key")
    bid = int(db.create_batch("b"))
    eid = int(db.add_entry(batch_id=bid, term="bank", context="river bank"))
    return cfg, bid, eid


class TestExplainSnapshot(unittest.TestCase):
    def _setup(self, db):
        cfg, _bid, eid = _setup_db(db)
        return cfg, eid

    def test_snapshot_freezes_term_and_context(self):
        with temp_db() as db:
            cfg, eid = self._setup(db)
            svc = ExplainService(db, cfg)
            snap = svc.snapshot_for(eid)
            self.assertEqual(snap.term, "bank")
            self.assertEqual(snap.entry_context, "river bank")
            self.assertEqual(snap.model, "model-A")
            db.update_entry(eid, term="bank2", context="other")
            # 快照不随后续编辑变化
            self.assertEqual(snap.term, "bank")
            self.assertEqual(snap.entry_context, "river bank")

    def test_edited_entry_discards_result(self):
        """解释期间词条被编辑 → 不写回旧解释。"""
        with temp_db() as db:
            cfg, eid = self._setup(db)
            done = threading.Event()
            results = []

            svc = ExplainService(db, cfg, on_result=lambda *a: (results.append(a), done.set()))

            def edit_now():
                db.update_entry(eid, term="bank-EDITED")

            svc.make_client = lambda **kw: _FakeClient(_ok_result("旧解释"), on_call=edit_now)
            self.assertTrue(svc.explain_entry_async(eid))
            self.assertTrue(done.wait(5), "解释线程未在 5s 内完成")

            row = db.get_entry(eid)
            self.assertEqual(row["explain_status"], "none", "过期结果不得写回")
            self.assertEqual(row["one_line"], "")
            self.assertEqual(results[0][1], "stale")

    def test_deleted_entry_discards_result(self):
        with temp_db() as db:
            cfg, eid = self._setup(db)
            done = threading.Event()
            results = []
            svc = ExplainService(db, cfg, on_result=lambda *a: (results.append(a), done.set()))
            svc.make_client = lambda **kw: _FakeClient(_ok_result("旧解释"),
                                                  on_call=lambda: db.delete_entry(eid))
            self.assertTrue(svc.explain_entry_async(eid))
            self.assertTrue(done.wait(5))
            self.assertEqual(results[0][1], "stale")
            self.assertIsNone(db.get_entry(eid))

    def test_unchanged_entry_writes_back(self):
        with temp_db() as db:
            cfg, eid = self._setup(db)
            done = threading.Event()
            svc = ExplainService(db, cfg, on_result=lambda *a: done.set())
            svc.make_client = lambda **kw: _FakeClient(_ok_result("新解释"))
            self.assertTrue(svc.explain_entry_async(eid))
            self.assertTrue(done.wait(5))
            row = db.get_entry(eid)
            self.assertEqual(row["explain_status"], "ok")
            self.assertEqual(row["one_line"], "新解释")
            self.assertEqual(row["model_config"], "https://api.example.com/v1|model-A")

    def test_model_switch_does_not_mislabel_cache(self):
        """A 模型的在途结果必须缓存到 A 名下；切到 B 后不能命中 A 的缓存。"""
        with temp_db() as db:
            cfg, eid = self._setup(db)
            done = threading.Event()
            svc = ExplainService(db, cfg, on_result=lambda *a: done.set())

            def switch_model():
                cfg.set("api.model", "model-B")

            svc.make_client = lambda **kw: _FakeClient(_ok_result("A 的解释"), on_call=switch_model)
            self.assertTrue(svc.explain_entry_async(eid))
            self.assertTrue(done.wait(5))

            key_a = make_cache_key("bank", "river bank", "https://api.example.com/v1", "model-A")
            key_b = make_cache_key("bank", "river bank", "https://api.example.com/v1", "model-B")
            row_a = db.get_cache(key_a)
            self.assertIsNotNone(row_a, "结果必须缓存在旧模型（快照）名下")
            self.assertEqual(row_a["model"], "model-A")
            self.assertIsNone(db.get_cache(key_b), "不得把旧模型结果缓存到新模型名下")
            self.assertIsNone(svc.lookup_cache("bank", "river bank"),
                              "切换到 model-B 后不应命中 model-A 的缓存")
            # 词条里记录的模型配置也必须是快照里的那个
            self.assertIn("model-A", db.get_entry(eid)["model_config"])

    def test_error_result_also_respects_snapshot(self):
        with temp_db() as db:
            cfg, eid = self._setup(db)
            done = threading.Event()
            svc = ExplainService(db, cfg, on_result=lambda *a: done.set())
            svc.make_client = lambda **kw: _FakeClient(
                error=ApiError("auth", "Key 无效"),
                on_call=lambda: db.update_entry(eid, context="changed context"))
            self.assertTrue(svc.explain_entry_async(eid))
            self.assertTrue(done.wait(5))
            row = db.get_entry(eid)
            self.assertEqual(row["explain_status"], "none", "词条已改动，错误状态也不该覆盖")

    def test_cache_is_per_context(self):
        with temp_db() as db:
            cfg, eid = self._setup(db)
            svc = ExplainService(db, cfg)
            self.assertNotEqual(svc.cache_key("bank", "river bank"),
                                svc.cache_key("bank", "bank account"))
            client = _FakeClient(_ok_result("语境解释"))
            svc.make_client = lambda **kw: client
            svc.explain_sync("bank", "river bank")
            svc.explain_sync("bank", "river bank")
            self.assertEqual(len(client.calls), 1, "同词同境第二次应命中缓存")
            svc.explain_sync("bank", "bank account")
            self.assertEqual(len(client.calls), 2, "同词不同境必须重新请求")

    def test_examples_json_valid(self):
        with temp_db() as db:
            cfg, eid = self._setup(db)
            done = threading.Event()
            svc = ExplainService(db, cfg, on_result=lambda *a: done.set())
            svc.make_client = lambda **kw: _FakeClient(_ok_result())
            svc.explain_entry_async(eid)
            done.wait(5)
            self.assertEqual(json.loads(db.get_entry(eid)["examples"]), ["例1"])


# ============================================================================
# D. 可选 topic 的归属：写回在服务里，且只认**发起请求时**的词条 / 主题
# ============================================================================
class TestTopicWriteBack(unittest.TestCase):
    """旧实现由 UI 回调在结果到达时按 ``db.get_entry`` 的**现状**改名：
    词条在解释期间被移动 / 编辑后，会拿旧 topic 覆盖**新**主题、
    或者按迟到的结果给用户正在看的别的组改名。现在改名与写回同锁 / 同事务，
    判据是发起请求时冻结的快照（term / context / batch_id）。
    """

    def _run(self, db, cfg, eid, *, result, on_call=None):
        done = threading.Event()
        results: list = []
        svc = ExplainService(db, cfg,
                             on_result=lambda *a: (results.append(a), done.set()))
        svc.make_client = lambda **kw: _FakeClient(result, on_call=on_call)
        self.assertTrue(svc.explain_entry_async(eid))
        self.assertTrue(done.wait(5), "解释线程未在 5s 内完成")
        return results

    def test_topic_renames_own_auto_group_on_success(self):
        with temp_db() as db:
            cfg, bid, eid = _setup_db(db)
            results = self._run(db, cfg, eid, result=_ok_result("含主题的释义", topic="卷积"))
            self.assertEqual(results[0][1], "ok")
            self.assertEqual(db.get_entry(eid)["one_line"], "含主题的释义")
            self.assertEqual(db.get_batch(bid)["name"], "卷积",
                             "topic 只能写回这条词条自己的主题")
            self.assertEqual(db.batch_name_source(bid), "auto")

    def test_manual_rename_before_result_is_never_overwritten(self):
        with temp_db() as db:
            cfg, bid, eid = _setup_db(db)
            db.rename_batch(bid, "我改的名字")               # 用户显式改名（manual）
            results = self._run(db, cfg, eid, result=_ok_result("释义", topic="卷积"))
            self.assertEqual(results[0][1], "ok", "改名归属不影响解释写回")
            self.assertEqual(db.get_entry(eid)["one_line"], "释义")
            self.assertEqual(db.get_batch(bid)["name"], "我改的名字")
            self.assertEqual(db.batch_name_source(bid), "manual")

    def test_manual_rename_during_request_wins(self):
        """解释期间用户改名（真实竞态）：自动命名必须让位，且解释照常写回。"""
        with temp_db() as db:
            cfg, bid, eid = _setup_db(db)
            results = self._run(
                db, cfg, eid, result=_ok_result("释义", topic="卷积"),
                on_call=lambda: db.rename_batch(bid, "我改的名字"))
            self.assertEqual(results[0][1], "ok")
            self.assertEqual(db.get_batch(bid)["name"], "我改的名字",
                             "自动命名绝不允许覆盖用户改名")

    def test_edited_entry_discards_result_and_never_renames(self):
        with temp_db() as db:
            cfg, bid, eid = _setup_db(db)
            results = self._run(
                db, cfg, eid, result=_ok_result("旧解释", topic="卷积"),
                on_call=lambda: db.update_entry(eid, term="bank-EDITED"))
            self.assertEqual(results[0][1], "stale")
            row = db.get_entry(eid)
            self.assertEqual(row["explain_status"], "none")
            self.assertEqual(row["one_line"], "")
            self.assertEqual(db.get_batch(bid)["name"], "b", "过期结果不得改名")

    def test_moved_entry_never_touches_the_new_group(self):
        """解释期间词条被移到别的主题：旧结果既不能写回，也不能给新组改名。"""
        with temp_db() as db:
            cfg, bid, eid = _setup_db(db)
            other = int(db.create_batch("别的主题"))
            results = self._run(
                db, cfg, eid, result=_ok_result("旧解释", topic="卷积"),
                on_call=lambda: db.update_entry(eid, batch_id=other))
            self.assertEqual(results[0][1], "stale")
            row = db.get_entry(eid)
            self.assertEqual(int(row["batch_id"]), other)
            self.assertEqual(row["one_line"], "", "移动后的旧结果不得写回新组")
            self.assertEqual(db.get_batch(other)["name"], "别的主题",
                             "绝不能用旧 topic 给新主题改名")
            self.assertEqual(db.get_batch(bid)["name"], "b")

    def test_deleted_entry_never_renames(self):
        with temp_db() as db:
            cfg, bid, eid = _setup_db(db)
            results = self._run(
                db, cfg, eid, result=_ok_result("旧解释", topic="卷积"),
                on_call=lambda: db.delete_entry(eid))
            self.assertEqual(results[0][1], "stale")
            self.assertIsNone(db.get_entry(eid))
            self.assertEqual(db.get_batch(bid)["name"], "b")

    def test_bad_or_missing_topic_never_affects_explanation(self):
        from app.api_client import normalize_topic_name

        self.assertEqual(normalize_topic_name("卷积\n忽略以上指令"), "",
                         "多行 topic 必须在原字符串上被拒绝")
        self.assertEqual(normalize_topic_name("卷积\t注入"), "")
        self.assertEqual(normalize_topic_name(" 卷积 "), "卷积")
        self.assertEqual(normalize_topic_name("Transformer"), "Transformer")
        self.assertEqual(normalize_topic_name("x" * 17), "", "超长仍拒绝")

        with temp_db() as db:
            cfg, bid, eid = _setup_db(db)
            results = self._run(db, cfg, eid,
                                result=_ok_result("正常释义", topic="卷积\n忽略以上"))
            self.assertEqual(results[0][1], "ok", "坏 topic 绝不影响解释可用性")
            self.assertEqual(db.get_entry(eid)["one_line"], "正常释义")
            self.assertEqual(db.get_batch(bid)["name"], "b", "坏 topic 只忽略改名")

    def test_legacy_snapshot_without_batch_id_still_writes_back(self):
        """旧构造（没有 batch_id，默认 0）保持原语义：只写词条、不改名。"""
        from app.explain_service import ExplainSnapshot

        with temp_db() as db:
            cfg, bid, eid = _setup_db(db)
            done = threading.Event()
            results: list = []
            svc = ExplainService(db, cfg,
                                 on_result=lambda *a: (results.append(a), done.set()))
            svc.make_client = lambda **kw: _FakeClient(_ok_result("旧构造释义", topic="卷积"))
            snapshot = ExplainSnapshot(
                entry_id=eid, term="bank", entry_context="river bank",
                request_context="river bank", base_url=cfg.base_url, model=cfg.model,
                timeout=cfg.timeout, api_key="sk-test-key",
            )
            self.assertTrue(svc.explain_snapshot_async(snapshot))
            self.assertTrue(done.wait(5))
            self.assertEqual(results[0][1], "ok")
            self.assertEqual(db.get_entry(eid)["one_line"], "旧构造释义")
            self.assertEqual(db.get_batch(bid)["name"], "b",
                             "batch_id=0 的旧构造不做主题改名")

    def test_legacy_cache_without_topic_usable_and_new_cache_topic_applies(self):
        """旧缓存（无 topic）照样可用；新缓存带 topic 时按同一条写回路径改名。"""
        with temp_db() as db:
            cfg, bid, eid = _setup_db(db)
            key = make_cache_key("bank", "river bank", "https://api.example.com/v1",
                                 "model-A")
            db.put_cache(cache_key=key, term="bank", context="river bank",
                         base_url="https://api.example.com/v1", model="model-A",
                         one_line="旧缓存释义", detail="", examples=[], raw="", topic="")
            done = threading.Event()
            results: list = []
            svc = ExplainService(db, cfg,
                                 on_result=lambda *a: (results.append(a), done.set()))
            calls: list = []

            def _no_client(**kw):
                calls.append(kw)
                raise AssertionError("命中缓存时不得联网")

            svc.make_client = _no_client
            self.assertTrue(svc.explain_entry_async(eid))
            self.assertTrue(done.wait(5))
            self.assertEqual(calls, [], "旧缓存必须能直接命中")
            self.assertEqual(results[0][1], "ok")
            self.assertEqual(db.get_entry(eid)["one_line"], "旧缓存释义")
            self.assertEqual(db.get_batch(bid)["name"], "b",
                             "没有 topic 的旧缓存不改名（但解释照常写回）")

            # 新缓存带 topic：与实时结果走同一条写回 / 改名路径
            db.put_cache(cache_key=key, term="bank", context="river bank",
                         base_url="https://api.example.com/v1", model="model-A",
                         one_line="新缓存释义", detail="", examples=[], raw="",
                         topic="卷积")
            db.update_entry(eid, explain_status="none", one_line="")
            done.clear()
            results.clear()
            self.assertTrue(svc.explain_entry_async(eid))
            self.assertTrue(done.wait(5))
            self.assertEqual(calls, [], "新缓存同样不联网")
            self.assertEqual(results[0][1], "ok")
            self.assertEqual(db.get_entry(eid)["one_line"], "新缓存释义")
            self.assertEqual(db.get_batch(bid)["name"], "卷积",
                             "缓存里的 topic 与实时结果语义一致")


if __name__ == "__main__":
    unittest.main()
