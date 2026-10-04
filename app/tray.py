"""系统托盘图标（pystray / Pillow，可选依赖）。

职责边界（重要）
----------------
* 菜单四项：**打开词典 / 暂停取词 / 浮窗置顶（勾选） / 退出**；
* 菜单回调运行在托盘的**消息循环线程**里，只把事件名投进 ``app._ui_q``，
  **绝不**调用 Tk / 窗口 / 数据库 —— 所有真实动作都在 UI 线程的
  ``App._handle_event`` 里执行（打开走 ``request_open_main`` 的硬门控路径）；
* ``pystray`` / ``PIL`` 全部**延迟导入**：没装依赖只是没有托盘，
  取词、热键、主界面一律不受影响；
* ``icon_factory`` / ``image_loader`` 可注入，回归测试用假图标验证
  「跨线程只入队」与「stop 清理」，不加载真实 pystray。
"""
from __future__ import annotations

import threading

from . import paths
from .logging_setup import get_logger

log = get_logger("tray")

#: 投进 ``App._ui_q`` 的事件名（与 ``App._handle_event`` 一一对应）
EVENT_OPEN = "tray_open"
EVENT_TOGGLE_PAUSE = "tray_toggle_pause"
EVENT_TOPMOST = "tray_topmost"
EVENT_QUIT = "tray_quit"

TITLE = "探索词典"

MENU_OPEN = "打开词典"
MENU_PAUSE = "暂停取词"
MENU_RESUME = "恢复取词"
MENU_TOPMOST = "浮窗置顶"
MENU_QUIT = "退出"


def load_icon_image():
    """加载 ``assets/app.ico``（frozen 下从 ``_runtime`` 资源根找）。

    Pillow 缺失 / 文件缺失时返回 ``None``：调用方据此判定托盘不可用。
    """
    try:
        from PIL import Image
    except Exception:
        log.warning("未安装 Pillow，托盘图标不可用")
        return None
    path = paths.app_icon_path()
    try:
        if path is not None and path.exists():
            with Image.open(path) as image:
                return image.copy()
    except Exception:  # pragma: no cover - 坏图标不该阻止启动
        log.debug("托盘图标加载失败：%s", path, exc_info=True)
    try:
        return Image.new("RGBA", (64, 64), (30, 80, 140, 255))
    except Exception:  # pragma: no cover
        return None


class TrayIcon:
    """一个常驻托盘图标；``start()`` / ``stop()`` 幂等。"""

    def __init__(self, app, *, title: str = TITLE, icon_factory=None,
                 image_loader=None):
        self.app = app
        self.title = str(title)
        self._icon_factory = icon_factory
        self._image_loader = image_loader
        self._icon = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stopped = False

    # ------------------------------------------------------------- 生命周期
    def start(self) -> bool:
        """创建图标并起后台消息循环；依赖不可用时返回 ``False``。"""
        with self._lock:
            if self._stopped:
                return False
            if self._icon is not None:
                return True
            icon = self._build_icon()
            if icon is None:
                return False
            self._icon = icon
        thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread = thread
        thread.start()
        return True

    def _run(self) -> None:
        icon = self._icon
        try:
            icon.run()
        except Exception:  # pragma: no cover - 托盘线程异常不影响主程序
            log.exception("托盘消息循环异常退出")
        finally:
            with self._lock:
                if self._icon is icon:
                    self._icon = None

    def stop(self) -> None:
        """删除图标并等消息循环退出（幂等）。"""
        with self._lock:
            self._stopped = True
            icon, self._icon = self._icon, None
        if icon is None:
            return
        try:
            icon.stop()
        except Exception:  # pragma: no cover
            log.debug("停止托盘图标失败", exc_info=True)
        thread = self._thread
        if thread is not None and thread.is_alive() \
                and thread is not threading.current_thread():
            thread.join(2.0)

    def notify(self, message: str, title: str | None = None) -> bool:
        """弹一次气泡（失败静默：提示只是体验增强）。"""
        with self._lock:
            icon = self._icon
        if icon is None:
            return False
        try:
            icon.notify(str(message), self.title if title is None else str(title))
        except Exception:  # pragma: no cover
            log.debug("托盘通知失败", exc_info=True)
            return False
        return True

    def running(self) -> bool:
        with self._lock:
            return self._icon is not None

    # --------------------------------------------------------------- 构造
    def _build_icon(self):
        factory = self._icon_factory
        if factory is None:
            try:
                import pystray
            except Exception:
                log.warning("未安装 pystray，托盘不可用")
                return None
            factory = pystray.Icon
        image = self._load_image()
        if image is None:
            return None
        menu = self._build_menu()
        try:
            return factory(self.title, image, self.title, menu)
        except TypeError:  # pragma: no cover - 极简替身只接受 (name, image)
            return factory(self.title, image)

    def _load_image(self):
        if self._image_loader is not None:
            return self._image_loader()
        return load_icon_image()

    def _build_menu(self):
        try:
            import pystray
        except Exception:  # pragma: no cover - 注入 factory 时不依赖 pystray
            return None
        return pystray.Menu(
            pystray.MenuItem(MENU_OPEN, self._on_open, default=True),
            pystray.MenuItem(lambda item: MENU_RESUME if self._paused() else MENU_PAUSE,
                             self._on_toggle_pause,
                             checked=lambda item: self._paused()),
            pystray.MenuItem(MENU_TOPMOST, self._on_toggle_topmost,
                             checked=lambda item: self._topmost_consent()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(MENU_QUIT, self._on_quit),
        )

    # --------------------------------------------------------------- 回调
    def _paused(self) -> bool:
        """暂停状态（只读配置，不碰 Tk；读不到按「未暂停」）。"""
        try:
            return not bool(self.app.capture_enabled())
        except Exception:  # pragma: no cover
            return False

    def _topmost_consent(self) -> bool:
        """浮窗置顶授权（只读设置，不碰 Tk；未授权 / 读不到一律不勾选）。"""
        config = getattr(self.app, "config", None)
        try:
            return config.overlay_topmost_consent is True
        except Exception:  # pragma: no cover
            return False

    def _emit(self, event: str) -> None:
        """**唯一**的线程出口：把事件投进 UI 队列。"""
        q = getattr(self.app, "_ui_q", None)
        if q is None:
            return
        try:
            q.put_nowait((event, None))
        except Exception:  # pragma: no cover - 队列满 / 已关闭
            log.debug("托盘事件投递失败：%s", event, exc_info=True)

    def _on_open(self, icon=None, item=None) -> None:
        self._emit(EVENT_OPEN)

    def _on_toggle_pause(self, icon=None, item=None) -> None:
        self._emit(EVENT_TOGGLE_PAUSE)

    def _on_toggle_topmost(self, icon=None, item=None) -> None:
        """浮窗置顶勾选项：**显式点击 = 一次授权 / 撤销**，真正的切换在 UI 线程。"""
        self._emit(EVENT_TOPMOST)

    def _on_quit(self, icon=None, item=None) -> None:
        self._emit(EVENT_QUIT)
