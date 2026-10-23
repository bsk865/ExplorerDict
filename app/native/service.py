"""Direct access to existing workspace rules, without a local HTTP server."""
from pathlib import Path
import json
from ..workspace_server import Workspace, parse_file
from ..study_hub import StudyHub
from ..study_activity import StudyActivity
from ..reading_annotations import ReadingAnnotations
from .. import content_management, reading_preferences


class NativeService:
    def __init__(self, directory):
        self.workspace = Workspace(Path(directory))
        self.store = self.workspace.store
        self.hub = StudyHub(self.store)
        self.activity = StudyActivity(self.store)
        self.annotations = ReadingAnnotations(self.store)

    def snapshot(self):
        return {'articles': self.store.summaries(), 'study': self.hub.snapshot(),
                'activity': self.activity.summary(),
                'preferences': reading_preferences.load(self.store.directory)}

    def article(self, key):
        article = self.store.article(key)
        self.workspace.map_preloader.offer(article)
        return {'article': article, 'annotations': self.annotations.list(key),
                'notes': self.hub.notes(key), 'assets': {b['asset']: self.store.asset(b['asset'])[1]
                    for b in article['blocks'] if b['type'] == 'image'}}

    def import_file(self, path):
        path = Path(path)
        if path.stat().st_size > 28_000_000:
            raise ValueError('文件请小于28MB。')
        return self.store.import_article(path.stem, parse_file(self.store, path.name, path.read_bytes()))

    def import_text(self, title, text):
        if not text.strip():
            raise ValueError('请填写文章内容。')
        return self.store.import_article(title, [{'type': 'text', 'text': p}
                                                for p in text.split('\n\n') if p.strip()])

    def append(self, article_id, blocks, after=None):
        old = self.store.article(article_id)
        if not blocks:
            raise ValueError('请添加原文内容。')
        if old.get('source_status') == 'pending_source':
            from ..capture_documents import attach_full_source
            return attach_full_source(self.store, old, self.store.import_article(old['title'], blocks))
        clean = [{k: v for k, v in block.items() if k != 'id'} for block in old['blocks']]
        index = len(clean) if after is None else after+1
        if not 0 <= index <= len(clean):
            raise ValueError('插入位置已变化，请重新打开文章。')
        article = self.store.import_article(old['title'], clean[:index]+blocks+clean[index:])
        if article['id'] == old['id']:
            return article
        article['previous_id'] = old['id']
        for field in ('source_url', 'source_status'):
            if field in old:
                article[field] = old[field]
        self.store.update_article(article)
        note = self.store.get('sessions', old['id']+':'+old['version']+':', {}).get('note')
        if note:
            self.store.update_session(article['id']+':'+article['version']+':', {'note': note})
        for obj in old['objects']:
            old_index = next(i for i, b in enumerate(old['blocks']) if b['id'] == obj['block'])
            new_index = old_index + (len(blocks) if old_index >= index else 0)
            copied = self.store.object(article['id'], dict(obj, block=f'b{new_index}'))
            self.store.update_object(copied['id'], obj)
        return self.store.article(article['id'])

    def append_file(self, article_id, path, after=None):
        path = Path(path)
        if path.stat().st_size > 28_000_000:
            raise ValueError('文件请小于28MB。')
        return self.append(article_id, parse_file(self.store, path.name, path.read_bytes()), after)

    def extract(self, url, article_id=None):
        from ..reader_source import capture_source
        old = self.store.article(article_id) if article_id else None
        article = capture_source(self.store, url or (old or {}).get('source_url', ''))
        if old and old['id'] != article['id']:
            if old.get('source_status') == 'pending_source':
                from ..capture_documents import attach_full_source
                return attach_full_source(self.store, old, article)
            old['superseded_by'] = article['id']
            self.store.update_article(old)
            article['previous_id'] = old['id']
            self.store.update_article(article)
        return article

    def rename(self, article_id, title):
        title = str(title).strip()
        if not title or len(title) > 200:
            raise ValueError('标题请填写1至200字。')
        article = self.store.article(article_id)
        article['title'] = title
        self.store.update_article(article)
        return article

    def note_drafts(self):
        with self.store.connect() as db:
            rows = db.execute("SELECT id,payload FROM settings WHERE id LIKE 'native-note-drafts:%'").fetchall()
        return [(row['id'], json.loads(row['payload'])) for row in rows]

    def inventory(self):
        result = content_management.inventory(self.store)
        advisor = self.store.get('settings', 'native-advisor-draft', {})
        drafts = []
        if advisor.get('request') or advisor.get('history'):
            drafts.append({'id':'native-advisor-draft', 'title':'小探规划草稿',
                           'content':'\n\n'.join([t['content'] for t in advisor.get('history', [])] + [advisor.get('request', '')])})
        for _, values in self.note_drafts():
            for body in values:
                drafts.append({**body, 'id':'note-draft:'+body['draft_id'], 'note_id':body.get('id'),
                               'title':body.get('title') or '未命名笔记草稿'})
        result['native_drafts'] = drafts
        return result

    def storage_info(self):
        rows = []
        with self.store.connect() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table, title in [('attempts', '导图草稿与复核记录'), ('map_parts', '长文分段导图缓存'),
                                 ('articles', '文章与正式导图'), ('sessions', '问答与笔记')]:
                if table in tables:
                    count, size = db.execute('SELECT COUNT(*),COALESCE(SUM(LENGTH(CAST(payload AS BLOB))),0) FROM ' + table).fetchone()
                    rows.append({'label':title, 'count':count, 'bytes':size})
        return {'directory':str(self.store.directory.resolve()), 'database':'reading_workspace.sqlite3', 'items':rows}

    def prune_note_drafts(self, matches):
        for key, values in self.note_drafts():
            kept = [body for body in values if not matches(body)]
            if len(kept) != len(values):
                self.store.put('settings', key, kept)

    def remove(self, kind, key):
        with self.store.lock, self.workspace.lock:
            for job in self.workspace.jobs.values():
                if job.get('background'):
                    job['cancel'].set()
            if any(j['status'] == 'running' and not j.get('background')
                   for j in self.workspace.jobs.values()):
                raise ValueError('请先停止当前任务，再删除内容。')
            if kind == 'articles':
                related = self.store.related_versions(key)
                result = self.store.delete_articles(related)
                self.prune_note_drafts(lambda body: body.get('article_id') in related)
                return result
            if kind == 'native_drafts' and key == 'native-advisor-draft':
                self.store.put('settings', key, {})
                return {'ok': True}
            if kind == 'native_drafts' and key.startswith('note-draft:'):
                self.prune_note_drafts(lambda body: body.get('draft_id') == key.removeprefix('note-draft:'))
                return {'ok': True}
            result = content_management.remove(self.store, kind, key)
            if kind == 'notes':
                self.prune_note_drafts(lambda body: body.get('id') == key)
            return result

    def close(self):
        self.workspace.map_preloader.close()
        self.workspace.rag.close()
        for job in self.workspace.jobs.values():
            job['cancel'].set()
