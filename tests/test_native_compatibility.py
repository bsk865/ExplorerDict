import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.native.compatibility import check_copy
from app.native.service import NativeService


class NativeCompatibilityTests(unittest.TestCase):
    def test_unmarked_directory_is_rejected_before_opening_service(self):
        with tempfile.TemporaryDirectory() as directory, patch('app.native.compatibility.NativeService') as service:
            with self.assertRaises(ValueError):
                check_copy(Path(directory))
            service.assert_not_called()
            self.assertEqual([], list(Path(directory).iterdir()))

    def test_marked_copy_loads_data_without_exporting_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = NativeService(root)
            article = service.import_text('Private test title', 'Private test content')
            service.annotations.save({'article_id': article['id'], 'version': article['version'],
                'style': 'yellow', 'segments': [{'block': 'b0', 'start': 0, 'end': 7, 'quote': 'Private'}]})
            service.close()
            (root / '.native-compatibility-copy').touch()
            self.assertEqual(0, check_copy(root))
            text = (root / 'compatibility-runtime.json').read_text(encoding='utf-8')
            report = json.loads(text)
            self.assertEqual(1, report['articles_loaded'])
            self.assertEqual(1, report['marks_loaded'])
            self.assertFalse(report['webengine_loaded'])
            self.assertNotIn('Private', text)
            self.assertEqual([], list(root.glob('*.png')))


if __name__ == '__main__':
    unittest.main()
