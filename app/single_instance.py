"""单实例保护：用 Win32 **命名互斥体**做原子判重（不建 IPC、不杀进程）。

为什么需要
----------
用户机器上曾经同时跑着 6 个旧实例，随后出现「呼出热键注册失败」——
每个实例都会 `RegisterHotKey(Ctrl+Alt+Shift+D/G/S/P/Q)`，而热键是**独占**的：
后启动的实例拿不到，界面上就报「热键被占用」，用户看到的却是「快捷键没反应」。
旧实现里没有任何判重，双击几次就有几份实例同时抢热键、同时轮询前台。

为什么是互斥体
--------------
``CreateMutexW`` + ``GetLastError() == ERROR_ALREADY_EXISTS`` 是**原子**的：
内核保证只有一个调用者拿到「新创建」的结果，不存在「先查再建」的竞态。
因此：

* **只判重、不通信**：这里没有共享内存、没有命名管道、没有窗口消息；
* **不杀任何进程**：检测到已有实例时只是**自己退出**（退出码由调用方决定）；
* **不弹窗**：pythonw 下没有控制台，弹窗在无桌面会话时还会二次失败；
* **不使用命名事件**，不打扰已有实例（把「唤起已有窗口」留给用户自己）。

失败时的取向（fail-open）
------------------------
互斥体创建失败（极端环境 / ctypes 不可用）时**不阻止启动**：单实例是体验
改进，不是安全边界。真正必须保持的硬门控（游戏 / 全屏 / 暂停）与本模块无关。

可注入的 ``backend`` 让回归测试完全离线：默认后端是 ctypes，测试用假后端
就能覆盖「已有实例」「创建失败」「释放」「重复释放」这些分支。

名字按**数据目录**区分
----------------------
互斥体名字不再是一个固定字符串，而是「前缀 + 规范化数据目录绝对路径的 hash」
（:func:`mutex_name_for`）：项目副本 / ``EXPLORER_DICT_DATA_DIR`` 不同就是两份
互不相干的数据，硬绑同一个名字会让第二个副本「莫名其妙启动不了」。
hash 统一由 :func:`data_dir_digest` 产出（64 位签名），``app.activation`` 的
「呼出已有实例」事件名复用同一份签名 —— 同一份数据 = 同一把锁 + 同一个事件。

进程级只加一次锁
----------------
:bootstrap: 与 ``python -m app.main`` 是同一个进程里的两层入口：
:func:`acquire_process_guard` 保证它们**只真正 CreateMutexW 一次**（后者复用
前者持有的锁），:func:`release_process_guard` 只在 ``finally`` 里释放本进程
持有的那一枚。
"""
from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path

from .logging_setup import get_logger
from .paths import project_root

log = get_logger("single")

#: 互斥体名字的**前缀**：完整名字 = 前缀 + 数据目录绝对路径的 hash
#: （见 :func:`mutex_name_for`）。``Local\`` = 当前登录会话：同一用户的多会话
#: （远程桌面）各自允许一份实例，符合「双击启动器的人只想看到一个窗口」的直觉。
MUTEX_NAME = r"Local\ExplorerDict.SingleInstance.v2"

#: 数据目录散列的十六进制长度：16 个字符 = **64 位签名**。
#: 互斥体名（本模块）与呼出事件名（``app.activation``）共用同一份签名，
#: 因此「同一份数据目录」= 同一把锁 + 同一个呼出通道，两份副本互不串台。
DATA_DIR_DIGEST_HEX = 16

ERROR_ALREADY_EXISTS = 183
ERROR_ACCESS_DENIED = 5

#: 退出码：检测到另一个实例已经在跑（调用方据此写日志 / 返回进程码）
EXIT_ALREADY_RUNNING = 5

_SYNCHRONIZE = 0x00100000


