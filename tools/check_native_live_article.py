"""Exercise article chat and image capability using synthetic material only."""
import argparse
import io
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

from check_native_live_rag import ROOT, read_config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('configuration_directory', type=Path)
    parser.add_argument('--image-budget', type=int, default=16384)
    parser.add_argument('--max-seconds', type=int, default=180)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--report-directory', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.max_seconds <= 600:
        parser.error('max-seconds must be between 1 and 600')
    parent = ROOT/'artifacts/native-checks'
    parent.mkdir(parents=True, exist_ok=True)
    directory = args.report_directory or Path(tempfile.mkdtemp(prefix='live-article-', dir=parent))
    if not args.worker:
        command = [sys.executable, str(Path(__file__).resolve()), str(args.configuration_directory.resolve()),
                   '--image-budget', str(args.image_budget), '--max-seconds', str(args.max_seconds),
                   '--worker', '--report-directory', str(directory)]
        environment = dict(os.environ, QT_QPA_PLATFORM='offscreen')
        started = time.monotonic()
        with (directory/'worker.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen(command, env=environment, stdout=log, stderr=log)
            try:
                code = process.wait(timeout=args.max_seconds + 15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
                report = {'directory':str(directory), 'passed':False, 'errors':['external_deadline_exceeded'],
                          'elapsed_seconds':round(time.monotonic()-started, 2)}
                (directory/'verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
                code = 1
        result = directory/'verification.json'
        report = json.loads(result.read_text(encoding='utf-8')) if result.exists() else {
            'directory':str(directory), 'passed':False, 'errors':['worker_report_missing']}
        print(json.dumps({k:report[k] for k in ('directory', 'passed', 'elapsed_seconds', 'errors') if k in report}, ensure_ascii=False))
        return 0 if code == 0 and report['passed'] else 1
    logging.disable(logging.CRITICAL)
    config = read_config(args.configuration_directory.resolve(strict=True))
    if not config.get('key'):
        (directory/'verification.json').write_text(json.dumps({'directory':str(directory), 'passed':False,
                                                              'errors':['model_key_missing']}), encoding='utf-8')
        return 1
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QWidget
    from PIL import Image, ImageDraw
    from app.native.service import NativeService
    from app.native.chat import ArticleConversation

    app = QApplication([])
    service = NativeService(directory)
    service.store.model_config = lambda: dict(config)
    article = service.import_text('合成阅读问答', '星河阅读活动每周三举行，每次阅读40分钟。活动结束后每人提出2个问题。')
    host = QWidget()
    host.service = service
    controller = ArticleConversation(host, article)
    report = {'directory': str(directory), 'model': config['model'], 'passed': False,
              'synthetic_content_only': True, 'errors': []}
    phase = 'text'
    detection = None
    started = time.monotonic()
    finished = False
    heartbeat = [time.monotonic()]
    watchdog_stop = threading.Event()

    def watch():
        captured = set()
        while not watchdog_stop.wait(1):
            age = time.monotonic() - heartbeat[0]
            if age < 5 or phase in captured:
                continue
            captured.add(phase)
            frames = []
            for thread_id, frame in sys._current_frames().items():
                stack = []
                while frame and len(stack) < 40:
                    stack.append({'file':frame.f_code.co_filename, 'function':frame.f_code.co_name, 'line':frame.f_lineno})
                    frame = frame.f_back
                frames.append({'thread':thread_id, 'stack':stack})
            # Code locations only: never serialize frame locals, model keys or request content.
            (directory/f'heartbeat-{phase}.json').write_text(json.dumps(
                {'phase':phase, 'heartbeat_age':age, 'threads':frames}, ensure_ascii=False, indent=2), encoding='utf-8')

    threading.Thread(target=watch, daemon=True).start()

    def finish(error=None):
        nonlocal finished
        if finished:
            return
        finished = True
        watchdog_stop.set()
        if error:
            report['errors'].append(error)
            report['failure_phase'] = phase
            message = controller.message
            for private in (config.get('key'), config.get('base_url')):
                if private:
                    message = message.replace(private, '[redacted]')
            report['controller_message'] = message
        timer.stop()
        controller.timer.stop()
        report['elapsed_seconds'] = round(time.monotonic()-started, 2)
        report['passed'] = not report['errors'] and report.get('text_answer_cited', False) and report.get('image_answer_cited', False)
        (directory/'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        service.close()
        host.close()
        app.quit()

    def tick():
        nonlocal phase, detection, controller
        heartbeat[0] = time.monotonic()
        try:
            if time.monotonic()-started > args.max_seconds:
                controller.stop()
                finish('request_deadline_exceeded')
                return
            if phase == 'text' and controller.job is None:
                messages = controller.session.get('messages', [])
                assert messages and messages[-1]['role'] == 'assistant', 'text_answer_missing'
                answer = messages[-1]
                assert '40' in answer['content'] and '2' in answer['content'], 'text_facts_missing'
                assert any(e['article_id'] == article['id'] for e in answer.get('evidence', [])), 'text_citation_missing'
                assert not controller.session.get('draft'), 'draft_not_cleared'
                report['text_answer'] = answer['content']
                report['text_answer_cited'] = True
                phase = 'detect'
                detection = service.workspace.start('detect', {})['job']
            elif phase == 'detect':
                with service.workspace.lock:
                    job = dict(service.workspace.jobs[detection])
                if job['status'] == 'running':
                    return
                result = service.workspace.config()['vision']
                report['vision_status'] = result.get('status', 'unknown')
                report['served_model'] = result.get('served_model', '')
                if result.get('status') != 'supported':
                    finish('image_capability_not_confirmed')
                    return
                picture = Image.new('RGB', (300, 180), 'white')
                paint = ImageDraw.Draw(picture)
                for x in (30, 110, 190):
                    paint.rectangle((x, 60, x+40, 100), fill='blue')
                raw = io.BytesIO()
                picture.save(raw, 'PNG')
                asset, _ = service.store.image(raw.getvalue())
                image_article = service.store.import_article('合成图片核对', [{'type': 'image', 'asset': asset, 'caption': '测试图'}])
                obj = next(o for o in service.store.article(image_article['id'])['objects'] if o['type'] == 'image')
                controller = ArticleConversation(host, image_article, obj['id'])
                from app.conversation_budget import save_policy
                save_policy(service.store, args.image_budget)
                report['isolated_image_budget'] = args.image_budget
                phase = 'image'
                controller.send('图片中有几个色块，是什么颜色？请引用图片依据。')
            elif phase == 'image' and controller.job is None:
                messages = controller.session.get('messages', [])
                assert messages and messages[-1]['role'] == 'assistant', 'image_answer_missing'
                answer = messages[-1]
                assert ('3' in answer['content'] or '三' in answer['content']) and '蓝' in answer['content'], 'image_facts_missing'
                assert any(e.get('image_sent') for e in answer.get('evidence', [])), 'image_citation_missing'
                report['image_answer'] = answer['content']
                report['image_answer_cited'] = True
                finish()
        except Exception as exc:
            finish(str(exc) if isinstance(exc, AssertionError) else type(exc).__name__)

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(100)
    controller.send('星河阅读活动每次阅读多久，结束后每人提出几个问题？请引用原文。')
    app.exec()
    print(json.dumps({k: report[k] for k in ('directory', 'model', 'passed', 'elapsed_seconds', 'errors')}, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
