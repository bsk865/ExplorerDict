"""Shared application paths. Data defaults beside the executable; an explicit
user-selected location is activated only after a verified shutdown migration.
Packaged scripts remain under the runtime resources directory.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "探索词典"
DB_FILENAME = "explorer_dict.sqlite3"


def is_frozen() -> bool:
    """是否运行在 PyInstaller 冻结环境（每次调用都实时判断，便于测试替换）。"""
    return bool(getattr(sys, "frozen", False))


def _executable_dir() -> Path:
    """frozen 下的「程序所在目录」= ``sys.executable`` 的父目录。"""
    return Path(sys.executable).resolve().parent


def project_root() -> Path:
    """数据根目录（frozen = exe 所在目录；源码 = app/ 的上一级）。"""
    if is_frozen():
        return _executable_dir()
    return Path(__file__).resolve().parent.parent


def resource_root() -> Path:
    """只读资源根目录（frozen = ``sys._MEIPASS``；源码 = 项目根）。"""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            return Path(meipass)
        # 极端情况（旧式 onedir / 缺少 _MEIPASS）：退回 exe 旁边，保证有解
        return _executable_dir()
    return project_root()


def data_dir() -> Path:
    """Environment override first, then saved shared location, then project data."""
    override = os.environ.get("EXPLORER_DICT_DATA_DIR")
    if not override:
        from .storage_location import read
        override = read().get('active')
    d = Path(override) if override else (project_root() / "data")
    d.mkdir(parents=True, exist_ok=True)
    return d


def logs_dir() -> Path:
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    return data_dir() / DB_FILENAME


def log_path() -> Path:
    return logs_dir() / "app.log"


def scripts_dir() -> Path:
    """随包分发的脚本目录（frozen 下在 ``_runtime/scripts``，是只读资源）。"""
    return resource_root() / "scripts"


def uia_helper_script() -> Path:
    return scripts_dir() / "uia_helper.ps1"


def assets_dir() -> Path:
    """随包分发的只读资源目录（frozen 下在 ``_runtime/assets``）。"""
    return resource_root() / "assets"


def app_icon_path() -> Path:
    """托盘图标 ``assets/app.ico``（frozen 下由 spec 打进 ``_runtime/assets``）。"""
    return assets_dir() / "app.ico"


def ui_state_path() -> Path:
    return data_dir() / "ui_state.json"


def exports_dir() -> Path:
    """导出目录 ``<data>/exports``（B3）。

    **为什么不是「另存为」对话框**：冻结运行时里没有 ``tkinter.filedialog``
    （打进去的 tkinter 子模块只有 commondialog/constants/font/messagebox/
    simpledialog），所以导出固定写到这个目录，界面上直接把路径显示出来并
    用 ``os.startfile`` 打开文件夹让用户自己拿文件。
    """
    d = data_dir() / "exports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_inside_project(p: os.PathLike[str] | str) -> bool:
    try:
        resolved = Path(p).resolve()
    except OSError:
        return False
    root = project_root()
    try:
        resolved.relative_to(root)
    except ValueError:
        return False
    return True
