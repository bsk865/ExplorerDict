import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from app.discovery import Discovery, parse_feed, metadata, canonical, public_url, stamp, find_feeds
from app.native.service import NativeService

FEED = b'''<rss xmlns:media="http://search.yahoo.com/mrss/"><channel><item><title>Robotics research</title>
<link>https://example.org/story?utm_source=feed</link><description>&lt;p&gt;New robot research&lt;/p&gt;</description>
<pubDate>Fri, 23 Oct 2026 10:00:00 GMT</pubDate><media:thumbnail url="https://example.org/cover.png"/></item></channel></rss>'''
SOURCE = {'id':'test', 'name':'Example', 'kind':'news', 'url':'https://example.org/feed', 'enabled':True}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.native = NativeService(Path(self.temp.name))
        self.service = self.native.discovery
        prefs = self.service.preferences()
        prefs.update(sources=[SOURCE], topics=['机器人'])
        self.service.save_preferences(prefs)
        out = io.BytesIO()
        Image.new('RGB', (300, 200), 'red').save(out, 'PNG')
        self.image = out.getvalue()

    def tearDown(self):
        self.native.close()
        self.temp.cleanup()

    def loader(self, url, limit=2_000_000):
        return (self.image if 'cover.png' in url else FEED), url, 'utf-8'

    def test_feed_dedup_original_cover_cache_offline_and_preferences(self):
        first = self.service.refresh(self.loader)
        second = self.service.refresh(self.loader)
        self.assertEqual(1, len(second['items']))
        row = self.service.ranked()[0]
        self.assertEqual('https://example.org/story', row['url'])
        self.assertIn('机器人', row['reason'])
        self.assertTrue((self.service.cache_dir/row['cover_file']).is_file())
        self.service.flag(row['id'], 'saved', True)
        def offline(*args):
            raise OSError('offline')
        state = self.service.refresh(offline)
        self.assertEqual(first['items'][0]['id'], state['items'][0]['id'])
        self.assertEqual(1, len(state['errors']))
        self.assertEqual(1, len(Discovery(self.native.store).ranked('saved')))
        self.service.flag(row['id'], 'hidden', True)
        self.assertEqual([], self.service.ranked())

    def test_atom_paper_and_explicit_abstract_import(self):
        atom = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Robot learning</title>
        <link href="https://arxiv.org/abs/2610.12345"/><summary>Experiments with robots.</summary>
        <author><name>A Researcher</name></author><published>2026-10-23T10:00:00Z</published></entry></feed>'''
        rows = parse_feed(atom, dict(SOURCE, kind='paper'))
        self.native.store.put('settings', 'discovery-state', {'items':rows, 'updated':0})
        self.assertEqual([], self.native.store.summaries())
        article = self.service.import_item(rows[0]['id'], lambda *_: self.fail('abstract import should not fetch'), mode='abstract')
        self.assertEqual('abstract', article['source_status'])
        self.assertIn('非全文', article['text'])
        self.assertEqual('预印本', rows[0]['publication'])

    def test_valid_empty_issue_is_a_success_and_keeps_previous_articles(self):
        previous=self.service.refresh(self.loader)
        empty=b'<rss><channel><title>No scheduled issue today</title></channel></rss>'
        current=self.service.refresh(lambda url,*args:(empty,url,'utf-8'))
        self.assertEqual(previous['items'],current['items'])
        self.assertEqual([],current['errors'])
        health=current['source_health']['test']
        self.assertEqual('',health['error'])
        self.assertEqual(0,health['count'])
        self.assertGreater(health['updated'],0)
        self.assertIn('暂无新文章',health['message'])
        broken=self.service.refresh(lambda url,*args:(b'<html>not a feed</html>',url,'utf-8'))
        self.assertTrue(broken['errors'])
        self.assertEqual(health['updated'],broken['source_health']['test']['updated'])

    def test_refresh_reports_each_source_outcome_without_confusing_empty_with_failure(self):
        from app.native.discover import refresh_message
        preferences = self.service.preferences()
        preferences['sources'] = [SOURCE, dict(SOURCE, id='empty', url='https://example.net/empty'),
                                  dict(SOURCE, id='offline', url='https://example.com/offline')]
        self.service.save_preferences(preferences)
        def loader(url, limit=2_000_000):
            if 'offline' in url:
                raise OSError('offline')
            if 'empty' in url:
                return b'<rss><channel/></rss>', url, 'utf-8'
            return self.loader(url, limit)
        state = self.service.refresh(loader)
        self.assertEqual(dict(sources=3, failed=1, empty=1, articles=1, added=1, changed=0, unchanged=0), state['refresh_counts'])
        message = refresh_message(state)
        self.assertIn('已检查 2 个来源', message)
        self.assertIn('新增 1 条', message)
        self.assertIn('1 个来源本期暂无新文章', message)
        self.assertIn('1 个来源更新失败', message)
        self.assertEqual('', state['source_health']['empty']['error'])
        self.assertTrue(state['source_health']['offline']['error'])

    def test_website_subscription_uses_published_links_and_validates_each_feed(self):
        html=b'''<html><head><base href="https://example.org/public/">
        <link rel="alternate" type="application/rss+xml" href="../feed" title="News">
        <link rel="alternate" type="application/atom+xml" href="/invalid">
        <link rel="alternate" type="application/rss+xml" href="javascript:ignored">
        <link rel="alternate" type="application/rss+xml" href="../feed"></head></html>'''
        calls=[]
        def loader(url,*args):
            calls.append(url)
            return (FEED if url.endswith('/feed') else html),url,'utf-8'
        feeds=find_feeds('https://example.org',loader)
        self.assertEqual(1,len(feeds))
        self.assertEqual('https://example.org/feed',feeds[0]['url'])
        self.assertEqual('News',feeds[0]['name'])
        self.assertEqual(1,calls.count('https://example.org/feed'))
        direct=find_feeds('https://example.org/feed',loader)
        self.assertEqual('https://example.org/feed',direct[0]['url'])
        with self.assertRaisesRegex(ValueError,'没有可用的公开订阅入口'):
            find_feeds('https://example.org/page',lambda url,*args:(b'<html><p>Article</p></html>',url,'utf-8'))
        known=find_feeds('https://sspai.com',lambda url,*args:(FEED if url.endswith('/feed')
                        else b'<html><title>Website</title></html>',url,'utf-8'))
        self.assertEqual('https://sspai.com/feed',known[0]['url'])

    def test_lazy_cover_and_removed_source_preserve_bookmarks(self):
        rows = parse_feed(FEED, SOURCE)
        self.native.store.put('settings', 'discovery-state', {'items':rows, 'updated':0})
        self.assertEqual(1, self.service.cache_covers([rows[0]['id']], self.loader))
        item = self.service.ranked()[0]
        self.assertTrue((self.service.cache_dir/item['cover_file']).is_file())
        self.service.flag(item['id'], 'saved', True)
        prefs = self.service.preferences()
        prefs['sources'] = []
        self.service.save_preferences(prefs)
        self.assertEqual([], self.service.ranked())
        self.assertEqual(item['id'], self.service.ranked('saved')[0]['id'])

    def test_wechat_metadata_and_readable_body(self):
        html = '''<html><meta property="og:image" content="https://example.org/cover.png">
        <meta name="description" content="公开文章简介"><h1 id="activity-name">机器人研究</h1>
        <span id="js_name">研究手记</span><div id="js_content"><p>这是一段公开文章正文。</p></div></html>'''.encode()
        url = 'https://mp.weixin.qq.com/s?__biz=abc&mid=123&idx=1&sn=signature'
        def loader(address, limit=2_000_000):
            return (self.image if address.endswith('png') else html), address, 'utf-8'
        item = self.service.add_link(url, loader)
        self.assertEqual('研究手记', item['source'])
        self.assertEqual('机器人研究', item['title'])
        self.assertIn('sn=signature', item['url'])
        self.assertEqual([], self.native.store.summaries())
        article = self.service.import_item(item['id'], loader)
        self.assertIn('公开文章正文', article['text'])

    def test_bad_cover_and_restricted_page_remain_text_only(self):
        html = b'<html><title>Example</title><meta property="og:image" content="https://example.org/noimage"></html>'
        item = self.service.add_link('https://example.org/page', lambda url, *args: (html, url, 'utf-8'))
        self.assertFalse(item.get('cover_file'))
        with self.assertRaises(ValueError):
            self.service.import_item(item['id'], lambda url,*args:(html,url,'utf-8'))
        self.assertEqual([], self.native.store.summaries())

    def test_private_urls_entities_and_local_interest_opt_out(self):
        with self.assertRaises(ValueError):
            canonical('file:///private.txt')
        with patch('app.discovery.socket.getaddrinfo', return_value=[(2,1,6,'',('127.0.0.1',80))]):
            with self.assertRaises(ValueError):
                public_url('http://localhost/private')
        with self.assertRaises(ValueError):
            parse_feed(b'<!DOCTYPE rss [<!ENTITY e "value">]><rss/>', SOURCE)
        self.native.import_text('医学健康研究', '私人学习资料')
        self.assertIn('医学健康', dict(self.service.interests()))
        prefs = self.service.preferences()
        prefs.update(personal=False, topics=[])
        self.service.save_preferences(prefs)
        self.assertEqual([], self.service.interests())
        self.assertEqual(stamp('2026-10-23T10:00:00+08:00'), stamp('2026-10-23T02:00:00Z'))


if __name__ == '__main__':
    unittest.main()
