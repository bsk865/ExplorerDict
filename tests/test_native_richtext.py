import unittest
import io

from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QPoint
from PySide6.QtTest import QTest
from PIL import Image

from app.native.reader import OriginalText
from app.reading_annotations import block_text


class RichTextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_formats_tables_and_existing_offsets(self):
        html = '<h2>Heading</h2><p>One <b>bold</b> term.</p><ul><li>First</li><li>Second</li></ul><table><tr><td>A</td><td>B😀</td></tr></table>'
        block = {'id': 'b0', 'type': 'text', 'html': html}
        widget = OriginalText()
        widget.load_article({'blocks': [block]}, {})
        text = block_text(block)
        start = text.index('bold')
        widget.annotate([{'style': 'yellow', 'segments': [{'block': 'b0', 'start': start, 'end': start + 4}]}])
        selections = widget.extraSelections()
        selection = selections[0].cursor
        self.assertEqual('bold', selection.selectedText())
        self.assertGreater(selection.charFormat().fontWeight(), 400)
        self.assertIn('<table', widget.document().toHtml())
        start = text.index('B😀')
        spans = widget.ranges[0]['spans']
        cursor = widget.textCursor()
        cursor.setPosition(spans[start][0])
        cursor.setPosition(spans[start + 1][1], QTextCursor.MoveMode.KeepAnchor)
        widget.setTextCursor(cursor)
        self.assertEqual([{'block': 'b0', 'start': start, 'end': start + 2, 'quote': 'B😀'}], widget.selected_segments())
        widget.close()

    def test_whitespace_normalization_preserves_source_quote(self):
        block = {'id': 'b0', 'type': 'text', 'html': '<p> Alpha   beta </p>\n<p>Gamma</p>'}
        widget = OriginalText()
        widget.load_article({'blocks': [block]}, {})
        text = block_text(block)
        start, end = text.index('beta'), text.index('Gamma') + 5
        widget.annotate([{'style': 'underline', 'segments': [{'block': 'b0', 'start': start, 'end': end}]}])
        selections = widget.extraSelections()
        widget.setTextCursor(selections[0].cursor)
        self.assertEqual(text[start:end], widget.selected_segments()[0]['quote'])
        widget.close()

    def test_unsupported_content_keeps_all_source_text(self):
        block = {'id': 'b0', 'type': 'text', 'html': '<p>A<img src="missing">B</p>'}
        widget = OriginalText()
        widget.load_article({'blocks': [block]}, {})
        self.assertEqual('AB', widget.toPlainText().strip())
        widget.close()

    def test_image_source_location_has_its_own_anchor(self):
        widget = OriginalText()
        widget.load_article({'blocks': [
            {'id': 'b0', 'type': 'text', 'text': 'First paragraph'},
            {'id': 'b1', 'type': 'image', 'asset': 'missing', 'caption': 'Figure'}]}, {})
        widget.locate('b1')
        self.assertEqual(widget.anchors['b1'], widget.textCursor().position())
        self.assertGreater(widget.textCursor().position(), 0)
        widget.close()

    def test_image_resizes_and_clicks_without_moving_annotation_offsets(self):
        raw = io.BytesIO()
        Image.new('RGB', (1000, 400), '#6b8c77').save(raw, 'PNG')
        widget = OriginalText()
        widget.resize(800, 600)
        widget.load_article({'blocks': [
            {'id': 'b0', 'type': 'image', 'asset': 'test', 'caption': 'Figure'},
            {'id': 'b1', 'type': 'text', 'text': 'A😀B'}]}, {'test': raw.getvalue()})
        widget.annotate([{'style': 'yellow', 'segments': [{'block': 'b1', 'start': 1, 'end': 2}]}])
        widget.show()
        QTest.qWait(40)
        before = widget.ranges[0]['spans'][:]
        widget.resize(360, 500)
        QTest.qWait(60)
        cursor = QTextCursor(widget.document())
        cursor.setPosition(0)
        cursor.movePosition(QTextCursor.MoveOperation.NextCharacter, QTextCursor.MoveMode.KeepAnchor)
        fmt = cursor.charFormat().toImageFormat()
        self.assertLessEqual(fmt.width(), widget.image_width())
        self.assertAlmostEqual(fmt.height()/fmt.width(), .4)
        self.assertEqual(0, widget.horizontalScrollBar().maximum())
        self.assertEqual(before, widget.ranges[0]['spans'])
        selections = widget.extraSelections()
        self.assertEqual('😀', selections[0].cursor.selectedText())
        point = next(QPoint(x, y) for y in range(10, 150, 10) for x in range(10, 200, 10)
                     if widget.anchorAt(QPoint(x, y)) == 'native-image:b0')
        clicked = []
        widget.imageClicked.connect(clicked.append)
        QTest.mouseClick(widget.viewport(), Qt.MouseButton.LeftButton, pos=point)
        self.assertEqual(['b0'], clicked)
        QTest.mousePress(widget.viewport(), Qt.MouseButton.LeftButton, pos=point)
        QTest.mouseMove(widget.viewport(), point+QPoint(60, 0))
        QTest.mouseRelease(widget.viewport(), Qt.MouseButton.LeftButton, pos=point+QPoint(60, 0))
        self.assertEqual(['b0'], clicked)
        widget.close()

    def test_fixed_width_table_fits_narrow_reader_without_changing_source_or_quotes(self):
        html = '<table width="1200"><tr><td width="600">Alpha</td><td width="600">Beta😀</td></tr></table>'
        block = {'id':'b0', 'type':'text', 'html':html}
        widget = OriginalText()
        widget.resize(340, 450)
        widget.load_article({'blocks':[block, {'id':'b1','type':'text','text':'After the table.'}]}, {})
        widget.show()
        QTest.qWait(40)
        self.assertEqual(0, widget.horizontalScrollBar().maximum())
        self.assertEqual(html, block['html'])
        start = block_text(block).index('Beta😀')
        widget.annotate([{'style':'yellow', 'segments':[{'block':'b0','start':start,'end':start+5}]}])
        selections = widget.extraSelections()
        widget.setTextCursor(selections[0].cursor)
        self.assertEqual('Beta😀', widget.selected_segments()[0]['quote'])
        widget.resize(800, 500)
        QTest.qWait(40)
        self.assertEqual(0, widget.horizontalScrollBar().maximum())
        self.assertIn('After the table.', widget.toPlainText())
        widget.close()

    def test_long_formula_fits_viewport_and_copies_original_math(self):
        expression = ''.join('<mi>x</mi><mo>+</mo>' for _ in range(30)) + '<mn>1</mn>'
        block = {'id':'b0', 'type':'text', 'html':'<p>Formula: <math><mrow>'+expression+'</mrow></math></p>'}
        widget = OriginalText()
        widget.resize(360, 450)
        widget.load_article({'blocks':[block]}, {})
        widget.show()
        QTest.qWait(40)
        self.assertEqual(0, widget.horizontalScrollBar().maximum())
        clicked = []
        widget.formulaClicked.connect(clicked.append)
        point = next(QPoint(x, y) for y in range(5, 150, 5) for x in range(5, 330, 5)
                     if widget.anchorAt(QPoint(x, y)).startswith('native-formula:'))
        QTest.mouseClick(widget.viewport(), Qt.MouseButton.LeftButton, pos=point)
        self.assertEqual(1, len(clicked))
        self.assertIn(clicked[0], widget.formula_html)
        cursor = widget.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        widget.setTextCursor(cursor)
        self.assertEqual(block_text(block), widget.createMimeDataFromSelection().text().strip())
        self.assertIn('<math>', widget.createMimeDataFromSelection().html())
        widget.close()


if __name__ == '__main__':
    unittest.main()
