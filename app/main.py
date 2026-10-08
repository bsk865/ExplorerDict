"""程序入口：装配所有组件、跑 Tk 主循环。

    python app/main.py              # 正常启动
    python app/main.py --selftest   # 启动自检后自动退出（自动化检查用）
"""
from __future__ import annotations

import argparse
import copy
import os
import queue
import sys
import threading
import time
import traceback
from pathlib import Path

if __package__ in (None, ""):  # 支持 `python app/main.py` 直接运行
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "app"

import tkinter as tk
from tkinter import messagebox

from . import paths, win32util as w32
from .api_client import ApiError
from .capture_service import (CaptureService, ForegroundWatcher, normalize_context,
                              resolve_page_scope)
from .chat_service import ChatService
from .config import Config
from .db import Database
from .explain_service import ExplainService
from .gate import R_SELF, AccessGate, GateController
from .hotkeys import HK_GAME_MODE, HK_GRAB, HK_PAUSE, HK_QUIT, HK_RECALL, HOTKEYS, HotkeyManager
from .logging_setup import get_logger, setup_logging
from .map_service import MapService
from .models import CapturedSelection, SourceInfo
from .mouse_hook import MouseHook
from .single_instance import (acquire_process_guard, process_guard,
                              release_process_guard)
from .tray import TrayIcon
from .uia_bridge import UiaBridge
from .ui import app_menu, theme
from .ui.concept_map import ConceptMapWindow
from .ui.main_window import MainWindow
from .ui.manual_dialog import ManualEntryDialog
from .ui.reading_panel import ReadingPanel
from .ui.settings_dialog import SettingsDialog
from .ui.setup_wizard import SetupWizard, should_show as wizard_should_show

log = get_logger("main")

#: 门控轮询间隔。只做「前台元信息」读取，不读 UIA、不刷新 UI。
GATE_POLL_SECONDS = 0.5

#: 「另一个实例请求呼出主界面」的轮询间隔（``root.after``，非阻塞）。
#: 每次只做一次零超时的 ``WaitForSingleObject``，没有请求时开销可忽略。
ACTIVATION_POLL_MS = 250

#: 鼠标钩子安装失败后的重试间隔。门控轮询本身只有 0.5s，**绝不**每 60ms
#: 重启一次钩子；失败后至少等 2 秒再试。
HOOK_RETRY_SECONDS = 2.0


def build_overlays(root: tk.Misc, app) -> tuple[ReadingPanel, ReadingPanel]:
    """构造阅读面板（**同一个窗口**同时承担选区条与结果窗两个角色）。

    用户只看到一个面板：小方块（折叠）↔ 紧凑浮窗（展开）。返回两个相同引用是
    为了让 ``App`` 里既有的 ``self.selection_bar`` / ``self.explain_window``
    两套调用点继续可用（token / 门控 / 幂等语义一行不用改）。

    独立成一个函数是为了让**纯模拟回归测试**能替换掉窗口层：测试用假窗口
    （不创建任何 Tk 窗口）验证 App 的 token / 手势 / 门控逻辑，而产品运行时
    这里始终返回真实面板。
    """
    panel = ReadingPanel(root, app)
    return panel, panel


