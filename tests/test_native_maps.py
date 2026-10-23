import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QPushButton
from app.native.maps import MapScene, MapView


GRAPH = {'root': 'root', 'nodes': [
    {'id': 'root', 'parent': None, 'label': '阅读与思考', 'quote': '阅读与思考'},
    {'id': 'a', 'parent': 'root', 'label': '阅读全文', 'quote': '阅读全文'},
    {'id': 'b', 'parent': 'root', 'label': '记录自己的理解并且在需要的时候返回原文进行核对', 'quote': '记录理解'},
    {'id': 'c', 'parent': 'b', 'label': '核对原文', 'quote': '核对原文'}],
    'relations': [{'id': 'r', 'src': 'a', 'dst': 'c', 'label': '之后可以', 'quote': '阅读全文后可以核对原文'}]}


class NativeMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_wrapped_nodes_do_not_overlap(self):
        scene = MapScene(GRAPH, {})
        boxes = [item.rect() for item in scene.nodes.values()]
        self.assertEqual(4, len(boxes))
        for i, box in enumerate(boxes):
            for other in boxes[i+1:]:
                self.assertFalse(box.intersects(other))
        for item in scene.nodes.values():
            for text in item.childItems():
                self.assertLessEqual(text.boundingRect().height()+16, item.rect().height())

    def test_full_export_includes_hidden_nodes_and_relations(self):
        collapsed = MapScene(GRAPH, {}, depth=1)
        self.assertNotIn('c', collapsed.nodes)
        full = MapScene(GRAPH, {}, include_relations=True)
        with tempfile.TemporaryDirectory() as directory:
            svg = Path(directory) / 'map.svg'
            png = Path(directory) / 'map.png'
            full.export(svg)
            full.export(png)
            root = ET.parse(svg).getroot()
            text = ''.join(root.itertext())
            self.assertIn('核对原文', text)
            self.assertIn('之后可以', text)
            image = QImage(str(png))
            self.assertFalse(image.isNull())
            self.assertGreater(image.width(), 100)
            self.assertGreater(image.height(), 100)
            colors = {image.pixelColor(x, y).name() for x in range(0, image.width(), 7) for y in range(0, image.height(), 7)}
            self.assertGreater(len(colors), 2)

    def test_bindings_and_saved_terms_keep_visual_associations(self):
        graph = dict(GRAPH, bindings=[{'node_ids': ['a']}])
        scene = MapScene(graph, {}, objects=[{'type': 'term', 'saved': True, 'title': '核对原文'}])
        self.assertTrue(scene.nodes['a'].data(1))
        self.assertTrue(scene.nodes['c'].data(1))
        self.assertFalse(scene.nodes['b'].data(1))
        stripes = [item for item in scene.nodes['a'].childItems() if hasattr(item, 'brush')]
        other = [item for item in scene.nodes['b'].childItems() if hasattr(item, 'brush')]
        self.assertNotEqual(stripes[0].brush().color(), other[0].brush().color())

    def test_keyboard_traversal_selects_nodes_and_leaves_view(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        before, after = QPushButton('Before'), QPushButton('After')
        view = MapView()
        scene = MapScene(GRAPH, {})
        view.setScene(scene)
        for widget in (before, view, after):
            layout.addWidget(widget)
        panel.resize(480, 320)
        panel.show()
        panel.activateWindow()
        self.app.processEvents()
        before.setFocus()
        QTest.keyClick(before, Qt.Key.Key_Tab)
        self.app.processEvents()
        nodes = list(scene.nodes.values())
        for i, node in enumerate(nodes):
            self.assertIs(scene.focusItem(), node)
            QTest.keyClick(view, Qt.Key.Key_Return)
            self.assertEqual([node], scene.selectedItems())
            self.assertTrue(view.mapToScene(view.viewport().rect()).boundingRect().intersects(node.sceneBoundingRect()))
            QTest.keyClick(view, Qt.Key.Key_Tab)
            self.app.processEvents()
        self.assertTrue(after.hasFocus(), 'Tab must not trap focus in map')
        QTest.keyClick(after, Qt.Key.Key_Backtab)
        self.app.processEvents()
        self.assertIs(scene.focusItem(), nodes[-1])
        QTest.keyClick(view, Qt.Key.Key_Backtab)
        self.assertIs(scene.focusItem(), nodes[-2])
        panel.close()

    def test_large_map_wrapping_and_exports_keep_all_nodes(self):
        graph = {'root': 'root', 'nodes': [{'id': 'root', 'parent': None, 'label': '长图验收'}], 'relations': []}
        for i in range(60):
            graph['nodes'].append({'id': str(i), 'parent': 'root',
                                   'label': f'节点{i}：阅读较长材料后整理观点，并返回原文核对来源与结论。'})
        scene = MapScene(graph, {})
        boxes = [node.sceneBoundingRect() for node in scene.nodes.values()]
        self.assertEqual(61, len(boxes))
        for i, box in enumerate(boxes):
            self.assertTrue(scene.sceneRect().contains(box))
            self.assertFalse(any(box.intersects(other) for other in boxes[i+1:]))
        with tempfile.TemporaryDirectory() as directory:
            svg, png = Path(directory)/'long.svg', Path(directory)/'long.png'
            scene.export(svg)
            scene.export(png)
            contents = ''.join(ET.parse(svg).getroot().itertext())
            for i in range(60):
                self.assertIn(f'节点{i}：', contents)
            image = QImage(str(png))
            self.assertFalse(image.isNull())
            self.assertLessEqual(image.width()*image.height(), 24_000_000)

    def test_segmented_maximum_depth_is_visible_and_exported(self):
        # Eight validated parent hops, plus the long-document aggregate root.
        nodes = [{'id': 'document', 'parent': None, 'label': '长文章总览'}]
        for level in range(9):
            nodes.append({'id': str(level), 'parent': 'document' if level == 0 else str(level-1),
                          'label': f'第{level}层依据'})
        graph = {'root': 'document', 'nodes': nodes, 'relations': []}
        full = MapScene(graph, {})
        self.assertEqual(10, len(full.nodes))
        self.assertIn('8', full.nodes)
        self.assertNotIn('8', MapScene(graph, {}, depth=3).nodes)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'deep.svg'
            full.export(target)
            self.assertIn('第8层依据', ''.join(ET.parse(target).getroot().itertext()))


if __name__ == '__main__':
    unittest.main()
