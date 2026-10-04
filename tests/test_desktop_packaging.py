"""桌面打包（PyInstaller windowed / onedir）回归 —— **全离线、零真实 Win32**。

覆盖本轮新增的桌面化链路，一个真实窗口、互斥体、事件、弹窗都不创建：

* **frozen 路径分离**：``project_root`` = exe 目录、``resource_root`` =
  ``sys._MEIPASS``；``scripts/uia_helper.ps1`` 只从资源根找，而 ``data/``
  仍然是「程序根 / data」（不迁移、不读旧数据）；
* **同一把锁 / 同一个事件**：呼出事件名与单实例互斥体名共用**同一份数据目录
  64 位签名**，且 ``default_data_dir()`` 只解析路径、**不建目录**；
* **bootstrap 根目录**：frozen 下 ROOT 与 ``app.paths.project_root`` 一致，
  ``ROOT / "data"`` 兜底因此不会指向第二份数据；
* **desktop_entry**：参数原样转发（**不再**自动补 ``--open-main``，启动只显示浮窗）；
  ``--diagnose`` 不弹窗、不发事件；重复实例（退出码 5）走命名事件呼出，
  旧实例不支持时给中文提示；启动失败只弹「短中文 + 日志路径」，
  **绝不泄漏原始异常 / 密钥**；
* **激活 poll**：``App`` 的 250ms 轮询在硬阻断下只暂存一个 pending，
  门控解除后兑现一次。

真实 Win32 只在 :class:`TestActivationBackend` 里以 ``ctypes.WinDLL`` 替身检查
**调用参数**（auto-reset、零超时、指针宽度句柄），不加载 kernel32。
"""
from __future__ import annotations

import contextlib
import ctypes
import importlib
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from tests.support import ROOT, headless_app, tmp_dir

import desktop_entry
from app import activation, paths, single_instance


# --------------------------------------------------------------------- 工具
@contextlib.contextmanager
def frozen_env(base: Path):
    """把当前进程伪装成 PyInstaller onedir：``<base>/探索词典/探索词典.exe``。"""
    app_dir = base / "探索词典"
    meipass = app_dir / "_runtime"
    exe = app_dir / "探索词典.exe"
    with mock.patch.object(sys, "frozen", True, create=True), \
            mock.patch.object(sys, "_MEIPASS", str(meipass), create=True), \
            mock.patch.object(sys, "executable", str(exe)):
        yield exe, meipass


@contextlib.contextmanager
def no_data_override():
    """临时移除 EXPLORER_DICT_DATA_DIR，保证断言的是默认 ``<根>/data``。"""
    old = os.environ.pop("EXPLORER_DICT_DATA_DIR", None)
    try:
        yield
    finally:
        if old is not None:
            os.environ["EXPLORER_DICT_DATA_DIR"] = old


class FakeEventBackend:
    """假事件后端：记录调用，按脚本返回句柄 / 错误码 / 等待结果。"""

    def __init__(self, *, create_result=(0x11, 0), open_result=(0x22, 0), set_ok=True,
                 wait_result=activation.WAIT_OBJECT_0, raises=None):
        self.create_result = create_result
        self.open_result = open_result
        self.set_result = bool(set_ok)
        self.wait_result = int(wait_result)
        self.raises = raises
        self.created: list[str] = []
        self.opened: list[str] = []
        self.sets: list[int] = []
        self.waits: list[tuple[int, int]] = []
        self.closed: list[int] = []

    def _maybe_raise(self):
        if self.raises is not None:
            raise self.raises

    def create_event(self, name):
        self.created.append(str(name))
        self._maybe_raise()
        return self.create_result

    def open_event(self, name):
        self.opened.append(str(name))
        self._maybe_raise()
        return self.open_result

    def set_event(self, handle):
        self.sets.append(int(handle))
        self._maybe_raise()
        return self.set_result

    def wait(self, handle, timeout_ms):
        self.waits.append((int(handle), int(timeout_ms)))
        self._maybe_raise()
        return self.wait_result

    def close_handle(self, handle):
        self.closed.append(int(handle))


