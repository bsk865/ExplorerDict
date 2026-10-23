"""Verify real-model planning and undo through the native controller."""
import argparse
from datetime import date, timedelta
import json
import logging
from pathlib import Path
import tempfile
import time

from check_native_live_rag import ROOT, read_config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('configuration_directory', type=Path)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    config = read_config(args.configuration_directory.resolve(strict=True))
    if not config.get('key'):
        print(json.dumps({'passed': False, 'reason': 'model_key_missing'}))
        return 1
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QWidget
    from app.native.service import NativeService
    from app.native.advisor import AdvisorController

    parent = ROOT/'artifacts/native-checks'
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='live-plan-', dir=parent))
    app = QApplication([])
    service = NativeService(directory)
    service.store.model_config = lambda: dict(config)
    article = service.import_text('合成阅读任务', '读完后整理一个观点，并回到原文核对。')
    host = QWidget()
    host.service = service
    controller = AdvisorController(host)
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    controller.value = {'request': f'请在{tomorrow}上午09:00安排一项25分钟的阅读任务，阅读所选资料。只要这一项，直接添加计划。',
                        'article_ids': [article['id']], 'history': [],
                        'availability': {'start_date': tomorrow, 'end_date': tomorrow,
                                         'start_time': '09:00', 'end_time': '10:00', 'daily_minutes': 30}}
    report = {'directory': str(directory), 'model': config['model'], 'passed': False,
              'synthetic_content_only': True, 'controller': 'AdvisorController', 'errors': []}
    started = time.monotonic()
    finished = False

    def finish(error=None):
        nonlocal finished
        if finished:
            return
        finished = True
        if error:
            report['errors'].append(error)
        timer.stop()
        controller.timer.stop()
        report['elapsed_seconds'] = round(time.monotonic()-started, 2)
        report['passed'] = not report['errors'] and report.get('undo_removed_batch_tasks', False)
        (directory/'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        service.close()
        host.close()
        app.quit()

    def tick():
        if time.monotonic()-started > 180:
            controller.stop()
            finish('request_deadline_exceeded')
        elif controller.job is None:
            try:
                assert controller.result, 'no_saved_plan'
                tasks = service.hub.snapshot()['tasks']
                assert len(tasks) == 1, 'unexpected_task_count'
                task = tasks[0]
                assert task['date'] == tomorrow and task['time'] == '09:00' and task['minutes'] == 25, 'schedule_mismatch'
                assert task['article_ids'] == [article['id']], 'article_link_missing'
                report['saved_task'] = task
                assert not controller.value.get('request') and not controller.value.get('history'), 'submitted_draft_not_cleared'
                report['draft_cleared'] = True
                controller.undo()
                assert not service.hub.snapshot()['tasks'] and controller.result is None, 'undo_failed'
                assert service.store.article(article['id']), 'undo_removed_article'
                report['undo_removed_batch_tasks'] = True
                finish()
            except AssertionError as exc:
                finish(str(exc))

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(100)
    controller.submit()
    app.exec()
    print(json.dumps({k: report[k] for k in ('directory', 'model', 'passed', 'elapsed_seconds', 'errors')}, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
