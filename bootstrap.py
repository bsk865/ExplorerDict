"""探索词典启动诊断入口（pythonw / 控制台通用）。

为什么需要它
------------
pythonw.exe 没有控制台：`stdout` / `stderr` 是无效句柄，任何早期阶段打印都会
静默丢失，甚至连 ``import app.main`` 里的模块级错误也看不到——表现就是
「双击 启动.cmd 后什么都没发生，也没有进程」。这个入口在**导入 app.main 之前**
先把启动上下文、stdout/stderr 与启动阶段异常落到 ``data/logs/startup.log``，
所以「没有窗口」时至少还有日志可查。

用法
----
    pythonw bootstrap.py                # 正常启动（无控制台）
    python  bootstrap.py --console-log  # 控制台启动，保留屏幕输出
    python  bootstrap.py --diagnose     # 只导入组件 / 检查路径与 tkinter，不建窗

约束
----
* 只写项目 ``data/logs/startup.log``（或 EXPLORER_DICT_DATA_DIR 指向的隔离目录）；
* 绝不打印环境变量（避免 API Key 等密钥落盘）；
* 初始化失败只写日志 + 返回退出码，**不弹窗**（弹窗在无桌面会话时会二次失败）；
* 不回显到屏幕以外的任何网络/外部工具。

这个文件**只做启动诊断**，不含任何业务逻辑，也不改变业务门控行为。
"""
from __future__ import annotations

import argparse
import atexit
import io
import os
import shutil
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

#: 启动根目录：源码下 = ``bootstrap.py`` 所在目录；frozen（PyInstaller）下 =
#: ``sys.executable`` 所在目录。与 ``app.paths.project_root`` **同一判据**，
#: 因此日志、``data/`` 以及本文件里 ``ROOT / "data"`` 的兜底都指向同一个位置
#: （冻结后仍然是 exe 旁边的 ``data/``，不迁移、不新建第二份数据）。
ROOT = (Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False) else Path(__file__).resolve().parent)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LOG_NAME = "startup.log"
EXIT_OK = 0
EXIT_DIAGNOSE_FAILED = 3
EXIT_STARTUP_FAILED = 4
#: 已经有一个实例在运行（与 app.single_instance.EXIT_ALREADY_RUNNING 同值）
EXIT_ALREADY_RUNNING = 5

# 需要抽查的组件（--diagnose 只做 import，不实例化、不建窗）
_DIAGNOSE_MODULES = (
    "app",
    "app.paths",
    "app.win32util",
    "app.db",
    "app.config",
    "app.gate",
    "app.capture_service",
    "app.explain_service",
    "app.export_service",
    "app.search_service",
    "app.source_enrich",
    "app.map_service",
    "app.map_export",
    "app.hotkeys",
    "app.mouse_hook",
    "app.uia_bridge",
    # 界面模块也抽查：冻结运行时里少一个标准库子模块（例如 tkinter.ttk）就是
    # 「双击 exe 直接弹启动失败」，而这类错误只有 import 才暴露得出来。
    "app.ui.theme",
    "app.ui.widgets",
    "app.ui.glyph_text",
    "app.ui.concept_map",
    "app.ui.map_templates",
    "app.ui.template_dialog",
    "app.ui.relation_editor",
    "app.ui.edge_block_dialog",
    "app.ui.shortcuts_dialog",
    "app.ui.reading_panel",
    "app.ui.settings_dialog",
    "app.ui.setup_wizard",
    "app.ui.batch_dialogs",
    "app.main",
)
_DIAGNOSE_PATHS = ("data_dir", "logs_dir", "db_path", "log_path", "uia_helper_script")


class _TeeStream(io.TextIOBase):
    """把写入同时送到控制台（若存在）与日志文件。

    控制台保留原有输出，pythonw 下控制台句柄不可用时只写文件，绝不抛异常。
    """

    def __init__(self, sink, console=None):
        self._sink = sink
        self._console = console

    # -- io 接口 -------------------------------------------------------
    def writable(self) -> bool:  # pragma: no cover - 协议要求
        return True

    def write(self, text) -> int:
        if not isinstance(text, str):
            try:
                text = str(text)
            except Exception:
                return 0
        if not text:
            return 0
        try:
            self._sink.write(text)
        except Exception:
            pass
        if self._console is not None:
            try:
                self._console.write(text)
                self._console.flush()
            except Exception:
                self._console = None
        return len(text)

    def flush(self) -> None:
        for stream in (self._sink, self._console):
            if stream is None:
                continue
            try:
                stream.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        return False

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "utf-8"


