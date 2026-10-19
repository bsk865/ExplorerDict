"""Idempotent bridge between the reading workspace and native selection records.

Links are content, not preferences: the maintenance reset clears them too.
AI explanations remain messages; they never become personal notes.
"""
from __future__ import annotations
import json
from contextlib import closing
from .db import Database
from .capture_documents import document_identity, pending_article, captured_object, migrate_fragments


def _upgrade_source(store, native, entry, link):
    """Rebind a captured object when full text arrives, retaining evidence snapshots."""
    obj = store.get('objects', link['object_id'])
    if not obj:
        return False
    old = store.article(obj['article_id'])
    if old.get('source_status') not in {'selection_fragment','pending_source'}:
        return False
    source = native.query_one('SELECT * FROM articles WHERE doc_key=?', (entry['doc_key'],))
    if not source or not source['text'] or obj['title'] not in source['text'] or source['text'] == old['text']:
        return False
    article = store.import_article(source['title'] or entry['source_title'],
        [{'type':'text','text':source['text'],'capture_document':document_identity(entry)}], origin='capture')
    article.update(source_url=entry['source_url'], source_status='legacy_text', capture_document=document_identity(entry), capture_history=old.get('capture_history',[]))
    old_prefix = old['id'] + ':' + old['version'] + ':'
    new_prefix = article['id'] + ':' + article['version'] + ':'
    # Keep object identity, saved status and personal notes. Historical citations
    # still point to the archived immutable fragment they actually used.
    obj.update(article_id=article['id'], version=article['version'], block='b0')
    session = store.get('sessions', old_prefix + obj['id'])
    if session is not None:
        store.put('sessions', new_prefix + obj['id'], session)
    article_session = store.get('sessions', old_prefix)
    if article_session and not store.get('sessions', new_prefix):
        store.put('sessions', new_prefix, article_session)
    store.update_article(article)
    with store.connect() as db:
        db.execute('UPDATE objects SET article_id=?,payload=? WHERE id=?',
            (article['id'], json.dumps(obj, ensure_ascii=False), obj['id']))
    old['superseded_by'] = article['id']
    store.update_article(old)
    return True


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
        changed += migrate_fragments(store, entries, links)
        # Import captures. A capture without archived full text is explicitly a
        # fragment and is never presented to retrieval as the complete article.
        for eid, entry in entries.items():
            if eid in links:
                changed += int(_upgrade_source(store, native, entry, links[eid]))
                continue
            source = native.query_one('SELECT * FROM articles WHERE doc_key=?', (entry['doc_key'],))
            text = source['text'] if source and source['text'] and entry['term'] in source['text'] else ''
            full = bool(text)
            if full:
                article = store.import_article(source['title'] or entry['source_title'],
                    [{'type':'text','text':text,'capture_document':document_identity(entry)}],origin='capture')
                article.update(source_url=entry['source_url'],source_status='legacy_text')
                store.update_article(article)
            else:
                article = pending_article(store, entry)
            obj = captured_object(store, article, entry)
            store.update_object(obj['id'], {'saved':True})
            links[eid] = {'object_id':obj['id'], 'saved':True, 'turns':{}, 'explanation':''}
            with store.connect() as db:
                db.execute('INSERT OR REPLACE INTO capture_links VALUES(?,?,?)',
                    (eid,obj['id'],json.dumps(links[eid],ensure_ascii=False)))
            changed += 1
        # A native article group's edited title is shared by the reading workspace.
        for eid, link in links.items():
            entry=entries.get(eid)
            obj=store.get('objects',link['object_id'])
            if not entry or not obj:continue
            article=store.article(obj['article_id'])
            if article.get('origin')!='capture' and not article.get('capture_document'):continue
            batch=native.get_batch(entry['batch_id'])
            title=str(batch['name'] or '').strip() if batch else ''
            if title and article['title']!=title:
                article['title']=title
                store.update_article(article)
                changed+=1
        # Export saved words using a deterministic identity. Repeating a sync
        # never increments the capture count or duplicates an entry.
        linked = {link['object_id'] for link in links.values()}
        with store.connect() as db:
            objects = [json.loads(r[0]) for r in db.execute('SELECT payload FROM objects')]
        for obj in objects:
            if obj['type'] != 'term' or not obj.get('saved') or obj['id'] in linked:
                continue
            article = store.article(obj['article_id'])
            if article.get('superseded_by'):
                continue
            doc_key = 'workspace:' + article['id']
            batch = native.find_batch_by_source(doc_key)
            bid = batch['id'] if batch else native.create_batch(article['title'], source_key=doc_key, source_kind='workspace')
            context = obj.get('capture_context') or next((b.get('text','') for b in article['blocks'] if b['id']==obj['block']), obj['title'])
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
