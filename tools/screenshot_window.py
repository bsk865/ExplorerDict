"""把指定窗口截成 PNG（纯标准库：ctypes + GDI + zlib），无第三方依赖。

用法（**需要用户主动同意**，见下）：
    python tools/screenshot_window.py --title 探索词典 --out artifacts/main.png --allow-gui
    python tools/screenshot_window.py --hwnd 123456 --out x.png --allow-gui

执行前检查（默认拒绝）
----------------------
本工具属于「会打扰用户的 GUI 动作」：它可能把窗口提到前台（``--foreground``）
并读取屏幕像素，而用户经常在玩游戏（War Thunder）。因此：

* 默认**拒绝运行**，除非：
  1. 环境变量 ``EXPLORER_DICT_ALLOW_GUI_TESTS=1``（用户主动声明「现在可以打扰我」），或
  2. 显式加 ``--allow-gui``（仅用户主动验收时使用，脚本/测试**不得**自动加）；
* 即使同意，也要先做**实时真实前台**预检（``app.gui_preflight``）：
  只有「阅读白名单应用 + 非全屏 + 非游戏模式」才继续；否则直接退出、不读一个像素。
* ``--foreground``（SetForegroundWindow）默认关闭，且**永远不会**在预检不通过时执行。

实现说明：
* 用标准 GDI（GetWindowDC / BitBlt）抓取窗口区域，必要时先把窗口提到前台；
* 用 zlib 自己写 PNG（本机没有 Pillow，也不允许装第三方依赖）；
* 只读取窗口像素，不注入进程、不读其它进程内存。
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import struct
import sys
import time
import zlib
from pathlib import Path

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0
PW_RENDERFULLCONTENT = 0x00000002


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
user32.FindWindowW.restype = wt.HWND
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowDC.argtypes = [wt.HWND]
user32.GetWindowDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
user32.PrintWindow.restype = wt.BOOL
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.BitBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD]
gdi32.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p,
                            ctypes.POINTER(BITMAPINFO), wt.UINT]
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wt.HDC]


def enable_dpi_awareness() -> None:
    """本进程也要 DPI 感知，否则 GetWindowRect 拿到的是缩放后的虚拟坐标。

    不声明的话，在 125%/150% 缩放的多屏环境下窗口会被裁掉右侧和底部
    （表现为「内容显示不全」的截图）。
    """
    try:
        if hasattr(user32, "SetProcessDpiAwarenessContext"):
            user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            user32.SetProcessDpiAwarenessContext.restype = wt.BOOL
            if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # PER_MONITOR_V2
                return
    except (AttributeError, OSError):
        pass
    try:
        if user32.SetProcessDPIAware():
            return
    except (AttributeError, OSError):
        pass
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass


def find_window_by_title(part: str, exact: bool = False) -> int:
    """按标题找窗口（枚举顶层窗口，最大面积优先）。

    :param exact: True 时标题必须完全相等（避免匹配到别的窗口）。
    """
    matches: list[tuple[int, int]] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0 or not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        hit = (buf.value == part) if exact else (part in buf.value)
        if hit:
            r = RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            matches.append((int(hwnd), (r.right - r.left) * (r.bottom - r.top)))
        return True

    user32.EnumWindows(_cb, 0)
    if not matches:
        return 0
    matches.sort(key=lambda x: -x[1])
    return matches[0][0]


def grab_bgra(hwnd: int, *, printwindow: bool = True) -> tuple[int, int, bytes]:
    """抓取窗口区域像素，返回 (w, h, BGRA 顶行优先数据)。

    :param printwindow: 先用 ``PrintWindow(PW_RENDERFULLCONTENT)`` 让窗口**自己**
        渲染一份到内存 DC —— 这样即使窗口被别的窗口挡住（例如全屏应用在前面）
        也能拿到完整内容；失败或全黑时退回 ``BitBlt``。
    """
    rect = RECT()
    if not user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(rect)):
        raise OSError("GetWindowRect 失败")
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        raise OSError(f"窗口尺寸非法: {w}x{h}")

    src_dc = user32.GetWindowDC(wt.HWND(hwnd))
    mem_dc = gdi32.CreateCompatibleDC(src_dc)
    bmp = gdi32.CreateCompatibleBitmap(src_dc, w, h)
    old = gdi32.SelectObject(mem_dc, bmp)
    try:
        drawn = False
        if printwindow:
            try:
                drawn = bool(user32.PrintWindow(wt.HWND(hwnd), mem_dc, PW_RENDERFULLCONTENT))
            except (AttributeError, OSError):  # pragma: no cover
                drawn = False
        if not drawn:
            if not gdi32.BitBlt(mem_dc, 0, 0, w, h, src_dc, 0, 0, SRCCOPY):
                raise OSError("BitBlt 失败")
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = w
        info.bmiHeader.biHeight = -h  # 负数 = 顶行优先
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = BI_RGB
        buf = ctypes.create_string_buffer(w * h * 4)
        got = gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(info), DIB_RGB_COLORS)
        if got != h:
            raise OSError(f"GetDIBits 只拿到 {got}/{h} 行")
        data = buf.raw
        if printwindow and drawn and _looks_blank(data):
            # PrintWindow 拿到了全黑/全空内容 → 退回屏幕抓取
            if not gdi32.BitBlt(mem_dc, 0, 0, w, h, src_dc, 0, 0, SRCCOPY):
                raise OSError("BitBlt 失败")
            got = gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(info), DIB_RGB_COLORS)
            if got != h:  # pragma: no cover
                raise OSError(f"回退抓取只拿到 {got}/{h} 行")
            data = buf.raw
        return w, h, data
    finally:
        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(wt.HWND(hwnd), src_dc)


def _looks_blank(bgra: bytes, step: int = 4001) -> bool:
    """粗略判断位图是否全黑/全透明（PrintWindow 对某些窗口会这样返回）。"""
    if len(bgra) < 4:
        return True
    for i in range(0, len(bgra) - 3, step * 4):
        if bgra[i] or bgra[i + 1] or bgra[i + 2]:
            return False
    return True


def write_png(path: Path, w: int, h: int, bgra: bytes) -> None:
    """把 BGRA 数据写成 8-bit RGB PNG。"""
    raw = bytearray()
    stride = w * 4
    for y in range(h):
        row = bgra[y * stride:(y + 1) * stride]
        # BGRA -> 逐像素交错 RGB（不能分平面写，否则会得到竖条）
        row_out = bytearray(w * 3)
        row_out[0::3] = row[2::4]
        row_out[1::3] = row[1::4]
        row_out[2::3] = row[0::4]
        raw.append(0)  # filter type 0 (None)
        raw += row_out

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def _preflight_or_exit(argv: list[str]) -> None:
    """执行前检查：默认拒绝；同意后还要真实前台安全才允许继续。

    * 环境变量 ``EXPLORER_DICT_ALLOW_GUI_TESTS=1`` 或 ``--allow-gui``
      表示**用户主动**同意被打扰（脚本/测试不得自动提供）；
    * 无论是否同意，都必须通过 ``app.gui_preflight`` 的实时真实前台判定，
      否则 ``SystemExit(4)``，一个像素都不读。
    """
    if "--allow-gui" in argv:
        import os

        os.environ.setdefault("EXPLORER_DICT_ALLOW_GUI_TESTS", "1")

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app import gui_preflight as preflight

    if not preflight.user_consented():
        print(
            "[已拒绝] tools/screenshot_window.py 会创建/打扰前台并读取屏幕像素，"
            "默认不自动执行。\n"
            "  仅当**用户主动**要求截图时才运行，并二选一：\n"
            "    set EXPLORER_DICT_ALLOW_GUI_TESTS=1   （或在命令行加 --allow-gui）\n"
            f"  当前预检：{preflight.check().label}",
            file=sys.stderr,
        )
        raise SystemExit(3)

    decision = preflight.check()
    if not decision.allowed:
        print(f"[已跳过] {decision.skip_message('截图')}", file=sys.stderr)
        raise SystemExit(4)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="探索词典")
    ap.add_argument("--hwnd", type=int, default=0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--exact", action="store_true", help="标题必须完全匹配")
    ap.add_argument("--foreground", action="store_true",
                    help="截图前把窗口提到前台（默认不动用户的前台窗口）")
    ap.add_argument("--wait", type=float, default=0.6)
    ap.add_argument("--allow-gui", action="store_true",
                    help="用户主动同意被打扰（仅人工验收时使用）")
    args = ap.parse_args(argv)

    _preflight_or_exit(argv)

    hwnd = args.hwnd or find_window_by_title(args.title, exact=args.exact)
    if not hwnd:
        print(f"找不到标题匹配 {args.title!r} 的窗口", file=sys.stderr)
        return 2
    enable_dpi_awareness()
    if args.foreground:
        user32.ShowWindow(wt.HWND(hwnd), 9)  # SW_RESTORE
        user32.SetForegroundWindow(wt.HWND(hwnd))
        time.sleep(args.wait)
    w, h, data = grab_bgra(hwnd)
    out = Path(args.out)
    write_png(out, w, h, data)
    print(f"OK hwnd={hwnd} {w}x{h} -> {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())