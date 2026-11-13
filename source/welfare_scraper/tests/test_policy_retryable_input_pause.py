"""Temporary SQLite journeys: input-count holds pause without losing baselines."""
import json
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import database
from services import policy_sync as ps, policy_review_store as reviews
from tests import test_check_policies_baseline_failure as fixtures

SOURCE = '지원 내용은 교육 프로그램입니다. 신청 대상은 지역 주민입니다.'
CODES = ('source_input_count_unavailable', 'source_input_rate_limited')


class RetryablePolicyInput(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.TestCheckPoliciesBaselineExtractionFailure.setUp(self)
        database.execute_write(
            'INSERT INTO notion_policies (db_key,page_id,title,notion_fields,source_urls,source_hashes,source_texts) '
            'SELECT db_key,?, ?,notion_fields,source_urls,source_hashes,source_texts FROM notion_policies',
            ('second-policy', '두 번째 정책'))

    tearDown = fixtures.TestCheckPoliciesBaselineExtractionFailure.tearDown

    def rows(self):
        rows = [dict(row) for row in database.execute_query(
            'SELECT id,source_hashes,source_texts,audited_at,last_extracted_fields,notion_fields FROM notion_policies ORDER BY id')]
        for row in rows:
            for key in ('source_hashes', 'source_texts', 'last_extracted_fields', 'notion_fields'):
                if row[key] is not None:
                    row[key] = json.loads(row[key])
        return rows

    def mocks(self, stack, extract):
        stack.enter_context(patch.object(ps.ai_classifier, 'get_client', return_value=object()))
        ai = stack.enter_context(patch.object(ps.ai_classifier, 'extract_policy_fields_async', new=AsyncMock(side_effect=extract)))
        stack.enter_context(patch.object(ps, '_fetch_source', new=AsyncMock(return_value=(SOURCE, SOURCE, False))))
        stack.enter_context(patch.object(ps, '_get_db_schema', return_value={}))
        stack.enter_context(patch.object(ps, '_diff_fields_with_embeddings', new=AsyncMock(return_value=[])))
        stack.enter_context(patch('services.policy_full_check.ai_pause_reason', return_value=''))
        stack.enter_context(patch.object(ps.notion_service, 'update_page_properties',
            new=AsyncMock(side_effect=AssertionError('Unexpected Notion write'))))
        stack.enter_context(patch('aiohttp.ClientSession._request',
            new=AsyncMock(side_effect=AssertionError('Unexpected HTTP request'))))
        return ai

    async def test_retryable_input_hold_stops_remaining_policies_and_recovers_on_next_check(self):
        for code in CODES:
            with self.subTest(code=code):
                database.execute_write('UPDATE notion_policies SET audited_at=NULL,last_extracted_fields=NULL')
                before = self.rows()
                async def held(*args, review, **kwargs):
                    review.update(code=code, note='입력 확인을 잠시 보류했습니다.', fields=[], retryable=True)
                    return None
                with ExitStack() as stack:
                    ai = self.mocks(stack, held)
                    result = await ps.check_policies(['medical'], scraper=object(), include_initial=True, stop_on_quota=True)
                ai.assert_awaited_once()
                self.assertEqual(result['pause_reason'], code)
                self.assertEqual(result['ai_failed'], 1)
                self.assertEqual(self.rows(), before, 'No baseline, original or comparison marker may advance')
                self.assertEqual(reviews.unresolved_by_policy(['medical'])[before[0]['id']][0]['code'], code)
                with ExitStack() as stack:
                    ai = self.mocks(stack, lambda *args, **kwargs: {'support_detail': SOURCE})
                    result = await ps.check_policies(['medical'], scraper=object(), include_initial=True, stop_on_quota=True)
                self.assertEqual(ai.await_count, 2)
                self.assertEqual(result['pause_reason'], '')
                self.assertEqual(result['initial_compared'], 2)
                self.assertTrue(all(row['audited_at'] is not None for row in self.rows()))
                self.assertEqual(reviews.unresolved_by_policy(['medical']), {})

    async def test_initial_audit_counts_generic_failed_extraction_without_completing_baseline(self):
        before = self.rows()
        with ExitStack() as stack:
            ai = self.mocks(stack, lambda *args, **kwargs: None)
            result = await ps.run_initial_audit(['medical'])
        self.assertEqual(ai.await_count, 2)
        self.assertEqual(result['ai_failed'], 2)
        self.assertEqual(result['checked'], 2)
        self.assertIn('일부 완료', ps.AUDIT_STATUS['message'])
        self.assertEqual(self.rows(), before)

    async def test_initial_audit_retryable_hold_pauses_after_one_attempt(self):
        for code in CODES:
            with self.subTest(code=code):
                before = self.rows()
                async def held(*args, review, **kwargs):
                    review.update(code=code, fields=[], retryable=True)
                    return None
                with ExitStack() as stack:
                    ai = self.mocks(stack, held)
                    result = await ps.run_initial_audit(['medical'])
                ai.assert_awaited_once()
                self.assertEqual(result['pause_reason'], code)
                self.assertEqual(result['checked'], 1)
                self.assertEqual(result['ai_failed'], 1)
                self.assertIn('잠시 멈춤', ps.AUDIT_STATUS['message'])
                self.assertEqual(self.rows(), before)


if __name__ == '__main__':
    unittest.main()
