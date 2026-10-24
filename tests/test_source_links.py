"""The same full document is available through browser and discovery links."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import pymupdf
from app.native.service import NativeService


class SourceLinkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = NativeService(Path(self.temp.name))
        with pymupdf.open() as document:
            document.set_metadata({'title': 'Public research document'})
            document.new_page().insert_text((72, 72), 'Opening evidence on page one.')
            document.new_page().insert_text((72, 72), 'Final evidence on page two.')
            self.pdf = document.tobytes()

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def assert_document(self, article):
        self.assertIn('Opening evidence on page one.', article['text'])
        self.assertIn('Final evidence on page two.', article['text'])
        self.assertEqual('readable', article['source_status'])
        self.assertEqual(2, article['source_stats']['pages'])
        self.assertEqual(('application/pdf', self.pdf),
                         self.service.store.asset(article['source_document']['asset']))

    def test_browser_pdf_link_saves_every_page_and_original_after_redirect(self):
        url = 'https://example.org/research.pdf'
        final = 'https://cdn.example.org/document.pdf'
        with patch('app.discovery.fetch', return_value=(self.pdf, final, 'utf-8')) as loader:
            article = self.service.extract(url)
        self.assert_document(article)
        self.assertEqual(url, article['source_url'])
        self.assertEqual(final, article['source_document']['url'])
        self.assertEqual(final, article['resolved_source_url'])
        self.assertEqual('Public research document', article['title'])
        self.assertEqual(1, loader.call_count)

    def test_browser_arxiv_link_downloads_pdf_without_importing_abstract(self):
        url = 'https://arxiv.org/abs/2610.12345v2'
        with patch('app.discovery.fetch', return_value=(self.pdf, 'https://arxiv.org/pdf/2610.12345v2', 'utf-8')) as loader:
            article = self.service.extract(url)
        self.assert_document(article)
        loader.assert_called_once_with('https://arxiv.org/pdf/2610.12345v2', 28_000_000)

    def test_browser_publisher_link_follows_declared_document_without_refetching_page(self):
        url = 'https://publisher.org/short'
        html = b'<title>Published paper</title><meta name="citation_pdf_url" content="/full.pdf"><main>Abstract only.</main>'
        loader = Mock(side_effect=[(html, 'https://publisher.org/current', 'utf-8'),
                                   (self.pdf, 'https://publisher.org/full.pdf', 'utf-8')])
        with patch('app.discovery.fetch', loader):
            article = self.service.extract(url)
        self.assert_document(article)
        self.assertEqual('Published paper', article['title'])
        self.assertEqual(2, loader.call_count)

    def test_pdf_collection_does_not_import_until_requested_and_keeps_paper_kind(self):
        for suffix in ('research.pdf', 'download?id=123'):
            with self.subTest(suffix=suffix):
                url = 'https://example.org/' + suffix
                loader = Mock(return_value=(self.pdf, url, 'utf-8'))
                before = len(self.service.store.summaries())
                item = self.service.discovery.add_link(url, loader)
                self.assertEqual(before, len(self.service.store.summaries()))
                self.assertEqual('paper', item['content_kind'])
                self.assertEqual('Public research document', item['title'])
                self.assertEqual('', item['summary'])
                self.assertNotIn('cover_file', item)
                self.assert_document(self.service.discovery.import_item(item['id'], loader))

    def test_collected_publisher_article_uses_full_pdf_when_supplied_by_page(self):
        url = 'https://publisher.org/short'
        html = b'<title>Published paper</title><meta name="citation_pdf_url" content="/full.pdf"><main>Abstract only.</main>'
        def loader(address, *args):
            return (self.pdf if address.endswith('.pdf') else html), address, 'utf-8'
        item = self.service.discovery.add_link(url, loader)
        self.assertEqual('paper', item['content_kind'])
        self.assert_document(self.service.discovery.import_item(item['id'], loader))

    def test_failed_publisher_document_preserves_existing_article_and_its_translation(self):
        old = self.service.import_text('Existing source', 'Original saved article.')
        key = 'native-translation:article:' + old['id']
        self.service.store.put('settings', key, {'result': '原来的译文', 'target': 'zh'})
        html = b'<title>Restricted paper</title><meta name="citation_pdf_url" content="/full.pdf"><main>Abstract only.</main>'
        loader = Mock(side_effect=[(html, 'https://publisher.org/current', 'utf-8'), OSError('offline')])
        with patch('app.discovery.fetch', loader), self.assertRaises(ValueError):
            self.service.extract('https://publisher.org/short', old['id'])
        restored = self.service.store.article(old['id'])
        self.assertEqual(old['text'], restored['text'])
        self.assertNotIn('superseded_by', restored)
        self.assertEqual('原来的译文', self.service.store.get('settings', key)['result'])
        self.assertEqual(1, len(self.service.store.summaries()))

    def test_same_pdf_collected_again_keeps_the_articles_own_translation(self):
        url = 'https://example.org/research.pdf'
        loader = Mock(return_value=(self.pdf, url, 'utf-8'))
        with patch('app.discovery.fetch', loader):
            first = self.service.extract(url)
        key = 'native-translation:article:' + first['id']
        translation = {'text': first['text'], 'result': '本文译文', 'target': 'zh', 'complete': True}
        self.service.store.put('settings', key, translation)
        item = self.service.discovery.add_link(url, loader)
        second = self.service.discovery.import_item(item['id'], loader)
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(translation, self.service.store.get('settings', key))
        self.assertEqual(1, len(self.service.store.summaries()))

    def test_broken_or_encrypted_pdf_is_not_collected_as_garbled_html(self):
        with pymupdf.open() as document:
            document.new_page().insert_text((72, 72), 'Protected document')
            encrypted = document.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw='owner', user_pw='reader')
        for raw in (b'%PDF-1.7\nBroken file', encrypted):
            with self.subTest(encrypted=raw == encrypted):
                loader = Mock(return_value=(raw, 'https://example.org/file.pdf', 'utf-8'))
                with self.assertRaises(ValueError):
                    self.service.discovery.add_link('https://example.org/file.pdf', loader)
                self.assertEqual([], self.service.discovery.state()['items'])
                self.assertEqual([], self.service.store.summaries())


if __name__ == '__main__':
    unittest.main()
