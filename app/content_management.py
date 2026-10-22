"""Explicit management of user content without deleting unrelated material."""
import json
from contextlib import closing

from .study_hub import StudyHub
from .study_activity import StudyActivity
from .reading_annotations import ReadingAnnotations


def inventory(store):
    hub = StudyHub(store)
    StudyActivity(store)
    ReadingAnnotations(store)
    data = hub.snapshot()
    data['articles'] = store.summaries()
    with store.connect() as db:
        articles = {r['id']:json.loads(r['payload']) for r in db.execute('SELECT id,payload FROM articles')}
        labels = {'yellow': '黄色荧光', 'green': '绿色荧光', 'pink': '粉色荧光', 'underline': '下划线'}
        data['annotations'] = []
        for row in db.execute('SELECT payload FROM reading_annotations ORDER BY rowid DESC'):
            mark = json.loads(row[0])
            data['annotations'].append(dict(mark, title=labels[mark['style']],
                                            article_title=articles.get(mark['article_id'], {}).get('title', '旧资料'),
                                            content='\n'.join(s['quote'] for s in mark['segments'])))
        objects = {r['id']:json.loads(r['payload']) for r in db.execute('SELECT id,payload FROM objects')}
        data['objects'] = [dict(o, article_title=articles.get(o['article_id'],{}).get('title','旧资料')) for o in objects.values()]
        data['conversations'] = []
        data['legacy_notes'] = []
        for row in db.execute('SELECT id,payload FROM sessions'):
            session = json.loads(row['payload'])
            aid = row['id'].split(':')[0]
            title = articles.get(aid,{}).get('title','资料库问答')
            oid = row['id'].split(':')[-1]
            if oid in objects:
                title += ' · '+objects[oid]['title']
            if session.get('messages') or session.get('draft') or session.get('archives') or session.get('handoff'):
                data['conversations'].append({'id':row['id'],'title':title,'count':len(session.get('messages',[])), 'archives':len(session.get('archives',[]))})
            if session.get('note'):
                data['legacy_notes'].append({'id':row['id'],'title':title,'content':session['note'],'source':'session'})
        data['legacy_notes'] += [{'id':o['id'],'title':o['title'],'content':o['note'],'source':'object'} for o in objects.values() if o.get('note')]
        data['batches'] = [json.loads(r[0]) for r in db.execute('SELECT payload FROM study_batches ORDER BY rowid DESC')]
        data['maps'] = [{'id':a['id'],'title':a['title']} for a in articles.values() if a.get('map') or db.execute('SELECT 1 FROM attempts WHERE article_id=?',(a['id'],)).fetchone()]
        data['reading'] = [{'id':r['article_id'],'title':articles.get(r['article_id'],{}).get('title','旧资料')} for r in db.execute('SELECT article_id FROM study_reading')]
    city = store.get('settings','study-weather-city')
    data['weather'] = [{'id':'city','title':city['name']}] if city else []
    return data


def remove(store, kind, key):
    if not isinstance(key,str) or not key:
        raise ValueError('请选择要删除的内容。')
    if kind in {'themes','notes','tasks','batches'}:
        return StudyHub(store).delete(kind,key)
    with store.lock:
        with store.connect() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            links = list(db.execute('SELECT entry_id,object_id FROM capture_links')) if 'capture_links' in tables else []
        if kind in {'objects','conversations'}:
            oid = key if kind=='objects' else key.split(':')[-1]
            native_path = store.directory/'explorer_dict.sqlite3'
            if native_path.exists():
                from .db import Database
                with closing(Database(native_path)) as native:
                    for link in links:
                        if link['object_id']==oid:
                            if kind=='objects':
                                native.delete_entry(link['entry_id'])
                            else:
                                native.delete_chat_turns(link['entry_id'])
        with store.connect() as db:
            if kind=='annotations':
                if 'reading_annotations' in tables:
                    db.execute('DELETE FROM reading_annotations WHERE id=?', (key,))
            elif kind=='objects':
                obj = store.get('objects',key)
                if obj:
                    db.execute('DELETE FROM sessions WHERE id=?',(obj['article_id']+':'+obj['version']+':'+key,))
                    db.execute('DELETE FROM objects WHERE id=?',(key,))
                    if 'capture_links' in tables:
                        db.execute('DELETE FROM capture_links WHERE object_id=?',(key,))
            elif kind=='conversations':
                session = store.get('sessions',key,{})
                # Independent article notes survive conversation removal.
                db.execute('UPDATE sessions SET payload=? WHERE id=?',(json.dumps({'messages':[],'draft':'','note':session.get('note','')},ensure_ascii=False),key))
            elif kind in {'object_note','session_note'}:
                table = 'objects' if kind=='object_note' else 'sessions'
                value = store.get(table,key)
                if value:
                    value['note']=''
                    db.execute(f'UPDATE {table} SET payload=? WHERE id=?',(json.dumps(value,ensure_ascii=False),key))
            elif kind=='maps':
                article = store.article(key)
                article.pop('objects',None)
                article['map']=None
                article['quality']=None
                db.execute('UPDATE articles SET payload=? WHERE id=?',(json.dumps(article,ensure_ascii=False),key))
                db.execute('DELETE FROM attempts WHERE article_id=?',(key,))
                if 'map_parts' in tables:
                    db.execute('DELETE FROM map_parts WHERE version=?',(article['version'],))
            elif kind=='reading':
                for table in ('study_reading','study_visits'):
                    if table in tables:
                        db.execute(f'DELETE FROM {table} WHERE article_id=?',(key,))
            elif kind=='weather' and key=='city':
                db.execute("DELETE FROM settings WHERE id IN ('study-weather-city','study-weather-cache')")
            else:
                raise ValueError('无法删除此类内容。')
    return {'ok':True}
