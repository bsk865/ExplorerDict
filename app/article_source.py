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
* **本地文档不需要联网**（批次 M21-A）：来源是 ``file:///…`` 或一条本地路径
  （``D:\\笔记\\需求.txt``）时走 :func:`read_local_article` —— **直接读磁盘，
  一个数据包都不发**。用户用记事本 / VSCode 写下的草稿本来就是文件，读它有
  原文可归纳；以前这种情况被一律判成「没有可抓的网页正文」，所以导图只能退回
  按词条关系画线 —— 那不是设计，那是个漏洞。本地只认 UTF-8(BOM) / UTF-8 /
  GB18030 三种编码的**文本**文件（``.txt .md .html .htm`` 等）；PDF / Word
  这类二进制格式没有第三方库解析不了，如实说「读不了」，不假装读了。

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
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, build_opener

from .logging_setup import get_logger

log = get_logger("article")

#: 一次取回最多读多少字节（防「下整站」）。一篇长文按 UTF-8 也就几百 KB，
#: 1 MiB 足够；超了直接截断并如实标注（不静默假装读完了）。
MAX_BYTES = 1024 * 1024

#: 本地文档（``file:///…`` 或裸路径）一次最多读多少字节。本地文件是用户自己的
#: 草稿，不必像网页那样防「下整站」，但也别把 1 GB 的日志整个读进内存 ——
#: 超了就只读前这么多，并在 ``note`` 里如实说「只读了前 N 字节」。
MAX_LOCAL_BYTES = 4 * 1024 * 1024

#: 认得出是「文本正文」的本地文件后缀。其余后缀（.pdf/.docx/.png…）一律
#: 老实说读不了 —— 用文本解码器硬解二进制只会得到一堆乱码，那比「没取到」
#: 更糟：模型会把乱码当原文来归纳。
LOCAL_TEXT_SUFFIXES = (
    ".txt", ".text", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv",
    ".json", ".yaml", ".yml", ".ini", ".cfg", ".toml",
    ".html", ".htm", ".xhtml", ".xml",
    ".py", ".js", ".ts", ".java", ".c", ".h", ".cpp", ".cs", ".go", ".rs",
    ".sql", ".sh", ".ps1", ".bat", ".cmd",
)

#: 后缀认不出时，前 512 字节里出现这些就当成文本（用户自己的笔记常常没有后缀）。
_LOCAL_TEXT_HINTS = ("\n", "。", "，", " ", "\t")

#: 网络超时（秒）。取原文是**顺手**做的事，宁可抓不到也不能让界面等着。
DEFAULT_TIMEOUT = 8.0

#: 正文长度下限（字符）。低于这个数认为是没抓到正文 —— 首页、导航页、
#: 「请登录后阅读」的遮挡页都会落在这里。
MIN_ARTICLE_CHARS = 200

