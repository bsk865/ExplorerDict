"""测试公共设施。

临时目录一律建在项目内的 .tmp/ 下 —— 既满足「读写严格在项目内」，
也保证测试绝不触碰真实的 data/explorer_dict.sqlite3。

GUI 预检（重要）
----------------
用户经常在玩游戏（War Thunder）。任何**会创建窗口**的测试都必须先通过
:func:`skip_unless_foreground_safe`：它读**实时真实前台元信息**，只有
「白名单阅读应用 + 非全屏 + 非游戏模式」才放行；其余情况 ``SkipTest``，
**一个窗口都不创建**。该检查在 ``setUp`` 最前面执行，此刻没有任何
``mock.patch`` 生效，因此**无法用 mock 出来的前台/门控状态绕过**。
纯逻辑测试不调用它，照常运行。
"""
from __future__ import annotations

import os
import queue
import shutil
import sys
import time
import types
import unittest
import uuid
import contextlib
import itertools
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tkinter as _tk  # noqa: E402

#: **真实**的 tkinter 基类（在任何 mock 生效之前抓住引用）。
#: ``tk.Frame`` / ``tk.Label`` 的子类在导入时绑定基类，假环境需要就地替换这两个
#: 基类的 ``__init__``，而那时 ``tkinter.Frame`` 这个模块属性已经被换掉了。
_REAL_TK_BASES = (_tk.Frame, _tk.Label)

#: 需要就地替换成假实现的基类方法（子类会从基类继承这些实现）。
_FAKE_BASE_METHODS = (
    "tcl_call", "tcl_path",
    "pack", "pack_configure", "pack_forget", "pack_propagate", "propagate",
    "grid", "grid_configure", "grid_forget", "grid_propagate",
    "columnconfigure", "rowconfigure", "grid_columnconfigure", "grid_rowconfigure",
    "place", "place_configure", "place_forget",
    "configure", "config", "cget", "bind", "bind_all", "unbind",
    "winfo_children", "winfo_reqwidth", "winfo_reqheight", "winfo_width",
    "winfo_height", "winfo_exists", "winfo_id", "winfo_ismapped",
    "winfo_screenwidth", "winfo_screenheight", "winfo_rootx", "winfo_rooty",
    "focus_set", "focus_force", "destroy", "update_idletasks", "after",
    "insert", "delete", "get", "yview", "yview_scroll", "set", "see",
    "selection_clear", "selection_set", "curselection",
    "create_window", "itemconfigure", "bbox", "create_text", "create_line",
    "create_oval", "add_command", "add_checkbutton", "add_separator",
    "tk_popup", "grab_release",
)


def _fake_base_method(name: str):
    """把一个基类方法名映射到 :class:`FakeWidget` 的同名实现上。"""

    def _call(self, *args, **kwargs):
        return getattr(FakeWidget, name)(self, *args, **kwargs)

    _call.__name__ = name
    return _call

from app.capture_service import CaptureService  # noqa: E402
from app.config import Config  # noqa: E402
from app.db import Database  # noqa: E402
from app import gui_preflight  # noqa: E402
from app import win32util as w32  # noqa: E402
# 关键：在**任何 mock 生效之前**导入窗口模块。``tk.Frame`` 的子类（例如主界面的
# ``ScrollFrame``）在导入时就绑定了基类；如果等到 ``_FakeTkEnv`` 把
# ``tkinter.Frame`` 换掉之后再导入，子类会绑到 mock 对象上并炸掉。
# 先导入一次，假环境里再**就地改写基类的方法**，这样「真实窗口类 + 假控件」
# 才能同时成立（依然一个真实窗口都不创建）。
from app.ui.main_window import ScrollFrame as _RealScrollFrame  # noqa: E402,F401
from app.ui.floating import FloatingWindow as _RealFloatingWindow  # noqa: E402,F401
from app.ui.reading_panel import ReadingPanel as _RealReadingPanel  # noqa: E402,F401

TMP_ROOT = ROOT / ".tmp" / "tests"

#: 记录「真实 install_mouse_hook 被调用」的次数（覆盖过 install_mouse_hook 的
#: 测试不会往里记）。测试用它断言「门控未放行时绝不安装全局鼠标钩子」。
MOUSE_HOOK_INSTALLS: list[tuple[int, int]] = []


# --------------------------------------------------------------- GUI 预检
def foreground_preflight():
    """对**实时真实前台**做一次执行前预检（只读，不建窗口）。

    必须在任何 ``mock.patch`` 生效**之前**调用：它读的是此刻真实的
    前台窗口元信息与只读的 ``gate.game_mode`` 设置，不看任何被伪造的状态。
    """
    return gui_preflight.check()


def skip_unless_foreground_safe(what: str = "GUI 测试"):
    """前台不安全就 ``SkipTest``（因此**不会创建任何窗口**）。

    这是给「会创建 Tk 窗口」的测试用的硬闸门：不通过就 skip，
    不存在「用 mock 出来的允许状态继续跑」的路径。
    """
    decision = foreground_preflight()
    if not decision.allowed:
        raise unittest.SkipTest(decision.skip_message(what))
    return decision


def requires_real_foreground(what: str = "GUI 测试"):
    """装饰器：进入被装饰函数（``setUp`` 或测试体）前做实时前台预检。

    用法：
    * 装在 ``setUp`` 上 → 该类每个用例都先预检（推荐，覆盖整个类）；
    * 装在单个 ``test_*`` 上 → 只有这个用例预检（同类里还有纯逻辑用例时用）。
    """
    def _wrap(func):
        import functools

        @functools.wraps(func)
        def _inner(*args, **kwargs):
            skip_unless_foreground_safe(what)
            return func(*args, **kwargs)

        return _inner

    return _wrap


@contextmanager
def tmp_dir(prefix: str = "case_"):
    """项目内的临时目录，退出时删除。

    注意：不使用 tempfile.mkdtemp —— 它以 0700 建目录，在本机沙箱/ACL 下会导致
    后续无法写入。这里用默认权限自行建目录。
    """
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = TMP_ROOT / f"{prefix}{uuid.uuid4().hex[:12]}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@contextmanager
def temp_data_dir():
    """把项目数据目录重定向到项目内的临时目录（保护真实用户数据）。"""
    with tmp_dir("data_") as tmp:
        old = os.environ.get("EXPLORER_DICT_DATA_DIR")
        os.environ["EXPLORER_DICT_DATA_DIR"] = str(tmp)
        try:
            yield tmp
        finally:
            if old is None:
                os.environ.pop("EXPLORER_DICT_DATA_DIR", None)
            else:
                os.environ["EXPLORER_DICT_DATA_DIR"] = old


@contextmanager
def temp_db():
    """临时 SQLite 库。"""
    with tmp_dir("db_") as tmp:
        db = Database(tmp / "test.sqlite3")
        try:
            yield db
        finally:
            db.close()


def make_service(db: Database, bridge=None, **config_overrides) -> tuple[CaptureService, Config]:
    cfg = Config(db)
    for k, v in config_overrides.items():
        cfg.set(k.replace("__", "."), str(v))
    svc = CaptureService(db, cfg, bridge)
    return svc, cfg


class FakeBridge:
    """假的 UIA helper 客户端。可传 dict 或 callable。

    process_id / hwnd 缺省（None）时**跟随当前（被伪造的）前台窗口**，
    这样才能真实模拟「UIA 读到的就是前台那个窗口」；
    显式传值则用于构造「身份不一致」的用例。

    真实 helper 会在读取前先核对 expected_hwnd/expected_pid（前台身份），
    这里同样核对：前台在调用过程中变了就直接返回 ``foreground_changed``，
    **不会**返回选区文本 —— 否则测试会误以为「切窗口也能读到词」。
    """

    def __init__(self, response, process_id=None, hwnd=None):
        self.response = response
        self.process_id = process_id
        self.hwnd = hwnd
        self.calls: list[tuple] = []
        self.expected: list[tuple] = []
        self.enabled = True
        self.stop_calls = 0
        #: 每次调用时**实时读到**的前台身份，供「调用途中切窗口」的用例使用
        self.foreground_at_call: list[tuple[int, int]] = []

    def _next(self, index: int):
        if callable(self.response):
            return self.response(index)
        return self.response

    def get_selection(self, x=None, y=None, context_chars=120, timeout=3.0,
                      expected_hwnd=None, expected_pid=None):
        index = len(self.calls)
        self.calls.append((x, y, context_chars))
        self.expected.append((expected_hwnd, expected_pid))
        if not self.enabled:
            raise AssertionError("门控拒绝时不应调用 UIA")
        fg = w32.foreground_info()
        self.foreground_at_call.append((int(fg.get("hwnd") or 0), int(fg.get("pid") or 0)))
        if not expected_hwnd or not expected_pid:
            # 与真实 helper 一致：没有可核对的身份就 fail-closed，不做读取
            return {"ok": False, "reason": "expected_window_required"}
        if int(fg.get("hwnd") or 0) != int(expected_hwnd) or \
                int(fg.get("pid") or 0) != int(expected_pid):
            return {"ok": False, "reason": "foreground_changed"}
        payload = dict(self._next(index))
        payload["process_id"] = fg.get("pid", 0) if self.process_id is None else self.process_id
        payload["hwnd"] = fg.get("hwnd", 0) if self.hwnd is None else self.hwnd
        return payload

    def ensure_started(self):
        return True

    def ping(self, timeout=1.0):
        return True

    def check(self, timeout=1.0):
        return {"ok": True}

    def set_enabled(self, value):
        self.enabled = bool(value)

    def stop(self):
        self.stop_calls += 1

    def start(self):
        return True

    def pid(self):
        return 4242

    def last_error(self):
        return ""


def ok_response(text="alpha", context="…alpha beta…", url="", top_title="",
                method="TextPattern", process_id=9999, hwnd=55501,
                context_prefix=None):
    """UIA 成功响应。

    ``context_prefix`` = 选区起点在 ``context`` 里的字符偏移（新版 helper 才回传：
    用来判定 Chromium/Edge 的 PDF 选区是不是少了尾字符）。缺省**不带这个键**，
    正好覆盖「旧 helper / 偏移未知 ⇒ 一个字都不补」的 fail-closed 路径。
    """
    resp = {
        "ok": True, "reason": "", "text": text, "context": context,
        "method": method, "url": url, "top_title": top_title,
        "found_via": "focused", "process_id": process_id, "hwnd": hwnd,
    }
    if context_prefix is not None:
        resp["context_prefix"] = int(context_prefix)
    return resp


def fail_response(reason="no_textpattern"):
    return {"ok": False, "reason": reason, "text": "", "context": ""}


@contextmanager
def visibility_gate(allowed: bool = True, reason: str = "ok"):
    """把**浮窗可见性门控**钉在指定判定上。

    只替换 ``AccessGate.evaluate`` 的返回值（供 FloatBar.show / 主界面按钮这类
    「显示入口」测试用），``w32.foreground_info`` 仍是真实（或被 foreground()
    伪造的）前台 —— 这样测试既能精确控制「允许/不允许显示」，
    又不会意外允许真实的全局鼠标钩子。
    """
    from app.gate import GateDecision

    decision = GateDecision(allowed=allowed, reason=reason)
    with mock.patch("app.gate.AccessGate.evaluate", return_value=decision):
        yield decision


