import tempfile
import unittest
from pathlib import Path
from app.native.service import NativeService
from app.native.materials import reading_markdown


class NativeMaterialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = NativeService(Path(self.temp.name))

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_insert_creates_version_and_keeps_old_marks_and_citations(self):
        old = self.service.import_text('阅读', '第一段\n\n第二段')
        obj = self.service.store.object(old['id'], {'type': 'term', 'block': 'b1', 'title': '第二段'})
        self.service.store.update_object(obj['id'], {'saved': True, 'note': '收藏心得'})
        mark = self.service.annotations.save({'article_id': old['id'], 'version': old['version'], 'style': 'yellow',
            'segments': [{'block': 'b1', 'start': 0, 'end': 3, 'quote': '第二段'}]})
        key = old['id']+':'+old['version']+':'+obj['id']
        self.service.store.put('sessions', key, {'messages': [{'role': 'assistant', 'content': '旧回答',
            'evidence': [{'article_id': old['id'], 'block': 'b1', 'quote': '第二段'}]}]})
        new = self.service.append(old['id'], [{'type': 'text', 'text': '新增段落'}], after=0)
        self.assertNotEqual(new['version'], old['version'])
        self.assertEqual(old['id'], new['previous_id'])
        self.assertEqual('b2', new['objects'][0]['block'])
        self.assertTrue(new['objects'][0]['saved'])
        self.assertEqual('收藏心得', new['objects'][0]['note'])
        self.assertEqual(mark['id'], self.service.annotations.list(old['id'])['items'][0]['id'])
        self.assertEqual([], self.service.annotations.list(new['id'])['items'])
        self.assertEqual('b1', self.service.store.get('sessions', key)['messages'][0]['evidence'][0]['block'])
        self.assertEqual({old['id'], new['id']}, self.service.store.related_versions(new['id']))

    def test_rename_keeps_version_and_export_contains_independent_notes(self):
        article = self.service.import_text('原来的标题', '原文')
        self.service.hub.save('notes', {'article_id': article['id'], 'content': '随文心得'})
        renamed = self.service.rename(article['id'], '新的标题')
        self.assertEqual(article['version'], renamed['version'])
        exported = reading_markdown(self.service, renamed)
        self.assertIn('# 新的标题', exported)
        self.assertIn('随文心得', exported)
        self.assertIn('原文', exported)


if __name__ == '__main__':
    unittest.main()
