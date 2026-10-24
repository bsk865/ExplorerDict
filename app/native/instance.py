"""Switch reader builds through normal close/save, keeping one data writer.

Future builds use a named shutdown event. Legacy builds are identified by their
exact main-window title, executable name and saved data directory before WM_CLOSE.
No process is terminated; a refused or slow shutdown leaves the new build closed.
"""
import ctypes
import json
import os
import sys
import uuid
from pathlib import Path
from ..single_instance import _normalized_path


def build_identity():
    frozen = getattr(sys, 'frozen', False)
    path = Path(sys.executable).resolve() if frozen else Path(__file__).resolve().parents[2] / 'native_desktop.py'
    stat = path.stat()
    stamp = f'{stat.st_size}:{stat.st_mtime_ns}'
    if not frozen:
        from hashlib import sha256
        files = sorted((path.parent / 'app').rglob('*.py'))
        stamp += ':' + sha256('|'.join(f'{p}:{p.stat().st_size}:{p.stat().st_mtime_ns}' for p in files).encode()).hexdigest()
    return f'{_normalized_path(path)}:{stamp}'


def read_owner(directory):
    try:
        return json.loads((Path(directory) / '.reader-instance.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


class Registration:
    def __init__(self, directory):
        self.path = Path(directory) / '.reader-instance.json'
        from ..storage_location import config_path
        self.value = {'pid': os.getpid(), 'build': build_identity(), 'token': uuid.uuid4().hex,
                      'config': str(config_path().resolve())}

    def write(self):
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.value), encoding='utf-8')
        temporary.replace(self.path)

    def close(self):
        if read_owner(self.path.parent).get('token') == self.value['token']:
            self.path.unlink(missing_ok=True)


def legacy_reader(directory):
    """Find only the native reader attached to this data path, via OS lifecycle APIs."""
    from ctypes import wintypes as wt
    user = ctypes.WinDLL('user32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wt.LPARAM]
    user.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel.OpenProcess.restype = wt.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    matches = []
    @callback_type
    def inspect(hwnd, _):
        title = ctypes.create_unicode_buffer(256)
        user.GetWindowTextW(hwnd, title, len(title))
        if title.value != '探索词典 · 阅读空间':
            return True
        pid = wt.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = kernel.OpenProcess(0x1000, False, pid.value)
        if not handle:
            return True
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wt.DWORD(len(buffer))
            if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return True
            exe = Path(buffer.value)
            if exe.name != '探索词典-原生版.exe':
                return True
            config = {}
            try:
                config = json.loads((exe.parent / 'storage-location.json').read_text(encoding='utf-8'))
                active = config.get('active') or str(exe.parent / 'data')
            except (OSError, ValueError):
                active = str(exe.parent / 'data')
            if _normalized_path(active) == _normalized_path(directory):
                matches.append({'hwnd': int(hwnd), 'pid': pid.value, 'exe': str(exe), 'config': config})
        finally:
            kernel.CloseHandle(handle)
        return True
    user.EnumWindows(inspect, 0)
    return matches[0] if len(matches) == 1 else None


def request_legacy_close(reader):
    from ctypes import wintypes as wt
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    # WM_CLOSE follows the reader's closeEvent and flushes drafts/tasks normally.
    return bool(user.PostMessageW(reader['hwnd'], 0x0010, 0, 0))


def acquire_reader(app, guard, directory, identity):
    """Return (acquired, previous location config). Never silently activate another build."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QVBoxLayout, QLabel
    from .surfaces import QDialog, QMessageBox
    from .widgets import button, stylesheet
    from .. import capture_bridge, reading_preferences
    if guard.acquire():
        return True, {}
    guard.release()  # A failed CreateMutex still owns a reference; release before retry.
    owner = read_owner(directory)
    legacy = legacy_reader(directory)
    if legacy and owner.get('pid') != legacy['pid']:
        owner = {}
    same_build = owner.get('build') == build_identity() if owner else (
        legacy and _normalized_path(legacy['exe']) == _normalized_path(sys.executable))
    if same_build:
        capture_bridge.signal_event(identity + '.Show')
        return False, {}
    dialog = QDialog()
    dialog.setStyleSheet(stylesheet(reading_preferences.load(directory)))
    dialog.setWindowTitle('正在打开新版探索词典')
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(24, 18, 24, 22)
    label = QLabel('正在等待旧窗口完成保存…')
    label.setWordWrap(True)
    layout.addWidget(label)
    layout.addWidget(button('取消打开', dialog.reject))
    dialog.resize(460, 180)
    requested = capture_bridge.signal_event(identity + '.Switch') if owner else False
    if not requested and legacy:
        requested = request_legacy_close(legacy)
    if not requested:
        QMessageBox.information(dialog, '旧版本仍在运行', '请完全退出旧版本后重新打开此程序。此次没有切回旧窗口。')
        return False, {}
    acquired = False
    attempts = 0
    def retry():
        nonlocal acquired, attempts
        attempts += 1
        if guard.acquire():
            acquired = True
            dialog.accept()
        else:
            guard.release()
            if attempts >= 300:
                label.setText('旧窗口仍在保存或等待操作，请处理旧窗口后重新打开新版。')
                timer.stop()
    timer = QTimer(dialog)
    timer.timeout.connect(retry)
    timer.start(200)
    dialog.exec()
    timer.stop()
    if not acquired:
        return False, {}
    # The old delivery may have activated a pending data migration while closing.
    config = legacy.get('config', {}) if legacy else {}
    if legacy:
        try:
            config = json.loads((Path(legacy['exe']).parent / 'storage-location.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
    elif owner.get('config'):
        try:
            config = json.loads(Path(owner['config']).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
    return True, config
