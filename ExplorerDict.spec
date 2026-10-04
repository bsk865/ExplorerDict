# -*- mode: python ; coding: utf-8 -*-
"""探索词典 PyInstaller 打包脚本（windowed / onedir / 运行时目录 ``_runtime``）。

构建（用仓库内的构建环境，无需联网）
------------------------------------
    .build-env\\Scripts\\python.exe -m PyInstaller --noconfirm ExplorerDict.spec

产物布局（**exe 与 _runtime 必须一起保留、一起拷贝**）
-----------------------------------------------------
    dist/探索词典/探索词典.exe        ← 双击入口（windowed：无控制台窗口）
    dist/探索词典/_runtime/           ← 全部运行时依赖（内含 scripts/uia_helper.ps1）
    dist/探索词典/data/               ← 运行后自动生成，与源码模式同一套 data/

路径约定（见 ``app/paths.py``）
-------------------------------
* ``project_root``（frozen）= exe 所在目录 → 数据仍然落在 exe 旁边的 ``data/``；
* ``resource_root``（frozen）= ``sys._MEIPASS`` = ``_runtime/`` → 只读资源在这找；
* 因此**不打包、不迁移任何用户数据**：把 exe + ``_runtime`` 放进项目目录，
  用的就是原来那份 ``data/``（日志、数据库、密钥密文都在里面）。
* 同理**不打包** ``data/``、日志、密钥 / 配置密文、``tests/``、``tools/``、``.tmp/``。

图标：``assets/app.ico``。首次构建时由本脚本用纯 Python 生成（不依赖 Pillow、
不联网）；已经存在则直接使用，不再覆盖。
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

PROJECT_ROOT = Path(SPECPATH).resolve()  # noqa: F821 - PyInstaller 注入的全局
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

APP_NAME = "探索词典"
ENTRY = str(PROJECT_ROOT / "desktop_entry.py")
CONTENTS_DIR = "_runtime"
ICON_PATH = PROJECT_ROOT / "assets" / "app.ico"
UIA_HELPER = PROJECT_ROOT / "scripts" / "uia_helper.ps1"

#: 刻意排除：测试 / 构建工具 / 打包器自身，绝不进用户手里
EXCLUDES = [
    "tests",
    "tools",
    "PyInstaller",
    "pytest",
    "_pytest",
    "setuptools",
    "pip",
]

#: 只打包这一个随包脚本（UIA helper 由 powershell.exe 运行，不是 Python 模块）。
#: 托盘图标 ``assets/app.ico`` 在下面图标步骤之后再补进 DATAS（首次构建时它
#: 才由本脚本生成）。
DATAS = [(str(UIA_HELPER), "scripts")]


# --------------------------------------------------------------------- 图标
def _inside_round_rect(x, y, x0, y0, x1, y1, r) -> bool:
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    dx, dy = x - cx, y - cy
    return dx * dx + dy * dy <= r * r


def _is_page(x, y) -> bool:
    if not (0.28 <= y <= 0.74):
        return False
    return (0.15 <= x <= 0.475) or (0.525 <= x <= 0.85)


def _is_text_line(x, y) -> bool:
    for top in (0.36, 0.47, 0.58):
        if top <= y <= top + 0.045:
            return (0.20 <= x <= 0.43) or (0.57 <= x <= 0.80)
    return False


def _icon_rows(size: int):
    """逐像素画一个「深蓝底板 + 白色打开的书」，返回 BGRA 行（自上而下）。

    2x2 超采样做抗锯齿；纯数学，不依赖 Pillow / 字体 / 网络。
    """
    bg = (0x8C, 0x50, 0x1E)      # BGR #1E508C
    page = (0xFF, 0xFA, 0xF7)    # BGR #F7FAFF
    line = (0xDC, 0xB8, 0x9A)    # BGR #9AB8DC
    margin, radius = 1.0 / 32.0, 6.0 / 32.0
    offsets = (0.25, 0.75)
    rows = []
    for py in range(size):
        row = []
        for px in range(size):
            covered = pages = lines = 0
            for sy in offsets:
                for sx in offsets:
                    x = (px + sx) / size
                    y = (py + sy) / size
                    if not _inside_round_rect(x, y, margin, margin, 1 - margin,
                                              1 - margin, radius):
                        continue
                    covered += 1
                    if _is_page(x, y):
                        pages += 1
                        if _is_text_line(x, y):
                            lines += 1
            if not covered:
                row.append((0, 0, 0, 0))
                continue
            f_page = pages / covered
            f_line = (lines / covered) if pages else 0.0
            channels = []
            for i in range(3):
                base = bg[i] * (1 - f_page) + page[i] * f_page
                channels.append(int(round(base * (1 - f_line) + line[i] * f_line)))
            alpha = int(round(255 * covered / 4))
            row.append((channels[0], channels[1], channels[2], alpha))
        rows.append(row)
    return rows


def _ico_image(size: int) -> bytes:
    """单尺寸的 ICO 图像块：BITMAPINFOHEADER + 32bpp XOR 位图 + AND 掩码。"""
    pixels = bytearray()
    for row in reversed(_icon_rows(size)):  # BMP 行序：自下而上
        for b, g, r, a in row:
            pixels += bytes((b, g, r, a))
    stride = ((size + 31) // 32) * 4
    and_mask = bytes(stride * size)  # 全 0：透明完全由 alpha 通道决定
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                         len(pixels) + len(and_mask), 0, 0, 0, 0)
    return header + bytes(pixels) + and_mask


def _ensure_icon(path: Path) -> bool:
    """生成多尺寸 ``.ico``（已存在则不动）。失败只返回 False，不阻断构建。"""
    try:
        sizes = (16, 24, 32, 48, 64, 128, 256)
        images = [_ico_image(size) for size in sizes]
        offset = 6 + 16 * len(sizes)
        entries = bytearray()
        for size, blob in zip(sizes, images):
            entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32,
                                   len(blob), offset)
            offset += len(blob)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(struct.pack("<HHH", 0, 1, len(sizes)) + bytes(entries)
                         + b"".join(images))
        return True
    except Exception:
        return False


if not ICON_PATH.exists():
    _ensure_icon(ICON_PATH)
ICON = str(ICON_PATH) if ICON_PATH.exists() else None
if ICON_PATH.exists():
    # tray 运行时从资源根（frozen = _runtime）加载这张图标
    DATAS.append((str(ICON_PATH), "assets"))


# --------------------------------------------------------------- hiddenimports
def _diagnose_hiddenimports() -> list:
    """直接取 ``bootstrap._DIAGNOSE_MODULES``，诊断能过的模块打包后也必须能导入。"""
    try:
        import bootstrap

        return [str(name) for name in bootstrap._DIAGNOSE_MODULES]
    except Exception:  # pragma: no cover - 兜底：与 bootstrap 的列表保持一致
        return [
            "app", "app.paths", "app.win32util", "app.db", "app.config", "app.gate",
            "app.capture_service", "app.explain_service", "app.hotkeys",
            "app.mouse_hook", "app.uia_bridge", "app.main",
        ]


HIDDEN_IMPORTS = _diagnose_hiddenimports() + [
    "app.activation",     # 重复启动 → 呼出已有实例（命名事件）
    "app.tray",           # 常驻托盘图标（pystray / Pillow 延迟导入）
    "pystray",            # 托盘后端按平台动态导入，必须显式打包
    "pystray._win32",     # Windows 后端（pystray.Icon 内部 import）
    "six",                # pystray 的兼容依赖（部分版本仍 import six）
    "PIL.Image",          # 字形渲染 / 托盘图标：打包后必须仍然可用
    "PIL.ImageTk",
    "PIL.ImageDraw",
    "PIL.ImageFont",
    "PIL._tkinter_finder",  # ImageTk 需要（PyInstaller 官方 hook 也会加）
]


# ------------------------------------------------------------------- 构建
a = Analysis(  # noqa: F821 - PyInstaller 注入
    [ENTRY],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                  # windowed：双击不弹控制台
    disable_windowed_traceback=True,  # 失败提示由 desktop_entry 的 MessageBoxW 负责
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
    contents_directory=CONTENTS_DIR,  # onedir：依赖全放 _runtime/
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
