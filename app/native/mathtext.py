"""Local MathML rendering with source ranges retained for annotation mapping."""
from functools import lru_cache
from html.parser import HTMLParser
import hashlib

from bs4 import BeautifulSoup
from PySide6.QtCore import QRectF, QUrl
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


class MathRanges(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.source_offset = 0
        self.start = None
        self.ranges = []
        self.feed(html)

    def handle_data(self, data):
        self.source_offset += len(data)

    def handle_starttag(self, tag, attrs):
        if tag == 'math':
            self.start = self.source_offset

    def handle_endtag(self, tag):
        if tag == 'math' and self.start is not None:
            self.ranges.append((self.start, self.source_offset))
            self.start = None


@lru_cache(maxsize=128)
def formula_svg(mathml, color):
    import ziamath
    ziamath.config.svg2 = False
    tree = BeautifulSoup(mathml, 'html.parser')
    # Semantics carries one presentation expression plus alternate encodings.
    # Normalize only this rendering copy; annotation offsets use the source.
    for semantics in reversed(tree.find_all('semantics')):
        expression = next((child for child in semantics.find_all(recursive=False)
                           if child.name not in ('annotation', 'annotation-xml')), None)
        if expression is None:
            raise ValueError('MathML has no presentation expression')
        semantics.replace_with(expression.extract())
    tree.math['mathcolor'] = color
    return ziamath.Math(str(tree.math), size=20).svg().encode('utf-8')


def prepare_math(html, document, color='#302d29', sources=None):
    html = html.replace('\r\n', '\n').replace('\r', '\n')
    if '<math' not in html.lower():
        return html, {}
    ranges = MathRanges(html).ranges
    soup = BeautifulSoup(html, 'html.parser')
    formulas = soup.find_all('math')
    if len(ranges) != len(formulas):
        return html, {}
    atoms = {}
    for formula, (start, end) in zip(formulas, ranges):
        if start == end:
            continue
        try:
            svg = formula_svg(str(formula), color)
            renderer = QSvgRenderer(svg)
            bounds = renderer.viewBoxF()
            if not renderer.isValid() or bounds.isEmpty() or bounds.width()*bounds.height() > 4_000_000:
                continue
            image = QImage(max(1, int(bounds.width()*2+1)), max(1, int(bounds.height()*2+1)), QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(0)
            painter = QPainter(image)
            try:
                renderer.render(painter, QRectF(0, 0, image.width(), image.height()))
            finally:
                painter.end()
            name = 'math:' + hashlib.sha256(svg).hexdigest()
            document.addResource(2, QUrl(name), image)
            if sources is not None:
                sources[name] = str(formula)
            replacement = soup.new_tag('img', src=name, width=str(round(bounds.width())), height=str(round(bounds.height())))
            replacement['style'] = 'vertical-align:middle'
            formula.replace_with(replacement)
            atoms[start] = end
        except (ValueError, KeyError, IndexError, TypeError, NotImplementedError):
            # Preserve the source when an unsupported expression cannot be rendered.
            continue
    return str(soup), atoms
