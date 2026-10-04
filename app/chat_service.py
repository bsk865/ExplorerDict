"""词条级追问（对话）服务：**用户点「发送」才请求**，纯异步、每个词独立历史。

边界（用户明确要求，勿放宽）
----------------------------
* **只有用户显式动作**才会走到这里（面板详情页底部的「发送」/「重试」）；
  划选、展开、点标签、折叠都不会发起任何请求；
* 请求体只包含：**当前词语 + 短上下文 + 已有解释 + 该词最近有限轮次 + 本次问题**，
  绝不发送窗口标题、文件路径、整篇文档或其它词条（见 ``api_client.build_chat_messages``）；
* 没有 API Key 时**什么都不发**：``submit`` 直接返回 ``None``，由 UI 给出可配置的提示，
  用户输入的问题原样保留（不伪造答案、不读取任何环境变量里的 Key）；
* 历史**按词条归属**：``chat_turns.entry_id`` 决定一切，切词 / 折叠 / 新选区都不会串话；
* 迟到的回答写回**原词条**；词条已被删除 → 结果丢弃（不写库、不显示）；
* 同一个词条在途时重复点「发送」**不会**产生第二次请求（``_inflight`` 闸门）；
* 失败的回答以 ``status='error'`` 存成一条助手轮次，UI 可以提供「重试」，
  重试**不会**重复记录用户那一问（``record_user=False``）；
* 错误信息继续复用 ``ApiError`` + ``redact`` 脱敏，绝不泄露 Key。

请求快照与终态保证（本轮修正）
------------------------------
1. **配置在 submit 时固定**：模型名 / Base URL / Key / 上下文长度 / 历史轮数
   在用户点「发送」的那一刻冻结成 :class:`_RequestSnapshot`。排队期间用户去设置里
   改 model / 换 API，也不会让这次请求「发到一半换了模型」或串到别的配置上。
2. **整条流水线都在 try 里**：客户端构造（``make_client``）、历史构造
   （``history``）、消息构造（``build_messages``）过去在 try 之外 —— 任何一步抛异常
   都会只清 ``_inflight`` 而不发回调，UI 永远停在「正在回答…」。
   现在请求准备、网络调用、写库全部纳入同一段保护，**任何**路径都会走到一个
   终态回调（``ok`` / ``error`` / ``discarded``），且错误文本一律脱敏。
3. **删除词条的晚结果丢弃**：回答回来时词条已不存在 → 不写库、只发 ``discarded``。

服务本身不碰 Tk：结果通过回调交给调用方（``App`` 入 UI 队列）。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from .api_client import ApiError, build_chat_messages
from .logging_setup import get_logger, redact

log = get_logger("chat")

#: 一次追问的归属状态
REQUEST_OK = "ok"
REQUEST_ERROR = "error"
#: 词条已被删除 / 结果不再有意义
REQUEST_DISCARDED = "discarded"

#: 一次请求历史上限（快照里固定，避免排队期间设置变化把历史撑大）
_MAX_SNAPSHOT_TURNS = 50


class _EntryGone(Exception):
    """回答回来时词条已经不在了：结果丢弃（不写库、不显示）。"""


class _PersistFailed(Exception):
    """词条还在，但对话轮次写库失败（数据库关闭 / 磁盘错误）。

    绝不能糊弄成 ``ok``：UI 会收到 ``error`` 终态并提供「重试」。
    """


@dataclass(frozen=True)
class _RequestSnapshot:
    """submit 时刻冻结下来的请求输入（配置 + 词条 + 历史）。

    冻结的意义：用户点「发送」之后再去设置里改模型 / 换 Key / 删词条，
    这次请求仍然按当时的那份配置与内容发出去 —— 请求不会「串配置」，
    也不会因为排队期间库里的变化而改变已经展示给用户的语义。
    """

    entry_id: int
    term: str
    context: str
    explanation: str
    history: tuple
    base_url: str
    model: str
    api_key: str
    timeout: float
    max_history_chars: int
    context_chars: int
    history_turns: int

    def history_list(self) -> list[dict]:
        return [dict(x) for x in self.history]


class ChatService:
    """词条级追问：请求编排 + 历史裁剪 + 归属校验（可整体替换的实现边界）。"""

    def __init__(self, db, config, on_result=None):
        """:param on_result: ``(request_id, entry_id, status, content, error_text)``。"""
        self.db = db
        self.config = config
        self._on_result = on_result
        self._lock = threading.RLock()
        self._inflight: set[int] = set()
        self._next_request_id = 1

    # ------------------------------------------------------------- 回调
    def set_result_sink(self, callback) -> None:
        self._on_result = callback

    def _deliver(self, request_id: int, entry_id: int, status: str, content: str,
                 error_text: str = "") -> None:
        cb = self._on_result
        if cb is None:
            return
        try:
            cb(int(request_id), int(entry_id), status, content, error_text)
        except Exception:  # pragma: no cover - 回调异常不得打断服务
            log.exception("追问结果回调失败")

    # ------------------------------------------------------------- 客户端
    def make_client(self, *, base_url: str | None = None, model: str | None = None,
                    timeout: float | None = None, api_key: str | None = None,
                    reasoning_effort: str | None = None):
        """构造客户端（测试通过替换本方法注入假客户端，绝不联网）。

        没有任何参数时读**当前**配置；请求线程一律把 :meth:`prepare_request`
        冻结下来的值传进来，因此排队期间改设置不会串到在途请求上。
        """
        from .api_client import DeepSeekClient

        return DeepSeekClient(
            base_url=self.config.base_url if base_url is None else base_url,
            model=self.config.model if model is None else model,
            api_key=self.config.api_key() if api_key is None else api_key,
            timeout=self.config.timeout if timeout is None else timeout,
            reasoning_effort=self.config.reasoning_effort
            if reasoning_effort is None else reasoning_effort,
        )

    def is_ready(self) -> tuple[bool, str]:
        if not self.config.chat_enabled:
            return False, "追问功能已在设置中关闭"
        if not self.config.has_api_key():
            return False, "尚未配置 API Key"
        if not self.config.base_url:
            return False, "尚未配置 Base URL"
        return True, ""

    def unavailable_hint(self) -> str:
        """给用户看的提示（可在设置里配置文案；不含任何密钥信息）。"""
        ok, msg = self.is_ready()
        if ok:
            return ""
        hint = self.config.chat_no_key_hint
        return f"{hint}（{msg}）" if msg else hint

    # ------------------------------------------------------------- 状态
    def is_inflight(self, entry_id: int) -> bool:
        with self._lock:
            return int(entry_id) in self._inflight

    def inflight_count(self) -> int:
        with self._lock:
            return len(self._inflight)

    # ------------------------------------------------------------- 历史
    def history(self, entry_id: int, *, turns: int | None = None) -> list[dict]:
        """该词条**最近有限轮次**的对话（已裁剪，只保留可发送的轮次）。

        * 只取 ``entry_id`` 自己的记录（每个词独立历史）；
        * 助手轮次只保留 ``status='ok'`` 的（失败/丢弃的提示文字不发给模型）；
        * 轮数 = ``chat.history_turns``（1 轮 = 用户 + 助手）。
        """
        if not entry_id:
            return []
        rounds = self.config.chat_history_turns if turns is None else max(0, int(turns))
        if rounds <= 0:
            return []
        # 多取一条：本次问题在 ``submit`` 里已经落库，它占掉最新的一行，
        # 不补这一条会让「最近 1 轮」退化成「最近 1 条回答」。
        rows = self.db.list_chat_turns(int(entry_id), limit=rounds * 2 + 1)
        out: list[dict] = []
        for row in rows:
            role = str(row["role"] or "")
            status = str(row["status"] or "ok")
            if role not in ("user", "assistant"):
                continue
            if role == "assistant" and status != "ok":
                continue
            content = str(row["content"] or "").strip()
            if not content:
                continue
            out.append({"role": role, "content": content})
        return out

    def append_turn(self, entry_id: int, role: str, content: str, *,
                    request_id: int = 0, status: str = "ok") -> int | None:
        """写入一轮对话（用户问题 / 助手回答）。词条不存在时不写。

        返回 ``None`` 的两种含义由 :meth:`_write_turn` 统一解释（删除 → discarded、
        写库失败 → error 终态），调用方**不得**把 ``None`` 当成写入成功。

        读库 / 写库自身抛异常（数据库已关闭 / 磁盘错误）同样返回 ``None``：
        这样调用方不会收到「ok」，而是走 ``_PersistFailed`` → ``error`` 终态。
        """
        try:
            if self.db.get_entry(int(entry_id)) is None:
                return None
            return self.db.add_chat_turn(entry_id=int(entry_id), role=str(role),
                                         content=str(content or ""),
                                         request_id=int(request_id or 0),
                                         status=str(status or "ok"))
        except Exception:  # 读库失败 / 写库失败：都交给调用方按终态处理
            log.exception("写入追问轮次失败 #%s", entry_id)
            return None

    def _entry_missing(self, entry_id: int) -> bool:
        """词条是否真的已经不在了（读库本身失败时保守返回 False）。"""
        try:
            return self.db.get_entry(int(entry_id)) is None
        except Exception:
            return False

    def _write_turn(self, entry_id: int, role: str, content: str, *,
                    request_id: int = 0, status: str = "ok") -> int:
        """写一轮对话，并且**必须**检查返回值（本轮修正）。

        ``append_turn`` 返回 ``None`` 有两种可能，绝不能一律当成功：

        * 词条已被删除 → :class:`_EntryGone`（上层走 ``discarded``，不写库不显示）；
        * 词条还在但写库失败（数据库关闭 / 磁盘错误）→ :class:`_PersistFailed`
          （上层走 ``error`` 终态并允许重试）。
        """
        written = self.append_turn(entry_id, role, content,
                                   request_id=request_id, status=status)
        if written is not None:
            return int(written)
        if self._entry_missing(entry_id):
            raise _EntryGone()
        raise _PersistFailed(f"{role} 轮次写入数据库失败（词条仍在，但没有落库）")

    def turn_count(self, entry_id: int) -> int:
        return self.db.count_chat_turns(int(entry_id))

    # ------------------------------------------------------------- 消息
    def build_messages(self, entry_id: int, question: str, *, history=None) -> list[dict]:
        """按「本词 + 短上下文 + 已有解释 + 有限历史 + 本次问题」构造消息。

        纯组装：所有输入（词条快照 / 历史 / 长度上限）都由调用方给定，
        本方法不再自己读配置或库 —— 请求线程用的是 submit 时冻结的那一份。
        """
        row = self.db.get_entry(int(entry_id))
        if row is None:
            return []
        return self.build_messages_from(
            term=(row["term"] or "").strip(),
            question=question,
            context=row["context"] or "",
            explanation="\n".join(
                x for x in [(row["one_line"] or "").strip(), (row["detail"] or "").strip()] if x
            ),
            history=self.history(int(entry_id)) if history is None else history,
            max_chars=self.config.chat_max_history_chars,
            context_chars=self.config.context_chars,
        )

    @staticmethod
    def build_messages_from(*, term: str, question: str, context: str, explanation: str,
                            history, max_chars: int, context_chars: int) -> list[dict]:
        """纯函数版消息组装（不读配置、不读库，便于回归「不串配置」）。"""
        return build_chat_messages(
            term=str(term or ""),
            question=str(question or ""),
            context=str(context or "")[: int(context_chars) * 2],
            explanation=str(explanation or ""),
            history=list(history or []),
            max_chars=int(max_chars),
        )

    # ------------------------------------------------------- 请求快照（冻结）
    def prepare_request(self, entry_id: int, *, history=None) -> _RequestSnapshot | None:
        """在 **submit 时刻**把这次请求需要的一切冻结下来。

        冻结项：词条（词语 / 上下文 / 已有解释）+ 该词历史 + 连接配置与长度上限。
        之后即使用户改模型、换 Key、甚至删掉词条，这次请求的输入也不再变化
        （词条被删的结果在回答回来时按 ``discarded`` 丢弃）。
        返回 ``None`` = 词条不存在（调用方据此不发请求）。
        """
        entry_id = int(entry_id or 0)
        row = self.db.get_entry(entry_id)
        if row is None:
            return None
        turns = int(getattr(self.config, "chat_history_turns", 6) or 0)
        turns = max(0, min(turns, _MAX_SNAPSHOT_TURNS))
        if history is None:
            history = self.history(entry_id, turns=turns)
        return self._snapshot(entry_id, row, history, turns)

    def _snapshot(self, entry_id: int, row, history, turns: int) -> _RequestSnapshot:
        explanation = "\n".join(
            x for x in [(row["one_line"] or "").strip(), (row["detail"] or "").strip()] if x
        )
        cfg = self.config
        return _RequestSnapshot(
            entry_id=int(entry_id),
            term=(row["term"] or "").strip(),
            context=row["context"] or "",
            explanation=explanation,
            history=tuple(dict(x) for x in (history or [])),
            base_url=str(cfg.base_url or ""),
            model=str(cfg.model or ""),
            api_key=str(cfg.api_key() or ""),
            timeout=float(cfg.timeout),
            max_history_chars=int(cfg.chat_max_history_chars),
            context_chars=int(cfg.context_chars),
            history_turns=int(turns),
        )

    # ------------------------------------------------------------- 提交
    def submit(self, entry_id: int, question: str, *, record_user: bool = True) -> int | None:
        """发起一次追问，返回 **request_id**（``None`` = 没有发起）。

        ``None`` 的三种情况（调用方据此给出不同提示）：
        * 配置不完整（没有 Key / 未开启追问）—— UI 保留用户输入并提示去设置；
        * 词条不存在（已被删除）；
        * 该词条已有一问在途（重复点「发送」不会重复请求）。

        **绝不把异常抛给 UI**（本轮修正）：初始的 ``db.get_entry`` / ``is_ready``
        读取（数据库可能已经关闭）与请求准备（配置读不出来 / 写提问失败）全部在
        保护里 —— 任何失败都会清掉在途闸门（可重试），并且只要已经分配了
        request_id，就一定给调用方一个 ``error`` 终态回调，不会停在「正在回答…」。
        """
        question = (question or "").strip()
        entry_id = int(entry_id or 0)
        if not entry_id or not question:
            return None
        request_id = 0
        snapshot = None
        try:
            if self.db.get_entry(entry_id) is None:
                return None
            ok, _msg = self.is_ready()
            if not ok:
                return None
            with self._lock:
                if entry_id in self._inflight:
                    return None
                request_id = self._next_request_id
                self._next_request_id += 1
                self._inflight.add(entry_id)
            try:
                if record_user:
                    # 用户那一问先落库（历史快照里也会带上它，构造消息时再去掉，
                    # 保证「最近 N 轮」在只有一问时不会退化）
                    self._write_turn(entry_id, "user", question,
                                     request_id=request_id, status="ok")
                snapshot = self.prepare_request(entry_id)
            except _EntryGone:
                # 词条在排队期间被删除：不发请求，直接一个终态
                self._release(entry_id)
                self._deliver(request_id, entry_id, REQUEST_DISCARDED, "")
                return None
            except Exception as exc:
                # 准备阶段失败也要有确定的结果：不留「永远在回答」的状态，也不起线程
                log.exception("追问准备失败 request=%s", request_id)
                self._release(entry_id)
                self._finish_error(entry_id, request_id,
                                   self._error_text(exc, secret=self._known_key()),
                                   secret=self._known_key())
                return None
        except Exception as exc:
            # 初始查询 / 配置读取失败（例如数据库已关闭）：不抛给 UI，清在途、可重试。
            # 此时可能连 request_id 都还没分配（没有任何在途请求），那就只清理不回调。
            log.exception("追问提交失败 request=%s", request_id)
            self._release(entry_id)
            if request_id:
                self._finish_error(entry_id, request_id,
                                   self._error_text(exc, secret=self._known_key()),
                                   secret=self._known_key())
            return None
        if snapshot is None:
            # 词条在排队期间被删除：不发请求，直接一个终态
            self._release(entry_id)
            self._deliver(request_id, entry_id, REQUEST_DISCARDED, "")
            return None
        threading.Thread(
            target=self._worker,
            args=(request_id, snapshot, question),
            name=f"chat-r{request_id}",
            daemon=True,
        ).start()
        return request_id

    def retry(self, entry_id: int) -> int | None:
        """重试**最后一次**用户提问：不重复记录那一问，只再发一次请求。"""
        entry_id = int(entry_id or 0)
        if not entry_id:
            return None
        try:
            rows = self.db.list_chat_turns(entry_id, limit=20)
        except Exception:  # pragma: no cover - 读库失败不该炸 UI
            log.exception("读取追问历史失败 #%s", entry_id)
            return None
        last_question = ""
        for row in reversed(rows):
            if str(row["role"]) == "user" and str(row["content"] or "").strip():
                last_question = str(row["content"]).strip()
                break
        if not last_question:
            return None
        return self.submit(entry_id, last_question, record_user=False)

    # ------------------------------------------------------------- 执行
    def _known_key(self) -> str:
        """尽力取出当前配置里的 Key（只用于按值脱敏，绝不写进日志）。

        是**实例方法**（读 ``self.config``）：调用点统一写 ``self._known_key()``，
        配置读取失败（例如数据库已关闭）时返回空串，绝不抛出。
        """
        config = getattr(self, "config", None)
        getter = getattr(config, "api_key", None)
        if not callable(getter):
            return ""
        try:
            return str(getter() or "")
        except Exception:  # pragma: no cover - 数据库关闭时取不出来
            return ""

    def _error_text(self, exc: BaseException, *, secret: str = "") -> str:
        """把任意异常归类成**脱敏**后的用户可见文本（绝不泄露 Key）。

        ``secret`` 是本次请求快照里的 API Key：有些服务商的 Key 不是 ``sk-``
        前缀（正则匹配不到），必须按**值**替换，异常里带出整串也不会泄漏。
        """
        if isinstance(exc, ApiError):
            return redact(exc.display(), secrets=(secret,))
        try:
            raw = str(exc) or exc.__class__.__name__
        except Exception:  # pragma: no cover - __str__ 自身异常
            raw = exc.__class__.__name__
        return redact(raw, secrets=(secret,))

    def _release(self, entry_id: int) -> None:
        with self._lock:
            self._inflight.discard(int(entry_id))

    def _worker(self, request_id: int, snapshot: _RequestSnapshot,
                question: str) -> None:
        """一次追问的全部执行：准备 → 调用 → 写回。

        **整段都在保护里**（本轮修正）：客户端构造 / 历史构造 / 消息构造过去在
        try 之外，任何一步抛异常都只会清 ``_inflight`` 而不发回调，UI 就永远
        停在「正在回答…」。现在无论哪一步失败，都会走 :meth:`_finish_error`
        （脱敏 + 落一条 error 轮次 + 终态回调），用户可以直接点「重试」。
        """
        entry_id = int(snapshot.entry_id)
        delivered = False
        try:
            client = self.make_client(
                base_url=snapshot.base_url, model=snapshot.model,
                timeout=snapshot.timeout, api_key=snapshot.api_key)
            history = snapshot.history_list()
            # 本次问题要么刚被记进历史（record_user=True），要么本来就在最后一条
            # （重试路径）—— 两种情况下都不能在消息里重复出现同一问。
            while history and history[-1]["role"] == "user" \
                    and str(history[-1]["content"]).strip() == question:
                history.pop()
            messages = self.build_messages_from(
                term=snapshot.term, question=question, context=snapshot.context,
                explanation=snapshot.explanation, history=history,
                max_chars=snapshot.max_history_chars,
                context_chars=snapshot.context_chars,
            )
            if not messages:
                self._deliver(request_id, entry_id, REQUEST_DISCARDED, "")
                delivered = True
                return
            answer = self._call_model(client, messages, question, snapshot, history)
            # **必须**检查写库结果：助手轮次写不进去（词条已删 / 数据库关闭）
            # 就绝不能发 ``ok`` —— ``_write_turn`` 会把两种失败区分成
            # ``_EntryGone``（丢弃）与 ``_PersistFailed``（error 终态 + 可重试）。
            self._write_turn(entry_id, "assistant", answer,
                             request_id=request_id, status="ok")
            self._deliver(request_id, entry_id, REQUEST_OK, answer)
            delivered = True
        except _EntryGone:
            # 迟到的回答：词条已被删除 → 不写库、也不显示
            self._deliver(request_id, entry_id, REQUEST_DISCARDED, "")
            delivered = True
        except Exception as exc:  # noqa: BLE001 - 兜底：任何失败都必须有终态回调
            log.exception("追问失败 request=%s", request_id)
            # 快照里的 Key 按**值**脱敏：非 ``sk-`` 前缀的 Key 也能被抹掉
            self._finish_error(entry_id, request_id,
                               self._error_text(exc, secret=snapshot.api_key),
                               secret=snapshot.api_key)
            delivered = True
        finally:
            # ``_inflight`` 是「这一刻不能再发第二条」的闸门：回调发完之后才清，
            # 这样 UI 收到终态回调时闸门已经打开（失败可立刻重试），
            # 而等待 ``not is_inflight`` 的人也能确定**这个线程真的干完了**
            # （否则会在写库中途看到 False，退出/关闭连接时把进程带崩）。
            if not delivered:  # pragma: no cover - 理论上不可达，防御性终态
                try:
                    self._deliver(request_id, entry_id, REQUEST_ERROR, "",
                                  "追问未能完成（内部错误）")
                except Exception:
                    log.exception("追问终态回调失败 request=%s", request_id)
            self._release(entry_id)

    def _call_model(self, client, messages, question: str, snapshot: _RequestSnapshot,
                    history) -> str:
        """调用模型并做写回前的归属校验（词条被删 → :class:`_EntryGone`）。"""
        try:
            raw = client.chat(messages) if hasattr(client, "chat") else \
                client.ask(question, term=snapshot.term, history=history)
        except ApiError:
            raise
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc
        answer = (raw or "").strip()
        if not answer:
            raise RuntimeError("模型返回了空回答")
        # 迟到回答写回**原词条**；词条已删除 → 丢弃
        if self.db.get_entry(snapshot.entry_id) is None:
            raise _EntryGone()
        return answer

    def _finish_error(self, entry_id: int, request_id: int, message: str,
                      *, secret: str = "") -> None:
        """失败也留痕（status='error'），用户可以重试；错误文本已脱敏。

        写库本身也包在保护里：即使数据库不可用，也必须把失败回调发出去，
        否则 UI 会一直停在「正在回答…」。``secret`` 是本次请求快照里的 Key，
        按**值**再做一次脱敏（非 ``sk-`` 前缀的 Key 也不会漏）。
        """
        message = redact(message or "未知错误", secrets=(secret,))[:1000]
        try:
            if self.db.get_entry(int(entry_id)) is not None:
                self.append_turn(entry_id, "assistant", message, request_id=request_id,
                                 status=REQUEST_ERROR)
        except Exception:  # pragma: no cover - 写库失败不阻断终态回调
            log.exception("写入失败的追问轮次失败 #%s", entry_id)
        self._deliver(request_id, entry_id, REQUEST_ERROR, "", message)
