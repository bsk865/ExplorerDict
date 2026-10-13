"""离线审核不得因一个失败案例丢失其它结果，也不得默认重绘。"""
import json
from pathlib import Path
import tempfile
import unittest
from app import article_map as am
from tests.test_full_article_map import FIXTURES
from tools import audit_article_maps as audit


class AuditArticleMapsTests(unittest.TestCase):
    def prepare(self,root):
        fixture=FIXTURES[0]
        good=root/"good";good.mkdir()
        source={"text":fixture["text"],"selected_words":fixture["selected_words"],"title":"离线测试文章"}
        data=am.validate_draft(fixture["draft"],fixture["text"],fixture["selected_words"])
        (good/"source.json").write_text(json.dumps(source,ensure_ascii=False),encoding="utf-8")
        (good/"approved.json").write_text(json.dumps({"article_map":data},ensure_ascii=False),encoding="utf-8")
        (root/"configuration.json").write_text(json.dumps({"model":"fixture-model","thinking":"enabled"}),encoding="utf-8")
        return good

    def test_failure_is_recorded_without_losing_successful_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);good=self.prepare(root)
            bad=root/"bad";bad.mkdir();(bad/"approved.json").write_text("{",encoding="utf-8")
            pending=root/"pending";pending.mkdir();(pending/"source.json").write_text("{}",encoding="utf-8")
            self.assertEqual(audit.main([str(root)]),1)
            report=json.loads((root/"layout-audit.json").read_text(encoding="utf-8"))
            self.assertEqual(report["counts"],{"passed":1,"failed":0,"not_approved":1,"input_error":1})
            self.assertFalse((good/"full-map.svg").exists())
            passed=next(c for c in report["cases"] if c["status"]=="passed")
            self.assertTrue(passed["initial"]["ok"])
            self.assertEqual(len(passed["drag_checks"]),3)
            self.assertTrue(all(c["ok"] for c in passed["drag_checks"]))

    def test_opt_in_render_preserves_title_model_and_audit_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);good=self.prepare(root)
            first=audit.audit_directory(root,render=True)
            self.assertTrue(first["ok"])
            svg=(good/"full-map.svg").read_text(encoding="utf-8")
            self.assertIn("离线测试文章",svg)
            self.assertIn("fixture-model",svg)
            self.assertIn("thinking=enabled",svg)
            second=audit.audit_directory(root)
            history=(root/"layout-audit-history.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(history),2)
            self.assertEqual(json.loads(history[0])["render_requested"],True)
            self.assertEqual(json.loads(history[1])["render_requested"],False)
            self.assertTrue(second["ok"])


if __name__=="__main__":
    unittest.main()
