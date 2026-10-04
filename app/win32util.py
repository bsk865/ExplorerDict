"""Win32 封装（纯 ctypes，无 pywin32）。

覆盖：进程 DPI 感知、前台窗口信息、自身窗口判定、无激活窗口样式、
低级鼠标钩子、全局热键。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

IS_64 = ctypes.sizeof(ctypes.c_void_p) == 8
LONG_PTR = ctypes.c_int64 if IS_64 else ctypes.c_int32
#: GDI / 窗口句柄都是**指针宽度**：64 位下必须是 c_void_p，用 c_int 会被截断
HRGN = ctypes.c_void_p
HGDIOBJ = ctypes.c_void_p

# ------------------------------------------------------------------ 常量
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SW_SHOW = 5

HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

WH_MOUSE_LL = 14
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_MOUSEMOVE = 0x0200
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)

MONITOR_DEFAULTTONEAREST = 2


class POINT(wt.POINT):
    pass


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wt.HWND),
        ("message", wt.UINT),
        ("wParam", wt.WPARAM),
        ("lParam", wt.LPARAM),
        ("time", wt.DWORD),
        ("pt", wt.POINT),
    ]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wt.POINT),
        ("mouseData", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wt.DWORD),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(LONG_PTR, ctypes.c_int, wt.WPARAM, wt.LPARAM)

# ------------------------------------------------------------------ 原型
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
user32.IsWindow.argtypes = [wt.HWND]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.WindowFromPoint.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetAncestor.restype = wt.HWND
user32.GetParent.argtypes = [wt.HWND]
user32.GetParent.restype = wt.HWND
user32.IsZoomed.argtypes = [wt.HWND]
user32.IsIconic.argtypes = [wt.HWND]
user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
user32.MonitorFromWindow.restype = wt.HANDLE
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.CallNextHookEx.restype = LONG_PTR
user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wt.HWND, wt.UINT, wt.UINT]
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
user32.MonitorFromPoint.restype = wt.HANDLE
user32.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.POINTER(MONITORINFO)]
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetDpiForSystem.restype = wt.UINT
user32.SetProcessDPIAware.restype = wt.BOOL
if hasattr(user32, "SetProcessDpiAwarenessContext"):
    user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetProcessDpiAwarenessContext.restype = wt.BOOL

if IS_64:
    user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = LONG_PTR
    user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, LONG_PTR]
    user32.SetWindowLongPtrW.restype = LONG_PTR
else:  # pragma: no cover - 32 位 Python
    user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = LONG_PTR
    user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, LONG_PTR]
    user32.SetWindowLongW.restype = LONG_PTR

kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR,
                                               ctypes.POINTER(wt.DWORD)]
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HINSTANCE
kernel32.GetCurrentThreadId.restype = wt.DWORD


def _get_window_long(hwnd: int, index: int) -> int:
    if IS_64:
        return int(user32.GetWindowLongPtrW(wt.HWND(hwnd), index))
    return int(user32.GetWindowLongW(wt.HWND(hwnd), index))


def _set_window_long(hwnd: int, index: int, value: int) -> int:
    if IS_64:
        return int(user32.SetWindowLongPtrW(wt.HWND(hwnd), index, LONG_PTR(value)))
    return int(user32.SetWindowLongW(wt.HWND(hwnd), index, LONG_PTR(value)))


# ------------------------------------------------------------------ DPI
def enable_dpi_awareness() -> str:
    """声明进程 DPI 感知，保证鼠标坐标与窗口坐标一致。返回实际生效的模式。"""
    try:
        if user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
            return "per-monitor-v2"
    except (AttributeError, OSError):
        pass
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        if shcore.SetProcessDpiAwareness(2) == 0:
            return "per-monitor"
    except (AttributeError, OSError):
        pass
    try:
        if user32.SetProcessDPIAware():
            return "system"
    except (AttributeError, OSError):
        pass
    return "none"


def get_system_dpi() -> int:
    try:
        dpi = int(user32.GetDpiForSystem())
        if dpi > 0:
            return dpi
    except (AttributeError, OSError):
        pass
    try:
        hdc = user32.GetDC(0)
        dpi = int(ctypes.WinDLL("gdi32").GetDeviceCaps(hdc, 88))
        user32.ReleaseDC(0, hdc)
        if dpi > 0:
            return dpi
    except (AttributeError, OSError):
        pass
    return 96


# ------------------------------------------------------------------ 窗口
def get_window_text(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(wt.HWND(hwnd))
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(wt.HWND(hwnd), buf, n + 1)
    return buf.value


def get_window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(wt.HWND(hwnd), buf, 256)
    return buf.value


def get_window_pid(hwnd: int) -> int:
    pid = wt.DWORD(0)
    user32.GetWindowThreadProcessId(wt.HWND(hwnd), ctypes.byref(pid))
    return int(pid.value)


def get_window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    r = RECT()
    if not user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(r)):
        return None
    return (r.left, r.top, r.right, r.bottom)


def get_process_image(pid: int) -> str:
    """尽力拿到进程可执行文件全路径；失败返回空串。"""
    if pid <= 0:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def get_foreground_window() -> int:
    return int(user32.GetForegroundWindow() or 0)


def get_cursor_pos() -> tuple[int, int]:
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return (pt.x, pt.y)


def is_self_window(hwnd: int) -> bool:
    """窗口是否属于本进程（用于「忽略本进程窗口」）。"""
    if not hwnd:
        return False
    return get_window_pid(hwnd) == os.getpid()


SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Windows.UI.Core.CoreWindow",
                 "ApplicationFrameWindow", "XamlExplorerHostIslandWindow"}


def is_probably_desktop(hwnd: int) -> bool:
    if not hwnd:
        return True
    return get_window_class(hwnd) in ("Progman", "WorkerW", "Shell_TrayWnd")


def foreground_info() -> dict:
    """前台窗口信息快照（只用标准 Win32 窗口元信息，不注入进程、不读内存）。"""
    hwnd = get_foreground_window()
    pid = get_window_pid(hwnd) if hwnd else 0
    exe = get_process_image(pid)
    return {
        "hwnd": hwnd,
        "pid": pid,
        "title": get_window_text(hwnd) if hwnd else "",
        "class": get_window_class(hwnd) if hwnd else "",
        "exe": exe,
        "app": os.path.basename(exe) if exe else "",
        "is_self": pid == os.getpid() if pid else False,
    }


# ------------------------------------------- 全屏 / 窗口根 / 鼠标目标窗口
GA_ROOT = 2
FULLSCREEN_TOLERANCE_PX = 2


def is_zoomed(hwnd: int) -> bool:
    """窗口是否处于「最大化」（最大化不算全屏，浏览网页很常见）。"""
    return bool(hwnd) and bool(user32.IsZoomed(wt.HWND(hwnd)))


def is_iconic(hwnd: int) -> bool:
    return bool(hwnd) and bool(user32.IsIconic(wt.HWND(hwnd)))


def window_root(hwnd: int) -> int:
    """取窗口的最顶层祖先（GetAncestor(GA_ROOT)）。失败时原样返回。"""
    if not hwnd:
        return 0
    return int(user32.GetAncestor(wt.HWND(hwnd), GA_ROOT) or hwnd)


def window_root_from_point(x: int, y: int) -> int:
    """屏幕坐标下的最顶层窗口（用于「鼠标点在谁的窗口上」）。"""
    h = int(user32.WindowFromPoint(wt.POINT(int(x), int(y))) or 0)
    return window_root(h) if h else 0


def monitor_rect_for_window(hwnd: int) -> tuple[int, int, int, int] | None:
    """窗口所在显示器的完整矩形（含任务栏区域）。"""
    if not hwnd:
        return None
    mon = user32.MonitorFromWindow(wt.HWND(hwnd), MONITOR_DEFAULTTONEAREST)
    if not mon:
        return None
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
        return None
    return (mi.rcMonitor.left, mi.rcMonitor.top, mi.rcMonitor.right, mi.rcMonitor.bottom)


def is_fullscreen(hwnd: int, tolerance: int = FULLSCREEN_TOLERANCE_PX) -> bool:
    """窗口是否占满整块显示器（全屏）。

    仅用标准窗口矩形 + 显示器矩形判断：
    * 必须可见、未最小化；
    * 窗口矩形完全覆盖所在显示器矩形（允许 tolerance 像素误差）；
    * ``IsZoomed`` 为真的「最大化」窗口不算全屏（浏览器/Word 最大化是正常阅读场景）。

    该判定**不区分**全屏游戏与全屏视频/演示，二者都按「全屏 → 暂停取词」处理，
    这也是本项目的安全默认值。
    """
    if not hwnd or not is_window_visible(hwnd) or is_iconic(hwnd):
        return False
    rect = get_window_rect(hwnd)
    mon = monitor_rect_for_window(hwnd)
    if rect is None or mon is None:
        return False
    covers = (
        rect[0] <= mon[0] + tolerance
        and rect[1] <= mon[1] + tolerance
        and rect[2] >= mon[2] - tolerance
        and rect[3] >= mon[3] - tolerance
    )
    if not covers:
        return False
    return not is_zoomed(hwnd)


# ------------------------------------------------- 无激活 / 不抢焦点
def make_no_activate(hwnd: int) -> bool:
    """加 WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW：点击不激活、不进 Alt+Tab。"""
    if not hwnd:
        return False
    ex = _get_window_long(hwnd, GWL_EXSTYLE)
    new = ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    if new != ex:
        _set_window_long(hwnd, GWL_EXSTYLE, new)
    return True


def clear_no_activate(hwnd: int) -> bool:
    """去掉 WS_EX_NOACTIVATE（**只**在用户明确点击追问输入框时用）。

    这是「划选自动展开不抢阅读焦点」与「用户要打字」之间唯一的开关：
    自动路径永远带 NOACTIVATE；用户点了输入框才允许窗口被激活，
    输入结束（折叠 / 失焦 / 下次自动显示）又会补回 NOACTIVATE。
    """
    if not hwnd:
        return False
    ex = _get_window_long(hwnd, GWL_EXSTYLE)
    new = ex & ~WS_EX_NOACTIVATE
    if new != ex:
        _set_window_long(hwnd, GWL_EXSTYLE, new)
    return True


def activate_window(hwnd: int) -> bool:
    """把窗口提到前台并给键盘焦点（仅用户显式点击输入框时调用）。"""
    if not hwnd:
        return False
    try:
        user32.SetForegroundWindow(wt.HWND(hwnd))
        user32.SetFocus(wt.HWND(hwnd))
        return True
    except OSError:  # pragma: no cover - 前台锁定等极端情况
        return False


def show_no_activate(hwnd: int, topmost: bool = True) -> None:
    """显示窗口但不激活、不抢焦点。

    ``SetWindowPos(SWP_SHOWWINDOW)`` 已经负责「置顶 + 显示」；
    只有窗口确实不可见时才再补一次 ``ShowWindow`` ——
    对已可见窗口重复显示会多一次重绘（闪）。
    """
    if not hwnd:
        return
    flags = SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
    already_visible = is_window_visible(hwnd)
    user32.SetWindowPos(wt.HWND(hwnd), wt.HWND(HWND_TOPMOST if topmost else 0), 0, 0, 0, 0, flags)
    if not already_visible:
        user32.ShowWindow(wt.HWND(hwnd), SW_SHOWNOACTIVATE)


def set_topmost(hwnd: int, value: bool) -> None:
    """只改 Z 序（置顶/取消置顶）：不移动、不改尺寸、不激活、不显示。"""
    if not hwnd:
        return
    flags = SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE
    user32.SetWindowPos(wt.HWND(hwnd), wt.HWND(HWND_TOPMOST if value else HWND_NOTOPMOST),
                        0, 0, 0, 0, flags)


def is_topmost(hwnd: int) -> bool:
    return bool(hwnd) and bool(_get_window_long(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)



def hide_window(hwnd: int) -> None:
    if hwnd:
        user32.ShowWindow(wt.HWND(hwnd), SW_HIDE)


def is_window_visible(hwnd: int) -> bool:
    return bool(hwnd) and bool(user32.IsWindowVisible(wt.HWND(hwnd)))


def work_area_for_point(x: int, y: int) -> tuple[int, int, int, int]:
    """返回该点所在显示器的工作区 (l, t, r, b)。"""
    pt = wt.POINT(x, y)
    mon = user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if mon and user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
        return (mi.rcWork.left, mi.rcWork.top, mi.rcWork.right, mi.rcWork.bottom)
    return (0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))


def is_left_button_down() -> bool:
    return bool(user32.GetAsyncKeyState(0x01) & 0x8000)


WS_CHILD = 0x40000000


def toplevel_hwnd(widget_id: int) -> int:
    """把 Tk 的 winfo_id() 解析成真正的顶层 HWND（Tk 会嵌套一层 wrapper）。"""
    hwnd = int(widget_id)
    for _ in range(8):
        style = _get_window_long(hwnd, GWL_STYLE)
        if not (style & WS_CHILD):
            break
        parent = int(user32.GetParent(wt.HWND(hwnd)) or 0)
        if not parent:
            break
        hwnd = parent
    return hwnd


def is_no_activate(hwnd: int) -> bool:
    return bool(_get_window_long(hwnd, GWL_EXSTYLE) & WS_EX_NOACTIVATE)


# ------------------------------------------------------- 圆角窗口 region（窄封装）
#: ``SetWindowRgn`` 的 ``bRedraw``：region 变化后让系统重画窗口
user32.SetWindowRgn.argtypes = [wt.HWND, HRGN, wt.BOOL]
user32.SetWindowRgn.restype = ctypes.c_int
gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_int]
gdi32.CreateRoundRectRgn.restype = HRGN
gdi32.DeleteObject.argtypes = [HGDIOBJ]
gdi32.DeleteObject.restype = wt.BOOL

#: ``SetWindowRgn(hwnd, NULL, TRUE)``：清掉 region，窗口退回**方形**
NULL_REGION = None


def create_round_rect_region(width: int, height: int, radius: int) -> int:
    """创建圆角矩形 region；失败返回 0。

    ``CreateRoundRectRgn`` 的右下角是**开区间**，所以传 ``width+1 / height+1``
    才能覆盖整块窗口；椭圆宽高传 ``2r`` 得到半径 ``r`` 的圆角。
    返回的句柄**归调用方所有**（必须交给 :func:`set_window_region` 或自己删除）。
    """
    w, h = int(width), int(height)
    if w <= 0 or h <= 0:
        return 0
    r = max(0, min(int(radius), w // 2, h // 2))
    if r <= 0:
        return 0
    try:
        handle = gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, r * 2, r * 2)
    except OSError:  # pragma: no cover - GDI 资源耗尽等极端情况
        return 0
    return int(handle or 0)


def set_window_region(hwnd: int, region: int | None) -> bool:
    """把 region 应用到窗口，返回是否成功。

    **所有权**：``SetWindowRgn`` 成功时系统接管该 region（调用方不得再删除，
    否则窗口销毁时会二次释放）；失败时所有权仍在调用方，这里立刻
    ``DeleteObject``，避免 GDI 句柄泄漏。``region=None`` 表示清空 → 方形窗口。
    """
    if not hwnd:
        if region:
            _delete_region(region)
        return False
    try:
        ok = bool(user32.SetWindowRgn(wt.HWND(hwnd), HRGN(region) if region else NULL_REGION,
                                      True))
    except OSError:  # pragma: no cover
        ok = False
    if not ok and region:
        _delete_region(region)      # 转移失败：自己删，绝不泄漏
    return ok


def _delete_region(region: int) -> bool:
    if not region:
        return False
    try:
        return bool(gdi32.DeleteObject(HGDIOBJ(region)))
    except OSError:  # pragma: no cover
        return False


def delete_region(region: int) -> bool:
    """删除自己持有的 region 句柄（**只**用于没交给系统的那种）。"""
    return _delete_region(int(region or 0))


def clear_window_region(hwnd: int) -> bool:
    """清掉窗口 region（退回方形）：失败安全路径用它兜底。"""
    return set_window_region(hwnd, None)


def apply_round_region(hwnd: int, width: int, height: int, radius: int) -> bool:
    """给窗口设置 ``radius`` 圆角 region（一步到位）。

    * 创建失败（尺寸非法 / GDI 失败）→ **清掉 region 退回方形**并返回 False；
    * ``SetWindowRgn`` 失败 → 同上（region 已被 :func:`set_window_region` 释放）；
    * 成功 → 系统接管 region，句柄不再由本进程管理。

    整个函数**不映射、不激活、不移动**窗口：只改形状，可以安全地在门控判定
    之后、``deiconify`` 之前调用。绝不使用透明颜色键（那会在正文上打洞）。
    """
    if not hwnd:
        return False
    region = create_round_rect_region(width, height, radius)
    if not region:
        clear_window_region(hwnd)
        return False
    if set_window_region(hwnd, region):
        return True
    clear_window_region(hwnd)
    return False


# ------------------------------------------------------------- 剪贴板（只读）
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

user32.OpenClipboard.argtypes = [wt.HWND]
user32.CloseClipboard.argtypes = []
user32.GetClipboardData.argtypes = [wt.UINT]
user32.GetClipboardData.restype = wt.HANDLE
user32.IsClipboardFormatAvailable.argtypes = [wt.UINT]
kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]


def read_clipboard_text() -> str:
    """**只读**剪贴板文本，绝不写入。

    仅在用户显式点击「从剪贴板导入」时调用。
    """
    if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        return ""
    if not user32.OpenClipboard(None):
        return ""
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def send_ctrl_c() -> None:  # pragma: no cover - 明确禁止使用
    raise RuntimeError(
        "本项目禁止模拟 Ctrl+C 窃取选区（见 SPEC.md 非目标）。"
        "请使用 UIA TextPattern 或用户主动复制后导入。"
    )


# ------------------------------------------------------------------ 热键
def register_hotkey(hotkey_id: int, modifiers: int, vk: int) -> bool:
    return bool(user32.RegisterHotKey(None, hotkey_id, modifiers | MOD_NOREPEAT, vk))


def unregister_hotkey(hotkey_id: int) -> None:
    user32.UnregisterHotKey(None, hotkey_id)


def post_thread_quit(thread_id: int) -> None:
    user32.PostThreadMessageW(wt.DWORD(thread_id), WM_QUIT, 0, 0)


# ------------------------------------------------------------------ 钩子
def install_mouse_hook(callback) -> tuple[int, object, int]:
    """安装 WH_MOUSE_LL。

    返回 (hook_handle, hook_proc_ref, error_code)。
    hook_proc_ref 必须由调用方持有，否则回调会被 GC 回收导致崩溃。
    """
    proc = HOOKPROC(callback)
    hmod = kernel32.GetModuleHandleW(None)
    handle = user32.SetWindowsHookExW(WH_MOUSE_LL, proc, hmod, 0)
    if not handle:
        return (0, proc, ctypes.get_last_error())
    return (int(handle), proc, 0)


def unhook(handle: int) -> None:
    if handle:
        user32.UnhookWindowsHookEx(ctypes.c_void_p(handle))


def call_next_hook(handle: int, n_code: int, w_param: int, l_param: int) -> int:
    return int(user32.CallNextHookEx(ctypes.c_void_p(handle), n_code, w_param, l_param))


def get_message(msg: MSG) -> int:
    return int(user32.GetMessageW(ctypes.byref(msg), None, 0, 0))


def current_thread_id() -> int:
    return int(kernel32.GetCurrentThreadId())


def platform_ok() -> bool:
    return sys.platform == "win32"
