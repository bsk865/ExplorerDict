import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from app.native.service import NativeService


class NativeServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = NativeService(Path(self.temp.name))

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_notes_and_marks_keep_original_versions(self):
        article = self.service.import_text('原文', '第一段原文\n\n第二段')
        note = self.service.hub.save('notes', {'article_id': article['id'], 'content': '心得', 'block': 'b0', 'quote': '原文'})
        mark = self.service.annotations.save({'article_id': article['id'], 'version': article['version'], 'style': 'underline',
                                              'segments': [{'block': 'b0', 'start': 3, 'end': 5, 'quote': '原文'}]})
        loaded = self.service.article(article['id'])
        self.assertEqual(article['text'], loaded['article']['text'])
        self.assertEqual(note['id'], loaded['notes']['notes'][0]['id'])
        self.assertEqual(mark['id'], loaded['annotations']['items'][0]['id'])

    def test_removing_theme_does_not_remove_article_or_note(self):
        article = self.service.import_text('原文', '阅读材料')
        theme = self.service.hub.save('themes', {'title': '主题'})
        self.service.hub.assign(theme['id'], [article['id']], 'add')
        self.service.hub.save('notes', {'article_id': article['id'], 'content': '心得'})
        self.service.remove('themes', theme['id'])
        self.assertEqual(1, len(self.service.snapshot()['articles']))
        self.assertEqual(1, len(self.service.hub.notes(article['id'])['notes']))

    def test_storage_info_reports_saved_rows_without_changing_them(self):
        article = self.service.import_text('本地资料', '用于检查本地统计的原文。')
        key = article['id'] + ':' + article['version'] + ':article'
        session = {'messages':[{'role':'user', 'content':'已保存的问题'}], 'draft':'草稿', 'note':'心得'}
        self.service.store.put('sessions', key, session)
        info = self.service.storage_info()
        self.assertEqual(str(self.service.store.directory.resolve()), info['directory'])
        rows = {r['label']:r for r in info['items']}
        self.assertEqual(1, rows['文章与正式导图']['count'])
        self.assertEqual(1, rows['问答与笔记']['count'])
        self.assertGreater(rows['问答与笔记']['bytes'], 0)
        self.assertEqual(session, self.service.store.get('sessions', key))

    def test_conversation_removal_keeps_independent_and_legacy_notes(self):
        from app.content_management import inventory
        article = self.service.import_text('原文', '阅读材料')
        key = article['id'] + ':' + article['version'] + ':article'
        self.service.store.put('sessions', key, {'messages': [{'role': 'user', 'content': '问题'}],
                                                'draft': '待提问', 'note': '旧版笔记'})
        self.service.hub.save('notes', {'article_id': article['id'], 'content': '独立心得'})
        self.service.remove('conversations', key)
        saved = self.service.store.get('sessions', key)
        self.assertEqual([], saved['messages'])
        self.assertEqual('旧版笔记', saved['note'])
        self.assertEqual(1, len(self.service.hub.notes(article['id'])['notes']))
        self.assertEqual('旧版笔记', inventory(self.service.store)['legacy_notes'][0]['content'])


class NativeSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_utf16_selection_crosses_emoji_and_paragraph(self):
        from PySide6.QtGui import QTextCursor
        from app.native.reader import OriginalText, utf16_length
        widget = OriginalText()
        article = {'blocks': [{'id': 'b0', 'type': 'text', 'text': '甲😀乙'}, {'id': 'b1', 'type': 'text', 'text': '第二段'}]}
        widget.load_article(article, {})
        cursor = widget.textCursor()
        cursor.setPosition(1)
        cursor.setPosition(widget.ranges[1]['start'] + 2, QTextCursor.MoveMode.KeepAnchor)
        widget.setTextCursor(cursor)
        self.assertEqual([{'block': 'b0', 'start': 1, 'end': 3, 'quote': '😀乙'},
                          {'block': 'b1', 'start': 0, 'end': 2, 'quote': '第二'}], widget.selected_segments())
        self.assertEqual(2, utf16_length('😀'))
        widget.close()

    def test_library_draft_preserves_messages_and_failed_request_unlocks(self):
        from PySide6.QtCore import QObject
        from app.native.library import LibraryController
        with tempfile.TemporaryDirectory() as directory:
            service = NativeService(Path(directory))
            try:
                host = QObject()
                host.service = service
                host.run = lambda action, done: done(action())
                controller = LibraryController(host)
                article = service.import_text('太阳能资料', '太阳能电池将光能转换为电能。')
                controller.select([article['id']], True, 'question')
                messages = [{'role': 'user', 'content': '之前的问题'}]
                service.store.put('sessions', controller.key, {'messages': messages})
                controller.draft('太阳能')
                saved = service.store.get('sessions', controller.key)
                self.assertEqual(messages, saved['messages'])
                self.assertEqual('太阳能', saved['draft'])
                with patch.object(service.workspace, 'start', side_effect=ValueError('请先停止当前请求')):
                    controller.request('太阳能')
                self.assertFalse(controller.busy)
                self.assertEqual('请先停止当前请求', controller.message)
                controller.request('太阳能', preview=True)
                self.assertFalse(controller.busy)
                self.assertTrue(controller.hits)
                self.assertEqual(article['id'], controller.hits[0]['article_id'])
            finally:
                service.close()

    def test_library_ignores_outdated_selection_response(self):
        from PySide6.QtCore import QObject
        from app.native.library import LibraryController
        with tempfile.TemporaryDirectory() as directory:
            service = NativeService(Path(directory))
            try:
                pending = []
                host = QObject()
                host.service = service
                host.run = lambda action, done: pending.append((action, done))
                controller = LibraryController(host)
                first = service.import_text('第一篇', '第一篇资料')
                second = service.import_text('第二篇', '第二篇资料')
                controller.select([first['id']], True, 'question')
                controller.select([second['id']], True, 'question')
                action, done = pending[1]
                done(action())
                latest = controller.key
                action, done = pending[0]
                done(action())
                self.assertEqual(latest, controller.key)
                self.assertEqual([second['id']], controller.body['article_ids'])
            finally:
                service.close()


if __name__ == '__main__':
    unittest.main()
