import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from app.workspace_store import WorkspaceStore
from app.study_hub import StudyHub
from app.semantic_library import SemanticLibrary
from app.library_assistant import note_rows


class Encoder:
    def __init__(self):
        self.calls = 0
        self.hook = None

    def available(self):
        return True

    def token_count(self, text):
        return len(text)

    def encode(self, texts):
        self.calls += 1
        if self.hook:
            hook, self.hook = self.hook, None
            hook()
        return np.asarray([[1., 0.] for _ in texts], dtype='<f4')

    def query(self, text):
        return np.asarray([1., 0.], dtype='<f4')


class SemanticLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = WorkspaceStore(Path(self.temp.name))
        self.hub = StudyHub(self.store)
        self.encoder = Encoder()
        self.rag = SemanticLibrary(self.store, self.encoder)
        self.article = self.store.import_article('Source', [{'type': 'text', 'text': 'Battery temperature limits. '*80}])

    def tearDown(self):
        self.temp.cleanup()

    def test_persistence_incremental_and_exact_offsets(self):
        self.rag.sync()
        calls = self.encoder.calls
        self.rag.sync()
        self.assertEqual(calls, self.encoder.calls)
        reopened = SemanticLibrary(self.store, self.encoder)
        hits = reopened.retrieve([self.article], 'charging')
        self.assertTrue(hits)
        for hit in hits:
            self.assertEqual(hit['quote'], self.article['blocks'][0]['text'][hit['start']:hit['end']])
        self.assertEqual(reopened.status()['ready'], 1)

    def test_close_during_encoding_prevents_publish_and_restart(self):
        self.encoder.hook = self.rag.close
        self.rag.sync()
        self.assertEqual(0, self.rag.status()['vectors'])
        self.rag.request()
        self.assertFalse(self.rag.running)
        self.assertEqual(1, self.encoder.calls)

    def test_scope_edit_and_delete_filter_old_vectors_immediately(self):
        other = self.store.import_article('Other', [{'type':'text', 'text':'Different source.'}])
        self.rag.sync()
        self.assertTrue(all(h['article_id']==other['id'] for h in self.rag.retrieve([other], 'q')))
        self.article['blocks'][0]['text'] = 'Changed source.'
        self.store.update_article(self.article)
        self.assertEqual(self.rag.retrieve([self.article], 'q'), [])
        self.rag.sync()
        self.assertEqual(self.rag.retrieve([self.article], 'q')[0]['quote'], 'Changed source.')
        self.store.delete_articles([self.article['id']])
        self.assertEqual(self.rag.retrieve([self.article], 'q'), [])

    def test_notes_opt_in_and_edit_invalidates_even_when_version_unchanged(self):
        note = self.hub.save('notes', {'article_id':self.article['id'], 'content':'Private insight. '*220})
        self.rag.sync()
        self.assertFalse(any(h['type']=='note' for h in self.rag.retrieve([self.article], 'q')))
        allowed = note_rows(SimpleNamespace(store=self.store), [self.article])
        hits = self.rag.retrieve([self.article], 'q', allowed, limit=100)
        self.assertTrue(any(h['type']=='note' for h in hits))
        self.hub.save('notes', {'id':note['id'], 'content':'New insight.'})
        self.assertFalse(any(h['type']=='note' for h in self.rag.retrieve([self.article], 'q', allowed, limit=100)))

    def test_delete_during_inference_does_not_resurrect_document(self):
        self.encoder.hook = lambda: self.store.delete_articles([self.article['id']])
        self.rag.sync()
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM rag_vectors').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM rag_documents').fetchone()[0], 0)

    def test_reset_epoch_rejects_inflight_publish(self):
        self.encoder.hook = lambda: setattr(self.rag, 'epoch', self.rag.epoch+1)
        self.rag.sync()
        self.assertEqual(self.rag.status()['vectors'], 0)

    def test_failure_is_not_reported_ready(self):
        def fail():
            raise RuntimeError('inference failure')
        self.encoder.hook = fail
        self.rag.sync()
        self.assertEqual(self.rag.status()['documents'][0]['state'], 'error')
        self.assertEqual(self.rag.status()['vectors'], 0)


if __name__ == '__main__':
    unittest.main()