@contextmanager
def no_process_foreground():
    """把前台伪造成「无前台窗口」：真实鼠标钩子/读取在测试里必须跳过。"""
    info = {"hwnd": 0, "pid": 0, "title": "", "class": "", "exe": "", "app": "",
            "is_self": False}
    with mock.patch("app.win32util.get_foreground_window", return_value=0), \
            mock.patch("app.win32util.get_window_pid", return_value=0), \
            mock.patch("app.gate.w32.foreground_info", return_value=info), \
            mock.patch("app.capture_service.w32.foreground_info", return_value=info), \
            mock.patch("app.uia_bridge.w32.foreground_info", return_value=info):
        yield info


DEFAULT_FG = {"title": "Some Document", "app": "msedge.exe", "pid": 9999, "hwnd": 55501,
              "class": "Chrome_WidgetWin_1"}


@contextmanager
def foreground(title="Some Document", app="msedge.exe", pid=9999, hwnd=55501, is_self=False,
               fullscreen=False, exe_path=None):
    """伪造前台窗口信息（含全屏判定）。

    :param exe_path: 需要真实进程全路径时传入（例如 ``aces.exe`` 这类游戏进程）；
        缺省用 ``C:\\Program Files\\<app>``，``exe_name()`` 取到的仍是 ``app``。

    同时把**真实的**低级鼠标钩子安装拦下来：门控未放行时测试绝不能真的装
    全局钩子（见 test_hook_gating / TestRealMouseHook）。
    """
    info = {
        "hwnd": hwnd, "pid": pid, "title": title,
        "class": "Chrome_WidgetWin_1" if app.endswith("exe") else "",
        "exe": exe_path or f"C:\\Program Files\\{app}", "app": app, "is_self": is_self,
    }
    with mock.patch("app.capture_service.w32.foreground_info", return_value=info):
        with mock.patch("app.gate.w32.foreground_info", return_value=info):
            with mock.patch("app.capture_service.w32.is_probably_desktop", return_value=False):
                with mock.patch("app.gate.w32.is_probably_desktop", return_value=False):
                    with mock.patch("app.gate.w32.is_fullscreen", return_value=fullscreen):
                        with mock.patch("app.capture_service.w32.window_root_from_point",
                                        return_value=0):
                            with mock.patch("app.capture_service.w32.window_root",
                                            side_effect=lambda h: int(h)):
                                with mock.patch("app.win32util.get_foreground_window",
                                                return_value=hwnd):
                                    with mock.patch("app.mouse_hook.w32.install_mouse_hook",
                                                    return_value=(0, None, 5)):
                                        yield info


@contextmanager
def hook_gate_allows(title="Reader", app="chrome.exe", pid=9999, hwnd=55501):
    """让**真实门控**把前台判定为一个白名单阅读窗口（专门用于真实钩子用例）。

    与 ``foreground()`` 的区别只有两点：
    * 不拦 ``MouseHook.start()``，但会把真实的 ``WH_MOUSE_LL`` 安装替换成
      「记录 + 报错」，钩子线程随即可退出 —— 不留下真实的全局监听；
    * ``install_mouse_hook`` 的调用说明**门控确实放行过**：
      若门控不允许，``App._start_services`` / 恢复路径根本不会调用它。

    这样「真实钩子测试」只跑在门控允许的前提下，且不安装真实全局监听。
    """
    info = {"hwnd": hwnd, "pid": pid, "title": title, "class": "Chrome_WidgetWin_1",
            "exe": f"C:\\Program Files\\{app}", "app": app, "is_self": False}

    def _record(_cb):
        MOUSE_HOOK_INSTALLS.append((hwnd, pid))
        return (0, None, 5)  # 故意失败：门控已放行，但不留下真实全局监听

    with mock.patch("app.gate.w32.foreground_info", return_value=info), \
            mock.patch("app.capture_service.w32.foreground_info", return_value=info):
        with mock.patch("app.gate.w32.is_probably_desktop", return_value=False), \
                mock.patch("app.gate.w32.is_fullscreen", return_value=False):
            with mock.patch("app.win32util.get_foreground_window", return_value=hwnd):
                with mock.patch("app.mouse_hook.w32.install_mouse_hook",
                                side_effect=_record):
                    yield info


# ============================================================================
# 无 GUI 的 App 装置（纯 mock）：**不创建任何 Tk 窗口、不装钩子、不启 UIA、不联网**
# ============================================================================
#
# 用户经常在玩游戏，所以「统一按钮 / 门控 / token 归属」这些回归用例一律用
# 这套假窗口装置跑：App 的状态机是真的，窗口层与 Win32 全部是记录型假对象。
# 真实窗口的显示语义（先定位后映射、可见时不重复映射、负坐标夹取）由
# ``FloatingWindowProbe`` 用**真实 FloatingWindow 代码 + 假 Tk widget** 覆盖。

#: 无 GUI 装置默认的「安全前台」：一个普通应用窗口（产品运行时默认放行）
DEFAULT_HEADLESS_FG = {
    "hwnd": 55501, "pid": 9999, "title": "Some Document",
    "class": "Chrome_WidgetWin_1", "exe": "C:\\Program Files\\msedge.exe",
    "app": "msedge.exe", "is_self": False,
}

#: 假浮层窗口的 HWND（用于「按下瞬间点在浮层上」的判定）
BAR_HWND = 555001
EXPLAIN_HWND = 555002
#: 假「别处」窗口
OTHER_HWND = 555003


class FakeRoot:
    """最小 Tk root 替身：只实现 App 真正会调用到的方法。"""

    def __init__(self):
        self.protocols: dict = {}
        self.after_calls: list = []
        #: 被 ``after_cancel`` 取消掉的定时器 id（「关窗要取消提示淡出」用得上）
        self.after_cancelled: list = []
        self.attributes_calls: list = []
        self.lift_calls = 0
        self.focus_calls = 0
        self.deiconify_calls = 0
        self.title_calls: list = []
        self.geometry_calls: list = []
        self.minsize_calls: list = []
        self.configure_calls: list = []
        self.destroy_calls = 0

    def title(self, text=None):
        if text is not None:
            self.title_calls.append(str(text))
        return self.title_calls[-1] if self.title_calls else ""

    def geometry(self, spec=None):
        if spec is not None:
            self.geometry_calls.append(str(spec))
        return self.geometry_calls[-1] if self.geometry_calls else "1x1+0+0"

    def minsize(self, *args):
        self.minsize_calls.append(tuple(args))

    def configure(self, **kw):
        self.configure_calls.append(dict(kw))

    config = configure

    def winfo_screenwidth(self):
        return 1920

    def winfo_screenheight(self):
        return 1080

    def winfo_width(self):
        return 1080

    def winfo_height(self):
        return 680

    def winfo_rootx(self):
        return 0

    def winfo_rooty(self):
        return 0

    def winfo_id(self):
        return 777001

    def winfo_children(self):
        return []

    def protocol(self, name, func=None):
        self.protocols[name] = func

    def after(self, ms, func=None, *args):
        self.after_calls.append((ms, func))
        return f"after-{len(self.after_calls)}"

    def after_cancel(self, timer=None):
        """真实 Tk 的 ``after_cancel``：登记被取消的定时器 id。

        没有它的话，「关窗要取消提示的淡出定时器」这条测试会被产品里的
        ``getattr(self.win, "after_cancel", None)`` 兜底悄悄跳过 —— 那等于用假环境
        的缺口替产品背书（见 ``tests/test_ui_roundrect.py`` 的提示折叠用例）。
        """
        self.after_cancelled.append(timer)
        return None

    def attributes(self, *a, **k):
        self.attributes_calls.append(a)

    def lift(self):
        self.lift_calls += 1

    def focus_force(self):
        self.focus_calls += 1

    def deiconify(self):
        self.deiconify_calls += 1

    def withdraw(self):
        return None

    def destroy(self):
        self.destroy_calls += 1

    def winfo_exists(self):
        return 1


class FakeStatusLabel:
    def __init__(self):
        self.text = ""

    def configure(self, **kw):
        if "text" in kw:
            self.text = str(kw["text"])

    def cget(self, key):
        return self.text if key == "text" else ""


class FakeMainWindow:
    """主界面替身：只记录刷新 / 选中调用。"""

    def __init__(self, root, app):
        self.root = root
        self.app = app
        self._selected_entry_id = None
        self.status_label = FakeStatusLabel()
        self.refresh_batches_calls = 0
        self.refresh_entries_calls = 0
        self.refresh_selected_calls = 0
        self.update_status_calls = 0
        self.select_calls: list[int] = []
        self.gate_changes: list = []
        #: 页面切换 → 主界面跟随（只记录调用次数，不改任何数据）
        self.follow_calls = 0
        self.apply_follow_calls = 0

    def follow_current_page(self):
        self.follow_calls += 1
        return False

    def apply_pending_follow(self, *, force: bool = False):
        self.apply_follow_calls += 1
        return False

    def refresh_batches(self):
        self.refresh_batches_calls += 1

    def refresh_entries(self):
        self.refresh_entries_calls += 1

    def refresh_selected(self):
        self.refresh_selected_calls += 1

    def select_entry(self, entry_id):
        self._selected_entry_id = int(entry_id)
        self.select_calls.append(int(entry_id))

    def update_status(self):
        self.update_status_calls += 1

    def on_gate_changed(self, decision):
        self.gate_changes.append(decision)


def make_explain_result(one_line: str = "一句话解释"):
    """真实的 ``ExplainResult`` 对象（纯内存，不联网、不写库）。"""
    from app.models import ExplainResult

    return ExplainResult(one_line=one_line, detail="详细说明", examples=["例1"],
                         raw="{}", from_cache=False,
                         model_config="https://api.example.com/v1|model-A")


class FakeSelectionBar:
    """浮条替身：语义与真实 ``SelectionBar`` 一致（唯一按钮，划选不落库）。"""

    def __init__(self, app, hwnd: int = BAR_HWND):
        self.app = app
        self._hwnd = int(hwnd)
        self.visible = False
        self.selection = None
        self.token = 0
        self.point = (0, 0)
        self.topmost = None
        self.show_calls = 0
        self.hide_calls = 0
        #: 按钮回调计数（模拟用户点「解释并记录」）
        self.action_calls = 0
        #: 与真实实现一致：同一次选区只消费一次 + 防重入 + 必须可见
        self._consumed_token: int | None = None
        self._busy = False

    def hwnd(self):
        return self._hwnd

    def set_topmost(self, value):
        self.topmost = bool(value)

    def hide(self):
        if not self.visible:
            return False
        self.visible = False
        self.hide_calls += 1
        return True

    def show_selection(self, selection, token, point=(0, 0)):
        """与真实实现同一条门控：``App.overlay_display_allowed``（软受限也不弹条）。"""
        self.selection = selection
        self.token = int(token)
        if self._consumed_token != self.token:
            self._consumed_token = None
        if not self.app.overlay_display_allowed(self, already_visible=False):
            return False
        self.point = point
        self.visible = True
        self.show_calls += 1
        return True

    def click_action(self):
        """模拟用户点击浮条上唯一的按钮「解释并记录」（含真实的可见性/一次消费守卫）。"""
        selection = self.selection
        if selection is None or not self.visible:
            return None
        if self._consumed_token == self.token or self._busy:
            return None
        self._busy = True
        try:
            self._consumed_token = int(self.token)
            self.action_calls += 1
            return self.app.explain_and_record_selection(selection)
        finally:
            self._busy = False

    def clear(self):
        self.hide()
        self.selection = None
        self._consumed_token = None

    def owns_point(self, x, y):
        return False

    # ---- 新面板协议（阅读面板把展开态折叠成小方块；旧替身等价于 hide）----
    def collapse_to_dock(self, *, reason: str = "", drop_selection: bool = False,
                         explicit: bool = False):
        self.collapse_calls = getattr(self, "collapse_calls", 0) + 1
        if drop_selection:
            self.selection = None
            self._consumed_token = None
        return self.hide()

    def restore_dock(self, *, explicit: bool = False):
        """真实面板的「保证小方块在屏幕上」；旧替身在允许时恢复可见。"""
        if self.visible:
            return True
        self.visible = True
        self.show_calls += 1
        return True

    def refresh_terms(self, **_kw):
        return False

    def on_chat_result(self, *_a, **_kw):
        return False


