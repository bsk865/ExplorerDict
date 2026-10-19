import tempfile
import unittest
from pathlib import Path
from app import reading_preferences as prefs


class ReadingPreferencesTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()

    def test_persists_outside_browser_profile(self):
        self.assertEqual(prefs.load(self.root),{'palette':'clay','mode':'light'})
        prefs.save(self.root,{'palette':'forest','mode':'dark'})
        self.assertEqual(prefs.load(Path(self.temp.name)),{'palette':'forest','mode':'dark'})

    def test_invalid_choice_keeps_existing_preference(self):
        prefs.save(self.root,{'palette':'blue','mode':'light'})
        with self.assertRaises(ValueError):prefs.save(self.root,{'palette':'unknown','mode':'dark'})
        self.assertEqual(prefs.load(self.root),{'palette':'blue','mode':'light'})

    def test_every_palette_has_light_and_dark_tokens(self):
        for palette in prefs.PALETTES:
            for mode in ('light','dark'):
                colors=prefs.colors(palette,mode)
                for key in ('bg','surface','ink','muted','line','accent','accent-soft','strong'):
                    self.assertRegex(colors[key],r'^#[0-9a-fA-F]{6}$')
                self.assertNotEqual(colors['bg'],colors['ink'])
            self.assertNotEqual(prefs.colors(palette,'light')['bg'],prefs.colors(palette,'dark')['bg'])
