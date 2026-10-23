"""Local source retrieval. Every hit retains its exact article, block and offsets."""
from collections import Counter
import hashlib
import json
import math
import re

CHUNK_SIZE = 1400
OVERLAP = 180
STOP = set('the a an of to and or is are in on for with this that what how why which explain please'.split())


def tokens(text):
    result = []
    for run in re.findall(r'[a-z0-9]+|[\u3400-\u9fff]+', text.casefold()):
        if run.isascii():
            if run not in STOP:
                result.append(run)
        elif len(run) == 1:
            result.append(run)
        else:
            result.extend(run[i:i+2] for i in range(len(run)-1))
    return result


def split_text(text):
    start = 0
    while start < len(text):
        end = min(len(text), start + CHUNK_SIZE)
        if end < len(text):
            boundary = max(text.rfind(c, start + CHUNK_SIZE // 2, end) for c in '\n。.!?！？')
            if boundary >= 0:
                end = boundary + 1
        yield start, end, text[start:end]
        if end == len(text):
            break
        start = max(start + 1, end - OVERLAP)


class SourceIndex:
    def __init__(self, store, semantic=None):
        self.store = store
        self.semantic = semantic
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS source_chunks (id TEXT PRIMARY KEY, article_id TEXT NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS source_chunks_article ON source_chunks(article_id)')
            db.execute('CREATE TABLE IF NOT EXISTS source_indexes (article_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)')

    def ensure(self, article):
        # Include image descriptions: later visual indexing invalidates this cache.
        fingerprint = hashlib.sha256(json.dumps(article['blocks'], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.store.lock, self.store.connect() as db:
            row = db.execute('SELECT fingerprint FROM source_indexes WHERE article_id=?', (article['id'],)).fetchone()
            if row and row[0] == fingerprint:
                return
            db.execute('DELETE FROM source_chunks WHERE article_id=?', (article['id'],))
            for block in article['blocks']:
                text = block.get('text') if block['type'] == 'text' else block.get('caption', '')
                text = text or ''
                fragments = list(split_text(text)) or [(0, 0, '')]
                for start, end, quote in fragments:
                    key = f"{article['id']}:{block['id']}:{start}"
                    search = quote + ' ' + block.get('visual_description', '')
                    payload = {'article_id':article['id'], 'version':article['version'], 'article_title':article['title'],
                               'block':block['id'], 'start':start, 'end':end, 'quote':quote,
                               'page':block.get('page'), 'type':block['type'], 'asset':block.get('asset'),
                               'visual_description':block.get('visual_description', ''), 'terms':dict(Counter(tokens(search)))}
                    db.execute('INSERT INTO source_chunks VALUES(?,?,?)', (key, article['id'], json.dumps(payload, ensure_ascii=False)))
            db.execute('INSERT INTO source_indexes VALUES(?,?) ON CONFLICT(article_id) DO UPDATE SET fingerprint=excluded.fingerprint', (article['id'],fingerprint))

    def search(self, articles, question, *, focus=None, limit=10, budget=18000, text_only=False, extra_rows=None, diagnostics=None):
        for article in articles:
            self.ensure(article)
        ids = [a['id'] for a in articles]
        if not ids:
            return []
        with self.store.connect() as db:
            rows = [json.loads(r[0]) for r in db.execute('SELECT payload FROM source_chunks WHERE article_id IN ('+','.join('?' for _ in ids)+') ORDER BY rowid', ids)]
        rows=[r for r in rows if r['type']!='text' or r['quote'].strip()]
        if text_only:
            rows = [r for r in rows if r['type']=='text']
        rows.extend(extra_rows or [])
        if focus and focus.get('capture_context') and focus.get('block') is None:
            a=next(a for a in articles if a['id']==focus['article_id'])
            quote=focus['capture_context']
            rows.append({'article_id':a['id'],'version':a['version'],'article_title':a['title'],
                'block':None,'object_id':focus['id'],'start':0,'end':len(quote),'quote':quote,
                'page':None,'type':'text','asset':None,'visual_description':'',
                'terms':dict(Counter(tokens(quote)))})
        query = set(tokens(question))
        frequencies = Counter(t for row in rows for t in row['terms'] if t in query)
        avg = sum(sum(r['terms'].values()) for r in rows) / max(len(rows), 1) or 1
        scored = []
        nearby = set()
        if focus:
            article = next((a for a in articles if a['id']==focus['article_id']), None)
            if article:
                position = next((i for i,b in enumerate(article['blocks']) if b['id']==focus['block']), -1)
                nearby = {b['id'] for b in article['blocks'][max(0,position-2):position+3]}
        for ordinal, row in enumerate(rows):
            length = sum(row['terms'].values())
            score = 0.0
            for token in query:
                tf = row['terms'].get(token, 0)
                if tf:
                    idf = math.log(1 + (len(rows)-frequencies[token]+.5)/(frequencies[token]+.5))
                    score += idf * tf * 2.2 / (tf + 1.2 * (.25 + .75 * length/avg))
            if focus and row['article_id'] == focus['article_id'] and row['block'] == focus['block']:
                score += 100 if focus.get('title', '') in row['quote'] or row['type']=='image' else 1
            elif focus and row['article_id']==focus['article_id'] and row['block'] in nearby:
                score += 2
            scored.append((score, -ordinal, row))
        ranked = sorted(scored, key=lambda r:(r[0],r[1]), reverse=True)
        semantic_hits = []
        if self.semantic:
            try:
                semantic_hits = self.semantic.retrieve(articles, question, extra_rows)
                self.semantic.query_error = ''
            except Exception as exc:
                self.semantic.query_error = str(exc)
                if diagnostics is not None:
                    diagnostics['semantic_error'] = str(exc)
        if semantic_hits:
            # Reciprocal-rank fusion avoids comparing BM25's unbounded scores with cosine scores.
            lexical = [(score, ordinal, row) for score, ordinal, row in ranked if score > 0]
            fused = [[1/(60+i+1), ordinal, dict(row, keyword_score=score, retrieval=['keyword'])]
                     for i, (score, ordinal, row) in enumerate(lexical)]
            for rank, hit in enumerate(semantic_hits):
                match = next((entry for entry in fused if entry[2]['article_id'] == hit['article_id'] and
                              entry[2]['block'] == hit['block'] and entry[2].get('note_id') == hit.get('note_id') and entry[2].get('object_id') == hit.get('object_id') and
                              min(entry[2]['end'], hit['end'])-max(entry[2]['start'], hit['start']) >
                              .5*min(entry[2]['end']-entry[2]['start'], hit['end']-hit['start'])), None)
                if match:
                    if 'semantic' not in match[2]['retrieval']:
                        match[0] += 1/(60+rank+1)
                        match[2].update(semantic_score=hit['semantic_score'], retrieval=['keyword', 'semantic'])
                else:
                    fused.append([1/(60+rank+1), -len(rows)-rank, dict(hit, retrieval=['semantic'])])
            if focus:
                for entry in fused:
                    if entry[2]['article_id'] == focus['article_id'] and entry[2]['block'] == focus['block']:
                        entry[0] += 1
            ranked = sorted(fused, key=lambda r:(r[0],r[1]), reverse=True)
        if diagnostics is not None:
            diagnostics.update(matched=sum(score > 0 for score, _, _ in ranked), budget=budget,
                               mode='hybrid' if semantic_hits else 'keyword', semantic_hits=len(semantic_hits))
        if ranked and ranked[0][0] == 0:
            # General overview or no lexical match: disclose sampling to the model.
            count = min(limit, len(rows))
            indices = sorted({round(i*(len(rows)-1)/max(count-1,1)) for i in range(count)})
            ranked = [(0, -i, rows[i]) for i in indices]
        chosen = []
        used = 0
        for score, _, row in ranked:
            # If there are relevant hits, do not pad them with unrelated passages.
            if score <= 0 and chosen and ranked[0][0] > 0:
                break
            if any(r['article_id']==row['article_id'] and r['block']==row['block'] and r.get('note_id')==row.get('note_id') and r.get('object_id')==row.get('object_id') and min(r['end'],row['end'])-max(r['start'],row['start']) > min(r['end']-r['start'],row['end']-row['start'])*.5 for r in chosen):
                continue
            cost = len(row['quote']) + len(row.get('visual_description',''))
            if used + cost > budget:
                remaining = max(0, budget-used)
                if row['type'] not in {'text', 'note'} or remaining < 80:
                    continue
                # Preserve an exact substring around a query match when space is tight.
                quote = row['quote']
                positions = [quote.casefold().find(t) for t in query if t in quote.casefold()]
                offset = max(0, min(positions, default=0)-remaining//4)
                offset = min(offset, max(0, len(quote)-remaining))
                row = dict(row, quote=quote[offset:offset+remaining], start=row['start']+offset,
                           end=row['start']+offset+len(quote[offset:offset+remaining]))
                cost = len(row['quote'])
            chosen.append({k:v for k,v in row.items() if k!='terms'} | {'score':round(score, 4)})
            used += cost
            if len(chosen) >= limit:
                break
        # Pair retrieved PDF text with its actual page image; never claim to see it until sent.
        for hit in list(chosen):
            if hit['page'] is None or hit['type'] != 'text':
                continue
            image = next((r for r in rows if r['article_id']==hit['article_id'] and r['page']==hit['page'] and r['type']=='image'), None)
            if image and not any(r['article_id']==image['article_id'] and r['block']==image['block'] for r in chosen):
                chosen.append({k:v for k,v in image.items() if k!='terms'} | {'score':hit['score']})
        return [dict(row, citation=f'S{i+1}') for i,row in enumerate(chosen)]


def referenced_evidence(answer, evidence):
    allowed = {e['citation']:e for e in evidence}
    citations = re.findall(r'\[(S\d+)\]', answer)
    unknown = set(citations) - allowed.keys()
    if unknown:
        raise ValueError('回答引用了未提供的材料，请重试。')
    return [allowed[key] for key in dict.fromkeys(citations)]
