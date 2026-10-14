"""Launch the integrated reading workspace from the legacy capture UI."""
import subprocess
from .paths import project_root


def open_workspace():
    root = project_root()
    release = root / '探索词典-阅读版.exe'
    command = [str(release)] if release.exists() else [str(root / '.build-env/Scripts/pythonw.exe'), str(root / 'reading_entry.pyw')]
    subprocess.Popen(command, cwd=root, creationflags=0x08000000)
