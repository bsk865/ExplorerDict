import base64
import io
import json
import tempfile
import unittest
import threading
import urllib.request
import urllib.error
import http.cookiejar
from pathlib import Path
from http.server import ThreadingHTTPServer
from PIL import Image
from app.workspace_store import WorkspaceStore
from app.workspace_server import Workspace, make_handler, parse_file
from tests.workspace_fixtures import demo


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = WorkspaceStore(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def article(self):
        return self.store.import_article('测试', [{'type':'text','text':'细研磨增加表面积。'}])

    def test_content_deduplicates(self):
        a = self.article()
        self.assertEqual(a['id'], self.article()['id'])

    def test_old_version_unchanged(self):
        old = self.article()
        new = self.store.import_article('测试', [{'type':'text','text':'不同正文。'}])
        self.assertNotEqual(new['version'], old['version'])
        self.assertEqual(self.store.article(old['id'])['text'], '细研磨增加表面积。')

    def test_selection_must_belong_to_source(self):
        a = self.article()
        with self.assertRaises(ValueError):
            self.store.object(a['id'],{'type':'term','title':'不在原文','block':'b0'})
        o = self.store.object(a['id'],{'type':'term','title':'细研磨','block':'b0'})
        self.assertFalse(o['saved'])

    def test_image_survives_reopen(self):
        out=io.BytesIO(); Image.new('RGB',(100,60),'red').save(out,'PNG')
        aid,size=self.store.image(out.getvalue())
        self.assertEqual(size,(100,60))
        other=WorkspaceStore(Path(self.temp.name))
        self.assertEqual(other.asset(aid)[1],self.store.asset(aid)[1])

    def test_region_validation(self):
        a=demo(self.store); o=a['objects'][0]
        for region in ([.8,.1,.5,.2],[-.1,0,.2,.2],[0,0,0,.2],[float('nan'),0,.1,.2]):
            with self.assertRaises(ValueError):
                self.store.object(a['id'],{**o,'type':'region','region':region})

    def test_different_object_sessions(self):
        self.store.put('sessions','a:v:image1',{'draft':'第一张','messages':[]})
        self.store.put('sessions','a:v:image2',{'draft':'第二张','messages':[]})
        self.assertEqual(self.store.get('sessions','a:v:image1')['draft'],'第一张')

    def test_note_and_draft_updates_preserve_each_other_and_messages(self):
        key='a:v:'
        self.store.put('sessions',key,{'messages':[{'role':'user','content':'Question'}],'draft':'Draft'})
        self.store.update_session(key,{'note':'Article note'})
        self.store.update_session(key,{'draft':'New draft'})
        result=self.store.get('sessions',key)
        self.assertEqual(result['note'],'Article note')
        self.assertEqual(result['draft'],'New draft')
        self.assertEqual(result['messages'][0]['content'],'Question')

    def test_saved_notes_include_superseded_article_and_article_notes(self):
        a=self.article()
        obj=self.store.object(a['id'],{'type':'term','title':'细研磨','block':'b0'})
        self.store.update_object(obj['id'],{'note':'Remember this'})
        a['superseded_by']='new-version'
        self.store.update_article(a)
        self.store.update_session(a['id']+':'+a['version']+':',{'note':'Article note'})
        rows=self.store.saved_entries()
        self.assertEqual(len(rows),2)
        self.assertTrue(all(r['archived'] for r in rows))
        self.assertEqual({r['note'] for r in rows},{'Remember this','Article note'})

    def test_settings_key_encrypted_and_capability_reset(self):
        self.store.save_config({'base_url':'https://example.com/v1','model':'vision','key':'test-secret'})
        self.assertNotIn('test-secret',self.store.path.read_bytes().decode('latin1'))
        self.assertEqual(self.store.model_config()['key'],'test-secret')
        self.store.put('settings','vision',{'status':'supported'})
        self.store.save_config({'base_url':'https://example.com/v1','model':'different','key':''})
        self.assertEqual(self.store.get('settings','vision'),{})

    def test_utf8_import(self):
        b=parse_file(self.store,'中文.md','你好\n\n世界'.encode())
        self.assertEqual([x['text'] for x in b],['你好','世界'])

    def test_demo_is_explicitly_not_ai_verified(self):
        a=demo(self.store)
        self.assertEqual(len(a['objects']),2)
        self.assertEqual(a['quality'],{'demo':True})

    def test_pdf_contains_text_and_page_image(self):
        import pymupdf as fitz
        doc=fitz.open();page=doc.new_page();page.insert_text((30,30),'Reading evidence')
        b=parse_file(self.store,'a.pdf',doc.tobytes());doc.close()
        self.assertIn('Reading evidence',b[0]['text'])
        self.assertEqual(b[1]['type'],'image')

    def test_reply_preserves_new_draft_and_object_scope(self):
        workspace = Workspace(Path(self.temp.name))
        article = self.article()
        obj = self.store.object(article['id'], {'type':'term','title':'细研磨','block':'b0'})
        key = article['id'] + ':' + article['version'] + ':' + obj['id']
        other_key = article['id'] + ':' + article['version'] + ':'
        workspace.store.put('sessions',other_key,{'draft':'文章自己的草稿','messages':[]})
        class Client:
            model = 'test'
            def chat(inner, messages):
                workspace.store.put('sessions',key,{'draft':'等待时输入的新问题','note':'等待时保存的笔记','messages':[]})
                return '细研磨增加表面积。[b0]'
        workspace.client = lambda: Client()
        job={'cancel':threading.Event()}
        workspace.work(job,'chat',{'article_id':article['id'],'object_id':obj['id'],'question':'为什么？'})
        self.assertEqual(job['status'],'done')
        self.assertEqual(workspace.store.get('sessions',key)['draft'],'等待时输入的新问题')
        self.assertEqual(workspace.store.get('sessions',key)['note'],'等待时保存的笔记')
        self.assertEqual(len(workspace.store.get('sessions',key)['messages']),2)
        self.assertEqual(workspace.store.get('sessions',other_key)['messages'],[])

    def test_cancelled_reply_is_not_saved(self):
        workspace=Workspace(Path(self.temp.name)); article=self.article()
        event=threading.Event()
        class Client:
            model='test'
            def chat(inner,messages):
                event.set()
                return '这条已经取消的回答不能保存。'
        workspace.client=lambda:Client()
        job={'cancel':event}
        workspace.work(job,'chat',{'article_id':article['id'],'question':'解释'})
        self.assertEqual(job['status'],'cancelled')
        self.assertIsNone(workspace.store.get('sessions',article['id']+':'+article['version']+':'))

    def test_api_requires_token_and_same_origin(self):
        workspace=Workspace(Path(self.temp.name))
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(workspace,'token'))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        root='http://127.0.0.1:'+str(server.server_port)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:opener.open(root+'/api/state')
            self.assertEqual(error.exception.code,403)
            error.exception.close()
            opener.open(root+'/session?token=token').close()
            self.assertIn('articles',json.load(opener.open(root+'/api/state')))
            req=urllib.request.Request(root+'/api/demo',data=b'{}',headers={'Origin':'https://evil.invalid','Content-Type':'application/json'})
            with self.assertRaises(urllib.error.HTTPError) as error:opener.open(req)
            self.assertEqual(error.exception.code,403)
            error.exception.close()
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__=='__main__':
    unittest.main()
