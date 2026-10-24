import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app.discovery import entry
from app.native.service import NativeService
from app.native.discover import refresh_message
from tests.test_discovery import FEED, SOURCE


class DiscoveryUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.native = NativeService(Path(self.temp.name))
        self.discovery = self.native.discovery
        preferences = self.discovery.preferences()
        preferences.update(sources=[dict(SOURCE)], personal=False, topics=['机器人'])
        self.discovery.save_preferences(preferences)
        self.covers = patch.object(self.discovery, 'cover', side_effect=lambda item, *args: item)
        self.covers.start()

    def tearDown(self):
        self.covers.stop()
        self.native.close()
        self.temp.cleanup()

    def refresh(self, body=FEED):
        return self.discovery.refresh(lambda url, *args: (body, url, 'utf-8'))

    def test_same_issue_is_not_reported_as_new_and_first_seen_stays_stable(self):
        first = self.refresh()
        self.assertEqual(1, first['refresh_counts']['added'])
        second = self.refresh()
        self.assertEqual(dict(sources=1, failed=0, empty=0, articles=1, added=0, changed=0, unchanged=1), second['refresh_counts'])
        self.assertEqual({'added': [], 'changed': []}, second['refresh_changes'])
        self.assertEqual(first['items'][0]['first_seen'], second['items'][0]['first_seen'])
        self.assertIn('暂无新增或修改', refresh_message(second))
        self.assertEqual('暂无新增或修改', refresh_message(second))
        self.assertEqual(0, second['source_health']['test']['added'])

    def test_added_and_edited_items_are_distinct_and_do_not_change_saved_flags(self):
        first = self.refresh()
        old_id = first['items'][0]['id']
        self.discovery.flag(old_id, 'saved', True)
        edited = FEED.replace(b'New robot research', b'Updated robot research')
        extra = b'<item><title>New article</title><link>https://example.org/new</link></item>'
        body = edited.replace(b'</channel>', extra + b'</channel>')
        state = self.refresh(body)
        self.assertEqual((1, 1, 0), tuple(state['refresh_counts'][k] for k in ('added', 'changed', 'unchanged')))
        self.assertEqual([old_id], state['refresh_changes']['changed'])
        self.assertEqual(1, len(state['refresh_changes']['added']))
        self.assertTrue(self.discovery.ranked('saved')[0]['saved'])
        self.assertIn('Updated robot research', self.discovery.ranked('saved')[0]['summary'])
        self.assertEqual(1, state['source_health']['test']['added'])
        self.assertEqual(1, state['source_health']['test']['changed'])
        self.assertIn('新增 1 条 · 修改 1 条', refresh_message(state))

    def test_same_article_from_multiple_feeds_counts_once(self):
        preferences = self.discovery.preferences()
        preferences['sources'].append(dict(SOURCE, id='second', url='https://example.net/feed'))
        self.discovery.save_preferences(preferences)
        first = self.refresh()
        self.assertEqual(2, first['refresh_counts']['articles'])
        self.assertEqual(1, first['refresh_counts']['added'])
        self.assertEqual(1, len(first['refresh_changes']['added']))
        second = self.refresh()
        self.assertEqual(1, second['refresh_counts']['unchanged'])
        self.assertEqual(0, second['refresh_counts']['added'])

    def test_latest_puts_recent_articles_first_while_still_mixing_publishers(self):
        preferences = self.discovery.preferences()
        preferences['sources'].append(dict(SOURCE, id='other', name='Other', url='https://example.net/feed'))
        self.discovery.save_preferences(preferences)
        rows = []
        for i, (source, title, date) in enumerate([
            ('test', 'robot robotics', '2026-10-01T10:00:00Z'),
            ('test', 'Recent news', '2026-10-25T10:00:00Z'),
            ('test', 'Another recent story', '2026-10-25T09:00:00Z'),
            ('other', 'Different publisher', '2026-10-24T10:00:00Z'),
        ]):
            row = entry('https://example.org/' + str(i), title, source)
            row.update(source_id=source, published=date)
            rows.append(row)
        self.native.store.put('settings', 'discovery-state', dict(items=rows, updated=0))
        self.assertEqual('robot robotics', self.discovery.ranked()[0]['title'])
        latest = self.discovery.ranked(order='latest')
        self.assertEqual(['Recent news', 'Different publisher', 'Another recent story', 'robot robotics'],
                         [row['title'] for row in latest])
        self.assertEqual(['Recent news', 'Another recent story', 'robot robotics'],
                         [row['title'] for row in self.discovery.ranked(source_id='test', order='latest')])


if __name__ == '__main__':
    unittest.main()
