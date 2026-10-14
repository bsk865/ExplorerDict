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

    def test_text_extractor_includes_start_and_tail_without_annotations(self):
        html = '<article><p>' + 'Beginning text. ' * 50 + '</p><math><mi>x</mi><annotation>\\mathcal{X}</annotation></math><p>' + 'Ending text. ' * 50 + '</p></article>'
        text, method, _ = extract_article_text(html)
        self.assertEqual(method, 'semantic-article')
        self.assertLess(text.index('Beginning'), text.index('Ending'))
        self.assertNotIn('\\mathcal', text)

    def test_failed_image_is_explicit_and_code_not_prose(self):
        _, blocks, stats = readable_blocks('<article><h1>Title</h1><figure><img src="a.png"><figcaption>Chart</figcaption></figure><pre>secret source code</pre></article>', 'https://example.org', None)
        self.assertEqual(stats['missing_images'], 1)
        self.assertTrue(blocks[1]['image_missing'])
        self.assertNotIn('secret source code', str(blocks))
