"""执行前 GUI 预检（Preflight）：任何会**创建窗口**的动作在开始之前必须通过它。

为什么需要这个模块
------------------
本项目是阅读辅助工具，用户经常在玩游戏（例如 War Thunder / ``aces.exe``）。
自动测试、截图脚本、演示脚本如果直接开 Tk 窗口，会：

* 抢走前台焦点、可能把游戏切到窗口化（严重干扰游戏，甚至影响渲染）；
* 让「全屏/游戏前台必须暂停取词」的安全承诺在**测试进程**里失效。

因此规定：**只有实时前台满足「已知阅读应用 + 非全屏 + 非游戏模式」时，
才允许创建窗口**；其余情况一律 skip / 退出，并且**一个窗口都不创建**。

注意：这里的「已知阅读应用」清单与产品运行时的取词放行**不是同一件事**。
产品运行时默认放开普通窗口（见 ``app/permissions.py`` 的说明），
但自动化 GUI 预检**故意更保守**、不随产品放宽而放宽 ——
自动化脚本在用户玩游戏或任何未预期的前台下都不能建窗口。

不可被 mock 绕过的保证
----------------------
* 判定只用**实时的真实前台元信息** —— 在模块导入时就**冻结**真实的
  ``app.win32util`` 函数引用（见 :func:`_freeze_real_api`），之后无论谁
  ``mock.patch("app.win32util.foreground_info", ...)`` 都改不到预检读到的数据；
* 快照与判定也**不看**任何被伪造的门控对象/判定结果；
* 没有任何「允许」参数、环境变量或强制开关能把它变成放行
  （唯一的隐藏环境变量 ``EXPLORER_DICT_EXPECT_UNSAFE=0`` 只能让它**更严**）；
* 判定逻辑 :func:`evaluate_preflight` 是纯函数，只接受调用方传入的
  **同一份**实时快照，测试可以覆盖逻辑但不能伪造放行结果。

本模块只做只读操作：读窗口元信息 + 以 ``mode=ro`` 打开设置库读
``gate.game_mode``。不读密钥、不写任何文件、不安装钩子、不读 UIA。
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

#: 冻结的**真实** Win32 读取函数（在模块导入时绑定，之后不受 mock.patch 影响）
_REAL_API: dict[str, object] = {}


def _freeze_real_api() -> None:
    """在导入时刻抓取真实函数对象，供预检永久使用。

    这是「预检不可被 mock 绕过」的关键：测试/脚本里常见的
    ``mock.patch("app.win32util.foreground_info", ...)`` 只替换模块属性，
    改不到这里已经存下的函数对象。
    """
    try:
        from . import win32util as w32

        _REAL_API["fg_info"] = w32.foreground_info
        _REAL_API["is_fullscreen"] = w32.is_fullscreen
        _REAL_API["platform_ok"] = w32.platform_ok
    except Exception as exc:  # pragma: no cover - 导入失败即 fail-closed
        _REAL_API["error"] = f"{type(exc).__name__}: {exc}"


_freeze_real_api()

# ------------------------------------------------------------------ 原因码
P_OK = "ok"
P_GAME_MODE = "game_mode_setting"
P_GAME_PROCESS = "game_process"
P_NO_FOREGROUND = "no_foreground"
P_SELF = "self_window"
P_DESKTOP = "desktop"
P_NOT_WHITELISTED = "not_whitelisted"
P_FULLSCREEN = "fullscreen"
P_NOT_WINDOWS = "not_windows"
P_UNKNOWN_ERROR = "preflight_error"

#: 允许创建窗口的唯一原因码
ALLOW_REASON = P_OK

REASON_LABELS: dict[str, str] = {
    P_OK: "前台是已知阅读窗口且非全屏，允许创建窗口",
    P_GAME_MODE: "用户已开启「游戏模式」（设置 gate.game_mode=1），禁止创建窗口",
    P_GAME_PROCESS: "前台是已知游戏/游戏平台进程，禁止创建窗口",
    P_NO_FOREGROUND: "没有可用的前台窗口，禁止创建窗口",
    P_SELF: "前台是本项目自己的窗口，禁止创建窗口",
    P_DESKTOP: "前台是桌面/任务栏，禁止创建窗口",
    P_NOT_WHITELISTED: "前台不是已知阅读应用（自动化预检比产品运行时更严格），禁止创建窗口",
    P_FULLSCREEN: "前台窗口处于全屏状态，禁止创建窗口",
    P_NOT_WINDOWS: "当前不是 Windows，无法做前台预检，禁止创建窗口",
    P_UNKNOWN_ERROR: "预检读取前台元信息失败（fail-closed），禁止创建窗口",
}


#: 用户若要**主动**做一次 GUI 验收（明确知道自己会被打扰）才设这个为 1。
#: 本模块不读它来做放行判断 —— 只有会创建窗口的工具脚本才读，
#: 且必须同时在说明里标注「仅用户主动验收时使用」。
CONSENT_ENV = "EXPLORER_DICT_ALLOW_GUI_TESTS"


@dataclass(frozen=True)
class ForegroundSnapshot:
    """一份**实时**前台窗口快照（只用标准 Win32 元信息）。"""

    hwnd: int = 0
    pid: int = 0
    exe: str = ""
    app: str = ""
    title: str = ""
    cls: str = ""
    is_self: bool = False
    fullscreen: bool = False
    supported: bool = True          # 是否成功读到了信息（读取失败 → False）
    error: str = ""

    def describe(self) -> str:
        if not self.supported:
            return f"读取前台信息失败：{self.error or '未知原因'}"
        if not self.hwnd:
            return "无前台窗口"
        return (f"{self.app or self.exe or '未知进程'} "
                f"hwnd={self.hwnd} pid={self.pid} 标题={self.title[:60]!r}"
                f"{'（全屏）' if self.fullscreen else ''}")


@dataclass(frozen=True)
class Preflight:
    """预检结论。``allowed`` 为 True 才允许创建窗口。"""

    allowed: bool
    reason: str
    snapshot: ForegroundSnapshot = field(default_factory=ForegroundSnapshot)
    detail: str = ""

    @property
    def label(self) -> str:
        return REASON_LABELS.get(self.reason, self.reason)

    def skip_message(self, what: str = "GUI 测试") -> str:
        """给 ``unittest.SkipTest`` / 脚本用的中文说明。"""
        return (f"前台预检未放行：{self.label}｜{self.detail or self.snapshot.describe()}"
                f"｜已跳过{what}，未创建任何窗口。"
                f"如需真实验收：请先切到已知阅读应用（浏览器/记事本等）的"
                f"非全屏窗口，并确认游戏模式（Ctrl+Alt+Shift+G）已关闭。")


def _exe_name(path_or_name: str) -> str:
    return os.path.basename((path_or_name or "").strip()).lower()


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _default_db_path() -> Path:
    """设置库路径。允许测试/脚本用 EXPLORER_DICT_DATA_DIR 重定向（与 app.paths 一致）。"""
    override = os.environ.get("EXPLORER_DICT_DATA_DIR")
    if override:
        return Path(override) / "explorer_dict.sqlite3"
    return Path(__file__).resolve().parent.parent / "data" / "explorer_dict.sqlite3"


def read_game_mode(db_path=None) -> bool:
    """只读检查用户设置里的「手动游戏模式」。读不到就当作关闭（默认值）。

    只查 ``settings`` 表的 ``gate.game_mode`` 一行，**不读 secrets 表**。
    """
    path = Path(db_path) if db_path else _default_db_path()
    if not path.exists():
        return False
    try:
        uri = path.absolute().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except (sqlite3.Error, OSError):
        return False
    try:
        row = conn.execute("SELECT v FROM settings WHERE k = 'gate.game_mode'").fetchone()
        return _truthy(row[0]) if row else False
    except sqlite3.Error:
        return False
    finally:
        try:
            conn.close()
        except sqlite3.Error:  # pragma: no cover
            pass


def snapshot_foreground() -> ForegroundSnapshot:
    """读取**实时**前台元信息，只走导入时冻结的真实 Win32 函数。

    因此无论调用者是否正在 ``mock.patch("app.win32util...")``，
    这里拿到的都是真实系统状态 —— mock 无法伪造这次预检。
    """
    try:
        if "error" in _REAL_API:
            return ForegroundSnapshot(supported=False,
                                      error=f"Win32 封装不可用：{_REAL_API['error']}")
        fg_info = _REAL_API["fg_info"]
        is_fullscreen = _REAL_API["is_fullscreen"]
        if not _REAL_API["platform_ok"]():
            return ForegroundSnapshot(supported=False, error="非 Windows 平台")

        info = fg_info()
        hwnd = int(info.get("hwnd") or 0)
        pid = int(info.get("pid") or 0)
        exe_path = info.get("exe") or ""
        fullscreen = bool(is_fullscreen(hwnd)) if hwnd else False
        return ForegroundSnapshot(
            hwnd=hwnd,
            pid=pid,
            exe=exe_path,
            app=info.get("app") or _exe_name(exe_path),
            title=info.get("title") or "",
            cls=info.get("class") or "",
            is_self=bool(info.get("is_self")),
            fullscreen=fullscreen,
            supported=True,
        )
    except Exception as exc:  # pragma: no cover - 读取失败一律 fail-closed
        return ForegroundSnapshot(supported=False, error=f"{type(exc).__name__}: {exc}")


def evaluate_preflight(snapshot: ForegroundSnapshot, game_mode: bool) -> Preflight:
    """纯判定：给定**实时快照**与游戏模式设置，是否可以创建窗口。

    判定顺序（任一不满足即拒绝）：
    1. 平台/读取失败 → 拒绝（fail-closed）
    2. 用户手动游戏模式 → 拒绝
    3. 前台是已知游戏/游戏平台进程 → 拒绝
    4. 无前台 / 本进程窗口 / 桌面 → 拒绝
    5. 前台不是**已知阅读应用** → 拒绝（比产品运行时严格，见模块文档）
    6. 前台窗口全屏 → 拒绝
    7. 其它 → 允许

    注意：函数**不接受** ``allowed`` 之类的入参，也不读环境变量，
    因此调用方无法用「先伪造一个允许状态」的方式绕过真实预检。
    """
    from .permissions import GAME_APPS, READING_APPS

    if not snapshot.supported:
        return Preflight(False, P_NOT_WINDOWS if snapshot.error == "非 Windows 平台"
                         else P_UNKNOWN_ERROR, snapshot, snapshot.error)
    if game_mode:
        return Preflight(False, P_GAME_MODE, snapshot)
    exe = _exe_name(snapshot.exe or snapshot.app)
    if exe in GAME_APPS:
        return Preflight(False, P_GAME_PROCESS, snapshot)
    if not snapshot.hwnd or not snapshot.pid:
        return Preflight(False, P_NO_FOREGROUND, snapshot)
    if snapshot.is_self or snapshot.pid == os.getpid():
        return Preflight(False, P_SELF, snapshot)
    if snapshot.cls in ("Progman", "WorkerW", "Shell_TrayWnd"):
        return Preflight(False, P_DESKTOP, snapshot)
    if exe not in READING_APPS:
        return Preflight(False, P_NOT_WHITELISTED, snapshot)
    if snapshot.fullscreen:
        return Preflight(False, P_FULLSCREEN, snapshot)
    return Preflight(True, P_OK, snapshot)


def check(db_path=None) -> Preflight:
    """完整预检：实时前台快照 + 只读游戏模式设置。

    任何异常都返回「不允许」（fail-closed）。
    """
    try:
        snapshot = snapshot_foreground()
        if snapshot.error == "非 Windows 平台":
            return evaluate_preflight(snapshot, False)
        return evaluate_preflight(snapshot, read_game_mode(db_path))
    except Exception as exc:  # pragma: no cover
        return Preflight(False, P_UNKNOWN_ERROR,
                         ForegroundSnapshot(supported=False, error=str(exc)), str(exc))


def user_consented() -> bool:
    """用户是否**主动**声明「我现在允许被 GUI 测试打扰」。

    只给会创建窗口的工具脚本用（见各脚本说明），预检本身不读它。
    """
    return _truthy(os.environ.get(CONSENT_ENV, ""))


def require(what: str = "GUI 测试", db_path=None) -> Preflight:
    """预检不通过时抛 :class:`RuntimeError`。

    供 ``unittest.SkipTest`` 之外的脚本使用（如 tools/ 下的截图脚本）；
    测试请用 :func:`tests.support.skip_unless_foreground_safe`。
    """
    decision = check(db_path)
    if not decision.allowed:
        raise RuntimeError(decision.skip_message(what))
    return decision
