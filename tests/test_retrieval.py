import io
import re
import tempfile
import threading
import unittest
from pathlib import Path
from PIL import Image
from app.workspace_store import WorkspaceStore
from app.workspace_server import Workspace
from app.retrieval import SourceIndex, referenced_evidence


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = WorkspaceStore(Path(self.temp.name))
        self.index = SourceIndex(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def article(self, title, text):
        return self.store.import_article(title, [{'type':'text', 'text':text}])

    def test_finds_answer_after_old_context_limit_inside_one_long_paragraph(self):
        text = '普通背景资料与数据采集。' * 4000 + '最终实验发现，钙钛矿失效阈值为83摄氏度。'
        article = self.article('长篇实验', text)
        hits = self.index.search([article], '钙钛矿失效阈值是多少？')
        self.assertIn('83摄氏度', hits[0]['quote'])
        self.assertGreater(hits[0]['start'], 26000)
        for hit in hits:
            self.assertEqual(text[hit['start']:hit['end']], hit['quote'])

    def test_cross_document_search_keeps_exact_source_identity(self):
        first = self.article('试验甲', '试验甲使用干燥气体进行加热。')
        second = self.article('试验乙', '低温磁化率在七开尔文附近发生跃迁。')
        hits = self.index.search([first,second], '低温磁化率跃迁在哪里？')
        self.assertEqual(hits[0]['article_id'], second['id'])
        self.assertEqual(hits[0]['article_title'], '试验乙')
        restricted = self.index.search([first], '低温磁化率跃迁在哪里？')
        self.assertTrue(all(h['article_id']==first['id'] for h in restricted))

    def test_pdf_text_hit_brings_corresponding_page_not_another_page(self):
        article = self.store.import_article('页码验证', [
            {'type':'text','text':'第一页讨论控制变量。','page':1},
            {'type':'text','text':'第二页测得阻抗峰值为91欧姆。','page':2},
            {'type':'image','asset':'page1','caption':'原文第1页','page':1},
            {'type':'image','asset':'page2','caption':'原文第2页','page':2}])
        hits = self.index.search([article], '阻抗峰值', limit=1)
        self.assertEqual(hits[0]['page'], 2)
        self.assertEqual([h['asset'] for h in hits if h['type']=='image'], ['page2'])

    def test_focus_image_retrieves_surrounding_body(self):
        article = self.store.import_article('图文', [
            {'type':'text','text':'样品在低温下呈现规则晶格。'},
            {'type':'image','asset':'diagram','caption':'显微图'},
            {'type':'text','text':'图中间距对应原文的测量条件。'}])
        hits = self.index.search([article], '这张图是什么意思', focus=article['objects'][0])
        self.assertEqual(hits[0]['type'],'image')
        self.assertTrue(any('测量条件' in h['quote'] for h in hits))

    def test_reindex_does_not_duplicate_and_updates_modified_sources(self):
        article = self.article('版本', '最初测量为十米。')
        self.index.ensure(article); self.index.ensure(article)
        article['blocks'][0]['text'] = '修正测量为二十米。'
        hits = self.index.search([article], '修正测量')
        self.assertIn('二十米',hits[0]['quote'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM source_chunks').fetchone()[0],1)

    def test_unknown_citation_is_not_promoted_to_evidence(self):
        with self.assertRaises(ValueError):
            referenced_evidence('未经提供的结论[S99]', [{'citation':'S1','quote':'原文'}])
        self.assertEqual(len(referenced_evidence('一[S1]二[S1]', [{'citation':'S1'}])),1)

    def test_chat_uses_retrieval_and_persists_only_cited_evidence(self):
        article = self.article('长文', '实验背景。'*8000+'离子迁移能垒为0.83电子伏特。')
        workspace = Workspace(Path(self.temp.name))
        captured = []
        class Client:
            model='test'
            def chat(self,messages):
                captured.extend(messages)
                return '迁移能垒是0.83电子伏特。[S1]'
        workspace.client=lambda:Client()
        job={'cancel':threading.Event()}
        workspace.work(job,'chat',{'article_id':article['id'],'question':'离子迁移能垒是多少？'})
        self.assertEqual(job['status'],'done', job.get('message'))
        answer = job['result']['messages'][-1]
        self.assertEqual(len(answer['evidence']),1)
        self.assertIn('0.83',answer['evidence'][0]['quote'])
        self.assertIn('0.83',str(captured))

    def test_unrepairable_reference_is_error_and_not_saved(self):
        article = self.article('文章','这是实际提供的原文。')
        workspace=Workspace(Path(self.temp.name))
        class Client:
            model='test'
            def chat(self,messages): return '伪造引用[S999]'
        workspace.client=lambda:Client()
        job={'cancel':threading.Event()}
        workspace.work(job,'chat',{'article_id':article['id'],'question':'解释原文'})
        self.assertEqual(job['status'],'error')
        self.assertIsNone(self.store.get('sessions',article['id']+':'+article['version']+':'))
