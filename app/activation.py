"""重复启动 → 呼出已有实例：命名 Win32 **事件**通道（不模拟按键）。

为什么需要
----------
单实例互斥体（``app.single_instance``）只让第二个实例**干净退出**：用户双击
exe 时看到的仍然是「什么都没发生」。本轮要求把这次「多余的一次双击」变成
**呼出请求** —— 已有实例把词典主界面显示到最前，而不是新开一个进程。

为什么是命名事件
----------------
* ``CreateEventW``（auto-reset）+ ``SetEvent`` + ``WaitForSingleObject(0)`` 是
  内核对象，**不会模拟按键**、不抢焦点、不注入任何输入，也不依赖窗口消息；
* 接收端由 Tk 主循环用 ``root.after(250ms)`` **零超时**轮询，绝不阻塞 UI；
* 旧版本没有这个事件：``OpenEventW`` 直接失败（ERROR_FILE_NOT_FOUND），
  desktop_entry 据此提示「正在后台运行，先完全退出再开新版」，绝不做破坏性动作。

名字与单实例**同源**
--------------------
事件名 = 前缀 + :func:`app.single_instance.data_dir_digest`（规范化数据目录
绝对路径的 **64 位签名**，16 个十六进制字符）。同一份数据目录 = 同一把锁 +
同一个事件；``EXPLORER_DICT_DATA_DIR`` / 不同副本互不串台。

失败时的取向（fail-open）
------------------------
事件创建 / 打开 / 等待失败一律**不阻止启动**（与单实例判重同一取向）：
呼出只是体验改进，不是安全边界。所有句柄都在 ``finally`` 里关闭。

可注入的 ``backend`` 让回归测试完全离线：默认后端是 ctypes，测试用假后端
即可覆盖「已存在」「不存在」「设置失败」「后端抛异常」这些分支。
"""
from __future__ import annotations

import ctypes
from pathlib import Path

from .logging_setup import get_logger
from .single_instance import data_dir_digest, default_data_dir

log = get_logger("activate")

#: 事件名**前缀**：完整名字 = 前缀 + 数据目录的 64 位签名（见 :func:`event_name_for`）
EVENT_NAME = r"Local\ExplorerDict.Activate.v1"

#: ``OpenEventW`` 需要的权限：SetEvent 用 MODIFY_STATE，轮询用 SYNCHRONIZE
EVENT_MODIFY_STATE = 0x0002
SYNCHRONIZE = 0x00100000

#: ``WaitForSingleObject`` 的返回值
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258

#: ``OpenEventW`` 找不到事件（= 旧实例没有这个通道）
ERROR_FILE_NOT_FOUND = 2
ERROR_ACCESS_DENIED = 5

#: ``request_activation`` 的三种结果（调用方据此决定提示什么）
REQUEST_SIGNALED = "signaled"        # 已请求已有实例呼出
REQUEST_NO_INSTANCE = "no_instance"  # 事件不存在：旧实例不支持自动呼出
REQUEST_FAILED = "failed"            # 事件存在但信号发不出去（权限 / 环境）


def activation_signature(data_dir=None) -> str:
    """本数据目录的 64 位签名（与单实例互斥体**同一份散列**）。"""
    return data_dir_digest(data_dir)


def event_name_for(data_dir=None) -> str:
    """按数据目录生成呼出事件名（同一目录永远同名）。"""
    return f"{EVENT_NAME}.{activation_signature(data_dir)}"


class ActivationBackend:
    """默认后端：直接调 kernel32（**只有这一个类碰真实 Win32**）。

    句柄一律按指针宽度声明（``wintypes.HANDLE`` / ``c_void_p``）：64 位 Python
    下用 ``c_int`` 接句柄会被截断，后续 ``SetEvent`` / ``CloseHandle`` 全部失效。
    """

    def __init__(self):
        import ctypes.wintypes as wt

        self._wt = wt
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL,
                                                wt.LPCWSTR]
        self._kernel32.CreateEventW.restype = wt.HANDLE
        self._kernel32.OpenEventW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
        self._kernel32.OpenEventW.restype = wt.HANDLE
        self._kernel32.SetEvent.argtypes = [wt.HANDLE]
        self._kernel32.SetEvent.restype = wt.BOOL
        self._kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
        self._kernel32.WaitForSingleObject.restype = wt.DWORD
        self._kernel32.CloseHandle.argtypes = [wt.HANDLE]
        self._kernel32.CloseHandle.restype = wt.BOOL

    def create_event(self, name: str) -> tuple[int, int]:
        """创建 auto-reset 命名事件，返回 ``(句柄, 错误码)``（句柄 0 = 失败）。"""
        ctypes.set_last_error(0)
        # 参数：安全属性 / bManualReset=False（auto-reset）/ 初始未触发 / 名字
        handle = self._kernel32.CreateEventW(None, False, False, str(name))
        err = int(ctypes.get_last_error())
        if not handle:
            return 0, err
        return int(handle), 0

    def open_event(self, name: str) -> tuple[int, int]:
        """打开已有事件，返回 ``(句柄, 错误码)``（句柄 0 = 不存在 / 无权限）。"""
        ctypes.set_last_error(0)
        handle = self._kernel32.OpenEventW(EVENT_MODIFY_STATE | SYNCHRONIZE, False,
                                           str(name))
        err = int(ctypes.get_last_error())
        if not handle:
            return 0, err
        return int(handle), 0

    def set_event(self, handle: int) -> bool:
        return bool(self._kernel32.SetEvent(self._wt.HANDLE(int(handle))))

    def wait(self, handle: int, timeout_ms: int) -> int:
        """``WaitForSingleObject``；``timeout_ms=0`` 表示**非阻塞**检查一次。"""
        return int(self._kernel32.WaitForSingleObject(self._wt.HANDLE(int(handle)),
                                                      int(timeout_ms)))

    def close_handle(self, handle: int) -> None:
        self._kernel32.CloseHandle(self._wt.HANDLE(int(handle)))


