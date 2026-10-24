"""Render saved conversations and exercise references with isolated local data."""
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
from PySide6.QtCore import QUrl
from PySide6.QtGui import QFontDatabase, QFont
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from app.native.service import NativeService
from app.native.window import NativeWindow
from app.native.content import ContentPage
from app.native.reader import ReaderPage
from app.reading_preferences import save


def main():
    directory = Path(tempfile.mkdtemp(prefix='content-records-', dir=ROOT/'artifacts/native-checks'))
    os.environ['EXPLORER_DICT_DATA_DIR'] = str(directory)
    os.environ['EXPLORER_DICT_STORAGE_CONFIG'] = str(directory/'location.json')
    app = QApplication([])
    font = QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
    app.setFont(QFont(QFontDatabase.applicationFontFamilies(font)[0], 10))
    service = NativeService(directory)
    service.workspace.map_preloader.close()
    service.workspace.rag.request = Mock()
    service.workspace.client = Mock(side_effect=AssertionError('No model requests'))
    save(directory, dict(palette='forest', mode='light'))
    article = service.import_text('阅读图表的方法', '比较图表前，先确认指标的名称、单位和统计口径。\n\n随后检查分组条件，最后核对结论与原始数据是否一致。')
    other = service.import_text('阅读实验结果', '对比不同实验结果时，还需要考虑样本数量与测量条件。')
    evidence = dict(article_id=article['id'], article_title=article['title'], version=article['version'], block='b0',
                    quote='比较图表前，先确认指标的名称、单位和统计口径。', citation='S1')
    key = article['id']+':'+article['version']+':'
    service.store.put('sessions', key, dict(archives=[dict(messages=[
        dict(role='user', content='看图表时，第一步应该关注什么？'),
        dict(role='assistant', content='先检查指标名称、单位和统计口径，再比较各组数值。[S1]', evidence=[evidence])])],
        messages=[dict(role='user', content='不同文章的实验结果可以直接放在一起比较吗？'),
            dict(role='assistant', content='需要先核对样本数量和测量条件。[S2]', evidence=[
                dict(article_id=other['id'], article_title=other['title'], version=other['version'], block='b0',
                     quote=other['text'], citation='S2')])],
        note='比较前先核对条件，不能只看数值大小。', draft='如果统计口径不同，应该如何整理笔记？'))
    service.annotations.save(dict(article_id=article['id'], version=article['version'], style='yellow',
        segments=[dict(block='b0', start=0, end=22, quote=article['blocks'][0]['text'][:22])]))
    output = ROOT/'docs/screenshots/content-records-20261024'
    output.mkdir(parents=True, exist_ok=True)
    report = dict(passed=False, synthetic_content_only=True, model_requests=0)
    with patch('app.native.topbar.TopBar.check_capture', lambda self: None):
        window = NativeWindow(service, discovery_updates=False)
        window.show()
        def settle():
            end = time.monotonic()+10
            while window.navigation_busy or window.tasks.pending:
                app.processEvents()
                if time.monotonic() > end:
                    raise RuntimeError('Window did not settle')
                time.sleep(.01)
            QTest.qWait(80)
        settle()
        window.content_filters = dict(kind='conversations', query='统计口径', article='')
        window.navigate('content')
        settle()
        page = window.stack.currentWidget()
        assert isinstance(page, ContentPage) and page.count.text() == '1 项'
        window.resize(1440, 900)
        settle()
        assert window.grab().save(str(output/'list.png'))
        page.details(page.rows[0][0])
        settle()
        contents = page.detail.browser.toPlainText()
        assert all(value in contents for value in ('先前对话 1', '当前对话', '会话笔记', '待继续的问题', '统计口径'))
        assert len(page.detail.references) == 2
        assert app.activeModalWidget() is None
        assert window.grab().save(str(output/'conversation.png'))
        window.resize(900, 620)
        settle()
        assert page.detail.width() <= page.width()
        assert page.detail.browser.horizontalScrollBar().maximum() == 0
        assert window.grab().save(str(output/'conversation-narrow.png'))
        page.detail.open_reference(QUrl('content-source:0'))
        settle()
        assert isinstance(window.stack.currentWidget(), ReaderPage)
        window.go_back()
        settle()
        page = window.stack.currentWidget()
        assert page.detail and page.detail.record['id'] == key
        assert page.search.text() == '统计口径'
        page.back_to_list()
        settle()
        page.category.setCurrentIndex(page.category.findData('annotations'))
        page.details(page.rows[0][0])
        settle()
        assert window.grab().save(str(output/'annotation.png'))
        assert app.activeModalWidget() is None
        service.workspace.client.assert_not_called()
        report.update(passed=True, archived_and_current_visible=True, saved_reference_count=2,
                      back_restores_record=True, inline_annotation=True, no_modal_windows=True)
        window.close()
        settle()
    (directory/'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(directory)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