#: 抓取结果里的 ``status`` 取值。
STATUS_OK = "ok"
STATUS_FAILED = "failed"
#: 这一页压根没有可归纳的正文：桌面软件窗口、输入框里手工敲的词、二进制文档
#: （PDF / Word）—— 不是失败，是「没有这回事」，界面不该报错。
#: 注意：**本地文本文件不在这一类**（批次 M21-A 起走 :func:`read_local_article`）。
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
    """删掉**第一个** ``<tag>…</tag>`` 整块（含嵌套的同名标签）。

    只删一个元素，调用方自己循环 —— 这一点很要紧：把「继续往后找下一个同类元素」
    也塞进本函数，第一次调用就会顺手把紧跟其后的正文容器一起删掉。

    配对规则（三条都踩过坑）：
    1. 命中点之后第一个闭标签就是它自己的，直接删到那里；
    2. 中间先遇到同名开标签，说明有嵌套，往后按深度配平；
    3. **配平不成（这个块根本没闭合）时一个字都不删**，只把开标签本身吃掉。
       老写法在这种情况下「删到输入末尾」，页面里只要有一个没闭合的
       ``<div class="nav">``，它后面的正文会被整段抹掉 —— 而真实网页上
       「导航容器忘了写闭合标签」并不罕见。
    """
    open_re = re.compile(rf"<{tag}\b[^>]*>", re.IGNORECASE)
    close_re = re.compile(rf"</{tag}\s*>", re.IGNORECASE)
    m = open_re.search(html)
    if m is None:  # 没有这个标签，原样返回
        return html
    after = m.end()
    iopen = open_re.search(html, after)
    iclose = close_re.search(html, after)
    if iclose is None:
        # 整篇都没有闭标签：只吃掉开标签，后面的内容原样留着。
        return html[: m.start()] + html[after:]
    if iopen is None or iclose.start() < iopen.start():
        return html[: m.start()] + html[iclose.end():]
    depth = 2
    scan = iopen.end()
    while depth > 0:
        scan_open = open_re.search(html, scan)
        scan_close = close_re.search(html, scan)
        if scan_close is None:
            # 嵌套里的块没闭合：只吃掉这两个开标签，正文一个字都不动。
            return html[: m.start()] + html[iopen.end():]
        if scan_open is not None and scan_open.start() < scan_close.start():
            depth += 1
            scan = scan_open.end()
        else:
            depth -= 1
            scan = scan_close.end()
    return html[: m.start()] + html[scan:]


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
    for _ in range(100):  # 上限纯粹是防呆：真页面不会有这么多命中容器
        hit = None
        for m in pattern.finditer(html):
            if _DROP_HINTS.search(m.group(2) or ""):
                hit = m
                break
        if hit is None:
            return html
        at = hit.start()
        # 只把「从命中标签开始的那一段」交给 _drop_element：它删掉的是其中
        # **第一个** ``<div …>`` 整块（也就是命中的那个），再拼回前半段。
        stripped = _drop_element(html[at:], hit.group(1).lower())
        if len(stripped) == len(html) - at:  # 什么都没删掉：防呆，别死循环
            return html
        html = html[:at] + stripped
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
        self._annotation_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self._annotation_depth or tag in {'annotation', 'annotation-xml'}:
            self._annotation_depth += 1
            return
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
        if self._annotation_depth:
            self._annotation_depth -= 1
            return
        if tag in _BLOCK_TAGS and tag in self._stack:
            while self._stack:
                top = self._stack.pop()
                self._depth = max(0, self._depth - 1)
                if top == tag:
                    break
            self._flush()

    def handle_data(self, data):
        if data and not self._annotation_depth:
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


def _normalize_plain_text(text: str) -> str:
    """纯文本文件的规范化：**保留段落结构**，不碰行内空格与缩进。

    和 :func:`_normalize_block`（给 HTML 用的）刻意不同：Markdown 的缩进、
    代码块的缩进、中文写作里刻意留的空格都是原文的一部分，压掉就不是原文了。
    这里只统一换行符、去掉每行尾的空格、把连续空行收到一个空行。
    """
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in t.split("\n")]
    t = "\n".join(lines)
    t = re.sub(r"\n{3,}", "\n\n", t)
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
    semantic = None
    for tag in ('article', 'main'):
        if len(re.findall(rf'<{tag}\b', raw, re.I)) == 1:
            semantic = re.search(rf'<{tag}\b[^>]*>(.*)</{tag}\s*>', raw, re.I | re.S)
            if semantic:
                break
    stripped = _strip_boilerplate(semantic.group(1) if semantic else raw)
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

    if semantic:
        return _join_blocks([text for _tag, text, _depth in blocks]), 'semantic-article', ''

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

def decode_bytes(raw: bytes, candidates) -> tuple[str, str]:
    """按 ``candidates`` 给的顺序逐个试解码。返回 ``(文本, 真正用上的编码)``。

    一个能解出来就返回 —— 顺序就是「谁更可信」的排序，调用方负责给对顺序。
    全都解不出来才退回 ``utf-8/replace``（保底不抛异常）。
    """
    for enc in candidates or ():
        name = str(enc or "").strip().lower()
        if not name:
            continue
        try:
            return raw.decode(name), name
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace"), "utf-8/replace"


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
    return decode_bytes(raw, candidates)



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


#: 磁盘盘符开头（``D:\…`` / ``D:/…``）—— 划词时抓到的「来源 URL」经常就是一条
#: 裸路径（记事本、VSCode、Typora 都是这么报的）。
_WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


def is_http_url(url: str) -> bool:
    """值不值得**联网**去抓（只有 http/https 才联网）。"""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.netloc)


