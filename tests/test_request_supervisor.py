import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app.api_transport import ApiError
from app.request_supervisor import RequestSupervisor
from app.api_client import DeepSeekClient, RetryPolicy
from tools.fake_api_server import FakeApiServer


class SupervisorTests(unittest.TestCase):
    def call(self, manager, operation, name='a', **kwargs):
        return manager.run(url='https://test.invalid/v1', payload={'model': 'test', 'text': name},
                           api_key='private-key', operation=operation, timeout=kwargs.pop('timeout', 2), **kwargs)

    def test_duplicate_call_shares_transport_and_cancel_is_per_subscriber(self):
        manager = RequestSupervisor()
        entered, release, cancel = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def operation(stop, remaining):
            calls.append(1)
            entered.set()
            release.wait(2)
            self.assertFalse(stop.is_set())
            return 'shared'
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.call, manager, operation, cancel=cancel, background=True)
            self.assertTrue(entered.wait(1))
            second = pool.submit(self.call, manager, operation)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                with manager.lock:
                    if next(iter(manager.active)).subscribers == 2:
                        break
                time.sleep(.01)
            cancel.set()
            with self.assertRaises(ApiError) as error:
                first.result(1)
            self.assertEqual(error.exception.kind, 'cancelled')
            release.set()
            self.assertEqual(second.result(1), 'shared')
        self.assertEqual(len(calls), 1)

    def test_stuck_transport_retains_slot_and_opens_cooldown_without_replay(self):
        manager = RequestSupervisor(capacity=1, queue_timeout=.1, cooldown=.15)
        release = threading.Event()
        def operation(stop, remaining):
            release.wait(2)
            return 'late answer'
        try:
            began = time.monotonic()
            with self.assertRaises(ApiError) as error:
                self.call(manager, operation, timeout=.12)
            self.assertEqual(error.exception.kind, 'timeout')
            self.assertLess(time.monotonic() - began, .5)
            self.assertEqual(manager.snapshot()['active'], 1)
            with self.assertRaises(ApiError) as duplicate:
                self.call(manager, lambda *_: self.fail('replayed stalled request'))
            self.assertEqual(duplicate.exception.kind, 'busy')
            self.assertEqual(manager.snapshot()['cooling_down'], 1)
        finally:
            release.set()
        deadline = time.monotonic() + 1
        while manager.snapshot()['active'] and time.monotonic() < deadline:
            time.sleep(.01)
        time.sleep(.16)
        self.assertEqual(self.call(manager, lambda *_: 'recovered'), 'recovered')

    def test_foreground_overtakes_waiting_background(self):
        manager = RequestSupervisor(capacity=1)
        entered, release = threading.Event(), threading.Event()
        order = []
        def active(*_):
            entered.set()
            release.wait(2)
        with ThreadPoolExecutor(3) as pool:
            first = pool.submit(self.call, manager, active, 'first')
            self.assertTrue(entered.wait(1))
            background = pool.submit(self.call, manager, lambda *_: order.append('background'), 'background', background=True)
            foreground = pool.submit(self.call, manager, lambda *_: order.append('foreground'), 'foreground')
            deadline = time.monotonic() + 1
            while manager.snapshot()['queued'] != 2 and time.monotonic() < deadline:
                time.sleep(.01)
            release.set()
            first.result(1); foreground.result(1); background.result(1)
        self.assertEqual(order, ['foreground', 'background'])

    def test_queue_timeout_and_redacted_audit(self):
        manager = RequestSupervisor(capacity=1, queue_timeout=.08)
        entered, release = threading.Event(), threading.Event()
        def active(*_):
            entered.set(); release.wait(2)
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.call, manager, active)
            self.assertTrue(entered.wait(1))
            try:
                with self.assertRaises(ApiError) as error:
                    self.call(manager, lambda *_: self.fail('expired queued request ran'), 'private-document')
                self.assertEqual(error.exception.kind, 'busy')
            finally:
                release.set()
            future.result(1)
        report = str(manager.snapshot())
        for secret in ('private-key', 'private-document', 'test.invalid'):
            self.assertNotIn(secret, report)

    def test_background_leaves_capacity_for_foreground(self):
        manager = RequestSupervisor(capacity=2, per_route=2)
        entered, release = threading.Event(), threading.Event()
        def background(*_):
            entered.set(); release.wait(2)
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.call, manager, background, background=True)
            self.assertTrue(entered.wait(1))
            try:
                self.assertEqual(self.call(manager, lambda *_: 'foreground', 'other'), 'foreground')
            finally:
                release.set()
            future.result(1)


class ManagedTransportTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeApiServer()
        self.server.start()
        self.addCleanup(self.server.stop)
        self.manager = RequestSupervisor(cooldown=.1)
        self.patch = patch('app.request_supervisor.supervisor', self.manager)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_slow_http_call_has_a_real_deadline_and_is_not_reposted(self):
        self.server.responder = lambda request: (200, {'choices': [{'message': {'content': '{"one_line":"late"}'}}]}, .6)
        client = DeepSeekClient(self.server.base_url, 'test', 'secret', timeout=.15,
                                retry_policy=RetryPolicy(max_retries=3, total_timeout=.15))
        began = time.monotonic()
        with self.assertRaises(ApiError) as error:
            client.explain('资料正文', '上下文')
        self.assertEqual(error.exception.kind, 'timeout')
        self.assertLess(time.monotonic() - began, .5)
        self.assertEqual(len(self.server.requests), 1)

    def test_cancel_returns_promptly_and_late_response_is_not_delivered(self):
        entered = threading.Event()
        def respond(request):
            entered.set()
            return 200, {'choices': [{'message': {'content': '{"one_line":"late"}'}}]}, .5
        self.server.responder = respond
        client = DeepSeekClient(self.server.base_url, 'test', 'secret', timeout=2)
        client.cancel_event = threading.Event()
        with ThreadPoolExecutor(1) as pool:
            answer = pool.submit(client.explain, '资料正文', '上下文')
            self.assertTrue(entered.wait(1))
            began = time.monotonic()
            client.cancel_event.set()
            with self.assertRaises(ApiError) as error:
                answer.result(.3)
            self.assertEqual(error.exception.kind, 'cancelled')
            self.assertLess(time.monotonic() - began, .3)
        self.assertEqual(len(self.server.requests), 1)

    def test_exhausted_task_budget_never_sends_another_request(self):
        client = DeepSeekClient(self.server.base_url, 'test', 'secret', timeout=120)
        client.request_deadline = time.monotonic() - 1
        with self.assertRaises(ApiError) as error:
            client.full_article_map(stage='read', text='原文')
        self.assertEqual(error.exception.kind, 'timeout')
        self.assertEqual(len(self.server.requests), 0)

    def test_transient_failures_recover_on_third_attempt(self):
        def respond(request):
            if len(self.server.requests) < 3:
                return 503, {'error': {'message': 'busy'}}, 0
            return FakeApiServer.default_responder(request)
        self.server.responder = respond
        client = DeepSeekClient(self.server.base_url, 'test', 'secret', timeout=1,
            retry_policy=RetryPolicy(base_delay=.01, jitter=0))
        self.assertTrue(client.explain('资料', '上下文').one_line)
        self.assertEqual(len(self.server.requests), 3)
        self.assertEqual(len(self.manager.snapshot()['history']), 1)

    def test_retry_exhaustion_is_bounded(self):
        self.server.responder = lambda request: (503, {'error': {}}, 0)
        client = DeepSeekClient(self.server.base_url, 'test', 'secret', timeout=1,
            retry_policy=RetryPolicy(base_delay=.01, jitter=0))
        with self.assertRaises(ApiError) as error:
            client.explain('资料', '上下文')
        self.assertEqual(len(self.server.requests), 3)
        self.assertIn('检查网络或服务设置', str(error.exception))

    def test_cancel_during_retry_wait_does_not_repost(self):
        self.server.responder = lambda request: (503, {'error': {}}, 0)
        waiting = threading.Event()
        client = DeepSeekClient(self.server.base_url, 'test', 'secret', timeout=2)
        client.cancel_event = threading.Event()
        client.on_retry = lambda *_: waiting.set()
        with ThreadPoolExecutor(1) as pool:
            result = pool.submit(client.explain, '资料', '上下文')
            self.assertTrue(waiting.wait(1))
            client.cancel_event.set()
            with self.assertRaises(ApiError) as error:
                result.result(.5)
            self.assertEqual(error.exception.kind, 'cancelled')
        self.assertEqual(len(self.server.requests), 1)

    def test_managed_call_waits_for_cooldown(self):
        self.server.responder = lambda request: (503, {'error': {}}, 0)
        client = DeepSeekClient(self.server.base_url, 'test', 'secret', timeout=1,
            retry_policy=RetryPolicy(max_retries=0))
        with self.assertRaises(ApiError):
            client.explain('资料', '上下文')
        self.server.responder = FakeApiServer.default_responder
        client.retry_policy = RetryPolicy(base_delay=.01, jitter=0)
        self.assertTrue(client.explain('资料', '上下文').one_line)
        self.assertEqual(len(self.server.requests), 2)

    def test_timeout_can_recover_within_total_budget(self):
        def respond(request):
            status, body, _ = FakeApiServer.default_responder(request)
            return status, body, .3 if len(self.server.requests) == 1 else 0
        self.server.responder = respond
        client = DeepSeekClient(self.server.base_url, 'test', 'secret', timeout=.1,
            retry_policy=RetryPolicy(base_delay=.01, jitter=0, total_timeout=1))
        self.assertTrue(client.explain('资料', '上下文').one_line)
        self.assertEqual(len(self.server.requests), 2)

    def test_auth_failure_does_not_retry(self):
        self.server.responder = lambda request: (401, {'error': {}}, 0)
        client = DeepSeekClient(self.server.base_url, 'test', 'secret', timeout=1)
        with self.assertRaises(ApiError) as error:
            client.explain('资料', '上下文')
        self.assertEqual(error.exception.kind, 'auth')
        self.assertEqual(len(self.server.requests), 1)
