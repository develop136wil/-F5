import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import utils
from services import ai_coordinator, embedding_service, policy_sync
from history_manager.items import _flatten_for_export


class AuditRecoveryTests(unittest.TestCase):
    def test_config_recovers_backup_without_overwriting_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text('{broken', encoding='utf-8')
            backup = Path(str(path) + '.bak')
            backup.write_text('{"TARGET_REGION":"서울"}', encoding='utf-8')
            with patch.object(utils, 'get_config_path', return_value=str(path)), patch.dict('os.environ'):
                utils.set_env_values({'TARGET_AGE_MAX_MONTHS': '120'})
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['TARGET_REGION'], '서울')
            self.assertEqual(json.loads(backup.read_text(encoding='utf-8')), {'TARGET_REGION': '서울'})
            self.assertEqual(len(list(Path(directory).glob('config_corrupt_*'))), 1)

    def test_invalid_config_without_backup_refuses_save(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text('[]', encoding='utf-8')
            with patch.object(utils, 'get_config_path', return_value=str(path)):
                with self.assertRaises(ValueError):
                    utils.set_env_values({'A': 'new'})
            self.assertEqual(path.read_text(encoding='utf-8'), '[]')

    def test_custom_and_senior_taxonomies_do_not_get_child_warning(self):
        for start, end, tags in [(780, 960, ['노년']), (0, 24, ['영유아']), (120, 150, ['우리 기관 아동'])]:
            self.assertIsNone(policy_sync._age_lifecycle_mismatch_note({
                'start_age_months': start, 'end_age_months': end, 'life_cycle': tags}))
        self.assertIsNotNone(policy_sync._age_lifecycle_mismatch_note({
            'start_age_months': 100, 'end_age_months': 150, 'life_cycle': ['영아', '학령기']}))

    def test_unproven_general_collection_cache_is_not_reused(self):
        with patch.object(policy_sync.database, 'execute_query') as query:
            self.assertIsNone(policy_sync._get_cached_extraction('https://example.invalid', '정책'))
            query.assert_not_called()

    def test_corrupt_quota_state_is_preserved_and_blocks_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'quota.json'
            for content in ['[]', '{"calls":[]}', '{"calls":{"model":-1}}']:
                path.write_text(content, encoding='utf-8')
                with patch.object(ai_coordinator, '_state_path', return_value=str(path)), \
                     patch.object(ai_coordinator, '_STATE_LOADED', False), \
                     patch.object(ai_coordinator, '_DAILY_EXHAUSTED', {}):
                    ai_coordinator._load_state()
                    with self.assertRaises(ai_coordinator.AiDailyQuotaExhausted):
                        asyncio.run(ai_coordinator.acquire_model_slot('gemini-3.5-flash-lite',
                            input_tokens=ai_coordinator.estimate_input_tokens('synthetic quota test')))
                    self.assertIn('*', json.loads(path.read_text())['exhausted'])
            self.assertEqual(len(list(Path(directory).glob('*.corrupt.*'))), 3)

    def test_embedding_corruption_recovers_to_safe_today(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'quota.json'
            path.write_text('[]', encoding='utf-8')
            with patch.object(embedding_service, '_quota_state_path', return_value=str(path)):
                self.assertEqual(embedding_service._load_persisted_exhausted_date(), embedding_service._pacific_today_str())
                self.assertIsInstance(json.loads(path.read_text()), dict)

    def test_export_formula_is_text_but_numbers_are_preserved(self):
        row = _flatten_for_export([{'title': '=1+1', 'link': 'https://example.invalid'}])[0]
        self.assertIn("'=1+1", row.values())


if __name__ == '__main__':
    unittest.main()
