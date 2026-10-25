"""Check a new source environment with authored material and an isolated data folder."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--offscreen', action='store_true')
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error('Choose a new or empty output directory.')
    output.mkdir(parents=True, exist_ok=True)
    os.environ['EXPLORER_DICT_DATA_DIR'] = str(output / 'data')
    os.environ['EXPLORER_DICT_STORAGE_CONFIG'] = str(output / 'storage.json')
    if args.offscreen:
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    started = time.perf_counter()
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer
    from app.native.service import NativeService
    from app.native.window import NativeWindow
    from app.local_embeddings import LocalEmbeddings
    from app.workspace_store import WorkspaceStore
    ctypes.WinDLL(str(root / 'scripts' / 'ExplorerDict.Native.dll'))
    embeddings = LocalEmbeddings()
    vectors = embeddings.encode(['探索词典把文章和笔记保存在本机。'])
    if vectors.shape != (1, 384):
        raise RuntimeError('Unexpected local embedding shape.')
    app = QApplication([])
    app.setStyle('Fusion')
    if app.platformName() == 'offscreen':
        from PySide6.QtGui import QFontDatabase, QFont
        fonts = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
        family = None
        for name in ('msyh.ttc', 'msyhbd.ttc'):
            font_id = QFontDatabase.addApplicationFont(str(fonts / name))
            families = QFontDatabase.applicationFontFamilies(font_id)
            if families and family is None:
                family = families[0]
        if family:
            app.setFont(QFont(family, 10))
    service = NativeService(output / 'data')
    article = service.import_text('源码启动示例', '探索词典把文章和笔记保存在本机。\n\n阅读时可以查找原句，记录自己的理解。')
    window = NativeWindow(service, discovery_updates=False)
    window.resize(1280, 800)
    result = {'python': sys.version.split()[0], 'qt_platform': app.platformName(),
              'local_vector_dimensions': int(vectors.shape[1]), 'native_helper_loaded': True,
              'model_api_called': False}
    failure = []

    def finish():
        try:
            if window.stack.count() == 0:
                raise RuntimeError('Main window did not load a page.')
            if not window.grab().save(str(output / 'startup.png')):
                raise RuntimeError('Could not capture the main window.')
            reopened = WorkspaceStore(output / 'data').article(article['id'])
            if reopened['text'] != article['text']:
                raise RuntimeError('Imported article did not persist.')
            result.update(passed=True, article_reopened=True,
                          startup_seconds=round(time.perf_counter() - started, 3))
        except Exception as exc:
            failure.append(exc)
            result.update(passed=False, error=str(exc))
        finally:
            (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            window.close()
            app.quit()

    window.show()
    QTimer.singleShot(1500, finish)
    app.exec()
    service.close()
    print(json.dumps(result, ensure_ascii=False))
    if failure:
        raise failure[0]
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