# ------------------------------------------------------------------ 路径
class TestFrozenPaths(unittest.TestCase):
    def test_source_mode_uses_project_root_for_everything(self):
        with no_data_override():
            self.assertEqual(paths.project_root(), ROOT)
            self.assertEqual(paths.resource_root(), ROOT)
            self.assertEqual(paths.scripts_dir(), ROOT / "scripts")
            self.assertEqual(paths.uia_helper_script(),
                             ROOT / "scripts" / "uia_helper.ps1")

    def test_frozen_splits_code_dir_from_resource_dir(self):
        with tmp_dir("frozen_") as base, no_data_override():
            with frozen_env(base) as (exe, meipass):
                self.assertEqual(paths.project_root(), exe.parent)
                self.assertEqual(paths.resource_root(), meipass)
                self.assertEqual(paths.scripts_dir(), meipass / "scripts")
                self.assertEqual(paths.uia_helper_script(),
                                 meipass / "scripts" / "uia_helper.ps1")
                self.assertNotEqual(paths.resource_root(), paths.project_root(),
                                    "资源根（_runtime）必须与程序根分开")

    def test_frozen_data_path_stays_next_to_the_executable(self):
        with tmp_dir("frozen_data_") as base, no_data_override():
            with frozen_env(base) as (exe, meipass):
                data = paths.data_dir()
                self.assertEqual(data, exe.parent / "data",
                                 "数据永远跟着程序根，不迁移、不换位置")
                self.assertFalse(str(data).startswith(str(meipass)),
                                 "绝不把用户数据写进只读的 _runtime")
                self.assertEqual(single_instance.default_data_dir(), data,
                                 "判重 / 呼出与数据目录必须是同一个判据")

    def test_single_instance_default_data_dir_does_not_create_directories(self):
        with tmp_dir("frozen_lock_") as base, no_data_override():
            with frozen_env(base):
                expected = paths.project_root() / "data"
                self.assertEqual(single_instance.default_data_dir(), expected)
                self.assertFalse(expected.exists(), "只解析路径，绝不 mkdir")

    def test_activation_event_shares_the_mutex_data_dir_signature(self):
        first = r"D:\tmp\探索词典\data"
        second = r"D:\tmp\另一份\data"
        mutex = single_instance.mutex_name_for(first)
        event = activation.event_name_for(first)
        self.assertTrue(event.startswith(activation.EVENT_NAME))
        self.assertEqual(event.rsplit(".", 1)[-1], mutex.rsplit(".", 1)[-1],
                         "同一份数据目录 = 同一把锁 + 同一个呼出事件")
        self.assertEqual(len(activation.activation_signature(first)), 16,
                         "必须是 64 位签名（16 个十六进制字符）")
        self.assertNotEqual(activation.event_name_for(first),
                            activation.event_name_for(second))

    def test_activation_default_event_follows_frozen_data_dir(self):
        with tmp_dir("frozen_event_") as base, no_data_override():
            with frozen_env(base) as (exe, _meipass):
                self.assertEqual(activation.event_name_for(),
                                 activation.event_name_for(exe.parent / "data"))

    def test_bootstrap_root_matches_project_root(self):
        import bootstrap

        with no_data_override():
            self.assertEqual(bootstrap.ROOT, paths.project_root())

    def test_bootstrap_root_follows_frozen_executable(self):
        import bootstrap

        with tmp_dir("frozen_boot_") as base, no_data_override():
            original_path = list(sys.path)
            try:
                with frozen_env(base) as (exe, _meipass):
                    reloaded = importlib.reload(bootstrap)
                    self.assertEqual(reloaded.ROOT, exe.parent)
                    self.assertEqual(reloaded.ROOT / "data",
                                     paths.project_root() / "data",
                                     "ROOT 兜底的 data 路径必须与 paths 一致")
            finally:
                importlib.reload(bootstrap)
                sys.path[:] = original_path
        self.assertEqual(bootstrap.ROOT, paths.project_root())


