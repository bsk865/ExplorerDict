"""Prepare and audit isolated data for manual native settings relocation QA."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import io
from contextlib import closing

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from check_native_data_compatibility import fingerprints


def prepare():
    from app.native.service import NativeService
    from app.db import Database
    from app import reading_preferences, map_customization
    from PIL import Image
    parent = ROOT / 'artifacts/native-relocation'
    parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='relocation-', dir=parent))
    source, target = root/'original', root/'destination'
    target.mkdir()
    service = NativeService(source)
    picture = io.BytesIO()
    Image.new('RGB', (32, 24), '#6b8c77').save(picture, 'PNG')
    asset, _ = service.store.image(picture.getvalue())
    article = service.store.import_article('迁移验收资料', [
        {'type':'text','text':'阅读资料迁移后仍需保留。'},
        {'type':'image','asset':asset,'caption':'迁移图片'}], graph={
            'root':'root','nodes':[{'id':'root','parent':None,'label':'阅读资料','quote':'阅读资料迁移后仍需保留。'}], 'relations':[]})
    article, _, _ = map_customization.save(service.store, article, node_id='root', label='我的整理', position=[20, 10])
    service.store.put('settings', 'native-translation:article:'+article['id'],
                      {'source':'zh','target':'en','text':article['text'],'result':'Reading materials are retained.','complete':True})
    note = service.hub.save('notes', {'article_id': article['id'], 'title': '迁移验收笔记', 'content': '保留心得和原文关联。'})
    mark = service.annotations.save({'article_id': article['id'], 'version': article['version'], 'style': 'yellow',
                                    'segments': [{'block': 'b0', 'start': 0, 'end': 4, 'quote': '阅读资料'}]})
    service.close()
    with closing(Database(source/'explorer_dict.sqlite3')) as legacy:
        batch = legacy.create_batch('迁移主题')
        entry = legacy.add_entry(batch_id=batch, term='迁移词条', context='隔离资料中的上下文')
        legacy.set_setting('relocation-fixture', 'shared settings retained')
    reading_preferences.save(source, {'palette':'forest','mode':'light'})
    attachment = source/'attachments'/'original-paper.pdf'
    attachment.parent.mkdir()
    import pymupdf
    with pymupdf.open() as document:
        document.new_page().insert_text((40, 60), 'Retained original attachment')
        attachment.write_bytes(document.tobytes())
    (source/'retained-asset.txt').write_text('Retain this file across relocation.', encoding='utf-8')
    (root/'storage-location.json').write_text(json.dumps({'active': str(source)}), encoding='utf-8')
    fixture = {'directory': str(root), 'source': str(source), 'target': str(target),
               'article': article['id'], 'note': note['id'], 'mark': mark['id'],
               'asset':asset,'batch':batch,'entry':entry}
    (root/'fixture.json').write_text(json.dumps(fixture, ensure_ascii=False, indent=2), encoding='utf-8')
    (root/'.native-relocation-check').touch()
    print(json.dumps(fixture, ensure_ascii=False))
    return root


def check(root):
    fixture = json.loads((root/'fixture.json').read_text(encoding='utf-8'))
    source, target = Path(fixture['source']), Path(fixture['target'])
    config = json.loads((root/'storage-location.json').read_text(encoding='utf-8'))
    databases = [p.name for p in source.glob('*.sqlite3')]
    report = {'directory': str(root), 'active_target': Path(config['active']).resolve() == target.resolve(),
              'pending_cleared': 'pending' not in config, 'error_cleared': 'error' not in config,
              'original_retained': source.is_dir(), 'databases': {}}
    for name in databases:
        left, right = fingerprints(source/name), fingerprints(target/name)
        report['databases'][name] = {'equal': left == right, 'integrity': right['_integrity']}
    report['asset_equal'] = (source/'retained-asset.txt').read_bytes() == (target/'retained-asset.txt').read_bytes()
    report['attachment_equal'] = ((source/'attachments/original-paper.pdf').read_bytes()
                                  == (target/'attachments/original-paper.pdf').read_bytes())
    report['passed'] = (all(report[k] for k in ('active_target', 'pending_cleared', 'error_cleared', 'original_retained', 'asset_equal'))
                        and report['attachment_equal']
                        and bool(databases) and all(v['equal'] and v['integrity'] == 'ok' for v in report['databases'].values()))
    (root/'relocation-verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'check'])
    parser.add_argument('directory', type=Path, nargs='?')
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare()
    else:
        if not args.directory:
            parser.error('check requires the fixture directory')
        raise SystemExit(check(args.directory.resolve()))
