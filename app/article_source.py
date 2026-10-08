"""原文取回与正文识别（**只用标准库**）。

为什么需要这个模块：导图的最终目标不再是「把几个词连起来」，而是「按这一篇
文章的脉络归纳」。归纳必须看得到原文 —— 只拿每个词的 240 字上下文，模型能给出
「词与词的关系」，给不出「这篇文章在讲什么」。所以划词那一刻顺手把这一页的正文
抄一份留下来（:func:`fetch_article` + :func:`extract_article_text`）。

设计边界（刻意为之）：

* **不加任何第三方依赖**：只用 ``urllib.request`` 与 ``html.parser``。正文识别是
  自己写的「文本块密度」打分，不引入 readability 之类的库 —— 依赖清单越短，
  冻结包越小，越不容易在别人机器上装不上（见 tools/build-requirements.txt）。
* **纯函数在前、网络在后**：:func:`extract_article_text` 只吃 HTML 字符串，
  不联网、不碰数据库、不读时钟，因此可以拿固定样本单测；只有
  :func:`fetch_article` 碰网络，而且它把所有的失败都翻译成一句**人话原因**。
* **失败就说失败**：抓不到（付费墙 / 登录后 / JS 渲染 / 纯图片页码）一律返回
  ``ok=False`` + 原因，**绝不**返回「看起来像正文的导航文字」充当原文。
  上层据此如实告诉用户「未取到原文」，而不是硬编一份归纳。
* **只抓用户正在看的那一页**：URL 来自划词时的来源信息，不爬站、不跟站内链接。

隐私与边界：抓到的正文存在本机数据库（``articles`` 表，见 ``app/db.py``），
只用于给用户自己的导图做归纳；发往模型的内容仍由上层
（``app/api_client.py`` 的载荷构造函数）决定，本模块不发送任何东西。
"""
from __future__ import annotations

import gzip
import re
import zlib
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from .logging_setup import get_logger

log = get_logger("article")

#: 一次取回最多读多少字节（防「下整站」）。一篇长文按 UTF-8 也就几百 KB，
#: 1 MiB 足够；超了直接截断并如实标注（不静默假装读完了）。
MAX_BYTES = 1024 * 1024

#: 网络超时（秒）。取原文是**顺手**做的事，宁可抓不到也不能让界面等着。
DEFAULT_TIMEOUT = 8.0

#: 正文长度下限（字符）。低于这个数认为是没抓到正文 —— 首页、导航页、
#: 「请登录后阅读」的遮挡页都会落在这里。
MIN_ARTICLE_CHARS = 200

#: 抓取结果里的 ``status`` 取值。
STATUS_OK = "ok"
STATUS_FAILED = "failed"
#: 这一页压根没有可抓的 http(s) 正文（本地 PDF、桌面软件、输入框手工词）——
#: 不是失败，是「没有这回事」，界面不该报错。
STATUS_SKIP = "skipped"

#: 浏览器 UA：不伪装成爬虫（有些站点直接把非浏览器 UA 的响应换成验证页，
#: 那样抓到的「正文」反而是验证提示，不如老实要桌面页面）。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

#: 明显的非正文容器：整块丢掉。用「开标签 → 配对闭标签」整段删除（见
#: :func:`_drop_element`），因此嵌套的导航 / 页脚也能一次清干净。
_DROP_TAGS = (
    "script", "style", "noscript", "template", "svg", "canvas", "iframe",
    "nav", "header", "footer", "aside", "form", "button", "select",
    "textarea", "label", "figure", "video", "audio", "object", "embed",
    "menu", "dialog", "map", "picture", "source", "track",
)

#: 类名 / id 里带这些词的元素整块丢掉（评论区、推荐位、广告、分享条…）。
#: 只匹配「独立的词」，避免 ``content`` 这种正文常用词被误伤。
_DROP_HINTS = re.compile(
    r"(?:^|[\s_\-])(?:nav|navbar|navigation|menu|sidebar|side-bar|footer|"
    r"header|comment|comments|reply|share|sharing|social|advert|ads|ad|"
    r"banner|promo|recommend|related|breadcrumb|pagination|pager|"
    r"cookie|consent|subscribe|newsletter|popup|modal|toolbar|"
    r"copyright|disclaimer|tags?-list|taglist|"
    r"评论|广告|推荐|相关阅读|导航|页脚|分享|订阅)(?:$|[\s_\-])",
    re.IGNORECASE,
)

