import asyncio
import sqlite3
import unittest
from unittest.mock import AsyncMock
from services import news_profile_store as profiles, news_analysis_store as jobs
from services.news_profile import NewsProfile
from services.news_candidate_intake import register_candidates
from services.news_semantic_gate import process_candidate, _locks


class TestSemanticGate(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        profiles.ensure_schema(self.conn)
        jobs.ensure_schema(self.conn)
        profiles.create_profile(self.conn, NewsProfile('p', 1, '노년', '전국', keywords=('지원',),
            interest_topics=('주거',), site_ids=('1',), semantic_text='노년층 돌봄 소식'))
        self.run = profiles.prepare_run(self.conn, 'p', expected_revision=1,
            sites=[dict(id=1, name='시험', url='https://example.invalid')])
        self.key = register_candidates(self.conn, self.run, '1',
            [dict(title='돌봄 소식', link='https://example.invalid', content='내용')], model='fixed')[0]['candidate_key']

    def tearDown(self):
        self.conn.close()
        _locks.clear()

    async def db(self, fn, *args, **kwargs):
        return fn(self.conn, *args, **kwargs)

    async def process(self, scorer):
        return await process_candidate(self.run, self.key, scorer=scorer, threshold=0.8,
            analysis_model='fixed', scoring_version='test-vector-v1', call_db=self.db)

    async def test_high_score_queues_once(self):
        scorer = AsyncMock(return_value=0.9)
        first = await self.process(scorer)
        second = await self.process(scorer)
        self.assertEqual(first, second)
        self.assertEqual(first['state'], 'semantic_matched')
        scorer.assert_awaited_once_with('노년층 돌봄 소식\n찾을 지원 분야: 주거', '돌봄 소식')
        self.assertEqual(jobs.get_job(self.conn, first['job_id'])['state'], 'queued')

    async def test_low_score_is_review_not_exclusion(self):
        result = await self.process(AsyncMock(return_value=0.2))
        self.assertEqual(result['state'], 'semantic_review')
        self.assertIsNone(result['job_id'])
        self.assertEqual(jobs.list_profile_results(self.conn, 'p')['items'][0]['state'], 'semantic_review')

    async def test_failure_and_invalid_scores_stay_pending(self):
        for scorer in (AsyncMock(side_effect=RuntimeError('429 private')), AsyncMock(return_value=None),
                       AsyncMock(return_value=float('nan')), AsyncMock(return_value=True)):
            result = await self.process(scorer)
            self.assertEqual(result['state'], 'semantic_candidate')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_jobs').fetchone()[0], 0)

    async def test_item_holds_are_persisted_once_without_automatic_retry(self):
        from services.ai_input_safety import AiInputReviewRequired
        from services.embedding_input_limits import EmbeddingInputLimitExceeded
        from services.ai_generation_safety import AiResponseReviewRequired
        for error, reason in ((AiInputReviewRequired(), 'ai_input_review_required'),
                              (EmbeddingInputLimitExceeded(), 'ai_input_size_review_required'),
                              (AiResponseReviewRequired('incomplete'), 'ai_response_review_required')):
            with self.subTest(reason=reason):
                self.key = register_candidates(self.conn, self.run, '1', [dict(title='공고 ' + reason,
                    link='https://example.invalid/' + reason, content='내용')], model='fixed')[0]['candidate_key']
                scorer = AsyncMock(side_effect=error)
                first = await self.process(scorer)
                self.assertEqual(await self.process(scorer), first)
                scorer.assert_awaited_once()
                self.assertEqual(first['reason'], reason)
                self.assertEqual(first['state'], 'semantic_review')
                job = jobs.get_job(self.conn, first['job_id'])
                self.assertEqual((job['state'], job['reason'], job['attempts']), ('failed', reason, 0))
                with self.assertRaises(ValueError):
                    jobs.resume_job(self.conn, first['job_id'], expected_attempts=0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_cache').fetchone()[0], 0)

    async def test_app_budget_reasons_remain_pending(self):
        from services.ai_coordinator import AiLocalBudgetExhausted, AiDailyQuotaExhausted
        for error, reason in ((AiLocalBudgetExhausted(), 'local_budget_exhausted'),
                              (AiDailyQuotaExhausted(), 'daily_quota_exhausted')):
            with self.subTest(reason=reason):
                result = await self.process(AsyncMock(side_effect=error))
                self.assertEqual(result['reason'], reason)
                self.assertEqual(result['state'], 'semantic_candidate')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_jobs').fetchone()[0], 0)

    async def test_request_token_limit_is_an_item_hold(self):
        from services.ai_coordinator import AiInputTokenBudgetExceeded
        scorer = AsyncMock(side_effect=AiInputTokenBudgetExceeded('size'))
        result = await self.process(scorer)
        self.assertEqual(result['state'], 'semantic_review')
        self.assertEqual(result['reason'], 'ai_input_size_review_required')
        self.assertEqual(await self.process(scorer), result)
        scorer.assert_awaited_once()

    async def test_hold_persistence_error_propagates_and_rolls_back(self):
        from services.ai_input_safety import AiInputReviewRequired
        self.conn.execute("""CREATE TRIGGER reject_hold BEFORE UPDATE ON news_run_candidates
            BEGIN SELECT RAISE(ABORT, 'test storage failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.process(AsyncMock(side_effect=AiInputReviewRequired()))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_jobs').fetchone()[0], 0)
        self.assertEqual(jobs.list_profile_results(self.conn, 'p')['items'][0]['state'], 'semantic_candidate')

    async def test_concurrent_calls_use_one_score(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        async def score(*args):
            entered.set()
            await finish.wait()
            return 0.9
        scorer = AsyncMock(side_effect=score)
        first = asyncio.create_task(self.process(scorer))
        await entered.wait()
        second = asyncio.create_task(self.process(scorer))
        finish.set()
        self.assertEqual(await first, await second)
        self.assertEqual(scorer.await_count, 1)

    async def test_cancelled_score_remains_pending(self):
        entered = asyncio.Event()
        async def score(*args):
            entered.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(self.process(score))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(jobs.list_profile_results(self.conn, 'p')['items'][0]['state'], 'semantic_candidate')
