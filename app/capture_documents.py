"""Group captured fragments by source identity, never by selected text or title."""
import hashlib
import json


def document_identity(entry):
    key = entry['doc_key'] or 'unknown-entry:' + str(entry['id'])
    return hashlib.sha256(key.encode()).hexdigest()


def consolidate(store, entries, links):
    groups = {}
    for eid, link in links.items():
        entry = entries.get(eid)
        obj = store.get('objects', link['object_id'])
        if not entry or not obj or not entry['doc_key']:
            continue
        article = store.article(obj['article_id'])
        if article.get('source_status') != 'selection_fragment':
            continue
        group = groups.setdefault(document_identity(entry), {})
        group[article['id']] = article
    changed = 0
    for identity, articles in groups.items():
        if len(articles) < 2:
            continue
        blocks, locations, indices = [], {}, {}
        for old in articles.values():
            for block in old['blocks']:
                clean = {k:v for k,v in block.items() if k != 'id'}
                clean['capture_document'] = identity
                fingerprint = json.dumps(clean, sort_keys=True, ensure_ascii=False)
                if fingerprint not in indices:
                    indices[fingerprint] = len(blocks)
                    blocks.append(clean)
                locations[(old['id'], block['id'])] = 'b' + str(indices[fingerprint])
        first = next(iter(articles.values()))
        merged = store.import_article(first['title'], blocks, origin='capture')
        history = {}
        for old in articles.values():
            for item in old.get('capture_history',[]):
                history[item['id']] = item
            history[old['id']] = {'id':old['id'],'title':old['title'],'has_map':bool(old.get('map'))}
        history.pop(merged['id'], None)
        merged.update(source_status='selection_fragment', source_url=first.get('source_url',''),
                      capture_document=identity, capture_history=list(history.values()))
        store.update_article(merged)
        prefix = merged['id'] + ':' + merged['version'] + ':'
        # Commit rebindings and archives together. Old source blocks/sessions stay
        # immutable so previous answer citations and drafts remain recoverable.
        with store.connect() as db:
            article_sessions = []
            for old in articles.values():
                old_prefix = old['id'] + ':' + old['version'] + ':'
                session = db.execute('SELECT payload FROM sessions WHERE id=?',(old_prefix,)).fetchone()
                if session:
                    article_sessions.append(json.loads(session[0]))
                for obj in old['objects']:
                    obj.update(article_id=merged['id'], version=merged['version'],
                               block=locations[(old['id'],obj['block'])])
                    db.execute('UPDATE objects SET article_id=?,payload=? WHERE id=?',
                        (merged['id'],json.dumps(obj,ensure_ascii=False),obj['id']))
                    session = db.execute('SELECT payload FROM sessions WHERE id=?',(old_prefix+obj['id'],)).fetchone()
                    if session:
                        db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?)',(prefix+obj['id'],session[0]))
                if old['id'] != merged['id']:
                    old['superseded_by'] = merged['id']
                    old.pop('objects', None)
                    db.execute('UPDATE articles SET payload=? WHERE id=?',(json.dumps(old,ensure_ascii=False),old['id']))
            if article_sessions:
                combined = {'messages':[m for s in article_sessions for m in s.get('messages',[])]}
                for field in ('draft','note'):
                    combined[field] = '\n\n'.join(dict.fromkeys(s[field] for s in article_sessions if s.get(field)))
                db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?)',(prefix,json.dumps(combined,ensure_ascii=False)))
        changed += 1
    return changed

def pending_article(store, entry):
    """One stable article shell per document; capture context is never its body."""
    identity = document_identity(entry)
    with store.connect() as db:
        existing=[json.loads(r[0]) for r in db.execute('SELECT payload FROM articles')]
    for old in existing:
        if old.get('capture_document')==identity and not old.get('superseded_by') and old.get('source_status')!='selection_fragment':
            return store.article(old['id'])
    article = store.import_article(entry['source_title'] or '未命名文章',
        [{'type':'text','text':'','capture_document':identity}], origin='capture')
    article.update(source_status='pending_source', capture_document=identity,
                   source_url=entry.get('source_url',''))
    store.update_article(article)
    return article