def local_path_of(url: str) -> str:
    """从「来源值」里认出**本地文件路径**；认不出返回空串。纯函数：不碰磁盘。

    认三种写法：

    * ``file:///D:/笔记/需求.txt`` → ``D:\\笔记\\需求.txt``（URL 转义会还原）；
    * ``D:\\笔记\\需求.txt`` / ``D:/笔记/需求.txt`` → 原样；
    * ``\\\\服务器\\共享\\需求.txt``（UNC）→ 原样。

    ``file://`` 指向网络位置（``file://server/share/x.txt``）也认，读不到就由
    :func:`read_local_article` 如实报告 —— 那一步是纯本地 I/O，照样不联网。
    """
    raw = (url or "").strip().strip('"')
    if not raw:
        return ""
    parts = None
    try:
        parts = urlsplit(raw)
    except ValueError:  # pragma: no cover - 极罕见：URL 里有非法字符
        parts = None
    if parts is not None and parts.scheme.lower() == "file":
        path = unquote(parts.path or "")
        netloc = (parts.netloc or "").strip()
        if netloc and netloc.lower() != "localhost":
            # file://server/share/x.txt（UNC）：拼回 \\server\share\x.txt
            return "\\\\" + netloc + path.replace("/", "\\")
        if re.match(r"^/[A-Za-z]:", path):
            path = path[1:]                      # /D:/x → D:/x
        return path.replace("/", "\\") if path else ""
    if parts is not None and parts.scheme and len(parts.scheme) > 1:
        return ""                                # 别的协议（ftp:/mailto:…）不管
    if _WINDOWS_PATH_RE.match(raw) or raw.startswith("\\\\"):
        return raw
    return ""


def looks_like_file_name(value: str) -> bool:
    """这个字符串像不像一个**文件名**（纯函数：不碰磁盘）。

    记事本的窗口标题是「新建 文本文档.txt - 记事本」：``src.url`` 是空的，
    标题里只有**裸文件名**、没有目录。批次 M21-A 的 :func:`local_path_of` 只认
    带盘符 / UNC / ``file://`` 的写法，于是这种最普通的用法一个字节都没读到
    （批次 M22-A 的真病根）。

    判定刻意保守：必须有白名单后缀、长度合理、不含路径分隔符与换行，
    再加一条「像文件名的字符」检查 —— 免得把一句普通标题当成文件名去磁盘上找。
    """
    raw = (value or "").strip().strip('"')
    if not raw or len(raw) > 200:
        return False
    if any(ch in raw for ch in ("\n", "\r", "\t")):
        return False
    if raw.startswith("http://") or raw.startswith("https://"):
        return False
    suffix = Path(raw).suffix.lower()
    if suffix not in LOCAL_TEXT_SUFFIXES:
        return False
    stem = Path(raw).stem
    return bool(stem) and any(ch.isalnum() or ch in "._-（）()【】[] " for ch in stem)


def local_name_of(value: str) -> str:
    """从「来源值」里认出一个**裸文件名**；认不出返回空串（纯函数：不碰磁盘）。

    与 :func:`local_path_of` 的分工：那个认**绝对路径**（认出来就能直接读盘），
    这个只认「没有目录、但有文件后缀」的写法 —— 它必须再经过
    :func:`resolve_local_path` 去常见目录里找，才知道文件到底在哪。
    """
    raw = (value or "").strip().strip('"')
    if not raw or not looks_like_file_name(raw):
        return ""
    if _WINDOWS_PATH_RE.match(raw) or raw.startswith("\\\\"):
        return ""                                # 绝对路径归 local_path_of 管
    if "/" in raw or "\\" in raw:
        return ""                                # 带目录的相对路径：先不支持，交给上层如实报
    return raw


#: 裸文件名先在这些目录里找（按顺序）：用户最常把「新建 文本文档.txt」扔在桌面。
LOCAL_SEARCH_DIRS = ("Desktop", "Documents", "Downloads")


def local_search_roots() -> tuple[Path, ...]:
    """找裸文件名时要翻的目录（真实路径；取不到的目录直接跳过）。"""
    home = Path.home()
    roots = [home / name for name in LOCAL_SEARCH_DIRS]
    roots.append(Path.cwd())
    return tuple(roots)


