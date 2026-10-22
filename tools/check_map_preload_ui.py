"""Isolated browser acceptance with a deterministic delayed map producer."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.workspace_server import Workspace, make_handler
from app import article_map

root = Path(tempfile.mkdtemp(prefix='map-preload-ui-'))
w = Workspace(root)
w.store.model_config = lambda: {'base_url': 'https://test.invalid', 'model': 'test', 'key': 'test'}
w.client = lambda: SimpleNamespace(base_url='https://test.invalid', model='test')
w.store.import_article('后台准备验证', [{'type': 'text', 'text': '资源有限，需要减少计算量。'}])
calls = []

def produce(*args, **kwargs):
    calls.append(kwargs['text'])
    time.sleep(3)
    return {'format': 'full_article_map_v1', 'root': 'n0',
            'nodes': [{'id': 'n0', 'parent': None, 'kind': 'branch', 'label': '资源约束', 'quote': kwargs['text']}],
            'relations': [], 'bindings': []}, {'approved': True}

class ModelHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        prompt = body['messages'][0]['content']
        material = json.loads(body['messages'][-1]['content'])
        calls.append(prompt)
        if os.environ.get('MAP_RETRY_FAILURES') == '1' and len(calls) in (1, 2):
            raw = b'{"error":{"message":"temporarily unavailable"}}'
            self.send_response(503)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        time.sleep(1)
        if prompt == article_map.READ:
            result = {'claims': [{'subject': '资源约束', 'predicate': '描述', 'object': '',
                                 'quote': material['text'], 'relation_required': False}], 'selections': []}
        elif prompt == article_map.GENERATE:
            result = {'root': 'n0', 'nodes': [{'id': 'n0', 'parent': None, 'kind': 'branch',
                      'label': '资源约束', 'quote': material['text']}], 'relations': [], 'bindings': []}
        elif prompt == article_map.REVIEW:
            result = {'node_verdicts': [{'id': 'n0', 'verdict': 'supported', 'note': '测试原文支持'}],
                      'relation_verdicts': [], 'binding_verdicts': [],
                      'claim_coverage': [{'claim_index': 0, 'node_ids': ['n0'], 'relation_ids': []}],
                      'coverage_ok': True, 'issues': [], 'reader_issues': []}
        else:
            raise AssertionError('Unexpected model request')
        raw = json.dumps({'choices': [{'message': {'content': json.dumps(result, ensure_ascii=False)}}]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


exe = os.environ.get('ADAPTIVE_EXE')
process = None
server = ThreadingHTTPServer(('127.0.0.1', 0), ModelHandler if exe else make_handler(w, 'preload-ui-test'))
threading.Thread(target=server.serve_forever, daemon=True).start()
info = root / 'info.json'
try:
    url = f'http://127.0.0.1:{server.server_port}/session?token=preload-ui-test'
    env = {**os.environ, 'ADAPTIVE_UI_INFO': str(info)}
    if exe:
        w.store.save_config({'base_url': f'http://127.0.0.1:{server.server_port}/v1', 'model': 'test', 'key': 'test'})
        env.update(EXPLORER_DICT_DATA_DIR=str(root), EXPLORER_DICT_STORAGE_CONFIG=str(root/'location.json'))
        process = subprocess.Popen([exe, '--headless'], env=env, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=0x08000000)
        for _ in range(200):
            if (root/'workspace-session-v7.json').exists():
                break
            if process.poll() is not None:
                raise RuntimeError('Packaged server failed to start')
            time.sleep(.1)
        url = json.loads((root/'workspace-session-v7.json').read_text())['url']
    info.write_text(json.dumps({'root': str(root), 'url': url}), encoding='utf-8')
    with patch('app.long_article_map.run', side_effect=produce):
        subprocess.run(['node', 'tools/check_map_preload_ui.cjs'], check=True,
                       env=env)
        expected = (8 if os.environ.get('MAP_RETRY_FAILURES') == '1' else 6) if exe else 2
        assert len(calls) == expected, f'Duplicate or missing model requests: {len(calls)}'
    if process:
        process.wait(timeout=15)
    print('PASS two articles, two total map productions; no duplicate request on reopen or foreground join')
    print('Evidence:', root)
finally:
    if process and process.poll() is None:
        process.terminate()
        process.wait(timeout=15)
    w.map_preloader.close()
    for job in w.jobs.values():
        job['cancel'].set()
    server.shutdown()
    server.server_close()
