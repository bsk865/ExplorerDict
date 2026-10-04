"""日志：写项目内 data/logs/app.log，并对 API key 之类敏感串做脱敏。"""
from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler

from . import paths

# sk-xxxx / Bearer xxxx / api_key":"..." 一律脱敏
_PATTERNS = [
    re.compile(r"(sk-[A-Za-z0-9_\-]{4})[A-Za-z0-9_\-]+"),
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)(\S+)"),
    re.compile(r"(?i)(\"?(?:api[_-]?key|apikey|authorization)\"?\s*[:=]\s*\"?)([^\s\",}]+)"),
]

REDACTED = "***REDACTED***"

#: 已知密钥的**字面值**（DPAPI 解出的真实 Key）。有些服务商的 Key 不是 ``sk-``
#: 前缀，正则匹配不到，只能按值替换；这里保存不可变 tuple，跨线程读取安全。
_known_secrets: tuple[str, ...] = ()
_MAX_KNOWN_SECRETS = 8
#: 短于这个长度的值不登记（避免把普通文本误替换掉）
_MIN_SECRET_LEN = 4


def register_secret(value: str) -> None:
    """登记一个已知密钥值：日志 / 异常文本里出现它时一律替换掉。

    只要 Key 进过 :class:`app.config.Config`（设置页保存或读取 DPAPI 密文），
    它就会在这里登记一次 —— 因此**非 sk- 前缀**的第三方 Key 也不会泄漏。
    """
    global _known_secrets
    text = str(value or "")
    if len(text) < _MIN_SECRET_LEN or text in _known_secrets:
        return
    _known_secrets = (_known_secrets + (text,))[-_MAX_KNOWN_SECRETS:]


def forget_secrets() -> None:
    """清空已登记的密钥（测试用）。"""
    global _known_secrets
    _known_secrets = ()


def _all_secrets(secrets) -> list[str]:
    known = set(_known_secrets)
    for value in (secrets or ()):
        text = str(value or "")
        if len(text) >= _MIN_SECRET_LEN:
            known.add(text)
    return sorted(known, key=len, reverse=True)


def redact(text: str, *, secrets=()) -> str:
    """对敏感串脱敏。日志与错误消息都必须先过这里。

    :param secrets: 额外需要按值替换的密钥（例如本次请求快照里的 API Key）。
    """
    if not text:
        return text
    out = str(text)
    for secret in _all_secrets(secrets):
        if secret in out:
            out = out.replace(secret, REDACTED)
    for pat in _PATTERNS:
        out = pat.sub(lambda m: m.group(1) + REDACTED, out)
    return out


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - 防御
            return True
        red = redact(msg)
        if red != msg:
            record.msg = red
            record.args = ()
        return True


class RedactingFormatter(logging.Formatter):
    """先按常规格式渲染，再对**最终字符串**整体脱敏。

    :class:`_RedactingFilter` 只看得到 ``record.getMessage()``；而 ``log.exception``
    的异常链（traceback）是 Formatter 在格式化阶段**追加**到消息之后的 ——
    过滤器看不见，于是 ``raise RuntimeError(f"bad key {key}")`` 这类异常仍会把
    Key 写进日志。因此在 ``format()`` 的返回值上再脱敏一次：普通消息、
    异常类型 / 异常文本 / traceback 帧里的字面值都覆盖到。
    """

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        text = super().format(record)
        try:
            return redact(text)
        except Exception:  # pragma: no cover - 脱敏本身失败也不吞日志
            return text


_configured = False


def setup_logging(level: int = logging.INFO, console: bool = False) -> logging.Logger:
    global _configured
    logger = logging.getLogger("explorer_dict")
    if _configured:
        return logger
    logger.setLevel(level)
    logger.propagate = False

    # 所有自己的 handler 都用 RedactingFormatter：过滤器管 getMessage，
    # 格式化器管「消息 + 异常链」的最终输出（两道一起才覆盖 log.exception）。
    fmt = RedactingFormatter(
        "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        fh = RotatingFileHandler(
            paths.log_path(), maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        fh.addFilter(_RedactingFilter())
        logger.addHandler(fh)
    except OSError:
        pass

    if console:
        ch = logging.StreamHandler(stream=sys.stderr)
        ch.setFormatter(fmt)
        ch.addFilter(_RedactingFilter())
        logger.addHandler(ch)

    if not logger.handlers:
        logger.addHandler(logging.NullHandler())

    _configured = True
    return logger


def get_logger(name: str = "app") -> logging.Logger:
    return logging.getLogger(f"explorer_dict.{name}")


def reset_logging() -> None:
    """关闭并移除所有处理器（测试用；也用于切换日志目录）。"""
    global _configured
    logger = logging.getLogger("explorer_dict")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # pragma: no cover
            pass
    _configured = False
