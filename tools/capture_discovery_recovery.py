"""Read original public feeds and capture their current status in an isolated app."""
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


def main():
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtWidgets import QApplication, QLabel
    from app.native.service import NativeService
    from app.native.window import NativeWindow
    from app.native.discover import refresh_message
    reused = len(sys.argv) > 1
    directory = Path(sys.argv[1]).resolve(strict=True) if reused else Path(tempfile.mkdtemp(
        prefix='discovery-recovery-', dir=ROOT/'artifacts/native-checks'))
    os.environ['EXPLORER_DICT_DATA_DIR'] = str(directory)
    os.environ['EXPLORER_DICT_STORAGE_CONFIG'] = str(directory/'location.json')
    output = ROOT/'docs/screenshots/discovery-recovery-20261024'
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    app.setStyle('Fusion')
    font_id = QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
    app.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0], 10))
    service = NativeService(directory)
    service.workspace.map_preloader.close()
    preferences = service.discovery.preferences()
    preferences.update(personal=False, automatic=False)
    service.discovery.save_preferences(preferences)
    state = service.discovery.state() if reused else service.discovery.refresh()
    rows = service.discovery.ranked()
    report = dict(refresh_counts=state['refresh_counts'], source_health=state['source_health'],
                  message=refresh_message(state), items=len(rows),
                  platforms=len({row['platform'] for row in rows}),
                  covers=sum(bool(row.get('cover_file')) for row in rows),
                  first_ten=[dict(title=row['title'], source=row['source'], platform=row['platform']) for row in rows[:10]],
                  errors=state['errors'], private_history_used=False, model_requests=0)
    with patch('app.native.topbar.TopBar.check_capture', lambda self: None):
        window = NativeWindow(service, discovery_updates=False)
        window.discovery.message = report['message']
        window.resize(1440, 900)
        window.show()
        def settle():
            deadline = time.monotonic()+15
            while time.monotonic() < deadline:
                app.processEvents()
                if not window.navigation_busy and not window.tasks.pending:
                    for _ in range(12): app.processEvents()
                    return
                time.sleep(.01)
            raise RuntimeError('Navigation did not settle')
        settle()
        assert window.grab().save(str(output/'discovery.png'))
        page = window.stack.currentWidget()
        page.show_preferences()
        settle()
        settings = page.stack.currentWidget()
        first_source = next(label for label in settings.findChildren(QLabel) if label.text().startswith('IT之家 ·'))
        for width, height, suffix in ((1440, 900, ''), (900, 620, '-narrow')):
            window.resize(width, height)
            settle()
            settings.ensureWidgetVisible(first_source.parentWidget(), 0, 0)
            settle()
            assert settings.horizontalScrollBar().maximum() == 0
            assert window.grab().save(str(output/('sources'+suffix+'.png')))
        window.close()
        settle()
        window.close()
    service.close()
    report['reused_saved_refresh'] = reused
    (directory/('view-report.json' if reused else 'verification.json')).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(dict(directory=str(directory), **{key:report[key] for key in
        ('refresh_counts', 'items', 'platforms', 'covers', 'errors')}), ensure_ascii=False))


if __name__ == '__main__':
    main()
