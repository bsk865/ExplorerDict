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
import queue
import ctypes

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
        :param on_press: 轻量回调 (x, y, monotonic_time) -> None，左键按下时触发；
            用于「点别处 → 作废当前待处理选区」。允许为 None（不需要时零开销）。
        """
        self._callback = on_select_gesture
        self._on_press = on_press
        self._thread: threading.Thread | None = None
        self._hook = 0
        self._proc_ref = None
        self._handler = None
        self._thread_id = 0
        self._stop_flag = threading.Event()
        self._ready = threading.Event()
        self._ok = False
        self._error = 0
        self._down_pt: tuple[int, int] | None = None
        self._down_time = 0.0
        self._last_up_time = 0.0
        self._suppress_until = 0.0
        self._events = queue.Queue(maxsize=256)
        self._dispatch_thread = None
        self._last_event = 0.0
        self._probe_point = None
        self._left_down = False
        self._poll_pending_at = None
        self._polling_used = False
        self._health_at = 0.0
        self._retry_hook_at = 0.0

    # ------------------------------------------------------------------ API
    @property
    def running(self) -> bool:
        return self._ok and not self._stop_flag.is_set() and bool(self._thread and self._thread.is_alive())

    def start(self, timeout: float = 5.0) -> bool:
        if self._thread and self._thread.is_alive():
            return self._ok
        if self._dispatch_thread and self._dispatch_thread.is_alive():
            self._stop_flag.set()
            self._dispatch_thread.join(timeout)
            if self._dispatch_thread.is_alive():
                return False
        self._stop_flag.clear()
        self._ready.clear()
        self._events = queue.Queue(maxsize=256)
        self._dispatch_thread = threading.Thread(target=self._dispatch, name="mouse-dispatch", daemon=True)
        self._dispatch_thread.start()
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
        if self._dispatch_thread and self._dispatch_thread.is_alive():
            self._dispatch_thread.join(timeout)
        self._ok = False

    def suppress(self, seconds: float = 0.4) -> None:
        """临时忽略手势（例如刚处理完一次取词）。"""
        self._suppress_until = time.monotonic() + seconds

    def last_error(self) -> int:
        return self._error

    # --------------------------------------------------------------- 内部
    def _dispatch(self) -> None:
        while not self._stop_flag.is_set():
            try:
                args = self._events.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._callback(*args)
            except Exception:
                log.exception("鼠标事件处理失败")

    def _enqueue(self, *args) -> None:
        try:
            self._events.put_nowait(args)
        except queue.Full:
            self._down_pt = None

    def _check_health(self) -> None:
        # Windows may silently remove a slow low-level hook. Cursor movement
        # without ANY hook event indicates a lost hook; an idle mouse does not.
        point = w32.get_cursor_pos()
        moved = self._probe_point is not None and point != self._probe_point
        self._probe_point = point
        now = time.monotonic()
        if moved and now - self._last_event > 2.0 and now >= self._retry_hook_at:
            self._retry_hook_at = now + 30.0
            handle, proc, error = w32.install_mouse_hook(self._handler)
            if handle:
                w32.unhook(self._hook)
                self._hook, self._proc_ref = handle, proc
                if not self._left_down:
                    self._down_pt = None
                self._last_up_time = 0.0
                self._last_event = time.monotonic()
                log.info("鼠标监听已重新连接")
            else:
                self._error = error

    def _poll_buttons(self) -> None:
        # Some desktop environments accept WH_MOUSE_LL but never deliver its
        # callbacks. Observe only the physical left-button state, with no input
        # injection. The same gesture/capture gates apply to this fallback.
        down = w32.left_button_down()
        if down == self._left_down:
            self._poll_pending_at = None
            return
        now = time.monotonic()
        if self._poll_pending_at is None:
            self._poll_pending_at = now
            return
        # Give a native event two timer ticks to arrive; _left_down deduplicates
        # native/polled edges, so one physical gesture generates one request.
        if now - self._poll_pending_at < 0.025:
            return
        self._poll_pending_at = None
        x, y = w32.get_cursor_pos()
        info = w32.MSLLHOOKSTRUCT()
        info.pt.x, info.pt.y = x, y
        self._handle(w32.WM_LBUTTONDOWN if down else w32.WM_LBUTTONUP,
                     ctypes.addressof(info))
        if not self._polling_used:
            self._polling_used = True
            log.info("鼠标事件使用兼容检测")

    def _run(self) -> None:
        self._thread_id = w32.current_thread_id()

        def _proc(n_code, w_param, l_param):  # 运行在钩子线程
            if n_code >= 0:
                self._last_event = time.monotonic()
                try:
                    self._handle(w_param, l_param)
                except Exception:  # pragma: no cover - 钩子内绝不可抛出
                    pass
            return w32.call_next_hook(self._hook, n_code, w_param, l_param)

        self._handler = _proc
        handle, proc_ref, err = w32.install_mouse_hook(_proc)
        self._proc_ref = proc_ref  # 必须持有引用，否则回调被回收 → 崩溃
        if not handle:
            self._error = err
            self._ok = False
            self._ready.set()
            self._stop_flag.set()
            log.error("安装鼠标钩子失败 (GetLastError=%s)", err)
            return

        self._hook = handle
        self._error = 0
        self._last_event = time.monotonic()
        self._probe_point = None
        self._left_down = False
        self._down_pt = None
        self._last_up_time = 0.0
        self._poll_pending_at = None
        self._ok = True
        self._ready.set()
        log.info("鼠标钩子已安装")

        msg = w32.MSG()
        timer = w32.start_thread_timer(16)
        try:
            while not self._stop_flag.is_set():
                ret = w32.get_message(msg)
                if ret == 0 or ret == -1:
                    break
                if msg.message == w32.WM_TIMER:
                    self._poll_buttons()
                    now = time.monotonic()
                    if now >= self._health_at:
                        self._health_at = now + 1.0
                        self._check_health()
        finally:
            self._stop_flag.set()
            w32.stop_thread_timer(timer)
            w32.unhook(self._hook)
            self._hook = 0
            self._ok = False
            log.info("鼠标钩子已卸载")

    def _handle(self, w_param: int, l_param: int) -> None:
        if w_param == w32.WM_LBUTTONDOWN:
            if self._left_down:
                return
            self._left_down = True
            info = w32.MSLLHOOKSTRUCT.from_address(l_param)
            self._down_pt = (info.pt.x, info.pt.y)
            self._down_time = time.monotonic()
            if self._on_press is not None:
                try:
                    # Invalidate in-flight selections immediately, before an
                    # older result can reach the UI. This callback only takes
                    # a hit-window snapshot and updates short-lived counters.
                    self._on_press(info.pt.x, info.pt.y, self._down_time)
                except Exception:  # pragma: no cover - 钩子内绝不可抛出
                    pass
            return

        if w_param != w32.WM_LBUTTONUP:
            return
        if not self._left_down:
            return
        self._left_down = False

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
        self._enqueue(pt[0], pt[1], kind)
