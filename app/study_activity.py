"""Observed reading visits and explicit completion; never infer mastery."""
from datetime import datetime, timedelta
import json
from .study_hub import now


class StudyActivity:
    def __init__(self, store):
        self.store=store
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS study_reading(article_id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS study_visits(article_id TEXT NOT NULL, day TEXT NOT NULL, PRIMARY KEY(article_id,day))')

    def record(self, article_id, completed=None):
        if completed is not None and type(completed) is not bool:
            raise ValueError('阅读状态无效。')
        with self.store.lock,self.store.connect() as db:
            self.store.article(article_id)
            row=db.execute('SELECT payload FROM study_reading WHERE article_id=?',(article_id,)).fetchone()
            value=json.loads(row[0]) if row else {'article_id':article_id,'first_opened':now(),'completed':False}
            value['last_opened']=now()
            if completed is not None:
                value['completed']=completed
                value['completed_at']=now() if completed else None
            db.execute('INSERT INTO study_reading VALUES(?,?) ON CONFLICT(article_id) DO UPDATE SET payload=excluded.payload',(article_id,json.dumps(value)))
            db.execute('INSERT OR IGNORE INTO study_visits VALUES(?,?)',(article_id,datetime.now().date().isoformat()))
        return value

    def summary(self):
        today=datetime.now().date()
        days=[(today-timedelta(days=i)).isoformat() for i in reversed(range(7))]
        with self.store.connect() as db:
            readings=[json.loads(r[0]) for r in db.execute('SELECT payload FROM study_reading')]
            counts=dict(db.execute('SELECT day,COUNT(*) FROM study_visits WHERE day>=? GROUP BY day',(days[0],)))
        return {'readings':readings,'days':[{'date':d,'articles_opened':counts.get(d,0)} for d in days]}
