"""Publisher identities and a selectable catalogue of original public feeds."""
from collections import defaultdict
from urllib.parse import urlsplit

CATALOG_VERSION = 2
MAX_SOURCES = 30


def source(key, name, url, platform, category, kind='news', enabled=True, introduced=1):
    return dict(id=key, name=name, url=url, platform=platform, category=category,
                kind=kind, enabled=enabled, introduced=introduced)


CATALOG = [
    source('ithome', 'IT之家', 'https://www.ithome.com/rss/', 'ithome.com', '中文科技'),
    source('bbc-tech', 'BBC 科技', 'https://feeds.bbci.co.uk/news/technology/rss.xml', 'bbc.co.uk', '国际新闻'),
    source('science', 'ScienceDaily 科学', 'https://www.sciencedaily.com/rss/top/science.xml', 'sciencedaily.com', '科学传播'),
    source('arxiv-ai', 'arXiv · 人工智能', 'https://rss.arxiv.org/rss/cs.AI', 'arxiv.org', '开放论文', 'paper'),
    source('arxiv-robot', 'arXiv · 机器人', 'https://rss.arxiv.org/rss/cs.RO', 'arxiv.org', '开放论文', 'paper'),
    source('guardian-tech', 'The Guardian 科技', 'https://www.theguardian.com/uk/technology/rss', 'theguardian.com', '国际新闻', introduced=2),
    source('npr-tech', 'NPR 科技', 'https://feeds.npr.org/1019/rss.xml', 'npr.org', '国际新闻', introduced=2),
    source('solidot', 'Solidot', 'https://www.solidot.org/index.rss', 'solidot.org', '中文社区', introduced=2),
    source('plos', 'PLOS ONE', 'https://journals.plos.org/plosone/feed/atom', 'plos.org', '开放论文', 'paper', introduced=2),
    source('conversation', 'The Conversation 科技', 'https://theconversation.com/uk/technology/articles.atom', 'theconversation.com', '学者观点', introduced=2),
    source('sspai', '少数派', 'https://sspai.com/feed', 'sspai.com', '中文社区', enabled=False, introduced=2),
    source('nature', 'Nature 科研动态', 'https://www.nature.com/nature.rss', 'nature.com', '科学传播', enabled=False, introduced=2),
    source('elife', 'eLife 研究动态', 'https://elifesciences.org/rss/recent.xml', 'elifesciences.org', '生命科学', enabled=False, introduced=2),
    source('hackernews', 'Hacker News 社区', 'https://hnrss.org/frontpage', 'ycombinator.com', '技术社区', enabled=False, introduced=2),
]
SOURCES = [dict(row) for row in CATALOG if row['enabled']]


def platform_key(url):
    host = (urlsplit(url).hostname or '').lower()
    # Group regional feeds and subdomains of the same publisher together.
    for domain in {row['platform'] for row in CATALOG} | {'mp.weixin.qq.com'}:
        if host == domain or host.endswith('.' + domain):
            return domain
    parts = host.split('.')
    length = 3 if len(parts) > 2 and '.'.join(parts[-2:]) in {'co.uk', 'com.cn', 'com.au', 'org.cn'} else 2
    return '.'.join(parts[-length:])


def diversify(rows):
    """One item per publisher per round, with occasional off-interest material.

    A publisher's multiple feeds share one queue. No source or topic is invented
    when only one is available, and an explicit source filter remains possible.
    """
    groups = defaultdict(list)
    for row in rows:
        groups[row['platform']].append(row)
    result = []
    while groups:
        order = sorted(groups, key=lambda key: groups[key][0]['score'], reverse=True)
        if result and len(order) > 1 and result[-1]['platform'] == order[0]:
            order[0], order[1] = order[1], order[0]
        for key in order:
            queue = groups[key]
            index = 0
            if len(result) % 5 == 4:
                index = next((i for i, row in enumerate(queue) if not row.get('matched_topics')), 0)
            row = dict(queue.pop(index))
            if index:
                row['reason'] = '探索其他方向 · ' + row['source']
            result.append(row)
            if not queue:
                del groups[key]
    return result
