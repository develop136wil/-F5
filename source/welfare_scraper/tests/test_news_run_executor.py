import asyncio
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from services.news_profile import NewsProfile
from services import news_profile_store as profiles, news_analysis_store as jobs
from services.news_candidate_intake import register_candidates
from services.news_semantic_scorer import SemanticScorer, EmbeddingRecipe
from services.news_run_executor import execute_registered_run
from services.news_direct_analysis import DirectAnalysis


class TestRunExecutor(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # This workflow fixture uses model='m' and a mocked generation boundary.
        # Keep the real 6000-unit app ceiling; short requests still pass through
        # v19's local full-request sizing instead of reading operational policy.
        patch('services.ai_coordinator.input_token_budget', return_value=6000).start()
        patch('services.ai_coordinator.input_token_capacity', return_value=6000).start()
        self.conn = sqlite3.connect(':memory:')
        profiles.ensure_schema(self.conn)
        jobs.ensure_schema(self.conn)
        profiles.create_profile(self.conn, NewsProfile('p', 1, '시험', '전국', site_ids=('1',),
            keywords=('지원',), semantic_text='돌봄 소식'))
        self.run = profiles.prepare_run(self.conn, 'p', expected_revision=1,
            sites=[dict(id=1, name='시험', url='https://example.invalid')])
        self.job = register_candidates(self.conn, self.run, '1',
            [dict(title='지원', link='https://example.invalid', content='본문')], model='m')[0]['job_id']
        self.scorer = SemanticScorer(object(), EmbeddingRecipe('embedding', dimensions=2))
        self.start = patch('services.news_run_executor.ai_coordinator.try_start_job', new_callable=AsyncMock, return_value=True).start()
        self.finish = patch('services.news_run_executor.ai_coordinator.finish_job').start()
        self.acquire = patch('services.policy_sync.try_acquire_maintenance_lock', new_callable=AsyncMock, return_value=True).start()
        self.release = patch('services.policy_sync.release_maintenance_lock').start()

    def tearDown(self):
        patch.stopall()
        self.conn.close()

    async def db(self, fn, *args, **kwargs):
        return fn(self.conn, *args, **kwargs)

    async def execute(self, **kwargs):
        return await execute_registered_run(self.run, client=object(), scorer=self.scorer,
            analysis_model='m', threshold=kwargs.get('threshold', 0.8), call_db=self.db)

    async def test_busy_does_not_touch_jobs_or_release_other_ai_job(self):
        self.start.return_value = False
        self.assertEqual(await self.execute(), {'state': 'busy'})
        self.finish.assert_not_called()
        self.release.assert_called_once()
        self.assertEqual(jobs.get_job(self.conn, self.job)['state'], 'queued')

    async def test_success_runs_registered_job_and_releases_both_locks(self):
        result = dict(is_relevant=True, category='복지', target='일반', residence=[], life_cycle=[],
                      target_characteristics=[], interest_topics=[], start_age_months=None,
                      end_age_months=None, evidence_snippet='',
                      applicant_type='unknown', applicant_detail='미상', beneficiary_detail='미상',
                      applicant_evidence='', life_cycle_evidence=[], residence_evidence=[],
                      _support_source_units=[1], eligibility_detail='', contact='', deadline='',
                      fact_evidence={key: '' for key in ('eligibility_detail', 'contact', 'deadline', 'is_relevant')})
        with patch('ai_classifier._call_gemini_json', new_callable=AsyncMock, return_value=result):
            summary = await self.execute()
        self.assertEqual(summary['jobs_processed'], 1)
        self.assertEqual(jobs.get_job(self.conn, self.job)['state'], 'complete')
        self.finish.assert_called_once()
        self.release.assert_called_once()

    async def test_resume_cannot_change_threshold(self):
        with patch('services.news_run_executor.process_job', new_callable=AsyncMock, return_value='waiting'):
            await self.execute()
        with self.assertRaises(ValueError):
            await self.execute(threshold=0.7)
        self.assertEqual(self.release.call_count, 2)

    async def test_cancelled_worker_releases_admission(self):
        entered = asyncio.Event()
        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        with patch('services.news_run_executor.process_job', side_effect=blocked):
            task = asyncio.create_task(self.execute())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.finish.assert_called_once()
        self.release.assert_called_once()

    async def test_future_retry_keeps_pass_waiting(self):
        self.conn.execute("UPDATE news_analysis_jobs SET state='waiting',retry_after=? WHERE job_id=?", (10**12, self.job))
        summary = await self.execute()
        self.assertTrue(summary['work_remaining'])
        self.assertEqual(summary['stop_reason'], 'scheduled_wait')

    async def test_terminal_job_failure_is_not_reported_as_complete(self):
        with patch('services.news_run_executor.process_job', new_callable=AsyncMock, return_value='failed'):
            summary = await self.execute()
        self.assertEqual(summary['state'], 'failed')
        self.assertFalse(summary['work_remaining'])
        self.assertEqual(summary['stop_reason'], 'analysis_failed')

    async def test_direct_mode_internal_pagination_does_not_hardcode_ten(self):
        register_candidates(self.conn, self.run, '1', [dict(title=f'공고 {i}',
            link=f'https://example.invalid/{i}', content='본문') for i in range(22)], model='m')
        async def finish_job(job_id, client, **kwargs):
            token = jobs.claim(self.conn, job_id, now=0)
            jobs.complete(self.conn, job_id, token, {'target_status':'review'})
            return 'complete'
        with patch('services.news_run_executor.process_with_embeddings', new_callable=AsyncMock) as embeddings, \
             patch('services.news_run_executor.process_job', side_effect=finish_job) as worker:
            for expected_remaining in (3, 0):
                summary = await execute_registered_run(self.run, client=object(), scorer=DirectAnalysis(),
                    analysis_model='m', threshold=None, call_db=self.db, limit=20)
                self.assertLessEqual(summary['jobs_processed'], 20)
                self.assertEqual(summary['remaining'], expected_remaining)
                if expected_remaining:
                    self.assertEqual(summary['stop_reason'], 'batch_limit')
            self.assertEqual(worker.call_count, 23)
            embeddings.assert_not_called()
        self.assertEqual(len(jobs.list_profile_results(self.conn, 'p')['items']), 23)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM news_run_candidates WHERE job_id IS NULL").fetchone()[0], 0)

    async def test_direct_mode_stops_on_quota_wait_and_cannot_switch_on_resume(self):
        with patch('services.news_run_executor.process_job', new_callable=AsyncMock, return_value='waiting') as worker:
            summary = await execute_registered_run(self.run, client=object(), scorer=DirectAnalysis(),
                analysis_model='m', threshold=None, call_db=self.db)
            self.assertEqual(summary['stop_reason'], 'waiting')
            worker.assert_awaited_once()
        with self.assertRaises(ValueError):
            await self.execute()

    async def test_direct_mode_does_not_accept_fake_threshold(self):
        with self.assertRaises(ValueError):
            await execute_registered_run(self.run, client=object(), scorer=DirectAnalysis(),
                analysis_model='m', threshold=0, call_db=self.db)

    async def test_daily_quota_failure_preserves_recovery_reason(self):
        async def fail(job_id, client, **kwargs):
            token = jobs.claim(self.conn, job_id, now=0)
            jobs.defer(self.conn, job_id, token, retry_after=0, reason='daily_quota_exhausted', failed=True)
            return 'failed'
        with patch('services.news_run_executor.process_job', side_effect=fail):
            summary = await self.execute()
        self.assertEqual(summary['state'], 'pass_finished')
        self.assertEqual(summary['stop_reason'], 'daily_quota_exhausted')
        self.assertEqual(summary['remaining'], 1)
        self.assertTrue(summary['recovery_required'])

    async def test_preexisting_failed_job_cannot_be_skipped_as_complete(self):
        token = jobs.claim(self.conn,self.job,now=0)
        jobs.defer(self.conn,self.job,token,retry_after=0,reason='invalid_analysis',failed=True)
        with patch('services.news_run_executor.process_job',new_callable=AsyncMock) as worker:
            summary = await self.execute()
            worker.assert_not_awaited()
        self.assertEqual(summary['state'],'failed')
        self.assertEqual(summary['failed_jobs'],1)

    async def test_private_input_failure_keeps_specific_reason_without_retry(self):
        from services.ai_input_safety import AiInputReviewRequired
        with patch('ai_classifier._call_gemini_json', new_callable=AsyncMock,
                   side_effect=AiInputReviewRequired()) as provider:
            first = await self.execute()
            second = await self.execute()
        self.assertEqual(provider.await_count, 1)
        for result in (first, second):
            self.assertEqual(result['state'], 'pass_finished')
            self.assertFalse(result['work_remaining'])
            self.assertEqual(result['completion_status'], 'complete_with_review')
            self.assertEqual(result['review_reasons'], {'ai_input_review_required': 1})
            self.assertEqual(result['remaining'], 0)

    async def test_private_semantic_input_is_saved_and_other_candidates_continue(self):
        register_candidates(self.conn, self.run, '1', [
            dict(title='담당자 010-1234-5678', link='https://example.invalid/private', content='본문'),
            dict(title='일반 공고', link='https://example.invalid/safe', content='본문')], model='m')
        with patch.object(SemanticScorer, '__call__', new_callable=AsyncMock, return_value=.9) as scorer, \
             patch('services.news_run_executor.process_job', side_effect=self.finish_job) as worker:
            first = await self.execute()
            second = await self.execute()
        self.assertEqual(scorer.await_count, 1)
        self.assertEqual(worker.await_count, 2)
        self.assertEqual(first['semantic_processed'], 2)
        self.assertEqual(second['semantic_processed'], 0)
        for result in (first, second):
            self.assertEqual(result['completion_status'], 'complete_with_review')
            self.assertEqual(result['review_required_count'], 1)
            self.assertEqual(result['remaining'], 0)

    async def test_local_budget_job_can_resume_without_resetting_attempt_history(self):
        from services.ai_coordinator import AiLocalBudgetExhausted
        with patch('ai_classifier._call_gemini_json', new_callable=AsyncMock,
                   side_effect=AiLocalBudgetExhausted()):
            result = await self.execute()
        self.assertTrue(result['work_remaining'])
        self.assertTrue(result['recovery_required'])
        self.assertEqual(result['stop_reason'], 'local_budget_exhausted')
        job = jobs.get_job(self.conn, self.job)
        jobs.resume_job(self.conn, self.job, expected_attempts=job['attempts'])
        resumed = jobs.get_job(self.conn, self.job)
        self.assertEqual(resumed['state'], 'queued')
        self.assertEqual(resumed['attempts'], job['attempts'])

    async def test_incomplete_response_is_manual_review_and_not_retried_on_next_pass(self):
        from services.ai_generation_safety import AiResponseReviewRequired
        with patch('ai_classifier._call_gemini_json', new_callable=AsyncMock,
                   side_effect=AiResponseReviewRequired('output_limit')) as model:
            first = await self.execute()
            second = await self.execute()
        self.assertEqual(model.await_count, 1)
        for summary in (first, second):
            self.assertEqual(summary['state'], 'pass_finished')
            self.assertEqual(summary['review_reasons'], {'ai_response_review_required': 1})
            self.assertEqual(summary['completion_status'], 'complete_with_review')
            self.assertFalse(summary['work_remaining'])
        self.assertEqual(jobs.get_job(self.conn, self.job)['reason'], 'ai_response_review_required')

    async def finish_job(self, job_id, _client, **_kwargs):
        token = jobs.claim(self.conn, job_id, now=0)
        jobs.complete(self.conn, job_id, token, {'target_status': 'review'})
        return 'complete'

    async def test_size_hold_in_semantic_phase_does_not_block_queued_ai_jobs(self):
        from services.embedding_input_limits import EmbeddingInputLimitExceeded
        register_candidates(self.conn, self.run, '1', [dict(title='일반 공고',
            link='https://example.invalid/large', content='본문')], model='m')
        with patch.object(SemanticScorer, 'review_inputs', side_effect=EmbeddingInputLimitExceeded()), \
             patch('services.news_run_executor.process_job', side_effect=self.finish_job) as worker:
            summary = await self.execute()
        worker.assert_awaited_once()
        self.assertEqual(summary['completion_status'], 'complete_with_review')
        self.assertEqual(summary['review_reasons'], {'ai_input_size_review_required': 1})
        self.assertEqual(summary['remaining'], 0)
        self.assertFalse(summary['work_remaining'])

    async def test_all_four_item_holds_continue_to_a_later_success(self):
        from services.ai_input_safety import AiInputReviewRequired
        from services.ai_generation_safety import AiResponseReviewRequired
        from services.ai_coordinator import AiInputTokenBudgetExceeded
        from services.news_source_selection import SourceInputReviewRequired
        register_candidates(self.conn, self.run, '1', [dict(title=f'지원 {index}',
            link=f'https://example.invalid/{index}', content='본문') for index in range(4)], model='m')
        from services.news_analysis_worker import process_job
        errors = [AiInputReviewRequired(), AiInputTokenBudgetExceeded('size'),
                  AiResponseReviewRequired('output_limit'), SourceInputReviewRequired('source format')]
        async def process(job_id, client, **kwargs):
            if errors:
                with patch('ai_classifier._call_gemini_json', new_callable=AsyncMock, side_effect=errors.pop(0)):
                    return await process_job(job_id, client, **kwargs)
            return await self.finish_job(job_id, client)
        with patch('services.news_run_executor.process_job', side_effect=process) as worker:
            first = await self.execute()
            second = await self.execute()
        self.assertEqual(worker.await_count, 5)
        self.assertEqual(first['jobs_processed'], 5)
        self.assertEqual(second['jobs_processed'], 0)
        self.assertEqual(first['review_required_count'], 4)
        self.assertEqual(second['review_required_count'], 4)
        self.assertEqual(second['review_reasons']['ai_source_review_required'], 1)
        self.assertEqual(second['remaining'], 0)
        self.assertEqual(second['completion_status'], 'complete_with_review')
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM news_analysis_jobs WHERE state='complete'").fetchone()[0], 1)

    async def test_preexisting_manual_hold_does_not_block_later_jobs_or_quota_recovery(self):
        token = jobs.claim(self.conn, self.job, now=0)
        jobs.defer(self.conn, self.job, token, retry_after=0, reason='ai_input_review_required', failed=True)
        later = register_candidates(self.conn, self.run, '1', [dict(title='지원 추가',
            link='https://example.invalid/later', content='본문')], model='m')[0]['job_id']
        async def fail(job_id, _client, **_kwargs):
            token = jobs.claim(self.conn, job_id, now=0)
            jobs.defer(self.conn, job_id, token, retry_after=0, reason='daily_quota_exhausted', failed=True)
            return 'failed'
        with patch('services.news_run_executor.process_job', side_effect=fail) as worker:
            first = await self.execute()
            second = await self.execute()
        worker.assert_awaited_once()
        self.assertEqual(worker.call_args.args[0], later)
        for result in (first, second):
            self.assertEqual(result['stop_reason'], 'daily_quota_exhausted')
            self.assertTrue(result['recovery_required'])
            self.assertEqual(result['review_required_count'], 1)
            self.assertEqual(result['remaining'], 1)

    async def test_semantic_quota_stop_does_not_start_pending_body_jobs(self):
        from services.ai_coordinator import AiDailyQuotaExhausted
        register_candidates(self.conn, self.run, '1', [dict(title='일반 공고',
            link='https://example.invalid/quota', content='본문')], model='m')
        with patch.object(SemanticScorer, '__call__', new_callable=AsyncMock,
                          side_effect=AiDailyQuotaExhausted()), \
             patch('services.news_run_executor.process_job', new_callable=AsyncMock) as worker:
            result = await self.execute()
        worker.assert_not_awaited()
        self.assertEqual(result['stop_reason'], 'daily_quota_exhausted')
        self.assertTrue(result['work_remaining'])
        self.assertEqual(result['remaining'], 2)
