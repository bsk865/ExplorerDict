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
import pathlib
import unittest
import zlib
from email.message import Message
from urllib.error import HTTPError, URLError

from app.article_source import (
    CLIP_HEAD_RATIO, DEFAULT_TIMEOUT, MAX_BYTES, MAX_LOCAL_BYTES, MIN_ARTICLE_CHARS,
    STATUS_SKIP, Article, clip_article, clip_text, decode_html, detect_blocked,
    extract_article_text, fetch_article, is_fetchable_url, is_http_url, is_local_source,
    local_name_of, local_path_of, looks_like_file_name, read_local_article,
    resolve_local_path, source_hint_of_doc_key, source_value_of, strip_title_marks,
    title_candidates,
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
        self.assertEqual(method, "semantic-article")
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

    # 下面五条是**补测试缺口**用的：原装置里的导航 / 页脚分别是裸 ``<nav>``
    # / ``<header>`` / ``<footer>``，早就被 _DROP_TAGS 删干净了，
    # 于是「class / id 里带提示词的**容器**」这条路径其实从没被跑到过。
    # 2026-10-08 真机核查时发现：这条路径上只要导航排在正文之前，正文会被整段
    # 吃掉，而 34 条测试全绿 —— 就是缺了这几个用例。
    def test_hinted_div_before_the_article_does_not_eat_it(self):
        """导航容器（带提示词的 div）排在正文**之前**时，正文一个字都不许丢。"""
        before = "<div class=\"nav\"><a href=\"/\">首页</a><a href=\"/t\">技术</a></div>"
        body = f"{before}<div class=\"content\"><p>{para('正文')}</p></div>"
        text, method, why = extract_article_text(page(body))
        self.assertEqual(why, "")
        self.assertEqual(method, "density")
        self.assertIn("正文：", text, "导航排在正文之前是真实网页的常态，正文必须留下")
        self.assertNotIn("首页", text)
        self.assertNotIn("技术", text)

    def test_hinted_container_by_id_is_dropped_too(self):
        """提示词也可能藏在 ``id`` 里（``id="nav"``），同样不许吞正文。"""
        before = "<div id=\"nav\"><a href=\"/\">首页</a></div><aside id=\"sidebar\">侧边栏</aside>"
        body = f"{before}<div id=\"main\"><p>{para('正文')}</p></div>"
        text, _method, why = extract_article_text(page(body))
        self.assertEqual(why, "")
        self.assertIn("正文：", text)

    def test_unclosed_hinted_container_does_not_eat_the_rest(self):
        """导航容器忘了写 ``</div>`` 时，只许吃掉它自己，后面正文全留。

        老写法在这种情况下会「删到输入末尾」—— 一个漏掉的闭标签就能让整篇文章
        变成「未取到原文」。"""
        before = "<div class=\"nav\">首页 关于我们"
        body = f"{before}<div class=\"content\"><p>{para('正文')}</p></div>"
        text, _method, why = extract_article_text(page(body))
        self.assertEqual(why, "")
        self.assertIn("正文：", text, "没闭合的导航容器只该吃掉自己，不许连累正文")

    def test_hinted_containers_after_the_article_do_not_eat_it(self):
        """排在正文**之后**的评论 / 广告容器同样不许影响正文。"""
        body = (f"<div class=\"content\"><p>{para('正文')}</p></div>"
                "<div class=\"comment-list\"><p>评论区第一楼</p></div>"
                "<div class=\"ad-banner\">广告位</div>")
        text, _method, why = extract_article_text(page(body))
        self.assertEqual(why, "")
        self.assertIn("正文：", text)
        self.assertNotIn("评论区第一楼", text)
        self.assertNotIn("广告位", text)

    def test_paragraph_with_hint_word_is_never_dropped(self):
        """``<p>`` 不在容器白名单里：段落就算叫 ``class="nav-note"`` 也不许删。"""
        body = f"<div class=\"content\"><p class=\"nav-note\">{para('正文')}</p></div>"
        text, _method, why = extract_article_text(page(body))
        self.assertEqual(why, "")
        self.assertIn("正文：", text)

    def test_only_the_first_matching_block_is_dropped_per_call(self):
        """``_drop_element`` 一次只删**一个**元素：顺手删兄弟块就是吞正文的元凶。"""
        from app.article_source import _drop_element
        html = ("<div class=\"nav\">首页</div>"
                "<div class=\"content\"><p>正文在这里</p></div>")
        once = _drop_element(html, "div")
        self.assertNotIn("首页", once)
        self.assertIn("正文在这里", once, "删导航不许把紧随其后的容器一起删掉")
        twice = _drop_element(once, "div")
        self.assertNotIn("正文在这里", twice, "第二轮的命中点才是正文容器")

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
            self.assertTrue(is_http_url(good))
        for bad in ("", "   ", "javascript:void(0)", "about:blank", "https://",
                    "ftp://a.example.com/x", "mailto:a@b.c"):
            self.assertFalse(is_http_url(bad), f"{bad!r} 不该被判为可抓的网页")

    def test_local_sources_are_recognized_but_never_count_as_http(self):
        """批次 M21-A：本地文档要能取原文（读盘），但**不是**网页。"""
        for path in ("D:\\x\\需求.txt", "D:/x/需求.txt", "file:///D:/x/需求.txt",
                     "\\\\server\\share\\x.md"):
            self.assertTrue(is_local_source(path), f"{path!r} 是本地文件")
            self.assertTrue(is_fetchable_url(path), "本地文件也算「能取到原文」")
            self.assertFalse(is_http_url(path), "本地文件不许走网络那条路")
        for junk in ("", "   ", "计算器", "https://a.example.com/p",
                     "mailto:a@b.c", "javascript:void(0)"):
            self.assertFalse(is_local_source(junk), f"{junk!r} 不是本地文件")

    def test_non_fetchable_url_skips_without_network(self):
        opener = FakeOpener(error=AssertionError("不许联网"))
        art = fetch_article("javascript:void(0)", opener=opener)
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


# ----------------------------------------------------- 本地文档（批次 M21-A）
class TestLocalArticle(unittest.TestCase):
    """用户口径 2026-10-09：「我用的是本地的 txt 写了一段测试文本，你告诉我没有
    抓取到原文？而且抓取原文为什么需要联网？」

    守两条：**本地文本文件读得进来**（且一个数据包都不发），**读不进来的要说清
    为什么**（不是 PDF 这种格式 / 找不到 / 是文件夹 / 是二进制 / 太短）。
    """

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name: str, text: str, encoding: str = "utf-8") -> pathlib.Path:
        target = self.root / name
        target.write_bytes(text.encode(encoding))
        return target

    def test_reads_a_plain_text_file_without_touching_the_network(self):
        body = "第一段讲问题。\n\n" + para("需求")
        target = self.write("需求.txt", body)
        art = read_local_article(str(target))
        self.assertTrue(art.ok, art.note)
        self.assertEqual(art.status, "ok")
        self.assertEqual(art.method, "local")
        self.assertEqual(art.title, "需求", "标题就用文件名，不编造")
        self.assertIn("第一段讲问题。", art.text)
        self.assertEqual(art.charset, "utf-8-sig")
        self.assertEqual(art.url, target.resolve().as_uri(),
                         "留档里要写成 file:///… 的规范形式")

    def test_file_url_and_bare_path_land_on_the_same_file(self):
        body = para("路径") + "\n"
        target = self.write("路径.txt", body)
        for value in (str(target), target.as_uri(),
                      str(target).replace("\\", "/")):
            art = read_local_article(value)
            self.assertTrue(art.ok, f"{value} 应当读得到：{art.note}")
            self.assertEqual(art.chars, read_local_article(str(target)).chars)

    def test_gbk_file_is_decoded_correctly(self):
        body = para("国标") + "\n"
        target = self.write("国标.txt", body, encoding="gb18030")
        art = read_local_article(str(target))
        self.assertTrue(art.ok, art.note)
        self.assertEqual(art.charset, "gb18030")
        self.assertIn("国标：", art.text)
        self.assertNotIn("\ufffd", art.text, "不许留乱码替换符")

    def test_html_file_reuses_the_article_extractor(self):
        target = self.write("文章.html", page(f"<article><p>{para('网页')}</p></article>"))
        art = read_local_article(str(target))
        self.assertTrue(art.ok, art.note)
        self.assertTrue(art.method.startswith("local:"), art.method)
        self.assertIn("网页：", art.text)
        self.assertNotIn("评论区第一楼", art.text, "本地 HTML 也要剥导航评论")

    def test_unsupported_format_says_what_it_is(self):
        target = self.write("论文.pdf", "not really a pdf" + para("假"))
        art = read_local_article(str(target))
        self.assertFalse(art.ok)
        self.assertIn("pdf", art.note.lower())
        self.assertIn("纯文本", art.note)

    def test_missing_and_directory_and_empty_are_three_different_sentences(self):
        missing = read_local_article(str(self.root / "没有这个文件.txt"))
        self.assertFalse(missing.ok)
        self.assertIn("找不到", missing.note)
        folder = read_local_article(str(self.root))
        self.assertFalse(folder.ok)
        self.assertIn("文件夹", folder.note)
        empty = self.write("空的.txt", "")
        blank = read_local_article(str(empty))
        self.assertFalse(blank.ok)
        self.assertNotIn(blank.note, (missing.note, folder.note))

    def test_binary_file_is_refused_not_garbled(self):
        target = self.root / "程序.txt"
        target.write_bytes(b"\x00\x01\x02PK\x03\x04" + b"\x00" * 400)
        art = read_local_article(str(target))
        self.assertFalse(art.ok)
        self.assertIn("二进制", art.note)

    def test_too_short_file_says_how_many_chars_it_saw(self):
        target = self.write("短.txt", "太短了")
        art = read_local_article(str(target))
        self.assertFalse(art.ok)
        self.assertIn("短", art.note)

    def test_oversized_file_is_truncated_and_says_so(self):
        target = self.write("大.txt", "很长的一段话。" * 200)
        art = read_local_article(str(target), max_bytes=120)
        self.assertLessEqual(art.raw_bytes, 120, "最多只读这么多字节")
        self.assertIn("文件较大", art.note)

    def test_fetch_article_routes_local_paths_to_the_reader(self):
        """``fetch_article`` 对本地路径同样不许联网 —— 传进去的 opener 一次都不该被用。"""
        opener = FakeOpener(error=AssertionError("本地文件绝不允许联网"))
        target = self.write("走这条.txt", para("直读"))
        art = fetch_article(str(target), opener=opener)
        self.assertTrue(art.ok, art.note)
        self.assertEqual(opener.requests, [])
        self.assertEqual(art.method, "local")

    def test_local_reader_accepts_a_max_bytes_budget(self):
        target = self.write("预算.txt", para("预算"))
        art = read_local_article(str(target), max_bytes=MAX_LOCAL_BYTES)
        self.assertTrue(art.ok, art.note)


