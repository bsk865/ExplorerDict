"""DeepSeek 兼容 chat/completions 客户端。

隐私与安全红线（SPEC.md FR-9 / FR-10）：
* 只发送「术语 + 短上下文」，不发送窗口标题、路径、整篇文档或其它词条；
* key 从 DPAPI 密文解出，只放进请求头，绝不进日志、绝不进异常消息（统一走 redact）；
* 任何错误都归类成可读、可重试的类型。
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request

from .logging_setup import get_logger, redact
from .models import ExplainResult

log = get_logger("api")

KIND_LABELS = {
    "config": "配置缺失",
    "network": "网络不可达",
    "timeout": "请求超时",
    "auth": "鉴权失败",
    "rate_limit": "请求过于频繁（限流）",
    "bad_request": "请求被拒绝",
    "server": "服务端错误",
    "bad_response": "响应非法",
    "unknown": "未知错误",
}

SYSTEM_PROMPT = """你是一个严谨的阅读助手。用户在阅读时划选了一个术语，请给出简洁、准确、可核查的解释。

严格只输出一个 JSON 对象，不要输出任何其它文字，不要使用 Markdown 代码围栏。JSON 结构：
{"one_line": "一句话解释，不超过 60 字", "detail": "详细解释，可含背景、易混点、必要前提", "examples": ["例句或用法示例"], "topic": "可选：这组阅读内容所属主题的简短名字，6 字以内，无法判断时给空串"}

要求：
1. 只依据术语本身与给定上下文作答。若上下文不足以确定含义，必须在 detail 中写明「仅凭该语境无法确定」，并把最可能的解释标注为「推测」。
2. examples 最多 3 条，可以为空数组。
3. 不要编造原文中不存在的事实、出处、页码或引文。
4. 不要输出与解释无关的联想关系；若确有必要提及关联概念，必须写在 detail 中并标注「以下为推测性关联」，不得表述为原文事实。
5. 使用与上下文一致的语言作答（上下文为中文时用中文）。
6. topic 只用于给「这一次阅读的内容」分组命名：必须来自术语与上下文本身，不要复制窗口标题、网址或整段原文；判断不了就给空串。"""

USER_TEMPLATE = """术语：{term}

上下文（用户正在阅读的原文片段，仅用于消歧，可能被截断）：
{context}"""

NO_CONTEXT = "（无可用上下文）"

# ---------------------------------------------------------------- 追问（对话）
#: 追问系统提示：明确「只依据本词与已给材料」，不自作主张、不编造。
CHAT_SYSTEM_PROMPT = """你是《探索词典》里的阅读助手，正在和用户讨论**一个已经保存的词条**。

规则：
1. 只依据用户给出的「词语 / 上下文 / 已有解释 / 最近几轮对话」回答，不要引入其它词条或猜测用户的其它资料。
2. 信息不足时明确说「仅凭现有信息无法确定」，不要编造出处、页码、数据或引文。
3. 直接、简洁地回答本次问题；确有必要时用短段落或短列表，不要复述整篇已有解释。
4. 用与用户提问一致的语言作答。
5. 不要输出 JSON、不要输出代码围栏，除非用户明确要求代码。"""

CHAT_CONTEXT_TEMPLATE = """【词语】{term}

【阅读上下文（可能被截断）】
{context}

【已有解释（可能为空）】
{explanation}"""


def build_chat_messages(*, term: str, question: str, context: str = "",
                        explanation: str = "", history=None,
                        max_chars: int = 4000) -> list[dict]:
    """构造追问请求的消息列表（**隐私边界就在这里**）。

    只包含：当前词语 + 短上下文 + 已有解释 + 该词**最近有限轮次**的历史 +
    本次问题。绝不包含窗口标题、文件路径、其它词条或整篇文档。

    :param history: ``[{"role": "user"|"assistant", "content": str}, ...]``
                    （调用方已按轮次裁剪；这里再做一次总字符上限裁剪）
    """
    messages: list[dict] = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}]
    head = CHAT_CONTEXT_TEMPLATE.format(
        term=(term or "").strip(),
        context=((context or "").strip() or NO_CONTEXT),
        explanation=((explanation or "").strip() or "（暂无）"),
    )
    messages.append({"role": "user", "content": head})

    kept: list[dict] = []
    budget = max(0, int(max_chars))
    for item in reversed(list(history or [])):
        item = item or {}
        role = str(item.get("role") or "")
        content = str(item.get("content") or "")
        # 失败 / 丢弃的轮次绝不发给模型（它们只是 UI 上的提示文字）
        if str(item.get("status") or "ok") not in ("ok", ""):
            continue
        if role not in ("user", "assistant") or not content.strip():
            continue
        if len(content) > budget:
            content = content[:budget]
        budget -= len(content)
        kept.append({"role": role, "content": content})
        if budget <= 0:
            break
    messages.extend(reversed(kept))
    messages.append({"role": "user", "content": (question or "").strip()})
    return messages


def parse_chat_reply(content: str) -> str:
    """追问回答是**纯文本**（不是 JSON）：只去掉可能的代码围栏与首尾空白。"""
    if not isinstance(content, str) or not content.strip():
        raise ApiError("bad_response", "模型返回内容为空")
    return _strip_fence(content).strip()


# ------------------------------------------------------- 参考关系图（候选边）
#: 允许的关系类型（唯一权威定义在 :mod:`app.map_service`，这里只用于提示词）
MAP_REL_TYPES = ("包含", "属于", "依赖", "用途", "因果", "对照")
#: 一次请求最多带多少个词条（受限输入：只带当前主题的词条，绝不带整篇文档）
MAP_MAX_ENTRIES = 30
#: 每个词条的上下文 / 释义截断长度（受限输入）
MAP_MATERIAL_CHARS = 240

#: 导图提示词：**只允许**基于给定材料提出候选关系，且必须带原文证据片段。
#: 依据不足时必须返回空数组 —— 「不强行连线、不默认相关」是硬约束。
MAP_SYSTEM_PROMPT = """你在为《探索词典》的一张「AI 参考关系图」提出**有材料依据的**候选关系。

