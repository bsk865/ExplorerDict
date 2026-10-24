"""Show the saved synthetic plan and preferences without new model requests."""
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
sys.path.insert(0,str(ROOT))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'


def main():
    source = Path(sys.argv[1]).resolve(strict=True)
    report = json.loads((source/'verification.json').read_text(encoding='utf-8'))
    assert report['passed'] and report['synthetic_content_only']
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFontDatabase, QFont
    from app.native.service import NativeService
    from app.native.window import NativeWindow
    from app.native.advisor import AdvisorDialog
    from app.reading_preferences import save

    directory = Path(tempfile.mkdtemp(prefix='plan-conflicts-view-',dir=ROOT/'artifacts/native-checks'))
    with closing(sqlite3.connect((source/'before-undo.sqlite3').as_uri()+'?mode=ro',uri=True)) as old:
        with closing(sqlite3.connect(directory/'reading_workspace.sqlite3')) as new:
            old.backup(new)
    os.environ['EXPLORER_DICT_DATA_DIR'] = str(directory)
    os.environ['EXPLORER_DICT_STORAGE_CONFIG'] = str(directory/'location.json')
    app = QApplication([])
    app.setStyle('Fusion')
    font_id = QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
    family = QFontDatabase.applicationFontFamilies(font_id)[0]
    QFont.insertSubstitution('Microsoft YaHei UI',family)
    app.setFont(QFont(family,10))
    service = NativeService(directory)
    service.workspace.map_preloader.close()
    service.workspace.rag.request = Mock()
    service.workspace.client = Mock(side_effect=AssertionError('No model requests while viewing'))
    save(directory,dict(palette='forest',mode='light'))
    output = ROOT/'docs/screenshots/plan-conflicts-20261025'
    output.mkdir(parents=True,exist_ok=True)
    metrics = dict(passed=False,new_model_requests=0,views=[])
    with patch('app.native.topbar.TopBar.check_capture',lambda self:None):
        window = NativeWindow(service,discovery_updates=False)
        window.show()
        def settle():
            deadline = time.monotonic()+15
            while window.navigation_busy or window.tasks.pending:
                app.processEvents()
                if time.monotonic()>deadline:
                    raise RuntimeError('Window did not settle')
                time.sleep(.01)
            deadline = time.monotonic()+.2
            while time.monotonic()<deadline:
                app.processEvents()
                time.sleep(.01)
        settle()
        window.navigate('plans')
        settle()
        for width,height,suffix in ((1440,900,''),(900,620,'-narrow')):
            window.resize(width,height)
            settle()
            assert window.grab().save(str(output/('plans'+suffix+'.png')))
        window.advisor.result = report['result']
        window.advisor.message = '已避开时间冲突，已添加 2 项安排'
        dialog = AdvisorDialog(window)
        dialog.show()
        for width,height,expanded,suffix in ((680,600,False,''),(520,450,True,'-preferences-narrow')):
            dialog.resize(width,height)
            dialog.preferences.setChecked(expanded)
            settle()
            assert dialog.send.isVisible()
            assert dialog.rect().contains(dialog.send.mapTo(dialog,dialog.send.rect().bottomRight()))
            assert dialog.preference_panel.horizontalScrollBar().maximum()==0
            if expanded:
                assert dialog.preference_panel.viewport().height()>=180
            assert dialog.grab().save(str(output/('advisor'+suffix+'.png')))
            metrics['views'].append(dict(requested=[width,height],actual=[dialog.width(),dialog.height()],
                preferences_expanded=expanded,send_visible=True))
        dialog.close()
        window.close()
        settle()
        service.workspace.client.assert_not_called()
        metrics['passed'] = True
    (directory/'view-verification.json').write_text(json.dumps(metrics,indent=2),encoding='utf-8')
    print(json.dumps(dict(directory=str(directory),**metrics)))


if __name__ == '__main__':
    main()
