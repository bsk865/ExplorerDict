import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.workspace_server import Workspace
from app.map_preload import fingerprint
from app import article_map


class MapPreloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.w = Workspace(Path(self.temp.name))
        self.config = {'base_url': 'https://test.invalid', 'model': 'test', 'key': 'test'}
        self.w.store.model_config = lambda: dict(self.config)
        self.w.client = lambda: SimpleNamespace(base_url=self.config['base_url'], model=self.config['model'])
        self.a = self.w.store.import_article('文章', [{'type': 'text', 'text': '资源有限，需要减少计算量。'}])
        self.graph = {'root': 'n0', 'nodes': [], 'relations': [], 'bindings': []}

    def tearDown(self):
        self.w.map_preloader.close()
        for job in self.w.jobs.values():
            job['cancel'].set()
        if self.w.map_preloader.thread:
            self.w.map_preloader.thread.join(2)
        self.temp.cleanup()

    def wait_job(self, job):
        deadline = time.monotonic() + 4
        while job['status'] == 'running' and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertNotEqual(job['status'], 'running')

    def test_background_and_foreground_share_one_request_then_persist_cache(self):
        entered, release = threading.Event(), threading.Event()
        def run(*args, **kwargs):
            entered.set()
            release.wait(3)
            return self.graph, {'approved': True}
        with patch('app.long_article_map.run', side_effect=run) as runner:
            result = self.w.start('map', {'article_id': self.a['id']}, background=True)
            self.assertTrue(entered.wait(2))
            job = self.w.jobs[result['job']]
            self.assertTrue(job['background'])
            self.assertEqual(self.w.start('map', {'article_id': self.a['id']}), result)
            self.assertFalse(job['background'])
            latest = self.w.store.article(self.a['id'])
            latest['user_metadata'] = '保留并发更新'
            self.w.store.update_article(latest)
            release.set()
            self.wait_job(job)
            self.assertEqual(job['status'], 'done', job.get('message'))
            self.assertTrue(self.w.start('map', {'article_id': self.a['id']})['cached'])
            self.assertEqual(runner.call_count, 1)
            self.assertEqual(self.w.store.article(self.a['id'])['user_metadata'], '保留并发更新')
            other = Workspace(Path(self.temp.name))
            other.store.model_config = self.w.store.model_config
            self.assertTrue(other.start('map', {'article_id': self.a['id']})['cached'])
            other.map_preloader.close()

    def test_changed_input_and_deleted_article_cannot_receive_old_map(self):
        for change in ('model', 'delete', 'words'):
            with self.subTest(change=change):
                a = self.w.store.import_article(change, [{'type':'text', 'text': change + ' 原文。'}])
                def run(*args, **kwargs):
                    if change == 'model':
                        self.config['model'] += '-new'
                    elif change == 'delete':
                        self.w.store.delete_articles({a['id']})
                    else:
                        self.w.store.object(a['id'], {'type': 'term', 'title': '原文', 'block': 'b0'})
                    return self.graph, {'approved': True}
                with patch('app.long_article_map.run', side_effect=run):
                    result = self.w.start('map', {'article_id': a['id']}, background=True)
                    self.wait_job(self.w.jobs[result['job']])
                if change != 'delete':
                    self.assertIsNone(self.w.store.article(a['id'])['map'])
                else:
                    with self.assertRaises(ValueError):
                        self.w.store.article(a['id'])

    def test_offer_is_delayed_deduplicated_and_failure_has_no_retry_loop(self):
        entered = threading.Event()
        def run(*args, **kwargs):
            entered.set()
            raise ValueError('模拟失败')
        with patch('app.long_article_map.run', side_effect=run) as runner:
            for _ in range(4):
                self.w.map_preloader.offer(self.a)
            self.assertFalse(entered.wait(.2))
            self.assertTrue(entered.wait(3))
            self.wait_job(next(iter(self.w.jobs.values())))
            self.w.map_preloader.offer(self.a)
            self.assertEqual(len(self.w.map_preloader.pending), 0)
            self.assertEqual(runner.call_count, 1)

    def test_missing_key_and_incomplete_source_do_not_queue(self):
        self.config['key'] = ''
        self.w.map_preloader.offer(self.a)
        self.config['key'] = 'test'
        self.w.map_preloader.offer(dict(self.a, source_status='pending_source'))
        self.assertEqual(len(self.w.map_preloader.pending), 0)
        self.w.map_preloader.offer(dict(self.a, map={'legacy': True}, quality=None))
        self.assertEqual(len(self.w.map_preloader.pending), 0)

    def test_cancellation_between_model_calls_stops_next_stage(self):
        cancel = threading.Event()
        class Client:
            calls = 0
            def full_article_map(self, **kwargs):
                self.calls += 1
                cancel.set()
                return {}
        client = Client()
        with patch('app.article_map.validate_reading', return_value={}):
            with self.assertRaises(ValueError):
                article_map.run(client, text='原文。', cancel=cancel)
        self.assertEqual(client.calls, 1)