class FakeExplainWindow:
    """解释结果窗替身：只有「已记录 + 结果 + 重试 / 设置 / 关闭」，没有记录动作。

    与真实 ``ExplainWindow`` 同语义的关键点：

    * 结果更新要求 **entry_id + 请求 token 同时匹配**（``request_token=None``
      表示调用方没有 token 信息，退回「只看 entry」的历史语义）；
    * 隐藏 / 用户关闭后**绝不重开**（只写内存状态）。
    """

    def __init__(self, app, hwnd: int = EXPLAIN_HWND):
        self.app = app
        self._hwnd = int(hwnd)
        self.visible = False
        self.closed_by_user = False
        self.selection = None
        self.entry_id = None
        self.request_token = None
        self.token = None          # 旧字段名（= request_token）
        self.result = None
        self.status = ""
        self.message = ""
        self.show_calls = 0
        self.hide_calls = 0
        self.result_writes = 0
        #: 结果到达时窗口已经隐藏（写内容但**不重开**）的次数
        self.results_while_hidden = 0
        #: 因为 entry 不匹配 / 请求 token 不匹配而被拒绝的次数
        self.results_rejected = 0
        self.topmost = None

    def hwnd(self):
        return self._hwnd

    def set_topmost(self, value):
        self.topmost = bool(value)

    def hide(self):
        if not self.visible:
            return False
        self.visible = False
        self.hide_calls += 1
        return True

    def close_by_user(self):
        changed = self.hide()
        self.closed_by_user = True
        return changed

    def clear(self):
        self.hide()
        self.selection = None
        self.result = None
        self.entry_id = None
        self.request_token = None
        self.token = None
        self.message = ""
        self.status = ""

    def show_recorded(self, selection, entry_id, anchor=None, *, request_token=None,
                      token=None, status="pending", message="", explicit=False):
        if request_token is None:
            request_token = token
        self.selection = selection
        self.entry_id = int(entry_id)
        self.request_token = None if request_token is None else int(request_token)
        self.token = self.request_token
        self.result = None
        self.status = status
        self.message = message
        self.closed_by_user = False
        self.visible = True
        self.show_calls += 1
        return True

    def show_result(self, entry_id, status, result, error_text="", *, request_token=None):
        """与真实实现同语义：entry 不符 / 请求 token 不符拒绝；隐藏后绝不重开。"""
        if self.entry_id is None or int(self.entry_id) != int(entry_id):
            self.results_rejected += 1
            return False
        if request_token is not None and (
                self.request_token is None or int(request_token) != int(self.request_token)):
            self.results_rejected += 1
            return False
        self.status = status
        self.result = result if status == "ok" else None
        if not self.visible or self.closed_by_user:
            self.results_while_hidden += 1  # 写内容但**不重开**窗口
            return False
        self.result_writes += 1
        return True

    #: 与真实实现一致：只有还需要用户动作的状态才值得显式找回
    RESTORABLE_STATUS = ("pending_no_key", "pending", "error")

    def restore(self, anchor=None, *, explicit=True):
        if self.entry_id is None or self.closed_by_user:
            return False
        if self.status not in self.RESTORABLE_STATUS or not self.message:
            return False
        if self.visible:
            return True
        return self.show_recorded(self.selection, int(self.entry_id), anchor,
                                  request_token=self.request_token, status=self.status,
                                  message=self.message, explicit=explicit)

    def click_retry(self):
        if not self.entry_id:
            return False
        return self.app.retry_explain_entry(int(self.entry_id))

    def click_close(self):
        return self.close_by_user()

    def owns_point(self, x, y):
        return False

    # ---- 新面板协议（同一个窗口承担两个角色时的折叠 / 刷新钩子）----
    def collapse_to_dock(self, *, reason: str = "", drop_selection: bool = False,
                         explicit: bool = False):
        self.collapse_calls = getattr(self, "collapse_calls", 0) + 1
        if drop_selection:
            self.selection = None
        return self.hide()

    def restore_dock(self, *, explicit: bool = False):
        return bool(self.visible)

    def refresh_terms(self, **_kw):
        return False

    def on_chat_result(self, *_a, **_kw):
        return False


class MouseHookSpy:
    """代替真实 WH_MOUSE_LL：绝不安装全局钩子。"""

    def __init__(self):
        self.running = False
        self.starts = 0
        self.stops = 0

    def start(self, timeout: float = 5.0) -> bool:
        self.starts += 1
        self.running = True
        return True

    def stop(self, timeout: float = 3.0) -> None:
        self.stops += 1
        self.running = False

    def suppress(self, seconds: float = 0.4) -> None:
        pass

    def last_error(self) -> int:
        return 0


class UiaSpy:
    """代替真实 UiaBridge：绝不启动 PowerShell，只记录启停。"""

    def __init__(self):
        self.enabled = True
        self.stop_calls = 0
        self.start_calls = 0

    def set_enabled(self, value: bool) -> None:
        self.enabled = bool(value)

    def stop(self) -> None:
        self.stop_calls += 1

    def start(self) -> bool:
        self.start_calls += 1
        return self.enabled

    def pid(self):
        return None

    def ping(self, timeout: float = 1.0) -> bool:
        return self.enabled

    def last_error(self) -> str:
        return ""


class HotkeySpy:
    def __init__(self, *a, **k):
        self.registered: list[int] = []
        self.failed: list[tuple[int, str]] = []

    @property
    def recall_registered(self) -> bool:
        return False

    def start(self, timeout: float = 5.0) -> bool:
        return False

    def stop(self, timeout: float = 3.0) -> None:
        pass

    def restart(self, timeout: float = 5.0) -> bool:
        return False

    def missing_required(self):
        from app.hotkeys import required_failures
        return required_failures(self.registered)


class WatcherSpy:
    def __init__(self, *a, **k):
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def is_alive(self):
        return self.started


@contextmanager
def headless_app(*, fg=None, config=None, bridge=None, overlays: str = "fakes",
                 main_window: str = "fake"):
    """构造一个**纯 mock** 的 App：不建 Tk 窗口、不装钩子、不启 UIA、不联网。

    返回的 App 上：
    * ``app.selection_bar`` / ``app.explain_window`` 是记录型假窗口；
    * ``app.main`` 是 :class:`FakeMainWindow`；
    * ``app.mouse_hook`` / ``app.uia`` / ``app.hotkeys`` / ``app.watcher`` 全是 spy；
    * 前台元信息被钉在 ``fg``（默认普通应用窗口）。

    ``overlays="panel"``（**共享窗口链路**）：窗口层换成**真实
    :class:`~app.ui.reading_panel.ReadingPanel`**（假 Tk 控件 + 假 Win32，
    仍然一个真实窗口都不创建），``selection_bar is explain_window`` 与产品
    完全一致 —— 用于验证「App 的门控轮询 / 唯一动作 / 迟到结果」在**同一个
    窗口**上不会自相矛盾（两套替身会把这个类问题掩盖掉）。

    ``main_window="real"``：``app.main`` 换成**真实** ``MainWindow``
    （同样只建假控件），用于验证面板菜单 / 状态刷新在真实主界面代码上可用。
    纯逻辑测试不需要实时 GUI 预检 —— 这里一个窗口都不会创建。
    """
    from app.config import Config
    from app.db import Database
    import app.main as app_main

    info = dict(fg or DEFAULT_HEADLESS_FG)
    with temp_db() as db:
        cfg = Config(db)
        for k, v in (config or {}).items():
            cfg.set(k.replace("__", "."), str(v))
        root = FakeRoot()
        hook = MouseHookSpy()
        uia = UiaSpy()
        built = {}
        fake_env = _FakeTkEnv() if (overlays == "panel" or main_window == "real") else None

        def _build_overlays(_root, app_):
            if overlays == "panel":
                from app.ui.reading_panel import ReadingPanel

                panel = ReadingPanel(_root, app_)
                built["panel"] = panel
                return panel, panel
            bar = FakeSelectionBar(app_)
            exp = FakeExplainWindow(app_)
            built["bar"], built["exp"] = bar, exp
            return bar, exp

        def _main_window(_root, app_):
            if main_window == "real":
                from app.ui.main_window import MainWindow

                win = MainWindow(_root, app_)
                built["main"] = win
                return win
            return FakeMainWindow(_root, app_)

        if fake_env is not None:
            fake_env.__enter__()
            # 假窗口环境自带前台 / self / 桌面 / 全屏判定：不要再叠一层固定值，
            # 否则「阅读面板自己在前台」这类场景根本喂不进去。
            fake_env.w32.foreground_template = dict(info)
            foreground_patches = [mock.patch("app.main.w32", fake_env.w32),
                                  mock.patch("app.gate.w32", fake_env.w32)]
        else:
            foreground_patches = [
                mock.patch("app.win32util.foreground_info", return_value=info),
                mock.patch("app.win32util.is_probably_desktop", return_value=False),
                mock.patch("app.win32util.is_fullscreen", return_value=False),
                mock.patch("app.gate.w32.foreground_info", return_value=info),
                mock.patch("app.gate.w32.is_probably_desktop", return_value=False),
                mock.patch("app.gate.w32.is_fullscreen", return_value=False),
            ]
        try:
            with contextlib.ExitStack() as stack:
                for patch_obj in foreground_patches:
                    stack.enter_context(patch_obj)
                stack.enter_context(mock.patch("app.main.MainWindow", side_effect=_main_window))
                stack.enter_context(mock.patch("app.main.MouseHook", return_value=hook))
                stack.enter_context(mock.patch("app.main.UiaBridge", return_value=uia))
                stack.enter_context(mock.patch("app.main.HotkeyManager", HotkeySpy))
                stack.enter_context(mock.patch("app.main.ForegroundWatcher", WatcherSpy))
                stack.enter_context(mock.patch("app.main.build_overlays",
                                               side_effect=_build_overlays))
                box = stack.enter_context(mock.patch("app.main.messagebox"))
                # 主界面自己 import 了 messagebox（`from tkinter import messagebox`），
                # 不一起打桩的话「删除主题 / 删除词条」会在测试里弹出真实的模态框。
                stack.enter_context(mock.patch("app.ui.main_window.messagebox"))
                application = app_main.App(root, db, cfg)
                application.fake_messagebox = box
                application.fake_root = root
                application.fake_tk = fake_env
                application.built = built
                if bridge is not None:
                    application.capture_service.bridge = bridge
                try:
                    yield application
                finally:
                    application._closing = True
                    db.close()
        finally:
            if fake_env is not None:
                fake_env.__exit__(None, None, None)


