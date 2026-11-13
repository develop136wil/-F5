"""Real policy preflight/admission; fake external SDK only, isolated runner only."""
import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from google.genai import types
import ai_classifier
from services import ai_coordinator as quota
from tests import test_news_custom_input_budget as fixtures


SHORT = '지원내용\n교통비 3만원 지원\n신청방법\n주민센터 방문 신청'
LARGE = '지원내용\n' + ('지원금은 서류 심사를 거쳐 지급하며 중복 지급은 불가합니다.\n' * 90)


def null_response():
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(parts=[types.Part(text='null')]), finish_reason='STOP')])


class PolicyPresendBudget(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from services import ai_verified_input
        fixtures.CurrentCustomInputBudget.setUp(self)
        ai_verified_input._reset_for_tests()
        self.addCleanup(ai_verified_input._reset_for_tests)
        self.client.aio.models.count_tokens = AsyncMock(return_value=SimpleNamespace(total_tokens=10000))

    async def extract(self, source, callback):
        review = {}
        with patch.dict(os.environ, {'CLASSIFY_MODEL': self.model}):
            result = await ai_classifier.extract_policy_fields_async(self.client, '합성 정책',
                'https://example.invalid/policy', content=source, review=review, before_send=callback)
        return result, review

    async def test_twelve_oversize_inputs_consume_no_workflow_slot_or_sdk_quota(self):
        callback = AsyncMock()
        for _ in range(12):
            result, review = await self.extract(LARGE, callback)
            self.assertIsNone(result)
            self.assertEqual(review['code'], 'source_input_budget_exceeded')
        callback.assert_not_awaited()
        self.provider.assert_not_awaited()
        self.assertEqual(quota.status()['recorded_requests_today'], 0)

    async def test_privacy_hold_occurs_before_workflow_slot(self):
        callback = AsyncMock()
        _, review = await self.extract(SHORT + '\n담당자 휴대전화: 010-1234-5678', callback)
        self.assertEqual(review['code'], 'ai_input_review_required')
        callback.assert_not_awaited()
        self.provider.assert_not_awaited()
        self.assertEqual(quota.status()['recorded_requests_today'], 0)

    async def test_prepared_request_enters_callback_then_durable_quota_then_provider(self):
        events = []
        async def before():
            self.assertEqual(quota.status()['recorded_requests_today'], 0)
            self.provider.assert_not_awaited()
            events.append('local_slot')
        async def provider(**_kwargs):
            self.assertEqual(quota.status()['recorded_requests_today'], 1)
            events.append('provider')
            return null_response()
        self.provider.side_effect = provider
        result, review = await self.extract(SHORT, before)
        self.assertIsNone(result)
        self.assertEqual(review['code'], 'model_abstained')
        self.assertEqual(events, ['local_slot', 'provider'])
        self.provider.assert_awaited_once()

    async def test_provider_failure_keeps_workflow_attempt_and_durable_reservation(self):
        callback = AsyncMock()
        self.provider.side_effect = RuntimeError('Synthetic provider failure without retry status')
        result, _ = await self.extract(SHORT, callback)
        self.assertIsNone(result)
        callback.assert_awaited_once()
        self.provider.assert_awaited_once()
        self.assertEqual(quota.status()['recorded_requests_today'], 1)

    async def test_cancel_during_local_wait_propagates_without_provider_or_reservation(self):
        callback = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await self.extract(SHORT, callback)
        callback.assert_awaited_once()
        self.provider.assert_not_awaited()
        self.assertEqual(quota.status()['recorded_requests_today'], 0)

    async def test_actual_reservation_rechecks_corrupt_minute_ledger_after_count(self):
        self.client.aio.models.count_tokens.return_value = SimpleNamespace(total_tokens=1000)
        async def before():
            # This path is a disposable fixture's ledger, never an app profile.
            quota._atomic_json(quota._state_path() + '.minute.json', {'version': -1})
        result, review = await self.extract(LARGE, before)
        self.assertIsNone(result)
        self.assertEqual(review['code'], 'source_input_count_unavailable')
        self.assertTrue(review['retryable'])
        self.client.aio.models.count_tokens.assert_awaited_once()
        self.provider.assert_not_awaited()
        self.assertEqual(quota.status()['recorded_requests_today'], 0)

    async def test_actual_reservation_rechecks_temporary_capacity_after_count(self):
        from services import ai_rate_policy as rate
        self.client.aio.models.count_tokens.return_value = SimpleNamespace(total_tokens=1000)
        async def before():
            path = quota._state_path() + '.minute.json'
            window = rate.read_window(path)
            for _ in range(3):
                rate.lower_after_rate_error(window, self.model, 1, limit_kind='unknown')
            quota._atomic_json(path, window)
        result, review = await self.extract(LARGE, before)
        self.assertIsNone(result)
        self.assertEqual(review['code'], 'source_input_rate_limited')
        self.assertTrue(review['retryable'])
        self.client.aio.models.count_tokens.assert_awaited_once()
        self.provider.assert_not_awaited()
        self.assertEqual(quota.status()['recorded_requests_today'], 0)


class PolicyWorkflowPresend(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from tests.test_check_policies_baseline_failure import TestCheckPoliciesBaselineExtractionFailure
        fixtures.CurrentCustomInputBudget.setUp(self)
        from services import ai_verified_input
        ai_verified_input._reset_for_tests()
        self.addCleanup(ai_verified_input._reset_for_tests)
        self.client.aio.models.count_tokens = AsyncMock(return_value=SimpleNamespace(total_tokens=10000))
        TestCheckPoliciesBaselineExtractionFailure.setUp(self)
        self.addCleanup(TestCheckPoliciesBaselineExtractionFailure.tearDown, self)

    async def test_two_actual_input_holds_do_not_trigger_workflow_rpm_wait(self):
        import database
        from services import policy_sync as ps
        database.execute_write('''INSERT INTO notion_policies
            (db_key,page_id,title,notion_fields,source_urls,source_hashes,source_texts)
            VALUES (?,?,?,?,?,?,?)''', ('medical', 'second-oversize', '두 번째 합성 정책', '{}',
                json.dumps(['https://test.example/policy-second']),
                json.dumps({'https://test.example/policy-second': 'second_old_hash'}),
                json.dumps({'https://test.example/policy-second': '두 번째 이전 원문'})))
        with patch.dict(os.environ, {'CLASSIFY_MODEL': self.model, 'POLICY_AI_RPM': '1',
                                     'POLICY_AI_RATE_PERIOD': '0.02'}), \
                patch.object(ps, '_fetch_source', new=AsyncMock(return_value=(LARGE, LARGE, False))), \
                patch.object(ps, '_get_db_schema', return_value={}), \
                patch.object(ai_classifier, 'get_client', return_value=self.client):
            result = await ps.check_policies(['medical'], scraper=object())
        self.assertEqual(result['ai_required'], 2)
        self.assertEqual(result['ai_failed'], 2)
        self.assertEqual(result['ai_rate_waited'], 0)
        self.provider.assert_not_awaited()
        self.assertEqual(quota.status()['recorded_requests_today'], 0)
