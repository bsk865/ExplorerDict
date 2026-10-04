"""项目内路径解析。

硬约束：所有读写都发生在项目根目录内，不触碰用户其它目录、不读全局配置。

源码模式 vs 冻结模式（PyInstaller onedir）
------------------------------------------
打包成 ``探索词典.exe`` 之后有**两套**根目录，绝不能混为一谈：

* **project_root（数据根）**：源码下是 ``app/`` 的上一级；frozen 下是
  ``sys.executable`` 所在目录（exe 同级）。用户数据 ``data/`` 永远跟着它走 ——
  **不迁移、不复制、不读旧数据**：把 exe 与 ``_runtime`` 放进项目目录，
  用的还是原来那份 ``data/``；
* **resource_root（资源根）**：源码下等于 project_root；frozen 下是
  ``sys._MEIPASS``（onedir 的 ``_runtime`` 目录）。随包分发的只读资源
  （例如 ``scripts/uia_helper.ps1``）只在这里找。

因此 :func:`scripts_dir` 走 resource_root，而 :func:`data_dir` / :func:`logs_dir`
/ :func:`db_path` 走 project_root —— 打包后日志与数据库仍然落在 exe 旁边的
``data/``，而不是只读的 ``_runtime`` 里。
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
    """数据目录。

    默认 ``<project_root>/data``：源码下是项目内的 ``data/``，frozen 下是
    exe 同级的 ``data/``（**原样沿用，不迁移、不读旧位置的数据**）。
    测试可通过环境变量 EXPLORER_DICT_DATA_DIR 指向临时目录，
    以免测试污染真实用户数据（见 tests/）。
    """
    override = os.environ.get("EXPLORER_DICT_DATA_DIR")
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
