import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtWidgets import QApplication, QWidget
from app.native.service import NativeService
from app.native.settings import SettingsDialog
from app import conversation_budget


class BudgetControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.host = QWidget()
        self.host.service = NativeService(Path(self.temp.name))
        self.store = self.host.service.store
        self.store.save_config({'base_url':'https://example.invalid/v1', 'model':'test-model', 'key':'fixture'})
        with patch('app.native.settings.storage_location.read', return_value={}):
            self.dialog = SettingsDialog(self.host)

    def tearDown(self):
        self.dialog.close()
        self.host.service.close()
        self.host.close()
        self.temp.cleanup()

    def test_model_and_effort_share_saved_configuration(self):
        self.dialog.model.setCurrentText('deepseek-flash')
        self.dialog.effort.setCurrentIndex(self.dialog.effort.findData('high'))
        self.assertTrue(self.dialog.save_model())
        config = self.store.model_config()
        self.assertEqual(('deepseek-flash', 'high', 'fixture'),
                         (config['model'], config['reasoning_effort'], config['key']))
        self.assertEqual('high', self.host.service.workspace.client().reasoning_effort)

    def test_unknown_limit_default_and_explicit_save(self):
        self.assertEqual(16384, self.dialog.budget.maximum())
        self.dialog.budget.setValue(9000)
        self.assertEqual(16384, conversation_budget.policy(self.store)['budget'])
        self.dialog.save_context()
        self.assertEqual(9000, conversation_budget.policy(self.store)['budget'])
        self.dialog.default_budget.click()
        self.assertEqual(16384, self.dialog.budget.value())
        self.assertEqual(9000, conversation_budget.policy(self.store)['budget'])

    def test_model_switch_updates_official_limit_and_restores_saved_budget(self):
        self.dialog.budget.setValue(10000)
        self.dialog.save_context()
        self.dialog.base.setText('https://api.deepseek.com')
        self.dialog.model.setCurrentText('deepseek-flash')
        self.assertTrue(self.dialog.save_model())
        self.assertEqual(1000000, self.dialog.budget.maximum())
        self.assertEqual(1000000, self.dialog.budget.value())
        self.dialog.base.setText('https://example.invalid/v1')
        self.dialog.model.setCurrentText('test-model')
        self.assertTrue(self.dialog.save_model())
        self.assertEqual(16384, self.dialog.budget.maximum())
        self.assertEqual(10000, self.dialog.budget.value())

    def test_large_model_capacity_caps_budget_at_one_million(self):
        signature = conversation_budget.policy(self.store)['signature']
        self.store.put('settings', 'context_limits', {signature: 2000000})
        self.dialog.load_budget(self.host.service.workspace.config())
        self.dialog.default_budget.click()
        self.assertEqual(1000000, self.dialog.budget.value())
        self.dialog.save_context()
        self.assertEqual(1000000, conversation_budget.policy(self.store)['budget'])


if __name__ == '__main__':
    unittest.main()
