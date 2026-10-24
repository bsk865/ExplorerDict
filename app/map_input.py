"""Choose and label map evidence without requiring vision for text PDF pages."""


def resolve_mode(article, mode='auto'):
    if mode not in {'auto', 'text', 'multimodal'}:
        raise ValueError('请选择正文导图或图文导图。')
    blocks = article.get('blocks', [])
    text = [b for b in blocks if b['type'] == 'text' and b.get('text', '').strip()]
    images = [b for b in blocks if b['type'] == 'image']
    pages = {b.get('page') for b in text if b.get('page')}
    complete_pdf_text = bool(images) and all(b.get('page') and b['page'] in pages for b in images)
    if mode == 'auto':
        return 'text' if not images or complete_pdf_text else 'multimodal'
    if mode == 'text':
        if not text:
            raise ValueError('这份资料没有可提取的正文，请选择图文导图并使用支持图片的模型。')
        if any(b.get('page') and b['page'] not in pages for b in images):
            raise ValueError('资料包含没有可提取正文的扫描页，请选择图文导图，避免遗漏页面。')
    return mode


def markdown(article):
    from .map_customization import display_graph
    graph = display_graph(article)
    nodes = {n['id']: n for n in graph.get('nodes', [])}
    children = {}
    for node in nodes.values():
        children.setdefault(node.get('parent'), []).append(node)
    lines = ['# '+article['title'], '', '生成依据：'+('正文（未分析图片）' if (article.get('quality') or {}).get('input_mode') == 'text' else '正文与图片；具体依据见各节点'), '', '## 结构', '']
    visited = set()
    def visit(key, level=0):
        if key in visited or key not in nodes:
            return
        visited.add(key)
        node = nodes[key]
        lines.append('  '*level+'- '+node['label'].replace('\n', ' '))
        if node.get('manual_label'):
            lines.append('  '*(level+1)+'手动修改；原标签：'+node['original_label'].replace('\n', ' '))
        if node.get('quote'):
            lines.append('  '*(level+1)+'依据：'+node['quote'].replace('\n', ' '))
        for child in children.get(key, []):
            visit(child['id'], level+1)
    visit(graph.get('root'))
    lines += ['', '## 原文关系', '']
    for relation in graph.get('relations', []):
        lines += ['- '+nodes[relation['src']]['label']+' → '+relation['label']+' → '+nodes[relation['dst']]['label'],
                  '  依据：'+relation.get('quote', '').replace('\n', ' ')]
    return '\n'.join(lines)+'\n'
