"""Exercise real withdrawn companion widgets against an isolated preference DB."""
import json
from pathlib import Path
import sys
import tkinter as tk


def run(directory):
    from . import paths, reading_preferences as preferences
    from .main import build_app
    directory = Path(directory).resolve()
    if not (directory/'.appearance-check').is_file() or paths.data_dir().resolve() != directory:
        raise ValueError('An isolated appearance fixture is required')
    report = {'frozen': bool(getattr(sys, 'frozen', False)), 'cases': [], 'passed': False}
    root = None
    app = None
    try:
        preferences.save(directory, {'palette': 'ocean', 'mode': 'light'})
        root = tk.Tk()
        root.withdraw()
        app = build_app(root)
        app._schedule_activation_poll = lambda: None
        panel = app.reading_panel
        root.update_idletasks()
        expected = preferences.colors('ocean', 'light')
        assert str(panel.win.cget('background')).lower() == expected['bg'].lower(), 'startup background'
        assert str(panel.entry_input.cget('background')).lower() == expected['bg'].lower(), 'startup input'
        report['startup_restored'] = True
        panel.entry_input.insert(0, '未发送的问题')
        window_id = panel.win.winfo_id()
        show_calls = panel.show_calls
        for palette in sorted(preferences.PALETTES):
            for mode in ('light', 'dark'):
                value = {'palette': palette, 'mode': mode}
                # Separate database connection, matching the main-window writer.
                preferences.save(directory, value)
                app._poll_activation()
                root.update_idletasks()
                tokens = preferences.colors(palette, mode)
                actual = {key: str(widget.cget(option)).lower() for key, widget, option in (
                    ('bg', panel.win, 'background'), ('input_bg', panel.entry_input, 'background'),
                    ('ink', panel.title_label, 'foreground'), ('accent', panel.btn_send, 'background'))}
                assert app._reading_appearance == value, 'preference not observed'
                assert actual['bg'] == tokens['bg'].lower(), (value, actual)
                assert actual['input_bg'] == tokens['bg'].lower(), (value, actual)
                assert actual['ink'] == tokens['ink'].lower(), (value, actual)
                assert actual['accent'] == tokens['accent'].lower(), (value, actual)
                assert panel.entry_input.get() == '未发送的问题', 'draft changed'
                assert panel.win.winfo_id() == window_id, 'window replaced'
                assert panel.win.state() == 'withdrawn' and panel.show_calls == show_calls, 'window revealed'
                report['cases'].append(dict(value, colors=actual))
        app.db.close()
        root.destroy()
        app = root = None
        root = tk.Tk()
        root.withdraw()
        app = build_app(root)
        assert app._reading_appearance == value, 'restarted preference'
        assert str(app.reading_panel.win.cget('background')).lower() == tokens['bg'].lower(), 'restarted color'
        report.update(restart_restored=True, draft_preserved=True, no_window_reveal=True, passed=True)
    except Exception as exc:
        report['error'] = repr(exc)
    finally:
        if app is not None:
            app.db.close()
        if root is not None:
            root.destroy()
    (directory/'appearance-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if report['passed'] else 1
