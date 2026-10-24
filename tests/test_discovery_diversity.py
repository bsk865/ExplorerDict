import tempfile
import unittest
from pathlib import Path
from app.discovery import Discovery, SOURCES, entry
from app.discovery_sources import CATALOG, platform_key
from app.native.service import NativeService


class DiscoveryDiversityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.native = NativeService(Path(self.temp.name))
        self.discovery = self.native.discovery

    def tearDown(self):
        self.native.close()
        self.temp.cleanup()

    def test_recommendations_mix_publishers_even_when_one_has_all_high_scores(self):
        preferences = self.discovery.preferences()
        preferences.update(topics=['机器人'], personal=False)
        self.discovery.save_preferences(preferences)
        rows = []
        for source in SOURCES:
            for index in range(12):
                item = entry(f'https://example.org/{source["id"]}/{index}',
                             'robot robotics' if source['id'].startswith('arxiv') else f'Other subject {index}',
                             source['name'], source['kind'])
                item['source_id'] = source['id']
                rows.append(item)
        self.native.store.put('settings', 'discovery-state', dict(items=rows, updated=0))
        ranked = self.discovery.ranked()
        platforms = {s['platform'] for s in SOURCES}
        self.assertEqual(platforms, {r['platform'] for r in ranked[:len(platforms)]})
        self.assertEqual(1, sum(r['platform'] == 'arxiv.org' for r in ranked[:len(platforms)]))
        self.assertTrue(all(r['source_id'] == 'arxiv-ai' for r in self.discovery.ranked(source_id='arxiv-ai')))
        self.assertTrue(any(not r['matched_topics'] for r in ranked[:5]))

    def test_legacy_upgrade_keeps_disabled_and_removed_feeds_and_only_adds_new_defaults(self):
        legacy = [dict(s) for s in SOURCES if s['introduced'] == 1 and s['id'] != 'arxiv-robot']
        legacy[0]['enabled'] = False
        self.native.store.put('settings', 'discovery-preferences', dict(sources=legacy))
        upgraded = self.discovery.preferences()
        self.assertNotIn('arxiv-robot', {s['id'] for s in upgraded['sources']})
        self.assertFalse(upgraded['sources'][0]['enabled'])
        self.assertIn('plos', {s['id'] for s in upgraded['sources']})
        self.discovery.save_preferences(upgraded)
        count = len(upgraded['sources'])
        self.assertEqual(count, len(Discovery(self.native.store).preferences()['sources']))
        upgraded['sources'] = []
        self.discovery.save_preferences(upgraded)
        self.assertEqual([], self.discovery.preferences()['sources'])

    def test_same_link_from_two_feeds_survives_one_feed_being_removed(self):
        prefs = self.discovery.preferences()
        prefs['sources'] = [dict(s) for s in SOURCES[:2]]
        self.discovery.save_preferences(prefs)
        xml = b'<rss><channel><item><title>Shared story</title><link>https://example.org/story</link></item></channel></rss>'
        self.discovery.refresh(lambda url, *args: (xml, url, 'utf-8'))
        self.assertEqual(1, len(self.discovery.state()['items']))
        self.assertEqual(2, len(self.discovery.state()['items'][0]['origins']))
        self.assertTrue(all(not s['error'] for s in self.discovery.state()['source_health'].values()))
        prefs['sources'] = prefs['sources'][:1]
        self.discovery.save_preferences(prefs)
        self.assertEqual('ithome', self.discovery.ranked()[0]['source_id'])

    def test_catalogue_covers_multiple_languages_and_paper_platforms(self):
        self.assertGreaterEqual(len({s['platform'] for s in SOURCES}), 8)
        self.assertGreaterEqual(len({s['platform'] for s in SOURCES if s['kind'] == 'paper'}), 2)
        self.assertEqual(platform_key('https://rss.arxiv.org/rss/cs.AI'), platform_key('https://arxiv.org/abs/1'))
        prefs = self.discovery.preferences()
        prefs['sources'] = [dict(s, enabled=True) for s in CATALOG]
        self.discovery.save_preferences(prefs)
        self.assertEqual(len(CATALOG), len(self.discovery.preferences()['sources']))
        prefs['sources'].append(dict(prefs['sources'][0]))
        with self.assertRaisesRegex(ValueError, '重复'):
            self.discovery.save_preferences(prefs)
