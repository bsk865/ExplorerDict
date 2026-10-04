"""搜索命中片段（B5）。

用户搜「经济学」，列表里如果只显示词语本身，根本看不出**为什么**命中 ——
这条词可能是靠上下文、来源标题、例子甚至标签命中的。这里回答两件事：

* 命中了**哪个字段**（词语 / 上下文 / 释义 / 来源 / 例子 / 标签…）；
* 命中处周围的**一小段原文**，命中词用 ``【】`` 圈出来（高亮）。

纯函数、不查库、不联网：标签由调用方传进来（标签在关系表里，不在 entries 行上）。
"""
from __future__ import annotations

import json

#: 命中片段左右各留多少字（总长约为它的两倍 + 命中词）
SNIPPET_PAD = 18
#: 字段检查顺序：越靠前越「贴近用户想找的东西」
FIELD_ORDER: tuple[tuple[str, str], ...] = (
    ("term", "词语"),
    ("context", "上下文"),
    ("one_line", "释义"),
    ("detail", "释义"),
    ("examples", "例子"),
    ("source_title", "来源"),
    ("source_url", "来源链接"),
    ("source_app", "来源应用"),
)


def _flat(text) -> str:
    """压掉换行与连续空白（片段要在一行里显示）。"""
    return " ".join(str(text or "").split())


def _examples_flat(raw) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except Exception:
        return _flat(text)
    if isinstance(data, list):
        return _flat("；".join(str(x) for x in data))
    return _flat(text)


def highlight(text: str, query: str) -> str:
    """把 ``text`` 里所有（大小写不敏感的）``query`` 用 ``【】`` 圈起来。"""
    needle = str(query or "").strip()
    hay = str(text or "")
    if not needle or not hay:
        return hay
    low_hay, low_needle = hay.lower(), needle.lower()
    out: list[str] = []
    start = 0
    while True:
        hit = low_hay.find(low_needle, start)
        if hit < 0:
            out.append(hay[start:])
            break
        out.append(hay[start:hit])
        out.append("【" + hay[hit:hit + len(needle)] + "】")
        start = hit + len(needle)
    return "".join(out)


def snippet(text: str, query: str, *, pad: int = SNIPPET_PAD) -> str:
    """截出命中处周围的片段（两端被裁掉时补 ``…``），命中处已加 ``【】``。"""
    flat = _flat(text)
    needle = str(query or "").strip()
    if not flat:
        return ""
    hit = flat.lower().find(needle.lower()) if needle else -1
    if hit < 0:
        # 没有直接命中（例如靠标签命中）→ 只给开头一段
        return flat[: pad * 2] + ("…" if len(flat) > pad * 2 else "")
    start = max(0, hit - pad)
    end = min(len(flat), hit + len(needle) + pad)
    piece = flat[start:end]
    marked = highlight(piece, needle)
    return ("…" if start > 0 else "") + marked + ("…" if end < len(flat) else "")


def match_field(row, query: str, *, tags=()) -> tuple[str, str] | None:
    """返回 ``(字段标签, 字段原文)``；什么都没命中返回 ``None``。

    标签在 ``tags`` 里单独传（数据库行上没有这一列），它的字段标签是「标签」。
    """
    needle = str(query or "").strip()
    if not needle:
        return None
    low = needle.lower()
    for key, label in FIELD_ORDER:
        try:
            value = row[key]
        except (KeyError, IndexError):
            continue
        text = _examples_flat(value) if key == "examples" else _flat(value)
        if text and low in text.lower():
            return label, text
    for tag in tags or ():
        if low in str(tag).lower():
            return "标签", f"#{tag}"
    return None


def describe(row, query: str, *, tags=()) -> str | None:
    """给卡片用的一行说明：``上下文：【…】…``；没命中返回 ``None``。"""
    found = match_field(row, query, tags=tags)
    if found is None:
        return None
    label, text = found
    return f"{label}：{snippet(text, query)}"
