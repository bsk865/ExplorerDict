"""View a live refresh against an existing public-feed cache, without user history."""
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
    from PySide6.QtGui import QFontDatabase, QFont
    from PySide6.QtWidgets import QApplication
    from app.workspace_store import WorkspaceStore
    from app.native.service import NativeService
    from app.native.window import NativeWindow
    from app.reading_preferences import save

    cache = WorkspaceStore(Path(sys.argv[1]).resolve(strict=True))
    directory = Path(tempfile.mkdtemp(prefix='discovery-updates-', dir=ROOT/'artifacts/native-checks'))
    os.environ['EXPLORER_DICT_DATA_DIR'] = str(directory)
    os.environ['EXPLORER_DICT_STORAGE_CONFIG'] = str(directory/'location.json')
    service = NativeService(directory)
    service.store.put('settings', 'discovery-state', cache.get('settings', 'discovery-state'))
    preferences = service.discovery.preferences()
    preferences.update(personal=False, automatic=False)
    service.discovery.save_preferences(preferences)
    service.workspace.map_preloader.close()
    service.workspace.rag.request = Mock()
    service.workspace.client = Mock(side_effect=AssertionError('No model requests in feed refresh'))
    save(directory, {'palette': 'forest', 'mode': 'light'})
    app = QApplication([])
    app.setStyle('Fusion')
    font_id = QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
    family = QFontDatabase.applicationFontFamilies(font_id)[0]
    QFont.insertSubstitution('Microsoft YaHei UI', family)
    app.setFont(QFont(family, 10))
    output = ROOT/'docs/screenshots/discovery-updates-20261025'
    output.mkdir(parents=True, exist_ok=True)
    try:
        with patch('app.native.topbar.TopBar.check_capture', lambda self: None):
            window = NativeWindow(service, discovery_updates=False)
            window.resize(1440, 900)
            window.show()
            def settle():
                deadline = time.monotonic() + 55
                while window.navigation_busy or window.tasks.pending or window.discovery.busy:
                    app.processEvents()
                    if time.monotonic() > deadline:
                        raise RuntimeError('Discovery refresh has not finished')
                    time.sleep(.01)
                for _ in range(10):
                    app.processEvents()
            settle()
            if '--cached' in sys.argv:
                from app.native.discover import refresh_message
                window.discovery.message = refresh_message(service.discovery.state())
                window.discovery.changed.emit()
            else:
                window.discovery.refresh()
            settle()
            page = window.stack.currentWidget()
            state = service.discovery.state()
            counts = state['refresh_counts']
            assert all(key in counts for key in ('added', 'changed', 'unchanged'))
            assert len(state['refresh_changes']['added']) == counts['added']
            recommended = [row['id'] for row in service.discovery.ranked()[:20]]
            page.order.setCurrentIndex(page.order.findData('latest'))
            settle()
            latest = service.discovery.ranked(order='latest')
            assert window.grab().save(str(output/'latest.png'))
            if counts['added'] or counts['changed']:
                page.updates_button.click()
                settle()
                assert window.grab().save(str(output/'this-update.png'))
                page.updates_button.click()
            else:
                assert not page.updates_button.isVisible()
                assert '暂无新增或修改' in page.status.text()
            page.show_preferences()
            settle()
            assert window.grab().save(str(output/'sources.png'))
            report = dict(passed=True, counts=counts, health=state['source_health'],
                          message=window.discovery.message, items=len(state['items']),
                          platforms=len({row['platform'] for row in latest}),
                          order_saved=service.store.get('settings', 'discovery-view')['order'],
                          recommended_and_latest_differ=recommended != [row['id'] for row in latest[:20]],
                          private_history_used=False, model_requests=0, refreshed='--cached' not in sys.argv,
                          screenshots=str(output.relative_to(ROOT)))
            service.workspace.client.assert_not_called()
            window.close()
            settle()
        (directory/'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8', newline='\n')
        print(json.dumps(dict(directory=str(directory), **report), ensure_ascii=False))
    finally:
        service.close()


if __name__ == '__main__':
    main()
