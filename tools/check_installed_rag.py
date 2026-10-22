"""Verify the running local reader without printing its private session URL."""
import http.cookiejar
import json
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.paths import data_dir

session = json.loads((data_dir()/'workspace-session-v7.json').read_text(encoding='utf-8'))
parts = urlsplit(session['url'])
assert parts.hostname == '127.0.0.1'
base = parts.scheme+'://'+parts.netloc
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
with opener.open(session['url'], timeout=10) as response:
    response.read()
for _ in range(60):
    with opener.open(base+'/api/rag/status', timeout=10) as response:
        status = json.load(response)
    if not status['running'] and not status['pending']:
        break
    time.sleep(1)
assert status['available'] and not status['error'], status['error']
assert not status['pending'] and not status['running'], 'Index not yet complete'
assert all(d['state'] in ('ready','empty') for d in status['documents'])
report = {k:status[k] for k in ('model','available','ready','vectors','pending','running','error')}
report['document_states'] = [{k:d[k] for k in ('id','type','state','chunks')} for d in status['documents']]
Path('artifacts/installed-rag-status.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report))
