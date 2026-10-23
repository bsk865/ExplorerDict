import unittest

from PySide6.QtCore import QUrl
from PySide6.QtGui import QTextCursor, QImage
from PySide6.QtWidgets import QApplication

from app.native.reader import OriginalText
from app.reading_annotations import block_text
from app.native.mathtext import formula_svg


class NativeMathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_math_layout_and_original_annotation_offsets(self):
        html = '<p>Before <math><mfrac><mi>a</mi><msqrt><msup><mi>x</mi><mn>2</mn></msup></msqrt></mfrac></math> after 😀.</p>'
        block = {'id': 'b0', 'type': 'text', 'html': html}
        widget = OriginalText()
        widget.load_article({'blocks': [block]}, {})
        source = block_text(block)
        text = widget.toPlainText()
        self.assertIn('\ufffc', text)
        start = source.index('ax2')
        spans = widget.ranges[0]['spans']
        self.assertEqual(spans[start], spans[start+2])
        widget.annotate([{'style': 'yellow', 'segments': [{'block': 'b0', 'start': start, 'end': start+3}]}])
        selections = widget.extraSelections()
        widget.setTextCursor(selections[0].cursor)
        self.assertEqual('ax2', widget.selected_segments()[0]['quote'])
        cursor = selections[0].cursor
        fmt = cursor.charFormat().toImageFormat()
        self.assertGreater(fmt.height(), 30)
        image = widget.document().resource(2, QUrl(fmt.name()))
        self.assertIsInstance(image, QImage)
        self.assertFalse(image.isNull())
        self.assertTrue(any(image.pixelColor(x, y).alpha() for x in range(0, image.width(), 3)
                            for y in range(0, image.height(), 3)))
        start = source.index('after')
        widget.annotate([{'style': 'underline', 'segments': [{'block': 'b0', 'start': start, 'end': len(source)}]}])
        selections = widget.extraSelections()
        self.assertEqual('after 😀.', selections[0].cursor.selectedText())
        widget.close()

    def test_multiple_formulas_table_and_dark_ink(self):
        block = {'id': 'b0', 'type': 'text', 'html': '<table><tr><td><math><mi>x</mi></math></td><td><math><msub><mi>a</mi><mn>1</mn></msub></math></td></tr></table>'}
        widget = OriginalText()
        widget.load_article({'blocks': [block]}, {}, '#eeeeee')
        self.assertEqual(2, widget.toPlainText().count('\ufffc'))
        cursor = widget.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        widget.setTextCursor(cursor)
        self.assertEqual('xa1', widget.selected_segments()[0]['quote'])
        widget.close()

    def test_copy_retains_formula_source_and_surrounding_text(self):
        widget = OriginalText()
        widget.load_article({'blocks': [
            {'id': 'b0', 'type': 'text', 'html': '<p>😀 <math><mfrac><mi>a</mi><mi>b</mi></mfrac></math> end</p>'},
            {'id': 'b1', 'type': 'text', 'text': 'Next paragraph'}]}, {})
        cursor = widget.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        widget.setTextCursor(cursor)
        mime = widget.createMimeDataFromSelection()
        self.assertIn('😀 ab end', mime.text())
        self.assertIn('\nNext paragraph', mime.text())
        self.assertNotIn('\ufffc', mime.text())
        self.assertIn('<mfrac>', mime.html())
        self.assertNotIn('src="math:', mime.html())
        self.assertIn('Next paragraph', mime.html())
        widget.load_article({'blocks': [{'id': 'b0', 'type': 'text', 'text': 'Plain text'}]}, {})
        cursor = widget.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        widget.setTextCursor(cursor)
        self.assertEqual('Plain text', widget.createMimeDataFromSelection().text().strip())
        widget.close()

    def test_imported_math_constructs_keep_source_and_render(self):
        examples = [
            '<semantics><mrow><mi>x</mi><mo>+</mo><mn>1</mn></mrow></semantics>',
            '<mroot><mi>x</mi><mn>3</mn></mroot>',
            '<msubsup><mi>x</mi><mi>i</mi><mn>2</mn></msubsup>',
            '<munder><mo>lim</mo><mrow><mi>x</mi><mo>→</mo><mn>0</mn></mrow></munder>',
            '<mover accent="true"><mi>x</mi><mo>¯</mo></mover>',
            '<menclose notation="box"><mi>x</mi></menclose>',
            '<mpadded width="+1em"><mstyle mathvariant="bold"><mi>x</mi></mstyle></mpadded>',
            '<mmultiscripts><mi>T</mi><mi>i</mi><none/><mprescripts/><mi>j</mi><mn>2</mn></mmultiscripts>',
            '<mrow><mtext>value</mtext><mspace width="1em"/><mo>=</mo><mn>2</mn></mrow>',
        ]
        for expression in examples:
            with self.subTest(expression=expression):
                block = {'id': 'b0', 'type': 'text', 'html': '<p>Start <math>' + expression + '</math> end.</p>'}
                original = dict(block)
                widget = OriginalText()
                widget.load_article({'blocks': [block]}, {})
                self.assertIn('\ufffc', widget.toPlainText())
                cursor = widget.textCursor()
                cursor.select(QTextCursor.SelectionType.Document)
                widget.setTextCursor(cursor)
                self.assertEqual(block_text(block), widget.selected_segments()[0]['quote'])
                self.assertEqual(block_text(block), widget.createMimeDataFromSelection().text().strip())
                self.assertEqual(original, block)
                widget.close()

    def test_semantics_renders_only_presentation_with_identical_layout(self):
        expressions = [
            '<mfrac><mi>x</mi><msqrt><mi>y</mi></msqrt></mfrac>',
            '<mrow><mo>(</mo><mtable><mtr><mtd><mi>a</mi></mtd><mtd><mi>b</mi></mtd></mtr>'
            '<mtr><mtd><mi>c</mi></mtd><mtd><mi>d</mi></mtd></mtr></mtable><mo>)</mo></mrow>',
            '<munderover><mo>∑</mo><mrow><mi>i</mi><mo>=</mo><mn>0</mn></mrow><mi>n</mi></munderover>',
        ]
        for expression in expressions:
            with self.subTest(expression=expression):
                plain = formula_svg('<math>'+expression+'</math>', '#302d29')
                wrapped = '<math><semantics>'+expression+'<annotation encoding="application/x-tex">alternate text</annotation>' \
                          '<annotation-xml encoding="MathML-Content"><ci>alternate</ci></annotation-xml></semantics></math>'
                with self.assertNoLogs(level='WARNING'):
                    self.assertEqual(plain, formula_svg(wrapped, '#302d29'))


if __name__ == '__main__':
    unittest.main()
