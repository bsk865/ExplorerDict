"""Verify shelf forms, asynchronous saves and refreshed counts with isolated data."""
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parent = ROOT / 'artifacts/native-checks'
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='shelf-workflow-', dir=parent))
    os.environ['EXPLORER_DICT_DATA_DIR'] = str(directory)
    os.environ['EXPLORER_DICT_STORAGE_CONFIG'] = str(directory / 'storage-location.json')
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtWidgets import QApplication, QPushButton, QScrollArea
    from app.native.service import NativeService
    from app.native.window import NativeWindow
    from app.native.shelf import ShelfPage

    app = QApplication([])
    if app.platformName() == 'offscreen':
        from PySide6.QtGui import QFontDatabase, QFont
        font_id = QFontDatabase.addApplicationFont(str(Path(os.environ.get('WINDIR', 'C:/Windows'))/'Fonts/msyh.ttc'))
        assert font_id >= 0
        app.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0], 10))
    service = NativeService(directory)
    article = None
    window = NativeWindow(service)
    window.capture_enabled = False
    report = {'directory': str(directory), 'passed': False, 'errors': []}
    phase = 0
    theme_id = None

    def finish(error=None):
        if window.closing:
            return
        if error:
            report['errors'].append(str(error))
        report['passed'] = phase == 6 and not report['errors']
        (directory / 'verification.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        modal = app.activeModalWidget()
        if modal:
            modal.reject()
        window.close()

    def modal_action(action):
        def run():
            try:
                dialog = app.activeModalWidget()
                assert dialog, 'modal missing'
                action(dialog)
            except Exception as exc:
                finish(repr(exc))
        QTimer.singleShot(50, run)

    def verify_counts(page, assigned):
        memberships = page.snapshot['study']['memberships']
        assert len(memberships) == assigned, memberships
        with service.hub.store.connect() as db:
            assert db.execute('SELECT COUNT(*) FROM study_memberships').fetchone()[0] == assigned
        assert page.themes.item(0).text().endswith('1')
        assert page.themes.item(1).text().endswith(str(1 - assigned))
        assert page.themes.item(2).text().endswith(str(assigned))
        page.select_theme(theme_id)
        assert page.articles.count() == assigned
        page.select_theme('unfiled')
        assert page.articles.count() == 1 - assigned
        page.select_theme('')
        assert page.articles.count() == 1

    def inspect():
        nonlocal phase, theme_id, article
        if window.closing:
            return
        try:
            page = window.stack.currentWidget()
            if phase == 0:
                phase = 1
                window.navigate('shelf')
            elif phase == 1 and isinstance(page, ShelfPage):
                if article is None:
                    assert page.articles.count() == 0 and page.empty.isVisible()
                    assert not page.rooms.isVisible()
                    assert page.empty.text() == '这里还没有文章。'
                    window.grab().save(str(directory / 'empty.png'))
                    report['empty_shelf'] = True
                    article = service.import_text('主题归类验收', '用于核对主题归类、数量及重复保存。')
                    window.refresh()
                    return
                assert page.articles.count() == 1 and not page.empty.isVisible()
                report['first_import_visible'] = True
                phase = 2
                def create(dialog):
                    dialog.fields['title'][1].setText('阅读与研究')
                    dialog.fields['description'][1].setPlainText('核对文章归类与整理。')
                    dialog.submit()
                modal_action(create)
                page.theme_form()
            elif phase == 2 and isinstance(page, ShelfPage):
                theme_id = page.snapshot['study']['themes'][0]['id']
                verify_counts(page, 0)
                report['theme_created'] = True
                phase = 3
                def choose(dialog):
                    choices = dialog.findChild(QScrollArea).findChildren(QPushButton)
                    assert len(choices) == 1 and choices[0].isEnabled()
                    choices[0].click()
                modal_action(choose)
                page.classify(article)
            elif phase == 3 and isinstance(page, ShelfPage):
                verify_counts(page, 1)
                report['assignment_saved_and_refreshed'] = True
                window.grab().save(str(directory / 'assigned.png'))
                phase = 4
                def cancel(dialog):
                    choice = dialog.findChild(QScrollArea).findChildren(QPushButton)[0]
                    assert not choice.isEnabled()
                    dialog.reject()
                modal_action(cancel)
                page.classify(article)
                verify_counts(page, 1)
                report['cancel_and_existing_membership'] = True
                page.assign(theme_id, [article['id'], article['id']])
            elif phase == 4 and isinstance(page, ShelfPage):
                verify_counts(page, 1)
                report['duplicate_assignment_idempotent'] = True
                phase = 5
                page.select_theme(theme_id)
                def remove(dialog):
                    items = dialog.fields['ids'][1]
                    assert items.item(0).checkState() == Qt.CheckState.Checked
                    items.item(0).setCheckState(Qt.CheckState.Unchecked)
                    dialog.submit()
                modal_action(remove)
                page.members()
            elif phase == 5 and isinstance(page, ShelfPage):
                verify_counts(page, 0)
                assert service.store.article(article['id'])['title'] == article['title']
                report['membership_removed_article_retained'] = True
                window.grab().save(str(directory / 'unfiled.png'))
                phase = 6
                finish()
        except Exception as exc:
            finish(repr(exc))

    window.ready.connect(lambda: QTimer.singleShot(200, inspect))
    window.tasks.error.connect(finish)
    QTimer.singleShot(20000, lambda: finish('workflow timed out'))
    window.show()
    app.exec()
    service.close()
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