用户会给出一个主题下已经保存的词条，每条包含：编号、词语、阅读上下文（可能被截断）、已有释义（可能为空）。
严格只输出一个 JSON 对象，不要输出任何其它文字，不要使用 Markdown 代码围栏。结构：
{"relations": [{"src": 0, "dst": 1, "type": "包含", "reason": "一句话依据", "evidence": "材料里支持这条关系的片段"}]}

规则：
1. src / dst 只能是给定词条的**编号**（整数），不能是词语文字，不能超出给定范围，也不能指向同一条；
2. type 只能是这六个之一，方向含义固定：
   - 包含 A→B：A 是 B 的上位概念或整体（B 包含在 A 里）；
   - 属于 A→B：A 是 B 的下位概念或组成部分（A 属于 B）；
   - 依赖 A→B：A 依赖 B，B 是 A 的前提 / 基础；
   - 用途 A→B：A 用于 B；
   - 因果 A→B：A 导致 / 造成 B；
   - 对照 A→B：A 与 B 形成对照 / 对比。
   自检：写一条关系前，先在材料里找出一句能**直接读出**这个类型的话 —— 光有两个词
   同时出现在一句里不算。下面这些**都不是**这六种关系，材料只写了这些就不要输出：
   - 「A 在 B 中 / A 部署在 B 中 / A 出现在 B 里」＝ 场景描述，既不是依赖也不是因果；
   - 「A 在 B 里的开销高 / A 对 B 而言是个问题」＝ A 的困难发生在 B 里，不是 A 导致 B；
   - 「现有方案依赖 A」＝ 依赖的前件是那个方案；方案本身不在本次词条里时，不要把这条
     依赖挂到别的词上；
   - 两个词只是同属一个话题、总是结伴出现 ＝ 没有类型，不要输出。
   类型拿不准时**宁可不输出**：类型标错的候选会被核对判为不成立，反而让这个词变成孤立词；
3. reason 是一句简短依据（不超过 40 字）；evidence 必须**逐字**来自本次给出的上下文或释义，
   不许改写、不许编造出处 / 页码 / 引文；
4. 材料不足以支持、只能靠常识猜测、或者需要主题以外的知识时：**不要输出这条关系**。
   一条都没有就返回 {"relations": []}；空数组合法 —— 绝不强行连线，也不要用「相关」这类默认关系；
5. 同一条关系只输出一次（「对照」的正反视为同一条）；**只有包含 / 属于的层级矛盾
   禁止**：同一对词不要两个方向都说「包含」（或都说「属于」）—— 上下位互相颠倒 =
   不可能成立的层级。依赖 / 因果的双向（互相依赖、互为因果）**不是**矛盾：只有两个
   方向**各自都有材料明确支持**时才允许同时输出，只有一个方向有材料就只写那一个方向；
6. 只使用本次给出的词条与材料，不要引用其它主题、整篇文档、窗口标题或网址。"""

MAP_USER_TEMPLATE = """主题：{topic}