# ------------------------------------------------------------------ 事件
class TestActivationEvent(unittest.TestCase):
    def test_receiver_creates_event_and_polls_without_blocking(self):
        backend = FakeEventBackend()
        receiver = activation.ActivationReceiver(backend=backend)
        self.assertTrue(receiver.create())
        self.assertEqual(backend.created, [receiver.name])
        self.assertTrue(receiver.name.startswith(activation.EVENT_NAME))
        self.assertTrue(receiver.poll())
        self.assertEqual(backend.waits, [(0x11, 0)],
                         "轮询必须是 WaitForSingleObject(handle, 0)")
        backend.wait_result = activation.WAIT_TIMEOUT
        self.assertFalse(receiver.poll())

    def test_receiver_create_is_idempotent_and_close_releases_once(self):
        backend = FakeEventBackend()
        receiver = activation.ActivationReceiver(backend=backend)
        receiver.create()
        receiver.create()
        self.assertEqual(len(backend.created), 1)
        self.assertTrue(receiver.close())
        self.assertEqual(backend.closed, [0x11])
        self.assertFalse(receiver.close(), "重复释放必须是安全空操作")
        self.assertEqual(backend.closed, [0x11], "不得重复 CloseHandle")
        self.assertFalse(receiver.poll(), "释放后不再等待")

    def test_receiver_failure_is_fail_open(self):
        failing = activation.ActivationReceiver(
            backend=FakeEventBackend(create_result=(0, activation.ERROR_FILE_NOT_FOUND)))
        self.assertFalse(failing.create())
        self.assertEqual(failing.backend_error, activation.ERROR_FILE_NOT_FOUND)
        self.assertFalse(failing.poll())

        broken = activation.ActivationReceiver(
            backend=FakeEventBackend(raises=OSError("no kernel32")))
        self.assertFalse(broken.create(), "后端异常不得阻止启动")
        self.assertFalse(broken.poll())

    def test_request_activation_signals_and_always_closes_handle(self):
        backend = FakeEventBackend()
        status = activation.request_activation(r"D:\tmp\a", backend=backend)
        self.assertEqual(status, activation.REQUEST_SIGNALED)
        self.assertEqual(backend.opened, [activation.event_name_for(r"D:\tmp\a")])
        self.assertEqual(backend.sets, [0x22])
        self.assertEqual(backend.closed, [0x22], "句柄必须在 finally 里关闭")

    def test_request_activation_reports_old_instance_without_event(self):
        backend = FakeEventBackend(open_result=(0, activation.ERROR_FILE_NOT_FOUND))
        status = activation.request_activation(r"D:\tmp\a", backend=backend)
        self.assertEqual(status, activation.REQUEST_NO_INSTANCE)
        self.assertEqual(backend.closed, [], "没打开句柄就没有可关闭的东西")

    def test_request_activation_reports_signal_failure(self):
        backend = FakeEventBackend(set_ok=False)
        self.assertEqual(activation.request_activation(r"D:\tmp\a", backend=backend),
                         activation.REQUEST_FAILED)
        self.assertEqual(backend.closed, [0x22], "失败路径同样必须关闭句柄")

    def test_request_activation_never_raises(self):
        backend = FakeEventBackend(raises=OSError("boom"))
        self.assertEqual(activation.request_activation(r"D:\tmp\a", backend=backend),
                         activation.REQUEST_FAILED)


