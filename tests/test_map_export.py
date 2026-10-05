"""参考关系图导出（``app/map_export.py``）：完整的 PNG + 可编辑的 SVG。

用户要的是**图**：不完整（只抓到看得见的那一块）不行，给 Excel / JSON 也不行。
所以这里断言三件事：名字成对、SVG 能被当作真正的矢量图解析、目录里**不会**
冒出 ``.csv`` / ``.json``。

硬约束（照 ``tests/support.py`` 的规矩）
---------------------------------------
* **不创建任何 Tk 窗口**：本文件不 ``import tkinter``，抓图只走「假画布 + 错误
  路径」—— 真实像素抓取需要真窗口，交给用户在真机上跑（README 的「未实机验收」）。
  分块 / 抽稀 / 内容外框这些**纯函数**在这里全测。
* 临时目录一律用 ``tests.support.tmp_dir``（项目内 ``.tmp/tests``，绝不碰真实 data/）。
"""
from __future__ import annotations

import struct
import subprocess
import sys
import unittest
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app import map_export
from app.map_export import (
    MapExportError, MapExportResult, capture_canvas_png, export_map, write_png,
)
from app.map_service import ManualRelation, MapRelation
from tests.support import tmp_dir

STAMP = datetime(2026, 3, 4, 5, 6, 7)
_PROJECT_ROOT = Path(__file__).resolve().parents[1]      # 子进程里能 import app.*
SVG_TAG = "{http://www.w3.org/2000/svg}svg"


# --------------------------------------------------------------- 小 PNG 解析器
class Png:
    """20 行的 PNG 解析器：只认本模块写出来的那一种（8 位 RGB、filter 0）。"""

    def __init__(self, blob: bytes):
        self.blob = blob
        self.signature_ok = blob[:8] == b"\x89PNG\r\n\x1a\n"
        self.chunks: list[tuple[bytes, bytes]] = []
        offset = 8
        while offset + 8 <= len(blob):
            length = struct.unpack(">I", blob[offset:offset + 4])[0]
            tag = blob[offset + 4:offset + 8]
            data = blob[offset + 8:offset + 8 + length]
            crc = struct.unpack(">I", blob[offset + 8 + length:offset + 12 + length])[0]
            assert crc == zlib.crc32(tag + data) & 0xFFFFFFFF, f"{tag!r} 的 CRC32 不对"
            self.chunks.append((tag, data))
            offset += 12 + length

    def chunk(self, tag: bytes) -> bytes:
        for name, data in self.chunks:
            if name == tag:
                return data
        raise AssertionError(f"缺少 chunk {tag!r}")

    @property
    def tags(self) -> list[bytes]:
        return [tag for tag, _data in self.chunks]

    @property
    def header(self) -> tuple[int, int, int, int, int, int, int]:
        return struct.unpack(">IIBBBBB", self.chunk(b"IHDR"))

    @property
    def size(self) -> tuple[int, int]:
        width, height = self.header[0], self.header[1]
        return width, height

    @property
    def scanlines(self) -> bytes:
        return zlib.decompress(self.chunk(b"IDAT"))

    def pixels(self, width: int, height: int) -> list[tuple[int, int, int]]:
        """按 filter 0 读出像素（顺便断言每个扫描行的 filter 字节都是 0）。"""
        raw = self.scanlines
        stride = width * 3
        assert len(raw) == height * (1 + stride), "解压后的长度不对"
        out: list[tuple[int, int, int]] = []
        for row in range(height):
            base = row * (1 + stride)
            assert raw[base] == 0, f"第 {row + 1} 行的 filter type 不是 0"
            line = raw[base + 1:base + 1 + stride]
            out.extend((line[i], line[i + 1], line[i + 2]) for i in range(0, stride, 3))
        return out


# --------------------------------------------------------------- 假布局
def _edge(rel, label, label_pos, points, *, kind="cross", symmetric=False):
    """一条画好的连线（只带 ``map_export`` 真正读的属性）。"""
    return SimpleNamespace(rel=rel, kind=kind, points=tuple(points), label=label,
                           label_pos=tuple(label_pos), symmetric=symmetric)


