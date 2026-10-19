from contextlib import closing
from pathlib import Path
import io
import tempfile
import unittest
import zipfile
from app.db import Database
from app.workspace_store import WorkspaceStore
from app.workspace_sync import synchronize
from app.workspace_server import parse_file


class SharedReadingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = WorkspaceStore(Path(self.temp.name))
        self.native = Database(Path(self.temp.name)/'explorer_dict.sqlite3')

    def tearDown(self):
        self.native.close()
        self.temp.cleanup()

    def capture(self):
        batch = self.native.create_batch('来源')
        eid = self.native.add_entry(batch_id=batch,term='术语',context='原文中的术语含义。',source_title='外部文章',doc_key='test-document')
        self.native.update_entry(eid,explain_status='ok',detail='模型解释',one_line='解释摘要')
        self.native.add_chat_turn(entry_id=eid,role='user',content='追问')
        self.native.add_chat_turn(entry_id=eid,role='assistant',content='回答')
        return eid

    def session(self):
        article = self.store.article(self.store.summaries()[0]['id'])
        obj = article['objects'][0]
        key = article['id']+':'+article['version']+':'+obj['id']
        return article,obj,key

    def test_capture_import_has_source_and_no_generated_personal_note(self):
        eid = self.capture()
        synchronize(self.store)
        article,obj,key = self.session()
        self.assertEqual(article['source_status'],'selection_fragment')
        self.assertEqual(obj['note'],'')
        self.assertEqual([m['content'] for m in self.store.get('sessions',key)['messages']],['模型解释','追问','回答'])
        version = self.native.query_one('PRAGMA data_version')[0]
        self.assertEqual(synchronize(self.store),0)
        self.assertEqual(self.native.query_one('PRAGMA data_version')[0],version)
        self.assertEqual(len(self.native.list_chat_turns(eid)),2)

    def test_main_save_answer_and_chat_reach_float_without_duplicates(self):
        article=self.store.import_article('本机文章',[{'type':'text','text':'一个术语。'}])
        obj=self.store.object(article['id'],{'type':'term','title':'术语','block':'b0'})
        self.store.update_object(obj['id'],{'saved':True,'note':'我的笔记'})
        key=article['id']+':'+article['version']+':'+obj['id']
        self.store.put('sessions',key,{'messages':[{'role':'user','content':'解释'},{'role':'assistant','content':'正文依据 [S1]','evidence':[{'citation':'S1'}]}],'draft':'草稿'})
        synchronize(self.store)
        synchronize(self.store)
        self.assertEqual(len(self.store.summaries()),1)
        entries=self.native.query('SELECT * FROM entries')
        self.assertEqual(len(entries),1)
        self.assertEqual(entries[0]['detail'],'正文依据 [S1]')
        self.assertEqual(len(self.native.list_chat_turns(entries[0]['id'])),2)
        result=self.store.get('sessions',key)
        self.assertEqual(result['draft'],'草稿')
        self.assertEqual(len(result['messages']),2)
        self.assertEqual(result['messages'][1]['evidence'],[{'citation':'S1'}])
        self.assertEqual(self.store.get('objects',obj['id'])['note'],'我的笔记')

    def test_deletion_does_not_resurrect_and_can_save_again(self):
        eid=self.capture(); synchronize(self.store)
        _,obj,_=self.session()
        self.native.delete_entry(eid)
        synchronize(self.store); synchronize(self.store)
        self.assertFalse(self.store.get('objects',obj['id'])['saved'])
        self.assertEqual(self.native.query('SELECT * FROM entries'),[])
        self.store.update_object(obj['id'],{'saved':True})
        synchronize(self.store)
        self.assertEqual(len(self.native.query('SELECT * FROM entries')),1)
        self.store.update_object(obj['id'],{'saved':False})
        synchronize(self.store); synchronize(self.store)
        self.assertEqual(self.native.query('SELECT * FROM entries'),[])

    def test_updates_keep_draft_and_note(self):
        eid=self.capture(); synchronize(self.store)
        _,obj,key=self.session()
        self.store.update_session(key,{'draft':'未发送','note':'个人笔记'})
        self.native.update_entry(eid,detail='新解释')
        self.native.add_chat_turn(entry_id=eid,role='assistant',content='补充回答')
        synchronize(self.store)
        result=self.store.get('sessions',key)
        self.assertEqual(result['draft'],'未发送')
        self.assertEqual(result['note'],'个人笔记')
        self.assertEqual(result['messages'][0]['content'],'新解释')
        self.assertEqual(result['messages'][-1]['content'],'补充回答')

    def test_model_configuration_is_shared(self):
        self.store.save_config({'base_url':'https://example.com/v1','model':'test-model','key':'isolated-key'})
        self.assertEqual(self.native.get_secret('api_key'),'isolated-key')
        self.native.set_setting('api.model','updated-model')
        self.assertEqual(self.store.model_config()['model'],'updated-model')

    def test_late_full_source_preserves_identity_notes_draft_and_citations(self):
        from app.article_source import Article
        eid = self.capture()
        synchronize(self.store)
        old, obj, key = self.session()
        self.store.update_object(obj['id'], {'note':'我的批注','understood':True})
        session = self.store.get('sessions',key)
        session['draft'] = '尚未发送'
        session['messages'][-1]['evidence'] = [{'article_id':old['id'],'quote':'术语'}]
        self.store.put('sessions',key,session)
        self.store.update_session(old['id']+':'+old['version']+':', {'note':'全文笔记'})
        entry = self.native.get_entry(eid)
        self.native.put_article(entry['doc_key'], Article(ok=True,title='完整原文',text='前言。原文中的术语含义。后续完整段落。'))
        synchronize(self.store)
        article, current, new_key = self.session()
        self.assertEqual(article['source_status'],'legacy_text')
        self.assertEqual(current['id'],obj['id'])
        self.assertEqual(current['note'],'我的批注')
        self.assertTrue(current['understood'])
        self.assertEqual(self.store.get('sessions',new_key),session)
        self.assertEqual(self.store.get('sessions',article['id']+':'+article['version']+':')['note'],'全文笔记')
        self.assertEqual(self.store.article(old['id'])['text'],old['text'])
        self.assertEqual(synchronize(self.store),0)
        self.assertEqual(len(self.native.query('SELECT * FROM entries')),1)
        self.store.delete_articles(self.store.related_versions(article['id']))
        synchronize(self.store)
        self.assertEqual(self.store.summaries(),[])
        self.assertEqual(self.native.query('SELECT * FROM entries'),[])

    def test_remove_article_clears_linked_capture_and_keeps_other_material(self):
        self.capture(); synchronize(self.store)
        article,obj,key=self.session()
        other=self.store.import_article('保留资料',[{'type':'text','text':'另一份原文'}])
        newer=self.store.import_article('新版本',[{'type':'text','text':'补充后的原文'}])
        newer['previous_id']=article['id'];self.store.update_article(newer)
        self.store.delete_articles(self.store.related_versions(newer['id']))
        synchronize(self.store)
        self.assertEqual([a['id'] for a in self.store.summaries()],[other['id']])
        self.assertIsNone(self.store.get('sessions',key))
        self.assertIsNone(self.store.get('objects',obj['id']))
        self.assertEqual(self.native.query('SELECT * FROM entries'),[])

    def test_docx_without_relationship_part(self):
        out=io.BytesIO()
        with zipfile.ZipFile(out,'w') as z:
            z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>正文</w:t></w:r></w:p></w:body></w:document>')
        self.assertEqual(parse_file(self.store,'text.docx',out.getvalue()),[{'type':'text','text':'正文'}])

    def test_eight_selections_share_one_document_without_fulltext(self):
        batch = self.native.create_batch('同一文档')
        ids=[]
        for i in range(8):
            ids.append(self.native.add_entry(batch_id=batch,term=f'词语{i}',context=f'这是第{i}处的词语{i}上下文。',doc_key='same-document',source_title='测试文章'))
            synchronize(self.store)
            self.assertEqual(len(self.store.summaries()),1)
        article=self.store.article(self.store.summaries()[0]['id'])
        self.assertEqual(len(article['objects']),8)
        self.assertEqual(len(article['blocks']),8)
        self.assertEqual(article['source_status'],'selection_fragment')
        self.assertEqual(synchronize(self.store),0)
        self.assertEqual(len(self.native.query('SELECT * FROM entries')),8)
        from app.article_source import Article
        self.native.put_article('same-document',Article(ok=True,title='测试文章',text='\n'.join(f'词语{i}的完整原文' for i in range(8))))
        synchronize(self.store)
        self.assertEqual(len(self.store.summaries()),1)
        article=self.store.article(self.store.summaries()[0]['id'])
        self.assertEqual(len(article['objects']),8)
        self.assertEqual(article['source_status'],'legacy_text')
        self.store.delete_articles(self.store.related_versions(article['id']))
        self.assertEqual(self.native.query('SELECT * FROM entries'),[])

    def test_merge_can_reuse_existing_article_without_archiving_itself(self):
        from app.capture_documents import consolidate, document_identity
        entry = {'id':1, 'doc_key':'same-document'}
        articles=[]
        links={}
        entries={}
        for i in range(2):
            block={'type':'text','text':'词语0和词语1位于同一段原文'}
            if i:
                block['capture_document']=document_identity(entry)
            article=self.store.import_article('原文',[block],origin='capture')
            article['source_status']='selection_fragment'
            self.store.update_article(article)
            obj=self.store.object(article['id'],{'type':'term','title':f'词语{i}','block':'b0'})
            self.store.update_object(obj['id'],{'note':f'笔记{i}'})
            entries[i]=dict(entry,id=i)
            links[i]={'object_id':obj['id']}
            articles.append(article)
        self.assertEqual(consolidate(self.store,entries,links),1)
        self.assertEqual(len(self.store.summaries()),1)
        current=self.store.article(articles[1]['id'])
        self.assertFalse(current.get('superseded_by'))
        self.assertEqual(len(current['objects']),2)
        self.assertEqual({o['note'] for o in current['objects']},{'笔记0','笔记1'})
        self.assertEqual([h['id'] for h in current['capture_history']],[articles[0]['id']])
        self.assertEqual(consolidate(self.store,entries,links),0)

    def test_identical_titles_and_text_do_not_merge_distinct_documents(self):
        batch=self.native.create_batch('来源')
        for key in ['document-A','document-B']:
            self.native.add_entry(batch_id=batch,term='术语',context='相同的术语上下文',doc_key=key,source_title='同名文章')
        synchronize(self.store)
        self.assertEqual(len(self.store.summaries()),2)

    def test_existing_split_fragments_merge_without_losing_personal_work(self):
        import json
        batch=self.native.create_batch('来源')
        original=[]
        for i in range(2):
            eid=self.native.add_entry(batch_id=batch,term=f'词语{i}',context=f'词语{i}的片段',doc_key='same-document',source_title='原文')
            a=self.store.import_article('旧片段',[{'type':'text','text':f'词语{i}的片段'}],origin='capture')
            a.update(source_status='selection_fragment',map={'retained':i});self.store.update_article(a)
            obj=self.store.object(a['id'],{'type':'term','title':f'词语{i}','block':'b0'})
            self.store.update_object(obj['id'],{'saved':True,'note':f'笔记{i}'})
            key=a['id']+':'+a['version']+':'+obj['id']
            self.store.put('sessions',key,{'draft':f'草稿{i}','messages':[{'role':'user','content':f'问题{i}','evidence':[{'article_id':a['id']}]}]})
            with self.store.connect() as db:
                db.execute('CREATE TABLE IF NOT EXISTS capture_links(entry_id INTEGER PRIMARY KEY,object_id TEXT,payload TEXT NOT NULL)')
                db.execute('INSERT INTO capture_links VALUES(?,?,?)',(eid,obj['id'],json.dumps({'object_id':obj['id'],'saved':True})))
            original.append((a,obj,key))
        synchronize(self.store)
        self.assertEqual(len(self.store.summaries()),1)
        merged=self.store.article(self.store.summaries()[0]['id'])
        self.assertEqual({h['id'] for h in merged['capture_history']},{a['id'] for a,_,_ in original})
        for i,(old,obj,key) in enumerate(original):
            current=self.store.get('objects',obj['id'])
            self.assertEqual(current['note'],f'笔记{i}')
            new_key=current['article_id']+':'+current['version']+':'+current['id']
            saved=self.store.get('sessions',new_key)
            self.assertEqual(saved['draft'],f'草稿{i}')
            self.assertEqual(saved['messages'][0]['evidence'][0]['article_id'],old['id'])
            self.assertEqual(self.store.article(old['id'])['map'],{'retained':i})
            # A map job that began before grouping cannot revive an old card.
            old['map']={'finished_late':i};self.store.update_article(old)
            self.assertTrue(self.store.article(old['id']).get('superseded_by'))
        self.assertEqual(len(self.store.summaries()),1)
        self.assertEqual(synchronize(self.store),0)
