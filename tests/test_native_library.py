import unittest
from threading import RLock
from unittest.mock import Mock, patch

from PySide6.QtCore import QTimer, Qt, QUrl
from PySide6.QtWidgets import QApplication, QWidget, QDialogButtonBox, QPushButton, QScrollArea

from app.native.library import LibraryController, LibraryPage
from app.native.widgets import stylesheet


class NativeLibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        self.host.service = Mock()
        self.host.service.store.get.return_value = {}
        self.host.service.store.lock = RLock()
        self.host.notice = Mock()
        self.host.navigate = Mock()
        self.host.open_article = Mock()
        self.pending = []
        self.host.run = lambda work, done: self.pending.append((work, done))
        self.host.library = LibraryController(self.host)
        self.snapshot = {
            'preferences': {'palette': 'clay', 'mode': 'light'},
            'articles': [
                {'id': 'a', 'title': '原文标题' * 40, 'chars': 200},
                {'id': 'b', 'title': '第二篇', 'chars': 100},
                {'id': 'c', 'title': '等待补充原文', 'chars': 100, 'source_status': 'pending_source'},
            ],
            'study': {'themes': [{'id': 'theme', 'title': '阅读主题'}],
                      'memberships': [{'theme_id': 'theme', 'article_id': 'b'}]},
        }
        self.page = None
        self.policy = patch('app.native.library.conversation_budget.policy', return_value={'budget': 8000})
        self.policy.start()

    def tearDown(self):
        self.policy.stop()
        if self.page:
            self.page.close()
            self.page.deleteLater()
        self.host.library.timer.stop()
        self.host.deleteLater()
        self.app.processEvents()

    def show(self):
        self.page = LibraryPage(self.host, self.snapshot)
        self.page.setStyleSheet(stylesheet(self.snapshot['preferences']))
        self.page.resize(650, 470)
        self.page.show()
        self.app.processEvents()
        self.pending.pop()[1]({'key': 'scope-ab', 'session': {'messages': [], 'draft': '保留问题'}})

    def test_default_scope_and_compact_controls(self):
        self.show()
        self.assertEqual(['a', 'b', 'c'], self.host.library.body['article_ids'])
        self.assertFalse(self.page.articles.isVisible())
        self.assertFalse(self.page.restart.isVisible())
        self.assertFalse(self.page.cancel.isVisible())
        self.assertEqual(650, self.page.width())
        self.assertLessEqual(self.page.input.geometry().bottom(), self.page.send.parentWidget().height())
        self.assertEqual('保留问题', self.page.input.toPlainText())

    def test_cancel_scope_preserves_selection_and_draft(self):
        self.show()
        def cancel():
            self.page.articles.item(0).setCheckState(Qt.CheckState.Unchecked)
            self.app.activeModalWidget().reject()
        QTimer.singleShot(0, cancel)
        self.page.scope_button.click()
        self.assertEqual(['a', 'b', 'c'], self.host.library.body['article_ids'])
        self.assertEqual('保留问题', self.page.input.toPlainText())
        self.assertFalse(self.page.articles.isVisible())

    def test_theme_selection_applies_only_on_confirmation(self):
        self.show()
        def apply():
            dialog = self.app.activeModalWidget()
            next(b for b in dialog.findChildren(QPushButton) if b.property('theme_id') == 'theme').click()
            controls = dialog.findChild(QDialogButtonBox)
            next(b for b in controls.buttons() if controls.buttonRole(b) == QDialogButtonBox.ButtonRole.AcceptRole).click()
        QTimer.singleShot(0, apply)
        self.page.scope_button.click()
        self.assertEqual(['b'], self.host.library.body['article_ids'])
        self.assertFalse(self.page.articles.isVisible())

    def test_theme_buttons_wrap_without_overlap(self):
        self.snapshot['study']['themes'] = [{'id': str(i), 'title': '主题名称' * (i+1)} for i in range(12)]
        self.show()
        failures = []
        def inspect():
            dialog = self.app.activeModalWidget()
            try:
                dialog.resize(440, 480)
                self.app.processEvents()
                scroll = dialog.findChild(QScrollArea)
                controls = [b for b in scroll.findChildren(QPushButton) if b.property('theme_id') is not None]
                self.assertEqual(13, len(controls))
                self.assertEqual(0, scroll.horizontalScrollBar().maximum())
                for i, control in enumerate(controls):
                    self.assertLessEqual(control.geometry().right(), scroll.widget().width())
                    for other in controls[i+1:]:
                        self.assertFalse(control.geometry().intersects(other.geometry()))
                self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
            except Exception as exc:
                failures.append(exc)
            finally:
                dialog.reject()
        QTimer.singleShot(0, inspect)
        self.page.scope_button.click()
        self.assertEqual([], failures)

    def test_compare_is_one_request_not_a_persistent_mode(self):
        self.show()
        self.host.library.request = Mock()
        self.page.compare_action.trigger()
        self.host.library.request.assert_called_once_with('保留问题', mode='compare')
        self.assertEqual('question', self.host.library.body['mode'])
        self.page.input.clear()
        self.page.compare_action.trigger()
        self.assertEqual('这些文章有哪些共同点和差异？', self.host.library.request.call_args.args[0])

    def test_old_scope_response_cannot_replace_new_selection(self):
        controller = self.host.library
        controller.select(['a'], False, 'question')
        first = self.pending[-1][1]
        controller.select(['b'], True, 'question')
        second = self.pending[-1][1]
        second({'key': 'b', 'session': {'draft': '新问题'}})
        first({'key': 'a', 'session': {'draft': '旧问题'}})
        self.assertEqual('b', controller.key)
        self.assertEqual('新问题', controller.session['draft'])

    def test_scope_failure_is_recoverable_and_closed_session_cannot_send(self):
        controller = self.host.library
        controller.select(['a'], False, 'question')
        work, done = self.pending.pop()
        with patch('app.native.library.library_assistant.session', side_effect=OSError('internal detail')):
            done(work())
        self.assertIsNone(controller.key)
        self.assertNotIn('internal detail', controller.message)
        controller.select(['a'], False, 'question')
        self.pending.pop()[1]({'key': 'a', 'session': {'closed': True}})
        controller.request('问题')
        self.assertFalse(controller.busy)
        self.assertEqual([], self.pending)

    def test_each_answer_keeps_its_sources_and_note_action(self):
        self.show()
        hit = {'article_id': 'a', 'article_title': '第一篇', 'version': 'v1', 'quote': '引用内容', 'block': 'p1'}
        self.host.library.session['messages'] = [
            {'role': 'assistant', 'content': '第一条回答', 'evidence': [hit]},
            {'role': 'user', 'content': '继续'},
            {'role': 'assistant', 'content': '第二条回答', 'evidence': [dict(hit, article_title='第二篇')]},
        ]
        self.page.render()
        self.assertNotIn('第一篇', self.page.transcript.toPlainText())
        self.page.message_action(QUrl('library:sources:0'))
        self.assertIn('第一篇', self.page.transcript.toPlainText())
        self.assertNotIn('第二篇', self.page.transcript.toPlainText())
        with patch.object(self.page, 'answer_note') as note, patch.object(self.page, 'show_evidence') as preview:
            self.page.message_action(QUrl('library:note:0'))
            note.assert_called_once_with(0)
            self.page.message_action(QUrl('library:source:0:0'))
            preview.assert_called_once_with(hit)
            self.page.message_action(QUrl('https://example.com'))
            self.page.message_action(QUrl('library:source:0:-1'))
            self.assertEqual(1, preview.call_count)

    def test_search_preview_is_separate_and_keeps_question(self):
        self.show()
        self.host.library.hits = [{'article_id': 'a', 'article_title': '原文', 'quote': '检索结果'}]
        self.host.library.previewed.emit()
        self.app.processEvents()
        dialog = self.app.activeModalWidget()
        self.assertEqual('查找依据', dialog.windowTitle())
        self.assertFalse(self.page.evidence.isVisible())
        dialog.reject()
        self.assertEqual('保留问题', self.page.input.toPlainText())

    def test_inline_citations_resolve_within_each_answer(self):
        self.show()
        self.host.library.session['messages'] = [
            {'role': 'assistant', 'content': '第一条 [S1]，未知来源 [S9]。',
             'evidence': [{'citation': 'S1', 'article_title': '甲'}]},
            {'role': 'user', 'content': '用户文字 [S1]'},
            {'role': 'assistant', 'content': '第二条 [S1]',
             'evidence': [{'citation': 'S1', 'article_title': '乙'}]},
        ]
        self.page.render()
        document = self.page.transcript.document()
        first = document.find('[S1]')
        self.assertEqual('library:source:0:0', first.charFormat().anchorHref())
        user = document.find('[S1]', first)
        self.assertFalse(user.charFormat().isAnchor())
        second = document.find('[S1]', user)
        self.assertEqual('library:source:2:0', second.charFormat().anchorHref())
        self.assertFalse(document.find('[S9]').charFormat().isAnchor())

    def test_markdown_lists_do_not_continue_into_controls_or_later_messages(self):
        self.show()
        self.host.library.session['messages'] = [
            {'role':'assistant', 'content':'- 第一点 [S1]\n- 第二点',
             'evidence':[{'citation':'S1','article_title':'依据'}]},
            {'role':'user', 'content':'下一问'},
            {'role':'assistant', 'content':'普通回答'},
        ]
        self.page.render()
        self.page.message_action(QUrl('library:sources:0'))
        block = self.page.transcript.document().begin()
        points = []
        while block.isValid():
            text = block.text()
            if text.startswith(('第一点', '第二点')):
                self.assertIsNotNone(block.textList())
                points.append(text)
            else:
                self.assertIsNone(block.textList(), text)
            block = block.next()
        self.assertEqual(2, len(points))

    def open_reference(self, hit):
        errors = []
        def locate():
            dialog = self.app.activeModalWidget()
            try:
                control = next(b for b in dialog.findChildren(QPushButton) if b.text() in {'在原文中定位', '查看笔记'})
                control.click()
            except Exception as exc:
                errors.append(exc)
            finally:
                dialog.reject()
        QTimer.singleShot(0, locate)
        self.page.show_evidence(hit)
        self.assertEqual([], errors)

    def test_source_version_must_match_before_navigation(self):
        self.show()
        hit = {'article_id': 'a', 'version': 'v1', 'block': 'p1', 'quote': '原始引用'}
        self.host.service.store.article.return_value = {'id': 'a', 'version': 'v2'}
        self.open_reference(hit)
        self.host.open_article.assert_not_called()
        self.host.notice.assert_called_once_with('原文版本已变化，请重新检索核对。')
        self.host.service.store.article.return_value['version'] = 'v1'
        self.open_reference(hit)
        self.host.open_article.assert_called_once_with('a', block='p1')

    def test_personal_reference_opens_note_and_retains_deleted_excerpt(self):
        self.show()
        hit = {'article_id': 'a', 'source': 'personal_note', 'note_id': 'note', 'quote': '引用的笔记'}
        self.host.service.store.article.return_value = {'id': 'a', 'version': 'v2'}
        self.host.service.hub.notes.return_value = {'notes': []}
        self.open_reference(hit)
        self.host.open_article.assert_not_called()
        self.host.service.hub.notes.return_value = {'notes': [{'id': 'note'}]}
        self.open_reference(hit)
        self.host.open_article.assert_called_once_with('a', note_id='note')


if __name__ == '__main__':
    unittest.main()