def headless_press(app, x: int, y: int, *, overlay: bool = False, when: float | None = None):
    """模拟全局鼠标按下：在**按下时刻**采集元信息并入队（不碰真实 Win32）。

    ``overlay=True`` 表示按在浮条 / 解释窗上（此时 ``window_root_from_point``
    返回浮层 HWND），否则返回「别处」的窗口 HWND。
    """
    target = BAR_HWND if overlay else OTHER_HWND
    when = time.monotonic() if when is None else float(when)
    with mock.patch("app.capture_service.w32.window_root_from_point", return_value=target):
        meta = app.capture_service.handle_mouse_press(
            x, y, when, overlay_hwnds=app.overlay_hwnds())
    app._ui_q.put(("overlay_press", meta))
    return meta


def headless_gesture(app, x=10, y=10, kind="drag"):
    """模拟一次「划选」：直接走真实的 capture_service.handle_gesture。

    返回 ``(selection, reason)``；期间产生的 UI 事件仍在队列里，调用方自行
    用 ``pump_app(app)`` 消费。
    """
    gen = app.capture_service.current_generation()
    app._on_gesture(x, y, kind)
    app.capture_service.handle_gesture(x, y, kind, delay=0, generation=gen)
    return app.capture_service.last_selection(), app.capture_service.last_failure_reason


def pump_app(app, times: int = 5) -> None:
    """把 UI 队列里的真实事件交给 ``App._pump`` 消费（不驱动 Tk 主循环）。"""
    for _ in range(times):
        app._pump()


class _GestureQueueOnce:
    """只放行**一条**手势的假队列：首条消费完，下一次 ``get`` 置停止位并抛 Empty。

    ``App._gesture_loop`` 的真实循环是 ``while not stop``：真实队列上永远等不到
    结束条件。包装真实队列就能**同步**跑完一轮真实消费链路，而不必提前置停止位
    —— 先置位会让循环一次都不执行（那样测试等于什么都没验证）。
    """

    def __init__(self, source, stop, timeout: float):
        self._source = source
        self._stop = stop
        self._timeout = float(timeout)
        self.delivered = 0

    def get(self, timeout=None):
        if self.delivered:
            self._stop.set()
            raise queue.Empty
        try:
            item = self._source.get(timeout=self._timeout)
        except queue.Empty:
            self._stop.set()
            raise
        self.delivered += 1
        return item

    def put_nowait(self, item):
        return self._source.put_nowait(item)

    def empty(self) -> bool:
        return bool(self._source.empty())


def run_gesture_worker(app, *, timeout: float = 5.0) -> None:
    """像产品一样把**已入队**的手势交给取词工作线程处理（同步跑一轮）。

    走的是 ``App._dispatch_gesture`` 入队 → ``App._gesture_loop`` 消费这条
    **真实链路**：世代 / 页面世代指纹由 ``_dispatch_gesture`` 在入队时就写好
    （与拨动工作线程的时序一致），因此能验证「切页后第一个词」的归属判定。
    队列处理完即返回：首条之后由 :class:`_GestureQueueOnce` 置停止位，循环自然退出。
    """
    real_q, stop = app._gesture_q, app._gesture_stop
    app._gesture_q = _GestureQueueOnce(real_q, stop, timeout)
    try:
        app._gesture_loop()
    finally:
        app._gesture_q = real_q
        stop.clear()


def _chain_binds(binds: dict, sequence, func) -> None:
    """把一次 ``bind`` 记进假控件的 ``binds``，**保留先前绑定的那些回调**。

    真 Tk 的 ``bind(..., add="+")`` 是叠加：同一个控件上「点击」可以既走自己的
    处理、又走挂上去的悬浮说明（例如搜索框：点进去要收起占位提示，同时说明气泡
    要收掉）。早先这里直接 ``binds[sequence] = func``，后绑的把先绑的**顶掉**，
    于是「点进搜索框，占位提示没消失」这种真问题在假环境里根本红不了 ——
    拿替身的缺口替产品背书。

    仍然按**单个回调**取用（``binds[seq](event)`` / ``binds.get(seq)``）：只有一个
    回调时存的就是它本人，两个以上时存一个可调用的 :class:`_ChainedBinds`。
    """
    if func is None:
        binds.pop(sequence, None)
        return
    current = binds.get(sequence)
    if current is None:
        binds[sequence] = func
    elif isinstance(current, _ChainedBinds):
        current.append(func)
    else:
        binds[sequence] = _ChainedBinds([current, func])


class _ChainedBinds:
    """一个控件上同一事件的多条绑定，按登记顺序依次执行。"""

    def __init__(self, funcs):
        self.funcs = list(funcs)

    def append(self, func) -> None:
        self.funcs.append(func)

    def __call__(self, event=None):
        result = None
        for func in list(self.funcs):
            result = func(event)
        return result

    def __eq__(self, other):
        return list(self.funcs) == [other] if not isinstance(other, _ChainedBinds) \
            else self.funcs == other.funcs

    def __hash__(self):
        return hash(tuple(self.funcs))

    def __repr__(self):
        return f"<_ChainedBinds {[getattr(f, '__name__', f) for f in self.funcs]}>"


# ------------------------------------------------------------------ 浮窗探针
class FakeTkWindow:
    """假 Tk Toplevel：记录 geometry / deiconify / withdraw 的**调用顺序**。"""

    def __init__(self, width: int = 220, height: int = 70, **_kw):
        self._w = int(width)
        self._h = int(height)
        #: 建窗时传进来的配置（``bg`` / ``padx`` / ``pady`` …）。真实 Tk 会照单
        #: 收下；悬浮说明的假替身要能核对「说明小窗自己有没有多余的颜色」。
        self.kw: dict = dict(_kw)
        #: ``winfo_width`` / ``winfo_height`` 报告出来的**实测尺寸**：默认与展开面板
        #: 一致，测试可以改写它来模拟「Tk 还停在旧尺寸上」这类时序。
        self.winfo_w = 360
        self.winfo_h = 460
        self.events: list[str] = []
        self.geometry_specs: list[str] = []
        #: ``bind`` 登记的回调（顶层窗口**不该**有拖动绑定，回归据此断言）
        self.binds: dict = {}
        #: ``after_idle`` 登记的回调（浮窗的 region 回执走这条路径，可手动驱动）
        self.idle_callbacks: list = []
        #: ``after`` 登记的回调（只登记不执行，与 :class:`FakeRoot` 一致）
        self.after_calls: list = []
        #: 被 ``after_cancel`` 取消掉的定时器 id
        self.after_cancelled: list = []
        self.mapped = False

    # --- FloatingWindow 用到的 Tk API ---
    def withdraw(self):
        self.events.append("withdraw")
        self.mapped = False

    def deiconify(self):
        self.events.append("deiconify")
        self.mapped = True

    def lift(self):
        self.events.append("lift")

    def after_idle(self, func=None, *args):
        self.idle_callbacks.append(func)
        return f"idle-{len(self.idle_callbacks)}"

    def after(self, ms, func=None, *args):
        """真实 Toplevel 的 ``after``：**只登记、不执行**（与 :class:`FakeRoot` 一致）。

        导图窗口的右下角提示把「20 秒后淡出」排在这里（``self.win.after``）。没有这个
        方法的话，产品里那句 ``callable(after)`` 会兜底跳过 —— 于是「提示到底有没有
        排上定时器、延时是不是 20 秒」在假环境里**永远测不出来**（拿替身的缺口替产品
        背书）。登记下来，测试就能自己把那一次回调跑掉。
        """
        self.after_calls.append((ms, func))
        if not ms and func is not None:
            # 真实 Tk 的 ``after(0, …)`` 就是「下一轮事件循环立刻做」：等到真跑起来时
            # 它跟同步调用没有区别，所以这里直接跑掉。这一步是必要的 ——
            # ``settings_dialog._finish_connection_test`` 与 ``concept_map`` 的
            # 「回 UI 线程」都走 ``after(0, …)``，只登记不执行的话结果永远回不来。
            # 真正要等的定时器（延时 > 0）仍然只登记、由测试自己驱动。
            func(*args)
        return f"after-{len(self.after_calls)}"

    def after_cancel(self, timer=None):
        """真实 Toplevel 的 ``after_cancel``：登记被取消的定时器 id。"""
        self.after_cancelled.append(timer)
        return None

    def geometry(self, spec):
        self.events.append(f"geometry:{spec}")
        self.geometry_specs.append(spec)

    def update_idletasks(self):
        self.events.append("update_idletasks")

    def winfo_reqwidth(self):
        return self._w

    def winfo_reqheight(self):
        return self._h

    def winfo_screenwidth(self):
        return 1920

    def winfo_screenheight(self):
        return 1080

    def winfo_id(self):
        return 4242

    def focus_force(self):
        self.focus_calls = getattr(self, "focus_calls", 0) + 1

    def focus_set(self):
        self.focus_calls = getattr(self, "focus_calls", 0) + 1

    def winfo_ismapped(self):
        return self.mapped

    def overrideredirect(self, *_a, **_k):
        self.events.append("overrideredirect")

    def configure(self, *_a, **_k):
        return None

    def title(self, text=None):
        self._title = str(text) if text is not None else getattr(self, "_title", "")
        return self._title

    def transient(self, master=None):
        self.transient_master = master

    def resizable(self, *_a, **_k):
        return None

    def attributes(self, *_a, **_k):
        return None

    def bind(self, sequence=None, func=None, **_k):
        _chain_binds(self.binds, sequence, func)
        return None

    def destroy(self):
        self.events.append("destroy")

    # --- 面板菜单（app_menu.popup）用到的定位 API ---
    def winfo_rootx(self):
        return 100

    def winfo_rooty(self):
        return 100

    def winfo_width(self):
        return int(self.winfo_w)

    def winfo_height(self):
        return int(self.winfo_h)

    def winfo_exists(self):
        return 1


