"""Local interest ranking, RSS/Atom adapters and explicit article collection."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, urljoin, parse_qsl, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from http.client import IncompleteRead, RemoteDisconnected
from xml.etree import ElementTree as ET
import ipaddress
import io
import json
import re
import socket
import ssl
import time
from bs4 import BeautifulSoup
from PIL import Image, ImageOps
from .discovery_sources import SOURCES, CATALOG, CATALOG_VERSION, MAX_SOURCES, platform_key, diversify

TOPICS = {
    '人工智能': ['人工智能', '机器学习', '大语言模型', '深度学习', 'artificial intelligence', 'machine learning', 'deep learning', 'llm', 'language model'],
    '机器人': ['机器人', '自动驾驶', '运动控制', 'robot', 'robotics', 'autonomous driving', 'mpc', 'model predictive control'],
    '新能源': ['新能源', '太阳能', '电池', '光伏', 'solar', 'battery', 'batteries', 'renewable', 'energy storage'],
    '计算机': ['计算机', '编程', '算法', 'computer', 'software', 'programming', 'algorithm'],
    '医学健康': ['医学', '健康', '疾病', 'medical', 'health', 'disease', 'clinical'],
    '生命科学': ['生物', '基因', '蛋白质', 'biology', 'gene', 'protein', 'genome'],
    '环境气候': ['气候', '环境', '生态', 'climate', 'ecology', 'environment'],
    '物理天文': ['物理', '量子', '天文', 'physics', 'quantum', 'astronomy', 'space'],
    '数学': ['数学', '积分', '概率', 'mathematics', 'integral', 'probability'],
}


def clean(value, limit=1000):
    return re.sub(r'\s+', ' ', BeautifulSoup(value or '', 'html.parser').get_text(' ', strip=True))[:limit]


def canonical(url):
    p = urlsplit(url.strip())
    if p.scheme not in {'https', 'http'} or not p.hostname or p.username or p.password:
        raise ValueError('请填写公开文章或订阅源的 HTTP/HTTPS 地址。')
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in {'fbclid', 'gclid'}]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or '/', urlencode(query), ''))


def public_url(url):
    url = canonical(url)
    p = urlsplit(url)
    if p.port not in (None, 80, 443):
        raise ValueError('仅支持公开网站的标准 HTTP/HTTPS 地址。')
    addresses = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError('发现页只读取公开网站，不访问本机或内网地址。')
    return url


class PublicRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, limit=2_000_000):
    url = public_url(url)
    request = Request(url, headers={'User-Agent': 'ExplorerDict/1.0 (personal reading feed)', 'Accept': '*/*'})
    with build_opener(PublicRedirect()).open(request, timeout=8) as response:
        raw = response.read(limit+1)
        if len(raw) > limit:
            raise ValueError('内容过大，请在原站阅读。')
        return raw, response.geturl(), response.headers.get_content_charset() or 'utf-8'


def subscription_retry_delay(error):
    """Retry temporary transport failures once; respect a publisher's backoff."""
    if isinstance(error, HTTPError):
        if error.code not in {408, 429, 500, 502, 503, 504}:
            return None
        value = error.headers.get('Retry-After') if error.headers else None
        if value:
            try:
                delay = float(value)
            except ValueError:
                try:
                    delay = parsedate_to_datetime(value).timestamp() - time.time()
                except (ValueError, TypeError, OverflowError):
                    delay = .5
            # Longer waits belong to a later refresh, not an immediate request.
            return max(.5, delay) if delay <= 2 else None
        return .5
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, ssl.SSLCertVerificationError):
        return None
    if isinstance(reason, ssl.SSLError):
        return .5 if isinstance(reason, (ssl.SSLEOFError, ssl.SSLZeroReturnError)) else None
    if isinstance(reason, socket.gaierror):
        return .5 if reason.errno == socket.EAI_AGAIN else None
    if isinstance(reason, (OSError, IncompleteRead, RemoteDisconnected)):
        return .5
    return None


def read_subscription(source, loader):
    for attempt in (1, 2):
        try:
            raw, resolved, _ = loader(source['url'])
            return parse_feed(raw, dict(source, url=resolved)), None, attempt
        except Exception as error:
            delay = subscription_retry_delay(error)
            if isinstance(error, HTTPError):
                error.close()
            if attempt == 2 or delay is None:
                return [], source['name'] + '：更新失败，保留上次内容', attempt
            time.sleep(delay)


