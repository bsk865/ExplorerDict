"""Isolated settings/exit/reopen verification; never controls the desktop."""
import json
import os
import sys
import time
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


def attach_check(window, root, stage, *, with_capture=False):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QPushButton
    from .settings import SettingsDialog
    from .. import storage_location, capture_bridge

    started = False
    report = {'stage': stage, 'frozen': bool(getattr(sys, 'frozen', False)),
              'capture_lifecycle_tested': with_capture, 'passed': False}

    def finish(error=None):
        if error:
            report['error'] = type(error).__name__+': '+str(error)
        (root / (stage + '-runtime.json')).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        window.close()

    def complete():
        try:
            fixture = json.loads((root / 'fixture.json').read_text(encoding='utf-8'))
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
                assert (Path(fixture['target']) / 'retained-asset.txt').is_file()
            report['webengine_loaded'] = any('QtWebEngine' in name for name in sys.modules)
            assert not report['webengine_loaded']
            report['passed'] = True
        except Exception as exc:
            finish(exc)
        else:
            finish()

    def wait_for_companion(deadline):
        try:
            path = root/(stage+'-companion-ready.json')
            if path.is_file():
                ready = json.loads(path.read_text(encoding='utf-8'))
                assert ready['ipc_attached'] and ready['windows_withdrawn']
                assert ready['mutex_matches_data'] and ready['frozen'] == report['frozen']
                report['companion_ready'] = ready
                complete()
                return
            child = capture_bridge._launch
            if child is None or child.poll() is not None:
                raise RuntimeError('Companion exited before lifecycle readiness')
            if time.monotonic() >= deadline:
                raise RuntimeError('Companion did not become ready before deadline')
        except Exception as exc:
            finish(exc)
        else:
            QTimer.singleShot(100, lambda: wait_for_companion(deadline))

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
            if fixture.get('asset'):
                from .. import map_customization
                assert fixture['asset'] in bundle['assets']
                assert map_customization.display_graph(bundle['article'])['nodes'][0]['label'] == '我的整理'
                assert map_customization.positions(bundle['article']) == {'root':[20, 10]}
                translation = window.service.store.get('settings', 'native-translation:article:'+fixture['article'])
                assert translation['result'] == 'Reading materials are retained.' and translation['complete']
                report.update(images_loaded=True, map_edits_retained=True, translation_retained=True)
            report['content_loaded'] = True
            if with_capture:
                capture_bridge.ensure_started(lifecycle_check=root)
                QTimer.singleShot(100, lambda: wait_for_companion(time.monotonic()+15))
            else:
                complete()
        except Exception as exc:
            finish(exc)

    window.ready.connect(lambda: QTimer.singleShot(0, inspect))
