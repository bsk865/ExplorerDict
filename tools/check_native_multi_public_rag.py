"""Cross-source questions over three identified public articles, without user data."""
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from check_native_live_rag import ROOT, read_config

SOURCES = [
    ('robot', 'artifacts/fulltext-translation-20261024/public-paper.pdf',
     'e0c2d43159201cc471608ed47f29553d05809bb0c51f343a0afa9afe6411eb67',
     'RGraph-Eval · 机器人视频评估', 'https://arxiv.org/abs/2610.31121'),
    ('plos', 'artifacts/native-checks/publisher-paper-u1nwnlyx/original.pdf',
     '24dcf7ebf6fcb0702cae62f334c19595d194836aef6f991b02df6e5d79480938',
     'PLOS ONE · DSS合成', 'https://doi.org/10.1371/journal.pone.0361484'),
    ('acs', 'artifacts/multi-public-rag-inputs/PMC11822487.xml',
     '96df758d31c5095e193afbee0f197d041d101505e4d8baf867ad9ab5a3f4d456',
     'ACS Omega · SAA合成', 'https://doi.org/10.1021/acsomega.4c09986'),
]
QUESTION = ('RGraph-Eval 的人类评分实际评了多少生成视频、来自多少场景？'
            'PLOS ONE 的 DSS 合成论文用 strain 6 在生物反应器最终获得多少 mM DSS、用了多少小时？'
            'ACS Omega 的 SAA 合成论文在优化条件下从多少 mM L-Tyrosine 获得多少 mM SAA？'
            '请逐项回答，分别引用各篇原文，不要混用两篇生物论文的产物或数值。')
