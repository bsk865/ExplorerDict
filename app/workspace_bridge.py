"""Explicit local handoffs from the selection window to the reading workspace."""
import json
import time
import uuid
from pathlib import Path


def send_translation(directory, text, origin='悬浮取词'):
    text = str(text)
    if not text.strip():
        raise ValueError('请先选中文字，或打开一个词条。')
    if len(text) > 300_000:
        raise ValueError('所选内容过长，请分段带入翻译。')
    queue = Path(directory) / 'workspace-handoffs'
    queue.mkdir(parents=True, exist_ok=True)
    key = f'{time.time_ns()}-{uuid.uuid4().hex}'
    path = queue / (key + '.json')
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(dict(kind='translate', text=text, origin=str(origin)[:250], created=time.time()), ensure_ascii=False), encoding='utf-8')
    temp.replace(path)
    return path


def take_translation(directory):
    queue = Path(directory) / 'workspace-handoffs'
    if not queue.is_dir():
        return None
    for path in sorted(queue.glob('*.json')):
        try:
            if path.stat().st_size > 2_000_000:
                path.unlink(missing_ok=True)
                continue
            value = json.loads(path.read_text(encoding='utf-8'))
            valid = (isinstance(value, dict) and value.get('kind') == 'translate'
                     and isinstance(value.get('text'), str) and 0 < len(value['text']) <= 300_000
                     and value['text'].strip() and isinstance(value.get('created'), (int, float))
                     and 0 <= time.time() - value['created'] <= 600)
            path.unlink(missing_ok=True)
            if valid:
                return dict(text=value['text'], origin=str(value.get('origin', '悬浮取词'))[:250])
        except (OSError, ValueError, TypeError):
            # Invalid local handoffs cannot interrupt the application's UI loop.
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    return None
