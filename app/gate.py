"""前台环境门控（默认放开普通窗口，只挡游戏 / 全屏 / 用户暂停）。

判定顺序（先用户意图，后环境）
------------------------------
1. 用户手动暂停 ``capture.enabled=0``            → ``paused``
2. 手动游戏模式 ``gate.game_mode=1``              → ``game_mode``
3. 无前台窗口                                     → ``no_foreground``
4. 前台是本进程窗口                               → ``self_window``
5. 桌面 / 任务栏                                   → ``desktop``
6. 已知游戏 / 游戏平台进程（``app/permissions.py``） → ``game_process``
7. 前台窗口全屏（可能是全屏游戏/视频）             → ``fullscreen``
8. 其它（**包括未知普通程序**）                    → ``ok``

与旧版的区别
------------
旧版第 6 步是「进程不在阅读白名单 → 拒绝」，把微信、QQ、公司内部工具之类
正常软件全部挡掉（用户反馈「阅读白名单挡太多正常软件」）。现在改为
**默认可尝试读取**：只要不是游戏、不是全屏、用户没暂停，就在鼠标选词后
尝试一次标准 UIA ``TextPattern`` 读取；读不到就什么都不做。

安全边界（硬约束，未变）
------------------------
* 只用标准 Windows 前台窗口元信息判断：窗口 HWND / 进程 PID / 进程可执行名 /
  窗口标题与类名 / 窗口矩形与显示器矩形（全屏判定）。
* **不注入**任何进程、**不读**其它进程内存、**不加载**模块、**不安装**驱动、
  **不读取**游戏窗口的 UIA/OCR、**不模拟** Ctrl+C、**不读写**剪贴板、
  **不尝试**任何反作弊绕过。
* 已知游戏进程与全屏前台一律**禁止**读取（连一次 UIA 请求都不发）。
* 浏览器里的「窗口化网页游戏」只靠进程名无法识别 —— 这是已知边界，
  由用户手动开启「游戏模式」兜底。本模块**不宣称**任何反作弊兼容性。

硬阻断 vs 软受限
----------------
``allowed=False`` 有两种性质完全不同的情况，副作用必须分开：

* **硬阻断**（用户暂停 / 游戏模式 / 已知游戏进程 / 全屏）→ 拆服务：
  卸鼠标钩子、停 UIA helper、取消置顶；
* **软受限**（本程序自身窗口 / 桌面 / 无前台）→ 只是「这里不该弹浮层」：
  服务照常运行、置顶保持用户偏好。

把两者混为一谈会让「点回自己的主界面」这种高频动作反复杀掉 UIA 进程、
并在切换窗口期间丢失取词（旧实现的缺陷）。
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field

from . import win32util as w32
from .logging_setup import get_logger
from .permissions import GAME_APPS, display_name, exe_name, is_known_game

log = get_logger("gate")

# ------------------------------------------------------------------ 原因码
R_OK = "ok"
R_PAUSED = "paused"
R_GAME_MODE = "game_mode"
R_NO_FOREGROUND = "no_foreground"
R_SELF = "self_window"
R_DESKTOP = "desktop"
R_GAME_PROCESS = "game_process"
R_FULLSCREEN = "fullscreen"

REASON_LABELS: dict[str, str] = {
    R_OK: "允许取词",
    R_PAUSED: "用户已手动暂停取词",
    R_GAME_MODE: "游戏模式已开启（持续禁用鼠标监听与取词）",
    R_NO_FOREGROUND: "没有前台窗口",
    R_SELF: "前台是探索词典自身窗口",
    R_DESKTOP: "前台是桌面/任务栏",
    R_GAME_PROCESS: "前台是已知游戏/游戏平台进程",
    R_FULLSCREEN: "前台窗口处于全屏状态",
}

# 用户主动设置的暂停（不会被「切换前台」自动解除）
USER_BLOCK_REASONS = frozenset({R_PAUSED, R_GAME_MODE})

# 这些原因下**连一次 UIA 读取请求都不发**（游戏避让的硬保证）
HARD_BLOCK_REASONS = frozenset({R_PAUSED, R_GAME_MODE, R_GAME_PROCESS, R_FULLSCREEN})

#: 软受限：当前前台不是「可阅读目标」（本程序窗口 / 桌面 / 无前台），
#: 因此不显示浮层，但**不涉及游戏避让** —— 不得卸载钩子、不得停 UIA、不得取消置顶。
SOFT_BLOCK_REASONS = frozenset({R_NO_FOREGROUND, R_SELF, R_DESKTOP})

#: 控制状态：只有 hard 才允许做「拆服务」的副作用。
STATE_ALLOW = "allow"
STATE_SOFT = "soft"
STATE_HARD = "hard"


@dataclass
class GateDecision:
    """一次门控判定的结果。"""

    allowed: bool
    reason: str
    hwnd: int = 0
    pid: int = 0
    exe: str = ""
    title: str = ""
    app_name: str = ""
    fullscreen: bool = False
    info: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return REASON_LABELS.get(self.reason, self.reason)

    @property
    def target(self) -> str:
        """前台是谁：优先中文名，其次 exe 名，最后窗口标题。"""
        return self.app_name or self.exe or self.title or "当前前台"

    @property
    def blocked_by_user(self) -> bool:
        return self.reason in USER_BLOCK_REASONS

    @property
    def hard_blocked(self) -> bool:
        """游戏避让硬约束：绝不允许发起任何 UIA 读取。"""
        return self.reason in HARD_BLOCK_REASONS

    @property
    def soft_blocked(self) -> bool:
        """软受限：前台不是阅读目标，但不涉及游戏/全屏/用户暂停。

        这类状态只应「不显示浮层 + 收起已有浮条」，
        **不得**卸载鼠标钩子、停止 UIA helper 或取消用户置顶。
        """
        return (not self.allowed) and self.reason in SOFT_BLOCK_REASONS

    @property
    def control_state(self) -> str:
        """给 :class:`GateController` 用的三态：allow / soft / hard。"""
        if self.allowed:
            return STATE_ALLOW
        return STATE_HARD if self.hard_blocked else STATE_SOFT

    def allows_read(self) -> bool:
        """是否允许向 UIA helper 发起一次读取请求。

        这是 ``attempt_capture`` 唯一的放行判据：``self_window`` / ``desktop`` /
        ``no_foreground`` 虽然不允许**读取**（此时前台不是用户正在阅读的窗口），
        但它们既不是游戏也不是用户暂停，读到结果的概率为零，直接跳过。
        """
        return bool(self.allowed)

    def allows_overlay(self) -> bool:
        """是否允许显示自动浮层（操作小条 / 解释结果窗）。

        与 :meth:`allows_read` 的区别：本程序自己的窗口或桌面在前台时仍然
        允许读取（用户可能刚在别的应用里选完词），但**不显示**浮层 ——
        用户正在操作主界面，浮层冒出来只会碍事、并造成「反复开关」的闪烁。
        """
        if not self.allowed:
            return False
        return self.reason not in (R_SELF, R_DESKTOP, R_NO_FOREGROUND)

    def status_text(self) -> str:
        """状态栏用的一行中文。"""
        if self.allowed:
            if self.reason == R_SELF:
                return "取词：就绪（当前前台是本程序窗口）"
            return f"取词：进行中（{self.target}）"
        if self.reason in USER_BLOCK_REASONS:
            return f"取词：已暂停（{self.label}）"
        return f"取词：已暂停 · {self.target} —— {self.label}"

    def detail_text(self) -> str:
        bits = [self.label]
        if self.exe:
            bits.append(f"进程：{self.exe}")
        if self.title:
            bits.append(f"窗口：{self.title[:80]}")
        if self.fullscreen:
            bits.append("窗口占满显示器（全屏）")
        return "；".join(bits)


class AccessGate:
    """前台门控。纯判定，不做任何 UI/Win32 副作用（便于单测）。"""

    def __init__(self, config, *, is_self_window=None):
        self.config = config
        #: 「这个 HWND 是不是我们自己」的判据。默认用 ``w32.is_self_window``；
        #: 宿主（``App``）会换成一个**更宽**的判据：把自家的浮层（阅读面板，
        #: 含它的 Tk 子控件）也算作 self —— 于是 ``R_SELF`` 能区分
        #: 「主词典窗口在前台」与「阅读面板自己在前台」，两者的副作用完全不同。
        self._is_self_window = is_self_window or (lambda hwnd: w32.is_self_window(hwnd))

    def set_self_window_predicate(self, predicate) -> None:
        """替换 self 判定（``predicate(hwnd) -> bool``；``None`` 恢复默认）。"""
        self._is_self_window = predicate or (lambda hwnd: w32.is_self_window(hwnd))

    # ------------------------------------------------------------ 用户开关
    @property
    def game_mode(self) -> bool:
        """手动游戏模式：用户主动开启后持续禁用全局鼠标监听与取词。"""
        return self.config.get_bool("gate.game_mode", False)

    def set_game_mode(self, value: bool) -> None:
        self.config.set_bool("gate.game_mode", bool(value))
        log.info("游戏模式 %s", "开启" if value else "关闭")

    @property
    def capture_enabled(self) -> bool:
        return self.config.get_bool("capture.enabled", True)

    # ------------------------------------------------------------ 判定
    def evaluate(self, info: dict | None = None) -> GateDecision:
        info = info if info is not None else w32.foreground_info()
        hwnd = int(info.get("hwnd") or 0)
        pid = int(info.get("pid") or 0)
        exe = exe_name(info.get("exe") or info.get("app") or "")
        title = info.get("title") or ""
        app_name = display_name(exe)

        def decide(allowed: bool, reason: str, fullscreen: bool = False) -> GateDecision:
            return GateDecision(allowed=allowed, reason=reason, hwnd=hwnd, pid=pid, exe=exe,
                                title=title, app_name=app_name, fullscreen=fullscreen,
                                info=dict(info))

        # 1) 用户手动暂停 / 游戏模式：最高优先级，环境变化不得覆盖
        if not self.capture_enabled:
            return decide(False, R_PAUSED)
        if self.game_mode:
            return decide(False, R_GAME_MODE)

        # 2) 前台窗口本身不可用
        if not hwnd:
            return decide(False, R_NO_FOREGROUND)
        if info.get("is_self") or pid == os.getpid():
            return decide(False, R_SELF)
        try:
            if hwnd and self._is_self_window(hwnd):
                # 自家的浮层（阅读面板及其 Tk 子控件）也算 self：这一点让
                # ``on_soft_block`` 能区分「主词典在前台」与「面板自己在前台」
                return decide(False, R_SELF)
        except Exception:  # pragma: no cover - 判据异常不得打断门控
            log.exception("self 判定失败，按非 self 处理")
        if w32.is_probably_desktop(hwnd):
            return decide(False, R_DESKTOP)

        # 3) 已知游戏 / 游戏平台：硬禁止（游戏避让优先级保持）
        if is_known_game(exe):
            return decide(False, R_GAME_PROCESS)

        # 4) 全屏一律暂停（全屏游戏 / 全屏视频 / 全屏演示都算）
        if w32.is_fullscreen(hwnd):
            return decide(False, R_FULLSCREEN, fullscreen=True)

        # 5) 其它（包括未知普通程序）→ 允许尝试一次 UIA 读取
        return decide(True, R_OK)

    def is_allowed(self, info: dict | None = None) -> bool:
        return self.evaluate(info).allowed

    @property
    def known_game_apps(self) -> frozenset[str]:
        return GAME_APPS


class GateController:
    """把门控判定翻译成「进入 / 退出受限状态」的副作用。

    只在**状态发生翻转**时触发回调（``allow`` / ``soft`` / ``hard`` 三态变化才算翻转），
    避免每次轮询都重复卸载钩子 / 重复隐藏浮层 —— 这正是用户报告的
    「浮层反复开关造成频闪」的根因之一。

    三态语义（重要）：

    * ``hard``（用户暂停 / 游戏模式 / 已知游戏进程 / 全屏）
      → ``on_lockdown``：收起浮层、卸载鼠标钩子、停 UIA、取消置顶；
    * ``soft``（本程序窗口 / 桌面 / 无前台）
      → ``on_soft_block``：**只收浮层**，服务与置顶保持不动；
    * ``allow`` → 若之前是 hard 则 ``on_release`` 恢复服务。

    ``locked`` 只表示 hard：主界面在前台（极高频）不应该被当成受限环境，
    否则每次点回自己的窗口都会杀掉 UIA 进程、丢一次取词。

    回调由调用方提供（UI 线程），本类自身不碰 Tk 也不碰 Win32。
    """

    def __init__(self, gate: AccessGate, on_lockdown=None, on_release=None,
                 on_soft_block=None):
        self.gate = gate
        self._on_lockdown = on_lockdown
        self._on_release = on_release
        self._on_soft_block = on_soft_block
        self._lock = threading.RLock()
        self.locked = False
        self._state: str | None = None
        self.last: GateDecision | None = None
        self.lockdown_count = 0
        self.release_count = 0
        self.soft_block_count = 0

    def _fire(self, cb, decision: GateDecision) -> None:
        if cb is None:
            return
        try:
            cb(decision)
        except Exception:  # pragma: no cover - 回调异常不得打断门控
            log.exception("门控回调失败")

    def update(self, info: dict | None = None) -> GateDecision:
        decision = self.gate.evaluate(info)
        with self._lock:
            state = decision.control_state
            previous = self._state
            self.last = decision
            # 相同状态 → 什么都不做，绝不重复显隐
            if previous == state:
                return decision
            self._state = state
            if state == STATE_HARD:
                self.locked = True
                self.lockdown_count += 1
                log.info("进入受限前台：%s", decision.detail_text())
                self._fire(self._on_lockdown, decision)
            else:
                if self.locked:
                    self.locked = False
                    self.release_count += 1
                    log.info("恢复取词：%s", decision.status_text())
                    self._fire(self._on_release, decision)
                if state == STATE_SOFT:
                    self.soft_block_count += 1
                    self._fire(self._on_soft_block, decision)
        return decision

    def is_locked(self) -> bool:
        with self._lock:
            return self.locked

    def last_reason(self) -> str:
        with self._lock:
            return self.last.reason if self.last else ""

    def last_decision(self) -> GateDecision | None:
        with self._lock:
            return self.last
