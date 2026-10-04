"""取词归属与收藏落库。

核心规则（见 SPEC.md FR-2 / FR-3 / FR-4）：
* 前台门控（app/gate.py）放行才会发起 UIA 请求：默认放开普通窗口，
  已知游戏进程 / 全屏 / 用户暂停一律零 UIA 调用；
* 捕获前后校验前台 hwnd/pid，UIA 响应里的 process_id/hwnd 必须与前台一致；
* 忽略本进程窗口（含鼠标点在我们自己浮条上的情况），绝不污染来源状态；
* 用 doc_key 判断「同一个文档」：保留文档身份参数，只剥明确追踪参数；
* **主题（分组）完全自动**：每个阅读页面按 ``doc_key`` 独立成组（URL/文档 id
  优先，进程名 + 去页码标题回退），同页复用、不同来源永不因「主题名相同」而合并；
  新组默认用**本次首个关键词**的简短名，没有 API Key 也能用；
  固定的 / 手工选中的旧接口保留兼容，但**不再**影响新捕获的归属；
* **选词只产生内存快照**：鼠标选词不落库、不建批次、不发网络；只有用户点
  浮条上唯一的按钮「解释并记录」才会走 :meth:`CaptureService.bookmark`
  写 SQLite（先落库、再解释、写回同一个 entry_id）。
"""
from __future__ import annotations

import inspect
import os
import re
import threading
import time
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import source_enrich
from . import win32util as w32
from .gate import AccessGate
from .logging_setup import get_logger
from .models import CapturedSelection, SourceInfo, now_iso

log = get_logger("capture")


def _accepts_three_args(cb) -> bool:
    """回调能不能接住第三个位置参数（前台变化事件的世代指纹）。

    判不出来时按「能」处理：产品装配的 ``App`` 回调就是三参版本，
    轻量替身（``lambda src, kind: …``）才需要退回两参调用。
    """
    try:
        sig = inspect.signature(cb)
    except (TypeError, ValueError):  # pragma: no cover - 内建 / 不可内省的可调用对象
        return True
    positional = 0
    for param in sig.parameters.values():
        if param.kind is inspect.Parameter.VAR_POSITIONAL:
            return True
        if param.kind in (inspect.Parameter.POSITIONAL_ONLY,
                          inspect.Parameter.POSITIONAL_OR_KEYWORD):
            positional += 1
        elif param.kind is inspect.Parameter.KEYWORD_ONLY:
            return False
    return positional >= 3

# 标题里的**页码样式**：只有无歧义的分页标记才剥除，翻页/换页不产生新批次。
# 注意：标题末尾的**单个数字**或**括号数字**（"Report (7)"、"Deck | 12"）
# 可能是完全不同的文档（版本号、卷号、编号），一律保留。
_PAGE_PATTERNS = [
    re.compile(r"\s*[-–—|]\s*\d+\s*/\s*\d+\s*$"),
    re.compile(r"\s*\(\s*\d+\s*/\s*\d+\s*\)\s*$"),
    re.compile(r"\s*[-–—|]\s*(?:Page|page|P|p)\.?\s*\d+\s*(?:of|/)\s*\d+\s*$"),
    re.compile(r"\s*(?:第\s*\d+\s*页(?:\s*[/／]\s*共?\s*\d+\s*页)?)\s*$"),
]

# 仅剥「明确追踪参数」；其余 query（如 ?id=1、?article=2）属于文档身份，必须保留。
_TRACKING_PARAMS = {
    "gclid", "fbclid", "msclkid", "yclid", "dclid", "twclid", "igshid", "mc_eid",
    "mc_cid", "_hsenc", "_hsmi", "mkt_tok", "vero_id", "vero_conv", "wickedid",
    "spm", "scm", "from_source", "share_source", "share_medium", "share_plat",
    "ref_src", "ref_url", "referrer", "sourceid", "s_kwcid", "trk", "trkCampaign",
}
_TRACKING_PREFIXES = ("utm_", "pk_", "piwik_", "matomo_", "hsa_", "_ga", "wt_")

# PDF 页码锚点（Adobe / Chrome 内置阅读器）：#page=3&zoom=... 属于同一文档的翻页。
_PDF_PAGE_FRAGMENT = re.compile(r"^page=\d+(?:[&;].*)?$", re.IGNORECASE)

# 默认端口归一化：https://x.com:443/a 与 https://x.com/a 视为同一文档。
_DEFAULT_PORTS = {"http": "80", "https": "443"}

_PID_EXE_CACHE: dict[int, str] = {}


def _exe_for_pid(pid: int) -> str:
    if pid <= 0:
        return ""
    cached = _PID_EXE_CACHE.get(pid)
    if cached is not None:
        return cached
    exe = w32.get_process_image(pid)
    if len(_PID_EXE_CACHE) > 256:
        _PID_EXE_CACHE.clear()
    _PID_EXE_CACHE[pid] = exe
    return exe


# ------------------------------------------------------------------ 纯函数
def _is_tracking_param(name: str) -> bool:
    n = (name or "").strip().lower()
    if not n:
        return False
    if n in _TRACKING_PARAMS:
        return True
    if n.startswith("utm_"):
        return True
    return any(n.startswith(p) for p in _TRACKING_PREFIXES)


def normalize_url_key(url: str) -> str:
    """URL 归一化：**保留文档身份**，只剥明确追踪参数。

    规则：
    * scheme/host 小写，去掉默认端口；
    * path 保留（含 SPA 路由），仅去掉多余的结尾 ``/``；
    * query **保留**（``?id=1`` 与 ``?id=2`` 是不同文档），只删除 ``utm_*`` /
      ``gclid`` / ``fbclid`` 之类明确的追踪参数；参数顺序归一化；
    * fragment **保留**（``#/spa/route``、``#section`` 可能是不同文档），
      唯一例外是 PDF 页码锚点 ``#page=3``（同一文档翻页）。
    """
    url = (url or "").strip()
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.scheme or not parts.netloc:
        return url

    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    if ":" in netloc:
        host, _, port = netloc.rpartition(":")
        if port.isdigit() and _DEFAULT_PORTS.get(scheme) == port:
            netloc = host

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]

    query = ""
    if parts.query:
        try:
            pairs = parse_qsl(parts.query, keep_blank_values=True)
        except ValueError:
            pairs = []
        kept = [(k, v) for (k, v) in pairs if not _is_tracking_param(k)]
        if kept:
            query = urlencode(sorted(kept))

    fragment = parts.fragment or ""
    if fragment and _PDF_PAGE_FRAGMENT.match(fragment):
        fragment = ""

    return urlunsplit((scheme, netloc, path, query, fragment))


