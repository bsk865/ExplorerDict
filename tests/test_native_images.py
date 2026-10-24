import io
import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock
from PIL import Image
from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from app.native.images import ImageView, ImageDialog
from app.native.service import NativeService


class NativeImageSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        data = io.BytesIO()
        Image.new('RGB', (1000, 500), 'white').save(data, 'PNG')
        self.raw = data.getvalue()
        self.view = ImageView(self.raw)
        self.view.resize(620, 420)
        self.view.show()
        self.app.processEvents()
        self.view.fit()

    def tearDown(self):
        self.view.close()

    def test_reverse_drag_preserves_normalized_coordinates_after_zoom(self):
        self.view.zoom(1.25)
        first = self.view.mapFromScene(QPointF(700, 350))
        second = self.view.mapFromScene(QPointF(300, 150))
        QTest.mousePress(self.view.viewport(), Qt.MouseButton.LeftButton, pos=first)
        QTest.mouseMove(self.view.viewport(), second)
        QTest.mouseRelease(self.view.viewport(), Qt.MouseButton.LeftButton, pos=second)
        self.assertIsNotNone(self.view.region)
        for actual, expected in zip(self.view.region, [.3, .3, .4, .4]):
            self.assertAlmostEqual(expected, actual, delta=.005)
        self.view.clear_region()
        self.assertIsNone(self.view.region)

    def test_drag_outside_image_clamps_to_original_bounds(self):
        first = self.view.mapFromScene(QPointF(500, 250))
        last = self.view.viewport().rect().bottomRight()
        QTest.mousePress(self.view.viewport(), Qt.MouseButton.LeftButton, pos=first)
        QTest.mouseMove(self.view.viewport(), last)
        QTest.mouseRelease(self.view.viewport(), Qt.MouseButton.LeftButton, pos=last)
        x, y, w, h = self.view.region
        self.assertLessEqual(x+w, 1.00001)
        self.assertLessEqual(y+h, 1.00001)
        self.assertGreater(w, 0)
        self.assertGreater(h, 0)

    def test_fit_follows_resize_until_manual_zoom_and_can_resume(self):
        before = self.view.transform().m11()
        self.view.resize(920, 720)
        self.app.processEvents()
        self.assertGreater(self.view.transform().m11(), before)
        self.view.zoom(2)
        manual = self.view.transform().m11()
        self.view.resize(600, 400)
        self.app.processEvents()
        self.assertAlmostEqual(manual, self.view.transform().m11())
        self.view.fit()
        self.assertLess(self.view.transform().m11(), manual)
        self.view.resize(920, 720)
        self.app.processEvents()
        visible = self.view.mapFromScene(self.view.bounds).boundingRect()
        self.assertLessEqual(visible.width(), self.view.viewport().width())
        self.assertLessEqual(visible.height(), self.view.viewport().height())

    def test_pan_moves_zoomed_image_without_replacing_selection(self):
        self.view.zoom(3)
        self.view.region = [.3, .3, .2, .2]
        self.view.set_panning(True)
        seen = []
        self.view.selected.connect(seen.append)
        horizontal = self.view.horizontalScrollBar()
        horizontal.setValue(horizontal.maximum() // 2)
        before = horizontal.value()
        start = self.view.viewport().rect().center()
        finish = start + QPointF(70, 0).toPoint()
        QTest.mousePress(self.view.viewport(), Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(self.view.viewport(), finish)
        QTest.mouseRelease(self.view.viewport(), Qt.MouseButton.LeftButton, pos=finish)
        self.assertNotEqual(before, horizontal.value())
        self.assertEqual([.3, .3, .2, .2], self.view.region)
        self.assertEqual([], seen)
        self.view.set_panning(False)
        self.assertFalse(self.view.outline.rect().isEmpty())

    def test_unreadable_image_cannot_open_a_conversation(self):
        host = QWidget()
        host.service = Mock()
        host.open_article = Mock()
        dialog = ImageDialog(host, {'id': 'article'},
                             {'title': '图片', 'block': 'b0', 'asset': 'missing'}, b'broken image')
        self.assertFalse(dialog.use.isEnabled())
        self.assertIn('无法读取', dialog.error.text())
        dialog.choose()
        host.service.store.object.assert_not_called()
        host.open_article.assert_not_called()
        dialog.close()
        host.close()

    def test_selected_region_reopens_on_its_article_without_model_request(self):
        with tempfile.TemporaryDirectory() as directory:
            service = NativeService(Path(directory))
            service.workspace.map_preloader.close()
            host = QWidget()
            host.service = service
            host.open_article = Mock()
            service.workspace.start = Mock()
            asset, _ = service.store.image(self.raw)
            article = service.store.import_article('图片阅读', [{'type': 'image', 'asset': asset, 'caption': '示意图'}])
            original = service.store.object(article['id'], {'type': 'image', 'block': 'b0', 'asset': asset, 'title': '示意图'})
            dialog = ImageDialog(host, article, original, self.raw)
            dialog.view.region = [.2, .1, .4, .5]
            dialog.choose()
            chosen = host.open_article.call_args.kwargs['object_id']
            self.assertEqual(article['id'], host.open_article.call_args.args[0])
            saved = next(obj for obj in service.store.article(article['id'])['objects'] if obj['id'] == chosen)
            reopened = ImageDialog(host, article, saved, self.raw)
            self.assertEqual('region', saved['type'])
            self.assertEqual([.2, .1, .4, .5], reopened.view.region)
            self.assertEqual('理解选区', reopened.use.text())
            service.workspace.start.assert_not_called()
            reopened.close()
            host.close()
            service.close()


if __name__ == '__main__':
    unittest.main()
