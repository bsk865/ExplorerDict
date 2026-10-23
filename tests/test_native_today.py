import unittest
from datetime import date
from unittest.mock import Mock
from PySide6.QtWidgets import QApplication, QWidget, QLabel
from app.native.study import StudyPage
from app.native.widgets import stylesheet


class NativeTodayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        for name in ('show_advisor', 'edit_task', 'choose_city', 'open_article', 'refresh', 'notice'):
            setattr(self.host, name, Mock())
        self.host.tasks = Mock()
        self.host.service = Mock()
        self.snapshot = {'preferences':{'palette':'clay', 'mode':'light'},
                         'study':{'tasks':[]}, 'articles':[], 'activity':{'readings':[]}}
        self.page = None

    def tearDown(self):
        if self.page:
            self.page.timer.stop()
            self.page.close()
            self.page.deleteLater()
        self.host.deleteLater()

    def show(self):
        self.page = StudyPage(self.host, self.snapshot, 'today')
        self.page.setStyleSheet(stylesheet(self.snapshot['preferences']))
        self.page.resize(660, 520)
        self.page.show()
        self.app.processEvents()

    def test_empty_today_and_unavailable_weather(self):
        self.show()
        self.assertEqual('暂无安排', self.page.today_count.text())
        self.assertTrue(any(w.text() == '从一件小事开始' for w in self.page.findChildren(QLabel)))
        self.page.show_weather({'status':'error', 'city':{'name':'测试城市'}})
        self.assertEqual('暂时无法获取天气', self.page.today_weather.condition.text())
        self.assertEqual('更换城市', self.page.today_weather.change.text())
        self.page.today_weather.change.click()
        self.host.choose_city.assert_called_once()

    def test_long_task_and_article_titles_do_not_widen_page(self):
        self.snapshot['study']['tasks'] = [{'id':'task', 'title':'很长的阅读任务名称'*20,
            'date':date.today().isoformat(), 'time':'14:00', 'minutes':30, 'status':'todo',
            'article_ids':['article'], 'goal':'理解原文'}]
        self.snapshot['articles'] = [{'id':'article', 'title':'很长的文章标题'*24}]
        self.snapshot['activity']['readings'] = [{'article_id':'article','completed':False,'last_opened':'2026-10-23'}]
        self.show()
        self.assertEqual(0, self.page.today_scroll.horizontalScrollBar().maximum())
        self.assertEqual(660, self.page.width())

    def test_failed_completion_restores_checkbox_and_progress(self):
        self.snapshot['study']['tasks'] = [{'id':'task', 'title':'阅读',
            'date':date.today().isoformat(), 'time':'14:00', 'minutes':30, 'status':'todo', 'article_ids':[]}]
        self.host.tasks.submit.side_effect = lambda action, done, failed: failed('请稍后重试')
        self.show()
        self.page.today_checks[0].click()
        self.assertFalse(self.page.today_checks[0].isChecked())
        self.assertTrue(self.page.today_checks[0].isEnabled())
        self.assertEqual(0, self.page.today_progress.value())
        self.host.notice.assert_called_once_with('请稍后重试')


if __name__ == '__main__':
    unittest.main()
