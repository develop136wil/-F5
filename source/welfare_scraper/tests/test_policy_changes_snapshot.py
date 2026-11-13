import json
import unittest
from unittest.mock import patch

from services import policy_sync as ps


class TestChangesSnapshot(unittest.TestCase):
    def rows(self, count=125):
        return [dict(id=i, db_key='test', page_id='page', title='test', change_status='detected',
                     pending_diff=json.dumps({'fields': [
                         {'field': name} for name in ['지역', '추가 자격요건', '대상연령', '미확인']]}),
                     dead_links='[]', source_urls='[]', last_changed_at=None)
                for i in range(count)]

    def test_one_snapshot_and_safety_gates(self):
        with patch.object(ps, '_active_configured_keys', return_value=['test']), \
             patch.object(ps.database, 'execute_query', return_value=self.rows()) as query, \
             patch.object(ps.notion_service, 'get_policy_props', return_value=[
                 ('residence', '지역', 'multi_select'),
                 ('eligibility_detail', '추가 자격요건', 'rich_text')]) as props:
            result = ps.get_changes(['test'])
        self.assertEqual(len(result), 125)
        props.assert_called_once()
        self.assertTrue(query.call_args.kwargs['raise_on_error'])
        self.assertEqual([f['requires_individual_review'] for f in result[0]['fields']],
                         [True, True, True, True])

    def test_setting_change_is_visible_on_next_request(self):
        with patch.object(ps, '_active_configured_keys', return_value=['test']), \
             patch.object(ps.database, 'execute_query', return_value=self.rows(1)), \
             patch.object(ps.notion_service, 'get_policy_props', side_effect=[
                 [('category', '지역', 'select')],
                 [('category', '새 분류 이름', 'select')]]):
            self.assertFalse(ps.get_changes(['test'])[0]['fields'][0]['requires_individual_review'])
            self.assertTrue(ps.get_changes(['test'])[0]['fields'][0]['requires_individual_review'])

    def test_database_failure_is_not_an_empty_list(self):
        with patch.object(ps, '_active_configured_keys', return_value=['test']), \
             patch.object(ps.database, 'execute_query', side_effect=RuntimeError('db unavailable')):
            with self.assertRaises(RuntimeError):
                ps.get_changes(['test'])
