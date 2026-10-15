"""Frozen desktop entry, with single-instance session reuse."""
import http.cookiejar
import json
import os
import subprocess
import sys
import urllib.request
from urllib.parse import urlsplit
from pathlib import Path
from app.paths import data_dir


def main():
    if '--headless' not in sys.argv:
        from app.capture_bridge import start_on_launch
        start_on_launch()
        try:
            record=json.loads((data_dir()/'workspace-session-v5.json').read_text(encoding='utf-8'))
            url=record['url']; parts=urlsplit(url)
            if parts.scheme!='http' or parts.hostname!='127.0.0.1':
                raise ValueError()
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            with opener.open(url,timeout=2) as response:
                if response.status!=200:raise ValueError()
            edge=Path(os.environ.get('ProgramFiles(x86)','C:/Program Files (x86)'))/'Microsoft/Edge/Application/msedge.exe'
            if edge.exists():
                subprocess.Popen([str(edge),'--app='+url,'--user-data-dir='+str(data_dir()/'workspace-browser'),'--no-first-run','--window-size=1440,960'])
            else:
                import webbrowser;webbrowser.open(url)
            return
        except Exception:
            pass
    from app.workspace_server import main as serve
    serve()


if __name__=='__main__':
    main()
