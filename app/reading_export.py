"""Portable notes from one article and its own local reading state."""
import base64
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

from .reader_source import safe_markup
from .translation import LANGUAGES
from .reading_annotations import block_text


def quote(text):
    return '\n'.join('> ' + line for line in str(text).splitlines())


def heading(text):
    return ' '.join(str(text).split())


def location(article, block_id=None, page=None, *, image=False, region=False):
    block = next((block for block in article['blocks'] if block['id'] == block_id), {})
    page = page or block.get('page')
    if page:
        label = f'第{page}页'
    elif block:
        label = f"原文第{article['blocks'].index(block)+1}处"
    else:
        label = '原文片段'
    if image or block.get('type') == 'image':
        label += ' · 图片选区' if region else ' · 图片'
    return label


def messages_markdown(article, messages):
    lines = []
    for message in messages:
        lines.extend(['**我**' if message.get('role') == 'user' else '**小探**', str(message.get('content', ''))])
        evidence = message.get('evidence', [])
        if evidence:
            lines.append('回答时引用的资料：')
        for hit in evidence:
            label = heading(hit.get('article_title') or article['title'])
            if hit.get('source') == 'personal_note' or hit.get('note_id'):
                label += ' · 个人笔记' + ('《'+heading(hit['note_title'])+'》' if hit.get('note_title') else '')
            elif hit.get('article_id') == article['id']:
                label += ' · ' + location(article, hit.get('block'), hit.get('page'),
                                          image=hit.get('type') == 'image', region=bool(hit.get('region')))
            elif hit.get('page'):
                label += f" · 第{hit['page']}页"
            lines.append(f"[{heading(hit.get('citation', '来源'))}] {label}")
            if hit.get('quote'):
                lines.append(quote(hit['quote']))
    return lines


def article_versions(service, article):
    related = service.store.related_versions(article['id'])
    with service.store.connect() as db:
        keys = [row[0] for row in db.execute('SELECT id FROM articles ORDER BY rowid') if row[0] in related]
    return [service.store.article(key) for key in keys]


def reading_markdown(service, article):
    # Include durable drafts while taking a consistent snapshot of this article.
    with service.store.lock:
        article = service.store.article(article['id'])
        lines = ['# ' + heading(article['title']), '## 原文']
        address = article.get('source_url', '')
        if urlsplit(address).scheme in ('http', 'https'):
            lines.insert(1, '原文链接：<' + address + '>')
        for block in article['blocks']:
            if block['type'] == 'text':
                lines.append(safe_markup(block['html']) if block.get('html') else block_text(block))
            elif block.get('asset'):
                mime, raw = service.store.asset(block['asset'])
                caption = heading(block.get('caption') or '原图').replace('[', '\\[').replace(']', '\\]')
                lines.append('!['+caption+'](data:'+mime+';base64,'+base64.b64encode(raw).decode()+')')
        marks = service.annotations.list(article['id'])['items']
        if marks:
            lines.append('## 原文标注')
        labels = {'yellow':'黄色荧光', 'green':'绿色荧光', 'pink':'粉色荧光', 'underline':'下划线'}
        for mark in marks:
            lines.append('### ' + labels.get(mark['style'], '标注'))
            for segment in mark['segments']:
                lines.extend([location(article, segment['block']), quote(segment['quote'])])
        for obj in [None, *article['objects']]:
            key = article['id']+':'+article['version']+':'+(obj['id'] if obj else '')
            session = service.store.get('sessions', key, {})
            if not (session.get('messages') or session.get('note') or session.get('archives')
                    or session.get('draft') or obj and obj.get('note')):
                continue
            lines.append('## '+heading(obj['title']) if obj else '## 文章笔记与问答')
            if obj:
                lines.append(location(article, obj.get('block'), image=obj['type'] != 'term', region=bool(obj.get('region'))))
                if obj.get('note'):
                    lines.extend(['### 对象笔记', obj['note']])
            if session.get('note'):
                lines.extend(['### 会话笔记', session['note']])
            for index, archived in enumerate(session.get('archives', []), 1):
                lines.append(f'### 先前对话 {index}')
                if archived.get('note'):
                    lines.extend(['会话笔记：', archived['note']])
                lines.extend(messages_markdown(article, archived.get('messages', [])))
            if session.get('messages'):
                lines.extend(['### 当前对话', *messages_markdown(article, session['messages'])])
            if session.get('draft', '').strip():
                lines.extend(['### 待继续的问题', session['draft']])
        notes = list(service.hub.notes(article['id'])['notes'])
        for draft in service.store.get('settings', 'native-note-drafts:'+article['id'], []):
            if draft.get('article_id') != article['id'] or not draft.get('content', '').strip():
                continue
            notes = [note for note in notes if not (draft.get('id') and note['id'] == draft['id'])
                     and not (draft.get('draft_id') and note.get('draft_id') == draft['draft_id'])]
            notes.append(dict(draft, export_draft=True))
        if notes:
            lines.append('## 随文心得')
        for note in notes:
            lines.append('### '+heading(note.get('title') or '心得') + ('（草稿）' if note.get('export_draft') else ''))
            if note.get('quote'):
                lines.extend([location(article, note.get('block')), quote(note['quote'])])
            lines.append(note['content'])
        translation = service.store.get('settings', 'native-translation:article:'+article['id'], {})
        if translation.get('result'):
            lines.append('## 译文' + ('（未完成）' if not translation.get('complete') else ''))
            lines.append(LANGUAGES.get(translation.get('source'), '自动识别') + ' → ' + LANGUAGES.get(translation.get('target'), '目标语言'))
            original = '\n\n'.join(block_text(block) for block in article['blocks'] if block['type'] == 'text')
            if translation.get('text') and translation['text'] != original:
                lines.extend(['### 本次翻译的原文', quote(translation['text']), '### 翻译结果'])
            lines.append(translation['result'])
        return '\n\n'.join(line for line in lines if line) + '\n'


def write_reading_notes(service, article, filename):
    text = reading_markdown(service, article)
    target = Path(filename)
    descriptor, temporary = tempfile.mkstemp(prefix='.'+target.name+'.', suffix='.tmp', dir=target.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