class FakeFloatingW32:
    """``app.ui.floating.w32`` 的替身：置顶 / 可见性 / 工作区全部可控。"""

    def __init__(self, work_area=(-1920, 0, 0, 1080)):
        self.work_area = work_area
        self.topmost: set[int] = set()
        self.no_activate: set[int] = set()
        self.windows: dict[int, FakeTkWindow] = {}
        self.topmost_calls: list[tuple[int, bool]] = []
        self.visible_calls = 0
        self.activate_calls: list[int] = []
        self.work_area_calls: list[tuple[int, int]] = []
        #: ``get_foreground_window`` 的返回值（默认没有前台窗口）
        self.foreground_hwnd = 0
        #: 圆角 region：``(hwnd, w, h, radius)`` 调用记录 + 失败开关
        self.region_calls: list[tuple[int, int, int, int]] = []
        self.region_clears: list[int] = []
        self.region_ok = True
        #: 子窗口 HWND → 顶层祖先（``window_root`` / GetAncestor(GA_ROOT)）
        self.root_map: dict[int, int] = {}
        #: 这些 HWND 属于本进程其它窗口（主词典 / 设置对话框）→ ``is_self=True``
        self.is_self_hwnds: set[int] = set()
        #: ``foreground_info`` 的基础字段
        self.foreground_template: dict = {
            "hwnd": 55501, "pid": 9999, "title": "Some Document", "app": "msedge.exe",
            "exe": "C:\\Program Files\\msedge.exe", "is_self": False,
        }

    def set_work_area(self, work_area):
        """模拟显示器变化（拔掉副屏 / 改分辨率）。"""
        self.work_area = work_area

    def toplevel_hwnd(self, widget_id):
        return int(widget_id)

    # --- 圆角 region（真实实现走 CreateRoundRectRgn / SetWindowRgn）---
    def apply_round_region(self, hwnd, width, height, radius):
        self.region_calls.append((int(hwnd), int(width), int(height), int(radius)))
        return bool(self.region_ok)

    def clear_window_region(self, hwnd):
        self.region_clears.append(int(hwnd))
        return True

    def make_no_activate(self, hwnd):
        self.no_activate.add(int(hwnd))
        return True

    def clear_no_activate(self, hwnd):
        self.no_activate.discard(int(hwnd))
        return True

    def activate_window(self, hwnd):
        self.activate_calls.append(int(hwnd))
        return True

    def is_no_activate(self, hwnd):
        return int(hwnd) in self.no_activate

    def set_topmost(self, hwnd, value):
        self.topmost_calls.append((int(hwnd), bool(value)))
        if value:
            self.topmost.add(int(hwnd))
        else:
            self.topmost.discard(int(hwnd))

    def is_topmost(self, hwnd):
        return int(hwnd) in self.topmost

    def is_window_visible(self, hwnd):
        self.visible_calls += 1
        win = self.windows.get(int(hwnd))
        return bool(win and win.mapped)

    def is_probably_desktop(self, hwnd):
        return False

    def is_fullscreen(self, hwnd, tolerance: int = 0):
        return False

    def is_self_window(self, hwnd):
        return bool(int(hwnd) in self.is_self_hwnds)

    def work_area_for_point(self, x, y):
        self.work_area_calls.append((int(x), int(y)))
        return self.work_area

    def window_root_from_point(self, x, y):
        return int(x)  # 测试里用坐标当 HWND，便于断言「点在谁身上」

    def get_foreground_window(self):
        return int(self.foreground_hwnd)

    def window_root(self, hwnd):
        return int(self.root_map.get(int(hwnd), int(hwnd)))

    def foreground_info(self):
        """门控读的前台元信息（默认与 ``DEFAULT_HEADLESS_FG`` 一致）。

        测试改了 ``foreground_hwnd`` 之后，``is_self`` 也要跟着变 —— 这正是
        「主词典窗口在前台」与「阅读面板自己在前台」两种 self 场景的分界。
        """
        fg = int(self.foreground_hwnd or 0)
        base = dict(self.foreground_template)
        if fg:
            base["hwnd"] = fg
            base["is_self"] = bool(fg in self.is_self_hwnds)
        return base


class FloatingWindowProbe:
    """用**真实 FloatingWindow 代码** + 假 Tk widget 验证显示语义（无 GUI）。"""

    def __init__(self, width: int = 220, height: int = 70, work_area=(-1920, 0, 0, 1080),
                 topmost_pref: bool = True, display_gate=None):
        from app.ui.floating import FloatingWindow

        self.width = width
        self.height = height
        self.w32 = FakeFloatingW32(work_area)
        self.rendered: list[str] = []
        probe = self

        class _Probe(FloatingWindow):
            def _render(self, text):
                probe.rendered.append(text)

        stub = types.SimpleNamespace(config=types.SimpleNamespace(topmost=topmost_pref))
        if display_gate is not None:
            # 与 App.overlay_display_allowed 同签名（window, *, already_visible, explicit）
            stub.overlay_display_allowed = display_gate
        app_stub = stub
        self._stack = contextlib.ExitStack()
        self._stack.enter_context(mock.patch("app.ui.floating.w32", self.w32))
        self._stack.enter_context(mock.patch("app.ui.floating.tk.Toplevel",
                                             side_effect=self._make_win))
        self.win = _Probe(None, app_stub, name="probe")

    def _make_win(self, master=None):
        widget = FakeTkWindow(self.width, self.height)
        self.widget = widget
        hwnd = widget.winfo_id()
        self.w32.windows[hwnd] = widget
        return widget

    def close(self):
        self._stack.close()

    # --- 便捷断言 ---
    def events(self):
        return list(self.widget.events)

    def show_calls(self):
        return self.win.show_calls

    def deiconify_count(self):
        return sum(1 for e in self.widget.events if e == "deiconify")


def floating_probe(**kwargs) -> FloatingWindowProbe:
    return FloatingWindowProbe(**kwargs)


# ============================================================================
# 真实浮层类 + 假 Tk 控件（**一个窗口都不创建**）
# ============================================================================
#
# ``SelectionBar`` / ``ExplainWindow`` 的显示门控、请求 token 归属、
# 「同一次选区只消费一次」这些语义都在**真实类**里，用假 Tk 控件跑真实代码
# 才能验证 —— 只断言字符串或假的替身是不够的。

class FakeVar:
    """``tk.StringVar`` / ``tk.BooleanVar`` 的替身（只需要 get / set）。"""

    def __init__(self, value=None):
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value

    def trace_add(self, *_a, **_k):  # 真实代码暂时不用，留个安全实现
        return ""


#: 假 Tk 里登记的「Tcl 窗口路径」→ 控件实例。
#: 真实 Tk 里 ``Widget._w`` 就是这条路径（例如 ``.!frame.!canvas``），
#: ``create_line`` / ``bind`` / ``pack`` 全靠它找窗口；产品代码若把它覆盖成
#: 别的值，实机上这些命令全部失败。假控件也照同一条路径校验（见
#: :meth:`FakeWidget.tcl_call`），因此这种 bug 在零真实窗口的回归里就能炸出来。
_TCL_PATHS: dict[str, object] = {}
_TCL_PATH_SEQ = itertools.count(1)


class FakeTclPathError(RuntimeError):
    """把 Tcl 命令发给了非法窗口路径（真实 Tk 会报 ``bad window path name``）。"""


def _new_tcl_path(instance, master) -> str:
    """给假控件分配一条形如 ``.!w12`` 的 Tcl 窗口路径并登记（模拟真实 Tk）。"""
    parent = getattr(master, "_w", ".")
    if not isinstance(parent, str) or not parent.startswith(".") \
            or _TCL_PATHS.get(parent) is not master:
        parent = "."
    path = f"{parent}!w{next(_TCL_PATH_SEQ)}"
    _TCL_PATHS[path] = instance
    return path


def _init_fake_widget(instance, master=None, **kw):
    """``FakeWidget`` 的初始化实现。

    独立成模块级函数（并保留一份**原始引用**）：假环境里会把
    ``tk.Frame.__init__`` / ``tk.Label.__init__`` 换成 :func:`_fake_tk_init`，
    子类（``ScrollFrame`` / ``FlatButton``）再调 ``super().__init__`` 时
    必须回到这里，而不能回到那个已被替换的 ``FakeWidget.__init__``（会无限递归）。
    """
    instance.master = master
    instance.kw = dict(kw)
    instance.packed = 0
    instance.pack_kw = {}
    instance.binds = {}
    instance.placed = 0
    instance.place_kw = {}
    instance._buffer = str(kw.get("text", ""))
    instance._children = []
    instance.tk = getattr(master, "tk", None)
    #: 本控件的 Tcl 窗口路径（真实 Tk 的 ``_w``）+ 真实发出去的命令记录
    instance._w = _new_tcl_path(instance, master)
    instance.tcl_calls = []
    instance.illegal_tcl_calls = []
    if isinstance(master, FakeWidget):
        master._children.append(instance)
    return instance


def _fake_tk_init(self, *args, **kwargs):
    """``tk.Frame.__init__`` / ``tk.Label.__init__`` 的替身。

    真实 ``tk.Frame`` 的子类在导入时就绑定了基类，光 patch ``tkinter.Frame``
    拦不住它们；这里把**基类的 __init__ 就地换掉**，让「真实窗口类 + 假控件」
    的组合成立（一个真实窗口都不创建）。
    """
    if getattr(self, "_fake_inited", False):
        return None
    _init_fake_widget(self, args[1] if len(args) > 1 else kwargs.get("master"))
    self._fake_inited = True
    return None


