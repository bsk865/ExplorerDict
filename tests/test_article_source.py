"""取原文的纯函数用例（**零联网**：所有网络分支都用假 opener / 假响应）。

为什么单独一个文件：``app/article_source.py`` 的下半截（``fetch_article``）
是唯一会碰网络的地方，所以它的每一个失败分支都必须在这里被钉死 ——
用户看到的必须是「一句人话原因」，而不是异常栈；而「抓不到」这件事
**绝不许**被写成「抓到了」（那会让导图拿导航文字冒充正文）。

覆盖：
* 正文识别（文本块密度 / 退最长块 / 嵌套 div / 无闭合段落）；
* 剥导航页脚评论广告（含 ``class="content"`` 不许误伤的白名单）；
* 登录墙识别、编码回退、gzip/deflate 解压；
* ``fetch_article`` 的七种失败分支一律返回 ``Article(ok=False, note=人话)``；
* ``Article.locate`` 在原文里定位划过的词。
"""
from __future__ import annotations

import gzip
import unittest
import zlib
from email.message import Message
from urllib.error import HTTPError, URLError

from app.article_source import (
    DEFAULT_TIMEOUT, MAX_BYTES, MIN_ARTICLE_CHARS, STATUS_SKIP, Article,
    decode_html, detect_blocked, extract_article_text, fetch_article,
    is_fetchable_url,
)


def para(marker: str, times: int = 14) -> str:
    """一段够长、够像正文的中文（每段都过 MIN_ARTICLE_CHARS 门槛）。"""
    return f"{marker}：" + "这是一句用来测试正文识别的中文句子，标点符号齐全。" * times


def page(body: str, *, head: str = "", tail: str = "") -> str:
    """套一层完整页面外壳：有导航、有页脚噪音，正文在中间。"""
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>测试文章标题 - 某某网</title>"
        "<style>.nav{color:#fff}</style><script>var x=1;</script>"
        f"{head}</head><body>"
        "<nav class=\"site-nav\"><a href=\"/\">首页</a><a href=\"/a\">分类</a></nav>"
        "<header class=\"page-header\">某某网 · 科技频道</header>"
        f"{body}"
        "<footer class=\"site-footer\">版权所有 © 某某网 京ICP备00000000号</footer>"
        "<div class=\"comment-list\"><p>评论区第一楼</p><p>评论区第二楼</p></div>"
        f"{tail}"
        "</body></html>"
    )


# --------------------------------------------------------------------- 假响应
class FakeHeaders(Message):
    """够用的假头：``get_content_charset`` / ``get_content_type`` / ``get``。"""

    def __init__(self, ctype="text/html", charset="", encoding=""):
        super().__init__()
        value = ctype
        if charset:
            value = f'{ctype}; charset={charset}'
        self["Content-Type"] = value
        if encoding:
            self["Content-Encoding"] = encoding


