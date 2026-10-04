"""T7/T8 假 API 服务的成功与失败路径。"""
from __future__ import annotations

import json
import threading
import unittest

from tests.support import temp_db
from tools.fake_api_server import FakeApiServer

from app.api_client import (ApiError, DeepSeekClient, MAP_VERIFY_SYSTEM_PROMPT,
                            build_map_payload, build_map_verify_payload,
                            build_payload, parse_explanation)
from app.config import Config
from app.explain_service import ExplainService

FAKE_KEY = "sk-fake-0123456789abcdef"


def completion(content: str, model: str = "fake-model") -> dict:
    return {
        "id": "chatcmpl-x",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
    }


class ApiTestBase(unittest.TestCase):
    def setUp(self):
        self.server = FakeApiServer()
        self.server.start()
        self.addCleanup(self.server.stop)

    def client(self, timeout: float = 8.0) -> DeepSeekClient:
        return DeepSeekClient(self.server.base_url, "fake-model", FAKE_KEY, timeout=timeout)


class TestSuccessPath(ApiTestBase):
    def test_success_returns_structured_result(self):
        """T7：假服务返回结构化 JSON，客户端解析正确。"""
        client = self.client()
        result = client.explain("卷积", "深度学习中的卷积运算")
        self.assertEqual(result.one_line, "这是一个假的解释。")
        self.assertIn("假服务", result.detail)
        self.assertEqual(result.examples, ["示例一", "示例二"])
        self.assertFalse(result.from_cache)

        self.assertEqual(len(self.server.requests), 1)
        req = self.server.requests[0]
        self.assertEqual(req["path"], "/v1/chat/completions")
        self.assertEqual(req["headers"].get("authorization"), f"Bearer {FAKE_KEY}")

    def test_request_body_contains_only_term_and_context(self):
        """隐私检查：请求只带术语 + 上下文，不带标题/路径等其它信息。"""
        client = self.client()
        client.explain("term-x", "context-y")
        payload = self.server.requests[0]["json"]
        self.assertEqual(
            set(payload.keys()), {"model", "messages", "temperature", "stream", "response_format"}
        )
        user_msg = payload["messages"][1]["content"]
        self.assertIn("term-x", user_msg)
        self.assertIn("context-y", user_msg)
        self.assertEqual(len(payload["messages"]), 2)

    def test_code_fence_is_stripped(self):
        body = json.dumps({"one_line": "带围栏", "detail": "", "examples": []}, ensure_ascii=False)
        self.server.responder = lambda r: (200, completion(f"```json\n{body}\n```"), 0)
        result = self.client().explain("t", "c")
        self.assertEqual(result.one_line, "带围栏")

    def test_async_explain_updates_entry(self):
        with temp_db() as db:
            cfg = Config(db)
            cfg.set("api.base_url", self.server.base_url)
            cfg.set("api.model", "fake-model")
            cfg.set_api_key(FAKE_KEY)
            svc = ExplainService(db, cfg)
            bid = db.create_batch("b")
            eid = db.add_entry(batch_id=bid, term="卷积", context="语境")

            done = threading.Event()
            captured = {}

            def on_result(entry_id, status, result, error):
                captured.update(entry_id=entry_id, status=status, result=result, error=error)
                done.set()

            svc._on_result = on_result
            self.assertTrue(svc.explain_entry_async(eid))
            self.assertTrue(done.wait(15), "解释线程未在超时内完成")
            self.assertEqual(captured["status"], "ok", captured.get("error"))
            row = db.get_entry(eid)
            self.assertEqual(row["explain_status"], "ok")
            self.assertEqual(row["one_line"], "这是一个假的解释。")
            self.assertEqual(json.loads(row["examples"]), ["示例一", "示例二"])
            self.assertTrue(row["model_config"].endswith("fake-model"))

    def test_second_run_hits_cache_and_skips_network(self):
        with temp_db() as db:
            cfg = Config(db)
            cfg.set("api.base_url", self.server.base_url)
            cfg.set_api_key(FAKE_KEY)
            svc = ExplainService(db, cfg)
            first = svc.explain_sync("t", "c")
            self.assertFalse(first.from_cache)
            n = len(self.server.requests)
            second = svc.explain_sync("t", "c")
            self.assertTrue(second.from_cache)
            self.assertEqual(len(self.server.requests), n, "命中缓存不应再发请求")
            third = svc.explain_sync("t", "c2")
            self.assertFalse(third.from_cache, "不同语境应重新请求")
            self.assertEqual(len(self.server.requests), n + 1)


