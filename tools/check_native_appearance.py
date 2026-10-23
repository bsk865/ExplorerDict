"""Render all original appearance assets in a private settings directory."""
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFontDatabase, QFont
from app.native.appearance import AppearanceDialog, ThemePreview, THEMES
from app.reading_preferences import load


def main():
    app = QApplication([])
    fid = QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
    app.setFont(QFont(QFontDatabase.applicationFontFamilies(fid)[0], 10))
    directory = Path(tempfile.mkdtemp(prefix='appearance-', dir=ROOT/'artifacts/native-checks'))
    dialog = AppearanceDialog(directory)
    dialog.show()
    results = []
    for key, _ in THEMES:
        dialog.choices[key].click()
        for mode in ('light', 'dark'):
            if dialog.value['mode'] != mode:
                dialog.mode.click()
            for width, columns in ((620, 3), (400, 2)):
                dialog.resize(width, 420)
                for _ in range(4):
                    app.processEvents()
                assert dialog.columns == columns
                assert load(directory) == {'palette':key, 'mode':mode}
                for control in dialog.choices.values():
                    assert not control.findChild(ThemePreview).pixmap.isNull()
                    assert control.geometry().right() < dialog.width()
                    assert control.geometry().bottom() < dialog.mode.geometry().top()
                dialog.grab().save(str(directory/f'{key}-{mode}-{width}.png'))
                results.append({'palette':key, 'mode':mode, 'width':width})
    dialog.close()
    report = {'passed':True, 'directory':str(directory), 'cases':results}
    (directory/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed':True, 'directory':str(directory), 'cases':len(results)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
