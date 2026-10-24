import tempfile
import threading
import unittest
from pathlib import Path
from app.workspace_store import WorkspaceStore
from app.study_hub import StudyHub
from app.study_activity import StudyActivity
from app.content_management import inventory, remove


class ContentManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store=WorkspaceStore(Path(self.temp.name))
        self.hub=StudyHub(self.store)
        self.article=self.store.import_article('Paper',[{'type':'text','text':'Material for notes and exploration.'}])
        self.obj=self.store.object(self.article['id'],{'type':'term','title':'Material','block':'b0'})
        self.key=self.article['id']+':'+self.article['version']+':'+self.obj['id']

    def test_object_delete_keeps_source_and_other_object(self):
        other=self.store.object(self.article['id'],{'type':'term','title':'notes','block':'b0'})
        self.store.put('sessions',self.key,{'messages':[{'role':'user','content':'Explain'}]})
        remove(self.store,'objects',self.obj['id'])
        self.assertIsNone(self.store.get('objects',self.obj['id']))
        self.assertIsNone(self.store.get('sessions',self.key))
        self.assertIsNotNone(self.store.get('objects',other['id']))
        self.assertEqual(self.store.article(self.article['id'])['text'],'Material for notes and exploration.')

    def test_conversation_and_note_have_separate_lifecycles(self):
        self.store.put('sessions',self.key,{'messages':[{'role':'user','content':'Question'}],'note':'Keep note','archives':[{'handoff':'Old'}]})
        remove(self.store,'conversations',self.key)
        self.assertEqual(self.store.get('sessions',self.key)['note'],'Keep note')
        self.assertFalse(inventory(self.store)['conversations'])
        self.assertTrue(inventory(self.store)['legacy_notes'])
        remove(self.store,'session_note',self.key)
        self.assertFalse(inventory(self.store)['legacy_notes'])

    def test_batch_delete_keeps_tasks(self):
        batch=self.hub.save_plan([{'title':'Read','date':'2030-01-01','time':'14:00'}],'Goal',threading.Event())
        remove(self.store,'batches',batch['id'])
        self.assertFalse(inventory(self.store)['batches'])
        self.assertEqual(len(self.hub.snapshot()['tasks']),1)

    def test_theme_delete_keeps_notes_and_article(self):
        theme=self.hub.save('themes',{'title':'Theme'})
        self.hub.assign(theme['id'],[self.article['id']])
        self.hub.save('notes',{'article_id':self.article['id'],'content':'Thought'})
        remove(self.store,'themes',theme['id'])
        data=inventory(self.store)
        self.assertFalse(data['memberships'])
        self.assertEqual(len(data['notes']),1)
        self.assertEqual(len(data['articles']),1)

    def test_clear_weather_and_reading_preserves_material(self):
        self.store.put('settings','study-weather-city',{'name':'City'})
        self.store.put('settings','study-weather-cache',{'temperature':20})
        StudyActivity(self.store).record(self.article['id'],True)
        remove(self.store,'weather','city')
        remove(self.store,'reading',self.article['id'])
        data=inventory(self.store)
        self.assertFalse(data['weather'])
        self.assertFalse(data['reading'])
        self.assertIsNone(self.store.get('settings','study-weather-cache'))
        self.assertEqual(len(data['articles']),1)

    def test_unknown_kind_does_not_modify_content(self):
        with self.assertRaises(ValueError):
            remove(self.store,'articles',self.article['id'])
        self.assertIsNotNone(self.store.article(self.article['id']))

    def test_removing_map_clears_resumable_stages_and_preserves_other_articles(self):
        other=self.store.import_article('Other',[{'type':'text','text':'Unrelated material.'}])
        with self.store.connect() as db:
            for table in ('map_parts','map_stages'):
                db.execute(f'CREATE TABLE {table}(id TEXT PRIMARY KEY,version TEXT,payload TEXT)')
                db.executemany(f'INSERT INTO {table} VALUES(?,?,?)',
                               [('removed',self.article['version'],'{}'),('kept',other['version'],'{}')])
        remove(self.store,'maps',self.article['id'])
        with self.store.connect() as db:
            for table in ('map_parts','map_stages'):
                self.assertEqual([r[0] for r in db.execute(f'SELECT id FROM {table}')],['kept'])
        self.assertEqual(self.store.article(self.article['id'])['text'],'Material for notes and exploration.')

    def test_synced_object_does_not_return_after_deletion(self):
        from contextlib import closing
        from app.db import Database
        from app.workspace_sync import synchronize
        with closing(Database(self.store.directory/'explorer_dict.sqlite3')):
            pass
        self.store.update_object(self.obj['id'],{'saved':True})
        synchronize(self.store)
        remove(self.store,'objects',self.obj['id'])
        synchronize(self.store)
        self.assertIsNone(self.store.get('objects',self.obj['id']))
        with closing(Database(self.store.directory/'explorer_dict.sqlite3')) as db:
            self.assertFalse(db.query('SELECT id FROM entries'))
