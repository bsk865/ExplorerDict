import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import pymupdf
from bs4 import BeautifulSoup

from app.paper_source import read_paper, pdf_links
from app.reader_source import capture_source
from app.workspace_store import WorkspaceStore


class PublisherPaperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = WorkspaceStore(Path(self.temp.name))
        self.item = dict(url='https://example.org/short', title='Original paper', summary='Abstract')
        with pymupdf.open() as document:
            document.new_page().insert_text((72, 72), 'Introduction: public full text.')
            document.new_page().insert_text((72, 72), 'Conclusions and references on final page.')
            self.pdf = document.tobytes()

    def tearDown(self):
        self.temp.cleanup()

    def test_publisher_landing_page_follows_advertised_pdf_after_redirect(self):
        landing = b'<title>Paper</title><meta name="citation_pdf_url" content="../files/paper?download=1&amp;type=pdf"><main>Abstract</main>'
        loader = Mock(side_effect=[(landing, 'https://publisher.org/articles/paper', 'utf-8'),
                                   (self.pdf, 'https://cdn.publisher.org/paper.pdf', 'utf-8')])
        title, blocks, stats, document = read_paper(self.item, self.store, loader)
        self.assertEqual('Original paper', title)
        self.assertEqual('https://publisher.org/files/paper?download=1&type=pdf', loader.call_args.args[0])
        self.assertEqual(28_000_000, loader.call_args.args[1])
        self.assertEqual(2, stats['pages'])
        self.assertIn('references on final page', '\n'.join(b.get('text', '') for b in blocks))
        self.assertEqual('https://cdn.publisher.org/paper.pdf', document['url'])
        self.assertEqual(('application/pdf', self.pdf), self.store.asset(document['asset']))

    def test_broken_pdf_link_falls_back_to_complete_html(self):
        html = ('<meta name="citation_pdf_url" content="/full.pdf"><article><h1>Full HTML</h1><section><p>'
                + 'Complete original body. '*40 + '</p></section></article>').encode()
        loader = Mock(side_effect=[(html, self.item['url'], 'utf-8'), OSError('offline')])
        title, blocks, _, document = read_paper(self.item, self.store, loader)
        self.assertEqual('Full HTML', title)
        self.assertIn('Complete original body.', blocks[-1]['text'])
        self.assertIsNone(document)

    def test_broken_or_non_pdf_response_never_saves_abstract_as_full_text(self):
        for result in [OSError('offline'), (b'<main>Login</main>', 'https://example.org/login', 'utf-8')]:
            with self.subTest(result=type(result).__name__):
                html = b'<meta name="citation_pdf_url" content="/full.pdf"><main>Abstract only</main>'
                loader = Mock(side_effect=[(html, self.item['url'], 'utf-8'), result])
                with self.assertRaises(ValueError):
                    read_paper(self.item, self.store, loader)
                self.assertEqual([], self.store.summaries())

    def test_explicit_pdf_links_are_bounded_deduplicated_and_not_guessed(self):
        soup = BeautifulSoup('''<meta name="citation_pdf_url" content="https://[broken">
            <meta name="citation_pdf_url" content="file:///private.pdf">
            <meta name="citation_pdf_url" content="https://user:password@example.org/private.pdf">
            <meta name="CITATION_PDF_URL" content="/paper.pdf#page=1">
            <link type="application/pdf" href="/paper.pdf">
            <link type="application/pdf" href="https://cdn.example.org/other.pdf">
            <a href="/unadvertised.pdf">Other document</a>''', 'html.parser')
        self.assertEqual(['https://example.org/paper.pdf', 'https://cdn.example.org/other.pdf'], pdf_links(soup, self.item['url']))

    def test_self_link_does_not_repeat_landing_page_request(self):
        loader = Mock(return_value=(b'<meta name="citation_pdf_url" content="/short"><main>Abstract</main>', self.item['url'], 'utf-8'))
        with self.assertRaises(ValueError):
            read_paper(self.item, self.store, loader)
        loader.assert_called_once()

    def test_browser_link_capture_uses_resolved_page_for_relative_images(self):
        import io
        from PIL import Image
        image = io.BytesIO()
        Image.new('RGB', (20, 10), 'green').save(image, 'PNG')
        html = b'<article><h1>Moved article</h1><p>Full body.</p><img src="figure.png"></article>'
        loader = Mock(side_effect=[(html, 'https://publisher.org/story/current', 'utf-8'),
                                  (image.getvalue(), 'https://publisher.org/story/figure.png', 'utf-8')])
        with patch('app.discovery.fetch', loader):
            article = capture_source(self.store, self.item['url'])
        self.assertEqual('https://publisher.org/story/figure.png', loader.call_args.args[0])
        self.assertEqual(self.item['url'], article['source_url'])
        self.assertEqual('https://publisher.org/story/current', article['resolved_source_url'])
        self.assertEqual(1, article['source_stats']['images'])
