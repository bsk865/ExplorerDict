import tempfile
import threading
import unittest
import time
from pathlib import Path
from unittest.mock import Mock, patch
from PySide6.QtCore import QObject, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from app.native.service import NativeService
from app.native.chat import ArticleConversation


class NativeArticleChatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = NativeService(Path(self.temp.name))
        self.host = QObject()
        self.host.service = self.service
        self.article = self.service.import_text('太阳能', '太阳能电池将光能转换为电能。')
        self.controller = ArticleConversation(self.host, self.article)
        self.client = Mock()
        self.client.model = 'fixture'
        self.service.workspace.client = Mock(return_value=self.client)

    def tearDown(self):
        self.controller.timer.stop()
        self.service.close()
        self.temp.cleanup()

    def send(self, response):
        self.client.chat.return_value = response
        def start(kind, body):
            job = {'id': 'fixture', 'status': 'running', 'cancel': threading.Event()}
            self.service.workspace.jobs['fixture'] = job
            self.service.workspace.work(job, kind, body)
            return {'job': 'fixture'}
        with patch.object(self.service.workspace, 'start', start):
            self.controller.send('太阳能如何转换？')
        self.controller.poll()

    def test_answer_keeps_source_citations_and_session_note(self):
        self.service.store.update_session(self.controller.key, {'note': '原来的笔记'})
        self.send('太阳能电池将光能转换为电能。[S1]')
        saved = self.service.store.get('sessions', self.controller.key)
        self.assertEqual('原来的笔记', saved['note'])
        self.assertEqual('', saved['draft'])
        self.assertEqual(2, len(saved['messages']))
        self.assertEqual(self.article['id'], saved['messages'][-1]['evidence'][0]['article_id'])

    def test_failed_answer_keeps_draft_and_restores_controls(self):
        self.client.chat.side_effect = ValueError('网络暂不可用')
        self.send('')
        self.assertIsNone(self.controller.job)
        self.assertEqual('太阳能如何转换？', self.controller.session['draft'])
        self.assertFalse(self.controller.session.get('messages'))
        self.assertEqual('网络暂不可用', self.controller.message)

    def test_explanation_instructions_stay_out_of_draft_and_history(self):
        self.client.chat.return_value = '太阳能电池将光能转换为电能。[S1]'
        question = '请解释太阳能电池。'
        job = {'cancel': threading.Event()}
        self.service.workspace.work(job, 'chat', {
            'article_id': self.article['id'], 'question': question, 'intent': 'explain'})
        self.assertEqual('done', job['status'])
        saved = self.service.store.get('sessions', self.controller.key)
        self.assertEqual(question, saved['messages'][0]['content'])
        self.assertIn('容易混淆', self.client.chat.call_args.args[0][0]['content'])

    def test_model_validation_instructions_are_not_shown_as_error(self):
        from app.api_client import ApiError
        self.client.chat.side_effect = ApiError('schema', '缺少字段。请只输出 JSON，不要改写引用。')
        self.send('')
        self.assertNotIn('JSON', self.controller.message)
        self.assertNotIn('字段', self.controller.message)
        self.assertIn('重试', self.controller.message)

    def test_http_retry_exhaustion_then_recovery_keeps_one_answer(self):
        from app.api_client import DeepSeekClient, RetryPolicy
        from app.request_supervisor import RequestSupervisor
        from tools.fake_api_server import FakeApiServer
        server = FakeApiServer()
        server.start()
        manager = RequestSupervisor(cooldown=.05)
        self.service.workspace.client = lambda: DeepSeekClient(
            server.base_url, 'fixture', 'fixture-key', timeout=1,
            retry_policy=RetryPolicy(base_delay=.02, jitter=0))
        server.responder = lambda request: (503, {'error': {'message': 'unavailable'}}, 0)
        ticks = []
        heartbeat = QTimer()
        heartbeat.timeout.connect(lambda: ticks.append(1))
        heartbeat.start(20)
        def finished():
            deadline = time.monotonic() + 4
            while self.controller.job and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.01)
            self.assertIsNone(self.controller.job, self.controller.message)
        try:
            with patch('app.request_supervisor.supervisor', manager):
                self.controller.send('太阳能如何转换？')
                finished()
                self.assertEqual(3, len(server.requests))
                self.assertIn('检查网络或服务设置', self.controller.message)
                self.assertEqual('太阳能如何转换？', self.controller.session['draft'])
                self.assertFalse(self.controller.session.get('messages'))
                self.assertEqual(0, manager.snapshot()['active'])
                self.assertGreater(len(ticks), 2)
                server.responder = lambda request: (200, {'choices': [{'message': {
                    'content': '太阳能电池将光能转换为电能。[S1]'}}]}, 0)
                self.controller.send(self.controller.session['draft'])
                finished()
                self.assertEqual(4, len(server.requests))
                saved = self.service.store.get('sessions', self.controller.key)
                self.assertEqual('', saved['draft'])
                self.assertEqual(2, len(saved['messages']))
                self.assertEqual('太阳能电池将光能转换为电能。[S1]', saved['messages'][-1]['content'])
                self.assertEqual(0, manager.snapshot()['active'])
        finally:
            heartbeat.stop()
            server.stop()

    def test_stop_releases_controller_while_transport_is_still_blocked(self):
        from app.api_client import DeepSeekClient
        from app.request_supervisor import RequestSupervisor
        entered, release = threading.Event(), threading.Event()
        supervisor = RequestSupervisor()
        client = DeepSeekClient('https://example.invalid/v1', 'fixture', 'test', timeout=20)
        self.service.workspace.client = lambda: client
        ticks = []
        heartbeat = QTimer()
        heartbeat.timeout.connect(lambda: ticks.append(1))
        heartbeat.start(20)
        def blocked(*args, **kwargs):
            entered.set()
            release.wait(5)
            return '{"choices":[{"message":{"content":"late answer"}}]}'
        def until(predicate):
            deadline = time.monotonic() + 2
            while not predicate() and time.monotonic() < deadline:
                QTest.qWait(20)
                time.sleep(.01)
            self.assertTrue(predicate(), self.controller.message)
        try:
            with patch('app.api_client.post_json', side_effect=blocked), patch('app.request_supervisor.supervisor', supervisor):
                self.controller.send('太阳能如何转换？')
                until(entered.is_set)
                self.controller.stop()
                until(lambda: self.controller.job is None)
                self.assertGreater(len(ticks), 2)
                self.assertEqual('已停止，问题已保留', self.controller.message)
                self.assertEqual('太阳能如何转换？', self.controller.session['draft'])
                self.assertFalse(self.controller.session.get('messages'))
                self.assertEqual(1, supervisor.snapshot()['active'])
                release.set()
                until(lambda: supervisor.snapshot()['active'] == 0)
                self.assertFalse(self.service.store.get('sessions', self.controller.key).get('messages'))
        finally:
            release.set()
            heartbeat.stop()

    def test_closed_conversation_keeps_handoff_until_restart(self):
        closed = {'messages': [{'role': 'user', 'content': '先前问题'}], 'closed': True,
                  'draft': '待继续的问题', 'handoff': '# 交接内容'}
        self.service.store.put('sessions', self.controller.key, closed)
        self.controller.session = closed
        with patch.object(self.service.workspace, 'start') as start:
            self.controller.send('不能直接发送')
            start.assert_not_called()
        self.assertEqual('待继续的问题', self.controller.session['draft'])
        self.controller.restart()
        self.assertEqual('# 交接内容', self.controller.session['archives'][0]['handoff'])
        self.assertEqual('待继续的问题', self.controller.session['draft'])

    def test_object_conversation_uses_separate_existing_session_key(self):
        obj = self.service.store.object(self.article['id'], {'type': 'term', 'title': '太阳能', 'block': 'b0'})
        other = ArticleConversation(self.host, self.article, obj['id'])
        other.draft('解释这个词')
        self.controller.draft('文章问题')
        self.assertEqual('解释这个词', self.service.store.get('sessions', other.key)['draft'])
        self.assertEqual('文章问题', self.service.store.get('sessions', self.controller.key)['draft'])


if __name__ == '__main__':
    unittest.main()
