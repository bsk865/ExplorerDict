import unittest

from app.models import ExplainResult
from app.ui.reading_panel import _local_body


class ExplanationLayoutTests(unittest.TestCase):
    def test_live_and_saved_explanations_have_the_same_sections(self):
        detail = '展开理解：\n说明机制。\n\n在本文中\n说明语境。\n\n辨析与边界:\n说明限制。'
        result = ExplainResult('核心定义。', detail, ['不需要的造句。'])
        row = {'one_line': result.one_line, 'detail': detail, 'examples': 'invalid'}
        self.assertEqual(result.as_text(), _local_body(row))
        self.assertEqual(result.as_text().count('展开理解'), 1)
        self.assertIn('辨析与边界\n说明限制。', result.as_text())
        self.assertNotIn('不需要的造句', result.as_text())
        self.assertEqual(result.examples, ['不需要的造句。'])

    def test_simple_explanation_does_not_add_empty_sections(self):
        self.assertEqual(ExplainResult('定义。').as_text(), '核心含义\n定义。')
        self.assertEqual(ExplainResult('', '').as_text(), '')

    def test_legacy_detail_is_preserved(self):
        result = ExplainResult('定义。', '原有详细说明。\n第二段。')
        self.assertIn('展开理解\n原有详细说明。\n第二段。', result.as_text())
