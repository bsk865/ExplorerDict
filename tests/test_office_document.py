"""Word source-to-workspace regression tests, isolated from user records."""
import time
from pathlib import Path
from contextlib import contextmanager
from app.db import Database
from tests.support import tmp_dir
import unittest
from unittest.mock import Mock
from app.workspace_store import WorkspaceStore
from app.workspace_sync import synchronize
from tests.support import FakeBridge, foreground, make_service, ok_response


@contextmanager
def temp_db():
    with tmp_dir('word_') as root:
        db = Database(root / 'explorer_dict.sqlite3')
        try:
            yield db
        finally:
            db.close()


class OfficeDocumentTests(unittest.TestCase):
    def wait_source(self, db):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            row = db.query_one('SELECT * FROM articles')
            if row:
                return row
            time.sleep(.01)
        self.fail('Source never saved')

    def test_eight_words_one_snapshot_one_article_and_restart_reuses_source(self):
        words = ['alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot', 'golf', 'hotel']
        body = 'Introduction\r' + ' '.join(words) + '\rFull original conclusion'
        response = ok_response(text=words[0], context=words[0])
        response.update(method='OfficeSelection', office_document='C:/test.docx', office_handle=30, url='')
        bridge = FakeBridge(response)
        bridge.get_office_document = Mock(return_value=dict(ok=True, text=body, title='test.docx'))
        with temp_db() as db:
            service, _ = make_service(db, bridge)
            for word in words:
                response.update(text=word, context=word)
                with foreground(app='winword.exe'):
                    selection, reason = service.attempt_capture(10, 10, 'drag')
                self.assertEqual(reason, '')
                service.record_snapshot(selection)
                self.wait_source(db)
            self.assertEqual(bridge.get_office_document.call_count, 1)
            self.assertEqual(db.count_entries(), 8)
            self.assertEqual(len(db.query('SELECT * FROM articles')), 1)
            self.assertIn('Full original conclusion', db.query_one('SELECT * FROM articles')['text'])
            restarted, _ = make_service(db, bridge)
            with foreground(app='winword.exe'):
                selection, reason = restarted.attempt_capture(10, 10, 'drag')
            self.assertEqual(reason, '')
            self.assertEqual(bridge.get_office_document.call_count, 1)
            store = WorkspaceStore(Path(db.path).parent)
            synchronize(store)
            self.assertEqual(len(store.summaries()), 1)
            article = store.article(store.summaries()[0]['id'])
            self.assertEqual(len(article['objects']), 8)
            self.assertIn('Full original conclusion', article['text'])

    def test_failed_body_never_becomes_original_and_reason_reaches_workspace(self):
        response = ok_response(text='alpha', context='nearby alpha fragment')
        response.update(method='OfficeSelection', office_document='Document1', office_handle=30, url='')
        bridge = FakeBridge(response)
        bridge.get_office_document = Mock(return_value=dict(ok=False, reason='timeout'))
        with temp_db() as db:
            service, _ = make_service(db, bridge)
            with foreground(app='winword.exe'):
                selection, reason = service.attempt_capture(10, 10, 'drag')
            self.assertEqual(reason, '')
            self.assertEqual(db.count_entries(), 0)
            service.record_snapshot(selection)
            row = self.wait_source(db)
            self.assertEqual(row['text'], '')
            self.assertEqual(row['status'], 'failed')
            store = WorkspaceStore(Path(db.path).parent)
            synchronize(store)
            article = store.article(store.summaries()[0]['id'])
            self.assertEqual(article['source_status'], 'pending_source')
            self.assertIn('Word', article['source_error'])
            self.assertNotIn('nearby', article['text'])
