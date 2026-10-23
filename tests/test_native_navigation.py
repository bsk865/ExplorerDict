import unittest
from unittest.mock import Mock
from PySide6.QtWidgets import QApplication, QWidget
from app.native.navigation import Sidebar, ROUTES
from app.native.widgets import stylesheet
from app.reading_preferences import PALETTES


class NativeNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_original_assets_and_routes_across_themes(self):
        host = QWidget()
        host.resize(1380, 880)
        for name in ('navigate', 'import_material', 'preferences', 'model_settings', 'open_article'):
            setattr(host, name, Mock())
        sidebar = Sidebar(host)
        articles = [{'id': 'article', 'title': '一篇有较长标题的文章：引用、笔记与主题归类'}]
        for palette in PALETTES:
            for mode in ('light', 'dark'):
                preference = {'palette': palette, 'mode': mode}
                host.setStyleSheet(stylesheet(preference))
                sidebar.update_content(articles, preference, 'shelf')
                self.assertFalse(sidebar.mark.pixmap().isNull())
                self.assertFalse(sidebar.character.icon().isNull())
                self.assertEqual(224, sidebar.width())
                self.assertTrue(sidebar.buttons['shelf'].isChecked())
                self.assertTrue(all(not b.icon().isNull() for b in sidebar.buttons.values()))
        sidebar.adapt(900, 620)
        self.assertEqual(180, sidebar.width())
        self.assertTrue(sidebar.tagline.isHidden())
        for route, _, _ in ROUTES:
            sidebar.buttons[route].click()
            host.navigate.assert_called_with(route)
        sidebar.recent.itemAt(0).widget().click()
        host.open_article.assert_called_once_with('article')
        sidebar.add.click()
        host.import_material.assert_called_once()
        sidebar.deleteLater()
        host.deleteLater()


if __name__ == '__main__':
    unittest.main()
