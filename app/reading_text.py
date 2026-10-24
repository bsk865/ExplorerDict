"""One source-text view for reading, exact quotes and local retrieval.

HTML and original block fields stay untouched; offsets refer to this text view.
Keep parsing stable so existing reading annotations retain their positions.
"""
from html.parser import HTMLParser


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def block_text(block):
    source = (block.get('html') or block.get('text', '')).replace('\r\n', '\n').replace('\r', '\n')
    if not block.get('html'):
        return source
    parser = _Text()
    parser.feed(source)
    return ''.join(parser.parts)


def article_text(article):
    return '\n\n'.join(block_text(block) if block['type'] == 'text' else block.get('caption', '')
                       for block in article['blocks'])
