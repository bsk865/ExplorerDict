"""Process-local single-flight admission, deadlines and redacted request history.

Cancelled transports retain their slot until the worker really exits. A slow DNS
or socket call must never create an unlimited chain of replacement workers.
"""
from collections import deque
from dataclasses import dataclass, field
import hashlib
import json
import threading
import time

from .api_transport import ApiError


@dataclass(eq=False)
class Request:
    key: str
    route: str
    operation: object
    kind: str
    background: bool
    created: float
    deadline: float
    queue_deadline: float
    wait_for_recovery: bool = False
    cancel: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    subscribers: int = 0
    started: float | None = None
    status: str = 'queued'
    result: object = None
    error: object = None
    report: dict | None = None


class RequestSupervisor:
    def __init__(self, *, capacity=4, per_route=2, queue_limit=8, queue_timeout=8,
                 cooldown=15):
        self.capacity, self.per_route = capacity, per_route
        self.queue_limit, self.queue_timeout, self.cooldown = queue_limit, queue_timeout, cooldown
        self.lock = threading.RLock()
        self.requests, self.active, self.circuits = {}, set(), {}
        self.history = deque(maxlen=200)

    def _finish(self, task, *, result=None, error=None):
        if task.done.is_set():
            return
        now = time.monotonic()
        task.result, task.error = result, error
        task.status = error.kind if isinstance(error, ApiError) else 'done'
        task.report = {'kind': task.kind, 'status': task.status,
                       'queue_ms': round(1000 * ((task.started or now) - task.created)),
                       'elapsed_ms': round(1000 * (now - task.created)),
                       'background': task.background}
        self.history.append(task.report)
        from .logging_setup import get_logger
        get_logger('api.requests').info('request kind=%s status=%s queue_ms=%s elapsed_ms=%s background=%s',
            task.kind, task.status, task.report['queue_ms'], task.report['elapsed_ms'], task.background)
        if task.started is not None:
            if isinstance(error, ApiError) and error.kind in {'timeout', 'network', 'server', 'rate_limit', 'tls'}:
                failures = self.circuits.get(task.route, (0, 0))[0] + 1
                self.circuits[task.route] = (failures, now + min(60, self.cooldown * failures))
            elif error is None:
                self.circuits.pop(task.route, None)
        if self.requests.get(task.key) is task:
            self.requests.pop(task.key)
        task.done.set()

    def _dispatch(self):
        now = time.monotonic()
        for task in list(self.requests.values()):
            if now >= task.deadline:
                task.cancel.set()
                self._finish(task, error=ApiError('timeout', '等待时间过长，本次请求已结束，请稍后重试。'))
            elif task.started is None and now >= task.queue_deadline:
                if task.wait_for_recovery and self.circuits.get(task.route, (0, 0))[1] > now:
                    task.queue_deadline = min(task.deadline, self.circuits[task.route][1] + self.queue_timeout)
                    continue
                self._finish(task, error=ApiError('busy', '当前请求较多，请稍后重试。'))
        queued = sorted((t for t in self.requests.values() if t.started is None),
                        key=lambda t: (t.background, t.created))
        for task in queued:
            if self.circuits.get(task.route, (0, 0))[1] > now:
                if task.wait_for_recovery:
                    continue
                self._finish(task, error=ApiError('busy', '服务响应较慢，请稍后重试。'))
                continue
            if len(self.active) >= self.capacity:
                break
            if sum(t.route == task.route for t in self.active) >= self.per_route:
                continue
            if task.route in self.circuits and any(t.route == task.route for t in self.active):
                continue
            if task.background and any(t.background for t in self.active):
                continue
            task.started, task.status = now, 'running'
            self.active.add(task)
            threading.Thread(target=self._execute, args=(task,), daemon=True).start()

    def _execute(self, task):
        result, error = None, None
        try:
            result = task.operation(task.cancel, max(.001, task.deadline - time.monotonic()))
        except ApiError as exc:
            error = exc
        except Exception:
            error = ApiError('unknown', '本次请求未能完成，请稍后重试。')
        with self.lock:
            if time.monotonic() >= task.deadline and not task.done.is_set():
                task.cancel.set()
                error = ApiError('timeout', '等待时间过长，本次请求已结束，请稍后重试。')
            if task.cancel.is_set() and error is None:
                error = ApiError('cancelled', '本次请求已停止。')
            self._finish(task, result=result, error=error)
            if task.report is not None:
                task.report['transport_ms'] = round(1000 * (time.monotonic() - task.started))
            self.active.discard(task)
            self._dispatch()

    def run(self, *, url, payload, api_key, operation, timeout, cancel=None,
            background=False, kind='model', wait_for_recovery=False):
        if cancel is not None and cancel.is_set():
            raise ApiError('cancelled', '本次请求已停止。')
        route = hashlib.sha256((url + '\0' + api_key).encode()).hexdigest()
        key = hashlib.sha256((route + json.dumps(payload, ensure_ascii=False, sort_keys=True)).encode()).hexdigest()
        now = time.monotonic()
        caller_deadline = now + timeout
        with self.lock:
            self._dispatch()
            task = self.requests.get(key)
            if task is None:
                if any(t.key == key for t in self.active):
                    raise ApiError('busy', '上一请求正在结束，请稍后重试。')
                if not wait_for_recovery and self.circuits.get(route, (0, 0))[1] > now:
                    raise ApiError('busy', '服务响应较慢，请稍后重试。')
                if sum(t.started is None for t in self.requests.values()) >= self.queue_limit:
                    raise ApiError('busy', '当前请求较多，请稍后重试。')
                task = Request(key, route, operation, kind, background, now,
                               now + timeout, now + min(timeout, self.queue_timeout),
                               wait_for_recovery=wait_for_recovery)
                self.requests[key] = task
            elif not background:
                task.background = False
            task.subscribers += 1
            self._dispatch()
        try:
            while not task.done.wait(.05):
                if cancel is not None and cancel.is_set():
                    raise ApiError('cancelled', '本次请求已停止。')
                if time.monotonic() >= caller_deadline and caller_deadline < task.deadline:
                    raise ApiError('timeout', '等待时间过长，本次请求已结束，请稍后重试。')
                with self.lock:
                    self._dispatch()
            if cancel is not None and cancel.is_set():
                raise ApiError('cancelled', '本次请求已停止。')
            if time.monotonic() > caller_deadline and caller_deadline < task.deadline:
                raise ApiError('timeout', '等待时间过长，本次请求已结束，请稍后重试。')
            if task.error is not None:
                raise task.error
            return task.result
        finally:
            with self.lock:
                task.subscribers -= 1
                if task.subscribers == 0 and not task.done.is_set():
                    task.cancel.set()
                    self._finish(task, error=ApiError('cancelled', '本次请求已停止。'))
                self._dispatch()

    def snapshot(self):
        with self.lock:
            return {'active': len(self.active),
                    'queued': sum(t.started is None for t in self.requests.values()),
                    'cooling_down': sum(until > time.monotonic() for _, until in self.circuits.values()),
                    'running': [{'kind': t.kind, 'elapsed_ms': round(1000 * (time.monotonic() - t.started)),
                                 'background': t.background, 'cancel_requested': t.cancel.is_set()}
                                for t in self.active],
                    'history': [dict(row) for row in self.history]}


supervisor = RequestSupervisor()
