"""测试用的假 API 服务（OpenAI / DeepSeek 兼容 chat/completions）。

也在命令行里单独启动做手工连通性验证：
    python tools/fake_api_server.py 8801
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeApiServer:
    """可编程响应的假服务。

    responder(request) -> (status:int, body:dict|str, delay:float)
    request = {"path": str, "headers": dict, "json": dict|None, "raw": str}
    """

    def __init__(self, responder=None, port: int = 0):
        self.responder = responder or self.default_responder
        self.requests: list[dict] = []
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = port

    # ------------------------------------------------------------ 生命周期
    def start(self) -> str:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # 静音
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
                try:
                    parsed = json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    parsed = None
                record = {
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "json": parsed,
                    "raw": raw,
                }
                outer.requests.append(record)
                status, body, delay = outer.responder(record)
                if delay:
                    time.sleep(delay)
                if isinstance(body, (dict, list)):
                    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
                    ctype = "application/json"
                else:
                    payload = str(body).encode("utf-8")
                    ctype = "application/json"
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="fake-api", daemon=True)
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=3)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    # ------------------------------------------------------------ 默认响应
    @staticmethod
    def default_responder(_request):
        return 200, {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "model": "fake-model",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "one_line": "这是一个假的解释。",
                                "detail": "由本地假服务返回，用于验证链路。",
                                "examples": ["示例一", "示例二"],
                            },
                            ensure_ascii=False,
                        ),
                    },
                    "finish_reason": "stop",
                }
            ],
        }, 0


def main() -> int:  # pragma: no cover - 手工验证用
    import sys

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8801
    server = FakeApiServer(port=port)
    server.start()
    print(f"假 API 服务启动: {server.base_url}  (Ctrl+C 退出)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()
        print("\n已停止")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