class TestFailurePath(ApiTestBase):
    def test_401_auth_error(self):
        self.server.responder = lambda r: (401, {"error": {"message": "Invalid API key"}}, 0)
        with self.assertRaises(ApiError) as ctx:
            self.client().explain("t", "c")
        err = ctx.exception
        self.assertEqual(err.kind, "auth")
        self.assertEqual(err.label, "鉴权失败")
        self.assertIn("API Key", err.display())

    def test_429_rate_limit(self):
        self.server.responder = lambda r: (429, {"error": {"message": "rate limited"}}, 0)
        with self.assertRaises(ApiError) as ctx:
            self.client().explain("t", "c")
        self.assertEqual(ctx.exception.kind, "rate_limit")

    def test_500_server_error(self):
        self.server.responder = lambda r: (500, {"error": {"message": "boom"}}, 0)
        with self.assertRaises(ApiError) as ctx:
            self.client().explain("t", "c")
        self.assertEqual(ctx.exception.kind, "server")

    def test_400_bad_request(self):
        self.server.responder = lambda r: (400, {"error": {"message": "bad model"}}, 0)
        with self.assertRaises(ApiError) as ctx:
            self.client().explain("t", "c")
        self.assertEqual(ctx.exception.kind, "bad_request")

    def test_timeout(self):
        self.server.responder = lambda r: (200, completion("{}"), 4.0)
        with self.assertRaises(ApiError) as ctx:
            self.client(timeout=1.0).explain("t", "c")
        self.assertEqual(ctx.exception.kind, "timeout")
        self.assertEqual(ctx.exception.label, "请求超时")

    def test_network_unreachable(self):
        client = DeepSeekClient("http://127.0.0.1:1/v1", "m", FAKE_KEY, timeout=2.0)
        with self.assertRaises(ApiError) as ctx:
            client.explain("t", "c")
        self.assertIn(ctx.exception.kind, ("network", "timeout"))

    def test_non_json_output_is_bad_response(self):
        self.server.responder = lambda r: (200, completion("这不是 JSON"), 0)
        with self.assertRaises(ApiError) as ctx:
            self.client().explain("t", "c")
        self.assertEqual(ctx.exception.kind, "bad_response")
        self.assertTrue(ctx.exception.preview, "非法响应必须带原始片段以便排查")

    def test_missing_choices_is_bad_response(self):
        self.server.responder = lambda r: (200, {"id": "x", "object": "chat.completion"}, 0)
        with self.assertRaises(ApiError) as ctx:
            self.client().explain("t", "c")
        self.assertEqual(ctx.exception.kind, "bad_response")

    def test_missing_one_line_is_bad_response(self):
        self.server.responder = lambda r: (
            200, completion(json.dumps({"detail": "only detail"})), 0)
        with self.assertRaises(ApiError) as ctx:
            self.client().explain("t", "c")
        self.assertEqual(ctx.exception.kind, "bad_response")
        self.assertIn("one_line", str(ctx.exception))

    def test_examples_wrong_type_is_bad_response(self):
        self.server.responder = lambda r: (
            200, completion(json.dumps({"one_line": "x", "detail": "", "examples": "not-a-list"})), 0)
        with self.assertRaises(ApiError) as ctx:
            self.client().explain("t", "c")
        self.assertEqual(ctx.exception.kind, "bad_response")

    def test_error_message_never_leaks_key(self):
        """鉴权失败信息里绝不能出现 key 明文。"""
        self.server.responder = lambda r: (
            401, {"error": {"message": f"Invalid key {FAKE_KEY}"}}, 0)
        with self.assertRaises(ApiError) as ctx:
            self.client().explain("t", "c")
        text = ctx.exception.display() + str(ctx.exception.preview)
        self.assertNotIn(FAKE_KEY, text)
        self.assertIn("REDACTED", text)

    def test_retry_after_error_succeeds(self):
        """失败后必须可以重试。"""
        self.server.responder = lambda r: (500, {"error": {"message": "boom"}}, 0)
        with self.assertRaises(ApiError):
            self.client().explain("t", "c")
        self.server.responder = FakeApiServer.default_responder
        result = self.client().explain("t", "c")
        self.assertTrue(result.one_line)


