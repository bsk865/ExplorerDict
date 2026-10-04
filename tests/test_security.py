"""T9 密钥安全：DPAPI 往返、settings/日志/数据库文件中不得出现明文 key。"""
from __future__ import annotations

import unittest
from pathlib import Path

from tests.support import temp_data_dir, temp_db, tmp_dir

from app import crypto_dpapi, paths
from app.config import Config
from app.db import Database
from app.logging_setup import redact, setup_logging

SECRET = "sk-super-secret-abcdef1234567890"


class TestDpapi(unittest.TestCase):
    def test_roundtrip(self):
        blob = crypto_dpapi.protect(SECRET)
        self.assertNotIn(SECRET.encode("utf-8"), blob, "密文里不能出现明文")
        self.assertEqual(crypto_dpapi.unprotect(blob), SECRET)

    def test_ciphertext_differs_each_time(self):
        a = crypto_dpapi.protect(SECRET)
        b = crypto_dpapi.protect(SECRET)
        self.assertNotEqual(a, b, "DPAPI 每次加密结果应不同（含随机盐）")

    def test_corrupted_ciphertext_raises(self):
        with self.assertRaises(crypto_dpapi.DpapiError):
            crypto_dpapi.unprotect(b"not-a-real-blob")
        with self.assertRaises(crypto_dpapi.DpapiError):
            crypto_dpapi.unprotect(b"")


class TestNoPlaintextKey(unittest.TestCase):
    def test_secret_not_in_settings_table(self):
        with temp_db() as db:
            cfg = Config(db)
            cfg.set_api_key(SECRET)
            self.assertTrue(cfg.has_api_key())
            self.assertEqual(cfg.api_key(), SECRET)

            settings = db.all_settings()
            for k, v in settings.items():
                self.assertNotIn(SECRET, str(v), f"settings[{k}] 泄漏了明文 key")
            self.assertEqual(settings.get("api.key_saved"), "1")

    def test_plaintext_key_not_in_db_file(self):
        with tmp_dir("secret_") as tmp:
            path = Path(tmp) / "s.sqlite3"
            db = Database(path)
            cfg = Config(db)
            cfg.set_api_key(SECRET)
            db.log_event("test", f"尝试记录 {SECRET}")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.close()

            for f in Path(tmp).iterdir():
                data = f.read_bytes()
                self.assertNotIn(SECRET.encode("utf-8"), data,
                                 f"{f.name} 文件里出现了明文 key")

    def test_config_rejects_plaintext_key_setting(self):
        with temp_db() as db:
            cfg = Config(db)
            for bad in ("api.key", "api_key", "APIKEY"):
                with self.assertRaises(ValueError):
                    cfg.set(bad, SECRET)

    def test_clear_secret(self):
        with temp_db() as db:
            cfg = Config(db)
            cfg.set_api_key(SECRET)
            db.clear_secret("api_key")
            self.assertFalse(cfg.has_api_key())
            self.assertEqual(cfg.api_key(), "")
            self.assertEqual(db.get_setting("api.key_saved"), "0")


class TestLogRedaction(unittest.TestCase):
    def test_redact_patterns(self):
        self.assertNotIn(SECRET, redact(f"key={SECRET}"))
        self.assertNotIn("sk-abcdefghijklmn", redact("Authorization: Bearer sk-abcdefghijklmn"))
        self.assertNotIn(SECRET, redact(f'{{"api_key": "{SECRET}"}}'))

    def test_log_file_has_no_key(self):
        from app.logging_setup import reset_logging

        with temp_data_dir() as tmp:
            reset_logging()
            logger = setup_logging(console=False)
            logger.info("使用 key %s 调用", SECRET)
            logger.info('{"api_key": "%s"}', SECRET)
            for h in logger.handlers:
                h.flush()

            log_file = Path(tmp) / "logs" / "app.log"
            self.assertTrue(log_file.exists(), "日志文件应已创建")
            content = log_file.read_text(encoding="utf-8")
            self.assertNotIn(SECRET, content, "日志里出现了明文 key")
            self.assertIn("REDACTED", content)
            reset_logging()


