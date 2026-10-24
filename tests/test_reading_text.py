"""Visible rich text remains usable without rewriting existing article records."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from app.discovery import Discovery
from app.db import Database
from app.map_input import resolve_mode
from app.reading_annotations import ReadingAnnotations, block_text
from app.retrieval import SourceIndex
from app.semantic_library import SemanticLibrary
from app.study_hub import StudyHub
from app.workspace_store import WorkspaceStore
from app.workspace_sync import synchronize
from tests.test_semantic_library import Encoder


class ReadingTextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = WorkspaceStore(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def article(self):
        return self.store.import_article('实验记录', [dict(type='text',
            html='<p>人工智能实验\n  记录：钙钛矿的失效阈值为<strong>83摄氏度</strong>。</p>',
            text='人工智能实验 记录：钙钛矿的失效阈值为83摄氏度。')])

    def test_html_only_body_supports_search_exploration_notes_and_map(self):
        article = self.store.import_article('富文本', [dict(type='text',
            html='<p>钙钛矿失效阈值为<strong>83摄氏度</strong>。</p>')])
        source = block_text(article['blocks'][0])
        self.assertEqual(source, article['text'])
        self.assertEqual(len(source), self.store.summaries()[0]['chars'])
        hit = SourceIndex(self.store).search([article], '钙钛矿失效阈值', text_only=True)[0]
        self.assertEqual(source[hit['start']:hit['end']], hit['quote'])
        obj = self.store.object(article['id'], dict(type='term', block='b0', title='83摄氏度'))
        self.assertEqual('83摄氏度', obj['title'])
        note = StudyHub(self.store).save('notes', dict(article_id=article['id'],
            content='记住单位。', block='b0', quote=source))
        self.assertEqual(source, note['quote'])
        self.assertEqual('text', resolve_mode(article, 'text'))

    def test_old_payload_and_citations_survive_read_and_index_repair(self):
        article = self.article()
        source = block_text(article['blocks'][0])
        start = source.index('83摄氏度')
        annotations = ReadingAnnotations(self.store)
        mark = annotations.save(dict(article_id=article['id'], version=article['version'], style='yellow',
            segments=[dict(block='b0', start=start, end=start+len('83摄氏度'), quote='83摄氏度')]))
        key = article['id']+':'+article['version']+':'
        session = dict(messages=[dict(role='assistant', content='83摄氏度。', evidence=mark['segments'])])
        self.store.put('sessions', key, session)
        article['text'] = ''
        raw = json.dumps({k:v for k,v in article.items() if k != 'objects'}, ensure_ascii=False)
        index = SourceIndex(self.store)
        old_fingerprint = hashlib.sha256(json.dumps({k:article[k] for k in ('title','version','blocks')},
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.store.connect() as db:
            db.execute('UPDATE articles SET payload=? WHERE id=?', (raw, article['id']))
            db.execute('INSERT INTO source_indexes VALUES(?,?)', (article['id'], old_fingerprint))
        reopened = WorkspaceStore(self.store.directory)
        restored = reopened.article(article['id'])
        self.assertEqual(source, restored['text'])
        self.assertEqual(article['version'], restored['version'])
        self.assertEqual(article['blocks'], restored['blocks'])
        hits = SourceIndex(reopened).search([restored], '钙钛矿失效阈值', text_only=True)
        self.assertTrue(hits)
        for hit in hits:
            self.assertEqual(source[hit['start']:hit['end']], hit['quote'])
        self.assertEqual(session, reopened.get('sessions', key))
        self.assertEqual([mark], annotations.list(article['id'])['items'])
        self.assertEqual(len(source), reopened.summaries()[0]['chars'])
        with reopened.connect() as db:
            self.assertEqual(raw, db.execute('SELECT payload FROM articles WHERE id=?', (article['id'],)).fetchone()[0])

    def test_semantic_index_rejects_old_body_then_rebuilds_exact_offsets(self):
        article = self.article()
        rag = SemanticLibrary(self.store, Encoder())
        rag.sync()
        with self.store.connect() as db:
            db.execute("UPDATE rag_documents SET fingerprint='old-compressed-text'")
        self.assertEqual([], rag.retrieve([article], '阈值'))
        rag.sync()
        hits = rag.retrieve([article], '阈值')
        self.assertTrue(hits)
        for hit in hits:
            self.assertEqual(block_text(article['blocks'][0])[hit['start']:hit['end']], hit['quote'])

    def test_floating_context_and_local_interests_include_html_body(self):
        article = self.store.import_article('阅读记录', [dict(type='text', html='<p>人工智能研究的阈值为<strong>83摄氏度</strong>。</p>')])
        obj = self.store.object(article['id'], dict(type='term', block='b0', title='83摄氏度'))
        self.store.update_object(obj['id'], dict(saved=True))
        native = Database(self.store.directory/'explorer_dict.sqlite3')
        try:
            synchronize(self.store)
            self.assertEqual(block_text(article['blocks'][0]), native.query('SELECT * FROM entries')[0]['context'])
        finally:
            native.close()
        discovery = Discovery(self.store)
        preferences = discovery.preferences()
        preferences.update(topics=[], personal=True, blocked=[])
        discovery.save_preferences(preferences)
        self.assertIn('人工智能', dict(discovery.interests()))
