"""Run a real all-library request after adding a second synthetic document."""
import argparse
import base64
from contextlib import closing
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'


def read_config(source):
    from app.crypto_dpapi import unprotect
    with closing(sqlite3.connect((source/'reading_workspace.sqlite3').as_uri()+'?mode=ro', uri=True)) as db:
        row = db.execute("SELECT payload FROM settings WHERE id='model'").fetchone()
        model = json.loads(row[0]) if row else {}
    if model and not model.get('shared'):
        return {**model, 'key': unprotect(base64.b64decode(model['key'])) if model.get('key') else ''}
    with closing(sqlite3.connect((source/'explorer_dict.sqlite3').as_uri()+'?mode=ro', uri=True)) as db:
        settings = dict(db.execute('SELECT k,v FROM settings'))
        row = db.execute("SELECT blob FROM secrets WHERE name='api_key'").fetchone()
    return {'base_url': settings.get('api.base_url', ''), 'model': settings.get('api.model', ''),
            'reasoning_effort': settings.get('api.reasoning_effort', ''),
            'key': unprotect(row[0]) if row else ''}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('configuration_directory', type=Path)
    parser.add_argument('--handoff', action='store_true', help='Exercise budget closure and a topic-dependent follow-up after restart')
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    config = read_config(args.configuration_directory.resolve(strict=True))
    if not config.get('key'):
        print(json.dumps({'passed': False, 'reason': 'model_key_missing'}))
        return 1
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QWidget
    from app.native.service import NativeService
    from app.native.widgets import Tasks
    from app.native.library import LibraryController
    from app import conversation_budget

    parent = ROOT/'artifacts/native-checks'
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='live-rag-', dir=parent))
    app = QApplication([])
    service = NativeService(directory)
    service.workspace.map_preloader.close()
    # Credentials stay in memory; all articles, sessions and requests use the isolated store.
    service.store.model_config = lambda: dict(config)
    client_requests = []
    original_client = service.workspace.client
    def client():
        client_requests.append(time.monotonic())
        return original_client()
    service.workspace.client = client
    if args.handoff:
        conversation_budget.save_policy(service.store, 8192)
    article = service.import_text('合成阅读安排', '青禾阅读小组每次阅读25分钟，然后休息5分钟。每次阅读后回到原文核对笔记。')
    note = service.hub.save('notes', {'article_id': article['id'], 'title': '个人复习心得',
                                    'content': '我的个人复习心得：休息后写下3个待核对的问题。'})
    host = QWidget()
    host.service = service
    host.tasks = Tasks(host)
    host.run = lambda action, done: host.tasks.submit(action, done)
    controller = LibraryController(host)
    report = {'directory': str(directory), 'model': config['model'], 'passed': False,
              'synthetic_content_only': True, 'controller': 'LibraryController', 'errors': []}
    started = time.monotonic()
    submitted = False
    finished = False
    second = None
    resumed = False
    question = '青禾与蓝桥两个阅读小组每次阅读和休息各几分钟？我的个人复习心得建议写下几个待核对的问题？请区分两份原文与个人心得，并分别引用来源。'

    def finish(error=None):
        nonlocal finished
        if finished:
            return
        finished = True
        if error:
            report['errors'].append(error)
        controller.timer.stop()
        timer.stop()
        report['elapsed_seconds'] = round(time.monotonic()-started, 2)
        report['passed'] = not report['errors'] and report.get('article_and_note_cited', False)
        (directory/'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        service.close()
        host.tasks.pool.waitForDone(5000)
        host.close()
        app.quit()

    def tick():
        nonlocal submitted, second, resumed
        if time.monotonic()-started > 180:
            controller.stop()
            finish('request_deadline_exceeded')
            return
        if not submitted and controller.key:
            submitted = True
            original_key = controller.key
            second = service.import_text('蓝桥阅读安排', '蓝桥阅读小组每次阅读40分钟，然后休息10分钟，结束后整理2条摘录。')
            report['new_article_in_existing_session'] = controller.key == original_key
            if args.handoff:
                controller.session = {'messages':[
                    {'role':'user', 'content':'Earlier reading notes. ' * 1000},
                    {'role':'assistant', 'content':'Earlier notes are synthetic test material.'},
                    {'role':'user', 'content':question},
                    {'role':'assistant', 'content':'请继续回到原文与笔记核对具体数字。'}], 'draft':''}
                service.store.put('sessions', controller.key, controller.session)
            controller.request(question)
        elif submitted and not controller.busy:
            if args.handoff and not resumed:
                if not controller.session.get('closed'):
                    finish('budget_did_not_close_session')
                    return
                report['budget_closed_without_model_request'] = not client_requests
                old = controller.session
                controller.restart()
                report['archive_preserved'] = controller.session.get('archives', [])[-1]['messages'] == old['messages']
                report['pending_question_preserved'] = controller.session.get('draft') == question
                report['retrieval_topic_preserved'] = question in controller.session.get('carryover_query', '')
                if not all(report[k] for k in ('budget_closed_without_model_request','archive_preserved','pending_question_preserved','retrieval_topic_preserved')):
                    finish('handoff_state_was_not_preserved')
                    return
                conversation_budget.save_policy(service.store, 16384)
                resumed = True
                controller.request('这些分别是多少？请接着核对原文和个人心得，并引用各自来源。')
                return
            messages = controller.session.get('messages', [])
            if not messages or messages[-1].get('role') != 'assistant':
                finish('no_saved_assistant_answer')
                return
            answer = messages[-1]
            evidence = answer.get('evidence', [])
            report['answer'] = answer['content']
            report['answer_model'] = answer.get('model')
            report['client_instances'] = len(client_requests)
            report['cited_sources'] = [{'article_id': e.get('article_id'), 'note_id': e.get('note_id'),
                                        'source': e.get('source'), 'citation': e.get('citation')} for e in evidence]
            report['article_and_note_cited'] = (any(e.get('note_id') == note['id'] for e in evidence)
                and all(any(e.get('article_id') == item['id'] and e.get('source') != 'personal_note' for e in evidence)
                        for item in (article, second)))
            if answer.get('model') == '本地检索':
                finish('model_was_not_called')
            elif not all(value in answer['content'] for value in ('25', '5', '40', '10', '3')):
                finish('expected_facts_missing')
            else:
                finish()

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(100)
    host.tasks.error.connect(lambda _: finish('background_task_failed'))
    controller.select([], True, 'question', scope='all')
    app.exec()
    print(json.dumps({k: report[k] for k in ('directory', 'model', 'passed', 'elapsed_seconds', 'errors')}, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
