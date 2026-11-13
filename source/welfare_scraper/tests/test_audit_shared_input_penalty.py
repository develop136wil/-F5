"""Regression: temporary quota penalties must never become terminal size holds.
Synthetic input/provider only; use scripts/run_isolated_tests.py.
"""
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from services import ai_coordinator as quota, ai_verified_input as verified
from services import news_custom_analysis as custom, news_analysis_store as jobs
from services.news_analysis_worker import process_job
from google.genai import types
from tests import test_news_custom_input_budget as fixture


class AuditSharedPenalty(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.CurrentCustomInputBudget.setUp
    call_db = fixture.CurrentCustomInputBudget.call_db

    async def test_temporary_capacity_wait_preserves_short_news_and_resumes(self):
        verified._reset_for_tests()
        self.addCleanup(verified._reset_for_tests)
        self.client.aio.models.count_tokens = AsyncMock(return_value=SimpleNamespace(total_tokens=500))
        job = fixture.CurrentCustomInputBudget.enqueue(self, fixture.SHORT, recipe=custom.RECIPE)
        before = time.time()
        budgets = [quota.input_token_budget(self.model)]
        for _ in range(2):
            quota.register_api_error(self.model, RuntimeError('429 TokensPerMinute retryDelay: 2s'))
            budgets.append(quota.input_token_budget(self.model))
        self.assertEqual(budgets, [6000, 3000, 1500])
        with patch.object(verified, '_take_count_slot', new=AsyncMock(return_value=True)):
            outcome = await process_job(job, self.client, call_db=self.call_db, clock=lambda: 0)
        saved = jobs.get_job(self.conn, job)
        self.assertEqual(outcome, 'waiting')
        self.assertEqual(saved['reason'], 'rate_limited')
        self.provider.assert_not_awaited()
        self.assertEqual(len(fixture.SHORT), 40)
        self.assertEqual(quota.status()['recorded_requests_today'], 0)
        window = json.loads((self.root / 'quota.json.minute.json').read_text(encoding='utf-8'))
        penalty = window['penalties'][self.model]
        self.assertGreater(penalty['until'] - penalty['cooldown_until'], 3500)
        # Simulate elapsed time, never erase the durable penalty or usage state.
        self.provider.return_value = types.GenerateContentResponse(candidates=[types.Candidate(
            content=types.Content(parts=[types.Part(text=json.dumps(
                fixture.CurrentCustomInputBudget.response(self), ensure_ascii=False))]), finish_reason='STOP')])
        with patch.object(time, 'time', return_value=before + 3700), \
                patch.object(quota, 'time', SimpleNamespace(time=lambda: before + 3700,
                    monotonic=lambda: time.monotonic() + 3700, sleep=time.sleep)):
            self.assertEqual(await process_job(job, self.client, call_db=self.call_db,
                                              clock=lambda: 3700), 'complete')
        self.provider.assert_awaited_once()
        self.assertEqual(quota.status()['recorded_requests_today'], 1)
        self.assertEqual(jobs.get_job(self.conn, job)['state'], 'complete')

    def test_local_wait_is_retryable_even_after_many_claims(self):
        from services.news_retry_policy import decide_retry
        decision = decide_retry(quota.AiTemporaryRateLimit(3600), attempts=8)
        self.assertEqual(decision.reason, 'rate_limited')
        self.assertFalse(decision.terminal)
        self.assertEqual(decision.delay, 3600)
