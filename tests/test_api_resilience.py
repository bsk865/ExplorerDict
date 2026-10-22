"""Fault injection and real localhost HTTP checks, without paid API requests."""
from __future__ import annotations

import io
import json
import socket
import ssl
import threading
import time
import unittest
import urllib.request
from unittest import mock

from app.api_client import ApiError, DeepSeekClient, RetryPolicy
from app.api_transport import (current_route, http_error, network_error,
                               retry_after_seconds, validate_endpoint)
from tests.test_api import completion, FAKE_KEY
from tools.fake_api_server import FakeApiServer


class FakeWait:
    def __init__(self):
        self.waits = []
        self.now = 0.0
        self.cancelled = False

    def is_set(self):
        return self.cancelled

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += seconds
        return self.cancelled


class TestRequestResilience(unittest.TestCase):
    def setUp(self):
        self.server = FakeApiServer()
        self.server.start()
        self.addCleanup(self.server.stop)
        self.routing = mock.patch("app.api_transport.urllib.request.getproxies", return_value={})
        self.routing.start()
        self.addCleanup(self.routing.stop)
        self.addCleanup(mock.patch.stopall)

    def client(self, **overrides):
        client = DeepSeekClient(self.server.base_url, "fake-model", FAKE_KEY, timeout=2,
                                retry_policy=RetryPolicy(jitter=0, **overrides))
        self.wait = FakeWait()
        client.cancel_event = self.wait
        # These tests exercise raw transport retry policies. Admission and
        # no-replay behavior of managed requests are tested separately.
        from app.api_transport import post_json
        from app.api_client import endpoint_for
        client._post = lambda payload: post_json(endpoint_for(client.base_url), payload,
            api_key=FAKE_KEY, timeout=client.timeout, policy=client.retry_policy,
            cancel_event=client.cancel_event, on_retry=client.on_retry)
        return client

    def test_transient_failures_recover_with_same_body_and_key(self):
        statuses = iter([503, 429, 200])
        self.server.responder = lambda r: (
            next(statuses), completion('{"one_line":"恢复成功","detail":"","examples":[]}'), 0)
        client = self.client()
        progress = []
        client.on_retry = lambda *args: progress.append(args[:3])
        result = client.explain("原文依据", "所有关系依据原文。")
        self.assertEqual(result.one_line, "恢复成功")
        self.assertEqual(self.wait.waits, [1, 2])
        self.assertEqual(progress, [(1, 3, 1), (2, 3, 2)])
        self.assertEqual(len({r['raw'] for r in self.server.requests}), 1)
        self.assertTrue(all(r['headers']['authorization'] == 'Bearer ' + FAKE_KEY
                            for r in self.server.requests))

    def test_retries_are_finite_and_interval_is_capped(self):
        self.server.responder = lambda r: (503, {}, 0)
        client = self.client(base_delay=5, max_delay=8)
        with self.assertRaises(ApiError) as caught:
            client.explain("x", "c")
        self.assertEqual(self.wait.waits, [5, 8, 8])
        self.assertEqual(len(self.server.requests), 4)
        self.assertEqual(caught.exception.attempts, 4)

    def test_nontransient_errors_do_not_retry(self):
        for status in [400, 401, 402, 403, 404, 422, 501]:
            with self.subTest(status=status):
                self.server.requests.clear()
                self.server.responder = lambda r: (status, {}, 0)
                client = self.client()
                with self.assertRaises(ApiError):
                    client.explain("x", "c")
                self.assertEqual(len(self.server.requests), 1)
                self.assertEqual(self.wait.waits, [])

    def test_retry_after_is_respected_or_rejected_without_shortening(self):
        for interval, waits, count in [(3, [3, 3, 4], 4), (20, [], 1)]:
            with self.subTest(interval=interval):
                self.server.requests.clear()
                self.server.responder = lambda r: (429, {}, 0, {"Retry-After": str(interval)})
                client = self.client()
                with self.assertRaises(ApiError) as caught:
                    client.explain("x", "c")
                self.assertEqual(self.wait.waits, waits)
                self.assertEqual(len(self.server.requests), count)
                if interval > 8:
                    self.assertIn("等待上限", caught.exception.display())

    def test_total_budget_stops_before_another_attempt(self):
        self.server.responder = lambda r: (503, {}, 0)
        client = self.client(total_timeout=2.5)
        with mock.patch("app.api_transport.time.monotonic", side_effect=lambda: self.wait.now):
            with self.assertRaises(ApiError) as caught:
                client.explain("x", "c")
        self.assertEqual(self.wait.waits, [1])
        self.assertEqual(len(self.server.requests), 2)
        self.assertIn("等待上限", caught.exception.display())

    def test_cancel_during_backoff_never_posts_again(self):
        self.server.responder = lambda r: (503, {}, 0)
        client = self.client()
        client.cancel_event = threading.Event()
        client.on_retry = lambda *args: client.cancel_event.set()
        began = time.monotonic()
        with self.assertRaises(ApiError) as caught:
            client.explain("x", "c")
        self.assertEqual(caught.exception.kind, "cancelled")
        self.assertEqual(len(self.server.requests), 1)
        self.assertLess(time.monotonic() - began, 0.5)

    def test_global_opener_with_a_stale_proxy_is_not_used(self):
        stale = urllib.request.build_opener(urllib.request.ProxyHandler({
            'http': 'http://127.0.0.1:1'}))
        with mock.patch("urllib.request._opener", stale), \
                mock.patch.object(stale, "open", side_effect=AssertionError("stale route reused")):
            result = self.client().explain("x", "c")
        self.assertTrue(result.one_line)

    def test_route_changes_are_seen_between_attempts(self):
        self.server.responder = lambda r: (503 if len(self.server.requests) == 1 else 200,
                                           completion('{"one_line":"ok","detail":"","examples":[]}'), 0)
        with mock.patch("app.api_transport.urllib.request.getproxies", side_effect=[{}, {}]) as read:
            self.client().explain("x", "c")
        self.assertEqual(read.call_count, 2)

    def test_dead_local_proxy_is_diagnosed_without_sending_or_retrying(self):
        with mock.patch("app.api_transport.urllib.request.getproxies",
                        return_value={'http': 'http://user:private-proxy-pass@127.0.0.1:1'}), \
                mock.patch("app.api_transport.urllib.request.proxy_bypass", return_value=False):
            client = self.client()
            with self.assertRaises(ApiError) as caught:
                client.explain("x", "c")
        self.assertEqual(caught.exception.kind, "proxy")
        self.assertNotIn("private-proxy-pass", caught.exception.display())
        self.assertEqual(self.wait.waits, [])
        self.assertEqual(len(self.server.requests), 0)

    def test_http_200_errors_get_a_provider_verdict(self):
        for code, expected in [('invalid_api_key', 'auth'), ('insufficient_quota', 'billing'),
                               ('model_not_found', 'model'), ('rate_limit_exceeded', 'rate_limit')]:
            with self.subTest(code=code):
                self.server.requests.clear()
                self.server.responder = lambda r: (200, {'error': {'code': code, 'message': FAKE_KEY}}, 0)
                client = self.client()
                with self.assertRaises(ApiError) as caught:
                    client.explain("x", "c")
                self.assertEqual(caught.exception.kind, expected)
                self.assertNotIn(FAKE_KEY, caught.exception.preview + caught.exception.display())
                self.assertEqual(len(self.server.requests), 4 if expected == 'rate_limit' else 1)

    def test_invalid_configuration_never_reaches_http(self):
        for url in ['https://', 'https://host:xyz/v1', 'https://key:secret@host/v1',
                    'https://host/v1?key=secret', 'https://bad host/v1']:
            with self.subTest(url=url), self.assertRaises(ApiError):
                DeepSeekClient(url, 'm', FAKE_KEY).explain('x', 'c')
        self.assertEqual(self.server.requests, [])

    def test_audit_logging_does_not_contain_keys_or_material(self):
        self.server.responder = lambda r: (503, {}, 0)
        client = self.client(max_retries=0)
        with self.assertLogs('explorer_dict.api.transport', level='INFO') as records:
            with self.assertRaises(ApiError):
                client.explain('private-test-term', 'private-test-context')
        logs = '\n'.join(records.output)
        self.assertIn('attempt=1', logs)
        for secret in [FAKE_KEY, 'private-test-term', 'private-test-context']:
            self.assertNotIn(secret, logs)


