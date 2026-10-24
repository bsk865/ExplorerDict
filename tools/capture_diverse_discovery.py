"""Render real public subscriptions and a local selection-to-translation handoff."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont, QFontDatabase
from app.native.service import NativeService
from app.native.window import NativeWindow
from app.workspace_bridge import send_translation


def main():
    output = ROOT/'docs/screenshots/diverse-discovery-20261024'
    output.mkdir(parents=True, exist_ok=True)
    evidence = ROOT/'artifacts/diverse-discovery-20261024'
    evidence.mkdir(parents=True, exist_ok=True)
    application = QApplication([])
    application.setStyle('Fusion')
    font_id = QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
    application.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0], 10))
    with tempfile.TemporaryDirectory(prefix='public-discovery-') as directory:
        os.environ['EXPLORER_DICT_DATA_DIR'] = directory
        os.environ['EXPLORER_DICT_STORAGE_CONFIG'] = str(Path(directory)/'location.json')
        service = NativeService(Path(directory))
        prefs = service.discovery.preferences()
        prefs.update(topics=['机器人', '人工智能'], personal=False)
        service.discovery.save_preferences(prefs)
        state = service.discovery.refresh()
        rows = service.discovery.ranked()
        report = dict(items=len(rows), platforms=len({r['platform'] for r in rows}), errors=state['errors'],
                      source_health=state['source_health'], first_ten=[dict(title=r['title'], source=r['source'], platform=r['platform'], url=r['url']) for r in rows[:10]],
                      covers=sum(bool(r.get('cover_file')) for r in rows))
        (evidence/'public-sources.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        with patch('app.native.topbar.TopBar.check_capture', lambda self: None):
            window = NativeWindow(service, discovery_updates=False)
            window.show()
            def settle():
                deadline = time.monotonic()+15
                while time.monotonic() < deadline:
                    application.processEvents()
                    if not window.navigation_busy and not window.tasks.pending:
                        for _ in range(5): application.processEvents()
                        return
                    time.sleep(.01)
                raise RuntimeError('Navigation did not settle')
            settle()
            window.grab().save(str(output/'discovery.png'))
            page = window.stack.currentWidget()
            page.show_preferences()
            settle()
            page.stack.currentWidget().verticalScrollBar().setValue(650)
            settle()
            window.grab().save(str(output/'sources.png'))
            window.resize(900, 620)
            settle()
            page.stack.currentWidget().verticalScrollBar().setValue(650)
            settle()
            window.grab().save(str(output/'sources-narrow.png'))
            window.open_translation('Current translation draft', '原有草稿')
            settle()
            send_translation(service.store.directory, 'Robots can learn from demonstrations.', '浮窗选中文字 · 演示')
            window.receive_handoff()
            settle()
            window.grab().save(str(output/'translation-handoff.png'))
            window.close()
            settle()
            window.close()
            service.close()
    print(json.dumps({k:report[k] for k in ('items', 'platforms', 'covers', 'errors')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
