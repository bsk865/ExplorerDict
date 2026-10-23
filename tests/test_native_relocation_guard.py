import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.native.relocation_check import prepare_environment


class RelocationGuardTests(unittest.TestCase):
    def test_unmarked_directory_is_rejected_without_changing_environment(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                prepare_environment(directory, 'schedule')
            self.assertNotIn('EXPLORER_DICT_STORAGE_CONFIG', os.environ)

    def test_external_pending_location_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory).resolve()
            (root / '.native-relocation-check').touch()
            (root / 'fixture.json').write_text(json.dumps({
                'source': str(root / 'original'), 'target': str(root / 'destination')}))
            (root / 'storage-location.json').write_text(json.dumps({
                'active': str(root / 'original'), 'pending': str(root.parent / 'outside')}))
            with self.assertRaises(ValueError):
                prepare_environment(root, 'schedule')
            self.assertNotIn('EXPLORER_DICT_STORAGE_CONFIG', os.environ)

    def test_reopen_uses_saved_destination_without_data_override(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'EXPLORER_DICT_DATA_DIR': 'old'}, clear=True):
            root = Path(directory).resolve()
            (root / '.native-relocation-check').touch()
            (root / 'fixture.json').write_text(json.dumps({
                'source': str(root / 'original'), 'target': str(root / 'destination')}))
            (root / 'storage-location.json').write_text(json.dumps({'active': str(root / 'destination')}))
            prepare_environment(root, 'reopen')
            self.assertNotIn('EXPLORER_DICT_DATA_DIR', os.environ)
            self.assertEqual(str(root / 'storage-location.json'), os.environ['EXPLORER_DICT_STORAGE_CONFIG'])


if __name__ == '__main__':
    unittest.main()