class _StartupLog:
    """启动日志：行缓冲写文件 + 关键行同时回显控制台。"""

    def __init__(self, path: Path, console: bool):
        self.path = path
        self.console = console
        self._fh = None
        self._fallback = False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # line buffering：崩溃时已写入的行不会留在缓冲区里
            self._fh = open(path, "a", encoding="utf-8", buffering=1, errors="replace")
        except OSError:
            self._fh = None
            self._fallback = True

    # -- 写入 ----------------------------------------------------------
    def line(self, text: str, *, echo: bool = False) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = f"{stamp} {text}"
        if self._fh is not None:
            try:
                self._fh.write(row + "\n")
            except Exception:
                self._close()
        if echo and self.console:
            try:
                sys.__stderr__.write(row + "\n")
                sys.__stderr__.flush()
            except Exception:
                pass

    def _close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None

    def close(self) -> None:
        self._close()


def _redact(text: str) -> str:
    """复用 app 的脱敏规则；不可用时做最小兜底，绝不因脱敏本身报错。"""
    try:
        from app.logging_setup import redact

        return redact(text)
    except Exception:
        return text


def _has_console(stream) -> bool:
    """判断 stdio 是否可用（pythonw 下为 None 或 invalid handle）。"""
    if stream is None:
        return False
    try:
        stream.fileno()
    except Exception:
        return False
    return True


def _describe_stream(stream) -> str:
    if stream is None:
        return "none"
    if not _has_console(stream):
        return "unusable"
    try:
        return "ok" if stream.isatty() else "ok(redirected)"
    except Exception:
        return "ok"


def _resolve_pythonw() -> str:
    """解析「启动.cmd 会选中的 pythonw.exe 绝对路径」（与 .cmd 的顺序一致）。"""
    found = shutil.which("pythonw.exe")
    if found:
        return str(Path(found).resolve())
    for ver in ("Python314", "Python313", "Python312", "Python311"):
        cand = (Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python"
                / ver / "pythonw.exe")
        if cand.exists():
            return str(cand)
    return ""


def _read_launcher_info(path: Path) -> dict[str, str]:
    """读取启动.cmd 写下的 sidecar（key=value，UTF-8）。

    .cmd 本身无法安全输出 UTF-8，所以由这里统一折进 startup.log，
    保证整个日志是单一编码、中文路径不出现乱码。
    """
    info: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return info
    for row in text.splitlines():
        key, sep, value = row.partition("=")
        if sep and key.strip():
            info[key.strip()] = value.strip()
    return info


def _resolve_data_dir() -> Path:
    """与 app.paths 完全一致：优先 EXPLORER_DICT_DATA_DIR（隔离测试目录）。"""
    try:
        from app import paths

        return Path(paths.data_dir())
    except Exception:
        override = os.environ.get("EXPLORER_DICT_DATA_DIR")
        d = Path(override) if override else (ROOT / "data")
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return d


