"""Render management filters and long content at desktop/compact widths offscreen."""
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtWidgets import QApplication, QWidget, QPushButton
from PySide6.QtGui import QFontDatabase, QFont
from app.native.content import ContentPage
from app.native.dialogs import NoteForm
from app.native.widgets import stylesheet


def main():
    app = QApplication([])
    font_id = QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
    app.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0], 10))
    host = QWidget()
    for name in ('edit_note', 'edit_task', 'open_article', 'delete', 'refresh', 'resume_draft', 'import_material'):
        setattr(host, name, Mock())
    articles = [{'id':'a', 'title':'阅读与思考：如何把理解转化为自己的知识'}, {'id':'b', 'title':'第二篇阅读材料'}]
    notes = [{'id':'n1', 'article_id':'a', 'article_title':articles[0]['title'],
              'title':'读后心得：理解论点，也要核对支撑论点的原文依据',
              'content':'先把作者的主要观点写下来，再核对证据。\n保留仍未解决的问题，在下次阅读时继续确认。',
              'quote':'阅读让想法逐渐清晰。'},
             {'id':'n2', 'article_id':'b', 'article_title':articles[1]['title'], 'title':'小探整理的阅读要点',
              'origin':'assistant_confirmed', 'content':'用自己的话整理阅读内容，再返回文章检查每一处引用。' * 5}]
    directory = Path(tempfile.mkdtemp(prefix='content-layout-', dir=ROOT/'artifacts/native-checks'))
    report = []
    for mode in ('light', 'dark'):
        host.snapshot = {'preferences':{'palette':'clay', 'mode':mode}}
        app.setStyleSheet(stylesheet(host.snapshot['preferences']))
        page = ContentPage(host, {'articles':articles, 'notes':notes}, 'notes')
        page.setObjectName('page')
        page.show()
        for width in (1050, 600, 420):
            page.resize(width, 650)
            for _ in range(6):
                app.processEvents()
            assert page.width() == width
            assert page.scroll.horizontalScrollBar().maximum() == 0
            for _, row in page.rows:
                for control in row.findChildren(QPushButton):
                    assert control.mapTo(row, control.rect().bottomRight()).x() < row.width()
            page.grab().save(str(directory/f'{mode}-{width}.png'))
            report.append({'mode':mode, 'width':width, 'rows':len(page.rows)})
        form = NoteForm(notes[0], articles, lambda _: None)
        form.show()
        app.processEvents()
        form.grab().save(str(directory/f'{mode}-editor.png'))
        form.close()
        page.close()
    result = {'passed':True, 'directory':str(directory), 'cases':report}
    (directory/'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
