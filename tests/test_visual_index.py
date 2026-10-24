import io
import base64
import tempfile
import threading
import unittest
from pathlib import Path
from PIL import Image
from app.workspace_store import WorkspaceStore
from app.visual_index import VisualIndex
from app.retrieval import SourceIndex


class VisualIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=WorkspaceStore(Path(self.temp.name))
        self.index=VisualIndex(self.store)
        self.cancel=threading.Event()
        self.calls=[]
        parent=self
        class Client:
            def chat(self,messages):
                parent.calls.append(messages)
                return '阻抗曲线在频率七百赫兹附近出现峰值，峰值为91欧姆。'
        self.client=Client()

    def tearDown(self): self.temp.cleanup()

    def article(self):
        out=io.BytesIO();Image.new('RGB',(100,100),'blue').save(out,'PNG')
        asset,_=self.store.image(out.getvalue())
        return self.store.import_article('扫描文献',[{'type':'image','asset':asset,'caption':'第1页','page':1}])

    def test_visual_terms_are_searchable_without_replacing_source(self):
        a=self.article()
        enriched=self.index.enrich([a],self.client,'model-a',self.cancel,lambda _:None)
        hits=SourceIndex(self.store).search(enriched,'阻抗峰值')
        self.assertGreater(hits[0]['score'],0)
        self.assertEqual(hits[0]['quote'],'第1页')
        self.assertIn('91欧姆',hits[0]['visual_description'])
        self.assertNotIn('visual_description',self.store.article(a['id'])['blocks'][0])
        self.assertEqual(self.store.article(a['id'])['version'],a['version'])
        self.assertIn('data:image/png;base64,',str(self.calls[0]))

    def test_reopen_uses_cache_but_model_change_reindexes(self):
        a=self.article()
        self.index.enrich([a],self.client,'model-a',self.cancel,lambda _:None)
        VisualIndex(self.store).enrich([a],self.client,'model-a',self.cancel,lambda _:None)
        self.assertEqual(len(self.calls),1)
        self.index.enrich([a],self.client,'model-b',self.cancel,lambda _:None)
        self.assertEqual(len(self.calls),2)

    def test_region_index_uses_only_selected_pixels_and_its_own_cache(self):
        a = self.article()
        image = a['objects'][0]
        self.index.enrich([a], self.client, 'model-a', self.cancel, lambda _:None)
        first = dict(image, type='region', region=[.1, .2, .3, .4])
        self.index.enrich([a], self.client, 'model-a', self.cancel, lambda _:None, focus=first)
        self.index.enrich([a], self.client, 'model-a', self.cancel, lambda _:None, focus=first)
        second = dict(image, type='region', region=[.5, .5, .2, .2])
        self.index.enrich([a], self.client, 'model-a', self.cancel, lambda _:None, focus=second)
        self.index.enrich([a], self.client, 'model-a', self.cancel, lambda _:None)
        self.assertEqual(3, len(self.calls))
        sizes = []
        for messages in self.calls:
            url = messages[1]['content'][1]['image_url']['url']
            with Image.open(io.BytesIO(base64.b64decode(url.split(',', 1)[1]))) as sent:
                sizes.append(sent.size)
        self.assertEqual([(100,100), (30,40), (20,20)], sizes)

    def test_cancelled_response_is_not_saved(self):
        a=self.article()
        outer=self
        class Client:
            def chat(self,messages):
                outer.cancel.set()
                return '迟到的图片描述'
        with self.assertRaises(ValueError):
            self.index.enrich([a],Client(),'model-a',self.cancel,lambda _:None)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM visual_index').fetchone()[0],0)

    def test_empty_failure_does_not_poison_cache(self):
        class Client:
            def chat(self,messages): return ''
        a=self.article()
        with self.assertRaises(ValueError):
            self.index.enrich([a],Client(),'model-a',self.cancel,lambda _:None)
        self.index.enrich([a],self.client,'model-a',self.cancel,lambda _:None)
        self.assertEqual(len(self.calls),1)

    def test_delete_keeps_shared_image_and_removes_last_reference(self):
        a=self.article()
        asset=a['blocks'][0]['asset']
        other=self.store.import_article('另一文献',[{'type':'text','text':'另一份材料'},
            {'type':'image','asset':asset,'caption':'附图'}])
        self.index.enrich([a],self.client,'model-a',self.cancel,lambda _:None)
        self.store.delete_articles({a['id']})
        self.assertTrue(self.store.asset(asset)[1])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM visual_index').fetchone()[0],1)
        self.store.delete_articles({other['id']})
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM visual_index').fetchone()[0],0)
        with self.assertRaises(ValueError):self.store.asset(asset)
