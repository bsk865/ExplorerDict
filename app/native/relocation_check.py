"""Isolated settings/exit/reopen verification; never controls the desktop."""
import json
import os
import sys
from pathlib import Path


def prepare_environment(root, stage):
    root = Path(root).resolve()
    if not (root / '.native-relocation-check').is_file():
        raise ValueError('A prepared relocation fixture is required')
    fixture = json.loads((root / 'fixture.json').read_text(encoding='utf-8'))
    for key, name in (('source', 'original'), ('target', 'destination')):
        if Path(fixture[key]).resolve() != root / name or (root / name).is_symlink():
            raise ValueError('Fixture paths must remain inside the isolated directory')
    config_path = root / 'storage-location.json'
    if config_path.resolve() != config_path:
        raise ValueError('Fixture configuration must remain inside the isolated directory')
    config = json.loads(config_path.read_text(encoding='utf-8'))
    if config.get('pending') and Path(config['pending']).resolve() != root / 'destination':
        raise ValueError('Pending fixture directory must remain isolated')
    expected = fixture['source' if stage == 'schedule' else 'target']
    if Path(config['active']).resolve() != Path(expected).resolve():
        raise ValueError('Unexpected active fixture directory')
    os.environ.pop('EXPLORER_DICT_DATA_DIR', None)
    os.environ['EXPLORER_DICT_STORAGE_CONFIG'] = str(root / 'storage-location.json')


def attach_check(window, root, stage):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QPushButton
    from .settings import SettingsDialog
    from .. import storage_location

    started = False
    report = {'stage': stage, 'frozen': bool(getattr(sys, 'frozen', False)),
              'capture_lifecycle_tested': False, 'passed': False}

    def inspect():
        nonlocal started
        if started:
            return
        started = True
        try:
            fixture = json.loads((root / 'fixture.json').read_text(encoding='utf-8'))
            expected = fixture['source' if stage == 'schedule' else 'target']
            assert window.service.store.directory.resolve() == Path(expected).resolve()
            bundle = window.service.article(fixture['article'])
            assert bundle['article']['title'] == '迁移验收资料'
            assert any(n['id'] == fixture['note'] for n in bundle['notes']['notes'])
            assert any(m['id'] == fixture['mark'] for m in bundle['annotations']['items'])
            report['content_loaded'] = True
            if stage == 'schedule':
                dialog = SettingsDialog(window)
                dialog.target.setText(fixture['target'])
                dialog.storage_dialog.show()
                QApplication.processEvents()
                next(b for b in dialog.storage_dialog.findChildren(QPushButton)
                     if b.text() == '保存位置').click()
                config = storage_location.read()
                assert config['pending'] == fixture['target']
                assert config['active'] == fixture['source']
                dialog.storage_dialog.grab().save(str(root / 'storage-scheduled.png'))
                dialog.storage_dialog.close()
                dialog.close()
                report['settings_scheduled'] = True
            else:
                assert 'pending' not in storage_location.read()
                assert (Path(expected) / 'retained-asset.txt').is_file()
            report['webengine_loaded'] = any('QtWebEngine' in name for name in sys.modules)
            assert not report['webengine_loaded']
            report['passed'] = True
        except Exception as exc:
            report['error'] = type(exc).__name__ + ': ' + str(exc)
        finally:
            (root / (stage + '-runtime.json')).write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            window.close()

    window.ready.connect(lambda: QTimer.singleShot(0, inspect))
