"""Connect the reading window to the existing native selection companion."""
import json
import os
import subprocess
import sys
import threading
import time
from .paths import project_root, data_dir
from .activation import ActivationBackend, ActivationReceiver
from .single_instance import data_dir_digest, mutex_name_for

_lock = threading.Lock()
_launch = None

def event_name():
    return 'Local\\ExplorerDict.Float.' + data_dir_digest(data_dir())


def reveal_existing():
    return signal_event(event_name())


def signal_event(name):
    backend = ActivationBackend()
    handle, _ = backend.open_event(name)
    if not handle:
        return False
    try:
        return backend.set_event(handle)
    finally:
        backend.close_handle(handle)


def stop_existing():
    return signal_event(event_name() + '.Quit')


def stop_and_wait(timeout=8):
    """Closing immediately after startup must also stop a still-initializing companion."""
    deadline = time.monotonic() + timeout
    while running() or (_launch is not None and _launch.poll() is None):
        stop_existing()
        if time.monotonic() >= deadline:
            return False
        time.sleep(.1)
    return True


def start_on_launch():
    # A capture failure must not prevent access to the user's reading materials.
    try:
        ensure_started()
    except Exception:
        from .logging_setup import get_logger
        get_logger('capture_bridge').exception('启动悬浮取词失败')

def running():
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenMutexW.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.LPCWSTR]
    kernel.OpenMutexW.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.OpenMutexW(0x00100000,False,mutex_name_for(data_dir()))
    if handle:
        kernel.CloseHandle(handle)
        return True
    return ctypes.get_last_error()==5

def status():
    active=running()
    result={'running':active,'ready':False,'label':'悬浮取词未开启'}
    if active:
        result['label']='悬浮取词正在启动'
        try:
            record=json.loads((data_dir()/'capture-status.json').read_text(encoding='utf-8'))
            if time.time()-record['updated']<5:
                result.update(record)
        except (OSError,ValueError,KeyError):
            pass
    return result

def ensure_started(*, reveal=False):
    global _launch
    with _lock:
        if running():
            if reveal:
                if not reveal_existing():
                    raise ValueError('取词组件需要更新。请从托盘退出探索词典，再重新打开应用。')
            return status()
        if _launch is not None and _launch.poll() is None:
            return {'running':False,'ready':False,'label':'悬浮取词正在启动'}
        executable=project_root()/'探索词典.exe'
        command = [str(executable)]
        if not executable.is_file():
            source = project_root() / 'bootstrap.py'
            if not getattr(sys, 'frozen', False) and source.is_file():
                command = [sys.executable, str(source)]
            else:
                raise ValueError('缺少悬浮取词组件，请重新安装应用。')
        try:
            _launch=subprocess.Popen(command,cwd=project_root(),
                                     env={**os.environ, 'PYINSTALLER_RESET_ENVIRONMENT': '1'}, creationflags=0x08000000)
        except OSError as exc:
            raise ValueError('悬浮取词未能启动，请稍后重试。') from exc
        return {'running':False,'ready':False,'label':'悬浮取词正在启动'}

def attach(app):
    receiver=ActivationReceiver(name=event_name())
    receiver.create()
    app._reading_capture_receiver=receiver
    app._reading_quit_receiver = ActivationReceiver(name=event_name() + '.Quit')
    app._reading_quit_receiver.create()

def poll(app):
    quit_receiver = getattr(app, '_reading_quit_receiver', None)
    if quit_receiver and quit_receiver.poll():
        app.quit()
        return
    receiver=getattr(app,'_reading_capture_receiver',None)
    if receiver is None:
        return
    if receiver.poll():
        if not app.capture_enabled():
            app.toggle_capture()
        app.recover_panel()
    now=time.time()
    if now-getattr(app,'_reading_capture_reported',0)<1:
        return
    app._reading_capture_reported=now
    enabled=app.capture_enabled()
    hook=bool(app.mouse_hook.running) if hasattr(app,'mouse_hook') else False
    decision = getattr(app.gate_controller, 'last', None)
    blocked = bool(decision and decision.hard_blocked)
    ready=bool(app._uia_started and app.uia.pid() and hook and enabled and not blocked)
    label='划选后自动弹出' if ready else '取词已暂停' if not enabled else decision.label if blocked else '回到原文中划选' if app._uia_started and app.uia.pid() else '取词组件正在准备'
    record={'updated':now,'pid':os.getpid(),'ready':ready,'enabled':enabled,'hook':hook,'label':label}
    record['appearance'] = getattr(app, '_reading_appearance', None)
    panel = getattr(app, 'reading_panel', None)
    if panel is not None:
        try:
            record['panel_background'] = str(panel.win.cget('background')).lower()
        except Exception:
            pass
    target=data_dir()/'capture-status.json'
    temporary=target.with_suffix('.tmp')
    temporary.write_text(json.dumps(record,ensure_ascii=False),encoding='utf-8')
    temporary.replace(target)

def close(app):
    quit_receiver = getattr(app, '_reading_quit_receiver', None)
    if quit_receiver:
        quit_receiver.close()
    receiver=getattr(app,'_reading_capture_receiver',None)
    if receiver:
        receiver.close()
