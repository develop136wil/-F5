"""Offline integration: completed field apply followed by source checks.

Uses real SQLite/finalization/check orchestration, with source and AI mocked.
No Notion writes, Gemini calls, or embedding calls are permitted.
"""
import json
import unittest
from unittest.mock import patch

import database
from services import policy_sync as ps
from tests import test_check_policies_baseline_failure as baseline


class CompletedRecheckTests(unittest.IsolatedAsyncioTestCase):
    # Reuse isolated fixtures without inheriting unrelated test cases.
    setUp = baseline.TestCheckPoliciesBaselineExtractionFailure.setUp
    tearDown = baseline.TestCheckPoliciesBaselineExtractionFailure.tearDown
    _row = baseline.TestCheckPoliciesBaselineExtractionFailure._row
    _run_check = baseline.TestCheckPoliciesBaselineExtractionFailure._run_check

    def complete(self):
        extracted = {'eligibility_detail': '보호자 동승 시 50% 할인'}
        database.execute_write(
            'UPDATE notion_policies SET last_extracted_fields=?, change_status=?',
            (json.dumps(extracted), 'detected'))
        row = database.execute_query('SELECT id FROM notion_policies', fetch_one=True)
        label = ps._PROP_LABEL['eligibility_detail']
        pending = {'fields': [{'field': label, 'old': '기존 조건',
                              'new': '보호자 동승 시 50% 감면', 'level': 'high'}]}
        self.assertEqual(ps._finalize_apply(row['id'], label, pending,
                         'eligibility_detail', '보호자 동승 시 50% 감면'), 0)
        self.assertEqual(self._row()['change_status'], 'detected')
        self.assertTrue(json.loads(self._row()['pending_diff'])['completed'])
        return extracted

    async def check(self, extracted):
        async def fake_extract(*args, **kwargs):
            return dict(extracted)

        async def offline_diff(old, new, client):
            return ps._diff_fields(old, new, None, {})

        with patch.object(ps, '_diff_fields_with_embeddings', side_effect=offline_diff):
            return await self._run_check(fake_extract)

    async def test_completed_styled_value_not_reopened_and_unchanged_source_skips_ai(self):
        extracted = self.complete()
        first = await self.check(extracted)
        self.assertEqual(first['changed'], 0)
        self.assertEqual(first['ai_completed'], 1)
        self.assertEqual(self._row()['change_status'], 'detected')
        second = await self.check(extracted)
        self.assertEqual(second['changed'], 0)
        self.assertEqual(second['ai_required'], 0)
        self.assertEqual(self._row()['change_status'], 'detected')

    async def test_real_condition_change_reopens_completed_policy(self):
        self.complete()
        result = await self.check({'eligibility_detail': '보호자 동승 시 30% 할인'})
        self.assertEqual(result['changed'], 1)
        row = self._row()
        self.assertEqual(row['change_status'], 'detected')
        self.assertIn('30%', row['pending_diff'])
