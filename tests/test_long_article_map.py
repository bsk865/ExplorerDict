import hashlib
import tempfile
import threading
import unittest
from pathlib import Path
from app.workspace_store import WorkspaceStore
from app.long_article_map import run,segments


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
            self.assertEqual(text,self.text[start:end]);self.assertLessEqual(len(text),24000)
            if i:self.assertLess(start,parts[i-1][1])

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

    def test_cancelled_segment_does_not_enter_cache(self):
        def stopped(client,**kwargs):
            result=self.runner(client,**kwargs);self.cancel.set();return result
        with self.assertRaises(ValueError):self.generate(runner=stopped)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM map_parts').fetchone()[0],0)
