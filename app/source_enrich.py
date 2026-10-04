"""来源标题自动补全（改进清单 B6）—— **纯离线函数**，不发网络、不读其它进程内存。

用户看到的「来源标题」原本是**窗口标题原文**，于是长这样::

    机器学习入门 - Google Chrome
    第 3 章 概率论.pdf - Adobe Acrobat Reader
    (12) 什么是边际效用？ - YouTube and 8 more pages - 个人 - Microsoft Edge
    未命名 1 - 记事本

对复习和导出（B3 的 CSV / Markdown）来说，这些尾巴是噪声：用户想要的是
「哪篇文档」而不是「哪个程序在显示它」。本模块把这类尾巴剥掉，只保留文档名。

设计边界（对应清单里的隐私约束）：

* **只在用户点「解释并记录」那一刻调用一次**（见
  ``capture_service.bookmark()``），不做后台轮询、不持续采集窗口信息；
* 只处理**已经拿到的字符串**，不新增任何``win32``/UIA 调用 —— 浏览器真实页面标题
  与 PDF 真实 URL 的抓取早就由 :meth:`capture_service.CaptureService.enrich_source_with_uia`
  在同一个时刻完成了；
* 只清洗**落库的标题**，不改 ``SourceInfo.title`` / ``doc_key`` —— 文档键一旦变了，
  同一篇文章会被拆成两个主题，那是比脏标题严重得多的问题。

因此这里全是纯函数，可以脱离 Windows / Tk 直接单测。
"""

from __future__ import annotations

import os
import re

#: 浏览器 / 阅读器 / 编辑器在窗口标题尾部加的程序名。用小写匹配。
#: 顺序无关，长名字在前也不会互相遮住（逐个剥）。
_APP_TAILS = (
    "google chrome",
    "microsoft edge",
    "mozilla firefox",
    "firefox",
    "brave",
    "opera",
    "vivaldi",
    "360安全浏览器",
    "360极速浏览器",
    "qq浏览器",
    "搜狗高速浏览器",
    "adobe acrobat reader",
    "adobe acrobat",
    "adobe reader",
    "foxit pdf reader",
    "foxit reader",
    "foxit phantompdf",
    "sumatrapdf",
    "wps pdf",
    "wps office",
    "microsoft word",
    "microsoft excel",
    "microsoft powerpoint",
    "word",
    "excel",
    "powerpoint",
    "notepad++",
    "notepad",
    "记事本",
    "typora",
    "obsidian",
    "zotero",
    "calibre",
    "kindle",
    "微信读书",
    "百度网盘",
    "google docs",
    "visual studio code",
)

#: Edge / Chrome 的标签页计数尾巴：``and 8 more pages``。
_MORE_PAGES = re.compile(r"\s*(?:and\s+\d+\s+more\s+pages?|\+\s*\d+\s*个标签页?)\s*", re.I)

#: 中间那段「 - 个人 - 」等浏览器资料名；只当它是独立一段时才剥。
_PROFILE_SEGMENTS = {
    "个人", "工作", "profile 1", "profile 2", "profile 3", "person 1", "person 2",
    "default", "默认",
}

#: 当作分隔符的字符（窗口标题里常见）。
_SEPARATORS = (" - ", " – ", " — ", " | ", " · ", " — ", " :: ")

#: 认为「这段是文档文件名」的扩展名（去掉扩展名后当标题用）。
_DOC_EXTS = (
    ".pdf", ".epub", ".mobi", ".azw3", ".djvu", ".txt", ".md", ".markdown",
    ".doc", ".docx", ".odt", ".rtf", ".ppt", ".pptx", ".xls", ".xlsx", ".csv",
)

_WHITESPACE = re.compile(r"\s+")


def _split_segments(text: str) -> list[str]:
    """按常见的标题分隔符切段（保留顺序），顺便去掉空段。"""
    segments = [text]
    for sep in _SEPARATORS:
        nxt: list[str] = []
        for piece in segments:
            nxt.extend(piece.split(sep))
        segments = nxt
    return [s.strip() for s in segments if s and s.strip()]


def _is_app_segment(segment: str) -> bool:
    low = segment.strip().lower()
    if low in _APP_TAILS:
        return True
    # 「Google Chrome」前面可能还带版本号 / 括号说明
    return any(low.startswith(name) and len(low) <= len(name) + 12 for name in _APP_TAILS)


def _strip_doc_ext(segment: str) -> str:
    low = segment.lower()
    for ext in _DOC_EXTS:
        if low.endswith(ext) and len(segment) > len(ext):
            return segment[: -len(ext)].strip()
    return segment


def clean_document_title(raw: str, *, app: str = "") -> str:
    """把窗口标题清洗成「文档标题」。

    规则（按顺序）：

    1. 压掉所有连续空白（含全角空格）；
    2. 去掉 ``and 8 more pages`` / ``+12 个标签页`` 这类计数尾巴；
    3. 按 `` - `` / `` | `` 等分隔符切段，丢掉**程序名段**（:data:`_APP_TAILS`）、
       **浏览器资料名段**（「个人」「Profile 2」…）与空段；
    4. 剩下的段里，最后一段若带文档扩展名（``.pdf`` 等）就去掉扩展名；
    5. 用 `` - `` 重新接起来（多段时保留「章节 - 文档」这类层次）。

    清洗后为空（例如窗口标题就叫「记事本」）时返回 ``app``（应用程序名），
    再不行才返回原样短标题 —— **宁可给个大略的来源，也不要写空字符串**。
    """
    text = _WHITESPACE.sub(" ", str(raw or "").replace("\u3000", " ")).strip()
    if not text:
        return str(app or "").strip()

    text = _MORE_PAGES.sub(" ", text).strip()
    # 标题前后常见的装饰符
    text = text.strip(" \t-–—|·:：")

    segments = _split_segments(text)
    kept: list[str] = []
    for segment in segments:
        if _is_app_segment(segment):
            continue
        if segment.strip().lower() in _PROFILE_SEGMENTS:
            continue
        kept.append(segment)
    if not kept:
        # 整个标题就是程序名 / 资料名 —— 退回应用名更有信息量
        fallback = str(app or "").strip()
        return fallback or text

    cleaned = " - ".join(kept)
    if len(kept) == 1:
        cleaned = _strip_doc_ext(cleaned)
    else:
        cleaned = " - ".join([*kept[:-1], _strip_doc_ext(kept[-1])])
    cleaned = _WHITESPACE.sub(" ", cleaned).strip(" \t-–—|·:：")
    if not cleaned:
        fallback = str(app or "").strip()
        return fallback or text
    return cleaned


def document_kind(raw: str, url: str = "") -> str:
    """粗判来源类型：``"pdf"`` / ``"web"`` / ``""``（未知）。

    只做字符串判断（扩展名、``http`` 前缀、``.pdf`` 出现在 URL 里），
    用于界面/导出里的一句话说明，不参与任何写库决策。
    """
    low_url = str(url or "").lower()
    low_title = str(raw or "").lower()
    if low_url.startswith(("http://", "https://")):
        if low_url.split("?")[0].endswith(".pdf"):
            return "pdf"
        return "web"
    if low_url.startswith("file://"):
        low_url = low_url[7:]
    path = low_url or low_title
    ext = os.path.splitext(path.split("?")[0])[1]
    if ext in _DOC_EXTS:
        return "pdf" if ext == ".pdf" else "doc"
    for doc_ext in _DOC_EXTS:
        if doc_ext in low_title:
            return "pdf" if doc_ext == ".pdf" else "doc"
    return ""
