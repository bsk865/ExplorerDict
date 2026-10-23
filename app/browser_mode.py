"""Launch the local workspace in the default browser, without importing Qt."""
import http.cookiejar
import json
import time
import urllib.request
from urllib.parse import urlsplit

from .paths import data_dir
from .single_instance import SingleInstance, data_dir_digest


def existing_url(directory):
    try:
        record = json.loads((directory / 'workspace-session-v7.json').read_text(encoding='utf-8'))
        url = record['url']
        parsed = urlsplit(url)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
                or not parsed.port or parsed.path != '/session'
                or parsed.username or parsed.password):
            return None
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        with opener.open(url, timeout=1) as response:
            if response.status != 200:
                return None
        return url
    except (OSError, ValueError, KeyError, TypeError):
        return None


def main():
    import sys
    import webbrowser
    directory = data_dir()
    guard = SingleInstance(name='Local\\ExplorerDict.Reader.' + data_dir_digest(directory))
    if not guard.acquire():
        for _ in range(100):
            url = existing_url(directory)
            if url:
                webbrowser.open(url)
                return
            time.sleep(.1)
        raise RuntimeError('阅读服务未能响应，请关闭原阅读窗口后重新打开兼容模式。')
    try:
        from .workspace_server import main as serve
        sys.argv = [sys.argv[0], '--browser']
        serve()
    finally:
        guard.release()


def launch():
    try:
        main()
    except Exception:
        from .logging_setup import get_logger
        get_logger('browser_mode').exception('浏览器兼容模式启动失败')
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, '阅读服务暂时无法启动，请关闭原阅读窗口后重试。', '探索词典', 0x10)
