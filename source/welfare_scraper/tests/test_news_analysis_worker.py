import asyncio
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

from services.news_profile import capture_legacy_profile
from services import news_profile_store as profiles, news_analysis_store as jobs
from services.news_analysis_worker import process_job, RECIPE


def tearDownModule():
    import database
    database.close_all_connections()


class TestNewsWorker(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        profiles.ensure_schema(self.conn)
        jobs.ensure_schema(self.conn)
        profile = capture_legacy_profile(region='도봉', max_age_months=108,
            keywords=['영유아'], excluded_keywords=[], site_ids=['1'], ai_prompt='고정 지침',
            semantic_text='원래 문장', relevance_enabled=True)
        profiles.initialize_legacy(self.conn, profile)
        run = profiles.prepare_run(self.conn, profile.profile_id, expected_revision=1,
            sites=[dict(id=1, name='테스트', url='https://example.invalid')])
        self.job = jobs.enqueue(self.conn, run, payload=dict(title='제목', link='https://example.invalid',
            site_name='사이트', content='내용'), model='pinned-model', recipe=RECIPE)

    def tearDown(self):
        self.conn.close()

    async def call_db(self, fn, *args, **kwargs):
        return fn(self.conn, *args, **kwargs)

    async def test_success_passes_fixed_model_and_strict_mode(self):
        with patch('ai_classifier.classify_post_async', new_callable=AsyncMock,
                   return_value=dict(is_relevant=True, category='복지', target='영유아')) as ai:
            self.assertEqual(await process_job(self.job, object(), call_db=self.call_db), 'complete')
        self.assertTrue(ai.call_args.kwargs['strict'])
        self.assertEqual(ai.call_args.kwargs['model_override'], 'pinned-model')
        self.assertEqual(ai.call_args.kwargs['custom_prompt'], '고정 지침')
        self.assertEqual(jobs.get_job(self.conn, self.job)['state'], 'complete')

    async def test_invalid_output_is_not_cached(self):
        with patch('ai_classifier.classify_post_async', new_callable=AsyncMock, return_value={}):
            self.assertEqual(await process_job(self.job, object(), call_db=self.call_db), 'failed')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_cache').fetchone()[0], 0)

    async def test_old_queued_recipe_still_runs_with_its_original_input_strategy(self):
        from services.news_analysis_worker import OLD_RECIPE
        self.conn.execute('UPDATE news_analysis_jobs SET recipe=? WHERE job_id=?', (OLD_RECIPE, self.job))
        self.conn.commit()
        with patch('ai_classifier.classify_post_async', new_callable=AsyncMock,
                   return_value=dict(is_relevant=True, category='복지', target='영유아')) as ai:
            self.assertEqual(await process_job(self.job, object(), call_db=self.call_db), 'complete')
        self.assertEqual(ai.call_args.kwargs['input_strategy'], 'prefix')

    async def test_reversed_age_output_is_not_cached(self):
        with patch('ai_classifier.classify_post_async', new_callable=AsyncMock, return_value={
            'is_relevant': True, 'category': '복지', 'target': '아동',
            'start_age_months': 120, 'end_age_months': 12}):
            self.assertEqual(await process_job(self.job, object(), call_db=self.call_db), 'failed')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_cache').fetchone()[0], 0)

    async def test_api_error_becomes_waiting_without_raw_error(self):
        with patch('ai_classifier.classify_post_async', new_callable=AsyncMock,
                   side_effect=RuntimeError('429 secret-request')):
            self.assertEqual(await process_job(self.job, object(), call_db=self.call_db, clock=lambda: 100), 'waiting')
        row = jobs.get_job(self.conn, self.job)
        self.assertEqual(row['retry_after'], 160)
        self.assertEqual(row['reason'], 'rate_limited')

    async def test_repeated_failure_stops_after_five_worker_attempts(self):
        with patch('ai_classifier.classify_post_async', new_callable=AsyncMock,
                   side_effect=RuntimeError('503 unavailable')) as ai:
            for attempt in range(5):
                state = await process_job(self.job, object(), call_db=self.call_db,
                                          clock=lambda: attempt * 1000)
                self.assertEqual(state, 'failed' if attempt == 4 else 'waiting')
            self.assertEqual(await process_job(self.job, object(), call_db=self.call_db,
                                              clock=lambda: 10000), 'not_claimed')
            self.assertEqual(ai.await_count, 5)
        self.assertEqual(jobs.get_job(self.conn, self.job)['reason'], 'retry_limit_reached')

    async def test_daily_quota_stops_without_caching(self):
        with patch('ai_classifier.classify_post_async', new_callable=AsyncMock,
                   side_effect=RuntimeError('429 requests per day quota')):
            self.assertEqual(await process_job(self.job, object(), call_db=self.call_db), 'failed')
        self.assertEqual(jobs.get_job(self.conn, self.job)['reason'], 'daily_quota_exhausted')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_cache').fetchone()[0], 0)

    async def test_local_capacity_waits_do_not_spend_or_reset_provider_failure_budget(self):
        from services.ai_coordinator import AiTemporaryRateLimit
        errors = [TimeoutError(), *[AiTemporaryRateLimit(60) for _ in range(6)],
                  *[TimeoutError() for _ in range(4)]]
        with patch('ai_classifier.classify_post_async', new_callable=AsyncMock, side_effect=errors):
            for attempt in range(len(errors)):
                outcome = await process_job(self.job, object(), call_db=self.call_db,
                                            clock=lambda: attempt * 1000)
                self.assertEqual(outcome, 'failed' if attempt == len(errors) - 1 else 'waiting')
        saved = jobs.get_job(self.conn, self.job)
        self.assertEqual(saved['attempts'], 11)
        self.assertEqual(saved['retry_start_attempt'], 6)
        self.assertEqual(saved['reason'], 'retry_limit_reached')

    async def test_persisted_daily_quota_does_not_become_generic_retry(self):
        from services.ai_coordinator import AiDailyQuotaExhausted
        with patch('ai_classifier.classify_post_async', new_callable=AsyncMock,
                   side_effect=AiDailyQuotaExhausted('오늘 AI 한도가 소진되어 요청을 보류합니다.')) as ai:
            self.assertEqual(await process_job(self.job, object(), call_db=self.call_db), 'failed')
            self.assertEqual(await process_job(self.job, object(), call_db=self.call_db), 'not_claimed')
            self.assertEqual(ai.await_count, 1)
        self.assertEqual(jobs.get_job(self.conn, self.job)['reason'], 'daily_quota_exhausted')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_cache').fetchone()[0], 0)

    async def test_cancelled_ai_releases_claim(self):
        entered = asyncio.Event()
        async def wait(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        with patch('ai_classifier.classify_post_async', side_effect=wait):
            task = asyncio.create_task(process_job(self.job, object(), call_db=self.call_db))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(jobs.get_job(self.conn, self.job)['state'], 'waiting')

    async def test_strict_classifier_does_not_hide_provider_error(self):
        import ai_classifier
        with patch.object(ai_classifier, '_call_gemini_json', side_effect=RuntimeError('provider')):
            with self.assertRaises(RuntimeError):
                await ai_classifier.classify_post_async(object(), 'title', 'url',
                                                       custom_prompt='', strict=True)
            legacy = await ai_classifier.classify_post_async(object(), 'title', 'url', custom_prompt='')
            self.assertEqual(legacy['analysis_status'], 'failed')
            self.assertEqual(legacy['analysis_failure']['reason'], 'analysis_error')
            self.assertNotIn('category', legacy)
            self.assertNotIn('is_relevant', legacy)

    async def test_cancellation_during_claim_is_drained_before_release(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        async def delayed(fn, *args, **kwargs):
            value = fn(self.conn, *args, **kwargs)
            if fn is jobs.claim:
                entered.set()
                await finish.wait()
            return value
        task = asyncio.create_task(process_job(self.job, object(), call_db=delayed))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(jobs.get_job(self.conn, self.job)['state'], 'waiting')

    async def test_cancellation_during_commit_keeps_success(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        async def delayed(fn, *args, **kwargs):
            value = fn(self.conn, *args, **kwargs)
            if fn is jobs.complete:
                entered.set()
                await finish.wait()
            return value
        with patch('ai_classifier.classify_post_async', new_callable=AsyncMock,
                   return_value=dict(is_relevant=True, category='복지', target='영유아')):
            task = asyncio.create_task(process_job(self.job, object(), call_db=delayed))
            await entered.wait()
            task.cancel()
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(jobs.get_job(self.conn, self.job)['state'], 'complete')
