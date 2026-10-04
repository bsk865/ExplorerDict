"""解释结果的**归属**校验（解释结果窗 / 卡片 / 编辑作废）。

> 本轮**不执行**这些用例（会创建真实 Tk 窗口）；接口已按最终交互更新。

最终交互下的归属规则：

* 结果一律按 ``entry_id`` 归属（``on_result(entry_id, status, result, error)``）；
* 解释结果窗只更新**它自己那条词条**的显示；别的词条的结果既不改写窗口归属，
  也不会把窗口重新弹出来（隐藏状态只写内容、不重开）；
* 词条内容被编辑后，在途解释变成 ``stale``，不得覆盖新内容；
* 结果窗里**没有**任何记录动作（落库只发生在浮条那一次点击）。
"""
from __future__ import annotations

import unittest

from tests.support import foreground, requires_real_foreground, temp_data_dir

try:
    import tkinter as tk
except ImportError:  # pragma: no cover
    tk = None


@unittest.skipIf(tk is None, "没有 tkinter")
class _AppCase(unittest.TestCase):
    @requires_real_foreground("解释结果归属用例（会创建主窗口）")
    def setUp(self):
        from app.config import Config
        from app.db import Database
        from app.main import App
        from app.ui import theme
        import app.paths as paths
        from app import win32util as w32

        self._data = temp_data_dir()
        self.tmp = self._data.__enter__()
        self.root = tk.Tk()
        w32.enable_dpi_awareness()
        theme.init(self.root, 96)
        self.db = Database(paths.db_path())
        self.config = Config(self.db)
        self.app = App(self.root, self.db, self.config)
        self.root.update()
        self.batch_id = self.db.create_batch("【测试数据】解释归属批次")
        self.eid_a = self.db.add_entry(batch_id=self.batch_id, term="alpha", context="ctx A",
                                       doc_key="k")
        self.eid_b = self.db.add_entry(batch_id=self.batch_id, term="beta", context="ctx B",
                                       doc_key="k")

    def tearDown(self):
        try:
            self.app._closing = True
            for aid in self.root.tk.call("after", "info"):
                try:
                    self.root.after_cancel(aid)
                except tk.TclError:  # pragma: no cover
                    pass
        except tk.TclError:  # pragma: no cover
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        try:
            self.db.close()
        except Exception:
            pass
        self._data.__exit__(None, None, None)

    def pump(self, times: int = 3) -> None:
        for _ in range(times):
            self.app._pump()
            try:
                self.root.update()
            except tk.TclError:  # pragma: no cover
                return

    def show_entry(self, entry_id: int, term: str) -> None:
        """让解释结果窗进入「正在展示某条已记录词条」的状态（受门控约束）。"""
        from tests.support import visibility_gate

        with visibility_gate(allowed=True):
            ok = self.app.explain_window.show_recorded(
                _sel(term), entry_id, token=1, status="pending", explicit=True)
        self.assertTrue(ok)
        self.assertEqual(self.app.explain_window.entry_id, entry_id)


class TestExplainResultScoping(_AppCase):
    def _deliver_ok(self, entry_id: int, one_line: str) -> None:
        """模拟解释线程真正的完成顺序：先写库，再把结果事件交给 UI 队列。"""
        from app.models import ExplainResult

        result = ExplainResult(one_line=one_line, detail="详细", examples=["例"],
                               raw="{}", from_cache=False, model_config="m")
        self.db.update_entry(entry_id, explain_status="ok", one_line=one_line,
                             detail="详细", examples='["例"]', model_config="m")
        self.app._ui_q.put(("explain_result", (entry_id, "ok", result, None)))
        self.pump()

    def test_result_for_other_entry_does_not_touch_window(self):
        """窗口展示 B；A 的结果回来 → 窗口归属不变、内容不变，A 的卡片刷新。"""
        self.show_entry(self.eid_b, "beta")
        body_before = self.app.explain_window._body_text()
        self._deliver_ok(self.eid_a, "A 的一句话")

        self.assertEqual(self.app.explain_window.entry_id, self.eid_b, "窗口归属不得被改写")
        self.assertEqual(self.app.explain_window._body_text(), body_before,
                         "别的词条的结果不得改写窗口内容")
        row_a = self.db.get_entry(self.eid_a)
        self.assertEqual(row_a["one_line"], "A 的一句话", "结果必须写进它自己的词条")
        self.assertGreaterEqual(self.app.main.refresh_entries_calls, 1, "卡片必须刷新")

    def test_result_for_own_entry_updates_window(self):
        self.show_entry(self.eid_a, "alpha")
        self._deliver_ok(self.eid_a, "alpha 的一句话")
        self.assertIn("alpha 的一句话", self.app.explain_window._body_text())
        self.assertEqual(self.app.explain_window.status, "ok")

    def test_error_result_refreshes_card_and_window(self):
        from app.api_client import ApiError

        self.show_entry(self.eid_a, "alpha")
        err = ApiError("auth", "Key 无效")
        self.app._ui_q.put(("explain_result", (self.eid_a, "error", None, err)))
        self.pump()
        self.assertIn("解释失败", self.app.explain_window._body_text())
        self.assertIn("解释失败", self.app.main.status_label.cget("text"))

    def test_hidden_window_is_not_reopened_by_late_result(self):
        self.show_entry(self.eid_a, "alpha")
        self.app.explain_window.hide()
        self.assertFalse(self.app.explain_window.visible)
        self._deliver_ok(self.eid_a, "迟到的释义")
        self.assertFalse(self.app.explain_window.visible, "迟到的结果不得重开窗口")
        self.assertEqual(self.db.get_entry(self.eid_a)["one_line"], "迟到的释义",
                         "窗口隐藏不影响已经请求的写回")

    def test_switching_window_entry_clears_previous_explanation(self):
        self.show_entry(self.eid_a, "alpha")
        self._deliver_ok(self.eid_a, "A 的释义")
        self.assertIn("A 的释义", self.app.explain_window._body_text())

        from tests.support import visibility_gate

        with visibility_gate(allowed=True):
            self.app.explain_window.show_recorded(_sel("beta"), self.eid_b,
                                                  token=2, status="pending",
                                                  explicit=True)
        self.assertEqual(self.app.explain_window.entry_id, self.eid_b)
        self.assertNotIn("A 的释义", self.app.explain_window._body_text(),
                         "切换词条后不得残留上一条的解释")


