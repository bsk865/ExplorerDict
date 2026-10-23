import json
import unittest
from unittest.mock import Mock

from PySide6.QtCore import Qt, QMimeData, QPointF, QTimer
from PySide6.QtGui import QDragMoveEvent, QDragLeaveEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QListWidgetItem, QLineEdit, QPushButton, QScrollArea, QLabel
from PySide6.QtTest import QTest

from app.native.shelf import ThemeList, ThemeRooms, ARTICLE_MIME, ShelfPage
from app.reading_preferences import colors


class ThemeDragTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widget = ThemeList()
        for label, key in [('全部资料', ''), ('未分类', 'unfiled'), ('阅读', 'theme')]:
            item = QListWidgetItem(label, self.widget)
            item.setData(Qt.ItemDataRole.UserRole, key)
        self.widget.resize(240, 240)
        self.widget.show()
        self.app.processEvents()
        self.widget.setCurrentRow(0)
        self.mime = QMimeData()
        self.mime.setData(ARTICLE_MIME, json.dumps(['article']).encode())
        self.assigned = []
        self.widget.assigned.connect(lambda key, ids: self.assigned.append((key, ids)))

    def tearDown(self):
        self.widget.close()

    def move(self, row):
        point = self.widget.visualItemRect(self.widget.item(row)).center()
        event = QDragMoveEvent(point, Qt.DropAction.CopyAction, self.mime,
                               Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        self.widget.dragMoveEvent(event)
        return event

    def test_hover_and_cancel_preserve_current_theme(self):
        self.assertTrue(self.move(2).isAccepted())
        self.assertEqual(0, self.widget.currentRow())
        self.assertIs(self.widget.item(2), self.widget.drop_target)
        self.widget.dragLeaveEvent(QDragLeaveEvent())
        self.assertIsNone(self.widget.drop_target)
        self.assertEqual(0, self.widget.currentRow())
        self.assertEqual([], self.assigned)

    def test_drop_assigns_without_changing_filter_and_rejects_empty(self):
        point = QPointF(self.widget.visualItemRect(self.widget.item(2)).center())
        for payload, accepted in [(['article'], True), ([], False), ([''], False)]:
            self.mime.setData(ARTICLE_MIME, json.dumps(payload).encode())
            self.move(2)
            event = QDropEvent(point, Qt.DropAction.CopyAction, self.mime,
                               Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            self.widget.dropEvent(event)
            self.assertEqual(accepted, event.isAccepted())
            self.assertIsNone(self.widget.drop_target)
            self.assertEqual(0, self.widget.currentRow())
        self.assertEqual([('theme', ['article'])], self.assigned)

    def test_non_theme_rows_clear_target_and_reject_drag(self):
        self.move(2)
        self.assertFalse(self.move(1).isAccepted())
        self.assertIsNone(self.widget.drop_target)
        self.assertFalse(self.move(0).isAccepted())


class ShelfActionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = Mock()
        self.host.search = QLineEdit()
        self.article = {'id':'article', 'title':'很长的阅读资料名称' * 12, 'chars':100}
        snapshot = {'preferences':{'palette':'clay', 'mode':'light'}, 'articles':[self.article],
                    'study':{'themes':[{'id':'theme','title':'阅读','description':'阅读心得'}], 'memberships':[], 'notes':[]}}
        self.page = ShelfPage(self.host, snapshot)
        self.page.resize(650, 450)
        self.page.show()
        self.app.processEvents()

    def tearDown(self):
        self.page.close()

    def test_empty_filter_and_removed_theme_recover(self):
        self.assertFalse(self.page.empty.isVisible())
        self.page.select_theme('theme')
        self.assertTrue(self.page.empty.isVisible())
        self.assertEqual('这里还没有文章。', self.page.empty.text())
        self.assertFalse(self.page.articles.isVisible())
        self.page.snapshot['study']['themes'] = []
        self.page.select_theme('theme')
        self.assertEqual('', self.page.theme)
        self.assertEqual(0, self.page.themes.currentRow())
        self.assertEqual(1, self.page.articles.count())
        self.assertTrue(self.page.articles.isVisible())
        self.page.filter('无匹配内容')
        self.assertTrue(self.page.empty.isVisible())
        self.assertEqual('没有找到匹配的文章。', self.page.empty.text())
        self.page.filter('')
        self.assertFalse(self.page.empty.isVisible())
        self.assertTrue(self.page.articles.isVisible())

    def test_room_grid_fills_width_and_scrolls_without_overlap(self):
        rooms = ThemeRooms(colors('clay', 'light'))
        for i in range(30):
            QListWidgetItem(f'主题{i}：阅读与研究方法\n1 篇文章', rooms)
        rooms.show()
        try:
            for width in (820, 390, 630):
                rooms.resize(width, 340)
                self.app.processEvents()
                self.app.processEvents()
                columns = max(1, rooms.viewport().width() // 194)
                self.assertLess(rooms.viewport().width() - columns * rooms.gridSize().width(), columns)
                self.assertEqual(0, rooms.horizontalScrollBar().maximum())
                self.assertGreater(rooms.verticalScrollBar().maximum(), 0)
                rectangles = [rooms.visualItemRect(rooms.item(i)) for i in range(30)]
                for i, rect in enumerate(rectangles):
                    self.assertTrue(all(not rect.intersects(other) for other in rectangles[i+1:]))
        finally:
            rooms.close()

    def test_single_click_reads_but_control_click_only_selects(self):
        row = self.page.articles.visualItemRect(self.page.articles.item(0))
        point = row.topLeft() + row.center()/4
        QTest.mouseClick(self.page.articles.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, point)
        self.host.open_article.assert_not_called()
        QTest.mouseClick(self.page.articles.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
        self.host.open_article.assert_called_once_with('article')

    def test_row_buttons_are_reachable_and_classification_cancel_does_not_open(self):
        row = self.page.articles.itemWidget(self.page.articles.item(0))
        actions = row.findChildren(QPushButton)
        self.assertEqual(['阅读', '归入主题', '删除资料'], [b.toolTip() for b in actions])
        for action in actions:
            self.assertTrue(row.rect().contains(action.geometry()))
        QTimer.singleShot(0, lambda: self.app.activeModalWidget().reject())
        actions[1].click()
        self.host.open_article.assert_not_called()
        self.host.tasks.submit.assert_not_called()
        actions[2].click()
        self.host.delete.assert_called_once_with('articles', 'article', self.article['title'])

    def test_many_themes_scroll_and_failed_save_can_retry(self):
        self.page.snapshot['study']['themes'] = [
            {'id':str(i), 'title':f'主题{i}：'+('阅读与研究方法' * 9)} for i in range(30)]
        self.page.snapshot['study']['memberships'] = [{'theme_id':'0', 'article_id':'article'}]
        errors = []
        def inspect():
            dialog = self.app.activeModalWidget()
            try:
                scroll = dialog.findChild(QScrollArea)
                self.assertLessEqual(dialog.height(), 560)
                self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
                self.assertEqual(0, scroll.horizontalScrollBar().maximum())
                controls = scroll.findChildren(QPushButton)
                self.assertEqual(30, len(controls))
                self.assertFalse(controls[0].isEnabled())
                for control in controls:
                    label = control.findChild(QLabel)
                    self.assertTrue(control.rect().contains(label.geometry()))
                    self.assertGreaterEqual(label.height(), label.heightForWidth(label.width()))
                self.host.tasks.submit.side_effect = lambda work, done, failed: failed('private error')
                controls[-1].click()
                self.assertTrue(controls[-1].isEnabled())
                self.assertFalse(controls[0].isEnabled())
                self.assertTrue(any('重试' in label.text() for label in dialog.findChildren(QLabel)))
                self.host.tasks.submit.side_effect = lambda work, done, failed: done(work())
                controls[-1].click()
                self.host.service.hub.assign.assert_called_once_with('29', ['article'], 'add')
                self.host.refresh.assert_called_once()
            except Exception as exc:
                errors.append(str(exc))
            finally:
                dialog.reject()
        QTimer.singleShot(50, inspect)
        self.page.classify(self.article)
        self.assertEqual([], errors)
