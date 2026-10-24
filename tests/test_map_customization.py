import copy
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock, patch
from PySide6.QtCore import Qt, QPoint, QPointF, QEvent
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from app import map_customization as custom
from app.map_input import markdown
from app.native.maps import MapPage
from app.workspace_store import WorkspaceStore
from tests.test_native_maps import GRAPH


class MapCustomizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = WorkspaceStore(Path(self.temp.name))
        self.article = self.store.import_article('阅读与思考', [{'type': 'text', 'text': '阅读全文后，记录理解，再核对原文。'}],
                                                 graph=copy.deepcopy(GRAPH), quality={'passed': True, 'input_mode': 'text'})

    def tearDown(self):
        self.temp.cleanup()

    def page(self):
        host = QWidget()
        host.maps = {}
        host.snapshot = {'preferences': {'palette': 'forest', 'mode': 'light'}}
        host.service = Mock(store=self.store)
        host.notice = Mock()
        page = MapPage(host, {'article': self.store.article(self.article['id'])})
        page.resize(1100, 700)
        page.show()
        self.app.processEvents()
        return host, page

    def test_edit_reopens_and_preserves_reviewed_graph_and_other_articles(self):
        changed, _, _ = custom.save(self.store, self.article, node_id='a', label='先通读，再理解', position=[25, -30])
        reopened = WorkspaceStore(self.store.directory).article(changed['id'])
        self.assertEqual('先通读，再理解', custom.display_graph(reopened)['nodes'][1]['label'])
        self.assertEqual({'a': [25, -30]}, custom.positions(reopened))
        self.assertEqual(self.article['map'], reopened['map'])
        self.assertEqual(self.article['quality'], reopened['quality'])
        self.assertEqual('阅读全文', custom.display_graph(reopened)['nodes'][1]['quote'])
        other = self.store.import_article('另一篇', [{'type': 'text', 'text': '不同文章'}], graph=copy.deepcopy(GRAPH))
        self.assertEqual({}, custom.customization(other))
        reopened['map']['nodes'][1]['label'] = '新图内容'
        self.store.update_article(reopened)
        self.assertEqual({}, custom.customization(self.store.article(reopened['id'])))

    def test_stale_deleted_and_archived_articles_cannot_be_written(self):
        changed = copy.deepcopy(self.article)
        changed['map']['nodes'][0]['label'] = '新图'
        self.store.update_article(changed)
        with self.assertRaises(ValueError):
            custom.save(self.store, self.article, node_id='a', label='旧图修改')
        self.store.update_article(self.article)
        changed = copy.deepcopy(self.article)
        changed['version'] = 'different'
        self.store.update_article(changed)
        with self.assertRaises(ValueError):
            custom.save(self.store, self.article, node_id='a', position=[3, 3])
        changed['superseded_by'] = 'new-article'
        self.store.update_article(changed)
        with self.assertRaises(ValueError):
            custom.save(self.store, changed, reset_layout=True)
        self.store.delete_articles([changed['id']])
        with self.assertRaises(ValueError):
            custom.save(self.store, changed, replacement={})
        self.assertIsNone(self.store.get('articles', changed['id']))

    def test_reset_layout_undo_and_latest_unrelated_fields(self):
        changed, before, after = custom.save(self.store, self.article, node_id='a', label='新标签', position=[22, 13])
        latest = self.store.article(changed['id'])
        latest['personal_note'] = '另一个页面写入'
        self.store.update_article(latest)
        restored, _, _ = custom.save(self.store, changed, reset_layout=True)
        self.assertEqual({'labels': {'a': '新标签'}}, custom.customization(restored))
        self.assertEqual('另一个页面写入', restored['personal_note'])
        undone, _, _ = custom.save(self.store, restored, replacement=after)
        self.assertEqual({'a': [22, 13]}, custom.positions(undone))
        original, _, _ = custom.save(self.store, undone, replacement=before)
        self.assertEqual({}, custom.customization(original))
        self.assertNotIn('map_customizations', original)

    def test_corrupt_overrides_and_invalid_values_are_rejected_or_ignored(self):
        for value in ([], 'broken', {'labels': ['bad'], 'positions': 'bad'},
                      {'labels': {'a': '', 'gone': 'bad'}, 'positions': {'a': [float('nan'), 1]}}):
            changed = dict(self.article, map_customizations={custom.fingerprint(GRAPH): value})
            self.store.update_article(changed)
            self.assertEqual({}, custom.customization(changed))
            fixed, _, _ = custom.save(self.store, changed, node_id='a', label='可用的标签')
            self.assertEqual({'labels': {'a': '可用的标签'}}, custom.customization(fixed))
        for value in ([True, 3], [float('inf'), 0], [100001, 0], [1], 'bad'):
            with self.assertRaises(ValueError):
                custom.save(self.store, fixed, node_id='a', position=value)
        for value in ('', ' ' * 4, '字' * 97):
            with self.assertRaises(ValueError):
                custom.save(self.store, fixed, node_id='a', label=value)

    def test_conflicting_undo_does_not_overwrite_another_edit(self):
        changed, _, _ = custom.save(self.store, self.article, node_id='a', label='标签一')
        other, _, _ = custom.save(self.store, changed, node_id='b', label='标签二')
        with self.assertRaises(ValueError):
            custom.save(self.store, changed, replacement={})
        self.assertEqual(custom.customization(other), custom.customization(self.store.article(other['id'])))

    def test_page_edit_export_undo_and_keyboard_movement(self):
        host, page = self.page()
        self.addCleanup(host.close)
        self.addCleanup(page.close)
        page.view.zoom(1.1)
        scale = page.view.transform().m11()
        page.view.scene().nodes['a'].setSelected(True)
        page.edit_label.click()
        page.label_input.setText('通读整篇文章')
        QTest.keyClick(page.label_input, Qt.Key.Key_Return)
        self.assertEqual('通读整篇文章', page.selected['label'])
        self.assertIn('原文字：阅读全文', page.quote.toPlainText())
        self.assertAlmostEqual(scale, page.view.transform().m11())
        self.assertIn('通读整篇文章', page.relations.item(0).text())
        self.assertIsNone(QApplication.activeModalWidget())
        page.arrange.click()
        node = page.view.scene().nodes['a']
        node.setFocus()
        old_edge = page.view.scene().edges[0][2].path()
        QTest.keyClick(page.view, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(QPointF(20, 0), node.pos())
        self.assertEqual({'a': [20, 0]}, custom.positions(self.store.article(self.article['id'])))
        self.assertNotEqual(old_edge, page.view.scene().edges[0][2].path())
        self.assertAlmostEqual(scale, page.view.transform().m11())
        target = Path(self.temp.name) / 'map.svg'
        with patch('app.native.maps.QFileDialog.getSaveFileName', return_value=(str(target), 'SVG')):
            page.export()
        contents = ''.join(ET.parse(target).getroot().itertext())
        self.assertIn('通读整篇文章', contents)
        self.assertIn('手动修改', contents)
        self.assertIn('核对原文', contents)
        self.assertIn('手动修改；原标签：阅读全文', markdown(page.controller.article))
        page.reset_layout.click()
        self.assertEqual(QPointF(), node.pos())
        self.assertEqual('通读整篇文章', page.selected['label'])
        page.undo.click()
        self.assertEqual(QPointF(20, 0), node.pos())
        page.edit_label.click()
        page.restore_label.click()
        self.assertEqual('阅读全文', page.selected['label'])
        page.undo.click()
        self.assertEqual('通读整篇文章', page.selected['label'])
        page.controller.job = 'in-progress'
        page.render()
        self.assertFalse(page.arrange.isEnabled())
        self.assertFalse(page.arrange.isChecked())
        self.assertFalse(page.edit_label.isEnabled())
        self.assertFalse(page.controller.customize(node_id='a', label='生成中误改'))
        page.controller.job = None

    def test_node_drag_saves_on_release_without_replacing_scene(self):
        host, page = self.page()
        self.addCleanup(host.close)
        self.addCleanup(page.close)
        page.arrange.click()
        scene = page.view.scene()
        node = scene.nodes['a']
        start = page.view.mapFromScene(node.sceneBoundingRect().center())
        end = start + QPoint(35, 20)
        QTest.mousePress(page.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
        event = QMouseEvent(QEvent.Type.MouseMove, QPointF(end), QPointF(page.view.viewport().mapToGlobal(end)),
                            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(page.view.viewport(), event)
        self.assertFalse(custom.positions(self.store.article(self.article['id'])))
        QTest.mouseRelease(page.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)
        self.assertIs(scene, page.view.scene())
        self.assertNotEqual(QPointF(), node.pos())
        self.assertEqual([node.pos().x(), node.pos().y()], custom.positions(self.store.article(self.article['id']))['a'])
        self.assertTrue(scene.sceneRect().contains(node.sceneBoundingRect()))


if __name__ == '__main__':
    unittest.main()
