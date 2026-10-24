"""View saved cross-source answers and follow each citation, without API calls."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'


def main():
    source = Path(sys.argv[1]).resolve(strict=True)
    report = json.loads((source/'verification.json').read_text(encoding='utf-8'))
    assert report['passed'] and report['public_material_only'] and report['cases'][0]['passed']
    from PySide6.QtCore import QTimer, QPoint
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtWidgets import QApplication, QPushButton
    from app.native.service import NativeService
    from app.native.window import NativeWindow
    from app.reading_preferences import save

    directory = Path(tempfile.mkdtemp(prefix='multi-public-view-', dir=ROOT/'artifacts/native-checks'))
    with closing(sqlite3.connect((source/'reading_workspace.sqlite3').as_uri()+'?mode=ro', uri=True)) as old:
        with closing(sqlite3.connect(directory/'reading_workspace.sqlite3')) as new:
            old.backup(new)
    os.environ['EXPLORER_DICT_DATA_DIR'] = str(directory)
    os.environ['EXPLORER_DICT_STORAGE_CONFIG'] = str(directory/'location.json')
    app = QApplication([])
    app.setStyle('Fusion')
    font_id = QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
    family = QFontDatabase.applicationFontFamilies(font_id)[0]
    QFont.insertSubstitution('Microsoft YaHei UI', family)
    app.setFont(QFont(family, 10))
    service = NativeService(directory)
    service.workspace.map_preloader.close()
    service.workspace.rag.request = Mock()
    service.workspace.client = Mock(side_effect=AssertionError('No model requests while viewing'))
    save(directory, dict(palette='forest', mode='light'))
    output = ROOT/'docs/screenshots/multi-public-rag-20261024'
    output.mkdir(parents=True, exist_ok=True)
    metrics = dict(passed=False, new_model_requests=0, citations=[])
    with patch('app.native.topbar.TopBar.check_capture', lambda self:None):
        window = NativeWindow(service, discovery_updates=False)
        window.show()
        def settle():
            deadline = time.monotonic()+15
            while window.navigation_busy or window.tasks.pending:
                app.processEvents()
                if time.monotonic() > deadline:
                    raise RuntimeError('Window did not settle')
                time.sleep(.01)
            # Allow queued layout/paint and the 16 ms image-sizing timer to finish.
            deadline = time.monotonic()+.2
            while time.monotonic() < deadline:
                app.processEvents()
                time.sleep(.01)
        settle()
        window.navigate('library')
        settle()
        page = window.stack.currentWidget()
        assert window.library.session['messages'][-1]['content'] == report['cases'][0]['answer']
        for width, height, suffix in ((1440,900,''),(900,620,'-narrow')):
            window.resize(width, height)
            settle()
            page.transcript.verticalScrollBar().setValue(page.transcript.verticalScrollBar().maximum())
            assert page.transcript.horizontalScrollBar().maximum() == 0
            assert window.grab().save(str(output/('answers'+suffix+'.png')))
        for ordinal, hit in enumerate(report['cases'][0]['evidence']):
            errors = []
            def locate():
                dialog = app.activeModalWidget()
                try:
                    assert dialog is not None
                    action = next(b for b in dialog.findChildren(QPushButton) if b.text() == '在原文中定位')
                    action.click()
                except Exception as exc:
                    errors.append(str(exc))
                    if dialog:
                        dialog.reject()
            QTimer.singleShot(20, locate)
            page.show_evidence(hit)
            assert not errors, errors
            settle()
            assert window.article_id == hit['article_id']
            reader = window.stack.currentWidget()
            assert hit['block'] in reader.text.anchors
            # Navigation consumes article_block when handing it to ReaderPage.
            assert reader.focus_passage == hit
            selected = reader.text.selected_segments()
            assert selected == [{k:hit[k] for k in ('block','start','end','quote')}]
            assert reader.text.viewport().rect().intersects(reader.text.cursorRect())
            assert reader.text.textCursor().block().layout().lineCount() > 0
            assert reader.text.document().documentLayout().blockBoundingRect(reader.text.textCursor().block()).height() > 0
            assert app.activeModalWidget() is None
            assert window.grab().save(str(output/('source-'+str(ordinal+1)+'.png')))
            metrics['citations'].append(dict(article=hit['article_id'], block=hit['block'], page=hit.get('page'), located=True,
                viewport=[reader.text.viewport().width(), reader.text.viewport().height()],
                cursor=[reader.text.cursorRect().x(), reader.text.cursorRect().y(), reader.text.cursorRect().height()],
                scroll=reader.text.verticalScrollBar().value(),
                cursor_text=reader.text.textCursor().block().text()[:180],
                visible_text=reader.text.cursorForPosition(QPoint(20,20)).block().text()[:180],
                rect=str(reader.text.document().documentLayout().blockBoundingRect(reader.text.textCursor().block())),
                block_visible=reader.text.textCursor().block().isVisible(),
                lines=reader.text.textCursor().block().layout().lineCount(),
                exact_quote_selected=True))
            window.navigate('library')
            settle()
            page = window.stack.currentWidget()
            assert window.library.session['messages'][-1]['content'] == report['cases'][0]['answer']
        service.workspace.client.assert_not_called()
        metrics['passed'] = len(metrics['citations']) == 3
        window.close()
        settle()
    (directory/'view-verification.json').write_text(json.dumps(metrics, indent=2), encoding='utf-8')
    print(json.dumps(dict(directory=str(directory), **metrics)))


if __name__ == '__main__':
    main()
