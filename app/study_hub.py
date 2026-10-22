"""Local themes, anchored notes and study plans in the reading database."""
from datetime import datetime, timedelta
from contextlib import nullcontext
import json
import uuid


def now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def text(value, limit, label, required=True):
    if not isinstance(value, str) or len(value.strip()) > limit or (required and not value.strip()):
        raise ValueError(f'{label}请输入{limit}字以内的文字。')
    return value.strip()


class StudyHub:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS study_themes(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS study_memberships(theme_id TEXT NOT NULL, article_id TEXT NOT NULL,
                    PRIMARY KEY(theme_id, article_id));
                CREATE TABLE IF NOT EXISTS study_notes(id TEXT PRIMARY KEY, article_id TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS study_tasks(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS study_batches(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS study_notes_article ON study_notes(article_id);
                CREATE UNIQUE INDEX IF NOT EXISTS study_notes_draft ON study_notes(json_extract(payload, '$.draft_id'));
            ''')

    def snapshot(self):
        with self.store.connect() as db:
            result = {name: [json.loads(r[0]) for r in db.execute(f'SELECT payload FROM study_{name} ORDER BY rowid DESC')]
                      for name in ('themes', 'notes', 'tasks')}
            result['memberships'] = [dict(r) for r in db.execute('SELECT * FROM study_memberships')]
            result['batches'] = [json.loads(r[0]) for r in db.execute('SELECT payload FROM study_batches ORDER BY rowid DESC LIMIT 30')]
        visible = {a['id'] for a in self.store.summaries()}
        result['memberships'] = [r for r in result['memberships'] if r['article_id'] in visible]
        result['stats'] = {'articles': len(visible), 'notes': len(result['notes']),
                           'completed_tasks': sum(t['status'] == 'done' for t in result['tasks'])}
        return result

    def save(self, kind, body, *, _db=None):
        if kind not in {'themes', 'notes', 'tasks'} or not isinstance(body, dict):
            raise ValueError('无法保存此内容。')
        with self.store.lock, (self.store.connect() if _db is None else nullcontext(_db)) as db:
            key = body.get('id')
            if kind == 'notes' and not key and body.get('draft_id'):
                text(body['draft_id'], 80, '草稿编号')
                row = db.execute("SELECT id FROM study_notes WHERE json_extract(payload, '$.draft_id')=?", (body['draft_id'],)).fetchone()
                if row:
                    key = row[0]
            old = None
            if key:
                row = db.execute(f'SELECT payload FROM study_{kind} WHERE id=?', (key,)).fetchone()
                if not row:
                    raise ValueError('内容已删除，请刷新后重试。')
                old = json.loads(row[0])
            item = dict(old or {'id': uuid.uuid4().hex, 'created_at': now()})
            item['updated_at'] = now()
            if kind == 'themes':
                item['title'] = text(body.get('title', item.get('title')), 80, '主题名称')
                item['description'] = text(body.get('description', item.get('description', '')), 1000, '主题说明', False)
            elif kind == 'notes':
                article = self.store.article(body.get('article_id', item.get('article_id')))
                if old and article['id'] != old['article_id']:
                    raise ValueError('笔记不能移动到其他文章。')
                item['article_id'] = article['id']
                if not old and body.get('draft_id'):
                    item['draft_id'] = body['draft_id']
                item['article_title'] = article['title']
                item['content'] = text(body.get('content', item.get('content')), 20000, '笔记')
                item['title'] = text(body.get('title', item.get('title', '')), 200, '笔记标题', False)
                if not old:
                    block_id = body.get('block', '')
                    quote = text(body.get('quote', ''), 10000, '引用', False)
                    block = next((b for b in article['blocks'] if b['id'] == block_id), None)
                    if block_id and not block:
                        raise ValueError('原文位置不存在。')
                    if quote and (not block or quote not in block.get('text', '')):
                        raise ValueError('引用文字与原文不一致。')
                    item.update(version=article['version'], block=block_id, quote=quote, source='personal')
            else:
                item['title'] = text(body.get('title', item.get('title')), 200, '任务名称')
                item['description'] = text(body.get('description', item.get('description', '')), 5000, '任务说明', False)
                item['goal'] = text(body.get('goal', item.get('goal', '')), 200, '目标', False)
                item['date'] = body.get('date', item.get('date', ''))
                item['time'] = body.get('time', item.get('time', ''))
                try:
                    if item['date']:
                        datetime.strptime(item['date'], '%Y-%m-%d')
                    if item['time']:
                        datetime.strptime(item['time'], '%H:%M')
                        if not item['date']:
                            raise ValueError()
                except (ValueError, TypeError):
                    raise ValueError('请选择有效的日期和时间。') from None
                minutes = body.get('minutes', item.get('minutes', 30))
                if type(minutes) is not int or not 5 <= minutes <= 1440:
                    raise ValueError('预计用时应为5至1440分钟。')
                item['minutes'] = minutes
                status = body.get('status', item.get('status', 'todo'))
                if status not in {'todo', 'doing', 'done'}:
                    raise ValueError('任务状态无效。')
                item['completed_at'] = ((old or {}).get('completed_at') or now()) if status == 'done' else None
                item['status'] = status
                ids = body.get('article_ids', item.get('article_ids', []))
                if not isinstance(ids, list) or any(not isinstance(a, str) for a in ids):
                    raise ValueError('请选择有效的文章。')
                visible = {a['id'] for a in self.store.summaries()}
                if not set(ids) <= visible:
                    raise ValueError('关联文章已更新或删除，请重新选择。')
                item['article_ids'] = sorted(set(ids))
                if item['time'] and item['status'] != 'done':
                    start = datetime.strptime(item['date']+' '+item['time'], '%Y-%m-%d %H:%M')
                    end = start + timedelta(minutes=minutes)
                    if end.date() != start.date() and end.time() != datetime.min.time():
                        raise ValueError('任务不能跨越午夜，请拆分为两天安排。')
                    for row in db.execute('SELECT payload FROM study_tasks WHERE id<>?', (item['id'],)):
                        other = json.loads(row[0])
                        if other['status'] == 'done' or not other.get('date') or not other.get('time'):
                            continue
                        other_start = datetime.strptime(other['date']+' '+other['time'], '%Y-%m-%d %H:%M')
                        if start < other_start + timedelta(minutes=other['minutes']) and end > other_start:
                            raise ValueError('这段时间与「'+other['title']+'」重叠，请调整时间。')
            encoded = json.dumps(item, ensure_ascii=False)
            if kind == 'notes':
                db.execute('INSERT INTO study_notes VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                           (item['id'], item['article_id'], encoded))
            else:
                db.execute(f'INSERT INTO study_{kind} VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload', (item['id'], encoded))
            return item

    def notes(self, article_id):
        article = self.store.article(article_id)
        related = self.store.related_versions(article_id)
        with self.store.connect() as db:
            notes = [json.loads(r[0]) for r in db.execute('SELECT payload FROM study_notes WHERE article_id IN (' + ','.join('?' for _ in related) + ') ORDER BY rowid DESC', sorted(related))]
        return {'notes': notes, 'legacy': [n for n in self.store.saved_entries() if n['article_id'] in related and n.get('note')], 'version': article['version']}

    def save_answer_note(self, body):
        with self.store.lock,self.store.connect() as db:
            session=self.store.get('sessions',body.get('session_key'),{})
            index=body.get('message_index')
            messages=session.get('messages',[])
            if type(index) is not int or not 0<=index<len(messages) or messages[index].get('role')!='assistant':
                raise ValueError('回答已变化，请重新打开后保存。')
            if body.get('source_content') != messages[index].get('content'):
                raise ValueError('回答已变化，请重新打开后保存。')
            aid=body.get('article_id')
            allowed=session.get('article_ids',[])
            if aid not in allowed:
                raise ValueError('请选择本次问答范围中的文章。')
            note=self.save('notes',{'article_id':aid,'title':body.get('title','阅读整理'),
                                   'content':body.get('content'),'draft_id':body.get('draft_id')},_db=db)
            note.update(origin='assistant_confirmed', evidence=messages[index].get('evidence',[]),
                        source_session=body['session_key'],source_message=index)
            db.execute('UPDATE study_notes SET payload=? WHERE id=?',(json.dumps(note,ensure_ascii=False),note['id']))
            return note

    def save_plan(self, tasks, request, cancel):
        if not isinstance(tasks, list) or not 1 <= len(tasks) <= 60 or any(not isinstance(t, dict) for t in tasks):
            raise ValueError('小探尚未给出有效计划，请补充时间要求后重试。')
        with self.store.lock, self.store.connect() as db:
            saved = []
            for raw in tasks:
                if raw.get('id') or raw.get('status', 'todo') != 'todo':
                    raise ValueError('新计划不能覆盖已有任务或标记为已完成。')
                if not raw.get('date') or not raw.get('time'):
                    raise ValueError('计划缺少具体日期或时间，请补充可用时段。')
                item = self.save('tasks', raw, _db=db)
                saved.append(item)
            if cancel.is_set():
                raise ValueError('规划已停止，没有保存新计划。')
            batch = {'id': uuid.uuid4().hex, 'created_at': now(), 'request': request,
                     'tasks': saved, 'undone': False}
            db.execute('INSERT INTO study_batches VALUES(?,?)', (batch['id'], json.dumps(batch, ensure_ascii=False)))
        return batch

    def undo_plan(self, key):
        with self.store.lock, self.store.connect() as db:
            row = db.execute('SELECT payload FROM study_batches WHERE id=?', (key,)).fetchone()
            if not row:
                raise ValueError('这份计划不存在。')
            batch = json.loads(row[0])
            if batch['undone']:
                return {'ok': True}
            for original in batch['tasks']:
                current = db.execute('SELECT payload FROM study_tasks WHERE id=?', (original['id'],)).fetchone()
                if current and json.loads(current[0]) != original:
                    raise ValueError('其中的任务已修改或完成，请在计划中逐项调整。')
            db.executemany('DELETE FROM study_tasks WHERE id=?', [(t['id'],) for t in batch['tasks']])
            batch['undone'] = True
            db.execute('UPDATE study_batches SET payload=? WHERE id=?', (json.dumps(batch, ensure_ascii=False), key))
        return {'ok': True}

    def split_task(self, key, minutes):
        with self.store.lock, self.store.connect() as db:
            row = db.execute('SELECT payload FROM study_tasks WHERE id=?', (key,)).fetchone()
            if not row:
                raise ValueError('任务已删除。')
            task = json.loads(row[0])
            if task['status'] == 'done':
                raise ValueError('已完成任务无需拆分。')
            if type(minutes) is not int or not 5 <= minutes <= task['minutes']-5:
                raise ValueError('拆分后每项任务至少保留5分钟。')
            first = self.save('tasks', {'id':key, 'minutes':minutes}, _db=db)
            second = {k:v for k,v in task.items() if k not in {'id','created_at','updated_at','completed_at'}}
            second.update(title=(task['title'][:190]+' · 继续'), minutes=task['minutes']-minutes, status='todo')
            if task.get('time'):
                start = datetime.strptime(task['date']+' '+task['time'], '%Y-%m-%d %H:%M')+timedelta(minutes=minutes)
                second.update(date=start.strftime('%Y-%m-%d'), time=start.strftime('%H:%M'))
            return {'tasks':[first,self.save('tasks',second,_db=db)]}

    def assign(self, theme_id, article_ids, mode='replace'):
        if mode not in {'replace', 'add'}:
            raise ValueError('归类方式无效。')
        if not isinstance(article_ids, list) or any(not isinstance(a, str) for a in article_ids):
            raise ValueError('请选择有效的文章。')
        with self.store.lock, self.store.connect() as db:
            if not db.execute('SELECT 1 FROM study_themes WHERE id=?', (theme_id,)).fetchone():
                raise ValueError('主题已删除。')
            visible = {a['id'] for a in self.store.summaries()}
            if not set(article_ids) <= visible:
                raise ValueError('文章已更新或删除，请刷新。')
            if mode == 'replace':
                db.execute('DELETE FROM study_memberships WHERE theme_id=?', (theme_id,))
            db.executemany('INSERT OR IGNORE INTO study_memberships VALUES(?,?)', [(theme_id, a) for a in sorted(set(article_ids))])
        return {'ok': True}

    def delete(self, kind, key):
        if kind not in {'themes', 'notes', 'tasks', 'batches'}:
            raise ValueError('无法删除此内容。')
        with self.store.lock, self.store.connect() as db:
            db.execute(f'DELETE FROM study_{kind} WHERE id=?', (key,))
            if kind == 'notes' and db.execute("SELECT 1 FROM sqlite_master WHERE name='rag_vectors'").fetchone():
                db.execute('DELETE FROM rag_vectors WHERE document_id=?', ('note:'+key,))
                db.execute('DELETE FROM rag_documents WHERE id=?', ('note:'+key,))
            if kind == 'themes':
                db.execute('DELETE FROM study_memberships WHERE theme_id=?', (key,))
        return {'ok': True}