class FakeWidget:
    """假 Tk 控件：记录 configure/pack/bind/place，并维护一个最小文本缓冲。"""

    #: 由 :class:`_FakeTkEnv` 临时挂上的「pack 调用顺序」日志（布局结构验证用）。
    #: 真实 Tk 里 pack 的分配顺序 = 调用顺序，因此这个顺序就是**空间优先级**：
    #: 先 pack 的控件先拿到自己的空间（``side="bottom"`` 的底部输入行同理）。
    pack_log: list | None = None

    def __init__(self, master=None, **kw):
        _init_fake_widget(self, master, **kw)
        self.master = master
        self.kw = dict(kw)
        self.packed = 0
        self.pack_kw = {}
        self.binds: dict = {}
        #: ``after`` 登记的回调（``(延时, 函数)``，只登记不执行）与取消记录
        self.after_calls: list = []
        self.after_cancelled: list = []
        self.placed = 0
        self.place_kw: dict = {}
        self._buffer = str(kw.get("text", ""))
        self._children: list = []
        if isinstance(master, FakeWidget):
            master._children.append(self)

    # --- Tcl 窗口路径（真实 Tk 的 ``_w``）---
    def tcl_call(self, command: str) -> str:
        """记录一次发给本控件的 Tcl 命令，并校验窗口路径真的指向本控件。

        真实 Tk 里每条命令都是 ``self.tk.call((self._w, command, …))``：``_w``
        不是合法路径就直接失败。历史 bug（chevron 子类写 ``self._w = 宽度``）正是
        这样让实机上一条线都画不出来，而只会「记录属性」的假控件看不出来。
        这里不放过任何非法路径 —— 抛 :class:`FakeTclPathError` 并留痕，
        **绝不**为了让测试通过而放宽判据。
        """
        path = self.__dict__.get("_w")
        if not isinstance(path, str) or not path.startswith(".") \
                or _TCL_PATHS.get(path) is not self:
            self.illegal_tcl_calls.append((command, path))
            raise FakeTclPathError(
                f"Tcl 命令 {command!r} 的窗口路径非法：{path!r}"
                f"（真实 Tk 里 _w 是控件路径，不是尺寸 / 数字）")
        self.tcl_calls.append((path, command))
        return path

    def tcl_path(self) -> str:
        """本控件当前登记过的 Tcl 窗口路径（供回归断言用）。"""
        return self.tcl_call("path")

    # --- 布局 / 配置 ---
    def pack(self, *_a, **_k):
        self.tcl_call("pack")
        self.packed += 1
        self.pack_kw = dict(_k)
        log = FakeWidget.pack_log
        if log is not None:
            log.append((self, dict(_k)))
        return None

    #: 真实 Tk 的 ``pack_configure`` 就是 ``pack`` 的别名（重新配置已 pack 的控件）
    pack_configure = pack

    grid = pack

    def pack_forget(self):
        self.packed = 0
        return None

    def place(self, *_a, **_k):
        self.tcl_call("place")
        self.placed += 1
        self.place_kw = dict(_k)
        return None

    def place_forget(self):
        self.placed = 0
        return None

    def place_info(self):
        """真实 Tk 的 ``place_info()``：当前 place 参数（没 place 过就是空字典）。"""
        return dict(self.place_kw)

    def pack_propagate(self, *_a, **_k):
        return None

    propagate = pack_propagate

    def columnconfigure(self, *_a, **_k):
        return None

    def rowconfigure(self, *_a, **_k):
        return None

    grid_columnconfigure = columnconfigure
    grid_rowconfigure = rowconfigure

    def configure(self, cnf=None, **kw):
        self.tcl_call("configure")
        if isinstance(cnf, dict):
            kw = {**cnf, **kw}
        self.kw.update(kw)
        if "text" in kw:
            self._buffer = str(kw["text"])
        return None

    config = configure

    def cget(self, key):
        return self.kw.get(key, "")

    def bind(self, sequence=None, func=None, **_k):
        self.tcl_call("bind")
        _chain_binds(self.binds, sequence, func)
        return None

    def bind_all(self, *_a, **_k):
        return None

    def winfo_children(self):
        return list(self._children)

    def winfo_reqwidth(self):
        text = str(self.kw.get("text", "") or "")
        if text:
            return 12 + 8 * len(text)
        return int(self.kw.get("width", 80) or 80)

    def winfo_reqheight(self):
        return 22

    def winfo_width(self):
        return int(self.kw.get("width", 0) or 0)

    def winfo_height(self):
        # 真控件总是有高度的。恒返回 0 会让「把说明摆到控件正下方」这类
        # 定位计算在假环境里算出荒唐坐标 —— 那是替身的缺口，不是产品的行为。
        return 22

    def winfo_rootx(self):
        return 100

    def winfo_rooty(self):
        return 100

    def winfo_screenwidth(self):
        return 1920

    def winfo_screenheight(self):
        return 1080

    def winfo_exists(self):
        return 1

    def winfo_id(self):
        return 0

    def focus_set(self):
        return None

    focus_force = focus_set

    def destroy(self):
        self.tcl_call("destroy")
        for child in list(self._children):
            child.destroy()
        self._children = []
        self.destroyed = True
        if isinstance(self.master, FakeWidget) and self in self.master._children:
            self.master._children.remove(self)
        return None

    # --- 文本控件用到的 API ---
    def delete(self, *_a):
        self._buffer = ""

    def insert(self, _index, text):
        self._buffer += str(text)

    def get(self, *_a):
        return self._buffer

    def yview(self, *_a):
        return None

    def yview_scroll(self, *_a):
        return None

    def set(self, *_a):
        return None

    # --- Canvas（ScrollArea / 自绘卡片 / 自绘滑轨用）---
    def create_window(self, *_a, **_k):
        return 1

    def itemconfigure(self, *_a, **_k):
        return None

    def bbox(self, *_a):
        return (0, 0, 0, 0)

    def create_text(self, *_a, **_k):
        return 1

    def create_line(self, *_a, **_k):
        return 1

    def create_oval(self, *_a, **_k):
        return 1

    def create_polygon(self, *_a, **_k):
        return 1

    def coords(self, *_a, **_k):
        return ()

    def update_idletasks(self):
        return None

    def after(self, ms, func=None, *args):
        """真实控件的 ``after``：**只登记、不执行**（与 :class:`FakeRoot` 一致）。

        导图窗口的提示把淡出的每一拍排在提示自己的 Frame 上。登记下来（而不是像
        早先那样直接 ``return None``），测试才能自己驱动「20 秒到 → 淡出 → 折叠」
        这条链子；不然产品把定时器排在哪个控件上、排了几拍，假环境里全看不见。
        """
        self.after_calls.append((ms, func))
        if not ms and func is not None:
            # 同 ``FakeTkWindow.after``：``after(0, …)`` 在真 Tk 上就是「立刻做」，
            # 这里直接跑掉；延时 > 0 的定时器只登记，由测试自己驱动。
            func(*args)
        return f"after-{len(self.after_calls)}"

    def after_cancel(self, timer=None):
        """真实控件的 ``after_cancel``：登记被取消的定时器 id。"""
        self.after_cancelled.append(timer)
        return None

    # --- Listbox（主界面批次列表）---
    def insert(self, _index, text):       # noqa: F811 - 与 Text 的 insert 兼容
        self._children.append(str(text))
        self._buffer += str(text)

    def selection_clear(self, *_a):
        return None

    def selection_set(self, *_a):
        return None

    def curselection(self):
        return ()

    def see(self, *_a):
        return None

    # --- Menu（面板弹出菜单 / 主窗口菜单栏）---
    def add_command(self, **_kw):
        return None

    def add_checkbutton(self, **_kw):
        return None

    def add_separator(self, *_a, **_k):
        return None

    def tk_popup(self, *_a, **_k):
        return None

    def grab_release(self):
        return None


def _flat_coords(args) -> list:
    """把 ``create_*`` 的坐标参数摊平（tkinter 允许 ``create_polygon([x, y, …])``）。"""
    out: list = []
    for arg in args:
        if isinstance(arg, (list, tuple)):
            out.extend(_flat_coords(arg))
        else:
            out.append(arg)
    return out


def _clamp_origin(origin: float, low: float, high: float, window: float) -> float:
    """Tk ``CanvasSetOrigin`` 的限幅：视图原点必须让整个视口留在滚动范围内。

    滚动范围不比视口大时原点钉在左 / 上界（内容装得下 = 不可滚动）。范围**从负
    坐标开始**时照同一条规则算 —— 概念图窗口四边各留一个视口（见
    ``app.ui.concept_map.ConceptMapWindow._region_box``），原点因此可以在负数与
    正数之间自由移动。
    """
    try:
        origin, low = float(origin), float(low)
        high, window = float(high), float(window)
    except (TypeError, ValueError):  # pragma: no cover - 极简替身
        return 0.0
    if high - low <= window:
        return low
    return max(low, min(high - window, origin))


def _view_span(low: float, high: float) -> float:
    """滚动范围总宽 / 总高（真实 Tk 的 ``scrollX2 - scrollX1``，至少 1）。"""
    return max(1.0, float(high) - float(low))


