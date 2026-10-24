import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock
from app.workspace_server import Workspace
from app.library_assistant import session, search


class LibraryAssistantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.w = Workspace(Path(self.temp.name))
        self.a = self.w.store.import_article('甲实验', [{'type':'text','text':'钙钛矿失效阈值为83摄氏度。'}])
        self.b = self.w.store.import_article('乙实验', [{'type':'text','text':'私人密码材料不应出现在甲实验的回答中。'}])
        self.body = {'article_ids':[self.a['id']], 'question':'钙钛矿失效阈值是多少？'}
        self.body['session_key'] = session(self.w, self.body)['key']
        self.client = Mock(model='test', base_url='https://test.invalid')
        self.client.chat.return_value = '阈值为83摄氏度。[S1]'
        self.w.client = Mock(return_value=self.client)

    def tearDown(self):
        self.temp.cleanup()

    def run_chat(self):
        job = {'cancel':threading.Event()}
        self.w.work(job, 'library-chat', self.body)
        return job

    def test_empty_library_can_open_all_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            w = Workspace(Path(directory))
            value = session(w, {'scope':'all'})
            self.assertTrue(value['key'])
            self.assertEqual([], value['article_ids'])
            self.assertEqual([], search(w, {'scope':'all', 'question':'问题'})[3])

    def test_all_scope_picks_up_new_import_without_losing_conversation(self):
        body = {'scope':'all', 'article_ids':[], 'question':'钙钛矿'}
        first = session(self.w, body)
        self.w.store.put('sessions', first['key'], {'messages':[], 'draft':'继续阅读'})
        added = self.w.store.import_article('新资料', [{'type':'text', 'text':'钙钛矿测试结果。'}])
        current = session(self.w, body)
        self.assertEqual(first['key'], current['key'])
        self.assertEqual('继续阅读', current['session']['draft'])
        self.assertIn(added['id'], current['article_ids'])
        self.assertIn(added['id'], [h['article_id'] for h in search(self.w, body)[3]])

    def test_capture_context_is_retrievable_but_not_article_body(self):
        from app.capture_documents import pending_article, captured_object
        entry = {'id':42, 'doc_key':'capture-test', 'term':'光合作用',
                 'context':'光合作用把光能转为化学能。', 'source_title':'抓取网页', 'source_url':''}
        article = pending_article(self.w.store, entry)
        captured_object(self.w.store, article, entry)
        hits = search(self.w, {'scope':'all', 'question':'光合作用'})[3]
        hit = next(h for h in hits if h['article_id'] == article['id'])
        self.assertEqual('capture_context', hit['source'])
        self.assertIn('化学能', hit['quote'])
        self.assertIsNone(hit['block'])
        self.assertFalse(self.w.store.article(article['id'])['text'].strip())

    def test_pronoun_question_after_restart_uses_previous_topic_and_current_sources(self):
        from app import conversation_budget
        body={'scope':'all','question':'它呢？'}
        key=session(self.w,body)['key']
        original={'messages':[{'role':'user','content':'钙钛矿失效阈值是多少？'},
                              {'role':'assistant','content':'请回到原文核对。'}], 'draft':'它呢？'}
        self.w.store.put('sessions',key,original)
        conversation_budget.close(self.w.store,key,'它呢？','到达预算')
        restored=conversation_budget.restart(self.w.store,key)
        self.assertEqual([],restored['messages'])
        self.assertEqual(1,len(restored['archives']))
        hits=search(self.w,body)[3]
        self.assertEqual({self.a['id']},{hit['article_id'] for hit in hits})
        self.assertTrue(any('83' in hit['quote'] for hit in hits))
        restored['messages']=[{'role':'user','content':'它呢？'}, {'role':'assistant','content':'请继续核对。'}]
        self.w.store.put('sessions',key,restored)
        self.assertEqual({self.a['id']},{hit['article_id'] for hit in search(self.w,body)[3]})
        # A legacy handoff still works, and a different session never inherits it.
        restored.pop('carryover_query')
        self.w.store.put('sessions',key,restored)
        self.assertTrue(search(self.w,body)[3])
        self.assertFalse(search(self.w,dict(body,article_ids=[self.b['id']],scope='selected'))[3])

    def test_restarted_retrieval_does_not_use_archived_answer_as_source(self):
        from app import conversation_budget
        body={'scope':'all','question':'它呢？'}
        key=session(self.w,body)['key']
        self.w.store.put('sessions',key,{'messages':[{'role':'user','content':'钙钛矿失效阈值？'},
            {'role':'assistant','content':'旧回答为99摄氏度。'}]})
        conversation_budget.close(self.w.store,key,'它呢？','到达预算')
        conversation_budget.restart(self.w.store,key)
        article=self.w.store.article(self.a['id'])
        article['blocks'][0]['text']='钙钛矿失效阈值现为87摄氏度。'
        article['text']=article['blocks'][0]['text']
        article['version']='updated-version'
        self.w.store.update_article(article)
        hits=search(self.w,body)[3]
        self.assertTrue(hits)
        self.assertTrue(all('99' not in hit['quote'] for hit in hits))
        self.assertTrue(any('87' in hit['quote'] for hit in hits))

    def test_all_scope_rejects_source_changes_during_answer(self):
        self.body.update(scope='all')
        self.body['session_key'] = session(self.w, self.body)['key']
        def changed(*args, **kwargs):
            self.w.store.import_article('新版本', [{'type':'text', 'text':'内容变化'}])
            return '阈值为83摄氏度。[S1]'
        self.client.chat.side_effect = changed
        self.assertNotEqual('done', self.run_chat()['status'])
        saved = self.w.store.get('sessions', self.body['session_key'], {})
        self.assertEqual([], saved.get('messages', []))

    def test_all_scope_rejects_note_edits_and_deletion_during_answer(self):
        from app.study_hub import StudyHub
        hub = StudyHub(self.w.store)
        for mutation in ('edit', 'delete', 'rename'):
            with self.subTest(mutation=mutation):
                note = hub.save('notes', {'article_id':self.a['id'], 'title':'个人测量', 'content':'ZEBRAXYZ心得阈值为91摄氏度。'})
                self.body = dict(scope='all', include_notes=True, question='ZEBRAXYZ阈值是多少？')
                self.body['session_key'] = session(self.w, self.body)['key']
                self.w.store.put('sessions', self.body['session_key'], {'messages':[], 'draft':'保留我的问题'})
                def changed(*args, **kwargs):
                    if mutation == 'delete':
                        hub.delete('notes', note['id'])
                    else:
                        hub.save('notes', {'id':note['id'], **({'content':'ZEBRAXYZ阈值已改为95摄氏度。'} if mutation == 'edit' else {'title':'修订后的笔记'})})
                    return '个人笔记记为91摄氏度。[S1]'
                self.client.chat.side_effect = changed
                self.assertEqual('error', self.run_chat()['status'])
                saved = self.w.store.get('sessions', self.body['session_key'])
                self.assertEqual([], saved['messages'])
                self.assertEqual('保留我的问题', saved['draft'])
                hub.delete('notes', note['id'])

    def test_note_snapshot_precedes_retrieval_and_unused_notes_do_not_invalidate(self):
        from app.study_hub import StudyHub
        from unittest.mock import patch
        hub = StudyHub(self.w.store)
        note = hub.save('notes', {'article_id':self.a['id'], 'content':'ZEBRAXYZ阈值为91摄氏度。'})
        self.body = dict(scope='all', include_notes=True, question='ZEBRAXYZ')
        self.body['session_key'] = session(self.w, self.body)['key']
        original = self.w.index.search
        def changed(*args, **kwargs):
            hits = original(*args, **kwargs)
            hub.save('notes', {'id':note['id'], 'content':'ZEBRAXYZ阈值为95摄氏度。'})
            return hits
        with patch.object(self.w.index, 'search', side_effect=changed):
            self.assertEqual('error', self.run_chat()['status'])
        self.client.chat.assert_not_called()
        self.body.update(include_notes=False, question='钙钛矿失效阈值是多少？')
        self.body['session_key'] = session(self.w, self.body)['key']
        self.client.chat.side_effect = lambda _: (hub.save('notes', {'id':note['id'], 'content':'无关笔记更新。'}) and '阈值为83摄氏度。[S1]')
        self.assertEqual('done', self.run_chat()['status'])

    def test_scope_and_separate_session(self):
        j = self.run_chat()
        self.assertEqual(j['status'], 'done')
        prompt = str(self.client.chat.call_args)
        self.assertIn('83摄氏度', prompt)
        self.assertNotIn('私人密码', prompt)
        self.assertEqual(j['result']['messages'][-1]['evidence'][0]['article_id'], self.a['id'])

    def test_greeting_does_not_call_model_even_when_matching_material(self):
        self.body['question'] = '你好！'
        result = self.run_chat()
        self.assertEqual(result['status'], 'done')
        self.assertIn('你好', result['result']['messages'][-1]['content'])
        self.assertEqual(result['result']['messages'][-1]['evidence'], [])
        self.w.client.assert_not_called()

    def test_greeting_prefix_does_not_hide_question(self):
        self.body['question'] = '你好，钙钛矿失效阈值是多少？'
        self.assertEqual(self.run_chat()['status'], 'done')
        self.w.client.assert_called_once()
        other = session(self.w, {'article_ids':[self.b['id']]})
        self.assertEqual(other['session']['messages'], [])
        self.assertNotEqual(other['key'], self.body['session_key'])

    def test_no_match_does_not_call_api(self):
        self.body['question'] = 'ZXQVVV'
        j = self.run_chat()
        self.assertEqual(j['status'], 'done')
        self.w.client.assert_not_called()
        self.assertIn('没找到相关内容', j['result']['messages'][-1]['content'])

    def test_preview_is_local_and_text_only(self):
        _, _, _, hits = search(self.w, self.body)
        self.assertTrue(hits)
        self.assertTrue(all(e['type']=='text' for e in hits))
        self.w.client.assert_not_called()

    def test_cancellation_does_not_save_answer(self):
        j = {'cancel':threading.Event()}
        self.client.chat.side_effect = lambda _: (j['cancel'].set() or '阈值83摄氏度。[S1]')
        self.w.work(j, 'library-chat', self.body)
        self.assertEqual(j['status'], 'cancelled')
        self.assertEqual(session(self.w, self.body)['session']['messages'], [])

    def test_success_clears_only_the_submitted_draft(self):
        key = self.body['session_key']
        self.w.store.put('sessions', key, {'messages':[], 'draft':self.body['question']})
        self.assertEqual('', self.run_chat()['result']['draft'])
        saved = self.w.store.get('sessions', key)
        saved['draft'] = self.body['question']
        self.w.store.put('sessions', key, saved)
        def next_draft(_):
            latest = self.w.store.get('sessions', key)
            latest['draft'] = '接下来想问的新问题'
            self.w.store.put('sessions', key, latest)
            return '阈值为83摄氏度。[S1]'
        self.client.chat.side_effect = next_draft
        result = self.run_chat()
        self.assertEqual('done', result['status'])
        self.assertEqual('接下来想问的新问题', result['result']['draft'])
        self.assertEqual(4, len(result['result']['messages']))

    def test_deleting_article_removes_library_conversation(self):
        self.run_chat()
        self.w.store.delete_articles({self.a['id']})
        self.assertIsNone(self.w.store.get('sessions', self.body['session_key']))
        with self.assertRaises(ValueError):
            search(self.w, self.body)

    def test_deleted_during_response_cannot_resurrect(self):
        self.client.chat.side_effect = lambda _: (self.w.store.delete_articles({self.a['id']}) or '阈值83摄氏度。[S1]')
        self.assertEqual(self.run_chat()['status'], 'error')
        self.assertIsNone(self.w.store.get('sessions', self.body['session_key']))

    def test_invalid_citation_not_saved(self):
        self.client.chat.return_value = '阈值99摄氏度。[S999]'
        self.assertEqual(self.run_chat()['status'], 'error')
        self.assertEqual(session(self.w, self.body)['session']['messages'], [])

    def test_changed_version_rejects_old_session(self):
        a = self.w.store.article(self.a['id']); a['version'] = 'changed'
        self.w.store.update_article(a)
        self.assertEqual(self.run_chat()['status'], 'error')
        self.w.client.assert_not_called()

    def test_personal_notes_are_opt_in_and_versioned(self):
        from app.study_hub import StudyHub
        hub = StudyHub(self.w.store)
        note = hub.save('notes', {'article_id':self.a['id'], 'content':'独立心得：ZEBRAXYZ需要再次核验。'})
        body = {'article_ids':[self.a['id']], 'question':'ZEBRAXYZ'}
        self.assertFalse(search(self.w, body)[3])
        body['include_notes'] = True
        _, key, _, hits = search(self.w, body)
        self.assertEqual(hits[0]['source'], 'personal_note')
        self.assertEqual(hits[0]['note_id'], note['id'])
        hub.save('notes', {'id':note['id'], 'content':'ZEBRAXYZ已经重新检查。'})
        self.assertNotEqual(key, search(self.w, body)[1])

    def test_budget_shortage_is_not_no_match(self):
        from unittest.mock import patch
        diagnostics = {}
        with patch('app.library_assistant.conversation_budget.retrieval_budget', return_value=0):
            hits = search(self.w, self.body, diagnostics)[3]
        self.assertFalse(hits)
        self.assertGreater(diagnostics['matched'], 0)

    def test_comparison_uses_each_article_even_without_keyword_match(self):
        body={'article_ids':[self.a['id'],self.b['id']],'question':'Compare methods and limitations','mode':'compare'}
        hits=search(self.w,body)[3]
        self.assertEqual({h['article_id'] for h in hits},{self.a['id'],self.b['id']})
        self.assertEqual(len({h['citation'] for h in hits}),len(hits))


if __name__ == '__main__':
    unittest.main()
