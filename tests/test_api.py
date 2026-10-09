"""T7/T8 假 API 服务的成功与失败路径。"""
from __future__ import annotations

import json
import threading
import unittest

from tests.support import temp_db
from tools.fake_api_server import FakeApiServer

from app.api_client import (ApiError, DeepSeekClient, MAP_LOGIC_CHAIN_SYSTEM_PROMPT,
                            MAP_VERIFY_SYSTEM_PROMPT, build_logic_chain_material,
                            build_map_logic_chain_payload, build_map_payload,
                            build_map_rescue_payload, build_map_verify_payload,
                            build_payload, logic_chain_is_empty,
                            parse_explanation, parse_map_logic_chain)
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


class TestMapLogicChain(unittest.TestCase):
    """批次 M24 第零轮：读懂文章的逻辑链（纯函数 + 请求体，零网络）。"""

    ARTICLE = ("开展项目开发需要一套持续迭代的工作流程。首先要确定项目需求；"
               "项目任务拆解必须依赖已经确认好的项目需求。完成任务开发之后，开展项目复盘。"
               "复盘得出的结论可以反向调整项目需求，重新拆解任务，形成循环迭代。")

    def test_the_prompt_answers_the_logic_chain_not_a_relation_list(self):
        body = build_map_logic_chain_payload("m", self.ARTICLE, topic="项目开发",
                                             terms=("项目开发", "任务拆解"))
        system = body["messages"][0]["content"]
        user = body["messages"][1]["content"]
        self.assertIn("逻辑链", system)
        # 方向必须按逻辑写：M24 之前模型照抄主谓顺序，把「拆解依赖需求」写成
        # 拆解 -> 需求，核对判 contradicted，主线当场断一节。
        self.assertIn("不许照抄句子里的主谓顺序", system)
        self.assertIn("一个都不许漏", system, "概念词必须全量列出，不许挑几个")
        self.assertIn("synthesis", system, "文末收口句必须有专门的字段")
        self.assertIn("项目开发", user)
        self.assertIn("任务拆解", user, "划出来的词要单独告诉它")
        self.assertIn("首先要确定项目需求", user, "第零轮必须看见整篇文章正文")
        self.assertEqual(body["stream"], False)
        self.assertEqual(body["response_format"], {"type": "json_object"})

    def test_the_article_is_sent_whole_and_cut_honestly(self):
        """正文过长时走既有的截断口径，并且**如实标注**省略了。"""
        body = build_map_logic_chain_payload("m", self.ARTICLE * 60, topic="项目开发",
                                             text_limit=200)
        user = body["messages"][1]["content"]
        self.assertIn("正文过长", user, "截断必须如实写出来")
        self.assertLess(len(user), 1200)

    def test_parse_keeps_the_shape_and_drops_junk(self):
        chain = parse_map_logic_chain(json.dumps({
            "logic_type": "时序",
            "core": "项目开发的循环流程",
            "concepts": ["项目开发", {"name": "确定项目需求", "role": "起点"},
                         {"role": "没有名字"}, ""],
            "hierarchy": [{"whole": "项目开发体系",
                           "parts": ["确定项目需求", "任务拆解", ""]}],
            "temporal": [{"first": "确定项目需求", "then": "任务拆解",
                          "basis": "首先要确定项目需求"}],
            "causal": [{"cause": "项目复盘", "effect": "循环迭代"}],
            "dependency": [{"dependent": "任务拆解", "prerequisite": "确定项目需求",
                            "basis": "必须依赖"}],
            "contrast": [{"a": "只写", "b": ""}],
            "turning_points": ["复盘结论反向调整需求"],
            "synthesis": {"whole": "项目开发体系",
                          "parts": ["确定项目需求", "任务拆解", "项目复盘"]},
        }))
        self.assertEqual(chain["logic_type"], "时序")
        self.assertEqual([item["name"] for item in chain["concepts"]],
                         ["项目开发", "确定项目需求"], "没名字的概念词要丢掉")
        self.assertEqual(chain["hierarchy"][0]["parts"], ["确定项目需求", "任务拆解"])
        self.assertEqual(chain["dependency"],
                         [{"dependent": "任务拆解", "prerequisite": "确定项目需求",
                           "basis": "必须依赖"}])
        self.assertEqual(chain["contrast"], [], "缺一半的对照不算一对")
        self.assertEqual(chain["synthesis"]["whole"], "项目开发体系")
        self.assertFalse(logic_chain_is_empty(chain))

    def test_parse_never_raises_on_garbage(self):
        for bad in ("", "   ", "not json", "[]", "null", '"x"', "{}"):
            chain = parse_map_logic_chain(bad)
            self.assertTrue(logic_chain_is_empty(chain),
                            f"「{bad}」必须解析成空骨架而不是抛异常")
        self.assertEqual(parse_map_logic_chain('{"logic_type": "随便编的"}')["logic_type"],
                         "", "不在六种里的主逻辑一律置空")

    def test_the_material_spells_the_dependency_direction_out(self):
        """★★ 这一条是本批的核心：骨架喂给第一轮时，依赖必须写成「A → B」。"""
        chain = parse_map_logic_chain(json.dumps({
            "logic_type": "时序",
            "concepts": [{"name": "确定项目需求"}, {"name": "任务拆解"}],
            "dependency": [{"dependent": "任务拆解", "prerequisite": "确定项目需求",
                            "basis": "必须依赖"}],
            "synthesis": {"whole": "项目开发体系",
                          "parts": ["确定项目需求", "任务拆解"]},
        }))
        text = build_logic_chain_material(chain)
        self.assertIn("方向是 确定项目需求 → 任务拆解", text,
                      "依赖必须当场转成箭头方向，模型才不会再写反")
        self.assertIn("一个都没漏", text)
        self.assertIn("共同构成 项目开发体系", text, "收口句要提醒它画属于关系")
        self.assertNotIn("任务拆解 → 确定项目需求", text)
        self.assertEqual(build_logic_chain_material({}), "", "空骨架不许编出内容")
        self.assertEqual(build_logic_chain_material(None), "")

    def test_the_chain_rides_along_with_the_first_round(self):
        entries = [{"term": "卷积", "context": "卷积核在输入上滑动"}]
        plain = build_map_payload("m", entries, topic="卷积网络")
        chain = parse_map_logic_chain(json.dumps({
            "logic_type": "层级",
            "hierarchy": [{"whole": "卷积网络", "parts": ["卷积"]}]}))
        with_chain = build_map_payload("m", entries, topic="卷积网络", logic_chain=chain)
        self.assertEqual(plain["messages"][0]["content"],
                         with_chain["messages"][0]["content"],
                         "系统提示词一个字都不许变")
        self.assertNotIn("逻辑链", plain["messages"][1]["content"])
        self.assertIn("逻辑链", with_chain["messages"][1]["content"],
                      "骨架必须进第一轮的用户消息")
        self.assertIn("卷积网络 是整体", with_chain["messages"][1]["content"])
        self.assertIn("卷积核在输入上滑动", with_chain["messages"][1]["content"],
                      "词条材料仍然要在，模型还得自己找逐字证据")

    def test_an_empty_chain_leaves_the_first_round_byte_identical(self):
        """★ 兼容性守卫：拿不到骨架时，请求体必须与批次 M23 逐字一致。"""
        entries = [{"term": "卷积", "context": "卷积核在输入上滑动",
                    "explanation": "加权求和"}]
        plain = build_map_payload("m", entries, topic="卷积网络")
        for empty in ({}, parse_map_logic_chain(""), None):
            same = build_map_payload("m", entries, topic="卷积网络", logic_chain=empty)
            self.assertEqual(same["messages"][1]["content"],
                             plain["messages"][1]["content"],
                             f"空骨架（{empty!r}）不许改动第一轮的任何字节")

    def test_the_rescue_round_sees_the_same_skeleton(self):
        """补问与第一轮共用同一份骨架 —— 否则补问又会问出方向相反的边。"""
        entries = [{"term": "任务拆解", "context": "必须依赖已经确认好的项目需求"}]
        chain = parse_map_logic_chain(json.dumps({
            "dependency": [{"dependent": "任务拆解", "prerequisite": "确定项目需求"}]}))
        plain = build_map_rescue_payload("m", entries, topic="项目开发")
        with_chain = build_map_rescue_payload("m", entries, topic="项目开发",
                                              logic_chain=chain)
        self.assertNotIn("逻辑链", plain["messages"][1]["content"])
        self.assertIn("方向是 确定项目需求 → 任务拆解",
                      with_chain["messages"][1]["content"])

    def test_the_client_posts_the_chain_round_and_parses_it(self):
        """第零轮走的是**同一个** POST 路径（``_post`` → ``_extract_content``）。

        注意口径：``map_logic_chain`` 自己**只**解析形状，认不出就是空骨架；
        连返回体都不是合法 JSON 时仍按别的轮次一样抛 ``ApiError`` —— 由
        ``MapService.map_logic_chain`` 兜住（那一层才保证「第零轮失败不打断生成」）。
        """
        client = DeepSeekClient("https://api.example.com/v1", "m", FAKE_KEY)
        posted: list = []

        def fake_post(payload):
            posted.append(payload)
            inner = json.dumps({"logic_type": "时序",
                                "temporal": [{"first": "确定项目需求",
                                              "then": "任务拆解"}]})
            return json.dumps(completion(inner))

        with unittest.mock.patch.object(client, "_post", side_effect=fake_post):
            chain = client.map_logic_chain(self.ARTICLE, topic="项目开发",
                                           terms=("任务拆解",))
        self.assertEqual(chain["temporal"],
                         [{"first": "确定项目需求", "then": "任务拆解", "basis": ""}])
        self.assertEqual(posted[0]["response_format"], {"type": "json_object"})
        self.assertEqual(posted[0]["messages"][0]["content"],
                         MAP_LOGIC_CHAIN_SYSTEM_PROMPT)
        self.assertIn("文章正文", posted[0]["messages"][1]["content"],
                      "发出去的是正文本身，不是词条碎片")

        # 模型把话说成散文（不是 JSON 对象）：同一层也不许抛，退成空骨架
        with unittest.mock.patch.object(
                client, "_post",
                side_effect=lambda payload: json.dumps(
                    completion("我认为这篇文章讲的是项目开发"))):
            self.assertTrue(logic_chain_is_empty(
                client.map_logic_chain(self.ARTICLE, topic="项目开发")))


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
