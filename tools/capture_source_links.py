"""Download one public original, then view both link paths without model calls."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'


def main():
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFontDatabase, QFont
    from app.discovery import fetch
    from app.native.service import NativeService
    from app.native.window import NativeWindow
    from app.reading_preferences import save
    import pymupdf

    directory = Path(tempfile.mkdtemp(prefix='source-links-', dir=ROOT/'artifacts/native-checks'))
    os.environ['EXPLORER_DICT_DATA_DIR'] = str(directory)
    os.environ['EXPLORER_DICT_STORAGE_CONFIG'] = str(directory/'location.json')
    url = 'https://journals.plos.org/plosone/article/file?id=10.1371/journal.pone.0361484&type=printable'
    response = fetch(url, 28_000_000)
    raw, final, _ = response
    assert raw.lstrip().startswith(b'%PDF-')
    (directory/'public-paper.pdf').write_bytes(raw)
    with pymupdf.open(stream=raw, filetype='pdf') as original:
        page_count = len(original)
        final_text = original[-1].get_text(sort=True).strip()
    loader = Mock(return_value=response)
    service = NativeService(directory)
    service.workspace.map_preloader.close()
    service.workspace.rag.request = Mock()
    service.workspace.client = Mock(side_effect=AssertionError('No model request while viewing'))
    try:
        with patch('app.discovery.fetch', loader):
            article = service.extract(url)
        assert article['source_stats']['pages'] == page_count
        assert service.store.asset(article['source_document']['asset'])[1] == raw
        assert any(block.get('page') == page_count and block.get('text', '').strip()
                   for block in article['blocks'] if block['type'] == 'text')
        item = service.discovery.add_link(url, loader)
        assert item['content_kind'] == 'paper' and not item['summary']
        assert len(service.store.summaries()) == 1
        collected = service.discovery.import_item(item['id'], loader)
        assert collected['id'] == article['id']
        assert service.store.asset(collected['source_document']['asset'])[1] == raw
        save(directory, {'palette': 'forest', 'mode': 'light'})

        app = QApplication([])
        app.setStyle('Fusion')
        font_id = QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
        family = QFontDatabase.applicationFontFamilies(font_id)[0]
        QFont.insertSubstitution('Microsoft YaHei UI', family)
        app.setFont(QFont(family, 10))
        output = ROOT/'docs/screenshots/source-links-20261025'
        output.mkdir(parents=True, exist_ok=True)
        with patch('app.native.topbar.TopBar.check_capture', lambda self: None):
            window = NativeWindow(service, discovery_updates=False)
            window.resize(1440, 900)
            window.show()
            def settle():
                deadline = time.monotonic() + 15
                while window.navigation_busy or window.tasks.pending:
                    app.processEvents()
                    if time.monotonic() > deadline:
                        raise RuntimeError('Window did not settle')
                    time.sleep(.01)
                for _ in range(10):
                    app.processEvents()
            settle()
            window.stack.currentWidget().show_detail(item['id'])
            settle()
            assert window.grab().save(str(output/'collected-pdf.png'))
            window.open_article(article['id'])
            settle()
            reader = window.stack.currentWidget()
            assert window.grab().save(str(output/'pdf-reader.png'))
            reader.translate()
            settle()
            assert reader.translation_page.controller.scope == 'article:' + article['id']
            assert not reader.translation_page.result.toPlainText()
            assert 'translate' not in window.nav
            assert window.grab().save(str(output/'article-tool.png'))
            window.close()
            settle()
        service.workspace.client.assert_not_called()
        report = dict(passed=True, url=url, resolved_url=final, bytes=len(raw),
                      sha256=hashlib.sha256(raw).hexdigest(), pages=page_count,
                      characters=len(article['text']), final_page_characters=len(final_text),
                      network_downloads=1, response_reused_for_both_paths=True,
                      model_requests=0, same_article=True, original_preserved=True,
                      screenshots=str(output.relative_to(ROOT)))
        (directory/'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8', newline='\n')
        print(json.dumps(dict(directory=str(directory), **report), ensure_ascii=False))
    finally:
        service.close()


if __name__ == '__main__':
    main()