def default_data_dir() -> Path:
    """本进程的数据目录（与 ``app.paths.data_dir`` 同一判据，但**不建目录**）。

    与 ``app.paths`` 共用同一个 :func:`app.paths.project_root`：源码下是项目内的
    ``data/``，frozen（PyInstaller）下是 exe 同级的 ``data/``。
    这里只解析路径，**绝不** ``mkdir``：判重 / 发呼出请求时顺手建目录会在
    只读介质或权限不足的位置留下副作用。
    """
    override = os.environ.get("EXPLORER_DICT_DATA_DIR")
    if override:
        return Path(override)
    return project_root() / "data"


def _normalized_path(path) -> str:
    """把目录规范化为可比较的字符串：绝对路径 + 斜杠统一 + 大小写折叠。"""
    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, RuntimeError):  # pragma: no cover - 极端路径
        resolved = Path(path).absolute()
    return str(resolved).replace("\\", "/").rstrip("/").casefold()


def data_dir_digest(data_dir=None) -> str:
    """数据目录绝对路径的 **64 位签名**（16 个十六进制字符）。

    单实例互斥体名与呼出事件名都从这里取后缀，保证「同一份数据 = 同一把锁 +
    同一个事件」，不会因为两处各写一套散列而错位。
    """
    base = default_data_dir() if data_dir is None else data_dir
    return hashlib.sha256(_normalized_path(base).encode("utf-8")).hexdigest()[
        :DATA_DIR_DIGEST_HEX]


def mutex_name_for(data_dir=None) -> str:
    """按**数据目录绝对路径**的 hash 生成互斥体名（同一目录永远同名）。"""
    return f"{MUTEX_NAME}.{data_dir_digest(data_dir)}"


class SingleInstanceBackend:
    """默认后端：直接调 kernel32（**只有这一个类碰真实 Win32**）。"""

    def __init__(self):
        import ctypes.wintypes as wt

        self._wt = wt
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
        self._kernel32.CreateMutexW.restype = wt.HANDLE
        self._kernel32.CloseHandle.argtypes = [wt.HANDLE]
        self._kernel32.CloseHandle.restype = wt.BOOL

    def create_mutex(self, name: str) -> tuple[int, bool, int]:
        """创建 / 打开命名互斥体，返回 ``(句柄, 是否已存在, 错误码)``。"""
        ctypes.set_last_error(0)
        handle = self._kernel32.CreateMutexW(None, False, str(name))
        err = int(ctypes.get_last_error())
        if not handle:
            return 0, False, err
        return int(handle), bool(err == ERROR_ALREADY_EXISTS), 0

    def close_handle(self, handle: int) -> None:
        self._kernel32.CloseHandle(self._wt.HANDLE(int(handle)))