def strip_page_marker(title: str) -> str:
    """剥掉标题末尾**无歧义**的分页标记（详见 _PAGE_PATTERNS 注释）。"""
    t = (title or "").strip()
    for _ in range(3):
        before = t
        for pat in _PAGE_PATTERNS:
            t = pat.sub("", t).strip()
        if t == before:
            break
    return t


def make_doc_key(app: str, url: str, title: str) -> str:
    """文档键：有 URL 用 URL，否则用「进程名 + 去页码标题」。"""
    url_key = normalize_url_key(url)
    if url_key:
        return "url:" + url_key
    t = strip_page_marker(title).lower()
    a = (app or "").strip().lower()
    if not a and not t:
        return ""
    return f"app:{a}|title:{t}"


def confidence_for(url: str) -> str:
    return "url_document" if normalize_url_key(url) else "window_title_only"


_WS_RE = re.compile(r"[ \t\u00a0\u3000]+")
_NL_RE = re.compile(r"\n{3,}")


def normalize_term(text: str) -> str:
    """选区文本规范化：折叠空白与换行。"""
    if not text:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = _WS_RE.sub(" ", t)
    t = _NL_RE.sub("\n\n", t)
    return t.strip()


def normalize_context(text: str) -> str:
    return normalize_term(text)


#: 尾字补齐最多向后走几个字符。一路补到上限还在词里 ⇒ 这不是「少一格」，
#: 宁可按原样返回（宁可少一个字母，也不猜出一个不存在的词）。
WORD_TAIL_LIMIT = 8


def _is_word_char(ch: str) -> bool:
    """ASCII 字母 / 数字才算「词的内部」。

    中文**故意不参与**补齐：汉字之间没有空格，多补一格就可能把下一个词吞进来，
    而英文/数字有明确的词边界，补到边界为止是确定性的。
    """
    return ch.isascii() and ch.isalnum()


def complete_word_tail(term: str, context: str, prefix, *,
                       limit: int = WORD_TAIL_LIMIT) -> str:
    """选区尾部少字符时用上下文把词补完（**只补，绝不改写**已有字符）。

    为什么需要：Chromium 系（Edge / Chrome 的 PDF 阅读器）的 UIA ``TextPattern``
    会给出**少最后一个字符**的选区文本。本机实测：库里最近 10 条词语里 8 条
    正好是 ``context`` 中紧跟其后那个字母的缺格版本（``shiftin`` / ``shifting``、
    ``adherenc`` / ``adherence``、``high computationa`` / ``…computational``）。

    helper 因此额外回传 ``context_prefix``：**选区起点在 ``context`` 里的字符
    偏移**（把起点左移时 ``MoveEndpointByUnit`` 实际移动的格数）。有了它，
    「这个词是不是这个位置的选区、后面还差几个字母」就是**可验证**的，
    不需要任何猜测：

    * ``prefix`` 不可用（旧 helper / 多选区 / 偏移越界）→ 原样返回；
    * ``context[start:]`` 必须真的以 ``term`` 开头，否则原样返回；
    * 只在 ``term`` 末尾是 ASCII 字母/数字、且紧随其后也是 ASCII 字母/数字时补；
    * 一路补到 ``limit`` 仍在词里 ⇒ 原样返回（不是「少一格」）。

    :param term: UIA 给的选区文本（调用方保证已 strip、**未**折叠内部空白：
        坐标系必须与 ``context`` 一致，否则 ``startswith`` 会对不上而放弃）。
    :param context: 原始上下文（与 ``prefix`` 同一坐标系）。
    :param prefix: 选区起点在 ``context`` 中的字符偏移；``None`` / 负数表示未知。
    """
    if not term or not context or prefix is None:
        return term
    try:
        start = int(prefix)
    except (TypeError, ValueError):
        return term
    if start < 0 or start + len(term) > len(context):
        return term
    if not context.startswith(term, start):
        return term
    if not _is_word_char(term[-1]):
        return term
    end = start + len(term)
    tail = 0
    while end + tail < len(context) and _is_word_char(context[end + tail]):
        tail += 1
        if tail > limit:
            return term
    if tail <= 0:
        return term
    return term + context[end:end + tail]


def default_batch_name(source: SourceInfo, when: datetime | None = None) -> str:
    when = when or datetime.now()
    base = (source.title or "").strip() or source.app or "未命名来源"
    base = strip_page_marker(base)
    if len(base) > 40:
        base = base[:40] + "…"
    return f"{base} · {when.strftime('%m-%d %H:%M')}"


#: 自动主题名最长多少个字（「简短名」：一个词/短语，不整段标题）
TOPIC_NAME_MAX = 16


def auto_topic_name(term: str, source: SourceInfo | None = None,
                    when: datetime | None = None, *, limit: int = TOPIC_NAME_MAX) -> str:
    """新自动分组（主题）的默认名字：**本次阅读页面的首个关键词**。

    规则（纯函数，无 key / 无网络也完全可用）：

    * 有词 → 用这个词本身截短成「简短名」（折叠空白、超长截断加省略号）；
    * 没词 → 退回页面短标题（去页码标记），再退回进程名；
    * 都没有 → 「未命名主题」。

    名字只是给人看的标签，从不参与归属判断：归属始终由 ``doc_key`` 决定，
    因此「两个不同来源恰好同名」不会被并到一起。
    """
    body = " ".join(str(term or "").split())
    if body:
        limit = max(1, int(limit))
        if len(body) > limit:
            body = body[:limit] + "…"
        return body
    if source is not None:
        base = strip_page_marker((source.title or "").strip()) or (source.app or "").strip()
        if base:
            limit = max(1, int(limit))
            return base if len(base) <= limit else base[:limit] + "…"
    return "未命名主题"