def resolve_local_path(name: str, *, extra_dirs=(), recursive: bool = False) -> str:
    """把「裸文件名」找成一条真实存在的路径；找不到返回空串。

    先看当前工作目录（同名文件就在手边时零成本命中），再按
    :func:`local_search_roots` 逐个查；``extra_dirs`` 由调用方补充（比如刚才
    划词那个进程的工作目录）。``recursive`` 打开时才往下钻一层子目录。
    """
    raw = (name or "").strip().strip('"')
    if not raw:
        return ""
    dirs: list[Path] = []
    for item in extra_dirs or ():
        if item:
            dirs.append(Path(item))
    try:
        dirs.append(Path.cwd())
    except OSError:                              # pragma: no cover - 工作目录被删
        pass
    dirs.extend(local_search_roots())
    seen: set[str] = set()
    for folder in dirs:
        try:
            key = str(folder.resolve()).lower()
        except OSError:                          # pragma: no cover
            continue
        if key in seen:
            continue
        seen.add(key)
        candidate = folder / raw
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:                          # pragma: no cover
            continue
        if not recursive:
            continue
        try:
            for found in folder.glob("*"):
                if not found.is_dir():
                    continue
                nested = found / raw
                if nested.is_file():
                    return str(nested)
        except OSError:                          # pragma: no cover
            continue
    return ""


def read_local_article_by_name(name: str, *, max_bytes: int = MAX_LOCAL_BYTES,
                               extra_dirs=()) -> Article:
    """按**裸文件名**读原文（读盘不联网）；找不到就说清楚为什么。

    这是记事本那条路的最后一公里：文件名认出来了，但磁盘上找不到时**不许
    静默放弃** —— 如实写一行「在桌面 / 文档 / 下载里都没找到这个文件」，
    用户点开「未取到原文」能看到到底卡在哪。
    """
    raw = (name or "").strip().strip('"')
    if not raw:
        return Article(ok=False, url="", status=STATUS_SKIP,
                       note="这一页没有可归纳的正文（桌面软件 / 手工输入）")
    found = resolve_local_path(raw, extra_dirs=extra_dirs)
    if not found:
        return Article(ok=False, url=raw, status=STATUS_FAILED,
                       note=f"认出了文件名「{raw}」，但在桌面 / 文档 / 下载"
                            f"和当前目录里都没找到它（文件可能已改名、被移动，"
                            f"或者还没保存到磁盘）")
    return read_local_article(found, max_bytes=max_bytes, label=raw)


def is_local_source(url: str) -> bool:
    """这条来源值是不是一份**本地文件**（是 → 走读盘，不联网）。"""
    return bool(local_path_of(url))


#: 窗口标题上那种「当前状态」标记（纯装饰，不是文件名的一部分）。
#: 记事本改过还没保存时标题前面挂一个 ``*``；有的编辑器用 ``•`` / ``●``。
#: 只认这几个 —— 加得太野会把一个正常以 ``*`` 开头的文件名改得认不出来。
TITLE_STATE_MARKS = "*•●∙"


def strip_title_marks(value: str) -> str:
    """剥掉窗口标题首尾的「未保存 / 已修改」标记（纯函数）。

    真病根（批次 M25 实测复现）：记事本改过没保存时，窗口标题是
    ``*新建 文本文档.txt - 记事本``；磁盘上的文件叫 ``新建 文本文档.txt``。
    带星号的名字 :func:`looks_like_file_name` 认得出（后缀对、字符也像），
    但 :func:`resolve_local_path` 拿着带星号的名字在桌面里找不到任何文件，
    于是 :func:`source_value_of` 返回空串 ⇒ **一个字都不抓**：留档里连一行
    「没找到」都没有，用户看到的现象就是「导图永远按词条硬凑」。

    实测（2026-10-09，本机）：``source_value_of('', '*新建 文本文档.txt - 记事本')``
    → ``''``；把星号剥掉之后 → ``'新建 文本文档.txt'`` → 桌面上的文件确实读到了。
    """
    text = (value or "").strip()
    return text.strip(TITLE_STATE_MARKS).strip()