def item_origins(item):
    origins = {row['source_id']: dict(row) for row in item.get('origins', []) if row.get('source_id')}
    if item.get('source_id') and item['source_id'] not in origins:
        origins[item['source_id']] = {key: item.get(key) for key in ('source_id', 'source', 'platform', 'kind')}
    return origins


def paper_content(item, sources=()):
    from .paper_source import arxiv_identifier
    origins = item_origins(item)
    return (item.get('content_kind', item.get('kind')) == 'paper'
            or any(origin.get('kind') == 'paper' for origin in origins.values())
            or any(source['kind'] == 'paper' and source['id'] in origins for source in sources)
            or bool(arxiv_identifier(item.get('url', ''))))


def stamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
    except (ValueError, AttributeError):
        try:
            return parsedate_to_datetime(value).timestamp()
        except (ValueError, TypeError, AttributeError, OverflowError):
            return 0


def entry(url, title, source, kind='news', **fields):
    url = canonical(url)
    return dict(id=sha256(url.encode()).hexdigest()[:24], url=url, title=clean(title, 250) or urlsplit(url).hostname,
                source=source, kind=kind, summary='', published='', cover_url='', **fields)


def metadata(raw, url, charset='utf-8'):
    soup = BeautifulSoup(raw.decode(charset, errors='replace'), 'html.parser')
    def meta(*names):
        for name in names:
            node = soup.find('meta', attrs={'property': name}) or soup.find('meta', attrs={'name': name})
            if node and node.get('content'):
                return node['content']
        return ''
    title = meta('og:title', 'twitter:title') or (soup.title.get_text() if soup.title else '')
    wx = urlsplit(url).hostname == 'mp.weixin.qq.com'
    if wx and soup.select_one('#activity-name'):
        title = soup.select_one('#activity-name').get_text(' ', strip=True)
    source = meta('og:site_name') or urlsplit(url).hostname
    if wx and soup.select_one('#js_name'):
        source = soup.select_one('#js_name').get_text(' ', strip=True)
    cover = meta('og:image', 'twitter:image', 'twitter:image:src')
    result = entry(url, title, clean(source, 80), 'article')
    result.update(summary=clean(meta('og:description', 'description', 'twitter:description'), 900),
                  cover_url=urljoin(url, cover) if cover else '',
                  published=meta('article:published_time'), source_id='links',
                  description_type='网页简介', imported_at=time.time())
    return result


def parse_feed(raw, source):
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('订阅源包含不支持的 XML 声明。')
    root = ET.fromstring(raw)
    local = lambda tag: tag.rsplit('}', 1)[-1]
    if local(root.tag) not in {'rss', 'feed', 'RDF'}:
        raise ValueError('这个地址不是 RSS 或 Atom 订阅源。')
    channel = next((x for x in root if local(x.tag) == 'channel'), root)
    rows = [n for n in root.iter() if local(n.tag) in {'item', 'entry'}]
    results = []
    for node in rows[:40]:
        def value(*names):
            return next((''.join(c.itertext()) for c in node if local(c.tag) in names and ''.join(c.itertext()).strip()), '')
        links = [c for c in node if local(c.tag) == 'link']
        link = next((c.get('href') or c.text for c in links if c.get('rel', 'alternate') == 'alternate'), '')
        if not link:
            continue
        try:
            item = entry(urljoin(source['url'], link.strip()), value('title'), source['name'], source['kind'])
        except ValueError:
            continue
        description = value('description', 'summary', 'encoded', 'content')
        cover = ''
        for image in node.iter():
            if local(image.tag) in {'thumbnail', 'content', 'enclosure'} and image.get('url') and (
                    local(image.tag) == 'thumbnail' or image.get('type', '').startswith('image/') or image.get('medium') == 'image'):
                cover = urljoin(source['url'], image.get('url'))
                break
        if not cover:
            image = BeautifulSoup(description, 'html.parser').find('img')
            cover = urljoin(item['url'], image.get('src', '')) if image and image.get('src') else ''
        summary = clean(description, 20000)
        if source['kind'] == 'paper':
            summary = re.sub(r'^.*?Abstract:\s*', '', summary, flags=re.I)
        limit = 6000 if source['kind'] == 'paper' else 1200
        truncated = len(summary) > limit
        summary = summary[:limit]
        authors = value('creator', 'author')
        item.update(summary=summary, cover_url=cover, published=value('pubDate', 'published', 'date', 'updated'),
                    authors=clean(authors, 250), source_id=source['id'],
                    platform=source.get('platform') or platform_key(source['url']),
                    description_type=('来源摘要节选' if truncated else '来源摘要') if source['kind'] == 'paper' else '来源简介',
                    summary_truncated=truncated,
                    publication='预印本' if urlsplit(item['url']).hostname in {'arxiv.org', 'www.arxiv.org', 'export.arxiv.org'} else '', imported_at=time.time())
        results.append(item)
    return results