# ------------------------------------- 标题上的「已修改」标记（批次 M25）
class TestTitleStateMarks(unittest.TestCase):
    """真病根（2026-10-09，用户实测）：记事本改过没保存时窗口标题是
    ``*新建 文本文档.txt - 记事本`` —— 标题里那个 ``*`` 是程序标的「已修改」，
    磁盘上的文件叫 ``新建 文本文档.txt``。

    ``looks_like_file_name`` 认得出带星号的名字（后缀对），于是**卡在找文件这一步**：
    ``resolve_local_path`` 拿着 ``*新建 文本文档.txt`` 在桌面 / 文档 / 下载里找不到
    任何东西，``source_value_of`` 返回空串 ⇒ 一个字都不抓 ⇒ ``articles`` 表空 ⇒
    导图永远退回「按词条硬凑」。这一组用例把这三个环节逐一钉死。
    """

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.name = "用例笔记-20261009.txt"
        self.marked = "*" + self.name
        (self.root / self.name).write_text(para("标记"), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_strip_title_marks_only_touches_the_ends(self):
        self.assertEqual(strip_title_marks(self.marked), self.name)
        self.assertEqual(strip_title_marks(f"*{self.name} - 记事本"),
                         f"{self.name} - 记事本")
        self.assertEqual(strip_title_marks("• 报告.md"), "报告.md")
        self.assertEqual(strip_title_marks(self.name), self.name,
                         "没有标记时一个字都不改")
        self.assertEqual(strip_title_marks(""), "")
        self.assertEqual(strip_title_marks("*中间*有星号.txt"), "中间*有星号.txt",
                         "只剥首尾，名字中间的星号不许动")

    def test_title_candidates_offer_the_unmarked_name(self):
        got = title_candidates(f"*{self.name} - 记事本")
        self.assertIn(self.name, got, "★ 必须给出去掉标记的那一份")
        self.assertIn(f"*{self.name}", got, "原样那一份也留着（有的程序名字真带星号）")
        self.assertEqual(title_candidates(""), ())
        self.assertEqual(title_candidates("*"), ("*",),
                         "剥完是空串就不往里塞空的候选值")

    def test_the_suffix_check_was_never_the_blocker(self):
        self.assertTrue(looks_like_file_name(self.marked),
                        "认得出是文件名 —— 所以病根不在这儿")
        self.assertEqual(local_name_of(self.marked), self.marked)

    def test_the_star_is_what_makes_the_file_unfindable(self):
        self.assertEqual(resolve_local_path(self.marked, extra_dirs=(self.root,)), "",
                         "带星号的名字在磁盘上找不到（复现旧现象）")
        self.assertEqual(resolve_local_path(self.name, extra_dirs=(self.root,)),
                         str(self.root / self.name))

    def test_source_value_of_recovers_the_unmarked_name(self):
        found = source_value_of("", f"*{self.name} - 记事本",
                                extra_dirs=(str(self.root),))
        self.assertEqual(found, self.name,
                         "★ 标题带 * 也要认出文件：返回**没有星号**的那个名字")

    def test_source_value_of_still_prefers_a_real_url(self):
        self.assertEqual(source_value_of("https://a.example.com/p", "*x.txt"),
                         "https://a.example.com/p")

    def test_source_value_of_gives_up_loudly_enough(self):
        self.assertEqual(source_value_of("", "某个聊天窗口"), "",
                         "认不出来就是空串，绝不编一个来源")


class TestClipText(unittest.TestCase):
    """正文截断（批次 M26）：留档与「发给模型」共用同一套保头保尾写法。"""

    @staticmethod
    def body(paragraphs: int = 20) -> str:
        return "\n\n".join(
            f"第{i}段：" + "内容" * 100 + f"（本段结论{i}）" for i in range(paragraphs))

    def test_short_text_is_returned_untouched(self):
        text = self.body(2)
        self.assertEqual(clip_text(text, 5000), text)

    def test_head_and_tail_both_survive(self):
        out = clip_text(self.body(20), 900)
        self.assertIn("第0段", out, "头要留")
        self.assertIn("（本段结论19）", out, "结论在最后一段，必须留")
        self.assertNotIn("第10段", out, "中间那些段该被省掉")
        self.assertIn("（正文过长，中间省略", out)
        self.assertEqual(out.count("正文过长"), 1, "只写一处说明")

    def test_it_says_how_much_it_dropped(self):
        text = self.body(20)
        out = clip_text(text, 900)
        omitted = int(out.split("中间省略 ")[1].split(" 字")[0])
        self.assertGreater(omitted, 0)
        self.assertLessEqual(omitted, len(text), "省掉的字数不可能比原文还多")

    def test_two_ends_say_it_the_same_way(self):
        """留档（clip_text）与发模型（build_article_material）必须是同一句话。"""
        from app.api_client import build_article_material

        text = self.body(20)
        self.assertEqual(clip_text(text, 900, head_ratio=CLIP_HEAD_RATIO),
                         build_article_material(text, limit=900))


class TestClipArticle(unittest.TestCase):
    """``clip_article`` 就地截断一行留档，并把这件事写进 note。"""

    def test_it_clips_in_place_and_leaves_a_trace(self):
        article = Article(ok=True, url="u", title="长文", text="。" * 5000, method="http")
        self.assertTrue(clip_article(article, 1000))
        self.assertLessEqual(article.chars, 1100)
        self.assertIn("5000", article.note, "原文多少字要写明")
        self.assertTrue(article.ok, "截断后它依旧是这一页的原文")

    def test_it_keeps_an_existing_note(self):
        article = Article(ok=True, url="u", title="长文", text="。" * 5000,
                          note="取回方式：正文密度", method="http")
        clip_article(article, 1000)
        self.assertIn("取回方式：正文密度", article.note)
        self.assertIn("留档", article.note)

    def test_short_text_is_left_alone(self):
        article = Article(ok=True, url="u", title="短文", text="短句。" * 10)
        self.assertFalse(clip_article(article, 1000))
        self.assertEqual(article.note, "")


class TestDocKeySourceHint(unittest.TestCase):
    """``doc_key`` 里存着**当时的窗口标题**（批次 M26）—— 那是找回文件的最后线索。"""

    def test_it_reads_the_window_title_out_of_an_app_key(self):
        key = "app:notepad.exe|title:*新建 文本文档.txt - notepad"
        self.assertEqual(source_hint_of_doc_key(key), "*新建 文本文档.txt - notepad")

    def test_it_reads_the_address_out_of_a_url_key(self):
        self.assertEqual(source_hint_of_doc_key("url:https://a.example.com/p"),
                         "https://a.example.com/p")

    def test_it_gives_up_on_keys_without_a_hint(self):
        self.assertEqual(source_hint_of_doc_key(""), "")
        self.assertEqual(source_hint_of_doc_key("app:notepad.exe"), "")

    def test_the_hint_recovers_the_file_the_clean_title_lost(self):
        """真病根：``source_title`` 是清洗过的（``.txt`` 被去掉），doc_key 没有。"""
        import tempfile

        with tempfile.TemporaryDirectory() as raw_root:
            root = pathlib.Path(raw_root)
            name = "用例笔记-20261009.txt"
            (root / name).write_text("正文", encoding="utf-8")
            clean_title = "*用例笔记-20261009"          # clean_document_title 的产物
            hint = source_hint_of_doc_key(
                f"app:notepad.exe|title:*{name} - notepad")
            self.assertEqual(source_value_of("", clean_title,
                                             extra_dirs=(str(root),)), "",
                             "清洗过的标题认不出文件（复现旧现象）")
            self.assertEqual(source_value_of("", hint, extra_dirs=(str(root),)), name,
                             "★ doc_key 里的窗口标题认得出来")


if __name__ == "__main__":
    unittest.main()