class App:
    """装配所有组件、驱动选区状态机。

    最终交互（用户明确要求，勿再拆成两个按钮）
    ------------------------------------------
    * **鼠标划选**：只产生一份**内存快照**并在选区旁弹出置顶小浮条；
      不写数据库、不建批次、不发任何网络请求；
    * **浮条上唯一的按钮「解释并记录」**：幂等落库（关键词 + 来源 / 上下文）
      → 立刻异步解释 → 结果写回**同一个 entry_id**；
    * **点别处**：只**作废**当前待处理选区（旧快照不再属于用户接下来要看的东西），
      什么都不保存 —— 窗口的显隐只由用户与硬阻断决定，软事件不折叠、不收起；
    * **没有 API Key**：也先落库，解释窗显示「已记录（待解释）」并提供
      **真实可见的「打开设置」/「重新解释」入口**；重试沿用同一 entry_id，
      不会重复记词、不会再加词频；
    * **解释窗**只显示「已记录状态 + 释义 + 重试 / 设置 / 关闭」，
      自身**没有**任何记录动作。

    token 域（两套计数器，绝不互相比较）
    ------------------------------------
    * ``_selection_token``：**选区序号**，每次新选区 +1，只用于「哪一次选词」；
    * ``_explain_tokens[entry_id]``：**解释服务**返回的**请求 token**，
      只用于「这一次请求的结果该不该更新结果窗」；
    * 结果窗的更新条件是 **entry_id + 请求 token 同时匹配**
      （``ExplainWindow.show_result``）；卡片 / 详情仍然只按 ``entry_id`` 刷新。
    * 显示门控只有一处：``App.overlay_display_allowed`` —— 所有浮层入口
      （选区条 / 结果窗）最终都经过 ``FloatingWindow.show_at``，硬阻断
      （游戏 / 全屏 / 暂停）下**只隐藏、零 deiconify**。
    """

    def __init__(self, root: tk.Tk, db: Database, config: Config):
        self.root = root
        self.db = db
        self.config = config
        self._closing = False
        #: 已有实例的「呼出」命名事件接收器（没有 / 创建失败时为 None：
        #: 只是不能从外部呼出，不影响任何既有功能）
        self._activation = None
        #: ``root.after`` 的轮询句柄（退出时取消）
        self._activation_after_id = None
        #: 硬阻断期间暂存的**一个**打开主界面请求（``--open-main`` 与事件共用）：
        #: 游戏/全屏/暂停下一次 deiconify 都不做，等门控解除后兑现一次
        self._pending_open_main = False
        #: 暂存请求的**来源**：``argv``（首次双击）不显示「已在运行」提示，
        #: ``activation``（重复启动呼出）才提示一次。
        self._pending_open_main_source = ""
        self._ui_q: queue.Queue = queue.Queue()
        self._gesture_q: queue.Queue = queue.Queue(maxsize=64)
        self._gesture_stop = threading.Event()
        self._gate_next_check = 0.0
        self._last_click: tuple[int, int, float] | None = None
        #: 服务生命周期标记：构造期（``__init__`` 里的第一次门控评估）绝不
        #: 安装钩子 / 拉起 UIA；``_start_services`` 开始才置位。
        self._services_started = False
        #: 下一次允许重试安装鼠标钩子的时刻（失败后 2s 内不再试）
        self._hook_retry_at = 0.0
        #: start / resume 共用的一把锁：UIA 启动绝不并发交错
        self._uia_start_lock = threading.Lock()
        #: 常驻托盘图标（没有 pystray/Pillow 时为 None，其它功能不受影响）
        self._tray = None
        #: 托盘构造入口（测试可替换成假 factory）
        self.tray_factory = None

        # ---- 选区状态机（全部在 UI 线程里读写）----
        #: 当前选区快照（**深拷贝**，未落库）
        self._current_selection = None
        #: 当前选区是在**哪一页**（CaptureService 的页面世代）拿到的：
        #: 迟到的 ``foreground_changed`` 事件据此判断该不该作废它
        self._selection_page_epoch: int | None = None
        #: 选区序号：只标识「哪一次选词」，与解释服务的 token 完全独立
        self._selection_token = 0
        #: 浮条显示时刻（monotonic）：早于它的鼠标按下不得用来收起浮条
        self._bar_shown_at = 0.0
        #: 本次选区已经落库的结果：``{token, signature, entry_id, created}``
        #: 同一个选区被连续点击时只记一次，不重复写库、不再加词频
        self._recorded_selection: dict | None = None
        #: entry_id → 解释服务 token（展示 / 诊断用，不参与归属判定）
        self._explain_tokens: dict[int, int] = {}
        #: 我们自己的浮层顶层 HWND（钩子线程按下时据此判断「点在浮层上」）
        self._overlay_hwnds: tuple[int, ...] = ()
        #: 主窗口当前的 ``-topmost`` 状态（始终 ``False``；只在第一次下发，
        #: 避免门控轮询重复 attributes / lift）
        self._main_topmost: bool | None = None
        #: 上一次同步过小方块的门控状态（只在 allow 翻转时兜底显示一次）
        self._last_overlay_state: str | None = None
        #: 启动浮窗（``start_floating_ui``）是否已经负责过显隐：在此之前
        #: 门控兜底不再抢着显示小方块（否则首次授权会先闪一下小方块再展开）
        self._float_ui_started = True

        self.gate = AccessGate(config)
        #: 门控的 ``self_window`` 判据要能认出「我们自己的**浮层**」：主词典窗口
        #: 在前台和阅读面板自己在前台，副作用完全不同（前者折叠面板、后者必须
        #: 保持展开与输入）。Tk 的输入框还是子窗口，所以要取顶层祖先再比。
        self.gate.set_self_window_predicate(self._foreground_is_our_overlay)
        self.gate_controller = GateController(
            self.gate,
            on_lockdown=self._enter_gate_lockdown,
            on_release=self._release_gate_lockdown,
            on_soft_block=self._soft_block_overlays,
        )
        self.uia = UiaBridge()
        self.capture_service = CaptureService(
            db, config, self.uia, on_event=lambda e, p: self._ui_q.put((e, p)), gate=self.gate,
            on_foreground_change=lambda src, kind, meta=None: self._ui_q.put(
                ("foreground_changed",
                 {"source": src, "kind": kind,
                  "page_epoch": (meta or {}).get("page_epoch"),
                  "generation": (meta or {}).get("generation")})
            ),
        )
        #: 解释结果的**唯一 UI 回调**：旧 ``on_result`` 的 4 元组**追加请求 token**
        #: （``(entry_id, status, result, error, token)``）。旧的 4 元组消费者
        #: 仍然按位置解包可用；卡片 / 详情刷新在本程序里**只按 entry_id**，
        #: 解释结果窗则要求 ``entry_id`` 与请求 token **同时匹配**
        #: （见 _handle_entry_explain_result）。
        self.explain_service = ExplainService(db, config)
        self.explain_service.set_entry_result_sink(
            lambda *a: self._ui_q.put(("explain_result", a)))
        #: 追问（对话）服务：用户点「发送」才会请求；结果按 (request_id, entry_id) 归属
        self.chat_service = ChatService(db, config)
        self.chat_service.set_result_sink(
            lambda *a: self._ui_q.put(("chat_result", a)))
        #: 参考关系图服务：用户打开导图 / 切主题 / 点生成才会请求；结果按
        #: (token, topic_id, fingerprint) 归属，UI 只接受与当前主题 + 指纹都匹配的结果
        self.map_service = MapService(db, config)
        self.map_service.set_result_sink(
            lambda *a: self._ui_q.put(("map_result", a)))
        self.mouse_hook = MouseHook(self._on_gesture, on_press=self._on_mouse_press)
        self.hotkeys = HotkeyManager(lambda hid: self._ui_q.put(("hotkey", hid)))
        self.watcher = ForegroundWatcher(self.capture_service)
        self._gesture_worker: threading.Thread | None = None
        self._uia_started = False
        self._uia_wanted = True
        self._recall_hotkey_ok = False
        self._settings_win: SettingsDialog | None = None
        self._wizard_win = None  # 首次配置向导（见 maybe_show_setup_wizard）
        self._map_win: ConceptMapWindow | None = None

        # ---- 关键顺序：先评估环境，再决定是否置顶，最后才创建窗口 ----
        # 旧实现在 GateController 还是默认 unlocked 时就建主窗口并置顶，
        # 于是「用户在游戏里双击启动」会让启动窗口盖在游戏上。
        # 这里在任何窗口/置顶操作之前先做一次**实时**门控判定。
        self._evaluate_environment()
        self.main = MainWindow(root, self)
        #: ``main_window`` 是个**只读别名**，指的还是 ``self.main``。
        #: 加它是因为导图窗与勾选筛选那边两处都写成 ``getattr(app, "main_window")``
        #: —— 名字不存在 + ``getattr`` 的兜底 = 静默失效（不报错，功能就是不生效）。
        #: 属性名对不上的写法以后再落到这个别名上也不会再坑一次。
        self.main_window = self.main
        self.selection_bar, self.explain_window = build_overlays(root, self)
        #: 同一个面板的两个角色引用（用户只看到一个窗口）
        self.reading_panel = self.selection_bar
        self._refresh_overlay_hwnds()
        self.apply_topmost()
        root.protocol("WM_DELETE_WINDOW", self.on_main_close)

    def _evaluate_environment(self, *, force: bool = False):
        """建窗/置顶之前先评估一次环境（游戏/全屏/暂停一律不置顶不弹层）。"""
        return self._apply_gate(force=force)

    # ------------------------------------------------------------- 启动
    def start(self) -> None:
        self.root.after(60, self._pump)
        self.root.after(200, self._start_services)
        #: 托盘随应用启动常驻：关闭主窗口后仍然能在托盘里打开词典 / 暂停 / 退出。
        #: 依赖缺失（没装 pystray/Pillow）只是没有托盘，不影响任何既有功能。
        self._start_tray()

    def _start_services(self) -> None:
        self._services_started = True
        self._float_ui_started = False
        self._start_uia_async("uia-start")

        self.hotkeys.start()
        self._verify_hotkeys()
        decision = self._apply_gate(force=True)
        #: 只有**硬阻断**才不装钩子：``self`` / 桌面这类软受限前台也照常安装监听
        #: （取词门控不变：真正读取仍然要 ``decision.allowed``）。旧实现用
        #: ``decision.allowed`` 判断，双击 exe（``--open-main`` 先把自己显示出来
        #: → 前台是自身窗口）时钩子永远装不上，用户回到浏览器也没有任何监听。
        if getattr(decision, "hard_blocked", False):
            log.info("启动时前台禁止取词，暂不安装鼠标钩子：%s", decision.label)
        else:
            self._ensure_mouse_hook()
        self._apply_gate_overlay_policy()
        self.watcher.start()
        self.capture_service.note_foreground()

        self._gesture_worker = threading.Thread(
            target=self._gesture_loop, name="capture-worker", daemon=True
        )
        self._gesture_worker.start()

        # 启动**只显示浮窗**（阅读时后台运行的形态）：不打开主界面、不抢焦点。
        # 授权未知时这里展开的是内联授权卡，其余情况是小方块；硬阻断下
        # show_at 会拒绝映射，一个窗口都不会出现。
        self._refresh_overlay_hwnds()
        try:
            self.start_floating_ui()
        finally:
            self._float_ui_started = True
        self.main.update_status()

        failed = self.hotkeys.failed
        if failed:
            names = "、".join(label for _, label in failed)
            self.set_status(f"以下热键被占用，注册失败：{names}")

    def _ensure_mouse_hook(self) -> bool:
        """确保全局鼠标钩子在运行（非硬阻断前台下）。

        取词**门控不变**：钩子只负责「按下 / 抬起」事件，真正读取仍然要
        ``GateDecision.allowed``。软受限前台（自身窗口 / 桌面）也允许安装，
        否则双击 exe（``--open-main`` 把主界面显示到最前）时永远装不上监听。

        失败重试至少间隔 :data:`HOOK_RETRY_SECONDS`：门控轮询会反复调用本方法，
        绝不每 60ms 重启一次钩子。
        """
        if not self._services_started or self._closing:
            return False
        if self.gate_controller.is_locked():
            return False
        hook = getattr(self, "mouse_hook", None)
        if hook is None:
            return False
        if hook.running:
            return True
        now = time.monotonic()
        if now < self._hook_retry_at:
            return False
        self._hook_retry_at = now + HOOK_RETRY_SECONDS
        try:
            ok = bool(hook.start())
        except Exception:  # pragma: no cover - 安装失败不得打断门控轮询
            log.exception("启动鼠标钩子失败")
            ok = False
        if not ok:
            log.error("鼠标钩子启动失败")
            self.set_status("鼠标钩子启动失败，请尝试以管理员身份运行")
            return False
        return True


    def _verify_hotkeys(self) -> None:
        """启动检查：**必须**确认呼出键注册成功（不能因为其它热键成功就算通过）。"""
        self._recall_hotkey_ok = self.hotkeys.recall_registered
        if self._recall_hotkey_ok:
            log.info("呼出热键注册成功：Ctrl+Alt+Shift+D")
            return
        missing = self.hotkeys.missing_required()
        log.error("关键热键注册失败（呼出键）：%s", missing)
        for hid, label in self.hotkeys.failed:
            log.error("热键注册失败: %s", label)

    def recall_hotkey_ok(self) -> bool:
        return bool(self._recall_hotkey_ok)

    def hotkey_warning(self) -> str:
        """呼出键失败时给用户看的一句话（返回空串表示正常）。

        技术细节（哪个键被谁占用、如何排查）**只写日志**，界面上最多一句话 +
        「重试注册」按钮 —— 用户要的是「还能不能呼出」，不是注册表报告。
        文案只说**界面上真实存在**的入口（提醒条上的「重试注册」按钮），
        不再提已经被删掉的浮窗菜单。
        """
        if self._recall_hotkey_ok:
            return ""
        return "呼出热键被占用，请点「重试注册」"

    def retry_hotkeys(self) -> None:
        ok = self.hotkeys.restart()
        self._verify_hotkeys()
        self.main.update_status()
        if ok:
            self.set_status("呼出热键已注册成功")
        else:
            self.set_status("呼出热键仍被占用，请关闭占用程序后重试")

    # ------------------------------------------------------------- 门控
    def gate_decision(self):
        """**实时**门控判定（不看轮询缓存）。

        显示入口（``overlay_allowed`` / ``SelectionBar.show_selection``）依赖它：
        缓存可能停留在上一次轮询时的判定，而此刻前台可能已经切到游戏；
        只有实时判定才能作为否决项。
        ``GateController`` 仍然负责「状态翻转时的副作用」
        （收浮层 / 卸钩子 / 停 UIA / 取消置顶）。
        """
        return self.gate.evaluate()

    def _apply_gate(self, *, force: bool = False, info: dict | None = None):
        """轮询门控；状态翻转时执行进入/退出受限环境的副作用。

        ``GateController`` 只在上一次判定与本次不同（allowed/reason 变化）时回调，
        所以同一条「允许的普通窗口」反复轮询**不会**触发任何显隐 ——
        这是修掉「浮层反复开关造成频闪」的关键。

        轮询结束后只在**真正翻转进 allow** 时兜底一次小方块显示
        （``_sync_dock_after_gate``）：不重建界面、不重建标签、不强行 lift。

        另外在**非硬阻断**且服务已启动时补装鼠标钩子（``_ensure_mouse_hook``）：
        钩子可能因为「启动时正在游戏里」「安装失败」而缺失，回到普通阅读前台
        必须能自己补回来；失败重试有 2 秒下限，绝不每 60ms 重启。
        """
        decision = self.gate_controller.update(info)
        if self._services_started and not getattr(decision, "hard_blocked", False):
            self._ensure_mouse_hook()
        self._sync_dock_after_gate(decision)
        return decision

    def _sync_dock_after_gate(self, decision) -> bool:
        """门控翻转进「允许」时做一次**幂等**的窗口收尾（绝不改变用户选择的状态）。

        * 硬阻断（游戏 / 全屏 / 暂停）与软受限（self / 桌面 / 无前台）→ 什么都不做
          （硬阻断的收起由 :meth:`_enter_gate_lockdown` 负责，软受限不该动窗口）；
        * 翻转进「允许」→ 交给 :meth:`ReadingPanel.settle_after_gate`：

          - 面板**已经展开** → 只做一次实时硬阻断复核，**原样保留**；
            旧实现在这里无条件 ``_restore_dock()``，会把用户正开着的面板
            折叠成 44x44（「从设置 / 桌面切回阅读窗口，面板自己缩了」）；
          - 面板隐藏且用户没关过 → 只补回**小方块**（离开游戏时 dock-only，
            绝不自动展开）；
          - 用户关闭过 → 不复活。

        因此「普通 → 设置 / self → 普通」「普通 → 桌面 / 无前台 → 普通」这类
        轮询循环里窗口状态一步都不动（零 deiconify / withdraw / geometry）。
        """
        state = str(getattr(decision, "control_state", "") or "")
        allowed = bool(getattr(decision, "allowed", False))
        previous = self._last_overlay_state
        self._last_overlay_state = state
        if not allowed or state == previous:
            return False
        if not getattr(self, "_float_ui_started", True):
            # 启动浮窗由 start_floating_ui 统一负责（授权未知时要展开授权卡）
            return False
        panel = getattr(self, "reading_panel", None)
        if panel is None or not self._ui_ready():
            return False
        settle = getattr(panel, "settle_after_gate", None)
        if not callable(settle):
            return self._restore_dock()      # 旧窗口层：保持历史行为
        try:
            shown = bool(settle())
        except tk.TclError:  # pragma: no cover - 窗口已销毁
            return False
        if shown:
            self._refresh_overlay_hwnds()
        return shown

    def _restore_dock(self, *, explicit: bool = False) -> bool:
        """幂等显示小方块（用户主动关闭过的不自动找回）。"""
        panel = getattr(self, "reading_panel", None)
        if panel is None or not self._ui_ready():
            return False
        restore = getattr(panel, "restore_dock", None)
        if not callable(restore):
            return False
        try:
            shown = bool(restore(explicit=explicit))
        except tk.TclError:  # pragma: no cover
            return False
        if shown:
            self._refresh_overlay_hwnds()
        return shown

    def overlay_allowed(self) -> bool:
        """**新浮层**是否允许出现（等价于 ``overlay_display_allowed(already_visible=False)``）。"""
        return self.overlay_display_allowed(already_visible=False)

    def overlay_display_allowed(self, window=None, *, already_visible: bool = False,
                                explicit: bool = False) -> bool:
        """**所有浮层显示的唯一实时门控**（``FloatingWindow.show_at`` 每次都会问它）。

        判定用的是 ``self.gate.evaluate()``（此刻的前台），不是轮询缓存 ——
        迟到的结果 / 延迟动作在游戏前台上**一次 ``deiconify`` 都做不出来**。

        三档策略：

        * **硬阻断**（用户暂停 / 游戏模式 / 已知游戏进程 / 全屏）→ 一律拒绝：
          不新映射、不移动、已经显示的窗口立刻收起（游戏避让硬约束）；
        * **普通阅读前台** → 放行；
        * **软受限**（本程序自己的窗口 / 桌面 / 无前台）→ 选区条一律不显示；
          解释结果窗是**用户自己打开**的窗口，允许继续显示与更新
          （``already_visible``），也允许用户显式动作把它找回来（``explicit``，
          见 :meth:`restore_pending_explain_window`）—— 否则「点打开设置去配 Key，
          配完回来发现『重新解释』入口不见了」。
        """
        decision = self.gate.evaluate()
        if decision.hard_blocked:
            return False
        if decision.allows_overlay():
            return True
        if window is not getattr(self, "explain_window", None):
            return False
        return bool(already_visible or explicit)

    def _ui_ready(self) -> bool:
        """窗口层是否已经构造完成。

        ``__init__`` 里在**创建任何窗口之前**就要评估一次环境（否则游戏里启动
        会让启动窗口置顶），而那时门控回调可能被触发 —— 回调必须先确认
        ``main`` / ``selection_bar`` 已经存在，不能摸到半个 App。
        """
        return getattr(self, "main", None) is not None and \
            getattr(self, "selection_bar", None) is not None

    def _refresh_overlay_hwnds(self) -> tuple[int, ...]:
        """缓存两个浮层的顶层 HWND，供**钩子线程**在鼠标按下瞬间判定落点。

        必须在 UI 线程调用（``winfo_id`` 不是线程安全的）。返回值是不可变
        tuple，钩子线程读到的永远是一份完整快照，不会看到半个集合。
        """
        hwnds = []
        for win in (getattr(self, "selection_bar", None),
                    getattr(self, "explain_window", None)):
            if win is None:
                continue
            try:
                h = int(win.hwnd() or 0)
            except Exception:  # pragma: no cover - 窗口已销毁
                h = 0
            if h:
                hwnds.append(h)
        self._overlay_hwnds = tuple(hwnds)
        return self._overlay_hwnds

    def overlay_hwnds(self) -> tuple[int, ...]:
        """钩子线程读取的浮层 HWND 快照。"""
        return self._overlay_hwnds

    def _hide_overlays(self) -> None:
        """完全收起浮层（硬阻断 / 退出）。幂等：已经隐藏时不做任何 Win32 调用。

        **不作废在途解释**：用户显式点过「解释并记录」之后，即便浮层因为
        门控切换被收起，已经发出的保存与解释仍必须写回数据库；
        这里只负责「不再显示」，不取消任何用户已经请求的工作。
        """
        if not self._ui_ready():
            return
        changed = False
        try:
            changed = bool(self.selection_bar.hide()) or changed
        except tk.TclError:  # pragma: no cover
            pass
        try:
            changed = bool(self.explain_window.hide()) or changed
        except tk.TclError:  # pragma: no cover
            pass
        if changed:
            self.selection_bar.selection = None
            self._current_selection = None
            self._refresh_overlay_hwnds()

    def _collapse_panel(self, *, drop_selection: bool = False, reason: str = "") -> bool:
        """把展开的面板**折叠成小方块**（保留待处理词，除非 ``drop_selection``）。

        .. note:: 只应由**用户显式动作**调用
           自定义软事件（点外部 / 换窗口 / 本程序窗口在前台）**一律不再**调用它：
           面板的显隐完全由用户与硬阻断决定。保留这个方法是为了兼容旧调用方
           与显式折叠入口。
        """
        changed = False
        targets: list = []
        for win in (getattr(self, "reading_panel", None),
                    getattr(self, "selection_bar", None),
                    getattr(self, "explain_window", None)):
            if win is not None and not any(win is t for t in targets):
                targets.append(win)
        for win in targets:
            collapse = getattr(win, "collapse_to_dock", None)
            try:
                if callable(collapse):
                    changed = bool(collapse(reason=reason,
                                            drop_selection=drop_selection)) or changed
                elif hasattr(win, "hide"):
                    changed = bool(win.hide()) or changed
            except tk.TclError:  # pragma: no cover
                pass
        if drop_selection:
            self._current_selection = None
            for win in targets:
                if hasattr(win, "selection"):
                    try:
                        win.selection = None
                    except Exception:  # pragma: no cover
                        pass
        if changed:
            self._refresh_overlay_hwnds()
        return changed

    def _hide_overlays_for_foreground_change(self, src, kind: str, *,
                                             page_key: str | None = None,
                                             page_epoch: int | None = None) -> None:
        """前台换窗口 / 同一窗口换标签页：**作废旧选区，但不折叠面板**。

        最新的用户契约把「窗口可见性」和「内容归属」彻底分开：

        * **作废**：旧选区、旧解释绑定、旧 token 一律作废 —— 迟到的结果不得覆盖
          新页面的内容（``_selection_stale_reason`` 也会再兜一层）；
        * **不动窗口**：面板是展开的就继续展开，是小方块就保持小方块。
          上面这些软事件（换窗口 / 换标签页）**没有**权力替用户收起面板；
          只有用户自己（点折叠 / 点关闭）和硬阻断（游戏 / 全屏 / 暂停）可以。

        这里也**没有**任何「签名冷却时间」：用户在新文档里重新划选同一个词
        必须立刻展开面板，不能被上一份选区的冷却吞掉。

        **迟到事件的世代回比（本轮修复）**：这条事件是排队送到 UI 线程的，
        而用户完全可能在它被消费之前就在新页面上划好了第一个词。旧实现无条件
        作废当前选区，于是「新页面的首选区」被这条迟到事件清掉（用户看到的就是
        「切页后第一个词点了没反应 / 面板又空了」）。现在按**页面世代**判定：

        * 当前选区所属世代 ``_selection_page_epoch`` **不早于**事件带的
          ``page_epoch`` → 它属于新页面（含**同代乱序**：新选区先到、事件后到），
          **保留**；
        * 选区比事件更老（``_selection_page_epoch < page_epoch``）→ 旧页面的
          选区，照旧作废；
        * ``page_key`` 兜底（更老的事件 + 世代指纹缺失时）：事件页面 == 当前
          页面 → 保留。

        注意：``generation`` 的递增**始终**在 ``CaptureService.note_foreground``
        里同步完成（前台一变，旧结果当场失效）；这里只做 UI 侧的归属判定，
        **绝不**把世代递增挪到 UI 消费时执行。
        """
        epoch = None if page_epoch is None else int(page_epoch)
        current_epoch = self.capture_service.current_page_epoch()
        selection_epoch = getattr(self, "_selection_page_epoch", None)
        keep = False
        if epoch is not None and selection_epoch is not None \
                and int(selection_epoch) >= int(epoch):
            # 选区是在**这一代之后**（或同一代）拿到的：属于新页面，绝不作废。
            # 判据只看「选区 vs 事件」两个世代号，不看 current_epoch —— 旧实现
            # 多要求 epoch < current_epoch，同代乱序时会把新选区误清掉。
            keep = True
        if not keep and epoch is not None and page_key is not None \
                and int(epoch) < int(current_epoch):
            # 世代指纹缺失时的兜底：事件说的页面**就是**当前页面 → 保留
            try:
                source = self.capture_service.last_source()
            except Exception:  # pragma: no cover - 来源读取失败按「不保留」处理
                source = None
            now_key = getattr(source, "doc_key", "") if source is not None else ""
            if now_key and str(now_key) == str(page_key):
                keep = True
        if keep:
            # 迟到事件：选区是新页面的，绝不作废；主界面视图仍然跟到新页面，
            # 浮窗保持正在显示的内容（换页不改内容，刷新只发生在划词记录时）。
            log.info("前台变化事件（page_epoch=%s）不清理不早于它的选区（selection_epoch=%s）",
                     epoch, selection_epoch)
            self._follow_page_views()
            return
        self._drop_current_selection()
        self._selection_page_epoch = None
        self._selection_token += 1
        self._recorded_selection = None
        self._follow_page_views()
        reason = "前台窗口已切换" if kind == "window" else "标签页/文档已切换"
        title = (getattr(src, "title", "") or getattr(src, "app", "") or "")[:60]
        self.set_status(f"{reason}，已作废上一个选区（{title}）")

    def _follow_page_views(self) -> None:
        """让**视图**跟上当前阅读页面（零写入、零请求、不动任何窗口矩形）。

        * **主界面**：只清掉手工浏览筛选（纯内存）。它收起时按老规矩延迟重建，
          下次打开时列表已经是新页面的词；
        * **浮窗**：``reading_panel`` 正是屏幕上那个用户看得见的窗口 —— 换页
          **不改它正在显示的内容**（用户翻页不等于「要看新页面的词表」：刷新的
          真实时机是用户**划选并记录**，即 ``_record_snapshot_once`` →
          ``_refresh_panel_terms``，以及显示某词详情时的 ``show_recorded`` ——
          页面身份在抬手那一刻就已采到，所以那次刷新取到的就是划词时所在的
          页面），这里只交还手工浏览范围、收起主题选择，显隐 / 尺寸 / 位置
          一个像素都不动；
        * 页面身份只走 ``CaptureService.current_page_scope`` / ``page_doc_key``
          （只读查找，不建主题、不写 settings）；新页面还没有保存过词时是**空词表**，
          真正的落库仍然只发生在用户点「解释并记录」之后的 ``resolve_batch``，
          因此换页绝不会重复建主题。
        """
        follow = getattr(self.main, "follow_current_page", None)
        if callable(follow):
            try:
                follow()
            except Exception:  # pragma: no cover - 主界面异常不得打断取词
                log.exception("主界面跟随页面失败")
        panel = getattr(self, "reading_panel", None)
        refresher = getattr(panel, "follow_page_change", None)
        if callable(refresher):
            try:
                refresher()
            except tk.TclError:  # pragma: no cover - 浮窗已销毁
                log.exception("浮窗跟随页面失败")

    def _handle_page_resume(self, src) -> None:
        """用户离开又回到**同一个阅读页**（``resume`` 事件）：恢复自动跟随。

        触发场景：用户在浏览器里划词 → 打开主界面点选某个主题浏览（
        ``browse_scope`` 非 None）→ 切回浏览器**同一页**。旧实现里
        ``ForegroundWatcher`` 只在 HWND / 标题变化时才调 ``note_foreground``，
        回到同一页什么都不发生，列表就一直停在手工浏览的主题上。

        这里只有一条规则：**手工浏览非 None 时**才让视图交还「跟随当前页」。
        绝不作废本页选区、绝不动任何窗口（显隐 / 尺寸 / 位置）、绝不递增世代
        —— 前台从头到尾都是同一页。
        """
        if self.browse_scope() is None:
            return
        log.info("回到同一阅读页：交还手工浏览，恢复跟随当前页")
        self._follow_page_views()

    # ------------------------------------------------------------- 托盘
    def _start_tray(self) -> bool:
        """创建一个常驻托盘图标（幂等）。

        托盘动作只把事件投进 ``_ui_q``（见 ``app/tray.py``），Tk 永远只在 UI
        线程里被碰。依赖缺失 / 环境不支持时返回 False —— 没有托盘不影响
        取词、热键与主界面，只有「打开词典 / 暂停 / 退出」少一个入口。
        """
        if self._tray is not None:
            return True
        factory = self.tray_factory or TrayIcon
        try:
            tray = factory(self)
            ok = bool(tray.start()) if tray is not None else False
        except Exception:  # pragma: no cover - 托盘失败绝不阻止启动
            log.exception("托盘创建失败（不影响其它功能）")
            return False
        self._tray = tray
        return ok

    def close_tray(self) -> bool:
        """删除托盘图标（幂等；退出路径调用）。"""
        tray, self._tray = self._tray, None
        if tray is None:
            return False
        try:
            tray.stop()
        except Exception:  # pragma: no cover - 清理失败不该影响退出
            log.exception("关闭托盘失败")
        return True

    def _notify_already_running(self) -> None:
        """重复启动（已有实例被呼出）时的一次性提示：状态栏 + 托盘气泡。

        重复 exe 只**恢复浮窗**（不打开主界面），所以文案说的是浮窗。
        ``--open-main``（显式开发参数）**不走这里**：那不是「已在运行」场景。
        硬阻断下这条提示由 :meth:`request_open_main` 暂存，等门控解除、
        浮窗真的显示出来时才出现。
        """
        self.set_status("探索词典已在运行，已显示浮窗")
        tray = getattr(self, "_tray", None)
        notify = getattr(tray, "notify", None)
        if callable(notify):
            try:
                notify("探索词典已在运行，已显示浮窗")
            except Exception:  # pragma: no cover - 通知失败不影响开窗
                log.debug("托盘通知失败", exc_info=True)

    def _foreground_is_our_overlay(self, hwnd: int | None = None) -> bool:
        """**实时**判定：这个窗口（默认当前前台）是不是我们自己的浮层。

        门控的 ``R_SELF`` 只看 ``is_self`` / PID，分不出「主词典窗口在前台」和
        「阅读面板自己在前台」。这个区分很关键：用户点面板里的追问输入框时，
        前台正好是面板本身（Tk 输入框甚至是它的子窗口）；如果一律按 ``R_SELF``
        折叠，下一次门控轮询就会把面板收成 44x44，输入框随焦点一起消失。

        与浮层 HWND 快照比较（同一份 ``_overlay_hwnds``，鼠标钩子线程也在读），
        并用 ``GetAncestor(GA_ROOT)`` 把子窗口归到顶层窗口。
        """
        overlays = tuple(h for h in (self._overlay_hwnds or ()) if h)
        if not overlays:
            return False
        if hwnd is None:
            try:
                hwnd = int(w32.get_foreground_window() or 0)
            except OSError:  # pragma: no cover
                return False
        hwnd = int(hwnd or 0)
        if not hwnd:
            return False
        candidates = {hwnd}
        try:
            candidates.add(int(w32.window_root(hwnd) or hwnd))
        except OSError:  # pragma: no cover
            pass
        return any(int(h) in candidates for h in overlays)

    def _soft_block_overlays(self, decision) -> None:
        """软受限前台（本程序窗口 / 桌面 / 无前台）：**什么都不折叠**。

        最新契约（用户硬要求）：面板的显示与收起**只由用户控制**。软受限只是一条
        「不要在这里**新映射**浮层」的规则（由 ``overlay_display_allowed`` 实时
        执行），绝不是「把已经展开的面板收起来」的理由：

        * 前台是本程序窗口（主词典 / 设置 / 面板自己）→ 保持现状；
        * 桌面 / 无前台 → 保持现状。

        这些前台下仍然**不读本应用文本**（``CaptureService`` 的自身窗口过滤与
        门控照旧生效），只是不再动窗口。真正需要收起的是硬阻断
        （游戏 / 全屏 / 手动游戏模式 / 暂停），那条路径走
        :meth:`_enter_gate_lockdown`。
        """
        return None

    def _enter_gate_lockdown(self, decision) -> None:
        """进入游戏/全屏/暂停前台：收起浮层、卸载鼠标钩子、停止 UIA、取消置顶。

        构造期（``_services_started`` 尚未置位）只记录「UIA 不该被拉起」，
        绝不触碰还不存在的服务 —— 那正是旧实现「构造期就把钩子/UIA 停掉」的坑。
        """
        self.capture_service.bump_generation()
        self._drain_gestures()
        self._hide_overlays()
        if self._services_started:
            try:
                self.mouse_hook.stop()
            except Exception:  # pragma: no cover
                log.exception("停止鼠标钩子失败")
        # 先禁止一切新请求，再异步停掉 helper 进程（绝不向目标窗口发读取请求）
        self._uia_wanted = False
        self.uia.set_enabled(False)
        if self._services_started:
            threading.Thread(target=self._stop_uia_bg, name="uia-stop", daemon=True).start()
        self._apply_gate_overlay_policy()
        if self._ui_ready():
            self.main.on_gate_changed(decision)
            self.main.update_status()

    def _release_gate_lockdown(self, decision) -> None:
        """离开受限前台：恢复钩子、UIA 与浮窗**之前的状态**。

        恢复走 :meth:`start_floating_ui`：展开过的恢复展开、折叠的还是小方块
        （绝不一律 ``_restore_dock`` 把展开缩成 44x44），用户关闭过的不复活；
        授权仍然未知时补问一次。**绝不打开主界面**。
        """
        self._uia_wanted = True
        if self._services_started:
            self.uia.set_enabled(True)
            self._start_uia_async("uia-resume")
            self._ensure_mouse_hook()
        self._apply_gate_overlay_policy()
        self.start_floating_ui()
        if self._ui_ready():
            self.main.on_gate_changed(decision)
            self.main.update_status()

    # ------------------------------------------------------- 浮窗（只显隐）
    def start_floating_ui(self) -> bool:
        """启动 / 恢复**浮窗**：只决定浮窗显隐，绝不打开主界面。

        * **硬阻断**（游戏 / 全屏 / 暂停）→ 一次 ``deiconify`` 都不做；
        * **用户关闭过**（``closed_by_user``）→ 不自动复活（只能由用户显式呼出）；
        * **授权未知** → 展开这一个浮窗显示内联授权卡（首次启动 / 硬阻断解除后
          补询问；已授权绝不重复问）；
        * 已授权 / 已拒绝 → 恢复**之前**的展开 / 折叠状态（启动时是折叠的小方块）；
        * **软受限**（桌面 / 自身窗口）→ 允许**显式**显示小方块 / 展开面板。
        """
        panel = getattr(self, "reading_panel", None)
        if panel is None or not self._ui_ready():
            return False
        if self.gate_controller.is_locked() or getattr(panel, "closed_by_user", False):
            return False
        if (self.config.overlay_topmost_consent is None
                and not getattr(self, "_consent_prompt_shown", False)):
            return self._ask_overlay_topmost_consent()
        return self._restore_floating_state()

    def restore_floating_ui(self) -> bool:
        """用户重复启动：找回已关闭浮窗，保留原展开状态。"""
        panel = getattr(self, "reading_panel", None)
        if panel is not None and not self.gate_controller.is_locked():
            panel.closed_by_user = False
        return self.start_floating_ui()

    def _restore_floating_state(self) -> bool:
        """恢复之前的展开 / 折叠状态（复用面板的 ``state.expanded`` / ``visible``）。

        * 已经显示 → 原样保留（展开的绝不折叠）；
        * 展开过 → 重新展开（用面板自己保存的矩形，**不改位置尺寸**）；
        * 其余 → 小方块。
        """
        panel = getattr(self, "reading_panel", None)
        if panel is None or not self._ui_ready():
            return False
        if getattr(panel, "visible", False):
            return True
        if bool(getattr(getattr(panel, "state", None), "expanded", False)):
            return self._expand_panel()
        return self._restore_dock(explicit=True)

    def _expand_panel(self) -> bool:
        """显式展开面板（权限同 ``ReadingPanel.expand(explicit=True)``）。"""
        panel = getattr(self, "reading_panel", None)
        expand = getattr(panel, "expand", None)
        if panel is None or not callable(expand):
            return False
        try:
            shown = bool(expand(explicit=True))
        except tk.TclError:  # pragma: no cover - 窗口已销毁
            return False
        if shown:
            self._refresh_overlay_hwnds()
        return shown

    def _ask_overlay_topmost_consent(self) -> bool:
        """授权未知：**只展开这一个浮窗**显示内联授权卡（不弹系统对话框）。"""
        panel = getattr(self, "reading_panel", None)
        show_card = getattr(panel, "show_consent_card", None)
        if not callable(show_card):
            return self._restore_dock(explicit=True)
        try:
            show_card()
            shown = bool(panel.expand(explicit=True))
        except tk.TclError:  # pragma: no cover - 窗口已销毁
            return False
        if shown:
            self._consent_prompt_shown = True
            self._refresh_overlay_hwnds()
        return shown

    # ------------------------------------------------------------- 置顶
    def topmost_preference(self) -> bool:
        """旧配置字段 ``ui.topmost``（兼容保留，**不再**驱动任何窗口层级）。"""
        return self.config.topmost

    def effective_topmost(self) -> bool:
        """浮窗（小方块 / 展开面板）此刻该不该置顶。

        **用户已授权且当前不是硬阻断**才置顶；授权状态未知（还没问过用户）
        **不默认置顶** —— 首次启动会先显示内联授权卡（见 ``start_floating_ui``）。
        游戏 / 全屏 / 用户暂停（硬阻断）期间一律取消置顶。
        主窗口**始终普通层级**：它不再是「置顶偏好」的作用对象，用户点开主界面
        时由 ``open_main_window`` 的 ``deiconify + lift`` 负责到最前。

        这里只读设置 + 门控状态，**没有任何 Win32 调用**，也不碰窗口显隐。
        """
        if self.gate_controller.is_locked():
            return False
        return self.config.overlay_topmost_consent is True

    def apply_topmost(self) -> None:
        """把置顶状态同步到浮窗，并把**主窗口固定为普通层级**。

        幂等：主窗口只在第一次（或状态真的漂了）下发一次 ``-topmost False``，
        之后既不重复 ``attributes``、也不重复 ``lift`` —— 轮询里反复提升窗口
        正是用户报告的「频闪」来源之一。

        **只有硬阻断**（游戏 / 全屏 / 用户暂停）才收起浮层：普通阅读环境下
        浮窗 / 小方块的矩形、``visible``、``closed_by_user``、待处理选区与解释
        状态**一律原地保留**，零 ``withdraw`` / ``deiconify``。
        """
        if self._main_topmost is not False:
            try:
                self.root.attributes("-topmost", False)
            except tk.TclError:  # pragma: no cover
                pass
            self._main_topmost = False
        want = self.effective_topmost()
        self.selection_bar.set_topmost(want)
        self.explain_window.set_topmost(want)
        if self.gate_controller.is_locked():
            # 硬阻断：浮层一并收起（不重复取消置顶，避免每次轮询都动 Win32）
            self._hide_overlays()

    def _apply_gate_overlay_policy(self) -> None:
        """受限环境只影响浮窗置顶；隐藏浮层由门控回调统一负责。"""
        if self._ui_ready():
            self.apply_topmost()

    def set_topmost_preference(self, value: bool) -> None:
        """兼容入口：旧配置字段仍然可写，但**不影响**主窗口 / 浮窗层级。"""
        self.config.set_topmost(bool(value))
        self.apply_topmost()
        self.main.update_status()
        self.set_status("浮窗在普通阅读下自动置顶，主界面保持普通层级")

    # --------------------------------------------------- 浮窗置顶授权（用户）
    def overlay_topmost_consent(self) -> bool | None:
        """当前授权：``None`` = 还没问过用户，True 允许，False 拒绝。"""
        return self.config.overlay_topmost_consent

    def set_overlay_topmost_consent(self, allow: bool) -> bool:
        """记录用户对「浮窗置顶」的授权（持久保存），并**立即**同步置顶。

        **不改面板显隐**：这里只调 ``apply_topmost``（层级），不动 ``visible``。
        权限只用应用自己的设置 —— 不需要管理员 / UAC，也不碰系统设置。
        """
        self.config.set_overlay_topmost_consent(bool(allow))
        self.apply_topmost()
        panel = getattr(self, "reading_panel", None)
        hide_card = getattr(panel, "hide_consent_card", None)
        if callable(hide_card):
            hide_card()
        return True

    def toggle_overlay_topmost_consent(self) -> bool:
        """托盘菜单点击 = 一次显式授权 / 撤销（未授权时点击即授权）。"""
        allow = self.config.overlay_topmost_consent is not True
        self.set_overlay_topmost_consent(allow)
        self.set_status("浮窗已允许置顶" if allow else "浮窗已取消置顶")
        return allow


    def _start_uia_async(self, name: str = "uia-start") -> None:
        """在后台线程里启动 UIA helper（start / resume 走同一条路）。

        线程只负责启动，不碰任何窗口状态（``uia_ready`` 事件只刷新状态栏）。
        """
        threading.Thread(target=self._start_uia, name=name, daemon=True).start()

    def _stop_uia_bg(self) -> None:
        with self._uia_start_lock:
            if not self._uia_wanted:
                self.uia.stop()

    def _drain_gestures(self) -> None:
        while True:
            try:
                self._gesture_q.get_nowait()
            except queue.Empty:
                return

    def _start_uia(self, source: str = "uia") -> None:
        """启动 UIA helper。硬阻断（游戏/全屏/暂停）期间**连尝试都不尝试**。

        start / resume 可能同时在两个线程里跑（启动阶段 + 门控解除），这里用
        同一把锁保证 ``uia.start()`` 绝不并发交错 —— 旧实现两个线程各自
        ``start()``，PowerShell 进程会被重复拉起 / 互相覆盖句柄。
        """
        if not self._uia_wanted:
            log.info("门控处于硬阻断：不拉起 UIA helper（%s）", source)
            return
        with self._uia_start_lock:
            if not self._uia_wanted:
                log.info("门控在启动途中转为硬阻断：放弃拉起 UIA helper（%s）", source)
                return
            ok = self.uia.start()
        self._uia_started = ok
        self._ui_q.put(("uia_ready", ok))
        if not ok:
            log.warning("UIA helper 不可用：%s", self.uia.last_error())

    # ------------------------------------------------------------- 事件
    def _on_gesture(self, x: int, y: int, kind: str) -> None:
        self._dispatch_gesture(x, y, kind)

    def _on_mouse_press(self, x: int, y: int, when: float) -> None:
        """全局左键按下（钩子线程）：**当场**采集 Win32 元信息，判断留给 UI 线程。

        这里必须记录「按下瞬间鼠标点在谁的窗口上」（``is_overlay``）：
        等 UI 线程消费时浮条可能已经隐藏，再用 ``WindowFromPoint`` 做二次
        hit-test 会把外部点击误判成「点在浮条上」，浮条就收不掉了。
        浮层 HWND 集合由 UI 线程预先缓存成不可变 tuple，钩子线程只读。
        """
        try:
            meta = self.capture_service.handle_mouse_press(
                x, y, when, overlay_hwnds=self._overlay_hwnds)
        except Exception:  # pragma: no cover - 钩子线程里绝不可抛出
            log.exception("鼠标按下元信息捕获失败")
            return
        try:
            self._ui_q.put_nowait(("overlay_press", meta))
        except queue.Full:  # pragma: no cover
            pass

    def _dispatch_gesture(self, x: int, y: int, kind: str) -> None:
        """把一次划选手势交给工作线程（**带上排队时的世代指纹**）。

        ``generation`` 与 ``page_epoch`` 一起入队：前台在 UIA 调用期间换了窗口
        或标签页时，这份结果会因为页面世代变了而被丢弃，不会算到新页面上。

        **入队前先同步采一次前台**（``note_foreground``）：``ForegroundWatcher``
        每 0.7 秒才轮询一次，新页面上的第一个词完全可能先到 —— 那时世代指纹还是
        旧页面的，结果会被 page_changed 丢掉（用户看到的就是「切页后第一个词
        划了没反应」）。采样后**一次锁内**取两个世代号，保证它们属于同一时刻。
        """
        service = self.capture_service
        try:
            service.note_foreground()
        except Exception:  # pragma: no cover - 采样失败不得丢掉这次手势
            log.exception("手势入队前的前台采样失败")
        try:
            generation, page_epoch = service.gesture_fingerprint()
            self._gesture_q.put_nowait((x, y, kind, generation, page_epoch))
        except queue.Full:  # pragma: no cover
            pass

    def _gesture_loop(self) -> None:
        while not self._gesture_stop.is_set():
            try:
                item = self._gesture_q.get(timeout=0.4)
            except queue.Empty:
                continue
            try:
                x, y, kind = int(item[0]), int(item[1]), str(item[2])
                gen = int(item[3]) if len(item) > 3 and item[3] is not None else None
                epoch = int(item[4]) if len(item) > 4 and item[4] is not None else None
                self.capture_service.handle_gesture(x, y, kind, generation=gen,
                                                    page_epoch=epoch)
            except Exception:  # pragma: no cover
                log.exception("处理手势失败")

    def _pump(self) -> None:
        """UI 泵：所有 Tk 操作都在主线程执行。

        顺序很关键：**先更新门控，再消费事件队列**。这样刚切到游戏/全屏时，
        队列里排队的 selection_ready 会在处理前就看到最新的受限判定，
        不会被当作「允许显示」而上屏。
        """
        if self._closing:
            return
        now = time.monotonic()
        if now >= self._gate_next_check:
            self._gate_next_check = now + GATE_POLL_SECONDS
            try:
                self._apply_gate()
            except Exception:  # pragma: no cover
                log.exception("门控检查失败")
        drained = 0
        while drained < 100:
            try:
                event, payload = self._ui_q.get_nowait()
            except queue.Empty:
                break
            drained += 1
            if event == "overlay_press":
                self._handle_overlay_press(payload)
                continue
            try:
                self._handle_event(event, payload)
            except Exception:  # pragma: no cover
                log.exception("处理 UI 事件失败: %s", event)
        self.root.after(60, self._pump)

    # ------------------------------------------------- 结果的时效性判定（核心）
    def _selection_stale_reason(self, payload: dict) -> str:
        """判断一条 selection_ready 是否已过期。

        过期 = 不显示浮条、不解释、不落库（迟到的结果一概丢弃）。
        判据（任一不满足即过期）：
        1. 捕获时的门控世代必须等于当前世代（切走游戏、切到别的窗口都会 +1）；
        2. 捕获时的前台顶层窗口 hwnd/pid 必须仍是当前前台窗口；
        3. 当前前台不能是本程序自己的窗口。
        """
        gen = payload.get("generation")
        if gen is not None and int(gen) != self.capture_service.current_generation():
            return "generation_changed"
        fg = w32.foreground_info()
        if fg.get("is_self"):
            return "self_window"
        hwnd = int(payload.get("hwnd") or 0)
        pid = int(payload.get("pid") or 0)
        if hwnd and int(fg.get("hwnd") or 0) != hwnd:
            return "foreground_window_changed"
        if pid and int(fg.get("pid") or 0) != pid:
            return "foreground_process_changed"
        return ""

    # ----------------------------------------------------- 选区状态机动作
    def current_selection(self):
        """当前内存里的选区快照（未落库）。"""
        return self._current_selection

    def current_selection_token(self) -> int:
        return self._selection_token

    def _selection_signature(self, selection=None):
        """选区的判等键；拿不到时返回 None。"""
        sel = self._current_selection if selection is None else selection
        if sel is None:
            return None
        try:
            return sel.signature()
        except Exception:  # pragma: no cover - 防御异常快照
            return None

    def _handle_selection_ready(self, payload: dict) -> None:
        """一份**新的内存快照**到达：只弹浮条，不落库、不联网。"""
        sel = payload.get("selection")
        if sel is None:
            return
        stale = self._selection_stale_reason(payload)
        if stale:
            log.info("丢弃过期选区（%s）：%s", stale, getattr(sel, "term", ""))
            return

        # 深拷贝：内存快照与调用方（工作线程）彻底解耦，之后的任何改动都影响不到它
        snapshot = copy.deepcopy(sel)
        # 新选区：旧快照与「已记录」标记一并作废；解释结果窗收起 ——
        # 迟到的旧结果不得覆盖新词（但旧的**数据库写回**不受影响，见 _hide_overlays）
        self._current_selection = snapshot
        self._selection_token += 1
        #: 这份选区是**哪一页**的（页面世代）：迟到的 foreground_changed 事件
        #: 据此判断「该不该清掉它」—— 新页面拿到的选区绝不被旧事件清掉
        self._selection_page_epoch = self._payload_page_epoch(payload)
        self._recorded_selection = None
        try:
            self.explain_window.clear()
        except tk.TclError:  # pragma: no cover
            pass

        if not self.overlay_allowed() and not self._panel_is_visible():
            decision = self.gate_decision()
            # 当前前台不该**新映射**浮窗（桌面 / 无前台 / 硬阻断）：
            # 选区快照仍然留在内存里等用户显式操作，但**绝不动已经展开的面板**。
            self.set_status(f"已取到「{snapshot.term}」（当前前台不展开面板：{decision.label}）")
            return

        point = payload.get("point") or (0, 0)
        shown = self.selection_bar.show_selection(snapshot, self._selection_token, point)
        if shown:
            self._bar_shown_at = time.monotonic()
        self._refresh_overlay_hwnds()
        if shown:
            self.set_status(f"已选中：{snapshot.term}（点「解释并记录」才会保存）")
        else:
            self.set_status(f"已取到「{snapshot.term}」"
                            "（面板已由用户关闭，需要时按 Ctrl+Alt+Shift+D 重新打开）")

    def _handle_selection_cleared(self, payload: dict) -> None:
        """新选区为空 / 取不到选区 → 收起操作条（不落库、不联网）。"""
        gen = payload.get("generation")
        if gen is not None and int(gen) != self.capture_service.current_generation():
            return
        self._dismiss_selection_bar()

    @staticmethod
    def _payload_page_epoch(payload) -> int | None:
        """事件负载里的页面世代（缺失 / 脏数据 → ``None``，按「未知」处理）。"""
        try:
            meta = payload or {}
            value = meta.get("page_epoch")
        except AttributeError:  # pragma: no cover - 极简替身
            return None
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):  # pragma: no cover - 脏数据
            return None

    @staticmethod
    def _page_key_of(src) -> str | None:
        """来源对象的页面身份（``doc_key``）；拿不到就返回 ``None``。"""
        key = getattr(src, "doc_key", "") if src is not None else ""
        return str(key) if key else None

    def _dismiss_selection_bar(self) -> None:
        """作废当前待处理选区：**只清内存快照，不折叠、不隐藏任何窗口**。

        最新契约下「点外部 / 选区为空」这类软事件只能让旧选区失效 ——
        面板是用户自己打开并保持的，软事件没有权力替用户把它收起来。
        真正的显隐只由 :meth:`ReadingPanel.collapse_to_dock`（用户点折叠）、
        :meth:`ReadingPanel.close_by_user`（用户点关闭）与硬阻断
        （:meth:`_hide_overlays`）驱动。

        **绝不**写库、绝不发网络请求、绝不影响在途解释。
        """
        self._drop_current_selection()

    def _drop_current_selection(self) -> bool:
        """把「当前选区」从内存里清掉（面板上的待处理词也一并清）。"""
        self._current_selection = None
        self._selection_page_epoch = None
        win = getattr(self, "selection_bar", None)
        drop = getattr(win, "_drop_pending_selection", None)
        try:
            if callable(drop):
                drop()
            elif win is not None and hasattr(win, "selection"):
                win.selection = None
        except Exception:  # pragma: no cover - 面板替身没有这个能力
            return False
        return True

    def _panel_is_visible(self) -> bool:
        """面板此刻是否映射在屏幕上（用于区分「新映射」与「保持现状」）。"""
        win = getattr(self, "selection_bar", None)
        return bool(getattr(win, "visible", False))

    def _finish_selection_action(self, *, page: str | None = None) -> bool:
        """「解释并记录」之后消费当前选区：**共享面板保持窗口矩形**。

        用户只看到一个面板（``selection_bar is explain_window``）。旧的
        ``ExplainWindow.clear()`` / ``SelectionBar.hide()`` 会先隐藏再显示，
        对同一个窗口就是「先缩成小方块再展开」——用户看到的频闪。

        因此这里优先走面板的 :meth:`ReadingPanel.finish_action`（消费 + 换页，
        零 geometry 调用）；只有旧式替身（真实 ``SelectionBar``）才 ``hide()``。
        """
        win = getattr(self, "selection_bar", None)
        consume = getattr(win, "finish_action", None)
        try:
            if callable(consume):
                return bool(consume(page=page))
        except tk.TclError:  # pragma: no cover
            pass
        # 旧式选区条（没有共享窗口）：隐藏它，不碰结果窗
        legacy = getattr(win, "hide", None)
        try:
            if callable(legacy):
                changed = bool(legacy())
                if changed:
                    self._refresh_overlay_hwnds()
                return changed
        except tk.TclError:  # pragma: no cover
            pass
        return False

    def _handle_overlay_press(self, meta: dict) -> None:
        """全局左键按下的**按下时刻元信息**（钩子线程采集，UI 线程消费）。

        判定只用按下瞬间记录的 ``is_overlay``（Win32 ``WindowFromPoint`` +
        我们的浮层 HWND 集合）：

        * 点在面板 / 小方块上 → 什么都不做（交给 Tk 自己的回调；因此在详情页
          输入框里打字、点标签、点按钮都不会被误判成「点外部」）；
        * 点在任何**其它**地方 → **只作废当前待处理选区**（旧选区不再属于
          用户接下来要看的东西），**绝不**替用户收起已经展开的面板。
          面板的显隐只由用户点「折叠 / ×」和硬阻断决定；
        * 按下发生在本面板显示**之前**（迟到的队列事件）→ 忽略，
          绝不误动作刚展开的面板。

        这里既不做「浮条隐藏之后的二次 hit-test」，也不用任何时间宽限：
        点在浮层上这件事在按下那一刻就已经定死了。
        """
        when = float(meta.get("when") or 0.0)
        self._last_click = (int(meta.get("x") or 0), int(meta.get("y") or 0), when)
        if meta.get("is_overlay"):
            return
        if when and when < self._bar_shown_at:
            return
        self._dismiss_selection_bar()

    def _handle_event(self, event: str, payload) -> None:
        if event == "selection_ready":
            self._handle_selection_ready(payload)

        elif event == "selection_cleared":
            self._handle_selection_cleared(payload)

        elif event == "foreground_changed":
            # 普通窗口 → 普通窗口 / 同 HWND 换标签页：GateController 三态不变，
            # 旧浮条只能靠这条事件收起（否则会一直挂在屏幕上）。
            # 事件带着**变化当时**的页面世代：迟到的它不得清掉新页面的首选区
            # （见 _hide_overlays_for_foreground_change）。
            kind = str(payload.get("kind") or "window")
            if kind == "resume":
                self._handle_page_resume(payload.get("source"))
            elif kind == "first":
                # 首次确认阅读页（进程启动后第一次看到非自身窗口）：
                # 只让视图跟上当前页，不作废任何选区（此刻还没有选区）。
                self._follow_page_views()
            else:
                self._hide_overlays_for_foreground_change(
                    payload.get("source"), kind,
                    page_key=self._page_key_of(payload.get("source")),
                    page_epoch=payload.get("page_epoch"))

        elif event == "tray_open":
            # 托盘菜单只投事件：真正的门控判定与开窗都在 UI 线程里做。
            self.request_open_main("tray")

        elif event == "tray_toggle_pause":
            self.toggle_capture()

        elif event == "tray_topmost":
            # 托盘的「浮窗置顶」勾选项：显式菜单点击 = 一次授权 / 撤销
            self.toggle_overlay_topmost_consent()

        elif event == "tray_quit":
            self.quit()

        elif event == "bookmarked":
            # 「解释并记录」的落库事件：刷新列表与卡片（浮条已经隐藏）
            self.main.refresh_batches()
            self.main.refresh_entries()

        elif event == "explain_result":
            self._handle_entry_explain_result(payload)

        elif event == "chat_result":
            self._handle_chat_result(payload)

        elif event == "map_result":
            self._handle_map_result(payload)

        elif event == "hotkey":
            self._on_hotkey(int(payload))

        elif event == "uia_ready":
            self.main.update_status()
            if not payload:
                self.set_status("取词不可用，可手动录入或从剪贴板导入")


    def _on_hotkey(self, hid: int) -> None:
        if hid == HK_RECALL:  # 呼出
            self.recall_ui()
        elif hid == HK_GRAB:  # 立即抓取
            self.grab_now()
        elif hid == HK_PAUSE:  # 暂停/恢复
            self.toggle_capture()
        elif hid == HK_GAME_MODE:  # 游戏模式
            self.toggle_game_mode()
        elif hid == HK_QUIT:  # 退出
            self.quit()

    def panel_visible(self) -> bool:
        """共享面板此刻是否已经映射（展开浮窗或小方块都算）。"""
        panel = getattr(self, "reading_panel", None)
        return bool(getattr(panel, "visible", False))

    def recall_ui(self) -> None:
        """呼出（快捷键 Ctrl+Alt+Shift+D）：打开词典主界面，并保证小方块可见。

        显式呼出的三档语义（用户硬要求）：

        * **硬阻断**（游戏 / 全屏 / 用户暂停）→ 仍然拒绝任何浮层并收起，
          一次 ``deiconify`` 都不做；
        * **面板已经显示**（展开或小方块，含 self / 桌面 / 无前台等软受限前台）
          → **原样保留**：不折叠、不改矩形、更不 ``_restore_dock`` 缩成 44x44；
        * **已关闭 / 已隐藏** → 这是用户的显式动作，才把小方块找回来
          （``explicit=True``，会清除 ``closed_by_user``）。
        """
        decision = self._apply_gate()
        if decision.hard_blocked:
            self._hide_overlays()
            self.set_status(f"已打开主界面（当前前台不展开面板：{decision.label}）")
        elif self.panel_visible():
            self.set_status("已打开主界面（面板保持原状）")
        else:
            self._restore_dock(explicit=True)
        # 用户显式「打开词典」：和双击 exe 走同一套 —— 第一次会带出配置向导
        self.open_main_window(wizard=True)

    def recover_panel(self) -> bool:
        """菜单 / 快捷键的显式恢复：把用户主动关闭过的小方块找回来。"""
        shown = self._restore_dock(explicit=True)
        if shown:
            self.set_status("已找回小方块：点它即可展开阅读面板")
        return shown

    # ---------------------------------------------------------- 供 UI 调用
    def capture_enabled(self) -> bool:
        return self.config.capture_enabled

    def game_mode(self) -> bool:
        return self.gate.game_mode

    def toggle_capture(self) -> None:
        """安全暂停：任何前台环境下都可用（用户意图最高优先级）。"""
        new = not self.config.capture_enabled
        self.config.set_bool("capture.enabled", new)
        self._apply_gate(force=True)
        self.set_status("已暂停取词（回到阅读窗口也不会自动恢复）" if not new else "已恢复取词")
        self.main.update_status()

    def toggle_game_mode(self) -> None:
        """手动游戏模式：持续禁用全局鼠标监听与取词，直到用户主动关闭。"""
        new = not self.gate.game_mode
        self.gate.set_game_mode(new)
        self._apply_gate(force=True)
        self.main.update_status()
        if new:
            log.info("游戏模式已开启：已卸载全局鼠标监听并停止取词")
            self.set_status("游戏模式已开启（Ctrl+Alt+Shift+G 关闭）")
        else:
            self.set_status("游戏模式已关闭")

    def uia_status_text(self) -> str:
        if not self._uia_started:
            return "UIA：不可用"
        return "UIA：就绪" if self.uia.pid() else "UIA：未运行"

    def hotkey_status_text(self) -> str:
        n = len(self.hotkeys.registered)
        total = len(HOTKEYS)
        if n == total:
            return f"热键：{n}/{total}"
        if not self._recall_hotkey_ok:
            return f"热键：{n}/{total}（呼出键失败！）"
        return f"热键：{n}/{total}（有冲突）"

    def hotkey_description(self) -> str:
        from .hotkeys import HOTKEYS as HK
        from .hotkeys import required_failures

        failed = {hid for hid, _ in self.hotkeys.failed}
        missing = set(required_failures(self.hotkeys.registered))
        lines = []
        for hid, (label, _mods, _vk) in HK.items():
            mark = ""
            if hid in failed:
                mark = "  ✗ 注册失败（可能被占用）"
                if hid in missing:
                    mark += " ← 关键快捷键，必须可用"
            lines.append(f"  {label}{mark}")
        return "\n".join(lines)

    def set_status(self, text: str) -> None:
        try:
            self.main.status_label.configure(text=text)
        except tk.TclError:  # pragma: no cover
            pass

    def describe_current_source(self) -> str:
        src = self.capture_service.last_source()
        if not src:
            return "来源：(未知)"
        conf = {"url_document": "真实 URL", "window_title_only": "仅窗口标题识别",
                "manual": "人工修正"}.get(src.confidence, src.confidence)
        line = src.title or src.app or "(未知来源)"
        if src.url:
            line += f"\n{src.url}"
        return f"{line}   [{conf}]"

    def lookup_cached_explanation(self, term: str, context: str):
        """只查本地缓存，绝不联网。"""
        try:
            return self.explain_service.lookup_cache(term, context)
        except Exception:  # pragma: no cover
            log.exception("查缓存失败")
            return None

    def explain_selected(self, force: bool) -> None:
        entry_id = self.main._selected_entry_id
        if not entry_id:
            # 校验反馈**内联**在状态行（不弹系统 messagebox）
            self.set_status("请先在列表中选择一条词条")
            return
        self._explain(entry_id, force)

    # ------------------------------------------- 唯一动作：解释并记录
    def explain_and_record_selection(self, selection, *, force: bool = False,
                                     anchor: tuple[int, int] | None = None) -> int | None:
        """浮条上**唯一按钮**「解释并记录」的全部行为。

        顺序固定，不可交换：

        1. **幂等落库**：把这份内存快照（关键词 + 来源 / 上下文）写进原有
           SQLite / 批次；同一个选区被连续点击只会写一次，不会重复记词、
           也不会再加词频；
        2. **消费当前选区**（不取消任何已经开始的写库 / 解释）：共享面板
           **保持窗口矩形**、切到该词条的详情页；旧的独立选区条才隐藏自己
           —— 用户只看到一个面板，先折叠再展开就是频闪；
        3. **异步解释**并写回**同一个 entry_id**；没有 API Key 时也**先保存**，
           解释窗显示「已记录（待解释）」以及可见的「打开设置 / 重新解释」。

        返回落库的 ``entry_id``（失败返回 None）。
        """
        if selection is None:
            self._dismiss_selection_bar()
            self.set_status("没有可解释并记录的选区（请先选中文本）")
            return None
        # 深拷贝：这是本次动作唯一的事实来源，之后任何清空 / 新选区都影响不到它
        snapshot = copy.deepcopy(selection)
        self._finish_selection_action(page="detail")
        entry_id = self._record_snapshot_once(snapshot)
        if entry_id is None:
            return None
        self._start_explain_for_entry(entry_id, snapshot, force=force, anchor=anchor)
        return entry_id

    def _record_snapshot_once(self, snapshot) -> int | None:
        """幂等落库：同一选区只写一次（不重复记词、不再加词频）。

        「同一个选区」的判据是 **选区序号 + 选区签名**：序号由
        :meth:`_handle_selection_ready` 每次新选区 +1，因此跨选区的重复词
        仍然走原有去重规则（``repeat_count +1``），只有**同一次选区的
        连续点击**才会被合并成一次写入。
        """
        signature = self._selection_signature(snapshot)
        recorded = self._recorded_selection
        if recorded and signature is not None and recorded.get("signature") == signature \
                and int(recorded.get("token") or -1) == int(self._selection_token):
            return int(recorded["entry_id"])
        try:
            entry_id, created = self.capture_service.record_snapshot(snapshot)
        except Exception as exc:
            log.exception("记录失败")
            messagebox.showerror("解释并记录", f"写入数据库失败：{exc}", parent=self.root)
            return None
        self._recorded_selection = {
            "token": int(self._selection_token),
            "signature": signature,
            "entry_id": int(entry_id),
            "created": bool(created),
        }
        self.main.refresh_batches()
        self.main.refresh_entries()
        self.main.select_entry(entry_id)
        self._refresh_panel_terms()
        self.set_status(("已记录" if created else "已记录（重复 +1）")
                        + f"：{getattr(snapshot, 'term', '')}")
        return int(entry_id)

    def _refresh_panel_terms(self) -> None:
        """让面板的标签列表跟上最新数据（**只读本地库**，不联网）。"""
        panel = getattr(self, "reading_panel", None)
        refresh = getattr(panel, "refresh_terms", None)
        if callable(refresh):
            try:
                refresh()
            except tk.TclError:  # pragma: no cover
                pass

    def _start_explain_for_entry(self, entry_id: int, selection, *, force: bool = False,
                                 anchor: tuple[int, int] | None = None) -> bool:
        """对**已经落库的 entry_id** 发起异步解释（词条归属，不是选区 token）。

        解释服务返回的 token 是**请求 token**：它只用于「这一次请求的结果该不该
        更新结果窗」的判定（``_explain_tokens[entry_id]`` = 该词条最新请求），
        **永不**与 ``_selection_token`` 比较 —— 两套计数器互相独立。
        """
        if anchor is None:
            anchor = self._last_click[:2] if self._last_click else None
        ok, msg = self.explain_service.is_ready()
        if not ok:
            # 用户可见的只有正文这一句；技术原因（is_ready 的具体判定）写日志，便于定位。
            # 正文不写按钮名 / 内部编号：可执行的配置提示是面板底部那句**可点**的话。
            log.info("未发起解释 #%s：%s", entry_id, msg)
            self._show_pending(entry_id, selection, anchor, "已记录，待解释。")
            self.set_status(f"已记录（待解释）：{getattr(selection, 'term', '')}")
            return False

        token = self.explain_service.explain_entry_async(entry_id, force=force)
        if token is None:
            if self.explain_service.is_inflight(int(entry_id)):
                self._show_pending(entry_id, selection, anchor,
                                   "已记录。正在解释，结果会显示在这里。")
                self.set_status("正在解释…")
                return False
            self._show_pending(entry_id, selection, anchor,
                               "已记录，但解释未能启动。请检查设置后重试。")
            self.set_status("解释未能启动（请检查设置）")
            return False

        self._explain_tokens[int(entry_id)] = int(token)
        self.explain_window.show_recorded(selection, int(entry_id), anchor,
                                          request_token=int(token), status="pending",
                                          explicit=True)
        self._refresh_overlay_hwnds()
        self.set_status(f"正在解释：{getattr(selection, 'term', '')}")
        return True

    def _show_pending(self, entry_id: int, selection, anchor, message: str) -> None:
        """无 Key / 未启动时的**待解释**状态：窗口真的可见，且带重试 / 设置入口。

        窗口跟随「该词条已知的最新请求 token」（如果有）：这样一条仍在途的旧请求
        结果回来时能正确落到窗口上；没有请求 token 时（例如从未配置过 Key）
        任何结果都不会误改这个「待解释」状态。

        ``explicit=True``：这条路径只由用户点「解释并记录」触发，属于显式动作，
        所以允许展开（并清除 ``closed_by_user``）；后台更新不走这里。
        """
        known = self._explain_tokens.get(int(entry_id))
        self.explain_window.show_recorded(selection, int(entry_id), anchor,
                                          request_token=known, status="pending_no_key",
                                          message=message, explicit=True)
        self._refresh_overlay_hwnds()

    def restore_pending_explain_window(self) -> bool:
        """**明确恢复入口**：把同一条词条的待解释结果窗重新显示出来。

        用户在没有 Key 时点了「解释并记录」→ 结果窗显示「已记录，待解释」；
        去设置里配好 Key 之后（窗口可能因为前台切换被收起），用本方法把**同一个
        entry / 同一个请求 token** 的窗口找回来，接着点详情页的「解释」即可。

        **只显示，不联网、不写库**：真正的解释永远只由用户点按钮触发；
        用户已经「关闭」过的窗口不会被找回（``closed_by_user``）。
        """
        win = getattr(self, "explain_window", None)
        restore = getattr(win, "restore", None)
        if not callable(restore):
            return False
        try:
            shown = bool(restore())
        except tk.TclError:  # pragma: no cover
            return False
        if shown:
            self._refresh_overlay_hwnds()
            self.set_status("已找回待解释窗口（配置 API 后可解释）")
        return shown

    def retry_explain_entry(self, entry_id: int | None, *,
                            anchor: tuple[int, int] | None = None) -> bool:
        """解释窗上的「重新解释」：**只解释这一条已经落库的词条**。

        绝不重新记词、绝不再加词频：这里只调用 ``explain_entry_async``，
        落库路径（``record_snapshot``）根本不会被触碰。
        """
        if not entry_id:
            return False
        row = self.db.get_entry(int(entry_id))
        if row is None:
            self.set_status("词条不存在（可能已被删除），无法重新解释")
            return False
        if self.explain_service.is_inflight(int(entry_id)):
            self.set_status("该词条正在解释中，请稍候…")
            return False
        snapshot = self._selection_for_entry(int(entry_id), row)
        return self._start_explain_for_entry(int(entry_id), snapshot, force=True, anchor=anchor)

    def _selection_for_entry(self, entry_id: int, row=None):
        """解释窗自己的快照来源：优先用窗口手里那份（同一条词条），否则按库重建。

        这是「解释窗按自己的 entry/token 请求快照隔离」的落点：入口永远针对
        窗口上的 ``entry_id``，而不是当前选区。窗口手里那份必须**同时满足**
        「绑定的就是这条词条」与「term / context 都与库里这条词的完整内容一致」
        —— 打开词条 B 时面板可能还攥着上一次划选的 A，单看 ``entry_id`` 会把 A
        的 term/context 当成 B；同词不同 context（同一条词的语境被改过、或另一条
        同名词条）则只有 term 相同，也会把旧 context 当成库里的内容。任一不一致
        就按库重建，显示层的截断永远不会进入快照。
        """
        win = self.explain_window
        row = row if row is not None else self.db.get_entry(int(entry_id))
        if row is None:
            return None
        held = getattr(win, "selection", None)
        same_entry = (getattr(win, "entry_id", None) is not None
                      and int(win.entry_id) == int(entry_id))
        if same_entry and held is not None \
                and str(getattr(held, "term", "") or "") == str(row["term"] or "") \
                and str(getattr(held, "context", "") or "") == str(row["context"] or ""):
            return copy.deepcopy(held)
        return CapturedSelection(
            term=row["term"] or "",
            context=row["context"] or "",
            method="manual_input",
            source=SourceInfo(
                app=row["source_app"] or "",
                title=row["source_title"] or "",
                url=row["source_url"] or "",
                confidence=row["source_confidence"] or "window_title_only",
            ),
        )

    def _handle_entry_explain_result(self, payload) -> None:
        """词条解释结果（成功 / 失败 / 过期）。

        分两条互不干扰的路径处理：

        * **卡片 / 详情**：只按 ``entry_id`` 刷新 —— 这就是旧
          ``on_result(entry_id, ...)`` 的语义，迟到的结果也必须刷新它自己的卡片；
        * **解释结果窗**：要求 ``entry_id`` **与解释请求 token 同时匹配**。
          带 token 的事件来自 :meth:`ExplainService.set_entry_result_sink`
          （4 元组后面追加 token）；只有 4 元组的旧事件（没有 token 信息）
          才退回「只看 entry」的历史语义。
          这样用户在旧结果还在排队时点了「重新解释」（生成新请求 token），
          旧结果就**不会**覆盖新请求的窗口状态，而卡片照常刷新。

        这里的 token 是**解释服务的请求 token**，与选区序号（``_selection_token``）
        是两套计数器，永不互相比较。

        写库由解释服务在后台线程完成（**不受窗口关闭 / 切源影响**）；
        这里只负责刷新界面，并且**绝不**用迟到的结果重新打开已经隐藏的解释窗。
        """
        values = list(payload)
        entry_id = int(values[0])
        status = values[1] if len(values) > 1 else ""
        result = values[2] if len(values) > 2 else None
        error = values[3] if len(values) > 3 else None
        request_token = values[4] if len(values) > 4 else None
        row = self.db.get_entry(entry_id)
        term = (row["term"] if row else "") or ""
        error_text = "" if status in ("ok", "stale") else (
            error.display() if isinstance(error, ApiError) else (str(error) if error else "未知错误"))
        if status == "ok":
            self.set_status(f"解释完成：{term}")
        elif status == "stale":
            self.set_status("词条已改动，已丢弃过期解释")
        else:
            self.set_status(f"解释失败：{error_text}")
        self.main.refresh_selected()
        self.main.refresh_entries()
        if status == "ok":
            # 主题名可能已被解释结果优化 —— 改名的动作在 ExplainService._mark_ok
            # 里与「写回词条」同锁 / 同事务完成（见该方法的说明）；
            # UI 回调**只刷新**，不再在这里按 db.get_entry 的现状起名。
            self.main.refresh_batches()
        # 词卡摘要（one_line）变了就更新一次标签；数据没变时 refresh_terms 幂等，
        # 一个控件都不重建，也绝不重新映射窗口。
        self._refresh_panel_terms()
        if self.main._selected_entry_id is None and row is not None:
            self.main.select_entry(entry_id)
        # 结果窗只在「entry 与请求 token 同时匹配、且仍然可见」时更新；
        # 隐藏后绝不重开（show_result 自己保证），硬阻断下立即收起（零 deiconify）。
        self.explain_window.show_result(
            entry_id, status, result, error_text, request_token=request_token)

    def invalidate_snapshot_explanation(self, entry_id: int) -> None:
        """词条内容被编辑后：若面板正显示这条词条的解释，就地作废。"""
        if self.explain_window.entry_id is None:
            return
        if int(self.explain_window.entry_id) != int(entry_id):
            return
        self.explain_window.clear()
        self.set_status("词条已修改，原解释已作废")

    # ------------------------------------------------ 追问（对话）的唯一入口
    def ask_entry_question(self, entry_id: int, question: str) -> int | None:
        """**用户点「发送」**才会走到这里：异步追问，返回 request_id（None = 没发起）。

        没有任何自动触发路径：划选、展开、点标签、折叠都不会调用它。
        没有 Key 时 ``ChatService.submit`` 直接返回 None（零网络请求），
        由面板给出可配置提示并**保留用户输入**。
        """
        if not entry_id or not (question or "").strip():
            return None
        ok, msg = self.chat_service.is_ready()
        if not ok:
            self.set_status(f"未能发送追问：{msg}")
            return None
        token = self.chat_service.submit(int(entry_id), str(question).strip())
        if token is None:
            if self.chat_service.is_inflight(int(entry_id)):
                self.set_status("正在回答，请稍候…")
            else:
                self.set_status("追问未能启动（词条可能已删除，或追问已关闭）")
            return None
        # 发送范围（本词 + 短上下文 + 已有解释 + 最近几轮）写日志，界面上只留一句
        log.info("追问已发送 #%s：只发送本词、短上下文、已有解释与最近几轮对话", entry_id)
        self.set_status("正在回答…")
        return int(token)

    def retry_entry_question(self, entry_id: int | None) -> int | None:
        """追问失败后的「重试」：重发最后一次提问，**不重复记录**那一问。"""
        if not entry_id:
            return None
        row = self.db.get_entry(int(entry_id))
        if row is None:
            self.set_status("词条不存在（可能已被删除），无法重试追问")
            return None
        ok, msg = self.chat_service.is_ready()
        if not ok:
            self.set_status(f"未配置：{msg}")
            return None
        token = self.chat_service.retry(int(entry_id))
        if token is None:
            self.set_status("没有可重试的提问（或正在回答中）")
            return None
        self.set_status("正在回答…")
        return int(token)

    def chat_unavailable_hint(self) -> str:
        """无 Key / 追问关闭时给用户看的提示（文案可在设置里配置）。"""
        return self.chat_service.unavailable_hint()

    def chat_inflight(self, entry_id: int) -> bool:
        return self.chat_service.is_inflight(int(entry_id or 0))

    def _handle_chat_result(self, payload) -> None:
        """追问结果（成功 / 失败 / 丢弃）。

        写库由 :class:`ChatService` 在后台线程按 ``entry_id`` 完成，
        这里只把它交给面板做**归属校验**（当前词 + 当前 request_id 同时匹配
        才更新界面）：切词、折叠、删除词条都不会串话，也不会重新展开面板。
        """
        values = list(payload)
        request_id = int(values[0]) if values else 0
        entry_id = int(values[1]) if len(values) > 1 else 0
        status = str(values[2]) if len(values) > 2 else ""
        content = str(values[3]) if len(values) > 3 else ""
        error_text = str(values[4]) if len(values) > 4 else ""
        if status == "ok":
            self.set_status("追问已回答")
        elif status == "error":
            self.set_status(f"追问失败：{error_text or '未知错误'}")
        else:
            self.set_status("该词条已被删除，这次追问的回答已丢弃")
        panel = getattr(self, "reading_panel", None)
        handler = getattr(panel, "on_chat_result", None)
        if callable(handler):
            try:
                handler(request_id, entry_id, status, content, error_text)
            except tk.TclError:  # pragma: no cover
                log.exception("刷新追问结果失败")

    # ------------------------------------------------------------ 面板辅助
    def panel_topic_line(self) -> str:
        """浮窗顶栏**唯一**的来源文案：当前这一页的**短主题名**（没有 → 空串）。

        刻意**不拼**浏览器窗口标题 / URL：用户要求顶栏只说「现在这组词是什么」，
        完整标题与链接只在主界面的「来源链接」里看。返回空串时面板会把
        来源行**连分隔线一起隐藏**（不留空行）。
        """
        topic = ""
        #: 跟随**当前阅读页面**（只读查找，不建主题、不写 settings）
        try:
            batch_id = resolve_page_scope(self.capture_service)[0]
        except Exception:  # pragma: no cover - 取词服务缺失时当作没有主题
            batch_id = None
        batch = self.db.get_batch(batch_id) if batch_id else None
        if batch is not None:
            topic = str(batch["name"] or "").strip()
        return topic

    def panel_context_line(self) -> str:
        """兼容旧名：等价于 :meth:`panel_topic_line`。

        历史实现会把「主题名 · 浏览器窗口标题」拼在一起显示在浮窗顶栏；
        最新契约下顶栏只有短主题名，因此这里直接转发，避免出现第二套文案。
        """
        return self.panel_topic_line()

    def open_app_menu(self, panel=None) -> None:
        """面板「菜单」按钮：弹出与主窗口菜单栏同一份菜单（保证随时可退出）。"""
        try:
            app_menu.popup(panel or self.reading_panel, self)
        except tk.TclError:  # pragma: no cover
            log.exception("弹出菜单失败")

    def _explain(self, entry_id: int, force: bool) -> None:
        ok, msg = self.explain_service.is_ready()
        if not ok:
            # 缺 Key：状态行给一句短反馈并**直接打开设置**（不弹系统 messagebox）
            self.set_status(f"未配置：{msg}")
            self.open_settings()
            return
        if force:
            log.info("强制重解释 #%s（跳过缓存）", entry_id)
        started = self.explain_service.explain_entry_async(entry_id, force=force)
        if started:
            self.set_status("正在解释…")
        elif self.explain_service.is_inflight(entry_id):
            self.set_status("正在解释…")

    def open_manual_dialog(self) -> None:
        preset_term = ""
        sel = self.capture_service.last_selection()
        if sel:
            preset_term = sel.term
        ManualEntryDialog(self.root, self, preset_term=preset_term)

    def import_clipboard(self) -> bool:
        """用户主动触发；只读剪贴板，从不写入。"""
        try:
            result = self.capture_service.clipboard_entry()
        except Exception as exc:  # pragma: no cover
            log.exception("剪贴板导入失败")
            messagebox.showerror("剪贴板导入", f"导入失败：{exc}", parent=self.root)
            return False
        if result is None:
            messagebox.showinfo(
                "剪贴板导入",
                "剪贴板里没有可用文本。\n\n请先在原应用里选中文字并按 Ctrl+C（这一步由你完成），"
                "再回到这里导入。",
                parent=self.root,
            )
            return False
        entry_id, created = result
        row = self.db.get_entry(entry_id)
        self.after_manual_save(entry_id, created, False)
        self.set_status(f"已从剪贴板导入：{row['term'] if row else ''}")
        return True

    def float_clipboard_import(self) -> None:
        self.import_clipboard()

    def after_manual_save(self, entry_id: int, created: bool, then_explain: bool) -> None:
        """手动录入 / 剪贴板导入后的统一收尾（这两条路径本来就是用户显式动作）。"""
        row = self.db.get_entry(entry_id)
        term = row["term"] if row else ""
        batch = self.db.get_batch(int(row["batch_id"])) if row else None
        note = ("已记录" if created else "已在主题中（重复 +1）") + \
            f" → 《{batch['name'] if batch else ''}》"
        self.main.refresh_batches()
        self.main.refresh_entries()
        self.main.select_entry(entry_id)
        if then_explain:
            self.open_main_window()
            self._explain(entry_id, False)
        self.set_status(f"{note}：{term}")

    def grab_now(self) -> None:
        """Ctrl+Alt+Shift+S：对当前前台窗口立刻尝试一次 UIA 取词（受门控限制）。

        取到选区只会弹出操作浮条 —— 是否记录/解释仍由用户点按钮决定。
        """
        if not self.config.capture_enabled:
            self.set_status("取词已暂停（Ctrl+Alt+Shift+P 恢复）")
            return
        decision = self._apply_gate()
        if not decision.allowed:
            self.set_status(f"当前前台是游戏/全屏或已暂停，已忽略抓取：{decision.label}")
            return
        try:
            x, y = w32.get_cursor_pos()
        except OSError:  # pragma: no cover
            x = y = 0
        self._on_gesture(x, y, "hotkey")
        self.set_status(f"已触发取词…（{decision.target}）")

    def open_main_window(self, *, wizard: bool = False) -> None:
        """把主界面亮出来；``wizard=True`` 时顺带弹一次首次配置向导。

        ``wizard`` 只在「启动后第一次把主界面亮出来」那条路径上传 True
        （见 :meth:`show_on_startup`）：设置页、手动录入后追解释、顶栏「主界面」
        按钮都走默认值，**不会**顺带弹出向导。
        """
        # 打开之前先把「跟到当前页面」的待办落地：列表显示的永远是现在这一页
        apply_follow = getattr(self.main, "apply_pending_follow", None)
        if callable(apply_follow):
            try:
                apply_follow()
            except Exception:  # pragma: no cover - 刷新失败不该挡住打开窗口
                log.exception("主界面刷新失败")
        try:
            self.root.deiconify()
            self.lift_main_window()
        except tk.TclError:  # pragma: no cover
            pass
        if wizard:
            self.maybe_show_setup_wizard()

    def show_on_startup(self) -> None:
        """启动流程亮主界面：与 :meth:`open_main_window` 同一套动作 + 首弹向导。"""
        self.open_main_window(wizard=True)

    def lift_main_window(self) -> None:
        """把主界面提到最前并给焦点（用户显式呼出时才调用）。"""
        try:
            self.root.lift()
            self.root.focus_force()
        except tk.TclError:  # pragma: no cover
            pass

    # ------------------------------------------------- 首次配置向导
    def maybe_show_setup_wizard(self) -> bool:
        """第一次打开主界面时弹一次配置向导；返回是否真的弹了。

        判断只在 :func:`app.ui.setup_wizard.should_show` 里（标记 ``ui.wizard_done``
        + 门控没被硬阻断）：游戏 / 全屏 / 暂停时**不弹**，等下次打开主界面再说。

        向导**只是首次上手引导**：它建不出来（没有真实 Tk 环境 / 控件异常）时
        记一条日志就返回 ``False``，绝不让「主界面已经亮出来了、顺手弹个向导」
        反过来把 ``request_open_main()`` 整条路径掀翻 —— 那会让双击快捷方式
        什么也打不开。
        """
        try:
            locked = bool(self.gate_controller.is_locked())
        except Exception:  # pragma: no cover - 门控替身
            locked = False
        if not wizard_should_show(self.config, gate_locked=locked):
            return False
        try:
            self.open_setup_wizard()
        except Exception:  # pragma: no cover - 无真实 Tk（headless / 控件异常）
            log.exception("首次配置向导没能建出来（不影响主界面）")
            self._wizard_win = None
            return False
        return True

    def open_setup_wizard(self) -> None:
        """造出向导窗（已经开着就抬到前面）。

        **绝不调用** :meth:`open_main_window`（那会在末尾回调回来 ⇒ 无限递归），
        也**不重复 deiconify / lift 主窗**（调用方 :meth:`show_on_startup` 刚做过，
        再来一次会让「打开主界面 = deiconify 一次」这条既有约定被破坏）：
        这里只造窗，然后把向导自己抬到最前。
        """
        win = self._wizard_win
        if win is not None:
            try:
                if win.win.winfo_exists():
                    win.win.lift()
                    win.win.focus_force()
                    return
            except tk.TclError:  # pragma: no cover - 已经关了
                pass
            self._wizard_win = None
        self._wizard_win = SetupWizard(self.root, self)
        try:  # 主窗刚刚亮出来，向导要压在它上面
            self._wizard_win.win.lift()
            self._wizard_win.win.focus_force()
        except tk.TclError:  # pragma: no cover - 极端环境
            pass

    # --------------------------------------------- 外部呼出（命名事件）
    def attach_activation(self, receiver) -> bool:
        """挂上「重复启动 → 呼出主界面」的事件接收器，并起 250ms 非阻塞轮询。

        接收器为 ``None`` / 创建失败都只是「没有外部呼出」这一条体验通路：
        主界面、门控、热键、取词**一律不受影响**（fail-open，与单实例同一取向）。
        """
        self._activation = receiver
        if receiver is not None:
            try:
                receiver.create()
            except Exception:  # pragma: no cover - 极端环境：照常启动
                log.exception("呼出事件创建失败（不影响其它功能）")
        self._schedule_activation_poll()
        return self._activation is not None

    def _schedule_activation_poll(self) -> None:
        """安排下一次轮询（正在退出 / 窗口已销毁时静默跳过）。"""
        if self._closing:
            return
        try:
            self._activation_after_id = self.root.after(ACTIVATION_POLL_MS,
                                                        self._poll_activation)
        except Exception:  # pragma: no cover - Tk 已销毁
            self._activation_after_id = None

    def _poll_activation(self) -> bool:
        """非阻塞检查一次外部呼出请求；返回本次是否真的打开了主界面。"""
        if self._closing:
            return False
        opened = False
        try:
            opened = self._drain_activation()
        except Exception:  # pragma: no cover - 轮询绝不允许打断主循环
            log.exception("处理呼出请求失败")
        finally:
            self._schedule_activation_poll()
        return opened

    def _drain_activation(self) -> bool:
        """消费事件 + 处理暂存的 pending（硬阻断下继续保留，等解除后兑现）。"""
        requested = bool(self._pending_open_main)
        source = self._pending_open_main_source if requested else ""
        receiver = self._activation
        if receiver is not None:
            try:
                if receiver.poll():
                    requested = True
                    # 事件呼出 = 重复启动：兑现时按「已有实例」提示一次
                    source = "activation"
            except Exception:  # pragma: no cover - 事件异常不影响主界面
                log.exception("读取呼出事件失败")
        if not requested:
            return False
        return self.request_open_main(source or "activation")

    def request_open_main(self, source: str = "") -> bool:
        """请求打开主界面：**硬阻断下只保留一个 pending**。

        ``source="activation"``（重复双击 exe）**只恢复浮窗**：已展开的不折叠、
        已关闭的由此次显式操作找回、绝不自动开主界面。
        ``tray``（托盘「打开词典」）与 ``argv``（显式 ``--open-main``）仍然是
        用户显式打开主界面：前台是游戏 / 全屏 / 用户暂停时**一次 deiconify 都
        不做**，只记住请求，门控解除后由同一个 250ms 轮询兑现（多次只兑现一次）。

        ``source="activation"`` 在**真的显示出来**之后才提示一次「已在运行」；
        硬阻断期间提示同样暂存。其它来源不提示。
        """
        if source:
            self._pending_open_main_source = source
        try:
            decision = self.gate.evaluate()
        except Exception:
            # 无法确认前台安全时保留请求，不能借启动入口绕开游戏限制。
            self._pending_open_main = True
            log.exception("打开主界面前的门控判定失败，暂缓显示")
            return False
        if bool(getattr(decision, "hard_blocked", False)):
            if not self._pending_open_main:
                log.info("呼出请求暂存（当前前台硬阻断：%s）",
                         getattr(decision, "reason", ""))
            self._pending_open_main = True
            return False
        self._pending_open_main = False
        if source == "activation":
            log.info("重复启动呼出：只恢复浮窗（不打开主界面）")
            shown = self.restore_floating_ui()
            if not shown:
                # 门控轮询可能尚未处理刚离开游戏的状态；保留显式呼出请求。
                self._pending_open_main = True
                return False
            self._notify_already_running()
            return bool(shown)
        log.info("打开主界面（来源：%s）", source or "unknown")
        # 双击 exe / 首次呼出都走这里：第一次亮主界面时顺带弹一次配置向导
        # （``ui.wizard_done`` 看过就不再弹；硬阻断的请求根本到不了这一行）。
        self.open_main_window(wizard=True)
        return True

    def close_activation(self) -> bool:
        """收尾：停掉轮询并释放事件句柄（幂等；退出路径调用）。"""
        after_id, self._activation_after_id = self._activation_after_id, None
        if after_id is not None:
            cancel = getattr(self.root, "after_cancel", None)
            if callable(cancel):
                try:
                    cancel(after_id)
                except Exception:  # pragma: no cover - 窗口已销毁
                    pass
        receiver, self._activation = self._activation, None
        if receiver is None:
            return False
        try:
            receiver.close()
        except Exception:  # pragma: no cover - 关闭失败不该影响退出
            log.exception("关闭呼出事件失败")
        return True

    def open_settings(self) -> None:
        if self._settings_win is not None and self._settings_win.win.winfo_exists():
            self._settings_win.win.lift()
            self._settings_win.win.focus_force()
            return
        # 顺序要紧：先把**主窗**抬起来（设置窗是它的子窗口），再建设置窗，
        # 最后把设置窗抬到自己上面 —— 旧实现先建窗再 lift 主窗，结果设置窗
        # 被主窗压在下面，用户点了「设置」像是没反应。
        # 主窗可能是 withdraw（默认收起）：只 ``lift`` 不 ``deiconify`` 的话
        # 设置窗建在一个不可见的父窗口上，用户什么都看不到 —— 所以这里走
        # 完整的 ``open_main_window``（deiconify + lift）。浮窗显隐不受影响。
        self.open_main_window()
        self._settings_win = SettingsDialog(self.root, self)
        self._lift_settings_window()

    def _lift_settings_window(self) -> bool:
        """把设置窗抬到最前并给焦点（创建之后调用一次；窗口已销毁时安全返回）。"""
        win = getattr(self._settings_win, "win", None)
        if win is None:
            return False
        try:
            win.lift()
            win.focus_force()
        except tk.TclError:  # pragma: no cover - 窗口已销毁
            return False
        return True

    # ------------------------------------------------------ 浏览范围（浮窗⇄侧栏）
    def browse_scope(self) -> int | None:
        """当前**浏览**的主题 id（``None`` = 跟随当前阅读页）。

        只有一份状态（主界面持有），浮窗顶栏的主题清单与主界面左侧栏共用它：
        两边永远显示同一组词。浏览**只影响显示**，新词的保存位置永远由来源页面
        自动归属决定（见 ``CaptureService`` 的页面范围解析）。
        """
        main = getattr(self, "main", None)
        getter = getattr(main, "browse_batch_id", None)
        if not callable(getter):
            return None
        try:
            value = getter()
        except Exception:  # pragma: no cover - 主界面还没建好
            return None
        return None if value in (None, "") else int(value)

    def set_browse_scope(self, batch_id: int | None) -> bool:
        """切换浏览主题（浮窗 / 主界面 / 托盘都走这里），返回是否真的变了。"""
        main = getattr(self, "main", None)
        setter = getattr(main, "set_browse_scope", None)
        if not callable(setter):
            return False
        try:
            return bool(setter(batch_id))
        except Exception:  # pragma: no cover - 主界面状态异常不该炸调用方
            log.exception("切换浏览主题失败")
            return False

    def release_browse_scope(self) -> bool:
        """把浏览范围交还「跟随当前阅读页」（**纯内存**，不重建主界面控件）。

        浮窗换页（``ReadingPanel.follow_page_change``）走这里：换页等于结束手工
        浏览，下一次刷新（用户开始划选时）取的就是新页面的范围；主界面自己的
        列表刷新仍然按 ``follow_current_page`` 的延迟策略 —— 两条路径共用同一份
        ``_browse_batch_id``，永远同步。
        """
        main = getattr(self, "main", None)
        release = getattr(main, "release_browse_scope", None)
        if not callable(release):
            return False
        try:
            return bool(release())
        except Exception:  # pragma: no cover - 主界面还没建好 / 状态异常
            log.exception("交还浏览范围失败")
            return False

    def on_browse_scope_changed(self) -> None:
        """浏览范围变了：让浮窗词表立刻跟着换（主界面自己会刷新侧栏与列表）。"""
        panel = getattr(self, "reading_panel", None)
        refresher = getattr(panel, "refresh_terms", None)
        if not callable(refresher):
            return
        try:
            refresher(force=True)
        except tk.TclError:  # pragma: no cover - 窗口已销毁
            log.exception("刷新浮窗词表失败")

    def open_concept_map(self) -> None:
        """打开参考关系图（**单例**：已经开着就抬到最前，不重建、不动父窗口）。

        旧实现有三个直接缺陷：新窗口建好之后被主窗盖住（没有 lift）、
        打开时会先把父窗口藏起来（父窗口是不可见的，建出来的子窗口也跟着看不见）、
        画布首次 ``<Configure>`` 之前 ``winfo_width`` 只有 1 → 什么节点都画不出来。
        现在：主窗口**不动**（只 lift 导图窗），窗口自己按当前画布尺寸重画。
        """
        #: 勾选范围（K1）：主界面勾上的词才进这张图 —— 词条攒多了之后，
        #: 最后总结只要几个关键术语，不必把整个主题都画出来。
        #: 主窗口存的是 ``self.main``（见本文件 ``self.main = MainWindow(...)``）——
        #: 早先这里写的是 ``self.main_window``，那个属性根本不存在：``getattr`` 的
        #: 兜底让它静默退化成「一个词都没勾」，勾选筛选于是在真机上完全不起作用。
        only_ids = []
        full_ids = []
        browse_id = None
        main = getattr(self, "main", None)
        if main is not None:
            ids = getattr(main, "checked_ids", None)
            if callable(ids):
                only_ids = list(ids())
            scope = getattr(main, "checked_scope_ids", None)
            if callable(scope):
                full_ids = list(scope())
            #: 主界面正在看的主题：勾选是按它勾的，图必须落在这个主题上。
            raw_browse = getattr(main, "_browse_batch_id", None)
            if raw_browse is not None:
                browse_id = int(raw_browse)
        win = self._map_win
        if win is not None and win.alive():
            win.lift()
            apply_ids = getattr(win, "apply_only_ids", None)
            if callable(apply_ids):
                #: 重新点「导图」= 按**现在的勾选**重新定范围（取消几个词后再点一下，
                #: 图里就只剩勾上的那些）。窗口本来就在，只是换了个范围。
                apply_ids(only_ids, full_ids=full_ids)
                #: 一并跟到主界面正在看的主题：勾选是按那个主题勾的。
                win.refresh(topic_id=browse_id)
            self.set_status("参考关系图已在最前")
            return
        self._map_win = ConceptMapWindow(self.root, self,
                                         only_ids=only_ids, full_ids=full_ids)
        if browse_id is not None:
            self._map_win.refresh(topic_id=browse_id)
        self.set_status("参考关系图：AI 参考关系（带依据与原文片段）")

    def _handle_map_result(self, payload) -> None:
        """后台生成的参考关系回到 UI 线程：只交给**当前活着的**导图窗。

        窗口已经关掉 → 结果直接丢弃（缓存已经在服务里按主题 + 指纹写好，
        下次打开命中缓存立刻显示，**不会**复活旧图、**不会**串到别的主题）。
        """
        win = self._map_win
        if win is None or not win.alive():
            return
        try:
            win.on_map_result(payload)
        except tk.TclError:  # pragma: no cover - 窗口正在销毁
            log.debug("导图窗口已销毁，丢弃结果", exc_info=True)

    def notify_map_entries_changed(self) -> None:
        """词条新增 / 编辑 / 删除 / 解释完成：让打开的图窗按当前库**本地**跟上。

        由 MainWindow 的既有刷新路径（``refresh_entries``）调用：图窗没开就是
        零开销；开着时只做本地重读 + 丢弃过期图与请求关联 + 一句短状态提示，
        **绝不**在这里自动发起任何 LLM 请求（用户点图窗里的生成按钮才请求）。
        """
        win = getattr(self, "_map_win", None)
        if win is None:
            return
        try:
            if not win.alive():
                return
            win.on_entries_changed()
        except tk.TclError:  # pragma: no cover - 窗口正在销毁
            log.debug("导图窗口已销毁，跳过词条变化通知", exc_info=True)


    def on_settings_changed(self) -> None:
        self._apply_gate(force=True)
        self.apply_topmost()
        self.main.update_status()
        self.set_status("设置已更新")

    def on_main_close(self) -> None:
        """主词典窗口的「×」：**只收起窗口，不退出程序**。

        取词、浮窗与后台服务全部继续运行；再打开主界面用
        Ctrl+Alt+Shift+D 或浮窗菜单里的「打开词典」。
        真正退出只有菜单 / 托盘菜单里的「退出」与 Ctrl+Alt+Shift+Q。
        """
        try:
            self.root.withdraw()
        except tk.TclError:  # pragma: no cover
            return
        self.set_status("词典主界面已收起，程序仍在后台运行")

    # ------------------------------------------------------------- 退出
    def quit(self) -> None:
        if self._closing:
            return
        self._closing = True
        log.info("正在退出…")
        self._uia_wanted = False
        try:
            # 托盘先删（否则退出后图标会残留到下一次交互）
            self.close_tray()
        except Exception:  # pragma: no cover
            log.exception("关闭托盘失败")
        try:
            # 面板是同一个窗口承担两个角色：只销毁一次（第二次是幂等的空操作）
            self.selection_bar.destroy()
        except tk.TclError:  # pragma: no cover
            pass
        self._gesture_stop.set()

        try:
            self.mouse_hook.stop()
        except Exception:  # pragma: no cover
            log.exception("停止鼠标钩子失败")
        try:
            self.hotkeys.stop()
        except Exception:  # pragma: no cover
            log.exception("停止热键失败")
        try:
            self.watcher.stop()
        except Exception:  # pragma: no cover
            log.exception("停止前台监视失败")
        try:
            self.uia.stop()
        except Exception:  # pragma: no cover
            log.exception("停止 UIA helper 失败")
        try:
            self.db.close()
        except Exception:  # pragma: no cover
            log.exception("关闭数据库失败")
        try:
            # 收尾：停掉呼出轮询并释放命名事件句柄（幂等）
            self.close_activation()
        except Exception:  # pragma: no cover
            log.exception("关闭呼出事件失败")
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def run_selftest(app: App) -> int:
    """启动自检：装配是否成功、服务是否起来、面板是否置顶且不抢焦点、呼出热键可用。"""
    checks: list[tuple[str, bool, str]] = []
    checks.append(("数据库", app.db is not None, str(paths.db_path())))
    checks.append(("主窗口", bool(app.root.winfo_exists()), "ok"))
    panel = app.reading_panel
    checks.append(("阅读面板（小方块 + 浮窗同一窗口）", panel.hwnd() != 0,
                   f"hwnd={panel.hwnd()} 状态={panel.mode_name()}"))
    checks.append(("面板不抢焦点", w32.is_no_activate(panel.hwnd()), "WS_EX_NOACTIVATE"))
    checks.append(("浮窗默认置顶", bool(panel.topmost()) == app.effective_topmost(),
                   f"浮窗topmost={panel.topmost()} 硬阻断={app.gate_controller.is_locked()}"))
    checks.append(("面板折叠成小方块", True,
                   f"mode={panel.mode_name()} 可见={panel.visible}"))
    checks.append(("追问服务", True,
                   f"就绪={app.chat_service.is_ready()[0]} 在途={app.chat_service.inflight_count()}"))
    decision = app.gate_decision()
    checks.append(("前台门控", True,
                   f"locked={app.gate_controller.is_locked()} reason={decision.reason} "
                   f"允许读取={decision.allows_read()} 允许浮层={decision.allows_overlay()}"))
    checks.append(("鼠标钩子", app.mouse_hook.running or app.gate_controller.is_locked(),
                   f"running={app.mouse_hook.running} locked={app.gate_controller.is_locked()} "
                   f"err={app.mouse_hook.last_error()}"))
    # 关键点：单独校验「呼出键」，其余热键成功不能算通过
    checks.append(("呼出热键 Ctrl+Alt+Shift+D", app.recall_hotkey_ok(),
                   f"registered={app.hotkeys.registered} failed={app.hotkeys.failed}"))
    app.uia.set_enabled(True)  # 自检只看 helper 本身是否可用
    checks.append(("UIA helper", app.uia.ping(10.0), f"pid={app.uia.pid()}"))
    checks.append(("前台监视", app.watcher.is_alive(), "thread"))
    ok = all(c[1] for c in checks)
    for name, good, detail in checks:
        print(f"[{'PASS' if good else 'FAIL'}] {name}: {detail}")
    return 0 if ok else 1


