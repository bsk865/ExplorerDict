"""全局快捷键：必须使用 Ctrl+Alt+Shift 组合，且呼出键必须单独校验。"""
from __future__ import annotations

import unittest

from app import hotkeys
from app.hotkeys import HOTKEYS, HK_RECALL, required_failures


class TestHotkeyBindings(unittest.TestCase):
    def test_all_hotkeys_use_ctrl_alt_shift(self):
        """DSH Desktop 占用 Ctrl+Alt+D，全部快捷键改成 Ctrl+Alt+Shift 组合。"""
        for hid, (label, mods, vk) in HOTKEYS.items():
            with self.subTest(hid):
                self.assertTrue(mods & hotkeys.w32.MOD_CONTROL, label)
                self.assertTrue(mods & hotkeys.w32.MOD_ALT, label)
                self.assertTrue(mods & hotkeys.w32.MOD_SHIFT, label)
                self.assertNotIn("Ctrl+Alt+", label.replace("Ctrl+Alt+Shift+", ""),
                                 "标签里不得出现两段式组合")
                self.assertTrue(0x30 <= vk <= 0x5A, label)

    def test_labels_show_three_modifiers(self):
        for hid, (label, _m, _v) in HOTKEYS.items():
            self.assertIn("Ctrl+Alt+Shift+", label, label)

    def test_recall_is_required(self):
        self.assertIn(HK_RECALL, hotkeys.REQUIRED_HOTKEYS)

    def test_required_failures_is_not_any_success(self):
        """其它热键成功不能算通过：呼出键缺失必须单独报出来。"""
        self.assertEqual(required_failures([2, 3, 4, 5]), [HK_RECALL])
        self.assertEqual(required_failures([]), [HK_RECALL])
        self.assertEqual(required_failures([HK_RECALL, 2]), [])

    def test_labels_for(self):
        text = hotkeys.labels_for([HK_RECALL])
        self.assertIn("Ctrl+Alt+Shift+D", text)

    def test_manager_defaults(self):
        mgr = hotkeys.HotkeyManager(lambda hid: None)
        self.assertFalse(mgr.recall_registered)
        self.assertEqual(mgr.missing_required(), [HK_RECALL])


if __name__ == "__main__":
    unittest.main()