class SingleInstance:
    """进程内的单实例闸门（拿到 / 释放一个命名互斥体）。

    用法::

        guard = SingleInstance()
        if not guard.acquire():
            log.warning("已经有一个实例在运行")
            return EXIT_ALREADY_RUNNING
        ...
        guard.release()          # 退出前释放（进程结束内核也会释放）

    ``reason`` 里是中文诊断文本（写进 startup.log 用，不弹窗）。
    """

    def __init__(self, name: str | None = None, *, backend=None, logger=None,
                 data_dir=None):
        self.data_dir = default_data_dir() if data_dir is None else Path(data_dir)
        #: 互斥体名：显式传名优先，否则由**数据目录**推导（见 :func:`mutex_name_for`）
        self.name = str(name) if name else mutex_name_for(self.data_dir)
        self.reason = ""
        self.handle = 0
        self.already_running = False
        self.created = False
        self.acquired = False
        self.backend_error = 0
        self._backend = backend if backend is not None else SingleInstanceBackend()
        self._log = logger if logger is not None else log

    # ------------------------------------------------------------- 获取
    def acquire(self) -> bool:
        """尝试成为唯一实例。

        返回 ``True``：可以继续启动（本进程拿到了互斥体，或环境不支持判重）。
        返回 ``False``：**已经有一个实例在运行**，调用方应当直接退出。
        """
        if self.acquired:
            return True
        try:
            handle, existed, err = self._backend.create_mutex(self.name)
        except Exception as exc:  # pragma: no cover - 极端环境：不阻止启动
            self.reason = f"单实例保护不可用（{exc.__class__.__name__}: {exc}），继续启动"
            self._log.warning(self.reason)
            self.created = False
            return True

        if not handle:
            self.backend_error = int(err or 0)
            if self.backend_error == ERROR_ACCESS_DENIED:
                self.reason = "单实例互斥体被拒绝访问（可能是更高权限的实例），继续启动"
            else:
                self.reason = f"单实例互斥体创建失败（错误码 {self.backend_error}），继续启动"
            self._log.warning(self.reason)
            return True

        self.handle = int(handle)
        self.created = True
        if existed:
            # 已经有了：**不放掉这枚引用**（句柄留到进程退出由内核回收）。
            # 提前 CloseHandle 会留下一个空档：第一个实例若正好在此时退出，
            # 第三个实例就会把互斥体当成「新建」的。持有到退出才没有这个窗口。
            self.already_running = True
            self.reason = "已经有一个探索词典实例在运行（不重复注册热键），本次启动退出"
            self._log.info(self.reason)
            return False

        self.acquired = True
        self.reason = "单实例互斥体已获取"
        self._log.debug(self.reason)
        return True

    # ------------------------------------------------------------- 释放
    def release(self) -> bool:
        """释放互斥体（幂等；没拿到 / 已释放时什么都不做）。"""
        if not self.handle:
            self.acquired = False
            return False
        self._close_quietly()
        self.acquired = False
        self.created = False
        return True

    def _close_quietly(self) -> None:
        handle, self.handle = self.handle, 0
        if not handle:
            return
        try:
            self._backend.close_handle(handle)
        except Exception:  # pragma: no cover - 释放失败不该影响退出
            self._log.debug("释放单实例互斥体失败", exc_info=True)

    def __enter__(self) -> "SingleInstance":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> bool:
        self.release()
        return False


# --------------------------------------------------------------- 进程级闸门
#: 本进程当前持有的闸门。``bootstrap`` 与 ``python -m app.main`` 是同一进程的
#: 两层入口：后者必须**复用**前者拿到的锁，否则「自己锁自己」，第二条入口永远
#: 进不去（也会多创建一个同名互斥体句柄）。
_ACTIVE: SingleInstance | None = None


def process_guard() -> SingleInstance | None:
    """本进程当前持有的单实例闸门（没有则 ``None``）。"""
    return _ACTIVE


def acquire_process_guard(*, name: str | None = None, backend=None, logger=None,
                          data_dir=None) -> tuple[SingleInstance, int | None]:
    """**进程级**获取单实例闸门：同一个进程只真正加锁一次。

    返回 ``(guard, 退出码或 None)``：退出码为 ``None`` 表示可以继续启动；
    非 ``None`` 表示已有实例在跑，调用方直接返回它（拿到的那枚句柄**不释放**，
    见 :meth:`SingleInstance.acquire`）。

    首次调用之外的调用一律返回同一个 guard，且不会再多调一次 ``CreateMutexW``。
    """
    global _ACTIVE
    if _ACTIVE is not None:
        return _ACTIVE, None
    guard = SingleInstance(name=name, backend=backend, logger=logger, data_dir=data_dir)
    if guard.acquire():
        _ACTIVE = guard
        return guard, None
    return guard, int(EXIT_ALREADY_RUNNING)


def release_process_guard(guard: "SingleInstance | None" = None) -> bool:
    """释放**本进程持有**的闸门（幂等；入口的 ``finally`` 调用）。

    只认当前记录在案的那一个：传入「已有实例」那次调用留下的句柄（不是本进程
    持有的锁）时什么都不做 —— 那枚句柄必须留到进程退出，提前关闭会给出错误的
    「现在没有实例」窗口。
    """
    global _ACTIVE
    target = _ACTIVE
    if target is None or (guard is not None and guard is not target):
        return False
    _ACTIVE = None
    return bool(target.release())
