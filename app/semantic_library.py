"""Persistent local vectors with incremental, version-checked publication."""
import hashlib
import json
import threading
import time

from .local_embeddings import LocalEmbeddings, MODEL_ID


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class SemanticLibrary:
    def __init__(self, store, encoder=None):
        self.store = store
        self.encoder = encoder or LocalEmbeddings()
        self.lock = threading.RLock()
        self.running = False
        self.pending = False
        self.epoch = 0
        self.current = ''
        self.progress = [0, 0]
        self.last_error = ''
        self.query_error = ''
        self.closed = False
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS rag_documents (
                    id TEXT PRIMARY KEY, article_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    model TEXT NOT NULL, chunks INTEGER NOT NULL, error TEXT NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS rag_vectors (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL, article_id TEXT NOT NULL,
                    model TEXT NOT NULL, payload TEXT NOT NULL, vector BLOB NOT NULL);
                CREATE INDEX IF NOT EXISTS rag_vectors_article ON rag_vectors(article_id);
                CREATE INDEX IF NOT EXISTS rag_vectors_document ON rag_vectors(document_id);
            ''')

    def documents(self):
        result = {}
        with self.store.lock, self.store.connect() as db:
            visible = {a['id'] for a in self.store.summaries()}
            articles = {r['id']: json.loads(r['payload']) for r in db.execute('SELECT id,payload FROM articles') if r['id'] in visible}
            for aid, article in articles.items():
                blocks = [b for b in article['blocks'] if b['type'] == 'text' and b.get('text', '').strip()]
                value = {'id': 'article:'+aid, 'article_id': aid, 'version': article['version'],
                         'title': article['title'], 'blocks': blocks, 'type': 'text'}
                value['fingerprint'] = digest(value)
                result[value['id']] = value
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='study_notes'").fetchone():
                for row in db.execute('SELECT payload FROM study_notes'):
                    note = json.loads(row[0])
                    if note['article_id'] not in articles:
                        continue
                    value = {'id': 'note:'+note['id'], 'article_id': note['article_id'],
                             'version': note['version'], 'title': articles[note['article_id']]['title'],
                             'note_id': note['id'], 'note_title': note['title'],
                             'origin': note.get('origin', 'personal'), 'type': 'note',
                             'blocks': [{'id': note['block'], 'text': note['content']}]}
                    value['fingerprint'] = digest(value)
                    result[value['id']] = value
        return result

    def request(self):
        with self.lock:
            if self.closed:
                return
            if not self.encoder.available():
                self.last_error = '本地语义模型尚未安装，当前仅使用关键词检索。'
                return
            self.pending = True
            if self.running:
                return
            self.running = True
            threading.Thread(target=self._run, name='local-rag-index', daemon=True).start()

    def close(self):
        with self.lock:
            self.closed = True
            self.epoch += 1
            self.pending = False

    def reset(self):
        with self.lock:
            self.epoch += 1
            with self.store.lock, self.store.connect() as db:
                db.execute('DELETE FROM rag_vectors')
                db.execute('DELETE FROM rag_documents')
        self.request()

    def _run(self):
        try:
            while True:
                with self.lock:
                    if self.closed:
                        self.running = False
                        return
                    self.pending = False
                    epoch = self.epoch
                self.sync(epoch)
                with self.lock:
                    if not self.pending:
                        self.running = False
                        return
        except Exception as exc:
            with self.lock:
                self.last_error = str(exc)
                self.running = False
        finally:
            with self.lock:
                if not self.running:
                    self.current = ''

    def chunks(self, document):
        for block in document['blocks']:
            text = block['text']
            start = 0
            while start < len(text):
                end = min(len(text), start+240)
                if end < len(text):
                    boundary = max(text.rfind(c, start+120, end) for c in '\n。.!?！？')
                    if boundary >= 0:
                        end = boundary+1
                while self.encoder.token_count(text[start:end]) > 512:
                    end = start+(end-start)//2
                    if end <= start:
                        raise ValueError('无法切分此段文字。')
                row = {'article_id': document['article_id'], 'version': document['version'],
                       'article_title': document['title'], 'block': block['id'],
                       'start': start, 'end': end, 'quote': text[start:end], 'type': document['type'],
                       'page': block.get('page'), 'asset': None}
                if document['type'] == 'note':
                    row.update(source='personal_note', note_id=document['note_id'],
                               note_title=document['note_title'], origin=document['origin'])
                yield row
                if end == len(text):
                    break
                start = max(start+1, end-40)

    def sync(self, epoch=None):
        epoch = self.epoch if epoch is None else epoch
        documents = self.documents()
        with self.store.lock, self.store.connect() as db:
            existing = {r['id']: dict(r) for r in db.execute('SELECT * FROM rag_documents')}
            for key in existing.keys()-documents.keys():
                db.execute('DELETE FROM rag_vectors WHERE document_id=?', (key,))
                db.execute('DELETE FROM rag_documents WHERE id=?', (key,))
        for key, document in documents.items():
            old = existing.get(key)
            if old and old['fingerprint'] == document['fingerprint'] and old['model'] == MODEL_ID:
                continue
            with self.lock:
                if self.epoch != epoch:
                    return
                self.current = document['title']
                self.progress = [0, 0]
            with self.store.lock, self.store.connect() as db:
                db.execute('DELETE FROM rag_vectors WHERE document_id=?', (key,))
                db.execute('DELETE FROM rag_documents WHERE id=?', (key,))
            error, encoded = '', []
            try:
                rows = list(self.chunks(document))
                with self.lock:
                    self.progress = [0, len(rows)]
                for start in range(0, len(rows), 4):
                    with self.lock:
                        if self.epoch != epoch:
                            return
                    batch = rows[start:start+4]
                    vectors = self.encoder.encode([r['quote'] for r in batch])
                    encoded.extend((digest([key, r['block'], r['start']]), key, document['article_id'], MODEL_ID,
                                    json.dumps(r, ensure_ascii=False), v.tobytes()) for r, v in zip(batch, vectors))
                    with self.lock:
                        self.progress[0] = len(encoded)
            except Exception as exc:
                error = str(exc)
                encoded = []
            # Inference never holds the store lock. Revalidate before publishing a completed document.
            with self.lock, self.store.lock, self.store.connect() as db:
                live = self.documents().get(key)
                if self.epoch != epoch or not live or live['fingerprint'] != document['fingerprint']:
                    self.pending = True
                    continue
                db.executemany('INSERT OR REPLACE INTO rag_vectors VALUES(?,?,?,?,?,?)', encoded)
                db.execute('INSERT OR REPLACE INTO rag_documents VALUES(?,?,?,?,?,?,?)',
                           (key, document['article_id'], document['fingerprint'], MODEL_ID, len(encoded), error, time.time()))
                self.last_error = ''

    def status(self):
        documents = self.documents()
        with self.store.connect() as db:
            records = {r['id']: dict(r) for r in db.execute('SELECT * FROM rag_documents')}
        items = []
        for key, document in documents.items():
            record = records.get(key)
            current = record and record['fingerprint'] == document['fingerprint'] and record['model'] == MODEL_ID
            state = ('error' if record['error'] else 'ready' if record['chunks'] else 'empty') if current else 'pending'
            items.append({'id': key, 'article_id': document['article_id'], 'title': document.get('note_title') or document['title'],
                          'type': document['type'], 'state': state, 'chunks': record['chunks'] if current else 0,
                          'error': record['error'] if current else ''})
        with self.lock:
            return {'model': MODEL_ID, 'available': self.encoder.available(), 'running': self.running,
                    'current': self.current, 'progress': list(self.progress), 'error': self.last_error or self.query_error,
                    'documents': items, 'vectors': sum(i['chunks'] for i in items),
                    'ready': sum(i['state'] == 'ready' for i in items),
                    'pending': sum(i['state'] == 'pending' for i in items)}

    def retrieve(self, articles, question, extra_rows=None, limit=24):
        """Exact cosine search over the permitted live versions, including opt-in notes only."""
        versions = {a['id']: a['version'] for a in articles}
        sources = {(a['id'], b['id']): b.get('text', '') for a in articles for b in a['blocks'] if b['type'] == 'text'}
        if not versions or not self.encoder.available():
            return []
        live = self.documents()
        with self.store.connect() as db:
            records = list(db.execute('SELECT v.payload,v.vector,v.document_id,d.fingerprint FROM rag_vectors v JOIN rag_documents d ON d.id=v.document_id WHERE v.model=? AND v.article_id IN ('+
                                     ','.join('?' for _ in versions)+')', [MODEL_ID, *versions]))
        if not records:
            return []
        import numpy as np
        notes = {(n.get('note_id'), n['version']) for n in (extra_rows or [])}
        rows, vectors = [], []
        for record in records:
            row = json.loads(record['payload'])
            document = live.get(record['document_id'])
            if not document or document['fingerprint'] != record['fingerprint']:
                continue
            if row['type'] == 'note':
                if (row['note_id'], row['version']) not in notes:
                    continue
            elif versions.get(row['article_id']) != row['version']:
                continue
            elif sources.get((row['article_id'], row['block']), '')[row['start']:row['end']] != row['quote']:
                continue
            rows.append(row)
            vectors.append(np.frombuffer(record['vector'], dtype='<f4'))
        if not rows:
            return []
        query = self.encoder.query(question)
        scores = np.stack(vectors) @ query
        order = np.argsort(-scores, kind='stable')[:limit]
        return [dict(rows[i], semantic_score=round(float(scores[i]), 6)) for i in order if scores[i] >= .82]

    def inspect(self, key, offset=0):
        document = self.documents().get(key)
        if not document:
            raise ValueError('资料已删除。')
        with self.store.connect() as db:
            record = db.execute('SELECT * FROM rag_documents WHERE id=?', (key,)).fetchone()
            if not record or record['fingerprint'] != document['fingerprint'] or record['model'] != MODEL_ID:
                return {'items': [], 'total': 0, 'pending': True}
            rows = db.execute('SELECT payload FROM rag_vectors WHERE document_id=? ORDER BY rowid LIMIT 30 OFFSET ?',
                              (key, max(0, offset)))
            return {'items': [json.loads(r[0]) for r in rows], 'total': record['chunks'], 'pending': False}