class TestExceptionChainRedaction(unittest.TestCase):
    """``log.exception`` 的异常链也必须脱敏（过滤器只覆盖 getMessage）。

    ``_RedactingFilter`` 改写的是 ``record.getMessage()``，而 traceback 是
    :class:`logging.Formatter` 在格式化阶段追加到消息之后的 —— 因此必须在
    **格式化后的完整输出**上再脱敏一次（``RedactingFormatter``）。
    这里断言的是「最终写进日志文件的完整文本」，不只是 ``getMessage()``。
    """

    #: 非 ``sk-`` 前缀的第三方 Key：正则匹配不到，只能按**值**替换
    NON_SK_SECRET = "thirdparty-key-9f3c1b7a2e"
    #: ``sk-`` 前缀的 Key：正则能匹配
    SK_SECRET = "sk-exception-chain-abcdef123456"

    def setUp(self):
        from app.logging_setup import forget_secrets, register_secret

        forget_secrets()
        register_secret(self.NON_SK_SECRET)
        register_secret(self.SK_SECRET)
        self.addCleanup(forget_secrets)

    def test_exception_chain_and_normal_logs_are_fully_redacted(self):
        from app.logging_setup import reset_logging

        with temp_data_dir() as tmp:
            reset_logging()
            self.addCleanup(reset_logging)
            logger = setup_logging(console=False)

            # ① 普通日志：消息正文里的两种 Key
            logger.info("调用第三方服务 key=%s", self.NON_SK_SECRET)
            logger.info("Bearer %s", self.SK_SECRET)

            # ② log.exception：Key 在**异常链**里（异常文本 + raise 的帧）
            try:
                raise RuntimeError(f"上游拒绝，key={self.NON_SK_SECRET}")
            except RuntimeError:
                logger.exception("请求失败 sk=%s", self.SK_SECRET)
            try:
                raise ValueError(f"another leak {self.SK_SECRET}")
            except ValueError:
                logger.exception("第二次失败 非 sk=%s", self.NON_SK_SECRET)

            for h in logger.handlers:
                h.flush()

            log_file = Path(tmp) / "logs" / "app.log"
            content = log_file.read_text(encoding="utf-8")
            # 完整输出里必须两点都成立：不是 sk 形式的已登记假 Key 与 sk 形式都不在
            self.assertNotIn(self.NON_SK_SECRET, content,
                             "非 sk 形式的已登记假 Key 出现在日志完整输出里")
            self.assertNotIn(self.SK_SECRET, content,
                             "sk 形式的 Key 出现在日志完整输出里")
            self.assertNotIn("sk-exception-chain", content)
            # 异常链确实被写进日志（否则这条断言就是空过）
            self.assertIn("RuntimeError", content)
            self.assertIn("ValueError", content)
            self.assertIn("Traceback (most recent call last)", content)
            self.assertIn("REDACTED", content)

    def test_formatter_redacts_exception_text_not_only_getmessage(self):
        """直接对格式化器的返回值断言：过滤器放行后，异常链仍被脱敏。"""
        import logging
        import io

        from app.logging_setup import RedactingFormatter

        stream = io.StringIO()
        handler = logging.StreamHandler(stream=stream)
        handler.setFormatter(RedactingFormatter("%(message)s"))
        log = logging.getLogger("explorer_dict.test_exception_chain")
        log.setLevel(logging.INFO)
        log.propagate = False
        log.addHandler(handler)
        self.addCleanup(log.removeHandler, handler)
        try:
            raise KeyError(f"secret in args {self.NON_SK_SECRET} / {self.SK_SECRET}")
        except KeyError:
            log.exception("boom")
        handler.flush()
        output = stream.getvalue()
        self.assertNotIn(self.NON_SK_SECRET, output)
        self.assertNotIn(self.SK_SECRET, output)
        self.assertIn("KeyError", output)


class TestPathsStayInsideProject(unittest.TestCase):
    def test_all_paths_inside_project(self):
        for p in (paths.project_root(), paths.db_path(), paths.log_path(),
                  paths.uia_helper_script(), paths.ui_state_path()):
            self.assertTrue(paths.is_inside_project(p), f"{p} 不在项目目录内")

    def test_data_dir_override(self):
        with temp_data_dir() as tmp:
            self.assertEqual(paths.data_dir(), tmp)


if __name__ == "__main__":
    unittest.main()
