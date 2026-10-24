"""Acquire an original paper document, never silently substitute its abstract."""
import hashlib
import re
from urllib.parse import urlsplit, urljoin, urldefrag


def arxiv_identifier(url):
    parts = urlsplit(url)
    if parts.hostname not in {'arxiv.org', 'www.arxiv.org', 'export.arxiv.org'}:
        return None
    match = re.fullmatch(r'/(?:abs|pdf|html)/((?:\d{4}\.\d{4,5}|[a-zA-Z.-]+/\d{7})(?:v\d+)?)(?:\.pdf)?/?', parts.path)
    return match[1] if match else None


def pdf_links(soup, url):
    """Only follow document links explicitly published in the landing page."""
    addresses = [m.get('content', '') for m in soup.find_all('meta')
                 if str(m.get('name', '')).casefold() == 'citation_pdf_url']
    addresses += [link.get('href', '') for link in soup.find_all('link')
                  if str(link.get('type', '')).casefold() == 'application/pdf']
    result = []
    for address in addresses:
        try:
            resolved = urldefrag(urljoin(url, address.strip()))[0]
            parts = urlsplit(resolved)
        except ValueError:
            continue
        if (address.strip() and parts.scheme in {'http', 'https'} and parts.hostname
                and not parts.username and not parts.password and resolved not in result):
            result.append(resolved)
    return result[:3]


def pdf_document(item, store, raw, final):
    from .workspace_server import parse_file
    blocks = parse_file(store, 'paper.pdf', raw)
    text = [b for b in blocks if b['type'] == 'text' and b.get('text', '').strip()]
    if not text:
        raise ValueError('PDF 没有可提取正文，尚未作为全文加入。请下载原文件后通过资料导入处理。')
    asset = hashlib.sha256(raw).hexdigest()
    with store.connect() as db:
        db.execute('INSERT OR IGNORE INTO assets VALUES(?,?,?)', (asset, 'application/pdf', raw))
    stats = {'pages': max(b.get('page', 0) for b in blocks), 'text_pages': len(text),
             'images': sum(b['type'] == 'image' for b in blocks), 'bytes': len(raw)}
    return item['title'], blocks, stats, {'asset': asset, 'format': 'pdf', 'url': final, 'bytes': len(raw)}


def read_paper(item, store, loader):
    identifier = arxiv_identifier(item['url'])
    url = 'https://arxiv.org/pdf/' + identifier if identifier else item['url']
    try:
        raw, final, charset = loader(url, 28_000_000)
    except Exception as exc:
        raise ValueError('未能下载论文全文，尚未加入资料库。可稍后重试，或导入已下载的 PDF；摘要可单独保存。') from exc
    if raw.lstrip().startswith(b'%PDF-'):
        return pdf_document(item, store, raw, final)
    # Generic paper sources must expose a document body, not an abstract landing page.
    from bs4 import BeautifulSoup
    from .reader_source import readable_blocks
    html = raw.decode(charset, errors='replace')
    soup = BeautifulSoup(html, 'html.parser')
    for address in pdf_links(soup, final):
        if address in {url, final}:
            continue
        try:
            document, resolved, _ = loader(address, 28_000_000)
            if document.lstrip().startswith(b'%PDF-'):
                return pdf_document(item, store, document, resolved)
        except (OSError, ValueError, RuntimeError):
            # A failed document link does not prevent using a complete HTML body.
            continue
    article = soup.select_one('article')
    sections = article.select('section') if article else []
    if not sections or len(article.get_text(' ', strip=True)) <= len(item.get('summary', '')) + 500:
        raise ValueError('此来源只提供摘要或受限页面，尚未取得全文。请导入原始 PDF，或明确选择仅保存摘要。')
    title, blocks, stats = readable_blocks(html, final, store,lambda address:loader(address,24_000_000)[0])
    return title, blocks, stats, None