def _fake_layout(*, manual=False, isolated=False, symmetric=False):
    """一张最小的假布局（不碰 tkinter，形状与 ``concept_map.MapLayout`` 一致）。"""
    topic = SimpleNamespace(entry_id=0, label="卷积网络", lines=("卷积网络",),
                            x=220.0, y=40.0, w=140.0, h=44.0, isolated=False)
    node_a = SimpleNamespace(entry_id=1, label="神经网络", lines=("神经网络",),
                             x=60.0, y=140.0, w=120.0, h=60.0, isolated=False)
    node_b = SimpleNamespace(entry_id=3, label="卷积", lines=("卷积",),
                             x=320.0, y=140.0, w=110.0, h=60.0, isolated=isolated)
    edges = [_edge(MapRelation(1, 3, "依赖", "神经网络要用到卷积", "证据片段"),
                   "依赖", (200.0, 175.0),
                   ((120.0, 150.0), (200.0, 175.0), (265.0, 150.0)),
                   symmetric=symmetric)]
    if manual:
        edges.append(_edge(ManualRelation(1, 3, "用途", "我加的", "证据"),
                           "用途", (200.0, 205.0),
                           ((120.0, 165.0), (200.0, 205.0), (265.0, 165.0))))
    return SimpleNamespace(
        width=420.0, height=260.0, topic=topic, nodes=(node_a, node_b),
        edges=tuple(edges),
        groups=(SimpleNamespace(root_id=1, members=(1,), x0=10.0, y0=110.0,
                                x1=140.0, y1=175.0),),
        topic_links=(((220.0, 62.0), (80.0, 110.0)),),
        content_w=420.0, content_h=260.0,
        isolated_label_pos=(200.0, 240.0) if isolated else None,
    )


def _empty_layout():
    return SimpleNamespace(width=0.0, height=0.0, topic=None, nodes=(), edges=(),
                           groups=(), topic_links=(), content_w=0.0, content_h=0.0,
                           isolated_label_pos=None)


def _fake_gdi(view=(0, 0, 800, 600), *, dc=0):
    """假的 ``user32`` / ``gdi32``：只回答 ``GetClientRect``。

    用来在没有窗口的情况下验证「**先**算尺寸、**再**申请位图」这一段顺序：
    ``ctypes.byref`` 传进来的对象挂在 ``_obj`` 上，改它就是改真实的 ``_Rect``。
    """

    class User32:
        @staticmethod
        def GetClientRect(_hwnd, pointer):
            rect = getattr(pointer, "_obj", None)
            if rect is None:
                return 0
            rect.left, rect.top, rect.right, rect.bottom = view
            return 1

        @staticmethod
        def GetDC(_hwnd):
            return 0

        @staticmethod
        def ReleaseDC(_hwnd, _hdc):
            return 1

    class Gdi32:
        @staticmethod
        def CreateCompatibleDC(_hdc):
            return dc

    return User32(), Gdi32()


