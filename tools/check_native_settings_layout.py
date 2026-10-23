"""Render native settings with isolated configuration and no model requests."""
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtGui import QFontDatabase, QFont
from app.native.settings import SettingsDialog
from app.native.service import NativeService
from app.native.widgets import stylesheet
from app import reading_preferences


def main():
    app = QApplication([])
    fid = QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
    app.setFont(QFont(QFontDatabase.applicationFontFamilies(fid)[0], 10))
    directory = Path(tempfile.mkdtemp(prefix='settings-layout-', dir=ROOT/'artifacts/native-checks'))
    os.environ['EXPLORER_DICT_STORAGE_CONFIG'] = str(directory/'location.json')
    host = QWidget()
    host.service = NativeService(directory/'data')
    host.service.store.save_config({'base_url':'https://example.invalid/v1', 'model':'test-model', 'key':'fixture'})
    report = []
    try:
        for mode in ('light', 'dark'):
            reading_preferences.save(host.service.store.directory, {'palette':'clay', 'mode':mode})
            app.setStyleSheet(stylesheet({'palette':'clay', 'mode':mode}))
            dialog = SettingsDialog(host)
            dialog.show()
            assert not dialog.storage_dialog.isVisible()
            for width in (620, 460):
                dialog.resize(width, 620)
                for _ in range(4):
                    app.processEvents()
                assert dialog.width() == width
                assert dialog.scroll.horizontalScrollBar().maximum() == 0
                dialog.scroll.verticalScrollBar().setValue(0)
                dialog.grab().save(str(directory/f'{mode}-{width}-top.png'))
                dialog.scroll.verticalScrollBar().setValue(dialog.scroll.verticalScrollBar().maximum())
                dialog.grab().save(str(directory/f'{mode}-{width}-budget.png'))
                report.append({'mode':mode, 'width':width})
            dialog.storage_dialog.show()
            app.processEvents()
            dialog.storage_dialog.grab().save(str(directory/f'{mode}-storage.png'))
            dialog.storage_dialog.close()
            dialog.close()
    finally:
        host.service.close()
    result = {'passed':True, 'directory':str(directory), 'cases':report}
    (directory/'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
