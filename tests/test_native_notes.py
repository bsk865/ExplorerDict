import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication
from app.native.service import NativeService
from app.native.notes import NotesController


class QueuedTasks:
    def __init__(self):
        self.queue = []

    def submit(self, action, done, error):
        self.queue.append((action, done, error))

    def complete(self):
        action, done, error = self.queue.pop(0)
        try:
            value = action()
        except Exception as exc:
            error(str(exc))
        else:
            done(value)


class NativeNotesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = NativeService(Path(self.temp.name))
        self.article = self.service.import_text('阅读笔记测试', '阅读原文，保留引用。')
        self.host = QObject()
        self.host.service = self.service
        self.host.tasks = QueuedTasks()
        self.controller = NotesController(self.host, self.article, self.service.hub.notes(self.article['id']))

    def tearDown(self):
        for draft in self.controller.drafts:
            draft.timer.stop()
        self.service.close()
        self.temp.cleanup()

    def test_edit_during_save_serializes_without_duplicate(self):
        draft = self.controller.active
        draft.edit('标题', '第一版')
        draft.flush()
        draft.edit('标题', '第二版')
        draft.flush()
        self.assertEqual(1, len(self.host.tasks.queue))
        self.host.tasks.complete()
        self.assertEqual(1, len(self.host.tasks.queue))
        self.host.tasks.complete()
        notes = self.service.hub.notes(self.article['id'])['notes']
        self.assertEqual(['第二版'], [n['content'] for n in notes])
        self.assertFalse(draft.dirty)
        self.assertEqual([], self.service.store.get('settings', self.controller.key))

    def test_switching_before_save_finishes_keeps_both_notes_and_anchor(self):
        block = self.article['blocks'][0]
        self.controller.select({'article_id': self.article['id'], 'block': block['id'], 'quote': '阅读原文'})
        first = self.controller.active
        first.edit('第一条', '第一篇心得')
        self.controller.select()
        second = self.controller.active
        second.edit('第二条', '第二篇心得')
        second.flush()
        self.host.tasks.complete()
        self.assertIs(second, self.controller.active)
        self.host.tasks.complete()
        notes = self.service.hub.notes(self.article['id'])['notes']
        self.assertEqual(2, len(notes))
        self.assertEqual('阅读原文', next(n for n in notes if n['title'] == '第一条')['quote'])
        self.assertEqual('第二篇心得', second.body['content'])

    def test_failed_save_recovers_draft_and_retry(self):
        draft = self.controller.active
        draft.edit('恢复', '不能丢失的内容')
        draft.flush()
        with patch.object(self.service.hub, 'save', side_effect=OSError('test failure')):
            self.host.tasks.complete()
        self.assertFalse(draft.busy)
        self.assertTrue(draft.dirty)
        recovered = NotesController(self.host, self.article, self.service.hub.notes(self.article['id']))
        self.assertEqual('不能丢失的内容', recovered.active.body['content'])
        recovered.flush()
        self.host.tasks.complete()
        self.assertEqual(1, len(self.service.hub.notes(self.article['id'])['notes']))

    def test_delete_and_discard_do_not_change_article(self):
        original = self.service.store.article(self.article['id'])
        draft = self.controller.active
        draft.edit('测试', '笔记内容')
        draft.flush()
        self.host.tasks.complete()
        self.controller.remove()
        self.host.tasks.complete()
        self.assertFalse(self.service.hub.notes(self.article['id'])['notes'])
        self.controller.active.edit('只写标题', '')
        self.controller.remove()
        self.assertEqual([], self.service.store.get('settings', self.controller.key))
        self.assertEqual(original, self.service.store.article(self.article['id']))

    def test_long_draft_is_preserved_without_failed_save_loop(self):
        draft = self.controller.active
        draft.edit('长笔记', '文' * 20001)
        draft.flush()
        self.assertFalse(self.host.tasks.queue)
        self.assertIn('20000', draft.message)
        self.assertEqual(20001, len(self.service.store.get('settings', self.controller.key)[0]['content']))

    def test_delete_completion_does_not_replace_another_open_draft(self):
        self.controller.active.edit('第一条', '已保存内容')
        self.controller.flush()
        self.host.tasks.complete()
        self.controller.remove()
        self.controller.select()
        other = self.controller.active
        other.edit('第二条', '仍在编辑')
        self.host.tasks.complete()
        self.assertIs(other, self.controller.active)
        self.assertEqual('仍在编辑', other.body['content'])

    def test_management_lists_and_discards_only_selected_draft(self):
        first = self.controller.active
        first.edit('第一份草稿', '')
        self.controller.select()
        self.controller.active.edit('第二份草稿', '')
        self.service.store.put('settings', 'native-advisor-draft', {'request':'规划需求'})
        drafts = self.service.inventory()['native_drafts']
        self.assertEqual(3, len(drafts))
        key = 'note-draft:'+first.body['draft_id']
        paused = self.controller.pause_matching(lambda body: body['draft_id'] == first.body['draft_id'])
        self.service.remove('native_drafts', key)
        self.controller.finish_removal(paused, True)
        remaining = self.service.inventory()['native_drafts']
        self.assertEqual({'第二份草稿', '小探规划草稿'}, {d['title'] for d in remaining})

    def test_queued_save_cannot_recreate_deleted_draft(self):
        draft = self.controller.active
        draft.edit('不应恢复', '等待保存')
        draft.flush()
        paused = self.controller.pause_matching(lambda body: body['draft_id'] == draft.body['draft_id'])
        self.service.remove('native_drafts', 'note-draft:'+draft.body['draft_id'])
        self.controller.finish_removal(paused, True)
        self.host.tasks.complete()
        self.assertFalse(self.service.hub.notes(self.article['id'])['notes'])
        self.assertFalse(self.service.inventory()['native_drafts'])

    def test_late_save_callback_cannot_restore_deleted_note(self):
        draft = self.controller.active
        draft.edit('已有笔记', '第一版')
        draft.flush()
        self.host.tasks.complete()
        draft.edit('已有笔记', '后台保存的第二版')
        draft.flush()
        action, done, failed = self.host.tasks.queue.pop(0)
        result = action()
        paused = self.controller.pause_matching(lambda body: body.get('id') == draft.body['id'])
        self.service.remove('notes', draft.body['id'])
        self.controller.finish_removal(paused, True)
        done(result)
        self.assertFalse(self.service.hub.notes(self.article['id'])['notes'])
        self.assertFalse(self.service.inventory()['native_drafts'])

    def test_failed_delete_restores_autosave(self):
        draft = self.controller.active
        draft.edit('保留', '删除没有完成时保留内容')
        paused = self.controller.pause_matching(lambda body: True)
        self.controller.finish_removal(paused, False)
        draft.flush()
        self.host.tasks.complete()
        self.assertEqual('删除没有完成时保留内容', self.service.hub.notes(self.article['id'])['notes'][0]['content'])

    def test_removing_article_also_removes_its_drafts(self):
        self.controller.active.edit('仅有标题', '')
        paused = self.controller.pause_matching(lambda body: body.get('article_id') == self.article['id'])
        self.service.remove('articles', self.article['id'])
        self.controller.finish_removal(paused, True)
        self.assertFalse(self.service.inventory()['native_drafts'])


if __name__ == '__main__':
    unittest.main()