#: 块级标签：正文由这些元素里的文字组成。
_BLOCK_TAGS = frozenset({
    "p", "div", "section", "article", "main", "li", "td", "th", "dd", "dt",
    "blockquote", "pre", "figcaption", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "table", "tr", "tbody", "thead", "body",
})

#: 未知/自闭合标签，不参与配对（避免把 ``<br>`` 当成容器把后面全吞掉）。
#: 注意：``br`` **不在这里** —— 它是「段落内换行」的信号，要单独处理。
_VOID_TAGS = frozenset({
    "area", "base", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})

#: 付费墙 / 登录墙 / 风控页的特征词：命中就判定「没抓到正文」。
_BLOCKED_HINTS = (
    "登录后阅读", "请先登录", "注册后继续", "订阅后阅读", "付费阅读",
    "仅限会员", "继续阅读请", "开启 JavaScript", "enable javascript",
    "sign in to continue", "subscribe to continue", "log in to continue",
    "access denied", "are you a robot", "verify you are human",
    "just a moment", "checking your browser", "验证码", "人机验证",
)

_PARA_SPLIT = re.compile(r"\n{2,}")
_WS_RUN = re.compile(r"[ \t\u00a0\u3000]+")
#: 中文字符（含扩展区）——文本密度的重要信号：中文正文段几乎不含标签。
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_PUNCT_RE = re.compile(r"[,.!?;:，。！？；：、）)】》]")
_CHARSET_RE = re.compile(rb'charset\s*=\s*["\']?\s*([\w\-]+)', re.IGNORECASE)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