class TestPureHelpers(unittest.TestCase):
    def test_parse_rejects_empty(self):
        for bad in ("", "   ", "[]", "null", '{"detail":"x"}'):
            with self.assertRaises(ApiError):
                parse_explanation(bad)

    def test_build_payload_uses_no_context_marker(self):
        payload = build_payload("m", "term", "")
        self.assertIn("（无可用上下文）", payload["messages"][1]["content"])
        self.assertNotIn("Authorization", json.dumps(payload))

    def test_endpoint_join(self):
        self.assertEqual(DeepSeekClient("https://api.deepseek.com/v1", "m", "k").base_url,
                         "https://api.deepseek.com/v1")
        with self.assertRaises(ApiError):
            DeepSeekClient("", "m", "k").explain("t", "c")
        with self.assertRaises(ApiError):
            DeepSeekClient("ftp://x", "m", "k").explain("t", "c")

    def test_map_prompt_only_forbids_hierarchy_contradictions(self):
        """第 5 条必须与服务一致：只禁包含 / 属于的层级矛盾；依赖 / 因果的双向
        反馈环只在**两个方向各自都有材料**时允许 —— 纯请求体，零网络。"""
        body = build_map_payload("m", [{"term": "卷积", "context": "卷积核在输入上滑动"}],
                                 topic="卷积网络")
        system = body["messages"][0]["content"]
        self.assertIn("只有包含 / 属于的层级矛盾", system, "只禁层级矛盾")
        self.assertNotIn("除「对照」外不要同时输出", system, "旧的「双向一概矛盾」必须删掉")
        self.assertIn("依赖 / 因果的双向", system, "依赖 / 因果反馈环不得再被当矛盾")
        self.assertIn("各自都有材料明确支持", system, "双向仅在两方向各自有材料时允许")
        self.assertIn("同一条关系只输出一次", system, "去重规则仍在")
        self.assertEqual(body["stream"], False)
        self.assertEqual(body["response_format"], {"type": "json_object"})

    def test_map_second_round_verification_is_kept(self):
        """第二次**独立核对**仍在：导图请求体与核对请求体各自带自己的系统提示词。"""
        body = build_map_verify_payload("m", [{
            "index": 0, "type": "依赖",
            "direction": "卷积运算 → 卷积核；前件依赖后件，后件是前件的前提 / 基础",
            "source": {"term": "卷积运算", "context": "卷积运算依赖卷积核",
                       "explanation": "先有核才能逐点相乘"},
            "target": {"term": "卷积核", "context": "卷积运算依赖卷积核",
                       "explanation": "卷积核是运算单元"},
            "reason": "卷积运算依赖卷积核", "evidence": "卷积运算依赖卷积核",
        }], topic="卷积网络")
        self.assertEqual(body["messages"][0]["content"], MAP_VERIFY_SYSTEM_PROMPT)
        self.assertIn("独立核对", body["messages"][0]["content"])
        self.assertIn("supported", body["messages"][0]["content"])
        self.assertIn("依赖", body["messages"][1]["content"], "核对材料必须带具体方向")
        self.assertEqual(body["stream"], False)


class TestReasoningEffortIsOptIn(unittest.TestCase):
    """推理强度是**可选字段**：不选就一个字节都不发，选了才进请求体。

    这直接决定兼容性：不认识这个字段的第三方网关，在「不发送」时必须
    收到与改动前**逐字一致**的请求体。
    """

    def test_every_builder_omits_the_field_by_default(self):
        explain = build_payload("m", "卷积", "上下文")
        mapping = build_map_payload("m", [{"term": "卷积", "context": "上下文"}])
        verify = build_map_verify_payload("m", [], topic="卷积网络")
        for name, body in (("解释", explain), ("导图", mapping), ("核对", verify)):
            self.assertNotIn("reasoning_effort", body, f"{name}请求默认不得带这个字段")

    def test_every_builder_injects_it_when_asked(self):
        explain = build_payload("m", "卷积", "上下文", reasoning_effort="high")
        mapping = build_map_payload("m", [{"term": "卷积", "context": "上下文"}],
                                    reasoning_effort="medium")
        verify = build_map_verify_payload("m", [], topic="卷积网络",
                                          reasoning_effort="low")
        self.assertEqual(explain["reasoning_effort"], "high")
        self.assertEqual(mapping["reasoning_effort"], "medium")
        self.assertEqual(verify["reasoning_effort"], "low")
        # 原有字段一个不动
        self.assertEqual(mapping["response_format"], {"type": "json_object"})
        self.assertEqual(explain["stream"], False)

    def test_blank_or_space_only_effort_is_not_sent(self):
        for value in ("", "   ", None):
            body = build_payload("m", "卷积", "上下文", reasoning_effort=value)
            self.assertNotIn("reasoning_effort", body, f"{value!r} 必须按「不发送」处理")

    def test_the_client_sends_the_effort_it_was_configured_with(self):
        """客户端把它配置里的强度带给**每一个**调用（解释 / 导图 / 核对）。"""
        client = DeepSeekClient("https://api.example.com/v1", "m", "k",
                                reasoning_effort=" High ")
        self.assertEqual(client.reasoning_effort, "high", "统一小写")
        sent = []
        client._post = lambda payload: sent.append(payload) or json.dumps(
            completion(json.dumps({"one_line": "x", "detail": "y", "examples": []})))
        client.explain("term", "ctx")
        self.assertEqual(sent[-1]["reasoning_effort"], "high")
        self.assertEqual(DeepSeekClient("https://api.example.com/v1", "m", "k")
                         .reasoning_effort, "")


