"""单实例保护回归 —— **全 fake backend，一个真实互斥体都不创建**。

覆盖用户本轮报告的实际问题：多实例同时跑 → 热键注册冲突。
这里验证的是「原子判重 + 干净退出」这条链路本身：

* 首个实例拿到互斥体 → 允许启动；
* 第二个实例（``ERROR_ALREADY_EXISTS``）→ **拒绝启动**并给出中文原因，
  且**握着句柄不提前释放**（否则第一个实例正好退出时第三个实例会误判成
  「我是唯一实例」）；不杀进程、不建任何 IPC、不弹窗；
* 互斥体创建失败（错误码 / 后端抛异常）→ **fail-open**：不阻止用户启动；
* 释放幂等、重复 acquire 幂等；``--diagnose`` 完全不占用互斥体。

真实 ``CreateMutexW`` **没有被调用**：``SingleInstance`` 的后端全部注入。
"""
from __future__ import annotations

import contextlib
import unittest

from app.single_instance import (ERROR_ACCESS_DENIED, ERROR_ALREADY_EXISTS,
                                 EXIT_ALREADY_RUNNING, MUTEX_NAME, SingleInstance)


class FakeMutexBackend:
    """假后端：记录调用，按脚本返回 ``(句柄, 是否已存在, 错误码)``。"""

    def __init__(self, *, existed: bool = False, handle: int = 0x1234,
                 error: int = 0, raises: Exception | None = None):
        self.existed = existed
        self.handle = handle
        self.error = error
        self.raises = raises
        self.created: list[str] = []
        self.closed: list[int] = []

    def create_mutex(self, name: str):
        self.created.append(str(name))
        if self.raises is not None:
            raise self.raises
        if not self.handle:
            return 0, False, self.error
        return self.handle, bool(self.existed), 0

    def close_handle(self, handle: int) -> None:
        self.closed.append(int(handle))


class RecordingLogger:
    def __init__(self):
        self.rows: list[tuple[str, str]] = []

    def debug(self, msg, *a, **k):
        self.rows.append(("debug", str(msg)))

    def info(self, msg, *a, **k):
        self.rows.append(("info", str(msg)))

    def warning(self, msg, *a, **k):
        self.rows.append(("warning", str(msg)))

    def exception(self, msg, *a, **k):
        self.rows.append(("exception", str(msg)))


class TestSingleInstance(unittest.TestCase):
    def test_first_instance_acquires_and_release_is_idempotent(self):
        from app.single_instance import default_data_dir, mutex_name_for

        backend = FakeMutexBackend()
        guard = SingleInstance(backend=backend, logger=RecordingLogger())
        self.assertTrue(guard.acquire(), "第一个实例必须能启动")
        self.assertTrue(guard.acquired)
        self.assertFalse(guard.already_running)
        self.assertEqual(backend.created, [guard.name], "互斥体名必须与 guard 一致")
        self.assertEqual(guard.name, mutex_name_for(default_data_dir()),
                         "默认名字必须由当前数据目录推导（同目录恒同名）")
        self.assertNotEqual(guard.name, MUTEX_NAME,
                            "名字必须按数据目录区分，不能是所有副本共用的固定串")
        self.assertEqual(guard.handle, 0x1234)

        self.assertTrue(guard.release())
        self.assertEqual(backend.closed, [0x1234])
        self.assertFalse(guard.release(), "重复释放必须是安全的空操作")
        self.assertEqual(backend.closed, [0x1234], "不得重复 CloseHandle")

    def test_second_instance_is_rejected_and_keeps_its_handle(self):
        backend = FakeMutexBackend(existed=True)
        log = RecordingLogger()
        guard = SingleInstance(backend=backend, logger=log)
        self.assertFalse(guard.acquire(), "已有实例时必须拒绝启动")
        self.assertTrue(guard.already_running)
        self.assertFalse(guard.acquired)
        self.assertIn("已经有一个探索词典实例在运行", guard.reason)
        self.assertEqual(backend.closed, [],
                         "不能提前释放句柄：那会让紧随其后的第三个实例误判成唯一实例")
        self.assertTrue(any(level == "info" for level, _ in log.rows), "必须留下日志")

    def test_create_failure_fails_open(self):
        backend = FakeMutexBackend(handle=0, error=1234)
        guard = SingleInstance(backend=backend, logger=RecordingLogger())
        self.assertTrue(guard.acquire(), "互斥体创建失败不得阻止用户启动")
        self.assertFalse(guard.already_running)
        self.assertFalse(guard.acquired)
        self.assertEqual(guard.backend_error, 1234)
        self.assertIn("创建失败", guard.reason)
        self.assertFalse(guard.release(), "没拿到句柄时释放是空操作")

    def test_access_denied_is_reported_and_fails_open(self):
        backend = FakeMutexBackend(handle=0, error=ERROR_ACCESS_DENIED)
        guard = SingleInstance(backend=backend, logger=RecordingLogger())
        self.assertTrue(guard.acquire())
        self.assertEqual(guard.backend_error, ERROR_ACCESS_DENIED)
        self.assertIn("拒绝访问", guard.reason)

    def test_backend_exception_never_blocks_startup(self):
        backend = FakeMutexBackend(raises=OSError("no kernel32"))
        guard = SingleInstance(backend=backend, logger=RecordingLogger())
        self.assertTrue(guard.acquire())
        self.assertFalse(guard.created if hasattr(guard, "created") else False)
        self.assertIn("不可用", guard.reason)

    def test_acquire_is_idempotent(self):
        backend = FakeMutexBackend()
        guard = SingleInstance(backend=backend, logger=RecordingLogger())
        guard.acquire()
        guard.acquire()
        self.assertEqual(len(backend.created), 1, "拿到之后不得重复创建互斥体")

    def test_context_manager_releases(self):
        backend = FakeMutexBackend()
        with SingleInstance(backend=backend, logger=RecordingLogger()) as guard:
            self.assertTrue(guard.acquired)
        self.assertEqual(backend.closed, [0x1234])

    def test_exit_code_matches_bootstrap_contract(self):
        import bootstrap

        self.assertEqual(int(bootstrap.EXIT_ALREADY_RUNNING), int(EXIT_ALREADY_RUNNING),
                         "bootstrap 的退出码必须与 single_instance 一致")


