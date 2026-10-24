"""Read and review every long-document segment; retain exact source evidence."""
import hashlib
import json
from . import article_map

SEGMENT_CHARS = 8000


def segments(text, limit=SEGMENT_CHARS, overlap=500):
    start = 0
    while start < len(text):
        end = min(start+limit,len(text))
        if end < len(text):
            boundary=max(text.rfind(mark,start+limit//2,end) for mark in ('\n\n','。','！','？','. '))
            if boundary >= 0:
                end=boundary+1
        yield start,end,text[start:end]
        if end==len(text):break
        start=max(start+1,end-overlap)


def task_timeout(text):
    """Reserve time for independent reading, quote repair, generation and review."""
    count = sum(1 for _ in segments(text))
    return min(1800, 600 * count)


def run(store, client, *, text, title, version, signature, selected_words=(), cancel, progress, on_attempt, runner=None, force=False):
    pieces=list(segments(text))
    if not pieces:
        raise ValueError('这份资料尚无可整理的正文。')
    prepared=[]
    for start,end,source in pieces:
        selected=[j for j,w in enumerate(selected_words) if w in source]
        words=[selected_words[j] for j in selected]
        cache_key=hashlib.sha256(json.dumps([version,signature,start,end,words,hashlib.sha256(source.encode()).hexdigest(),article_map.POLICY_VERSION,'long-map-v2'],ensure_ascii=False).encode()).hexdigest()
        prepared.append((selected,words,cache_key))
    with store.lock, store.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS map_parts(id TEXT PRIMARY KEY, version TEXT, payload TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS map_stages(id TEXT PRIMARY KEY, version TEXT, payload TEXT)')
        if cancel.is_set():raise ValueError('已停止生成导图。')
        if force:
            # Invalidate every matching segment before requesting any new result.
            # A failed rebuild must never resume with untouched parts of the old map.
            keys=[(key,) for _,_,key in prepared]
            db.executemany('DELETE FROM map_parts WHERE id=?',keys)
            db.executemany('DELETE FROM map_stages WHERE id=?',keys)
    graph={'format':article_map.FORMAT,'root':'document','nodes':[{'id':'document','parent':None,'kind':'branch','label':title[:96] or '文章脉络','quote':text[:120]}],
           'relations':[],'bindings':[],'source_sha256':hashlib.sha256(text.encode()).hexdigest()}
    bindings={}
    reports=[]
    for i,(start,end,source) in enumerate(pieces):
        if cancel.is_set():raise ValueError('已停止生成导图。')
        progress(f'正在研读第 {i+1}/{len(pieces)} 段…')
        selected,words,cache_key=prepared[i]
        with store.connect() as db:
            row=db.execute('SELECT payload FROM map_parts WHERE id=?',(cache_key,)).fetchone()
        if row:
            result=json.loads(row[0]); part,quality=result['graph'],result['quality']
            progress(f'第 {i+1}/{len(pieces)} 段已整理')
        else:
            def attempt(round_no,draft,issues,review):
                on_attempt(round_no,draft,issues,dict(review,segment=i+1,segments=len(pieces)))
                progress(f'第 {i+1}/{len(pieces)} 段 · '+('正在完善导图…' if issues else '已整理'))
            if runner:
                part,quality=runner(client,text=source,selected_words=words,on_attempt=attempt)
            else:
                with store.connect() as db:
                    stage=db.execute('SELECT payload FROM map_stages WHERE id=?',(cache_key,)).fetchone()
                def checkpoint(state):
                    with store.lock, store.connect() as db:
                        if not cancel.is_set():
                            db.execute('INSERT OR REPLACE INTO map_stages VALUES(?,?,?)',
                                       (cache_key,version,json.dumps(state,ensure_ascii=False)))
                part,quality=article_map.run(client,text=source,selected_words=words,on_attempt=attempt,
                    cancel=cancel,progress=lambda message: progress(f'第 {i+1}/{len(pieces)} 段 · '+message),
                    resume=json.loads(stage[0]) if stage else None,checkpoint=checkpoint)
            if cancel.is_set():raise ValueError('已停止生成导图。')
            if not quality.get('approved'):raise ValueError('本段导图尚未通过核对，已保留原图。')
            part=article_map.validate_draft(part,source,words)
            with store.lock, store.connect() as db:
                if cancel.is_set():raise ValueError('已停止生成导图。')
                db.execute('INSERT OR REPLACE INTO map_parts VALUES(?,?,?)',(cache_key,version,json.dumps({'graph':part,'quality':quality},ensure_ascii=False)))
                db.execute('DELETE FROM map_stages WHERE id=?',(cache_key,))
        # Validate cached results too: old/malformed cache cannot bypass checks.
        part=article_map.validate_draft(part,source,words)
        if len(pieces) == 1:
            if selected != list(range(len(selected_words))):
                raise ValueError('有划选词未能关联到原文，请检查后重试。')
            return part,quality
        prefix=f'p{i+1}:'
        for node in part['nodes']:
            graph['nodes'].append(dict(node,id=prefix+node['id'],parent=prefix+node['parent'] if node['parent'] else 'document',segment=i+1))
        for relation in part['relations']:
            graph['relations'].append(dict(relation,id=prefix+relation['id'],src=prefix+relation['src'],dst=prefix+relation['dst']))
        for binding in part['bindings']:
            j=selected[binding['selection_index']]
            target=bindings.setdefault(j,dict(selection_index=j,node_ids=[],candidates=[],suggest_exclude=True,note=''))
            target['node_ids'].extend(prefix+n for n in binding['node_ids'])
            target['candidates'].extend(dict(c,node_id=prefix+c['node_id']) for c in binding['candidates'])
            target['suggest_exclude'] &= binding['suggest_exclude']
            if binding['note']:target['note']=(target['note']+' '+binding['note']).strip()
        reports.append({'start':start,'end':end,'quality':quality})
    if set(bindings)!=set(range(len(selected_words))):
        raise ValueError('有划选词未能关联到原文，请检查后重试。')
    graph['bindings']=list(bindings.values())
    graph['segments']=len(pieces)
    return graph,{'approved':True,'method':'segmented_reading','segments':reports,
                  'note':'各段分别阅读全文并复核；分段之间按原文顺序组织，不添加未经证实的关系。'}