class TestPingConnection(ApiTestBase):
    """设置页「测试连接」用的极轻量请求（用户明确要求的配置检查增强）。

    要点：**只带最基本字段**（model + 一句 ping + max_tokens=8），不带
    temperature / reasoning_effort / thinking —— 测的是「地址 + Key + 网络」，
    不是模型能力；字段越少越兼容。
    """

    def test_ping_body_is_minimal(self):
        client = self.client()
        sent = []
        client._post = lambda payload: sent.append(payload) or json.dumps(
            completion("pong", model="fake-model"))
        message = client.ping()
        self.assertEqual(len(sent), 1)
        body = sent[0]
        self.assertEqual(set(body), {"model", "messages", "max_tokens", "stream"})
        self.assertEqual(body["max_tokens"], 8)
        self.assertEqual(body["stream"], False)
        self.assertEqual(body["model"], "fake-model")
        self.assertEqual(body["messages"], [{"role": "user", "content": "ping"}])
        self.assertIn("连接成功", message)
        self.assertIn("fake-model", message)

    def test_ping_does_not_send_effort_thinking_or_temperature(self):
        client = DeepSeekClient("https://api.example.com/v1", "m", "k",
                                reasoning_effort="max", thinking="disabled")
        sent = []
        client._post = lambda payload: sent.append(payload) or json.dumps(
            completion("pong", model="m"))
        client.ping()
        for field in ("temperature", "reasoning_effort", "thinking", "response_format"):
            self.assertNotIn(field, sent[0], f"{field} 不该出现在测试连接里")

    def test_ping_reports_the_served_model_when_it_differs(self):
        client = self.client()
        client._post = lambda payload: json.dumps(completion("pong", model="served-model"))
        self.assertIn("服务端模型 served-model", client.ping())

    def test_ping_endpoint_reports_success_and_failure(self):
        """``ping_endpoint`` 永远返回 ``(bool, 一句话)``，绝不往下抛异常。"""
        from app.api_client import ping_endpoint

        ok, message = ping_endpoint(base_url=self.server.base_url, model="fake-model",
                                    api_key=FAKE_KEY, timeout=8.0)
        self.assertTrue(ok, message)
        self.assertIn("连接成功", message)

        self.server.responder = lambda r: (401, {"error": {"message": "Invalid API key"}}, 0)
        ok, message = ping_endpoint(base_url=self.server.base_url, model="fake-model",
                                    api_key=FAKE_KEY, timeout=8.0)
        self.assertFalse(ok)
        self.assertIn("连接失败", message)
        self.assertIn("Key", message)

    def test_ping_endpoint_never_raises_on_a_dead_host(self):
        from app.api_client import ping_endpoint

        ok, message = ping_endpoint(base_url="http://127.0.0.1:1/v1", model="m",
                                    api_key="k", timeout=3.0)
        self.assertFalse(ok)
        self.assertIn("连接失败", message)

    def test_ping_endpoint_refuses_a_blank_key(self):
        from app.api_client import ping_endpoint

        ok, message = ping_endpoint(base_url="http://127.0.0.1:1/v1", model="m",
                                    api_key="", timeout=3.0)
        self.assertFalse(ok)
        self.assertIn("API Key", message)


if __name__ == "__main__":
    unittest.main()
