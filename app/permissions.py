"""允许/禁止取词的应用清单（**默认放开，只挡游戏**）。

设计变更（用户反馈「阅读白名单挡太多正常软件」）
------------------------------------------------
旧版本要求前台进程必须命中一份「阅读白名单」（浏览器 / Office / PDF / 记事本…）
才允许取词，导致任何未列入的普通软件（微信、QQ、各类阅读器、公司内部工具…）
都被静默拦下。现在改为**从简免配置**的默认：

* 用户主动暂停 / 手动游戏模式            → 一律禁止（用户意图最高优先级）；
* 已知游戏 / 游戏平台进程（本文件清单）  → 禁止；
* 全屏前台窗口                          → 保守禁止（可能是全屏游戏/视频）；
* 桌面 / 任务栏 / 本程序自身窗口          → 不取词；
* **其它一切普通窗口（含未知程序）**      → 允许尝试 UIA 读取。

读取方式依旧是标准 UI Automation ``TextPattern``：不注入进程、不读其它进程内存、
不模拟 Ctrl+C、不读写剪贴板；读到就读到，读不到就什么都不做。

``READING_APPS`` 的用途已经改变
--------------------------------
它**不再参与运行时放行判定**，只保留两个用途：

1. 给状态栏/提示提供更好读的中文应用名（未知进程回退到 exe 名）；
2. ``app/gui_preflight.py`` 的自动化 GUI 预检 —— 那是「自动化脚本是否可以建窗口」
   的**严格**闸门，故意比产品运行时更保守，不随本模块放开而放宽。
"""
from __future__ import annotations

import os

#: 仅用于「显示名」与自动化 GUI 预检，**不用于运行时放行**。
READING_APPS: dict[str, str] = {
    # 浏览器
    "msedge.exe": "Microsoft Edge",
    "chrome.exe": "Google Chrome",
    "firefox.exe": "Mozilla Firefox",
    "brave.exe": "Brave",
    "vivaldi.exe": "Vivaldi",
    "opera.exe": "Opera",
    "chromium.exe": "Chromium",
    "librewolf.exe": "LibreWolf",
    "waterfox.exe": "Waterfox",
    "360se.exe": "360 安全浏览器",
    "360chrome.exe": "360 极速浏览器",
    "qqbrowser.exe": "QQ 浏览器",
    "sogouexplorer.exe": "搜狗浏览器",
    "maxthon.exe": "傲游浏览器",
    # 文档 / 办公
    "winword.exe": "Microsoft Word",
    "wps.exe": "WPS Office",
    "kwps.exe": "WPS 文字",
    "powerpnt.exe": "Microsoft PowerPoint",
    "wpp.exe": "WPS 演示",
    "onenote.exe": "Microsoft OneNote",
    # PDF
    "acrobat.exe": "Adobe Acrobat",
    "acrord32.exe": "Adobe Acrobat Reader",
    "sumatrapdf.exe": "SumatraPDF",
    "foxitreader.exe": "Foxit Reader",
    "foxitphantompdf.exe": "Foxit PDF Editor",
    "pdfxedit.exe": "PDF-XChange Editor",
    "pdfxcview.exe": "PDF-XChange Viewer",
    "nitro.exe": "Nitro PDF",
    "mupdf.exe": "MuPDF",
    # 文本 / 阅读器 / 电子书
    "notepad.exe": "记事本",
    "notepad++.exe": "Notepad++",
    "sublime_text.exe": "Sublime Text",
    "code.exe": "Visual Studio Code",
    "typora.exe": "Typora",
    "obsidian.exe": "Obsidian",
    "gvim.exe": "GVim",
    "kindle.exe": "Kindle",
    "calibre.exe": "Calibre",
    "calibre-ebook-viewer.exe": "Calibre 电子书阅读器",
    "ebook-viewer.exe": "Calibre 电子书阅读器",
    "digitaleditions.exe": "Adobe Digital Editions",
    "thorium.exe": "Thorium Reader",
}

#: 已知游戏 / 游戏平台进程：**运行时也一律禁止取词**（不只是中文提示）。
#: 用户此前明确要求「游戏避让优先级」保持不变，War Thunder 的 ``aces.exe``
#: 即在其列（本机实际在玩）。浏览器里的窗口化网页游戏仍然只能靠
#: 手动「游戏模式」兜底 —— 见 README 的已知边界。
GAME_APPS: frozenset[str] = frozenset({
    # 平台 / 启动器
    "steam.exe", "steamwebhelper.exe", "steamservice.exe",
    "epicgameslauncher.exe", "epicwebhelper.exe",
    "galaxyclient.exe", "galaxyclientservice.exe", "goggalaxy.exe",
    "battle.net.exe", "agent.exe", "blizzard error.exe",
    "riotclientservices.exe", "leagueclient.exe", "leagueclientux.exe",
    "wegame.exe", "tencentdl.exe", "tgp_daemon.exe",
    "origin.exe", "eadesktop.exe", "eabackgroundservice.exe",
    "ubisoftconnect.exe", "upc.exe", "uplay.exe",
    "playnite.exe", "itch.exe", "battle.net helper.exe",
    # 常见游戏本体 / 引擎
    "war thunder.exe", "launcher.exe", "aces.exe", "warthunder.exe",
    "genshinimpact.exe", "yuanshen.exe", "starrail.exe", "wutheringwaves.exe",
    "dota2.exe", "cs2.exe", "csgo.exe", "pubg.exe", "minecraft.exe", "javaw.exe",
    "valorant.exe", "valorant-win64-shipping.exe",
    "eldenring.exe", "cyberpunk2077.exe", "gta5.exe", "rdr2.exe",
    "destiny2.exe", "apex_legends.exe", "rainbowsix.exe", "r5apex.exe",
    "overwatch.exe", "worldofwarcraft.exe", "ffxiv_dx11.exe",
    "hoyoplay.exe", "launcher_loader.exe",
})


def exe_name(path_or_name: str) -> str:
    """把进程路径或名字统一成小写 exe 名。"""
    return os.path.basename((path_or_name or "").strip()).lower()


def is_known_game(exe: str) -> bool:
    return exe_name(exe) in GAME_APPS


def display_name(exe: str) -> str:
    """给状态栏用的可读名字：优先已知中文名，其次 exe 名。"""
    name = exe_name(exe)
    if not name:
        return ""
    return READING_APPS.get(name, name)
