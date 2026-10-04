"""追问（词条级对话）回归测试 —— **纯 mock，零网络**。

覆盖用户本轮明确列出的每一条：

* 只有用户点「发送」才请求；**缺 Key 不发**（零网络、零对话记录）；
* 请求只带「本词 + 短上下文 + 已有解释 + 该词最近有限轮次 + 本次问题」，
  不含窗口标题 / 路径 / 整篇文档 / 其它词条；
* 历史**按词条归属**（每个词独立），切词不串话；
* 重复点发送不重复请求；失败可重试且不重复记录那一问；
* 迟到回答写回**原词条**；词条被删除 → 结果丢弃；
* 错误信息继续脱敏（绝不泄露 Key）；
* **增量迁移**：v1 旧库（没有 chat_turns 表）打开后数据原样保留并补齐新表。

测试用临时数据库 + 假客户端：绝不联网、绝不读取密钥、绝不打开正式库。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import unittest
from pathlib import Path

from tests.support import tmp_dir, temp_db, headless_app

from app.api_client import ApiError, DeepSeekClient, build_chat_messages, parse_chat_reply
from app.chat_service import ChatService
from app.config import Config
from app.db import Database, SCHEMA_VERSION
from app.models import now_iso

KEY = "sk-fake-chat-key-not-real"


def configure_key(cfg: Config, *, model: str = "model-A") -> None:
    cfg.set("api.base_url", "https://api.example.com/v1")
    cfg.set("api.model", model)
    cfg.set_api_key(KEY)


class FakeChatClient:
    """假追问客户端：记录消息，绝不联网。"""

    def __init__(self, answer: str = "这是回答", error: Exception | None = None,
                 gate: threading.Event | None = None):
        self.answer = answer
        self.error = error
        self.gate = gate
        self.calls: list[list[dict]] = []
        self.entered = threading.Event()

    def chat(self, messages, **_kw) -> str:
        self.calls.append([dict(m) for m in messages])
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(5)
        if self.error is not None:
            raise self.error
        return self.answer


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def make_service(db: Database, *, client: FakeChatClient | None = None,
                 **settings) -> tuple[ChatService, list]:
    cfg = Config(db)
    configure_key(cfg)
    for k, v in settings.items():
        cfg.set(k.replace("__", "."), str(v))
    svc = ChatService(db, cfg)
    results: list = []
    svc.set_result_sink(lambda *a: results.append(a))
    if client is not None:
        svc.make_client = lambda **kw: client       # 绝不联网
    return svc, results


def seed_entry(db: Database, term: str = "卷积", context: str = "深度学习中的卷积",
               one_line: str = "一句话解释", detail: str = "详细说明",
               source_title: str = "论文 A") -> int:
    bid = db.pinned_batch()
    if bid is None:
        bid_id = db.create_batch("批次")
    else:
        bid_id = int(bid["id"])
    eid = db.add_entry(batch_id=bid_id, term=term, context=context,
                       source_title=source_title, source_url="https://a.example.com/p")
    if one_line or detail:
        db.update_entry(eid, explain_status="ok", one_line=one_line, detail=detail)
    return int(eid)


# ============================================================ 缺 Key：零请求
class TestNoKeyNeverSends(unittest.TestCase):
    def test_submit_returns_none_and_writes_nothing(self):
        with temp_db() as db:
            cfg = Config(db)
            svc = ChatService(db, cfg)
            calls: list = []
            svc.make_client = lambda **kw: calls.append(kw)
            eid = seed_entry(db)
            self.assertIsNone(svc.submit(eid, "为什么？"))
            self.assertEqual(calls, [], "没有 Key 时绝不构造客户端/发起请求")
            self.assertEqual(db.count_chat_turns(eid), 0, "也不该留下任何对话记录")
            self.assertFalse(svc.is_inflight(eid))
            ok, msg = svc.is_ready()
            self.assertFalse(ok)
            self.assertIn("API Key", msg)

    def test_hint_is_configurable(self):
        with temp_db() as db:
            cfg = Config(db)
            cfg.set("chat.no_key_hint", "自定义提示：先去设置里配 Key")
            svc = ChatService(db, cfg)
            self.assertIn("自定义提示", svc.unavailable_hint())

    def test_chat_disabled_blocks_even_with_key(self):
        with temp_db() as db:
            svc, results = make_service(db, client=FakeChatClient())
            svc.config.set_bool("chat.enabled", False)
            eid = seed_entry(db)
            self.assertIsNone(svc.submit(eid, "问题"))
            self.assertEqual(results, [])
            self.assertEqual(svc.db.count_chat_turns(eid), 0)


# ============================================================ 正常一问一答
class TestChatRoundTrip(unittest.TestCase):
    def test_question_and_answer_are_persisted_per_entry(self):
        with temp_db() as db:
            client = FakeChatClient("卷积是……")
            svc, results = make_service(db, client=client)
            eid = seed_entry(db)
            token = svc.submit(eid, "它和互相关有什么区别？")
            self.assertIsNotNone(token)
            self.assertTrue(wait_until(lambda: results))
            self.assertEqual(db.count_chat_turns(eid), 2, "一问一答两条轮次")
            rows = db.list_chat_turns(eid)
            self.assertEqual([r["role"] for r in rows], ["user", "assistant"])
            self.assertEqual(rows[1]["content"], "卷积是……")
            self.assertEqual(rows[1]["request_id"], token)
            self.assertEqual(results[0][0], token)
            self.assertEqual(results[0][1], eid)
            self.assertEqual(results[0][2], "ok")

    def test_history_is_isolated_per_entry(self):
        with temp_db() as db:
            svc, results = make_service(db, client=FakeChatClient("答"))
            a = seed_entry(db, term="alpha", context="ctx A")
            b = seed_entry(db, term="beta", context="ctx B")
            svc.submit(a, "关于 alpha 的问题")
            self.assertTrue(wait_until(lambda: svc.turn_count(a) == 2))
            svc.submit(b, "关于 beta 的问题")
            self.assertTrue(wait_until(lambda: svc.turn_count(b) == 2))
            self.assertEqual([t["content"] for t in svc.history(a)],
                             ["关于 alpha 的问题", "答"])
            self.assertEqual([t["content"] for t in svc.history(b)],
                             ["关于 beta 的问题", "答"])

    def test_duplicate_send_does_not_start_second_request(self):
        with temp_db() as db:
            gate = threading.Event()
            client = FakeChatClient("慢回答", gate=gate)
            svc, results = make_service(db, client=client)
            eid = seed_entry(db)
            first = svc.submit(eid, "第一问")
            self.assertTrue(client.entered.wait(5))
            self.assertIsNone(svc.submit(eid, "第二问"), "在途时重复点发送不得再发")
            gate.set()
            self.assertTrue(wait_until(lambda: results))
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(db.count_chat_turns(eid), 2, "第二问根本不该被记录")

    def test_error_is_recorded_and_can_be_retried_without_duplicate_question(self):
        with temp_db() as db:
            client = FakeChatClient(error=ApiError("timeout", f"请求超时（{KEY}）"))
            svc, results = make_service(db, client=client)
            eid = seed_entry(db)
            with self.assertLogs("explorer_dict.chat", level="ERROR"):
                svc.submit(eid, "会失败的问题")
                self.assertTrue(wait_until(lambda: results))
            rows = db.list_chat_turns(eid)
            self.assertEqual(rows[-1]["status"], "error")
            self.assertNotIn(KEY, rows[-1]["content"], "错误信息必须脱敏")

            client.error = None
            client.answer = "这次成功"
            client.calls.clear()
            token = svc.retry(eid)
            self.assertIsNotNone(token)
            self.assertTrue(wait_until(lambda: len(client.calls) == 1))
            users = [r for r in db.list_chat_turns(eid) if r["role"] == "user"]
            self.assertEqual(len(users), 1, "重试不得重复记录用户那一问")
            self.assertEqual(client.calls[0][-1]["content"], "会失败的问题")
            self.assertTrue(wait_until(lambda: any(
                r["role"] == "assistant" and r["status"] == "ok"
                for r in db.list_chat_turns(eid))))

    def test_retry_without_question_does_nothing(self):
        with temp_db() as db:
            svc, results = make_service(db, client=FakeChatClient())
            eid = seed_entry(db)
            self.assertIsNone(svc.retry(eid))
            self.assertEqual(results, [])


# ============================================================ 隐私与历史裁剪
class TestPrivacyAndHistoryWindow(unittest.TestCase):
    def test_request_only_carries_term_context_explanation_and_bounded_history(self):
        with temp_db() as db:
            client = FakeChatClient("答")
            svc, results = make_service(db, client=client)
            eid = seed_entry(db, term="卷积", context="深度学习中的卷积运算",
                             one_line="一句话解释", detail="详细说明",
                             source_title="很敏感的论文标题")
            other = seed_entry(db, term="别的词", context="别的语境")
            svc.submit(other, "别的词的问题")
            self.assertTrue(wait_until(lambda: svc.turn_count(other) == 2))

            client.calls.clear()
            svc.submit(eid, "本次问题")
            self.assertTrue(wait_until(lambda: len(client.calls) == 1))
            payload = json.dumps(client.calls[0], ensure_ascii=False)
            for needle in ("卷积", "深度学习中的卷积运算", "一句话解释", "详细说明", "本次问题"):
                self.assertIn(needle, payload)
            for forbidden in ("很敏感的论文标题", "a.example.com", "别的词", "别的语境",
                              "别的词的问题"):
                self.assertNotIn(forbidden, payload, "不得发送来源标题 / URL / 其它词条")
            self.assertEqual([m["role"] for m in client.calls[0]],
                             ["system", "user", "user"], "只有系统提示 + 词条上下文 + 本次问题")
            self.assertTrue(wait_until(lambda: results))

    def test_history_turns_limit_is_respected(self):
        with temp_db() as db:
            client = FakeChatClient("答")
            svc, results = make_service(db, client=client, chat__history_turns=1)
            eid = seed_entry(db)
            for i in range(3):
                svc.submit(eid, f"第{i}问")
                self.assertTrue(wait_until(lambda: len(results) >= i + 1))
                self.assertTrue(wait_until(lambda: not svc.is_inflight(eid)))
            client.calls.clear()
            svc.submit(eid, "最后一问")
            self.assertTrue(wait_until(lambda: len(client.calls) == 1))
            contents = [m["content"] for m in client.calls[0]]
            self.assertIn("最后一问", contents)
            self.assertNotIn("第0问", contents, "超出轮次上限的旧问答不得再发送")
            self.assertIn("第2问", contents, "最近一轮必须带上")
            self.assertTrue(wait_until(lambda: results))

    def test_history_excludes_failed_answers(self):
        with temp_db() as db:
            svc, results = make_service(db, client=FakeChatClient(error=ApiError(
                "auth", "Key 无效：sk-secret-value")))
            eid = seed_entry(db)
            eid2 = seed_entry(db, term="另一个词")
            db.add_chat_turn(entry_id=eid, role="assistant", content="Key 无效：sk-secret-value",
                             status="error")
            history = svc.history(eid)
            self.assertEqual(history, [], "失败的助手轮次不参与后续请求")
            self.assertEqual(svc.history(eid2), [])

    def test_build_messages_pure_helper(self):
        messages = build_chat_messages(term="词", question="问", context="上下文",
                                       explanation="解释",
                                       history=[{"role": "user", "content": "旧问"},
                                                {"role": "assistant", "content": "旧答"},
                                                {"role": "assistant", "content": "失败",
                                                 "status": "error"}],
                                       max_chars=100)
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("词", messages[1]["content"])
        self.assertEqual(messages[-1], {"role": "user", "content": "问"})
        self.assertNotIn("失败", json.dumps(messages, ensure_ascii=False))

    def test_parse_chat_reply_strips_fence_and_rejects_empty(self):
        self.assertEqual(parse_chat_reply("```\n答案\n```"), "答案")
        with self.assertRaises(ApiError):
            parse_chat_reply("   ")


# ============================================================ 迟到结果 / 删除
class TestLateAndDeleted(unittest.TestCase):
    def test_late_answer_writes_back_to_original_entry(self):
        with temp_db() as db:
            gate = threading.Event()
            client = FakeChatClient("迟到的回答", gate=gate)
            svc, results = make_service(db, client=client)
            a = seed_entry(db, term="alpha")
            b = seed_entry(db, term="beta")
            token_a = svc.submit(a, "问 alpha")
            self.assertTrue(client.entered.wait(5))
            token_b = svc.submit(b, "问 beta")   # 用户切到别的词继续问
            self.assertIsNotNone(token_b)
            self.assertNotEqual(token_a, token_b)
            gate.set()
            self.assertTrue(wait_until(lambda: svc.turn_count(a) == 2))
            self.assertTrue(wait_until(lambda: svc.turn_count(b) == 2))
            rows_a = db.list_chat_turns(a)
            rows_b = db.list_chat_turns(b)
            self.assertEqual({r["request_id"] for r in rows_a}, {token_a},
                             "alpha 的问答必须挂在 alpha 的请求上")
            self.assertEqual({r["request_id"] for r in rows_b}, {token_b},
                             "beta 的问答必须挂在 beta 的请求上")
            self.assertEqual([r["role"] for r in rows_a], ["user", "assistant"])
            self.assertEqual([r["role"] for r in rows_b], ["user", "assistant"])
            self.assertEqual(str(rows_a[0]["content"]), "问 alpha")
            self.assertEqual(str(rows_b[0]["content"]), "问 beta")

    def test_deleted_entry_discards_result(self):
        with temp_db() as db:
            gate = threading.Event()
            client = FakeChatClient("无主回答", gate=gate)
            svc, results = make_service(db, client=client)
            eid = seed_entry(db)
            svc.submit(eid, "问题")
            self.assertTrue(client.entered.wait(5))
            db.delete_entry(eid)
            gate.set()
            self.assertTrue(wait_until(lambda: results))
            self.assertEqual(results[0][2], "discarded")
            self.assertEqual(db.count_chat_turns(), 0, "词条删了，回答不得留在库里")

    def test_submit_after_delete_returns_none(self):
        with temp_db() as db:
            svc, results = make_service(db, client=FakeChatClient())
            eid = seed_entry(db)
            db.delete_entry(eid)
            self.assertIsNone(svc.submit(eid, "问题"))
            self.assertEqual(results, [])


# ============================================================ 终态保证（本轮修正）
class TestTerminalStateGuarantees(unittest.TestCase):
    """助手轮次写库失败 / 词条已删 / 初始 DB 已关闭：只能是 error / discarded。

    绝不允许「append_turn 返回 None 却发 ok」——UI 会停在「正在回答…」，
    或者更糟：把一条根本没落库的回答当成成功显示出来。
    """

    def test_known_key_reads_config_and_never_raises(self):
        """``_known_key`` 是实例方法（调用点 ``self._known_key()``，签名一致）。"""
        with temp_db() as db:
            svc, _ = make_service(db, client=FakeChatClient())
            self.assertEqual(svc._known_key(), KEY)
            db.close()                              # 配置读不出来 → 空串，不抛异常
            self.assertEqual(svc._known_key(), "")

    def test_assistant_persist_failure_never_delivers_ok(self):
        with temp_db() as db:
            client = FakeChatClient("会写不进去的回答")
            svc, results = make_service(db, client=client)
            eid = seed_entry(db)
            original = db.add_chat_turn
            calls = {"n": 0}

            def flaky(*a, **kw):
                # 只有**成功回答**的写回失败（提问与失败留痕照常写）
                if str(kw.get("role")) == "assistant" and str(kw.get("status")) == "ok":
                    calls["n"] += 1
                    raise sqlite3.OperationalError("database is locked")
                return original(*a, **kw)

            db.add_chat_turn = flaky
            try:
                # 失败路径会记日志（异常被服务自己吃掉）—— 当场收走，别糊到测试输出里
                with self.assertLogs("explorer_dict.chat", level="ERROR") as captured:
                    token = svc.submit(eid, "问题")
                    self.assertIsNotNone(token)
                    self.assertTrue(wait_until(lambda: results))
            finally:
                db.add_chat_turn = original

            self.assertTrue(any("追问失败" in line for line in captured.output))

            self.assertEqual(calls["n"], 1, "回答确实尝试写回过一次")
            self.assertEqual([r[2] for r in results], ["error"],
                             "写库失败绝不能报 ok")
            self.assertEqual(results[0][3], "", "失败时不得把回答内容当成功结果送出")
            self.assertFalse(svc.is_inflight(eid), "失败后必须放掉在途闸门（可重试）")
            rows = db.list_chat_turns(eid)
            self.assertEqual([r["role"] for r in rows], ["user", "assistant"])
            self.assertEqual(rows[-1]["status"], "error", "失败以 error 轮次留痕")
            self.assertIn("写入数据库失败", results[0][4])

    def test_answer_arrives_after_entry_deleted_is_discarded(self):
        """已删竞态：回答回来时词条已经没了 → discarded，且绝不写任何轮次。"""
        with temp_db() as db:
            gate = threading.Event()
            client = FakeChatClient("无主回答", gate=gate)
            svc, results = make_service(db, client=client)
            eid = seed_entry(db)
            token = svc.submit(eid, "问题")
            self.assertTrue(client.entered.wait(5))
            # 用户在等待期间删掉了这条词（对话级联删除）
            db.delete_entry(eid)
            self.assertEqual(db.count_chat_turns(), 0)
            gate.set()
            self.assertTrue(wait_until(lambda: results))
            self.assertEqual([r[2] for r in results], ["discarded"])
            self.assertEqual(results[0][0], token)
            self.assertEqual(db.count_chat_turns(), 0, "词条删了，回答不得留在库里")
            self.assertFalse(svc.is_inflight(eid), "丢弃后必须放掉在途闸门")

    def test_db_closed_midflight_still_delivers_error_terminal(self):
        """回答回来时数据库已经关闭：写不进去，但**必须有** error 终态。

        ``ProgrammingError`` 只允许出现在**测试运行期间**、并且被服务自己捕获
        记日志（``assertLogs`` 当场收走）—— 收尾关库之后不能再有任何后台写库。
        """
        with temp_db() as db:
            gate = threading.Event()
            client = FakeChatClient("来不及写回的回答", gate=gate)
            svc, results = make_service(db, client=client)
            eid = seed_entry(db)
            svc.submit(eid, "问题")
            self.assertTrue(client.entered.wait(5))
            db.close()                      # 退出瞬间库被关掉（写回必然失败）
            with self.assertLogs("explorer_dict.chat", level="ERROR") as captured:
                gate.set()                  # 放行回答：写回必然失败
                self.assertTrue(wait_until(lambda: results),
                                "数据库关闭也必须给终态回调，不能停在「正在回答…」")
            self.assertEqual([r[2] for r in results], ["error"])
            self.assertFalse(svc.is_inflight(eid))
            self.assertTrue(any("追问失败" in line for line in captured.output),
                            f"异常必须被服务捕获并记录，而不是泄漏到调用方：{captured.output}")

    def test_submit_with_closed_db_returns_none_without_raising(self):
        """初始 DB 查询就失败（库已关闭）：不抛给 UI、没有终态回调也不留闸门。"""
        with temp_db() as db:
            svc, results = make_service(db, client=FakeChatClient())
            eid = seed_entry(db)
            db.close()
            with self.assertLogs("explorer_dict.chat", level="ERROR") as captured:
                self.assertIsNone(svc.submit(eid, "问题"),
                                  "关闭的库不得把异常抛给 UI")
                self.assertEqual(svc.retry(eid), None, "retry 读库失败也必须安全返回")
            self.assertEqual(results, [], "还没分配 request_id，不产生终态回调")
            self.assertFalse(svc.is_inflight(eid))
            self.assertTrue(any("追问提交失败" in line for line in captured.output))
            self.assertTrue(any("读取追问历史失败" in line for line in captured.output))

    def test_submit_when_question_write_fails_reports_error_terminal(self):
        """准备阶段写提问失败：必须给 error 终态并放掉闸门（不会永远「正在回答」）。"""
        with temp_db() as db:
            svc, results = make_service(db, client=FakeChatClient())
            eid = seed_entry(db)
            original = db.add_chat_turn

            def broken(*_a, **_kw):
                raise sqlite3.OperationalError("disk I/O error")

            db.add_chat_turn = broken
            try:
                with self.assertLogs("explorer_dict.chat", level="ERROR") as captured:
                    token = svc.submit(eid, "写不进去的提问")
            finally:
                db.add_chat_turn = original
            self.assertIsNone(token, "准备失败就不该起线程")
            self.assertEqual([r[2] for r in results], ["error"])
            self.assertFalse(svc.is_inflight(eid))
            self.assertTrue(any("追问准备失败" in line for line in captured.output),
                            "准备阶段的异常必须被捕获并记录")

    def test_error_message_redacts_snapshot_key_by_value(self):
        """非 ``sk-`` 前缀的 Key 也要按**值**脱敏（异常里带出整串也不漏）。"""
        secret = "plain-secret-key-123456"
        with temp_db() as db:
            svc, results = make_service(
                db, client=FakeChatClient(error=RuntimeError(f"连接失败：{secret}")))
            svc.config.set("api.base_url", "https://api.example.com/v1")
            svc.config.set("api.model", "model-A")
            svc.config.set_api_key(secret)
            eid = seed_entry(db)
            # 失败会记日志（异常被服务自己吃掉）：当场收走，避免把带 Key 的
            # 原始 traceback 糊到测试输出里（真正的产品日志还有 _RedactingFilter）
            with self.assertLogs("explorer_dict.chat", level="ERROR"):
                svc.submit(eid, "问题")
                self.assertTrue(wait_until(lambda: results))
            self.assertNotIn(secret, results[0][4], "错误文本必须按值脱敏")
            rows = db.list_chat_turns(eid)
            self.assertNotIn(secret, rows[-1]["content"])


# ============================================================ 数据库增量迁移
class TestChatMigration(unittest.TestCase):
    OLD_DDL = """
    CREATE TABLE batches (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        source_key TEXT NOT NULL DEFAULT '', source_kind TEXT NOT NULL DEFAULT '',
        pinned INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
        term TEXT NOT NULL, context TEXT NOT NULL DEFAULT '', doc_key TEXT NOT NULL DEFAULT '',
        source_app TEXT NOT NULL DEFAULT '', source_title TEXT NOT NULL DEFAULT '',
        source_url TEXT NOT NULL DEFAULT '',
        source_confidence TEXT NOT NULL DEFAULT 'window_title_only',
        source_note TEXT NOT NULL DEFAULT '', capture_method TEXT NOT NULL DEFAULT 'manual_input',
        captured_at TEXT NOT NULL, repeat_count INTEGER NOT NULL DEFAULT 1,
        explain_status TEXT NOT NULL DEFAULT 'none', one_line TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '', examples TEXT NOT NULL DEFAULT '[]',
        model_config TEXT NOT NULL DEFAULT '', explain_error TEXT NOT NULL DEFAULT '',
        explained_at TEXT);
    CREATE TABLE settings (k TEXT PRIMARY KEY, v TEXT NOT NULL);
    INSERT INTO settings(k, v) VALUES('schema_version', '1');
    INSERT INTO settings(k, v) VALUES('ui.topmost', '1');
    """

    def _make_old_db(self, path: Path) -> None:
        conn = sqlite3.connect(str(path))
        conn.executescript(self.OLD_DDL)
        conn.execute("INSERT INTO batches(name, created_at, updated_at) VALUES(?,?,?)",
                     ("旧批次", now_iso(), now_iso()))
        conn.execute(
            "INSERT INTO entries(batch_id, term, context, captured_at, one_line) "
            "VALUES(1, '旧词', '旧语境', ?, '旧的解释')", (now_iso(),))
        conn.commit()
        conn.close()

    def test_old_db_keeps_data_and_gains_chat_table(self):
        with tmp_dir("chatmig_") as tmp:
            path = Path(tmp) / "old.sqlite3"
            self._make_old_db(path)
            conn = sqlite3.connect(str(path))
            try:
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                conn.close()
            self.assertNotIn("chat_turns", tables, "旧库本来没有对话表")

            db = Database(path)          # 打开 = 增量迁移
            try:
                # 迁移后的版本号 = app.db.SCHEMA_VERSION（本轮 v2 → v3：
                # batches.name_source / explain_cache.topic）。这里写死 "2"
                # 是旧断言，本任务加 v3 后已过期 —— 改成跟随常量，避免再次漂移。
                self.assertEqual(db.get_setting("schema_version"), str(SCHEMA_VERSION))
                self.assertEqual(db.get_setting("ui.topmost"), "1", "旧设置必须保留")
                self.assertEqual(db.count_entries(), 1, "旧词条必须保留")
                row = db.list_entries()[0]
                self.assertEqual(row["term"], "旧词")
                self.assertEqual(row["one_line"], "旧的解释")
                turn_id = db.add_chat_turn(entry_id=int(row["id"]), role="user",
                                           content="迁移后的问题")
                self.assertTrue(turn_id)
                self.assertEqual(db.count_chat_turns(int(row["id"])), 1)
            finally:
                db.close()

            db2 = Database(path)         # 再开一次：迁移必须幂等
            try:
                self.assertEqual(db2.count_entries(), 1)
                self.assertEqual(db2.count_chat_turns(), 1)
            finally:
                db2.close()

    def test_chat_turns_cascade_on_entry_delete(self):
        with temp_db() as db:
            eid = seed_entry(db)
            db.add_chat_turn(entry_id=eid, role="user", content="问题")
            db.add_chat_turn(entry_id=eid, role="assistant", content="回答")
            self.assertEqual(db.count_chat_turns(eid), 2)
            db.delete_entry(eid)
            self.assertEqual(db.count_chat_turns(), 0, "删词条必须级联删除对话")

    def test_history_limit_returns_latest_in_order(self):
        with temp_db() as db:
            eid = seed_entry(db)
            for i in range(6):
                db.add_chat_turn(entry_id=eid, role="user", content=f"q{i}")
            rows = db.list_chat_turns(eid, limit=4)
            self.assertEqual([r["content"] for r in rows], ["q2", "q3", "q4", "q5"])


# ============================================================ API 客户端接口
class TestChatClientInterface(unittest.TestCase):
    def test_missing_key_raises_config_error_without_network(self):
        client = DeepSeekClient("https://api.example.com/v1", "m", "", timeout=1)
        with self.assertRaises(ApiError) as ctx:
            client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(ctx.exception.kind, "config")

    def test_ask_builds_messages_through_public_helper(self):
        client = DeepSeekClient("https://api.example.com/v1", "m", "", timeout=1)
        sent: list = []
        client.chat = lambda messages, **kw: sent.append(messages) or "答"
        self.assertEqual(client.ask("问题", term="词", context="上下文",
                                    explanation="解释",
                                    history=[{"role": "user", "content": "旧"}]), "答")
        payload = json.dumps(sent[0], ensure_ascii=False)
        self.assertIn("问题", payload)
        self.assertIn("旧", payload)


# ============================================================ App 级接线
class TestAppChatWiring(unittest.TestCase):
    def test_app_ask_without_key_is_a_no_op(self):
        with headless_app() as app:
            eid = app.db.add_entry(batch_id=app.db.create_batch("b"), term="alpha")
            self.assertIsNone(app.ask_entry_question(eid, "为什么？"))
            self.assertEqual(app.db.count_chat_turns(eid), 0)
            self.assertFalse(app.chat_inflight(eid))

    def test_app_ask_with_fake_client_persists_and_notifies_panel(self):
        with headless_app() as app:
            configure_key(app.config)
            client = FakeChatClient("App 级回答")
            app.chat_service.make_client = lambda **kw: client
            eid = app.db.add_entry(batch_id=app.db.create_batch("b"), term="alpha")
            token = app.ask_entry_question(eid, "问题")
            self.assertIsNotNone(token)
            self.assertTrue(wait_until(lambda: app.db.count_chat_turns(eid) == 2))
            self.assertTrue(wait_until(
                lambda: any("App 级回答" in str(r["content"])
                            for r in app.db.list_chat_turns(eid))))
            # UI 队列里的结果由 _pump 消费（面板侧归属校验在 test_reading_panel 覆盖）
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and app._ui_q.qsize():
                app._pump()
            self.assertFalse(app.chat_inflight(eid))

    def test_retry_requires_existing_entry(self):
        with headless_app() as app:
            configure_key(app.config)
            app.chat_service.make_client = lambda **kw: FakeChatClient()
            self.assertIsNone(app.retry_entry_question(999999))
            self.assertIn("词条不存在", app.main.status_label.cget("text"))


if __name__ == "__main__":
    unittest.main()
