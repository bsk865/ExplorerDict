import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from app.workspace_server import server_should_exit, process_alive


class ServerLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.workspace = SimpleNamespace(last_seen=0, jobs={})

    @patch('app.workspace_server.time.monotonic', return_value=3600)
    def test_minimized_desktop_survives_an_hour_without_browser_requests(self, _):
        self.assertFalse(server_should_exit(self.workspace, owner_alive=True))
        self.assertTrue(server_should_exit(self.workspace))

    def test_closed_owner_exits_even_with_inflight_jobs(self):
        self.workspace.jobs = {'job': {'status':'running'}}
        self.assertTrue(server_should_exit(self.workspace, owner_alive=False))
        self.assertFalse(server_should_exit(self.workspace))

    def test_windows_owner_process_check(self):
        self.assertTrue(process_alive(os.getpid()))


if __name__ == '__main__':
    unittest.main()