def build_app(root: tk.Tk, window_title: str | None = None) -> App:
    theme.init(root, w32.get_system_dpi())
    db = Database(paths.db_path())
    config = Config(db)
    app = App(root, db, config)
    # 主界面**不再挂原生菜单栏**：功能键只在自绘工具条里出现一组，
    # 「退出」也只有工具条右端那一个入口（见 MainWindow._build_toolbar）。
    # 词典主窗口默认 **withdraw**：阅读时只留小方块在屏幕上；
    # Ctrl+Alt+Shift+D、面板菜单「打开词典」都能把它叫回来。
    try:
        root.withdraw()
    except tk.TclError:  # pragma: no cover
        pass
    if window_title:
        # 仅用于界面截图/自动化：给窗口一个可定位的标题（默认标题不变）
        root.title(window_title)
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="探索词典")
    parser.add_argument("--selftest", action="store_true", help="启动自检后退出")
    parser.add_argument("--console-log", action="store_true", help="同时输出日志到控制台")
    parser.add_argument("--window-title", default="", help="覆盖主窗口标题（截图/自动化用）")
    parser.add_argument("--open-main", action="store_true",
                        help="启动后打开词典主界面（双击 exe / 重复启动呼出用）")
    args = parser.parse_args(argv)

    setup_logging(console=args.console_log or args.selftest)
    log.info("=== 探索词典启动 (pid=%s) ===", os.getpid())
    log.info("项目目录: %s", paths.project_root())

    # 单实例闸门：**必须在建 Tk / 建库 / 装钩子 / 注册热键之前**。
    # ``python -m app.main`` 这条入口过去完全没有判重：双击几次就有几份实例
    # 同时抢独占热键、并发写库。bootstrap → app.main 是同进程的两层入口，
    # 闸门被上层持有时这里只**复用**，也不替上层释放（who acquires, who releases）。
    already_held = process_guard() is not None
    guard, exit_code = acquire_process_guard()
    if exit_code is not None:
        log.warning("已经有一个探索词典实例在运行：本次启动退出（避免抢热键 / 并发写库）")
        return int(exit_code)
    try:
        return _run_ui(args)
    finally:
        if not already_held:
            release_process_guard(guard)


