"""Open public sources without changing browser privileges or restarting it."""
import os


def edge_privilege_conflict():
    if os.name != 'nt':
        return False
    import ctypes
    import winreg
    from ctypes import wintypes as wt
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice') as key:
            if not winreg.QueryValueEx(key, 'ProgId')[0].lower().startswith('msedge'):
                return False
    except OSError:
        return False
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel.OpenProcess.restype = wt.HANDLE
    kernel.GetCurrentProcess.restype = wt.HANDLE
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    advapi.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)]
    def elevated(process):
        token = wt.HANDLE()
        if not advapi.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
            return None
        try:
            value, size = wt.DWORD(), wt.DWORD()
            if advapi.GetTokenInformation(token, 20, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(size)):
                return bool(value.value)
        finally:
            kernel.CloseHandle(token)
        return None
    ours = elevated(kernel.GetCurrentProcess())
    if ours is None:
        return False
    class Process(ctypes.Structure):
        _fields_ = [('dwSize', wt.DWORD), ('cntUsage', wt.DWORD), ('pid', wt.DWORD),
                    ('heap', ctypes.c_size_t), ('module', wt.DWORD), ('threads', wt.DWORD),
                    ('parent', wt.DWORD), ('priority', wt.LONG), ('flags', wt.DWORD), ('exe', wt.WCHAR * 260)]
    kernel.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wt.HANDLE
    kernel.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(Process)]
    kernel.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(Process)]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return False
    try:
        item = Process()
        item.dwSize = ctypes.sizeof(item)
        found = kernel.Process32FirstW(snapshot, ctypes.byref(item))
        processes = []
        while found:
            if item.exe.lower() == 'msedge.exe':
                processes.append((item.pid, item.parent))
            found = kernel.Process32NextW(snapshot, ctypes.byref(item))
        pids = {pid for pid, _ in processes}
        # Renderer/AppContainer children intentionally have lower privileges.
        for pid, parent in processes:
            if parent in pids:
                continue
            process = kernel.OpenProcess(0x1000, False, pid)
            if process:
                try:
                    level = elevated(process)
                    if level is not None and level != ours:
                        return True
                finally:
                    kernel.CloseHandle(process)
    finally:
        kernel.CloseHandle(snapshot)
    return False


def copy_link(host, url):
    from PySide6.QtWidgets import QApplication
    QApplication.clipboard().setText(url)
    host.notice('原站链接已复制')


def open_source(host, url):
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    from ..discovery import canonical
    try:
        url = canonical(url)
        conflict = edge_privilege_conflict()
    except (ValueError, OSError):
        host.notice('暂时无法打开浏览器，可以复制原站链接。')
        return False
    if conflict:
        host.notice('Edge 与词典的运行权限不同。请正常退出 Edge 后以普通权限重开，或使用“复制链接”。')
        return False
    if not QDesktopServices.openUrl(QUrl(url)):
        host.notice('浏览器未能打开，可以复制原站链接。')
        return False
    return True
