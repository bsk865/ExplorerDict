import io
import ssl
import tempfile
import unittest
from collections import Counter
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from app.discovery import entry
from app.native.service import NativeService
from tests.test_discovery import FEED, SOURCE


class DiscoveryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.native = NativeService(Path(self.temp.name))
        self.discovery = self.native.discovery
        self.preferences = self.discovery.preferences()
        self.preferences.update(sources=[dict(SOURCE)], personal=False)
        self.discovery.save_preferences(self.preferences)
        self.sleep = patch('app.discovery.time.sleep')
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        self.native.close()
        self.temp.cleanup()

    def refresh_with_failure(self, error, *, persistent=False):
        calls = Counter()
        def loader(url, *args):
            calls[url] += 1
            if url == SOURCE['url']:
                if persistent or calls[url] == 1:
                    raise error
                return FEED, url, 'utf-8'
            raise ValueError('No cover in this fixture')
        return self.discovery.refresh(loader), calls

    def test_temporary_failures_recover_once_without_duplicate_items(self):
        for error in (TimeoutError('timeout'), ConnectionResetError('reset'),
                      URLError(ssl.SSLEOFError(8, 'connection closed')),
                      HTTPError(SOURCE['url'], 503, 'unavailable', {}, io.BytesIO())):
            with self.subTest(error=type(error).__name__):
                state, calls = self.refresh_with_failure(error)
                self.assertEqual(2, calls[SOURCE['url']])
                self.assertEqual([], state['errors'])
                self.assertEqual(1, len(state['items']))
                self.assertEqual(2, state['source_health']['test']['attempts'])
                self.assertIn('重试后', state['source_health']['test']['message'])

    def test_non_temporary_failures_are_not_retried_and_cached_items_remain(self):
        self.refresh_with_failure(TimeoutError())
        before = self.discovery.state()
        long_backoff = Message()
        long_backoff['Retry-After'] = '120'
        for error in (HTTPError(SOURCE['url'], 403, 'forbidden', {}, io.BytesIO()),
                      HTTPError(SOURCE['url'], 404, 'not found', {}, io.BytesIO()),
                      HTTPError(SOURCE['url'], 429, 'limited', long_backoff, io.BytesIO()),
                      URLError(ssl.SSLCertVerificationError(1, 'invalid certificate')),
                      ValueError('not a feed')):
            with self.subTest(error=type(error).__name__):
                state, calls = self.refresh_with_failure(error, persistent=True)
                self.assertEqual(1, calls[SOURCE['url']])
                self.assertEqual(before['items'], state['items'])
                self.assertEqual(before['updated'], state['updated'])
                self.assertEqual(1, len(state['errors']))
                self.assertEqual(1, state['source_health']['test']['attempts'])
                self.assertEqual(1, state['source_health']['test']['last_count'])
        state, calls = self.refresh_with_failure(TimeoutError(), persistent=True)
        self.assertEqual(2, calls[SOURCE['url']])
        self.assertEqual(before['items'], state['items'])

    def test_short_publisher_backoff_is_respected(self):
        headers = Message()
        headers['Retry-After'] = '2'
        with patch('app.discovery.time.sleep') as sleep:
            state, calls = self.refresh_with_failure(HTTPError(SOURCE['url'], 429, 'limited', headers, io.BytesIO()))
        self.assertEqual(2, calls[SOURCE['url']])
        self.assertFalse(state['errors'])
        sleep.assert_called_once_with(2)

    def test_relative_article_links_follow_the_final_feed_address(self):
        body = b'<rss><channel><item><title>Redirected feed</title><link>article/story</link></item></channel></rss>'
        state = self.discovery.refresh(lambda url, *args: (body, 'https://feeds.example.net/public/feed.xml', 'utf-8'))
        self.assertEqual('https://feeds.example.net/public/article/story', state['items'][0]['url'])
        self.assertEqual(SOURCE['id'], state['items'][0]['source_id'])

    def test_pause_remove_or_change_during_refresh_does_not_add_stale_results(self):
        for change in ('pause', 'remove', 'url', 'kind'):
            with self.subTest(change=change):
                self.preferences['sources'] = [dict(SOURCE)]
                self.discovery.save_preferences(self.preferences)
                self.native.store.put('settings', 'discovery-state', dict(items=[], updated=0))
                calls = []
                def loader(url, *args):
                    calls.append(url)
                    preferences = self.discovery.preferences()
                    if change == 'pause':
                        preferences['sources'][0]['enabled'] = False
                    elif change == 'remove':
                        preferences['sources'] = []
                    else:
                        preferences['sources'][0][change] = 'https://example.org/new-feed' if change == 'url' else 'paper'
                    self.discovery.save_preferences(preferences)
                    return FEED, url, 'utf-8'
                state = self.discovery.refresh(loader)
                self.assertEqual([SOURCE['url']], calls)
                self.assertEqual([], state['items'])
                self.assertEqual(0, state['updated'])
                self.assertEqual(0, state['refresh_counts']['sources'])
                self.assertEqual(1, state['refresh_counts']['skipped'])
                self.assertEqual([], state['errors'])

    def test_change_while_cover_downloads_is_checked_before_saving(self):
        def loader(url, *args):
            if url == SOURCE['url']:
                return FEED, url, 'utf-8'
            preferences = self.discovery.preferences()
            preferences['sources'][0]['enabled'] = False
            self.discovery.save_preferences(preferences)
            raise ValueError('No image')
        state = self.discovery.refresh(loader)
        self.assertEqual([], state['items'])
        self.assertEqual(1, state['refresh_counts']['skipped'])

    def test_collected_link_keeps_both_origins_when_subscription_refreshes(self):
        html = b'<html><title>Robotics research</title></html>'
        def loader(url, *args):
            return (FEED if url == SOURCE['url'] else html), url, 'utf-8'
        item = self.discovery.add_link('https://example.org/story', loader)
        self.discovery.flag(item['id'], 'saved', True)
        self.discovery.refresh(loader)
        self.assertEqual(item['id'], self.discovery.ranked('article')[0]['id'])
        self.assertEqual(item['id'], self.discovery.ranked('news')[0]['id'])
        self.assertEqual({'links', 'test'}, set(origin['source_id'] for origin in self.discovery.state()['items'][0]['origins']))
        self.assertTrue(self.discovery.ranked('saved')[0]['saved'])
        # Collecting after the feed was read must retain the feed origin too.
        self.discovery.add_link('https://example.org/story', loader)
        self.assertEqual(1, len(self.discovery.state()['items']))
        self.assertEqual(item['id'], self.discovery.ranked('news', source_id='test')[0]['id'])
        self.preferences['sources'] = []
        self.discovery.save_preferences(self.preferences)
        self.assertEqual([], self.discovery.ranked('news'))
        self.assertEqual(item['id'], self.discovery.ranked('article')[0]['id'])
        self.assertEqual(item['id'], self.discovery.ranked('saved')[0]['id'])

    def test_inactive_cache_cannot_crowd_out_active_subscription(self):
        active = dict(entry('https://example.org/active-story', 'Active story', 'Example'),
                      source_id='test', platform='example.org', published='2000-01-01', imported_at=1)
        inactive = [dict(entry(f'https://example.org/inactive-{index}', 'Old subscription', 'Paused'),
                         source_id='paused', platform='example.org', published='2026-10-24', imported_at=1000)
                    for index in range(450)]
        self.native.store.put('settings', 'discovery-state', dict(items=inactive + [active], updated=0))
        state = self.discovery.refresh(lambda url: (b'<rss><channel/></rss>', url, 'utf-8'))
        self.assertEqual(400, len(state['items']))
        self.assertEqual([active['id']], [row['id'] for row in self.discovery.ranked()])

    def test_collecting_paper_preserves_abstract_and_full_document_import(self):
        self.preferences['sources'] = [dict(SOURCE, kind='paper')]
        self.discovery.save_preferences(self.preferences)
        html = b'<html><title>Paper title</title></html>'
        def loader(url, *args):
            return (FEED if url == SOURCE['url'] else html), url, 'utf-8'
        self.discovery.refresh(loader)
        item = self.discovery.add_link('https://example.org/story', loader)
        self.assertEqual('paper', item['content_kind'])
        self.assertEqual('New robot research', item['summary'])
        collected = self.discovery.ranked('article')[0]
        self.assertEqual('article', collected['kind'])
        self.assertEqual('paper', collected['content_kind'])
        abstract = self.discovery.import_item(item['id'], mode='abstract')
        self.assertEqual('abstract', abstract['source_status'])
        self.assertIn('New robot research', abstract['text'])
        document = {'asset': 'public-pdf', 'format': 'pdf', 'url': item['url'], 'bytes': 123}
        with patch('app.paper_source.read_paper', return_value=('Paper title', [dict(type='text', text='Full paper body.')],
                                                               {'pages': 1}, document)) as read:
            article = self.discovery.import_item(item['id'])
            read.assert_called_once()
        self.assertEqual('readable', article['source_status'])
        self.assertEqual(document, article['source_document'])
        self.assertEqual('Full paper body.', article['text'])


if __name__ == '__main__':
    unittest.main()