class TestBootstrapGate(unittest.TestCase):
    """``bootstrap.acquire_single_instance``：只改启动闸门，不碰其它逻辑。

    **不构造真实 ``Bootstrap``**：它的构造函数会打开 ``data/logs/startup.log``
    （生产日志）。这里用记录型替身，一个真实文件都不碰。
    """

    def setUp(self):
        from app import single_instance

        # 进程级闸门是模块状态：每个用例结束后清掉，避免串味
        self.addCleanup(single_instance.release_process_guard)

    class _Boot:
        def __init__(self, diagnose: bool = False):
            self.diagnose = bool(diagnose)
            self.notes: list[str] = []

        def enter(self, phase: str) -> None:
            self.notes.append(f"PHASE {phase}")

        def note(self, text: str) -> None:
            self.notes.append(str(text))

        def error(self, text: str) -> None:
            self.notes.append(f"ERROR {text}")

    def test_diagnose_never_takes_the_mutex(self):
        import bootstrap

        boot = self._Boot(diagnose=True)
        guard, code = bootstrap.acquire_single_instance(boot, [])
        self.assertIsNone(guard)
        self.assertIsNone(code)
        self.assertTrue(any("skipped" in row for row in boot.notes))

    def test_second_launch_exits_with_the_already_running_code(self):
        import bootstrap
        from unittest import mock

        boot = self._Boot()
        fake_guard = SingleInstance(backend=FakeMutexBackend(existed=True),
                                    logger=RecordingLogger())
        with mock.patch("app.single_instance.SingleInstance", return_value=fake_guard):
            guard, code = bootstrap.acquire_single_instance(boot, [])
        self.assertIs(guard, fake_guard)
        self.assertEqual(code, int(bootstrap.EXIT_ALREADY_RUNNING))
        self.assertTrue(any("already running" in row for row in boot.notes))
        self.assertTrue(any(row.startswith("ERROR") for row in boot.notes),
                        "必须把原因写进启动日志")

    def test_first_launch_reports_ok(self):
        import bootstrap
        from unittest import mock

        boot = self._Boot()
        fake_guard = SingleInstance(backend=FakeMutexBackend(), logger=RecordingLogger())
        with mock.patch("app.single_instance.SingleInstance", return_value=fake_guard):
            guard, code = bootstrap.acquire_single_instance(boot, [])
        self.assertIs(guard, fake_guard)
        self.assertIsNone(code)
        self.assertTrue(any("single_instance: ok" in row for row in boot.notes))


class EventBackend(FakeMutexBackend):
    """把加锁 / 解锁写进共享事件表：用于断言「锁在 Tk / 建库之前」。"""

    def __init__(self, events: list, **kw):
        super().__init__(**kw)
        self.events = events

    def create_mutex(self, name: str):
        self.events.append(("lock", str(name)))
        return super().create_mutex(name)

    def close_handle(self, handle: int) -> None:
        self.events.append(("unlock", int(handle)))
        return super().close_handle(handle)