class TestActivationBackend(unittest.TestCase):
    """用 ``ctypes.WinDLL`` 替身检查真实后端的**调用参数**（不加载 kernel32）。"""

    def _backend(self, kernel32):
        with mock.patch.object(ctypes, "WinDLL", return_value=kernel32):
            return activation.ActivationBackend()

    def test_create_event_is_auto_reset_and_unnamed_security(self):
        import ctypes.wintypes as wt

        kernel32 = mock.MagicMock()
        kernel32.CreateEventW.return_value = 0xAB
        backend = self._backend(kernel32)
        handle, err = backend.create_event(r"Local\ExplorerDict.Activate.v1.test")
        self.assertEqual((handle, err), (0xAB, 0))
        args = kernel32.CreateEventW.call_args.args
        self.assertIsNone(args[0], "默认安全属性")
        self.assertFalse(args[1], "bManualReset=False → auto-reset 事件")
        self.assertFalse(args[2], "初始未触发")
        self.assertEqual(args[3], r"Local\ExplorerDict.Activate.v1.test")
        # 64 位：句柄必须按指针宽度声明，绝不能用 c_int 截断
        self.assertIs(kernel32.CreateEventW.restype, wt.HANDLE)
        self.assertIs(kernel32.OpenEventW.restype, wt.HANDLE)
        self.assertTrue(issubclass(wt.HANDLE, ctypes.c_void_p))

    def test_wait_uses_zero_timeout_and_open_requests_modify_state(self):
        kernel32 = mock.MagicMock()
        kernel32.WaitForSingleObject.return_value = activation.WAIT_OBJECT_0
        kernel32.OpenEventW.return_value = 0xCD
        backend = self._backend(kernel32)
        self.assertEqual(backend.wait(0xCD, 0), activation.WAIT_OBJECT_0)
        self.assertEqual(kernel32.WaitForSingleObject.call_args.args[1], 0,
                         "轮询必须是零超时（非阻塞）")
        handle, err = backend.open_event("evt")
        self.assertEqual((handle, err), (0xCD, 0))
        rights = kernel32.OpenEventW.call_args.args[0]
        self.assertTrue(rights & activation.EVENT_MODIFY_STATE, "SetEvent 需要写权限")
        self.assertTrue(rights & activation.SYNCHRONIZE, "轮询需要同步权限")


# ------------------------------------------------------------- desktop_entry
class TestDesktopEntry(unittest.TestCase):
    def setUp(self):
        self.boxes: list[str] = []
        patcher = mock.patch.object(
            desktop_entry, "message_box",
            side_effect=lambda text: self.boxes.append(str(text)) or True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, argv, *, code=0, side_effect=None):
        main = mock.Mock(return_value=code, side_effect=side_effect)
        with mock.patch("bootstrap.main", main):
            result = desktop_entry.main(list(argv))
        return result, main

    # -- 正常启动 ------------------------------------------------------
    def test_normal_start_does_not_append_open_main(self):
        """启动只显示浮窗：desktop_entry **不再**替用户追加 ``--open-main``。"""
        code, main = self._run([], code=0)
        self.assertEqual(code, 0)
        main.assert_called_once_with([])
        self.assertEqual(self.boxes, [], "正常启动不弹窗")

    def test_explicit_flags_are_forwarded_untouched(self):
        """显式参数（含开发用途的 ``--open-main``）原样转发，绝不重复追加。"""
        _code, main = self._run(["--console-log"], code=0)
        main.assert_called_once_with(["--console-log"])
        _code, main = self._run(["--open-main", "--console-log"], code=0)
        main.assert_called_once_with(["--open-main", "--console-log"])

    # -- 诊断 ----------------------------------------------------------
    def test_diagnose_never_appends_open_main_or_touches_ipc(self):
        with mock.patch("app.activation.request_activation") as request:
            code, main = self._run(["--diagnose"], code=0)
        self.assertEqual(code, 0)
        main.assert_called_once_with(["--diagnose"])
        request.assert_not_called()
        self.assertEqual(self.boxes, [], "--diagnose 绝不弹窗")

    def test_diagnose_failure_stays_silent(self):
        code, main = self._run(["--diagnose"], code=3)
        self.assertEqual(code, 3)
        main.assert_called_once_with(["--diagnose"])
        self.assertEqual(self.boxes, [], "诊断失败也只写日志，不弹窗")

    # -- 重复实例 ------------------------------------------------------
    def test_second_launch_requests_the_existing_instance(self):
        with mock.patch("app.activation.request_activation",
                        return_value=activation.REQUEST_SIGNALED) as request:
            code, main = self._run([], code=desktop_entry.EXIT_ALREADY_RUNNING)
        self.assertEqual(code, desktop_entry.EXIT_ALREADY_RUNNING)
        main.assert_called_once_with([])
        request.assert_called_once_with()
        self.assertEqual(self.boxes, [], "呼出请求送达时不再打扰用户")

    def test_second_launch_hints_when_old_instance_has_no_event(self):
        with mock.patch("app.activation.request_activation",
                        return_value=activation.REQUEST_NO_INSTANCE):
            code, _main = self._run([], code=desktop_entry.EXIT_ALREADY_RUNNING)
        self.assertEqual(code, desktop_entry.EXIT_ALREADY_RUNNING)
        self.assertEqual(len(self.boxes), 1)
        text = self.boxes[0]
        self.assertIn("已在运行", text)
        self.assertIn("后台运行", text)
        self.assertIn("完全退出", text)
        self.assertIn("重新打开", text)

    def test_second_launch_hints_when_request_raises(self):
        with mock.patch("app.activation.request_activation",
                        side_effect=OSError("no kernel32")):
            code, _main = self._run([], code=desktop_entry.EXIT_ALREADY_RUNNING)
        self.assertEqual(code, desktop_entry.EXIT_ALREADY_RUNNING)
        self.assertEqual(len(self.boxes), 1)

    # -- 失败提示 ------------------------------------------------------
    def test_startup_failure_shows_short_message_with_log_path_only(self):
        secret = "sk-live-SECRET-abcdef"
        code, _main = self._run([], side_effect=RuntimeError(secret))
        self.assertEqual(code, desktop_entry.EXIT_STARTUP_FAILED)
        self.assertEqual(len(self.boxes), 1)
        text = self.boxes[0]
        self.assertIn("启动失败", text)
        self.assertIn("startup.log", text, "必须给出日志路径")
        self.assertNotIn(secret, text, "绝不泄漏原始异常 / 密钥")
        self.assertNotIn("RuntimeError", text)
        self.assertNotIn("Traceback", text)

    def test_bootstrap_exit_code_4_shows_message(self):
        code, _main = self._run([], code=4)
        self.assertEqual(code, 4)
        self.assertEqual(len(self.boxes), 1)
        self.assertIn("startup.log", self.boxes[0])

    def test_early_bootstrap_import_failure_is_caught(self):
        fake = types.ModuleType("bootstrap")

        def _explode(_argv):
            raise MemoryError("sk-live-SECRET-abcdef")

        fake.main = _explode
        with mock.patch.dict(sys.modules, {"bootstrap": fake}):
            code = desktop_entry.main([])
        self.assertEqual(code, desktop_entry.EXIT_STARTUP_FAILED)
        self.assertEqual(len(self.boxes), 1)
        self.assertNotIn("sk-live-SECRET-abcdef", self.boxes[0])
        self.assertIn("startup.log", self.boxes[0])


