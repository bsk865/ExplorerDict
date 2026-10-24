"""Run the native map workflow using a public paper and in-memory credentials."""
import argparse
import hashlib
import json
import logging
import os
import shutil
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from check_native_live_rag import read_config


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('configuration_directory', type=Path)
    parser.add_argument('public_pdf', type=Path)
    parser.add_argument('--title', default='RGraph-Eval · 公开论文')
    parser.add_argument('--synthetic', action='store_true')
    parser.add_argument('--resume', type=Path)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    config = read_config(args.configuration_directory.resolve(strict=True))
    if not config.get('key'):
        raise SystemExit('No configured test credential.')
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFont, QFontDatabase
    from app.native.service import NativeService
    from app.native.window import NativeWindow
    from app.native.maps import MapScene
    from app.map_input import markdown
    from app.workspace_server import parse_file
    from app import article_map, long_article_map
    from unittest.mock import patch
    parent = ROOT/'artifacts/native-checks'
    parent.mkdir(parents=True, exist_ok=True)
    if args.resume:
        directory=args.resume.resolve(strict=True)
        if not (directory/'.public-map-check').is_file():
            raise SystemExit('Resume requires an isolated public-paper check directory.')
        prior=json.loads((directory/'run.json').read_text(encoding='utf-8'))
        if prior.get('pdf_sha256') != hashlib.sha256(args.public_pdf.read_bytes()).hexdigest():
            raise SystemExit('Resume input differs from the original document.')
        if prior.get('policy_version', article_map.POLICY_VERSION) != article_map.POLICY_VERSION:
            raise SystemExit('Map policy changed; use a new isolated check directory.')
        history=directory/'attempt-history'/str(time.time_ns())
        history.mkdir(parents=True)
        for name in ('run.json','verification.json'):
            if (directory/name).is_file():
                shutil.copy2(directory/name,history/name)
        (directory/'verification.json').unlink(missing_ok=True)
    else:
        directory = Path(tempfile.mkdtemp(prefix='public-live-map-', dir=parent))
        (directory/'.public-map-check').touch()
    os.environ['EXPLORER_DICT_DATA_DIR'] = str(directory)
    os.environ['EXPLORER_DICT_STORAGE_CONFIG'] = str(directory/'location.json')
    application = QApplication([])
    application.setStyle('Fusion')
    font_id = QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
    application.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0], 10))
    service = NativeService(directory)
    service.store.model_config = lambda: dict(config)
    service.workspace.map_preloader.close()
    article = service.store.import_article(args.title, parse_file(service.store, args.public_pdf.name, args.public_pdf.read_bytes()))
    report = dict(directory=str(directory), model=config['model'], reasoning_effort=config.get('reasoning_effort'),
                  source='synthetic' if args.synthetic else 'https://arxiv.org/abs/2610.31121', characters=len(article['text']), public_material_only=not args.synthetic,
                  map_mode='text', policy_version=article_map.POLICY_VERSION,
                  segments_total=len(list(long_article_map.segments(article['text']))),
                  pdf_sha256=hashlib.sha256(args.public_pdf.read_bytes()).hexdigest(), passed=False)
    (directory/'run.json').write_text(json.dumps(dict(report, process_id=os.getpid(), started_at=time.time()),
                                                  ensure_ascii=False, indent=2), encoding='utf-8')
    started = time.monotonic()
    with patch('app.native.topbar.TopBar.check_capture', lambda self: None):
        window = NativeWindow(service, discovery_updates=False)
        window.show()
        def settle():
            deadline = time.monotonic()+15
            while time.monotonic() < deadline:
                application.processEvents()
                if not window.navigation_busy and not window.tasks.pending:
                    return
                time.sleep(.01)
            raise RuntimeError('Native navigation did not settle')
        settle()
        window.navigate('map', article['id'])
        settle()
        page = window.stack.currentWidget()
        page.controller.generate()
        message = ''
        def job_deadline():
            with service.workspace.lock:
                job = service.workspace.jobs.get(page.controller.job, {})
                return job.get('deadline', started+1800)+30
        while page.controller.job and time.monotonic() < job_deadline():
            application.processEvents()
            current = page.controller.message
            if current != message:
                print(current, flush=True)
                message = current
            time.sleep(.025)
        if page.controller.job:
            page.controller.stop()
            deadline = time.monotonic()+15
            while page.controller.job and time.monotonic() < deadline:
                application.processEvents()
                time.sleep(.025)
        latest = service.store.article(article['id'])
        graph = latest.get('map')
        quality = latest.get('quality') or {}
        report.update(elapsed_seconds=round(time.monotonic()-started, 2), message=page.controller.message,
                      passed=bool(graph and quality.get('approved')), segments=(graph or {}).get('segments', 1) if graph else None,
                      nodes=len((graph or {}).get('nodes', [])), relations=len((graph or {}).get('relations', [])))
        if report['passed']:
            scene = MapScene(graph, page.palette, include_relations=True)
            scene.export(directory/'map.svg')
            (directory/'map.md').write_text(markdown(latest), encoding='utf-8')
            (directory/'map.json').write_text(json.dumps(dict(graph=graph, quality=quality), ensure_ascii=False, indent=2), encoding='utf-8')
            window.grab().save(str(directory/'map-window.png'))
        (directory/'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        window.close()
        settle()
        window.close()
        service.close()
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
