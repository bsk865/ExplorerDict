import tempfile
import unittest
import threading
from pathlib import Path

from app.workspace_store import WorkspaceStore
from app.study_hub import StudyHub


class StudyHubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = WorkspaceStore(Path(self.temp.name))
        self.hub = StudyHub(self.store)
        self.article = self.store.import_article('Research', [{'type': 'text', 'text': 'A grounded observation.'}])

    def test_multiple_themes_do_not_copy_article(self):
        for title in ('Control', 'Reading'):
            theme = self.hub.save('themes', {'title': title})
            self.hub.assign(theme['id'], [self.article['id'], self.article['id']])
        self.assertEqual(len(self.store.summaries()), 1)
        self.assertEqual(len(self.hub.snapshot()['memberships']), 2)

    def test_note_keeps_anchor_and_version_on_edit(self):
        note = self.hub.save('notes', {'article_id': self.article['id'], 'content': 'My reflection',
                                     'block': 'b0', 'quote': 'grounded'})
        updated = self.hub.save('notes', {'id': note['id'], 'content': 'Revised reflection', 'quote': 'invented'})
        self.assertEqual(updated['quote'], 'grounded')
        self.assertEqual(updated['version'], self.article['version'])
        with self.assertRaises(ValueError):
            self.hub.save('notes', {'article_id': self.article['id'], 'content': 'Invalid', 'block': 'b0', 'quote': 'invented'})

    def test_deleted_article_cleans_notes_and_links_but_preserves_plan(self):
        theme = self.hub.save('themes', {'title': 'Topic'})
        self.hub.assign(theme['id'], [self.article['id']])
        self.hub.save('notes', {'article_id': self.article['id'], 'content': 'Note'})
        task = self.hub.save('tasks', {'title': 'Review', 'article_ids': [self.article['id']]})
        self.store.delete_articles({self.article['id']})
        state = self.hub.snapshot()
        self.assertFalse(state['notes'])
        self.assertFalse(state['memberships'])
        self.assertEqual(state['tasks'][0]['id'], task['id'])
        self.assertFalse(state['tasks'][0]['article_ids'])

    def test_completion_is_reversible_and_persistent(self):
        task = self.hub.save('tasks', {'title': 'Read', 'date': '2026-10-23', 'time': '19:00', 'minutes': 60})
        done = self.hub.save('tasks', {'id': task['id'], 'status': 'done'})
        self.assertTrue(done['completed_at'])
        reopened = StudyHub(WorkspaceStore(Path(self.temp.name)))
        self.assertEqual(reopened.snapshot()['stats']['completed_tasks'], 1)
        reopened.save('tasks', {'id': task['id'], 'status': 'todo'})
        self.assertEqual(reopened.snapshot()['stats']['completed_tasks'], 0)

    def test_invalid_schedule_rejected(self):
        for changes in ({'date': '2026-02-30'}, {'time': '19:00'}, {'minutes': -1}, {'status': 'unknown'}):
            with self.assertRaises(ValueError):
                self.hub.save('tasks', {'title': 'Task', **changes})

    def test_plan_conflict_rolls_back_entire_batch(self):
        self.hub.save('tasks', {'title':'Existing', 'date':'2026-10-23', 'time':'19:00', 'minutes':60})
        with self.assertRaises(ValueError):
            self.hub.save_plan([
                {'title':'Valid first', 'date':'2026-10-23', 'time':'17:00'},
                {'title':'Conflict', 'date':'2026-10-23', 'time':'19:30'},
            ], 'Study', threading.Event())
        self.assertEqual([t['title'] for t in self.hub.snapshot()['tasks']], ['Existing'])

    def test_plan_undo_preserves_edited_tasks(self):
        batch = self.hub.save_plan([{'title':'Read', 'date':'2026-10-23', 'time':'19:00'}], 'Study', threading.Event())
        self.hub.save('tasks', {'id':batch['tasks'][0]['id'], 'status':'done'})
        with self.assertRaises(ValueError):
            self.hub.undo_plan(batch['id'])
        self.assertEqual(self.hub.snapshot()['stats']['completed_tasks'], 1)

    def test_cancelled_plan_has_no_partial_writes(self):
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(ValueError):
            self.hub.save_plan([{'title':'Read', 'date':'2026-10-23', 'time':'19:00'}], 'Study', cancel)
        self.assertFalse(self.hub.snapshot()['tasks'])

    def test_retried_note_draft_does_not_duplicate(self):
        body = {'article_id':self.article['id'], 'content':'Reflection', 'draft_id':'same-draft'}
        first = self.hub.save('notes', body)
        second = self.hub.save('notes', body)
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(len(self.hub.notes(self.article['id'])['notes']), 1)

    def test_manual_schedule_conflict_and_adjacent_slot(self):
        self.hub.save('tasks', {'title':'First','date':'2026-10-23','time':'19:00','minutes':30})
        with self.assertRaises(ValueError):
            self.hub.save('tasks', {'title':'Overlap','date':'2026-10-23','time':'19:15','minutes':30})
        self.hub.save('tasks', {'title':'Adjacent','date':'2026-10-23','time':'19:30','minutes':30})
        self.assertEqual(len(self.hub.snapshot()['tasks']),2)

    def test_split_keeps_total_time_and_links(self):
        task=self.hub.save('tasks', {'title':'Read','date':'2026-10-23','time':'19:00','minutes':60,'article_ids':[self.article['id']]})
        parts=self.hub.split_task(task['id'],25)['tasks']
        self.assertEqual(sum(t['minutes'] for t in parts),60)
        self.assertEqual(parts[1]['time'],'19:25')
        self.assertEqual(parts[1]['article_ids'],[self.article['id']])

    def test_availability_limits_daily_total(self):
        from app.study_advisor import availability,validate_availability
        limits=availability({'start_date':'2026-10-23','end_date':'2026-10-24','start_time':'19:00','end_time':'21:00','daily_minutes':60})
        with self.assertRaises(ValueError):
            validate_availability([{'date':'2026-10-23','time':'19:00','minutes':40},{'date':'2026-10-23','time':'20:00','minutes':40}],limits)
        with self.assertRaises(ValueError):
            validate_availability([{'date':'2026-10-23','time':'18:00','minutes':30}],limits)

    def test_confirmed_answer_note_keeps_provenance(self):
        self.store.put('sessions','library:test',{'article_ids':[self.article['id']], 'messages':[{'role':'assistant','content':'Answer [S1]','evidence':[{'citation':'S1','article_id':self.article['id']}]}]})
        body={'session_key':'library:test','message_index':0,'source_content':'Answer [S1]','article_id':self.article['id'],'content':'My checked version'}
        note=self.hub.save_answer_note(body)
        self.assertEqual(note['origin'],'assistant_confirmed')
        self.assertEqual(note['evidence'][0]['citation'],'S1')
        with self.assertRaises(ValueError):
            self.hub.save_answer_note(dict(body,source_content='Old answer'))


if __name__ == '__main__':
    unittest.main()
