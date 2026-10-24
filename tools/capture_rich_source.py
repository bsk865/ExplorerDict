"""Check rich source selection and article-local translation with isolated data."""
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
    from PySide6.QtGui import QFont, QFontDatabase, QTextCursor
    from PySide6.QtWidgets import QApplication
    from app.native.service import NativeService
    from app.native.window import NativeWindow
    from app.reading_text import block_text
    from app.retrieval import SourceIndex

    directory = Path(tempfile.mkdtemp(prefix='rich-source-', dir=ROOT/'artifacts/native-checks'))
    os.environ['EXPLORER_DICT_DATA_DIR'] = str(directory)
    os.environ['EXPLORER_DICT_STORAGE_CONFIG'] = str(directory/'location.json')
    app = QApplication([])
    app.setStyle('Fusion')
    font_id = QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
    family = QFontDatabase.applicationFontFamilies(font_id)[0]
    QFont.insertSubstitution('Microsoft YaHei UI', family)
    app.setFont(QFont(family, 10))
    service = NativeService(directory)
    service.workspace.map_preloader.close()
    service.workspace.rag.request = Mock()
    service.workspace.client = Mock(side_effect=AssertionError('No model requests'))
    article = service.store.import_article('材料实验阅读记录', [dict(type='text',
        html='<h2>实验结果</h2><p>人工智能辅助的材料实验\n  记录：钙钛矿的失效阈值为<strong>83摄氏度</strong>。</p>')])
    other = service.store.import_article('另一篇阅读资料', [dict(type='text', html='<p>Robots learn from demonstrations.</p>')])
    output = ROOT/'docs/screenshots/rich-source-20261024'
    output.mkdir(parents=True, exist_ok=True)
    report = dict(passed=False, synthetic_content_only=True, model_requests=0)
    with patch('app.native.topbar.TopBar.check_capture', lambda self:None):
        window = NativeWindow(service, discovery_updates=False)
        window.resize(900, 620)
        window.show()
        def settle():
            deadline = time.monotonic()+10
            while window.navigation_busy or window.tasks.pending:
                app.processEvents()
                if time.monotonic() > deadline:
                    raise RuntimeError('Window did not settle')
                time.sleep(.01)
            for _ in range(10):
                app.processEvents()
        settle()
        window.open_article(article['id'])
        settle()
        reader = window.stack.currentWidget()
        source = block_text(article['blocks'][0])
        row = reader.text.ranges[0]
        start = source.index('83摄氏度')
        cursor = reader.text.textCursor()
        cursor.setPosition(row['spans'][start][0])
        cursor.setPosition(row['spans'][start+len('83摄氏度')-1][1], QTextCursor.MoveMode.KeepAnchor)
        reader.text.setTextCursor(cursor)
        segment = reader.text.selected_segments()[0]
        assert segment['quote'] == '83摄氏度'
        assert source[segment['start']:segment['end']] == segment['quote']
        reader.save_mark('yellow')
        settle()
        note = service.hub.save('notes', dict(article_id=article['id'], content='注意温度单位。', **{k:segment[k] for k in ('block','quote')}))
        hits = SourceIndex(service.store).search([article], '失效阈值', text_only=True)
        assert hits and all(source[h['start']:h['end']] == h['quote'] for h in hits)
        reader = window.stack.currentWidget()
        reader.translate()
        controller = reader.translation_page.controller
        controller.state.update(source='zh', target='en', result='83 degrees Celsius', complete=True)
        controller.flush()
        controller.changed.emit()
        settle()
        assert window.grab().save(str(output/'article-translation.png'))
        window.open_article(other['id'])
        settle()
        second = window.stack.currentWidget()
        second.translate()
        assert second.translation_page.result.toPlainText() == ''
        assert 'Robots' in second.translation_page.original.toPlainText()
        assert app.activeModalWidget() is None
        assert window.grab().save(str(output/'other-article.png'))
        window.open_article(article['id'])
        settle()
        reader = window.stack.currentWidget()
        reader.translate()
        assert reader.translation_page.result.toPlainText() == '83 degrees Celsius'
        assert service.annotations.list(article['id'])['items'][0]['segments'] == [segment]
        assert service.hub.notes(article['id'])['notes'][0]['id'] == note['id']
        service.workspace.client.assert_not_called()
        report.update(passed=True, exact_selection=segment, exact_retrieval=True,
            note_saved=True, mark_retained=True, article_translation_isolated=True, no_modal=True)
        window.close()
        settle()
    (directory/'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(directory)


if __name__ == '__main__':
    main()
