"""Read and review every long-document segment; retain exact source evidence."""
import hashlib
import json
from . import article_map


def segments(text, limit=24000, overlap=500):
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


def run(store, client, *, text, title, version, signature, selected_words=(), cancel, progress, on_attempt, runner=None):
    runner = runner or article_map.run
    if len(text)<=article_map.MAX_TEXT_CHARS:
        return runner(client,text=text,selected_words=selected_words,on_attempt=on_attempt)
    pieces=list(segments(text))
    with store.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS map_parts(id TEXT PRIMARY KEY, version TEXT, payload TEXT)')
    graph={'format':article_map.FORMAT,'root':'document','nodes':[{'id':'document','parent':None,'kind':'branch','label':title[:96] or '文章脉络','quote':text[:120]}],
           'relations':[],'bindings':[],'source_sha256':hashlib.sha256(text.encode()).hexdigest()}
    bindings={}
    reports=[]
    for i,(start,end,source) in enumerate(pieces):
        if cancel.is_set():raise ValueError('已停止生成导图。')
        progress(f'正在研读第 {i+1}/{len(pieces)} 段…')
        selected=[j for j,w in enumerate(selected_words) if w in source]
        words=[selected_words[j] for j in selected]
        cache_key=hashlib.sha256(json.dumps([version,signature,start,end,words,hashlib.sha256(source.encode()).hexdigest(),article_map.POLICY_VERSION,'long-map-v1'],ensure_ascii=False).encode()).hexdigest()
        with store.connect() as db:
            row=db.execute('SELECT payload FROM map_parts WHERE id=?',(cache_key,)).fetchone()
        if row:
            result=json.loads(row[0]); part,quality=result['graph'],result['quality']
        else:
            def attempt(round_no,draft,issues,review):
                on_attempt(round_no,draft,issues,dict(review,segment=i+1,segments=len(pieces)))
                progress(f'第 {i+1}/{len(pieces)} 段 · 第 {round_no} 轮复核'+('，正在修正…' if issues else '完成'))
            part,quality=runner(client,text=source,selected_words=words,on_attempt=attempt)
            if cancel.is_set():raise ValueError('已停止生成导图。')
            if not quality.get('approved'):raise ValueError('本段导图尚未通过核对，已保留原图。')
            part=article_map.validate_draft(part,source,words)
            with store.lock, store.connect() as db:
                if cancel.is_set():raise ValueError('已停止生成导图。')
                db.execute('INSERT OR REPLACE INTO map_parts VALUES(?,?,?)',(cache_key,version,json.dumps({'graph':part,'quality':quality},ensure_ascii=False)))
        # Validate cached results too: old/malformed cache cannot bypass checks.
        part=article_map.validate_draft(part,source,words)
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