class ActivationReceiver:
    """已有实例侧：持有一枚 auto-reset 命名事件，非阻塞地收「呼出」请求。

    用法::

        receiver = ActivationReceiver()
        receiver.create()          # 失败只是「不能外部呼出」，不阻止启动
        ...
        if receiver.poll():        # WaitForSingleObject(handle, 0)
            app.open_main_window()
        ...
        receiver.close()           # 退出前释放（进程结束内核也会回收）
    """

    def __init__(self, *, name: str | None = None, backend=None, logger=None,
                 data_dir=None):
        self.data_dir = default_data_dir() if data_dir is None else Path(data_dir)
        self.name = str(name) if name else event_name_for(self.data_dir)
        self.handle = 0
        self.created = False
        self.backend_error = 0
        self._backend = backend if backend is not None else ActivationBackend()
        self._log = logger if logger is not None else log

    # ------------------------------------------------------------- 创建
    def create(self) -> bool:
        """创建事件（幂等）。失败返回 ``False``，但**绝不抛出**。"""
        if self.handle:
            return True
        try:
            handle, err = self._backend.create_event(self.name)
        except Exception as exc:  # pragma: no cover - 极端环境：不影响启动
            self._log.warning("呼出事件不可用（%s），继续启动", exc.__class__.__name__)
            return False
        if not handle:
            self.backend_error = int(err or 0)
            self._log.warning("呼出事件创建失败（错误码 %s），继续启动",
                              self.backend_error)
            return False
        self.handle = int(handle)
        self.created = True
        self._log.debug("呼出事件已就绪")
        return True

    # ------------------------------------------------------------- 轮询
    def poll(self) -> bool:
        """零超时检查一次：有请求返回 ``True``（auto-reset 自动消费掉）。"""
        if not self.handle:
            return False
        try:
            result = int(self._backend.wait(self.handle, 0))
        except Exception:  # pragma: no cover - 后端异常不该打断 UI 轮询
            self._log.debug("读取呼出事件失败", exc_info=True)
            return False
        return result == WAIT_OBJECT_0

    # ------------------------------------------------------------- 释放
    def close(self) -> bool:
        """释放事件句柄（幂等；已经释放时是安全空操作）。"""
        handle, self.handle = self.handle, 0
        self.created = False
        if not handle:
            return False
        try:
            self._backend.close_handle(handle)
        except Exception:  # pragma: no cover - 释放失败不该影响退出
            self._log.debug("关闭呼出事件失败", exc_info=True)
        return True

    def __enter__(self) -> "ActivationReceiver":
        self.create()
        return self

    def __exit__(self, *_exc) -> bool:
        self.close()
        return False


def request_activation(data_dir=None, *, backend=None, logger=None) -> str:
    """向**已有实例**发一次呼出请求（``SetEvent``，不模拟按键）。

    返回 :data:`REQUEST_SIGNALED` / :data:`REQUEST_NO_INSTANCE` /
    :data:`REQUEST_FAILED`；句柄在 ``finally`` 里关闭，绝不泄漏。
    """
    log_ = logger if logger is not None else log
    backend_ = backend if backend is not None else ActivationBackend()
    base = default_data_dir() if data_dir is None else data_dir
    name = event_name_for(base)
    handle = 0
    try:
        handle, err = backend_.open_event(name)
        if not handle:
            if int(err or 0) == ERROR_FILE_NOT_FOUND:
                log_.info("没有可呼出的实例（事件不存在：旧版本未监听）")
                return REQUEST_NO_INSTANCE
            if int(err or 0) == ERROR_ACCESS_DENIED:
                log_.warning("呼出事件被拒绝访问（可能是更高权限的实例）")
                return REQUEST_FAILED
            log_.warning("打开呼出事件失败（错误码 %s）", err)
            return REQUEST_FAILED
        try:
            ok = bool(backend_.set_event(handle))
        except Exception as exc:  # pragma: no cover - 后端异常按失败处理
            log_.warning("发送呼出请求失败（%s）", exc.__class__.__name__)
            return REQUEST_FAILED
        if not ok:
            log_.warning("发送呼出请求失败（SetEvent 返回失败）")
            return REQUEST_FAILED
        log_.info("已请求已有实例呼出主界面")
        return REQUEST_SIGNALED
    except Exception as exc:  # pragma: no cover - 环境不支持时静默降级
        log_.warning("发送呼出请求失败（%s）", exc.__class__.__name__)
        return REQUEST_FAILED
    finally:
        if handle:
            try:
                backend_.close_handle(handle)
            except Exception:  # pragma: no cover - 关闭失败不影响结果
                pass
