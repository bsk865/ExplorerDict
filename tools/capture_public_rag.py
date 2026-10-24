"""Render a copy of the saved public-paper answers without making API requests."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'


def main():
    source = Path(sys.argv[1]).resolve(strict=True)
    report = json.loads((source/'verification.json').read_text(encoding='utf-8'))
    assert report['passed'] and report['source'] == 'https://arxiv.org/abs/2610.31121'
    assert all(case['supporting_statements'] for case in report['cases'])
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFont, QFontDatabase
    from app.native.service import NativeService
    from app.native.window import NativeWindow
    from app import reading_preferences, capture_bridge
    parent = ROOT/'artifacts/native-checks'
    directory = Path(tempfile.mkdtemp(prefix='public-rag-view-', dir=parent))
    with closing(sqlite3.connect((source/'reading_workspace.sqlite3').as_uri()+'?mode=ro', uri=True)) as old:
        with closing(sqlite3.connect(directory/'reading_workspace.sqlite3')) as new:
            old.backup(new)
    os.environ['EXPLORER_DICT_DATA_DIR'] = str(directory)
    os.environ['EXPLORER_DICT_STORAGE_CONFIG'] = str(directory/'location.json')
    application = QApplication([])
    application.setStyle('Fusion')
    font_id = QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
    application.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0], 10))
    service = NativeService(directory)
    service.workspace.map_preloader.close()
    reading_preferences.save(directory, dict(palette='forest', mode='light'))
    session = service.store.get('sessions', 'library:all')
    # The old fixture predates automatic removal of the submitted draft.
    if session.get('draft') == session['messages'][-2]['content']:
        session['draft'] = ''
        service.store.put('sessions', 'library:all', session)
    output = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else ROOT/'docs/screenshots/source-consistency-20261024'
    output.mkdir(parents=True, exist_ok=True)
    with patch('app.native.topbar.TopBar.check_capture', lambda self: None):
        window = NativeWindow(service, discovery_updates=False)
        window.resize(1440, 900)
        window.show()
        def settle():
            deadline = time.monotonic()+15
            while time.monotonic() < deadline:
                application.processEvents()
                if not window.navigation_busy and not window.tasks.pending:
                    for _ in range(10): application.processEvents()
                    return
                time.sleep(.01)
            raise RuntimeError('Native view did not settle')
        settle()
        window.topbar.render_capture(capture_bridge.status())
        window.navigate('library')
        settle()
        assert len(window.library.session['messages']) == 4
        page = window.stack.currentWidget()
        page.transcript.verticalScrollBar().setValue(0)
        application.processEvents()
        assert window.grab().save(str(output/'public-paper-rag.png'))
        window.resize(900, 620)
        application.processEvents()
        assert window.grab().save(str(output/'public-paper-rag-narrow.png'))
        metrics = {'library_narrow_answer_height': page.transcript.viewport().height()}
        messages = window.library.session['messages']
        article_id = service.store.summaries()[0]['id']
        window.open_article(article_id)
        settle()
        reader = window.stack.currentWidget()
        reader.set_assistant_visible(True)
        reader.chat.controller.session['messages'] = messages
        reader.chat.render()
        for width, height, suffix in ((1440, 900, ''), (900, 620, '-narrow')):
            window.resize(width, height)
            for _ in range(10): application.processEvents()
            reader.chat.messages.verticalScrollBar().setValue(reader.chat.messages.verticalScrollBar().maximum())
            assert window.grab().save(str(output/('public-paper-reader'+suffix+'.png')))
            if suffix:
                metrics['reader_narrow_answer_height'] = reader.chat.messages.viewport().height()
                chat = reader.chat
                metrics['reader_panel'] = dict(viewport=chat.viewport().height(), body=chat.widget().height(),
                    minimum=chat.widget().minimumHeight(), hint=chat.widget().minimumSizeHint().height(),
                    minimum_layout=chat.widget().layout().minimumSize().height(),
                    height_for_width=chat.widget().layout().totalHeightForWidth(chat.viewport().width()),
                    send_y=chat.send.mapTo(chat.viewport(), chat.send.rect().topLeft()).y(),
                    send_height=chat.send.height(), outer_scroll=chat.verticalScrollBar().maximum())
        (directory/'view-metrics.json').write_text(json.dumps(metrics, indent=2), encoding='utf-8')
        print(json.dumps(metrics))
        window.close()
        application.processEvents()
    service.close()
    print('Captured saved public answers; no new model requests.')


if __name__ == '__main__':
    main()
