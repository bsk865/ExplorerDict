"""Fresh proxy routing, classified failures and bounded retries for model requests.

Never log keys, request bodies, response bodies, or proxy credentials. Diagnostics
are collected on the actual request, rather than issuing an extra paid preflight.
"""
from __future__ import annotations

from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import http.client
import json
import math
import random
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .logging_setup import get_logger, redact

log = get_logger("api.transport")
KIND_LABELS = {
    "schema": "生成未通过检查", "config": "配置有误", "network": "网络连接失败",
    "proxy": "代理无法连接", "dns": "地址解析失败", "tls": "安全连接失败",
    "timeout": "请求超时", "auth": "鉴权失败", "billing": "模型账户付费受限",
    "rate_limit": "请求过于频繁（限流）", "bad_request": "请求被拒绝",
    "model": "模型不可用", "endpoint": "接口地址有误", "server": "服务端错误",
    "bad_response": "响应非法", "unknown": "未知错误", "cancelled": "已停止解释",
    "busy": "暂时繁忙",
}


class ApiError(RuntimeError):
    def __init__(self, kind: str, message: str, *, status: int | None = None,
                 preview: str = "", retryable: bool = False, stage: str = "",
                 retry_after: float | None = None):
        super().__init__(message)
        self.kind, self.status, self.preview = kind, status, preview
        self.retryable, self.stage, self.retry_after = retryable, stage, retry_after
        self.attempts = 1
        self.retry_note = ""

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, KIND_LABELS["unknown"])

    def display(self) -> str:
        extra = f" (HTTP {self.status})" if self.status else ""
        note = f"；{self.retry_note}" if self.retry_note else ""
        return f"{self.label}{extra}：{self}{note}"


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 8.0
    # None preserves a long map request's configured timeout (e.g. 120 seconds).
    total_timeout: float | None = None
    jitter: float = 0.2

    def __post_init__(self):
        if not 0 <= self.max_retries <= 10:
            raise ValueError("max_retries must be between 0 and 10")
        for value in (self.base_delay, self.max_delay, self.jitter):
            if not math.isfinite(value) or value < 0:
                raise ValueError("retry intervals must be finite and nonnegative")
        if self.total_timeout is not None and (
                not math.isfinite(self.total_timeout) or self.total_timeout <= 0):
            raise ValueError("total_timeout must be positive and finite")

    def delay(self, retry_index: int) -> float:
        base = min(self.max_delay, self.base_delay * 2 ** retry_index)
        return min(self.max_delay, base + random.uniform(0, self.jitter * base))


def validate_endpoint(url: str, model: str = "") -> urllib.parse.SplitResult:
    try:
        target = urllib.parse.urlsplit(url)
        port = target.port
    except ValueError:
        raise ApiError("config", "接口地址或端口格式有误", stage="config") from None
    if target.scheme not in ("http", "https") or not target.hostname:
        raise ApiError("config", "接口地址必须是完整的 http:// 或 https:// 地址", stage="config")
    if target.username or target.password or target.query or target.fragment:
        raise ApiError("config", "接口地址不能带账号、密码、查询参数或片段", stage="config")
    if any(c.isspace() or ord(c) < 32 for c in url) or port == 0:
        raise ApiError("config", "接口地址包含空白或无效端口", stage="config")
    return target


def current_route(url: str) -> tuple[dict, str, urllib.parse.SplitResult | None]:
    """Take a new system/environment proxy snapshot on EVERY attempt."""
    target = validate_endpoint(url)
    proxies = urllib.request.getproxies()
    if urllib.request.proxy_bypass(target.netloc):
        proxies = {}
    proxy = proxies.get(target.scheme)
    if not proxy:
        return proxies, f"direct:{target.hostname}:{target.port or (443 if target.scheme == 'https' else 80)}", None
    try:
        parsed = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if not parsed.hostname or parsed.scheme not in ("http", "https"):
            raise ValueError()
    except ValueError:
        raise ApiError("config", "系统代理地址格式有误，请检查代理设置", stage="proxy") from None
    return proxies, f"proxy:{parsed.hostname}:{port}", parsed


def retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def http_error(status: int | None, detail: str, *, retry_after: float | None = None,
               secrets=()) -> ApiError:
    safe = redact(detail or "", secrets=secrets)[:1000]
    code = ""
    try:
        error = json.loads(detail).get("error", {})
        if isinstance(error, dict):
            code = str(error.get("code") or error.get("type") or "").lower()
    except (ValueError, AttributeError):
        pass
    # Explicit HTTP auth/billing verdicts take priority over gateway error text.
    if status in (401, 403):
        kind, message = "auth", "API Key 无效或没有权限，请在设置中检查 Key"
    elif status == 402 or code in ("insufficient_balance", "insufficient_quota", "billing_error"):
        kind, message = "billing", "请检查模型账户余额或付费状态，处理后再重试"
    elif code in ("invalid_api_key", "authentication_error", "permission_denied"):
        kind, message = "auth", "API Key 无效或没有权限，请在设置中检查 Key"
    elif code in ("model_not_found", "invalid_model", "model_not_available"):
        kind, message = "model", "模型不存在或当前账户不能使用，请检查模型名与权限"
    elif status == 404:
        kind, message = "endpoint", "接口或模型不存在，请检查 Base URL 和模型名"
    elif status == 429 or code in ("rate_limit_exceeded", "rate_limit_error"):
        kind, message = "rate_limit", "触发限流，请稍后重试"
    elif status in (408, 504):
        kind, message = "timeout", "服务端处理超时"
    elif status in (500, 502, 503) or code in ("server_error", "service_unavailable", "overloaded"):
        kind, message = "server", "模型服务暂时异常"
    elif status in (400, 405, 422):
        kind, message = "bad_request", "请求参数或模型设置被拒绝，请检查设置"
    else:
        kind, message = "unknown", "服务返回了未识别的错误"
    return ApiError(kind, message, status=status, preview=safe,
                    retryable=kind in ("rate_limit", "timeout", "server"),
                    stage="http", retry_after=retry_after)


def network_error(reason, *, using_proxy: bool, timeout: float) -> ApiError:
    # Don't put opaque exception strings (which may include credentials) in the UI.
    if isinstance(reason, ssl.SSLEOFError):
        return ApiError("network", "安全连接提前中断，请稍后重试", retryable=True, stage="tls")
    if isinstance(reason, ssl.SSLError):
        return ApiError("tls", "证书验证或安全连接失败，请检查系统时间和证书", stage="tls")
    if isinstance(reason, socket.gaierror):
        temporary = reason.errno in (getattr(socket, "EAI_AGAIN", -3), 11002)
        return ApiError("dns", "无法解析代理地址" if using_proxy else "无法解析服务地址",
                        retryable=temporary, stage="dns")
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return ApiError("timeout", f"连接或读取超时（本次上限 {timeout:g}s）",
                        retryable=True, stage="connection")
    refused = isinstance(reason, ConnectionRefusedError) or getattr(reason, "winerror", None) == 10061
    if refused and using_proxy:
        return ApiError("proxy", "代理端口拒绝连接，请启动代理或修正系统代理设置", stage="proxy")
    return ApiError("network", "目标服务拒绝连接" if refused else "连接中断或无法建立连接",
                    retryable=True, stage="connection")


