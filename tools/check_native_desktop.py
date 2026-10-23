"""Exercise the real native window with an isolated article, mark, note and task."""
import json
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile
from datetime import date

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.native.service import NativeService
from PIL import Image, ImageDraw


def import_fixtures(directory, png):
    import pymupdf
    folder = directory / 'import-fixtures'
    folder.mkdir()
    with pymupdf.open() as document:
        page = document.new_page(width=500, height=600)
        page.insert_text((40, 60), 'Native PDF import verification', fontsize=18)
        page.insert_image(pymupdf.Rect(40, 100, 460, 345), stream=png)
        document.save(folder / 'sample.pdf')
    with zipfile.ZipFile(folder / 'sample.docx', 'w') as archive:
        archive.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body><w:p><w:r><w:t>Native Word import verification</w:t></w:r></w:p><w:p><w:r><w:drawing><a:blip r:embed="image1"/></w:drawing></w:r></w:p></w:body></w:document>')
        archive.writestr('word/_rels/document.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="image1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image.png"/></Relationships>')
        archive.writestr('word/media/image.png', png)


def main():
    artifacts = ROOT / 'artifacts/native-checks'
    artifacts.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='native-', dir=artifacts))
    service = NativeService(directory)
    picture = Image.new('RGB', (720, 420), '#f4f7f6')
    paint = ImageDraw.Draw(picture)
    paint.rectangle((60, 80, 250, 280), fill='#6b8c77')
    paint.rectangle((430, 140, 620, 340), fill='#8583a6')
    paint.line((250, 180, 430, 240), fill='#302d29', width=5)
    raw = io.BytesIO()
    picture.save(raw, 'PNG')
    import_fixtures(directory, raw.getvalue())
    asset, _ = service.store.image(raw.getvalue())
    article = service.store.import_article('原生阅读验证', [
        {'type': 'text', 'text': '阅读让想法逐渐清晰。\n保留每一次好奇。'},
        {'type': 'text', 'text': '第二段用于验证跨段落的位置与笔记。'},
        {'type': 'image', 'asset': asset, 'caption': '阅读与核对示意图'}])
    service.hub.save('themes', {'title': '阅读与思考'})
    service.hub.save('notes', {'article_id': article['id'], 'title': '读后心得', 'content': '记录理解，再回到原文核对。'})
    service.hub.save('tasks', {'title': '阅读并整理心得', 'date': date.today().isoformat(), 'minutes': 25, 'article_ids': [article['id']]})
    service.annotations.save({'article_id': article['id'], 'version': article['version'], 'style': 'yellow',
                              'segments': [{'block': 'b0', 'start': 0, 'end': 2, 'quote': '阅读'}]})
    article['map'] = {'root': 'n0', 'nodes': [
        {'id': 'n0', 'parent': None, 'label': '阅读与理解', 'quote': '阅读让想法逐渐清晰。'},
        {'id': 'n1', 'parent': 'n0', 'label': '保留好奇', 'quote': '保留每一次好奇。',
         'evidence': [{'kind': 'image', 'asset': asset, 'block': 'b2', 'page': 1}]},
        {'id': 'n2', 'parent': 'n0', 'label': '核对笔记', 'quote': '第二段用于验证跨段落的位置与笔记。'}],
        'relations': [], 'bindings': []}
    article.pop('objects', None)
    service.store.update_article(article)
    obj = service.store.object(article['id'], {'type': 'term', 'block': 'b0', 'title': '好奇'})
    service.store.update_object(obj['id'], {'saved': True, 'note': '留意原文的表达。'})
    service.close()
    command = [sys.executable, str(ROOT / 'native_desktop.py'), '--verify-native', '--data-dir', str(directory)]
    working_directory = ROOT
    environment = os.environ.copy()
    if len(sys.argv) > 1:
        executable = Path(sys.argv[1]).resolve()
        command = [str(executable), '--verify-native', '--data-dir', str(directory)]
        working_directory = executable.parent
        environment.pop('PYTHONPATH', None)
        environment.pop('PYTHONHOME', None)
        system = Path(environment.get('SystemRoot', 'C:/Windows'))
        environment['PATH'] = str(system/'System32') + os.pathsep + str(system)
    with (directory / 'process.log').open('w', encoding='utf-8') as log:
        process = subprocess.Popen(command, cwd=working_directory, env=environment, stdout=log, stderr=log)
        try:
            code = process.wait(timeout=40)
        except subprocess.TimeoutExpired:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
            raise
    path = directory / 'native-verification.json'
    result = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'passed': False}
    print(json.dumps({'directory': str(directory), 'exit_code': code, 'result': result}, ensure_ascii=False))
    return 0 if code == 0 and result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