class TestClassifications(unittest.TestCase):
    def test_network_stages_and_retryability(self):
        for reason, proxy, kind, retry in [
            (ssl.SSLCertVerificationError('certificate'), False, 'tls', False),
            (ssl.SSLEOFError('unexpected EOF'), False, 'network', True),
            (ssl.SSLError('handshake'), False, 'tls', False),
            (socket.gaierror(socket.EAI_NONAME, 'no name'), False, 'dns', False),
            (socket.gaierror(socket.EAI_AGAIN, 'try again'), False, 'dns', True),
            (ConnectionRefusedError(), True, 'proxy', False),
            (ConnectionRefusedError(), False, 'network', True),
            (TimeoutError(), False, 'timeout', True),
        ]:
            error = network_error(reason, using_proxy=proxy, timeout=1)
            self.assertEqual((error.kind, error.retryable), (kind, retry))

    def test_retry_after_dates_and_invalid_numbers(self):
        with mock.patch('app.api_transport.time.time', return_value=0):
            self.assertEqual(retry_after_seconds('Thu, 01 Jan 1970 00:00:05 GMT'), 5)
        for value in ['NaN', 'inf', 'bad-date']:
            self.assertIsNone(retry_after_seconds(value))
        self.assertEqual(retry_after_seconds('-2'), 0)

    def test_jitter_never_exceeds_interval_cap(self):
        policy = RetryPolicy(base_delay=7, max_delay=8, jitter=0.2)
        with mock.patch('app.api_transport.random.uniform', side_effect=lambda a, b: b):
            self.assertLessEqual(policy.delay(0), 8)
            self.assertEqual(policy.delay(5), 8)