def _read_response(resp, deadline: float, request_timeout: float, cancel: threading.Event,
                   limit: int | None = None) -> bytes:
    chunks = []
    size = 0
    while True:
        if cancel.is_set():
            raise ApiError("cancelled", "本次解释已停止", stage="cancel")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ApiError("timeout", "已达到本次请求的总等待上限", stage="read")
        # read1 avoids a trickling body continually extending the total deadline.
        sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            sock.settimeout(min(request_timeout, remaining))
        chunk = resp.read1(min(65536, limit - size) if limit is not None else 65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
        if limit is not None and size >= limit:
            return b"".join(chunks)


def post_json(url: str, payload: dict, *, api_key: str, timeout: float,
              policy: RetryPolicy, cancel_event=None, on_retry=None) -> str:
    validate_endpoint(url)
    if not api_key:
        raise ApiError("config", "尚未配置 API Key（设置 → API Key）", stage="config")
    if not payload.get("model"):
        raise ApiError("config", "尚未配置模型名", stage="config")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ApiError("config", "超时设置必须为正数", stage="config")
    cancel = cancel_event if cancel_event is not None else threading.Event()
    budget = policy.total_timeout if policy.total_timeout is not None else max(60.0, timeout)
    deadline = time.monotonic() + budget
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for attempt in range(1, policy.max_retries + 2):
        if cancel.is_set():
            raise ApiError("cancelled", "本次解释已停止", stage="cancel")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ApiError("timeout", "已达到本次请求的总等待上限", stage="deadline")
        started = time.monotonic()
        route, proxy = "unresolved", None
        request_timeout = min(timeout, remaining)
        try:
            proxies, route, proxy = current_route(url)
            # A closed local proxy port takes ~2s to refuse on Windows. Check it
            # with a short deadline, and diagnose it instead of blindly retrying.
            if proxy and proxy.hostname in ("127.0.0.1", "localhost", "::1"):
                try:
                    with socket.create_connection((proxy.hostname, proxy.port or 80),
                                                  timeout=min(0.3, request_timeout)):
                        pass
                except OSError:
                    raise ApiError("proxy", "本地代理尚未就绪，请启动代理或修正系统代理设置", stage="proxy") from None
            opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
            req = urllib.request.Request(url, data=body, method="POST", headers={
                "Content-Type": "application/json", "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
            })
            with opener.open(req, timeout=request_timeout) as resp:
                text = _read_response(resp, deadline, request_timeout, cancel).decode("utf-8", "replace")
                if resp.status != 200:
                    raise http_error(resp.status, text, secrets=(api_key,))
            # Some compatible gateways return HTTP 200 containing an error.
            try:
                envelope = json.loads(text)
            except ValueError:
                envelope = None
            if isinstance(envelope, dict) and "error" in envelope and envelope.get("choices") is None:
                raise http_error(None, text, secrets=(api_key,))
            log.info("API attempt=%s route=%s result=ok elapsed_ms=%.1f", attempt, route,
                     (time.monotonic() - started) * 1000)
            return text
        except urllib.error.HTTPError as exc:
            try:
                detail = _read_response(exc, deadline, request_timeout, cancel, limit=2048).decode("utf-8", "replace")
            except (OSError, http.client.HTTPException):
                detail = ""
            finally:
                exc.close()
            error = http_error(exc.code, detail,
                               retry_after=retry_after_seconds(exc.headers.get("Retry-After") if exc.headers else None),
                               secrets=(api_key,))
        except urllib.error.URLError as exc:
            error = network_error(exc.reason, using_proxy=proxy is not None, timeout=request_timeout)
        except (OSError, http.client.HTTPException) as exc:
            error = network_error(exc, using_proxy=proxy is not None, timeout=request_timeout)
        except ApiError as exc:
            error = exc
        error.attempts = attempt
        log.info("API attempt=%s route=%s stage=%s kind=%s status=%s elapsed_ms=%.1f",
                 attempt, route, error.stage, error.kind, error.status,
                 (time.monotonic() - started) * 1000)
        if not error.retryable or attempt > policy.max_retries:
            if attempt > 1:
                error.retry_note = f"共尝试 {attempt} 次"
            raise error
        delay = policy.delay(attempt - 1)
        if error.retry_after is not None:
            delay = max(delay, error.retry_after)
        if delay > policy.max_delay or delay >= deadline - time.monotonic():
            error.retry_note = "已达到等待上限，请稍后重试"
            raise error
        if on_retry is not None:
            try:
                on_retry(attempt, policy.max_retries, delay, error)
            except Exception:
                log.warning("API retry progress callback failed")
        log.info("API retry=%s wait_seconds=%.3f", attempt, delay)
        if cancel.wait(delay):
            raise ApiError("cancelled", "本次解释已停止", stage="cancel")