def captured_object(store, article, entry):
    from .workspace_store import uid
    block = next((b['id'] for b in article['blocks'] if entry['term'] in b.get('text','')), None)
    if block is not None:
        obj = store.object(article['id'], {'type':'term','title':entry['term'],'block':block})
    else:
        obj = next((o for o in article['objects'] if o['type']=='term' and o['title']==entry['term']), None)
        if obj is None:
            obj={'id':uid(),'article_id':article['id'],'version':article['version'],
                 'type':'term','title':entry['term'],'block':None,'asset':None,'region':None,
                 'saved':True,'understood':False,'note':''}
    obj['capture_context'] = entry['context'] or entry['term']
    with store.connect() as db:
        db.execute('INSERT OR REPLACE INTO objects VALUES(?,?,?)',
            (obj['id'],article['id'],json.dumps(obj,ensure_ascii=False)))
    return obj


def migrate_fragments(store, entries, links):
    """Archive the old concatenated snapshots without deleting personal work."""
    changed=0
    for eid,link in links.items():
        entry=entries.get(eid);obj=store.get('objects',link['object_id'])
        if not entry or not obj:continue
        old=store.article(obj['article_id'])
        if old.get('source_status')!='selection_fragment':continue
        current=pending_article(store,entry)
        history={h['id']:h for h in current.get('capture_history',[])}
        for h in old.get('capture_history',[]):history[h['id']]=h
        history[old['id']]={'id':old['id'],'title':old['title'],'has_map':bool(old.get('map'))}
        current['capture_history']=list(history.values());store.update_article(current)
        old_prefix=old['id']+':'+old['version']+':'
        new_prefix=current['id']+':'+current['version']+':'
        with store.connect() as db:
            obj.update(article_id=current['id'],version=current['version'],block=None,
                       capture_context=entry['context'] or entry['term'])
            db.execute('UPDATE objects SET article_id=?,payload=? WHERE id=?',
                (current['id'],json.dumps(obj,ensure_ascii=False),obj['id']))
            for suffix in (obj['id'],''):
                row=db.execute('SELECT payload FROM sessions WHERE id=?',(old_prefix+suffix,)).fetchone()
                if row:
                    if suffix:db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?)',(new_prefix+suffix,row[0]))
                    else:
                        prior=db.execute('SELECT payload FROM sessions WHERE id=?',(new_prefix,)).fetchone()
                        value=json.loads(prior[0]) if prior else {};extra=json.loads(row[0])
                        for field in ('draft','note'):
                            value[field]='\n\n'.join(dict.fromkeys(v for v in (value.get(field,''),extra.get(field,'')) if v))
                        messages=value.get('messages',[])
                        for message in extra.get('messages',[]):
                            if message not in messages:messages.append(message)
                        value['messages']=messages
                        db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?)',(new_prefix,json.dumps(value,ensure_ascii=False)))
            old['superseded_by']=current['id'];old.pop('objects',None)
            db.execute('UPDATE articles SET payload=? WHERE id=?',(json.dumps(old,ensure_ascii=False),old['id']))
        changed+=1
    return changed

def attach_full_source(store, old, article):
    """Explicitly supplying original text rebinds existing words and sessions once."""
    article['capture_document']=old.get('capture_document')
    article['source_status']='readable'
    article['previous_id']=old['id']
    article['capture_history']=old.get('capture_history',[])
    store.update_article(article)
    before=old['id']+':'+old['version']+':'
    after=article['id']+':'+article['version']+':'
    with store.connect() as db:
        for obj in old['objects']:
            block=next((b['id'] for b in article['blocks'] if obj['title'] in b.get('text','')),None)
            obj.update(article_id=article['id'],version=article['version'],block=block)
            db.execute('UPDATE objects SET article_id=?,payload=? WHERE id=?',(article['id'],json.dumps(obj,ensure_ascii=False),obj['id']))
        for suffix in ['']+[o['id'] for o in old['objects']]:
            row=db.execute('SELECT payload FROM sessions WHERE id=?',(before+suffix,)).fetchone()
            if row:db.execute('INSERT OR REPLACE INTO sessions VALUES(?,?)',(after+suffix,row[0]))
    old['superseded_by']=article['id'];store.update_article(old)
    return store.article(article['id'])