class TestMessageBox(unittest.TestCase):
    def test_uses_win32_messageboxw(self):
        user32 = mock.MagicMock()
        with mock.patch.object(ctypes, "WinDLL", return_value=user32):
            self.assertTrue(desktop_entry.message_box("你好"))
        user32.MessageBoxW.assert_called_once()
        args = user32.MessageBoxW.call_args.args
        self.assertIsNone(args[0])
        self.assertEqual(args[1], "你好")
        self.assertEqual(args[2], desktop_entry.TITLE)

    def test_never_raises_without_a_desktop(self):
        user32 = mock.MagicMock()
        user32.MessageBoxW.side_effect = OSError("no desktop")
        with mock.patch.object(ctypes, "WinDLL", return_value=user32):
            self.assertFalse(desktop_entry.message_box("x"))


# ---------------------------------------------------------------- App 轮询
class FakeActivationReceiver:
    """App 侧假接收器：记录 create / poll / close，按脚本给「有请求」。"""

    def __init__(self, requests: int = 0):
        self.requests = int(requests)
        self.created = 0
        self.closed = 0
        self.polls = 0

    def create(self):
        self.created += 1
        return True

    def poll(self):
        self.polls += 1
        if self.requests > 0:
            self.requests -= 1
            return True
        return False

    def close(self):
        self.closed += 1


