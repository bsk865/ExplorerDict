"""Ask about a public PDF table through the article's image-region conversation."""
import argparse
import base64
import io
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('configuration_directory', type=Path)
    parser.add_argument('isolated_directory', type=Path, help='An existing isolated vision-check directory')
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    from tools.check_native_live_rag import read_config
    from PySide6.QtWidgets import QApplication
    from app.native.service import NativeService
    from app.native.window import NativeWindow
    from PIL import Image
    import pymupdf

    config = read_config(args.configuration_directory.resolve(strict=True))
    directory = args.isolated_directory.resolve(strict=True)
    # Never use the live configuration directory as the test workspace.
    assert directory != args.configuration_directory.resolve(strict=True)
    assert directory.parent == (ROOT/'artifacts/native-checks').resolve()
    os.environ['EXPLORER_DICT_DATA_DIR'] = str(directory)
    os.environ['EXPLORER_DICT_STORAGE_CONFIG'] = str(directory/'location.json')
    app = QApplication([])
    app.setStyle('Fusion')
    service = NativeService(directory)
    service.workspace.map_preloader.close()
    service.workspace.rag.request = lambda: None
    service.store.model_config = lambda: dict(config)
    requests = []
    original_client = service.workspace.client

    def client():
        value = original_client()
        chat = value.chat
        def record(messages, *args, **kwargs):
            sizes = []
            for message in messages:
                for part in message['content'] if isinstance(message['content'], list) else []:
                    if part.get('type') == 'image_url':
                        encoded = part['image_url']['url'].split(',', 1)[1]
                        with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
                            sizes.append(list(image.size))
            requests.append(dict(image_sizes=sizes))
            return chat(messages, *args, **kwargs)
        value.chat = record
        return value
    service.workspace.client = client
    pdf = ROOT/'artifacts/fulltext-translation-20261024/public-paper.pdf'
    with pymupdf.open(pdf) as document:
        raw = document[7].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5)).tobytes('png')
    asset, size = service.store.image(raw)
    article = service.store.import_article('RGraph-Eval · 表格图片', [
        dict(type='image', asset=asset, caption='RGraph-Eval · 第8页', page=8)])
    region = [.17, .445, .66, .212]
    obj = service.store.object(article['id'], dict(type='region', title='表2 · 局部', block='b0', asset=asset, region=region))
    session_key = article['id'] + ':' + article['version'] + ':' + obj['id']
    assert not service.store.get('sessions', session_key, {}).get('messages'), 'Use a fresh isolated workspace without prior answers'
    with Image.open(io.BytesIO(raw)) as image:
        x, y, w, h = region
        box = [int(x*size[0]), int(y*size[1]), int((x+w)*size[0]), int((y+h)*size[1])]
        image.crop(box).save(directory/'table-region.png')
        expected_size = [box[2]-box[0], box[3]-box[1]]
    question = ('请只根据选区表2的 Human evaluation 部分，分别给出 Ours 的 Objects F1 和 Relations F1，'
                '并与 SG-Ego 对应两项比较，引用这张图；不要混入 Dataset-stratified PPI 部分。')
    started = time.monotonic()
    report = dict(passed=False, source='https://arxiv.org/abs/2610.31121', pdf_page=8,
                  model=config['model'], reasoning_effort=config.get('reasoning_effort'),
                  public_image_only=True, user_articles_sent=False, fresh_session=True, question=question,
                  expected_crop_size=expected_size, region=region)
    with patch('app.native.topbar.TopBar.check_capture', lambda self: None):
        window = NativeWindow(service, discovery_updates=False)
        window.show()
        def settle():
            while window.navigation_busy or window.tasks.pending:
                app.processEvents()
                time.sleep(.01)
            for _ in range(10):
                app.processEvents()
        settle()
        window.open_article(article['id'], object_id=obj['id'])
        settle()
        controller = window.stack.currentWidget().chat.controller
        assert controller.object_id == obj['id'], 'Requested image region was not selected'
        controller.send(question)
        print(json.dumps(dict(directory=str(directory), job=controller.job, model=config['model'])), flush=True)
        while controller.job:
            app.processEvents()
            if time.monotonic()-started > 240:
                controller.stop()
            time.sleep(.02)
        messages = controller.session.get('messages', [])
        answer = messages[-1] if messages and messages[-1]['role'] == 'assistant' else {}
        report.update(elapsed_seconds=round(time.monotonic()-started, 2),
                      requests=requests, answer=answer, status=controller.message)
        content = answer.get('content', '')
        report['facts_correct'] = all(re.search(pattern, content) for pattern in (r'89\.67', r'82\.66', r'91\.78', r'67\.8(?:0)?'))
        report['crop_sent'] = any(request['image_sizes'] == [expected_size] for request in requests)
        report['all_images_within_selection'] = bool(requests) and all(
            size == expected_size for request in requests for size in request['image_sizes'])
        report['selected_image_cited'] = any(e.get('asset') == asset and e.get('region') == region and e.get('image_sent')
                                               for e in answer.get('evidence', []))
        report['passed'] = all(report[key] for key in ('facts_correct', 'crop_sent', 'all_images_within_selection', 'selected_image_cited'))
        # Save public-only evidence; credentials were never persisted in this directory.
        text = json.dumps(report, ensure_ascii=False, indent=2).replace(config['key'], '[redacted]')
        previous = directory/'image-answer.json'
        if previous.exists():
            history = directory/'attempt-history'
            history.mkdir(exist_ok=True)
            shutil.copy2(previous, history/(str(time.time_ns())+'.json'))
        (directory/'image-answer.json').write_text(text, encoding='utf-8')
        print(json.dumps({key:report[key] for key in ('passed','facts_correct','crop_sent','all_images_within_selection','selected_image_cited','elapsed_seconds','requests')}, ensure_ascii=False), flush=True)
        window.close()
        settle()
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
