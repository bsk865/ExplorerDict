import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from app.workspace_server import Workspace
from app.multimodal_map import source_material,attach_evidence,review_images


class MultimodalMapTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.w=Workspace(Path(self.temp.name));self.store=self.w.store
        self.store.model_config=lambda:{'base_url':'https://test.invalid','model':'test','key':'test'}
        out=io.BytesIO();Image.new('RGB',(80,80),'blue').save(out,'PNG')
        self.asset,_=self.store.image(out.getvalue())
        self.a=self.store.import_article('扫描页',[{'type':'image','asset':self.asset,'caption':'图1'}])
        self.material=self.store.article(self.a['id']);self.material['blocks'][0]['visual_description']='图片中有一个蓝色正方形。'
        self.text,self.spans=source_material(self.material)

    def tearDown(self):self.temp.cleanup()

    def graph(self):
        return {'root':'n0','nodes':[{'id':'n0','parent':None,'kind':'branch','label':'蓝色正方形','quote':'图片中有一个蓝色正方形。'}],'relations':[],'bindings':[]}

    def test_visual_quote_is_explicitly_linked_to_original_image(self):
        g=attach_evidence(self.graph(),self.text,self.spans)
        e=g['nodes'][0]['evidence'][0]
        self.assertEqual((e['kind'],e['asset'],e['block']),('image',self.asset,'b0'))
        self.assertEqual(self.store.article(self.a['id'])['text'],'图1')

    def test_repeated_ocr_quote_links_text_and_image(self):
        self.material['blocks'].insert(0,{'id':'text','type':'text','text':'图片中有一个蓝色正方形。'})
        text,spans=source_material(self.material)
        g=attach_evidence(self.graph(),text,spans)
        self.assertEqual({e['kind'] for e in g['nodes'][0]['evidence']},{'text','image'})

    def test_visual_review_receives_pixels_and_rejects_disputed_claim(self):
        g=attach_evidence(self.graph(),self.text,self.spans);calls=[]
        class Client:
            def chat(self,messages):
                calls.append(messages);return '{"approved":false,"issues":["数量不一致"]}'
        with self.assertRaises(ValueError):review_images(self.store,Client(),self.material,g,threading.Event(),lambda _:None)
        self.assertIn('data:image/png;base64,',str(calls))

    def test_image_only_map_job_keeps_source_and_requires_visual_review(self):
        signature=self.w.config()['signature']
        self.store.put('settings','vision',{'signature':signature,'status':'supported'})
        with self.store.connect() as db:db.execute('INSERT INTO visual_index VALUES(?,?,?)',(self.asset,signature,'图片中有一个蓝色正方形。'))
        class Client:
            model='test'
            base_url='https://test.invalid'
            def chat(self,messages):return '{"approved":true,"issues":[]}'
        self.w.client=lambda:Client()
        job={'cancel':threading.Event()}
        with patch('app.long_article_map.run',return_value=(self.graph(),{'approved':True})) as runner:
            self.w.work(job,'map',{'article_id':self.a['id']})
        self.assertEqual(job['status'],'done',job.get('message'))
        self.assertIn('蓝色正方形',runner.call_args.kwargs['text'])
        result=self.store.article(self.a['id'])
        self.assertEqual(result['text'],'图1');self.assertEqual(result['version'],self.a['version'])
        self.assertEqual(result['quality']['image_review'],[{'block':'b0','approved':True}])

    def test_failed_image_review_preserves_existing_map_and_invalidates_description(self):
        signature=self.w.config()['signature']
        self.store.put('settings','vision',{'signature':signature,'status':'supported'})
        with self.store.connect() as db:db.execute('INSERT INTO visual_index VALUES(?,?,?)',(self.asset,signature,'图片中有一个蓝色正方形。'))
        self.a['map']={'old':'keep'};self.store.update_article(self.a)
        class Client:
            model='test'
            base_url='https://test.invalid'
            def chat(self,messages):return '{"approved":false,"issues":["不能确认形状"]}'
        self.w.client=lambda:Client();job={'cancel':threading.Event()}
        with patch('app.long_article_map.run',return_value=(self.graph(),{'approved':True})):
            self.w.work(job,'map',{'article_id':self.a['id']})
        self.assertEqual(job['status'],'error')
        self.assertEqual(self.store.article(self.a['id'])['map'],{'old':'keep'})
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM visual_index').fetchone()[0],0)
            self.assertIn('不能确认形状',db.execute('SELECT payload FROM attempts').fetchone()[0])