class TestActivationPoll(unittest.TestCase):
    def test_gate_failure_defers_opening(self):
        with headless_app() as app:
            with mock.patch.object(app.gate, "evaluate", side_effect=OSError("unavailable")):
                self.assertFalse(app.request_open_main("argv"))
            self.assertTrue(app._pending_open_main)
            self.assertEqual(app.fake_root.deiconify_calls, 0)

    @staticmethod
    def _decision(allowed: bool, reason: str):
        from app.gate import GateDecision

        return GateDecision(allowed=allowed, reason=reason)

    def test_request_open_main_opens_when_not_hard_blocked(self):
        from app.gate import R_OK

        with headless_app() as app:
            with mock.patch.object(app.gate, "evaluate",
                                   return_value=self._decision(True, R_OK)):
                self.assertTrue(app.request_open_main("argv"))
            self.assertEqual(app.fake_root.deiconify_calls, 1)
            self.assertFalse(app._pending_open_main)

    def test_hard_block_defers_and_then_opens_exactly_once(self):
        from app.gate import R_GAME_PROCESS, R_OK

        blocked = self._decision(False, R_GAME_PROCESS)
        allowed = self._decision(True, R_OK)
        with headless_app(overlays="panel", main_window="real") as app:
            receiver = FakeActivationReceiver(requests=1)
            self.assertTrue(app.attach_activation(receiver))
            self.assertEqual(receiver.created, 1, "接收器必须被创建")
            self.assertTrue(app.fake_root.after_calls, "必须安排 250ms 轮询")
            self.assertEqual(app.fake_root.after_calls[0][0], 250)

            with mock.patch.object(app.gate, "evaluate", return_value=blocked):
                self.assertFalse(app._poll_activation())
                self.assertTrue(app._pending_open_main, "硬阻断下保留一个 pending")
                self.assertEqual(app.fake_root.deiconify_calls, 0,
                                 "硬阻断下一次 deiconify 都不做")
                receiver.requests = 1     # 又一次呼出：仍然只保留一个 pending
                self.assertFalse(app._poll_activation())
                self.assertEqual(app.fake_root.deiconify_calls, 0)

            with mock.patch.object(app.gate, "evaluate", return_value=allowed):
                self.assertTrue(app._poll_activation())
            self.assertFalse(app._pending_open_main)
            self.assertEqual(app.fake_root.deiconify_calls, 0)
            self.assertTrue(app.reading_panel.visible)

            with mock.patch.object(app.gate, "evaluate", return_value=allowed):
                self.assertFalse(app._poll_activation(), "没有新请求就不再打开")
            self.assertEqual(app.fake_root.deiconify_calls, 0)

    def test_close_activation_releases_event_and_stops_polling(self):
        with headless_app() as app:
            receiver = FakeActivationReceiver()
            app.attach_activation(receiver)
            self.assertTrue(app.close_activation())
            self.assertEqual(receiver.closed, 1)
            self.assertIsNone(app._activation)
            before = len(app.fake_root.after_calls)
            app._closing = True
            self.assertFalse(app._poll_activation())
            self.assertEqual(len(app.fake_root.after_calls), before,
                             "退出后不得再安排轮询")

    def test_helpers_are_safe_on_unknown_app(self):
        import app.main as app_main

        stub = types.SimpleNamespace()
        self.assertFalse(app_main._attach_activation(stub))
        self.assertFalse(app_main._request_open_main(stub))

    def test_open_main_flag_reaches_run_ui(self):
        import app.main as app_main

        seen: dict = {}

        def _fake_run_ui(args):
            seen["open_main"] = bool(args.open_main)
            return 0

        with mock.patch.object(app_main, "setup_logging"), \
                mock.patch.object(app_main, "_run_ui", side_effect=_fake_run_ui), \
                mock.patch.object(app_main, "process_guard", return_value=None), \
                mock.patch.object(app_main, "acquire_process_guard",
                                  return_value=(None, None)), \
                mock.patch.object(app_main, "release_process_guard"):
            self.assertEqual(app_main.main([]), 0)
            self.assertFalse(seen["open_main"], "默认不打开主界面")
            self.assertEqual(app_main.main(["--open-main"]), 0)
            self.assertTrue(seen["open_main"], "parser 必须接受 --open-main")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
