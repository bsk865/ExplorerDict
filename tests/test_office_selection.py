"""Execute native Office selection logic with controlled providers, no GUI."""
import subprocess
import unittest
from pathlib import Path

from app.uia_bridge import find_powershell

ROOT = Path(__file__).resolve().parents[1]


class TestOfficeSelectionContract(unittest.TestCase):
    @unittest.skipUnless(find_powershell(), "Windows PowerShell required")
    def test_window_binding_selection_and_context(self):
        result = subprocess.run(
            [find_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(ROOT / "tests/office_selection_contract.ps1"),
             "-HelperPath", str(ROOT / "scripts/uia_helper.ps1")],
            capture_output=True, timeout=20,
        )
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("PASS assertions=", output)

    def test_capture_preserves_method_and_waits_for_explicit_record(self):
        from tests.support import FakeBridge, foreground, make_service, ok_response, temp_db

        response = ok_response(text="原文依据", context="原文依据来自阅读材料")
        response["method"] = "OfficeSelection"
        with temp_db() as db:
            service, _ = make_service(db, FakeBridge(response))
            with foreground(app="wps.exe"):
                selection, reason = service.attempt_capture(10, 10, "drag")
            self.assertEqual(reason, "")
            self.assertEqual(selection.term, "原文依据")
            self.assertEqual(selection.method, "office_selection")
            self.assertEqual(db.count_entries(), 0)
            self.assertEqual(db.list_batches(), [])
            entry_id, _ = service.record_snapshot(selection)
            self.assertEqual(db.get_entry(entry_id)["capture_method"], "office_selection")
