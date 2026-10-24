"""Render public-paper import and an optional recorded public translation request."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFontDatabase, QFont
from app.discovery import entry, fetch
from app.native.service import NativeService
from app.native.window import NativeWindow


def main():
    app = QApplication([])
    app.setStyle('Fusion')
    font = QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
    family = QFontDatabase.applicationFontFamilies(font)[0]
    QFont.insertSubstitution('Microsoft YaHei UI', family)
    app.setFont(QFont(family, 10))
    output = ROOT / 'docs/screenshots/fulltext-translation-20261024'
    output.mkdir(parents=True, exist_ok=True)
    local = ROOT / 'artifacts/fulltext-translation-20261024'
    local.mkdir(parents=True, exist_ok=True)
    pdf = local / 'public-paper.pdf'
    if not pdf.is_file():
        pdf.write_bytes(fetch('https://arxiv.org/pdf/2610.31121', 28_000_000)[0])
    with tempfile.TemporaryDirectory(prefix='public-fulltext-') as directory:
        os.environ['EXPLORER_DICT_DATA_DIR'] = directory
        os.environ['EXPLORER_DICT_STORAGE_CONFIG'] = str(Path(directory) / 'location.json')
        service = NativeService(Path(directory))
        item = entry('https://arxiv.org/abs/2610.31121',
                     'RGraph-Eval: Task-Grounded Scene Graphs for Evaluating Robot Video Generation', 'arXiv', 'paper')
        item['source_id'] = 'arxiv-robot'
        import pymupdf
        with pymupdf.open(pdf) as document:
            item['summary'] = document[0].get_text()[:1000]
        service.store.put('settings', 'discovery-state', {'items':[item], 'updated':0})
        article = service.discovery.import_item(item['id'], lambda url, limit:(pdf.read_bytes(), url, 'utf-8'))
        print('Imported public PDF: pages=', article['source_stats']['pages'], 'text chars=', len(article['text']), 'original bytes=', article['source_document']['bytes'])
        with patch('app.native.topbar.TopBar.check_capture', lambda self:None):
            window = NativeWindow(service, discovery_updates=False)
            window.show()
            def settle():
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    app.processEvents()
                    if not window.tasks.pending and not window.navigation_busy:
                        for _ in range(12):
                            app.processEvents()
                        return
                    time.sleep(.01)
                raise RuntimeError('Tasks did not settle')
            settle()
            window.stack.currentWidget().show_detail(item['id'])
            settle()
            window.grab().save(str(output / 'fulltext-detail.png'))
            window.open_article(article['id'])
            settle()
            window.grab().save(str(output / 'paper-reader.png'))
            recorded = local / 'live-translation.json'
            if recorded.is_file():
                live = json.loads(recorded.read_text(encoding='utf-8'))
                window.translation.state.update(live['input'], result=live['result']['text'], complete=live['result']['complete'], origin='公开短句翻译')
                window.translation.flush()
            window.navigate('translate')
            settle()
            window.grab().save(str(output / 'translation.png'))
            window.resize(900, 620)
            settle()
            window.grab().save(str(output / 'translation-narrow.png'))
            window.close()
            settle()
            window.close()
    print(output)


if __name__ == '__main__':
    main()
