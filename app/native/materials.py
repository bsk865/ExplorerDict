"""Article versions, source acquisition and portable reading notes."""
import re
from pathlib import Path
from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import (QMenu, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem, QLabel)
from .surfaces import QInputDialog, QFileDialog, QDialog
from .dialogs import EditForm
from .widgets import button
from .external_links import open_source, copy_link
from ..reading_export import reading_markdown, article_versions, write_reading_notes
from ..reading_annotations import block_text


def text_length(article):
    return sum(len(block_text(block)) for block in article['blocks'] if block['type'] == 'text')


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
    summary = QLabel(f"{text_length(article):,} 字 · {images} 张图片")
    summary.setObjectName('muted')
    layout.addWidget(summary)
    url = QUrl(article.get('source_url', ''))
    if url.scheme() in ('http', 'https') and url.host():
        layout.addWidget(button('在原网页阅读', lambda: open_source(host, url.toString())))
        layout.addWidget(button('复制原站链接', lambda: copy_link(host, url.toString())))
    document = article.get('source_document', {})
    if document.get('asset'):
        def save_original():
            import re
            name = re.sub(r'[<>:"/\\|?*]', '_', article['title'][:60]).rstrip('. ') or '论文'
            filename, _ = QFileDialog.getSaveFileName(dialog, '保存原始论文', name + '.pdf', 'PDF (*.pdf)')
            if filename:
                try:
                    Path(filename).write_bytes(host.service.store.asset(document['asset'])[1])
                    host.notice('原始论文已保存')
                except (OSError, ValueError):
                    host.notice('未能保存原始论文，请检查文件夹权限。')
        layout.addWidget(button('保存原始 PDF', save_original))
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
    for index, value in enumerate(article_versions(host.service, article), 1):
        images = sum(block['type'] == 'image' for block in value['blocks'])
        label = f"{value['title']} · 第{index}版" + (' · 正在阅读' if value['id'] == article['id'] else '')
        item = QListWidgetItem(label + f"\n{text_length(value):,} 字 · {images} 张图片", rows)
        item.setData(Qt.ItemDataRole.UserRole, value['id'])
    def open_version(item):
        dialog.accept()
        host.open_article(item.data(Qt.ItemDataRole.UserRole))
    rows.itemDoubleClicked.connect(open_version)
    layout.addWidget(rows)
    layout.addWidget(button('打开', lambda: open_version(rows.currentItem()) if rows.currentItem() else None))
    layout.addWidget(button('关闭', dialog.close))
    dialog.exec()


def export_notes(host, article):
    controller = getattr(host, 'translation_controllers', {}).get('article:'+article['id'])
    if controller:
        controller.flush()
    name = re.sub(r'[<>:"/\\|?*]', '_', article['title'][:60]).rstrip('. ') or '文章'
    filename, _ = QFileDialog.getSaveFileName(host, '导出阅读笔记', name+' · 阅读笔记.md', 'Markdown (*.md)')
    if filename:
        host.run(lambda: write_reading_notes(host.service, article, filename), lambda _: host.notice('阅读笔记已导出'))


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
