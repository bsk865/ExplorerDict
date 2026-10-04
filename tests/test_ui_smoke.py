"""T11 UI 冒烟：主窗口、卡片、浮条、解释结果窗都能真实创建。

> 本轮**不执行**这些用例（会创建真实 Tk 窗口）；接口已按最终交互更新。

预检：会创建 Tk 窗口，因此先做**实时真实前台**预检
（``tests.support.skip_unless_foreground_safe``）—— 只有「白名单阅读应用 +
非全屏 + 非游戏模式」才跑；用户玩游戏/全屏时明确 skip，不创建任何窗口。
"""
from __future__ import annotations

import unittest

from tests.support import requires_real_foreground, temp_data_dir
from app import win32util as w32

try:
    import tkinter as tk
except ImportError:  # pragma: no cover
    tk = None


@unittest.skipIf(tk is None, "没有 tkinter")
class TestUiSmoke(unittest.TestCase):
    """只创建一个 Tk 实例，跑完全部界面检查再销毁（避免 Tk 8.6 多实例问题）。"""

    @requires_real_foreground("UI 冒烟（会创建主窗口与浮窗）")
    def test_main_window_and_single_button_overlays(self):
        from app.config import Config
        from app.db import Database
        from app.main import App
        from app.ui import theme
        import app.paths as paths

        with temp_data_dir() as tmp:
            root = tk.Tk()
            try:
                w32.enable_dpi_awareness()
                theme.init(root, 96)
                db = Database(paths.db_path())
                config = Config(db)

                bid = db.create_batch("冒烟批次", "url:https://smoke.example.com/x", "url_document")
                db.add_entry(batch_id=bid, term="卷积神经网络", context="深度学习核心结构",
                             doc_key="k", source_title="冒烟文档",
                             source_confidence="window_title_only")
                db.add_entry(batch_id=bid, term="attention", context="注意力机制", doc_key="k2",
                             source_title="冒烟文档", source_confidence="url_document",
                             source_url="https://smoke.example.com/x")

                app = App(root, db, config)  # 不调用 start()，不装钩子/热键
                root.update()

                # 主窗口
                self.assertTrue(root.winfo_exists())
                self.assertEqual(app.main.count_label.cget("text")[:1], "2")
                self.assertEqual(len(app.main._cards), 2, "两张卡片都应渲染")
                self.assertEqual(len(app.main._batch_rows), 1)

                # 选中词条 → 详情面板填充
                app.main.select_entry(int(db.list_entries()[0]["id"]))
                root.update()
                self.assertTrue(app.main.var_term.get())

                # 搜索
                app.main.search_var.set("注意力")
                app.main.refresh_entries()
                root.update()
                self.assertEqual(len(app.main._cards), 1)
                app.main.search_var.set("")
                app.main.refresh_entries()
                root.update()

                # 选区操作浮条：只有一个按钮，NOACTIVATE，不抢焦点
                from tests.support import visibility_gate

                sel = _sel()
                with visibility_gate(allowed=True):
                    self.assertTrue(app.selection_bar.show_selection(sel, 1, (200, 200)))
                root.update()
                bar_hwnd = app.selection_bar.hwnd()
                self.assertNotEqual(bar_hwnd, 0, "浮条应该有真实 HWND")
                self.assertTrue(w32.is_no_activate(bar_hwnd), "浮条必须带 WS_EX_NOACTIVATE")
                self.assertEqual(app.selection_bar.btn_action.cget("text"), "解释并记录")
                self.assertFalse(hasattr(app.selection_bar, "btn_record"),
                                 "浮条不得再有独立的「记录」按钮")
                self.assertEqual(app.selection_bar.term_label.cget("text"), "冒烟词")

                # 解释结果窗：已记录状态 + 重试 / 设置 / 关闭，没有记录按钮
                with visibility_gate(allowed=True):
                    self.assertTrue(app.explain_window.show_recorded(sel, 1, token=1,
                                                                     status="pending",
                                                                     explicit=True))
                root.update()
                exp_hwnd = app.explain_window.hwnd()
                self.assertNotEqual(exp_hwnd, 0, "解释结果窗应该有真实 HWND")
                self.assertTrue(w32.is_no_activate(exp_hwnd))
                self.assertFalse(hasattr(app.explain_window, "btn_record"),
                                 "解释结果窗不得有记录动作")
                self.assertTrue(hasattr(app.explain_window, "btn_retry"))
                self.assertTrue(hasattr(app.explain_window, "btn_settings"))
                self.assertTrue(hasattr(app.explain_window, "btn_close"))

                # 隐藏（幂等）
                app.selection_bar.hide()
                app.explain_window.hide()
                root.update()
                self.assertFalse(app.selection_bar.visible)
                self.assertFalse(app.explain_window.visible)
                self.assertFalse(app.selection_bar.hide(), "已隐藏时 hide() 必须返回 False")

                db.close()
            finally:
                try:
                    root.destroy()
                except tk.TclError:
                    pass


def _sel():
    from app.models import CapturedSelection, SourceInfo, now_iso

    src = SourceInfo(title="冒烟文档", app="msedge.exe", confidence="url_document")
    return CapturedSelection(term="冒烟词", context="冒烟上下文", method="ui_textpattern",
                             source=src, captured_at=now_iso())


if __name__ == "__main__":
    unittest.main()
