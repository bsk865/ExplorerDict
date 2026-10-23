"""Exercise citation -> saved note -> back with isolated native-window data."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--compact', action='store_true')
    args = parser.parse_args()
    parent = ROOT/'artifacts/native-checks'
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='note-navigation-', dir=parent))
    os.environ['EXPLORER_DICT_DATA_DIR'] = str(directory)
    os.environ['EXPLORER_DICT_STORAGE_CONFIG'] = str(directory/'storage-location.json')
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtWidgets import QApplication, QPushButton
    from app.native.service import NativeService
    from app.native.window import NativeWindow
    from app.native.library import LibraryPage
    from app.native.reader import ReaderPage

    app = QApplication([])
    service = NativeService(directory)
    article = service.import_text('引用定位验收', '原文帮助核对笔记中的理解。')
    other = service.hub.save('notes', {'article_id': article['id'], 'title': '另一条笔记', 'content': '不能误选此条。'})
    note = service.hub.save('notes', {'article_id': article['id'], 'title': '引用的心得', 'content': '回到原文核对推论。'})
    service.store.put('settings', 'native-reading:'+article['id'], {'tab': 0, 'scroll': 0})
    window = NativeWindow(service)
    window.capture_enabled = False
    if args.compact:
        window.resize(900, 620)
    report = {'directory': str(directory), 'compact': args.compact, 'passed': False, 'errors': []}
    phase = 0
    draft = '继续核对这条笔记'

    def finish(error=None):
        if error:
            report['errors'].append(str(error))
        report['passed'] = not report['errors'] and report.get('returned_draft', False)
        (directory/'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        window.close()

    def inspect():
        nonlocal phase
        if window.closing:
            return
        try:
            page = window.stack.currentWidget()
            if phase == 0:
                phase = 1
                window.navigate('library')
            elif phase == 1 and isinstance(page, LibraryPage):
                if not window.library.key or window.library.busy:
                    QTimer.singleShot(100, inspect)
                    return
                page.input.setPlainText(draft)
                hit = {'source': 'personal_note', 'article_id': article['id'], 'note_id': note['id'],
                       'article_title': article['title'], 'note_title': note['title'], 'quote': note['content'], 'citation': 'S1'}
                window.library.session['messages'] = [{'role': 'assistant', 'content': '核对心得 [S1]', 'evidence': [hit]}]
                page.render()
                phase = 2

                def locate():
                    dialog = QApplication.activeModalWidget()
                    try:
                        assert dialog and dialog.windowTitle() == '个人笔记依据'
                        dialog.grab().save(str(directory/'note-reference.png'))
                        next(b for b in dialog.findChildren(QPushButton) if b.text() == '查看笔记').click()
                    except Exception as exc:
                        if dialog:
                            dialog.reject()
                        finish(repr(exc))
                QTimer.singleShot(0, locate)
                page.message_action(QUrl('library:source:0:0'))
            elif phase == 2 and isinstance(page, ReaderPage):
                assert page.article['id'] == article['id'], 'wrong article'
                assert page.notes.draft.body.get('id') == note['id'], 'wrong note'
                assert page.notes.draft.body.get('id') != other['id']
                assert page.tabs.currentWidget() is page.notes and page.assistant_visible, 'notes not visible'
                assert page.notes.title.text() == note['title']
                assert page.notes.content.toPlainText() == note['content']
                assert page.notes.horizontalScrollBar().maximum() == 0, 'note panel overflows'
                assert page.notes.content.width() > 0 and page.notes.isVisible()
                window.grab().save(str(directory/'selected-note.png'))
                report['correct_note_selected'] = True
                phase = 3
                window.back.click()
            elif phase == 3 and isinstance(page, LibraryPage):
                assert page.input.toPlainText() == draft, 'return lost draft'
                window.grab().save(str(directory/'returned-question.png'))
                report['returned_draft'] = True
                finish()
        except Exception as exc:
            finish(repr(exc))

    window.ready.connect(lambda: QTimer.singleShot(250, inspect))
    window.tasks.error.connect(finish)
    QTimer.singleShot(15000, lambda: finish('navigation timed out') if not window.closing else None)
    window.show()
    app.exec()
    service.close()
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
