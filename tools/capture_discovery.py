"""Capture the native feed using public sources and an isolated local store."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont, QFontDatabase
from app.native.service import NativeService
from app.native.window import NativeWindow


def main():
    app = QApplication([])
    fid = QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
    family = QFontDatabase.applicationFontFamilies(fid)[0]
    QFont.insertSubstitution('Microsoft YaHei UI', family)
    app.setFont(QFont(family, 10))
    output = ROOT/'docs/screenshots/discovery-20261024'
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='public-discovery-') as directory:
        os.environ['EXPLORER_DICT_DATA_DIR'] = directory
        os.environ['EXPLORER_DICT_STORAGE_CONFIG'] = str(Path(directory)/'location.json')
        service = NativeService(Path(directory))
        state = service.discovery.refresh()
        print('Public items:', len(state['items']), 'covers:', sum(bool(row.get('cover_file')) for row in state['items']))
        with patch('app.native.topbar.TopBar.check_capture', lambda self:None):
            window = NativeWindow(service, discovery_updates=False)
            window.resize(1380,880)
            window.show()
            def settle():
                end = time.monotonic()+10
                while time.monotonic()<end:
                    app.processEvents()
                    if not window.navigation_busy and not window.tasks.pending:
                        for _ in range(12):app.processEvents()
                        return
                    time.sleep(.01)
                raise RuntimeError('Window did not settle')
            settle()
            page = window.stack.currentWidget()
            page.tabs['news'].click()
            settle()
            window.grab().save(str(output/'news.png'))
            page.tabs['paper'].click()
            settle()
            window.grab().save(str(output/'papers.png'))
            page.show_preferences()
            window.resize(900,620)
            settle()
            window.grab().save(str(output/'preferences.png'))
            window.close()
            settle()
            window.close()
    print(output)


if __name__ == '__main__':
    main()