@dataclass
class Article:
    """一篇取回来的原文。

    ``text`` 是**已经规范化**的正文（连续空格压成一个、段落之间空一行），
    段落结构保留下来是因为它本身就是原文的一部分信息（哪几段相邻）。
    """

    ok: bool
    url: str = ""
    title: str = ""
    text: str = ""
    status: str = STATUS_FAILED
    note: str = ""
    #: 供调试与「怎么抓到的」追溯：命中的是哪个元素 / 用了哪种策略。
    method: str = ""
    #: 实际读到的原始字节数（截断时是 MAX_BYTES）。
    raw_bytes: int = 0
    charset: str = ""

    @property
    def chars(self) -> int:
        return len(self.text)

    def paragraphs(self) -> list[str]:
        """正文段落（已去掉空段）。"""
        return [p for p in (s.strip() for s in _PARA_SPLIT.split(self.text)) if p]

    def locate(self, needle: str, *, span: int = 400) -> str:
        """在原文里找 ``needle``，返回它周围 ``span`` 字的片段；找不到返回空串。

        用途：某个关键词的上下文（划词时抓到的 120 字）与原文对不上时，
        从原文里取它真正所在的那一段 —— 归纳要看的是**原文里的位置**，
        不是划词浮条上截断的那一小截。
        """
        key = (needle or "").strip()
        if not key or not self.text:
            return ""
        at = self.text.find(key)
        if at < 0:
            return ""
        half = max(0, int(span) // 2)
        start = max(0, at - half)
        end = min(len(self.text), at + len(key) + half)
        return self.text[start:end].strip()


def _drop_element(html: str, tag: str) -> str:
    """删掉所有 ``<tag>…</tag>`` 整块（含嵌套的同名标签）。

    为什么要配对而不是「删到下一个 ``</tag>``」：``<div><div>…</div></div>``
    这种嵌套极常见，简单删法会把外层闭标签留下，正文就跟着被吞掉一截。
    """
    open_re = re.compile(rf"<{tag}\b[^>]*>", re.IGNORECASE)
    close_re = re.compile(rf"</{tag}\s*>", re.IGNORECASE)
    out = []
    pos = 0
    while True:
        m = open_re.search(html, pos)
        if m is None:
            out.append(html[pos:])
            break
        out.append(html[pos:m.start()])
        depth = 1
        scan = m.end()
        while depth > 0:
            nxt_open = open_re.search(html, scan)
            nxt_close = close_re.search(html, scan)
            if nxt_close is None:  # 没闭合：删到结尾
                scan = len(html)
                depth = 0
                break
            if nxt_open is not None and nxt_open.start() < nxt_close.start():
                depth += 1
                scan = nxt_open.end()
            else:
                depth -= 1
                scan = nxt_close.end()
        pos = scan
    return "".join(out)


def _drop_self_closing_container(html: str, tag: str) -> str:
    """删掉单个自闭合 / 无闭合的 ``<tag …>``（``<img>``、``<link>`` 等）。"""
    return re.sub(rf"<{tag}\b[^>]*/?>", "", html, flags=re.IGNORECASE)


def _strip_hinted_containers(html: str) -> str:
    """剥掉 class / id 里带「导航 / 评论 / 广告…」提示词的**容器**。

    只处理 div / section / ul / ol / aside 这类容器标签，**不碰 ``<p>``** ——
    正文段落常带 ``class="content"`` 之类，误伤的代价（正文缺一段）远大于收益。

    删掉一个容器后，它内部可能有别的命中容器，因此这里循环到不再命中为止；
    每轮都重新扫，所以不会因为「删了前面、后面下标失效」而漏删。
    """
    pattern = re.compile(
        r"<(div|section|ul|ol|aside|table)\b[^>]*"
        r"(?:class|id)\s*=\s*[\"']([^\"']*)[\"'][^>]*>",
        re.IGNORECASE,
    )
    for _ in range(50):  # 上限纯粹是防呆：真页面嵌套不会这么深
        hit = None
        for m in pattern.finditer(html):
            if _DROP_HINTS.search(m.group(2) or ""):
                hit = m
                break
        if hit is None:
            return html
        html = _drop_element(html[hit.start():], hit.group(1).lower()) + html[:hit.start()]
    return html


def _strip_boilerplate(html: str) -> str:
    """剥掉脚本、样式、导航、页脚、评论、广告等整块内容。"""
    for tag in _DROP_TAGS:
        if tag in _VOID_TAGS:
            html = _drop_self_closing_container(html, tag)
        else:
            html = _drop_element(html, tag)
    return _strip_hinted_containers(html)


class _BlockCollector(HTMLParser):
    """把 HTML 拆成「块级元素的纯文本」。

    输出 ``[(标签, 文本, 嵌套深度), …]``。文本在这里就把实体解码、
    把连续空白压成一个 —— 打分与最终正文用的是同一份文本，避免「按 A 打分、
    输出 B」两套口径。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str, int]] = []
        self._stack: list[str] = []
        self._buf: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "br":
            # 段落内的换行：留在同一个块里（数据里用 \n 表示）。
            self._buf.append("\n")
            return
        if tag in _VOID_TAGS:
            return
        if tag in _BLOCK_TAGS:
            self._flush()
            self._stack.append(tag)
            self._depth += 1
        # 行内标签（a / strong / span / em…）不切块：它们必须留在同一段里

    def handle_startendtag(self, tag, attrs):
        if tag.lower() == "br":
            self._buf.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _BLOCK_TAGS and tag in self._stack:
            while self._stack:
                top = self._stack.pop()
                self._depth = max(0, self._depth - 1)
                if top == tag:
                    break
            self._flush()

    def handle_data(self, data):
        if data:
            self._buf.append(data)

    def _flush(self) -> None:
        text = _normalize_block("".join(self._buf))
        self._buf = []
        if text:
            tag = self._stack[-1] if self._stack else "body"
            self.blocks.append((tag, text, len(self._stack)))

    def close(self) -> None:
        """收尾：把还没被块级闭标签切出来的文字冲成一个块。

        不能省 —— 有些页面（尤其 Markdown 渲染器输出的 HTML）最后一段没有
        ``</p>``，不冲就会丢掉正文的结尾，而那正是文章的结论所在。
        """
        super().close()
        self._flush()


def _normalize_block(text: str) -> str:
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    t = _WS_RUN.sub(" ", t)
    t = "\n".join(line.strip() for line in t.split("\n"))
    return t.strip()


def _score(text: str) -> float:
    """一段文字的「像正文」得分。

    * 太短（< 20 字）基本是按钮、标签、面包屑 ⇒ 0；
    * 中文越多越像正文（导航里中文少、标签多）；
    * 标点越密越像正文（列表项标点少）；
    * 长度取**对数**：长段落占优，但不让一个超长容器碾压一切。
    """
    n = len(text)
    if n < 20:
        return 0.0
    cjk = len(_CJK_RE.findall(text))
    punct = len(_PUNCT_RE.findall(text))
    unit = max(1, n)
    density = min(1.0, cjk * 2.0 / unit)          # 中文占比（中文一个字顶两个字节）
    stops = min(1.0, punct * 12.0 / unit)          # 每 ~8 字一个标点就算很密了
    return (1.0 + density) * (1.0 + stops) * (n ** 0.5)


def _join_blocks(blocks: list[str]) -> str:
    """把选中的块拼成正文：段与段之间空一行。

    相邻两行都比较短（< 60 字）时用换行而不是空行 —— 那多半是同一段被
    ``<br>`` 切开的诗句 / 列表，空行会让它看起来像两个独立段落。
    """
    out: list[str] = []
    for text in blocks:
        if out and len(out[-1]) < 60 and len(text) < 60:
            out.append("\n" + text)
        else:
            out.append("\n\n" + text)
    return "".join(out).strip()


def _clean_document_title(html: str) -> str:
    m = _TITLE_RE.search(html or "")
    if not m:
        return ""
    return _normalize_block(re.sub(r"<[^>]+>", "", m.group(1)))


def extract_article_text(html: str) -> tuple[str, str, str]:
    """从 HTML 里抽出正文。返回 ``(正文, 识别方法, 失败原因)``。

    纯函数：不联网、不碰数据库。策略是**文本块密度**：

    1. 剥掉脚本 / 样式 / 导航 / 页脚 / 评论 / 广告（:func:`_strip_boilerplate`）；
    2. 把剩下的 HTML 拆成块级元素的文字，逐块打分（:func:`_score`）；
    3. 取最高分那块为「正文种子」，再把与它**同级或更深**、
       得分不低于种子 35% 的相邻块一并收进来 —— 一篇文章常被切成
       若干 ``<p>`` 甚至若干 ``<div>``，只留一块会漏掉一半；
    4. 都太低就退回「最长的单块文字」（有些页面整个正文就是一个 ``<div>``）。

    识别不出来时返回空正文 + 原因，**绝不**拿导航文字冒充正文。
    """
    raw = html or ""
    if not raw.strip():
        return "", "", "页面是空的（没有 HTML 内容）"
    stripped = _strip_boilerplate(raw)
    collector = _BlockCollector()
    try:
        collector.feed(stripped)
        collector.close()
    except Exception as exc:  # pragma: no cover - HTMLParser 极少抛，兜底不炸
        log.debug("HTML 解析失败：%s", exc)
        return "", "", "HTML 解析失败"
    blocks = collector.blocks
    if not blocks:
        return "", "", "页面里没有可读的文字（可能是纯图片或脚本渲染）"
    # 登录墙 / 风控页要**先判**：它往往只有十几个字（"登录后阅读全文"），
    # 按长度判会先落进「太短」，于是把「请你登录」说成「文字太少」——用户看到的
    # 原因就错了。判据与 fetch 里那次检测共用 detect_blocked()。
    blocked = detect_blocked("\n".join(text for _tag, text, _depth in blocks))
    if blocked:
        return "", "", blocked

    scored = [(i, _score(text), text) for i, (_tag, text, _depth) in enumerate(blocks)]
    best_i, best_score, _ = max(scored, key=lambda item: item[1])
    if best_score <= 0.0:
        return "", "", "页面文字太短，不像正文（可能是纯图片或脚本渲染）"

    threshold = best_score * 0.35
    chosen: list[int] = [best_i]
    # 往前收
    for i in range(best_i - 1, -1, -1):
        if scored[i][1] >= threshold:
            chosen.append(i)
        elif len(blocks[i][1]) < 20:
            continue          # 空行 / 小标签：跳过，不打断连续性
        else:
            break
    # 往后收
    for i in range(best_i + 1, len(blocks)):
        if scored[i][1] >= threshold:
            chosen.append(i)
        elif len(blocks[i][1]) < 20:
            continue
        else:
            break
    texts = [blocks[i][1] for i in sorted(chosen)]
    text = _join_blocks(texts)
    method = "density"
    if len(text) < MIN_ARTICLE_CHARS:
        # 密度法没凑够：退回「最长单块」，它在单容器页面里通常就是正文
        longest = max(blocks, key=lambda item: len(item[1]))[1]
        if len(longest) > len(text):
            text = longest
            method = "longest-block"
    return text, method, ""


def detect_blocked(text: str) -> str:
    """正文是不是「登录墙 / 风控页」的提示文字。返回原因或空串。"""
    probe = (text or "")[:1200].lower()
    for hint in _BLOCKED_HINTS:
        if hint.lower() in probe:
            return f"页面返回的是「{hint}」这类提示，不是正文"
    return ""

def decode_html(raw: bytes, charset_hint: str = "") -> tuple[str, str]:
    """字节 → 文本。返回 ``(文本, 实际用的编码)``。

    顺序：HTTP 头给的 charset → HTML 里 ``<meta charset>`` → UTF-8 → GBK。
    GB18030 放在最后是因为国内不少老站点是真 GBK，UTF-8 硬解会得到满屏
    ``\ufffd``；放在最后就不会误伤本来就正常的 UTF-8 页面。
    """
    candidates: list[str] = []
    hint = (charset_hint or "").strip().lower()
    if hint:
        candidates.append(hint)
    m = _CHARSET_RE.search(raw[:4096])
    if m:
        found = m.group(1).decode("ascii", "ignore").strip().lower()
        if found and found not in candidates:
            candidates.append(found)
    for extra in ("utf-8", "gb18030"):
        if extra not in candidates:
            candidates.append(extra)
    for enc in candidates:
        try:
            return raw.decode(enc), enc
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace"), "utf-8/replace"


def _decompress(raw: bytes, encoding: str) -> bytes:
    enc = (encoding or "").strip().lower()
    try:
        if enc == "gzip":
            return gzip.decompress(raw)
        if enc == "deflate":
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except (OSError, zlib.error):  # pragma: no cover - 上游标错编码时兜底
        return raw
    return raw


def is_fetchable_url(url: str) -> bool:
    """这个 URL 值不值得去抓（只有 http/https 才抓）。"""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.netloc)


def fetch_article(url: str, *, timeout: float = DEFAULT_TIMEOUT,
                  opener=None, max_bytes: int = MAX_BYTES) -> Article:
    """取回并识别一页的正文。**所有的失败都变成一句人话原因**，不抛异常。

    :param url: 划词时记下的页面地址（原样，不做归一化 —— 归一化后的
        ``doc_key`` 里的 URL 可能已经不是可访问地址）。
    :param opener: 测试注入用（假 opener 实现 ``open(request, timeout=…)``）。
    """
    target = (url or "").strip()
    if not is_fetchable_url(target):
        return Article(ok=False, url=target, status=STATUS_SKIP,
                       note="这一页没有可抓的网页正文（本地文档 / 桌面软件 / 手工输入）")
    req = Request(target, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    })
    opener = opener or build_opener()
    try:
        with opener.open(req, timeout=timeout) as resp:
            charset_hint = ""
            try:
                charset_hint = resp.headers.get_content_charset() or ""
            except Exception:  # pragma: no cover - 假响应对象没有该方法
                charset_hint = ""
            encoding = ""
            try:
                encoding = resp.headers.get("Content-Encoding", "") or ""
            except Exception:  # pragma: no cover
                encoding = ""
            ctype = ""
            try:
                ctype = (resp.headers.get_content_type() or "").lower()
            except Exception:  # pragma: no cover
                ctype = ""
            raw = resp.read(max_bytes)
    except HTTPError as exc:
        return Article(ok=False, url=target,
                       note=f"服务器返回 HTTP {exc.code}（可能需要登录或已失效）")
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        return Article(ok=False, url=target, note=f"连不上这一页：{reason}")
    except TimeoutError:
        return Article(ok=False, url=target, note=f"这一页超过 {timeout:.0f} 秒没响应")
    except Exception as exc:  # pragma: no cover - 其余网络异常一律不炸上层
        return Article(ok=False, url=target, note=f"取回失败：{type(exc).__name__}: {exc}")

    raw = _decompress(raw, encoding)
    if ctype and not (ctype.startswith("text/") or "html" in ctype or "xml" in ctype):
        return Article(ok=False, url=target, status=STATUS_SKIP,
                       note=f"这一页不是网页（{ctype}），没有可归纳的正文",
                       raw_bytes=len(raw))
    text_html, charset = decode_html(raw, charset_hint)
    title = _clean_document_title(text_html)
    text, method, why = extract_article_text(text_html)
    if not text:
        return Article(ok=False, url=target, title=title, note=why,
                       raw_bytes=len(raw), charset=charset)
    blocked = detect_blocked(text)
    if blocked:
        return Article(ok=False, url=target, title=title, note=blocked,
                       raw_bytes=len(raw), charset=charset)
    if len(text) < MIN_ARTICLE_CHARS:
        return Article(
            ok=False, url=target, title=title, text=text, raw_bytes=len(raw),
            charset=charset,
            note=f"只取到 {len(text)} 字，不像正文（可能要登录，或者是脚本渲染的页面）",
        )
    return Article(ok=True, url=target, title=title, text=text,
                   status=STATUS_OK, note="", method=method,
                   raw_bytes=len(raw), charset=charset)
