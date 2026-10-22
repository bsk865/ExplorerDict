"""Local-only retrieval acceptance on a copy of existing source text; no answer API."""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.paths import data_dir
from app.workspace_store import WorkspaceStore
from app.semantic_library import SemanticLibrary
from app.retrieval import SourceIndex

# Freeze queries and evidence targets before running retrieval. These are acceptance
# examples, not an independent benchmark or a measure of general retrieval accuracy.
CASES = [
    ('When one brake hydraulic circuit leaks, how many wheels must still brake?', '两个车轮'),
    ('What force must the brake pedal withstand without damage?', '2000 N'),
    ('How many emergency stop switches must be installed on the vehicle?', '三个熄火按钮'),
    ('Can a racing helmet be modified or leave the face open?', '未经改装'),
    ('车检通过以后维修了碰撞损坏，还能直接上场吗？', '首席技术'),
    ('低压总电源开关周围的红色圆圈需要多大？', '50 mm'),
]
HELD_OUT = [
    ('刹车的一条油路坏了，剩下的系统最低要能控制几只轮子？', '两个车轮'),
    ('踩刹车的脚踏机构至少得承受多大的载荷？', '2000 N'),
    ('赛车紧急停止按钮要装几个？', '三个熄火按钮'),
    ('驾驶员防护头具能不能自己改装？', '未经改装'),
    ('赛车鼻尖贴车检标志，需要预留多大空白？', '25 cm'),
    ('赛车比赛允许自己改变尾翼的安装位置吗？', '装配位置不可调整'),
]

def main():
    source = WorkspaceStore(data_dir())
    with tempfile.TemporaryDirectory(prefix='real-rag-') as directory:
        store = WorkspaceStore(Path(directory))
        articles = []
        for summary in source.summaries():
            article = source.article(summary['id'])
            blocks = [{'type':'text', 'text':b['text'], 'page':b.get('page')} for b in article['blocks'] if b['type']=='text']
            if blocks:
                articles.append(store.import_article(article['title'], blocks))
        rag = SemanticLibrary(store)
        start = time.perf_counter()
        rag.sync()
        status = rag.status()
        assert status['ready'] == len(articles), status
        report = {'model':status['model'], 'documents':len(articles), 'vectors':status['vectors'],
                  'index_seconds':round(time.perf_counter()-start, 2), 'cases':[]}
        lexical, hybrid = SourceIndex(store), SourceIndex(store, rag)
        for question, expected in (HELD_OUT if '--held-out' in sys.argv else CASES):
            item = {'question':question, 'expected':expected}
            for label, index in [('keyword',lexical), ('hybrid',hybrid)]:
                start = time.perf_counter()
                hits = [h for h in index.search(articles, question, text_only=True) if h['score']>0]
                item[label] = {'rank':next((i+1 for i,h in enumerate(hits) if expected in h['quote']),None),
                               'seconds':round(time.perf_counter()-start,3)}
            report['cases'].append(item)
        report['unrelated_semantic_hits'] = len(rag.retrieve(articles, 'How do I bake a chocolate birthday cake?'))
        report['semantic_sample'] = [{'quote':h['quote'], 'score':h['semantic_score']} for h in rag.retrieve(articles, CASES[0][0], limit=3)]
        output = Path('artifacts/real-rag-held-out.json' if '--held-out' in sys.argv else 'artifacts/real-rag-acceptance.json')
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in report.items() if k!='semantic_sample'},ensure_ascii=True))

if __name__ == '__main__':
    main()
