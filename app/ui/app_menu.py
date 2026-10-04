"""应用菜单：主词典窗口的菜单栏 + 面板顶栏「菜单」弹出的同一份定义。

用户要求「保证随时可明确退出」，因此菜单项固定包含：

    打开词典 / 设置 / 暂停取词 / 游戏模式 / 退出

``menu_items(app)`` 是**纯函数**（返回 ``(id, 标签, 回调, 是否勾选)``），
不创建任何控件，便于零窗口回归测试；``build_menu`` 才真正建 Tk ``Menu``。
"""
from __future__ import annotations

import tkinter as tk

from . import theme

#: 菜单项 id（顺序即显示顺序）
MENU_OPEN = "open"
MENU_SETTINGS = "settings"
MENU_PAUSE = "pause"
MENU_GAME_MODE = "game_mode"
MENU_QUIT = "quit"


def menu_items(app) -> list[tuple[str, str, object, bool]]:
    """返回 ``[(id, 标签, 回调, 是否已勾选), ...]``（纯数据，不碰 Tk）。"""
    paused = not bool(app.capture_enabled())
    game_mode = bool(app.game_mode())
    return [
        (MENU_OPEN, "打开词典", app.open_main_window, False),
        (MENU_SETTINGS, "设置…", app.open_settings, False),
        (MENU_PAUSE, "恢复取词" if paused else "暂停取词", app.toggle_capture, paused),
        (MENU_GAME_MODE, "游戏模式", app.toggle_game_mode, game_mode),
        (MENU_QUIT, "退出", app.quit, False),
    ]


def build_menu(parent: tk.Misc, app, *, tearoff: bool = False) -> tk.Menu:
    """构造一份菜单（主窗口菜单栏 / 面板弹出菜单共用同一实现）。"""
    menu = tk.Menu(parent, tearoff=tearoff, bg=theme.PANEL, fg=theme.TEXT,
                   activebackground=theme.ACCENT, activeforeground=theme.BG,
                   bd=0, relief="flat", activeborderwidth=0,
                   font=theme.font(9))
    for item_id, label, command, checked in menu_items(app):
        if item_id in (MENU_PAUSE, MENU_GAME_MODE):
            var = tk.BooleanVar(master=parent, value=bool(checked))
            menu.add_checkbutton(label=label, variable=var, command=command,
                                 selectcolor=theme.PANEL)
        elif item_id == MENU_QUIT:
            menu.add_separator()
            menu.add_command(label=label, command=command)
        else:
            menu.add_command(label=label, command=command)
    return menu


def attach_menubar(root: tk.Tk, app) -> tk.Menu:
    """给主词典窗口装菜单栏（主窗口默认 withdraw，但菜单是「随时可退出」的兜底）。"""
    menu = build_menu(root, app)
    try:
        root.configure(menu=menu)
    except tk.TclError:  # pragma: no cover
        pass
    return menu


def popup(panel, app) -> tk.Menu:
    """在面板「菜单」按钮下方弹出同一份菜单。"""
    menu = build_menu(panel.win, app, tearoff=True)
    try:
        x = panel.win.winfo_rootx() + max(0, panel.win.winfo_width() - 180)
        y = panel.win.winfo_rooty() + 24
        menu.tk_popup(x, y)
    except tk.TclError:  # pragma: no cover
        pass
    finally:
        try:
            menu.grab_release()
        except tk.TclError:  # pragma: no cover
            pass
    return menu
