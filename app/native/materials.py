"""Article versions, source acquisition and portable reading notes."""
import base64
from pathlib import Path
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMenu, QInputDialog, QFileDialog, QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem, QLabel
from .dialogs import EditForm
from .widgets import button


def article_info(host, article, parent=None):
    dialog = QDialog(parent or host)
    dialog.setWindowTitle('资料信息')
    dialog.resize(560, 240)
    layout = QVBoxLayout(dialog)
    title = QLabel(article['title'])
    title.setWordWrap(True)
    title.setStyleSheet('font-size:18px; font-weight:500;')
    layout.addWidget(title)
    images = sum(block['type'] == 'image' for block in article['blocks'])
    summary = QLabel(f"{len(article.get('text', '')):,} 字 · {images} 张图片")
    summary.setObjectName('muted')
    layout.addWidget(summary)
    url = QUrl(article.get('source_url', ''))
    if url.scheme() in ('http', 'https') and url.host():
        layout.addWidget(button('在原网页阅读', lambda: QDesktopServices.openUrl(url)))
    actions = QHBoxLayout()
    def run(action):
        dialog.accept()
        action(host, article)
    for label, action in [('导出笔记', export_notes), ('插入图片', append_file), ('补充正文', append_text)]:
        actions.addWidget(button(label, lambda checked=False, action=action: run(action)))
    layout.addLayout(actions)
    layout.addStretch()
    layout.addWidget(button('关闭', dialog.close))
    dialog.exec()


def extract(host, article=None):
    url, ok = QInputDialog.getText(host, '读取网页文章', '文章网址', text=(article or {}).get('source_url') or '')
    if ok and url.strip():
        host.run(lambda: host.service.extract(url.strip(), (article or {}).get('id')),
                 lambda value: host.open_article(value['id']))


def append_text(host, article):
    positions = [('文章末尾', None), ('文章开头', -1)] + [
        (f"第 {i+1} 段后 · {(b.get('text') or b.get('caption') or '图片')[:24]}", i) for i, b in enumerate(article['blocks'])]
    result = {}
    def save(body):
        blocks = [{'type': 'text', 'text': p} for p in body['text'].split('\n\n') if p.strip()]
        result.update(host.service.append(article['id'], blocks, body['after']))
    dialog = EditForm('补充原文', [('after', '位置', 'choice', None, positions), ('text', '原文', 'long', '', None)], save, host)
    if dialog.exec():
        host.open_article(result['id'])


def append_file(host, article):
    filename, _ = QFileDialog.getOpenFileName(host, '补充原文件', '', '资料 (*.txt *.md *.pdf *.docx *.png *.jpg *.jpeg *.webp *.bmp)')
    if filename:
        host.run(lambda: host.service.append_file(article['id'], filename), lambda value: host.open_article(value['id']))


def rename(host, article):
    title, accepted = QInputDialog.getText(host, '修改标题', '标题', text=article['title'])
    if accepted:
        host.run(lambda: host.service.rename(article['id'], title))


def versions(host, article):
    dialog = QDialog(host)
    dialog.setWindowTitle('文章版本')
    dialog.resize(560, 420)
    layout = QVBoxLayout(dialog)
    rows = QListWidget()
    for key in sorted(host.service.store.related_versions(article['id'])):
        value = host.service.store.article(key)
        item = QListWidgetItem(value['title'] + (' · 当前' if key == article['id'] else '') + '\n' + value['version'][:12], rows)
        item.setData(Qt.ItemDataRole.UserRole, key)
    def open_version(item):
        dialog.accept()
        host.open_article(item.data(Qt.ItemDataRole.UserRole))
    rows.itemDoubleClicked.connect(open_version)
    layout.addWidget(rows)
    layout.addWidget(button('打开', lambda: open_version(rows.currentItem()) if rows.currentItem() else None))
    layout.addWidget(button('关闭', dialog.close))
    dialog.exec()


def reading_markdown(service, article):
    lines = ['# '+article['title'], '版本：'+article['version']]
    for block in article['blocks']:
        if block['type'] == 'text':
            lines.append(block['text'])
        elif block.get('asset'):
            mime, raw = service.store.asset(block['asset'])
            lines.append('!['+block.get('caption', '原图')+'](data:'+mime+';base64,'+base64.b64encode(raw).decode()+')')
    for obj in [None, *article['objects']]:
        key = article['id']+':'+article['version']+':'+(obj['id'] if obj else '')
        session = service.store.get('sessions', key, {})
        if obj:
            lines.extend(['## '+obj['title'], '来源：'+obj['block'], obj.get('note', '')])
        else:
            lines.append('## 文章笔记')
        lines.append(session.get('note', ''))
        lines.extend(('**我**' if message['role'] == 'user' else '**小探**')+'\n\n'+message['content'] for message in session.get('messages', []))
    lines.append('## 随文心得')
    for note in service.hub.notes(article['id'])['notes']:
        lines.extend(['### '+note.get('title', '心得'), note.get('quote', ''), note['content']])
    return '\n\n'.join(line for line in lines if line) + '\n'


def export_notes(host, article):
    filename, _ = QFileDialog.getSaveFileName(host, '导出阅读笔记', '阅读笔记.md', 'Markdown (*.md)')
    if filename:
        def write():
            Path(filename).write_text(reading_markdown(host.service, article), encoding='utf-8')
        host.run(write, lambda _: host.notice('阅读笔记已导出'))


def link_image(host, article):
    candidates = [a for a in host.snapshot['articles'] if a['id'] != article['id']]
    if not candidates:
        host.notice('请先添加要关联的文章。')
        return
    fields = [('target', '目标文章', 'choice', '', [(a['title'], a['id']) for a in candidates])]
    result = {}
    def save(body):
        blocks = [{k: v for k, v in b.items() if k != 'id'} for b in article['blocks'] if b['type'] == 'image']
        result.update(host.service.append(body['target'], blocks))
    if EditForm('将图片关联到文章', fields, save, host).exec():
        host.open_article(result['id'])


def article_menu(host, article, parent):
    menu = QMenu(parent)
    menu.addAction('修改标题', lambda: rename(host, article))
    menu.addAction('补充原文', lambda: append_text(host, article))
    menu.addAction('补充原文件', lambda: append_file(host, article))
    menu.addAction('读取网页原文', lambda: extract(host, article))
    if any(b['type'] == 'image' for b in article['blocks']):
        menu.addAction('将图片关联到文章', lambda: link_image(host, article))
    menu.addAction('文章版本', lambda: versions(host, article))
    menu.addAction('导出阅读笔记', lambda: export_notes(host, article))
    menu.addSeparator()
    menu.addAction('删除文章及历史版本', lambda: host.delete('articles', article['id'], article['title']))
    return menu