def _attach_activation(app) -> bool:
    """创建「呼出」命名事件接收器并挂到 App。

    没有 ``attach_activation`` 的替身（自动化测试）直接跳过；事件通道不可用时
    只写日志，绝不阻止启动（``--diagnose`` 不经过这里）。
    """
    attach = getattr(app, "attach_activation", None)
    if not callable(attach):
        return False
    try:
        from .activation import ActivationReceiver

        receiver = ActivationReceiver()
    except Exception:  # pragma: no cover - 事件通道不可用不影响启动
        log.exception("呼出事件接收器创建失败")
        return False
    try:
        return bool(attach(receiver))
    except Exception:  # pragma: no cover
        log.exception("呼出事件接收器挂载失败")
        close = getattr(receiver, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        return False


def _request_open_main(app, source: str = "argv") -> bool:
    """首次 ``--open-main``：与事件呼出走**同一条**门控 / pending 路径。"""
    request = getattr(app, "request_open_main", None)
    if not callable(request):
        return False
    try:
        return bool(request(source))
    except Exception:  # pragma: no cover - 打开失败不影响后台运行
        log.exception("打开主界面失败")
        return False


def _run_ui(args) -> int:
    """拿到单实例闸门之后才执行的启动主体（Tk / 数据库 / 钩子都在这里建）。"""
    try:
        w32.enable_dpi_awareness()
    except Exception:  # pragma: no cover
        log.exception("DPI 感知设置失败")

    root = tk.Tk()
    # **先隐藏再装配**：build_app 里会创建主界面与阅读面板，任何一次映射都可能让
    # 词典主窗口在屏幕上闪一下（启动闪窗）。所以 Tk 根窗口一建立就 withdraw，
    # 之后再由 build_app / App.start 按用户偏好与门控决定显示什么。
    # build_app 里保留同一份 withdraw 作为防御（两条路径都隐藏，幂等）。
    try:
        root.withdraw()
    except tk.TclError:  # pragma: no cover
        pass
    try:
        app = build_app(root, args.window_title or None)
    except Exception:
        log.exception("初始化失败")
        if not getattr(sys, "frozen", False):
            messagebox.showerror("探索词典", "初始化失败，请查看 data/logs/app.log。")
        root.destroy()
        return 2

    app.start()

    # 呼出通道：root 与 app 都建好之后才挂接收器（诊断路径根本不走这里）。
    # 轮询是 ``root.after(250ms)`` 的非阻塞检查 —— 没有请求时不碰任何窗口。
    _attach_activation(app)

    # ``--open-main``（双击 exe）会把主界面显示到最前，前台随即变成我们自己的
    # 窗口 —— 之后 watcher 会跳过自身，回到同一阅读页只能靠 ``resume`` 事件。
    # 所以**在呼出之前**先把当时的阅读来源记下来，并让视图跟到当前页：主界面
    # 打开时列表就已经是这一页的词，而不是上一次会话留下的旧主题。
    try:
        app.capture_service.note_foreground()
        app._follow_page_views()
    except Exception:  # pragma: no cover - 采样失败不影响启动
        log.exception("启动时记录阅读来源失败")

    # 双击 exe（desktop_entry 追加 --open-main）第一次打开主界面：与事件呼出
    # 同一条门控路径，硬阻断（游戏 / 全屏 / 暂停）下只暂存一个 pending。
    if getattr(args, "open_main", False):
        _request_open_main(app)

    if args.selftest:
        result: dict[str, int] = {}

        def _finish() -> None:
            result["code"] = run_selftest(app)
            app.quit()

        root.after(3500, _finish)
        root.mainloop()
        return result.get("code", 1)

    try:
        root.mainloop()
    except KeyboardInterrupt:  # pragma: no cover
        app.quit()
    return 0


def _excepthook(exc_type, exc, tb) -> None:
    log.error("未捕获异常: %s", "".join(traceback.format_exception(exc_type, exc, tb)))
    try:
        messagebox.showerror("探索词典 — 错误", "".join(
            traceback.format_exception(exc_type, exc, tb))[-1500:])
    except Exception:  # pragma: no cover
        pass


if __name__ == "__main__":
    setup_logging()
    sys.excepthook = _excepthook
    threading.excepthook = lambda a: log.error(
        "线程未捕获异常: %s", "".join(traceback.format_exception(a.exc_type, a.exc_value,
                                                                a.exc_traceback))
    )
    raise SystemExit(main())
