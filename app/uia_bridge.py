"""PowerShell UIA helper 的 Python 侧客户端。

* 常驻子进程 + stdin/stdout JSON 行协议（避免每次取词都付 PowerShell 启动成本）。
* 读线程按 id 匹配响应；超时/崩溃自动重启。
* 本模块只做「读取选区」，不写剪贴板、不发送 Ctrl+C。
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import paths
from . import win32util as w32
from .logging_setup import get_logger

log = get_logger("uia")

DEFAULT_TIMEOUT = 3.0
START_TIMEOUT = 20.0
#: 重启预算：窗口内最多重启几次（超过就等窗口滚动，避免重启风暴）
RESTART_BUDGET = 5
RESTART_WINDOW_SECONDS = 60.0


def find_powershell() -> str | None:
    exe = shutil.which("powershell.exe")
    if exe:
        return exe
    cand = Path(os.environ.get("SystemRoot", r"C:\Windows")) / \
        "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(cand) if cand.exists() else None


class UiaUnavailable(RuntimeError):
    pass


class UiaBridge:
    """对 PowerShell helper 的线程安全封装。"""

    def __init__(self, script: Path | None = None, *, enabled: bool = True):
        self.script = Path(script) if script else paths.uia_helper_script()
        self._proc: subprocess.Popen | None = None
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._pending: dict[int, queue.Queue] = {}
        self._next_id = 1
        self._reader: threading.Thread | None = None
        self._enabled = enabled
        self._restarts = 0
        self._restart_window = 0.0
        self._last_error = ""
        self._started_at = 0.0
        self._capabilities: dict | None = None

    # ------------------------------------------------------------- 生命周期
    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, value: bool) -> None:
        self._enabled = bool(value)

    def _powershell(self) -> str:
        exe = find_powershell()
        if not exe:
            raise UiaUnavailable("找不到 powershell.exe")
        return exe

    def start(self) -> bool:
        """启动 helper 并等待其可用。返回是否成功。

        ``enabled=False`` 时**绝不启动**：门控进入硬阻断（游戏/全屏/用户暂停）后，
        在途手势不得再拉起新的 PowerShell 进程。
        """
        with self._lock:
            if not self._enabled:
                self._last_error = "UIA 取词已关闭（门控阻断中），不启动 helper"
                return False
            if self._proc and self._proc.poll() is None:
                return True
            if not self.script.exists():
                self._last_error = f"helper 脚本不存在: {self.script}"
                log.error(self._last_error)
                return False
            try:
                exe = self._powershell()
            except UiaUnavailable as exc:
                self._last_error = str(exc)
                log.error(self._last_error)
                return False

            creationflags = 0
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                creationflags = subprocess.CREATE_NO_WINDOW

            try:
                self._proc = subprocess.Popen(
                    [
                        exe,
                        "-NoProfile",
                        "-NonInteractive",
                        "-ExecutionPolicy", "Bypass",
                        "-File", str(self.script),
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=creationflags,
                    cwd=str(paths.project_root()),
                )
            except OSError as exc:
                self._last_error = f"启动 PowerShell 失败: {exc}"
                log.error(self._last_error)
                self._proc = None
                return False

            self._started_at = time.time()
            self._reader = threading.Thread(
                target=self._read_loop, args=(self._proc,), name="uia-reader", daemon=True
            )
            self._reader.start()
            threading.Thread(
                target=self._drain_stderr, args=(self._proc,), name="uia-stderr", daemon=True
            ).start()

        try:
            resp = self._request({"cmd": "ping"}, timeout=START_TIMEOUT)
        except (UiaUnavailable, TimeoutError) as exc:
            self._last_error = f"helper 未就绪: {exc}"
            log.error(self._last_error)
            self.stop()
            return False
        ok = bool(resp.get("ok"))
        if ok:
            log.info("UIA helper 就绪 (PowerShell %s)", resp.get("ps", "?"))
        else:
            self._last_error = str(resp)
        return ok

    def stop(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        self._terminate(proc)

    def _terminate(self, proc) -> None:
        """结束给定 helper 进程并释放管道（``proc`` 为 None 时是空操作）。"""
        if not proc:
            return
        try:
            if proc.poll() is None and proc.stdin:
                try:
                    proc.stdin.write(b'{"cmd":"quit"}\n')
                    proc.stdin.flush()
                except (OSError, ValueError):
                    pass
                try:
                    proc.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except OSError:
            pass
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if stream:
                        stream.close()
                except OSError:
                    pass
        with self._lock:
            self._pending.clear()

    def _evict_stuck_helper(self, reason: str = "timeout") -> bool:
        """淘汰**卡死**的 helper：立刻摘掉句柄，后台终止进程。

        超时说明 helper 正卡在一次 UIA 调用里（它不再读 stdin），留着它只会让
        之后每一次请求都排在一个永远不会回话的进程后面。摘掉句柄（同步）保证
        **下一次**取词可以重新拉起一个干净的 helper，终止动作放到后台线程，
        绝不阻塞调用方。淘汰同时重置重启预算，让恢复不被历史计数挡住。
        """
        with self._lock:
            proc, self._proc = self._proc, None
            self._pending.clear()
            self._restarts = 0
            self._restart_window = 0.0
        if proc is None:
            return False
        log.warning("淘汰卡死的 UIA helper（%s），下次取词会重新启动", reason)
        threading.Thread(target=self._terminate, args=(proc,),
                         name="uia-evict", daemon=True).start()
        return True

    def _restart(self) -> bool:
        now = time.time()
        with self._lock:
            if self._restarts >= RESTART_BUDGET:
                if now - self._restart_window < RESTART_WINDOW_SECONDS:
                    log.warning("UIA helper 重启次数过多，暂不再重启")
                    return False
                self._restarts = 0
            if not self._restart_window or now - self._restart_window >= RESTART_WINDOW_SECONDS:
                self._restart_window = now
            self._restarts += 1
            count = self._restarts
        log.warning("重启 UIA helper（第 %d 次）", count)
        self.stop()
        return self.start()

    def reset_restart_budget(self) -> None:
        with self._lock:
            self._restarts = 0
            self._restart_window = 0.0

    # ------------------------------------------------------------ 子进程 IO
    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        try:
            assert proc.stderr is not None
            for raw in iter(proc.stderr.readline, b""):
                text = raw.decode("utf-8", "replace").strip()
                if text:
                    log.debug("uia-helper stderr: %s", text)
        except (OSError, ValueError):
            pass

    def _read_loop(self, proc: subprocess.Popen) -> None:
        buf = b""
        try:
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.readline()
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self._dispatch(line.decode("utf-8", "replace").strip())
        except (OSError, ValueError):
            pass
        finally:
            with self._lock:
                pending = list(self._pending.values())
                self._pending.clear()
            for q in pending:
                q.put(None)

    def _dispatch(self, line: str) -> None:
        if not line:
            return
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            log.debug("uia-helper 非 JSON 输出: %s", line[:200])
            return
        rid = obj.get("id")
        with self._lock:
            q = self._pending.pop(int(rid), None) if rid is not None else None
        if q is not None:
            q.put(obj)
        else:
            log.debug("uia-helper 未匹配响应: %s", line[:200])

    def _request(self, payload: dict, timeout: float = DEFAULT_TIMEOUT) -> dict:
        if not self._enabled:
            raise UiaUnavailable("UIA 取词已关闭")
        with self._lock:
            proc = self._proc
            if not proc or proc.poll() is not None:
                raise UiaUnavailable("UIA helper 未运行")
            rid = self._next_id
            self._next_id += 1
            q: queue.Queue = queue.Queue(maxsize=1)
            self._pending[rid] = q

        payload = dict(payload)
        payload["id"] = rid
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with self._write_lock:
                assert proc.stdin is not None
                proc.stdin.write(data)
                proc.stdin.flush()
        except (OSError, ValueError) as exc:
            with self._lock:
                self._pending.pop(rid, None)
            raise UiaUnavailable(f"写入 helper 失败: {exc}") from exc

        try:
            resp = q.get(timeout=timeout)
        except queue.Empty:
            with self._lock:
                self._pending.pop(rid, None)
            raise TimeoutError(f"UIA 请求超时（{timeout}s）") from None
        if resp is None:
            raise UiaUnavailable("UIA helper 已退出")
        return resp

    # -------------------------------------------------------------- 公共 API
    def ensure_started(self) -> bool:
        if not self._enabled:
            return False
        with self._lock:
            alive = self._proc is not None and self._proc.poll() is None
        if alive:
            return True
        return self.start()

    def ping(self, timeout: float = START_TIMEOUT) -> bool:
        if not self.ensure_started():
            return False
        try:
            resp = self._request({"cmd": "ping"}, timeout=timeout)
        except TimeoutError:
            self._evict_stuck_helper("ping timeout")
            return False
        except UiaUnavailable:
            return False
        return bool(resp.get("ok"))

    def check(self, timeout: float = DEFAULT_TIMEOUT) -> dict:
        """诊断：当前焦点元素的类型、是否支持 TextPattern、能否拿到 URL。

        诊断同样受前台身份约束：只允许针对**当前前台窗口**读取；实时前台
        在读之前被核对一次，helper 侧还会再核对候选所属顶层窗口。
        """
        fg = w32.foreground_info()
        hwnd = int(fg.get("hwnd") or 0)
        pid = int(fg.get("pid") or 0)
        if not hwnd or not pid:
            return {"ok": False, "reason": "no_foreground"}
        if not self.ensure_started():
            return {"ok": False, "reason": "helper_unavailable", "error": self._last_error}
        try:
            return self._request(
                {"cmd": "check", "expectedHwnd": hwnd, "expectedPid": pid}, timeout=timeout
            )
        except TimeoutError:
            self._evict_stuck_helper("check timeout")
            return {"ok": False, "reason": "timeout"}
        except UiaUnavailable as exc:
            return {"ok": False, "reason": "helper_unavailable", "error": str(exc)}

    def get_selection(
        self, x: int | None = None, y: int | None = None,
        context_chars: int = 120, timeout: float = DEFAULT_TIMEOUT,
        expected_hwnd: int | None = None, expected_pid: int | None = None,
    ) -> dict:
        """读取当前真实选区。永远返回 dict，不抛异常（失败时 ok=False）。

        :param expected_hwnd: 捕获开始时记录的**前台顶层窗口**句柄。
        :param expected_pid: 捕获开始时记录的**前台进程** pid。
            两者会一起发给 helper：helper 在**读取任何候选之前**先确认实时前台
            仍是这个窗口，并校验候选所属顶层窗口 / 进程；不符合直接返回，
            绝不调用 ``TextPattern``。缺任一项时 fail-closed，不做任何读取。
        """
        if not expected_hwnd or not expected_pid:
            return {"ok": False, "reason": "expected_window_required"}
        if not self._enabled:
            # 门控硬阻断期间：一个请求都不发，也不拉起 helper 进程
            return {"ok": False, "reason": "uia_disabled"}
        # 客户端侧再挡一次：调用途中切到游戏/其它应用，连请求都不发出去。
        fg = w32.foreground_info()
        if int(fg.get("hwnd") or 0) != int(expected_hwnd) or \
                int(fg.get("pid") or 0) != int(expected_pid):
            return {"ok": False, "reason": "foreground_changed_before_call"}

        if not self.ensure_started():
            if not self._restart():
                return {"ok": False, "reason": "helper_unavailable", "error": self._last_error}
        payload: dict = {
            "cmd": "selection",
            "contextChars": int(context_chars),
            "expectedHwnd": int(expected_hwnd),
            "expectedPid": int(expected_pid),
        }
        if x is not None and y is not None:
            payload["x"] = int(x)
            payload["y"] = int(y)
        try:
            resp = self._request(payload, timeout=timeout)
        except TimeoutError:
            log.debug("取词超时")
            # 超时 = helper 卡死在一次 UIA 调用里：当场淘汰，下一次能恢复
            self._evict_stuck_helper("selection timeout")
            return {"ok": False, "reason": "timeout"}
        except UiaUnavailable as exc:
            if self._restart():
                try:
                    resp = self._request(payload, timeout=timeout)
                except TimeoutError:
                    self._evict_stuck_helper("selection timeout")
                    return {"ok": False, "reason": "timeout"}
                except (UiaUnavailable) as exc2:
                    return {"ok": False, "reason": "helper_unavailable", "error": str(exc2)}
            else:
                return {"ok": False, "reason": "helper_unavailable", "error": str(exc)}

        if resp.get("ok"):
            resp["text"] = (resp.get("text") or "").strip()
            resp["context"] = (resp.get("context") or "").strip()
        return resp

    def last_error(self) -> str:
        return self._last_error

    def pid(self) -> int | None:
        with self._lock:
            return self._proc.pid if self._proc and self._proc.poll() is None else None
