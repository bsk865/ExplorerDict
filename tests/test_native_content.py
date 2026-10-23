import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from PySide6.QtWidgets import QApplication, QWidget, QPushButton, QPlainTextEdit
from app.native.content import ContentPage
from app.native.dialogs import NoteForm
from app.native.service import NativeService


class NativeContentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = NativeService(Path(self.temp.name))
        self.a = self.service.import_text('文章甲', '阅读材料甲')
        self.b = self.service.import_text('文章乙', '阅读材料乙')
        self.note = self.service.hub.save('notes', {'article_id':self.a['id'], 'title':'阅读记录'*20,
                                                  'content':'开头。' + '很长的正文。'*40 + '唯一检索词',
                                                  'quote':'阅读材料甲', 'block':'b0'})
        self.service.hub.save('notes', {'article_id':self.b['id'], 'title':'第二篇心得', 'content':'另一篇心得'})
        self.host = QWidget()
        self.host.service = self.service
        self.host.snapshot = self.service.snapshot()
        for name in ('edit_note', 'edit_task', 'open_article', 'delete', 'refresh', 'notice', 'resume_draft', 'import_material'):
            setattr(self.host, name, Mock())
        self.page = ContentPage(self.host, self.service.inventory(), 'notes')
        self.page.resize(600, 650)
        self.page.show()
        self.app.processEvents()

    def tearDown(self):
        self.page.close()
        self.host.close()
        self.service.close()
        self.temp.cleanup()

    def test_full_content_search_article_filter_and_refresh(self):
        self.page.search.setText('唯一检索词')
        self.assertEqual('1 项', self.page.count.text())
        self.page.article.setCurrentIndex(self.page.article.findData(self.b['id']))
        self.assertTrue(self.page.empty.isVisible())
        self.page.article.setCurrentIndex(self.page.article.findData(self.a['id']))
        refreshed = ContentPage(self.host, self.service.inventory(), 'notes')
        self.assertEqual('唯一检索词', refreshed.search.text())
        self.assertEqual(self.a['id'], refreshed.article.currentData())
        self.assertEqual('1 项', refreshed.count.text())
        refreshed.close()
        self.page.category.setCurrentIndex(self.page.category.findData('articles'))
        self.assertEqual('', self.page.search.text())
        self.assertTrue(self.page.filter_fields[2].isHidden())
        self.assertEqual('2 项', self.page.count.text())

    def test_inline_actions_and_compact_boundaries(self):
        record, row = next((r, w) for r, w in self.page.rows if r['id'] == self.note['id'])
        controls = row.findChildren(QPushButton)
        next(b for b in controls if b.text() == '编辑').click()
        self.host.edit_note.assert_called_once_with(record)
        next(b for b in controls if b.toolTip() == '查看原文').click()
        self.host.open_article.assert_called_once_with(self.a['id'], block='b0')
        next(b for b in controls if b.toolTip() == '删除').click()
        self.host.delete.assert_called_once_with('notes', self.note['id'], self.note['title'])
        for width in (1050, 600, 420):
            self.page.resize(width, 650)
            for _ in range(4):
                self.app.processEvents()
            self.assertEqual(width, self.page.width())
            self.assertEqual(0, self.page.scroll.horizontalScrollBar().maximum())
            for control in controls:
                self.assertLessEqual(control.mapTo(row, control.rect().bottomRight()).x(), row.width())

    def test_note_editor_keeps_source_quote_and_cancel(self):
        save = Mock()
        form = NoteForm(self.note, [self.a, self.b], save)
        self.assertFalse(form.fields['article_id'][1].isEnabled())
        self.assertEqual(self.a['id'], form.values()['article_id'])
        quote = next(w for w in form.findChildren(QPlainTextEdit) if w.accessibleName() == '引用原文')
        self.assertEqual('阅读材料甲', quote.toPlainText())
        self.assertTrue(quote.isReadOnly())
        form.fields['content'][1].setPlainText('未保存')
        form.reject()
        save.assert_not_called()
        self.assertEqual(self.note['content'], self.service.hub.notes(self.a['id'])['notes'][0]['content'])
        form.close()

    def test_note_editor_save_updates_original_record_and_keeps_quote(self):
        form = NoteForm(self.note, [self.a, self.b],
                        lambda body: self.service.hub.save('notes', {**self.note, **body}))
        form.fields['title'][1].setText('修改后的标题')
        form.fields['content'][1].setPlainText('修改后的理解')
        form.submit()
        self.assertEqual(1, form.result())
        notes = self.service.hub.notes(self.a['id'])['notes']
        self.assertEqual(1, len(notes))
        self.assertEqual(self.note['id'], notes[0]['id'])
        self.assertEqual('修改后的理解', notes[0]['content'])
        self.assertEqual('阅读材料甲', notes[0]['quote'])
        self.assertEqual('b0', notes[0]['block'])
        self.assertEqual('阅读材料甲', self.service.store.article(self.a['id'])['text'])
        form.close()

    def test_new_note_requires_article_and_draft_controls_are_preserved(self):
        from app.native.window import NativeWindow
        self.host.snapshot['articles'] = []
        NativeWindow.edit_note(self.host)
        self.host.notice.assert_called_once_with('先添加一篇资料，再记录笔记。')
        draft = {'id':'note-draft:one', 'draft_id':'one', 'article_id':self.a['id'], 'title':'未保存的心得'}
        page = ContentPage(self.host, {'native_drafts':[draft]}, 'native_drafts')
        row = page.rows[0][1]
        next(b for b in row.findChildren(QPushButton) if b.text() == '继续编辑').click()
        self.host.resume_draft.assert_called_once_with(draft)
        next(b for b in row.findChildren(QPushButton) if b.toolTip() == '删除').click()
        self.host.delete.assert_called_once_with('native_drafts', draft['id'], draft['title'])
        page.close()

    def test_legacy_edit_preserves_conversation_and_draft(self):
        key = self.a['id'] + ':' + self.a['version'] + ':article'
        old = {'note':'旧笔记', 'draft':'待提问', 'messages':[{'role':'user', 'content':'问题'}]}
        self.service.store.put('sessions', key, old)
        page = ContentPage(self.host, self.service.inventory(), 'legacy_notes')
        def submit(form):
            form.fields['content'][1].setPlainText('修改后的笔记')
            form.submit()
            return form.result()
        with patch('app.native.content.EditForm.exec', submit):
            page.edit(page.rows[0][0])
        saved = self.service.store.get('sessions', key)
        self.assertEqual('修改后的笔记', saved['note'])
        self.assertEqual(old['draft'], saved['draft'])
        self.assertEqual(old['messages'], saved['messages'])
        def reject_long(form):
            form.fields['content'][1].setPlainText('字' * 10001)
            form.submit()
            self.assertIn('10000', form.error.text())
            self.assertEqual(0, form.result())
            return form.result()
        with patch('app.native.content.EditForm.exec', reject_long):
            page.edit(page.rows[0][0])
        self.assertEqual(saved, self.service.store.get('sessions', key))
        page.remove(page.rows[0][0])
        self.assertEqual('session_note', self.host.delete.call_args.args[0])
        page.close()


if __name__ == '__main__':
    unittest.main()
