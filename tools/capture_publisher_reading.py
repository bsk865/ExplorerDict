"""View an isolated, previously downloaded public paper without model requests."""
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'


def main():
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtWidgets import QApplication
    from app.native.service import NativeService
    from app.native.window import NativeWindow

    directory = Path(sys.argv[1]).resolve(strict=True)
    evidence = json.loads((directory/'verification.json').read_text(encoding='utf-8'))
    assert evidence['passed'] and not evidence['user_data_accessed']
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
    service.workspace.client = Mock(side_effect=AssertionError('No model requests'))
    state = service.discovery.state()
    state['items'][0]['source_id'] = 'plos'
    service.store.put('settings', 'discovery-state', state)
    output = ROOT/'docs/screenshots/publisher-reading-20261024'
    output.mkdir(parents=True, exist_ok=True)
    report = dict(passed=False, model_requests=0, user_data_accessed=False)
    with patch('app.native.topbar.TopBar.check_capture', lambda self:None):
        window = NativeWindow(service, discovery_updates=False)
        window.resize(900, 620)
        window.show()
        def settle():
            deadline = time.monotonic()+10
            while window.navigation_busy or window.tasks.pending:
                app.processEvents()
                if time.monotonic() > deadline:
                    raise RuntimeError('Window did not settle')
                time.sleep(.01)
            for _ in range(10):
                app.processEvents()
        settle()
        page = window.stack.currentWidget()
        item = service.discovery.state()['items'][0]
        page.show_detail(item['id'])
        settle()
        detail = page.stack.currentWidget()
        assert page.detail_id == item['id'] and detail is not page.feed
        window.discovery.message = '全文已加入资料库，可打开阅读和问答。'
        window.discovery.changed.emit()
        settle()
        assert detail is page.stack.currentWidget()
        assert window.grab().save(str(output/'imported-paper.png'))
        window.open_article(evidence['article_id'])
        settle()
        reader = window.stack.currentWidget()
        assert reader.text.toPlainText().strip()
        assert reader.text.horizontalScrollBar().maximum() == 0
        assert window.grab().save(str(output/'reading.png'))
        reader.translate()
        settle()
        assert len(reader.translation_page.original.toPlainText()) > 39000
        assert reader.translation_page.controller.scope == 'article:'+evidence['article_id']
        assert window.grab().save(str(output/'translation.png'))
        assert app.activeModalWidget() is None
        service.workspace.client.assert_not_called()
        report.update(passed=True, detail_preserved=True, text_available=True,
            article_translation_ready=True, no_modal=True, width=900, height=620)
        window.close()
        settle()
    (directory/'ui-verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
