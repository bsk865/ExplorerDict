"""DeepSeek 兼容 chat/completions 客户端。

隐私与安全红线（SPEC.md FR-9 / FR-10）：
* 解释只发送术语与短上下文；用户发起全文导图时发送完整原文和划选词；
* key 从 DPAPI 密文解出，只放进请求头，绝不进日志、绝不进异常消息（统一走 redact）；
* 任何错误都归类成可读、可重试的类型。
"""
from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.request

from .article_source import clip_text
from .logging_setup import get_logger, redact
from .models import ExplainResult

log = get_logger("api")

KIND_LABELS = {
    "schema": "生成未通过检查",
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
MAP_REL_TYPES = ("包含", "属于", "依赖", "用途", "因果", "对照", "时序", "对策")
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
2. type 只能是这八个之一，方向含义固定：
   - 包含 A→B：A 是 B 的上位概念或整体（B 包含在 A 里）；
   - 属于 A→B：A 是 B 的下位概念或组成部分（A 属于 B）；
   - 依赖 A→B：A 依赖 B，B 是 A 的前提 / 基础；
   - 用途 A→B：A 用于 B；
   - 因果 A→B：A 导致 / 造成 B；
   - 对照 A→B：A 与 B 形成对照 / 对比；
   - 时序 A→B：A 发生在 B 之前（流程的前一步 → 后一步）；
   - 对策 A→B：针对 A 采取 / 提出 B。
   ★ **流程步骤之间绝不许写「包含 / 属于」**（这一条是本提示词最容易犯的错，必须
   自检）。「包含 / 属于」只允许用在**真正的整体—部分 / 上位—下位**上（例如「卷积
   神经网络」包含「卷积层」；「卷积层」属于「卷积神经网络」）。文章讲「先做 A，再
   做 B」时，A 是流程的**一步**、B 是**下一步**，两步是并列的环节，**不是** A 包含 B：
   这种情况写 **时序 A→B**；材料明说 B 以 A 为前提 / 依据（「B 必须依赖 A」）就写
   **依赖 B→A**；材料明说 A 导致 B（「A 之后做 B，因为 A 的结论要用于 B」）就写
   **因果 A→B**。
   反例（**必须避免**，实测模型最容易这样写）：
   - 「确定项目需求是项目开发流程的第一步」→ 不许写「项目开发 包含 确定项目需求」，
     应写「项目开发 → 确定项目需求 时序」（或按材料写成依赖）；
   - 「任务拆解是项目开发流程中的一环」→ 同上，写 **时序**，不许写「包含」。
   自检：写一条关系前，先在材料里找出一句能**直接读出**这个类型的话 —— 光有两个词
   同时出现在一句里不算。下面这些**都不是**上面这些关系，材料只写了这些就不要输出：
   - 「A 在 B 中 / A 部署在 B 中 / A 出现在 B 里」＝ 场景描述，既不是依赖也不是因果；
   - 「A 在 B 里的开销高 / A 对 B 而言是个问题」＝ A 的困难发生在 B 里，不是 A 导致 B；
   - 「现有方案依赖 A」＝ 依赖的前件是那个方案；方案本身不在本次词条里时，不要把这条
     依赖挂到别的词上；
   - 两个词只是同属一个话题、总是结伴出现 ＝ 没有类型，不要输出。
   类型拿不准时**宁可不输出**：类型标错的候选会被核对判为不成立，反而让这个词变成孤立词；
   ★ **每一个给出的词都要在候选里至少出现一次**：某个词是文章里一个独立的环节 /
   概念时，它至少与相邻的那一步有一条边（时序 / 依赖 / 因果之外都不行时，也要按材料
   写一条最贴切的）。一个词在候选里根本没出现，它在图上就会变成没有任何关系的
   「孤立词」—— 那对用户是最糟的结果。但如果材料里真的找不到任何一句能支撑它与别的
   词的关系，**宁可不写**，不要为了让它不孤立而编一条；
3. reason 是一句简短依据（不超过 40 字）；evidence 必须**逐字**来自本次给出的上下文或释义，
   不许改写、不许编造出处 / 页码 / 引文；
4. 材料不足以支持、只能靠常识猜测、或者需要主题以外的知识时：**不要输出这条关系**。
   一条都没有就返回 {"relations": []}；空数组合法 —— 绝不强行连线，也不要用「相关」这类默认关系；
5. 同一条关系只输出一次（「对照」的正反视为同一条）；**只有包含 / 属于的层级矛盾
   禁止**：同一对词不要两个方向都说「包含」（或都说「属于」）—— 上下位互相颠倒 =
   不可能成立的层级。依赖 / 因果的双向（互相依赖、互为因果）**不是**矛盾：只有两个
   方向**各自都有材料明确支持**时才允许同时输出，只有一个方向有材料就只写那一个方向；
6. 只使用本次给出的词条与材料，不要引用其它主题、整篇文档、窗口标题或网址。
★★ **同一条流程上的两个步骤之间，绝不许写「包含 / 属于」（批次 M24 立的第二道红线）。**
材料说的是「先 A、再 B」「A 之后做 B」「A 是第一步、B 是第二步」时，A 与 B 是**同一条
流程上的先后两步**，谁也不包含谁 —— 这种情况只允许「时序 A→B」（或按材料写依赖 / 因果）。
「A 是流程的一环」「A 是流程的第一步」**同样不是**包含关系的依据：它是**时序 / 依赖**的依据。
只有材料**明说** A 由 B（以及别的部件）**构成 / 组成 / 包括**，或者 B 是 A 的**组成部分 /
下位概念**时，才写「包含 A→B」。
反例（实测模型反复这样写，必须自检）：
- 「开展项目开发需要一套持续迭代的工作流程，首先要确定项目需求」→ **不许**写
  「项目开发 包含 确定项目需求」（那是流程的第一步，不是它的组成部分），
  应写「确定项目需求 → 项目开发 **依赖**」或按材料写时序；
- 「任务拆解是项目开发流程中的一环」→ **不许**写「项目开发 包含 任务拆解」，同上；
- 「确定项目需求、拆解任务、项目复盘共同构成完整的项目开发体系」→ 这句才是**真正的
  整体—部分**：只有**收口句这样明说**时才可以写「包含」。
自检问自己一句：**把 B 从 A 里拿掉，A 还是 A 吗？** 拿掉之后 A 就不成立的（部件、组成、
下位）才可以写包含；拿掉之后 A 只是少做了一步（流程）必须改写时序 / 依赖 / 因果。"""

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
   ★ **关系类型不同，依据的长相也不同**（批次 M23，别拿「层级」那一套去卡别的类型）：
   - 时序 / 依赖 / 因果 / 对策这四种，材料写「先 A 后 B」「B 依赖 / 依据 A」「A 之后做 B」
     「因 A 所以 B」**就是**正面依据 —— 材料不会专门写「A 是 B 的上位概念」这种话，
     别因为它没写就判 uncertain；
   - **包含 / 属于**要求更高：材料必须真的把 B 说成 A 的**整体 / 一部分、上位 / 下位**
     （「B 是 A 的一个环节 / 组成部分 / 一类」）。只说 B 在流程里位于 A 之后、或者
     只是同属一个话题，**不足以**支持包含 / 属于，这种情况判 contradicted 并说明
     应该用哪种类型；
4. ★★ **候选写「包含 / 属于」时，按最严的口径判**（批次 M24 立的红线，实测模型
   反复把流程步骤写成包含）：材料说的是「先 A、再 B」「A 之后做 B」「A 是第一步、
   B 是第二步」「A 是流程中的一环」时，A 与 B 是**同一条流程上的先后两步**，
   谁也不包含谁 ⇒ 判 ``contradicted``，并在 note 里写清「应写时序 / 依赖」。
   只有材料**明说** A 由 B（以及别的部件）**构成 / 组成 / 包括**，或者 B 是 A 的
   **组成部分 / 下位概念**时，才判 ``supported``。
   自检问一句：**把 B 从 A 里拿掉，A 还是 A 吗？** 拿掉之后 A 只是少做了一步
   （流程）⇒ contradicted；拿掉之后 A 就不成立了（部件、组成、下位）⇒ supported。
5. 只依据本次给出的材料判断，不要引用其它主题、整篇文档、窗口标题或网址。"""
MAP_VERIFY_USER_TEMPLATE = """主题：{topic}

候选关系（序号 / 类型 / 方向 / 端点材料 / 候选依据 / 候选证据）：
{items}"""

#: ★ 补问（第三轮，批次 M23）：某个词被核对删成**孤立词**之后，只针对它再问一次。
#: 与第一轮的区别：材料里**额外点名**那个还没连上的词，并明确「只为它提候选」。
#: 实测（主题「项目开发」，2026-10-09）：第一轮 9 条候选的核对结果是 6 条通过、1 条
#: ``uncertain``（「循环迭代」唯一那条「包含」）、2 条 ``contradicted`` ⇒ 那个词一个
#: 关系都没有、在图上变成孤立词。补问带的是同一条类型红线（流程步骤用时序 / 依赖 /
#: 因果），因此它有机会给出第一轮漏掉的那条边。
MAP_RESCUE_SYSTEM_PROMPT = MAP_SYSTEM_PROMPT + """

★ 本次是**补问**：材料里那几条已经有关系的词不必再提，请只为**还没连上的那个词**
提出候选关系（它至少要出现在一条候选里）。依然遵守上面全部规则，尤其是「流程步骤
之间不许写包含 / 属于」；材料确实支撑不了时返回 {"relations": []}，不要硬凑。"""

MAP_RESCUE_USER_TEMPLATE = """主题：{topic}

已保存词条（编号 / 词语 / 上下文 / 已有释义）：
{material}

这批词里，下面这些词已经至少有一条画出来的关系：
{linked}

**还没连上任何关系的词：{isolated}**（编号 {isolated_index}）。
请只为它提候选关系：它跟上面哪个词之间有什么类型的关系？每条都要能在材料里直接读出。"""


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


# -------------------------------------------- 按原文归纳（文章逻辑流水线）
#: 一篇文章的**主逻辑类型**（六种，唯一权威定义在 :mod:`app.map_service`）。
ARTICLE_LOGIC_TYPES = ("时序", "因果", "对策", "层级", "依赖", "对比")
#: 实体名上限（**软约束**）：逼模型用文章里的原词，而不要写一整句。
ARTICLE_ENTITY_CHARS = 10
#: 发出去的**文章正文**上限（字符，批次 M25：6000 → 30000）。
#: 为什么是 30000：中文 1 字 ≈ 0.6 token，30000 字 ≈ 1.8 万 token，加上提示词
#: 与输出，一次调用仍在 deepseek-chat 的 128K 上下文里绰绰有余；而 25 秒的旧
#: 超时（现默认 120 秒）在长文上根本答不完。常见来源（公众号 / 博客 / 产品文档 /
#: 论文页）绝大多数在 3 万字以内，一次就能交给模型看清全文。
#: 超长时**保头也保尾**（见 ARTICLE_TEXT_HEAD_RATIO）：文章的结论与收口句几乎
#: 总在最后，只留头部会把层级与总结整段砍掉 —— 而导图最需要的恰恰是那几句。
ARTICLE_TEXT_CHARS = 30000
#: 超长截断时留给**头部**的比例，剩下的给尾部（0.6 = 头 60% / 尾 40%）。
ARTICLE_TEXT_HEAD_RATIO = 0.6
ARTICLE_MAX_BRANCHES = 9
ARTICLE_MAX_ENTITIES = 60

#: 「重新生成」用的**思维阶梯**：每往后一档，就换一种想这件事的方式。
#: 为什么要这个：以前点「重新生成」= 同一个模型 + 同一段提示词 + ``temperature=0``
#: ⇒ 第二次必然拿到一模一样的图（用户实测反馈：布局一个像素都不动，等于白点）。
#: 现在每一档都换一套**明确不同**的归纳策略，并在状态行如实写清用的是哪一档。
#: 第 0 档必须与批次 M20 的老口径**逐字一致**，否则老缓存会失效、行为会漂移。
ARTICLE_VARY_STEPS: tuple[tuple[str, str, float], ...] = (
    (
        "标准",
        "",
        0.0,
    ),
    (
        "深挖一层",
        "在标准做法之外，这一遍请**再往下挖一层**：优先把文章里隐含的中间环节、"
        "前提条件、以及「为什么这一步必须在下一步之前」写出来；"
        "分支名要能看出推进关系，不要只写标题式的名词短语。",
        0.2,
    ),
    (
        "换一种结构",
        "这一遍请**换一种切法**：上一版是按阶段切的，这一遍优先按**因果链**来切，"
        "找出文章真正的主线（是哪一个问题导致哪一个结果）；"
        "如果文章有两种以上可能的切法，选**上一版没有选的那个**，让这张图明显不一样。",
        0.4,
    ),
    (
        "最大思考",
        "这一遍请**用最完整的方式重做**：先把全文的论证顺序在心里过一遍，"
        "找出上一版漏掉的关键环节与反例，再给出分支与关系；"
        "宁可想得慢一点，也要让这一版的脉络比上一版更完整、更准确。",
        0.6,
    ),
)
#: 阶梯最多转几档（到顶后停在最后一档，不无限上调）。
ARTICLE_VARY_MAX = len(ARTICLE_VARY_STEPS) - 1

ARTICLE_LOGIC_SYSTEM_PROMPT = """你在**读一篇文章**，然后把它归纳成一张「脉络清晰、顺序正确」的思维导图。

用户会给你一篇文章的正文（可能被截断，截断处会明确标注），以及**用户在这篇文章里划过的词**。
严格只输出一个 JSON 对象，不要输出任何其它文字，不要使用 Markdown 代码围栏。结构：
{"branches": [{"name": "分支名", "entities": ["实体", "实体"]}], "relations": [{"src": "实体", "dst": "实体", "type": "时序", "reason": "一句话依据"}]}

怎么归纳（顺序就是阅读顺序，不是重要性排序）：
1. 先通读全文，判断这篇文章**在讲什么**：它的主线是一条时间线、一条因果链、一个问题的对策、
   一组层层包含的概念、一条单向的依赖，还是两方的对照；
2. 再把文章切成 2~6 个**分支**：一个分支 = 文章里的一个逻辑阶段 / 一层 / 一方。
   分支名用 4~12 个字概括这一步在讲什么（例如「问题提出」「方案设计」「实测结果」）；
3. 每个分支里的实体，按**它们在文章中出现的先后顺序**排列（时序类文章尤其重要，
   顺序错了这张图就是错的）。实体名必须是文章里出现过的说法，**尽量不超过 10 个字**；
4. 如果用户划过词，把划过的那几个词放进它们**真正所属**的分支里（不是单独开一个分支）；
   划过的词是**用户点名要看的东西**，名字必须与用户给的**一字不差**照抄（用户写「任务拆解」，
   你就写「任务拆解」，**不要**改写成「项目任务拆解」或「拆解任务」；多了字、少了字都会被判成
   另一个词，那条线就画不出来了）；
5. 然后再给关系：src / dst 直接写**实体的名字**（与 branches 里写的**逐字一致**），
   type 只能是这六个（与第 1 步判断文章结构用的六个词是两套，别混）：
   - 时序 A→B：文章中 A 出现在 B 之前，且二者是同一条主线上的相邻两步；
   - 因果 A→B：文章明确说 A 导致 / 造成 B；
   - 对策 A→B：B 是针对问题 A 提出的办法 / 措施；
   - 层级 A→B：A 是 B 的上位概念或整体；
   - 依赖 A→B：A 依赖 B，没有 B 就做不成 A；
   - 对比 A→B：文章把 A 与 B 摆在一起比较。
   （「时序」与「对策」就是画线用的名字，不要再换成「因果」——那会把文章的
   步骤先后画成因果关系，是错的。）
6. relations 是给**画线**用的，宁可少也不要凑：**不要**把同一个分支里并列的词两两连线，
   也**不要**为了「连成一张网」而造关系。分支归属已经表达了并列，线只表达分支之间的推进。
   **但每一个划过的词都必须至少出现在一条关系里**（作为 src 或 dst）——文章里承接在
   别人后面的词，就要有那条线；一个词飘在图上、谁也不连，等于把它漏掉了。

红线（违反任何一条，这张图都算不合格）：
* 只写文章里真的写了的内容，不许补充常识、不许编造数字 / 出处 / 人名；
* 划过的词**照抄**，不许加字减字换字（加了「项目」「完成」这类前缀，图上就接不上那张卡）；
* 每一层里**只连真正推进的线**：文章里那个先后是 A→B 就写 A→B，不要反过来写；
* 时序类文章的主线**不许写成并列从属**（那不是这篇文章的结构）；
* 不要输出「其它」「杂项」这类分支名；某个词放在哪儿都不合适，就把它放进最接近的分支；
* 分支名与实体名都不要带序号（不要写「1. 问题提出」）；
* 拿不准的实体宁可不写，也不要写一个文章里没有的词。"""

ARTICLE_LOGIC_USER_TEMPLATE = """标题：{title}

用户在这篇文章里划过的词（可能为空，按先后顺序）：
{terms}

正文：
{article}{vary}"""

#: 「重新生成」时追加在用户消息末尾的换思路指令（空串 = 标准做法，一个字不加）。
ARTICLE_VARY_TEMPLATE = """

————————————————
这一遍的额外要求（{label}，第 {step} 档）：{approach}"""


def article_vary(step: int) -> tuple[str, str, float]:
    """把「第几档」翻译成 ``(标签, 追加指令, temperature)``（纯函数）。

    超出范围一律**夹到有效档位**（负数 → 第 0 档；太大 → 最后一档），
    因此界面传什么都不会炸，也不会因为一次脏计数就换出个乱七八糟的提示词。
    """
    index = max(0, min(int(step or 0), ARTICLE_VARY_MAX))
    label, approach, temperature = ARTICLE_VARY_STEPS[index]
    return label, approach, float(temperature)


def build_article_vary_note(step: int) -> str:
    """追加给模型的换思路指令（第 0 档返回空串 —— 标准做法不加任何东西）。"""
    label, approach, _temperature = article_vary(step)
    if not approach:
        return ""
    index = max(0, min(int(step or 0), ARTICLE_VARY_MAX))
    return ARTICLE_VARY_TEMPLATE.format(label=label, step=index + 1, approach=approach)



def build_article_material(text: str, *, limit: int = ARTICLE_TEXT_CHARS) -> str:
    """把正文压成提示词材料（纯函数）。

    压缩换行（连续空行只留一个）省 token；超过 ``limit`` 时**保头也保尾**
    （头 :data:`ARTICLE_TEXT_HEAD_RATIO`，其余给尾），中间如实写
    「（正文过长，中间省略 N 字）」—— 绝不假装这就是全文，否则模型会把
    半篇文章当成完整脉络来归纳。

    为什么必须保尾（批次 M25 改）：文章的主旨句、收口句、「A、B、C 共同构成
    完整的 X」这类**整体—部分总结**几乎总在最后一段。旧写法 ``body[:cap]``
    只留头部，等于每次把结论丢掉，图上就少了层级与总结那几条边。
    两端各自按段落边界收口，免得句子被从中间劈开。

    掐头去尾这件事本身住在 :func:`app.article_source.clip_text`（批次 M26）：
    留档时按 ``capture.article_max_chars`` 截的是同一个动作，两处必须用同一套
    写法，否则「省略了多少字」这句话在界面与提示词里会对不上。
    """
    body = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if not body:
        return "（没有取到原文）"
    return clip_text(body, limit, head_ratio=ARTICLE_TEXT_HEAD_RATIO)


def build_article_logic_payload(model: str, logic, *, title: str = "",
                                temperature: float = 0.0,
                                reasoning_effort: str = "",
                                thinking: str = "", vary: int = 0,
                                logic_chain: dict | str | None = None) -> dict:
    """构造「按原文归纳」的请求体。

    ``logic`` 由 :mod:`app.map_service` 构造：
    ``{"text": 原文, "terms": [划过的词], "feedback": 上一轮的差异清单}``。
    ``temperature`` 默认 **0.0**：同一篇文章的**同一档**归纳必须稳定，同一档
    重新生成不该换一张图（要换图请换档，见 ``vary``）。

    ``vary`` 是「重新生成」的思维阶梯档位（批次 M21-B）：第 0 档 = 批次 M20 的
    老口径（提示词一字不加），往后每档往用户消息末尾追加一段**换思路指令**。
    界面上那句「第 N 档」用的就是它。

    ``logic_chain`` 是**第零轮读全文得出的骨架**（批次 M24）：这条「按原文归纳」
    的流水线自己就通读了全文，但仍然要走第零轮 —— 由它先把方向、层级、回头闭环
    定下来，归纳那一轮只负责把骨架落到具体的分支与连线上。留空时用户消息**逐字
    等于**批次 M22 的老文本（老用例与老缓存行为不变）。
    """
    data = logic or {}
    text = str(data.get("text") or "")
    terms = [str(t).strip() for t in list(data.get("terms") or []) if str(t).strip()]
    feedback = str(data.get("feedback") or "").strip()
    _label, _approach, vary_temperature = article_vary(vary)
    user = ARTICLE_LOGIC_USER_TEMPLATE.format(
        title=(title or "").strip() or "（未知标题）",
        terms="、".join(terms) if terms else "（没有划过词）",
        article=build_article_material(text),
        vary=build_article_vary_note(vary),
    )
    chain_text = (logic_chain if isinstance(logic_chain, str)
                  else build_logic_chain_material(logic_chain or {}))
    if chain_text:
        user += ("\n\n【这篇文章的逻辑链（第零轮已经通读全文得出的骨架，方向以它为准；"
                 "你仍然要在正文里找到支持每条关系的依据）】\n" + chain_text)
    if feedback:
        user += ("\n\n上一轮你的输出有这样一些问题，请**改掉它们**后重新输出完整 JSON：\n"
                 + feedback)
    return _with_thinking(_with_reasoning({
        "model": model,
        "messages": [
            {"role": "system", "content": ARTICLE_LOGIC_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "temperature": float(temperature if not vary else vary_temperature),
        "stream": False,
        "response_format": {"type": "json_object"},
    }, reasoning_effort), thinking)


#: 逻辑链的「主逻辑」类型（第零轮用）。与 :data:`app.map_service.LOGIC_TYPES` 同值 ——
#: 定义在这里是因为 ``map_service`` 反过来 import 本模块，不能反向依赖（会成环）。
LOGIC_TYPES: tuple[str, ...] = ("时序", "因果", "对策", "层级", "依赖", "对比")

#: ★★ 逻辑链分析（第零轮，批次 M24）：**先读懂整篇文章**，再谈画哪条线。
#: 用户口径（2026-10-09）：「我的想法就是在导图生成流程中加入逻辑链检查，大模型必须
#: 对文章的逻辑链条，层级划分，因果关系等等有清晰的认知和划分，为了确保导图的精准性
#: 这一步必须不能省略，只有 LLM 彻底的理解文章链条，逻辑才能实现导图的精准生成。」
#:
#: 为什么单开一轮而不是把要求塞进第 1 轮：第 1 轮的材料是**词条**（一个词 + 它周围
#: 240 字的上下文），模型看到的是若干个互不相邻的碎片，**从来没有见过整篇文章**——
#: 于是它只能按「这两个词的上下文里都出现了什么」硬凑边，凑出来的方向经常是倒的
#: （实测主题「项目开发」：材料原话「项目任务拆解必须依赖已经确认好的项目需求」，
#: 候选写成 `任务拆解 -依赖-> 确定项目需求`，核对判 contradicted、主线当场断一节）。
#: 这一轮把**文章正文**整段交给模型，只让它回答「这篇文章的骨架是什么」，一条关系都
#: 不许提；它给出的骨架紧接着作为第 1 轮的**前置认知**发下去，模型再按骨架提候选。
#:
#: 这一轮的产物是「认知」而不是边界的权威：端点、类型、证据仍然全部由本地校验 +
#: 第二次核对决定（见 :mod:`app.map_service`）。
MAP_LOGIC_CHAIN_SYSTEM_PROMPT = """你在为《探索词典》做导图生成前的**第一步：读懂文章的逻辑链**。

用户会给你一篇短文章的正文，以及这篇文章里已经划出来的词。**这一步一条关系都不许提**，
你的唯一任务是**把文章的逻辑彻底想清楚并写下来** —— 后面的导图完全按照你写下的这份骨架
来画，所以这里写错、写漏，图就一定是错的。

严格只输出一个 JSON 对象，不要输出任何其它文字，不要使用 Markdown 代码围栏。结构：
{"logic_type": "时序", "core": "一句话说出这篇文章到底在讲什么",
 "concepts": [{"name": "文章里的概念词", "role": "这个词在文章里扮演什么角色"}],
 "hierarchy": [{"whole": "上位 / 整体", "parts": ["下位 / 组成部分"]}],
 "temporal": [{"first": "先", "then": "后", "basis": "文章里哪句话说明它在前"}],
 "causal": [{"cause": "因", "effect": "果", "basis": "文章里哪句话说明 A 造成 B"}],
 "dependency": [{"dependent": "要依赖别人的那个", "prerequisite": "被依赖的前提",
                 "basis": "文章里哪句话说明它依赖它"}],
 "contrast": [{"a": "一方", "b": "另一方", "basis": "文章里哪句话把它们对照"}],
 "turning_points": ["文章里改变方向 / 回头 / 形成闭环的那一句或那一处"],
 "synthesis": {"whole": "文章最后归结出来的那个整体", "parts": ["共同构成它的那些环节"]}}

填写规则（每一条都是硬要求）：
1. **concepts 必须是文章里出现过的概念词的**清单**（按文章顺序），一个都不许漏**：
   流程的每一步、每一层级的整体与部分、因果链上的因与果、专门被作者强调（加粗 / 单独
   成句 / 「必须」「需要」）的词，全部都要写进来。**只写名词性概念**，不要写「首先」
   「之后」这种连接词，也不要写整句话。用户划出来的词要在 concepts 里出现；
2. **方向必须按逻辑写，不许照抄句子里的主谓顺序**：这是最容易错的一处。汉语里
   「B 必须依赖已经确认好的 A」是 **A 在前、B 依赖 A**；「A 之后做 B」是 **A 在前、
   B 在后**；「A 导致 / 使得 / 因此有了 B」是 **A 是因、B 是果**。写 dependency 时
   `dependent` = 那个需要借助别人的词，`prerequisite` = 被借助的那个词；
   写 causal 时 `cause` = 起因，`effect` = 结果。写完**回头看一遍**：把这两个词按你写的
   顺序念出来，是否与文章的意思一致；
3. **temporal / causal / dependency / contrast 四种一张都不能空着交差**：文章里没有
   就用空数组 `[]`（空数组合法，绝不硬凑），但只要文章里有，就一定要写全 ——
   漏掉一条，图上就少一条线；
4. **层级（hierarchy）单独写**：整体—部分 / 上位—下位才写这里（例如「卷积神经网络」
   包含「卷积层」）。文章说「先做 A 再做 B」时，A / B 是**流程的两步**，不是整体与部分，
   那种情况写 temporal，**不许**写进 hierarchy；
5. **收口句（synthesis）一定要填**：文章最后常常用一句总结把前面几个环节收在一起
   （「…共同构成…」「…一起形成了…」「…都是…的一部分」）。`whole` 写那个被归结出来的
   整体，`parts` 写共同构成它的那些环节；文章确实没有这种句子时，`whole` 写空串、`parts` 写 []；
6. **turning_points 写「回头 / 反向 / 闭环」**：文章里凡是出现「反向调整」「重新」
   「再回到」「由此又」这类把流程拉回去、让链条闭合成环的地方，写进 turning_points
   （写那一句或那一处的意思），闭环是文章逻辑里最容易被漏掉的东西；
7. logic_type 只能是这六个之一：时序 / 因果 / 对策 / 层级 / 依赖 / 对比；
8. 只依据这篇文章的正文。**不许**用主题以外的知识补全，**不许**推测作者没写的因果关系；
   正文里确实没有的东西就留空数组。"""

#: 逻辑链分析的用户消息：**整篇文章正文** + 这篇文章划出来的词。
#: 与后面几轮（只发词条碎片）刻意不同 —— 这一轮存在的意义就是让模型看见全文。
MAP_LOGIC_CHAIN_USER_TEMPLATE = """主题：{topic}

文章正文：
{article}

这篇文章里用户划出来的词（你要保证它们都在 concepts 里）：
{terms}

请按上面的要求，写出这篇文章的逻辑链骨架。"""


def parse_map_logic_chain(content: str) -> dict:
    """解析逻辑链骨架（纯函数，宽容但绝不放行垃圾）。

    与其它解析器同一套路：认不出就返回**空骨架**（各字段空值），绝不抛 —— 逻辑链这一轮
    失败时**不能**把整次生成打断（调用方会退回到「没有骨架直接提候选」的老路径，并在
    结果里如实标注这一次没有走到逻辑链）。
    """
    empty: dict = {"logic_type": "", "core": "", "concepts": [], "hierarchy": [],
                   "temporal": [], "causal": [], "dependency": [], "contrast": [],
                   "turning_points": [], "synthesis": {"whole": "", "parts": []}}
    try:
        data = json.loads(str(content or ""))
    except (TypeError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty

    def _text(value, limit: int = 40) -> str:
        return str(value or "").strip()[:limit]

    def _items(value) -> list:
        return list(value) if isinstance(value, list) else []

    def _pairs(value, first_key: str, second_key: str) -> list:
        out: list = []
        for row in _items(value):
            if not isinstance(row, dict):
                continue
            first = _text(row.get(first_key))
            second = _text(row.get(second_key))
            if not first or not second:
                continue
            out.append({first_key: first, second_key: second,
                        "basis": _text(row.get("basis"), 60)})
        return out

    concepts: list = []
    for row in _items(data.get("concepts")):
        if isinstance(row, dict):
            name = _text(row.get("name"), 20)
            if name:
                concepts.append({"name": name, "role": _text(row.get("role"), 40)})
        elif isinstance(row, str) and row.strip():
            concepts.append({"name": _text(row, 20), "role": ""})
    hierarchy: list = []
    for row in _items(data.get("hierarchy")):
        if not isinstance(row, dict):
            continue
        whole = _text(row.get("whole"), 20)
        parts = [part for part in (_text(item, 20) for item in _items(row.get("parts")))
                 if part]
        if whole and parts:
            hierarchy.append({"whole": whole, "parts": parts})
    synthesis = data.get("synthesis")
    whole, parts = "", []
    if isinstance(synthesis, dict):
        whole = _text(synthesis.get("whole"), 20)
        parts = [part for part in (_text(item, 20) for item in _items(synthesis.get("parts")))
                 if part]
    turning = [item for item in (_text(item, 60) for item in _items(data.get("turning_points")))
               if item]
    logic_type = _text(data.get("logic_type"), 8)
    return {
        "logic_type": logic_type if logic_type in LOGIC_TYPES else "",
        "core": _text(data.get("core"), 80),
        "concepts": concepts,
        "hierarchy": hierarchy,
        "temporal": _pairs(data.get("temporal"), "first", "then"),
        "causal": _pairs(data.get("causal"), "cause", "effect"),
        "dependency": _pairs(data.get("dependency"), "dependent", "prerequisite"),
        "contrast": _pairs(data.get("contrast"), "a", "b"),
        "turning_points": turning,
        "synthesis": {"whole": whole, "parts": parts},
    }


def logic_chain_is_empty(chain) -> bool:
    """骨架是不是「什么都没写」（纯函数）。空骨架 = 这一轮实际没起作用。"""
    if not isinstance(chain, dict):
        return True
    for key in ("concepts", "hierarchy", "temporal", "causal", "dependency", "contrast",
                "turning_points"):
        if list(chain.get(key) or ()):
            return False
    whole = str((chain.get("synthesis") or {}).get("whole") or "").strip()
    return not whole


def build_logic_chain_material(chain, *, limit: int = 120) -> str:
    """把逻辑链骨架压成给**第一轮**看的前置认知（纯函数；空骨架返回空串）。

    只写「文章是怎么串起来的」，不写任何一条现成的边 —— 模型仍然要自己按材料提出
    候选并给出逐字证据；这里给的是**方向上的定调**（谁在前、谁依赖谁、谁构成谁）。
    """
    if not isinstance(chain, dict) or logic_chain_is_empty(chain):
        return ""
    cut = max(1, int(limit))
    lines: list[str] = []
    logic_type = str(chain.get("logic_type") or "").strip()
    core = str(chain.get("core") or "").strip()
    if logic_type or core:
        lines.append(f"这篇文章的主逻辑：{logic_type or '（未定）'}。{core}"[: cut * 2])
    names = "、".join(str(item.get("name") or "").strip()
                     for item in list(chain.get("concepts") or ())
                     if str(item.get("name") or "").strip())[: cut * 4]
    if names:
        lines.append(f"文章里的全部概念词（一个都没漏）：{names}")
    for row in list(chain.get("hierarchy") or ()):
        parts = "、".join(str(part) for part in list(row.get("parts") or ())[:8])
        lines.append(f"层级：{row.get('whole')} 是整体，包含 {parts}")
    for row in list(chain.get("temporal") or ()):
        lines.append(f"先后：{row.get('first')} 在 {row.get('then')} 之前"
                     + (f"（依据：{row.get('basis')}）" if row.get("basis") else ""))
    for row in list(chain.get("causal") or ()):
        lines.append(f"因果：{row.get('cause')} 导致 {row.get('effect')}"
                     + (f"（依据：{row.get('basis')}）" if row.get("basis") else ""))
    for row in list(chain.get("dependency") or ()):
        lines.append(f"依赖：{row.get('dependent')} 依赖 {row.get('prerequisite')}"
                     f" ⇒ 方向是 {row.get('prerequisite')} → {row.get('dependent')}"
                     + (f"（依据：{row.get('basis')}）" if row.get("basis") else ""))
    for row in list(chain.get("contrast") or ()):
        lines.append(f"对照：{row.get('a')} 与 {row.get('b')}")
    for point in list(chain.get("turning_points") or ())[:6]:
        lines.append(f"回头 / 闭环：{point}")
    whole = str((chain.get("synthesis") or {}).get("whole") or "").strip()
    parts = "、".join(str(part) for part in
                     list((chain.get("synthesis") or {}).get("parts") or ())[:8])
    if whole and parts:
        lines.append(f"收口：{parts} 共同构成 {whole}（这几条属于关系要画出来）")
    return "\n".join(lines)[: cut * 12]


def build_map_logic_chain_payload(model: str, article: str, *, topic: str = "",
                                  terms=(), temperature: float = 0.0,
                                  text_limit: int = ARTICLE_TEXT_CHARS,
                                  reasoning_effort: str = "",
                                  thinking: str = "") -> dict:
    """构造**逻辑链分析**请求体（第零轮）：只发这篇文章的正文 + 划出来的词。

    与第 1/2/3 轮的材料边界不同（那几轮只发词条碎片）—— 这一轮**必须**看见整篇文章，
    否则「读懂逻辑链」这件事根本无从谈起。发出去的东西仍然只有：主题名、文章正文、
    用户自己划出来的词。窗口标题 / 网址 / 文件路径 / 其它主题的词条一律不出现。
    """
    terms_text = "、".join(str(term).strip() for term in list(terms or ()) if str(term).strip())
    return _with_thinking(_with_reasoning({
        "model": model,
        "messages": [
            {"role": "system", "content": MAP_LOGIC_CHAIN_SYSTEM_PROMPT},
            {"role": "user", "content": MAP_LOGIC_CHAIN_USER_TEMPLATE.format(
                topic=(topic or "").strip() or "（未命名主题）",
                article=build_article_material(article, limit=int(text_limit)),
                terms=terms_text or "（没有划出来的词）")},
        ],
        "temperature": float(temperature),
        "stream": False,
        "response_format": {"type": "json_object"},
    }, reasoning_effort), thinking)


def parse_article_logic(content: str) -> dict:
    """解析归纳响应：**只做形状整理**，合不合格由 :mod:`app.map_service` 判定。

    顶层结构错误（空响应 / 非 JSON / 不是对象）一律抛 :class:`ApiError`。
    ``branches`` 缺失（或空数组）同样抛错 —— 一张没有分支的图不是归纳，是失败。
    """
    if not isinstance(content, str) or not content.strip():
        raise ApiError("bad_response", "模型返回内容为空")
    raw = _strip_fence(content)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError("bad_response", f"归纳输出不是合法 JSON：{exc.msg}",
                       preview=raw[:300]) from exc
    if not isinstance(obj, dict):
        raise ApiError("bad_response", "归纳输出的 JSON 不是对象", preview=raw[:300])
    raw_branches = obj.get("branches")
    if not isinstance(raw_branches, list):
        raise ApiError("bad_response", "归纳输出缺少 branches 数组", preview=raw[:300])

    branches: list[dict] = []
    for item in raw_branches:
        if isinstance(item, str):
            name, members = item.strip(), []
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("branch") or "").strip()
            members = item.get("entities")
            if members is None:
                members = item.get("members")
        else:
            continue
        if not name:
            continue
        seen: set[str] = set()
        entities: list[str] = []
        for entity in list(members or []):
            name_of = str(entity).strip()
            if not name_of or name_of in seen:
                continue
            seen.add(name_of)
            entities.append(name_of)
        branches.append({"name": name, "entities": entities})
    if not branches:
        raise ApiError("bad_response", "归纳输出里没有一个带名字的分支", preview=raw[:300])

    relations: list[dict] = []
    for item in list(obj.get("relations") or []):
        if not isinstance(item, dict):
            continue
        relations.append({
            "src": str(item.get("src", item.get("source", ""))).strip(),
            "dst": str(item.get("dst", item.get("target", ""))).strip(),
            "type": str(item.get("type") or item.get("rel") or "").strip(),
            "reason": str(item.get("reason") or item.get("evidence") or "").strip(),
        })
    return {
        "title": str(obj.get("title") or "").strip(),
        "main_logic": str(obj.get("main_logic") or "").strip(),
        "branches": branches,
        "relations": relations,
    }


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


def build_map_rescue_payload(model: str, entries, *, topic: str = "",
                             linked=(), isolated=(), material_chars: int = MAP_MATERIAL_CHARS,
                             max_entries: int = MAP_MAX_ENTRIES,
                             temperature: float = 0.0,
                             logic_chain=None,
                             reasoning_effort: str = "",
                             thinking: str = "") -> dict:
    """构造**补问**请求体（批次 M23）：词被核对删成孤立词时，只针对它再问一次。

    与 :func:`build_map_payload` 同源（同一份材料、同一条类型红线），只多两行点名：
    已经连上的词、以及**还没连上**的那个词（含它的编号）。材料边界完全一致 ——
    仍然只有当前主题的词条，不带窗口标题 / 网址 / 整篇文档；``logic_chain`` 与第一轮
    用**同一份**骨架，免得补问又问出一个方向相反的边。
    """
    material = build_map_material(entries, material_chars=material_chars,
                                  max_entries=max_entries)
    linked_text = "、".join(f"[{int(index)}] {str(term).strip()}"
                            for index, term in list(linked or ())) or "（没有）"
    words = "、".join(str(term).strip() for _index, term in list(isolated or ()))
    indexes = "、".join(str(int(index)) for index, _term in list(isolated or ()))
    chain_text = (logic_chain if isinstance(logic_chain, str)
                  else build_logic_chain_material(logic_chain)).strip()
    chain_block = ""
    if chain_text:
        chain_block = ("\n\n【这篇文章的逻辑链（第零轮通读全文得出的骨架，方向以它为准）】\n"
                       f"{chain_text}")
    return _with_thinking(_with_reasoning({
        "model": model,
        "messages": [
            {"role": "system", "content": MAP_RESCUE_SYSTEM_PROMPT},
            {"role": "user", "content": MAP_RESCUE_USER_TEMPLATE.format(
                topic=(topic or "").strip() or "（未命名主题）", material=material,
                linked=linked_text, isolated=words or "（无）",
                isolated_index=indexes or "（无）") + chain_block},
        ],
        "temperature": float(temperature),
        "stream": False,
        "response_format": {"type": "json_object"},
    }, reasoning_effort), thinking)


def build_map_payload(model: str, entries, *, topic: str = "",
                      material_chars: int = MAP_MATERIAL_CHARS,
                      max_entries: int = MAP_MAX_ENTRIES,
                      temperature: float = 0.0,
                      logic_chain=None,
                      reasoning_effort: str = "",
                      thinking: str = "") -> dict:
    """构造导图请求体：只含当前主题的词条材料 + 固定提示词。

    ``temperature`` 默认 **0.0**（曾经是 0.2）：导图是「同一份材料只该给同一张图」
    的确定性输出，界面上的「重新生成」不该把图洗一遍。采样随机性另有来源（思考
    模式），见 :func:`_with_thinking`。

    ``logic_chain``（批次 M24）是**第零轮**给出的逻辑链骨架（dict）或已经压好的
    前置认知文字（str）；给了就在用户消息最前面加一段「这篇文章是怎么串起来的」。
    它只定调方向（谁在前、谁依赖谁、谁构成谁），**不是**现成的边 —— 模型仍然要自己
    按材料提候选并给逐字证据。留空时用户消息与批次 M23 逐字一致（老缓存 / 老用例
    不受影响）。
    """
    material = build_map_material(entries, material_chars=material_chars,
                                  max_entries=max_entries)
    chain_text = (logic_chain if isinstance(logic_chain, str)
                  else build_logic_chain_material(logic_chain)).strip()
    if chain_text:
        user = (f"主题：{(topic or '').strip() or '（未命名主题）'}\n\n"
                "【这篇文章的逻辑链（第零轮已经通读全文得出的骨架，方向以它为准；"
                "你仍然要在下面的词条材料里找到支持每条关系的原文依据）】\n"
                f"{chain_text}\n\n"
                f"已保存词条（编号 / 词语 / 上下文 / 已有释义）：\n{material}")
    else:
        user = MAP_USER_TEMPLATE.format(
            topic=(topic or "").strip() or "（未命名主题）", material=material)
    return _with_thinking(_with_reasoning({
        "model": model,
        "messages": [
            {"role": "system", "content": MAP_SYSTEM_PROMPT},
            {"role": "user", "content": user},
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
                      temperature: float = 0.0,
                      logic_chain=None) -> list:
        """让模型对**当前主题的已保存词条**提出候选关系（返回原始候选列表）。

        ``entries`` 由 ``MapService`` 构造：``[{"index", "term", "context",
        "explanation"}, ...]``，只含当前主题的词条。这里只负责发请求 + 顶层结构
        校验；端点、类型、依据、环等业务校验全部在 ``MapService`` 里做。

        ``temperature`` 默认 0.0，并带上 ``thinking``（DeepSeek 系默认关掉思考
        模式）：导图必须**确定**，同一份材料重新生成要给同一张图。

        ``logic_chain``（批次 M24）是第零轮通读全文得出的逻辑链骨架，作为**前置
        认知**发下去（方向以它为准）；不传时行为与批次 M23 逐字一致。
        """
        payload = build_map_payload(self.model, entries, topic=topic,
                                    material_chars=material_chars,
                                    max_entries=max_entries,
                                    temperature=temperature,
                                    logic_chain=logic_chain,
                                    reasoning_effort=self.reasoning_effort,
                                    thinking=self.thinking)
        return parse_map_relations(self._extract_content(self._post(payload)))

    def map_isolated_rescue(self, entries, *, topic: str = "",
                            linked=(), isolated=(),
                            material_chars: int = MAP_MATERIAL_CHARS,
                            max_entries: int = MAP_MAX_ENTRIES,
                            temperature: float = 0.0,
                            logic_chain=None) -> list:
        """**补问**（批次 M23）：某个词被核对删成孤立词之后，只针对它再问一次。

        ``entries`` 与 :meth:`map_relations` 完全同一份（当前主题的词条材料）；
        ``linked`` / ``isolated`` 是 ``[(编号, 词语), ...]``。返回原始候选列表，
        仍然要交给 ``MapService`` 做端点 / 类型 / 证据校验与**第二次核对**才能画 ——
        本方法只负责发请求，绝不自己造边。
        """
        payload = build_map_rescue_payload(self.model, entries, topic=topic,
                                           linked=linked, isolated=isolated,
                                           material_chars=material_chars,
                                           max_entries=max_entries,
                                           temperature=temperature,
                                           logic_chain=logic_chain,
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

    # ------------------------------------------------- 逻辑链（第零轮，M24）
    def map_logic_chain(self, article: str, *, topic: str = "", terms=(),
                        temperature: float = 0.0) -> dict:
        """**第零轮**：先把整篇文章的逻辑链读出来（返回骨架 dict，认不出就是空骨架）。

        这一轮不可省略（用户口径：导图要准，就必须先让模型彻底理解文章链条）。
        它**只回答「文章是怎么串起来的」**，一条关系都不提；返回的骨架由
        ``MapService`` 压成第一轮的前置认知（``build_logic_chain_material``）。

        与其它几轮的材料边界不同：这一轮发的是**整篇正文**（受限输入里唯一一次），
        因为只发词条碎片时模型看不到文章 —— 方向写反就是这么来的。
        """
        payload = build_map_logic_chain_payload(self.model, article, topic=topic,
                                                terms=terms, temperature=temperature,
                                                reasoning_effort=self.reasoning_effort,
                                                thinking=self.thinking)
        return parse_map_logic_chain(self._extract_content(self._post(payload)))

    # ------------------------------------------------- 按原文归纳（文章逻辑）
    def article_logic(self, logic, *, title: str = "", temperature: float = 0.0,
                      vary: int = 0, reasoning_effort: str | None = None,
                      logic_chain: dict | str | None = None) -> dict:
        """读**整篇文章**，归纳出分支与关系（返回 ``parse_article_logic`` 的结果）。

        ``logic`` 由 ``MapService`` 构造：``{"text": 原文, "terms": 划过的词,
        "feedback": 上一轮差异清单}``。和 ``map_relations`` 的区别是：这里看的是
        文章本身（而不是每个词的 240 字上下文），所以能归纳出「这篇文章在讲什么」。

        ``vary`` = 思维阶梯档位（0 = 标准做法）；``reasoning_effort`` 不传就用
        客户端自己的（``None`` 与空串是两回事：空串 = 真的不发送这个字段）。
        ``logic_chain`` = 第零轮读全文得出的骨架（批次 M24），留空时用户消息逐字
        等于老文本。
        """
        effort = self.reasoning_effort if reasoning_effort is None else reasoning_effort
        payload = build_article_logic_payload(
            self.model, logic, title=title, temperature=temperature,
            reasoning_effort=effort, thinking=self.thinking, vary=vary,
            logic_chain=logic_chain)
        return parse_article_logic(self._extract_content(self._post(payload)))

    def full_article_map(self, *, stage: str, text: str, selected_words=(),
                         draft=None, reading=None, feedback=()) -> dict:
        """全文图：独立读原文 / 生成主题树 / 回原文复核，各阶段无共享对话历史。"""
        from .article_map import ArticleMapError, parse_object, payload
        try:
            request = payload(self.model, stage=stage, text=text,
                              selected_words=selected_words, draft=draft,
                              reading=reading, feedback=feedback,
                              reasoning_effort=self.reasoning_effort,
                              thinking=self.thinking)
            return parse_object(self._extract_content(self._post(request)))
        except ArticleMapError as exc:
            raise ApiError("schema", str(exc)) from exc

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