def find_feeds(url, loader=fetch):
    """Find and validate publisher-provided feeds from a feed or website URL."""
    raw, final, charset = loader(canonical(url))
    def candidate(body, address, title=''):
        address=canonical(address)
        source=dict(id=sha256(address.encode()).hexdigest()[:16],url=address,name=title or urlsplit(address).hostname,
                    kind='news',enabled=True)
        parse_feed(body,source)
        root=ET.fromstring(body)
        channel=next((node for node in root if node.tag.rsplit('}',1)[-1]=='channel'),root)
        feed_title=next((''.join(node.itertext()) for node in channel if node.tag.rsplit('}',1)[-1]=='title'),'')
        return dict(source,name=clean(feed_title or title or source['name'],80))
    try:
        return [candidate(raw,final)]
    except (ValueError,ET.ParseError):
        pass
    soup=BeautifulSoup(raw.decode(charset,errors='replace'),'html.parser')
    base=soup.find('base',href=True)
    base_url=urljoin(final,base['href']) if base else final
    addresses={}
    for link in soup.find_all('link',href=True):
        if ('alternate' in link.get('rel',[]) and link.get('type','').split(';')[0].strip().lower()
                in {'application/rss+xml','application/atom+xml','application/rdf+xml'}):
            try:
                address=canonical(urljoin(base_url,link['href']))
            except ValueError:
                continue
            addresses.setdefault(address,clean(link.get('title',''),80))
            if len(addresses)==8:break
    if not addresses:
        for source in CATALOG:
            if source['platform']==platform_key(final):
                addresses.setdefault(source['url'],source['name'])
    def read(item):
        try:
            address,title=item
            body,resolved,_=loader(address)
            return candidate(body,resolved,title)
        except Exception:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        found=[row for row in pool.map(read,addresses.items()) if row]
    unique={row['url']:row for row in found}
    if not unique:
        raise ValueError('这个网页没有可用的公开订阅入口。文章链接仍可在发现页收集。')
    return list(unique.values())


