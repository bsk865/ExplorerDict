import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock
import pymupdf
from app.native.service import NativeService
from app.discovery import entry
from app.translation import chunks, validate, translate
from app.api_client import DeepSeekClient, ApiError
from app.workspace_server import parse_file
from app.article_map import _quote


class FullPaperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.native = NativeService(Path(self.temp.name))
        self.item = entry('https://arxiv.org/abs/2610.12345v2', 'Robotics paper', 'arXiv', 'paper')
        self.item['summary'] = 'This is an abstract.'
        self.native.store.put('settings', 'discovery-state', {'items':[self.item], 'updated':0})

    def tearDown(self):
        self.native.close()
        self.temp.cleanup()

    def test_default_import_preserves_original_pdf_and_every_page(self):
        with pymupdf.open() as document:
            document.new_page().insert_text((72,72), 'Introduction: robot learning.')
            document.new_page().insert_text((72,72), 'Conclusion: full document evidence on page two.')
            pdf = document.tobytes()
        loader = Mock(return_value=(pdf, 'https://arxiv.org/pdf/2610.12345v2', 'utf-8'))
        abstract = self.native.discovery.import_item(self.item['id'], mode='abstract')
        article = self.native.discovery.import_item(self.item['id'], loader)
        loader.assert_called_once_with('https://arxiv.org/pdf/2610.12345v2', 28_000_000)
        self.assertEqual('readable', article['source_status'])
        self.assertIn('full document evidence on page two', article['text'])
        self.assertEqual(2, article['source_stats']['pages'])
        self.assertEqual({1, 2}, {b['page'] for b in article['blocks'] if b['type'] == 'text'})
        mime, saved = self.native.store.asset(article['source_document']['asset'])
        self.assertEqual(('application/pdf', pdf), (mime, saved))
        self.assertEqual('abstract', self.native.store.article(abstract['id'])['source_status'])
        flags = self.native.store.get('settings', 'discovery-flags', {})[self.item['id']]
        self.assertEqual(article['id'], flags['article_id'])
        self.assertEqual(abstract['id'], flags['abstract_article_id'])

    def test_failed_fulltext_does_not_import_abstract(self):
        with self.assertRaises(ValueError):
            self.native.discovery.import_item(self.item['id'], Mock(side_effect=OSError('offline')))
        self.assertEqual([], self.native.store.summaries())
        with self.assertRaises(ValueError):
            self.native.discovery.import_item(self.item['id'], lambda *args:(b'<main>Abstract only</main>', self.item['url'], 'utf-8'))
        self.assertEqual([], self.native.store.summaries())

    def test_pdf_margin_stamp_does_not_split_body_sentence(self):
        with pymupdf.open() as document:
            page = document.new_page(width=600, height=800)
            page.insert_text((100, 150), 'Benchmarks assess visual and temporal quality,', fontsize=12)
            page.insert_text((100, 165), 'but do not establish task correctness.', fontsize=12)
            page.insert_text((60, 230), 'arXiv [cs.RO] 2026', fontsize=12, rotate=90)
            pdf = document.tobytes()
        blocks = parse_file(self.native.store, 'paper.pdf', pdf)
        text = '\n\n'.join(b['text'] for b in blocks if b['type'] == 'text')
        quote = 'Benchmarks assess visual and temporal quality, but do not establish task correctness.'
        self.assertIn(_quote({'quote': quote}, text), text)
        self.assertIn('arXiv [cs.RO] 2026', text)
        self.assertEqual(1, len([b for b in blocks if b['type'] == 'image']))


class TranslationTests(unittest.TestCase):
    def test_chunks_preserve_all_unicode_and_whitespace(self):
        text = ('机器人\n\nTranslate this sentence. 数学公式 x+y=3。\n' * 200) + '尾声'
        self.assertEqual(text, ''.join(chunks(text)))
        self.assertTrue(all(len(part) <= 2400 for part in chunks(text)))
        for body in ({'text':'hello', 'source':'en', 'target':'en'}, {'text':'hello', 'target':'auto'}, {'text':''}):
            with self.assertRaises(ValueError):
                validate(body)

    def test_translation_preserves_requested_languages_and_partial_on_cancel(self):
        job = {'cancel': threading.Event()}
        client = Mock(model='test-model')
        def reply(messages, **kwargs):
            self.assertIn('英语翻译为简体中文', messages[0]['content'])
            return '测试译文'
        client.chat.side_effect = reply
        translate(client, job, {'source':'en','target':'zh','text':'Hello.'})
        self.assertEqual('测试译文', job['result']['text'])
        self.assertTrue(job['result']['complete'])
        job = {'cancel': threading.Event()}
        def stop(messages, **kwargs):
            if client.chat.call_count > 2:
                job['cancel'].set()
            return '部分译文'
        client.chat.side_effect = stop
        translate(client, job, {'source':'en', 'target':'zh', 'text':'a' * 6000})
        self.assertFalse(job['result']['complete'])
        self.assertEqual('部分译文', job['result']['text'])

    def test_truncated_translation_response_is_not_reported_as_complete(self):
        client = DeepSeekClient('https://example.org/v1', 'test', 'test-key')
        client.require_complete_response = True
        client._post = Mock(return_value=json.dumps({'choices':[{'finish_reason':'length','message':{'content':'partial'}}]}))
        with self.assertRaises(ApiError):
            client.chat([{'role':'user','content':'example'}])


if __name__ == '__main__':
    unittest.main()