class TestEditInvalidatesExplanation(_AppCase):
    def _seed_explanation(self, entry_id: int, one_line="旧释义") -> None:
        self.db.update_entry(entry_id, explain_status="ok", one_line=one_line,
                             detail="旧详细", examples='["旧例"]', model_config="m|1",
                             explained_at="2026-09-30T10:00:00+08:00")

    def test_edit_term_clears_explanation(self):
        self._seed_explanation(self.eid_a)
        self.app.main.select_entry(self.eid_a)
        self.root.update()

        self.app.main.var_term.set("alpha-改过")
        self.app.main.save_detail()
        self.root.update()

        row = self.db.get_entry(self.eid_a)
        self.assertEqual(row["term"], "alpha-改过")
        self.assertEqual(row["explain_status"], "none", "编辑后原解释必须作废")
        self.assertEqual(row["one_line"], "")
        self.assertEqual(row["detail"], "")
        self.assertEqual(row["examples"], "[]")
        self.assertIn("尚未解释", self.app.main.exp_text.get("1.0", tk.END))

    def test_source_only_edit_keeps_explanation(self):
        """只改来源（标题/URL）不影响释义与语境，不应清空解释。"""
        self._seed_explanation(self.eid_a)
        self.app.main.select_entry(self.eid_a)
        self.root.update()
        self.app.main.var_src_title.set("人工修正后的标题")
        self.app.main.save_detail()
        self.root.update()
        row = self.db.get_entry(self.eid_a)
        self.assertEqual(row["explain_status"], "ok", "只改来源不应作废解释")
        self.assertEqual(row["one_line"], "旧释义")
        self.assertEqual(row["source_confidence"], "manual")

    def test_edited_entry_discards_inflight_result(self):
        """编辑时若有在途解释，快照校验会让它变成 stale，不覆盖新内容。"""
        from app.explain_service import ExplainService
        from app.models import ExplainResult

        svc = ExplainService(self.db, self.config)
        snap = svc.snapshot_for(self.eid_a)
        self.assertIsNotNone(snap)
        self.db.update_entry(self.eid_a, term="alpha-改过")   # 模拟用户编辑
        self.assertFalse(svc._still_current(snap))
        ok = svc._mark_ok(snap, ExplainResult(one_line="旧解释", detail="", examples=[],
                                              raw="{}", from_cache=False, model_config="m"))
        self.assertFalse(ok, "编辑后不得写回过期解释")
        self.assertEqual(self.db.get_entry(self.eid_a)["one_line"], "")


class TestRetryReusesEntry(_AppCase):
    def test_retry_never_records_again(self):
        """「重新解释」只解释同一条词条：不新增词条、不加词频。"""
        calls: list[int] = []
        self.app.explain_service.explain_entry_async = (
            lambda eid, force=False: (calls.append(int(eid)) or 1))
        self.config.set_api_key("sk-test-key-not-real")
        before = self.db.count_entries()
        ok = self.app.retry_explain_entry(self.eid_a)
        self.assertTrue(ok)
        self.assertEqual(calls, [self.eid_a])
        self.assertEqual(self.db.count_entries(), before, "重试不得新增词条")
        self.assertEqual(self.db.get_entry(self.eid_a)["repeat_count"], 1,
                         "重试不得加词频")


def _sel(term="alpha"):
    from app.models import CapturedSelection, SourceInfo, now_iso

    src = SourceInfo(title="【测试数据】来源", app="msedge.exe", confidence="url_document")
    return CapturedSelection(term=term, context="ctx", method="ui_textpattern",
                             source=src, captured_at=now_iso())


if __name__ == "__main__":
    unittest.main()
