"""Versioned local reading workspace. Legacy database is strictly read-only."""
from __future__ import annotations
import base64
import hashlib
import io
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager, closing
from pathlib import Path
from PIL import Image, ImageOps
from . import crypto_dpapi


def uid():
    return uuid.uuid4().hex


class WorkspaceStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.path = self.directory / 'reading_workspace.sqlite3'
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS articles(id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY, mime TEXT NOT NULL, body BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS objects(id TEXT PRIMARY KEY, article_id TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY, article_id TEXT, payload TEXT NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, table, key, default=None):
        assert table in {'articles', 'objects', 'sessions', 'settings'}
        with self.connect() as db:
            row = db.execute(f'SELECT payload FROM {table} WHERE id=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, table, key, payload):
        assert table in {'sessions', 'settings'}
        with self.connect() as db:
            db.execute(f'INSERT INTO {table} VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload', (key, json.dumps(payload, ensure_ascii=False)))

    def article(self, aid):
        article = self.get('articles', aid)
        if not article:
            raise ValueError('这篇文章不存在，请刷新资料书架。')
        with self.connect() as db:
            article['objects'] = [json.loads(r[0]) for r in db.execute('SELECT payload FROM objects WHERE article_id=? ORDER BY rowid', (aid,))]
        return article

    def update_article(self, article):
        article = {k: v for k, v in article.items() if k != 'objects'}
        with self.connect() as db:
            db.execute('UPDATE articles SET payload=? WHERE id=?', (json.dumps(article, ensure_ascii=False), article['id']))

    def image(self, raw):
        if len(raw) > 24 * 1024 * 1024:
            raise ValueError('图片请小于24MB。')
        with Image.open(io.BytesIO(raw)) as im:
            if im.width * im.height > 40_000_000:
                raise ValueError('图片像素过大，请先缩小。')
            im = ImageOps.exif_transpose(im).convert('RGB')
            out = io.BytesIO()
            im.save(out, 'PNG')
            body = out.getvalue()
            size = im.size
        aid = hashlib.sha256(body).hexdigest()
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO assets VALUES(?,?,?)', (aid, 'image/png', body))
        return aid, size

    def asset(self, aid):
        with self.connect() as db:
            row = db.execute('SELECT mime,body FROM assets WHERE id=?', (aid,)).fetchone()
        if not row:
            raise ValueError('原图不存在。')
        return row['mime'], row['body']

    def import_article(self, title, blocks, *, origin='import', graph=None, quality=None):
        if not blocks or len(blocks) > 5000:
            raise ValueError('文章为空或段落过多。')
        canonical = json.dumps(blocks, ensure_ascii=False, sort_keys=True)
        fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
        with self.lock, self.connect() as db:
            old = db.execute('SELECT id FROM articles WHERE fingerprint=?', (fingerprint,)).fetchone()
            if old:
                return self.article(old[0])
            aid = uid()
            for i, block in enumerate(blocks):
                block['id'] = f'b{i}'
            article = {'id': aid, 'version': fingerprint, 'title': title[:200] or '未命名文章', 'blocks': blocks, 'origin': origin,
                       'map': graph, 'quality': quality, 'text': '\n\n'.join(b.get('text', b.get('caption', '')) for b in blocks)}
            db.execute('INSERT INTO articles VALUES(?,?,?)', (aid, fingerprint, json.dumps(article, ensure_ascii=False)))
        for b in blocks:
            if b['type'] == 'image':
                self.object(aid, {'type': 'image', 'title': b.get('caption') or '图片', 'block': b['id'], 'asset': b['asset']})
        return self.article(aid)

    def object(self, aid, obj):
        article = self.article(aid)
        block = next((b for b in article['blocks'] if b['id'] == obj.get('block')), None)
        if not block:
            raise ValueError('原文位置不存在。')
        kind = obj.get('type')
        if kind not in {'term', 'image', 'region'}:
            raise ValueError('探索对象类型不正确。')
        title = str(obj.get('title', '')).strip()[:200]
        if not title:
            raise ValueError('请输入名称。')
        if kind == 'term' and title not in block.get('text', ''):
            raise ValueError('所选词语不在这一段原文中。')
        region = obj.get('region')
        if kind != 'term':
            if block['type'] != 'image' or block.get('asset') != obj.get('asset'):
                raise ValueError('图片和原文位置不一致。')
            if kind == 'region':
                if not isinstance(region, list) or len(region) != 4 or any(type(v) not in (float, int) or not 0 <= v <= 1 for v in region):
                    raise ValueError('图片选区不正确。')
                x, y, w, h = region
                if w <= 0 or h <= 0 or x + w > 1.00001 or y + h > 1.00001:
                    raise ValueError('图片选区超出原图。')
        with self.lock, self.connect() as db:
            existing = [json.loads(r[0]) for r in db.execute('SELECT payload FROM objects WHERE article_id=?', (aid,))]
            match = next((o for o in existing if o['type'] == kind and o['block'] == block['id'] and o['title'] == title and o.get('region') == region), None)
            if match:
                return match
            record = {'id': uid(), 'article_id': aid, 'version': article['version'], 'type': kind, 'title': title, 'block': block['id'],
                      'asset': block.get('asset'), 'region': region, 'saved': False, 'understood': False, 'note': ''}
            db.execute('INSERT INTO objects VALUES(?,?,?)', (record['id'], aid, json.dumps(record, ensure_ascii=False)))
        return record

    def update_object(self, oid, changes):
        obj = self.get('objects', oid)
        if not obj:
            raise ValueError('对象不存在。')
        for key in ('saved', 'understood'):
            if key in changes:
                obj[key] = bool(changes[key])
        if 'note' in changes:
            obj['note'] = str(changes['note'])[:10000]
        with self.connect() as db:
            db.execute('UPDATE objects SET payload=? WHERE id=?', (json.dumps(obj, ensure_ascii=False), oid))
        return obj

    def summaries(self):
        with self.connect() as db:
            articles = [json.loads(r[0]) for r in db.execute('SELECT payload FROM articles ORDER BY rowid DESC')]
        return [{k: a.get(k) for k in ('id', 'title', 'version', 'origin', 'source_url', 'source_status')} | {'images': sum(b['type'] == 'image' for b in a['blocks']), 'chars': len(a['text']), 'has_map': bool(a.get('map'))} for a in articles if not a.get('superseded_by')]

    def model_config(self):
        config = self.get('settings', 'model', {})
        if config:
            config = dict(config)
            config['key'] = crypto_dpapi.unprotect(base64.b64decode(config['key'])) if config.get('key') else ''
            return config
        legacy = self.directory / 'explorer_dict.sqlite3'
        if not legacy.exists():
            return {'base_url': 'https://api.deepseek.com/v1', 'model': 'deepseek-chat', 'key': ''}
        with closing(sqlite3.connect(legacy.as_uri() + '?mode=ro', uri=True)) as db:
            settings = dict(db.execute('SELECT k,v FROM settings'))
            row = db.execute("SELECT blob FROM secrets WHERE name='api_key'").fetchone()
        return {'base_url': settings.get('api.base_url', ''), 'model': settings.get('api.model', ''),
                'key': crypto_dpapi.unprotect(row[0]) if row else ''}

    def save_config(self, body):
        from urllib.parse import urlsplit
        base = str(body.get('base_url', '')).strip().rstrip('/')
        parts = urlsplit(base)
        if parts.scheme not in {'https', 'http'} or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError('接口地址需要是完整的 HTTP/HTTPS 地址，不能带密码或查询参数。')
        model = str(body.get('model', '')).strip()
        if not model:
            raise ValueError('请输入模型名称。')
        key = str(body.get('key') or self.model_config().get('key', ''))
        self.put('settings', 'model', {'base_url': base, 'model': model, 'key': base64.b64encode(crypto_dpapi.protect(key)).decode() if key else ''})
        self.put('settings', 'vision', {})

    def sync_legacy(self):
        legacy = self.directory / 'explorer_dict.sqlite3'
        if not legacy.exists():
            return 0
        count = 0
        with closing(sqlite3.connect(legacy.as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            for row in db.execute("SELECT * FROM articles WHERE text<>''"):
                # Preserve the exact archived text for existing map fingerprints.
                blocks = [{'type': 'text', 'text': row['text']}]
                article = self.import_article(row['title'], blocks, origin='legacy')
                if not article.get('source_url'):
                    article['source_url'] = row['url']
                    article['source_status'] = 'legacy_text'
                    self.update_article(article)
                count += 1
                entries = list(db.execute('SELECT * FROM entries WHERE doc_key=?', (row['doc_key'],)))
                for e in entries:
                    b = next((b for b in article['blocks'] if e['term'] in b.get('text', '')), None)
                    if b:
                        existed = any(o['type'] == 'term' and o['title'] == e['term'] and o['block'] == b['id'] for o in article['objects'])
                        obj = self.object(article['id'], {'type': 'term', 'title': e['term'], 'block': b['id']})
                        if not existed:
                            self.update_object(obj['id'], {'saved': True, 'note': e['detail'] or e['one_line']})
                if not article.get('map'):
                    topics = sorted({e['batch_id'] for e in entries})
                    for tid in topics:
                        for cached in db.execute('SELECT raw FROM map_graphs WHERE topic_id=? ORDER BY id DESC', (tid,)):
                            try:
                                raw = json.loads(cached[0])
                                graph = raw.get('article_map', {})
                                if graph.get('source_sha256') == hashlib.sha256(article['text'].encode()).hexdigest() and raw.get('quality', {}).get('approved'):
                                    article['map'], article['quality'] = graph, raw['quality']
                                    self.update_article(article)
                                    break
                            except (ValueError, AttributeError):
                                continue
        return count