class TestWritePng(unittest.TestCase):
    def test_signature_chunks_and_scanline_length(self):
        with tmp_dir("png_") as tmp:
            path = tmp / "a.png"
            write_png(path, 4, 3, bytes(4 * 3 * 3))
            blob = path.read_bytes()
            self.assertTrue(blob.startswith(b"\x89PNG\r\n\x1a\n"))
            png = Png(blob)
            self.assertEqual(png.tags, [b"IHDR", b"IDAT", b"IEND"])
            width, height, depth, color, comp, filt, interlace = png.header
            self.assertEqual((width, height), (4, 3))
            self.assertEqual((depth, color, comp, filt, interlace), (8, 2, 0, 0, 0))
            self.assertEqual(len(png.scanlines), height * (1 + width * 3))

    def test_solid_colour_round_trip(self):
        with tmp_dir("png_") as tmp:
            path = tmp / "solid.png"
            width, height = 3, 2
            colour = bytes((12, 200, 255))
            write_png(path, width, height, colour * (width * height))
            pixels = Png(path.read_bytes()).pixels(width, height)
            self.assertEqual(pixels, [(12, 200, 255)] * (width * height),
                             "纯色像素必须原样读回（RGB 顺序、自上而下）")

    def test_rows_may_be_given_one_line_at_a_time(self):
        with tmp_dir("png_") as tmp:
            path = tmp / "rows.png"
            rows = [b"\xff\x00\x00" * 2, b"\x00\xff\x00" * 2]
            write_png(path, 2, 2, rows)
            pixels = Png(path.read_bytes()).pixels(2, 2)
            self.assertEqual(pixels[:2], [(255, 0, 0)] * 2)
            self.assertEqual(pixels[2:], [(0, 255, 0)] * 2)

    def test_zero_size_raises(self):
        with tmp_dir("png_") as tmp:
            for width, height in ((0, 4), (4, 0), (0, 0), (-1, 2)):
                with self.subTest(width=width, height=height):
                    with self.assertRaises(MapExportError) as ctx:
                        write_png(tmp / "bad.png", width, height, b"")
                    self.assertIn("尺寸", str(ctx.exception))
            self.assertFalse((tmp / "bad.png").exists(), "失败不得留下文件")

    def test_wrong_pixel_length_raises_and_leaves_nothing(self):
        with tmp_dir("png_") as tmp:
            path = tmp / "short.png"
            with self.assertRaises(MapExportError):
                write_png(path, 2, 2, b"\x00" * 5)           # 期望 12 字节
            self.assertFalse(path.exists())
            self.assertFalse(path.with_name("short.png.part").exists(),
                             "失败后连临时文件都不许留下")
            with self.assertRaises(MapExportError):
                write_png(path, 2, 2, [b"\x00" * 6])         # 只给 1 行
            self.assertFalse(path.exists())

    def test_import_does_not_touch_win32(self):
        """import 本模块不得调用任何 Win32 API（没有窗口的环境也要能 import）。

        用**全新解释器**（子进程）跑，而不是 ``importlib.reload``：reload 会换掉
        模块里 :class:`MapExportError` 等对象的身份，于是同一个测试进程里后面那些
        ``assertRaises`` 就抓不到重载后的新类了（踩过这个坑，测试会莫名其妙 E）。
        """
        source = (
            "import ctypes, importlib\n"
            "calls = []\n"
            "def _spy(*args, **kwargs):\n"
            "    calls.append(args)\n"
            "    raise AssertionError('import 期不许加载 Win32 DLL')\n"
            "ctypes.WinDLL = _spy\n"
            "import app.map_export as m\n"
            "assert hasattr(m, 'capture_canvas_png')\n"
            "assert hasattr(m, 'svg_document')\n"
            "assert calls == [], calls\n"
            "print('clean')\n"
        )
        proc = subprocess.run([sys.executable, "-X", "utf8", "-c", source],
                              cwd=str(_PROJECT_ROOT), capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("clean", proc.stdout)


class TestTextMetrics(unittest.TestCase):
    def test_char_units_counts_full_width_as_one(self):
        self.assertEqual(map_export.char_units("卷积"), 2.0)
        self.assertEqual(map_export.char_units("abc"), 3 * 0.55)
        self.assertAlmostEqual(map_export.char_units("卷积abc"), 2 + 3 * 0.55, places=6)
        self.assertEqual(map_export.char_units(""), 0.0)

    def test_text_width_scales_with_the_font(self):
        self.assertAlmostEqual(map_export.text_width("卷积", 10.0), 20.0, places=6)
        self.assertAlmostEqual(map_export.text_width("卷积", 20.0), 40.0, places=6)

    def test_num_and_esc_are_compact_and_safe(self):
        self.assertEqual(map_export._num(12.5), "12.5")
        self.assertEqual(map_export._num(12.0), "12")
        self.assertEqual(map_export._num(0.0), "0")
        self.assertEqual(map_export._esc('卷积 <b> & "x"'),
                         "卷积 &lt;b&gt; &amp; &quot;x&quot;")
        self.assertEqual(map_export._esc(None), "")


class TestManualOrigin(unittest.TestCase):
    def test_the_local_origin_constant_matches_the_service(self):
        """``map_export`` 不 import ``map_service``（那会连锁拉进 db / crypt32），
        所以来源标记是本地常量 —— 它必须与服务层一致，否则人工关系会被画成 AI 的。
        """
        from app import map_service

        self.assertEqual(map_export.MANUAL_ORIGIN, map_service.MANUAL_ORIGIN)

    def test_manual_edges_are_not_special_cased_for_style(self):
        """人工关系也走**它自己的线型**：不再被主色 / 加粗 / 实线接管。

        用户口径（截图反馈）：线上不要「人工·」标注、人工线不要跟别人不一样。
        """
        style = {"edges": {"cross": {"stroke": "#123456", "dash": (4, 3)}},
                 "edge_fill": "#9B9993", "line_width": 1.0}
        by_hand = SimpleNamespace(rel=SimpleNamespace(origin="user", kind="cross"),
                                  kind="cross")
        self.assertEqual(map_export.is_manual_edge(by_hand), True)
        self.assertEqual(map_export.edge_style(by_hand, style), ("#123456", 1.0, (4.0, 3.0)))
        # 没有自己线型的（例如线型表里没配的手工关系）就退回统一灰 + 统一线宽
        lonely = SimpleNamespace(rel=SimpleNamespace(origin="user", kind="manual"),
                                 kind="manual")
        self.assertEqual(map_export.edge_style(lonely, style), ("#9B9993", 1.0, ()))


class TestTiles(unittest.TestCase):
    def test_one_tile_when_the_content_fits(self):
        self.assertEqual(map_export.tile_origins(100, 200), [0])
        self.assertEqual(map_export.tile_origins(200, 200), [0])
        self.assertEqual(map_export.plan_tiles(100, 80, 200, 200), [(0, 0)])

    def test_last_tile_is_flushed_with_the_far_edge(self):
        # 没有重叠时：0 / 400 / 然后最后一块贴住 1000-400=600
        self.assertEqual(map_export.tile_origins(1000, 400, overlap=0), [0, 400, 600])

    def test_neighbouring_tiles_overlap(self):
        origins = map_export.tile_origins(1000, 400, overlap=2)
        self.assertEqual(origins[0], 0)
        self.assertEqual(max(origins), 600, "最后一块必须贴住右边界")
        self.assertTrue(all(b - a > 0 for a, b in zip(origins, origins[1:])),
                        "起点必须严格递增（原地重复 = 漏抓）")
        for previous, current in zip(origins, origins[1:]):
            self.assertLess(current, previous + 400 - 2 * 2 + 1,
                            "相邻两块至少重叠 2 像素（步长 = 视口 - 2×重叠）")

    def test_plan_tiles_is_a_grid_in_reading_order(self):
        tiles = map_export.plan_tiles(1000, 900, 400, 400)
        self.assertEqual(len(tiles), 9)
        self.assertEqual(tiles[0], (0, 0))
        self.assertEqual(tiles[-1], (600, 500), "右下角最后一块 = (宽-块宽, 高-块高)")
        self.assertEqual([t for t in tiles if t[1] == 0],
                         [(0, 0), (396, 0), (600, 0)], "同一行从左到右")

    def test_zero_or_negative_extent_is_a_single_tile(self):
        self.assertEqual(map_export.tile_origins(0, 400), [0])
        self.assertEqual(map_export.tile_origins(-5, 400), [0])
        self.assertEqual(map_export.plan_tiles(0, 0, 400, 400), [(0, 0)])

    def test_sample_step_only_kicks_in_for_huge_images(self):
        self.assertEqual(map_export.sample_step(1000, 1000, max_pixels=8_000_000), 1)
        self.assertEqual(map_export.sample_step(3000, 3000, max_pixels=8_000_000), 2,
                         "9M 像素 > 8M 上限 ⇒ 抽一半（2.25M）")
        self.assertEqual(map_export.sample_step(4000, 4000, max_pixels=1), 4000)
        self.assertEqual(map_export.sample_step(10, 10, max_pixels=0), 1,
                         "上限给 0 也要收敛，不能死循环")


class TestBgraToRgb(unittest.TestCase):
    def test_swaps_channels_for_a_top_down_buffer(self):
        # 顶向下两行：第一行 红(00 00 FF A0) 绿(00 FF 00 A0)，第二行 蓝(FF 00 00 A0)
        buffer = bytes([0x00, 0x00, 0xFF, 0xA0, 0x00, 0xFF, 0x00, 0xA0,
                        0xFF, 0x00, 0x00, 0xA0, 0x10, 0x20, 0x30, 0xA0])
        rgb = map_export.bgra_to_rgb(buffer, 2, 2)
        self.assertEqual(rgb, bytes([0xFF, 0x00, 0x00, 0x00, 0xFF, 0x00,
                                     0x00, 0x00, 0xFF, 0x30, 0x20, 0x10]))

    def test_short_buffer_raises(self):
        with self.assertRaises(MapExportError):
            map_export.bgra_to_rgb(b"\x00" * 7, 2, 2)

    def test_step_downsamples_and_rounds_up(self):
        # 3×2 的 BGRA（每行 12 字节），step=2 → 取 (0,0)/(2,0) 两个像素，1 行
        row0 = bytes([0x01, 0x02, 0x03, 0xFF, 0x04, 0x05, 0x06, 0xFF, 0x07, 0x08, 0x09, 0xFF])
        row1 = bytes([0x11, 0x12, 0x13, 0xFF, 0x14, 0x15, 0x16, 0xFF, 0x17, 0x18, 0x19, 0xFF])
        rgb = map_export.bgra_to_rgb(row0 + row1, 3, 2, step=2)
        self.assertEqual(rgb, bytes([0x03, 0x02, 0x01, 0x09, 0x08, 0x07]),
                         "抽稀后每行取第 0、2 个像素，输出宽 = ceil(3/2) = 2")
        self.assertEqual(len(map_export.bgra_to_rgb(row0 + row1, 3, 2, step=1)), 3 * 2 * 3)

    def test_step_below_one_is_treated_as_one(self):
        buffer = bytes([0x00, 0x00, 0xFF, 0xFF])
        self.assertEqual(map_export.bgra_to_rgb(buffer, 1, 1, step=0), b"\xff\x00\x00")


class TestContentBox(unittest.TestCase):
    def test_box_covers_nodes_groups_links_and_labels_with_margin(self):
        layout = _fake_layout()
        x0, y0, x1, y1 = map_export.content_box(layout)
        self.assertLessEqual(x0, 10.0 - 6.0 + 0.01, "左边的分组底衬要包进来，还要留白边")
        self.assertLessEqual(y0, 40.0 - 22.0 - 6.0 + 0.01, "词条标题在上面")
        self.assertGreaterEqual(x1, 320.0 + 55.0 + 6.0 - 0.01, "最右边的卡片要包进来")
        self.assertGreaterEqual(y1, 140.0 + 30.0 + 6.0 - 0.01, "最下面的卡片要包进来")

    def test_a_manual_label_still_counts_in_the_box(self):
        plain = map_export.content_box(_fake_layout())
        manual = map_export.content_box(_fake_layout(manual=True))
        self.assertGreater(manual[3], plain[3],
                           "人工关系那张标签也要算进去，否则导出时会被裁掉")

    def test_isolated_label_is_counted(self):
        plain = map_export.content_box(_fake_layout())
        isolated = map_export.content_box(_fake_layout(isolated=True))
        self.assertGreater(isolated[3], plain[3], "孤立词说明在下面，导出时不能丢")

    def test_origin_is_never_negative(self):
        layout = _fake_layout()
        layout.nodes[0].x = -50.0            # 脏数据 / 极端缩放
        layout.nodes[0].w = 40.0
        x0, y0, _x1, _y1 = map_export.content_box(layout)
        self.assertEqual(x0, 0.0, "画布坐标原点就是左上角，请求负坐标会被滚动位置悄悄夹住")
        self.assertGreaterEqual(y0, 0.0)
        self.assertLess(y0, 18.0, "夹的是左边界，上面该有多少白边还是多少")

    def test_margin_can_be_zeroed(self):
        zero = map_export.content_box(_fake_layout(), margin=0.0)
        default = map_export.content_box(_fake_layout())
        # 这张示例图最左边就是「神经网络」卡片（x=60、宽 120 ⇒ 左沿 0），
        # 所以 margin 只作用在别的三边 —— 左/上夹到 0 是**故意的**，不是丢边距。
        self.assertAlmostEqual(zero[0], 0.0, places=6)
        self.assertAlmostEqual(default[0], 0.0, places=6)
        self.assertAlmostEqual(zero[1], 18.0, places=6, msg="词条标题上沿 40-22=18")
        self.assertAlmostEqual(default[1], 12.0, places=6, msg="默认外框留 6 像素白边")
        self.assertAlmostEqual(default[2] - zero[2], 6.0, places=6)
        self.assertAlmostEqual(default[3] - zero[3], 6.0, places=6)

    def test_empty_layout_raises(self):
        with self.assertRaises(MapExportError) as ctx:
            map_export.content_box(_empty_layout())
        self.assertIn("还没有可以导出的内容", str(ctx.exception))


class TestSvgDocument(unittest.TestCase):
    def _svg(self, layout=None, *, style=None, meta=None):
        layout = layout if layout is not None else _fake_layout()
        box = map_export.content_box(layout, style=style)
        return map_export.svg_document(layout=layout, style=style, box=box, meta=meta), box

    def test_a_group_is_a_frame_with_its_name_not_a_filled_slab(self):
        """用户 2026-10-06：「褐色背景应该是作为组标签才对」—— 导出这条也得跟。

        分组画成**细框 + 左上角的组名**（``fill="none"`` + 边框色），不再铺一块
        褐色底衬（``panel_alt``）；框里混进别的卡时只写组名、不画框。
        """
        panel = map_export.DEFAULT_STYLE["panel_alt"]
        layout = _fake_layout()
        old = layout.groups[0]
        layout.groups = (SimpleNamespace(root_id=old.root_id, members=old.members,
                                         x0=old.x0, y0=old.y0, x1=old.x1, y1=old.y1,
                                         label="神经网络", framed=True),)
        text, _box = self._svg(layout)
        self.assertNotIn(f'fill="{panel}"', text, "分组不该再铺褐色底衬")
        frame = next(line for line in text.splitlines()
                     if "<rect" in line and 'fill="none"' in line)
        self.assertIn(f'stroke="{map_export.DEFAULT_STYLE["border"]}"', frame,
                      "组框是描边的细框")
        self.assertIn("神经网络", text, "组名要写成真文字")

        loose = layout.groups[0]
        layout.groups = (SimpleNamespace(root_id=loose.root_id, members=loose.members,
                                         x0=loose.x0, y0=loose.y0, x1=loose.x1, y1=loose.y1,
                                         label="神经网络", framed=False),)
        text, _box = self._svg(layout)
        self.assertIn("神经网络", text, "框里混进别的卡时仍然写组名")
        self.assertFalse([line for line in text.splitlines()
                          if "<rect" in line and 'fill="none"' in line],
                         "……但不画框，免得把别人圈进去")

    def test_is_well_formed_and_sized_like_the_content(self):
        text, box = self._svg()
        root = ET.fromstring(text)                     # 不是合法 XML 就直接抛
        self.assertEqual(root.tag, SVG_TAG)
        self.assertAlmostEqual(float(root.get("width")), box[2] - box[0], places=2)
        self.assertGreaterEqual(float(root.get("height")), box[3] - box[1])
        view_box = [float(v) for v in root.get("viewBox").split()]
        self.assertEqual(len(view_box), 4)
        self.assertAlmostEqual(view_box[0], box[0], places=2)
        self.assertAlmostEqual(view_box[1], box[1], places=2)

    def test_text_is_real_editable_text(self):
        text, _box = self._svg()
        for needle in ("卷积网络", "神经网络", "卷积", "依赖"):
            self.assertIn(needle, text, "方框里的字必须是真文字，能选中、能改")
        self.assertIn("<tspan", text, "多行文字用 tspan，一个词条一个文本框")

    def test_manual_edge_looks_exactly_like_an_ai_edge(self):
        # 用户口径（截图反馈）：线上的标注**不带**「人工·」前缀，人工关系也不加粗、
        # 不换颜色 —— 图上所有连线同一个灰、同一个粗细，只有虚线与文字区分类型。
        # 就算 EDGE_STYLES 里给 manual 配了别的颜色 / 虚线，也不许用上。
        style = {"edges": {"manual": {"stroke": "#00FF00", "dash": (1, 1)}}}
        text, _box = self._svg(_fake_layout(manual=True), style=style)
        self.assertEqual(text.count("用途"), 1, "标签就是类型名本身")
        self.assertNotIn("人工·", text, "线上标注不带前缀；人工关系只在页脚 / 依据区说明")
        self.assertNotIn("#00FF00", text)
        lines = [line for line in text.splitlines() if "<polyline" in line]
        widths = {line.split('stroke-width="')[1].split('"')[0] for line in lines}
        self.assertEqual(widths, {"1"}, "线宽全图统一（连线与主题细线都一样）")
        edge_lines = [line for line in lines
                      if f'stroke="{map_export.DEFAULT_STYLE["edge_fill"]}"' in line]
        self.assertEqual(len(edge_lines), 2, "AI 一条、人工一条，两条同色")
        self.assertNotIn("stroke-dasharray", text, "这两条线都是实线")

    def test_ai_edge_uses_the_kind_style_and_dashes(self):
        style = {"edges": {"cross": {"stroke": "#123456", "dash": (4, 3)}}}
        text, _box = self._svg(style=style)
        self.assertIn('stroke="#123456"', text)
        self.assertIn('stroke-dasharray="4 3"', text)
        marker_line = next(line for line in text.splitlines() if "<marker" in line)
        self.assertIn('fill="#123456"', marker_line, "箭头要和连线同色")

    def test_symmetric_edges_get_no_arrow(self):
        plain, _box = self._svg()
        self.assertEqual(plain.count('marker-end="url(#arrow-'), 1)
        self.assertIn("<defs>", plain)
        symmetric, _box = self._svg(_fake_layout(symmetric=True))
        self.assertEqual(symmetric.count('marker-end="url(#arrow-'), 0)
        self.assertNotIn("<defs>", symmetric, "一条箭头都用不上就不写 defs")

    def test_footer_carries_topic_counts_model_and_legend(self):
        meta = {"topic_name": "卷积网络", "model_config": "deepseek-chat",
                "exported_at": "2026-03-04 05:06"}
        text, _box = self._svg(_fake_layout(manual=True), meta=meta)
        self.assertIn("探索词典 · 关系图 —— 卷积网络", text)
        self.assertIn("2 个词 / 2 条关系（其中人工 1 条）", text)
        self.assertIn("模型：deepseek-chat", text)
        self.assertIn("导出：2026-03-04 05:06", text)
        self.assertIn(map_export.LEGEND_LINE, text)

    def test_no_footer_without_meta(self):
        text, _box = self._svg()
        self.assertNotIn("探索词典 · 关系图", text)
        self.assertNotIn(map_export.LEGEND_LINE, text)

    def test_the_footer_names_the_layout_skeleton_when_one_is_in_use(self):
        meta = {"topic_name": "卷积网络", "model_config": "deepseek-chat",
                "template_name": "树状图", "exported_at": "2026-03-04 05:06"}
        text, _box = self._svg(_fake_layout(), meta=meta)
        self.assertIn("模板：树状图", text, "屏幕用什么骨架，导出就得写什么")
        self.assertLess(text.index("模型：deepseek-chat"), text.index("模板：树状图"))
        self.assertLess(text.index("模板：树状图"), text.index("导出：2026-03-04 05:06"))

    def test_no_template_slot_when_the_skeleton_is_plain_auto(self):
        meta = {"topic_name": "卷积网络", "model_config": "deepseek-chat",
                "template_name": "", "exported_at": "2026-03-04 05:06"}
        text, _box = self._svg(_fake_layout(), meta=meta)
        self.assertNotIn("模板：", text, "自动布局不写模板名（与今天逐像素一致）")

    def test_isolated_label_is_drawn_with_the_caller_wording(self):
        style = {"isolated_text": "孤立词（候选关系都没通过核对，点词看原因）"}
        text, _box = self._svg(_fake_layout(isolated=True), style=style)
        self.assertIn("孤立词（候选关系都没通过核对，点词看原因）", text)

    def test_xml_specials_are_escaped(self):
        layout = _fake_layout()
        layout.nodes[0].lines = ('<b>卷积</b> & "池化"',)
        text, _box = self._svg(layout)
        self.assertIn("&lt;b&gt;卷积&lt;/b&gt; &amp; &quot;池化&quot;", text)
        ET.fromstring(text)                            # 转义对了才解析得动


class TestExportMap(unittest.TestCase):
    def test_writes_a_named_pair_and_no_spreadsheet(self):
        with tmp_dir("mapexp_") as tmp:
            result = export_map(layout=_fake_layout(), topic_name="深度学习",
                                model_config="deepseek-chat", directory=tmp, now=STAMP)
            self.assertIsInstance(result, MapExportResult)
            self.assertEqual(result.directory, Path(tmp))
            self.assertEqual(result.node_count, 2)
            self.assertEqual(result.count, 1)
            self.assertIsNone(result.png_path, "canvas=None 时没有 PNG")
            self.assertEqual(result.png_error, "")
            self.assertEqual(result.svg_path.name,
                             "探索词典-关系图-深度学习-20260304-050607.svg")
            self.assertIn(result.svg_path.name, result.summary())
            self.assertIn("可编辑矢量图", result.summary())
            self.assertNotIn("PNG 没抓到", result.summary())
            self.assertTrue(result.svg_path.exists())
            ET.fromstring(result.svg_path.read_text(encoding="utf-8"))
            names = sorted(path.name for path in tmp.iterdir())
            self.assertEqual(names, [result.svg_path.name],
                             "用户明确不要 Excel / JSON：目录里只能有图片")

    def test_export_writes_the_skeleton_name_into_the_picture(self):
        with tmp_dir("mapexp_") as tmp:
            result = export_map(layout=_fake_layout(), topic_name="深度学习",
                                model_config="deepseek-chat", template_name="鱼骨图",
                                directory=tmp, now=STAMP)
            text = result.svg_path.read_text(encoding="utf-8")
            self.assertIn("模板：鱼骨图", text, "template_name 要一路走到页脚")

    def test_topic_name_falls_back_to_quanbu(self):
        with tmp_dir("mapexp_") as tmp:
            result = export_map(layout=_fake_layout(), topic_name="   ",
                                directory=tmp, now=STAMP)
            self.assertEqual(result.svg_path.name,
                             "探索词典-关系图-全部-20260304-050607.svg")
            self.assertIn("探索词典 · 关系图 —— 全部",
                          result.svg_path.read_text(encoding="utf-8"),
                          "页脚也要写「全部」，不能出现「导出」这种词条导出的兜底名")

    def test_second_export_uses_the_same_stem_for_both_files(self):
        with tmp_dir("mapexp_") as tmp:
            first = export_map(layout=_fake_layout(), topic_name="主题",
                               directory=tmp, now=STAMP)
            second = export_map(layout=_fake_layout(), topic_name="主题",
                                directory=tmp, now=STAMP)
            self.assertEqual(second.svg_path.name,
                             "探索词典-关系图-主题-20260304-050607-2.svg")
            self.assertNotEqual(second.svg_path, first.svg_path)
            self.assertTrue(first.svg_path.exists(), "上一次的导出不能被覆盖")
            self.assertEqual(len(list(tmp.glob("*.svg"))), 2)

    def test_png_is_written_next_to_the_svg_when_capture_works(self):
        calls = []

        def fake_capture(canvas, path, *, box=None, max_pixels=None):
            calls.append((canvas, tuple(box) if box else None))
            write_png(path, 2, 2, bytes(2 * 2 * 3))
            return (int(box[2] - box[0]), int(box[3] - box[1]), 3)

        class AnyCanvas:
            pass

        with tmp_dir("mapexp_") as tmp:
            with mock.patch.object(map_export, "capture_canvas_png", fake_capture):
                result = export_map(layout=_fake_layout(), canvas=AnyCanvas(),
                                    topic_name="主题", directory=tmp, now=STAMP)
            self.assertEqual(len(calls), 1, "整套导出只抓一次图")
            self.assertIsNotNone(result.png_path)
            self.assertEqual(result.png_path.stem, result.svg_path.stem, "两个文件同名")
            self.assertTrue(result.png_path.exists())
            self.assertEqual(Png(result.png_path.read_bytes()).size, (2, 2))
            self.assertEqual(result.step, 3)
            self.assertIn("抽稀", result.summary(), "抽稀过就要在反馈里说清楚")
            self.assertIn(f"{result.width}×{result.height}", result.summary())

    def test_png_failure_keeps_the_svg(self):
        class BrokenCanvas:
            def winfo_id(self):
                raise RuntimeError("窗口已经销毁")

        with tmp_dir("mapexp_") as tmp:
            result = export_map(layout=_fake_layout(), canvas=BrokenCanvas(),
                                topic_name="主题", directory=tmp, now=STAMP)
            self.assertIsNone(result.png_path)
            self.assertIn("窗口已经销毁", result.png_error)
            self.assertIn("PNG 没抓到", result.summary())
            self.assertIn(result.svg_path.name, result.summary())
            self.assertTrue(result.svg_path.exists(), "矢量图必须还在 —— 那才是主产物")
            self.assertFalse((tmp / f"{result.svg_path.stem}.png").exists(),
                             "抓图失败不得留下半截 PNG")
            self.assertEqual(sorted(path.name for path in tmp.iterdir()),
                             [result.svg_path.name])

    def test_layout_none_raises(self):
        with tmp_dir("mapexp_") as tmp:
            with self.assertRaises(MapExportError) as ctx:
                export_map(layout=None, directory=tmp, now=STAMP)
            self.assertIn("还没有关系图", str(ctx.exception))
            self.assertEqual(list(tmp.iterdir()), [])

    def test_empty_layout_raises_before_writing_anything(self):
        with tmp_dir("mapexp_") as tmp:
            with self.assertRaises(MapExportError):
                export_map(layout=_empty_layout(), directory=tmp, now=STAMP)
            self.assertEqual(list(tmp.iterdir()), [],
                             "空图要在写文件**之前**就拒绝，别写出一个空壳")

    def test_directory_is_created(self):
        with tmp_dir("mapexp_") as tmp:
            target = tmp / "深层" / "exports"
            result = export_map(layout=_fake_layout(), directory=target, now=STAMP)
            self.assertEqual(result.directory, target)
            self.assertTrue(result.svg_path.exists())

    def test_unique_stem_avoids_the_png_of_an_earlier_export(self):
        with tmp_dir("mapexp_") as tmp:
            (tmp / "探索词典-关系图-主题-20260304-050607.png").write_bytes(b"x")
            result = export_map(layout=_fake_layout(), topic_name="主题",
                                directory=tmp, now=STAMP)
            self.assertEqual(result.svg_path.name,
                             "探索词典-关系图-主题-20260304-050607-2.svg",
                             "PNG 占了名字也要整体退避（同名才认得出是一套）")


class TestCaptureCanvasErrors(unittest.TestCase):
    """只验证**错误路径**：真实抓图需要真实窗口，本测试一律不创建窗口。"""

    def test_zero_handle_raises(self):
        class DeadCanvas:
            def winfo_id(self):
                return 0

        with tmp_dir("cap_") as tmp:
            path = tmp / "x.png"
            marker = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16      # 假装是上一次导出好的图
            path.write_bytes(marker)
            with self.assertRaises(MapExportError) as ctx:
                capture_canvas_png(DeadCanvas(), path, box=(0, 0, 100, 100))
            self.assertIn("句柄", str(ctx.exception))
            self.assertEqual(path.read_bytes(), marker, "失败不许动上一个文件")
            self.assertEqual([p.name for p in tmp.iterdir()], ["x.png"],
                             "失败后连 .part 临时文件都不许留下")

    def test_bad_handle_keeps_the_previous_file(self):
        class FakeCanvas:
            def winfo_id(self):
                return 0x5EED1234          # 不存在的窗口：GetClientRect 必然失败

        with tmp_dir("cap_") as tmp:
            path = tmp / "x.png"
            marker = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
            path.write_bytes(marker)
            with self.assertRaises(MapExportError) as ctx:
                capture_canvas_png(FakeCanvas(), path)
            self.assertIn("GetClientRect", str(ctx.exception), "消息要说清哪一步失败")
            self.assertEqual(path.read_bytes(), marker,
                             "抓图失败只能留下原文件，不能删掉也不能写半截")
            self.assertEqual([p.name for p in tmp.iterdir()], ["x.png"])

    def test_huge_region_is_refused_before_allocating_a_bitmap(self):
        """图太大时给一句人话，别让 GDI 去申请几 GB 位图。"""
        class AnyCanvas:
            def winfo_id(self):
                return 0x1234

        with tmp_dir("cap_") as tmp:
            with mock.patch.object(map_export, "_gdi_libraries", lambda: _fake_gdi()):
                with self.assertRaises(MapExportError) as ctx:
                    capture_canvas_png(AnyCanvas(), tmp / "x.png",
                                       box=(0, 0, 20000, 20000))   # 4 亿像素
            self.assertIn("太大", str(ctx.exception))
            self.assertEqual(list(tmp.iterdir()), [], "连临时文件都不许留下")

    def test_getdc_failure_names_the_step(self):
        class AnyCanvas:
            def winfo_id(self):
                return 0x1234

        with tmp_dir("cap_") as tmp:
            with mock.patch.object(map_export, "_gdi_libraries", lambda: _fake_gdi()):
                with self.assertRaises(MapExportError) as ctx:
                    capture_canvas_png(AnyCanvas(), tmp / "x.png")   # 没给 box → 视口 800×600
            self.assertIn("GetDC", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