class FakeCanvas(FakeWidget):
    """假 Canvas：**真的**记录 item / scrollregion / 视图原点。

    比 :class:`FakeWidget` 多出来的部分正是新控件需要的：圆角卡片与自绘滑轨会
    ``create_polygon`` / ``coords`` / ``itemconfigure`` / ``delete``（悬停改色、
    缩放重排），``ScrollArea`` 会 ``configure(scrollregion=…)`` 并 ``yview``。
    这些都必须真的记下来 —— 否则「两列卡片不重叠」「滑块拖动映射 yview」这类
    断言在假环境里等于没跑（把新滚动行为换成假空类是明确禁止的）。

    视图语义按**真实 Tk** 实现（``canvasx`` / ``xview`` / ``yview`` /
    ``moveto`` / ``scroll`` 一致）：状态是画布坐标里的**视图原点**
    （``xOrigin`` / ``yOrigin``），分数只是派生量
    ``(原点 - 滚动范围起点) / 总宽``。因此**负起点**的 ``scrollregion``
    （概念图窗口四边各留一个视口）也算得对，改 ``scrollregion`` 时与 Tk 一样
    **保留原点、只按新范围限幅**（刷新不会把用户的世界坐标偏移换成新分数）。

    每条画布命令都先过 :meth:`FakeWidget.tcl_call`：命令发往的窗口路径必须仍是
    创建时登记的那条 ``_w``。真实 Tk 里自绘控件若把 ``_w`` 覆盖成尺寸，实机上
    ``create_line`` 会直接失败；这里让假 Canvas 也照同一条路径校验，回归里就能
    抓到「实机画不出来、假测试却全绿」的那类 bug。
    """

    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self.items: dict[int, dict] = {}
        self.item_options: dict[int, dict] = {}
        self._next_item = 1
        self.scrollregion = None
        self.yview_calls: list[tuple] = []
        self.xview_calls: list[tuple] = []
        #: 视图**原点**在画布坐标里的位置（真实 Tk 的 ``xOrigin`` / ``yOrigin``）。
        #: ``xview()`` / ``yview()`` 返回的分数由它换算而来（见 ``view_state``）。
        self.x_origin = 0.0
        self.y_origin = 0.0
        #: ``-xscrollincrement`` / ``-yscrollincrement``（真实 Tk：> 0 时
        #: ``*view_scroll(n, "units")`` 的 1 unit 就是这个像素数）
        self._xinc = int(kw.get("xscrollincrement", 0) or 0)
        self._yinc = int(kw.get("yscrollincrement", 0) or 0)
        self._width = int(kw.get("width", 0) or 0)
        self._height = int(kw.get("height", 0) or 0)

    # ------------------------------------------------------------ 配置
    def configure(self, cnf=None, **kw):
        if isinstance(cnf, dict):
            kw = {**cnf, **kw}
        if "xscrollincrement" in kw:
            self._xinc = int(float(kw.pop("xscrollincrement") or 0))
        if "yscrollincrement" in kw:
            self._yinc = int(float(kw.pop("yscrollincrement") or 0))
        if "width" in kw:
            self._width = int(kw.pop("width") or 0)
        if "height" in kw:
            self._height = int(kw.pop("height") or 0)
        if "scrollregion" in kw:
            region = kw.pop("scrollregion")
            try:
                self.scrollregion = tuple(int(float(v)) for v in region)
            except (TypeError, ValueError):
                self.scrollregion = None
            # 真实 Tk 改 ``-scrollregion`` 时**保留视图原点**、只按新范围限幅
            # （``CanvasSetOrigin``）：内容变大 / 变小都不许把用户实际的世界坐标
            # 偏移悄悄换成「按新范围重算的分数」。
            self._set_x_origin(self.x_origin)
            self._set_y_origin(self.y_origin)
        return super().configure(cnf, **kw)

    config = configure

    def cget(self, key):
        if key == "width":
            return self._width
        if key == "height":
            return self._height
        return super().cget(key)

    def winfo_width(self):
        return int(self._width)

    def winfo_height(self):
        return int(self._height)

    # -------------------------------------------------------------- item
    def _add_item(self, kind: str, coords, options: dict) -> int:
        self.tcl_call(f"create_{kind}")
        item = self._next_item
        self._next_item += 1
        self.items[item] = {"kind": kind, "coords": list(coords)}
        self.item_options[item] = dict(options)
        return item

    def create_polygon(self, *args, **kw):
        return self._add_item("polygon", _flat_coords(args), kw)

    def create_rectangle(self, *args, **kw):
        return self._add_item("rectangle", _flat_coords(args), kw)

    def create_oval(self, *args, **kw):
        return self._add_item("oval", _flat_coords(args), kw)

    def create_line(self, *args, **kw):
        return self._add_item("line", _flat_coords(args), kw)

    def create_text(self, *args, **kw):
        return self._add_item("text", _flat_coords(args), kw)

    def create_window(self, *args, **kw):
        self.window_items = getattr(self, "window_items", [])
        self.window_items.append((_flat_coords(args), dict(kw)))
        return self._add_item("window", _flat_coords(args), kw)

    def coords(self, item, *args):
        self.tcl_call("coords")
        key = int(item)
        entry = self.items.get(key)
        if entry is None:
            return ()
        if args:
            entry["coords"] = _flat_coords(args)
        return tuple(entry["coords"])

    def find_withtag(self, tag) -> list:
        """真 ``tk.Canvas.find_withtag`` 的替身：按 tag（或 id）取图元 id，按创建顺序。

        产品代码**只能**走这条路认图元。曾经拖动是读 ``item_options`` 里的 ``_kind``
        找图元 —— 那个字典**只有这个替身有**，真 ``tk.Canvas`` 上没有，于是实机上
        左键拖不动卡片、松手才瞬移，而这里一直是绿的（2026-10-05 用户报障）。
        """
        self.tcl_call("find_withtag")
        return self._tagged(tag)

    def move(self, tag_or_id, x_amount, y_amount) -> None:
        """真 ``tk.Canvas.move`` 的替身：``tag_or_id`` 可以是 tag，整组平移。

        真 Tk 上是**一条 Tcl 命令搬一组图元**（实测 2 个图元 0.003 ms）——
        拖动每个 Motion 事件都要走它，替身必须跟真的一致，别让产品代码
        在这里退化成「逐个图元读坐标再写回」。
        """
        self.tcl_call("move")
        dx, dy = float(x_amount), float(y_amount)
        for item in self._tagged(tag_or_id):
            entry = self.items.get(item)
            if entry is None:  # pragma: no cover - _tagged 已保证存在
                continue
            entry["coords"] = [value + (dx if index % 2 == 0 else dy)
                               for index, value in enumerate(entry["coords"])]
        return None

    def _tagged(self, tag) -> list[int]:
        if isinstance(tag, int):
            return [tag] if tag in self.items else []
        out = []
        for item, options in self.item_options.items():
            tags = options.get("tags") or ()
            if isinstance(tags, str):
                tags = (tags,)
            if tag in tags:
                out.append(item)
        return sorted(out)

    def itemconfigure(self, item, cnf=None, **kw):
        self.tcl_call("itemconfigure")
        if isinstance(cnf, dict):
            kw = {**cnf, **kw}
        for key in self._tagged(item):
            self.item_options.setdefault(key, {}).update(kw)
        return None

    itemconfig = itemconfigure

    def itemcget(self, item, key):
        return self.item_options.get(int(item), {}).get(key)

    def delete(self, *args):
        self.tcl_call("delete")
        for tag in args:
            for item in self._tagged(tag):
                self.items.pop(item, None)
                self.item_options.pop(item, None)
        self._buffer = ""
        return None

    def bbox(self, item=None):
        self.tcl_call("bbox")
        if item is None:
            coords = [c for entry in self.items.values() for c in entry["coords"]]
        else:
            coords = list(self.items.get(int(item), {}).get("coords", ()))
        if not coords:
            return (0, 0, 0, 0)
        xs, ys = coords[0::2], coords[1::2]
        return (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))

    # ------------------------------------------------------------- yview
    def _region_box(self) -> tuple[float, float, float, float]:
        """滚动范围 ``(left, top, right, bottom)``（真实 Tk 的 ``-scrollregion``）。

        没设过 ``scrollregion`` 时 Tk 用画布内所有 item 的外框；假画布不追踪
        item 几何，因此退回「一屏」（等价于不可滚动）。
        """
        if not self.scrollregion:
            return (0.0, 0.0, float(self._width), float(self._height))
        left, top, right, bottom = (float(value) for value in self.scrollregion)
        return (left, top, right, bottom)

    def _set_x_origin(self, origin: float) -> None:
        left, _top, right, _bottom = self._region_box()
        self.x_origin = _clamp_origin(origin, left, right, float(self._width))

    def _set_y_origin(self, origin: float) -> None:
        _left, top, _right, bottom = self._region_box()
        self.y_origin = _clamp_origin(origin, top, bottom, float(self._height))

    @property
    def yview_state(self) -> tuple[float, float]:
        """当前 ``yview()`` 的分数对（真实 Tk：**相对滚动范围**，可为负起点）。"""
        _left, top, _right, bottom = self._region_box()
        span = _view_span(top, bottom)
        first = (self.y_origin - top) / span
        return (first, first + float(self._height) / span)

    @property
    def view_state(self) -> tuple[float, float]:
        """当前 ``xview()`` 的分数对（与 :attr:`yview_state` 同构）。"""
        left, _top, right, _bottom = self._region_box()
        span = _view_span(left, right)
        first = (self.x_origin - left) / span
        return (first, first + float(self._width) / span)

    def yview(self, *args):
        """真实 Tk 语义：无参数 = **查询**（不算一条命令、不进调用日志）；
        带参数（``moveto`` / ``scroll``）才记日志并按 Tk 规则挪动视图。

        ``moveto`` 的分数是**相对滚动范围**的：``原点 = 起点 + 分数 × 总高``，
        之后按范围限幅（Tk 的 ``CanvasSetOrigin``）—— 越界 / 负数的分数由 Tk
        夹回可见范围，调用方不必自己夹。
        """
        if not args:
            return self.yview_state
        self.yview_calls.append(tuple(args))
        if args[0] == "moveto" and len(args) > 1:
            try:
                fraction = float(args[1])
            except (TypeError, ValueError):  # pragma: no cover - 极简替身
                return self.yview_state
            _left, top, _right, bottom = self._region_box()
            self._set_y_origin(top + fraction * _view_span(top, bottom))
        return self.yview_state

    def yview_moveto(self, fraction):
        return self.yview("moveto", fraction)

    def yview_scroll(self, number, what="units"):
        """像真实 Tk 一样**真的挪动视图**（右键平移的回归要能量到位移）。

        ``units``：``yscrollincrement > 0`` 时 1 unit = 那个像素数（产品设成 1 →
        与指针位移严格 1:1），否则是一屏的 1/10；``pages`` = 一屏的 9/10。
        """
        self.yview_calls.append(("scroll", number, what))
        try:
            count = float(number)
        except (TypeError, ValueError):  # pragma: no cover - 极简替身
            return None
        step = float(self._yinc) if (str(what) == "units" and self._yinc > 0) \
            else float(self._height) * (0.1 if str(what) == "units" else 0.9)
        self._set_y_origin(self.y_origin + count * step)
        return None

    # ------------------------------------------------------------- xview
    def xview(self, *args):
        """与 :meth:`yview` 同构（真实 Tk 的 ``xview``；右键平移用它读视图位置）。"""
        if not args:
            return self.view_state
        self.xview_calls.append(tuple(args))
        if args[0] == "moveto" and len(args) > 1:
            try:
                fraction = float(args[1])
            except (TypeError, ValueError):  # pragma: no cover - 极简替身
                return self.view_state
            left, _top, right, _bottom = self._region_box()
            self._set_x_origin(left + fraction * _view_span(left, right))
        return self.view_state

    def xview_moveto(self, fraction):
        return self.xview("moveto", fraction)

    def xview_scroll(self, number, what="units"):
        """与 :meth:`yview_scroll` 同构（``xscrollincrement = 1`` → 1:1 像素）。"""
        self.xview_calls.append(("scroll", number, what))
        try:
            count = float(number)
        except (TypeError, ValueError):  # pragma: no cover - 极简替身
            return None
        step = float(self._xinc) if (str(what) == "units" and self._xinc > 0) \
            else float(self._width) * (0.1 if str(what) == "units" else 0.9)
        self._set_x_origin(self.x_origin + count * step)
        return None

    # ------------------------------------------------- canvas ↔ screen 坐标
    def canvasx(self, screenx, gridspacing=None):
        """屏幕 x → 画布 x（真实 Tk：``视图原点 + 屏幕坐标``，偏移只加这一次）。"""
        return float(screenx) + float(self.x_origin)

    def canvasy(self, screeny, gridspacing=None):
        """屏幕 y → 画布 y（与 :meth:`canvasx` 同构）。"""
        return float(screeny) + float(self.y_origin)

    def item_count(self) -> int:
        return len(self.items)


class DisplayGateStub:
    """``App.overlay_display_allowed`` 的替身，三档语义与产品实现一致。

    * ``allow``：普通阅读前台 → 全部放行；
    * ``hard``：游戏 / 全屏 / 用户暂停 → 一律拒绝（含已显示的窗口）；
    * ``soft``：本程序自己的窗口 / 桌面 / 无前台 → 选区条拒绝；
      解释结果窗**已显示**或**显式找回**时保留。
    """

    def __init__(self, mode: str = "allow", result_window=None):
        self.mode = mode
        self.result_window = result_window
        self.calls: list[tuple[object, bool, bool]] = []

    def __call__(self, window=None, *, already_visible: bool = False,
                 explicit: bool = False) -> bool:
        self.calls.append((window, bool(already_visible), bool(explicit)))
        if self.mode == "hard":
            return False
        if self.mode == "allow":
            return True
        return window is self.result_window and bool(already_visible or explicit)


class _FakeTkEnv:
    """假 Tk 环境：Toplevel + 控件工厂 + Win32 替身（**一个真实窗口都不建**）。"""

    def __init__(self, width: int = 220, height: int = 70,
                 work_area=(-1920, 0, 0, 1080)):
        self.w32 = FakeFloatingW32(work_area)
        self.widgets: list[FakeWidget] = []
        self.windows: list[FakeTkWindow] = []
        self.buttons: list = []
        #: 所有被创建出来的假 Canvas（自绘卡片 / 滑轨 / 滚动容器的断言用）
        self.canvases: list[FakeCanvas] = []
        #: ``(控件, pack 关键字)`` 的**调用顺序**日志（布局结构验证用）
        self.pack_calls: list = []
        self.width = width
        self.height = height
        self._stack = contextlib.ExitStack()

    def __enter__(self):
        import tkinter as tk

        FakeWidget.pack_log = self.pack_calls
        self._stack.callback(lambda: setattr(FakeWidget, "pack_log", None))
        self._stack.enter_context(mock.patch("app.ui.floating.w32", self.w32))
        self._stack.enter_context(mock.patch("app.ui.reading_panel.w32", self.w32))
        # 先导入 ttk（它在导入时会对 tkinter.Entry 等做子类化；
        # 若在 tkinter 被 mock 之后才导入会触发 metaclass 冲突）
        import tkinter.ttk  # noqa: F401
        for name in ("Toplevel", "Frame", "Label", "Text", "Canvas", "Scrollbar", "Entry",
                     "Listbox", "Checkbutton", "Radiobutton", "Menu"):
            self._stack.enter_context(mock.patch(f"tkinter.{name}",
                                                 side_effect=self._make_toplevel_or_widget(name)))
        # ``tk.Frame`` / ``tk.Label`` 的子类（主界面 ScrollFrame、widgets.FlatButton）
        # 在导入时就绑定了基类，直接 patch 模块属性拦不住它们；这里**就地改写基类
        # 的 ``__init__``**（魔术方法不能用 mock.patch.object），退出时逐个还原。
        for base in _REAL_TK_BASES:
            original_init = base.__init__
            original_setup = base._setup
            original_methods = {name: getattr(base, name, None)
                                for name in _FAKE_BASE_METHODS}
            base.__init__ = _fake_tk_init
            base._setup = lambda *a, **k: None
            for name in _FAKE_BASE_METHODS:
                setattr(base, name, _fake_base_method(name))

            def _restore(base=base, init=original_init, setup=original_setup,
                         methods=original_methods):
                base.__init__ = init
                base._setup = setup
                for name, func in methods.items():
                    if func is not None:
                        setattr(base, name, func)

            self._stack.callback(_restore)
        self._stack.enter_context(mock.patch("tkinter.ttk.Scrollbar",
                                             side_effect=self._make_widget))
        self._stack.enter_context(mock.patch("tkinter.ttk.Combobox",
                                             side_effect=self._make_widget))
        # BooleanVar / StringVar：真实 Tk 需要 root；假环境里给一个可读写的小对象
        self._stack.enter_context(mock.patch("tkinter.BooleanVar",
                                             side_effect=lambda master=None, value=None,
                                             **_k: FakeVar(bool(value) if value is not None
                                                           else False)))
        self._stack.enter_context(mock.patch("tkinter.StringVar",
                                             side_effect=lambda master=None, value=None,
                                             **_k: FakeVar("" if value is None
                                                           else str(value))))
        # FlatButton 继承自 tk.Label（在导入时就绑定了基类），假环境里换成
        # 记录型工厂：面板测试关心的是状态机与门控，不是按钮的绘制细节。
        self._stack.enter_context(mock.patch("app.ui.widgets.FlatButton",
                                             side_effect=self._make_button))
        return self

    def __exit__(self, *exc):
        self._stack.close()
        return False

    def _make_toplevel_or_widget(self, name):
        if name == "Toplevel":
            return self._make_toplevel
        if name == "Canvas":
            return self._make_canvas
        return self._make_widget

    def _make_canvas(self, master=None, **kw):
        widget = FakeCanvas(master, **kw)
        self.widgets.append(widget)
        self.canvases.append(widget)
        return widget

    def _make_var(self, master=None, value=None, name=None):
        return FakeVar(value)

    def _make_toplevel(self, master=None, **kw):
        # 配置原样交给替身（``FakeTkWindow`` 收进 ``self.kw``）：丢掉的话，
        # 「说明小窗有没有自己的底色 / 内边距」这类断言在假环境里根本无从查起
        # —— 那又是一次「拿替身的缺口替产品背书」。
        widget = FakeTkWindow(self.width, self.height, **kw)
        self.windows.append(widget)
        self.w32.windows[widget.winfo_id()] = widget
        return widget

    def _make_widget(self, master=None, **kw):
        widget = FakeWidget(master, **kw)
        self.widgets.append(widget)
        return widget

    def _make_button(self, master=None, text="", command=None, **kw):
        widget = FakeWidget(master, text=text)
        widget.command = command
        widget.primary = bool(kw.get("primary"))
        widget.invoke = lambda: command() if command else None
        widget.set_enabled = lambda value: setattr(widget, "enabled", bool(value))
        widget.is_enabled = lambda: getattr(widget, "enabled", True)
        self.widgets.append(widget)
        self.buttons.append(widget)
        return widget

    def toplevel(self) -> FakeTkWindow:
        return self.windows[0]

    def deiconify_count(self) -> int:
        return sum(1 for e in self.toplevel().events if e == "deiconify")

    def withdraw_count(self) -> int:
        return sum(1 for e in self.toplevel().events if e == "withdraw")

    def find(self, text: str):
        """按文案找控件（标签 / 按钮）。"""
        for widget in self.widgets:
            if str(widget.cget("text")) == text:
                return widget
        return None

    def find_button(self, text: str):
        for widget in self.buttons:
            if str(widget.cget("text")) == text:
                return widget
        return None

    # ------------------------------------------------- 布局结构（pack 顺序）
    def pack_index(self, widget) -> int:
        """控件第几个被 pack（-1 = 没 pack 过）。越小 = 空间优先级越高。"""
        for i, (w, _kw) in enumerate(self.pack_calls):
            if w is widget:
                return i
        return -1

    def pack_kw(self, widget) -> dict:
        for w, kw in self.pack_calls:
            if w is widget:
                return dict(kw)
        return {}


