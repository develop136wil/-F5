"""Complete policy input and durable review regressions, fake SDK boundary only.

Run through scripts/run_isolated_tests.py. No operational data or live provider.
"""
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from google.genai import types
import ai_classifier
from services import ai_coordinator as quota, ai_verified_input as verified
from services import policy_review_store, policy_source_plan
from services.policy_source_units import source_units
from tests import test_news_custom_input_budget as fixtures


def source_of_length(size):
    beginning = '지원내용\n교통비를 지원합니다.\n신청방법\n'
    unit = ('방문 신청 시 안내문을 먼저 읽어 주세요. 서류 접수 결과는 기관에서 안내합니다. ' * 3) + '\n'
    tail = '\n제외조건\n중복 지원은 받을 수 없습니다.'
    middle_size = size - len(beginning) - len(tail)
    return beginning + (unit * (middle_size // len(unit) + 1))[:middle_size] + tail


def setup_fixture(self):
    fixtures.CurrentCustomInputBudget.setUp(self)
    verified._reset_for_tests()
    self.addCleanup(verified._reset_for_tests)
    self.counter = AsyncMock(return_value=SimpleNamespace(total_tokens=1000))
    self.client.aio.models.count_tokens = self.counter


class PolicyInputParityAudit(unittest.IsolatedAsyncioTestCase):
    setUp = setup_fixture

    async def extract(self, source, *, draft=False, callback=None):
        review = {}
        with patch.dict(os.environ, {'CLASSIFY_MODEL': self.model,
                'POLICY_SOURCE_MAX_CHARS': '30000', 'AI_CONTENT_MAX_CHARS': '6000'}):
            result = await ai_classifier.extract_policy_fields_async(self.client, '교통비 지원',
                'https://example.invalid/policy', content=source, review=review,
                draft_preview=draft, before_send=callback)
        return result, review

    async def assert_complete_source(self, size, *, draft=False):
        source = source_of_length(size)
        ids = [row['id'] for row in source_units(source)]
        raw = dict(_source_units=dict(support_detail=ids, eligibility_detail=ids,
            age_detail=[], target_income=[]), category='', target_characteristics=[],
            residence=[], life_cycle=[], start_age_months=None, end_age_months=None, contact='')
        self.provider.return_value = types.GenerateContentResponse(candidates=[types.Candidate(
            content=types.Content(parts=[types.Part(text=json.dumps(raw, ensure_ascii=False))]),
            finish_reason='STOP')])
        result, review = await self.extract(source, draft=draft)
        self.assertIsNotNone(result, review)
        self.assertEqual(result['support_detail'], source)
        self.assertEqual(result['eligibility_detail'], source)
        self.assertIn('중복 지원은 받을 수 없습니다.', result['eligibility_detail'])
        self.counter.assert_awaited_once()
        self.provider.assert_awaited_once()
        request = self.provider.await_args.kwargs
        self.assertEqual(self.counter.await_args.kwargs['contents'], request['contents'])
        units = json.loads(request['contents'].split('Units (complete original text): ')[1].split('\nAllowed options:')[0])
        self.assertEqual(''.join(unit['text'] for unit in units), source)
        self.assertEqual(quota.input_token_capacity(self.model), 6000)
        self.assertEqual(quota.status()['recorded_requests_today'], 1)

    async def test_policy_1500_characters_is_counted_without_truncation(self):
        await self.assert_complete_source(1500)

    async def test_policy_3000_characters_is_counted_without_truncation(self):
        await self.assert_complete_source(3000)

    async def test_policy_6000_characters_is_counted_without_truncation(self):
        await self.assert_complete_source(6000)

    async def test_policy_15000_characters_keeps_tail_exception(self):
        await self.assert_complete_source(15000)

    async def test_policy_30000_characters_is_a_complete_single_request(self):
        await self.assert_complete_source(30000)

    async def test_draft_1500_characters_uses_the_same_full_input_path(self):
        await self.assert_complete_source(1500, draft=True)

    async def test_draft_15000_characters_keeps_tail_exception(self):
        await self.assert_complete_source(15000, draft=True)

    async def test_sensitive_tail_blocks_count_and_generation(self):
        result, review = await self.extract(source_of_length(1500) + '\n010-1234-5678')
        self.assertIsNone(result)
        self.assertEqual(review['code'], 'ai_input_review_required')
        self.counter.assert_not_awaited()
        self.provider.assert_not_awaited()

    async def test_count_unavailable_is_retryable_and_not_a_source_size_failure(self):
        self.counter.side_effect = RuntimeError('Synthetic count outage')
        result, review = await self.extract(source_of_length(1500))
        self.assertIsNone(result)
        self.assertEqual(review['code'], 'source_input_count_unavailable')
        self.assertTrue(review['retryable'])
        self.assertTrue(policy_review_store.supports_review(review))
        self.provider.assert_not_awaited()
        self.assertEqual(quota.status()['recorded_requests_today'], 0)

    async def test_true_counted_oversize_is_preserved_for_review_before_workflow_slot(self):
        self.counter.return_value = SimpleNamespace(total_tokens=10000)
        callback = AsyncMock()
        result, review = await self.extract(source_of_length(1500), callback=callback)
        self.assertIsNone(result)
        self.assertEqual(review['code'], 'source_input_budget_exceeded')
        self.assertTrue(policy_review_store.supports_review(review))
        self.assertNotIn('retryable', review)
        callback.assert_not_awaited()
        self.provider.assert_not_awaited()

    async def test_local_source_safety_cap_is_not_truncation(self):
        result, review = await self.extract(source_of_length(30001))
        self.assertIsNone(result)
        self.assertEqual(review['code'], 'source_too_long')
        self.counter.assert_not_awaited()
        self.provider.assert_not_awaited()

    async def test_cancel_during_count_propagates_without_generation(self):
        import asyncio
        self.counter.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.extract(source_of_length(1500))
        self.provider.assert_not_awaited()
        self.assertEqual(quota.status()['recorded_requests_today'], 0)

    async def test_temporary_quota_is_retryable_without_reclassifying_source_size(self):
        with patch.object(quota, 'acquire_model_slot', new=AsyncMock(
                side_effect=quota.AiTemporaryRateLimit(60))):
            result, review = await self.extract(source_of_length(1500))
        self.assertIsNone(result)
        self.assertEqual(review['code'], 'source_input_rate_limited')
        self.assertTrue(review['retryable'])
        self.assertTrue(policy_review_store.supports_review(review))
        self.provider.assert_not_awaited()

    async def test_request_preparation_does_not_block_status_event_loop(self):
        import asyncio
        import threading
        released = threading.Event()
        original = policy_source_plan.build_policy_extraction_request
        def preparation(*args, **kwargs):
            self.assertTrue(released.wait(2), 'Policy preparation blocked the event loop')
            return original(*args, **kwargs)
        timer = asyncio.get_running_loop().call_later(0.02, released.set)
        try:
            with patch.object(policy_source_plan, 'build_policy_extraction_request', side_effect=preparation):
                await self.assert_complete_source(1500)
        finally:
            timer.cancel()
            released.set()

    async def test_quota_ledger_failure_after_count_is_a_saved_retryable_hold(self):
        with patch.object(quota, 'acquire_model_slot', new=AsyncMock(
                side_effect=quota.AiQuotaStateUnavailable('Synthetic ledger failure'))):
            result, review = await self.extract(source_of_length(1500))
        self.assertIsNone(result)
        self.assertEqual(review['code'], 'source_input_count_unavailable')
        self.assertTrue(review['retryable'])
        self.assertTrue(policy_review_store.supports_review(review))
        self.counter.assert_awaited_once()
        self.provider.assert_not_awaited()
        self.assertEqual(quota.status()['recorded_requests_today'], 0)

    async def test_cancel_during_pure_preparation_sends_nothing_after_worker_finishes(self):
        import asyncio
        import threading
        started, released, finished = threading.Event(), threading.Event(), threading.Event()
        original = policy_source_plan.build_policy_extraction_request
        def preparation(*args, **kwargs):
            started.set()
            try:
                if not released.wait(2):
                    raise AssertionError('Test did not release pure preparation')
                return original(*args, **kwargs)
            finally:
                finished.set()
        with patch.object(policy_source_plan, 'build_policy_extraction_request', side_effect=preparation):
            task = asyncio.create_task(self.extract(source_of_length(1500)))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                task.cancel()
                released.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(await asyncio.to_thread(finished.wait, 2))
            finally:
                released.set()
        self.counter.assert_not_awaited()
        self.provider.assert_not_awaited()


class PolicyMissingDurableHoldAudit(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from tests.test_check_policies_baseline_failure import TestCheckPoliciesBaselineExtractionFailure
        setup_fixture(self)
        TestCheckPoliciesBaselineExtractionFailure.setUp(self)
        self.addCleanup(TestCheckPoliciesBaselineExtractionFailure.tearDown, self)

    async def test_input_size_hold_has_run_notice_and_saved_review(self):
        import database
        from services import policy_sync as ps
        source = source_of_length(1500)
        self.counter.return_value = SimpleNamespace(total_tokens=10000)
        with patch.dict(os.environ, {'CLASSIFY_MODEL': self.model, 'POLICY_SOURCE_MAX_CHARS': '30000'}), \
                patch.object(ps, '_fetch_source', new=AsyncMock(return_value=(source, source, False))), \
                patch.object(ps, '_get_db_schema', return_value={}), \
                patch.object(ai_classifier, 'get_client', return_value=self.client):
            result = await ps.check_policies(['medical'], scraper=object())
        self.assertEqual(result['review_count'], 1)
        self.assertEqual(result['review_notes'][0]['code'], 'source_input_budget_exceeded')
        saved = policy_review_store.list_unresolved(['medical'])
        self.assertEqual(saved['total'], 1)
        self.assertEqual(saved['items'][0]['code'], 'source_input_budget_exceeded')
        row = database.execute_query('SELECT source_hashes,pending_diff FROM notion_policies', fetch_one=True)
        self.assertIn('old_hash_abc', row['source_hashes'])
        self.assertIsNone(row['pending_diff'])
        self.provider.assert_not_awaited()
        self.counter.assert_awaited_once()
        # Reopening connections must not lose the actionable hold.
        database.close_all_connections()
        self.assertEqual(policy_review_store.list_unresolved(['medical'])['total'], 1)
