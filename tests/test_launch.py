"""T12 中文路径下的真实启动检查（端到端 subprocess）。

项目路径本身含中文（D:\\探索工具\\探索词典），所以这个测试同时验证了「中文路径可启动」。
数据目录通过 EXPLORER_DICT_DATA_DIR 指向临时目录，不污染真实数据。

预检：``--selftest`` 会真实创建主窗口与浮窗，因此执行前先做**实时真实前台**预检；
用户玩游戏/全屏/非白名单前台时明确 skip，不启动任何 GUI 进程。
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest

from tests.support import ROOT, requires_real_foreground, temp_data_dir


class TestRealLaunch(unittest.TestCase):
    @requires_real_foreground("启动自检（会真实创建主窗口与浮窗）")
    def test_selftest_subprocess(self):
        with temp_data_dir() as tmp:
            env = dict(os.environ)
            env["EXPLORER_DICT_DATA_DIR"] = str(tmp)
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"

            proc = subprocess.run(
                [sys.executable, "-X", "utf8", str(ROOT / "app" / "main.py"),
                 "--selftest", "--console-log"],
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                timeout=120,
            )
            out = proc.stdout.decode("utf-8", "replace")
            err = proc.stderr.decode("utf-8", "replace")
            self.assertEqual(proc.returncode, 0,
                             f"启动自检失败\nSTDOUT:\n{out}\nSTDERR:\n{err}")
            self.assertNotIn("[FAIL]", out)
            # 关键：呼出热键必须单独通过（不能因为其它热键成功就算通过）
            required_checks = ["数据库", "主窗口", "操作浮条", "浮条不抢焦点", "浮条默认置顶",
                               "解释结果窗", "前台门控", "鼠标钩子",
                               "呼出热键 Ctrl+Alt+Shift+D", "UIA helper", "前台监视"]
            for name in required_checks:
                line = next((l for l in out.splitlines() if name in l), "")
                self.assertTrue(line.startswith("[PASS]"), f"自检项未通过：{name} → {line!r}\n{out}")
            self.assertEqual(out.count("[PASS]"), len(required_checks),
                             f"自检项数量变化？\n{out}")

            # 数据确实写到了临时目录，而不是项目 data/
            self.assertTrue((tmp / "explorer_dict.sqlite3").exists())
            self.assertNotEqual((tmp / "explorer_dict.sqlite3").stat().st_size, 0)

    def test_agent_api_cli_readonly(self):
        from app.db import Database

        with temp_data_dir() as tmp:
            db = Database(tmp / "explorer_dict.sqlite3")
            bid = db.create_batch("CLI 批次")
            db.add_entry(batch_id=bid, term="CLI 词条")
            db.close()

            env = dict(os.environ)
            env["EXPLORER_DICT_DATA_DIR"] = str(tmp)
            env["PYTHONUTF8"] = "1"

            proc = subprocess.run(
                [sys.executable, "-X", "utf8", "-m", "app.agent_api", "batches"],
                cwd=str(ROOT), env=env, capture_output=True, timeout=60,
            )
            out = proc.stdout.decode("utf-8", "replace")
            self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
            self.assertIn("CLI 批次", out)

            proc2 = subprocess.run(
                [sys.executable, "-X", "utf8", "-m", "app.agent_api", "search", "CLI"],
                cwd=str(ROOT), env=env, capture_output=True, timeout=60,
            )
            self.assertEqual(proc2.returncode, 0)
            self.assertIn("CLI 词条", proc2.stdout.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main()
