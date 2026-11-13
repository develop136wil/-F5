"""Real worker/classifier/quota ledger, synthetic SDK boundary; no API calls."""
import asyncio
import json
import os
import sqlite3
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from services import ai_coordinator as quota
from services import ai_rate_policy
from services import news_profile_store as profiles, news_analysis_store as jobs
from services import news_custom_analysis as custom
from services.news_profile import NewsProfile, capture_legacy_profile
from services.news_analysis_worker import process_job, RECIPE


class TestNewsQuotaPipeline(unittest.IsolatedAsyncioTestCase):
    MODEL = 'gemini-3.5-flash-lite'

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='news-quota-pipeline-'))
        self.stack.enter_context(patch.object(quota, '_state_path', return_value=str(Path(directory) / 'quota.json')))
        # Use an observed supported model. The SDK is still entirely synthetic.
        observed = ai_rate_policy.observe({self.MODEL: {'rpm':15,'tpm':250000,'rpd':500},
            'gemini-embedding-001': {'rpm':100,'tpm':30000,'rpd':1000}}, key='test-key')
        (Path(directory)/'config.json').write_text(json.dumps({'GOOGLE_API_KEY':'test-key',
            ai_rate_policy.POLICY_KEY:observed, 'AI_DAILY_REQUEST_LIMIT':20}), encoding='utf-8')
        self.stack.enter_context(patch.dict(os.environ, {'AI_GLOBAL_GENERATE_RPM': '1000'}))
        quota._reset_for_tests()
        self.stack.callback(quota._reset_for_tests)
        self.conn = sqlite3.connect(':memory:')
        self.stack.callback(self.conn.close)
        profiles.ensure_schema(self.conn)
        jobs.ensure_schema(self.conn)
        legacy = capture_legacy_profile(region='도봉', max_age_months=108,
            keywords=['영유아'], excluded_keywords=[], site_ids=['1'], ai_prompt='',
            semantic_text='', relevance_enabled=True)
        profiles.initialize_legacy(self.conn, legacy)
        profiles.create_profile(self.conn, NewsProfile('custom-quota', 1, '시험', '전국', site_ids=('1',)))
        self.runs = {recipe: profiles.prepare_run(self.conn, profile_id, expected_revision=1,
            sites=[dict(id=1, name='시험', url='https://example.invalid')])
            for recipe, profile_id in ((RECIPE, legacy.profile_id), (custom.RECIPE, 'custom-quota'))}
        self.generate = AsyncMock()
        self.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=self.generate)))

    def enqueue(self, recipe, title='시험 안내'):
        return jobs.enqueue(self.conn, self.runs[recipe], payload=dict(title=title,
            link='https://example.invalid/notice', site_name='시험', content='영유아 돌봄 지원 안내'),
            model=self.MODEL, recipe=recipe)

    async def call_db(self, fn, *args, **kwargs):
        return fn(self.conn, *args, **kwargs)

    async def process(self, job, now=100):
        return await process_job(job, self.client, call_db=self.call_db, clock=lambda: now)

    async def test_worker_attempt_budget_counts_sdk_requests_not_nested_retries(self):
        self.generate.side_effect = RuntimeError('503 unavailable synthetic failure')
        # Skip only the classifier's internal backoff; the real ledger still reserves every call.
        with patch('ai_classifier.asyncio', SimpleNamespace(sleep=AsyncMock())):
            for recipe in self.runs:
                with self.subTest(recipe=recipe):
                    before = self.generate.await_count
                    job = self.enqueue(recipe)
                    for attempt in range(5):
                        self.assertEqual(await self.process(job, attempt * 1000),
                                         'failed' if attempt == 4 else 'waiting')
                    self.assertEqual(await self.process(job, 10000), 'not_claimed')
                    self.assertEqual(self.generate.await_count - before, 5)
                    self.assertEqual(jobs.get_job(self.conn, job)['reason'], 'retry_limit_reached')
        self.assertEqual(quota.status()['recorded_requests_today'], 10)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_cache').fetchone()[0], 0)

    async def test_daily_exhaustion_blocks_next_job_after_ledger_reload(self):
        self.generate.side_effect = RuntimeError('429 requests per day quota synthetic-secret')
        for index, recipe in enumerate(self.runs):
            job = self.enqueue(recipe)
            self.assertEqual(await self.process(job), 'failed')
            self.assertEqual(await self.process(job, 10000), 'not_claimed')
            row = jobs.get_job(self.conn, job)
            self.assertEqual(row['reason'], 'daily_quota_exhausted')
            self.assertNotIn('synthetic-secret', json.dumps(row))
            # Simulate a fresh coordinator reading the same durable hold.
            quota._reset_for_tests()
        self.assertEqual(self.generate.await_count, 1)
        self.assertEqual(quota.status()['recorded_requests_today'], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_cache').fetchone()[0], 0)

    async def test_provider_delay_is_saved_without_in_worker_retry(self):
        self.generate.side_effect = RuntimeError('429 RESOURCE_EXHAUSTED retryDelay: 120s')
        job = self.enqueue(RECIPE)
        mono = [1000.0]
        epoch = time.time()
        clock = SimpleNamespace(monotonic=lambda: mono[0], time=lambda: epoch + mono[0] - 1000,
                                time_ns=time.time_ns, sleep=time.sleep)
        with patch.object(quota, 'time', clock), \
             patch.object(ai_rate_policy, 'time', clock), \
             patch('ai_classifier.asyncio', SimpleNamespace(sleep=AsyncMock())) as internal:
            self.assertEqual(await self.process(job, 100), 'waiting')
            row = jobs.get_job(self.conn, job)
            self.assertEqual((row['reason'], row['retry_after']), ('rate_limited', 222))
            self.assertEqual(await self.process(job, 221), 'not_claimed')
            self.assertEqual(self.generate.await_count, 1)
            self.assertGreaterEqual(quota.status()['models'][self.MODEL]['cooldown_seconds'], 122)
            mono[0] += 122
            self.generate.side_effect = None
            self.generate.return_value = SimpleNamespace(text=json.dumps(
                dict(is_relevant=True, category='복지', target='영유아')))
            self.assertEqual(await self.process(job, 222), 'complete')
            internal.sleep.assert_not_awaited()
        self.assertEqual(self.generate.await_count, 2)
        self.assertEqual(quota.status()['recorded_requests_today'], 2)

    async def test_invalid_json_is_not_retried_or_cached(self):
        self.generate.return_value = SimpleNamespace(text='not-json')
        for recipe in self.runs:
            job = self.enqueue(recipe)
            self.assertEqual(await self.process(job), 'failed')
            self.assertEqual(jobs.get_job(self.conn, job)['reason'], 'invalid_analysis')
            self.assertEqual(await self.process(job, 10000), 'not_claimed')
        self.assertEqual(self.generate.await_count, 2)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_cache').fetchone()[0], 0)

    async def test_concurrent_claim_and_completed_job_do_not_repeat_sdk_call(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        async def generate(**kwargs):
            entered.set()
            await finish.wait()
            return SimpleNamespace(text=json.dumps(dict(is_relevant=True, category='복지', target='영유아')))
        self.generate.side_effect = generate
        job = self.enqueue(RECIPE)
        task = asyncio.create_task(self.process(job))
        try:
            await asyncio.wait_for(entered.wait(), 10)
            self.assertEqual(await self.process(job), 'not_claimed')
            finish.set()
            self.assertEqual(await asyncio.wait_for(task, 10), 'complete')
            self.assertEqual(await self.process(job, 10000), 'not_claimed')
            self.assertEqual(self.generate.await_count, 1)
            self.assertEqual(quota.status()['recorded_requests_today'], 1)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_direct_classifier_keeps_three_attempt_default(self):
        import ai_classifier
        self.generate.side_effect = RuntimeError('503 unavailable')
        with patch.object(ai_classifier, 'asyncio', SimpleNamespace(sleep=AsyncMock())) as internal:
            with self.assertRaises(RuntimeError):
                await ai_classifier._call_gemini_json(self.client, 'synthetic', '시험', model_override=self.MODEL)
            self.assertEqual(internal.sleep.await_count, 2)
        self.assertEqual(self.generate.await_count, 3)
        self.assertEqual(quota.status()['recorded_requests_today'], 3)

    async def test_invalid_attempt_budget_cannot_send_a_request(self):
        import ai_classifier
        for value in (0, 4, -1, True, 1.5, '1', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                await ai_classifier._call_gemini_json(self.client, 'synthetic', '시험', request_attempts=value)
        self.generate.assert_not_awaited()
