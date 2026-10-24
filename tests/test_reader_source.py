import io
import tempfile
import unittest
from pathlib import Path
from PIL import Image
from app.reader_source import readable_blocks, safe_markup
from app.workspace_store import WorkspaceStore
from app.article_source import extract_article_text


class ReaderSourceTests(unittest.TestCase):
    def test_math_keeps_presentation_without_code_or_active_content(self):
        result = safe_markup('<p onclick="bad()">A<math><semantics><mfrac><mi>x</mi><mn>2</mn></mfrac><annotation>\\frac{x}{2}</annotation></semantics></math><script>bad()</script><img src=x onerror=bad()><a href="javascript:bad()">B</a></p>')
        self.assertIn('<mfrac>', result)
        for forbidden in ('annotation', '\\frac', 'onclick', 'script', '<img', 'href'):
            self.assertNotIn(forbidden, result)

    def test_whole_article_order_and_persisted_image(self):
        raw = io.BytesIO()
        Image.new('RGB', (20, 10), 'red').save(raw, 'PNG')
        html = '<nav>Navigation</nav><article><h1>Title</h1><p>First paragraph.</p><figure><img src="figure.png"><figcaption>Figure one</figcaption></figure><h2>Appendix</h2><p>Last paragraph.</p></article>'
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkspaceStore(Path(tmp))
            title, blocks, stats = readable_blocks(html, 'https://example.org/paper', store, lambda _: raw.getvalue())
            self.assertEqual(title, 'Title')
            self.assertEqual([b.get('text', b.get('caption')) for b in blocks], ['Title', 'First paragraph.', 'Figure one', 'Appendix', 'Last paragraph.'])
            self.assertEqual(stats['images'], 1)
            self.assertTrue(store.asset(blocks[2]['asset'])[1])

    def test_no_semantic_article_does_not_claim_full_source(self):
        with self.assertRaises(ValueError):
            readable_blocks('<div>Only a snippet</div>', 'https://example.org', None)

    def test_inline_lazy_and_bare_cdn_images_preserve_order_and_formatting(self):
        raw=io.BytesIO()
        Image.new('RGB',(30,20),'blue').save(raw,'PNG')
        html='''<article><h1>Story</h1><p>Before <strong>bold<img data-src="https://cdn.example.org/chart.png" src="placeholder.gif" alt="Chart">after</strong> tail.</p><img src="/last.png" alt="Last figure"><p>End.</p></article>'''
        requested=[]
        def load(address):
            requested.append(address)
            return raw.getvalue()
        with tempfile.TemporaryDirectory() as folder:
            title,blocks,stats=readable_blocks(html,'https://example.org/story',WorkspaceStore(Path(folder)),load)
            self.assertEqual([b['type'] for b in blocks],['text','text','image','text','image','text'])
            self.assertEqual([b.get('text',b.get('caption')) for b in blocks],
                             ['Story','Before bold','Chart','after tail.','Last figure','End.'])
            self.assertIn('<strong>bold</strong>',blocks[1]['html'])
            self.assertIn('<strong>after</strong>',blocks[3]['html'])
            self.assertEqual(requested,['https://cdn.example.org/chart.png','https://example.org/last.png'])
            self.assertEqual(stats['images'],2)
            self.assertEqual(stats['missing_images'],0)

    def test_wechat_text_without_paragraph_wrappers_is_not_lost(self):
        html='<h1 id="activity-name">文章标题</h1><div id="js_content"><section><!-- hidden technical comment --><span>原文开头</span>中间文字<strong>原文结尾</strong></section></div>'
        title,blocks,_=readable_blocks(html,'https://mp.weixin.qq.com/s/public',None)
        self.assertEqual(title,'文章标题')
        self.assertEqual(''.join(b['text'] for b in blocks),'原文开头中间文字原文结尾')
        self.assertIn('<strong>原文结尾</strong>',blocks[-1]['html'])

    def test_failed_inline_image_remains_visible_between_text_fragments(self):
        _,blocks,stats=readable_blocks('<article><p>前文<img src="https://cdn.example.org/chart.png" alt="示意图">后文</p></article>',
                                      'https://example.org/story',None)
        self.assertEqual([b['text'] for b in blocks],['前文','[图片暂未保存] 示意图','后文'])
        self.assertEqual(stats['missing_images'],1)

    def test_publisher_bodies_include_tail_and_references_without_recommendations(self):
        cases=[('https://www.ithome.com/1/001/001.htm',
                '<h1>新闻标题</h1><div id="paragraph"><p>正文开头。</p><p>正文结尾。</p></div><p>相关推荐</p>',
                ['新闻标题','正文开头。','正文结尾。']),
               ('https://www.sciencedaily.com/releases/story.htm',
                '<main><h1>Research news</h1><div id="story_text"><p>Full body.</p></div><div id="story_source"><p>Original source.</p></div><div id="journal_references"><p>Journal reference.</p></div><p>Recommendations</p></main>',
                ['Research news','Full body.','Original source.','Journal reference.'])]
        for url,html,expected in cases:
            with self.subTest(url=url):
                _,blocks,_=readable_blocks(html,url,None)
                self.assertEqual([b['text'] for b in blocks],expected)

    def test_text_extractor_includes_start_and_tail_without_annotations(self):
        html = '<article><p>' + 'Beginning text. ' * 50 + '</p><math><mi>x</mi><annotation>\\mathcal{X}</annotation></math><p>' + 'Ending text. ' * 50 + '</p></article>'
        text, method, _ = extract_article_text(html)
        self.assertEqual(method, 'semantic-article')
        self.assertLess(text.index('Beginning'), text.index('Ending'))
        self.assertNotIn('\\mathcal', text)

    def test_failed_image_is_explicit_and_original_code_is_preserved(self):
        _, blocks, stats = readable_blocks('<article><h1>Title</h1><figure><img src="a.png"><figcaption>Chart</figcaption></figure><pre>secret source code</pre></article>', 'https://example.org', None)
        self.assertEqual(stats['missing_images'], 1)
        self.assertTrue(blocks[1]['image_missing'])
        self.assertIn('secret source code', blocks[2]['text'])
        self.assertTrue(blocks[2]['source_code'])

    def test_explicit_paywall_does_not_claim_fulltext(self):
        with self.assertRaises(ValueError):
            readable_blocks('<article><p>Opening paragraph.</p><div class="paywall">Subscribe</div></article>', 'https://example.org', None)
