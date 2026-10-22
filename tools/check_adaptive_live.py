"""Use the configured model with synthetic goals and isolated storage only."""
import json
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.paths import data_dir
from app.workspace_store import WorkspaceStore
from app.workspace_server import Workspace
from app.study_advisor import plan

config=WorkspaceStore(data_dir()).model_config()
with tempfile.TemporaryDirectory(prefix='adaptive-live-') as directory:
    w=Workspace(Path(directory))
    w.store.model_config=lambda:config
    job={'cancel':threading.Event()}
    started=time.monotonic()
    plan(w,job,{'request':'明天上午要开会，下午需要读完一篇公开科普文章，顺便整理心得。','article_ids':[]})
    result=job['result']
    tomorrow=(datetime.now()+timedelta(days=1)).strftime('%Y-%m-%d')
    assert result.get('batch_id') and not result['questions'], 'Model unnecessarily asked a question'
    assert all(t['date']==tomorrow and '12:00'<=t['time']<'18:00' for t in result['tasks'])
    print(json.dumps({'model':config.get('model'),'automatic_plan':True,'tasks':len(result['tasks']),'seconds':round(time.monotonic()-started,1)},ensure_ascii=True))
