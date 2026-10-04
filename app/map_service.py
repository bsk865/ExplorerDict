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
import threading
from dataclasses import dataclass, field

from .api_client import (
    MAP_MATERIAL_CHARS, MAP_MAX_ENTRIES, ApiError,
)
from .config import model_signature
from .logging_setup import get_logger, redact

log = get_logger("map")

#: 允许的关系类型（提示词、校验、缓存共用同一份白名单）
REL_TYPES: tuple[str, ...] = ("包含", "属于", "依赖", "用途", "因果", "对照")
#: 层级关系：参与「谁在谁上面」的分组
HIERARCHY_TYPES = frozenset({"包含", "属于"})
#: 有向关系：参与前后分层
DIRECTIONAL_TYPES = frozenset({"依赖", "因果"})
#: 跨边关系：不参与分层，只画带方向的跨边
CROSS_TYPES = frozenset({"用途", "对照"})
#: 对称关系：A↔B 与 B↔A 视为同一条（不算矛盾）
SYMMETRIC_TYPES = frozenset({"对照"})
#: 层级 / 方向约束（参与成环检测）
CONSTRAINT_TYPES = HIERARCHY_TYPES | DIRECTIONAL_TYPES

#: 依据 / 证据片段的长度上限（超出只截断，不改写、不编造）
REASON_CHARS = 60
EVIDENCE_CHARS = 120
#: 证据片段的最小有效长度（规范化之后按字符算）。
#: 2 个字太松 —— 「卷积」这种把端点词抄一遍的片段会被当成依据，因此提到 6 个字，
#: 并且额外禁止「证据整个包含在端点词语里」这种纯重复（见 :func:`evidence_supported`）。
MIN_EVIDENCE_CHARS = 6

#: 缓存里的**校验版本**：候选 → 本地校验 → 独立核对 这一版校验的编号。
#: 只有版本一致的缓存才允许直接展示；旧的「只做字串证据校验」缓存读到也不命中。
MAP_VALIDATION_VERSION = 2

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
}

