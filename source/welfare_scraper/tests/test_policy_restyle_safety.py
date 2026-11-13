import os
import sys
import unittest
import json
from unittest.mock import patch, AsyncMock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services import policy_sync as ps


class RestyleEligibility(unittest.TestCase):
    def field(self, **values):
        return dict(field=ps._PROP_LABEL['support_detail'], level='high', new='주차요금 50% 할인', **values)

    def test_applied_never_rewritten(self):
        self.assertFalse(ps._needs_policy_restyle(self.field(applied=True)))
        self.assertFalse(ps._needs_policy_restyle(self.field(applied=True, note='sparse')))

    def test_sparse_can_retry_but_completed_style_is_reused(self):
        self.assertTrue(ps._needs_policy_restyle(self.field(styled='주차요금 50% 감면', note='sparse')))
        self.assertFalse(ps._needs_policy_restyle(self.field(styled='주차요금 50% 감면')))

    def test_invalid_source_never_sent_to_ai(self):
        for value in [None, '', 'None', 'null', [], {}]:
            field = self.field()
            field['new'] = value
            self.assertFalse(ps._needs_policy_restyle(field))

    def test_invalid_old_style_is_retryable(self):
        for value in [None, '', 'None', [], {}]:
            self.assertTrue(ps._needs_policy_restyle(self.field(styled=value)))

    def test_tags_are_not_restyled(self):
        field = self.field(note='sparse')
        field['field'] = ps._PROP_LABEL['life_cycle']
        self.assertFalse(ps._needs_policy_restyle(field))


class RestyleResponseSafety(unittest.IsolatedAsyncioTestCase):
    async def test_quantity_changes_are_rejected_without_retry_or_overwrite(self):
        label = ps._PROP_LABEL['eligibility_detail']
        for proposed, expected in [('소득 50% 이하', 1), ('소득 80% 이하', 0), ('소득 기준 이하', 0)]:
            with self.subTest(proposed=proposed):
                row = {'id': 1, 'title': '시험용 정책', 'pending_diff': json.dumps({'fields': [
                    {'field': label, 'level': 'high', 'old': '기존 조건', 'new': '소득 50% 이하'}]})}
                previous = dict(ps.STYLE_STATUS)
                try:
                    with patch.object(ps.utils, 'get_env_value', return_value='간결하게'), \
                         patch.object(ps.ai_classifier, 'get_client', return_value=object()), \
                         patch.object(ps, '_active_configured_keys', return_value=['test']), \
                         patch.object(ps, '_db_query_strict', new=AsyncMock(return_value=[row])), \
                         patch.object(ps.ai_classifier, '_call_gemini_json', new=AsyncMock(return_value={label: proposed})) as ai, \
                         patch.object(ps, '_db_write_strict', new=AsyncMock()) as write:
                        result = await ps.restyle_pending_diffs(['test'])
                        self.assertEqual(result['styled'], expected)
                        self.assertEqual(result['rejected_fields'], 1 - expected)
                        self.assertEqual(ai.await_count, 1)
                        self.assertEqual(write.await_count, expected)
                finally:
                    ps.STYLE_STATUS.clear()
                    ps.STYLE_STATUS.update(previous)

    async def test_null_response_is_not_saved_or_counted(self):
        label = ps._PROP_LABEL['support_detail']
        row = {'id': 1, 'title': '시험용 주차 정책', 'pending_diff': json.dumps({
            'fields': [{'field': label, 'level': 'high', 'old': '50% 할인', 'new': '50% 감면'}]})}
        previous = dict(ps.STYLE_STATUS)
        try:
            with patch.object(ps.utils, 'get_env_value', return_value='간결하게'), \
                 patch.object(ps.ai_classifier, 'get_client', return_value=object()), \
                 patch.object(ps, '_active_configured_keys', return_value=['test']), \
                 patch.object(ps, '_db_query_strict', new=AsyncMock(return_value=[row])), \
                 patch.object(ps.ai_classifier, '_call_gemini_json', new=AsyncMock(return_value={label: None})), \
                 patch.object(ps, '_db_write_strict', new=AsyncMock()) as write:
                result = await ps.restyle_pending_diffs(['test'])
                self.assertEqual(result['styled'], 0)
                write.assert_not_awaited()
        finally:
            ps.STYLE_STATUS.clear()
            ps.STYLE_STATUS.update(previous)