class Bootstrap:
    """启动流程上下文：记录阶段、耗时与异常。"""

    def __init__(self, argv: list[str], *, console_log: bool, diagnose: bool):
        self.argv = list(argv)
        self.console_log = console_log
        self.diagnose = diagnose
        self.data_dir = _resolve_data_dir()
        self.log = _StartupLog(self.data_dir / "logs" / LOG_NAME,
                               console=console_log)
        self.launcher = _read_launcher_info(self.data_dir / "logs" / "launcher.env")
        self.resolved_pythonw = _resolve_pythonw()
        self.phase = "start"
        self._orig_out = sys.stdout
        self._orig_err = sys.stderr
        self._t0 = time.monotonic()

    # -- 阶段 ----------------------------------------------------------
    def enter(self, phase: str) -> None:
        self.phase = phase
        self.log.line(f"PHASE {phase}", echo=self.console_log)

    def note(self, text: str) -> None:
        self.log.line(f"      {_redact(text)}", echo=self.console_log)

    def error(self, text: str) -> None:
        self.log.line(f"ERROR {_redact(text)}", echo=self.console_log)

    # -- 启动上下文 ----------------------------------------------------
    def install_streams(self) -> None:
        """把 stdout/stderr 接进日志。控制台仍保留原样输出。"""
        out_console = self._orig_out if _has_console(self._orig_out) else None
        err_console = self._orig_err if _has_console(self._orig_err) else None
        sys.stdout = _TeeStream(self.log._fh or io.StringIO(), out_console)
        sys.stderr = _TeeStream(self.log._fh or io.StringIO(), err_console)

    def write_header(self) -> None:
        frozen = bool(getattr(sys, "frozen", False))
        self.log.line("=" * 68, echo=self.console_log)
        self.log.line(f"== 探索词典启动 {datetime.now():%Y-%m-%d %H:%M:%S} ==",
                      echo=self.console_log)
        self.log.line(f"python      : {sys.version.split()[0]} ({'64bit' if sys.maxsize > 2**32 else '32bit'})")
        self.log.line(f"executable  : {sys.executable}")
        self.log.line(f"pythonw_run : {_is_pythonw()}")
        self.log.line(f"frozen      : {frozen}")
        self.log.line(f"script      : {Path(__file__).resolve()}")
        self.log.line(f"cwd         : {os.getcwd()}")
        self.log.line(f"project_root: {ROOT}")
        self.log.line(f"data_dir    : {self.data_dir}")
        self.log.line(f"log_path    : {self.log.path}")
        self.log.line(f"argv        : {self.argv!r}")
        self.log.line(f"stdout      : {_describe_stream(self._orig_out)}")
        self.log.line(f"stderr      : {_describe_stream(self._orig_err)}")
        self.log.line(f"console_log : {self.console_log}   diagnose: {self.diagnose}")
        # 启动器解析出的解释器绝对路径（关键：确认到底跑的是哪个 Python）
        self.log.line(f"pythonw_res : {self.resolved_pythonw or '<not found>'}")
        if self.launcher:
            for key in ("cmd", "pythonw", "python", "bootstrap", "cwd"):
                if key in self.launcher:
                    self.log.line(f"launcher.{key}: {self.launcher[key]}")
            chosen = self.launcher.get("pythonw") or self.launcher.get("python") or ""
            if chosen and self.resolved_pythonw:
                self.log.line(f"pythonw_match: "
                              f"{_same_path(chosen, self.resolved_pythonw)}")
        else:
            self.log.line("launcher    : <no launcher.env; started directly>")
        # 刻意不打印任何环境变量值（可能含 API Key）
        self.log.line("env         : <not recorded on purpose (may contain secrets)>")

    # -- 收尾 ----------------------------------------------------------
    def finish(self, code: int) -> int:
        self.log.line(f"DONE exit={code} elapsed={time.monotonic() - self._t0:.2f}s")
        self.log.close()
        return code


def _same_path(a: str, b: str) -> bool:
    """Windows 下大小写不敏感地比较两个路径是否指向同一文件。"""
    try:
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
    except OSError:  # pragma: no cover
        return os.path.normcase(a) == os.path.normcase(b)


def _is_pythonw() -> bool:
    try:
        return Path(sys.executable).name.lower() == "pythonw.exe"
    except Exception:  # pragma: no cover
        return False


def _install_hooks(boot: Bootstrap) -> None:
    """启动阶段异常也要落盘（正常启动后由 app.main 接管 excepthook）。"""

    def _hook(exc_type, exc, tb) -> None:
        boot.error("未捕获异常: " + "".join(traceback.format_exception(exc_type, exc, tb)))
        boot.log.line(f"DONE exit={EXIT_STARTUP_FAILED} (unhandled "
                      f"{exc_type.__name__})")
        boot.log.close()

    sys.excepthook = _hook

    def _thread_hook(args) -> None:
        boot.error("线程未捕获异常: " + "".join(
            traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))

    try:
        import threading

        threading.excepthook = _thread_hook
    except Exception:  # pragma: no cover
        pass


