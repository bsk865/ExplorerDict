import json
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication
from app.native.service import NativeService
from app.native.advisor import AdvisorController


class NativeAdvisorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = NativeService(Path(self.temp.name))
        self.host = QObject()
        self.host.service = self.service
        self.controller = AdvisorController(self.host)
        self.client = Mock()
        self.service.workspace.client = Mock(return_value=self.client)
        self.day = (date.today() + timedelta(days=2)).isoformat()

    def tearDown(self):
        self.controller.timer.stop()
        self.service.close()
        self.temp.cleanup()

    def respond(self, response):
        self.client.chat.return_value = json.dumps(response)
        def start(kind, body):
            job = {'id': 'fixture', 'status': 'running', 'cancel': threading.Event()}
            self.service.workspace.jobs['fixture'] = job
            self.service.workspace.work(job, kind, body)
            return {'job': 'fixture'}
        with patch.object(self.service.workspace, 'start', start):
            self.controller.submit()
        self.controller.poll()

    def test_followup_generates_persistent_plan_and_undo_preserves_other_tasks(self):
        existing = self.service.hub.save('tasks', {'title': '已有任务', 'minutes': 20})
        self.controller.draft('安排阅读')
        self.respond({'message': '', 'questions': ['哪天阅读？'], 'tasks': []})
        self.assertEqual(2, len(self.controller.value['history']))
        self.assertEqual(1, len(self.service.hub.snapshot()['tasks']))
        self.controller.draft(self.day + '下午')
        self.respond({'message': '安排好了', 'questions': [], 'tasks': [
            {'title': '阅读与整理', 'date': self.day, 'time': '14:00', 'minutes': 30, 'article_ids': []}]})
        context = json.loads(self.client.chat.call_args[0][0][-1]['content'])
        self.assertEqual('安排阅读', context['conversation'][0]['content'])
        self.assertEqual(2, len(self.service.hub.snapshot()['tasks']))
        self.assertEqual('', self.service.store.get('settings', 'native-advisor-draft')['request'])
        self.controller.undo()
        tasks = self.service.hub.snapshot()['tasks']
        self.assertEqual([existing['id']], [t['id'] for t in tasks])

    def test_modified_task_prevents_batch_undo(self):
        self.controller.draft(self.day + '安排阅读')
        self.respond({'message': '安排好了', 'questions': [], 'tasks': [
            {'title': '阅读', 'date': self.day, 'time': '14:00', 'minutes': 30, 'article_ids': []}]})
        task = self.service.hub.snapshot()['tasks'][0]
        self.service.hub.save('tasks', {'id': task['id'], 'status': 'done'})
        self.controller.undo()
        self.assertIn('已修改或完成', self.controller.message)
        self.assertEqual('done', self.service.hub.snapshot()['tasks'][0]['status'])

    def test_failed_plan_keeps_request_for_retry(self):
        self.controller.draft(self.day + '安排阅读')
        self.respond({'questions': [], 'tasks': [{'title': '错误安排', 'date': '2000-01-01', 'time': '14:00', 'minutes': 30}]})
        self.assertIsNone(self.controller.job)
        self.assertEqual(self.day + '安排阅读', self.controller.value['request'])
        self.assertFalse(self.service.hub.snapshot()['tasks'])


if __name__ == '__main__':
    unittest.main()
