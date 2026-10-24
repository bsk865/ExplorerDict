import io
import base64
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

    def test_multiple_questions_keep_room_for_both_subjects(self):
        article = self.store.import_article('不同实验', [
            *[{'type':'text','text':f'钙钛矿钙钛矿钙钛矿失效阈值为83摄氏度。样本{i}。'} for i in range(15)],
            {'type':'text','text':'超导实验的跃迁温度为7开尔文。'}])
        report = {}
        hits = self.index.search([article], '钙钛矿失效阈值是多少？超导实验的跃迁温度是多少？请分别引用。',
                                 text_only=True, limit=6, budget=1000, diagnostics=report)
        self.assertTrue(any('83' in h['quote'] for h in hits))
        self.assertTrue(any('7开尔文' in h['quote'] for h in hits))
        self.assertEqual(2, report['questions'])
        self.assertLessEqual(sum(len(h['quote']) for h in hits), 1000)
        self.assertEqual(len(hits), len({h['citation'] for h in hits}))

    def test_semantic_subquestions_keep_exact_neighboring_context(self):
        from unittest.mock import Mock
        blocks = [{'type':'text','text':'The dataset contains 1000 scenes. '+ 'Background details. '*15},
                  {'type':'text','text':'One assessor rated 90 videos generated from 30 scenes. Each case received a quality rating. ' + 'Further evaluation details. '*15}]
        article = self.store.import_article('Public experiment', blocks)
        semantic = Mock()
        def retrieve(articles, query, extras):
            block = article['blocks'][0 if '数据集' in query else 1]
            start = 0 if block['id'] == 'b0' else block['text'].index('Each case')
            return [{'article_id':article['id'], 'version':article['version'], 'article_title':article['title'],
                     'block':block['id'], 'start':start, 'end':start+35, 'quote':block['text'][start:start+35],
                     'type':'text', 'page':None, 'asset':None, 'semantic_score':.91}]
        semantic.retrieve.side_effect = retrieve
        hits = SourceIndex(self.store, semantic).search([article], '数据集共有多少场景？另外评估了多少个视频？', text_only=True, budget=1000)
        self.assertTrue(any('90 videos' in h['quote'] for h in hits))
        for hit in hits:
            block = next(b for b in article['blocks'] if b['id'] == hit['block'])
            self.assertEqual(block['text'][hit['start']:hit['end']], hit['quote'])
        self.assertLessEqual(sum(len(h['quote']) for h in hits), 1000)

    def test_second_question_can_refer_to_the_first_subject(self):
        article = self.article('同一主题', '钙钛矿是一类材料。其失效阈值为83摄氏度。')
        hits = self.index.search([article], '钙钛矿是一种什么材料？它的失效阈值是多少？', text_only=True, budget=500)
        self.assertTrue(any('83摄氏度' in hit['quote'] for hit in hits))
        self.assertEqual(1, len(hits))

    def test_named_source_prefers_original_over_its_bibliographic_mention(self):
        from unittest.mock import Mock
        original = self.article('Northstar · 实验报告', 'The measured threshold was 83 degrees.')
        other = self.article('Other experiment', 'References: Northstar. The measured threshold was 17 degrees in this other experiment.')
        semantic = Mock()
        semantic.retrieve.return_value = [dict(article_id=a['id'], version=a['version'], article_title=a['title'],
            block='b0', start=0, end=len(a['text']), quote=a['text'], type='text', page=None, asset=None,
            semantic_score=score) for a,score in ((other,.95),(original,.93))]
        hits = SourceIndex(self.store, semantic).search([other,original], 'Northstar measured threshold', text_only=True)
        self.assertEqual(original['id'], hits[0]['article_id'])
        self.assertEqual(original['text'], hits[0]['quote'])
        self.assertTrue(any(h['article_id'] == other['id'] for h in hits))

    def test_title_match_does_not_turn_unrelated_body_into_keyword_evidence(self):
        original = self.article('Northstar report', 'Background without the requested measurements.')
        other = self.article('Other report', 'Another background paragraph.')
        hits = self.index.search([original,other], 'Northstar', text_only=True)
        self.assertTrue(all(h['score'] == 0 for h in hits))

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

    def test_cached_passages_follow_renames_and_versions_without_text_changes(self):
        article = self.article('旧标题', '阈值为83摄氏度。')
        self.index.search([article], '阈值')
        article.update(title='新标题', version='new-source-version')
        self.store.update_article(article)
        hit = self.index.search([self.store.article(article['id'])], '阈值')[0]
        self.assertEqual('新标题', hit['article_title'])
        self.assertEqual('new-source-version', hit['version'])

    def test_article_response_cannot_restore_deleted_source_conversation(self):
        from unittest.mock import Mock
        article = self.article('删除中', '阈值为83摄氏度。')
        workspace = Workspace(Path(self.temp.name))
        client = Mock(model='test')
        client.chat.side_effect = lambda _: (self.store.delete_articles({article['id']}) or '阈值为83摄氏度。[S1]')
        workspace.client = lambda: client
        job = {'cancel':threading.Event()}
        workspace.work(job, 'chat', {'article_id':article['id'], 'question':'阈值是多少？'})
        self.assertEqual('error', job['status'])
        self.assertIsNone(self.store.get('sessions', article['id']+':'+article['version']+':'))

    def test_article_response_rejects_changed_text_even_if_version_was_retained(self):
        from unittest.mock import Mock
        article = self.article('修订中', '阈值为83摄氏度。')
        workspace = Workspace(Path(self.temp.name))
        def revise(_):
            article['blocks'][0]['text'] = '阈值修订为87摄氏度。'
            self.store.update_article(article)
            return '阈值为83摄氏度。[S1]'
        workspace.client = lambda: Mock(model='test', chat=revise)
        job = {'cancel':threading.Event()}
        workspace.work(job, 'chat', {'article_id':article['id'], 'question':'阈值是多少？'})
        self.assertEqual('error', job['status'])
        self.assertIsNone(self.store.get('sessions', article['id']+':'+article['version']+':'))

    def test_unrelated_new_term_does_not_interrupt_reading_answer(self):
        from unittest.mock import Mock
        article = self.article('继续阅读', '阈值为83摄氏度。')
        workspace = Workspace(Path(self.temp.name))
        def save_term(_):
            self.store.object(article['id'], {'type':'term', 'block':'b0', 'title':'阈值'})
            return '阈值为83摄氏度。[S1]'
        workspace.client = lambda: Mock(model='test', chat=save_term)
        job = {'cancel':threading.Event()}
        workspace.work(job, 'chat', {'article_id':article['id'], 'question':'阈值是多少？'})
        self.assertEqual('done', job['status'], job.get('message'))

    def test_article_followup_after_restart_retrieves_previous_topic(self):
        from unittest.mock import Mock
        from app import conversation_budget
        text = '普通阅读背景。'*5000 + '钙钛矿失效阈值为83摄氏度。' + '其他材料说明。'*3000
        article = self.article('长篇研读', text)
        workspace = Workspace(Path(self.temp.name))
        key = article['id']+':'+article['version']+':'
        self.store.put('sessions', key, {'messages':[
            {'role':'user', 'content':'钙钛矿失效阈值是多少？'},
            {'role':'assistant', 'content':'请回到原文核对。'}]})
        conversation_budget.close(self.store, key, '它具体是多少？', '本段对话已满')
        conversation_budget.restart(self.store, key)
        client = Mock(model='test')
        client.chat.return_value = '阈值为83摄氏度。[S1]'
        workspace.client = lambda: client
        job = {'cancel':threading.Event()}
        workspace.work(job, 'chat', {'article_id':article['id'], 'question':'它具体是多少？'})
        self.assertEqual('done', job['status'], job.get('message'))
        evidence = job['result']['messages'][-1]['evidence']
        self.assertIn('83摄氏度', evidence[0]['quote'])
        self.assertEqual(text[evidence[0]['start']:evidence[0]['end']], evidence[0]['quote'])

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

    def test_image_descriptions_do_not_invalidate_source_or_expand_selected_region(self):
        for region in (None, [.2, .1, .4, .5]):
            with self.subTest(region=region):
                workspace = Workspace(Path(self.temp.name))
                workspace.map_preloader.close()
                raw = io.BytesIO()
                Image.new('RGB', (1000, 500), 'white').save(raw, 'PNG')
                asset, _ = workspace.store.image(raw.getvalue())
                article = workspace.store.import_article('图表', [{'type':'image', 'asset':asset, 'caption':'温度图', 'page':2}])
                obj = workspace.store.object(article['id'], {'type':'region' if region else 'image', 'asset':asset,
                                            'block':'b0', 'title':'温度', 'region':region})
                workspace.store.put('settings', 'vision', {'status':'supported', 'signature':workspace.config()['signature']})
                workspace.store.put('settings', 'context_limits', {workspace.config()['signature']:65536})
                calls = []
                class Client:
                    model = 'test'
                    def chat(self, messages):
                        calls.append(messages)
                        return '温度达到83摄氏度。[S1]' if '[S1]' in str(messages) else '图中温度为83摄氏度。'
                workspace.client = Client
                job = {'cancel':threading.Event()}
                workspace.work(job, 'chat', {'article_id':article['id'], 'object_id':obj['id'], 'question':'图中温度是多少？'})
                self.assertEqual('done', job['status'], job.get('message'))
                answer = job['result']['messages'][-1]
                self.assertEqual(asset, answer['evidence'][0]['asset'])
                self.assertEqual(region, answer['evidence'][0].get('region'))
                self.assertTrue(answer['evidence'][0]['image_sent'])
                self.assertNotIn('visual_description', workspace.store.article(article['id'])['blocks'][0])
                urls = [part['image_url']['url'] for message in calls[-1] if isinstance(message['content'], list)
                        for part in message['content'] if part.get('type') == 'image_url']
                self.assertEqual(1, len(urls))
                with Image.open(io.BytesIO(base64.b64decode(urls[0].split(',', 1)[1]))) as sent:
                    self.assertEqual((400,250) if region else (1000,500), sent.size)
                if region:
                    for messages in calls:
                        for message in messages:
                            for part in message['content'] if isinstance(message['content'], list) else []:
                                if part.get('type') == 'image_url':
                                    with Image.open(io.BytesIO(base64.b64decode(part['image_url']['url'].split(',', 1)[1]))) as sent:
                                        self.assertEqual((400,250), sent.size)

    def test_changes_during_image_indexing_still_reject_the_answer(self):
        workspace = Workspace(Path(self.temp.name))
        workspace.map_preloader.close()
        raw = io.BytesIO()
        Image.new('RGB', (100, 60), 'blue').save(raw, 'PNG')
        asset, _ = workspace.store.image(raw.getvalue())
        article = workspace.store.import_article('原图标题', [{'type':'image', 'asset':asset, 'caption':'蓝色图'}])
        workspace.store.put('settings', 'vision', {'status':'supported', 'signature':workspace.config()['signature']})
        workspace.store.put('settings', 'context_limits', {workspace.config()['signature']:65536})
        calls = []
        class Client:
            model = 'test'
            def chat(self, messages):
                calls.append(messages)
                if len(calls) == 1:
                    changed = workspace.store.article(article['id'])
                    changed['title'] = '更正后的标题'
                    workspace.store.update_article(changed)
                    return '蓝色矩形'
                return '图中是蓝色矩形。[S1]'
        workspace.client = Client
        job = {'cancel':threading.Event()}
        workspace.work(job, 'chat', {'article_id':article['id'], 'question':'图中有什么？'})
        self.assertEqual('error', job['status'])
        self.assertIn('资料在回答期间已变化', job['message'])
        self.assertFalse(workspace.store.get('sessions', article['id']+':'+article['version']+':', {}).get('messages'))

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