PATTERNS = {'robot': [r'(One assessor rated 90 videos|30.scene generation subset \(90 generated videos, one assessor\))', r'30.scene generation subset'],
            'plos': [r'19\.47\s*mM', r'32\s*h'],
            'acs': [r'81\.(67|7)\s*mM', r'100\s*mM']}


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    logging.disable(logging.CRITICAL)
    assert len(sys.argv) in {2, 3}, 'configuration_directory [--ask]'
    for _, name, expected, _, _ in SOURCES:
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest() == expected, 'Public input changed'
    config = read_config(Path(sys.argv[1]).resolve(strict=True))
    assert urlsplit(config['base_url']).hostname == 'api.deepseek.com' and config.get('key')
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    from PySide6.QtWidgets import QApplication, QWidget
    from app.native.service import NativeService
    from app.native.widgets import Tasks
    from app.native.library import LibraryController
    from app.library_assistant import search
    from app.reading_text import block_text
    from app import conversation_budget

    directory = Path(tempfile.mkdtemp(prefix='multi-public-rag-', dir=ROOT/'artifacts/native-checks'))
    app = QApplication([])
    host = QWidget()
    host.service = service = NativeService(directory)
    service.workspace.map_preloader.close()
    service.store.model_config = lambda: dict(config)
    host.tasks = Tasks(host)
    host.run = lambda work, done: host.tasks.submit(work, done)
    controller = LibraryController(host)
    started = time.monotonic()
    report = dict(passed=False, directory=str(directory), public_material_only=True,
                  model=config['model'], reasoning_effort=config.get('reasoning_effort'),
                  sources=[], cases=[], requests=[],
                  evidence_criteria='Robot counts may be supported by the explicit Table 3 caption; ACS reports 81.67 mM in its abstract and 81.7 mM in its conclusion, both accepted at their stated precision.')
    original_client = service.workspace.client
    def client():
        value = original_client()
        original_chat = value.chat
        def chat(messages):
            report['requests'].append(dict(messages=len(messages),
                characters=sum(len(m.get('content', '')) for m in messages),
                text_only=all(isinstance(m.get('content', ''), str) for m in messages)))
            return original_chat(messages)
        value.chat = chat
        return value
    service.workspace.client = client
    def wait(predicate):
        while not predicate():
            if time.monotonic()-started > 240:
                raise RuntimeError('Public library request deadline exceeded')
            app.processEvents()
            time.sleep(.01)
    try:
        articles = {}
        for tag, name, digest, title, url in SOURCES:
            path = ROOT/name
            if path.suffix == '.xml':
                root = ET.fromstring(path.read_bytes())
                # Retain every front/body/back text node; this is a text-only import,
                # not a claim that the application imported the XML or its figures.
                text = '\n\n'.join(' '.join(''.join(part.itertext()).split()) for part in root)
                extract = directory/'ACS Omega public text extract.txt'
                extract.write_text(text, encoding='utf-8')
                article = service.import_file(extract)
                article['source_status'] = 'public_text_extract'
            else:
                article = service.import_file(path)
            article.update(title=title, source_url=url)
            service.store.update_article(article)
            articles[tag] = service.store.article(article['id'])
            report['sources'].append(dict(tag=tag, url=url, sha256=digest,
                article_id=article['id'], format='text extract from official XML' if path.suffix == '.xml' else 'PDF',
                characters=len(articles[tag]['text'])))
        conversation_budget.save_policy(service.store, 32768)
        print(json.dumps(dict(phase='indexing', articles=len(articles))), flush=True)
        service.workspace.rag.sync()
        report['index'] = service.workspace.rag.status()
        assert report['index']['ready'] == 3 and report['index']['vectors'] > 0
        diagnostics = {}
        hits = search(service.workspace, dict(scope='all', question=QUESTION), diagnostics)[3]
        report['retrieval'] = dict(diagnostics=diagnostics, hits=hits, expected=PATTERNS)
        for tag, patterns in PATTERNS.items():
            passages = re.sub(r'\s+', ' ', '\n'.join(h['quote'] for h in hits if h['article_id'] == articles[tag]['id']))
            assert all(re.search(pattern, passages, re.I) for pattern in patterns), 'Missing original facts for '+tag
        report['retrieval']['covered_all_original_facts'] = True
        if len(sys.argv) == 3 and sys.argv[2] == '--ask':
            controller.select([], False, 'question', scope='all')
            wait(lambda:controller.key is not None)
            case = dict(question=QUESTION, passed=False)
            report['cases'].append(case)
            controller.request(QUESTION)
            wait(lambda:not controller.busy)
            assert controller.session.get('messages') and controller.session['messages'][-1]['role'] == 'assistant', controller.message
            answer = controller.session['messages'][-1]
            case.update(answer=answer['content'], evidence=answer.get('evidence', []))
            assert answer.get('model') == config['model'] and case['evidence']
            by_id = {a['id']:a for a in articles.values()}
            for hit in case['evidence']:
                article = by_id[hit['article_id']]
                block = next(b for b in article['blocks'] if b['id'] == hit['block'])
                assert block_text(block)[hit['start']:hit['end']] == hit['quote']
                assert hit['version'] == article['version'] and hit['page'] == block.get('page')
            for tag, patterns in PATTERNS.items():
                text = re.sub(r'\s+', ' ', '\n'.join(h['quote'] for h in case['evidence'] if h['article_id'] == articles[tag]['id']))
                assert all(re.search(pattern, text, re.I) for pattern in patterns), 'Answer lacks original evidence for '+tag
            assert all(value in answer['content'] for value in ('90', '30', '19.47', '32', '100'))
            assert re.search(r'81\.(67|7)', answer['content'])
            case.update(passed=True, exact_sources=True, all_original_facts_cited=True)
        report['passed'] = True
    except Exception as exc:
        report['error'] = type(exc).__name__+': '+str(exc)
    finally:
        controller.stop()
        controller.timer.stop()
        host.tasks.pool.waitForDone(5000)
        service.close()
        host.close()
        report['elapsed_seconds'] = round(time.monotonic()-started, 2)
        (directory/'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('directory','passed','model','reasoning_effort','elapsed_seconds')} |
                     dict(model_requests=len(report['requests']), error=report.get('error')), ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