已保存词条（编号 / 词语 / 上下文 / 已有释义）：
{material}"""

MAP_EMPTY_MATERIAL = "（本次没有可用词条）"


def _with_reasoning(payload: dict, reasoning_effort: str = "") -> dict:
    """按需注入 ``reasoning_effort``（纯函数：**空串就不加这个字段**）。

    空串是默认值，也是唯一能兼容所有网关的选择 —— 不认识的端点收到这个字段
    可能直接 400；识别它的模型（如 deepseek-reasoner 之外的推理模型）才需要。
    """
    effort = str(reasoning_effort or "").strip().lower()
    if effort:
        payload["reasoning_effort"] = effort
    return payload


def _with_thinking(payload: dict, thinking: str = "") -> dict:
    """按需注入思考模式开关 ``{"thinking": {"type": ...}}``（纯函数：空串不加字段）。

    DeepSeek 的思考模式**默认打开**，而思考模式不支持 ``temperature``（官方文档：
    传了不报错也不生效）—— 采样不受约束，同一份材料每次重新生成都会给不同的候选。
    实测（主题 10 个词条，``deepseek-chat``）：``temperature=0.2`` 三次是 6 / 6 / 5 条、
    并集 10 条；``temperature=0.0`` 三次仍是 6 / 5 / 5 条；再叠加
    ``{"thinking": {"type": "disabled"}}`` 就连逐条内容都一致了。

    别的网关不认这个字段（可能直接 400），所以**默认不发**；该不该发由
    ``Config.map_thinking`` 按模型名 / Base URL 判断（``api.map_thinking`` 可强制）。
    """
    mode = str(thinking or "").strip().lower()
    if mode:
        payload["thinking"] = {"type": mode}
    return payload


#: 核对（第二次调用）允许的判定；只有 ``supported`` 才允许这条边继续存在。
MAP_VERDICTS = ("supported", "uncertain", "contradicted")
#: 核对提示词：**只判断给定候选**，明确禁止新增 / 改写候选。
MAP_VERIFY_SYSTEM_PROMPT = """你在**独立核对**另一轮给出的候选关系是否真的被材料支持。

用户会给出若干条候选关系，每条包含：序号、关系类型、方向（前件 → 后件）、两个端点各自的上下文与释义、候选自己写的依据与证据片段。
严格只输出一个 JSON 对象，不要输出任何其它文字，不要使用 Markdown 代码围栏。结构：
{"verdicts": [{"index": 0, "verdict": "supported", "note": "一句话理由"}]}

规则：
1. index 必须是给定候选的序号，同一条候选只出现一次；**只做判断**：不许新增候选、不许改写方向或类型、不许补充材料里没有的事实；
2. verdict 只能是这三个之一：
   - supported：给出的材料明确支持「这个方向 + 这个类型」的关系；
   - uncertain：材料不足、只能靠常识或主题以外的知识才能判断；
   - contradicted：材料与这条关系（方向或类型）相冲突。
     特别注意这两种常见情况，都判 contradicted（不要因为两个词同现就判 supported）：
     ① 材料只是「A 在 B 中 / A 部署在 B 中 / A 在 B 里的开销高」这类**场景描述**时，
        依赖 / 因果 / 用途都不成立；② 材料支持的是**另一个方向**（例如材料说「方案依赖
        漂移检测方法」，候选却写成「<另一个词> 依赖 漂移检测方法」）时，方向错了；
3. 只是重复端点词语本身、或者只有两三个字的片段，**不算依据**；证据必须真的表达出这条关系；
4. 只依据本次给出的材料判断，不要引用其它主题、整篇文档、窗口标题或网址。"""
MAP_VERIFY_USER_TEMPLATE = """主题：{topic}

候选关系（序号 / 类型 / 方向 / 端点材料 / 候选依据 / 候选证据）：
{items}"""


def build_map_verify_material(items, *, material_chars: int = MAP_MATERIAL_CHARS) -> str:
    """把候选关系压成核对用的**受限**材料（纯函数）。

    每条只带：序号、类型、方向、两个端点的词 / 截断上下文 / 截断释义、候选依据与
    证据片段。不带 ``entry_id``、不带其它主题、不带整篇文档。
    """
    cut = max(1, int(material_chars))
    blocks: list[str] = []
    for item in list(items or []):
        item = item or {}
        src = item.get("source") or {}
        dst = item.get("target") or {}
        blocks.append(
            f"[{int(item.get('index', 0))}] 类型：{str(item.get('type') or '')}\n"
            f"  方向：{str(item.get('direction') or '')}\n"
            f"  前件：{str(src.get('term') or '（未命名）')}"
            f"｜上下文：{str(src.get('context') or '（无）')[:cut]}"
            f"｜释义：{str(src.get('explanation') or '（暂无）')[:cut]}\n"
            f"  后件：{str(dst.get('term') or '（未命名）')}"
            f"｜上下文：{str(dst.get('context') or '（无）')[:cut]}"
            f"｜释义：{str(dst.get('explanation') or '（暂无）')[:cut]}\n"
            f"  候选依据：{str(item.get('reason') or '')[:cut]}\n"
            f"  候选证据：{str(item.get('evidence') or '')[:cut]}"
        )
    return "\n".join(blocks) if blocks else MAP_EMPTY_MATERIAL


def build_map_verify_payload(model: str, items, *, topic: str = "",
                             material_chars: int = MAP_MATERIAL_CHARS,
                             temperature: float = 0.0,
                             reasoning_effort: str = "",
                             thinking: str = "") -> dict:
    """构造核对请求体：只含给定候选 + 固定提示词（**不允许新增边**）。"""
    material = build_map_verify_material(items, material_chars=material_chars)
    return _with_thinking(_with_reasoning({
        "model": model,
        "messages": [
            {"role": "system", "content": MAP_VERIFY_SYSTEM_PROMPT},
            {"role": "user", "content": MAP_VERIFY_USER_TEMPLATE.format(
                topic=(topic or "").strip() or "（未命名主题）", items=material)},
        ],
        "temperature": float(temperature),
        "stream": False,
        "response_format": {"type": "json_object"},
    }, reasoning_effort), thinking)


def parse_map_verdicts(content: str) -> list:
    """解析核对响应：**只做顶层结构校验**，逐条判定由 ``map_service`` 处理。

    顶层结构错误（空响应 / 非 JSON / 不是对象 / 没有 ``verdicts`` 数组）一律抛
    :class:`ApiError` —— 调用方据此报错并允许重试，**绝不**把没核对过的边当成
    核对通过的边。
    """
    if not isinstance(content, str) or not content.strip():
        raise ApiError("bad_response", "核对返回内容为空")
    raw = _strip_fence(content)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError("bad_response", f"核对输出不是合法 JSON：{exc.msg}",
                       preview=raw[:300]) from exc
    if not isinstance(obj, dict):
        raise ApiError("bad_response", "核对输出的 JSON 不是对象", preview=raw[:300])
    verdicts = obj.get("verdicts")
    if not isinstance(verdicts, list):
        raise ApiError("bad_response", "核对输出缺少 verdicts 数组", preview=raw[:300])
    return verdicts


#: 让模型挑「布局骨架」（G3）：**只发关系类型与起止词名**，不发材料、不发释义，
#: 也**不让它改关系** —— 它只回一个骨架 id + 一句理由。
MAP_TEMPLATE_SYSTEM_PROMPT = """你在为一张**已经确定好内容**的参考关系图挑一个「摆放骨架」。