def title_candidates(title: str) -> tuple[str, ...]:
    """窗口标题里的**候选来源值**（纯函数）：原样 + 按 ``" - "`` 拆出来的每一段，
    每段再带上一个**剥掉状态标记**的版本（批次 M25）。

    记事本 / Typora / VSCode 这类程序报不出 URL，窗口标题往往是
    「文件全路径 - 程序名」或「文件名 - 程序名」；按 ``" - "`` 拆开逐段试，
    才认得出哪一段是文件。剥标记的那一份必须一起给：标题上的 ``*`` 是程序标的
    「已修改」记号，磁盘上的真名里没有它（见 :func:`strip_title_marks`）。
    """
    text = (title or "").strip()
    if not text:
        return ()
    parts = [text, *(part.strip() for part in text.split(" - "))]
    out: list[str] = []
    for part in parts:
        for candidate in (part, strip_title_marks(part)):
            if candidate and candidate not in out:
                out.append(candidate)
    return tuple(out)


def source_value_of(url: str, title: str, *, extra_dirs=()) -> str:
    """这次划词的来源：**能直接取到原文**的值；取不到返回空串。

    优先 ``url``（浏览器的地址、编辑器报出的文件路径都在这儿），认不出再退一步
    看窗口标题。返回的可能是 http(s) 地址、绝对路径，或一条**裸文件名**
    （交给 :func:`read_local_article_by_name` 去常见目录里找）。
    """
    raw = (url or "").strip()
    if is_http_url(raw):
        return raw
    direct = local_path_of(raw)
    if direct:
        return direct
    for candidate in title_candidates(title):
        if is_http_url(candidate):
            return candidate
        found = local_path_of(candidate)
        if found:
            return found
        name = local_name_of(candidate)
        if name and resolve_local_path(name, extra_dirs=extra_dirs):
            return name
    return ""


def fetch_source(url: str, title: str = "", *, timeout: float = DEFAULT_TIMEOUT,
                 extra_dirs=()) -> Article:
    """取原文的**统一入口**：网址联网抓，本地文件读盘，裸文件名先找再读。

    与 :func:`fetch_article` 的区别只有一条：这里会拿窗口标题当补充线索
    （记事本那种「只有文件名、没有路径」的来源就靠它）。找不到文件时返回
    ``ok=False`` 的留档，note 里写清楚卡在哪一步，**绝不静默放弃**。
    """
    raw = (url or "").strip()
    if is_local_source(raw):
        return read_local_article(raw, max_bytes=MAX_LOCAL_BYTES)
    if raw:
        # 裸文件名（「新建 文本文档.txt」这种只有名字、没有目录的写法）：
        # ``is_local_source`` 认不出来（它不是路径），但它**绝不是网址** ——
        # 必须先按文件名读盘，否则会被 :func:`fetch_article` 当成一个畸形 URL
        # 去联网，拿到一句「取不到正文」的失败记录（批次 M22-A 的真病根）。
        if looks_like_file_name(raw):
            return read_local_article_by_name(raw, max_bytes=MAX_LOCAL_BYTES,
                                              extra_dirs=extra_dirs)
        return fetch_article(raw, timeout=timeout)
    for candidate in title_candidates(title):
        direct = local_path_of(candidate)
        if direct:
            return read_local_article(direct, max_bytes=MAX_LOCAL_BYTES)
        name = local_name_of(candidate)
        if name:
            return read_local_article_by_name(name, max_bytes=MAX_LOCAL_BYTES,
                                              extra_dirs=extra_dirs)
    return Article(ok=False, url="", status=STATUS_SKIP,
                   note="这一页没有可归纳的正文（桌面软件 / 手工输入）")


def is_fetchable_url(url: str) -> bool:
    """这条来源值能不能取到原文（http(s) 联网抓，或本地文件读盘）。

    保留这个名字是因为它是既有调用点的入口；批次 M21-A 起它**不再只认 http**，
    判定本身仍然不碰磁盘、不联网（能不能真的读到由取回那一步说）。
    """
    return is_http_url(url) or is_local_source(url)


