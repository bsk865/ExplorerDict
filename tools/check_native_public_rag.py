"""Two source-checked native-library requests against one identified public paper."""
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import time

from check_native_live_rag import ROOT, read_config

PUBLIC_SHA256 = 'e0c2d43159201cc471608ed47f29553d05809bb0c51f343a0afa9afe6411eb67'


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    if len(sys.argv) != 3:
        raise SystemExit('Usage: check_native_public_rag.py configuration_directory public_paper.pdf')
    document = Path(sys.argv[2]).resolve(strict=True)
    if hashlib.sha256(document.read_bytes()).hexdigest() != PUBLIC_SHA256:
        raise SystemExit('This check only sends the previously identified public RGraph-Eval PDF.')
    logging.disable(logging.CRITICAL)
    config = read_config(Path(sys.argv[1]).resolve(strict=True))
    if not config.get('key'):
        raise SystemExit('Configured test credential missing.')
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    from PySide6.QtWidgets import QApplication, QWidget
    from app.native.service import NativeService
    from app.native.library import LibraryController
    from app.native.widgets import Tasks
    from app import conversation_budget

    parent = ROOT/'artifacts/native-checks'
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='public-rag-', dir=parent))
    application = QApplication([])
    host = QWidget()
    host.service = service = NativeService(directory)
    service.workspace.map_preloader.close()
    service.store.model_config = lambda: dict(config)
    host.tasks = Tasks(host)
    host.run = lambda work, done: host.tasks.submit(work, done)
    controller = LibraryController(host)
    started = time.monotonic()
    report = dict(directory=str(directory), source='https://arxiv.org/abs/2610.31121',
                  pdf_sha256=PUBLIC_SHA256, model=config['model'], reasoning_effort=config.get('reasoning_effort'),
                  public_material_only=True, passed=False, cases=[])
    (directory/'run.json').write_text(json.dumps(dict(report, process_id=os.getpid()), indent=2), encoding='utf-8')

    def wait(predicate):
        while not predicate():
            if time.monotonic()-started > 240:
                raise RuntimeError('Public-paper check deadline exceeded')
            application.processEvents()
            time.sleep(.01)

    try:
        article = service.import_file(document)
        report['text_characters'] = len(article['text'])
        report['text_pages'] = len({b['page'] for b in article['blocks'] if b['type'] == 'text'})
        print(json.dumps(dict(phase='indexing_public_paper', pages=report['text_pages']), ensure_ascii=False), flush=True)
        indexing = time.monotonic()
        service.workspace.rag.sync()
        report['index_seconds'] = round(time.monotonic()-indexing, 2)
        index = service.workspace.rag.status()
        assert index['ready'] == 1 and index['vectors'] > 0, index
        report['vectors'] = index['vectors']
        conversation_budget.save_policy(service.store, 32768)
        controller.select([], False, 'question', scope='all')
        wait(lambda: controller.key is not None)
        key = controller.key
        cases = [
            ('ShareRobot-SG包含多少场景，其中自动标注和人工真值各多少？另一个视频生成评估子集包含多少场景、多少生成视频？请区分两个集合并引用原文。',
             ('1000', '900', '100', '30', '90')),
            ('再核对Table 2的Human evaluation：Ours的Objects F1与Relations F1分别是多少？只回答这两个指标，区分它们，不要混入Dataset-stratified PPI。请引用。',
             ('89.67', '82.66')),
        ]
        for ordinal, (question, expected) in enumerate(cases):
            if ordinal:
                service.rename(article['id'], 'RGraph-Eval · 原文指标复核')
            print(json.dumps(dict(phase='asking_public_paper', case=ordinal+1), ensure_ascii=False), flush=True)
            controller.request(question)
            wait(lambda: not controller.busy)
            saved = controller.session
            assert saved.get('messages') and saved['messages'][-1]['role'] == 'assistant', controller.message
            answer = saved['messages'][-1]
            evidence = answer.get('evidence', [])
            normalized = answer['content'].replace(',', '').replace('，', '')
            item = dict(question=question, answer=answer['content'], evidence=evidence, expected=list(expected),
                        exact_source=False, expected_values=False, model_called=answer.get('model') == config['model'])
            report['cases'].append(item)
            assert item['model_called'] and evidence
            current = service.store.article(article['id'])
            for hit in evidence:
                block = next(b for b in current['blocks'] if b['id'] == hit['block'])
                assert hit['article_id'] == article['id'] and hit['version'] == current['version']
                assert hit['quote'] == block['text'][hit['start']:hit['end']]
                assert hit['page'] == block['page'] and hit['article_title'] == current['title']
            item['exact_source'] = True
            assert all(value in normalized for value in expected), 'Expected numerical facts missing'
            joined = '\n'.join(hit['quote'] for hit in evidence).replace(',', '')
            assert all(value in joined for value in expected), 'Citations do not contain expected facts'
            item['expected_values'] = True
            assert controller.key == key and not saved.get('closed')
        report['rename_preserved_conversation'] = len(controller.session['messages']) == 4
        report['passed'] = report['rename_preserved_conversation']
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
    print(json.dumps({k:report[k] for k in ('directory','model','reasoning_effort','passed','elapsed_seconds')} | {'error':report.get('error')}, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