用户会给你：主题名、（可选的）适合套用的骨架清单（id / 名字 / 一句话说明 / 适合什么），以及这张图里**全部**关系的「起点 类型 终点」。
你要做的是**只挑一个骨架**，不要增删或改写任何关系。

严格只输出一个 JSON 对象，不要输出任何其它文字，不要使用 Markdown 代码围栏：
{"template": "<骨架 id>", "reason": "一句话理由"}

规则：
1. template 必须是清单里的 id 之一（清单为空时用 auto）；
2. 依据只看关系类型与词名：因果多 → 鱼骨图；包含 / 属于成层级 → 树状图或组织架构图；依赖成链 → 流程线；
   一个中心词发散 → 思维导图；什么都看不出来 → auto（自动）；
3. 宁可回 auto，也不要硬套：词数很少（少于 3）时一律 auto；
4. reason 用**中文**写一句话，直接说「因为……」，不要复述关系清单。"""

MAP_TEMPLATE_USER_TEMPLATE = """主题：{topic}

可选的骨架（id / 名字 / 说明 / 适合）：
{choices}

这张图的关系（起点 类型 终点）：
{pairs}"""


def build_map_template_choices(choices) -> str:
    """骨架清单（纯函数）：``[{"key", "name", "summary", "fit"}, ...]`` → 多行文本。"""
    lines: list[str] = []
    for item in list(choices or []):
        item = item or {}
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        lines.append(f"- {key}　{str(item.get('name') or '').strip()}"
                     f"　说明：{str(item.get('summary') or '').strip() or '（无）'}"
                     f"　适合：{str(item.get('fit') or '').strip() or '（不限）'}")
    return "\n".join(lines) if lines else "（没有可选清单，请回 auto）"


def build_map_template_material(pairs, *, limit: int = 200) -> str:
    """关系摘要（纯函数）：``[{"source", "type", "target"}, ...]`` → 每行 `A 类型 B`。

    **只发词名与类型**：不带 entry_id、不带上下文、不带释义、不带依据与证据 ——
    挑骨架只需要图的形状，材料留在本机。
    """
    cap = max(0, int(limit))
    lines: list[str] = []
    for item in list(pairs or [])[:cap]:
        item = item or {}
        src = str(item.get("source") or "").strip() or "（未命名）"
        dst = str(item.get("target") or "").strip() or "（未命名）"
        kind = str(item.get("type") or "").strip() or "相关"
        lines.append(f"{src} {kind} {dst}")
    return "\n".join(lines) if lines else "（这张图里没有关系）"


def build_map_template_payload(model: str, pairs, *, topic: str = "", choices=(),
                              temperature: float = 0.0, reasoning_effort: str = "",
                              thinking: str = "") -> dict:
    """构造「挑骨架」请求体：清单 + 关系类型与词名，``response_format`` 固定 JSON。"""
    return _with_thinking(_with_reasoning({
        "model": model,
        "messages": [
            {"role": "system", "content": MAP_TEMPLATE_SYSTEM_PROMPT},
            {"role": "user", "content": MAP_TEMPLATE_USER_TEMPLATE.format(
                topic=(topic or "").strip() or "（未命名主题）",
                choices=build_map_template_choices(choices),
                pairs=build_map_template_material(pairs))},
        ],
        "temperature": float(temperature),
        "stream": False,
        "response_format": {"type": "json_object"},
    }, reasoning_effort), thinking)


def parse_map_template(content: str, choices) -> tuple[str, str]:
    """解析「挑骨架」响应 → ``(骨架 id, 理由)``。

    只认清单里的 id（清单为空时只认 ``auto``）；**认不出来就回 ``("", "")``**，
    由调用方回退到本地规则 —— 挑骨架失败绝不能挡住画图。
    """
    allowed = {str((item or {}).get("key") or "").strip() for item in list(choices or [])}
    allowed.discard("")
    if not allowed:
        allowed = {"auto"}
    if not isinstance(content, str) or not content.strip():
        return "", ""
    try:
        obj = json.loads(_strip_fence(content))
    except json.JSONDecodeError:
        return "", ""
    if not isinstance(obj, dict):
        return "", ""
    key = str(obj.get("template") or obj.get("key") or "").strip()
    reason = str(obj.get("reason") or obj.get("note") or "").strip()
    if key not in allowed:
        return "", reason
    return key, reason


def build_map_material(entries, *, material_chars: int = MAP_MATERIAL_CHARS,
                       max_entries: int = MAP_MAX_ENTRIES) -> str:
    """把词条列表压成**受限**的提示词材料（纯函数，隐私边界就在这里）。

    只保留：编号、词语、截断后的上下文、截断后的释义。窗口标题、网址、文件路径、
    其它主题的词条一律不出现；总条数也设上限。
    """
    limit = max(0, int(max_entries))
    cut = max(1, int(material_chars))
    lines: list[str] = []
    for index, item in enumerate(list(entries or [])[:limit]):
        item = item or {}
        term = str(item.get("term") or "").strip()
        context = str(item.get("context") or "").strip()[:cut]
        explanation = str(item.get("explanation") or "").strip()[:cut]
        lines.append(f"[{index}] {term or '（未命名）'}")
        lines.append(f"  上下文：{context or '（无）'}")
        lines.append(f"  释义：{explanation or '（暂无）'}")
    return "\n".join(lines) if lines else MAP_EMPTY_MATERIAL


def build_map_payload(model: str, entries, *, topic: str = "",
                      material_chars: int = MAP_MATERIAL_CHARS,
                      max_entries: int = MAP_MAX_ENTRIES,
                      temperature: float = 0.0,
                      reasoning_effort: str = "",
                      thinking: str = "") -> dict:
    """构造导图请求体：只含当前主题的词条材料 + 固定提示词。

    ``temperature`` 默认 **0.0**（曾经是 0.2）：导图是「同一份材料只该给同一张图」
    的确定性输出，界面上的「重新生成」不该把图洗一遍。采样随机性另有来源（思考
    模式），见 :func:`_with_thinking`。
    """
    material = build_map_material(entries, material_chars=material_chars,
                                  max_entries=max_entries)
    return _with_thinking(_with_reasoning({
        "model": model,
        "messages": [
            {"role": "system", "content": MAP_SYSTEM_PROMPT},
            {"role": "user", "content": MAP_USER_TEMPLATE.format(
                topic=(topic or "").strip() or "（未命名主题）", material=material)},
        ],
        "temperature": float(temperature),
        "stream": False,
        "response_format": {"type": "json_object"},
    }, reasoning_effort), thinking)


def parse_map_relations(content: str) -> list:
    """解析导图响应：**只做结构校验**，逐条关系的字段校验交给 map_service。

    顶层结构错误（空响应 / 非 JSON / 不是对象 / 没有 ``relations`` 数组）一律抛
    :class:`ApiError` —— 调用方据此报错，**绝不**画出一张伪图。
    逐条关系里的坏字段由 :func:`app.map_service.validate_relations` 筛除并计数
    （端点越界 / 自环 / 类型非法 / 无证据 / 重复 / 成环）。
    """
    if not isinstance(content, str) or not content.strip():
        raise ApiError("bad_response", "模型返回内容为空")
    raw = _strip_fence(content)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError("bad_response", f"模型输出不是合法 JSON：{exc.msg}",
                       preview=raw[:300]) from exc
    if not isinstance(obj, dict):
        raise ApiError("bad_response", "模型输出的 JSON 不是对象", preview=raw[:300])
    relations = obj.get("relations")
    if not isinstance(relations, list):
        raise ApiError("bad_response", "缺少 relations 数组", preview=raw[:300])
    return relations


class ApiError(RuntimeError):
    def __init__(self, kind: str, message: str, *, status: int | None = None, preview: str = ""):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.preview = preview

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, KIND_LABELS["unknown"])

    def display(self) -> str:
        extra = f" (HTTP {self.status})" if self.status else ""
        return f"{self.label}{extra}：{self}"


def endpoint_for(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ApiError("config", "Base URL 为空")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def build_payload(model: str, term: str, context: str, *, temperature: float = 0.2,
                  reasoning_effort: str = "") -> dict:
    context_text = (context or "").strip() or NO_CONTEXT
    return _with_reasoning({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_TEMPLATE.format(term=term, context=context_text)},
        ],
        "temperature": temperature,
        "stream": False,
        "response_format": {"type": "json_object"},
    }, reasoning_effort)


def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


def parse_explanation(content: str) -> tuple[str, str, list[str]]:
    """校验并抽取结构化字段。非法时抛 ApiError('bad_response')。"""
    if not isinstance(content, str) or not content.strip():
        raise ApiError("bad_response", "模型返回内容为空")
    raw = _strip_fence(content)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError(
            "bad_response",
            f"模型输出不是合法 JSON：{exc.msg}",
            preview=raw[:300],
        ) from exc
    if not isinstance(obj, dict):
        raise ApiError("bad_response", "模型输出的 JSON 不是对象", preview=raw[:300])

    one_line = obj.get("one_line")
    if not isinstance(one_line, str) or not one_line.strip():
        raise ApiError("bad_response", "缺少非空字符串字段 one_line", preview=raw[:300])
    detail = obj.get("detail", "")
    if detail is None:
        detail = ""
    if not isinstance(detail, str):
        raise ApiError("bad_response", "字段 detail 不是字符串", preview=raw[:300])
    examples = obj.get("examples", [])
    if examples is None:
        examples = []
    if not isinstance(examples, list) or any(not isinstance(x, str) for x in examples):
        raise ApiError("bad_response", "字段 examples 不是字符串数组", preview=raw[:300])
    return one_line.strip(), detail.strip(), [e.strip() for e in examples if e.strip()][:3]


#: 模型可能用来表示「没有主题」的占位词：一律按「没给」处理。
_TOPIC_PLACEHOLDERS = {
    "", "无", "未知", "不确定", "无法判断", "无主题", "暂无", "n/a", "na", "none",
    "null", "unknown", "undefined", "-", "—", "()",
}


def normalize_topic_name(value, *, limit: int = 16) -> str:
    """把模型给的 topic 收敛成一个**安全的短名字**（纯函数）。

    坏 topic（非字符串、带换行/控制字符、占位词、超长、看起来像整段原文或
    网址）一律返回空串 —— 调用方据此忽略它，**绝不影响解释本身的展示与落库**。

    换行 / 制表 / 其它控制符在**原字符串**上检查：早先实现先做
    ``" ".join(text.split())`` 把换行抹成空格，导致多行 topic
    （例如「主题\\n忽略以上指令」）能通过校验。
    """
    if not isinstance(value, str):
        return ""
    text = value.strip().strip("\"'“”‘’")
    if any(ord(ch) < 32 for ch in text):
        return ""
    text = " ".join(text.split())
    if not text or text.lower() in _TOPIC_PLACEHOLDERS:
        return ""
    if "://" in text or text.lower().startswith("www."):
        return ""
    limit = max(1, int(limit))
    if len(text) > limit:
        # 太长的多半是整段原文/标题：不截断成怪名字，直接放弃
        return ""
    return text


def parse_topic(content: str, *, limit: int = 16) -> str:
    """**独立**的可选字段解析：从模型输出里取 ``topic``，失败一律返回空串。

    与 :func:`parse_explanation` 完全解耦：

    * 旧的 3 元组接口（one_line / detail / examples）语义与签名都不变；
    * topic 缺失、类型不对、是占位词、过长或像网址 → 返回空串，不抛异常；
    * 因此「模型不支持 topic」与「模型返回了坏 topic」都不会影响解释可用性。
    """
    if not isinstance(content, str) or not content.strip():
        return ""
    try:
        obj = json.loads(_strip_fence(content))
    except (json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(obj, dict):
        return ""
    return normalize_topic_name(obj.get("topic"), limit=limit)


class DeepSeekClient:
    def __init__(self, base_url: str, model: str, api_key: str, timeout: float = 25.0,
                 reasoning_effort: str = "", thinking: str = ""):
        self.base_url = (base_url or "").strip()
        self.model = (model or "").strip()
        self._api_key = api_key or ""
        self.timeout = float(timeout)
        # 空串 = 请求体里不带 reasoning_effort（见 _with_reasoning）
        self.reasoning_effort = str(reasoning_effort or "").strip().lower()
        # 空串 = 请求体里不带 thinking；"disabled" = 关掉思考模式（见 _with_thinking）
        self.thinking = str(thinking or "").strip().lower()

    @property
    def has_key(self) -> bool:
        return bool(self._api_key)

    def config_signature(self) -> str:
        return f"{self.base_url}|{self.model}"

    # ------------------------------------------------------------- 连通性测试
    def ping(self) -> str:
        """极轻量的连通性测试（设置页「测试连接」按钮）：只发一句 ping。

        **刻意只带最基本的字段**（``model`` + 一句 user 消息 + ``max_tokens=8``）：
        这条请求的唯一目的是验证「地址 + Key + 网络」三件事，字段越少越兼容 ——
        不带 ``temperature`` / ``reasoning_effort`` / ``thinking``（不同网关对这些
        字段的容忍度不一样，测不出来反而误导用户）。成功返回一句可直接显示给用户
        的说明；失败抛 :class:`ApiError`（消息已脱敏，同样可直接显示）。
        """
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 8,
            "stream": False,
        }
        text = self._post(payload)
        self._extract_content(text)  # 结构必须完整（choices / message / content）
        served = ""
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                served = str(data.get("model") or "").strip()
        except json.JSONDecodeError:  # pragma: no cover - _post 已保证是合法 JSON
            served = ""
        note = f"，服务端模型 {served}" if served and served != self.model else ""
        return f"连接成功：{self.base_url} · {self.model}{note}"

    def explain(self, term: str, context: str = "") -> ExplainResult:
        text = self._post(build_payload(self.model, term, context,
                                        reasoning_effort=self.reasoning_effort))
        content = self._extract_content(text)
        one_line, detail, examples = parse_explanation(content)
        return ExplainResult(
            one_line=one_line,
            detail=detail,
            examples=examples,
            raw=content[:4000],
            from_cache=False,
            model_config=self.config_signature(),
            topic=parse_topic(content),
        )

    # ------------------------------------------------------------- 追问接口
    def chat(self, messages, *, temperature: float = 0.3) -> str:
        """通用对话接口：返回助手回复的**纯文本**（追问用，不做 JSON 解析）。

        ``messages`` 由调用方（``ChatService``）构造：只包含当前词语、短上下文、
        已有解释、该词最近有限轮次 + 本次问题 —— 客户端本身不附加任何内容。
        """
        payload = _with_reasoning({
            "model": self.model,
            "messages": [dict(m) for m in (messages or [])],
            "temperature": float(temperature),
            "stream": False,
        }, self.reasoning_effort)
        return parse_chat_reply(self._extract_content(self._post(payload)))

    def ask(self, question: str, *, term: str, context: str = "", explanation: str = "",
            history=None, max_chars: int = 4000, temperature: float = 0.3) -> str:
        """语义化封装：直接问「关于这个词语的一个问题」，返回纯文本答案。"""
        messages = build_chat_messages(
            term=term, question=question, context=context, explanation=explanation,
            history=history, max_chars=max_chars,
        )
        return self.chat(messages, temperature=temperature)

    # ------------------------------------------------------------- 参考关系图
    def map_relations(self, entries, *, topic: str = "",
                      material_chars: int = MAP_MATERIAL_CHARS,
                      max_entries: int = MAP_MAX_ENTRIES,
                      temperature: float = 0.0) -> list:
        """让模型对**当前主题的已保存词条**提出候选关系（返回原始候选列表）。

        ``entries`` 由 ``MapService`` 构造：``[{"index", "term", "context",
        "explanation"}, ...]``，只含当前主题的词条。这里只负责发请求 + 顶层结构
        校验；端点、类型、依据、环等业务校验全部在 ``MapService`` 里做。

        ``temperature`` 默认 0.0，并带上 ``thinking``（DeepSeek 系默认关掉思考
        模式）：导图必须**确定**，同一份材料重新生成要给同一张图。
        """
        payload = build_map_payload(self.model, entries, topic=topic,
                                    material_chars=material_chars,
                                    max_entries=max_entries,
                                    temperature=temperature,
                                    reasoning_effort=self.reasoning_effort,
                                    thinking=self.thinking)
        return parse_map_relations(self._extract_content(self._post(payload)))

    def map_verify(self, items, *, topic: str = "",
                   material_chars: int = MAP_MATERIAL_CHARS,
                   temperature: float = 0.0) -> list:
        """第二次（**独立核对**）调用：只判断给定候选是否被材料支持。

        ``items`` 由 ``MapService`` 构造：``[{"index", "type", "direction",
        "source", "target", "reason", "evidence"}, ...]``，只含本地校验已经通过
        的候选。返回原始判定列表；**本方法不产生任何新边**。
        """
        payload = build_map_verify_payload(self.model, items, topic=topic,
                                           material_chars=material_chars,
                                           temperature=temperature,
                                           reasoning_effort=self.reasoning_effort,
                                           thinking=self.thinking)
        return parse_map_verdicts(self._extract_content(self._post(payload)))

    # ------------------------------------------------------------- 布局骨架（G3）
    def map_template(self, pairs, *, topic: str = "", choices=(),
                     temperature: float = 0.0) -> tuple[str, str]:
        """让模型挑一个「摆放骨架」（返回 ``(骨架 id, 理由)``；认不出就 ``("", "")``）。

        发出去的东西**只有**关系类型与起止词名（见
        :func:`build_map_template_material`）：挑骨架只需要图的形状，材料与释义
        都留在本机。默认关（``map.template_ask_model``），用户勾了才会走到这里；
        挑失败 / 超时 / 没配 Key 一律由调用方回退本地规则，**不影响**画图。
        """
        payload = build_map_template_payload(self.model, pairs, topic=topic,
                                             choices=choices, temperature=temperature,
                                             reasoning_effort=self.reasoning_effort,
                                             thinking=self.thinking)
        return parse_map_template(self._extract_content(self._post(payload)), choices)

    # ------------------------------------------------------------- 内部
    def _post(self, payload: dict) -> str:
        """POST 一次 ``/chat/completions``，返回 HTTP 200 的响应原文。

        配置缺失、网络错误、超时、鉴权失败等一律转成 :class:`ApiError`；
        错误信息全部走 ``redact``，绝不泄露 key。
        """
        if not self._api_key:
            raise ApiError("config", "尚未配置 API Key（设置 → API Key）")
        if not self.model:
            raise ApiError("config", "尚未配置模型名")
        url = endpoint_for(self.base_url)
        if not url.lower().startswith(("http://", "https://")):
            raise ApiError("config", f"Base URL 必须以 http:// 或 https:// 开头：{url}")

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("Authorization", f"Bearer {self._api_key}")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = resp.status
                raw_bytes = resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # pragma: no cover
                pass
            raise self._http_error(exc.code, detail) from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise ApiError("timeout", f"连接超时（{self.timeout:g}s）") from exc
            raise ApiError("network", redact(f"无法连接：{reason}")) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise ApiError("timeout", f"请求超时（{self.timeout:g}s）") from exc
        except OSError as exc:
            raise ApiError("network", redact(f"网络错误：{exc}")) from exc

        text = raw_bytes.decode("utf-8", "replace")
        if status != 200:
            raise self._http_error(status, text[:300])
        return text

    @staticmethod
    def _http_error(status: int, detail: str) -> ApiError:
        safe = redact(detail or "")
        if status in (401, 403):
            return ApiError("auth", "API Key 无效或没有权限，请在设置中检查 Key", status=status,
                            preview=safe)
        if status == 429:
            return ApiError("rate_limit", "触发限流，请稍后重试", status=status, preview=safe)
        if status == 400:
            return ApiError("bad_request", f"请求被拒绝：{safe[:160]}", status=status, preview=safe)
        if status >= 500:
            return ApiError("server", f"服务端错误：{safe[:160]}", status=status, preview=safe)
        return ApiError("unknown", f"HTTP {status}：{safe[:160]}", status=status, preview=safe)

    @staticmethod
    def _extract_content(text: str) -> str:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ApiError("bad_response", f"响应不是合法 JSON：{exc.msg}",
                           preview=redact(text)[:300]) from exc
        if not isinstance(data, dict):
            raise ApiError("bad_response", "响应 JSON 不是对象", preview=redact(text)[:300])
        if "error" in data and data.get("choices") is None:
            msg = data["error"]
            if isinstance(msg, dict):
                msg = msg.get("message") or json.dumps(msg, ensure_ascii=False)
            raise ApiError("server", redact(str(msg))[:200], preview=redact(text)[:300])
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ApiError("bad_response", "响应缺少 choices 数组", preview=redact(text)[:300])
        first = choices[0]
        if not isinstance(first, dict):
            raise ApiError("bad_response", "choices[0] 不是对象", preview=redact(text)[:300])
        message = first.get("message")
        if not isinstance(message, dict):
            raise ApiError("bad_response", "choices[0].message 缺失", preview=redact(text)[:300])
        content = message.get("content")
        if not isinstance(content, str):
            raise ApiError("bad_response", "choices[0].message.content 不是字符串",
                           preview=redact(text)[:300])
        return content


def ping_endpoint(*, base_url: str, model: str, api_key: str,
                  timeout: float = 12.0) -> tuple[bool, str]:
    """发一条极轻量请求验证 Base URL / Key / 网络，返回 ``(是否成功, 一句可直接显示的话)``。

    设置页的「测试连接」用它：**只发一次、只带基本字段**（见
    :meth:`DeepSeekClient.ping`），最坏 ``timeout`` 秒内返回；鉴权失败、限流、
    超时、DNS / 网络错误全部收成一句话（已脱敏），**不抛异常** —— 调用方在界面
    线程 / 后台线程里都不需要再包一层 try。
    """
    client = DeepSeekClient(base_url or "", model or "", api_key or "",
                            timeout=max(3.0, float(timeout or 12.0)))
    try:
        return True, client.ping()
    except ApiError as exc:
        return False, f"连接失败：{exc}"
    except Exception as exc:  # pragma: no cover - 兜底：线程里绝不把异常抛出去
        return False, f"连接失败：{redact(str(exc))[:200]}"