class FakePanelConfig:
    """面板用得到的最小设置对象（``get`` / ``set`` + 几个属性）。"""

    def __init__(self, values=None, *, topmost: bool = True):
        self.values = {"api.base_url": "https://api.example.com/v1",
                       "api.model": "model-A",
                       "chat.history_turns": "6",
                       "chat.no_key_hint": "尚未配置 API Key，问题没有被发送。",
                       "chat.enabled": "1"}
        if values:
            self.values.update({str(k): str(v) for k, v in values.items()})
        self.topmost = bool(topmost)

    def get(self, key, default=None):
        return self.values.get(key, default if default is not None else "")

    def set(self, key, value):
        self.values[str(key)] = str(value)

    def get_bool(self, key, default=False):
        v = self.values.get(key)
        if v is None:
            return bool(default)
        return str(v).strip() in ("1", "true", "True", "yes", "on")

    def save_panel_state(self, state):
        state.save(self.set)

    def panel_state(self):
        from app.ui.panel_geometry import PanelState

        return PanelState.load(self.get)


class PanelProbe:
    """真实 ``ReadingPanel`` + 假 Tk 控件 + 假 Win32（**不创建真实窗口**）。

    面板的全部状态机（折叠 / 展开 / 外部点击 / 拖动 / 改尺寸 / 追问激活）
    都在真实代码里，只有 Tk 控件与 Win32 是替身。
    """

    def __init__(self, gate_mode: str = "allow", *, work_area=(-1920, 0, 0, 1080),
                 config_values=None, db=None, topmost: bool = True):
        self.env = _FakeTkEnv(work_area=work_area)
        self.env.__enter__()
        self.actions: list[tuple] = []
        self.questions: list[tuple] = []
        self.retries: list[int] = []
        self.settings_opened = 0
        self.asked: list[int] = []
        #: 追问「正在回答」的词条（宿主 chat_inflight 的替身，可被测试写入）
        self.inflight_entries: set[int] = set()
        #: 解释「正在解释」的词条（宿主 explain_service.is_inflight 的替身）
        self.explain_inflight: set[int] = set()
        #: 详情页「重新解释」的调用记录 + 返回值开关
        self.explain_retries: list[int] = []
        self.retry_explain_result = True
        self.explain_settings_opened = 0
        self.gate = DisplayGateStub(gate_mode)
        self.config = FakePanelConfig(config_values, topmost=topmost)
        self.db = db
        stub = types.SimpleNamespace(config=self.config, db=db)
        stub.overlay_display_allowed = self.gate
        stub.explain_and_record_selection = self._record_action
        stub.retry_explain_entry = self._retry_explain
        stub.explain_service = types.SimpleNamespace(is_inflight=self._explain_inflight)
        stub.open_settings = self._open_settings
        stub.ask_entry_question = self._ask
        stub.chat_inflight = self._inflight
        stub.chat_unavailable_hint = lambda: self.config.get("chat.no_key_hint")
        stub.retry_entry_question = self._retry
        #: 顶栏短主题（新契约）与旧的「上下文行」都提供：面板必须优先取短主题
        stub.panel_topic_line = lambda: "默认批次"
        stub.panel_context_line = lambda: "示例文档 · 《默认批次》"
        #: 浏览范围（浮窗顶栏主题清单 ⇄ 主界面侧栏共用的同一份状态）
        self.browse = {"id": None}
        stub.browse_scope = lambda: self.browse["id"]
        stub.set_browse_scope = self._set_browse
        stub.open_app_menu = lambda panel=None: None
        self.stub = stub
        from app.ui.reading_panel import ReadingPanel

        self.panel = ReadingPanel(None, stub)
        self.gate.result_window = self.panel

    # ------------------------------------------------------------ 回调替身
    def _record_action(self, selection, anchor=None):
        self.actions.append((selection, anchor))
        return len(self.actions)

    def _open_settings(self):
        self.settings_opened += 1
        self.explain_settings_opened += 1

    def _retry_explain(self, entry_id, anchor=None):
        """宿主 retry_explain_entry 的替身：真实实现会**同步**把该词标成在途。"""
        self.explain_retries.append(int(entry_id))
        if not self.retry_explain_result:
            return False
        self.explain_inflight.add(int(entry_id))
        return True

    def _explain_inflight(self, entry_id):
        return int(entry_id) in self.explain_inflight

    def _ask(self, entry_id, question):
        self.questions.append((int(entry_id), str(question)))
        if not self.config.get_bool("chat.enabled", True):
            return None
        if not self.config.values.get("_has_key", "1") == "1":
            return None
        if self.db is not None:
            # 真实 ``ChatService.submit`` 在**返回 request_id 之前**就把用户那一问
            # 同步写进库（"写提问" 与 "起线程" 之间没有 await）：这里照做，
            # 面板的回显 / 历史读取才是在同一条真实时序上验证的。
            try:
                self.db.add_chat_turn(entry_id=int(entry_id), role="user",
                                      content=str(question),
                                      request_id=len(self.questions))
            except Exception:  # pragma: no cover - 探针不该因为写库失败而炸
                pass
        token = len(self.questions)
        self.asked.append(int(entry_id))
        self.inflight_entries.add(int(entry_id))
        return token

    def _inflight(self, entry_id):
        return int(entry_id) in self.inflight_entries

    def _retry(self, entry_id):
        self.retries.append(int(entry_id))
        return len(self.retries)

    def _set_browse(self, batch_id):
        """宿主 ``App.set_browse_scope`` 的替身：改状态 + 通知浮窗刷新。"""
        value = None if batch_id in (None, "") else int(batch_id)
        changed = self.browse["id"] != value
        self.browse["id"] = value
        self.panel.refresh_terms(force=True)
        return changed

    # ------------------------------------------------------------ 便捷断言
    def close(self):
        self.env.__exit__(None, None, None)

    @property
    def mode(self) -> str:
        return self.panel.mode

    @property
    def page(self) -> str:
        return self.panel.page

    @property
    def visible(self) -> bool:
        return bool(self.panel.visible)

    def deiconify_count(self) -> int:
        return self.env.deiconify_count()

    def withdraw_count(self) -> int:
        return self.env.withdraw_count()

    def geometry_specs(self) -> list:
        return list(self.env.toplevel().geometry_specs)

    def last_geometry(self) -> str:
        specs = self.geometry_specs()
        return specs[-1] if specs else ""



class SelectionBarProbe:
    """真实 ``SelectionBar`` + 假 Tk 控件：验证唯一按钮的守卫与显示门控。"""

    def __init__(self, gate_mode: str = "allow"):
        self.env = _FakeTkEnv()
        self.env.__enter__()
        self.actions: list[tuple] = []
        self.gate = DisplayGateStub(gate_mode)
        stub = types.SimpleNamespace(config=types.SimpleNamespace(topmost=True))

        def _record(selection, anchor=None):
            self.actions.append((selection, anchor))
            return 1

        stub.explain_and_record_selection = _record
        stub.overlay_display_allowed = self.gate
        from app.ui.selection_bar import SelectionBar

        self.bar = SelectionBar(None, stub)

    def close(self):
        self.env.__exit__(None, None, None)

    @property
    def visible(self) -> bool:
        return bool(self.bar.visible)


class ExplainWindowProbe:
    """真实 ``ExplainWindow`` + 假 Tk 控件：验证结果归属 token 与显式恢复。"""

    def __init__(self, gate_mode: str = "allow"):
        self.env = _FakeTkEnv()
        self.env.__enter__()
        self.retries: list[int] = []
        self.settings_opened = 0
        self.gate = DisplayGateStub(gate_mode)
        stub = types.SimpleNamespace(config=types.SimpleNamespace(topmost=True))

        def _retry(entry_id, anchor=None):
            self.retries.append(int(entry_id))
            return True

        def _settings():
            self.settings_opened += 1

        stub.retry_explain_entry = _retry
        stub.open_settings = _settings
        stub.overlay_display_allowed = self.gate
        from app.ui.explain_window import ExplainWindow

        self.win = ExplainWindow(None, stub)
        self.gate.result_window = self.win

    def close(self):
        self.env.__exit__(None, None, None)

    @property
    def visible(self) -> bool:
        return bool(self.win.visible)

    def body(self) -> str:
        return self.win._body_text()

    def deiconify_count(self) -> int:
        return self.env.deiconify_count()

    def withdraw_count(self) -> int:
        return self.env.withdraw_count()

