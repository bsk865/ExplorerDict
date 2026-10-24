"""Real companion widgets, database and exit IPC, kept inside a marked fixture."""
import json
from pathlib import Path
import sys
import tkinter as tk


def validate(directory):
    from . import storage_location
    from .single_instance import default_data_dir
    root = Path(directory).resolve()
    if not (root/'.native-relocation-check').is_file():
        raise ValueError('An isolated relocation fixture is required')
    fixture = json.loads((root/'fixture.json').read_text(encoding='utf-8'))
    if any(Path(fixture[key]).resolve() != root/name or (root/name).is_symlink()
           or (hasattr(root/name, 'is_junction') and (root/name).is_junction())
           for key, name in (('source', 'original'), ('target', 'destination'))):
        raise ValueError('Companion data must remain inside the fixture')
    if storage_location.config_path().resolve() != root/'storage-location.json':
        raise ValueError('Companion configuration must remain isolated')
    active = default_data_dir().resolve()
    if active not in {root/'original', root/'destination'}:
        raise ValueError('Companion must use the active fixture directory')
    if Path(storage_location.read().get('active', '')).resolve() != active:
        raise ValueError('Saved and process data directories must agree')
    return root, fixture, active


def run(directory):
    from .main import build_app, _attach_activation
    from . import capture_bridge, reading_preferences, map_customization
    from .workspace_store import WorkspaceStore
    from .single_instance import process_guard, mutex_name_for
    root, fixture, active = validate(directory)
    stage = 'schedule' if active == root/'original' else 'reopen'
    output = root/(stage+'-companion.json')
    report = {'frozen': bool(getattr(sys, 'frozen', False)), 'directory':str(active),
              'stage':stage, 'capture_hooks_started':False, 'passed':False}
    tk_root = app = None
    try:
        tk_root = tk.Tk()
        tk_root.withdraw()
        app = build_app(tk_root)
        assert Path(app.db.path).resolve() == active/'explorer_dict.sqlite3'
        guard = process_guard()
        assert guard is not None and guard.name == mutex_name_for(active)
        entry = app.db.get_entry(fixture['entry'])
        assert entry['term'] == '迁移词条'
        assert app.db.get_setting('relocation-fixture') == 'shared settings retained'
        assert app.db.get_batch(fixture['batch'])['name'] == '迁移主题'
        store = WorkspaceStore(active)
        article = store.article(fixture['article'])
        assert article['title'] == '迁移验收资料'
        assert store.asset(fixture['asset'])[1]
        assert map_customization.display_graph(article)['nodes'][0]['label'] == '我的整理'
        assert map_customization.positions(article) == {'root':[20, 10]}
        translation = store.get('settings', 'native-translation:article:'+fixture['article'])
        assert translation['result'] == 'Reading materials are retained.' and translation['complete']
        assert app._reading_appearance == reading_preferences.load(active)
        capture_bridge.attach(app)
        assert _attach_activation(app)
        assert not app.mouse_hook.running and not app.watcher.is_alive() and not app.uia.pid()
        tk_root.update_idletasks()
        assert app.reading_panel.win.state() == 'withdrawn' and tk_root.state() == 'withdrawn'
        report.update(loaded_entry=True, loaded_article=True, loaded_image=True,
                      shared_appearance=True, mutex_matches_data=True, ipc_attached=True, windows_withdrawn=True)
        # This write must reach the migrated DB only after the companion flushes/closes.
        app.db.set_setting('relocation-companion-'+stage, 'loaded and closed')
        (root/(stage+'-companion-ready.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        def timeout():
            report['error'] = 'Reader did not request companion shutdown before deadline'
            app.quit()
        tk_root.after(45000, timeout)
        tk_root.mainloop()
        report.update(database_closed=app.db.closed(), normal_exit=app._closing)
        report['passed'] = bool(app.db.closed() and app._closing and not report.get('error'))
    except Exception as exc:
        report['error'] = type(exc).__name__+': '+str(exc)
    finally:
        if app is not None and not app._closing:
            app.quit()
        elif tk_root is not None:
            try:
                tk_root.destroy()
            except tk.TclError:
                pass
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if report['passed'] else 1