class FakeResponse:
    def __init__(self, body: bytes, headers: FakeHeaders):
        self._body = body
        self.headers = headers

    def read(self, size: int = -1) -> bytes:
        return self._body if size is None or size < 0 else self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """记下每次请求，按预设返回响应或抛异常。"""

    def __init__(self, result=None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.requests: list = []
        self.timeouts: list = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        return self.result


def html_response(html: str, **kw) -> FakeResponse:
    return FakeResponse(html.encode("utf-8"), FakeHeaders(**kw))


# ------------------------------------------------------------------ 正文识别
class TestExtractArticleText(unittest.TestCase):
    def test_picks_article_and_drops_navigation(self):
        text, method, why = extract_article_text(page(f"<article><p>{para('甲')}</p></article>"))
        self.assertEqual(why, "")
        self.assertIn("甲：", text)
        self.assertEqual(method, "density")
        for noise in ("首页", "分类", "版权", "京ICP备", "评论区第一楼", "var x=1"):
            self.assertNotIn(noise, text, f"正文里不该出现页面噪音：{noise}")

    def test_joins_consecutive_paragraphs(self):
        body = "".join(f"<p>{para(f'第{i}段', 6)}</p>" for i in range(1, 5))
        text, _method, why = extract_article_text(page(f"<article>{body}</article>"))
        self.assertEqual(why, "")
        for i in range(1, 5):
            self.assertIn(f"第{i}段：", text, "同一篇文章的段落必须一起收进来")

    def test_class_content_is_not_dropped(self):
        """``class="content"`` 是正文最常见的外衣，提示词匹配不许误伤它。"""
        body = f"<div class=\"content\"><p>{para('正文')}</p></div>"
        text, _method, why = extract_article_text(page(body))
        self.assertEqual(why, "")
        self.assertIn("正文：", text)

    def test_nested_div_is_not_swallowed(self):
        body = (f"<div class=\"wrap\"><div class=\"inner\"><p>{para('内层')}</p>"
                f"</div><p>{para('外层')}</p></div>")
        text, _method, why = extract_article_text(page(body))
        self.assertEqual(why, "")
        self.assertIn("内层：", text, "嵌套 div 剥皮不许把正文一起删掉")
        self.assertIn("外层：", text)

    def test_block_without_closing_tag_is_kept(self):
        """有些 Markdown 渲染页最后一段没有 ``</p>``，不能丢结论段。"""
        body = f"<article><p>{para('第一段')}</p><p>{para('结论段')}"
        text, _method, _why = extract_article_text(page(body))
        self.assertIn("结论段：", text, "没闭合的末段也要冲出来")

    def test_falls_back_to_longest_block(self):
        """密度法凑不够 200 字时，退回「最长的单块」。"""
        lone = "短页面正文。" * 30
        text, method, why = extract_article_text(f"<html><body><div>{lone}</div></body></html>")
        self.assertEqual(why, "")
        self.assertIn("短页面正文。", text)
        self.assertIn(method, ("density", "longest-block"))

    def test_empty_html_returns_reason(self):
        text, method, why = extract_article_text("   ")
        self.assertEqual((text, method), ("", ""))
        self.assertIn("空", why)

    def test_too_little_text_returns_reason(self):
        text, _method, why = extract_article_text("<html><body><p>太短</p></body></html>")
        self.assertEqual(text, "")
        self.assertNotEqual(why, "", "识别不出必须给原因，不能静默返回空")
        self.assertIn("短", why)

    def test_script_only_page_returns_reason(self):
        text, _method, why = extract_article_text(
            "<html><body><script>document.write('x')</script></body></html>")
        self.assertEqual(text, "")
        self.assertNotEqual(why, "")


# ------------------------------------------------------------------ 登录墙 / 编码
class TestBlockedAndDecode(unittest.TestCase):
    def test_detects_login_wall(self):
        self.assertNotEqual(detect_blocked("登录后阅读全文，请先登录账号"), "")
        self.assertNotEqual(detect_blocked("Sign in to continue reading"), "")
        self.assertNotEqual(detect_blocked("请开启 JavaScript 后继续"), "")

    def test_normal_article_is_not_blocked(self):
        self.assertEqual(detect_blocked(para("正文")), "")

    def test_decode_prefers_http_header_charset(self):
        raw = "中文内容".encode("gb18030")
        text, enc = decode_html(raw, "gb18030")
        self.assertEqual(text, "中文内容")
        self.assertEqual(enc, "gb18030")

    def test_decode_reads_meta_charset(self):
        raw = "<meta charset=\"gbk\"><p>中文</p>".encode("gbk")
        text, enc = decode_html(raw)
        self.assertIn("中文", text)
        self.assertIn(enc, ("gbk", "gb18030"))

    def test_decode_falls_back_to_utf8_then_gb18030(self):
        text, enc = decode_html("纯中文".encode("utf-8"))
        self.assertEqual(text, "纯中文")
        self.assertEqual(enc, "utf-8")

    def test_decode_never_raises_on_garbage(self):
        text, _enc = decode_html(b"\xff\xfe\x00\x01\x02")
        self.assertIsInstance(text, str)


class TestArticleObject(unittest.TestCase):
    def test_chars_and_paragraphs(self):
        art = Article(ok=True, text="第一段\n\n第二段\n\n\n第三段")
        self.assertEqual(art.chars, len(art.text))
        self.assertEqual(art.paragraphs(), ["第一段", "第二段", "第三段"])

    def test_locate_returns_window_around_needle(self):
        art = Article(ok=True, text="前" * 300 + "目标词" + "后" * 300)
        hit = art.locate("目标词", span=40)
        self.assertIn("目标词", hit)
        self.assertLess(len(hit), 60, "只要它周围那一小截，不要把整篇倒出来")

    def test_locate_missing_needle_is_empty(self):
        art = Article(ok=True, text="只有这一段")
        self.assertEqual(art.locate("不存在的词"), "")

    def test_locate_empty_needle_is_empty(self):
        self.assertEqual(Article(ok=True, text="abc").locate("  "), "")


# ------------------------------------------------------------------- fetch_article
class TestFetchArticle(unittest.TestCase):
    def test_url_scheme_gate(self):
        for good in ("http://a.example.com/p", "https://a.example.com/p"):
            self.assertTrue(is_fetchable_url(good))
        for bad in ("", "   ", "file:///C:/a.txt", "javascript:void(0)",
                    "about:blank", "https://", "ftp://a.example.com/x"):
            self.assertFalse(is_fetchable_url(bad), f"{bad!r} 不该被判为可抓")

    def test_non_fetchable_url_skips_without_network(self):
        opener = FakeOpener(error=AssertionError("不许联网"))
        art = fetch_article("file:///C:/a.docx", opener=opener)
        self.assertFalse(art.ok)
        self.assertEqual(art.status, STATUS_SKIP)
        self.assertEqual(opener.requests, [], "非 http(s) 一律不许发请求")
        self.assertNotEqual(art.note, "")

    def test_ok_path_extracts_text_and_title(self):
        opener = FakeOpener(html_response(page(f"<article><p>{para('甲')}</p></article>")))
        art = fetch_article("https://a.example.com/p", opener=opener)
        self.assertTrue(art.ok, art.note)
        self.assertEqual(art.status, "ok")
        self.assertIn("甲：", art.text)
        self.assertGreaterEqual(art.chars, MIN_ARTICLE_CHARS)
        self.assertEqual(art.title, "测试文章标题 - 某某网")
        self.assertEqual(art.charset, "utf-8")
        self.assertEqual(art.raw_bytes, len(opener.result.read()))
        self.assertEqual(opener.timeouts, [DEFAULT_TIMEOUT])
        self.assertIn("AppleWebKit", opener.requests[0].get_header("User-agent") or "",
                      "取原文要以浏览器身份请求，不伪装爬虫")

    def test_http_error_becomes_sentence(self):
        err = HTTPError("https://a.example.com/p", 403, "Forbidden", None, None)
        art = fetch_article("https://a.example.com/p", opener=FakeOpener(error=err))
        self.assertFalse(art.ok)
        self.assertIn("403", art.note)
        self.assertIn("登录", art.note)

    def test_url_error_becomes_sentence(self):
        art = fetch_article("https://a.example.com/p",
                            opener=FakeOpener(error=URLError("名字解析失败")))
        self.assertFalse(art.ok)
        self.assertIn("连不上", art.note)

    def test_timeout_becomes_sentence(self):
        art = fetch_article("https://a.example.com/p", timeout=3.0,
                            opener=FakeOpener(error=TimeoutError()))
        self.assertFalse(art.ok)
        self.assertIn("3", art.note)
        self.assertIn("没响应", art.note)

    def test_unexpected_error_never_raises(self):
        art = fetch_article("https://a.example.com/p",
                            opener=FakeOpener(error=RuntimeError("boom")))
        self.assertFalse(art.ok, "意外异常也必须变成一句话，不许冒到上层")
        self.assertIn("boom", art.note)

    def test_non_html_content_type_skips(self):
        opener = FakeOpener(FakeResponse(b"%PDF-1.7", FakeHeaders(ctype="application/pdf")))
        art = fetch_article("https://a.example.com/p.pdf", opener=opener)
        self.assertFalse(art.ok)
        self.assertEqual(art.status, STATUS_SKIP)
        self.assertIn("application/pdf", art.note)

    def test_login_wall_is_rejected_not_saved_as_text(self):
        html = page("登录后阅读全文，请先登录账号")
        opener = FakeOpener(html_response(html))
        art = fetch_article("https://a.example.com/p", opener=opener)
        self.assertFalse(art.ok, "登录墙不许冒充正文")
        self.assertEqual(art.text, "", "失败的正文一个字都不留")
        self.assertIn("登录", art.note)

    def test_short_page_is_rejected_with_count(self):
        opener = FakeOpener(html_response("<html><body><p>只有一句话。</p></body></html>"))
        art = fetch_article("https://a.example.com/p", opener=opener)
        self.assertFalse(art.ok)
        self.assertIn("不像正文", art.note)

    def test_gzip_body_is_decompressed(self):
        raw = gzip.compress(page(f"<article><p>{para('压缩')}</p></article>").encode("utf-8"))
        opener = FakeOpener(FakeResponse(raw, FakeHeaders(encoding="gzip")))
        art = fetch_article("https://a.example.com/p", opener=opener)
        self.assertTrue(art.ok, art.note)
        self.assertIn("压缩：", art.text)

    def test_deflate_body_is_decompressed(self):
        html = page(f"<article><p>{para('紧缩')}</p></article>").encode("utf-8")
        for raw in (zlib.compress(html), zlib.compress(html)[2:-4]):
            opener = FakeOpener(FakeResponse(raw, FakeHeaders(encoding="deflate")))
            art = fetch_article("https://a.example.com/p", opener=opener)
            self.assertTrue(art.ok, f"deflate 解压失败：{art.note}")

    def test_gbk_page_is_decoded(self):
        html = page(f"<article><p>{para('国标')}</p></article>")
        opener = FakeOpener(FakeResponse(html.encode("gb18030"),
                                         FakeHeaders(charset="gb18030")))
        art = fetch_article("https://a.example.com/p", opener=opener)
        self.assertTrue(art.ok, art.note)
        self.assertIn("国标：", art.text)

    def test_oversized_page_is_truncated_not_crashed(self):
        filler = "填充文字。" * (MAX_BYTES // 5)
        raw = (page(f"<article><p>{para('甲')}</p></article>") + filler).encode("utf-8")
        opener = FakeOpener(FakeResponse(raw, FakeHeaders()))
        art = fetch_article("https://a.example.com/p", opener=opener)
        self.assertLessEqual(art.raw_bytes, MAX_BYTES, "最多只读 1MB，不许把内存吃满")
        self.assertTrue(art.ok, art.note)

    def test_failed_result_carries_url_for_the_log(self):
        art = fetch_article("https://a.example.com/p",
                            opener=FakeOpener(error=URLError("断网")))
        self.assertEqual(art.url, "https://a.example.com/p")
        self.assertEqual(art.text, "")


if __name__ == "__main__":
    unittest.main()