def read_local_article(path: str, *, max_bytes: int = MAX_LOCAL_BYTES,
                       label: str = "") -> Article:
    """读一份**本地文本文件**当原文（纯本地 I/O，一个数据包都不发）。

    :param path: 文件路径（``file://`` 与裸路径都先过 :func:`local_path_of`）。
    :param label: 落库时用的「来源值」（通常是划词时记下的原始写法），
        没给就用 ``file:///`` + 路径。
    :return: 和 :func:`fetch_article` 同样的 :class:`Article`；**所有失败都变成
        一句人话原因**，不抛异常。

    编码顺序刻意**不是**网页那套：本地中文笔记用 GB18030 存的很多，而 ``utf-8``
    硬解 GBK 文件**不会报错**（只会得到乱码），所以先认 BOM、再严格 UTF-8、最后
    GB18030 —— 顺序错了会把好文件读成乱码，比读不到更糟。
    """
    raw_value = (path or "").strip()
    resolved = local_path_of(raw_value) or raw_value
    target = str(label or "").strip()
    if not target and resolved:
        # 「来源值」是落库/界面用的可读写法：能查到文件就记成 file:/// 链接，
        # 查不到（不存在 / 权限）就原样记 —— 别伪造一个不存在的地址。
        candidate = Path(resolved)
        target = candidate.as_uri() if candidate.is_absolute() else raw_value
    if not resolved:
        return Article(ok=False, url=target, status=STATUS_SKIP,
                       note="这一份不是本地文件，也没有可抓的网页正文")

    def fail(note: str) -> Article:
        return Article(ok=False, url=target, title=Path(resolved).name, note=note)

    file_path = Path(resolved)
    suffix = file_path.suffix.lower()
    if suffix and suffix not in LOCAL_TEXT_SUFFIXES:
        return fail(f"这是 {suffix} 文件，本地读不了这种格式"
                    "（只认纯文本 / Markdown / HTML 这类文本文件）")
    try:
        if not file_path.exists():
            return fail("找不到这个本地文件（可能已改名、被移动或删掉了）")
        if file_path.is_dir():
            return fail("这是一条文件夹路径，不是文件")
        size = file_path.stat().st_size
        with file_path.open("rb") as handle:
            raw = handle.read(max_bytes)
    except PermissionError:
        return fail("没有权限读这个本地文件（可能被别的程序独占或受系统保护）")
    except OSError as exc:
        return fail(f"读这个本地文件失败：{exc}")

    if not raw:
        return fail("这个本地文件是空的（0 字节）")
    if b"\x00" in raw[:4096]:
        return fail("这个文件看着是二进制（PDF / Word / 压缩包？），本地读不了；"
                    "把它另存成纯文本再试")

    truncated = size > len(raw)
    text, charset = decode_bytes(raw, ("utf-8-sig", "utf-8", "gb18030"))
    title = file_path.stem
    method = "local"
    if suffix in (".html", ".htm", ".xhtml", ".xml"):
        document = _clean_document_title(text)
        if document:
            title = document
        extracted, method, why = extract_article_text(text)
        if not extracted:
            return fail(why or "这个本地网页里没找到正文")
        text = extracted
        method = f"local:{method}"
    text = _normalize_plain_text(text)
    note = ""
    if truncated:
        note = f"文件较大，只读了前 {len(raw)} 字节（共 {size} 字节）"
    if len(text) < MIN_ARTICLE_CHARS:
        if text:
            note = (f"只读到 {len(text)} 字，"
                    + ("本地文件太短，不够当一篇原文"
                       if not note else note))
        else:
            note = "这个本地文件里没有可读的文字（可能是空文档或纯符号）"
        return Article(ok=False, url=target, title=title, text=text,
                       note=note, method=method, raw_bytes=len(raw),
                       charset=charset)
    return Article(ok=True, url=target, title=title, text=text, status=STATUS_OK,
                   note=note, method=method, raw_bytes=len(raw), charset=charset)



def fetch_article(url: str, *, timeout: float = DEFAULT_TIMEOUT,
                  opener=None, max_bytes: int = MAX_BYTES) -> Article:
    """取回并识别一页的正文。**所有的失败都变成一句人话原因**，不抛异常。

    本地文件（``file://`` / 裸路径）自动转到 :func:`read_local_article` ——
    读盘，不联网。

    :param url: 划词时记下的页面地址（原样，不做归一化 —— 归一化后的
        ``doc_key`` 里的 URL 可能已经不是可访问地址）。
    :param opener: 测试注入用（假 opener 实现 ``open(request, timeout=…)``）。
    """
    target = (url or "").strip()
    if is_local_source(target):
        return read_local_article(target, max_bytes=MAX_LOCAL_BYTES)
    if not is_http_url(target):
        return Article(ok=False, url=target, status=STATUS_SKIP,
                       note="这一页没有可归纳的正文（桌面软件 / 手工输入）")
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


