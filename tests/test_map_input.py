import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from app.workspace_server import Workspace
from app.map_input import resolve_mode, markdown
from app.map_preload import fingerprint


class MapInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Workspace(Path(self.temp.name))
        self.config = dict(base_url='https://test.invalid', model='test', key='test', reasoning_effort='high')
        self.workspace.store.model_config = lambda: dict(self.config)
        self.workspace.client = lambda: SimpleNamespace(base_url=self.config['base_url'], model='test')
        self.article = self.workspace.store.import_article('PDF正文', [
            dict(type='text', text='第一段正文。', page=1),
            dict(type='image', asset='page-image', caption='原文第1页', page=1)])
        self.graph = dict(root='root', nodes=[dict(id='root', parent=None, label='正文', quote='第一段正文。')], relations=[], bindings=[])

    def tearDown(self):
        self.workspace.map_preloader.close()
        self.temp.cleanup()

    def test_pdf_text_map_does_not_require_image_capability_or_send_page_images(self):
        self.assertEqual('text', resolve_mode(self.article))
        with patch('app.long_article_map.run', return_value=(self.graph, dict(approved=True))) as run, \
             patch.object(self.workspace, 'prepare_images') as images:
            job = dict(cancel=threading.Event())
            self.workspace.work(job, 'map', dict(article_id=self.article['id']))
        self.assertEqual('done', job['status'], job.get('message'))
        images.assert_not_called()
        self.assertEqual('第一段正文。', run.call_args.kwargs['text'])
        article = self.workspace.store.article(self.article['id'])
        self.assertEqual('text', article['quality']['input_mode'])
        self.assertEqual('image', article['blocks'][1]['type'])
        self.assertIn('未分析图片', markdown(article))
        self.assertIn('第一段正文。', markdown(article))

    def test_scanned_page_cannot_be_silently_skipped_by_text_mode(self):
        self.article['blocks'].append(dict(id='scan', type='image', asset='scan', page=2, caption='扫描页'))
        self.assertEqual('multimodal', resolve_mode(self.article))
        with self.assertRaisesRegex(ValueError, '扫描页'):
            resolve_mode(self.article, 'text')

    def test_mode_and_reasoning_changes_cannot_reuse_wrong_cached_map(self):
        first = self.workspace.config()['map_signature']
        text = fingerprint(self.article, first, 'text')
        self.assertNotEqual(text, fingerprint(self.article, first, 'multimodal'))
        self.config['reasoning_effort'] = 'max'
        self.assertNotEqual(first, self.workspace.config()['map_signature'])
