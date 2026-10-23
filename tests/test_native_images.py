import io
import unittest
from PIL import Image
from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from app.native.images import ImageView


class NativeImageSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        data = io.BytesIO()
        Image.new('RGB', (1000, 500), 'white').save(data, 'PNG')
        self.view = ImageView(data.getvalue())
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


if __name__ == '__main__':
    unittest.main()
