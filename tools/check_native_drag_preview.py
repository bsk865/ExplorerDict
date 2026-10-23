"""Render drag previews offscreen in every supported palette and mode."""
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'


def main():
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter, QColor, QFont, QFontDatabase
    from PySide6.QtWidgets import QApplication
    from app.native.shelf import ArticleList
    from app.native.widgets import stylesheet
    from app.reading_preferences import PALETTES, colors

    app = QApplication([])
    font_path = Path(os.environ.get('WINDIR', 'C:/Windows'))/'Fonts/msyh.ttc'
    font_id = QFontDatabase.addApplicationFont(str(font_path))
    assert font_id >= 0, 'Chinese font unavailable for offscreen verification'
    font = QFont(QFontDatabase.applicationFontFamilies(font_id)[0], 10)
    app.setFont(font)
    parent = ROOT / 'artifacts/native-checks'
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='drag-preview-', dir=parent))
    sheet = QImage(640, 960, QImage.Format.Format_ARGB32)
    sheet.fill(Qt.GlobalColor.transparent)
    painter = QPainter(sheet)
    painter.setFont(font)
    report = {'passed': False, 'variants': [], 'system_drag_tested': False}
    for row, palette in enumerate(sorted(PALETTES)):
        for column, mode in enumerate(('light', 'dark')):
            theme = colors(palette, mode)
            view = ArticleList(theme)
            view.setStyleSheet(stylesheet({'palette': palette, 'mode': mode}))
            view.ensurePolished()
            view.setFont(font)
            x, y = column * 320, row * 160
            painter.fillRect(x, y, 320, 160, QColor(theme['bg']))
            painter.setPen(QColor(theme['ink']))
            painter.drawText(x + 20, y + 20, palette + ' / ' + mode)
            samples = [[{'id': 'a', 'title': '阅读与研究方法：回到原文核对观点与资料'}],
                       [{'id': str(i), 'title': '资料'} for i in range(3)]]
            for number, articles in enumerate(samples):
                preview = view.drag_preview(articles)
                assert round(preview.deviceIndependentSize().width()) == 280
                assert round(preview.deviceIndependentSize().height()) == 56
                image = preview.toImage()
                assert image.pixelColor(0, 0).alpha() == 0
                assert image.pixelColor(image.width() // 2, 5).alpha() > 0
                painter.drawPixmap(x + 20, y + 28 + number * 62, preview)
            view.close()
            report['variants'].append(palette + '/' + mode)
    painter.end()
    assert sheet.save(str(directory / 'previews.png'))
    report['passed'] = True
    (directory / 'verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({'directory': str(directory), **report}, ensure_ascii=False))


if __name__ == '__main__':
    main()
