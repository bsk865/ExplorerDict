"""Launch the integrated reading workspace from the legacy capture UI."""
import subprocess
import os
import sys
from .paths import project_root, data_dir


def open_workspace():
    root = project_root()
    native = root / '探索词典-原生版.exe'
    if native.is_file() or (not getattr(sys, 'frozen', False) and (root / 'native_desktop.py').is_file()):
        from .capture_bridge import signal_event
        from .single_instance import data_dir_digest
        identity = 'Local\\ExplorerDict.Reader.' + data_dir_digest(data_dir())
        if signal_event(identity + '.Show'):
            return
        command = [str(native)] if native.is_file() else [sys.executable, str(root / 'native_desktop.py')]
        env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1')
        env.pop('QT_QPA_PLATFORM', None)
        subprocess.Popen(command, cwd=root, env=env, creationflags=0x08000000)
        return
    release = root / '探索词典-阅读版6.exe'
    command = [str(release)] if release.exists() else [str(root / '.build-env/Scripts/pythonw.exe'), str(root / 'reading_entry.pyw')]
    subprocess.Popen(command, cwd=root, creationflags=0x08000000)
