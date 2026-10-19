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
