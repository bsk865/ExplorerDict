"""探索词典桌面入口（PyInstaller windowed / onedir 的 exe 入口）。

职责只有三件事，业务逻辑仍然全在 ``bootstrap`` / ``app.main`` 里：

1. **不替用户决定打开什么**：参数原样转发（``--open-main`` 只作开发用途显式传入）；
   启动只显示浮窗，由 ``app.main`` 决定；``--diagnose`` 同样原样转发（纯只读检查）。
2. **启动失败**：用 Win32 ``MessageBoxW`` 弹一句**短中文 + 日志路径**。
   绝不回显原始异常文本 / 环境变量 —— 那里可能有 API Key（失败详情只进
   ``data/logs/startup.log``，由 bootstrap 负责脱敏落盘）。
3. **重复实例（退出码 5）**：用命名 Win32 事件（``app.activation``）向已有实例
   发一次「呼出」请求，**不是模拟按键**；旧实例不支持这个通道时，提示用户
   「正在后台运行，先完全退出再开新版」。

这个文件同时捕捉 ``bootstrap`` 的**早期构造异常**（例如数据目录不可写导致
``Bootstrap(...)`` 直接抛错）：那种时候连启动日志都可能还没建好，但用户至少
要看到一个能读懂的中文提示和日志路径，而不是「双击没反应」。
"""
from __future__ import annotations

import sys
import os
from pathlib import Path

#: 与 bootstrap / single_instance 同一个退出码：已有实例在运行
EXIT_ALREADY_RUNNING = 5
#: 与 bootstrap.EXIT_STARTUP_FAILED 同值：启动阶段失败
EXIT_STARTUP_FAILED = 4

OPEN_MAIN_FLAG = "--open-main"
DIAGNOSE_FLAG = "--diagnose"

TITLE = "探索词典"

#: 启动失败：短中文 + 日志路径（不含任何异常原文 / 密钥）
STARTUP_FAILED_TEXT = ("探索词典启动失败。\n\n"
                       "详情见日志文件：\n{log_path}")

#: 已有实例但呼出请求送不到（旧版本没有事件通道）：不杀进程、不模拟按键
ALREADY_RUNNING_TEXT = ("探索词典已在运行（当前实例仍在后台运行）。\n\n"
                        "本次呼出请求没有送达。\n"
                        "若要换用新版本：请先完全退出当前实例"
                        "（应用菜单退出，或 Ctrl+Alt+Shift+Q），"
                        "再重新打开新版本。")

#: MessageBoxW 样式：确定 + 警告图标 + 置前
_MB_OK = 0x00000000
_MB_ICONWARNING = 0x00000030
_MB_SETFOREGROUND = 0x00010000
_MB_TOPMOST = 0x00040000
_MB_FLAGS = _MB_OK | _MB_ICONWARNING | _MB_SETFOREGROUND | _MB_TOPMOST


# --------------------------------------------------------------------- 弹窗
def message_box(text: str) -> bool:
    """Win32 ``MessageBoxW``（不依赖 Tk / 不 import app.*）。

    无桌面会话 / ctypes 不可用时静默失败（返回 ``False``）——弹窗本身
    绝不能再抛异常，否则「双击没反应」又回来了。
    """
    try:
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.MessageBoxW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                       ctypes.c_wchar_p, ctypes.c_uint]
        user32.MessageBoxW.restype = ctypes.c_int
        user32.MessageBoxW(None, str(text), TITLE, _MB_FLAGS)
        return True
    except Exception:  # pragma: no cover - 无桌面 / 非 Windows：尽力而为
        return False


def _log_path_hint() -> str:
    """尽力给出启动日志路径（``<程序目录>/data/logs/startup.log``）。

    刻意**不建目录、不读数据**：只解析路径。``app.paths`` 不可用（例如
    bootstrap 早期就崩了）时用 frozen / 源码两种布局手工兜底；
    连路径都算不出来时退回相对路径，绝不抛异常。
    """
    try:
        from app import paths

        override = os.environ.get("EXPLORER_DICT_DATA_DIR")
        base = Path(override) if override else paths.project_root() / "data"
        return str(base / "logs" / "startup.log")
    except Exception:
        pass
    try:  # pragma: no cover - 极端环境：连 paths 都进不去
        return str(Path(sys.executable).resolve().parent / "data" / "logs" / "startup.log")
    except Exception:
        return r"data\logs\startup.log"


# --------------------------------------------------------------------- 启动
def _run_bootstrap(argv: list[str]) -> int:
    """导入 bootstrap 并把它不认识的参数原样交给它；任何异常都收成退出码。"""
    try:
        import bootstrap
    except BaseException:
        # 连入口模块都进不去：没有日志可写，交给调用方提示（只给路径）
        return EXIT_STARTUP_FAILED
    try:
        return int(bootstrap.main(list(argv)) or 0)
    except SystemExit as exc:  # argparse 等主动退出
        return int(exc.code or 0)
    except BaseException:
        # Bootstrap 构造早期异常 / app.main 启动异常都落在这里
        return EXIT_STARTUP_FAILED


def _recall_existing_instance() -> bool:
    """给已有实例发一次呼出请求；返回请求是否真的送达。

    事件通道不可用（旧版本 / 权限不足 / 非 Windows）时返回 ``False``：
    调用方据此提示用户「正在后台运行，先完全退出再开新版」。
    """
    try:
        from app.activation import REQUEST_SIGNALED, request_activation

        return request_activation() == REQUEST_SIGNALED
    except BaseException:
        return False


def _main(argv: list[str] | None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)

    if DIAGNOSE_FLAG in raw:
        # 诊断：不弹窗、不做 IPC（只读检查，原样转发）
        return _run_bootstrap(raw)

    # 参数原样转发：**不再**自动追加 ``--open-main``（启动只显示浮窗，
    # 主界面只由用户显式动作打开；``--open-main`` 保留给开发 / 自动化显式传入）。
    code = _run_bootstrap(raw)

    if code == EXIT_ALREADY_RUNNING:
        if not _recall_existing_instance():
            message_box(ALREADY_RUNNING_TEXT)
        return code

    if code != 0:
        message_box(STARTUP_FAILED_TEXT.format(log_path=_log_path_hint()))
    return code


def main(argv: list[str] | None = None) -> int:
    """进程入口：**任何**异常都只变成「短中文提示 + 退出码」，绝不外漏原文。"""
    try:
        return _main(argv)
    except BaseException:
        try:
            message_box(STARTUP_FAILED_TEXT.format(log_path=_log_path_hint()))
        except BaseException:  # pragma: no cover - 提示本身失败也不能再抛
            pass
        return EXIT_STARTUP_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