# --------------------------------------------------------------------- 诊断
def run_diagnose(boot: Bootstrap) -> int:
    """只做「导入 + 路径 + tkinter 可导入」检查，然后写状态退出。

    硬约束：绝不 ``tk.Tk()`` 建窗、绝不装 hook / 鼠标钩子 / UIA helper、
    绝不碰数据库与配置，因此不会弹窗、不会干扰门控。
    """
    from app import paths

    failures: list[str] = []

    boot.enter("diagnose:entry")
    boot.note(f"interpreter: {sys.executable}")
    boot.note(f"pythonw    : {_is_pythonw()}")
    boot.note(f"pythonw_res: {boot.resolved_pythonw or '<not found>'}")
    boot.note(f"bootstrap  : {Path(__file__).resolve()}")
    boot.note(f"launcher   : {boot.launcher or '<none>'}")
    if boot.resolved_pythonw and not Path(boot.resolved_pythonw).exists():
        failures.append(f"解析出的 pythonw 不存在: {boot.resolved_pythonw}")
    if _is_pythonw() and boot.resolved_pythonw:
        if not _same_path(sys.executable, boot.resolved_pythonw):
            boot.note("NOTE: 实际解释器与 PATH 解析出的 pythonw 不同（可能是 .cmd 回退路径）")
    note_dir = boot.data_dir / "logs"
    try:
        note_dir.mkdir(parents=True, exist_ok=True)
        boot.note(f"logs_dir_writable: True ({note_dir})")
    except OSError as exc:
        failures.append(f"日志目录不可写: {note_dir} ({exc})")
        boot.note(f"logs_dir_writable: False ({exc})")

    boot.enter("diagnose:paths")
    for name in _DIAGNOSE_PATHS:
        fn = getattr(paths, name, None)
        if fn is None:
            failures.append(f"paths.{name} 缺失")
            boot.note(f"paths.{name}: <MISSING>")
            continue
        try:
            value = fn()
        except Exception as exc:
            failures.append(f"paths.{name}() 失败: {exc}")
            boot.note(f"paths.{name}: ERROR {exc}")
            continue
        boot.note(f"paths.{name}: {value}")
    try:
        db_exists = paths.db_path().exists()
        boot.note(f"db_present: {db_exists}")
        boot.note(f"uia_helper_present: {paths.uia_helper_script().exists()}")
    except Exception as exc:  # pragma: no cover - 防御
        failures.append(f"路径探测失败: {exc}")

    boot.enter("diagnose:imports")
    for mod in _DIAGNOSE_MODULES:
        try:
            __import__(mod)
        except Exception as exc:
            failures.append(f"import {mod}: {exc.__class__.__name__}: {exc}")
            boot.note(f"import {mod}: FAIL {exc.__class__.__name__}: {exc}")
            boot.note(traceback.format_exc().rstrip())
            continue
        boot.note(f"import {mod}: ok")

    boot.enter("diagnose:tkinter")
    try:
        import tkinter  # noqa: F401  只验证可导入

        boot.note("import tkinter: ok")
        try:
            from tkinter import Tcl  # noqa: F401

            boot.note("tkinter.Tcl: importable")
        except Exception as exc:  # pragma: no cover
            boot.note(f"tkinter.Tcl: {exc.__class__.__name__}: {exc}")
        boot.note("Tk window: NOT created (by design)")
    except Exception as exc:
        failures.append(f"import tkinter: {exc.__class__.__name__}: {exc}")
        boot.note(f"import tkinter: FAIL {exc.__class__.__name__}: {exc}")

    boot.enter("diagnose:result")
    if failures:
        for item in failures:
            boot.error(item)
        boot.note(f"STATUS FAILED failures={len(failures)}")
        return boot.finish(EXIT_DIAGNOSE_FAILED)
    boot.note("STATUS OK (no window created, no hook installed)")
    return boot.finish(EXIT_OK)