class Discovery:
    def __init__(self, store):
        self.store = store
        self.cache_dir = store.directory / 'discovery-covers'

    def preferences(self):
        saved = self.store.get('settings', 'discovery-preferences', {})
        value = dict(topics=[], blocked=[], personal=True, automatic=True, sources=[dict(s) for s in SOURCES]) | saved
        value['sources'] = [dict(s) for s in value['sources']]
        if saved and saved.get('catalog_version', 1) < CATALOG_VERSION:
            # Add only newly introduced defaults once; retain old removals/toggles.
            enabled = any(s.get('enabled') for s in value['sources'])
            known = {s['id'] for s in value['sources']}
            addresses = {s['url'] for s in value['sources']}
            for row in SOURCES:
                if row['introduced'] > saved.get('catalog_version', 1) and row['id'] not in known and row['url'] not in addresses:
                    value['sources'].append(dict(row, enabled=enabled))
        catalog = {s['id']: s for s in CATALOG}
        for row in value['sources']:
            original = catalog.get(row['id'], {})
            row.setdefault('platform', original.get('platform') or platform_key(row['url']))
            row.setdefault('category', original.get('category', '自定义来源'))
        value['catalog_version'] = CATALOG_VERSION
        return value

    def save_preferences(self, value):
        value = dict(value)
        value['topics'] = list(dict.fromkeys(t.strip()[:60] for t in value.get('topics', []) if t.strip()))[:20]
        value['blocked'] = list(dict.fromkeys(t.strip()[:60] for t in value.get('blocked', []) if t.strip()))[:20]
        sources = value.get('sources', [])
        if len(sources) > MAX_SOURCES:
            raise ValueError(f'最多添加{MAX_SOURCES}个订阅源。')
        ids, urls = set(), set()
        for source in sources:
            source['url'] = canonical(source['url'])
            if not source.get('id') or not str(source.get('name', '')).strip() or source.get('kind') not in {'news', 'paper'}:
                raise ValueError('订阅源需要名称、有效标识及内容类型。')
            if source['id'] in ids or source['url'] in urls:
                raise ValueError('订阅源重复，请保留一个地址。')
            ids.add(source['id'])
            urls.add(source['url'])
            source['platform'] = platform_key(source['url']) if not any(c['id'] == source['id'] and c['url'] == source['url'] for c in CATALOG) else next(c['platform'] for c in CATALOG if c['id'] == source['id'])
        value['catalog_version'] = CATALOG_VERSION
        self.store.put('settings', 'discovery-preferences', value)

    def state(self):
        return self.store.get('settings', 'discovery-state', {'items': [], 'updated': 0, 'attempted': 0, 'errors': []})

    def interests(self):
        prefs = self.preferences()
        scores = {topic: 10 for topic in prefs['topics']}
        if prefs['personal']:
            with self.store.connect() as db:
                records = []
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in ('articles', 'objects', 'sessions', 'study_notes'):
                    if table not in tables:
                        continue
                    records.extend(json.loads(r[0]) for r in db.execute(f'SELECT payload FROM {table} ORDER BY rowid DESC LIMIT 60'))
            for index, record in enumerate(records):
                text = ' '.join(str(record.get(k, '')) for k in ('title', 'note'))
                text += ' ' + str(record.get('text', ''))[:1200] + ' ' + str(record.get('content', ''))[:1200]
                text += ' ' + ' '.join(m.get('content', '') for m in record.get('messages', [])[-8:] if m.get('role') == 'user')
                text = text.casefold()
                for topic, aliases in TOPICS.items():
                    if any(alias.casefold() in text for alias in aliases):
                        scores[topic] = scores.get(topic, 0) + 1 / (1 + index / 30)
        return sorted(((k, v) for k, v in scores.items() if k not in prefs['blocked']), key=lambda x: -x[1])[:10]

    def ranked(self, kind='all', query='', source_id=''):
        prefs = self.preferences()
        interests = self.interests()
        flags = self.store.get('settings', 'discovery-flags', {})
        enabled = {s['id'] for s in prefs['sources'] if s.get('enabled')}
        sources = {s['id']: s for s in prefs['sources']}
        rows = []
        for item in self.state()['items']:
            item = dict(item)
            item['content_kind'] = 'paper' if paper_content(item, prefs['sources']) else item['kind']
            origins = list(item_origins(item).values())
            for origin in origins:
                source = sources.get(origin['source_id'], {})
                origin['kind'] = 'article' if origin['source_id'] == 'links' else source.get('kind') or origin.get('kind') or item['kind']
                if source:
                    origin.update(source=source['name'], platform=source.get('platform') or origin.get('platform'))
            active = [o for o in origins if o.get('source_id') in enabled | {'links'}]
            if source_id:
                active = [o for o in origins if o.get('source_id') == source_id and (source_id in enabled | {'links'} or kind == 'saved')]
                if not active:
                    continue
            if kind in {'news', 'paper', 'article'}:
                active = [origin for origin in active if origin['kind'] == kind]
            if active:
                item.update(active[0])
            flag = flags.get(item['id'], {})
            text = (item['title'] + ' ' + item.get('summary', '')).casefold()
            if not active and not (kind == 'saved' and flag.get('saved')):
                continue
            if flag.get('hidden') or any(alias.casefold() in text for t in prefs['blocked'] for alias in TOPICS.get(t, [t])):
                continue
            if query.casefold() not in text:
                continue
            if kind == 'saved' and not flag.get('saved') or kind in {'news', 'paper', 'article'} and item['kind'] != kind:
                continue
            matched = [(topic, weight) for topic, weight in interests if any(alias.casefold() in text for alias in TOPICS.get(topic, [topic]))]
            age = max(0, time.time() - stamp(item.get('published', ''))) / 86400
            score = sum(weight for _, weight in matched) + 2 / (1 + age / 7)
            item['platform'] = item.get('platform') or sources.get(item.get('source_id'), {}).get('platform') or platform_key(item['url'])
            rows.append(dict(item, **flag, matched_topics=[topic for topic, _ in matched], reason=('与你关注的“'+matched[0][0]+'”相关') if matched else '来自你的订阅与收集', score=score))
        ordered = sorted(rows, key=lambda row: (row['score'], stamp(row.get('published', '')), row.get('imported_at', 0)), reverse=True)
        return diversify(ordered) if kind not in {'saved', 'article'} and not source_id else ordered

    def flag(self, key, name, value):
        with self.store.lock:
            flags = self.store.get('settings', 'discovery-flags', {})
            flags.setdefault(key, {})[name] = value
            self.store.put('settings', 'discovery-flags', flags)

    def cover(self, item, loader=fetch, page_fallback=False):
        result = dict(item)
        if result.get('cover_file') and (self.cache_dir/result['cover_file']).is_file():
            return result
        try:
            if not result.get('cover_url') and page_fallback and result['kind'] != 'paper':
                raw, url, charset = loader(result['url'])
                result['cover_url'] = metadata(raw, url, charset)['cover_url']
            if not result.get('cover_url'):
                return result
            raw, _, _ = loader(result['cover_url'], 4_000_000)
            with Image.open(io.BytesIO(raw)) as im:
                if im.width * im.height > 20_000_000:
                    return result
                im = ImageOps.exif_transpose(im).convert('RGB')
                im.thumbnail((800, 600))
                self.cache_dir.mkdir(exist_ok=True)
                name = sha256(result['cover_url'].encode()).hexdigest()+'.jpg'
                im.save(self.cache_dir/name, 'JPEG', quality=85)
                result['cover_file'] = name
        except Exception:
            pass
        return result

    def refresh(self, loader=fetch):
        sources = [s for s in self.preferences()['sources'] if s.get('enabled')]
        with ThreadPoolExecutor(max_workers=4) as pool:
            requested = list(zip(sources, pool.map(lambda source: read_subscription(source, loader), sources)))
        def current_batches():
            current = {source['id']: source for source in self.preferences()['sources'] if source.get('enabled')}
            return [(current[source['id']], *result) for source, result in requested
                    if source['id'] in current and all(source.get(key) == current[source['id']].get(key)
                                                      for key in ('url', 'kind'))]
        batches = current_batches()
        # Covers are bounded and cached. No image generation or third-party proxy.
        with self.store.lock:
            existing = {item['id']: item for item in self.state()['items']}
        candidates = []
        cover_pool = [dict(row, score=stamp(row.get('published', ''))) for _, rows, _, _ in batches for row in rows[:3]]
        for item in diversify(sorted(cover_pool, key=lambda row: row['score'], reverse=True))[:12]:
            candidates.append(dict(item, cover_file=existing.get(item['id'], {}).get('cover_file', '')))
        with ThreadPoolExecutor(max_workers=4) as pool:
            covered = list(pool.map(lambda row: self.cover(row, loader, True), candidates))
        covers = {row['id']: {key: row[key] for key in ('cover_url', 'cover_file') if row.get(key)} for row in covered}
        with self.store.lock:
            # A source can be paused, removed or changed while requests/covers are running.
            batches = current_batches()
            fresh = [dict(item, source=source['name'], platform=source['platform'])
                     for source, items, _, _ in batches for item in items]
            state = self.state()
            merged = {item['id']: item for item in state['items']}
            for item in fresh:
                old = merged.get(item['id'], {})
                origins = item_origins(old)
                origins.update(item_origins(item))
                merged[item['id']] = dict(old, **dict(item, **covers.get(item['id'], {})), origins=list(origins.values()))
            flags = self.store.get('settings', 'discovery-flags', {})
            ordered = sorted(merged.values(), key=lambda row: row.get('imported_at', 0), reverse=True)
            protected = [row for row in ordered if flags.get(row['id'], {}).get('saved') or 'links' in item_origins(row)]
            kept_ids = {row['id'] for row in protected}
            pool = [dict(row, platform=row.get('platform') or platform_key(row['url']), score=stamp(row.get('published', ''))) for row in ordered if row['id'] not in kept_ids]
            enabled = {source['id'] for source in self.preferences()['sources'] if source.get('enabled')}
            active = [row for row in pool if enabled.intersection(item_origins(row))]
            dormant = [row for row in pool if not enabled.intersection(item_origins(row))]
            kept = protected + (diversify(active) + diversify(dormant))[:400]
            state.update(items=kept, attempted=time.time(), errors=[error for _, _, error, _ in batches if error],
                         refresh_counts={'sources': len(batches), 'failed': sum(bool(error) for _, _, error, _ in batches),
                                         'empty': sum(not items and not error for _, items, error, _ in batches),
                                         'articles': len(fresh)})
            if len(batches) != len(requested):
                state['refresh_counts']['skipped'] = len(requested) - len(batches)
            configured = {source['id'] for source in self.preferences()['sources']}
            health = {key: value for key, value in state.get('source_health', {}).items() if key in configured}
            for source, items, error, attempts in batches:
                old = health.get(source['id'], {})
                health[source['id']] = dict(attempted=time.time(), count=len(items), error=error or '',
                                             attempts=attempts,
                                             last_count=old.get('last_count', old.get('count', 0)) if error else len(items),
                                             updated=old.get('updated', 0) if error else time.time(),
                                             message='本期暂无新文章，已保留上次内容' if not error and not items
                                             else '重试后已更新' if not error and attempts > 1 else '')
            state['source_health'] = health
            if any(not error for _, _, error, _ in batches):
                state['updated'] = time.time()
            self.store.put('settings', 'discovery-state', state)
        keep_files = {row.get('cover_file') for row in kept}
        if self.cache_dir.exists():
            for path in self.cache_dir.glob('*.jpg'):
                if path.name not in keep_files:
                    path.unlink(missing_ok=True)
        return state

    def add_link(self, url, loader=fetch):
        raw, final, charset = loader(canonical(url))
        item = metadata(raw, final, charset)
        if not item['title'] or ('微信' in item['title'] and not item['summary'] and not item['cover_url']):
            raise ValueError('页面暂未提供文章信息，请确认链接可公开访问，或在原站阅读。')
        item = self.cover(item, loader)
        with self.store.lock:
            state = self.state()
            old = next((row for row in state['items'] if row['id'] == item['id']), {})
            if paper_content(old, self.preferences()['sources']) or paper_content(item):
                item['content_kind'] = 'paper'
                if len(old.get('summary', '')) > len(item.get('summary', '')):
                    for field in ('summary', 'summary_truncated', 'description_type'):
                        if field in old:
                            item[field] = old[field]
            origins = item_origins(old)
            origins.update(item_origins(item))
            item = dict(old, **item, origins=list(origins.values()))
            state['items'] = [item] + [row for row in state['items'] if row['id'] != item['id']]
            self.store.put('settings', 'discovery-state', state)
        return item

    def cache_covers(self, keys, loader=fetch):
        items = [row for row in self.state()['items'] if row['id'] in keys]
        with ThreadPoolExecutor(max_workers=4) as pool:
            covered = list(pool.map(lambda row: self.cover(row, loader), items))
        files = {row['id']: row['cover_file'] for row in covered if row.get('cover_file')}
        with self.store.lock:
            state = self.state()
            for item in state['items']:
                if item['id'] in files:
                    item['cover_file'] = files[item['id']]
            self.store.put('settings', 'discovery-state', state)
        return len(files)

    def import_item(self, key, loader=fetch, *, mode='full'):
        item = next((row for row in self.state()['items'] if row['id'] == key), None)
        if not item:
            raise ValueError('该内容已不在发现列表中，请重新添加链接。')
        paper = paper_content(item, self.preferences()['sources'])
        if mode not in {'full', 'abstract'} or mode == 'abstract' and not paper:
            raise ValueError('请选择全文导入，或为论文明确选择保存摘要。')
        document = None
        if paper and mode == 'abstract':
            if not item.get('summary'):
                raise ValueError('来源没有提供摘要，请在原站下载论文后导入。')
            blocks = [{'type': 'text', 'text': ('论文摘要节选（非全文）' if item.get('summary_truncated') else '论文摘要（非全文）')+'\n'+item['summary']}]
            title, stats, status = item['title'], {}, 'abstract'
        elif paper:
            from .paper_source import read_paper
            title, blocks, stats, document = read_paper(item, self.store, loader)
            status = 'readable'
        else:
            from .reader_source import readable_blocks
            raw, final, charset = loader(item['url'], 5_000_000)
            title, blocks, stats = readable_blocks(raw.decode(charset, errors='replace'), final, self.store,
                                                  lambda address:loader(address,24_000_000)[0])
            status = 'readable'
        article = self.store.import_article(title, blocks, origin='web')
        article.update(source_url=item['url'], source_status=status, source_stats=stats)
        if document:
            article.update(source_document=document)
        self.store.update_article(article)
        self.flag(key, 'abstract_article_id' if status == 'abstract' else 'full_article_id', article['id'])
        flags = self.store.get('settings', 'discovery-flags', {}).get(key, {})
        full_id = flags.get('full_article_id')
        self.flag(key, 'article_id', full_id if full_id and self.store.get('articles', full_id) else article['id'])
        return article
