"""Isolated browser acceptance for conversational planning."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.workspace_server import Workspace, make_handler

root = Path(tempfile.mkdtemp(prefix='adaptive-study-'))
w = Workspace(root)
w.store.model_config = lambda: {'base_url':'https://test.invalid','model':'test','key':'test'}
article = w.store.import_article('多模态 RAG 阅读', [{'type':'text','text':'检索增强生成与多模态学习。'}])
day = (datetime.now()+timedelta(days=1)).strftime('%Y-%m-%d')
client = Mock(model='test', base_url='https://test.invalid')
def answer(messages):
    time.sleep(1.3)
    context = json.loads(messages[-1]['content'])
    if not context['conversation']:
        return json.dumps({'message':'可以，阅读和心得一起安排。','questions':['你想安排在哪一天？'],'tasks':[]})
    assert context['conversation'][0]['content'] == '安排阅读多模态 RAG，然后写心得。'
    return json.dumps({'message':'按明天下午安排，先预留 45 分钟阅读。','questions':[], 'tasks':[{'title':'阅读多模态 RAG','description':'记录核心概念与一个疑问','goal':'理解论文','date':day,'time':'14:00','minutes':45,'article_ids':[article['id']]}]})
client.chat.side_effect = answer
w.client = lambda:client
exe = os.environ.get('ADAPTIVE_EXE')
class ModelHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        reply = json.dumps({'choices':[{'message':{'content':answer(body['messages'])}}]}).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

server = ThreadingHTTPServer(('127.0.0.1',0), ModelHandler if exe else make_handler(w,'isolated-adaptive-test'))
threading.Thread(target=server.serve_forever,daemon=True).start()
info = root/'info.json'
process = None
try:
    url = f'http://127.0.0.1:{server.server_port}/session?token=isolated-adaptive-test'
    env = {**os.environ,'ADAPTIVE_UI_INFO':str(info)}
    if exe:
        w.store.save_config({'base_url':f'http://127.0.0.1:{server.server_port}/v1','model':'test','key':'test'})
        env.update(EXPLORER_DICT_DATA_DIR=str(root),EXPLORER_DICT_STORAGE_CONFIG=str(root/'location.json'))
        process = subprocess.Popen([str(Path(exe).resolve()),'--headless'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=0x08000000)
        for _ in range(200):
            if (root/'workspace-session-v7.json').exists():break
            if process.poll() is not None:raise RuntimeError('Package failed to start')
            time.sleep(.1)
        url = json.loads((root/'workspace-session-v7.json').read_text())['url']
    info.write_text(json.dumps({'root':str(root),'url':url}),encoding='utf-8')
    subprocess.run(['node',os.environ.get('ADAPTIVE_CHECK_SCRIPT','tools/check_adaptive_ui.cjs')],env=env,check=True)
    if process:
        process.wait(timeout=15)
        subprocess.run([str(Path(exe).resolve()),'--verify-window'],env=env,check=True,timeout=55,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=0x08000000)
        report=json.loads((root/'window-verification.json').read_text())
        assert all(report.get(k) for k in ('rendered','maximize','restore','minimize','close_button'))
        print('PASS packaged planning API and Qt window checks')
    print('Evidence: '+str(root))
finally:
    if process and process.poll() is None:
        process.terminate()
        process.wait(timeout=15)
    server.shutdown()
    server.server_close()
