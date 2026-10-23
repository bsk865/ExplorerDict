"""Render an explicitly marked database copy, without starting capture or models."""
import json
import sys
from PySide6.QtWidgets import QApplication
from .service import NativeService
from .reader import OriginalText


def check_copy(directory):
    if not (directory / '.native-compatibility-copy').is_file():
        raise ValueError('Compatibility verification requires a prepared copy.')
    app = QApplication.instance() or QApplication([])
    service = NativeService(directory)
    service.workspace.map_preloader.close()
    report = {'articles_loaded': 0, 'marks_loaded': 0, 'images_loaded': 0, 'errors': [],
              'frozen': bool(getattr(sys, 'frozen', False))}
    try:
        service.snapshot()
        with service.store.connect() as db:
            ids = [row[0] for row in db.execute('SELECT id FROM articles')]
        for key in ids:
            view = None
            try:
                bundle = service.article(key)
                view = OriginalText()
                view.resize(900, 700)
                view.load_article(bundle['article'], bundle['assets'])
                view.annotate(bundle['annotations']['items'])
                if view.grab().isNull():
                    raise ValueError('Render failed')
                report['articles_loaded'] += 1
                report['marks_loaded'] += len(bundle['annotations']['items'])
                report['images_loaded'] += len(bundle['assets'])
            except Exception as exc:
                report['errors'].append({'article_id': key, 'type': type(exc).__name__})
            finally:
                if view:
                    view.close()
                    view.deleteLater()
                app.processEvents()
    finally:
        service.close()
    report['webengine_loaded'] = any('QtWebEngine' in name for name in sys.modules)
    report['passed'] = not report['errors'] and not report['webengine_loaded']
    (directory / 'compatibility-runtime.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return 0 if report['passed'] else 1
