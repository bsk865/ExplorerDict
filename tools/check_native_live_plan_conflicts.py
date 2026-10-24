"""Exercise a real plan interrupted by a new appointment, using synthetic data."""
from contextlib import closing
from datetime import date, datetime, timedelta
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from urllib.parse import urlsplit

from check_native_live_rag import ROOT, read_config


def main():
    logging.disable(logging.CRITICAL)
    config = read_config(Path(sys.argv[1]).resolve(strict=True))
    assert urlsplit(config['base_url']).hostname == 'api.deepseek.com' and config.get('key')
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    from PySide6.QtWidgets import QApplication, QWidget
    from app.article_map import parse_object
    from app.native.service import NativeService
    from app.native.advisor import AdvisorController
    from app.study_advisor import validate_availability

    directory = Path(tempfile.mkdtemp(prefix='live-plan-conflicts-', dir=ROOT/'artifacts/native-checks'))
    app = QApplication([])
    service = NativeService(directory)
    service.workspace.map_preloader.close()
    service.store.model_config = lambda: dict(config)
    article = service.import_text('阅读与核对练习', '读完后写下一个观点，再回到原文核对。')
    host = QWidget()
    host.service = service
    controller = AdvisorController(host)
    day = (date.today()+timedelta(days=1)).isoformat()
    limits = dict(start_date=day,end_date=day,start_time='09:00',end_time='12:00',daily_minutes=50)
    existing = service.hub.save('tasks', dict(title='固定复习',date=day,time='09:00',minutes=30))
    controller.value = dict(request=f'请在{day}09:00到12:00安排两次阅读，每次25分钟，总计50分钟，'
        '两次都关联所选资料，读后写一条核对笔记。避开已有安排，直接保存。',
        availability=limits,article_ids=[article['id']],history=[])
    report = dict(passed=False, directory=str(directory), model=config['model'],
        reasoning_effort=config.get('reasoning_effort'), synthetic_content_only=True,
        requests=[], existing=existing, availability=limits)
    original_client = service.workspace.client
    def client():
        value = original_client()
        original_chat = value.chat
        def chat(messages):
            assert len(report['requests']) < 2, 'Unexpected third model request'
            report['requests'].append(dict(messages=len(messages),
                characters=sum(len(m.get('content','')) for m in messages)))
            response = original_chat(messages)
            parsed = parse_object(response)
            if len(report['requests']) == 1:
                proposed = parsed.get('tasks', [])
                report['first_proposal'] = parsed
                assert not parsed.get('questions') and len(proposed) == 2, 'Expected two concrete tasks'
                assert all(t['minutes'] == 25 for t in proposed), 'Unexpected requested duration'
                validate_availability(proposed, limits)
                first = proposed[0]
                report['late_appointment'] = service.hub.save('tasks', dict(title='临时加入的讨论',
                    date=first['date'],time=first['time'],minutes=first['minutes']))
            else:
                context = json.loads(messages[-1]['content'])
                assert context.get('conflict') and len(context['previous_proposal']) == 2
                assert any(t['title'] == '临时加入的讨论' for t in context['existing_tasks'])
                report['repair_saw_latest_schedule'] = True
                report['repair_response'] = parsed
            return response
        value.chat = chat
        return value
    service.workspace.client = client
    started = time.monotonic()
    try:
        controller.submit()
        while controller.job:
            app.processEvents()
            if time.monotonic()-started > 240:
                raise RuntimeError('Planning deadline exceeded')
            time.sleep(.01)
        assert controller.result and controller.result.get('rescheduled'), controller.message
        report['result'] = controller.result
        tasks = controller.result['tasks']
        assert len(tasks) == 2 and sum(t['minutes'] for t in tasks) == 50
        for before, after in zip(report['first_proposal']['tasks'], tasks):
            for key in ('title','description','goal','minutes','article_ids'):
                assert before.get(key, '' if key in ('description','goal') else []) == after[key]
            assert after['article_ids'] == [article['id']]
        validate_availability(tasks, limits)
        all_tasks = service.hub.snapshot()['tasks']
        assert len(all_tasks) == 4 and len(service.hub.snapshot()['batches']) == 1
        intervals = sorted((datetime.fromisoformat(t['date']+'T'+t['time']),t['minutes']) for t in all_tasks)
        assert all(a+timedelta(minutes=m) <= b for (a,m),(b,_) in zip(intervals,intervals[1:]))
        assert existing in all_tasks and report['late_appointment'] in all_tasks
        assert not controller.value.get('request') and not controller.value.get('history')
        with closing(sqlite3.connect(service.store.path)) as old:
            with closing(sqlite3.connect(directory/'before-undo.sqlite3')) as new:
                old.backup(new)
        report['exact_task_content_preserved'] = True
        report['non_overlapping_saved_schedule'] = True
        controller.undo()
        assert {t['id'] for t in service.hub.snapshot()['tasks']} == {existing['id'],report['late_appointment']['id']}
        report['undo_preserved_both_appointments'] = True
        report['passed'] = True
    except Exception as exc:
        report['error'] = type(exc).__name__+': '+str(exc)
    finally:
        controller.stop()
        controller.timer.stop()
        deadline = time.monotonic()+15
        while any(j.get('status') == 'running' for j in service.workspace.jobs.values()) and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.01)
        service.close()
        host.close()
        report['elapsed_seconds'] = round(time.monotonic()-started,2)
        (directory/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:report.get(k) for k in ('directory','passed','model','reasoning_effort','elapsed_seconds','error')} |
        dict(model_requests=len(report['requests'])),ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