class TestMutexNameFollowsDataDir(unittest.TestCase):
    """互斥体名按**规范化数据目录绝对路径**区分：两份副本互不干扰。"""

    def test_same_dir_written_differently_is_the_same_mutex(self):
        from app.single_instance import mutex_name_for

        a = mutex_name_for(r"D:\探索工具\探索词典\data")
        b = mutex_name_for(r"D:\探索工具\探索词典\sub\..\data")
        c = mutex_name_for(r"d:\探索工具\探索词典\DATA")
        self.assertEqual(a, b, "同一目录的不同写法必须得到同一个互斥体名")
        self.assertEqual(a, c, "Windows 路径大小写不敏感 → 同一个互斥体名")
        self.assertTrue(a.startswith(MUTEX_NAME), "前缀必须仍然可辨认")

    def test_different_dirs_get_different_mutexes(self):
        from app.single_instance import mutex_name_for

        self.assertNotEqual(mutex_name_for(r"D:\a\data"), mutex_name_for(r"D:\b\data"),
                            "不同数据目录 = 两份互不相干的数据，各自允许一份实例")

    def test_default_name_follows_the_data_dir_override(self):
        import os
        from unittest import mock

        from app import single_instance

        old = os.environ.get("EXPLORER_DICT_DATA_DIR")
        os.environ["EXPLORER_DICT_DATA_DIR"] = r"D:\tmp\isolated_data"
        try:
            guard = SingleInstance(backend=FakeMutexBackend(), logger=RecordingLogger())
        finally:
            if old is None:
                os.environ.pop("EXPLORER_DICT_DATA_DIR", None)
            else:
                os.environ["EXPLORER_DICT_DATA_DIR"] = old
        self.assertEqual(guard.name, single_instance.mutex_name_for(r"D:\tmp\isolated_data"),
                         "名字必须由构造时的数据目录决定（不是 import 时的固定值）")
        self.assertNotEqual(guard.name, single_instance.mutex_name_for(r"D:\tmp\other_data"))
        self.assertTrue(guard.name.startswith(MUTEX_NAME))


class TestProcessGuard(unittest.TestCase):
    """``acquire_process_guard``：同一个进程只真正 CreateMutexW 一次。"""

    def setUp(self):
        from app import single_instance

        self.si = single_instance
        self.addCleanup(single_instance.release_process_guard)

    def _patched(self, backend):
        from unittest import mock

        return (
            mock.patch.object(self.si, "_ACTIVE", None),
            mock.patch.object(self.si, "SingleInstanceBackend", return_value=backend),
        )

    def test_two_entries_share_one_mutex(self):
        backend = FakeMutexBackend()
        p1, p2 = self._patched(backend)
        with p1, p2:
            first, code1 = self.si.acquire_process_guard(data_dir=r"D:\tmp\pg")
            second, code2 = self.si.acquire_process_guard(data_dir=r"D:\tmp\pg")
            self.assertIs(first, second, "第二条入口必须复用同一个闸门")
            self.assertIsNone(code1)
            self.assertIsNone(code2)
            self.assertEqual(len(backend.created), 1, "同进程只能 CreateMutexW 一次")
            self.assertIs(self.si.process_guard(), first, "进程记录必须指向它")
            self.assertEqual(backend.closed, [])
            self.assertTrue(self.si.release_process_guard(first))
            self.assertEqual(backend.closed, [0x1234])
            self.assertIsNone(self.si.process_guard(), "释放后进程记录必须清空")
        self.assertIsNone(self.si.process_guard())

    def test_already_running_returns_the_exit_code_and_keeps_the_handle(self):
        backend = FakeMutexBackend(existed=True)
        p1, p2 = self._patched(backend)
        with p1, p2:
            guard, code = self.si.acquire_process_guard(data_dir=r"D:\tmp\pg2")
        self.assertEqual(code, int(EXIT_ALREADY_RUNNING))
        self.assertTrue(guard.already_running)
        self.assertIsNone(self.si.process_guard(), "没拿到的锁不记进进程状态")
        self.assertFalse(self.si.release_process_guard(guard),
                         "不是本进程持有的闸门：释放是空操作")
        self.assertEqual(backend.closed, [],
                         "已有实例的句柄必须留到进程退出（提前关会开错窗口）")

    def test_release_ignores_a_foreign_guard(self):
        backend = FakeMutexBackend()
        p1, p2 = self._patched(backend)
        foreign = SingleInstance(backend=FakeMutexBackend(), logger=RecordingLogger())
        with p1, p2:
            guard, _code = self.si.acquire_process_guard(data_dir=r"D:\tmp\pg3")
            self.assertFalse(self.si.release_process_guard(foreign),
                             "只认当前持有的那一个")
            self.assertIs(self.si.process_guard(), guard)
            self.assertTrue(self.si.release_process_guard(guard))
        self.assertFalse(foreign.acquired)