# --------------------------------------------------------------------- 启动
def acquire_single_instance(boot: "Bootstrap", argv: list[str]):
    """在建窗 / 建库 / 注册热键**之前**做原子单实例判重。

    用户机器上曾同时跑着 6 个旧实例，随后出现「呼出热键注册失败」：
    热键是独占资源，多开必然互相抢。这里用 ``CreateMutexW`` 原子判重，
    **不建 IPC、不杀进程、不弹窗**；判重失败时本次启动直接干净退出。

    只对「真正要启动应用」的路径生效（``--diagnose`` 不占用互斥体）：
    诊断只做导入与只读检查，不应该让用户在排错时被单实例挡住。

    走的是 ``app.single_instance.acquire_process_guard``（**进程级只加一次锁**）：
    它随后调用的 ``app.main.main`` 属于同一条启动链，不能再自己加一次。

    返回 ``(guard 或 None, 退出码或 None)``。
    """
    if boot.diagnose:
        boot.note("single_instance: skipped (--diagnose 只做只读检查)")
        return None, None
    boot.enter("single_instance")
    try:
        from app.single_instance import acquire_process_guard
    except Exception as exc:  # pragma: no cover - 判重不可用不阻止启动
        boot.note(f"single_instance: unavailable ({exc.__class__.__name__}: {exc})")
        return None, None
    guard, exit_code = acquire_process_guard()
    if exit_code is None:
        boot.note(f"single_instance: ok ({guard.name})")
        return guard, None
    boot.note(f"single_instance: already running ({guard.name})")
    boot.error("已有一个实例在运行：本次启动退出（避免抢热键 / 并发写库）")
    return guard, int(exit_code)


def _release_single_instance(guard) -> None:
    """释放本进程持有的单实例闸门（没拿到 / 导入失败时是安全空操作）。"""
    if guard is None:
        return
    try:
        from app.single_instance import release_process_guard
    except Exception:  # pragma: no cover - 极端环境：尽力释放，不阻止退出
        guard.release()
        return
    release_process_guard(guard)


def run_app(boot: Bootstrap, argv: list[str]) -> int:
    """导入 app.main 并把它不认识的参数原样交给它（--selftest 等行为不变）。"""
    boot.enter("import:app.main")
    try:
        import app.main as app_main
    except BaseException:
        boot.error("导入 app.main 失败:\n" + traceback.format_exc().rstrip())
        return boot.finish(EXIT_STARTUP_FAILED)
    boot.note("import app.main: ok")

    boot.enter("app.main:main")
    try:
        code = int(app_main.main(argv) or 0)
    except SystemExit as exc:  # argparse 等主动退出
        code = int(exc.code or 0)
        boot.note(f"SystemExit({code})")
    except BaseException:
        boot.error("启动阶段异常:\n" + traceback.format_exc().rstrip())
        return boot.finish(EXIT_STARTUP_FAILED)
    return boot.finish(code)


def parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(prog="探索词典 bootstrap", add_help=True)
    parser.add_argument("--diagnose", action="store_true",
                        help="只导入组件/检查路径与 tkinter，写诊断状态后退出（不建窗）")
    parser.add_argument("--console-log", action="store_true",
                        help="关键启动信息同时输出到控制台")
    known, rest = parser.parse_known_args(argv)
    return known, rest


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    args, rest = parse_args(raw)

    boot = Bootstrap(raw, console_log=args.console_log, diagnose=args.diagnose)
    atexit.register(boot.log.close)
    boot.install_streams()
    boot.write_header()
    _install_hooks(boot)

    if args.diagnose:
        return run_diagnose(boot)

    # 单实例闸门：**在导入 app.main / 建窗 / 建库 / 注册热键之前**。
    # 异常路径统一在这里释放（正常退出由 atexit 兜底），失败也不阻止启动。
    guard, exit_code = acquire_single_instance(boot, rest)
    if guard is not None:
        atexit.register(guard.release)
    if exit_code is not None:
        return boot.finish(exit_code)
    try:
        return run_app(boot, rest)
    finally:
        _release_single_instance(guard)


if __name__ == "__main__":
    raise SystemExit(main())
