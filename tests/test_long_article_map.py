import hashlib
import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path
from app.workspace_store import WorkspaceStore
from app.long_article_map import run,segments,task_timeout,SEGMENT_CHARS


class LongMapTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=WorkspaceStore(Path(self.temp.name))
        self.cancel=threading.Event();self.calls=[]
        self.text='第一部分讨论样品的测量条件。\n\n'*2000+'最终样品温度83摄氏度。'

    def tearDown(self):self.temp.cleanup()

    def runner(self,client,*,text,selected_words,on_attempt):
        self.calls.append(text)
        nodes=[{'id':'root','parent':None,'kind':'branch','label':'本段内容','quote':text[:40]}]
        bindings=[]
        for i,word in enumerate(selected_words):
            nodes.append({'id':f'n{i}','parent':'root','kind':'concept','label':word,'quote':word})
            bindings.append({'selection_index':i,'node_ids':[f'n{i}'],'suggest_exclude':False,'note':'','candidates':[]})
        return {'root':'root','nodes':nodes,'relations':[],'bindings':bindings},{'approved':True}

    def generate(self,**kwargs):
        args=dict(text=self.text,title='长文',version='test-version',signature='test-model',selected_words=['样品','83摄氏度'],cancel=self.cancel,progress=lambda _:None,on_attempt=lambda *args:None,runner=self.runner)
        args.update(kwargs)
        return run(self.store,object(),**args)

    def test_complete_coverage_and_bounded_overlapping_segments(self):
        parts=list(segments(self.text))
        self.assertEqual(parts[0][0],0);self.assertEqual(parts[-1][1],len(self.text))
        for i,(start,end,text) in enumerate(parts):
            self.assertEqual(text,self.text[start:end]);self.assertLessEqual(len(text),SEGMENT_CHARS)
            if i:self.assertLess(start,parts[i-1][1])

    def test_task_budget_covers_more_segments_but_stays_bounded(self):
        self.assertEqual(task_timeout('短文'), 600)
        self.assertGreater(task_timeout(self.text), task_timeout('短文'))
        self.assertEqual(task_timeout('长文。' * 100_000), 1800)

    def test_merged_tree_preserves_every_word_and_exact_quotes(self):
        graph,quality=self.generate()
        self.assertGreater(len(self.calls),1)
        self.assertTrue(quality['approved'])
        self.assertEqual(graph['source_sha256'],hashlib.sha256(self.text.encode()).hexdigest())
        ids={n['id'] for n in graph['nodes']}
        self.assertEqual(len(ids),len(graph['nodes']))
        self.assertEqual([n['id'] for n in graph['nodes'] if n['parent'] is None],['document'])
        for n in graph['nodes']:
            self.assertIn(n['quote'],self.text)
            if n['parent']:self.assertIn(n['parent'],ids)
        self.assertEqual({b['selection_index'] for b in graph['bindings']},{0,1})

    def test_approved_segments_are_reused_after_restart(self):
        first,_=self.generate();count=len(self.calls)
        second,_=self.generate()
        self.assertEqual(first,second);self.assertEqual(len(self.calls),count)
        self.generate(signature='different-model');self.assertEqual(len(self.calls),count*2)

    def test_failure_resumes_without_rereading_completed_segments(self):
        original=self.runner;count=0
        def fails(client,**kwargs):
            nonlocal count
            count+=1
            if count==2:raise ValueError('模拟接口失败')
            return original(client,**kwargs)
        with self.assertRaises(ValueError):self.generate(runner=fails)
        self.assertEqual(len(self.calls),1)
        graph,_=self.generate()
        self.assertEqual(len(self.calls),graph['segments'])

    def test_invalid_or_unapproved_cached_segment_is_regenerated_without_losing_other_parts(self):
        self.generate()
        with self.store.connect() as db:
            key,version,payload=db.execute('SELECT id,version,payload FROM map_parts LIMIT 1').fetchone()
        invalid_quote=json.loads(payload)
        invalid_quote['graph']['nodes'][0]['quote']='原文不存在的依据'
        unapproved=json.loads(payload)
        unapproved['quality']['approved']=False
        for replacement in ('{broken',json.dumps(invalid_quote),json.dumps(unapproved)):
            with self.subTest(replacement=replacement):
                with self.store.connect() as db:
                    db.execute('UPDATE map_parts SET payload=? WHERE id=?',(replacement,key))
                count=len(self.calls)
                graph,quality=self.generate()
                self.assertTrue(quality['approved'])
                self.assertEqual(len(self.calls)-count,1)
                self.assertTrue(all(node['quote'] in self.text for node in graph['nodes']))

    def test_failed_forced_rebuild_clears_all_old_parts_and_resumes_new_progress(self):
        self.generate()
        count=len(self.calls)
        self.generate(signature='other-model')
        with self.store.connect() as db:
            keys=db.execute('SELECT id,version FROM map_parts').fetchall()
            db.executemany('INSERT INTO map_stages VALUES(?,?,?)',
                           [(row[0],row[1],'{}') for row in keys])
        original=self.runner
        attempts=0
        def rebuild(client,**kwargs):
            nonlocal attempts
            attempts+=1
            if attempts==2:raise ValueError('模拟重建中断')
            graph,quality=original(client,**kwargs)
            graph['nodes'][0]['label']='重新研读'
            return graph,quality
        with self.assertRaisesRegex(ValueError,'模拟重建中断'):
            self.generate(force=True,runner=rebuild)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM map_parts').fetchone()[0],count+1)
            self.assertEqual(db.execute('SELECT count(*) FROM map_stages').fetchone()[0],count)
        calls=len(self.calls)
        graph,_=self.generate()
        self.assertEqual(len(self.calls)-calls,count-1)
        self.assertEqual(graph['nodes'][1]['label'],'重新研读')
        calls=len(self.calls)
        self.generate(signature='other-model')
        self.assertEqual(len(self.calls),calls)

    def test_each_model_stage_reports_the_current_document_segment(self):
        messages = []
        def model_run(client, *, progress, cancel, **kwargs):
            progress('独立阅读全文')
            kwargs.pop('resume', None)
            kwargs.pop('checkpoint', None)
            return self.runner(client, **kwargs)
        with patch('app.long_article_map.article_map.run', side_effect=model_run):
            graph, _ = self.generate(runner=None, progress=messages.append)
        for segment in range(1, graph['segments']+1):
            self.assertIn(f'第 {segment}/{graph["segments"]} 段 · 独立阅读全文', messages)

    def test_cancelled_segment_does_not_enter_cache(self):
        def stopped(client,**kwargs):
            result=self.runner(client,**kwargs);self.cancel.set();return result
        with self.assertRaises(ValueError):self.generate(runner=stopped)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM map_parts').fetchone()[0],0)

    def test_medium_article_is_fully_segmented_and_review_stage_resumes_after_reopen(self):
        from tests.test_full_article_map import FixtureClient, FIXTURES
        from app.api_client import ApiError
        from app import article_map
        f=FIXTURES[0]
        class Interrupted(FixtureClient):
            interrupted=False
            def full_article_map(self, **kwargs):
                if kwargs['stage']=='review' and not self.interrupted:
                    self.interrupted=True
                    raise ApiError('network','模拟连接中断')
                return super().full_article_map(**kwargs)
        client=Interrupted(f)
        args=dict(text=f['text'],title='测试原文',version='stage-version',signature='test-model',
                  selected_words=f['selected_words'],cancel=self.cancel,progress=lambda _:None,on_attempt=lambda *args:None)
        with self.assertRaises(ApiError):run(self.store,client,**args)
        self.assertEqual([c['stage'] for c in client.calls],['read','generate'])
        reopened=WorkspaceStore(Path(self.temp.name))
        graph,quality=run(reopened,client,**args)
        self.assertTrue(quality['approved'])
        self.assertEqual([c['stage'] for c in client.calls],['read','generate','review'])
        self.assertEqual(graph['root'],f['draft']['root'])
        with reopened.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM map_stages').fetchone()[0],0)
        medium='一段完整的阅读材料。\n\n'*1000
        with patch('app.long_article_map.article_map.run', side_effect=lambda client,**kw:self.runner(
                client,text=kw['text'],selected_words=kw['selected_words'],on_attempt=kw['on_attempt'])):
            graph,_=self.generate(text=medium,selected_words=[],runner=None)
        self.assertLess(len(medium),article_map.MAX_TEXT_CHARS)
        self.assertGreater(graph['segments'],1)
