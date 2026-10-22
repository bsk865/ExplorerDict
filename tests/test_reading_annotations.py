import tempfile
import unittest
from pathlib import Path

from app.workspace_store import WorkspaceStore
from app.reading_annotations import ReadingAnnotations, block_text
from app.study_hub import StudyHub
from app.content_management import inventory, remove


class ReadingAnnotationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = WorkspaceStore(Path(temp.name))
        self.marks = ReadingAnnotations(self.store)
        self.article = self.store.import_article('原文', [{'type': 'text', 'text': '重复😀文字，重复😀文字。'},
                                                       {'type': 'text', 'text': '下一段。'}])

    def body(self, **changes):
        return dict({'article_id': self.article['id'], 'version': self.article['version'], 'style': 'yellow',
                     'segments': [{'block': 'b0', 'start': 6, 'end': 11, 'quote': '重复😀文字'}]}, **changes)

    def test_repeated_quote_unicode_persistence_and_idempotence(self):
        item = self.marks.save(self.body())
        self.assertEqual(self.marks.save(self.body())['id'], item['id'])
        self.assertEqual(ReadingAnnotations(self.store).list(self.article['id'])['items'], [item])
        self.assertEqual(self.store.article(self.article['id'])['text'], self.article['text'])

    def test_invalid_anchor_version_or_style_does_not_save(self):
        for body in [self.body(version='old'), self.body(style='script'),
                     self.body(segments=[{'block': 'b0', 'start': 0, 'end': 4, 'quote': '错误'}]),
                     self.body(segments=[{'block': 'b0', 'start': True, 'end': 4, 'quote': '错误'}])]:
            with self.assertRaises(ValueError):
                self.marks.save(body)
        self.assertFalse(self.marks.list(self.article['id'])['items'])

    def test_edit_style_keeps_identity_anchor_and_rejects_missing_mark(self):
        mark = self.marks.save(self.body())
        updated = self.marks.save(dict(mark, style='pink'))
        self.assertEqual(updated['id'], mark['id'])
        self.assertEqual(updated['segments'], mark['segments'])
        self.assertEqual(len(self.marks.list(self.article['id'])['items']), 1)
        self.marks.delete({'article_id': self.article['id'], 'id': mark['id']})
        with self.assertRaises(ValueError):
            self.marks.save(dict(mark, style='green'))

    def test_multiblock_is_atomic_and_overlap_can_be_deleted_independently(self):
        segments = self.body()['segments'] + [{'block': 'b1', 'start': 0, 'end': 3, 'quote': '下一段'}]
        first = self.marks.save(self.body(segments=segments))
        second = self.marks.save(self.body(style='underline'))
        self.marks.delete({'article_id': self.article['id'], 'id': first['id']})
        self.assertEqual(self.marks.list(self.article['id'])['items'], [second])
        segments[-1]['quote'] = '伪造'
        with self.assertRaises(ValueError):
            self.marks.save(self.body(segments=segments))
        self.assertEqual(len(self.marks.list(self.article['id'])['items']), 1)

    def test_rich_text_preserves_inline_offsets(self):
        self.assertEqual(block_text({'html': '<p>甲<strong>乙 &amp; 丙</strong></p><p>丁</p>'}), '甲乙 & 丙丁')
        article = self.store.import_article('富文本', [{'type': 'text', 'text': '甲乙 & 丙丁',
                                'html': '<p>甲<strong>乙 &amp; 丙</strong></p><p>丁</p>'}])
        item = self.marks.save({'article_id': article['id'], 'version': article['version'], 'style': 'green',
                               'segments': [{'block': 'b0', 'start': 1, 'end': 6, 'quote': '乙 & 丙'}]})
        self.assertEqual(self.marks.list(article['id'])['items'], [item])
        self.assertEqual(block_text({'text': '甲\r\n乙\r丙'}), '甲\n乙\n丙')

    def test_management_and_article_deletion_cleanup(self):
        item = self.marks.save(self.body())
        self.assertEqual(inventory(self.store)['annotations'][0]['id'], item['id'])
        remove(self.store, 'annotations', item['id'])
        self.assertFalse(self.marks.list(self.article['id'])['items'])
        self.marks.save(self.body())
        self.store.delete_articles({self.article['id']})
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM reading_annotations').fetchone()[0], 0)

    def test_additive_theme_assignment_keeps_existing_members_and_other_themes(self):
        hub = StudyHub(self.store)
        other = self.store.import_article('另一篇', [{'type': 'text', 'text': '资料'}])
        t1 = hub.save('themes', {'title': '一'})['id']
        t2 = hub.save('themes', {'title': '二'})['id']
        hub.assign(t1, [other['id']])
        hub.assign(t2, [self.article['id']])
        hub.assign(t1, [self.article['id']], 'add')
        hub.assign(t1, [self.article['id']], 'add')
        self.assertEqual(len(hub.snapshot()['memberships']), 3)
        with self.assertRaises(ValueError):
            hub.assign(t1, ['deleted'], 'add')
        self.assertEqual(len(hub.snapshot()['memberships']), 3)


if __name__ == '__main__':
    unittest.main()