def _parse_rect(resp: dict) -> tuple[int, int, int, int] | None:
    """从 UIA 响应里取选区矩形（可选增强；拿不到就返回 None，用鼠标点定位）。"""
    raw = resp.get("rect") or resp.get("sel_rect")
    if not raw:
        return None
    try:
        left, top, right, bottom = (int(v) for v in raw)
    except (TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


# ------------------------------------------------------------------ 服务
def resolve_page_scope(service) -> tuple[int | None, bool]:
    """UI 侧共用的**只读**页面范围解析（面板与主窗口必须用同一个判据）。

    返回 ``(batch_id, page_known)``；语义见
    :meth:`CaptureService.current_page_scope`。

    * ``service`` 有 ``current_page_scope`` → 用它（真实路径）；
    * 只有旧接口 ``current_batch_id``（旧替身 / 旧调用点）→ 用它的值，
      有值视为「已知页面」、无值视为「没有来源信息」；
    * 两者都没有（例如只读的面板探针）→ ``(None, False)``：调用方保持
      「全部词语」的历史行为。

    这里**不写任何东西**：不建主题、不写 settings、不改持久状态。
    """
    if service is None:
        return None, False
    getter = getattr(service, "current_page_scope", None)
    if callable(getter):
        try:
            scope = getter()
        except Exception:  # pragma: no cover - 读范围失败不得打断渲染
            log.exception("读取当前页面范围失败")
            return None, False
        if isinstance(scope, (tuple, list)) and len(scope) == 2:
            raw = scope[0]
            try:
                batch_id = int(raw) if raw else None
            except (TypeError, ValueError):
                batch_id = None
            return batch_id, bool(scope[1])
    legacy = getattr(service, "current_batch_id", None)
    if callable(legacy):
        try:
            raw = legacy()
        except Exception:  # pragma: no cover
            return None, False
        try:
            batch_id = int(raw) if raw else None
        except (TypeError, ValueError):
            batch_id = None
        return batch_id, batch_id is not None
    return None, False


class CaptureService:
    """把「一次鼠标手势 / 一次手工录入」变成一条落库的词条。"""

    def __init__(self, db, config, bridge=None, on_event=None, gate=None,
                 on_foreground_change=None):
        """:param on_foreground_change: 回调 ``(src, kind)``：
        ``kind`` ∈ ``{"window", "title"}``。前台换成**另一个窗口**或同一个 HWND
        的**标题变化**（浏览器标签页切换）时触发，UI 据此收起旧浮条并作废旧选区。
        """
        self.db = db
        self.config = config
        self.bridge = bridge
        self.gate = gate or AccessGate(config)
        self._on_event = on_event
        self._on_foreground_change = on_foreground_change
        self._lock = threading.RLock()
        self._current_batch_id: int | None = None
        self._current_doc_key: str = ""
        self._explicit_batch_id: int | None = None
        self._last_source: SourceInfo | None = None
        self._last_selection: CapturedSelection | None = None
        #: 会话内的「页面身份」缓存：``(HWND, 窗口标题) → 最可靠 doc_key``。
        #: ``note_foreground`` 只能拿到窗口标题（浏览器里 URL 要靠 UIA 才知道），
        #: 这里记住 UIA 补全过的身份，轮询产生的标题回退键会被换回它 ——
        #: 否则「URL 身份 ↔ 标题身份」会随每次轮询来回翻转。
        self._identity_keys: dict[tuple[int, str], str] = {}
        self._noted_fg: tuple[int, int] = (0, 0)
        self._noted_title: str = ""
        #: 「通知提交」串行化：watcher 线程与鼠标手势 / UI 线程都会调用
        #: :meth:`note_foreground`，样本交错时旧样本绝不能把 ``last_source``
        #: 写回已经切走的来源。每次调用取一个递增序号，只有最新序号能提交。
        self._note_lock = threading.Lock()
        self._note_seq = 0
        self._note_committed = 0
        self._fail_count = 0
        self.last_failure_reason = ""
        # 门控「世代」：每次进入受限前台 / 前台换窗口 / 外部鼠标按下就 +1，
        # 用于作废在途的手势与 UIA 结果。
        self.generation = 0
        #: **页面世代**：只有「前台换窗口 / 同一窗口换标题（浏览器换标签页）」才 +1。
        #: 与 :attr:`generation` 分开的原因：``generation`` 还会被外部鼠标按下、
        #: 进入受限前台推高，用它判「这份选区属于哪一页」会把新页面的**首选区**
        #: 误判成旧页面。派发到 UI 的 ``foreground_changed`` 事件带着**变化当时**
        #: 的页面世代，UI 只在「当前选区就是在这个世代之前拿到的」时才作废它 ——
        #: 迟到事件因此不会清掉新页面的首选区（见 ``App._hide_overlays_for_foreground_change``）。
        self.page_epoch = 0
        self._restore_from_db()

    # ------------------------------------------------------------- 初始化
    def _restore_from_db(self) -> None:
        bid = self.config.get_int("ui.current_batch_id", 0)
        if bid and self.db.get_batch(bid):
            self._current_batch_id = bid
        else:
            rows = self.db.query("SELECT id FROM batches ORDER BY updated_at DESC LIMIT 1")
            if rows:
                self._current_batch_id = int(rows[0]["id"])
        # 说明：这里**不再**让「固定批次」覆盖当前批次 —— 归属只由页面
        # （``doc_key``）决定，旧数据里的 pinned 标记不再影响新捕获。

    # ------------------------------------------------------------- 事件
    def _emit(self, event: str, payload: dict) -> None:
        if not self._on_event:
            return
        try:
            self._on_event(event, payload)
        except Exception:  # pragma: no cover
            log.exception("事件回调失败: %s", event)

    # ------------------------------------------------------------- 来源
    def note_foreground(self, info: dict | None = None, *,
                        resumed: bool = False) -> SourceInfo | None:
        """前台窗口变化时更新「最近来源」。自身窗口直接忽略。

        三种情况都会让门控世代 +1，并通知 UI **收起旧浮条、作废旧选区**：

        * 前台换成**另一个窗口**（正常窗口 → 正常窗口也算，不能只看硬/软门控翻转）；
        * **同一个 HWND 的标题变化** —— 浏览器里切标签页时 HWND 不变，
          旧选区已经不属于新页面，必须作废；
        * **首次有效来源**（进程启动后第一次看到非自身、非桌面的窗口）——
          旧实现把它当成「没有变化」，于是启动后浮窗与主界面一直停在上一份
          旧主题上。它的事件类型是 ``first``：只让**视图**跟随当前页，不作废
          任何选区（那时还没有选区），世代照样 +1。

        ``resumed=True``：前台从**自身窗口 / 桌面**回到**同一个阅读页**
        （用户去主界面手工浏览主题、或回到桌面后又切回来）。同页发送 ``resume``
        事件：**不递增** ``generation`` / ``page_epoch``，不作废选区、不动窗口，
        只让 UI 恢复「跟随当前阅读页」。

        只靠 ``GateController`` 的 allow/soft/hard 三态翻转是抓不到这些情况的
        （两个正常窗口之间切换，三态始终是 allow），旧实现在那种情况下会把
        上一个窗口的浮条留在屏幕上。

        **提交串行**：``ForegroundWatcher``（0.7s 轮询）与鼠标手势 / UI 线程会
        并发调用本方法，两个样本交错提交时旧样本可能后到并把 ``last_source``
        写回已经切走的来源。这里给每次调用发一个递增序号，只有最新序号的样本
        允许提交；旧样本直接丢弃，绝不回写来源或世代。
        """
        info = info or w32.foreground_info()
        if not info.get("hwnd") or info.get("is_self"):
            return None
        if w32.is_probably_desktop(info["hwnd"]):
            return None
        hwnd = int(info["hwnd"])
        pid = int(info.get("pid") or 0)
        title = info.get("title") or ""
        with self._lock:
            self._note_seq += 1
            seq = self._note_seq
        with self._note_lock:
            with self._lock:
                if seq < self._note_committed:
                    return self._last_source
                self._note_committed = seq
                prev = self._noted_fg
                prev_title = self._noted_title
                self._noted_fg = (hwnd, pid)
                self._noted_title = title
                first = prev[0] == 0
                switched = prev[0] not in (0, hwnd)
                retitled = (not switched) and (not first) and title != prev_title
                changed = bool(switched or retitled or first)
                if changed:
                    # **就地**（同步）递增：前台一变，旧结果当场失效 —— 不能把这个
                    # 递增挪到 UI 消费事件的时候做，否则「前台已经换了、队列里的旧
                    # 选区还没被清」的空档里，旧窗口的 UIA 结果会被当成新页面的。
                    self.generation += 1
                    self.page_epoch += 1
                    change_generation = self.generation
                    change_epoch = self.page_epoch
                else:
                    change_generation = None
                    change_epoch = None
            if switched:
                log.info("前台阅读窗口切换 %s → %s，门控世代 +1（作废旧窗口在途结果）",
                         prev[0], hwnd)
            elif first:
                log.info("首次确认阅读窗口 %s，页面世代 +1（视图跟随当前页）", hwnd)
            elif retitled:
                log.info("同一窗口(%s)标题变化，门控世代 +1（标签页/文档已切换）", hwnd)
            src = SourceInfo(
                hwnd=int(info["hwnd"]),
                pid=int(info["pid"]),
                exe=info.get("exe", ""),
                app=info.get("app", ""),
                title=title,
                url="",
                confidence="window_title_only",
                doc_key=make_doc_key(info.get("app", ""), "", title),
            )
            with self._lock:
                prev_src = self._last_source
                same_page = (not changed and prev_src is not None
                             and int(prev_src.hwnd or 0) == hwnd)
                if same_page:
                    # 同一个阅读页面（HWND 没换、标题也没变）：UIA 已经补全的 URL /
                    # 文档键是**更可靠**的身份，轮询里的标题回退值不得把它冲掉 ——
                    # 否则「URL 身份 → 标题身份 → URL 身份」会随每次轮询来回翻转，
                    # 浮窗与主窗口显示的主题也跟着跳。
                    src.url = prev_src.url
                    src.confidence = prev_src.confidence
                    src.doc_key = prev_src.doc_key
                self._last_source = src
            if not same_page:
                self._remember_identity(hwnd, title, src.doc_key)
            if changed:
                self._notify_foreground_change(
                    src, "first" if first else ("window" if switched else "title"),
                    generation=change_generation, page_epoch=change_epoch)
            elif resumed:
                # 从自身窗口 / 桌面回到同一阅读页：只恢复自动跟随。
                self._notify_foreground_change(src, "resume")
        return src

    def _notify_foreground_change(self, src: SourceInfo, kind: str, *,
                                  generation: int | None = None,
                                  page_epoch: int | None = None) -> None:
        """通知 UI：前台页面变了。

        第三个位置参数是这次变化的**世代指纹** ``{"generation", "page_epoch"}``；
        回调只接受 ``(src, kind)`` 时（旧装配 / 轻量替身）按旧签名调用 ——
        这里**不用** ``except TypeError`` 兜底：那会把回调内部的真实类型错误
        一起吞掉，反而掩盖缺陷。
        """
        cb = self._on_foreground_change
        if cb is None:
            return
        meta = {"generation": generation, "page_epoch": page_epoch}
        try:
            if _accepts_three_args(cb):
                cb(src, kind, meta)
            else:
                cb(src, kind)
        except Exception:  # pragma: no cover - 回调异常不得打断取词
            log.exception("前台变化回调失败")

    def last_source(self) -> SourceInfo | None:
        with self._lock:
            return self._last_source

    def _remember_identity(self, hwnd: int, title: str, key: str) -> None:
        """记住「(HWND, 窗口标题) → 文档键」，供 :meth:`page_doc_key` 顶掉回退键。

        只**升级**不降级：已经确认过的 ``url:`` 身份不会被后来的标题回退键
        （``app:…|title:…``）覆盖 —— 这正是「同页 title 回退与 UIA URL 丰富后的
        身份不在轮询里来回污染」的落点。只是**内存里的会话缓存**（有上限，
        满了整体清空），不写库、不写 settings。
        """
        if not hwnd or not key:
            return
        slot = (int(hwnd), str(title or ""))
        text = str(key)
        with self._lock:
            existing = self._identity_keys.get(slot)
            if existing and existing.startswith("url:") and not text.startswith("url:"):
                return
            if len(self._identity_keys) > 256:
                self._identity_keys.clear()
            self._identity_keys[slot] = text

    def page_doc_key(self, src: SourceInfo) -> str:
        """一个来源的**页面身份**：已确认过的可靠身份优先于标题回退键。

        ``note_foreground`` 的文档键是「进程名 + 窗口标题」回退值（浏览器里
        看不到 URL），而 UIA 取词能补出真正的 ``url:`` 身份。同一个页面在
        轮询里会反复产生回退键，这里用会话缓存把它换回可靠身份；**换了页面
        （HWND + 标题都变）时缓存不会命中**，因此身份只会真的变。
        """
        key = self.doc_key_for(src)
        if key and not key.startswith("app:"):
            return key
        hwnd = int(src.hwnd or 0)
        if not hwnd:
            return key
        with self._lock:
            richer = self._identity_keys.get((hwnd, src.title or ""))
        return richer or key

    def effective_source(self) -> SourceInfo:
        """手工录入/剪贴板导入时使用的来源。

        此刻前台多半是我们自己的窗口 —— 那就沿用「最近一次非自身来源」，
        这正是「跨窗口切到自身不污染来源」的落点。
        """
        info = w32.foreground_info()
        if info.get("hwnd") and not info.get("is_self") and not w32.is_probably_desktop(info["hwnd"]):
            return SourceInfo(
                hwnd=int(info["hwnd"]),
                pid=int(info["pid"]),
                exe=info.get("exe", ""),
                app=info.get("app", ""),
                title=info.get("title", ""),
                confidence="window_title_only",
                doc_key=make_doc_key(info.get("app", ""), "", info.get("title", "")),
            )
        with self._lock:
            if self._last_source:
                return self._last_source
        return SourceInfo(title="(未知来源)", confidence="window_title_only", doc_key="")

    def enrich_source_with_uia(self, src: SourceInfo, uia: dict) -> SourceInfo:
        """用 UIA 结果补全 URL / 标题。UIA 的 URL 才是「真实网页 URL」。"""
        url = (uia.get("url") or "").strip()
        top_title = (uia.get("top_title") or "").strip()
        window_title = src.title or ""
        if top_title and len(top_title) > len(src.title or ""):
            src.title = top_title
        if url:
            src.url = url
        src.confidence = confidence_for(src.url)
        src.doc_key = make_doc_key(src.app, src.url, src.title)
        if url:
            # 把「URL 身份」同时记在**轮询用的窗口标题**和 UIA 标题下：
            # 之后 note_foreground 生成的标题回退键会被换成这个身份。
            self._remember_identity(int(src.hwnd or 0), window_title, src.doc_key)
            self._remember_identity(int(src.hwnd or 0), src.title or "", src.doc_key)
        return src

    # ------------------------------------------------------------- 文档键
    def doc_key_for(self, src: SourceInfo) -> str:
        if src.doc_key:
            return src.doc_key
        return make_doc_key(src.app, src.url, src.title)

    def current_page_scope(self) -> tuple[int | None, bool]:
        """**只读**回答「当前阅读页面的词表该显示什么」。

        返回 ``(batch_id, page_known)``：

        * ``page_known=False``：还没有任何可用的非自身来源（程序刚启动、
          桌面 / 自身窗口在前台）→ 调用方保持「全部词语」的历史行为；
        * ``page_known=True, batch_id=None``：这一页**还没有**保存过任何词 →
          调用方必须显示**空词表**，绝不能退回全库（否则用户会在新页面里
          看到上一篇文章的词，误以为内容归错了组）；
        * ``page_known=True, batch_id=…``：这一页已经有主题 → 直接用它。

        这条路径**零写入**：不建主题、不写 settings、不动任何持久状态，最多
        只在内存里把「当前指针」挪到已经存在的主题上。因此划选、翻页、前台
        轮询都可以放心调用它；真正的落库仍然只发生在用户点「解释并记录」
        之后的 :meth:`resolve_batch`。
        """
        src = self.last_source()
        if src is None:
            return None, False
        key = self.page_doc_key(src)
        if not key:
            return None, False
        with self._lock:
            if self._current_batch_id and key == self._current_doc_key:
                return self._current_batch_id, True
        try:
            row = self.db.find_batch_by_source(key)
        except Exception:  # pragma: no cover - 读库失败按「这一页还没有词」处理
            log.exception("查找当前页面主题失败")
            row = None
        batch_id = int(row["id"]) if row is not None else None
        with self._lock:
            self._current_doc_key = key
            self._current_batch_id = batch_id
        return batch_id, True

    # ------------------------------------------------------------- 批次
    def current_batch_id(self) -> int | None:
        with self._lock:
            return self._current_batch_id

    def set_current_batch(self, batch_id: int | None, *, explicit: bool = False) -> None:
        """切换当前主题（旧称「批次」）。

        .. deprecated:: 归属已完全自动化
            ``explicit`` 参数**不再影响归属**：新捕获一律按页面 ``doc_key``
            自动分组（见 :meth:`resolve_batch`）。这个方法现在只用于
            「程序内把当前指针挪到某个主题」（例如恢复上次浏览），
            传 ``explicit=True`` 只是记录一个历史标记，不会让后续捕获写进来。
        """
        with self._lock:
            self._current_batch_id = batch_id
            row = self.db.get_batch(batch_id) if batch_id else None
            self._current_doc_key = (row["source_key"] if row else "") or ""
            self._explicit_batch_id = int(batch_id) if (explicit and batch_id) else None
        if batch_id:
            self.config.set("ui.current_batch_id", str(batch_id))

    def clear_explicit_batch(self) -> None:
        """清除历史「显式批次」标记（**保留**给旧调用方，不影响自动归属）。"""
        with self._lock:
            self._explicit_batch_id = None

    def explicit_batch_id(self) -> int | None:
        """历史「显式批次」标记；归属逻辑**不再读取**它。"""
        with self._lock:
            return self._explicit_batch_id

    def resolve_batch(self, src: SourceInfo, now: datetime | None = None,
                      term: str = "") -> tuple[int, bool]:
        """按**页面**自动归属：返回 ``(batch_id, 是否新建)``。

        判定顺序（只有一条规则链，没有任何「固定 / 手工选中」优先）：

        1. 当前主题的 ``doc_key`` 与这一页相同 → 复用（同页复用，零写库）；
        2. 库里已有同 ``doc_key`` 的主题 → 复用（同一页面跨天/跨会话仍是同一组）；
        3. 都没有 → 新建一个主题，默认名 = **本次首个关键词**的简短名
           （没词时退回页面短标题；无需 API Key）。

        旧行为（``pinned`` 固定批次 / ``set_current_batch(explicit=True)`` 选中
        优先）已**取消**：用户手工选主题只影响他正在浏览的列表，不会把新的
        捕获内容塞进那个主题。旧数据与旧接口都保留，但不干预自动归属。
        """
        now = now or datetime.now()
        key = self.page_doc_key(src)
        with self._lock:
            if self._current_batch_id and key and key == self._current_doc_key:
                return self._current_batch_id, False

        # 同一可靠来源**永久复用**（跨天、跨会话都复用）。
        if key:
            row = self.db.find_batch_by_source(key)
            if row is not None:
                with self._lock:
                    self._current_batch_id = int(row["id"])
                    self._current_doc_key = key
                self.config.set("ui.current_batch_id", str(int(row["id"])))
                return int(row["id"]), False

        name = auto_topic_name(term, src, now)
        bid = self.db.create_batch(name, key, src.confidence, name_source="auto")
        with self._lock:
            self._current_batch_id = bid
            self._current_doc_key = key
        self.config.set("ui.current_batch_id", str(bid))
        log.info("新建主题 #%s 《%s》(doc_key=%s)", bid, name, key or "-")
        return bid, True

    # ------------------------------------------------------------- 收藏
    def bookmark(self, sel: CapturedSelection) -> tuple[int, bool]:
        """把一次选词写进库。返回 (entry_id, 是否新建)。"""
        src = sel.source
        term = normalize_term(sel.term)
        batch_id, batch_created = self.resolve_batch(src, term=term)
        doc_key = self.page_doc_key(src)
        context = normalize_context(sel.context)

        if doc_key:
            # 新批次第一次接住这个来源时补上 source_key，之后「自动归属」也能找到它
            row = self.db.get_batch(batch_id)
            if row is not None and not (row["source_key"] or ""):
                self.db.set_batch_source_key(batch_id, doc_key)

        if self.config.dedupe:
            existing = self.db.find_entry(batch_id, term, doc_key, context)
            if existing is not None:
                self.db.bump_entry(int(existing["id"]), sel.captured_at)
                with self._lock:
                    self._last_selection = sel
                self._emit(
                    "bookmarked",
                    {
                        "entry_id": int(existing["id"]),
                        "created": False,
                        "batch_id": batch_id,
                        "batch_created": batch_created,
                        "selection": sel,
                    },
                )
                return int(existing["id"]), False

        # B4：同一个概念、**不同的上下文**再次被划到。
        # 默认还是建一条新词条（历史行为）；用户选了「追加上下文」时，把这句话
        # 追加到已有词条上 —— 一个词条收齐它在不同材料里的各种语境，旧解释标记成
        # 需要重新解释（内容变了，旧解释不再对应当前上下文）。
        if self.config.duplicate_action == "append" and context:
            same = self.db.find_entry_by_term(batch_id, term, doc_key)
            if same is not None:
                merged = self.db.append_entry_context(
                    int(same["id"]), context, captured_at=sel.captured_at)
                with self._lock:
                    self._last_selection = sel
                self._emit(
                    "bookmarked",
                    {
                        "entry_id": int(same["id"]),
                        "created": False,
                        "appended": True,
                        "context": merged,
                        "batch_id": batch_id,
                        "batch_created": batch_created,
                        "selection": sel,
                    },
                )
                return int(same["id"]), False

        entry_id = self.db.add_entry(
            batch_id=batch_id,
            term=term,
            context=context,
            doc_key=doc_key,
            source_app=src.app,
            # B6：落库前清洗一次来源标题（剥掉「 - Google Chrome」「.pdf」这类尾巴）。
            # 只清洗存进去的字符串，不动 src.title / doc_key —— 主题归属不能因此改变。
            source_title=source_enrich.clean_document_title(src.title, app=src.app),
            source_url=src.url,
            source_confidence=src.confidence,
            source_note=src.note,
            capture_method=sel.method,
            captured_at=sel.captured_at,
        )
        self.db.log_event("bookmark", f"#{entry_id} {term}")
        with self._lock:
            self._last_selection = sel
        self._emit(
            "bookmarked",
            {
                "entry_id": entry_id,
                "created": True,
                "batch_id": batch_id,
                "batch_created": batch_created,
                "selection": sel,
            },
        )
        return entry_id, True

    def last_selection(self) -> CapturedSelection | None:
        with self._lock:
            return self._last_selection

    # ------------------------------------------------------------- 取词
    def _length_ok(self, term: str) -> bool:
        n = len(term)
        return self.config.min_len <= n <= self.config.max_len

    def gate_decision(self, info: dict | None = None):
        return self.gate.evaluate(info)

    def handle_mouse_press(self, x: int, y: int, when: float = 0.0, *,
                           overlay_hwnds=()) -> dict:
        """全局左键**按下**时在钩子线程里执行的**轻量**处理。

        只做两件事（读窗口元信息，不碰 Tk / 数据库 / UIA）：

        1. 判断这一次按下是不是落在**我们自己的浮层**上（``is_overlay``）——
           用**按下那一刻**的 Win32 ``WindowFromPoint`` 结果与 UI 线程预先
           缓存的浮层 HWND 集合比对。之后 UI 消费这次点击时只依据这个判定，
           **绝不**在浮条已经隐藏之后再做二次 hit-test（那会把外部点击误判成
           「点在浮条上」，浮条就收不掉了），也不需要任何时间宽限；
        2. 只要不是点在我们自己的浮层上，**立刻**让门控世代 +1 ——
           即使浮条还没显示出来，也能让所有在途的 UIA 结果当场作废。
           紧接着的拖动/抬起会用**新的**世代重新取词。
           （``page_epoch`` 只在真的换了前台页面时 +1，外部按下**不动**它：
           新页面上的第一次划选必须仍然属于「当前这一页」。）
        """
        x, y = int(x), int(y)
        try:
            hwnd = w32.window_root_from_point(x, y)
        except OSError:  # pragma: no cover
            hwnd = 0
        overlay = {int(h) for h in (overlay_hwnds or ()) if h}
        is_overlay = bool(hwnd) and int(hwnd) in overlay
        if is_overlay:
            generation = self.current_generation()
        else:
            generation = self.bump_generation()
        return {
            "x": x, "y": y, "when": float(when),
            "hwnd": hwnd, "is_overlay": is_overlay,
            "generation": generation,
            "page_epoch": self.current_page_epoch(),
        }

    def _verify_uia_identity(self, resp: dict, fg: dict) -> str:
        """核对 UIA 响应的 process_id / hwnd 是否确实来自当前前台窗口。

        返回空串表示通过，否则返回失败原因（fail-closed）：
        拿不到可核对的身份信息时也拒绝，避免把别处的选区算到当前文档头上。
        """
        fg_pid = int(fg.get("pid") or 0)
        fg_hwnd = int(fg.get("hwnd") or 0)
        resp_pid = int(resp.get("process_id") or 0)
        resp_hwnd = int(resp.get("hwnd") or 0)

        if resp_pid <= 0 and resp_hwnd <= 0:
            return "uia_identity_missing"
        if resp_pid > 0:
            if resp_pid == os.getpid():
                return "uia_self"
            if fg_pid and resp_pid != fg_pid:
                return f"uia_pid_mismatch({resp_pid}!={fg_pid})"
        if resp_hwnd > 0 and fg_hwnd:
            resp_root = w32.window_root(resp_hwnd)
            fg_root = w32.window_root(fg_hwnd)
            if resp_root and fg_root and resp_root != fg_root:
                return f"uia_hwnd_mismatch({resp_root}!={fg_root})"
        return ""

    def attempt_capture(self, x: int, y: int, kind: str,
                        generation: int | None = None,
                        page_epoch: int | None = None) -> tuple[CapturedSelection | None, str]:
        """UIA 取词，**只返回内存快照**（不落库、不建批次、不发任何网络请求）。

        返回 (selection 或 None, 失败原因)。

        ``page_epoch``：本次手势**排队时**的页面世代。前台在 UIA 调用期间换了
        窗口 / 标签页时返回 ``page_changed`` —— 归属判据是**页面**，不是
        ``generation``（后者还会被外部鼠标按下推高，拿它判页面会把新页面的
        首选区误杀）。同一 HWND 只换了标题（浏览器切标签页）时返回
        ``foreground_title_changed``：``page_epoch`` 可能还没被 0.7s 的
        ``ForegroundWatcher`` 推高，只能靠**读取前后标题不一致**判定。

        门控未通过（游戏/全屏/用户暂停）时**绝不**向 UIA helper 发送任何读取请求
        （零 UIA 调用）。
        """
        if not self.config.capture_enabled:
            return None, "paused"
        decision = self.gate.evaluate()
        if not decision.allowed:
            return None, decision.reason
        if self.bridge is None:
            return None, "bridge_missing"

        info = decision.info
        hwnd = decision.hwnd
        pid = decision.pid
        #: 读取**之前**的前台标题：同一个 HWND 换了标题 = 浏览器切了标签页 /
        #: 换了文档，这份 UIA 结果属于旧页面（见下面的返回）。
        title_before = str((info or {}).get("title") or "")
        gen0 = self.current_generation() if generation is None else int(generation)
        ep0 = self.current_page_epoch() if page_epoch is None else int(page_epoch)
        # 先判**页面**再判门控世代：换页会同时推高两者，页面归属是更具体的
        # 失败原因（``generation`` 还会被外部按下推高，只作兜底）。
        if ep0 != self.current_page_epoch():
            return None, "page_changed"
        if gen0 != self.current_generation():
            return None, "generation_changed"

        # 鼠标落点在自己窗口上（点/拖浮条、主界面）→ 直接排除，
        # 即使前台仍是原应用也不会重复抓取旧选区。
        try:
            target = w32.window_root_from_point(x, y)
        except OSError:  # pragma: no cover
            target = 0
        if target and w32.is_self_window(target):
            return None, "self_window"

        resp = self.bridge.get_selection(
            x, y, context_chars=self.config.context_chars,
            expected_hwnd=hwnd, expected_pid=pid,
        )

        # UIA 调用期间环境可能已经变了：**先回比世代**（用户已经点过别处 /
        # 切过窗口 / 进游戏了），再做门控与前台身份复核。
        if ep0 != self.current_page_epoch():
            return None, "page_changed"
        if gen0 != self.current_generation():
            return None, "generation_changed"
        if not self.config.capture_enabled:
            return None, "paused"
        decision2 = self.gate.evaluate()
        if not decision2.allowed:
            return None, f"gate_closed_during_call:{decision2.reason}"

        fg_after = w32.foreground_info()
        if int(fg_after.get("hwnd") or 0) != hwnd or int(fg_after.get("pid") or 0) != pid:
            return None, "foreground_changed"
        if str(fg_after.get("title") or "") != title_before:
            # 同 HWND、同 PID，只有标题变了：标签页 / 文档已经换过，旧页面的
            # 选区绝不能算到新页面上（``page_epoch`` 可能还没被 watcher 推高）。
            return None, "foreground_title_changed"
        if fg_after.get("is_self"):
            return None, "self_window"

        if not resp.get("ok"):
            return None, resp.get("reason", "uia_failed")

        mismatch = self._verify_uia_identity(resp, fg_after)
        if mismatch:
            return None, mismatch

        #: 原始选区文本（helper 已 strip，但**没有**折叠内部空白）——尾字补齐
        #: 要在**原始坐标系**里做，折叠过空白的字符串和 ``context`` 的偏移对不上。
        raw_text = str(resp.get("text") or "")
        term = normalize_term(raw_text)
        if not term:
            return None, "empty_selection"
        fixed = complete_word_tail(raw_text, str(resp.get("context") or ""),
                                   resp.get("context_prefix"))
        if fixed != raw_text:
            # 补完必须仍然合法才采用：绝不因为「补全」把这次取词判成超长。
            candidate = normalize_term(fixed)
            if candidate and self._length_ok(candidate):
                log.info("选区尾部少字符，已补齐：%r → %r（context 偏移 %s）",
                         raw_text, candidate, resp.get("context_prefix"))
                term = candidate
            else:
                log.info("选区尾部少字符但补齐后不合法，保持原样：%r → %r",
                         raw_text, fixed)
        if not self._length_ok(term):
            return None, f"length_out_of_range({len(term)})"

        src = SourceInfo(
            hwnd=hwnd,
            pid=pid,
            exe=info.get("exe", ""),
            app=info.get("app", ""),
            title=info.get("title", ""),
        )
        src = self.enrich_source_with_uia(src, resp)
        with self._lock:
            self._last_source = src

        sel = CapturedSelection(
            term=term,
            context=normalize_context(resp.get("context", "")),
            method="ui_textpattern",
            source=src,
            captured_at=now_iso(),
            rect=_parse_rect(resp),
        )
        return sel, ""

    def handle_gesture(self, x: int, y: int, kind: str, *, delay: float = 0.12,
                       generation: int | None = None,
                       page_epoch: int | None = None) -> None:
        """在取词工作线程里执行：延迟 → 门控 → UIA → **只发内存快照事件**。

        最终交互（用户要求「划选本身不落库、不联网」）：
        这里**不再**调用 ``bookmark()`` —— 不写 SQLite、不建批次、不发网络。
        只有浮条上唯一的按钮「解释并记录」才会先落库再解释。

        事件契约（UI 侧 ``App._handle_event`` 依赖，勿随意改名）：
        * ``selection_ready``  : {selection, point, kind, generation, page_epoch, hwnd, pid}
        * ``selection_cleared``: {reason, x, y, kind, generation, page_epoch}
        其中 ``generation`` / ``page_epoch`` / ``hwnd`` / ``pid`` 是**捕获时**的
        环境指纹，UI 消费时必须回比当前门控世代、当前页面世代与当前前台窗口，
        过期结果不得显示浮条。
        """
        if not self.config.capture_enabled:
            log.debug("取词已暂停，忽略本次手势")
            return
        if not self.gate.is_allowed():
            log.debug("门控未放行（%s），忽略本次手势", self.gate.evaluate().reason)
            return  # 受限环境下连延迟都不必等
        # 捕获「开始时刻」的世代：发出的指纹必须是这次手势排队时的那一个，
        # 否则中途切窗口后仍会带上最新世代，UI 的世代回比形同虚设。
        if generation is None:
            generation = self.current_generation()
        if page_epoch is None:
            page_epoch = self.current_page_epoch()
        if delay > 0:
            time.sleep(delay)
        if generation != self.current_generation():
            # 门控已翻转 / 用户已经按下别处 / 前台换窗口，丢弃这次手势
            # （在途结果一并作废，否则迟到的 UIA 返回值会把旧浮条又弹出来）
            log.debug("丢弃过期手势 gen=%s（当前 %s）", generation, self.current_generation())
            return
        if page_epoch != self.current_page_epoch():
            log.debug("丢弃过期手势 page=%s（当前 %s）", page_epoch, self.current_page_epoch())
            return
        try:
            sel, reason = self.attempt_capture(x, y, kind, generation=generation,
                                               page_epoch=page_epoch)
        except Exception as exc:  # pragma: no cover
            log.exception("取词异常")
            sel, reason = None, f"exception:{exc}"

        if sel is None:
            self.last_failure_reason = reason
            # 安全门控 / 世代失效 / 页面已换：不打扰用户，也不改变现有浮条
            # （世代失效说明用户已经点了别处或换了窗口，那条旧浮条早就该走了）
            if reason in ("paused", "generation_changed", "page_changed",
                          "foreground_title_changed") \
                    or reason.startswith("gate_closed"):
                return
            self._fail_count += 1
            log.debug("取词未成功: %s (累计 %d)", reason, self._fail_count)
            # 通知 UI 收起上一次的浮条（「新选区为空则隐藏操作条」）。
            self._emit("selection_cleared", {
                "reason": reason, "x": x, "y": y, "kind": kind,
                "generation": generation, "page_epoch": page_epoch,
            })
            return

        with self._lock:
            self._last_selection = sel
        self._emit(
            "selection_ready",
            {
                "selection": sel,
                "point": (int(x), int(y)),
                "kind": kind,
                "reason": "",
                "generation": generation,
                "page_epoch": page_epoch,
                "hwnd": int(sel.source.hwnd or 0),
                "pid": int(sel.source.pid or 0),
            },
        )

    def record_snapshot(self, sel: CapturedSelection) -> tuple[int, bool]:
        """把这份**内存快照**写进原有 SQLite / 批次。返回 (entry_id, 是否新建)。

        这是唯一会把选词落库的路径（浮条上唯一的按钮「解释并记录」
        在解释之前调用它；解释结果窗自身没有任何记录动作）。
        """
        return self.bookmark(sel)

    # ------------------------------------------------------- 手工录入路径
    def manual_entry(self, term: str, context: str = "", source: SourceInfo | None = None,
                     refresh_source: bool = True) -> tuple[int, bool]:
        """手动录入（UIA 失败时的明确兜底）。"""
        term = normalize_term(term)
        if not term:
            raise ValueError("术语不能为空")
        src = source or (self.effective_source() if refresh_source else self.last_source())
        if src is None:
            src = SourceInfo(title="(未知来源)", confidence="window_title_only")
        sel = CapturedSelection(
            term=term,
            context=normalize_context(context),
            method="manual_input",
            source=src,
            captured_at=now_iso(),
        )
        return self.bookmark(sel)

    def clipboard_entry(self, context: str = "") -> tuple[int, bool] | None:
        """用户主动点「从剪贴板导入」时才读剪贴板；程序从不写剪贴板。"""
        text = w32.read_clipboard_text()
        text = normalize_term(text)
        if not text:
            return None
        if len(text) > self.config.max_len:
            text = text[: self.config.max_len]
        return self.manual_entry(text, context=context)

    # ------------------------------------------------------------- 状态
    def fail_count(self) -> int:
        return self._fail_count

    def bump_generation(self) -> int:
        """进入受限前台 / 外部鼠标按下时调用：作废在途手势/结果。

        **不动** ``page_epoch``：手势 / 结果作废是「这一刻的意图」问题，
        页面世代是「这份选区属于哪一页」问题（见 :attr:`page_epoch`）。
        """
        with self._lock:
            self.generation += 1
            return self.generation

    def current_generation(self) -> int:
        with self._lock:
            return self.generation

    def gesture_fingerprint(self) -> tuple[int, int]:
        """``(generation, page_epoch)`` 的**同一时刻**快照（手势入队用）。

        分两次读会在两次读之间漏进一次前台切换：手势就会带着「旧 generation +
        新 page_epoch」这种不可能的组合，归属判定随之出错。
        """
        with self._lock:
            return int(self.generation), int(self.page_epoch)

    def current_page_epoch(self) -> int:
        """当前页面世代（只有前台换窗口 / 换标签页才 +1）。"""
        with self._lock:
            return self.page_epoch


class ForegroundWatcher(threading.Thread):
    """轮询前台窗口，维护「最近来源」。开销极小（不含 UIA 调用）。"""

    def __init__(self, service: CaptureService, interval: float = 0.7):
        super().__init__(name="fg-watcher", daemon=True)
        self.service = service
        self.interval = interval
        self._stop = threading.Event()
        self._last_hwnd = 0
        self._last_title = ""
        #: 自身窗口 / 桌面打断过（用户去主界面手工浏览主题、或回到桌面）：
        #: 下次回到**同一个阅读页**时补一条 ``resume`` 事件，恢复自动跟随。
        #: 同页 resume 不递增任何世代，也不作废选区。
        self._interrupted = False

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                info = w32.foreground_info()
                hwnd = int(info.get("hwnd") or 0)
                if not info.get("is_self") and hwnd and not w32.is_probably_desktop(hwnd):
                    if hwnd != self._last_hwnd or info.get("title") != self._last_title:
                        self._last_hwnd = hwnd
                        self._last_title = info.get("title", "")
                        self._interrupted = False
                        self.service.note_foreground(info)
                    elif self._interrupted:
                        # 从自身窗口 / 桌面回到同一阅读页：用户可能正在主界面
                        # 手工浏览某个主题，视图必须能恢复「跟随当前页」。
                        self._interrupted = False
                        self.service.note_foreground(info, resumed=True)
                else:
                    self._interrupted = True
            except Exception:  # pragma: no cover
                log.debug("前台窗口轮询异常", exc_info=True)
            self._stop.wait(self.interval)
