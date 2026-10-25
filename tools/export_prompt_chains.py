"""Export selected visible conversation ranges for the project development archive."""
import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import re
import shutil


def runtime_message(role, text):
    return role == 'user' and bool(re.search(
        r'<(?:environment_context|codex_internal_context)\b|Another language model started to solve', text))


def public_entries(path):
    text = path.read_text(encoding='utf-8')
    pattern = re.compile(r'^<a id="([^"]+)"></a>\n### \1 · (用户|AI) · [^\n]+\n\n', re.M)
    matches = list(pattern.finditer(text))
    for number, match in enumerate(matches):
        end = matches[number + 1].start() if number + 1 < len(matches) else len(text)
        boundary = text.find('\n## 会话区间 ', match.end(), end)
        if boundary >= 0:
            end = boundary
        yield match[1], 'user' if match[2] == '用户' else 'assistant', text[match.end():end].strip(), match.start(), end


def check_public(output, clean=False):
    index_path = output / '导出索引.json'
    manifest = json.loads(index_path.read_text(encoding='utf-8'))
    records = {(row['chain'], row['entry']): row for row in manifest}
    seen, removed = set(), set()
    for path in sorted(output.glob('[ABC]-*.md')):
        text = path.read_text(encoding='utf-8')
        deletions = []
        count = 0
        for ident, role, body, start, end in public_entries(path):
            key = (path.stem, ident)
            if clean and runtime_message(role, body):
                removed.add(key)
                deletions.append((start, end))
                continue
            if runtime_message(role, body):
                raise ValueError(f'Runtime message in public chain: {key}')
            row = records[key]
            if row['role'] != role or hashlib.sha256(body.encode()).hexdigest() != row['public_text_sha256']:
                raise ValueError(f'Public text digest mismatch: {key}')
            seen.add(key)
            count += 1
        for start, end in reversed(deletions):
            text = text[:start] + text[end:]
        if deletions:
            path.write_text(text, encoding='utf-8', newline='\n')
        print(path.stem, count)
    if set(records) != seen | removed:
        raise ValueError('Manifest entries do not match the public chains')
    if removed:
        manifest = [row for row in manifest if (row['chain'], row['entry']) not in removed]
        index_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8', newline='\n')
    print(f'Checked {len(seen)} public messages; removed {len(removed)} runtime messages.')


def messages(path):
    rows = []
    for line in path.open(encoding='utf-8'):
        item = json.loads(line)
        payload = item.get('payload', {})
        if item.get('type') != 'response_item' or payload.get('type') != 'message':
            continue
        if payload.get('role') not in ('user', 'assistant'):
            continue
        body = '\n'.join(c.get('text', '') for c in payload.get('content', [])
                         if c.get('type') in ('input_text', 'output_text'))
        if body:
            rows.append((payload['role'], item.get('timestamp'), body))
    return rows


def export(early, late, output):
    sources = {'early': messages(early), 'late': messages(late)}
    chains = [
        ('A-本地RAG与本机分类', [('early', 4, 5), ('early', 118, 136), ('late', 0, 10)]),
        ('B-后台预读与请求重试', [('late', 29, 54), ('late', 62, 68)]),
        ('C-学习规划与任务进度', [('early', 6, 82), ('late', 69, 76)]),
    ]
    output.mkdir(parents=True, exist_ok=True)
    assets = output / '附件'
    assets.mkdir(exist_ok=True)
    manifest = []
    for title, ranges in chains:
        lines = [f'# {title}', '',
            '按功能相关的连续会话区间导出，区间内保留用户与 AI 的可见文本往返及时间顺序。时间为北京时间。',
            '公开副本对凭据、本机路径和不当用语作标记处理；工具执行细节见配套验收记录。', '']
        count = 0
        for source, start, end in ranges:
            lines += [f'## 会话区间 {source} · {start}–{end}', '']
            for index in range(start, end + 1):
                role, stamp, original = sources[source][index]
                if runtime_message(role, original):
                    continue
                body = original
                attachments = re.findall(r'<image[^>]*path="([^"]+)"[^>]*>.*?</image>', body, re.S)
                for filename in attachments:
                    path = Path(filename)
                    if path.is_file():
                        shutil.copy2(path, assets / path.name)
                body = re.sub(r'<image[^>]*path="([^"]+)"[^>]*>.*?</image>',
                    lambda m: f'\n![用户反馈截图](附件/{Path(m[1]).name})\n', body, flags=re.S)
                body = re.sub(r'\[([^\]]+)\]\(<?(?:[A-Za-z]:[/\\]|/D:/)[^)]*\)',
                              r'\1（本机文件）', body)
                body = re.sub(r'(?:[A-Za-z]:[/\\])Users[/\\][^/\\\s]+', '[用户目录]', body)
                body = re.sub(r'(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]+)',
                              '[凭据已脱敏]', body)
                body = re.sub(r'(?i)([?&]token=)[^\s<>"\)]+', r'\1[已脱敏]', body)
                body = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._-]{15,}', 'Bearer [已脱敏]', body)
                body = re.sub(r'傻逼|他妈的|操你|\bfuck\w*\b|\bshit\b', '[用语已规范]', body, flags=re.I)
                date = datetime.fromisoformat(stamp.replace('Z', '+00:00')).astimezone(timezone(timedelta(hours=8)))
                ident = f'{source}-{index:03d}'
                lines += [f'<a id="{ident}"></a>', f'### {ident} · {"用户" if role == "user" else "AI"} · {date:%Y-%m-%d %H:%M:%S}', '', body.strip(), '']
                manifest.append({'chain': title, 'entry': ident, 'role': role,
                    'timestamp': stamp, 'source_text_sha256': hashlib.sha256(original.encode()).hexdigest(),
                    'public_text_sha256': hashlib.sha256(body.strip().encode()).hexdigest()})
                count += 1
        (output / (title + '.md')).write_text('\n'.join(lines), encoding='utf-8', newline='\n')
        print(title, count)
    (output / '导出索引.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8', newline='\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--early', type=Path)
    parser.add_argument('--late', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--check-public', action='store_true')
    parser.add_argument('--clean-public', action='store_true')
    args = parser.parse_args()
    if args.check_public or args.clean_public:
        check_public(args.output, clean=args.clean_public)
    else:
        if not args.early or not args.late:
            parser.error('Export requires --early and --late')
        export(args.early, args.late, args.output)
