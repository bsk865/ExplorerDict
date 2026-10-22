import tempfile
import unittest
from pathlib import Path
from app.workspace_store import WorkspaceStore
from app.study_activity import StudyActivity


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=WorkspaceStore(Path(self.temp.name));self.activity=StudyActivity(self.store)
        self.article=self.store.import_article('Reading',[{'type':'text','text':'Source'}])

    def test_visits_do_not_imply_completion_and_count_once_daily(self):
        self.activity.record(self.article['id']);self.activity.record(self.article['id'])
        summary=self.activity.summary()
        self.assertFalse(summary['readings'][0]['completed'])
        self.assertEqual(sum(d['articles_opened'] for d in summary['days']),1)
        self.activity.record(self.article['id'],True)
        self.assertTrue(self.activity.summary()['readings'][0]['completed'])

    def test_delete_article_removes_activity(self):
        self.activity.record(self.article['id'],True)
        self.store.delete_articles({self.article['id']})
        self.assertFalse(self.activity.summary()['readings'])
        self.assertFalse(sum(d['articles_opened'] for d in self.activity.summary()['days']))
