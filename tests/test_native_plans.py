import unittest
from datetime import date
from unittest.mock import Mock
from PySide6.QtWidgets import QApplication, QWidget, QPushButton, QCheckBox, QLabel
from app.native.plans import PlanList, WeekPlans
from app.native.dialogs import TaskForm


class NativePlansTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        for name in ('edit_task', 'open_article', 'delete', 'refresh', 'notice', 'tasks', 'service'):
            setattr(self.host, name, Mock())
        self.tasks = [{'id':'a', 'title':'阅读任务'*20, 'goal':'整理心得', 'date':'2026-10-23',
                       'time':'09:00', 'minutes':25, 'status':'todo', 'article_ids':['article']},
                      {'id':'b', 'title':'复习', 'date':'2026-10-24', 'time':'10:00', 'minutes':10, 'status':'done'}]
        snapshot = {'preferences':{'palette':'clay','mode':'light'}, 'articles':[{'id':'article','title':'原文'}]}
        self.page = PlanList(self.host, self.tasks, snapshot)
        self.page.resize(600, 500)
        self.page.show()
        self.app.processEvents()

    def tearDown(self):
        self.page.close()
        self.host.close()

    def test_filters_hide_empty_groups_and_preserve_selection(self):
        self.page.status.setCurrentIndex(2)
        self.assertFalse(self.page.rows[0][2].isVisible())
        self.assertFalse(self.page.groups[0][1].isVisible())
        self.assertTrue(self.page.rows[1][2].isVisible())
        self.page.search.setText('没有这项')
        self.assertTrue(self.page.empty.isVisible())
        self.assertEqual({'query':'没有这项','status':'done'}, self.host.plan_filters)
        self.page.search.clear()
        self.page.status.setCurrentIndex(0)
        self.assertTrue(all(row.isVisible() for _, _, row in self.page.rows))

    def test_actions_and_failed_completion_keep_original_state(self):
        row = self.page.rows[0][2]
        next(b for b in row.findChildren(QPushButton) if b.toolTip() == '阅读 原文').click()
        self.host.open_article.assert_called_once_with('article')
        next(b for b in row.findChildren(QPushButton) if b.toolTip() == '删除计划').click()
        self.host.delete.assert_called_once_with('tasks', 'a', self.tasks[0]['title'])
        self.host.tasks.submit.side_effect = lambda work, done, failed: failed('保存未完成')
        check = row.findChild(QCheckBox)
        check.click()
        self.assertFalse(check.isChecked())
        self.assertTrue(check.isEnabled())
        self.host.notice.assert_called_once_with('保存未完成')
        self.assertEqual(0, self.page.scroll.horizontalScrollBar().maximum())
        for control in row.findChildren(QPushButton):
            self.assertLessEqual(control.mapTo(row, control.rect().bottomRight()).x(), row.width())

    def test_editor_delay_is_saved_only_on_submit(self):
        save = Mock()
        dialog = TaskForm(self.tasks[0], [], save, Mock(), Mock())
        dialog.delay()
        dialog.delay()
        self.assertEqual('2026-10-25', dialog.values()['date'])
        save.assert_not_called()
        dialog.reject()
        save.assert_not_called()
        self.assertEqual('2026-10-23', self.tasks[0]['date'])
        dialog.fields['date'][1].setText('invalid')
        dialog.delay()
        self.assertTrue(dialog.error.text())
        dialog.fields['date'][1].setText('2026-12-31')
        dialog.delay()
        dialog.submit()
        self.assertEqual('2027-01-01', save.call_args.args[0]['date'])
        dialog.close()

    def test_editor_delete_cancel_and_split_preserve_unsaved_values(self):
        save, remove, split = Mock(), Mock(return_value=False), Mock()
        dialog = TaskForm(self.tasks[0], [], save, remove, split)
        dialog.show()
        delete = next(b for b in dialog.findChildren(QPushButton) if b.toolTip() == '删除计划')
        delete.click()
        self.assertTrue(dialog.isVisible())
        dialog.fields['title'][1].setText('未保存的修改')
        next(b for b in dialog.findChildren(QPushButton) if b.text() == '拆分').click()
        self.assertFalse(dialog.isVisible())
        split.assert_called_once()
        save.assert_not_called()
        dialog.show()
        remove.return_value = True
        delete.click()
        self.assertFalse(dialog.isVisible())
        dialog.close()
        for task in ({}, self.tasks[1], {**self.tasks[0], 'minutes':5}):
            form = TaskForm(task, [], save, remove, split)
            self.assertFalse(any(b.text() == '拆分' for b in form.findChildren(QPushButton)))
            form.close()

    def test_week_reflows_and_orders_tasks_without_horizontal_scroll(self):
        tasks = [*self.tasks, {**self.tasks[0], 'id':'early', 'time':'08:00'}]
        week = WeekPlans(self.host, tasks, date(2026, 10, 19), {'palette':'clay', 'mode':'light'})
        week.show()
        for width, expected in ((1250, 7), (850, 3), (600, 1), (1250, 7)):
            week.resize(width, 650)
            for _ in range(4):
                self.app.processEvents()
            self.assertEqual(expected, week.columns)
            self.assertEqual(0, week.horizontalScrollBar().maximum())
            for section in week.days:
                self.assertLessEqual(section.geometry().right(), week.widget().width())
            for _, entry in week.entries:
                for text in entry.findChildren(QLabel):
                    self.assertLessEqual(text.geometry().right(), entry.width())
                    self.assertLessEqual(text.geometry().bottom(), entry.height())
        self.assertEqual(['early', 'a', 'b'], [t['id'] for t, _ in week.entries])
        week.entries[0][1].click()
        self.host.edit_task.assert_called_once_with(tasks[2])
        self.assertEqual(7, len(week.days))
        week.close()


if __name__ == '__main__':
    unittest.main()
