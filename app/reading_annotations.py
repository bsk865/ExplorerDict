"""Version-bound reading marks, separate from source text and retrieval indexes."""
import json
import uuid

from .study_hub import now
from .reading_text import block_text


class ReadingAnnotations:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS reading_annotations '
                       '(id TEXT PRIMARY KEY, article_id TEXT NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS reading_annotations_article ON reading_annotations(article_id)')

    def list(self, article_id):
        article = self.store.article(article_id)
        with self.store.connect() as db:
            items = [json.loads(row[0]) for row in db.execute(
                'SELECT payload FROM reading_annotations WHERE article_id=? ORDER BY rowid', (article_id,))]
        return {'items': [item for item in items if item['version'] == article['version']]}

    def save(self, body):
        with self.store.lock, self.store.connect() as db:
            article = self.store.article(body.get('article_id'))
            if body.get('version') != article['version']:
                raise ValueError('文章版本已变化，请重新打开后标注。')
            style = body.get('style')
            if style not in {'yellow', 'green', 'pink', 'underline'}:
                raise ValueError('请选择有效的标注样式。')
            segments = body.get('segments')
            if not isinstance(segments, list) or not 1 <= len(segments) <= 100:
                raise ValueError('请选择需要标注的正文，最多100段。')
            clean = []
            for segment in segments:
                if not isinstance(segment, dict):
                    raise ValueError('标注位置无效。')
                block = next((b for b in article['blocks'] if b['id'] == segment.get('block') and b['type'] == 'text'), None)
                start, end = segment.get('start'), segment.get('end')
                if not block or type(start) is not int or type(end) is not int:
                    raise ValueError('原文位置不存在。')
                source = block_text(block)
                if not 0 <= start < end <= len(source) or source[start:end] != segment.get('quote'):
                    raise ValueError('选中文字与原文不一致，请重新选择。')
                clean.append({'block': block['id'], 'start': start, 'end': end, 'quote': source[start:end]})
            if sum(len(s['quote']) for s in clean) > 50000:
                raise ValueError('一次最多标注50000字。')
            if body.get('id'):
                row = db.execute('SELECT payload FROM reading_annotations WHERE id=? AND article_id=?',
                                 (body['id'], article['id'])).fetchone()
                old = json.loads(row[0]) if row else None
                if not old or old['version'] != article['version'] or old['segments'] != clean:
                    raise ValueError('标注已变化，请刷新后重试。')
                old.update(style=style, updated_at=now())
                db.execute('UPDATE reading_annotations SET payload=? WHERE id=?',
                           (json.dumps(old, ensure_ascii=False), old['id']))
                return old
            for row in db.execute('SELECT payload FROM reading_annotations WHERE article_id=?', (article['id'],)):
                old = json.loads(row[0])
                if old['version'] == article['version'] and old['style'] == style and old['segments'] == clean:
                    return old
            item = {'id': uuid.uuid4().hex, 'article_id': article['id'], 'version': article['version'],
                    'style': style, 'segments': clean, 'created_at': now()}
            db.execute('INSERT INTO reading_annotations VALUES(?,?,?)',
                       (item['id'], article['id'], json.dumps(item, ensure_ascii=False)))
            return item

    def delete(self, body):
        with self.store.lock, self.store.connect() as db:
            db.execute('DELETE FROM reading_annotations WHERE id=? AND article_id=?',
                       (body.get('id'), body.get('article_id')))
        return {'ok': True}
