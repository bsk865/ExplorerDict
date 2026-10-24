"""Exercise native note export and readable version history using isolated sample data."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'


def main():
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QListWidget
    from app.native.service import NativeService
    from app.native.window import NativeWindow
    from app.native.materials import export_notes, versions
    from app.native.surfaces import QFileDialog
    from app import reading_preferences

    app = QApplication([])
    app.setStyle('Fusion')
    font = QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
    family = QFontDatabase.applicationFontFamilies(font)[0]
    QFont.insertSubstitution('Microsoft YaHei UI', family)
    app.setFont(QFont(family, 10))
    directory = Path(tempfile.mkdtemp(prefix='reading-notes-', dir=ROOT/'artifacts/native-checks'))
    os.environ['EXPLORER_DICT_DATA_DIR'] = str(directory)
    os.environ['EXPLORER_DICT_STORAGE_CONFIG'] = str(directory/'location.json')
    output = ROOT/'docs/screenshots/reading-notes-20261024'
    output.mkdir(parents=True, exist_ok=True)
    service = NativeService(directory)
    service.workspace.map_preloader.close()
    service.workspace.rag.request = Mock()
    service.workspace.client = Mock(side_effect=AssertionError('No model requests'))
    reading_preferences.save(directory, dict(palette='forest', mode='light'))
    original = 'Read each table row before comparing results.'
    table = ('<table width="1200"><tr><th>阅读步骤</th><th>关注内容</th></tr>'
             '<tr><td width="600">先看表头</td><td width="600">确认指标名称和单位</td></tr>'
             '<tr><td>再看分组</td><td>在同一条件下比较各项结果</td></tr></table>')
    old = service.store.import_article('图表阅读方法', [dict(type='text', text=original), dict(type='text', html=table)])
    expression = ''.join('<mi>x</mi><mo>+</mo>' for _ in range(20))+'<mn>1</mn>'
    article = service.append(old['id'], [dict(type='text', html='<p>逐项汇总：<math><mrow>'+expression+'</mrow></math></p>')])
    service.annotations.save(dict(article_id=article['id'], version=article['version'], style='yellow',
        segments=[dict(block='b0', start=0, end=19, quote=original[:19])]))
    service.hub.save('notes', dict(article_id=article['id'], title='读表顺序', content='先看指标，再核对分组与单位。', block='b0', quote=original))
    service.store.put('settings', 'native-note-drafts:'+article['id'], [dict(article_id=article['id'],
        title='进一步思考', content='比较时还要检查各组是否采用同一种统计口径。', draft_id='sample-draft')])
    service.store.put('settings', 'native-translation:article:'+article['id'], dict(source='en', target='zh',
        text=original, result='比较结果前，先逐行阅读表格。', complete=True))
    evidence = dict(article_id=article['id'], article_title=article['title'], block='b0', citation='S1', quote=original)
    service.store.put('sessions', article['id']+':'+article['version']+':', dict(messages=[
        dict(role='user', content='阅读表格时先看什么？'),
        dict(role='assistant', content='先逐行看表格，再进行比较。[S1]', evidence=[evidence])]))
    report = dict(passed=False, synthetic_content_only=True, model_requests=0, errors=[])
    with patch('app.native.topbar.TopBar.check_capture', lambda self:None):
        window = NativeWindow(service, discovery_updates=False)
        window.show()
        def settle():
            deadline = time.monotonic()+10
            while window.navigation_busy or window.tasks.pending:
                app.processEvents()
                if time.monotonic() > deadline:
                    raise RuntimeError('Window did not settle')
                time.sleep(.01)
            QTest.qWait(60)
        settle()
        window.open_article(article['id'])
        settle()
        window.resize(1440, 900)
        settle()
        assert window.grab().save(str(output/'reading.png'))
        window.resize(900, 620)
        reader = window.stack.currentWidget()
        reader.set_assistant_visible(True)
        settle()
        assert reader.text.horizontalScrollBar().maximum() == 0
        assert window.grab().save(str(output/'reading-narrow.png'))
        report['narrow_source_width'] = reader.text.viewport().width()
        report['source_html_unchanged'] = service.store.article(article['id'])['blocks'][1]['html'] == table
        reader.open_formula(next(iter(reader.text.formula_html)))
        settle()
        reader.formula_page.actual_size()
        settle()
        assert app.activeModalWidget() is None
        assert reader.reading_tools.currentWidget() is reader.formula_page
        assert reader.formula_page.view.panning
        assert abs(reader.formula_page.view.transform().m11()-.5) < .01
        assert window.grab().save(str(output/'formula.png'))
        report['formula_view_inside_article'] = True
        reader.show_original()
        reader.set_assistant_visible(False)
        reader.translate()
        settle()
        assert reader.translation_page.controller.scope == 'article:'+article['id']
        assert reader.translation_page.result.toPlainText() == '比较结果前，先逐行阅读表格。'
        assert window.grab().save(str(output/'translation.png'))
        reader.show_original()
        target = directory/'图表阅读方法 · 阅读笔记.md'
        def save_file():
            dialog = app.activeModalWidget()
            try:
                assert isinstance(dialog, QFileDialog)
                assert dialog.windowFlags() & Qt.WindowType.FramelessWindowHint
                dialog.setDirectory(str(directory))
                dialog.selectFile(target.name)
                QTest.qWait(80)
                assert dialog.grab().save(str(output/'export.png'))
                dialog.choose.click()
            except Exception as exc:
                report['errors'].append(str(exc))
                if dialog: dialog.reject()
        QTimer.singleShot(40, save_file)
        export_notes(window, article)
        settle()
        exported = target.read_text(encoding='utf-8')
        assert all(value in exported for value in ('原文标注', '回答时引用的资料', '读表顺序', '进一步思考（草稿）', '## 译文', '<math>'))
        assert all(value not in exported for value in (article['version'], article['id'], '来源：b0'))
        report['saved_notes_sha256'] = hashlib.sha256(target.read_bytes()).hexdigest()
        example = ROOT/'docs/examples/阅读笔记.md'
        example.parent.mkdir(exist_ok=True)
        shutil.copy2(target, example)
        def open_old_version():
            dialog = app.activeModalWidget()
            try:
                assert dialog.windowFlags() & Qt.WindowType.FramelessWindowHint
                rows = dialog.findChild(QListWidget)
                labels = [rows.item(index).text() for index in range(rows.count())]
                assert len(labels) == 2 and '第1版' in labels[0] and '第2版' in labels[1]
                assert all(value['version'][:12] not in '\n'.join(labels) for value in (old, article))
                rows.setCurrentRow(0)
                assert dialog.grab().save(str(output/'versions.png'))
                report['version_labels'] = labels
                rows.itemDoubleClicked.emit(rows.item(0))
            except Exception as exc:
                report['errors'].append(str(exc))
                if dialog: dialog.reject()
        QTimer.singleShot(40, open_old_version)
        versions(window, article)
        settle()
        assert window.article_id == old['id']
        assert len(service.store.article(old['id'])['blocks']) == 2
        service.workspace.client.assert_not_called()
        report['passed'] = not report['errors'] and report['source_html_unchanged']
        window.close()
        settle()
    (directory/'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(directory)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
