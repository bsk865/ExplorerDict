"""Idempotent bridge between the reading workspace and native selection records.

Links are content, not preferences: the maintenance reset clears them too.
AI explanations remain messages; they never become personal notes.
"""
from __future__ import annotations
import json
from contextlib import closing
from .db import Database


def synchronize(store):
    config = store.get('settings','model',{})
    if config and not config.get('shared'):
        store.save_config(store.model_config())
    path = store.directory / 'explorer_dict.sqlite3'
    if not path.exists():
        return 0
    with store.lock, closing(Database(path)) as native:
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS capture_links(entry_id INTEGER PRIMARY KEY, object_id TEXT, payload TEXT NOT NULL)')
            links = {r['entry_id']: json.loads(r['payload']) for r in db.execute('SELECT * FROM capture_links')}
        changed = 0
        entries = {r['id']: dict(r) for r in native.query('SELECT * FROM entries ORDER BY id')}
        for eid, link in list(links.items()):
            obj = store.get('objects',link['object_id'])
            if not link.get('saved') and obj and obj.get('saved'):
                del links[eid]
                with store.connect() as db:
                    db.execute('DELETE FROM capture_links WHERE entry_id=?',(eid,))
        # Import captures. A capture without archived full text is explicitly a
        # fragment and is never presented to retrieval as the complete article.
        for eid, entry in entries.items():
            if eid in links:
                continue
            source = native.query_one('SELECT * FROM articles WHERE doc_key=?', (entry['doc_key'],))
            text = source['text'] if source and source['text'] and entry['term'] in source['text'] else ''
            full = bool(text)
            text = text or entry['context'] or entry['term']
            if entry['term'] not in text:
                text = entry['term'] + '\n' + text
            title = (source['title'] if full else entry['source_title']) or '划选片段'
            article = store.import_article(title if full else title + ' · 划选片段', [{'type':'text','text':text}], origin='capture')
            article.update(source_url=entry['source_url'], source_status='legacy_text' if full else 'selection_fragment')
            store.update_article(article)
            obj = store.object(article['id'], {'type':'term','title':entry['term'],'block':'b0'})
            store.update_object(obj['id'], {'saved':True})
            links[eid] = {'object_id':obj['id'], 'saved':True, 'turns':{}, 'explanation':''}
            with store.connect() as db:
                db.execute('INSERT OR REPLACE INTO capture_links VALUES(?,?,?)',
                    (eid,obj['id'],json.dumps(links[eid],ensure_ascii=False)))
            changed += 1
        # Export saved words using a deterministic identity. Repeating a sync
        # never increments the capture count or duplicates an entry.
        linked = {link['object_id'] for link in links.values()}
        with store.connect() as db:
            objects = [json.loads(r[0]) for r in db.execute('SELECT payload FROM objects')]
        for obj in objects:
            if obj['type'] != 'term' or not obj.get('saved') or obj['id'] in linked:
                continue
            article = store.article(obj['article_id'])
            doc_key = 'workspace:' + article['id']
            batch = native.find_batch_by_source(doc_key)
            bid = batch['id'] if batch else native.create_batch(article['title'], source_key=doc_key, source_kind='workspace')
            context = next(b.get('text','') for b in article['blocks'] if b['id']==obj['block'])
            row = native.find_entry(bid, obj['title'], doc_key, context)
            eid = row['id'] if row else native.add_entry(batch_id=bid, term=obj['title'], context=context,
                doc_key=doc_key, source_title=article['title'], source_url=article.get('source_url',''), capture_method='workspace')
            entries[eid] = dict(native.get_entry(eid))
            links[eid] = {'object_id':obj['id'], 'saved':True, 'turns':{}, 'explanation':''}
            with store.connect() as db:
                db.execute('INSERT OR REPLACE INTO capture_links VALUES(?,?,?)',
                    (eid,obj['id'],json.dumps(links[eid],ensure_ascii=False)))
            changed += 1
        for eid, link in links.items():
            obj = store.get('objects', link['object_id'])
            if not obj:
                continue
            entry = entries.get(eid)
            # A deleted native record must not be resurrected by the next poll.
            if not entry:
                if link.get('saved'):
                    store.update_object(obj['id'], {'saved':False})
                    changed += 1
                link['saved'] = False
                continue
            if link.get('saved') and not obj.get('saved'):
                native.delete_entry(eid)
                link['saved'] = False
                changed += 1
                continue
            key = obj['article_id']+':'+obj['version']+':'+obj['id']
            session = store.get('sessions', key, {'messages':[], 'draft':''})
            before = json.dumps(session, sort_keys=True)
            messages = session.setdefault('messages', [])
            explanation = entry['detail'] or entry['one_line']
            if entry['explain_status'] == 'ok' and explanation and explanation != link.get('explanation'):
                message = next((m for m in messages if m.get('capture_explanation')==eid), None)
                if message is None:
                    message = {'role':'assistant','capture_explanation':eid}
                    messages.insert(0,message)
                message['content'] = explanation
                message['origin'] = 'capture'
                link['explanation'] = explanation
            turns = {r['id']:dict(r) for r in native.list_chat_turns(eid)}
            for tid, turn in turns.items():
                if turn['status'] != 'ok':
                    continue
                message = next((m for m in messages if m.get('capture_turn')==tid), None)
                if message is None:
                    messages.append({'role':turn['role'],'content':turn['content'],'capture_turn':tid,'origin':'capture'})
                elif message.get('origin') == 'capture':
                    message['content'] = turn['content']
            for message in messages:
                if message.get('capture_turn') and message['capture_turn'] not in turns:
                    message.pop('capture_turn')
                if message.get('capture_explanation') or message.get('capture_turn'):
                    continue
                tid = native.add_chat_turn(entry_id=eid,role=message['role'],content=message['content'])
                message['capture_turn'] = tid
            # The float's compact word card uses the first reading answer.
            if not explanation:
                answer = next((m['content'] for m in messages if m['role']=='assistant'), '')
                if answer:
                    native.update_entry(eid,detail=answer,one_line=answer.split('\n')[0][:180],explain_status='ok')
                    link['explanation'] = answer
            if json.dumps(session,sort_keys=True) != before:
                store.put('sessions',key,session)
                changed += 1
            link['saved'] = bool(obj.get('saved'))
        with store.connect() as db:
            for eid, link in links.items():
                db.execute('INSERT INTO capture_links VALUES(?,?,?) ON CONFLICT(entry_id) DO UPDATE SET payload=excluded.payload',
                    (eid,link['object_id'],json.dumps(link,ensure_ascii=False)))
        return changed
