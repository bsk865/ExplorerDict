import unittest
from datetime import date, timedelta
from unittest.mock import Mock
from PySide6.QtWidgets import QApplication, QWidget, QPushButton
from app.native.study import StudyPage


class NativeProgressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_scope_counts_theme_reading_and_note_navigation(self):
        today = date.today()
        host = QWidget()
        host.open_article = Mock()
        snapshot = {'preferences': {'palette': 'clay', 'mode': 'light'},
                    'study': {'stats': {'articles': 2, 'notes': 1, 'completed_tasks': 2},
                              'tasks': [{'date': today.isoformat(), 'status': 'done'},
                                        {'date': today.isoformat(), 'status': 'todo'},
                                        {'date': (today-timedelta(days=8)).isoformat(), 'status': 'done'}],
                              'notes': [{'id': 'n', 'article_id': 'a', 'title': '心得', 'content': '阅读心得'*80}],
                              'themes': [{'id':'t', 'title':'长主题名称'*20}],
                              'memberships': [{'theme_id':'t', 'article_id':'a'}, {'theme_id':'t', 'article_id':'b'}]},
                    'activity': {'readings':[{'article_id':'a', 'completed':True}],
                                 'days':[{'date':(today-timedelta(days=i)).isoformat(), 'articles_opened':i} for i in reversed(range(7))]}}
        page = StudyPage(host, snapshot, 'progress')
        page.resize(650, 600)
        page.show()
        self.app.processEvents()
        try:
            self.assertTrue(page.progress_scopes['week'].isChecked())
            self.assertEqual('1 / 2 项', page.progress_count.text())
            self.assertEqual('50%', page.progress_percent.text())
            page.progress_scopes['all'].click()
            self.assertEqual('2 / 3 项', page.progress_count.text())
            self.assertEqual('67%', page.progress_percent.text())
            self.assertEqual('all', host.progress_scope)
            self.assertEqual(1, page.topic_progress['t'].value())
            self.assertEqual(2, page.topic_progress['t'].maximum())
            self.assertEqual(0, page.progress_scroll.horizontalScrollBar().maximum())
            next(b for b in page.findChildren(QPushButton) if b.accessibleName() == '心得').click()
            host.open_article.assert_called_once_with('a', note_id='n')
        finally:
            page.close()
            host.close()


if __name__ == '__main__':
    unittest.main()
