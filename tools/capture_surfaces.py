"""Render the settings cards and shared chrome with disposable local data."""
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
from PySide6.QtGui import QFontDatabase, QFont
from app.native.service import NativeService
from app.native.window import NativeWindow
from app.native.settings import SettingsDialog
from app.native.surfaces import QMessageBox, QFileDialog


def main():
    app = QApplication([])
    app.setStyle('Fusion')
    font = QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
    family = QFontDatabase.applicationFontFamilies(font)[0]
    QFont.insertSubstitution('Microsoft YaHei UI', family)
    app.setFont(QFont(family, 10))
    output = ROOT / 'docs/screenshots/application-surfaces-20261024'
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='native-surfaces-') as directory:
        os.environ['EXPLORER_DICT_DATA_DIR'] = directory
        os.environ['EXPLORER_DICT_STORAGE_CONFIG'] = str(Path(directory) / 'location.json')
        service = NativeService(Path(directory))
        service.import_text('探索笔记', '阅读与探索。')
        (Path(directory) / '探索笔记.md').write_text('# 阅读与探索\n', encoding='utf-8')
        def settle():
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                app.processEvents()
                if not window.tasks.pending and not window.navigation_busy:
                    for _ in range(10):
                        app.processEvents()
                    return
                time.sleep(.01)
            raise RuntimeError('Tasks did not settle')
        with patch('app.native.topbar.TopBar.check_capture', lambda self: None):
            window = NativeWindow(service, discovery_updates=False)
            window.show()
            settle()
            dialog = SettingsDialog(window)
            dialog.show()
            dialog.show_storage_details()
            settle()
            dialog.grab().save(str(output / 'settings.png'))
            dialog.open_storage()
            settle()
            dialog.grab().save(str(output / 'storage-location.png'))
            prompt = QMessageBox(window)
            prompt.setWindowTitle('删除确认')
            prompt.setText('删除这条笔记？文章原文会保留。')
            prompt.setStandardButtons(prompt.StandardButton.Yes | prompt.StandardButton.No)
            prompt.button(prompt.StandardButton.Yes).setText('确认删除')
            prompt.button(prompt.StandardButton.No).setText('取消')
            prompt.show()
            settle()
            prompt.grab().save(str(output / 'confirmation.png'))
            prompt.close()
            picker = QFileDialog(window, '选择资料', directory, '资料 (*.txt *.md *.pdf)')
            picker.show()
            settle()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and picker.files.rowCount(picker.view.rootIndex()) == 0:
                app.processEvents()
                time.sleep(.01)
            picker.grab().save(str(output / 'file-picker.png'))
            picker.close()
            dialog.close()
            window.close()
            settle()
            window.close()
    print(output)


if __name__ == '__main__':
    main()
