"""Dedicated frameless reading window; window controls never depend on web JS."""
import json
import os
import secrets
import threading
import ctypes
import sys
import time
import subprocess
import urllib.request
import http.cookiejar
from pathlib import Path

# Qt uses Windows ICU; PDF processing uses its own ICU in a separate process.
if sys.platform == "win32":
    _system_icu = ctypes.WinDLL(str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/icuuc.dll"))
from http.server import ThreadingHTTPServer

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QIcon, QKeySequence, QShortcut, QDesktopServices
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSizeGrip, QFileDialog
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile

from .paths import data_dir, project_root
from .activation import ActivationReceiver
from .single_instance import SingleInstance, data_dir_digest
from . import capture_bridge
from .workspace_server import Workspace, make_handler


class LocalPage(QWebEnginePage):
    def __init__(self, profile, origin, parent):
        super().__init__(profile, parent)
        self.origin = origin

    def acceptNavigationRequest(self, url, kind, is_main):
        if not is_main:
            return True
        return url.toString().startswith(self.origin + '/') or url.toString() == 'about:blank'


class TitleBar(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setFixedHeight(42)
        layout = QHBoxLayout(self); layout.setContentsMargins(16, 0, 4, 0)
        title = QLabel('探索词典 · 阅读空间'); title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(title); layout.addStretch()
        for name, text, tip, action in [
            ('minimize', '−', '最小化，继续悬浮取词', window.showMinimized),
            ('maximize', '□', '最大化 / 还原', window.toggle_maximized),
            ('close', '×', '退出应用，同时关闭悬浮取词', window.close),
        ]:
            button = QPushButton(text); button.setObjectName(name)
            button.setAccessibleName(tip); button.setToolTip(tip)
            button.setFixedSize(42, 32); button.clicked.connect(action)
            layout.addWidget(button)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.window.windowHandle().startSystemMove()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.window.toggle_maximized()


class ReadingWindow(QWidget):
    def __init__(self, url, on_close):
        super().__init__()
        self.on_close = on_close
        self.closed = False
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle('探索词典 · 阅读空间')
        self.setWindowIcon(QIcon(str(project_root() / 'assets/app.ico')))
        self.setMinimumSize(640, 480)
        screen = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1440, int(screen.width() * .92)), min(960, int(screen.height() * .90)))
        self.move(screen.center() - self.rect().center())
        self.setStyleSheet('QWidget{background:#f7f3eb;color:#493c32;font-family:"Microsoft YaHei UI";} QLabel{font-size:12px;} QPushButton{border:0;border-radius:6px;font-size:19px;} QPushButton:hover{background:#ecdfd1;} QPushButton#close:hover{background:#9f5f43;color:white;}')
        layout = QVBoxLayout(self); layout.setContentsMargins(1, 1, 1, 1); layout.setSpacing(0)
        self.titlebar = TitleBar(self); layout.addWidget(self.titlebar)
        self.view = QWebEngineView(self)
        # A private profile has no old browser session, autofill, or stale note drafts.
        self.profile = QWebEngineProfile(self.view)
        origin = url.split('/session')[0]
        self.page = LocalPage(self.profile, origin, self.view)
        self.page.newWindowRequested.connect(self.open_source)
        self.profile.downloadRequested.connect(self.save_download)
        self.view.setPage(self.page); layout.addWidget(self.view, 1)
        self.grip = QSizeGrip(self); self.grip.setFixedSize(14, 14)
        self.shortcut = QShortcut(QKeySequence('Alt+F4'), self)
        self.shortcut.activated.connect(self.close)
        self.view.load(QUrl(url))

    def open_source(self, request):
        if request.isUserInitiated() and request.requestedUrl().scheme() in {'https', 'http'}:
            QDesktopServices.openUrl(request.requestedUrl())

    def save_download(self, download):
        from pathlib import Path
        filename, _ = QFileDialog.getSaveFileName(self, '保存文件', download.downloadFileName())
        if not filename:
            download.cancel()
            return
        target = Path(filename)
        download.setDownloadDirectory(str(target.parent))
        download.setDownloadFileName(target.name)
        download.accept()

    def toggle_maximized(self):
        self.showNormal() if self.isMaximized() else self.showMaximized()

    def resizeEvent(self, event):
        self.grip.move(self.width()-15, self.height()-15)
        self.grip.raise_()
        super().resizeEvent(event)

    def closeEvent(self, event):
        if not self.closed:
            self.closed = True
            self.on_close()
        event.accept()


def main():
    directory = data_dir()
    identity = 'Local\\ExplorerDict.Reader.' + data_dir_digest(directory)
    guard = SingleInstance(name=identity)
    if not guard.acquire():
        capture_bridge.signal_event(identity + '.Show')
        return
    receiver = ActivationReceiver(name=identity + '.Show'); receiver.create()
    session = directory / 'workspace-session-v6.json'
    session.unlink(missing_ok=True)
    command = [sys.executable, '--headless'] if getattr(sys, 'frozen', False) else [sys.executable, str(project_root() / 'reading_desktop.py'), '--headless']
    server_process = subprocess.Popen(command, cwd=project_root(), env={**os.environ, 'PYINSTALLER_RESET_ENVIRONMENT':'1'}, creationflags=0x08000000)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    try:
        for _ in range(80):
            try:
                record = json.loads(session.read_text(encoding='utf-8'))
                if record['pid'] != server_process.pid:
                    raise ValueError('Stale server')
                url = record['url']
                opener.open(url, timeout=1).close()
                break
            except (OSError, ValueError):
                if server_process.poll() is not None:
                    raise RuntimeError('Reading server could not start')
                time.sleep(.1)
        else:
            raise RuntimeError('Reading server startup timed out')
    except Exception:
        server_process.terminate(); guard.release(); receiver.close()
        raise
    qt = QApplication.instance() or QApplication([])
    qt.setQuitOnLastWindowClosed(True)
    def close():
        capture_bridge.stop_existing()
    window = ReadingWindow(url, close)
    timer = QTimer(window)
    def show_requested():
        if receiver.poll():
            window.showNormal(); window.raise_(); window.activateWindow()
    timer.timeout.connect(show_requested); timer.start(200)
    capture_bridge.start_on_launch()
    window.show()
    try:
        qt.exec()
    finally:
        close()
        capture_bridge.stop_and_wait()
        receiver.close(); guard.release()
        try:
            request = urllib.request.Request(url.split('/session')[0]+'/api/shutdown', data=b'{}', headers={'Content-Type':'application/json'})
            opener.open(request, timeout=1).close()
            server_process.wait(timeout=3)
        except Exception:
            server_process.terminate()
            server_process.wait(timeout=5)
        session.unlink(missing_ok=True)
