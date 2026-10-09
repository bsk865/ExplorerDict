"""AI 参考关系图服务：受限输入 → 结构化候选 → 本地校验 → 独立核对 → 缓存 → UI。

边界（用户明确要求，勿放宽）
---------------------------
1. **只有用户显式动作**才会请求：打开导图窗、切主题、点「生成 / 重新生成」。
   程序不会在后台偷偷为别的主题发请求。
2. 请求体只含**当前主题**的已保存词条：词语 + 截断后的上下文 + 截断后的释义。
   不带整篇文档、不带窗口标题 / 网址 / 路径、不带其它主题、不带内部 ``entry_id``
   （发给模型的是本次材料的 0..n-1 编号）。
3. 模型给的只是**候选**，要过两道关才能画出来：

   * **本地校验**：端点必须是本次给出的编号、类型必须在白名单、必须带一句简短
     依据 + **逐字**证据片段；证据不能只是把端点词语重复一遍、也不能是两三个字
     的短片段。端点越界 / 自环 / 类型非法 / 依据为空 / 证据在材料里找不到或太弱 /
     重复 / **层级互相包含** / 包含·属于成环 —— 一律筛除并计数。
     （依赖 / 因果的双向边是**真实反馈**，不在这里删：它们经核对支持后由布局的
     强连通分组 + 反馈跨边表达。）
   * **独立核对（第二次调用）**：把候选的端点词 / 上下文 / 释义、具体方向、依据与
     证据交给同一份**冻结配置**的客户端再问一次，只接受明确 ``supported`` 的边；
     ``uncertain`` / ``contradicted`` / 核对里缺这条判定 / 判定字段坏掉 —— 一律
     不画、不缓存。核对阶段**不能新增边**（只按序号回填判定，多出来的序号忽略）；
     核对调用本身失败（网络 / 顶层格式）→ 整个结果按错误处理，可重试。

   依据不足就是空关系：**绝不**强行连线、**绝不**默认「相关」。参考关系永远只是
   AI 参考，不声称「已证明为真」。
4. 结果按 ``(主题, 内容指纹)`` 缓存，并记录 ``validation_version``：指纹由该主题
   词条的 id/term/context/释义 + 模型配置算出，新词 / 改词 / 换模型都会得到不同
   指纹；校验版本不同（例如旧版只做过字串证据校验）的缓存**一律不命中**。缓存里
   存的是已通过本地校验 + 独立核对的关系，读回时再解析一次，坏数据当没有缓存。
5. 迟到结果带 ``(token, topic_id, fingerprint)`` 交回 UI，UI 自己比对当前主题与指纹；
   服务侧只把结果写进它自己的缓存键，绝不跨主题覆盖。
6. 本模块**不碰 Tk**：结果通过回调交给调用方（``App`` 入 UI 队列，UI 线程消费）。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field, replace

from .api_client import (
    ARTICLE_ENTITY_CHARS, ARTICLE_LOGIC_TYPES, LOGIC_TYPES as _API_LOGIC_TYPES,
    MAP_MATERIAL_CHARS, MAP_MAX_ENTRIES, ApiError, article_vary,
    logic_chain_is_empty,
)
from .article_source import MIN_ARTICLE_CHARS
from .config import effort_choices, model_signature
from .logging_setup import get_logger, redact

log = get_logger("map")

#: 允许的关系类型（提示词、校验、缓存共用同一份白名单）。
#: 前六种是「词与词」的老关系（批次 C 起就在用，**不动**）；后两种「时序 / 对策」
#: 是「按原文归纳」新增的 —— 归纳出来的线（步骤先后、问题→办法）能直接落在图上，
#: 不用硬塞进「因果」里（那会把「先做 A 再做 B」画成「A 导致 B」，是错的）。
REL_TYPES: tuple[str, ...] = ("包含", "属于", "依赖", "用途", "因果", "对照",
                              "时序", "对策")
#: 层级关系：参与「谁在谁上面」的分组
HIERARCHY_TYPES = frozenset({"包含", "属于"})
#: 有向关系：参与前后分层
DIRECTIONAL_TYPES = frozenset({"依赖", "因果", "时序", "对策"})
#: 跨边关系：不参与分层，只画带方向的跨边
CROSS_TYPES = frozenset({"用途", "对照"})
#: 对称关系：A↔B 与 B↔A 视为同一条（不算矛盾）
SYMMETRIC_TYPES = frozenset({"对照"})
#: 层级 / 方向约束（参与成环检测）
CONSTRAINT_TYPES = HIERARCHY_TYPES | DIRECTIONAL_TYPES
#: 「流程边」：材料把两个词说成「先做 A 再做 B」时用的类型。同一个词对之间已经有
#: 这类边了，层级边（包含 / 属于）就一定是把流程步骤误当成整体—部分（批次 M24 的
#: 本地硬判据，见 :func:`is_flow_step_pair`）。
FLOW_TYPES = DIRECTIONAL_TYPES

#: 依据 / 证据片段的长度上限（超出只截断，不改写、不编造）
REASON_CHARS = 60
EVIDENCE_CHARS = 120
#: 证据片段的最小有效长度（规范化之后按字符算）。
#: 2 个字太松 —— 「卷积」这种把端点词抄一遍的片段会被当成依据，因此提到 6 个字，
#: 并且额外禁止「证据整个包含在端点词语里」这种纯重复（见 :func:`evidence_supported`）。
MIN_EVIDENCE_CHARS = 6

#: 缓存里的**校验版本**：候选 → 本地校验 → 独立核对 这一版校验的编号。
#: 只有版本一致的缓存才允许直接展示；旧的「只做字串证据校验」缓存读到也不命中。
#: v3：新增「按原文归纳」这条流水线（六种文章逻辑类型 + 分支/实体/关系校验）。
#: v4：关系类型红线（流程步骤之间不许写「包含 / 属于」）+ 核对放宽时序/因果的证据
#:     口径 + 删边兜底（孤立词补问）。同一份材料在这版下画出来的图与 v3 不同，
#:     因此 v3 的缓存**一律不命中**，要重新生成才对得上。
#: v5：生成流程最前面加**第零轮「逻辑链分析」**（先通读全文写出骨架，骨架作为
#:     第一轮的前置认知；用户要求这一步不可省略）。方向定调变了 ⇒ 同一份材料在
#:     v4 与 v5 下画出来的图会不同，v4 缓存**一律不命中**。
MAP_VALIDATION_VERSION = 5

#: 删边兜底最多为几个孤立词补问（批次 M23）。实测一轮补问只救 0～1 个词，
#: 但每个词 = 一次候选调用 + 一次核对调用，因此设硬上限，绝不让兜底把一次
#: 生成的开销翻几倍。
RESCUE_MAX_ORPHANS = 3

#: 第零轮「逻辑链分析」发出去的正文上限（字符，批次 M24）。
#: 这一轮存在的意义就是让模型看见全文，但也不能把论文级长文整篇发出去 ——
#: 4000 字足够覆盖用户实际在用的短文，超长时按 :func:`app.api_client.build_article_material`
#: 在段落边界截断，并在材料里如实标注「（正文过长，以下省略）」。
LOGIC_CHAIN_TEXT_CHARS = 4000

#: 核对判定（唯一权威定义在 :mod:`app.api_client`，这里只做白名单）
VERDICT_SUPPORTED = "supported"
MAP_VERDICTS = ("supported", "uncertain", "contradicted")

#: 每种关系的**方向语义**（发给第二次核对调用的「具体方向」短句）。
#: 与提示词里的定义逐字一致：核对看的是「这个方向 + 这个类型」是否被材料支持。
REL_DIRECTION_TEXT = {
    "包含": "前件是后件的上位概念 / 整体（后件包含在前件里）",
    "属于": "前件是后件的下位概念 / 组成部分",
    "依赖": "前件依赖后件，后件是前件的前提 / 基础",
    "用途": "前件用于后件",
    "因果": "前件导致 / 造成后件",
    "对照": "前件与后件形成对照 / 对比",
    "时序": "前件在文章中出现在后件之前（同一条主线上的先后两步）",
    "对策": "后件是针对前件（问题 / 需求）提出的办法 / 措施",
}

RESULT_OK = "ok"
RESULT_ERROR = "error"

# ---------------------------------------------------- 按原文归纳（文章逻辑）
#: 一篇文章的**主逻辑类型**（判断这篇文章在讲什么时用，由模型在 ``main_logic``
#: 里回一个）。**注意**：真正画线的类型是上面的 :data:`REL_TYPES`，两套是
#: 两回事 —— 文章是「时序」，画出来的线仍然叫「时序」（:data:`REL_TYPES` 已含）。
LOGIC_TYPES: tuple[str, ...] = ("时序", "因果", "对策", "层级", "依赖", "对比")
#: 有先后顺序的三种：它们的边**必须顺着分支顺序走**（逆着走 = 把文章的脉络讲反了）。
LOGIC_ORDERED_TYPES = frozenset({"时序", "因果", "对策"})
#: 至少要留几个分支才算归纳（只有一条分支 = 没分，等于没归纳）。
LOGIC_MIN_BRANCHES = 2
#: 一次归纳最多重试几轮（每轮都要重新问模型，花的是用户的钱，超过就如实报错）。
LOGIC_MAX_ATTEMPTS = 2
#: 实体「像原文」的判定阈值：至少这个比例的实体要能逐字出现在原文里。
LOGIC_TEXT_MATCH_MIN = 0.6


@dataclass(frozen=True)
class LogicRelation:
    """归纳出来的一条边（端点是**实体名**，不是 entry_id —— 原文里的词未必在词库里）。"""

    src: str
    dst: str
    rel_type: str
    reason: str = ""

    def as_dict(self) -> dict:
        return {"src": self.src, "dst": self.dst,
                "type": self.rel_type, "reason": self.reason}


@dataclass(frozen=True)
class LogicGraph:
    """一篇文章归纳出来的**骨架**：分支（谁和谁一组、谁先谁后）+ 边。"""

    doc_key: str
    title: str = ""
    main_logic: str = ""
    branches: tuple[dict, ...] = ()
    relations: tuple[LogicRelation, ...] = ()
    warnings: tuple[str, ...] = ()
    #: 生成这份归纳时的模型签名（缓存用；换了模型就该重算）。
    model_config: str = ""
    #: 「重新生成」的第几档思路（批次 M21-B）：0 = 标准做法。落进 ``raw`` 那一列，
    #: 读缓存时由 :func:`_with_provenance` 认回来，界面才说得出这一次换了什么。
    vary: int = 0
    vary_label: str = ""

    def branch_of(self, entity: str) -> int | None:
        """这个实体在第几个分支里（找不到 → None）。"""
        name = str(entity or "").strip()
        for index, branch in enumerate(self.branches):
            if name in tuple(branch.get("entities") or ()):
                return index
        return None

    def branch_names(self) -> tuple[str, ...]:
        return tuple(str(branch.get("name") or "") for branch in self.branches)

    def entity_count(self) -> int:
        return sum(len(tuple(branch.get("entities") or ())) for branch in self.branches)

    def as_dict(self) -> dict:
        return {
            "doc_key": self.doc_key,
            "title": self.title,
            "main_logic": self.main_logic,
            "branches": [dict(branch) for branch in self.branches],
            "relations": [rel.as_dict() for rel in self.relations],
            "warnings": list(self.warnings),
            "model_config": self.model_config,
            "vary": int(self.vary or 0),
            "vary_label": self.vary_label,
        }


def canonical_term(name, terms) -> str:
    """把一个实体名对齐到**用户自己的词条**上；对不上就原样返回（纯函数）。

    批次 M22-C 的实测病根之一：模型在归纳时写的是原文里的**完整说法**
    （「项目任务拆解」「反向调整项目需求」「重新拆解任务」），而用户划下来的是
    **简写**（「任务拆解」「确定项目需求」）。两边一对不上，
    :func:`logic_to_map_relations` 就把这些边全当「涉及不在本主题的词」丢掉 ——
    用户那篇 210 字的文章归纳出 7 条关系，最后**只画得出 2 条**，图当然不像话。

    对齐规则（宁缺勿滥，只认**唯一**的候选）：

    * 完全一样 → 就是它；
    * 只有一个词条**被它包住**（去掉标点空白后比，如「项目任务拆解」包住
      「任务拆解」）→ 用那个词条的名字；
    * 有多个候选、或一个都没有 → **原样返回**。猜错一个词比丢一条边更糟：
      图会把关系画到错误的卡片上。

    **方向是单向的**：只认「模型写得比词条长」，不认「词条比模型写得长」。
    反过来收会把**文章里另一个真的不一样的说法**吞掉 —— 实测那篇 210 字的文章
    同时讲了「项目开发」和「项目开发体系」，两者共 4 个字，按互相包含去收就会合并
    成一个词，于是同一个词条在两个分支里各出现一次，图上也少了一张卡片。
    """
    raw = str(name or "").strip()
    if not raw:
        return raw
    words: list[str] = []
    for term in terms or ():
        word = str(term or "").strip()
        if word and word not in words:
            words.append(word)
    if raw in words:
        return raw
    probe = normalize_material(raw)
    if not probe:
        return raw
    hits = []
    for word in words:
        other = normalize_material(word)
        # 只收「模型写了全称、用户划的是简称」这一种（长名包住短名）。
        if other and other in probe:
            hits.append(word)
    if len(hits) == 1:
        return hits[0]
    return raw


def canonicalize_logic(graph, terms):
    """把归纳结果里的实体名**整份对齐**到用户的词条上（纯函数，返回新的图）。

    与 :func:`canonical_term` 同一套规则；分支里的实体名和每条关系的两端一起换，
    换完还认不出来的保持原样（:func:`logic_to_map_relations` 会如实记「没画上去」）。
    在 :func:`run_logic_graph` 拿到结果后立刻做这一步：**后面所有环节**
    （画图、按原文分层、落库的 raw）看到的都是同一份名字，不会再出现
    「分支里写的是全称、关系连的是简写」这种自相矛盾。
    """
    words = list(terms or ())
    if not words:
        return graph
    branches = []
    #: 对齐之后可能撞名（模型写「项目任务拆解」、另一个分支写「任务拆解」，都收成
    #: 同一个词），这里顺手去重：一个词只能待在一个分支里，**保留第一次出现**，
    #: 顺序不变。不做这一步的话，同一个词会出现在两个分组框里。
    placed: set[str] = set()
    for branch in _graph_field(graph, "branches"):
        if not isinstance(branch, dict):
            continue
        item = dict(branch)
        members: list[str] = []
        for name in list(branch.get("entities") or ()):
            word = canonical_term(name, words)
            if word and word not in placed:
                placed.add(word)
                members.append(word)
        if not members:
            continue
        item["entities"] = members
        branches.append(item)
    relations = []
    for rel in _graph_field(graph, "relations"):
        if isinstance(rel, dict):
            src = str(rel.get("src") or "")
            dst = str(rel.get("dst") or "")
            rel_type = str(rel.get("type") or "")
            reason = str(rel.get("reason") or "")
        else:
            src = str(getattr(rel, "src", "") or "")
            dst = str(getattr(rel, "dst", "") or "")
            rel_type = str(getattr(rel, "rel_type", "") or "")
            reason = str(getattr(rel, "reason", "") or "")
        relations.append(LogicRelation(src=canonical_term(src, words),
                                       dst=canonical_term(dst, words),
                                       rel_type=rel_type, reason=reason))
    return LogicGraph(
        doc_key=str(getattr(graph, "doc_key", "") or ""),
        title=str(getattr(graph, "title", "") or ""),
        main_logic=str(getattr(graph, "main_logic", "") or ""),
        branches=tuple(branches),
        relations=tuple(relations),
        warnings=tuple(getattr(graph, "warnings", ()) or ()),
        #: 原样带过去：``model_config`` 在现场是**字符串**（模型名 / 配置摘要），
        #: 不是字典 —— 别在这里自作主张转类型。
        model_config=getattr(graph, "model_config", ""),
        vary=int(getattr(graph, "vary", 0) or 0),
        vary_label=str(getattr(graph, "vary_label", "") or ""),
    )


def logic_text_of(article) -> str:
    """从「articles 表的一行」或普通字符串里取出正文（取不到就是空串）。"""
    if article is None:
        return ""
    if isinstance(article, str):
        return article
    try:
        return str(article["text"] or "")
    except (IndexError, KeyError, TypeError):  # pragma: no cover - 老库缺列
        return ""


def validate_logic(data, *, text: str = "", terms=(), entities=(),
                   main_logic: str = "") -> dict:
    """**确定性**校验一份归纳输出（纯函数：零 token、无幻觉）。

    这是这条流水线的第四步：不问模型「你觉得行不行」，而是拿代码算。
    返回 ``{"branches", "relations", "problems", "warnings", "ok", "matches"}``：

    * ``problems`` 里有任何一条 ⇒ ``ok=False``，调用方带着这份清单去重试或如实报错；
    * ``warnings`` 只是提醒（实体名过长、原文里找不到这个说法），不挡画图；
    * ``matches`` 是「能逐字在原文里找到的实体比例」，低于
      :data:`LOGIC_TEXT_MATCH_MIN` 说明模型在编词，直接算错。
    """
    raw = data or {}
    problems: list[str] = []
    warnings: list[str] = []
    #: 归一化后的原文：判断「这个词是不是原文里真有」全靠它（空 = 没有原文可比）。
    body = normalize_material(text)

    branches: list[dict] = []
    seen_entities: set[str] = set()
    for item in list(raw.get("branches") or ()):
        if not isinstance(item, dict):
            problems.append("分支不是对象")
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            problems.append("有一个分支没有名字")
            continue
        if name in ("其它", "其他", "杂项", "补充"):
            problems.append(f"分支名「{name}」太笼统：请按文章的逻辑阶段命名")
            continue
        members: list[str] = []
        for entity in list(item.get("entities") or ()):
            word = str(entity).strip()
            if not word:
                continue
            if word in seen_entities:
                warnings.append(f"「{word}」在多个分支里重复出现，只保留第一次")
                continue
            seen_entities.add(word)
            members.append(word)
        if not members:
            warnings.append(f"分支「{name}」是空的，已去掉")
            continue
        branches.append({"name": name, "entities": members})

    if len(branches) < LOGIC_MIN_BRANCHES:
        problems.append(f"只归纳出 {len(branches)} 个分支，至少要 {LOGIC_MIN_BRANCHES} 个")
    names = [branch["name"] for branch in branches]
    if len(set(names)) != len(names):
        problems.append("有两个分支名字一样")

    known = set(seen_entities)
    for entity in known:
        if len(entity) > ARTICLE_ENTITY_CHARS:
            warnings.append(f"「{entity}」超过 {ARTICLE_ENTITY_CHARS} 个字，建议用文章里的短说法")

    # 编词检查（第一道）：分支里的词必须能在原文里**逐字**找到。这是硬约束 ——
    # 归纳出来的词一旦原文里没有，画到图上就是把读者往文章外面带。
    absent = sorted(entity for entity in known
                    if not body or normalize_material(entity) not in body)
    if absent:
        problems.append(
            "这些词在原文里找不到（不许自己造词，原文怎么说就怎么写）："
            + "、".join(f"「{entity}」" for entity in absent))
        if not body:
            problems.pop()          # 没有原文时下面已经有更准确的一条，别重复报

    relations: list[LogicRelation] = []
    seen_edges: set[tuple[str, str, str]] = set()
    for item in list(raw.get("relations") or ()):
        if not isinstance(item, dict):
            problems.append("有一条关系不是对象")
            continue
        src = str(item.get("src") or "").strip()
        dst = str(item.get("dst") or "").strip()
        rel_type = str(item.get("type") or "").strip()
        reason = str(item.get("reason") or "").strip()[:REASON_CHARS]
        where = f"{src or '？'}→{dst or '？'}"
        if rel_type not in LOGIC_TYPES:
            problems.append(f"{where} 的关系类型「{rel_type or '空'}」不在允许的六种里")
            continue
        if not src or not dst:
            problems.append(f"{where} 的起点或终点是空的")
            continue
        if src == dst:
            problems.append(f"{where} 是自己连自己")
            continue
        if body:
            for name in (src, dst):
                if name not in known and normalize_material(name) not in body:
                    problems.append(f"{where} 用到了原文里找不到的词「{name}」")
        elif src not in known or dst not in known:
            problems.append(f"{where} 用到了不在任何分支里的词")
        if not reason:
            warnings.append(f"{where} 没有写依据，已按「无依据」标注")
        key = (src, dst, rel_type)
        if key in seen_edges:
            warnings.append(f"{where}（{rel_type}）重复，已去掉一条")
            continue
        seen_edges.add(key)
        relations.append(LogicRelation(src=src, dst=dst, rel_type=rel_type, reason=reason))

    # 逆行检查：有时序的三类边必须顺着分支顺序走，否则就是把文章的脉络讲反了。
    #
    # 批次 M22-B 的修正（这条改动**只放宽闭环**，不放过真讲反）：
    # 文章本身常是**循环**的 —— 本机那篇 210 字的测试文就是
    # 「确定项目需求 → 任务拆解 → 项目复盘 → 循环迭代 →（回）确定项目需求」。
    # 旧的检查把回边一律判成「讲反了」，模型被要求改，于是干脆把关系全删掉：
    # 真机上第 1 轮 5 条关系被判死、第 2 轮只剩 6 条且丢掉整条循环，
    # 最后界面静默退回「按词条关系生成」—— 用户看到的就是那张竖成一列的图。
    #
    # 现在的口径：**只有闭环里的那一条**才算「反馈」 —— 它在关系图里本来就有
    # 一条反着走得到的路径，画出来是一条反馈弧，不是错误；而一条孤立的回边
    # （反方向根本走不通）仍然是「讲反了」，照旧算错。
    flow = logic_flow(branches, terms)
    rank = {name: index for index, name in enumerate(flow)}

    def _reaches(start: str, goal: str) -> bool:
        """关系图里从 ``start`` 顺着走能不能到 ``goal``（纯函数，无副作用）。"""
        stack = [start]
        seen = {start}
        while stack:
            node = stack.pop()
            for other in after.get(node, ()):
                if other == goal:
                    return True
                if other not in seen:
                    seen.add(other)
                    stack.append(other)
        return False

    after: dict[str, set] = {}
    for rel in relations:
        if rel.rel_type not in LOGIC_ORDERED_TYPES:
            continue
        after.setdefault(rel.src, set()).add(rel.dst)

    backwards = []
    for rel in relations:
        if rel.rel_type not in LOGIC_ORDERED_TYPES:
            continue
        if rel.src not in rank or rel.dst not in rank:
            # 只归纳出一部分词是正常的（划到的词不一定都被写进分支）：
            # 端点在主线之外就**没法判断先后**，提醒一句，不许当成「讲反了」。
            warnings.append(f"{rel.src}→{rel.dst}（{rel.rel_type}）有一端不在分支里，"
                            f"没法判断先后")
            continue
        if rank[rel.src] > rank[rel.dst]:
            if _reaches(rel.dst, rel.src):
                # 闭环：文章讲的是一个循环，这条是回边（反馈弧）。如实说，不算错。
                warnings.append(
                    f"{rel.src}→{rel.dst}（{rel.rel_type}）是一条回边 —— "
                    f"「{rel.dst}」本来就在「{rel.src}」前面，这里说的是循环回到开头，"
                    f"画成反馈弧")
                continue
            backwards.append(f"{rel.src}→{rel.dst}（{rel.rel_type}）")
    if backwards:
        problems.append("这些关系与分支顺序相反（文章的先后被讲反了）：" + "、".join(backwards))

    # 编词检查：实体必须能在原文里逐字找到
    if body and known:
        found = sum(1 for entity in known if normalize_material(entity) in body)
        matches = found / len(known)
        if matches < LOGIC_TEXT_MATCH_MIN:
            problems.append(
                f"只有 {found}/{len(known)} 个词能在原文里逐字找到"
                f"（低于 {int(LOGIC_TEXT_MATCH_MIN * 100)}%），像是在编造原文里没有的说法")
    else:
        matches = 0.0
        if not body:
            problems.append("没有原文可比对，不能凭空归纳")

    # 「连不上的词」检查（批次 M22-C，用户的第三条判据）：
    # 用户划过、也出现在分支里的词，如果**一条关系都没有**，它在图上就只是一张
    # 孤零零的卡片 —— 实测那篇 210 字的文章里「循环迭代」明明承接在最后一步，
    # 却因为模型没写它和别人的关系而飘在旁边（用户原话：「漏掉循环迭代和其他
    # 卡片的关联，把它识别成孤立词汇」）。
    #
    # 这一条算 problem（会让 :func:`run_logic_graph` 带着清单重试一轮），但
    # ``run_logic_graph`` 在轮次用完时**照样画图**、只把清单降级成 warnings：
    # 宁可让用户看到「这张卡还没接上」的提示，也不许因为一句话把整张图吞掉。
    # 只盯**用户亲手划过的词**（``terms`` 那一份）：分支里的实体是模型自己拆的，
    # 多几句「顺带提到」的话很正常，不该逼它给每句话都编一条关系。划过的词不一样
    # —— 那是用户点名要看的东西，飘在图上就是漏了。
    # 注意不能用 ``flow``（那是**分支里的**词，模型拆多少就有多少），必须是传进来的
    # ``terms``：漏掉的词往往正好**没**被模型写进任何分支，只认 flow 就永远看不见它。
    marked: list[str] = []
    marked_norm: set[str] = set()
    for item in terms or ():
        word = str(item or "").strip()
        if word and word not in marked:
            marked.append(word)
            marked_norm.add(normalize_material(word))
    wired = {rel.src for rel in relations} | {rel.dst for rel in relations}
    wired_norm = {normalize_material(name) for name in wired}
    loose = [name for name in marked
             if name in known and name not in wired
             and normalize_material(name) not in wired_norm]
    if loose:
        problems.append(
            "这些词没有连上任何关系（漏掉了它们在文章里的关联）："
            + "、".join(f"「{name}」" for name in loose))

    declared = str(main_logic or raw.get("main_logic") or "").strip()
    if declared and declared not in LOGIC_TYPES:
        warnings.append(f"主逻辑类型「{declared}」不在六种里，已忽略")
        declared = ""

    return {
        "branches": branches,
        "relations": relations,
        "problems": problems,
        "warnings": warnings,
        "ok": not problems,
        "matches": matches,
        "main_logic": declared,
    }


def logic_feedback(result) -> str:
    """把校验结果里的问题压成**给模型看**的差异清单（纯函数）。"""
    data = result or {}
    lines = [f"- {item}" for item in list(data.get("problems") or ())]
    lines.extend(f"- （提醒）{item}" for item in list(data.get("warnings") or ()))
    return "\n".join(lines)


def run_logic_graph(client, *, doc_key: str = "", title: str = "", text: str,
                    terms=(), max_attempts: int = LOGIC_MAX_ATTEMPTS,
                    model_config: str = "", plan=None,
                    logic_sink: dict | None = None) -> LogicGraph:
    """跑「按原文归纳」：问模型 → **代码校验** → 不合格就带着差异清单重试。

    这是用户要的那条流水线的第 3、4、5 步，也是**唯一**会花 token 的地方。
    每一步都留痕：

    * 契约不符（不是 JSON / 没有 branches）⇒ 记一条问题后重试；
    * 校验不通过 ⇒ 把 ``problems`` 原样回给模型，让它改，最多 ``max_attempts`` 轮；
    * 用完轮次还不合格 ⇒ 返回最后一份带 ``problems`` 的结果（``relations`` 与
      ``branches`` 保留，界面据此如实说明「这一版没通过校验」），**绝不假装成功**。

    ``plan`` 是 :func:`vary_plan` 给的换思路方案（批次 M21-B）：``vary`` 决定
    追加哪段「换一种想这件事的方式」的指令，``temperature`` 与 ``effort`` 决定
    这一档有多发散 / 想多深。不传 = 第 0 档（老口径，提示词一字不加）。

    ``logic_sink`` 是**第零轮逻辑链**的出口（批次 M24）：传了就在归纳之前先跑一次
    逻辑链分析，把骨架写进 ``logic_sink["chain"]`` 并随之作为归纳那一轮的前置认知
    发下去；**不传就一次都不跑**（老口径，调用序列一字不变）。为什么留这个开关：
    这条流水线自己就通读了全文，但「读懂逻辑链」必须是一个看得见、留得下的步骤
    （用户口径：不可省略），所以由调用方决定要不要留痕，而不是偷偷多花一次钱。
    """
    attempts = max(1, int(max_attempts))
    body = str(text or "")
    #: 划过的词（允许直接传词条行；只取名字 —— 提示词、校验、主线都用这一份）。
    names = [str(getattr(item, "term", item) or "").strip() for item in (terms or ())]
    names = [name for name in names if name]
    chain: dict = {}
    if logic_sink is not None and callable(getattr(client, "map_logic_chain", None)):
        # ★ 第零轮（批次 M24）：先把逻辑链读懂、写下来，再谈画哪条线。这一步失败
        # 不算归纳失败 —— 拿不到骨架就按批次 M23 的老路径继续（``logic_sink`` 里
        # 留空 = 界面说「这一次没走到逻辑链」，不编造骨架）。
        try:
            candidate = client.map_logic_chain(body, topic=str(title or ""),
                                               terms=tuple(names))
            if isinstance(candidate, dict) and not logic_chain_is_empty(candidate):
                chain = dict(candidate)
        except Exception:  # noqa: BLE001 - 第零轮绝不把一次归纳变成错误
            log.exception("逻辑链分析失败（按原文归纳）：按老路径继续 doc_key=%s", doc_key)
        logic_sink["chain"] = dict(chain)
    plan = dict(plan or {})
    step = int(plan.get("vary") or 0)
    label, _approach, _temperature = article_vary(step)
    temperature = float(plan.get("temperature") or 0.0)
    effort = str(plan.get("effort") or "")
    last: dict = {"branches": [], "relations": [], "problems": [], "warnings": [],
                  "ok": False, "matches": 0.0, "main_logic": ""}
    feedback = ""
    for attempt in range(attempts):
        logic = {"text": body, "terms": list(names), "feedback": feedback}
        if not callable(getattr(client, "article_logic", None)):
            # 网关没有这个方法（老替身 / 老客户端）：如实报一条契约错误，
            # 而不是让 AttributeError 冒到兜底那一层去变成「未知错误」。
            last = dict(last, problems=["模型客户端不支持按原文归纳"])
            log.info("归纳第 %d 轮：客户端没有 article_logic", attempt + 1)
            break
        try:
            raw = client.article_logic(logic, title=title, vary=step,
                                       temperature=temperature,
                                       reasoning_effort=effort,
                                       logic_chain=(chain or None))
        except ApiError as exc:
            if exc.kind != "bad_response":
                # 网络 / 鉴权 / 限流这类失败重试也是白花用户的钱：如实报错，不重试。
                last = dict(last, problems=[f"模型调用失败：{exc.display()}"])
                log.info("归纳第 %d 轮调用失败（不重试）：%s", attempt + 1, exc.display())
                break
            # 模型这次没按契约输出 —— 那是它自己的错，把要求再说一遍让它重写。
            result = {"branches": [], "relations": [], "problems": [str(exc)],
                      "warnings": [], "ok": False, "matches": 0.0, "main_logic": ""}
            last = result
            feedback = logic_feedback(result)
            log.info("归纳第 %d 轮契约不符：%s（%s）", attempt + 1, exc,
                     "重试" if attempt + 1 < attempts else "已达重试上限")
            continue
        result = validate_logic(raw, text=body, terms=names)
        last = result
        if result["ok"]:
            log.info("归纳第 %d 轮通过校验：%d 个分支 / %d 条关系",
                     attempt + 1, len(result["branches"]), len(result["relations"]))
            break
        # 「某个词一条关系都没连上」是**可以接受**的：图照样画得出来，用户至少
        # 能看到哪张卡空着（见 validate_logic 里那一段）。为它多花一轮 token 不值，
        # 于是把它从 problems 降级成 warnings 后直接收工 —— 但**别的问题一条都不放过**。
        only_loose = bool(result["problems"]) and all(
            "没有连上任何关系" in item for item in result["problems"])
        if only_loose:
            loose = list(result["problems"])
            last = dict(result, warnings=list(result["warnings"]) + loose,
                        problems=[], ok=True)
            log.info("归纳第 %d 轮只剩「连不上的词」（%d 条），降级成提醒后直接画图",
                     attempt + 1, len(loose))
            break
        feedback = logic_feedback(result)
        log.info("归纳第 %d 轮没通过校验（%d 个问题），%s",
                 attempt + 1, len(result["problems"]),
                 "重试" if attempt + 1 < attempts else "已达重试上限")
    graph = LogicGraph(
        doc_key=str(doc_key or ""),
        title=str(title or ""),
        main_logic=str(last.get("main_logic") or ""),
        branches=tuple(last.get("branches") or ()),
        relations=tuple(last.get("relations") or ()),
        warnings=tuple(last.get("warnings") or ()),
        model_config=str(model_config or ""),
        vary=step,
        #: 第 0 档就是「标准做法」，不必给它起名字：界面只在真的换过思路时才说。
        vary_label=str(label or "") if step else "",
    )
    #: 批次 M22-C：模型写的是原文里的**完整说法**（「项目任务拆解」），用户划下来
    #: 的是**简写**（「任务拆解」）—— 两边对不上，这些边就会在落图那一步被当
    #: 「涉及不在本主题的词」整条丢掉（实测 7 条只画得出 2 条）。在这里一次性把
    #: 实体名对齐到用户自己的词上，画图、分层、落库看到的都是同一份名字。
    return canonicalize_logic(graph, names)


def _graph_field(graph, name: str):
    """从 ``LogicGraph`` 对象或它的 ``dict`` 形态里取一个字段（取不到给空元组）。

    为什么两种都收：归纳结果一路会以 ``dict`` 形态出现（落库的 ``raw``、测试夹具、
    界面回调），而生成侧手里是 ``LogicGraph``。这些纯函数不该因为「给的是字典」
    就把整篇归纳当成空的。
    """
    if isinstance(graph, dict):
        return graph.get(name) or ()
    return getattr(graph, name, ()) or ()


def logic_rel_pairs(graph) -> list[dict]:
    """把归纳出来的边转成 ``(src, dst, type)`` 三元组清单（纯函数，给测试与界面用）。"""
    pairs: list[dict] = []
    for rel in _graph_field(graph, "relations"):
        src = rel.get("src") if isinstance(rel, dict) else getattr(rel, "src", "")
        dst = rel.get("dst") if isinstance(rel, dict) else getattr(rel, "dst", "")
        kind = (rel.get("type") if isinstance(rel, dict)
                else getattr(rel, "rel_type", ""))
        reason = (rel.get("reason") if isinstance(rel, dict)
                  else getattr(rel, "reason", ""))
        pairs.append({"src": src, "dst": dst, "type": kind, "reason": reason})
    return pairs


def logic_to_map_relations(graph, nodes) -> tuple[list[MapRelation], list[str]]:
    """归纳结果 → 现有导图能画的关系（只保留**两端都在本主题词库里**的边）。

    为什么可以丢：这张图要落在词条卡片上，两端得是真词条。丢掉的边会被列进
    第二条返回值（界面如实写「有 N 条关系涉及不在本主题的词，没有画上去」），
    **不静默吞掉**。
    """
    by_term: dict[str, int] = {}
    for node in nodes or ():
        name = str(getattr(node, "term", "") or "").strip()
        if name and name not in by_term:
            by_term[name] = int(getattr(node, "entry_id", 0) or 0)
    relations: list[MapRelation] = []
    skipped: list[str] = []
    seen: set[tuple[int, int, str]] = set()
    for rel in _graph_field(graph, "relations"):
        if isinstance(rel, dict):
            src_name = str(rel.get("src") or "").strip()
            dst_name = str(rel.get("dst") or "").strip()
            rel_type = str(rel.get("type") or "")
            reason = str(rel.get("reason") or "")
        else:
            src_name = str(getattr(rel, "src", "") or "").strip()
            dst_name = str(getattr(rel, "dst", "") or "").strip()
            rel_type = str(getattr(rel, "rel_type", "") or "")
            reason = str(getattr(rel, "reason", "") or "")
        src = by_term.get(src_name)
        dst = by_term.get(dst_name)
        if not src or not dst or src == dst or rel_type not in REL_TYPES:
            skipped.append(f"{src_name}→{dst_name}（{rel_type}）")
            continue
        key = (src, dst, rel_type)
        if key in seen:
            continue
        seen.add(key)
        relations.append(MapRelation(
            src_entry_id=src, dst_entry_id=dst, rel_type=rel_type,
            reason=reason[:REASON_CHARS],
            evidence=reason[:EVIDENCE_CHARS],
        ))
    return relations, skipped


def logic_levels(graph, nodes) -> dict[int, int]:
    """每个词条落在第几个分支（界面用它分层摆放；``-1`` = 不在任何分支里）。"""
    by_term: dict[str, int] = {}
    for node in nodes or ():
        name = str(getattr(node, "term", "") or "").strip()
        if name and name not in by_term:
            by_term[name] = int(getattr(node, "entry_id", 0) or 0)
    levels: dict[int, int] = {}
    for index, branch in enumerate(_graph_field(graph, "branches")):
        if not isinstance(branch, dict):
            continue
        for entity in list(branch.get("entities") or ()):
            entry_id = by_term.get(str(entity).strip())
            if entry_id and entry_id not in levels:
                levels[entry_id] = index
    for node in nodes or ():
        entry_id = int(getattr(node, "entry_id", 0) or 0)
        levels.setdefault(entry_id, -1)
    return levels


def article_levels(graph, nodes) -> tuple[dict[int, int], dict[int, int]]:
    """**按原文的逻辑给词条分层**：一张图该横着排还是竖着排，由文章自己说了算。

    这是批次 M22-C 的核心（纯函数，只读，返回 ``(level, order)``）：

    * ``level[entry_id]``：这个词在文章的先后里排第几层（``依赖 / 因果 / 时序 /
      对策`` 这类**有方向**的边连起来的深度）。不在任何一条边上的词记 ``-1``
      （调用方按「孤立词」单独摆，与老口径一致）。
    * ``order[entry_id]``：同一个层里的左右次序 —— 按**分支在主逻辑里的先后**，
      再按分支内实体的先后（就是文章的讲述顺序）。分支本来就是「这一段讲什么」，
      因此同分支的词自然排在一起。

    为什么要它：老口径只数「第几个分支」（``logic_levels`` 返回分支序号），于是
    「项目开发流程」这个分支里的四个词全被压在第 0 层、竖着摞成一列 —— 用户看到的
    「一根竖柱」就是这么来的（图里那条链其实是横向的流程）。

    **成环照样有解**：文章讲一个循环时（需求→拆解→复盘→迭代→回需求），深搜遇到
    自己在栈上就停手，回边不参与抬高深度 —— 于是主链照旧铺开、回边在图上成为一条
    反馈弧，而不是把整张图判成「讲反了」后丢掉。
    """
    by_term: dict[str, int] = {}
    for node in nodes or ():
        name = str(getattr(node, "term", "") or "").strip()
        if name and name not in by_term:
            by_term[name] = int(getattr(node, "entry_id", 0) or 0)

    # 同一层的左右次序：分支顺序 → 分支内实体顺序（即文章的讲述顺序）。
    order: dict[int, int] = {}
    for branch in _graph_field(graph, "branches"):
        if not isinstance(branch, dict):
            continue
        for entity in list(branch.get("entities") or ()):
            entry_id = by_term.get(str(entity).strip())
            if entry_id and entry_id not in order:
                order[entry_id] = len(order)

    onward: dict[int, list[int]] = {}
    for rel in _graph_field(graph, "relations"):
        if isinstance(rel, dict):
            src_name = str(rel.get("src") or "").strip()
            dst_name = str(rel.get("dst") or "").strip()
            rel_type = str(rel.get("type") or "")
        else:
            src_name = str(getattr(rel, "src", "") or "").strip()
            dst_name = str(getattr(rel, "dst", "") or "").strip()
            rel_type = str(getattr(rel, "rel_type", "") or "")
        # 摆位置用的是**四类有方向的边**（:data:`DIRECTIONAL_TYPES`），比校验用的
        # ``LOGIC_ORDERED_TYPES`` 多一个「依赖」：模型常把同一条先后写成「依赖」
        # （实测真机那版第 2 条就是 `确定项目需求→任务拆解（依赖）`）。校验看的是
        # 「有没有把文章的顺序讲反」，依赖不算先后，所以那边不收；但**摆位置**只要
        # 知道谁在谁后面就够了，漏掉「依赖」会让下游的词掉回第 0 层、整条流程散开。
        if rel_type not in DIRECTIONAL_TYPES:
            continue
        src = by_term.get(src_name)
        dst = by_term.get(dst_name)
        if not src or not dst or src == dst or dst in onward.get(src, ()):
            continue
        onward.setdefault(src, []).append(dst)

    touched = {entry_id for entry_id in order}
    for src, targets in onward.items():
        touched.add(src)
        touched.update(targets)

    depth: dict[int, int] = {}

    # 先把**回边**挑出来（M22-C 实测修正）：文章讲一个循环时（需求→拆解→复盘→
    # 迭代→回需求），最后那条「回到开头」的边如果也算进深度，最早讲的词会被抬到
    # 最后一层 —— 实测「确定项目需求」被抬到第 3 层，横向的流程线于是读不出先后。
    # 判据是深搜时的栈：走回一个还在栈上的点 = 这一条是回边，只画反馈弧、不算层。
    after: dict[int, list[int]] = {}
    for src, targets in onward.items():
        for dst in targets:
            item = after.setdefault(src, [])
            if dst not in item:
                item.append(dst)
    back: set[tuple[int, int]] = set()
    state: dict[int, int] = {}                 # 0 = 正在走，1 = 已经走完

    def _mark(node: int, stack: frozenset) -> None:
        state[node] = 0
        for child in after.get(node, ()):
            if child in stack:
                back.add((node, child))
            elif state.get(child) is None:
                _mark(child, stack | {node})
        state[node] = 1

    # 从哪个词开始深搜，决定环在哪一条边上被切断 —— 必须按**文章讲述的先后**
    # 起步，断出来的才是「绕回开头」的那一条（实测：从「确定项目需求」起步会把
    # 「任务拆解→项目复盘」当回边切掉，于是流程线读不出先后）。
    for entry_id in sorted(after, key=lambda item: (order.get(item, 1 << 30), item)):
        if state.get(entry_id) is None:
            _mark(entry_id, frozenset())

    core: dict[int, list[int]] = {}
    for src, targets in after.items():
        for dst in targets:
            if (src, dst) in back:
                continue
            item = core.setdefault(src, [])
            if dst not in item:
                item.append(dst)

    # 无环之后就是**最长路径**（Kahn 拓扑序 + 逐层抬高）。
    # 入度同时按「第几条边」数，是为了让**成环的那些边**不至于让两个点永远进不了
    # 队列（进不去就等于没分层）：环里最先被文章讲到的那个词先入队，环上的边
    # 退化成「同一层 / 往回一条」，图不会崩，层号也还是文章的顺序。
    indegree: dict[int, int] = {entry_id: 0 for entry_id in touched}
    for src, targets in core.items():
        for dst in targets:
            indegree[dst] = indegree.get(dst, 0) + 1
    key_of = lambda item: (order.get(item, 1 << 30), item)   # noqa: E731 - 就地小排序键
    queue = sorted((entry_id for entry_id in touched if indegree.get(entry_id, 0) == 0),
                   key=key_of)
    while queue:
        node = queue.pop(0)
        for child in sorted(core.get(node, ()), key=key_of):
            depth[child] = max(depth.get(child, 0), depth.get(node, 0) + 1)
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
                queue.sort(key=key_of)

    level: dict[int, int] = {}
    for entry_id in touched:
        level[entry_id] = int(depth.get(entry_id, 0))
    for node in nodes or ():                   # 没被任何关系碰到的词：单独摆
        entry_id = int(getattr(node, "entry_id", 0) or 0)
        level.setdefault(entry_id, -1)
    return level, order


def logic_flow(branches, entries=(), *, prefer_terms: bool = False) -> list[str]:
    """把「分支里的实体」摊平成一条**唯一的主线**（纯函数）。

    规则：按分支顺序走；每个分支内部默认**原样保留模型给的先后** —— 因为这条主线
    同时是校验「有没有把先后讲反」的标尺（见 :func:`validate_logic` 的逆行检查），
    顺序必须来自模型自己的归纳，不能被我方改写。

    ``prefer_terms=True`` 时改成「用户划过的词排在本分支前面（按划词顺序），其余按
    模型给的顺序跟在后面」：这是**落位 / 分行**用的顺序，不是校验用的顺序。两者必须
    分开，否则用划词顺序去判「讲反了」会误伤（划词顺序与文章顺序本来就可以不一致）。

    ``entries`` 里给字符串或 ``(entry_id, term)`` 这类行都能用：取 ``.term``，
    取不到就当字符串本身。
    """
    order = {}
    if prefer_terms:
        # 注意：**不要**写 ``entries or ()`` —— 传进来的往往是一个字符串（一篇文章
        # 或一个词），它会被整体当成一个可迭代对象、逐字符拆开，排序就全错了。
        for index, item in enumerate(entries):
            name = str(getattr(item, "term", item) or "").strip()
            if name and name not in order:
                order[name] = index
    def _rank(name: str) -> tuple[int, int]:
        """划过的词（且确实在这个分支里）排 0，其余排 1；同档内按原顺序。"""
        if name in order:
            return (0, order[name])
        return (1, position[name])

    flat: list[str] = []
    for branch in list(branches or ()):
        members = [str(name).strip() for name in list((branch or {}).get("entities") or ())
                   if str(name).strip()]
        if order:
            position = {name: index for index, name in enumerate(members)}
            # 划过、并且**确实被归纳进这个分支**的词排前面（按划词顺序）；其余按
            # 模型给的先后跟着 —— 只把划过的词提前，不许因为「没划过」被挤到后面。
            members.sort(key=_rank)
        flat.extend(members)
    return flat


def normalize_material(text) -> str:
    """证据比对用的规范化：去掉空白与标点，只留字母 / 数字 / 汉字。

    纯函数；不做任何同义替换 —— 证据要么**逐字**出现在给出的材料里，要么不采信。
    """
    return "".join(ch for ch in str(text or "").lower() if ch.isalnum())


def evidence_supported(evidence, materials, *, terms=()) -> bool:
    """证据片段是否**逐字**出现在给出的材料里，且不是「只把词抄一遍」的弱证据。

    * 规范化后长度 < :data:`MIN_EVIDENCE_CHARS`（默认 6）→ 拒绝（两三个字的短片段
      不构成依据）；
    * 规范化后等于端点词语、或者整个被端点词语包含（例如证据就是「卷积」）→
      拒绝：重复词本身说明不了任何关系；
    * 其余情况必须逐字出现在材料里（子串匹配，不做同义替换）。
    """
    probe = normalize_material(evidence)
    if len(probe) < MIN_EVIDENCE_CHARS:
        return False
    for term in terms or ():
        word = normalize_material(term)
        if word and (probe == word or probe in word):
            return False
    for material in materials or ():
        if probe and probe in normalize_material(material):
            return True
    return False


def constraint_pair(rel_type: str, src_id: int, dst_id: int) -> tuple[int, int] | None:
    """把一条关系翻译成层级 / 方向约束 ``(before, after)``。

    这是**唯一**一处「关系类型 → 谁在前」的权威口径：界面分层摆放直接调它，
    免得界面里再写一份 if / else 然后和新加的类型漂移（``时序`` / ``对策`` 就是
    这样漏过一次 —— ``edge_kind()`` 认它们是方向边，分层却不认，一条线画出来
    但两张卡片并排）。

    * 包含 A→B：A 是上位（A 在 B 之前）；
    * 属于 A→B：B 是上位（B 在 A 之前）；
    * 依赖 A→B：B 是前提（B 在 A 之前）；
    * 因果 A→B：A 是因（A 在 B 之前）；
    * 时序 A→B：A 先发生（A 在 B 之前）；
    * 对策 A→B：问题 / 需求在前，办法在后（A 在 B 之前）；
    * 用途 / 对照：跨边，不产生层级约束（``None``）。
    """
    if rel_type == "包含":
        return (int(src_id), int(dst_id))
    if rel_type == "属于":
        return (int(dst_id), int(src_id))
    if rel_type == "依赖":
        return (int(dst_id), int(src_id))
    if rel_type in ("因果", "时序", "对策"):
        return (int(src_id), int(dst_id))
    return None


@dataclass(frozen=True)
class MapNode:
    """图上的一个词条（只来自**当前主题**的已保存数据）。"""

    entry_id: int
    term: str
    context: str = ""
    explanation: str = ""

    def materials(self) -> tuple[str, ...]:
        return (self.context or "", self.explanation or "")


@dataclass(frozen=True)
class MapRelation:
    """一条**已通过证据校验**的参考关系（仍然标注为 AI 参考，不是原文事实）。"""

    src_entry_id: int
    dst_entry_id: int
    rel_type: str
    reason: str = ""
    evidence: str = ""

    @property
    def symmetric(self) -> bool:
        return self.rel_type in SYMMETRIC_TYPES

    @property
    def constraint(self) -> tuple[int, int] | None:
        return constraint_pair(self.rel_type, self.src_entry_id, self.dst_entry_id)

    def as_dict(self) -> dict:
        return {
            "src": int(self.src_entry_id),
            "dst": int(self.dst_entry_id),
            "type": str(self.rel_type),
            "reason": str(self.reason or ""),
            "evidence": str(self.evidence or ""),
        }


#: 人工关系的来源标记（``origin`` 字段的唯一取值）。AI 关系不写这个字段。
MANUAL_ORIGIN = "user"


@dataclass(frozen=True)
class ManualRelation(MapRelation):
    """用户**手动添加 / 修正**的关系（人工最高优先级）。

    与 AI 关系走同一套布局与点击交互，只在 ``origin`` 上区分：
    :func:`is_manual` 为真时界面用加粗实线 + 「人工」前缀标注。重新生成关系图时
    **不参与**模型输出、也**绝不**被覆盖 —— 它是用户自己的判断，不是模型候选。
    """

    relation_id: int = 0
    note: str = ""
    origin: str = MANUAL_ORIGIN

    def as_dict(self) -> dict:
        data = super().as_dict()
        data.update({"id": int(self.relation_id), "origin": MANUAL_ORIGIN,
                     "note": str(self.note or "")})
        return data


def is_manual(rel) -> bool:
    """这条关系是不是用户人工添加的（AI 关系没有 ``origin`` 或为空）。"""
    return str(getattr(rel, "origin", "") or "") == MANUAL_ORIGIN


def manual_relations(rows, nodes, *, topic_id: int = 0) -> tuple[ManualRelation, ...]:
    """把数据库里的 ``relations`` 行转成 :class:`ManualRelation`（纯函数）。

    只保留**两端都在 ``nodes`` 里**的行：词条被删 / 不在当前主题时这条人工关系
    画不出来，也不该出现在依据区里（``nodes`` 是当前主题的节点快照）。
    """
    known = {int(getattr(node, "entry_id", 0) or 0) for node in nodes}

    def field(row, name: str) -> str:
        """按列名取字符串；行里没有这一列（老库 / 替身行）时给空串。"""
        try:
            return str(row[name] or "")
        except (KeyError, IndexError, TypeError, ValueError):
            return ""

    out: list[ManualRelation] = []
    seen: set[tuple[int, int, str]] = set()
    for row in rows or ():
        try:
            src = int(row["src_entry_id"])
            dst = int(row["dst_entry_id"])
            rid = int(row["id"])
        except (KeyError, IndexError, TypeError, ValueError):  # pragma: no cover - 坏行跳过
            continue
        if src == dst or src not in known or dst not in known:
            continue
        rel_type = field(row, "label").strip() or "相关"
        note = field(row, "note").strip()
        key = (src, dst, rel_type)
        if key in seen:
            continue
        seen.add(key)
        out.append(ManualRelation(src_entry_id=src, dst_entry_id=dst, rel_type=rel_type,
                                  reason=note, evidence="", relation_id=rid, note=note))
    out.sort(key=lambda rel: (int(rel.src_entry_id), int(rel.dst_entry_id), rel.rel_type))
    return tuple(out)


@dataclass(frozen=True)
class MapGraph:
    """一张参考关系图（关系 + 参与计算的词节点快照）。"""

    topic_id: int
    fingerprint: str
    relations: tuple[MapRelation, ...] = ()
    nodes: tuple[MapNode, ...] = ()
    dropped: dict = field(default_factory=dict)
    source: str = "ai"          # ai / cache / empty
    model_config: str = ""
    #: 核对的**每条判定**（含没通过的那些）：说明「这个词为什么是孤立词」。
    #: 老缓存没有这一列 → 空元组（界面按「这份缓存生成时还没记录判定」处理）。
    verdicts: tuple[dict, ...] = ()
    #: 这次生成里**沿用上次**的关系条数（材料没变、上次核对通过的边不丢）；
    #: 0 = 全新算的。见 :func:`carry_over_relations`。
    carried: int = 0
    #: 「按原文归纳」这条流水线的产物：这篇文章的主逻辑类型 + 分支（谁和谁一组、
    #: 谁先谁后）。为空 = 这张图是「按词条关系」生成的（没取到原文，或用户关了）。
    main_logic: str = ""
    branches: tuple[dict, ...] = ()
    #: 这张图是「重新生成」的第几档思路（批次 M21-B）：``0`` = 标准做法，
    #: ``1`` 起分别是「深挖一层」「换一种结构」「最大思考」。界面据此如实说明
    #: 「这一次换了什么」—— 说不出来就等于没换。
    vary: int = 0
    vary_label: str = ""
    #: 第零轮「逻辑链分析」的骨架（批次 M24）：``{"logic_type", "core", "concepts",
    #: "hierarchy", "temporal", "causal", "dependency", "contrast", "turning_points",
    #: "synthesis"}``。空 dict = 这一次没走到逻辑链（老客户端 / 没正文 / 调用失败），
    #: 由 :attr:`logic_chain_empty` 如实标注 —— 界面可以据此说清「这次是按老路径画的」。
    logic_chain: dict = field(default_factory=dict)
    logic_chain_empty: bool = True

    def relation_for(self, src_id: int, dst_id: int, rel_type: str) -> MapRelation | None:
        for rel in self.relations:
            if (rel.src_entry_id == int(src_id) and rel.dst_entry_id == int(dst_id)
                    and rel.rel_type == rel_type):
                return rel
        return None

    def verdicts_for(self, entry_id: int) -> tuple[dict, ...]:
        """与某个词有关的所有候选判定（前件或后件都算），按原始序号排序。"""
        target = int(entry_id)
        rows = [row for row in self.verdicts
                if int(row.get("src", 0) or 0) == target or int(row.get("dst", 0) or 0) == target]
        return tuple(sorted(rows, key=lambda row: int(row.get("index", 0) or 0)))

    def summary(self) -> str:
        dropped = dropped_summary(self.dropped)
        if not self.relations:
            base = "没有足够依据的关系"
        else:
            base = f"{len(self.relations)} 条参考关系"
        if self.carried:
            base = f"{base}（沿用上次 {self.carried} 条）"
        return f"{base}（已筛除：{dropped}）" if dropped else base


def dropped_summary(dropped: dict) -> str:
    """把筛除计数压成一句短标签（界面短状态用，不暴露内部细节）。"""
    labels = {
        "shape": "格式不合法",
        "endpoint": "端点不是本主题的词",
        "type": "关系类型不合法",
        "self": "自己连自己",
        "reason": "缺少依据",
        "evidence": "证据与材料不符",
        "duplicate": "重复",
        "contradiction": "层级互相包含",
        "cycle": "层级成环",
        "verify_uncertain": "核对不确定",
        "verify_contradicted": "核对认为不成立",
        "verify_bad": "核对无效判定",
        # 删边兜底（批次 M23）：这三个不是「被筛掉几条」，而是「为孤立词补问了几轮
        # / 救回几个」—— 措辞必须让人一眼看出这是补救，不是又筛掉了东西。
        "rescue_asked": "为孤立词补问",
        "rescue_supported": "补问答对并画上",
        "rescue_still_alone": "补问后仍没有关系",
        "logic_chain_empty": "这一次没走到逻辑链",
        # 第零轮骨架直接给的边 + 靠词语折回编号的候选（批次 M24）：同样是「救回来
        # 多少」，不是「筛掉多少」。
        "skeleton_edges": "第零轮骨架直接给的边",
        "endpoint_by_name": "按词语折回编号的候选",
        "candidate_names": "模型写成词语的候选",
        # 本地硬判据拦下的：同一对词之间已经有流程边，层级边就是把流程步骤误当成
        # 整体—部分（批次 M24，见 :func:`is_flow_step_pair`）。
        "flow_step": "流程步骤被误写成包含 / 属于",
    }
    parts = [f"{labels.get(key, key)} {int(value)}"
             for key, value in (dropped or {}).items() if int(value or 0) > 0]
    return "、".join(parts)


def _row_json(row, key: str):
    """从缓存行里安全地取一段 JSON（老库没有这一列 / 坏值一律当「没记录」）。"""
    try:
        text = row[key]
    except (IndexError, KeyError, TypeError):  # pragma: no cover - 老库缺列
        return None
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _load_dropped(row) -> dict:
    """缓存里的筛除计数（没记录 → 空 dict，界面就不说「已筛除 0 条」）。"""
    data = _row_json(row, "dropped")
    if not isinstance(data, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in data.items():
        try:
            count = int(value or 0)
        except (TypeError, ValueError):
            continue
        if count > 0:
            out[str(key)] = count
    return out


def _load_verdicts(row) -> tuple[dict, ...]:
    """缓存里的核对判定记录（没记录 → 空元组）。字段口径与 :func:`verdict_records` 一致。"""
    data = _row_json(row, "verdicts")
    if not isinstance(data, list):
        return ()
    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            src, dst = int(item.get("src", 0) or 0), int(item.get("dst", 0) or 0)
        except (TypeError, ValueError):
            continue
        if not src or not dst:
            continue
        out.append({
            "index": int(item.get("index", len(out)) or 0),
            "src": src,
            "dst": dst,
            "type": str(item.get("type") or ""),
            "reason": str(item.get("reason") or ""),
            "evidence": str(item.get("evidence") or ""),
            "verdict": str(item.get("verdict") or ""),
            "note": str(item.get("note") or ""),
            "kept": bool(item.get("kept")),
        })
    return tuple(out)


def _load_logic_chain(row) -> dict:
    """缓存里的逻辑链骨架（批次 M24；没记录 → 空 dict = 没走到逻辑链）。

    只认自己写进去的那一种形状（一个 JSON 对象）：老库那一列是空串、或者干脆
    不是 JSON，一律当「这一次没走到逻辑链」——**不编造**骨架。
    """
    data = _row_json(row, "logic_chain")
    return dict(data) if isinstance(data, dict) else {}


def _with_provenance(graph: MapGraph, row) -> MapGraph:
    """把「按原文归纳」的来龙去脉从缓存那一行里读回来（纯函数）。

    落库时 ``raw`` 那一列存的是归纳结果的 JSON（``main_logic`` / ``branches`` /
    ``relations``），这里只认出自己写的那一种形状；认不出（老口径存的候选原文、
    或者干脆不是 JSON）就原样返回 —— 界面上照样能画，只是说不出「按哪篇文章归纳的」。
    """
    data = _row_json(row, "raw")
    if not isinstance(data, dict):
        return graph
    branches = data.get("branches")
    if not isinstance(branches, list):
        return graph
    clean: list[dict] = []
    for item in branches:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        members = [str(word).strip() for word in list(item.get("entities") or ())
                   if str(word).strip()]
        if name and members:
            clean.append({"name": name, "entities": members})
    if not clean:
        return graph
    return replace(
        graph,
        source="article",
        main_logic=str(data.get("main_logic") or ""),
        branches=tuple(clean),
        vary=int(data.get("vary") or 0),
        vary_label=str(data.get("vary_label") or ""),
    )


def logic_vary_label(graph) -> str:
    """这张图是「重新生成」的第几档思路 → 界面用的一句短说明（没有 → 空串）。

    为什么要单独一个纯函数：档位一路会以 ``LogicGraph`` / ``MapGraph`` / 落库的
    ``dict`` 三种形态出现，界面不该自己猜是哪一种。
    """
    if isinstance(graph, dict):
        step = int(graph.get("vary") or 0)
        label = str(graph.get("vary_label") or "").strip()
    else:
        step = int(getattr(graph, "vary", 0) or 0)
        label = str(getattr(graph, "vary_label", "") or "").strip()
    if step <= 0:
        return ""
    return f"第 {step + 1} 档 · {label}" if label else f"第 {step + 1} 档"


def article_is_usable(article) -> bool:
    """这一行 **articles 留档**能不能拿来归纳（纯函数，界面与后台共用同一判据）。

    条件是「``status='ok'`` 且正文够长」（:data:`MIN_ARTICLE_CHARS`）。抓失败的那一行
    也留在库里（免得同一页每划一个词都重抓），它带着一句失败理由 —— 那句话**不是**
    文章，用它归纳等于凭空编。传字符串 / ``None`` 时只看有没有正文。
    """
    if article is None or isinstance(article, str):
        return bool(logic_text_of(article).strip())
    try:
        status = str(article["status"] or "").strip()
        text = str(article["text"] or "")
    except (IndexError, KeyError, TypeError):  # pragma: no cover - 老库缺列
        return False
    return status == "ok" and len(text.strip()) >= MIN_ARTICLE_CHARS


def relation_locally_valid(rel_type, reason, evidence, src_node, dst_node,
                           topic_materials=()) -> bool:
    """单条关系的**本地校验**（schema / 端点 / 依据 / 证据）—— 纯函数。

    本地校验与「核对之后再过一遍」共用同一份判据：类型在白名单、有非空依据、
    不是自环、证据**逐字**出现在材料里，且不是「把端点词语抄一遍」的弱证据。

    ``topic_materials``（批次 M24）是**本次主题所有词条**的上下文；给了它，证据
    允许来自本文任何一个词的上下文 —— 收口句（「A、B、C 共同构成 D」）常常写在
    **另一个词**的上下文里，只比两端那两段会把它整条丢掉。★ 这一层**必须**与
    :func:`validate_relations` / :func:`apply_verdicts` 用**同一个口径**：两处宽严
    不一致时会出现「本地放行、核对也判 supported，最后却按 `verify_bad` 丢掉」这种
    谁也说不清的结果（M24 真 Key 实测一次踩掉三条收口边）。
    """
    if rel_type not in REL_TYPES:
        return False
    if not str(reason or "").strip():
        return False
    if not str(evidence or "").strip():
        return False
    if int(src_node.entry_id) == int(dst_node.entry_id):
        return False
    materials = tuple(src_node.materials()) + tuple(dst_node.materials())
    terms = (str(getattr(src_node, "term", "") or ""), str(getattr(dst_node, "term", "") or ""))
    if evidence_supported(evidence, materials, terms=terms):
        return True
    if topic_materials:
        # 两端那两段里找不到，但**本文另一条词条**的上下文里有 → 照样算数；
        # 依据本身一点没放宽（仍然要求逐字出现在这篇材料里）。
        return evidence_supported(evidence, tuple(topic_materials), terms=terms)
    return False


def flow_step_pairs(candidates, nodes) -> tuple[set[tuple[int, int]], set[int]]:
    """候选里已经出现的**流程边**（纯函数）。

    返回 ``(pairs, members)``：``pairs`` 是流程边两端 ``entry_id`` 排序后的词对，
    ``members`` 是**上过流程链**的全部词 id。只按形状看：端点能对上、类型在
    :data:`FLOW_TYPES` 里、不是自环。用来做 :func:`is_flow_step_pair` 的判据 ——
    **先扫全批再判**，免得「层级边排在流程边前面」时判不出来（候选顺序是模型给的，
    不能当保证）。

    两种输入形状都吃（★ 实测踩过）：第一轮的**候选字典**用 ``"type"`` 键，
    **已成立的** :class:`MapRelation` 用 ``rel_type`` 字段 —— 沿用那道关传进来的是
    后者，只认 ``"type"`` 会静默返回空集合、沿用完全绕过关。

    ★ ``members`` 还要**向外扩一跳**（只走非层级边）：真 Key 实测「项目开发」自己
    没上过流程链（流程边全在它包含的那几个步骤之间），于是「项目开发 包含 X」一条
    都拦不住。凡是跟链上成员**有直接关系**的词，就已经是这条链的参与者了。
    扩的时候**不走层级边** —— 否则「项目开发体系 属于 项目开发」会把整体也拖进
    members，真正的整体—部分关系就再也画不出来了（这是刻意的取舍）。
    """
    nodes = list(nodes or [])
    pairs: set[tuple[int, int]] = set()
    members: set[int] = set()
    neighbours: dict[int, set[int]] = {}
    for item in list(candidates or []):
        if isinstance(item, dict):
            rel_type = item.get("type")
            src_index, dst_index = _as_index(item.get("src")), _as_index(item.get("dst"))
            if src_index is None or dst_index is None or not (
                    0 <= src_index < len(nodes) and 0 <= dst_index < len(nodes)):
                continue
            src_id = int(nodes[src_index].entry_id)
            dst_id = int(nodes[dst_index].entry_id)
        else:
            rel_type = getattr(item, "rel_type", "")
            src_id = int(getattr(item, "src_entry_id", -1))
            dst_id = int(getattr(item, "dst_entry_id", -1))
        rel_type = rel_type.strip() if isinstance(rel_type, str) else ""
        if src_id < 0 or dst_id < 0 or src_id == dst_id:
            continue
        if rel_type not in FLOW_TYPES:
            if rel_type in HIERARCHY_TYPES:      # 层级边不参与扩张（见 docstring）
                continue
            neighbours.setdefault(src_id, set()).add(dst_id)
            neighbours.setdefault(dst_id, set()).add(src_id)
            continue
        pairs.add(tuple(sorted((src_id, dst_id))))
        members.update((src_id, dst_id))
    frontier = set(members)
    while frontier:
        grown: set[int] = set()
        for entry_id in frontier:
            for other in neighbours.get(entry_id, ()):
                if other not in members:
                    members.add(other)
                    grown.add(other)
        frontier = grown
    return pairs, members


def is_flow_step_pair(rel_type, src_id, dst_id, flow_pairs) -> bool:
    """这条**层级**边是不是把流程步骤误当成了整体—部分（纯函数）。

    用户口径（批次 M24）：「**错误套用「包含」连接不同流程步骤（流程步骤不是包含
    从属）**」。只对 ``HIERARCHY_TYPES``（包含 / 属于）生效，两条判据：

    * ``flow_pairs`` 里有这一对（同一个词对之间已经有流程边）；
    * **「被包含」的那一端上过流程链**（``members``）—— 材料里已经在讲「先做 A 再
      做 B」了，它就是这条流程上的步骤，不能被说成是别的东西的一部分。真 Key 实测：
      模型每轮都提「项目开发 包含 确定项目需求 / 任务拆解 / 项目复盘」，而这一轮的
      流程边全在**这几个步骤之间** —— 只看「两边都在链上」会一条都拦不住。

    ★ 判据只看**被包含的那一端**，刻意不看包含端（实测踩过）：反过来判会把
    「过拟合 ← 卷积 包含」和「过拟合 ← 池化 依赖」这对组合一起误杀 —— 那两条都是好
    边（一条层级、一条流程），而「整体 包含 链上成员」恰恰是收口句要的形状。

    真正的整体—部分**不会**被误杀：收口句那个整体（「项目开发体系」）根本不在流程
    链上，它包含的三个步骤才是链上成员。没有流程边的层级边一律照旧放行。

    为什么非得写在本地：提示词（第一轮）与核对（第二轮）都立了这道红线，但真 Key
    实测**同一份材料三轮，模型每轮都提「项目开发 包含 X」、核对也每轮判 supported**
    —— 只有本地判据是不看运气的。
    """
    if rel_type not in HIERARCHY_TYPES:
        return False
    pair = tuple(sorted((int(src_id), int(dst_id))))
    pairs, members = flow_pairs if isinstance(flow_pairs, tuple) else (flow_pairs, set())
    if pair in (pairs or set()):
        return True
    return int(dst_id) in members


def validate_relations(candidates, nodes, *,
                       max_reason_chars: int = REASON_CHARS,
                       max_evidence_chars: int = EVIDENCE_CHARS
                       ) -> tuple[list[MapRelation], dict]:
    """把模型候选关系筛成**可画的候选**（纯函数，无 Tk、无 IO）。

    返回 ``(kept, dropped)``：``kept`` 保持候选原顺序，``dropped`` 是按原因分类的计数。
    规则见模块开头；核心是「端点必须是本次给出的词 + 必须带材料里逐字出现的证据」。
    这里产出的仍然只是**候选**：还要过第二次独立核对才能画 / 缓存。

    ★ 证据的取材范围（批次 M24）：**任何一条主题词条的上下文都算**，不只是两端
    那两个词的上下文。理由是这张图的材料本来就只有这一篇短文 —— 第零轮通读全文
    读出来的收口句（「A、B、C 共同构成 D」）常常写在**另一个词**的上下文里，只比
    两端那两段会把它整条丢掉；而「多认几个词」并不会放宽依据本身（仍然要求逐字
    出现在**这篇材料**里，也仍然要过第二次独立核对）。任务以外的材料一概不认。
    """
    nodes = list(nodes or [])
    dropped = {key: 0 for key in ("shape", "endpoint", "type", "self", "reason",
                                  "evidence", "duplicate", "contradiction", "cycle",
                                  "flow_step")}
    kept: list[MapRelation] = []
    seen: set = set()
    topic_materials: list[str] = []
    for node in nodes:
        topic_materials.extend(str(item) for item in node.materials())
    # ★ 流程边先扫全批（不能边判边攒：候选顺序是模型给的，层级边可能排在流程边前面）。
    flow_pairs = flow_step_pairs(candidates, nodes)

    for item in list(candidates or []):
        if not isinstance(item, dict):
            dropped["shape"] += 1
            continue
        src_index = _as_index(item.get("src"))
        dst_index = _as_index(item.get("dst"))
        if src_index is None or dst_index is None or not (
                0 <= src_index < len(nodes) and 0 <= dst_index < len(nodes)):
            dropped["endpoint"] += 1
            continue
        rel_type = item.get("type")
        rel_type = rel_type.strip() if isinstance(rel_type, str) else ""
        if rel_type not in REL_TYPES:
            dropped["type"] += 1
            continue
        src_node, dst_node = nodes[src_index], nodes[dst_index]
        if int(src_node.entry_id) == int(dst_node.entry_id):
            dropped["self"] += 1
            continue
        reason = item.get("reason")
        reason = " ".join(reason.split()) if isinstance(reason, str) else ""
        if not reason:
            dropped["reason"] += 1
            continue
        evidence = item.get("evidence")
        evidence = " ".join(evidence.split()) if isinstance(evidence, str) else ""
        if not relation_locally_valid(rel_type, reason, evidence, src_node, dst_node,
                                      topic_materials=topic_materials):
            # 判据全在 :func:`relation_locally_valid` 里（含「证据可以来自本文任何
            # 一条词条的上下文」这条 M24 口径）—— 这里不再自己写第二套，免得两处
            # 宽严不一致。
            dropped["evidence"] += 1
            continue
        if is_flow_step_pair(rel_type, int(src_node.entry_id), int(dst_node.entry_id),
                             flow_pairs):
            # ★ 本地硬判据（批次 M24）：同一个词对之间已经有先后 / 因果 / 依赖这类
            # **流程边**了，层级边就一定是把流程步骤误当成整体—部分 —— 用户原话
            # 「错误套用『包含』连接不同流程步骤（流程步骤不是包含从属）」。判据写
            # 在本地是因为提示词管不住：真 Key 实测同一份材料两轮，第二轮核对仍然
            # 把三条「项目开发 包含 X」判成 supported。
            dropped["flow_step"] += 1
            continue
        key = _relation_key(rel_type, int(src_node.entry_id), int(dst_node.entry_id))
        if key in seen:
            dropped["duplicate"] += 1
            continue
        seen.add(key)
        kept.append(MapRelation(
            src_entry_id=int(src_node.entry_id),
            dst_entry_id=int(dst_node.entry_id),
            rel_type=rel_type,
            reason=reason[: max(1, int(max_reason_chars))],
            evidence=evidence[: max(1, int(max_evidence_chars))],
        ))

    kept, conflicting = _drop_direction_conflicts(kept)
    dropped["contradiction"] += conflicting
    kept, cycles = _drop_layer_cycles(kept)
    dropped["cycle"] += cycles
    return kept, dropped


def touched_entry_ids(relations) -> set[int]:
    """这批关系**碰到的**词 id 集合（纯函数）。

    词在图上是不是「孤立词」，判据只有一个：它的 id 在不在这个集合里 ——
    界面侧用的是同一口径（``app/ui/concept_map.py`` 里
    ``isolated = [entry_id for entry_id in ids if entry_id not in touched]``）。
    批次 M23 的删边兜底（:meth:`MapService.rescue_orphan_relations`）也按它挑人。
    """
    touched: set[int] = set()
    for rel in relations or ():
        touched.add(int(getattr(rel, "src_entry_id", -1)))
        touched.add(int(getattr(rel, "dst_entry_id", -1)))
    touched.discard(-1)
    return touched


def _name_index(name: str, nodes) -> int | None:
    """把模型写的**词语**折回词条编号（纯函数，查不到返回 ``None``）。

    提示词要编号，实测模型仍会时不时把 ``src`` / ``dst`` 写成词语本身
    （批次 M24 真 Key 实测：五条候选全部写成名字）。以前这种候选一律按
    「端点越界」丢掉 —— 图上一条线都不剩，用户看到的就是「漏掉关联」。

    两级匹配，都要求**唯一命中**，宁可查不到也不猜：

    * 先**逐字**相等（去空白后）。这一级刻意不走 :func:`canonical_term` ——
      它会把「项目开发体系」折成「项目开发」，而这两个在文章里是**两个不同的
      说法**（M22-C 实测），同一条词条的两种写法不该被当成同一个词；
    * 再走 :func:`canonical_term` 的口径（模型写全称、词条是简称），且
      **只有一个**词条命中才算（「项目需求」能唯一折到「确定项目需求」；
      同时能折到两个词的就不折）。
    """
    key = " ".join(str(name or "").split())
    if not key:
        return None
    items = list(nodes or [])
    for index, node in enumerate(items):
        if str(getattr(node, "term", "") or "").strip() == key:
            return index
    names = [str(getattr(node, "term", "") or "") for node in items]
    folded = canonical_term(key, names)
    if not folded or folded == key:
        return None
    hits = [index for index, word in enumerate(names)
            if str(word or "").strip() == folded]
    return hits[0] if len(hits) == 1 else None


def resolve_candidate_endpoints(candidates, nodes) -> tuple[list, int]:
    """把候选里的端点**折回编号**（纯函数，无 Tk、无 IO）。

    返回 ``(fixed, resolved)``：``fixed`` 是端点已经能用编号表达的那部分候选
    （**逐条复制**，不动调用方拿到的原始对象），``resolved`` 是靠词语折回来的
    条数（写进 ``dropped`` 里如实告诉用户「这次有几条是这么救回来的」）。

    只动 ``src`` / ``dst``，其余字段一个字不改；本来编号就合法、以及彻底折
    不回来的候选**原样留着** —— 折这里不是为了放宽校验，只是把模型写词语这件
    事从「整条丢掉」降级成「照它说的那个词接上」，后面的端点 / 类型 / 证据 /
    核对一道都不少。
    """
    items = list(candidates or [])
    count = len(list(nodes or []))
    fixed: list = []
    resolved = 0
    for item in items:
        if not isinstance(item, dict):
            fixed.append(item)
            continue
        src, dst = item.get("src"), item.get("dst")
        src_ok = _as_index(src) is not None
        dst_ok = _as_index(dst) is not None
        if src_ok and dst_ok:
            fixed.append(item)
            continue
        row = dict(item)
        touched = False
        if not src_ok:
            found = _name_index(src, nodes)
            if found is not None:
                row["src"] = found
                touched = True
        if not dst_ok:
            found = _name_index(dst, nodes)
            if found is not None:
                row["dst"] = found
                touched = True
        if touched:
            resolved += 1
        fixed.append(row)
    return fixed, resolved


def _verbatim_evidence(basis, materials) -> str:
    """从模型写的 ``basis`` 里抠出**材料里真有**的那一段（纯函数）。

    模型写 basis 时经常把两句话用「；」「，」甚至「……」「...」连成一句（真 Key
    实测：`"首先要确定项目需求；然后是任务拆解。"`）。整段拿去比对必然通不过，于是
    这条骨架边就被「证据与材料不符」丢掉 —— 明明是它读对了、只是引用写得长。

    做法：先按标点把 basis 切成小段，每一段都在材料里**逐字**找一遍，返回**最长**
    的那一段的原文。★ 刻意不「在规范化后的 basis 里找最长子串」—— 真 Key 实测那样
    会返回 `'首先要确定项目需求；……；然后是任务拆解'`（两个「有效字符」段在
    basis 里相邻、在材料里却隔着标点），整段再拿去做逐字校验照样不通过。按段切就
    不会跨过标点。找不到（或太短）就返回空串，交给后面如实丢掉。
    """
    raw = str(basis or "")
    corpus = [normalize_material(item) for item in (materials or ())]
    corpus = [item for item in corpus if item]
    if not corpus:
        return raw.strip()
    pieces = [piece for piece in re.split(r"[^\w\u4e00-\u9fff]+", raw) if piece]
    best = ""
    for piece in pieces:
        probe = normalize_material(piece)
        if len(probe) < MIN_EVIDENCE_CHARS or len(probe) <= len(best):
            continue
        if any(probe in item for item in corpus):
            best = piece
    return best


def logical_canonical_candidates(chain, nodes, *,
                                 materials=()) -> tuple[list, dict]:
    """第零轮骨架 + 词条表 ⇒ 一批**端点确定**的候选关系（纯函数）。

    用户口径（批次 M24）：「大模型必须对文章的逻辑链条、层级划分、因果关系
    有清晰的认知和划分」，而且这份认知要**兜住**图 —— 所以第零轮读出来的
    关系（先后 / 因果 / 依赖 / 收口）不能只当提示词里的一段话，它本身就要
    变成候选：**先把骨架画出来，再让模型补材料依据**。

    只有端点能在词条表里**唯一**对上的骨架行才产出候选（对不上就由模型那条
    路去提，这里不猜）；同一条（类型 + 端点）只出一次。``materials`` 是本次
    主题所有词条的上下文，用来把 basis 抠成材料里真有的那一截（见
    :func:`_verbatim_evidence`）—— 不传就退化成老的「按句号切」口径。返回
    ``(candidates, counts)``，``counts`` 按类型计数（写进 ``dropped`` 如实
    告诉用户「其中几条是第零轮骨架直接给的」）。
    """
    items = list(nodes or [])
    if not items or not isinstance(chain, dict) or not chain:
        return [], {}
    corpus = [str(item) for item in (materials or ())]
    out: list = []
    counts: dict[str, int] = {}
    seen: set = set()

    def _pair(first, second) -> tuple[int, int] | None:
        a = _name_index(first, items)
        b = _name_index(second, items)
        if a is None or b is None or a == b:
            return None
        # ★ 给的是**本次材料里的位置**（0 起），不是 entry_id —— ``validate_relations``
        # 的 ``src`` / ``dst`` 是位置口径（实测把 entry_id 写进去会被整批判成
        # 「端点越界」，图上一条线都不剩）。
        return a, b

    def _add(kind: str, src_id: int, dst_id: int, reason: str, basis: str = "") -> None:
        key = (kind, src_id, dst_id)
        if key in seen:
            return
        evidence = _verbatim_evidence(basis, corpus) if corpus else ""
        if not evidence:
            # 没有材料可比对（或 basis 里抠不出材料里真有的那一截）时，退回老的
            # 「按句号 / 省略号切一段」口径 —— 总比把整句带「……」的拿去比对强。
            for piece in re.split(r"[.…]+|\.\.\.", str(basis or "")):
                piece = " ".join(piece.split())
                if len(normalize_material(piece)) > len(normalize_material(evidence)):
                    evidence = piece
        seen.add(key)
        out.append({"src": src_id, "dst": dst_id, "type": kind,
                    "reason": reason, "evidence": evidence})
        counts[kind] = counts.get(kind, 0) + 1

    for row in list(chain.get("temporal") or ()):
        if not isinstance(row, dict):
            continue
        pair = _pair(row.get("first"), row.get("then"))
        if pair:
            _add("时序", pair[0], pair[1], "第零轮通读全文读出来的先后顺序",
                 row.get("basis"))
    for row in list(chain.get("causal") or ()):
        if not isinstance(row, dict):
            continue
        pair = _pair(row.get("cause"), row.get("effect"))
        if pair:
            _add("因果", pair[0], pair[1], "第零轮通读全文读出来的因果关系",
                 row.get("basis"))
    for row in list(chain.get("dependency") or ()):
        if not isinstance(row, dict):
            continue
        # dependent 依赖 prerequisite ⇒ 方向是 prerequisite → dependent
        pair = _pair(row.get("prerequisite"), row.get("dependent"))
        if pair:
            _add("依赖", pair[0], pair[1], "第零轮通读全文读出来的依赖关系",
                 row.get("basis"))
    synthesis = chain.get("synthesis") or {}
    if isinstance(synthesis, dict):
        whole = str(synthesis.get("whole") or "").strip()
        whole_index = _name_index(whole, items)
        if whole_index is not None:
            for part in list(synthesis.get("parts") or ()):
                part_index = _name_index(part, items)
                if part_index is None or part_index == whole_index:
                    continue
                _add("属于", part_index, whole_index,
                     "收口句：这几部分共同构成这个整体",
                     f"{part}、{whole}共同构成")
    return out, counts


def logic_chain_article(nodes, *, limit: int = LOGIC_CHAIN_TEXT_CHARS) -> str:
    """把主题下各词条的**阅读上下文**拼回一篇文章（纯函数，给第零轮用）。

    为什么要拼：逻辑链这一轮（批次 M24）必须看见全文，否则「读懂文章链条」无从谈起。
    本机手头**只有**每个词的 240 字上下文 —— 那就是当初用户划词时看到的正文片段，
    把它们按词条顺序接起来，就是这篇文章能被还原出来的样子。

    做法：剥掉划词时加的「文章正文：」这类前缀，压掉空白，再去掉**被后一段包含**
    的重复片段（相邻片段经常大段重叠）。这不是原文副本，只要够读懂逻辑就够用 ——
    因此函数名与注释都不假装它是原文。
    """
    cap = max(0, int(limit))
    if cap <= 0:
        return ""
    pieces: list[str] = []
    for node in list(nodes or ()):
        text = str(getattr(node, "context", "") or "")
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r"^(文章正文|正文|原文)\s*[:：]\s*", "", text)
        if len(text) < 8:
            continue
        if any(text in piece for piece in pieces):
            continue
        pieces = [piece for piece in pieces if piece not in text]
        pieces.append(text)
    return " ".join(pieces)[:cap]


def build_labeled_map_material(entries, *, material_chars: int = MAP_MATERIAL_CHARS,
                               max_entries: int = MAP_MAX_ENTRIES) -> str:
    """给逻辑链那一轮用的词条材料：每行一个 ``[编号] 词语``（纯函数）。

    与 :func:`app.api_client.build_map_material` 的区别是**不带**上下文与释义 ——
    这一轮要的是「文章里划出来了哪些词」，把 240 字上下文再塞一遍只会让模型分心，
    而且逻辑链的输入本来就该是正文本身。
    """
    limit = max(0, int(max_entries))
    cut = max(1, int(material_chars))
    lines: list[str] = []
    for index, item in enumerate(list(entries or [])[:limit]):
        item = item or {}
        term = str(item.get("term") or "").strip()[:cut]
        if not term:
            continue
        lines.append(f"[{index}] {term}")
    return "\n".join(lines)


def apply_verdicts(relations, nodes, verdicts) -> tuple[list[MapRelation], dict]:
    """按**独立核对**（第二次调用）的判定筛边 —— 纯函数，只删不增。

    规则（对应模块开头第 3 条）：

    * 核对只能**按序号回填判定**：序号越界 / 不是整数 / 缺这条判定 / 同一条重复
      判定 / ``verdict`` 不在白名单 / 判定项不是对象 —— 一律按「没有明确支持」
      处理，**不画也不缓存**；
    * 核对阶段**绝不新增边**：本函数只在给定 ``relations`` 里挑，核对输出里多出来
      的内容一概忽略；
    * 判定为 ``supported`` 的边还要**再过一遍本地校验**（schema / 端点 / 依据 /
      证据 / 弱证据）才算数 —— 核对不能把一条本地已经不成立的边救回来；
    * ``uncertain`` / ``contradicted`` 分别计数，方便界面说清「筛掉了什么」。
    """
    nodes_by_id = {int(node.entry_id): node for node in (nodes or ())}
    counts = {key: 0 for key in ("verify_supported", "verify_uncertain",
                                 "verify_contradicted", "verify_bad")}
    # 证据的取材范围必须与 :func:`validate_relations` **一模一样**（M24）：收口句
    # 常写在别的词的上下文里，这里只比两端会把「本地放行、核对也判 supported」的
    # 边按 verify_bad 丢掉（真 Key 实测一次踩掉三条）。
    topic_materials: list[str] = []
    for node in (nodes or ()):
        topic_materials.extend(str(item) for item in node.materials())
    decisions: dict[int, str] = {}
    for item in list(verdicts or []):
        if not isinstance(item, dict):
            counts["verify_bad"] += 1
            continue
        index = _as_index(item.get("index"))
        verdict = item.get("verdict")
        verdict = verdict.strip() if isinstance(verdict, str) else ""
        if index is None or not (0 <= index < len(relations)) or verdict not in MAP_VERDICTS:
            counts["verify_bad"] += 1
            continue
        if index in decisions:            # 同一序号给了两次判定：第一次为准
            counts["verify_bad"] += 1
            continue
        decisions[index] = verdict

    kept: list[MapRelation] = []
    for index, rel in enumerate(relations):
        verdict = decisions.get(index)
        if verdict != VERDICT_SUPPORTED:
            counts["verify_uncertain" if verdict == "uncertain" else
                   "verify_contradicted" if verdict == "contradicted" else
                   "verify_bad"] += 1
            continue
        src_node = nodes_by_id.get(int(rel.src_entry_id))
        dst_node = nodes_by_id.get(int(rel.dst_entry_id))
        if src_node is None or dst_node is None or not relation_locally_valid(
                rel.rel_type, rel.reason, rel.evidence, src_node, dst_node,
                topic_materials=topic_materials):
            counts["verify_bad"] += 1
            continue
        counts["verify_supported"] += 1
        kept.append(rel)
    return kept, counts


def verdict_records(relations, nodes, verdicts) -> tuple[dict, ...]:
    """把核对的**每条判定**变成能落库、能显示的记录（纯函数，无 Tk、无 IO）。

    一条候选一行，键固定为：``index`` / ``src`` / ``dst`` / ``type`` / ``reason`` /
    ``evidence`` / ``verdict``（``""`` = 核对没给这条判定）/ ``note``（核对给的一句
    理由）/ ``kept``（这条最终有没有画进图）。

    :func:`apply_verdicts` 只给计数，答不了「这个词为什么成了孤立词」—— 这份记录
    就是那个问题的唯一依据，因此**必须与 :func:`apply_verdicts` 同一套判据**：
    同序号重复判定取第一次、越界 / 非整数 / 判定不在白名单一律当「没给判定」。
    这里直接复用它算出的保留集合，不另写一份规则（避免两处判定漂移）。
    """
    kept, _counts = apply_verdicts(relations, nodes, verdicts)
    kept_keys = {_relation_key(rel.rel_type, rel.src_entry_id, rel.dst_entry_id)
                 for rel in kept}
    decisions: dict[int, tuple[str, str]] = {}
    for item in list(verdicts or []):
        if not isinstance(item, dict):
            continue
        index = _as_index(item.get("index"))
        verdict = item.get("verdict")
        verdict = verdict.strip() if isinstance(verdict, str) else ""
        if index is None or not (0 <= index < len(relations)) or verdict not in MAP_VERDICTS:
            continue
        if index in decisions:            # 同一序号给了两次判定：第一次为准
            continue
        note = item.get("note")
        decisions[index] = (verdict, note.strip() if isinstance(note, str) else "")

    records: list[dict] = []
    for index, rel in enumerate(list(relations or [])):
        verdict, note = decisions.get(index, ("", ""))
        key = _relation_key(rel.rel_type, rel.src_entry_id, rel.dst_entry_id)
        records.append({
            "index": int(index),
            "src": int(rel.src_entry_id),
            "dst": int(rel.dst_entry_id),
            "type": str(rel.rel_type or ""),
            "reason": str(rel.reason or ""),
            "evidence": str(rel.evidence or ""),
            "verdict": verdict,
            "note": note,
            "kept": key in kept_keys,
        })
    return tuple(records)


#: 沿用上次判定时写进判定记录的说明（点词看原因时原样显示）
CARRY_NOTE = "沿用上次核对通过的判定（同一份材料，这次没重新提）"

#: 模型这次**又提了**这条边、但这一轮核对改判成不成立时的说明：
#: 同一份材料、上次已核对通过，就以上次为准（核对是整批一起判的，换一批候选
#: 结论会抖 —— 实测同一份材料两轮核对把同一条边判成 supported / contradicted）。
CARRY_OVERRIDE_NOTE = "沿用上次核对通过的判定（同一份材料，这次核对改判了，以上次为准）"


def carried_is_compatible(rel: MapRelation, kept) -> bool:
    """这条「沿用」的边和现有边放一起还成立吗（纯函数，只读）。

    沿用**绝不能动到已经画出来的边**：只要它与现有边构成「层级互相包含」的矛盾或
    不可能的层级环，就宁可不加（少画一条也不画矛盾）。依赖 / 因果的双向反馈不算
    矛盾，由布局阶段表达 —— 判据与 :func:`_drop_direction_conflicts` /
    :func:`_drop_layer_cycles` 完全一致，不另写一套规则。
    """
    trial = list(kept or []) + [rel]
    survivors, _conflicts = _drop_direction_conflicts(trial)
    if len(survivors) != len(trial) or rel not in survivors:
        return False
    survivors, _cycles = _drop_layer_cycles(survivors)
    if len(survivors) != len(trial) or rel not in survivors:
        return False
    return True


def carry_over_relations(previous, current, nodes, *, verdicts=None,
                         flow_pairs=None,
                         note: str = CARRY_NOTE,
                         override_note: str = CARRY_OVERRIDE_NOTE):
    """把**上一张图里已核对通过的关系**补回本轮（纯函数，只增不删）。

    为什么需要：提出阶段是模型采样，同一份材料重新生成也会漏掉上次提过的边 ——
    实测同一份材料连提三次是 6 / 5 / 5 条、并集 10 条，于是用户每点一次「重新生成」
    图就变一次，这是被明确抱怨的行为。只要材料没变（同一内容指纹）、端点还在本主题
    里、本地证据校验（:func:`relation_locally_valid`）照样通过，上次画出来的边就
    继续画。

    两条路径都要堵住：
    * **这次没提** ⇒ 直接把边补回来（判定合成 ``supported``，序号接在末尾）；
    * **这次又提了、但这一轮核对改判成不成立** ⇒ **以上次为准**，把这条边的判定
      换成 ``supported``（``override_note``）—— 核对是整批一起判的，换一批候选
      结论就会抖，不能让已经画出来的边因为「这次问出来不一样」就消失。

    ★ ``flow_pairs``（批次 M24）：本轮**已经成立的那些关系**算出来的流程边
    （:func:`flow_step_pairs` 的结果）。沿用的边同样要过「流程步骤不许写成包含 /
    属于」这道本地判据，否则它会**绕过把关从旧图里爬回来** —— 真 Key 实测就是这个
    形状：候选里 5 条被拦掉，图上却还剩 2 条「项目开发 包含 项目复盘 / 属于」，
    全是从上一次的缓存里沿用的。

    返回 ``(relations, verdicts, carried_keys)``：``verdicts`` 可直接与核对结果拼在
    一起交给 :func:`verdict_records` / :func:`apply_verdicts`（判定记录照样完整，点词
    仍能看到「这条为什么在图上」）；``carried_keys`` 是这次靠沿用才留在图上的
    ``_relation_key`` 集合 —— 调用方在核对之后再数一遍真正落在图上的条数，沿用计数
    才不会吹牛。
    """
    base = list(current or [])
    if previous is None or not getattr(previous, "relations", None):
        return base, list(verdicts or []), set()
    kept_now, _counts = apply_verdicts(base, nodes, verdicts or [])
    kept_keys = {_relation_key(rel.rel_type, rel.src_entry_id, rel.dst_entry_id)
                 for rel in kept_now}
    nodes_by_id = {int(node.entry_id): node for node in (nodes or ())}
    by_key = {_relation_key(rel.rel_type, rel.src_entry_id, rel.dst_entry_id): index
              for index, rel in enumerate(base)}
    extra: list[MapRelation] = []
    overrides: dict[int, dict] = {}
    carried_keys: set = set()
    for rel in previous.relations:
        if rel.rel_type not in REL_TYPES:
            continue
        src_node = nodes_by_id.get(int(rel.src_entry_id))
        dst_node = nodes_by_id.get(int(rel.dst_entry_id))
        if src_node is None or dst_node is None:      # 词被删了 / 不在本主题了
            continue
        key = _relation_key(rel.rel_type, rel.src_entry_id, rel.dst_entry_id)
        if key in kept_keys:
            continue                                   # 这轮照样画着，不用管
        if not relation_locally_valid(rel.rel_type, rel.reason, rel.evidence,
                                      src_node, dst_node):
            continue                                 # 材料改了、证据不再成立，就不画
        if flow_pairs is not None and is_flow_step_pair(
                rel.rel_type, int(rel.src_entry_id), int(rel.dst_entry_id), flow_pairs):
            continue                                 # 流程步骤被写成层级：连沿用都不许
        if not carried_is_compatible(rel, kept_now + extra):
            continue                                 # 与现有边矛盾 / 成环：宁可少画
        index = by_key.get(key)
        if index is None:                            # 这次没提：补一条
            index = len(base) + len(extra)
            extra.append(rel)
            overrides[index] = {"index": index, "verdict": VERDICT_SUPPORTED, "note": note}
        else:                                        # 又提了但被改判：以上次为准
            overrides[index] = {"index": index, "verdict": VERDICT_SUPPORTED,
                                "note": override_note}
        carried_keys.add(key)
    if not carried_keys:
        return base, list(verdicts or []), set()
    merged_verdicts = [item for item in (verdicts or [])
                       if not (isinstance(item, dict)
                               and _as_index(item.get("index")) in overrides)]
    merged_verdicts.extend(overrides[index] for index in sorted(overrides))
    return base + extra, merged_verdicts, carried_keys


def _as_index(value) -> int | None:
    """端点编号：只接受整数或纯数字字符串（其余一律当无效端点）。"""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return None


def _relation_key(rel_type: str, src_id: int, dst_id: int) -> tuple:
    """去重键：对称关系（对照）把 A→B 与 B→A 视为同一条。"""
    if rel_type in SYMMETRIC_TYPES:
        low, high = sorted((int(src_id), int(dst_id)))
        return (rel_type, low, high)
    return (rel_type, int(src_id), int(dst_id))


def _drop_direction_conflicts(relations: list[MapRelation]) -> tuple[list[MapRelation], int]:
    """筛掉「**层级互相包含**」的矛盾对（依赖 / 因果的反馈环保留）。

    * **包含 / 属于**：同一种层级关系在两个方向同时成立（A 包含 B 又 B 包含 A）
      是不可能的层级 → 两条都删，计入 ``contradiction``；
    * **依赖 / 因果**：A→B 与 B→A 是**真实存在的反馈**（互相依赖 / 互为因果）。
      经过第二次核对支持就要保留，由 :func:`app.ui.concept_map.layout_graph`
      用强连通分组 + 反馈跨边表达 —— 不再一概当矛盾删掉；
    * 对照本来对称（去重阶段已合并）、用途是跨边，都不在这里处理。
    """
    groups: dict[tuple, list[MapRelation]] = {}
    for rel in relations:
        if rel.rel_type not in HIERARCHY_TYPES:
            continue
        pair = tuple(sorted((int(rel.src_entry_id), int(rel.dst_entry_id))))
        groups.setdefault((rel.rel_type, *pair), []).append(rel)
    bad_ids: set[int] = set()
    dropped = 0
    for group in groups.values():
        directions = {(int(r.src_entry_id), int(r.dst_entry_id)) for r in group}
        if len(directions) > 1:
            for rel in group:
                bad_ids.add(id(rel))
            dropped += len(group)
    return [rel for rel in relations if id(rel) not in bad_ids], dropped


def _drop_layer_cycles(relations: list[MapRelation]) -> tuple[list[MapRelation], int]:
    """排除**不可能的层级环**：只有 包含 / 属于 之间的环才算环。

    * 包含 / 属于：A 在 B 之上、B 在 C 之上、C 又在 A 之上不可能成立 → 按候选顺序
      保留不成环的边，其余计入 ``cycle``，绝不伪装成层级；
    * 依赖 / 因果：即使成环（互相依赖 / 互为因果）也是**真实反馈**，经核对支持后
      一律保留 —— 分层由 :func:`app.ui.concept_map.layout_graph` 的强连通分组负责，
      既不伪造层级，也不把反馈当错误删掉。
    """
    adjacency: dict[int, set[int]] = {}
    kept: list[MapRelation] = []
    dropped = 0
    for rel in relations:
        if rel.rel_type not in HIERARCHY_TYPES:
            kept.append(rel)
            continue
        pair = rel.constraint
        if pair is None:                  # pragma: no cover - 层级类型一定有约束
            kept.append(rel)
            continue
        before, after = pair
        if before == after:
            dropped += 1
            continue
        if _reachable(adjacency, after, before):
            dropped += 1
            continue
        adjacency.setdefault(before, set()).add(after)
        kept.append(rel)
    return kept, dropped


def _reachable(adjacency: dict[int, set[int]], start: int, target: int) -> bool:
    """邻接表上 start 能否到达 target（DFS，纯函数）。"""
    stack = [int(start)]
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        if node == int(target):
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(adjacency.get(node, ()))
    return False


def explanation_text(row) -> str:
    """词条的「释义」= one_line + detail（读库行 → 纯文本）。"""
    parts = []
    for key in ("one_line", "detail"):
        try:
            value = row[key]
        except (IndexError, KeyError, TypeError):
            value = ""
        text = str(value or "").strip()
        if text:
            parts.append(text)
    return "\n".join(parts)


def vary_plan(step: int, *, reasoning_effort: str = "", model: str = "") -> dict:
    """把「第几次重新生成」翻译成一份**明确的换思路方案**（纯函数）。

    用户的实测反馈：点「重新生成」之前，这里其实什么都没变 —— 同一段提示词、
    同一个模型、``temperature=0``，第二次必然拿到一模一样的图，画面上一个像素
    都不动。所以重新生成必须**真的换一次思考方式**，这份方案就是那次改变：

    * ``vary``：阶梯档位（进指纹、进状态行、决定追加哪段换思路指令）；
    * ``temperature``：第 0 档 0.0（同内容同结果），往后逐档放宽；
    * ``effort``：在用户设置的档位上**往上抬 ``vary`` 级**（设置里是「不发送」
      时按该模型的默认档起步 —— DeepSeek 默认 high），抬到顶就停在最高档；
    * ``label``：这一档的名字（界面如实写「第 N 次：深挖一层」）。
    """
    from .api_client import ARTICLE_VARY_MAX      # 避免模块级循环 import
    index = max(0, min(int(step or 0), ARTICLE_VARY_MAX))
    label, _approach, temperature = article_vary(index)
    tiers = [str(item or "") for item in effort_choices(model)]
    base = str(reasoning_effort or "").strip().lower()
    if not tiers:
        return {"vary": index, "temperature": float(temperature),
                "effort": base, "label": label}
    if base in tiers:
        pos = tiers.index(base)
        target = pos + index
    else:
        # 用户手输的怪值（不在候选档里）：第 0 档原样不动，要升就一次升到最高档。
        target = 0 if not index else len(tiers) - 1
    return {"vary": index, "temperature": float(temperature),
            "effort": tiers[min(target, len(tiers) - 1)], "label": label}


def content_fingerprint(topic_id: int, nodes, *, base_url: str = "", model: str = "",
                        reasoning_effort: str = "", source: str = "",
                        vary: int = 0) -> str:
    """内容指纹：主题 + 每个词条的 (id, term, context, 释义) + 模型配置 (+ 生成方式 / 档位)。

    新词 / 改词 / 换模型 / 改推理强度 → 指纹变化 → 旧缓存不命中（过期检测）；
    同内容重复打开则稳定命中。词条按 id 排序，读库顺序变化不影响指纹。
    推理强度为空串时与旧口径逐字一致（老缓存不会因为这次改动失效）。

    ``source`` 是**生成方式**（``""`` = 老口径按词条关系；``"article:<doc_key>#<状态>"``
    = 按这篇文章的原文归纳）。为什么要算进指纹：同一批词「有原文」和「没有原文」
    会得到两张完全不同的图，若指纹相同，切回旧库时会把另一条路的图当成缓存直接
    显示 —— 那就成了拿旧图冒充新结果。默认空串保证老缓存照旧命中。

    ``vary`` 是「重新生成」的**思维阶梯档位**（批次 M21-B）。为什么要算进指纹：
    每一档是**另一张图**（提示词不同、``temperature`` 不同、推理强度也不同），
    共用一条指纹的话，从第 2 档退回第 1 档会把第 2 档的图当缓存显示 —— 又是
    「拿旧图冒充新结果」。默认 0 时 ``payload`` 里的键**逐个与老口径相同**，
    保证老缓存照旧命中。
    """
    entries = [
        {
            "id": int(node.entry_id),
            "term": str(node.term or ""),
            "context": str(node.context or ""),
            "explanation": str(node.explanation or ""),
        }
        for node in sorted(nodes or (), key=lambda item: int(item.entry_id))
    ]
    signature = model_signature(model, reasoning_effort)
    payload_data = {
        "topic_id": int(topic_id),
        "entries": entries,
        "base_url": str(base_url or "").strip().rstrip("/"),
        "model": signature,
        "source": str(source or ""),
    }
    step = int(vary or 0)
    if step:
        payload_data["vary"] = step
    payload = json.dumps(
        payload_data,
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class MapService:
    """导图服务：请求编排 + 证据校验 + 缓存隔离（可整体替换的实现边界）。"""

    def __init__(self, db, config, on_result=None):
        """:param on_result: ``(token, topic_id, fingerprint, status, graph, error)``。"""
        self.db = db
        self.config = config
        self._on_result = on_result
        self._lock = threading.RLock()
        self._inflight: set[tuple[int, str]] = set()
        self._next_token = 1

    # ------------------------------------------------------------- 回调
    def set_result_sink(self, callback) -> None:
        self._on_result = callback

    def _deliver(self, token: int, topic_id: int, fingerprint: str, status: str,
                 graph, error) -> None:
        cb = self._on_result
        if cb is None:
            return
        try:
            cb(int(token), int(topic_id), str(fingerprint), str(status), graph, error)
        except Exception:  # pragma: no cover - 回调异常不得打断服务
            log.exception("导图结果回调失败")

    # ------------------------------------------------------------- 客户端
    def make_client(self, *, base_url: str | None = None, model: str | None = None,
                    timeout: float | None = None, api_key: str | None = None,
                    reasoning_effort: str | None = None,
                    thinking: str | None = None):
        """构造客户端（测试替换本方法注入假客户端，绝不联网）。"""
        from .api_client import DeepSeekClient

        return DeepSeekClient(
            base_url=self.config.base_url if base_url is None else base_url,
            model=self.config.model if model is None else model,
            api_key=self.config.api_key() if api_key is None else api_key,
            timeout=self.config.timeout if timeout is None else timeout,
            reasoning_effort=self.config.reasoning_effort
            if reasoning_effort is None else reasoning_effort,
            # 导图要确定输出：DeepSeek 系默认关掉思考模式（见 Config.map_thinking）
            thinking=self.config.map_thinking if thinking is None else thinking,
        )

    def is_ready(self) -> tuple[bool, str]:
        if not self.config.get_bool("map.enabled", True):
            return False, "参考关系图已在设置中关闭"
        if not self.config.has_api_key():
            return False, "尚未配置 API Key"
        if not self.config.base_url:
            return False, "尚未配置 Base URL"
        return True, ""

    def unavailable_hint(self) -> str:
        """缺配置时的**内联**提示（不含任何密钥信息）。"""
        ok, msg = self.is_ready()
        if ok:
            return ""
        hint = (self.config.get("map.no_key_hint") or "").strip()
        return f"{hint}（{msg}）" if msg else hint

    # ------------------------------------------------------------- 数据
    def nodes_for_topic(self, topic_id: int, *, limit: int | None = None) -> list[MapNode]:
        """当前主题的已保存词条 → 图节点（**只读**本地库，绝不碰别的主题）。"""
        if not topic_id:
            return []
        cap = int(self.config.get_int("map.max_entries", MAP_MAX_ENTRIES))
        cap = max(1, min(cap, MAP_MAX_ENTRIES))
        if limit is not None:
            cap = max(1, min(cap, int(limit)))
        try:
            rows = list(self.db.list_entries(batch_id=int(topic_id), limit=cap))
        except Exception:  # pragma: no cover - 读库失败不该炸窗口
            log.exception("读取导图词条失败")
            return []
        nodes = [
            MapNode(
                entry_id=int(row["id"]),
                term=str(row["term"] or ""),
                context=str(row["context"] or ""),
                explanation=explanation_text(row),
            )
            for row in rows
        ]
        nodes.sort(key=lambda item: int(item.entry_id))
        return nodes

    def article_for_topic(self, topic_id: int):
        """这个主题对应的**原文留档**（``articles`` 表的一行；没有 → ``None``）。

        怎么找：主题就是一批词条（``entries.batch_id``），词条上带着 ``doc_key``
        （「同一篇文章」的身份，划词那一刻就算好了）。取这一批里第一个**非空**
        ``doc_key``，再拿它去 ``articles`` 表要正文。同一批词条来自同一页，
        所以取第一个就是这一篇。

        返回 ``None`` 的三种情况都要如实对待（调用方据此走「没有原文」那条路，
        **绝不**拿词条上下文硬编一篇原文出来）：这批词条一个 ``doc_key`` 都没有
        （老库 / 手工建的批次）、从没抓过、或者抓过但失败了（``status='failed'``
        的行**不是** ``None``，调用方要看 ``status``）。
        """
        if not topic_id:
            return None
        try:
            rows = self.db.query(
                "SELECT DISTINCT doc_key FROM entries WHERE batch_id=? AND doc_key<>''",
                (int(topic_id),))
        except Exception:  # pragma: no cover - 读库失败按「没有原文」处理
            log.exception("读取主题的 doc_key 失败 topic=%s", topic_id)
            return None
        for row in rows:
            key = str(row["doc_key"] or "").strip()
            if not key:
                continue
            try:
                article = self.db.get_article(key)
            except Exception:  # pragma: no cover
                log.exception("读取原文留档失败 doc_key=%s", key)
                continue
            if article is not None:
                return article
        return None

    def _article_usable(self, article) -> bool:
        """这一行留档能不能用来归纳（见 :func:`article_is_usable`）。"""
        return article_is_usable(article)

    def source_tag(self, article, text: str = "") -> str:
        """生成方式的指纹标签（纯函数；进 :func:`content_fingerprint`）。

        * ``""`` —— 老路：按词条关系生成（没有可用原文）；
        * ``"article:<doc_key>#ok:<字数>"`` —— 按这篇原文归纳。

        为什么带上字数：同一篇原文改了（用户换了页面 / 重新抓过）就是另一张图，
        不能让旧缓存顶上来。为什么带上 ``doc_key``：不同文章的同一批词也不该混。
        留档不是成功原文（抓失败 / 太短）时一律返回 ``""`` —— 那就该退回老路。
        """
        if not self._article_usable(article):
            return ""
        try:
            key = str(article["doc_key"] or "").strip()
        except (IndexError, KeyError, TypeError):  # pragma: no cover - 传了别的行
            return ""
        body = logic_text_of(article) if text is None else text
        if not key or not body:
            return ""
        return f"article:{key}#ok:{len(body)}"

    def material_payload(self, nodes) -> list[dict]:
        """发给模型的**受限**材料：编号 + 词语 + 截断上下文 + 截断释义。"""
        cut = max(1, int(self.config.get_int("map.material_chars", MAP_MATERIAL_CHARS)))
        out = []
        for index, node in enumerate(list(nodes or [])):
            out.append({
                "index": index,
                "term": str(node.term or ""),
                "context": str(node.context or "")[:cut],
                "explanation": str(node.explanation or "")[:cut],
            })
        return out

    def verify_payload(self, relations, nodes) -> list[dict]:
        """构造**第二次核对**调用的受限材料（纯数据，不再新增任何边）。

        每条只带：序号、类型、具体方向（端点词 + :data:`REL_DIRECTION_TEXT` 的
        方向短句）、两个端点各自的上下文 / 释义、候选自己的依据与证据片段。
        不带 ``entry_id``、不带其它主题、不带整篇文档 —— 核对输入与候选一一对应，
        核对输出只能按序号回填判定。
        """
        nodes_by_id = {int(node.entry_id): node for node in (nodes or ())}
        items = []
        for index, rel in enumerate(relations):
            src = nodes_by_id.get(int(rel.src_entry_id))
            dst = nodes_by_id.get(int(rel.dst_entry_id))
            items.append({
                "index": index,
                "type": str(rel.rel_type),
                "direction": (f"{str(getattr(src, 'term', '') or '')} → "
                              f"{str(getattr(dst, 'term', '') or '')}；"
                              f"{REL_DIRECTION_TEXT.get(rel.rel_type, '')}"),
                "source": {
                    "term": str(getattr(src, "term", "") or ""),
                    "context": str(getattr(src, "context", "") or ""),
                    "explanation": str(getattr(src, "explanation", "") or ""),
                },
                "target": {
                    "term": str(getattr(dst, "term", "") or ""),
                    "context": str(getattr(dst, "context", "") or ""),
                    "explanation": str(getattr(dst, "explanation", "") or ""),
                },
                "reason": str(rel.reason or ""),
                "evidence": str(rel.evidence or ""),
            })
        return items

    def map_logic_chain(self, client, topic_name: str, nodes) -> tuple[dict, bool]:
        """**第零轮**：先读懂整篇文章的逻辑链（批次 M24），返回 ``(骨架, 是否为空)``。

        用户口径（2026-10-09）：「在导图生成流程中加入逻辑链检查……为了确保导图的
        精准性这一步必须不能省略」。为什么必须单独一轮：第一轮的材料是**词条碎片**
        （一个词 + 240 字上下文），模型根本没看见过整篇文章，只能按「两个词的上下文
        里都出现了什么」硬凑边，方向经常是倒的（实测：「项目任务拆解必须依赖已经
        确认好的项目需求」被写成 `任务拆解 -依赖-> 确定项目需求`，核对判 contradicted，
        图上主线当场断一节）。这一轮把正文整段发出去，只让它回答「文章是怎么串起来
        的」，骨架紧接着作为第一轮的前置认知。

        这一轮**不是**权威：端点、类型、证据仍然全部由本地校验 + 第二次核对决定；
        骨架只用来定调**方向**（谁在前、谁依赖谁、谁构成谁）。

        失败（网络 / 格式 / 模型没配 / 老客户端没有这个方法）一律**不打断**生成：
        返回空骨架 + ``True``，调用方按批次 M23 的老路径继续，并在结果里如实记一个
        ``logic_chain_empty`` 计数（界面能说清「这一次没走到逻辑链」）。
        """
        if not callable(getattr(client, "map_logic_chain", None)):
            return {}, True
        article = logic_chain_article(nodes, limit=int(self.config.get_int(
            "map.logic_chain_chars", LOGIC_CHAIN_TEXT_CHARS)))
        if not article.strip():
            return {}, True
        terms = [str(getattr(node, "term", "") or "").strip()
                 for node in list(nodes or ())]
        terms = [term for term in terms if term]
        try:
            chain = client.map_logic_chain(article, topic=topic_name, terms=terms)
        except Exception as exc:               # 第零轮失败不能把一次生成变成错误
            log.info("逻辑链分析失败 topic=%s: %s", topic_name, redact(str(exc)))
            return {}, True
        if not isinstance(chain, dict) or logic_chain_is_empty(chain):
            return {}, True
        return chain, False

    def rescue_orphan_relations(self, client, topic_name: str, nodes, relations,
                                verdicts, *, limit: int = RESCUE_MAX_ORPHANS,
                                logic_chain=None
                                ) -> tuple[list, list, dict]:
        """删边兜底（批次 M23）：被核对删成**孤立词**的那些词，单独补问一次。

        为什么需要它（实测，主题「项目开发」2026-10-09）：第一轮 9 条候选里有 6 条
        通过、1 条 ``uncertain``、2 条 ``contradicted``。那条 ``uncertain`` 恰好是
        「循环迭代」唯一的关系 —— 删掉之后这个词一个边都没有，界面把它降级成
        「孤立词」行，用户看到的就是**文章里明明有的一个环节从图上消失了**。
        静默降级不可接受，因此这里补一轮：把材料再发一次，**点名**那个还没连上的词，
        「只为它提候选」；新候选照样要过 :func:`validate_relations` 与**第二次核对**，
        核对通过才画 —— 兜底只负责「再问一次」，绝不自己造边、不降低任何判据。

        返回 ``(relations, verdicts, stats)``：新边直接**追加**在传入列表尾部（核对的
        序号是按位置回填的，因此追加不会打乱已有判定）；``stats`` 是给界面 / 日志用的
        诚实计数（``asked`` / ``candidates`` / ``supported`` / ``still_alone``）。

        任何一步失败（网络 / 格式 / 没配 Key）都只是**少救一个词**，绝不抛给调用方：
        补问是补救措施，不能把一次本来成功的生成变成错误。
        """
        stats = {"asked": 0, "candidates": 0, "supported": 0, "still_alone": 0}
        relations = list(relations or ())
        verdicts = list(verdicts or ())
        nodes = list(nodes or ())
        if not nodes:
            return relations, verdicts, stats
        # 「谁还没连上」这件事与能不能补问无关：先算出来，界面 / 日志要如实说
        alone_ids = {int(node.entry_id) for node in nodes} - touched_entry_ids(relations)
        stats["still_alone"] = len(alone_ids)
        if not callable(getattr(client, "map_isolated_rescue", None)):
            return relations, verdicts, stats
        try:
            budget = max(0, int(limit))
        except (TypeError, ValueError):       # pragma: no cover - 调用方给的都是 int
            budget = RESCUE_MAX_ORPHANS
        if budget <= 0:
            return relations, verdicts, stats

        entries = self.material_payload(nodes)
        index_of = {int(node.entry_id): pos for pos, node in enumerate(nodes)}
        linked = [(pos, str(getattr(node, "term", "") or ""))
                  for pos, node in enumerate(nodes)
                  if int(node.entry_id) not in alone_ids]
        # 没连上的词按词表顺序（与界面上的孤立词网格同一序）
        alone = [node for node in nodes if int(node.entry_id) in alone_ids]
        for node in alone[:budget]:
            stats["asked"] += 1
            pos = index_of.get(int(node.entry_id))
            if pos is None:                   # pragma: no cover - 节点一定来自同一份词表
                continue
            try:
                raw = client.map_isolated_rescue(
                    entries, topic=topic_name,
                    linked=linked,
                    isolated=[(pos, str(getattr(node, "term", "") or ""))],
                    logic_chain=logic_chain)
            except Exception as exc:           # 补救失败：如实记，不打断生成
                log.info("孤立词补问失败 topic=%s term=%s: %s",
                         topic_name, getattr(node, "term", ""), redact(str(exc)))
                continue
            if not raw:
                continue
            fresh, _drops = validate_relations(raw, nodes)
            known = {_relation_key(rel.rel_type, rel.src_entry_id, rel.dst_entry_id)
                     for rel in relations}
            fresh = [rel for rel in fresh
                     if _relation_key(rel.rel_type, rel.src_entry_id, rel.dst_entry_id)
                     not in known]
            if not fresh:
                continue
            stats["candidates"] += len(fresh)
            try:
                new_verdicts = client.map_verify(self.verify_payload(fresh, nodes),
                                                 topic=topic_name)
            except Exception as exc:           # 新一轮核对失败：这一轮整批不要
                log.info("孤立词补问核对失败 topic=%s term=%s: %s",
                         topic_name, getattr(node, "term", ""), redact(str(exc)))
                continue
            kept, _counts = apply_verdicts(fresh, nodes, new_verdicts)
            if not kept:
                continue
            relations.extend(kept)
            # 核对的判定按序号回填（序号是**补问批次内部**的，从 0 开始）
            for item in list(new_verdicts or ()):
                if not isinstance(item, dict):  # pragma: no cover - 解析层已挡
                    continue
                try:
                    idx = int(item.get("index"))
                except (TypeError, ValueError):  # pragma: no cover
                    continue
                moved = dict(item)
                # 序号保持**补问批次内部**的（从 0 开始）：调用方把这一批与第一轮
                # 的判定记录拼起来之后会统一重排 index，这里多挪一次反而会错位。
                moved["index"] = idx
                verdicts.append(moved)
            stats["supported"] += len(kept)
            for rel in kept:                   # 救上来的词不再算孤立
                if int(rel.src_entry_id) == int(node.entry_id) or \
                        int(rel.dst_entry_id) == int(node.entry_id):
                    linked.append((pos, str(getattr(node, "term", "") or "")))
                    break
        # 补问之后**重新算一遍**：救回来的词已经不是孤立词了，剩下的才如实报
        stats["still_alone"] = len(alone_ids - touched_entry_ids(relations))
        return relations, verdicts, stats

    def node_budget(self) -> int:
        """单次生成最多分析多少个词（``MAP_MAX_ENTRIES`` 是硬上限）。"""
        cap = int(self.config.get_int("map.max_entries", MAP_MAX_ENTRIES))
        return max(1, min(cap, MAP_MAX_ENTRIES))

    def topic_entry_total(self, topic_id: int) -> int:
        """该主题**实际**有多少个已保存词条（与本次分析数对比，界面要说清）。"""
        if not topic_id:
            return 0
        try:
            return int(self.db.count_entries(batch_id=int(topic_id)))
        except Exception:  # pragma: no cover - 读库失败不该炸窗口
            log.exception("统计主题词条数失败")
            return 0

    def coverage_note(self, topic_id: int, analyzed: int) -> str:
        """一句话说清「本次分析多少 / 共多少」；没被截断就返回空串。"""
        total = self.topic_entry_total(int(topic_id or 0))
        analyzed = int(analyzed or 0)
        if total <= 0 or analyzed <= 0 or total <= analyzed:
            return ""
        return f"本次分析 {analyzed} 词 / 该主题共 {total} 词"

    def model_config(self) -> str:
        return f"{self.config.base_url}|{self.config.model}"

    def fingerprint(self, topic_id: int, nodes, *, article=None) -> str:
        """这个 (主题, 词条, 生成方式) 组合的内容指纹。

        ``article`` 给了且可用时，指纹里带上「按这篇原文归纳」的标记 —— 同一批词
        在「有原文」和「没有原文」下算出来的图是两张，指纹必须分得开，否则切回
        旧库时会把另一条路的图当缓存直接显示。
        """
        text = logic_text_of(article)
        return content_fingerprint(topic_id, nodes, base_url=self.config.base_url,
                                   model=self.config.model,
                                   reasoning_effort=self.config.reasoning_effort,
                                   source=self.source_tag(article, text))

    # ------------------------------------------------------------- 缓存
    def cached_graph(self, topic_id: int, nodes=None, *, fingerprint: str | None = None,
                     article=None, source: str | None = None):
        """按 (主题, 内容指纹) 取缓存；没有 / 坏了 / 版本旧 / 模型不匹配都返回 ``None``。

        ``validation_version`` 是**第二道闸门**：旧版只做过字串证据校验（没有第二次
        独立核对）的缓存版本号不同，一律当没有缓存 —— 绝不直接展示。

        ``article`` / ``source`` 二选一（``source`` 优先，两个都不给 = 老口径的
        「按词条关系」指纹，老缓存照旧命中）。

        ``dropped`` / ``verdicts`` 两列与关系一起读回来：重开窗口照样能显示「筛除了
        什么」与「孤立词为什么孤立」；老缓存这两列是空串，就按「没记录」处理
        （宁可不解释，也绝不编一个筛除计数出来）。
        """
        if not topic_id:
            return None
        nodes = list(nodes) if nodes is not None else self.nodes_for_topic(int(topic_id))
        if not nodes:
            return None
        if fingerprint is None:
            tag = source
            if tag is None:
                tag = self.source_tag(article, logic_text_of(article))
            fp = content_fingerprint(int(topic_id), nodes, base_url=self.config.base_url,
                                     model=self.config.model,
                                     reasoning_effort=self.config.reasoning_effort,
                                     source=tag)
        else:
            fp = str(fingerprint)
        try:
            row = self.db.get_map_graph(int(topic_id), fp,
                                        base_url=self.config.base_url,
                                        model=self.config.model)
        except Exception:  # pragma: no cover - 读缓存失败按未命中处理
            log.exception("读取导图缓存失败")
            return None
        if row is None:
            return None
        try:
            stored_version = int(row["validation_version"] or 0)
        except (IndexError, KeyError, TypeError, ValueError):  # pragma: no cover - 老库
            stored_version = 0
        if stored_version != MAP_VALIDATION_VERSION:
            return None
        try:
            raw_rel = json.loads(row["payload"] or "[]")
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        if not isinstance(raw_rel, list):
            return None
        valid_ids = {int(node.entry_id) for node in nodes}
        relations: list[MapRelation] = []
        for item in raw_rel:
            if not isinstance(item, dict):
                continue
            try:
                src_id, dst_id = int(item["src"]), int(item["dst"])
            except (KeyError, TypeError, ValueError):
                continue
            rel_type = str(item.get("type") or "")
            if rel_type not in REL_TYPES or src_id not in valid_ids or dst_id not in valid_ids:
                continue
            relations.append(MapRelation(src_id, dst_id, rel_type,
                                         str(item.get("reason") or ""),
                                         str(item.get("evidence") or "")))
        chain = _load_logic_chain(row)
        graph = MapGraph(
            topic_id=int(topic_id),
            fingerprint=fp,
            relations=tuple(relations),
            nodes=tuple(nodes),
            dropped=_load_dropped(row),
            source="cache",
            model_config=f"{row['base_url']}|{row['model']}",
            verdicts=_load_verdicts(row),
            logic_chain=chain,
            logic_chain_empty=not chain,
        )
        return _with_provenance(graph, row)

    # ------------------------------------------------------------- 状态
    def is_inflight(self, topic_id: int | None = None,
                    fingerprint: str | None = None) -> bool:
        """该主题是否已有在途请求；给出 ``fingerprint`` 时还要求**内容一致**。

        窗口关掉又打开时用 ``(主题, 当前内容指纹)`` 判断：同一份内容的请求还在途
        就不重复发（等它的结果直接显示）；只是同主题但内容已经变了的老请求不算，
        否则新内容会被永远挡在「正在生成…」外面。
        """
        with self._lock:
            if topic_id is None:
                return bool(self._inflight)
            topic_id = int(topic_id)
            if fingerprint is None:
                return any(key[0] == topic_id for key in self._inflight)
            return (topic_id, str(fingerprint)) in self._inflight

    def inflight_count(self) -> int:
        with self._lock:
            return len(self._inflight)

    # ------------------------------------------------------------- 生成
    def generate(self, topic_id: int, *, nodes=None, force: bool = False,
                 topic_name: str = "", article=None, vary: int = 0) -> int | None:
        """为某个主题生成参考关系。返回**请求 token**（``None`` = 没有发起网络请求）。

        ``None`` 的四种情况（调用方据此决定提示）：
        * 主题为空 / 该主题没有词条 —— 没什么可生成；
        * 命中有效缓存（非 ``force``）—— 已按 ``token=0`` 立刻投递缓存图；
        * 配置不完整（没有 Key / Base URL）—— 已按 ``token=0`` 投递配置错误；
        * 端点编号之类的内部错误（理论上不会发生）。

        ``article`` 是 ``articles`` 表的一行（调用方用
        :meth:`article_for_topic` 取；**抓失败的那一行也照传**，让指纹与状态说明
        看到真实情况）：给了**且 ``status='ok'`` 且正文够长**（
        :func:`article_is_usable`）时走「按原文归纳」那条流水线
        （:func:`run_logic_graph`）；否则一律走老路（按词条关系），并且**如实说明**
        没有原文可用 —— 绝不拿一句失败理由或 240 字上下文硬编一篇原文出来。

        ``vary`` 是「第几次重新生成」的**思维阶梯档位**（批次 M21-B，默认 0 =
        老口径）：每一档换一套归纳策略、放宽 ``temperature``、抬高推理强度，
        并**进指纹** —— 因此每一档各是一张图、各有自己的缓存，而不是拿旧图
        冒充新结果。有原文时才逐档递进；没原文（走词条关系那条路）时只有第 0 档
        有意义，别的档位会被跳过（见下方 ``step`` 的计算）。

        迟到的结果带 ``(token, topic_id, fingerprint)``：UI 只接受与当前主题 +
        当前指纹都匹配的结果，因此切主题 / 改词后的旧结果不会串图。
        """
        topic_id = int(topic_id or 0)
        if not topic_id:
            return None
        nodes = self.nodes_for_topic(topic_id) if nodes is None else list(nodes)
        if not nodes:
            return None
        base_url = str(self.config.base_url or "")
        model = str(self.config.model or "")
        text = logic_text_of(article)
        if text and not self._article_usable(article):
            # 留档里那一行不是成功的原文（抓失败 / 太短）：**退回老路**，
            # 绝不拿一句「取原文失败」的理由当文章去归纳。
            text = ""
        source = self.source_tag(article, text)
        # 思维阶梯只在**有原文**时逐档递进：没原文那张图是代码按关系分层画出来的，
        # 与「换个思路读文章」无关，硬抬档位只会白花钱。
        plan = vary_plan(vary if text else 0,
                         reasoning_effort=self.config.reasoning_effort,
                         model=model)
        fp = content_fingerprint(topic_id, nodes, base_url=base_url, model=model,
                                 reasoning_effort=plan["effort"],
                                 source=source, vary=plan["vary"])

        if not force:
            cached = self.cached_graph(topic_id, nodes, fingerprint=fp,
                                       source=source)
            if cached is not None:
                self._deliver(0, topic_id, fp, RESULT_OK, cached, None)
                return None
        ok, msg = self.is_ready()
        if not ok:
            err = ApiError("config", msg)
            self._deliver(0, topic_id, fp, RESULT_ERROR, None, err)
            return None

        with self._lock:
            token = self._next_token
            self._next_token += 1
            self._inflight.add((topic_id, fp))
        threading.Thread(
            target=self._worker,
            args=(token, topic_id, fp, tuple(nodes), base_url, model,
                  float(self.config.timeout), str(self.config.api_key()), str(topic_name or "")),
            kwargs={"article": (article, text) if text else None, "plan": plan},
            name=f"map-t{token}",
            daemon=True,
        ).start()
        return token

    # ------------------------------------------------------------- 布局骨架（G3）
    def pick_template(self, pairs, *, topic: str = "", choices=(), callback=None) -> bool:
        """让模型挑一个布局骨架（**可选**，默认关）。

        ``pairs`` 只含关系类型与起止词名（由界面用 :meth:`template_pairs` 生成）；
        ``choices`` 是可选清单 ``[{"key","name","summary","fit"}, ...]``。
        返回是否**真的发出了请求**：没配 Key / 没有关系 / 没有回调 → ``False``，
        调用方直接用本地规则，什么都不会缺。

        ``callback(key, reason, error)`` 在**后台线程**里被调用（``error`` 非空表示
        这次没问成）—— 回调实现必须自己把结果送回 UI 线程（界面用
        ``win.after(0, …)``）。挑骨架**不写任何缓存**：它只影响这一次的排版。
        """
        items = [dict(item) for item in list(pairs or []) if item]
        if callback is None or not items:
            return False
        ok, _msg = self.is_ready()
        if not ok:
            return False
        threading.Thread(
            target=self._template_worker,
            args=(tuple(items), str(topic or ""), tuple(dict(c) for c in (choices or ())),
                  str(self.config.base_url or ""), str(self.config.model or ""),
                  float(self.config.timeout), str(self.config.api_key()), callback),
            name="map-template",
            daemon=True,
        ).start()
        return True

    def _template_worker(self, pairs, topic: str, choices, base_url: str, model: str,
                         timeout: float, api_key: str, callback) -> None:
        """后台线程：问一次模型「这张图该用哪个骨架」，然后回调。

        任何失败（网络 / 超时 / 输出不是 JSON / id 不在清单里）都只是回
        ``("", reason, error)``：**挑骨架失败绝不能挡住画图**，界面会回退本地规则。
        """
        try:
            client = self.make_client(base_url=base_url, model=model,
                                      timeout=timeout, api_key=api_key)
            key, reason = client.map_template(pairs, topic=topic, choices=choices)
        except ApiError as exc:
            log.info("让模型挑布局骨架失败：%s", exc.display())
            callback("", "", exc.display())
            return
        except Exception as exc:  # pragma: no cover - 兜底
            log.exception("让模型挑布局骨架异常")
            callback("", "", redact(str(exc)))
            return
        if key:
            log.info("模型挑了布局骨架 %s（%s）", key, reason)
        callback(str(key or ""), str(reason or ""), "")

    def template_pairs(self, relations, labels=None) -> list[dict]:
        """把关系压成「挑骨架」用得上的最小摘要：``{"source","type","target"}``。

        **只含词名与类型**：不带 entry_id、不带上下文、不带释义、不带依据与证据。
        端点名优先查 ``labels``（``{entry_id: 词}``），查不到就退回 id 字符串。
        """
        names = dict(labels or {})
        out: list[dict] = []
        for rel in list(relations or []):
            src = int(getattr(rel, "src_entry_id", 0) or 0)
            dst = int(getattr(rel, "dst_entry_id", 0) or 0)
            kind = str(getattr(rel, "rel_type", "") or getattr(rel, "label", "") or "").strip()
            out.append({"source": str(names.get(src) or src),
                        "target": str(names.get(dst) or dst),
                        "type": kind or "相关"})
        return out

    def _carry_previous(self, topic_id: int, fp: str, nodes, relations, verdicts):
        """把上一次为**同一份材料**画出的关系补回来（沿用上次，纯读 + 纯函数）。

        读不到上次（没生成过 / 缓存版本旧 / 换过模型）就当没有：沿用只发生在**同一
        内容指纹**下 —— 改了词条或换了模型一律从头算，绝不把旧模型的结论拖过来。
        返回 ``(relations, verdicts, carried_keys)``；``carried_keys`` 是这次靠沿用才
        留在图上的 ``_relation_key`` 集合，调用方在核对之后再数一遍真正落在图上的
        条数（沿用的边也可能被本地校验挡掉，那时就不能在界面上说「沿用了几条」）。
        """
        try:
            previous = self.cached_graph(topic_id, nodes, fingerprint=fp)
        except Exception:  # pragma: no cover - 读缓存失败按「没有上次」处理
            log.exception("读取上次导图失败 topic=%s", topic_id)
            previous = None
        if previous is None or not previous.relations:
            return list(relations), list(verdicts), set()
        merged, merged_verdicts, carried_keys = carry_over_relations(
            previous, relations, nodes, verdicts=verdicts,
            # ★ 沿用的边也要过「流程步骤不许写成包含 / 属于」这道本地判据（批次
            # M24）—— 否则它会从旧图里绕过关爬回来。流程边按**本轮真正成立**的
            # 关系算（这里传进来的就已经是本地校验通过的候选）。
            flow_pairs=flow_step_pairs(relations, nodes))
        if carried_keys:
            log.info("导图沿用上次已核对通过的关系 %s 条 topic=%s", len(carried_keys), topic_id)
        return merged, merged_verdicts, carried_keys

    def _worker_article(self, token: int, topic_id: int, fp: str, nodes, article,
                        plan=None) -> None:
        """后台线程（按原文归纳那条路）：归纳 → 代码校验 → 落到词条上 → 写缓存。

        ``article`` 是 ``(articles 表的一行, 正文)``。这一路**不**做第二次模型核对：
        归纳的每一步都由 :func:`validate_logic` 拿代码算过（实体必须在原文里逐字
        找得到、时序不许讲反、分支要够），比再问一遍模型更硬，也少花一次钱。

        ``plan`` 是 :func:`vary_plan` 的换思路方案（批次 M21-B）：模型、推理强度、
        ``temperature``、追加指令全按它来 —— 点「重新生成」才会真的换一张图。

        分支落进 ``map_graphs.raw`` 那一列（存归纳结果的 JSON）：读缓存时由
        :func:`_with_provenance` 认回来，界面才说得出「按哪篇文章归纳的」。
        """
        row, text = article
        plan = dict(plan or {})
        #: ★ 第零轮逻辑链的出口（批次 M24）：``run_logic_graph`` 会把骨架写进来，
        #: 这里既用它落缓存、也用它决定界面上说不说「这一次没走到逻辑链」。
        sink: dict = {"chain": {}}
        doc_key = title = ""
        try:
            doc_key = str(row["doc_key"] or "").strip()
            title = str(row["title"] or "").strip()
        except (IndexError, KeyError, TypeError):  # pragma: no cover - 传了别的行
            pass
        try:
            client = self.make_client(base_url=str(self.config.base_url or ""),
                                      model=str(self.config.model or ""),
                                      timeout=float(self.config.timeout),
                                      api_key=str(self.config.api_key()),
                                      reasoning_effort=str(
                                          plan.get("effort")
                                          or self.config.reasoning_effort or ""))
            graph = run_logic_graph(
                client, doc_key=doc_key, title=title, text=text,
                terms=tuple(node.term for node in nodes),
                model_config=self.model_config(), plan=plan, logic_sink=sink)
            relations, skipped = logic_to_map_relations(graph, nodes)
            dropped: dict[str, int] = {}
            if skipped:
                dropped["not_in_topic"] = len(skipped)
        except ApiError as exc:
            log.info("按原文归纳失败 token=%s: %s", token, exc.display())
            self._deliver(token, topic_id, fp, RESULT_ERROR, None, exc)
            return
        except Exception as exc:  # pragma: no cover - 兜底，不让线程静默死掉
            log.exception("按原文归纳异常 token=%s", token)
            self._deliver(token, topic_id, fp, RESULT_ERROR, None,
                          ApiError("unknown", redact(str(exc))))
            return
        finally:
            with self._lock:
                self._inflight.discard((topic_id, fp))

        chain = dict(sink.get("chain") or {})
        if not chain:
            # 第零轮没跑成（方法不存在 / 报错 / 回了空骨架）：如实记一个，界面就能
            # 说清「这一次没走到逻辑链」，而不是假装走过了。
            dropped["logic_chain_empty"] = 1
        result = MapGraph(
            topic_id=topic_id,
            fingerprint=fp,
            relations=tuple(relations),
            nodes=tuple(nodes),
            dropped=dict(dropped),
            source="article",
            model_config=self.model_config(),
            main_logic=str(graph.main_logic or ""),
            branches=tuple(dict(branch) for branch in graph.branches),
            vary=int(graph.vary or 0),
            vary_label=str(graph.vary_label or ""),
            logic_chain=chain,
            logic_chain_empty=not chain,
        )
        try:
            self.db.put_map_graph(
                topic_id=topic_id, fingerprint=fp,
                base_url=str(self.config.base_url or ""),
                model=str(self.config.model or ""),
                relations=[rel.as_dict() for rel in relations],
                raw=json.dumps(graph.as_dict(), ensure_ascii=False)[:4000],
                validation_version=MAP_VALIDATION_VERSION,
                dropped=dict(dropped), verdicts=[],
                logic_chain=chain,
            )
        except Exception:  # 写缓存失败不影响本次显示
            log.exception("写导图缓存失败（按原文归纳）topic=%s", topic_id)
        self._deliver(token, topic_id, fp, RESULT_OK, result, None)

    def _worker(self, token: int, topic_id: int, fp: str, nodes, base_url: str,
                model: str, timeout: float, api_key: str, topic_name: str,
                article=None, plan=None) -> None:
        """后台线程：候选 → 本地校验 → **独立核对** → 写缓存 → 投递结果。

        有原文时走的是另一条路（``article=(行, 正文)``）：**按原文归纳**
        （:func:`run_logic_graph`）—— 一次调用出分支 + 边，代码校验（零 token），
        不合格带差异清单重试，最多 :data:`LOGIC_MAX_ATTEMPTS` 轮。归纳出来的边
        同样要过 :func:`validate_relations`；**只有两端都在本主题词库里的边**才画
        得上去（见 :func:`logic_to_map_relations`，丢掉的条数如实记进 ``dropped``）。

        第二次调用沿用**同一份冻结配置**（base_url / model / timeout / key 都是发起
        时抓下来的快照，改设置不会影响在途请求）。核对失败（网络 / 顶层格式）按错误
        处理并可重试；核对说 ``uncertain`` / ``contradicted`` / 判定坏掉 —— 那条边
        既不画也不缓存。任何异常都有终态，线程绝不静默死掉。

        核对的**每条判定**（含没通过的）都随图一起落库：重开窗口时既能显示「筛除了
        什么」，也能说清「这个词为什么成了孤立词」—— 判定以前只活在内存里。

        **沿用上次**：同一份材料（同一指纹）重新生成时，上次已核对通过、这次没提出来的
        边会补回来（见 :func:`carry_over_relations`）—— 提出阶段是采样，不沿用就会
        「每点一次重新生成，图就变一次」。
        """
        records: tuple[dict, ...] = ()
        carried = 0
        try:
            if article is not None:
                self._worker_article(token, topic_id, fp, tuple(nodes), article,
                                     plan=plan)
                return
            try:
                client = self.make_client(base_url=base_url, model=model,
                                          timeout=timeout, api_key=api_key)
                # ★ 第零轮（批次 M24）：先通读全文写出逻辑链骨架，作为第一轮的
                # **前置认知**发下去。这一步不可省略（用户口径），但它的失败不算
                # 生成失败 —— 拿不到骨架就按批次 M23 的老路径继续，并如实记一个
                # `logic_chain_empty`，界面上能说清「这一次没走到逻辑链」。
                chain, chain_empty = self.map_logic_chain(client, topic_name, nodes)
                # 本次主题所有词条的上下文：第零轮的 basis 要在这里面抠出**逐字存在**
                # 的那一截当证据（见 :func:`_verbatim_evidence`）。
                topic_materials: list[str] = []
                for node in nodes:
                    topic_materials.extend(str(item) for item in node.materials())
                # ★ 把关第一道：第零轮读出来的先后 / 因果 / 依赖 / 收口本身就要变成
                # 候选（用户口径是「先把逻辑链读清楚，导图才准」—— 那这份认知就不能
                # 只当提示词里的一段话）。它排在最前面，模型那条路只负责补材料依据。
                skeleton, skeleton_counts = logical_canonical_candidates(
                    chain, nodes, materials=topic_materials)
                asked = client.map_relations(self.material_payload(nodes),
                                             topic=topic_name,
                                             logic_chain=chain)
                # ★ 把关第二道：模型偶尔把 src / dst 写成词语而不是编号（真 Key
                # 实测五条全写成名字）。以前这种候选按「端点越界」整条丢掉、图上一条
                # 线都不剩 —— 现在按词折回编号，折不回来的才丢。
                asked, by_name = resolve_candidate_endpoints(asked, nodes)
                candidates = skeleton + asked
                relations, dropped = validate_relations(candidates, nodes)
                if skeleton_counts:
                    dropped["skeleton_edges"] = sum(skeleton_counts.values())
                if by_name:
                    dropped["endpoint_by_name"] = by_name
                if chain_empty:
                    # ★ 必须写在 ``validate_relations`` **之后**：``dropped`` 就是它
                    # 返回的那本账，写在前面会 UnboundLocalError（实测踩过：
                    # `app/map_service.py:2563`，整个生成线程当场报错）。
                    dropped["logic_chain_empty"] = 1
                # 只有本地校验通过的候选才值得再问一次（核对不许新增边）
                verdicts: list = []
                if relations:
                    verdicts = client.map_verify(self.verify_payload(relations, nodes),
                                                 topic=topic_name)
                else:
                    # 一条本地候选都没有：不花第二次调用，直接给空关系
                    dropped.setdefault("verify_supported", 0)
                relations, verdicts, carried_keys = self._carry_previous(
                    topic_id, fp, nodes, relations, verdicts)
                base_relations = list(relations)
                base_verdicts = list(verdicts)
                if verdicts:
                    relations, checked = apply_verdicts(relations, nodes, verdicts)
                    for key, value in checked.items():
                        dropped[key] = int(dropped.get(key, 0)) + int(value or 0)
                # 补问前后各切一刀：判定记录与**删边前**的关系表同一口径（第一轮每条
                # 候选都留档，没通过的 ``kept=False`` 就是「这个词为什么成了孤立词」
                # 的依据，界面点孤立词查原因靠它）；补问那一批只取真正追加进来的。
                before_verdicts = len(verdicts)
                kept_count = len(relations)
                # 删边兜底（批次 M23）：核对把某个词删到一个边都不剩时，为它**单独
                # 补问一次**，别让它在图上静默降级成「孤立词」。补救失败只少救一个词。
                relations, verdicts, rescued = self.rescue_orphan_relations(
                    client, topic_name, nodes, relations, verdicts,
                    logic_chain=chain)
                # ★ 判定记录必须与**删边前**的关系表同一口径：拿「删过边的关系表 +
                # 没删过的整份判定」回填会让后面的序号越界、判定全变空（实测踩过）。
                records = list(verdict_records(base_relations, nodes, base_verdicts))
                if rescued.get("asked"):
                    dropped["rescue_asked"] = int(rescued["asked"])
                    dropped["rescue_supported"] = int(rescued["supported"])
                    dropped["rescue_still_alone"] = int(rescued["still_alone"])
                    # 第一轮那几条判定**原样保留** —— 其中没通过的（``kept=False``）就是
                    # 「这个词为什么成了孤立词」的唯一依据，界面点孤立词查原因靠它。
                    # 补问批的判定追加在后面（两批各判各的），最后统一重排 ``index``：
                    # 判定走的都是 ``src`` / ``dst``（点词查原因），序号只用来显示与
                    # 排序，重排一次比在两处各挪一次更不容易错位。
                    later = verdict_records(relations[kept_count:], nodes,
                                            verdicts[before_verdicts:])
                    records = records + list(later)
                    for pos, item in enumerate(records):
                        item["index"] = pos
                records = tuple(records)
                # 诚实的沿用计数：沿用的边也可能在核对阶段被本地校验挡掉，
                # 那时就是没画出来，界面上不能说「沿用了几条」。
                carried = sum(1 for rel in relations
                              if _relation_key(rel.rel_type, rel.src_entry_id,
                                               rel.dst_entry_id) in carried_keys)
            except ApiError as exc:
                log.info("生成参考关系失败 token=%s: %s", token, exc.display())
                self._deliver(token, topic_id, fp, RESULT_ERROR, None, exc)
                return
            except Exception as exc:  # pragma: no cover - 兜底，不让线程静默死掉
                log.exception("生成参考关系异常 token=%s", token)
                self._deliver(token, topic_id, fp, RESULT_ERROR, None,
                              ApiError("unknown", redact(str(exc))))
                return

            graph = MapGraph(
                topic_id=topic_id,
                fingerprint=fp,
                relations=tuple(relations),
                nodes=tuple(nodes),
                dropped=dict(dropped),
                source="ai",
                model_config=f"{base_url}|{model}",
                verdicts=tuple(records),
                carried=carried,
                logic_chain=dict(chain or {}),
                logic_chain_empty=bool(chain_empty),
            )
            try:
                self.db.put_map_graph(
                    topic_id=topic_id, fingerprint=fp, base_url=base_url, model=model,
                    relations=[rel.as_dict() for rel in relations],
                    raw=json.dumps(list(candidates), ensure_ascii=False)[:4000],
                    validation_version=MAP_VALIDATION_VERSION,
                    dropped=dict(dropped), verdicts=list(records),
                    logic_chain=dict(chain or {}),
                )
            except Exception:  # 写缓存失败不影响本次显示
                log.exception("写导图缓存失败 topic=%s", topic_id)
            self._deliver(token, topic_id, fp, RESULT_OK, graph, None)
        finally:
            with self._lock:
                self._inflight.discard((topic_id, fp))
