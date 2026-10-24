"""Inspect a public PDF page in isolated Qt windows, without input capture or API requests."""
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
    import pymupdf
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from app.native.service import NativeService
    from app.native.window import NativeWindow
    from app.native.images import ImageDialog
    from app import reading_preferences

    app = QApplication([])
    app.setStyle('Fusion')
    font = QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
    family = QFontDatabase.applicationFontFamilies(font)[0]
    QFont.insertSubstitution('Microsoft YaHei UI', family)
    app.setFont(QFont(family, 10))
    directory = Path(tempfile.mkdtemp(prefix='image-reading-', dir=ROOT/'artifacts/native-checks'))
    os.environ['EXPLORER_DICT_DATA_DIR'] = str(directory)
    os.environ['EXPLORER_DICT_STORAGE_CONFIG'] = str(directory/'location.json')
    output = ROOT/'docs/screenshots/image-reading-20261024'
    output.mkdir(parents=True, exist_ok=True)
    pdf = ROOT/'artifacts/fulltext-translation-20261024/public-paper.pdf'
    with pymupdf.open(pdf) as document:
        raw = document[0].get_pixmap(matrix=pymupdf.Matrix(2, 2)).tobytes('png')
    service = NativeService(directory)
    service.workspace.map_preloader.close()
    service.workspace.rag.request = Mock()
    service.workspace.client = Mock(side_effect=AssertionError('No model requests'))
    service.workspace.start = Mock(side_effect=AssertionError('No background jobs'))
    reading_preferences.save(directory, dict(palette='forest', mode='light'))
    asset, _ = service.store.image(raw)
    article = service.store.import_article('RGraph-Eval', [
        {'type': 'image', 'asset': asset, 'caption': 'RGraph-Eval · 第1页'}])
    obj = service.store.object(article['id'], dict(type='image', title='RGraph-Eval · 第1页', block='b0', asset=asset))

    def settle(window):
        deadline = time.monotonic()+10
        while window.navigation_busy or window.tasks.pending:
            app.processEvents()
            if time.monotonic() > deadline:
                raise RuntimeError('Window did not settle')
            time.sleep(.01)
        for _ in range(10):
            app.processEvents()

    metrics = {}
    with patch('app.native.topbar.TopBar.check_capture', lambda self: None):
        window = NativeWindow(service, discovery_updates=False)
        window.show()
        settle(window)
        window.open_article(article['id'])
        settle(window)
        dialog = ImageDialog(window, article, obj, raw)
        dialog.show()
        app.processEvents()
        assert dialog.grab().save(str(output/'image.png'))
        dialog.resize(500, 400)
        for _ in range(10): app.processEvents()
        assert dialog.grab().save(str(output/'image-narrow.png'))
        metrics['narrow_size'] = [dialog.width(), dialog.height()]
        metrics['narrow_view_height'] = dialog.view.height()
        for control in (dialog.pan, dialog.use):
            assert dialog.rect().contains(control.mapTo(dialog, control.rect().bottomRight()))
        dialog.resize(860, 650)
        app.processEvents()
        # Commit a region through Qt events; the user's mouse is never moved.
        first = dialog.view.mapFromScene(QPointF(200, 450))
        last = dialog.view.mapFromScene(QPointF(1000, 800))
        QTest.mousePress(dialog.view.viewport(), Qt.MouseButton.LeftButton, pos=first)
        QTest.mouseMove(dialog.view.viewport(), last)
        QTest.mouseRelease(dialog.view.viewport(), Qt.MouseButton.LeftButton, pos=last)
        selected = list(dialog.view.region)
        dialog.view.zoom(2)
        dialog.pan.click()
        assert dialog.view.panning and dialog.view.region == selected
        assert dialog.grab().save(str(output/'image-pan.png'))
        dialog.choose()
        settle(window)
        chosen = window.stack.currentWidget().chat.current_object()
        assert chosen['type'] == 'region' and chosen['region'] == selected
        metrics['article_region_restored'] = chosen['region']
        restored = ImageDialog(window, service.store.article(article['id']), chosen, raw)
        assert restored.view.region == selected
        restored.close()
        service.workspace.start.assert_not_called()
        service.workspace.client.assert_not_called()
        window.close()
        settle(window)
    report = dict(passed=True, source='https://arxiv.org/abs/2610.31121', pdf_page=1,
                  model_requests=0, user_input_capture=False, metrics=metrics)
    (directory/'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(directory)


if __name__ == '__main__':
    main()
