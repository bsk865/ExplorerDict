"""解释服务：缓存 + 异步调用。

规则：
* 只有用户显式动作才会走到这里：
  - 浮条上唯一的按钮「解释并记录」→ **先落库**，再按 ``entry_id`` 解释并写回；
  - 主界面词条上的「解释 / 强制重解释」；
  - 解释结果窗上的「重新解释」（沿用同一个 ``entry_id``，不重复记词、不加词频）。
* 命中缓存不发网络请求；
* 网络调用在线程里跑，结果通过回调交回 UI 线程（UI 侧自己入队）；
* **发起时固定快照**（term/context + base_url/model/timeout/key），
  完成时若词条已被编辑或删除，则丢弃旧结果、不写回；
  设置中途被改也不会把旧模型的结果缓存到新模型名下；
* 结果**按 ``entry_id`` 归属**：成功 / 失败 / 过期三条路径一律调用
  ``on_result(entry_id, status, result, error)``，UI 据此刷新卡片；
* 每个请求带一个自增 ``token``：它是**解释服务自己的计数器**，与 UI 的
  「选区序号」完全独立，二者绝不互相比较。
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass

from .api_client import ApiError, DeepSeekClient, normalize_topic_name
from .config import model_signature
from .logging_setup import get_logger, redact
from .models import ExplainResult, now_iso

log = get_logger("explain")


def sanitize_topic(value, *, limit: int = 16) -> str:
    """解释结果里的主题名 → 可直接用于分组的短名字（坏名字一律退回空串）。

    坏 topic **只被忽略**，绝不影响解释正文、缓存与落库（调用方拿空串就跳过
    改名动作）。
    """
    return normalize_topic_name(value, limit=limit)


def make_cache_key(term: str, context: str, base_url: str, model: str) -> str:
    """缓存键 = 术语 + 语境 + 模型配置。同词不同语境必须区分开。"""
    payload = json.dumps(
        {
            "term": (term or "").strip(),
            "context": (context or "").strip(),
            "base_url": (base_url or "").strip().rstrip("/"),
            "model": (model or "").strip(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_topic(row) -> str:
    """从缓存行里安全读取 topic（旧缓存没有这一列 / 值坏了都当没给）。"""
    try:
        raw = row["topic"]
    except (IndexError, KeyError):
        return ""
    return sanitize_topic(raw)


@dataclass(frozen=True)
class ExplainSnapshot:
    """一次解释请求的不可变快照。发起后任何设置/词条改动都不影响它。

    ``entry_id == 0`` 表示「内存选区快照」（保留能力）：解释完成后**不写回任何
    词条**（只写解释缓存并把结果交给 UI）。最终交互里浮条的唯一按钮会**先落库**，
    因此正常路径总是 ``entry_id > 0``，结果写回同一条词条。
    """

    entry_id: int
    term: str
    entry_context: str
    request_context: str
    base_url: str
    model: str
    timeout: float
    api_key: str
    source_label: str = ""
    #: 发起解释时该词条所属主题（``batches.id``）。
    #: ``0`` = 旧构造 / 内存选区快照（``entry_id == 0``）：跳过主题归属校验与改名，
    #: 因此旧的 ``ExplainSnapshot(...)`` 调用点一行都不用改。
    batch_id: int = 0
    #: 发起解释时生效的推理强度（``""`` = 请求体不带 reasoning_effort）。
    #: 只影响**缓存键**与解释窗显示的 model_config，不进请求体的其它字段。
    reasoning_effort: str = ""

    @property
    def ad_hoc(self) -> bool:
        """是否是「还没有词条」的内存选区快照。"""
        return int(self.entry_id or 0) <= 0

    @property
    def cache_key(self) -> str:
        return make_cache_key(self.term, self.request_context, self.base_url,
                              model_signature(self.model, self.reasoning_effort))

    @property
    def model_config(self) -> str:
        """解释窗底部那行「模型配置」：换了推理强度要看得见（空强度时与旧口径一致）。"""
        effort = str(self.reasoning_effort or "").strip().lower()
        base = f"{self.base_url}|{self.model}"
        return f"{base}（推理强度 {effort}）" if effort else base


RESULT_OK = "ok"
RESULT_ERROR = "error"
RESULT_STALE = "stale"
#: 结果被更新的选词取代（迟到的解释不得覆盖新词）
RESULT_SUPERSEDED = "superseded"


class ExplainService:
    def __init__(self, db, config, on_result=None):
        """:param on_result: 回调 ``(entry_id, status, result_or_none, error_or_none)``。

        已有词条的结果一律走这条回调（成功 / 失败 / 过期都一样），
        UI 按 ``entry_id`` 刷新卡片并写回同一条词条。
        需要「结果窗只在 entry + 请求 token 同时匹配时才更新」的调用方
        （``App``）用 :meth:`set_entry_result_sink` 注册带 token 的回调，
        此时本回调不再触发（两选一，绝不重复投递）。
        """
        self.db = db
        self.config = config
        self._on_result = on_result
        self._on_token_result = None
        self._on_entry_result = None
        self._lock = threading.RLock()
        self._inflight: set[int] = set()
        self._token_inflight: set[int] = set()
        self._next_token = 1

    def set_token_sink(self, callback) -> None:
        """注册「按 token 归属」的结果回调 ``(token, status, result, error)``。"""
        self._on_token_result = callback

    def set_entry_result_sink(self, callback) -> None:
        """注册「词条 + **请求 token**」的结果回调。

        ``callback(entry_id, status, result, error, token)`` —— 就是旧
        ``on_result`` 的 4 元组**后面追加请求 token**，因此老的 4 元组消费者
        仍然能按位置解包。``token`` 是本次请求的**解释服务请求 token**
        （``explain_entry_async`` 的返回值），与 UI 的选区序号无关。
        解释结果窗据此拒绝「旧请求的迟到结果覆盖新请求状态」。
        注册本回调后 ``on_result`` 不再触发（二选一，绝不重复投递）。
        """
        self._on_entry_result = callback

    # ------------------------------------------------------------- 客户端
    def make_client(self, *, base_url: str | None = None, model: str | None = None,
                    timeout: float | None = None, api_key: str | None = None,
                    reasoning_effort: str | None = None) -> DeepSeekClient:
        """构造客户端。异步路径必须传入**快照**里的配置，而不是当前设置。"""
        return DeepSeekClient(
            base_url=self.config.base_url if base_url is None else base_url,
            model=self.config.model if model is None else model,
            api_key=self.config.api_key() if api_key is None else api_key,
            timeout=self.config.timeout if timeout is None else timeout,
            reasoning_effort=self.config.reasoning_effort
            if reasoning_effort is None else reasoning_effort,
        )

    def is_ready(self) -> tuple[bool, str]:
        if not self.config.has_api_key():
            return False, "尚未配置 API Key"
        if not self.config.base_url:
            return False, "尚未配置 Base URL"
        return True, ""

    # ------------------------------------------------------------- 缓存
    def cache_key(self, term: str, context: str) -> str:
        """按**当前**设置算缓存键（用于查缓存 / 展示）。"""
        return make_cache_key(term, context, self.config.base_url,
                              self.config.model_signature)

    def _lookup_cache_by_key(self, key: str) -> ExplainResult | None:
        row = self.db.get_cache(key)
        if row is None:
            return None
        try:
            examples = json.loads(row["examples"] or "[]")
        except json.JSONDecodeError:
            examples = []
        if not isinstance(examples, list):
            examples = []
        return ExplainResult(
            one_line=row["one_line"],
            detail=row["detail"],
            examples=[str(x) for x in examples],
            raw=row["raw"],
            from_cache=True,
            model_config=f"{row['base_url']}|{row['model']}",
            topic=_cache_topic(row),
        )

    def lookup_cache(self, term: str, context: str) -> ExplainResult | None:
        return self._lookup_cache_by_key(self.cache_key(term, context))

    def store_cache(self, term: str, context: str, result: ExplainResult, raw: str = "",
                    *, base_url: str | None = None, model: str | None = None,
                    reasoning_effort: str | None = None) -> None:
        """写缓存。base_url/model 缺省用当前设置；异步路径必须传快照值。"""
        base = base_url or self.config.base_url
        name = model or self.config.model
        effort = (self.config.reasoning_effort if reasoning_effort is None
                  else reasoning_effort)
        self._put_cache(make_cache_key(term, context, base, model_signature(name, effort)),
                        term, context, result, raw,
                        base_url=base,
                        model=name)

    def _put_cache(self, key: str, term: str, context: str, result: ExplainResult,
                   raw: str, *, base_url: str, model: str) -> None:
        self.db.put_cache(
            cache_key=key,
            term=term,
            context=context,
            base_url=base_url,
            model=model,
            one_line=result.one_line,
            detail=result.detail,
            examples=result.examples,
            raw=raw or result.raw,
            topic=sanitize_topic(getattr(result, "topic", "")),
        )

    # ------------------------------------------------------------- 同步调用
    def explain_sync(self, term: str, context: str, *, use_cache: bool = True) -> ExplainResult:
        context = context or ""
        snapshot = ExplainSnapshot(
            entry_id=0, term=term, entry_context=context, request_context=context,
            base_url=self.config.base_url, model=self.config.model,
            timeout=self.config.timeout, api_key=self.config.api_key(),
            reasoning_effort=self.config.reasoning_effort,
        )
        return self._run(snapshot, use_cache=use_cache)

    # ------------------------------------------------------------- 异步
    def is_inflight(self, entry_id: int) -> bool:
        with self._lock:
            return entry_id in self._inflight

    def is_inflight_token(self, token: int) -> bool:
        with self._lock:
            return int(token) in self._token_inflight

    def snapshot_for(self, entry_id: int) -> ExplainSnapshot | None:
        """把「词条内容 + 当前模型配置」冻结成一份快照。"""
        row = self.db.get_entry(entry_id)
        if row is None:
            return None
        full_context = row["context"] or ""
        return ExplainSnapshot(
            entry_id=entry_id,
            term=row["term"],
            entry_context=full_context,
            request_context=full_context[: self.config.context_chars * 2],
            base_url=self.config.base_url,
            model=self.config.model,
            timeout=self.config.timeout,
            api_key=self.config.api_key(),
            source_label=(row["source_title"] or row["source_app"] or ""),
            # 冻结**发起时**的主题归属：之后词条被移动 / 删除 / 编辑，
            # 旧结果都不能写回，也不能给新主题改名（见 _mark_ok）。
            batch_id=int(row["batch_id"] or 0),
            reasoning_effort=self.config.reasoning_effort,
        )

    def selection_snapshot(self, selection) -> ExplainSnapshot:
        """把**内存里的选区快照**冻结成一份解释快照（``entry_id == 0``）。

        保留能力：最终交互的浮条按钮会先落库再解释，所以正常路径用
        :meth:`snapshot_for` 走词条；这里只给「还没有词条」的场景用。
        """
        context = selection.context or ""
        src = getattr(selection, "source", None)
        return ExplainSnapshot(
            entry_id=0,
            term=selection.term,
            entry_context=context,
            request_context=context[: self.config.context_chars * 2],
            base_url=self.config.base_url,
            model=self.config.model,
            timeout=self.config.timeout,
            api_key=self.config.api_key(),
            source_label=(getattr(src, "title", "") or getattr(src, "app", "") or ""),
            reasoning_effort=self.config.reasoning_effort,
        )

    def explain_snapshot_async(self, snapshot: ExplainSnapshot, *,
                               force: bool = False) -> int | None:
        """对一份快照发起异步解释。返回 token（None 表示没启动）。

        与 :meth:`explain_entry_async` 的区别：接受任意快照，包括
        ``entry_id == 0`` 的「内存选区」；结果回调带 token，UI 用它做归属校验。
        """
        if snapshot is None or not (snapshot.term or "").strip():
            return None
        key = int(snapshot.entry_id or 0)
        with self._lock:
            if key and key in self._inflight:
                return None
            token = self._next_token
            self._next_token += 1
            self._token_inflight.add(token)
            if key:
                self._inflight.add(key)

        ok, msg = self.is_ready()
        if not ok:
            with self._lock:
                self._token_inflight.discard(token)
                self._inflight.discard(key)
            err = ApiError("config", msg)
            if key:
                try:
                    self.db.update_entry(key, explain_status="error",
                                         explain_error=err.display())
                except Exception:  # pragma: no cover
                    log.debug("写入配置错误状态失败", exc_info=True)
                self._finish(key, RESULT_ERROR, None, err, token)
            else:
                self._finish_token(token, RESULT_ERROR, None, err)
            return None

        threading.Thread(
            target=self._token_worker,
            args=(token, snapshot, force),
            name=f"explain-t{token}",
            daemon=True,
        ).start()
        return token

    def _token_worker(self, token: int, snapshot: ExplainSnapshot, force: bool) -> None:
        """按 ``entry_id``（或 ad-hoc token）归属执行一次解释，并送回 UI 线程。

        两条**互斥**的回调路径（用户明确要求，不要混用）：

        * 已有词条（``entry_id > 0``）→ :meth:`_finish`：
          结果按词条归属（带请求 token），UI 用 entry_id 刷新**卡片与详情**，
          结果窗再要求 entry + token 同时匹配，并把释义写回同一个 entry_id；
        * ``entry_id == 0``（内存选区 ad-hoc，保留能力）→ :meth:`_finish_token`：
          结果按服务 token 归属，与选区序号无关。

        成功 / stale / 错误三条路径的归属规则必须完全一致。
        """
        key = int(snapshot.entry_id or 0)
        try:
            try:
                result = self._run(snapshot, use_cache=not force)
            except ApiError as exc:
                log.info("解释失败 token=%s: %s", token, exc.display())
                self._mark_error(snapshot, exc.display())
                self._deliver(token, snapshot, RESULT_ERROR, None, exc)
                return
            except Exception as exc:  # pragma: no cover
                log.exception("解释异常 token=%s", token)
                err = ApiError("unknown", redact(str(exc)))
                self._mark_error(snapshot, err.display())
                self._deliver(token, snapshot, RESULT_ERROR, None, err)
                return

            if snapshot.ad_hoc:
                # 内存选区：没有词条可写回；结果只交给 UI（缓存在 _run 里已写）
                self._deliver(token, snapshot, RESULT_OK, result, None)
                return

            try:
                written = self._mark_ok(snapshot, result)
            except Exception:  # pragma: no cover
                log.exception("写回解释结果失败 #%s", snapshot.entry_id)
                written = False
            if written:
                self._deliver(token, snapshot, RESULT_OK, result, None)
            else:
                log.info("词条 #%s 在解释期间被修改或删除，丢弃过期解释结果",
                         snapshot.entry_id)
                self._deliver(token, snapshot, RESULT_STALE, result, None)
        finally:
            with self._lock:
                self._token_inflight.discard(token)
                if key:
                    self._inflight.discard(key)

    def _deliver(self, token: int, snapshot: ExplainSnapshot, status: str,
                 result, error) -> None:
        """把结果交给正确的归属回调。

        * 已有词条（``entry_id > 0``）→ ``on_result(entry_id, ...)``
          （或带请求 token 的 entry sink）：
          UI 按 entry_id 刷新卡片 / 详情，并写回同一个 entry_id；
        * ``entry_id == 0`` 的内存快照（保留能力）→ 按 token 回调。

        两条路径**互不混用**：词条结果绝不会被当成选区结果。
        """
        if snapshot.ad_hoc:
            self._finish_token(token, status, result, error)
        else:
            # 请求 token 一并回传：结果窗据此拒绝「旧请求的迟到结果覆盖新请求」
            self._finish(int(snapshot.entry_id), status, result, error, token)

    def explain_entry_async(self, entry_id: int, *, force: bool = False) -> int | None:
        """发起一次「词条」解释。返回**请求 token**（None 表示没启动）。

        token 属于**解释服务自己的请求计数器**，与 UI 的选区序号完全独立：
        结果归属看 ``on_result(entry_id, ...)`` 里的 ``entry_id``；
        解释结果窗另外用 :meth:`set_entry_result_sink` 拿到
        ``(entry_id, token, ...)``，只有「当前 entry + 当前请求 token」同时匹配
        才更新 —— 于是「重新解释」生成新 token 后，排队中的旧结果不会覆盖新状态。
        浮条上的唯一按钮「解释并记录」在落库之后就是通过这条路径解释并写回同一个
        entry_id 的。
        """
        snapshot = self.snapshot_for(entry_id)
        if snapshot is None:
            err = ApiError("config", "词条不存在")
            self._finish(entry_id, RESULT_ERROR, None, err, None)
            return None
        return self.explain_snapshot_async(snapshot, force=force)

    def _finish_token(self, token: int, status: str, result, error) -> None:
        cb = self._on_token_result
        if cb is None:
            return
        try:
            cb(token, status, result, error)
        except Exception:  # pragma: no cover
            log.exception("解释结果（token）回调失败")

    # ------------------------------------------------------- 写回前校验
    def _still_current(self, snapshot: ExplainSnapshot) -> bool:
        """词条是否还是发起时的那一条（未被编辑 / 移动 / 删除）。

        * ``term`` / ``context`` 变了 = 被编辑 → 旧结果作废；
        * ``batch_id`` 变了 = 被移动到别的主题 → 旧结果同样作废
          （绝不给新主题改名、也不写回新组里的这条词）；
        * ``snapshot.batch_id == 0``（旧构造 / 内存快照）→ 跳过主题校验，
          保持旧语义。

        ``entry_id == 0`` 的内存选区快照没有词条可写回，恒为 False。
        """
        if snapshot.ad_hoc:
            return False
        row = self.db.get_entry(snapshot.entry_id)
        if row is None:
            return False
        if (row["term"] or "") != (snapshot.term or ""):
            return False
        if (row["context"] or "") != (snapshot.entry_context or ""):
            return False
        if snapshot.batch_id and int(row["batch_id"] or 0) != int(snapshot.batch_id):
            return False
        return True

    def _mark_ok(self, snapshot: ExplainSnapshot, result: ExplainResult) -> bool:
        """写回解释结果；可选的 ``topic`` 与写回**同锁 / 同事务 / 同条件**完成。

        安全边界（与旧的 UI 回调起名相比，判据从「现在的库」换成了
        **发起请求时的快照**）：

        * 词条被编辑 / 删除 / **移动到别的主题** → 一条都不写、也不改名，
          返回 False（调用方按 ``stale`` 广播）；
        * ``topic`` 为空 / 是坏值（``sanitize_topic`` 拒绝）→ 只忽略改名，
          解释正文与落库完全不受影响；
        * 改名只作用于 ``name_source='auto'`` 的主题行：用户手工改名
          （``manual``）与旧库里的名字（``''``）原样保留，且与自动命名
          共用同一条 SQL 的原子判据，没有竞态窗口；
        * ``snapshot.batch_id == 0``（旧构造 / 内存快照）→ 不做主题改名。
        """
        fields = {
            "explain_status": RESULT_OK,
            "one_line": result.one_line,
            "detail": result.detail,
            "examples": json.dumps(result.examples, ensure_ascii=False),
            "model_config": snapshot.model_config,
            "explain_error": "",
            "explained_at": now_iso(),
        }
        topic = sanitize_topic(getattr(result, "topic", ""))
        writer = getattr(self.db, "apply_explanation", None)
        if callable(writer):
            # 校验 + 写回 + 可选改名在 DB 的同一次事务里完成
            changed = bool(writer(
                snapshot.entry_id,
                expected_term=snapshot.term,
                expected_context=snapshot.entry_context,
                expected_batch_id=snapshot.batch_id,
                fields=fields,
                topic=topic,
            ))
            if changed and topic and snapshot.batch_id:
                log.info("主题 #%s 按解释结果的 topic 命名为《%s》",
                         snapshot.batch_id, topic)
            return changed
        # 兼容极简的 DB 替身（没有 apply_explanation）：退回两步写法，语义一致
        if not self._still_current(snapshot):
            return False
        self.db.update_entry(snapshot.entry_id, **fields)
        return True

    def _mark_error(self, snapshot: ExplainSnapshot, message: str) -> None:
        if not self._still_current(snapshot):
            return
        self.db.update_entry(snapshot.entry_id, explain_status="error",
                             explain_error=message[:1000])

    def _finish(self, entry_id: int, status: str, result, error, token: int | None = None) -> None:
        """按词条广播一次结果（成功 / 失败 / 过期同一条路径）。

        * 注册了 :meth:`set_entry_result_sink`（``App`` 走这条）→ 回调是旧 4 元组
          **追加请求 token**：``(entry_id, status, result, error, token)``，
          结果窗只在 entry 与 token 同时匹配时更新；
        * 否则回退到旧的 ``on_result(entry_id, status, result, error)``：
          卡片 / 详情刷新只按 ``entry_id``（语义未变）。

        两条路径**二选一**，绝不重复投递同一个结果。
        """
        if self._on_entry_result is not None:
            try:
                self._on_entry_result(entry_id, status, result, error, token)
            except Exception:  # pragma: no cover
                log.exception("解释结果（entry+token）回调失败")
            return
        if not self._on_result:
            return
        try:
            self._on_result(entry_id, status, result, error)
        except Exception:  # pragma: no cover
            log.exception("解释结果回调失败")

    # ------------------------------------------------------------- 内部
    def _run(self, snapshot: ExplainSnapshot, *, use_cache: bool) -> ExplainResult:
        """执行一次（可能联网的）解释，缓存读写一律使用快照里的配置。"""
        key = snapshot.cache_key
        if use_cache:
            cached = self._lookup_cache_by_key(key)
            if cached is not None:
                # 归属信息必须来自**本次快照**：解释结果窗与「记录到词典」都用它。
                cached.model_config = snapshot.model_config
                return cached
        client = self.make_client(
            base_url=snapshot.base_url,
            model=snapshot.model,
            timeout=snapshot.timeout,
            api_key=snapshot.api_key,
            reasoning_effort=snapshot.reasoning_effort,
        )
        result = client.explain(snapshot.term, snapshot.request_context)
        # 关键：用**快照**的 base_url/model 写缓存，设置中途切换也不会张冠李戴。
        self._put_cache(key, snapshot.term, snapshot.request_context, result, "",
                        base_url=snapshot.base_url, model=snapshot.model)
        if not getattr(result, "model_config", ""):
            result.model_config = snapshot.model_config
        return result


# --------------------------------------------------- 解释结果 → 词条字段
def explanation_fields(result: ExplainResult) -> dict:
    """把一次解释结果展开成可写进词条的字段。

    「记录到词典」用它：**本次解释的 one_line / detail / examples / model_config
    一起保存**，之后在词条详情里能直接查阅，而不是只剩一句 one_line。
    """
    if result is None:
        return {}
    examples = [str(x) for x in (getattr(result, "examples", None) or []) if str(x).strip()]
    return {
        "explain_status": RESULT_OK,
        "one_line": result.one_line or "",
        "detail": result.detail or "",
        "examples": json.dumps(examples, ensure_ascii=False),
        "model_config": getattr(result, "model_config", "") or "",
        "explain_error": "",
        "explained_at": now_iso(),
    }


def record_explanation(db, entry_id: int, result: ExplainResult) -> bool:
    """把这份解释结果写进**已经存在**的词条（记录动作的一部分）。

    只在词条确实存在时写；失败不抛给 UI（记录本身已经成功），
    返回是否写成功。
    """
    fields = explanation_fields(result)
    if not fields or not entry_id:
        return False
    try:
        if db.get_entry(int(entry_id)) is None:
            return False
        db.update_entry(int(entry_id), **fields)
        return True
    except Exception:  # pragma: no cover - 记录本身已成功，解释回填失败不致命
        log.exception("把解释结果写进词条 #%s 失败", entry_id)
        return False
