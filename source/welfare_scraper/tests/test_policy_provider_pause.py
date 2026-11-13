"""Standalone policy tools stop at real provider holds; temporary profiles only."""
import asyncio
from contextlib import ExitStack
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import ai_classifier
import database
from services import ai_coordinator as quota, ai_verified_input, policy_sync as ps
from tests import test_news_custom_input_budget as input_fixtures
from tests import test_check_policies_baseline_failure as policy_fixtures
from tests.test_policy_presend_budget import LARGE


class StandaloneProviderPause(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        input_fixtures.CurrentCustomInputBudget.setUp(self)
        ai_verified_input._reset_for_tests()
        self.addCleanup(ai_verified_input._reset_for_tests)
        policy_fixtures.TestCheckPoliciesBaselineExtractionFailure.setUp(self)
        self.addCleanup(policy_fixtures.TestCheckPoliciesBaselineExtractionFailure.tearDown, self)
        database.execute_write(
            'INSERT INTO notion_policies (db_key,page_id,title,notion_fields,source_urls,source_hashes,source_texts) '
            'SELECT db_key,?, ?,notion_fields,source_urls,source_hashes,source_texts FROM notion_policies',
            ('provider-pause-second', '두 번째 합성 정책'))
        database.execute_write('UPDATE notion_policies SET source_texts=?',
            (json.dumps({policy_fixtures._URL: LARGE}),))
        self.counter = AsyncMock(return_value=SimpleNamespace(total_tokens=1000))
        self.client.aio.models.count_tokens = self.counter
        self.provider.side_effect = RuntimeError('429 GenerateRequestsPerDayPerProjectPerModel-FreeTier')
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for context in (
            patch.dict(os.environ, {'CLASSIFY_MODEL': self.model, 'POLICY_AI_RPM': '1',
                                     'POLICY_AI_RATE_PERIOD': '60'}),
            patch.object(ps, '_fetch_source', new=AsyncMock(return_value=(LARGE, LARGE, False))),
            patch.object(ps, '_get_db_schema', return_value={}),
            patch.object(ai_classifier, 'get_client', return_value=self.client),
            patch.object(ps.notion_service, 'update_page_properties',
                         new=AsyncMock(side_effect=AssertionError('Unexpected Notion write'))),
            patch('aiohttp.ClientSession._request',
                  new=AsyncMock(side_effect=AssertionError('Unexpected real HTTP request'))),
        ):
            self.stack.enter_context(context)

    def saved_rows(self):
        rows = [dict(row) for row in database.execute_query(
            'SELECT id,source_hashes,source_texts,audited_at,last_extracted_fields,pending_diff,notion_fields '
            'FROM notion_policies ORDER BY id')]
        # JSON escaping may be normalized by a successful local save; compare
        # the complete preserved values rather than equivalent serialization.
        for row in rows:
            for name in ('source_hashes', 'source_texts', 'last_extracted_fields', 'pending_diff', 'notion_fields'):
                if row[name] is not None:
                    row[name] = json.loads(row[name])
        return rows

    async def test_standalone_check_stops_after_provider_denial_without_rpm_wait_or_recount(self):
        before = self.saved_rows()
        result = await asyncio.wait_for(ps.check_policies(['medical'], scraper=object()), timeout=5)
        self.assertEqual(result['pause_reason'], 'daily_limit')
        self.assertEqual(result['ai_required'], 1)
        self.assertEqual(result['ai_failed'], 1)
        self.assertEqual(result['ai_rate_waited'], 0)
        self.assertIn('잠시 멈춤', ps.POLICY_STATUS['message'])
        self.counter.assert_awaited_once()
        self.provider.assert_awaited_once()
        self.assertEqual(self.saved_rows(), before)
        self.assertEqual(quota.status()['recorded_requests_today'], 1)

    async def test_standalone_check_existing_hold_never_counts_or_sends(self):
        quota.mark_daily_exhausted(self.model)
        before = self.saved_rows()
        result = await asyncio.wait_for(ps.check_policies(['medical'], scraper=object()), timeout=5)
        self.assertEqual(result['pause_reason'], 'daily_limit')
        self.assertEqual(result['ai_required'], 0)
        self.assertEqual(result['ai_rate_waited'], 0)
        self.counter.assert_not_awaited()
        self.provider.assert_not_awaited()
        self.assertEqual(self.saved_rows(), before)

    async def test_initial_audit_stops_after_provider_denial_preserving_unfinished_rows(self):
        before = self.saved_rows()
        result = await asyncio.wait_for(ps.run_initial_audit(['medical']), timeout=5)
        self.assertEqual(result['pause_reason'], 'daily_limit')
        self.assertEqual(result['checked'], 1)
        self.assertEqual(result['ai_failed'], 1)
        self.assertIn('잠시 멈춤', ps.AUDIT_STATUS['message'])
        self.counter.assert_awaited_once()
        self.provider.assert_awaited_once()
        self.assertEqual(self.saved_rows(), before)

    async def test_initial_audit_existing_hold_does_not_count_unstarted_policies(self):
        quota.mark_daily_exhausted(self.model)
        before = self.saved_rows()
        result = await ps.run_initial_audit(['medical'])
        self.assertEqual(result['pause_reason'], 'daily_limit')
        self.assertEqual(result['checked'], 0)
        self.assertEqual(result['ai_failed'], 0)
        self.counter.assert_not_awaited()
        self.provider.assert_not_awaited()
        self.assertEqual(self.saved_rows(), before)

    async def test_privacy_review_is_per_policy_not_a_global_provider_pause(self):
        async def private(*args, review, **kwargs):
            review.update(code='ai_input_review_required', fields=[], note='개인정보 확인 필요')
            return None
        with patch.object(ai_classifier, 'extract_policy_fields_async', new=AsyncMock(side_effect=private)) as extract:
            result = await ps.check_policies(['medical'], scraper=object())
            self.assertEqual(extract.await_count, 2)
            self.assertEqual(result['pause_reason'], '')
            self.assertEqual(result['ai_failed'], 2)
        self.provider.assert_not_awaited()
        self.counter.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
