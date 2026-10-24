"""Render a saved answer about a public table without making another model request."""
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
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFont, QFontDatabase
    from app.native.service import NativeService
    from app.native.window import NativeWindow
    from app import reading_preferences
    source = Path(sys.argv[1]).resolve(strict=True)
    report = json.loads((source/'image-answer.json').read_text(encoding='utf-8'))
    assert report['passed'] and report['fresh_session'] and report['all_images_within_selection']
    assert report['source'] == 'https://arxiv.org/abs/2610.31121'
    directory = Path(tempfile.mkdtemp(prefix='image-answer-view-', dir=ROOT/'artifacts/native-checks'))
    with closing(sqlite3.connect((source/'reading_workspace.sqlite3').as_uri()+'?mode=ro', uri=True)) as old:
        with closing(sqlite3.connect(directory/'reading_workspace.sqlite3')) as new:
            old.backup(new)
    os.environ['EXPLORER_DICT_DATA_DIR'] = str(directory)
    os.environ['EXPLORER_DICT_STORAGE_CONFIG'] = str(directory/'location.json')
    app = QApplication([])
    app.setStyle('Fusion')
    font = QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
    family = QFontDatabase.applicationFontFamilies(font)[0]
    QFont.insertSubstitution('Microsoft YaHei UI', family)
    app.setFont(QFont(family, 10))
    service = NativeService(directory)
    service.workspace.map_preloader.close()
    service.workspace.rag.request = Mock()
    service.workspace.client = Mock(side_effect=AssertionError('No model requests while rendering'))
    reading_preferences.save(directory, dict(palette='forest', mode='light'))
    article = service.store.article(report['answer']['evidence'][0]['article_id'])
    obj = next(obj for obj in article['objects'] if obj.get('region') == report['region'])
    output = ROOT/'docs/screenshots/image-reading-20261024'
    output.mkdir(parents=True, exist_ok=True)
    with patch('app.native.topbar.TopBar.check_capture', lambda self: None):
        window = NativeWindow(service, discovery_updates=False)
        window.resize(1440, 900)
        window.show()
        def settle():
            deadline = time.monotonic()+10
            while window.navigation_busy or window.tasks.pending:
                app.processEvents()
                if time.monotonic() > deadline:
                    raise RuntimeError('View did not settle')
                time.sleep(.01)
            for _ in range(10): app.processEvents()
        settle()
        window.open_article(article['id'], object_id=obj['id'])
        settle()
        reader = window.stack.currentWidget()
        assert reader.chat.controller.object_id == obj['id']
        assert reader.chat.controller.session['messages'][-1]['content'] == report['answer']['content']
        reader.splitter.setSizes([770, 420])
        reader.chat.messages.verticalScrollBar().setValue(0)
        app.processEvents()
        assert window.grab().save(str(output/'table-answer.png'))
        window.resize(900, 620)
        settle()
        assert reader.chat.verticalScrollBar().maximum() == 0
        assert reader.chat.viewport().rect().contains(reader.chat.send.mapTo(reader.chat.viewport(), reader.chat.send.rect().bottomRight()))
        reader.chat.messages.verticalScrollBar().setValue(reader.chat.messages.verticalScrollBar().maximum())
        app.processEvents()
        assert window.grab().save(str(output/'table-answer-narrow.png'))
        service.workspace.client.assert_not_called()
        window.close()
        settle()
    print(directory)


if __name__ == '__main__':
    main()
