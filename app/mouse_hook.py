"""全局低级鼠标钩子（WH_MOUSE_LL）。

只做两件事：

1. 判定「可能产生了新选区」的左键抬起，把坐标塞进回调（取词手势）；
2. 把「左键按下」的坐标交给 ``on_press``（用于「单击其它区域 → 作废当前待处理
   选区」；窗口显隐只由用户与硬阻断决定，钩子不负责收起任何窗口）。

回调必须极快（钩子有超时限制），所以这里只做几何判断 + 入队，
**绝不**在钩子里读 UIA、碰 Tk、写数据库。
"""
from __future__ import annotations

import threading
import time

from . import win32util as w32
from .logging_setup import get_logger

log = get_logger("mouse")

DRAG_MIN_PIXELS = 4
DOUBLECLICK_MS = 500


class MouseHook:
    """在独立线程里安装 WH_MOUSE_LL 并跑消息循环。"""

    def __init__(self, on_select_gesture, on_press=None):
        """
        :param on_select_gesture: 回调 (x, y, kind) -> None
                                  kind ∈ {"drag", "double_click"}
        :param on_press: 回调 (x, y, monotonic_time) -> None，左键按下时触发；
            用于「点别处 → 作废当前待处理选区」。允许为 None（不需要时零开销）。
        """
        self._callback = on_select_gesture
        self._on_press = on_press
        self._thread: threading.Thread | None = None
        self._hook = 0
        self._proc_ref = None
        self._thread_id = 0
        self._stop_flag = threading.Event()
        self._ready = threading.Event()
        self._ok = False
        self._error = 0
        self._down_pt: tuple[int, int] | None = None
        self._down_time = 0.0
        self._last_up_time = 0.0
        self._suppress_until = 0.0

    # ------------------------------------------------------------------ API
    @property
    def running(self) -> bool:
        return self._ok and not self._stop_flag.is_set()

    def start(self, timeout: float = 5.0) -> bool:
        if self._thread and self._thread.is_alive():
            return self._ok
        self._stop_flag.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="mouse-hook", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self._ok

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_flag.set()
        if self._thread_id:
            try:
                w32.post_thread_quit(self._thread_id)
            except OSError:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout)
        self._ok = False

    def suppress(self, seconds: float = 0.4) -> None:
        """临时忽略手势（例如刚处理完一次取词）。"""
        self._suppress_until = time.monotonic() + seconds

    def last_error(self) -> int:
        return self._error

    # --------------------------------------------------------------- 内部
    def _run(self) -> None:
        self._thread_id = w32.current_thread_id()

        def _proc(n_code, w_param, l_param):  # 运行在钩子线程
            if n_code >= 0:
                try:
                    self._handle(w_param, l_param)
                except Exception:  # pragma: no cover - 钩子内绝不可抛出
                    pass
            return w32.call_next_hook(self._hook, n_code, w_param, l_param)

        handle, proc_ref, err = w32.install_mouse_hook(_proc)
        self._proc_ref = proc_ref  # 必须持有引用，否则回调被回收 → 崩溃
        if not handle:
            self._error = err
            self._ok = False
            self._ready.set()
            log.error("安装鼠标钩子失败 (GetLastError=%s)", err)
            return

        self._hook = handle
        self._ok = True
        self._ready.set()
        log.info("鼠标钩子已安装")

        msg = w32.MSG()
        try:
            while not self._stop_flag.is_set():
                ret = w32.get_message(msg)
                if ret == 0 or ret == -1:
                    break
        finally:
            w32.unhook(self._hook)
            self._hook = 0
            self._ok = False
            log.info("鼠标钩子已卸载")

    def _handle(self, w_param: int, l_param: int) -> None:
        if w_param == w32.WM_LBUTTONDOWN:
            info = w32.MSLLHOOKSTRUCT.from_address(l_param)
            self._down_pt = (info.pt.x, info.pt.y)
            self._down_time = time.monotonic()
            if self._on_press is not None:
                try:
                    self._on_press(info.pt.x, info.pt.y, self._down_time)
                except Exception:  # pragma: no cover - 钩子内绝不可抛出
                    pass
            return

        if w_param != w32.WM_LBUTTONUP:
            return

        info = w32.MSLLHOOKSTRUCT.from_address(l_param)
        pt = (info.pt.x, info.pt.y)
        now = time.monotonic()
        if now < self._suppress_until:
            self._down_pt = None
            return

        moved = 0
        if self._down_pt is not None:
            moved = max(abs(pt[0] - self._down_pt[0]), abs(pt[1] - self._down_pt[1]))
        gap_ms = (now - self._last_up_time) * 1000.0 if self._last_up_time else 1e9
        self._last_up_time = now
        self._down_pt = None

        kind = None
        if moved >= DRAG_MIN_PIXELS:
            kind = "drag"
        elif gap_ms <= DOUBLECLICK_MS:
            kind = "double_click"
        if kind is None:
            return
        try:
            self._callback(pt[0], pt[1], kind)
        except Exception:  # pragma: no cover
            log.exception("取词回调异常")
