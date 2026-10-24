"""Read saved management records and their references without opening a window."""
from PySide6.QtGui import QTextCursor, QTextBlockFormat, QTextCharFormat, QColor
from PySide6.QtWidgets import QWidget, QVBoxLayout, QTextBrowser
from .widgets import button, FlowLayout
from .progress import label
from ..reading_annotations import block_text
from ..reading_export import location


class ContentDetail(QWidget):
    def __init__(self, owner, record):
        super().__init__(owner)
        self.owner, self.host, self.record = owner, owner.host, record
        self.references = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        controls = FlowLayout(spacing=8)
        controls.addWidget(button('返回列表', owner.back_to_list))
        if owner.source(record):
            controls.addWidget(button('查看原文', lambda: owner.open_source(record)))
        layout.addLayout(controls)
        title = label(record.get('title') or record.get('article_title') or '查看内容', size=20)
        title.setMinimumWidth(0)
        layout.addWidget(title)
        self.browser = QTextBrowser()
        self.browser.setMinimumWidth(0)
        self.browser.setOpenLinks(False)
        self.browser.setOpenExternalLinks(False)
        self.browser.setAccessibleName('保存的内容与引用')
        self.browser.anchorClicked.connect(self.open_reference)
        layout.addWidget(self.browser, 1)
        self.cursor = self.browser.textCursor()
        if owner.kind == 'conversations':
            session = self.host.service.store.get('sessions', record['id'])
            if session is None:
                self.paragraph('这组对话已删除。')
            else:
                for index, archived in enumerate(session.get('archives', []), 1):
                    self.section(f'先前对话 {index}', archived)
                self.section('当前对话', session)
        elif owner.kind == 'annotations':
            article = self.host.service.store.get('articles', record.get('article_id'), {})
            for segment in record.get('segments', []):
                self.paragraph(location(article, segment.get('block')) if article else '原文片段', bold=True)
                self.paragraph(segment.get('quote', ''))
                self.reference(dict(article_id=record.get('article_id'), version=record.get('version'),
                                    block=segment.get('block'), quote=segment.get('quote', ''),
                                    article_title=record.get('article_title', '原文')))
        elif owner.kind == 'objects':
            self.paragraph(record.get('note', ''))
            if record.get('capture_context'):
                self.paragraph('划词时的上下文', bold=True)
                self.paragraph(record['capture_context'])
            article = self.host.service.store.get('articles', record.get('article_id'), {})
            block = next((b for b in article.get('blocks', []) if b['id'] == record.get('block')), {})
            if block.get('type') == 'text':
                self.paragraph('所在原文', bold=True)
                self.paragraph(block_text(block))
        else:
            for key in ('article_title', 'content', 'description', 'request'):
                self.paragraph(record.get(key, ''))
            for task in record.get('tasks', []):
                self.paragraph(f"{task.get('date', '')} {task.get('time', '')}  {task.get('title', '')}")
        self.browser.moveCursor(QTextCursor.MoveOperation.Start)
        self.status = label('', muted=True)
        layout.addWidget(self.status)

    def paragraph(self, text, *, bold=False, markdown=False):
        if not text:
            return
        self.cursor.insertBlock(QTextBlockFormat(), QTextCharFormat())
        if markdown:
            self.cursor.insertMarkdown(str(text))
        else:
            fmt = QTextCharFormat()
            fmt.setFontWeight(600 if bold else 400)
            self.cursor.insertText(str(text), fmt)
        self.cursor.insertBlock(QTextBlockFormat(), QTextCharFormat())

    def section(self, title, session):
        self.paragraph(title, bold=True)
        for message in session.get('messages', []):
            self.paragraph('我' if message.get('role') == 'user' else '小探', bold=True)
            self.paragraph(message.get('content', ''), markdown=True)
            for hit in message.get('evidence', []):
                self.paragraph(hit.get('quote', ''))
                self.reference(hit)
        if session.get('note'):
            self.paragraph('会话笔记', bold=True)
            self.paragraph(session['note'])
        if session.get('draft'):
            self.paragraph('待继续的问题', bold=True)
            self.paragraph(session['draft'])
        if session.get('handoff') and not session.get('messages'):
            self.paragraph('对话回顾', bold=True)
            self.paragraph(session['handoff'], markdown=True)
        if not any(session.get(key) for key in ('messages', 'note', 'draft', 'handoff')):
            self.paragraph('这段对话没有消息。')

    def reference(self, hit):
        self.references.append(dict(hit))
        title = hit.get('note_title') if hit.get('source') == 'personal_note' else hit.get('article_title')
        text = (title or '原文') + (f" · 第{hit['page']}页" if hit.get('page') else '')
        fmt = QTextCharFormat()
        fmt.setAnchor(True)
        fmt.setAnchorHref(f'content-source:{len(self.references)-1}')
        fmt.setFontUnderline(True)
        fmt.setForeground(QColor(self.owner.colors.get('accent-text', self.owner.colors['accent'])))
        self.cursor.insertText('查看来源：'+text, fmt)
        self.cursor.insertBlock(QTextBlockFormat(), QTextCharFormat())

    def open_reference(self, url):
        value = url.toString()
        if not value.startswith('content-source:'):
            return
        try:
            index = int(value.removeprefix('content-source:'))
            if not 0 <= index < len(self.references):
                return
            hit = self.references[index]
            article = self.host.service.store.article(hit.get('article_id'))
        except (ValueError, TypeError):
            self.status.setText('原资料已删除；这里保留的是回答时的引用。')
            return
        if hit.get('source') == 'personal_note':
            notes = self.host.service.hub.notes(article['id'])['notes']
            if not any(note['id'] == hit.get('note_id') for note in notes):
                self.status.setText('原笔记已删除；这里保留的是回答时的引用。')
                return
            self.host.open_article(article['id'], note_id=hit['note_id'])
        elif hit.get('source') == 'capture_context':
            self.host.open_article(article['id'], object_id=hit.get('object_id'))
        elif hit.get('version') and hit['version'] != article.get('version'):
            self.status.setText('原文版本已变化，请保留引用并重新核对。')
        elif hit.get('block') and not any(block['id'] == hit['block'] for block in article['blocks']):
            self.status.setText('原文位置已变化，请保留引用并重新核对。')
        else:
            self.host.open_article(article['id'], block=hit.get('block'), object_id=hit.get('object_id'))