RESULT_OK = "ok"
RESULT_ERROR = "error"


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

    * 包含 A→B：A 是上位（A 在 B 之前）；
    * 属于 A→B：B 是上位（B 在 A 之前）；
    * 依赖 A→B：B 是前提（B 在 A 之前）；
    * 因果 A→B：A 是因（A 在 B 之前）；
    * 用途 / 对照：跨边，不产生层级约束（``None``）。
    """
    if rel_type == "包含":
        return (int(src_id), int(dst_id))
    if rel_type == "属于":
        return (int(dst_id), int(src_id))
    if rel_type == "依赖":
        return (int(dst_id), int(src_id))
    if rel_type == "因果":
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


def relation_locally_valid(rel_type, reason, evidence, src_node, dst_node) -> bool:
    """单条关系的**本地校验**（schema / 端点 / 依据 / 证据）—— 纯函数。

    本地校验与「核对之后再过一遍」共用同一份判据：类型在白名单、有非空依据、
    不是自环、证据**逐字**出现在两端材料里，且不是「把端点词语抄一遍」的弱证据。
    """
    if rel_type not in REL_TYPES:
        return False
    if not str(reason or "").strip():
        return False
    if int(src_node.entry_id) == int(dst_node.entry_id):
        return False
    materials = tuple(src_node.materials()) + tuple(dst_node.materials())
    terms = (str(getattr(src_node, "term", "") or ""), str(getattr(dst_node, "term", "") or ""))
    return evidence_supported(evidence, materials, terms=terms)


def validate_relations(candidates, nodes, *,
                       max_reason_chars: int = REASON_CHARS,
                       max_evidence_chars: int = EVIDENCE_CHARS
                       ) -> tuple[list[MapRelation], dict]:
    """把模型候选关系筛成**可画的候选**（纯函数，无 Tk、无 IO）。

    返回 ``(kept, dropped)``：``kept`` 保持候选原顺序，``dropped`` 是按原因分类的计数。
    规则见模块开头；核心是「端点必须是本次给出的词 + 必须带材料里逐字出现的证据」。
    这里产出的仍然只是**候选**：还要过第二次独立核对才能画 / 缓存。
    """
    nodes = list(nodes or [])
    dropped = {key: 0 for key in ("shape", "endpoint", "type", "self", "reason",
                                  "evidence", "duplicate", "contradiction", "cycle")}
    kept: list[MapRelation] = []
    seen: set = set()

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
        if not relation_locally_valid(rel_type, reason, evidence, src_node, dst_node):
            dropped["evidence"] += 1
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
                rel.rel_type, rel.reason, rel.evidence, src_node, dst_node):
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


def content_fingerprint(topic_id: int, nodes, *, base_url: str = "", model: str = "",
                        reasoning_effort: str = "") -> str:
    """内容指纹：主题 + 每个词条的 (id, term, context, 释义) + 模型配置。

    新词 / 改词 / 换模型 / 改推理强度 → 指纹变化 → 旧缓存不命中（过期检测）；
    同内容重复打开则稳定命中。词条按 id 排序，读库顺序变化不影响指纹。
    推理强度为空串时与旧口径逐字一致（老缓存不会因为这次改动失效）。
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
    payload = json.dumps(
        {
            "topic_id": int(topic_id),
            "entries": entries,
            "base_url": str(base_url or "").strip().rstrip("/"),
            "model": signature,
        },
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

    def fingerprint(self, topic_id: int, nodes) -> str:
        return content_fingerprint(topic_id, nodes, base_url=self.config.base_url,
                                   model=self.config.model,
                                   reasoning_effort=self.config.reasoning_effort)

    # ------------------------------------------------------------- 缓存
    def cached_graph(self, topic_id: int, nodes=None, *, fingerprint: str | None = None):
        """按 (主题, 内容指纹) 取缓存；没有 / 坏了 / 版本旧 / 模型不匹配都返回 ``None``。

        ``validation_version`` 是**第二道闸门**：旧版只做过字串证据校验（没有第二次
        独立核对）的缓存版本号不同，一律当没有缓存 —— 绝不直接展示。

        ``dropped`` / ``verdicts`` 两列与关系一起读回来：重开窗口照样能显示「筛除了
        什么」与「孤立词为什么孤立」；老缓存这两列是空串，就按「没记录」处理
        （宁可不解释，也绝不编一个筛除计数出来）。
        """
        if not topic_id:
            return None
        nodes = list(nodes) if nodes is not None else self.nodes_for_topic(int(topic_id))
        if not nodes:
            return None
        fp = fingerprint or self.fingerprint(int(topic_id), nodes)
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
        return MapGraph(
            topic_id=int(topic_id),
            fingerprint=fp,
            relations=tuple(relations),
            nodes=tuple(nodes),
            dropped=_load_dropped(row),
            source="cache",
            model_config=f"{row['base_url']}|{row['model']}",
            verdicts=_load_verdicts(row),
        )

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
                 topic_name: str = "") -> int | None:
        """为某个主题生成参考关系。返回**请求 token**（``None`` = 没有发起网络请求）。

        ``None`` 的四种情况（调用方据此决定提示）：
        * 主题为空 / 该主题没有词条 —— 没什么可生成；
        * 命中有效缓存（非 ``force``）—— 已按 ``token=0`` 立刻投递缓存图；
        * 配置不完整（没有 Key / Base URL）—— 已按 ``token=0`` 投递配置错误；
        * 端点编号之类的内部错误（理论上不会发生）。

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
        fp = content_fingerprint(topic_id, nodes, base_url=base_url, model=model,
                                 reasoning_effort=self.config.reasoning_effort)

        if not force:
            cached = self.cached_graph(topic_id, nodes, fingerprint=fp)
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
            previous, relations, nodes, verdicts=verdicts)
        if carried_keys:
            log.info("导图沿用上次已核对通过的关系 %s 条 topic=%s", len(carried_keys), topic_id)
        return merged, merged_verdicts, carried_keys

    def _worker(self, token: int, topic_id: int, fp: str, nodes, base_url: str,
                model: str, timeout: float, api_key: str, topic_name: str) -> None:
        """后台线程：候选 → 本地校验 → **独立核对** → 写缓存 → 投递结果。

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
            try:
                client = self.make_client(base_url=base_url, model=model,
                                          timeout=timeout, api_key=api_key)
                candidates = client.map_relations(self.material_payload(nodes),
                                                  topic=topic_name)
                relations, dropped = validate_relations(candidates, nodes)
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
                records = verdict_records(relations, nodes, verdicts)
                if verdicts:
                    relations, checked = apply_verdicts(relations, nodes, verdicts)
                    for key, value in checked.items():
                        dropped[key] = int(dropped.get(key, 0)) + int(value or 0)
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
            )
            try:
                self.db.put_map_graph(
                    topic_id=topic_id, fingerprint=fp, base_url=base_url, model=model,
                    relations=[rel.as_dict() for rel in relations],
                    raw=json.dumps(list(candidates), ensure_ascii=False)[:4000],
                    validation_version=MAP_VALIDATION_VERSION,
                    dropped=dict(dropped), verdicts=list(records),
                )
            except Exception:  # 写缓存失败不影响本次显示
                log.exception("写导图缓存失败 topic=%s", topic_id)
            self._deliver(token, topic_id, fp, RESULT_OK, graph, None)
        finally:
            with self._lock:
                self._inflight.discard((topic_id, fp))
