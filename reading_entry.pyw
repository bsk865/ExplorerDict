"""Desktop shortcut entry. Reuse the live loopback server, or start one silently."""
from pathlib import Path
import json
import os
import subprocess
import urllib.request
import http.cookiejar
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent


def launch():
    directory = Path(os.environ.get('EXPLORER_DICT_DATA_DIR', ROOT / 'data'))
    record = directory / 'workspace-session-v3.json'
    if record.exists():
        try:
            url = json.loads(record.read_text(encoding='utf-8'))['url']
            parts = urlsplit(url)
            if parts.scheme != 'http' or parts.hostname != '127.0.0.1':
                raise ValueError('Not a local workspace')
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            with opener.open(url, timeout=2) as response:
                if response.status != 200:
                    raise ValueError('Session expired')
            edge = Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe'
            if edge.exists():
                subprocess.Popen([str(edge), '--app=' + url, '--user-data-dir=' + str(directory/'workspace-browser'), '--no-first-run', '--window-size=1440,960'])
            else:
                import webbrowser
                webbrowser.open(url)
            return
        except Exception:
            pass
    python = ROOT / '.build-env/Scripts/pythonw.exe'
    subprocess.Popen([str(python), '-B', '-m', 'app.workspace_server'], cwd=ROOT, creationflags=0x08000000)


if __name__ == '__main__':
    launch()
