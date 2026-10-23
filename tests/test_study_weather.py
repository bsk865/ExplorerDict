import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app.study_hub import StudyHub
from app.workspace_store import WorkspaceStore
from app import study_weather


class WeatherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = WorkspaceStore(Path(self.temp.name))

    def test_unconfigured_makes_no_network_request(self):
        with patch.object(study_weather, 'fetch') as fetch:
            self.assertEqual(study_weather.current(self.store)['status'], 'unconfigured')
        fetch.assert_not_called()

    def test_cache_and_failed_refresh_are_labelled(self):
        study_weather.choose(self.store, {'name':'Shanghai','latitude':31.2,'longitude':121.5})
        with patch.object(study_weather, 'fetch', return_value={'current':{'temperature_2m':21,'weather_code':3,'time':'2026-10-20T12:00'}}) as fetch:
            self.assertEqual(study_weather.current(self.store)['status'], 'ready')
            self.assertEqual(study_weather.current(self.store)['temperature'], 21)
            self.assertEqual(fetch.call_count, 1)
        cache=self.store.get('settings','study-weather-cache');cache['fetched_at']=0
        self.store.put('settings','study-weather-cache',cache)
        with patch.object(study_weather, 'fetch', side_effect=ValueError('unavailable')):
            self.assertEqual(study_weather.current(self.store)['status'], 'stale')

    def test_city_restores_in_new_store_before_any_network_request(self):
        study_weather.choose(self.store, {'name':'Shanghai','latitude':31.2,'longitude':121.5})
        reopened = WorkspaceStore(self.store.directory)
        with patch.object(study_weather, 'fetch') as fetch:
            value = study_weather.saved(reopened)
        self.assertEqual('Shanghai', value['city']['name'])
        self.assertEqual('loading', value['status'])
        fetch.assert_not_called()
        with patch.object(study_weather, 'fetch', side_effect=ValueError('offline')):
            value = study_weather.current(reopened)
        self.assertEqual('Shanghai', value['city']['name'])
        self.assertEqual('unavailable', value['status'])

    def test_invalid_coordinates_rejected(self):
        with self.assertRaises(ValueError):
            study_weather.choose(self.store, {'name':'City','latitude':float('nan'),'longitude':0})


if __name__=='__main__':
    unittest.main()
