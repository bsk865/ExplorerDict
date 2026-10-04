"""全局热键（RegisterHotKey + 独立消息循环线程）。

Windows 上 RegisterHotKey(NULL, ...) 会把 WM_HOTKEY 投递到调用线程的消息队列，
所以必须在自己的线程里注册并跑 GetMessage 循环，再把事件转交给 UI 线程。

快捷键一律使用 **Ctrl+Alt+Shift+字母**：
`Ctrl+Alt+D` 等两段组合在不少环境里已被占用（例如 DSH Desktop 占用 Ctrl+Alt+D），
三段组合冲突概率低得多。启动时必须**单独校验「呼出键」是否注册成功**，
不能因为「其它热键注册上了」就当作通过。
"""
from __future__ import annotations

import threading

from . import win32util as w32
from .logging_setup import get_logger

log = get_logger("hotkey")

MODS = w32.MOD_CONTROL | w32.MOD_ALT | w32.MOD_SHIFT

# id 常量（被引用处不要写裸数字）
HK_RECALL = 1      # 呼出/显示浮窗 + 主界面
HK_GRAB = 2        # 立即抓取选区
HK_PAUSE = 3       # 暂停/恢复取词
HK_GAME_MODE = 4   # 切换手动游戏模式
HK_QUIT = 5        # 退出应用

# 必须注册成功的「关键热键」：呼出键。浮窗隐藏后它是唯一快捷键入口。
REQUIRED_HOTKEYS: tuple[int, ...] = (HK_RECALL,)

# id -> (显示名, 修饰键, 虚拟键)
HOTKEYS: dict[int, tuple[str, int, int]] = {
    HK_RECALL: ("Ctrl+Alt+Shift+D  呼出浮窗/主界面", MODS, 0x44),
    HK_GRAB: ("Ctrl+Alt+Shift+S  立即抓取选区", MODS, 0x53),
    HK_PAUSE: ("Ctrl+Alt+Shift+P  暂停/恢复取词", MODS, 0x50),
    HK_GAME_MODE: ("Ctrl+Alt+Shift+G  游戏模式开/关", MODS, 0x47),
    HK_QUIT: ("Ctrl+Alt+Shift+Q  退出应用", MODS, 0x51),
}


def labels_for(hotkey_ids) -> str:
    return "、".join(HOTKEYS[i][0] for i in hotkey_ids if i in HOTKEYS)


def required_failures(registered) -> list[int]:
    """返回**必须注册却失败**的热键 id（当前只有呼出键）。

    设计要点：只要呼出键失败就必须在主界面明确提醒，
    不能因为「其它热键注册成功了」而当成通过。
    """
    reg = set(registered)
    return [hid for hid in REQUIRED_HOTKEYS if hid not in reg]


class HotkeyManager:
    def __init__(self, on_hotkey):
        """:param on_hotkey: 回调 (hotkey_id) -> None，运行在热键线程。"""
        self._callback = on_hotkey
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._stop_flag = threading.Event()
        self._ready = threading.Event()
        self._registered: list[int] = []
        self._failed: list[tuple[int, str]] = []

    @property
    def registered(self) -> list[int]:
        return list(self._registered)

    @property
    def failed(self) -> list[tuple[int, str]]:
        return list(self._failed)

    @property
    def recall_registered(self) -> bool:
        """呼出键是否注册成功（启动检查必须看这个）。"""
        return HK_RECALL in self._registered

    def missing_required(self) -> list[int]:
        return required_failures(self._registered)

    def start(self, timeout: float = 5.0) -> bool:
        if self._thread and self._thread.is_alive():
            return self.recall_registered
        self._stop_flag.clear()
        self._ready.clear()
        self._registered.clear()
        self._failed.clear()
        self._thread = threading.Thread(target=self._run, name="hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self.recall_registered

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_flag.set()
        if self._thread_id:
            try:
                w32.post_thread_quit(self._thread_id)
            except OSError:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout)

    def restart(self, timeout: float = 5.0) -> bool:
        """重新注册全部热键。

        必须在**热键线程**上注册：``RegisterHotKey(NULL, ...)`` 把 WM_HOTKEY
        投递到调用线程的消息队列，所以「重试」只能重建线程，不能在 UI 线程直接注册。
        """
        self.stop()
        return self.start(timeout)

    def _run(self) -> None:
        self._thread_id = w32.current_thread_id()
        for hid, (label, mods, vk) in HOTKEYS.items():
            if w32.register_hotkey(hid, mods, vk):
                self._registered.append(hid)
            else:
                self._failed.append((hid, label))
                log.warning("热键注册失败（可能被占用）: %s", label)
        self._ready.set()

        msg = w32.MSG()
        try:
            while not self._stop_flag.is_set():
                ret = w32.get_message(msg)
                if ret == 0 or ret == -1:
                    break
                if msg.message == w32.WM_HOTKEY:
                    try:
                        self._callback(int(msg.wParam))
                    except Exception:  # pragma: no cover
                        log.exception("热键回调异常")
        finally:
            for hid in self._registered:
                w32.unregister_hotkey(hid)
            self._registered.clear()