class TestEntryPointsTakeTheMutexBeforeTk(unittest.TestCase):
    """两条入口（bootstrap / ``python -m app.main``）都加锁，且各自只加一次。"""

    def setUp(self):
        from app import single_instance

        self.si = single_instance
        self.addCleanup(single_instance.release_process_guard)

    def _run_app_main(self, backend, events, *, run_ui=None):
        """跑真实的 ``app.main.main``，只把 Tk / 建库 / 日志这些副作用换掉。

        ``run_ui`` 省略时跑**真实**的启动主体（Tk 换成记录型假 root）。
        """
        from unittest import mock

        import app.main as app_main

        class FakeRoot:
            def withdraw(self):
                events.append(("withdraw", None))

            def after(self, *_a):
                return None

            def mainloop(self):
                events.append(("mainloop", None))

        def _make_tk():
            events.append(("tk", None))
            return FakeRoot()

        class FakeApp:
            def start(self):
                events.append(("start", None))

            def quit(self):
                pass

        def _build_app(root, title):
            events.append(("build_app", None))
            return FakeApp()

        patchers = [mock.patch.object(self.si, "_ACTIVE", None),
                    mock.patch.object(self.si, "SingleInstanceBackend",
                                      return_value=backend),
                    mock.patch.object(app_main, "setup_logging"),
                    mock.patch.object(app_main.w32, "enable_dpi_awareness"),
                    mock.patch.object(app_main.tk, "Tk", side_effect=_make_tk),
                    mock.patch.object(app_main, "build_app", side_effect=_build_app),
                    mock.patch.object(app_main, "messagebox")]
        if run_ui is not None:
            patchers.append(mock.patch.object(app_main, "_run_ui", side_effect=run_ui))
        with contextlib.ExitStack() as stack:
            for patcher in patchers:
                stack.enter_context(patcher)
            return int(app_main.main([]))

    def test_main_locks_before_tk_and_releases_in_finally(self):
        """真实启动主体：锁 → Tk → 建库 → 主循环 → 释放。"""
        events: list = []
        code = self._run_app_main(EventBackend(events), events)
        self.assertEqual(code, 0)
        names = [name for name, _ in events]
        self.assertEqual(names[0], "lock", "必须**先**拿锁，再做任何事")
        self.assertLess(names.index("lock"), names.index("tk"), "锁必须在建 Tk 之前")
        self.assertLess(names.index("lock"), names.index("build_app"), "锁必须在建库之前")
        self.assertLess(names.index("build_app"), names.index("mainloop"))
        self.assertIn("unlock", names, "退出路径必须释放闸门")
        self.assertGreater(names.index("unlock"), names.index("mainloop"))
        self.assertIsNone(self.si.process_guard())

    def test_already_running_never_creates_tk(self):
        events: list = []
        code = self._run_app_main(EventBackend(events, existed=True), events)
        self.assertEqual(code, int(EXIT_ALREADY_RUNNING))
        names = [name for name, _ in events]
        self.assertNotIn("tk", names, "已有实例时一个窗口都不许建")
        self.assertNotIn("build_app", names, "已有实例时不许建库")
        self.assertNotIn("unlock", names, "没拿到锁就没有可释放的闸门")

    def test_bootstrap_chain_locks_exactly_once(self):
        """bootstrap 先拿锁 → app.main 复用、不重复加锁、也不替上层释放。"""
        from unittest import mock

        import app.main as app_main
        import bootstrap

        events: list = []
        backend = EventBackend(events)
        boot = TestBootstrapGate._Boot()
        with mock.patch.object(self.si, "_ACTIVE", None), \
                mock.patch.object(self.si, "SingleInstanceBackend", return_value=backend), \
                mock.patch.object(app_main, "setup_logging"), \
                mock.patch.object(app_main, "_run_ui", return_value=0):
            guard, code = bootstrap.acquire_single_instance(boot, [])
            self.assertIsNone(code)
            self.assertEqual(len(backend.created), 1)
            self.assertEqual(int(app_main.main([])), 0)
            self.assertEqual(len(backend.created), 1,
                             "app.main 属于同一条启动链：绝不能再加一次锁")
            self.assertIs(self.si.process_guard(), guard,
                          "app.main 不得替 bootstrap 释放闸门")
            self.assertEqual(backend.closed, [])
            bootstrap._release_single_instance(guard)
        self.assertEqual(backend.closed, [0x1234])
        self.assertIsNone(self.si.process_guard())

    def test_bootstrap_release_is_safe_without_a_guard(self):
        import bootstrap

        bootstrap._release_single_instance(None)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