# --------------------------------------------------------------------- 长度口径
#
# 正文有**两道**口子，各管各的（批次 M26）：
#
# * **留档**（:func:`clip_article`）：取回来的正文写进 ``articles`` 表之前按
#   ``capture.article_max_chars``（默认 20 万字）留一份。留档的意义就是「这一页
#   的原文」，宁可长一点：3 万字的一篇文章被砍成 3 万，用户以后再想按原文归纳
#   就永远少一截。20 万字能盖住绝大多数文章，再长就是拿数据库换一个几乎不会
#   被读到的尾巴。
# * **发给模型**（``app/api_client.py`` 的 ``ARTICLE_TEXT_CHARS``）：模型窗口有限，
#   只发 3 万字，超了保头保尾。
#
# 两处共用 :func:`clip_text`，所以「省略了多少字」这句话在两边写法完全一致 ——
# 用户在界面上看到的数字和实际发出去的内容对得上。

#: 保头保尾时头部占的比例（批次 M25 定下）。文章的开头交代问题与做法，结尾给
#: 结论，中间往往是例子与推导 —— 掐中间损失最小。
CLIP_HEAD_RATIO = 0.6


def clip_text(text: str, limit: int, *, head_ratio: float = CLIP_HEAD_RATIO) -> str:
    """把正文压到 ``limit`` 字以内：**保头保尾**，中间明说省了多少字。

    为什么不是「从头截到 limit」（旧写法）：文章最要紧的两句话是开头（要解决
    什么问题）与收口（所以结论是什么），而收口句永远在最后。只留开头等于把结论
    丢掉 —— 归纳出来的图会缺一整条因果链（批次 M25 的真实病根）。

    段落边界只在「省下的长度值得」时才收口（收口点必须过半），否则宁可按字符切，
    免得为了一行空行把大段正文浪费掉。
    """
    body = str(text or "")
    cap = max(200, int(limit))
    if len(body) <= cap:
        return body
    head_len = max(1, int(cap * head_ratio))
    tail_len = max(1, cap - head_len)
    head = body[:head_len]
    at = head.rfind("\n\n")
    if at > head_len // 2:
        head = head[:at]
    tail = body[len(body) - tail_len:]
    at = tail.find("\n\n")
    if 0 <= at < tail_len // 2:
        tail = tail[at + 2:]
    omitted = max(0, len(body) - len(head) - len(tail))
    return (head.rstrip()
            + f"\n（正文过长，中间省略 {omitted} 字）\n"
            + tail.lstrip())


def clip_article(article: "Article", limit: int) -> bool:
    """**就地**把这一行留档的正文压到 ``limit`` 字以内；返回是否真的截过。

    只动 ``text``（``chars`` 属性跟着变）并把这件事写进 ``note`` ——
    留档被截过是**事实**，得留下痕迹，不能悄悄少一截字数让以后的人以为
    那就是全文。截断后仍然 ``status='ok'``：它依旧是这一页的原文，只是长了。
    """
    if not isinstance(article, Article) or not article.text:
        return False
    original = len(article.text)
    clipped = clip_text(article.text, limit)
    if clipped == article.text:
        return False
    article.text = clipped
    note = str(article.note or "").strip()
    extra = f"原文 {original} 字，留档按上限 {int(limit)} 字保头保尾"
    article.note = f"{note}；{extra}" if note else extra
    return True


def source_hint_of_doc_key(doc_key: str) -> str:
    """从文档键里取回「当时那一页的地址或窗口标题」（批次 M26）。

    为什么需要它：``entries.source_title`` 记的是**清洗过的文档标题**
    （:func:`app.source_enrich.clean_document_title` 会把 ``.txt`` 这类扩展名去掉），
    于是记事本里的 ``*新建 文本文档.txt - notepad`` 只剩 ``*新建 文本文档`` ——
    拿它去磁盘上找文件永远找不到。而 ``doc_key`` 里**逐字**存着当时的窗口标题
    （``app:notepad.exe|title:*新建 文本文档.txt - notepad``），那才是能找回文件
    的依据。只做字符串解析，不碰磁盘、不联网：解析不出线索就返回 ``""``。
    """
    key = str(doc_key or "").strip()
    if not key:
        return ""
    if key.startswith("url:"):
        return key[len("url:"):].strip()
    marker = "|title:"
    at = key.find(marker)
    if at < 0:
        return ""
    return key[at + len(marker):].strip()
