"""Offline, synthetic checks for terminal logging and private-input redaction."""
import asyncio
import json
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

from services import app_lifecycle, news_profile_store as profiles, news_analysis_store as jobs
from services import news_profile_workflow as workflows, news_run_executor as executor
from services.news_profile import NewsProfile


def records(captured):
    return [json.loads(record.getMessage().split(' ', 1)[1]) for record in captured.records]


class WorkflowDiagnostics(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        profiles.ensure_schema(self.conn)
        jobs.ensure_schema(self.conn)
        profiles.create_profile(self.conn, NewsProfile('diagnostic', 1, 'Private condition', '전국',
                                                       site_ids=('1',)))
        self.run = profiles.prepare_run(self.conn, 'diagnostic', expected_revision=1,
            sites=[dict(id=1, name='Private site', url='https://example.invalid/private')])
        self.workflow = workflows.create_workflow(self.conn, self.run)

    async def db(self, fn, *args, **kwargs):
        return fn(self.conn, *args, **kwargs)

    async def run_workflow(self, **kwargs):
        return await workflows.run_workflow(self.workflow, client=object(), scorer=object(),
            analysis_model='not-logged-model', threshold=.8, call_db=kwargs.pop('call_db', self.db),
            **kwargs)

    async def test_complete_with_review_keeps_job_and_candidate_counts_distinct(self):
        for index in range(22):
            job = jobs.enqueue(self.conn, self.run, payload=dict(title='Private title',
                link=f'https://example.invalid/private/{index}', site_name='Private site',
                content='Private source'), model='private-model', recipe='private-recipe')
            token = jobs.claim(self.conn, job, now=0)
            if index < 19:
                jobs.defer(self.conn, job, token, retry_after=0,
                           reason='ai_input_size_review_required', failed=True)
            else:
                jobs.complete(self.conn, job, token, {'target_status': 'review'})

        async def collector(_run, **kwargs):
            await kwargs['progress']({'saved_candidates': 37})
            return {'state': 'finished', 'registered': 37, 'attachment_review': 13,
                    'site_issues': [{'site_name': 'Private site'}]}

        summary = dict(state='pass_finished', jobs_processed=22, remaining=0,
                       **executor._review_summary(self.conn, self.run))
        with self.assertLogs(workflows.logger, level='INFO') as captured:
            result = await self.run_workflow(collector=collector,
                executor=AsyncMock(return_value=summary))
        self.assertEqual(result['state'], 'complete')
        [record] = records(captured)
        self.assertEqual(record['outcome'], 'complete_with_review')
        self.assertEqual(record['analysis_jobs_review_required'], 19)
        self.assertEqual(record['analysis_job_review_reasons'], {'ai_input_size_review_required': 19})
        self.assertEqual(record['analysis_jobs_failed'], 19)
        self.assertEqual(record['candidate_analysis_total'], 22)
        self.assertEqual(record['candidate_analysis_processed'], 22)
        self.assertEqual(record['candidate_analysis_complete'], 3)
        self.assertEqual(record['candidate_analysis_remaining'], 0)
        self.assertEqual(record['candidate_saved_candidates'], 37)
        self.assertNotIn('Private', str(captured.output))
        self.assertNotIn('attachment', str(captured.output))

    async def test_crash_logs_safe_code_even_when_failure_cannot_be_persisted(self):
        async def fail_transition(fn, *args, **kwargs):
            if fn is workflows._transition and args[1] == 'failed':
                raise OSError('PRIVATE database path')
            return await self.db(fn, *args, **kwargs)

        with self.assertLogs(workflows.logger, level='ERROR') as captured:
            with self.assertRaisesRegex(RuntimeError, 'PRIVATE provider source'):
                await self.run_workflow(call_db=fail_transition,
                    collector=AsyncMock(side_effect=RuntimeError('PRIVATE provider source')))
        [record] = records(captured)
        self.assertEqual(record['outcome'], 'workflow_exception')
        self.assertEqual(record['error_code'], 'workflow_failed')
        self.assertFalse(record['state_persisted'])
        self.assertNotIn('PRIVATE', str(captured.output))
        self.assertTrue(all(item.exc_info is None for item in captured.records))
        self.assertEqual(app_lifecycle.active_profile_jobs, 0)

    async def test_waiting_and_cancelled_workflows_have_terminal_records(self):
        with self.assertLogs(workflows.logger, level='INFO') as captured:
            result = await self.run_workflow(collector=AsyncMock(return_value={'state': 'busy'}))
        self.assertEqual(result['state'], 'waiting')
        self.assertEqual(records(captured)[0]['outcome'], 'waiting')
        entered = asyncio.Event()

        async def collector(*_args, **_kwargs):
            entered.set()
            await asyncio.Event().wait()

        with self.assertLogs(workflows.logger, level='INFO') as captured:
            task = asyncio.create_task(self.run_workflow(collector=collector))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(records(captured)[0]['state'], 'cancelled')

    def test_terminal_log_ignores_unknown_text_and_non_integer_counts(self):
        private = 'PRIVATE credential https://example.invalid/body'
        with self.assertLogs(workflows.logger, level='INFO') as captured:
            workflows._log_terminal(dict(workflow_id=private, run_id=private,
                state='complete', stage=private, error_code=private,
                summary={'analysis': {'review_reasons': {private: 100,
                    'ai_input_size_review_required': 2, 'ai_input_review_required': True},
                    'failed_jobs': private}, 'collection': {'raw': private}},
                progress={'analysis_total': private, 'analysis_remaining': True,
                          'site_name': private}))
        [record] = records(captured)
        self.assertEqual(record['analysis_jobs_review_required'], 2)
        self.assertEqual(record['analysis_job_review_reasons'], {'ai_input_size_review_required': 2})
        self.assertEqual(record['workflow_id'], 'unavailable')
        self.assertNotIn('candidate_analysis_remaining', record)
        self.assertNotIn('PRIVATE', str(captured.output))

    def test_job_log_keeps_recognized_recipe_model_and_only_source_length(self):
        from services.ai_rate_policy import MODELS
        from services.news_custom_analysis import RECIPE
        source = 'PRIVATE source and credential'
        job = dict(run_id=self.run, job_id='a' * 32, model=MODELS[0], recipe=RECIPE,
                   reason='ai_input_size_review_required',
                   input_json=json.dumps(dict(content=source, title='PRIVATE title')))
        with self.assertLogs(executor.logger, level='INFO') as captured:
            executor._log_job_failure(job)
            executor._log_job_failure(dict(job, model=source, recipe=source, reason=source))
        first, second = records(captured)
        self.assertEqual(first['model'], MODELS[0])
        self.assertEqual(first['recipe'], RECIPE)
        self.assertEqual(first['source_chars'], len(source))
        self.assertEqual(second['model'], 'unrecognized')
        self.assertEqual(second['recipe'], 'unrecognized')
        self.assertEqual(second['reason'], 'unspecified')
        self.assertNotIn('PRIVATE', str(captured.output))

    async def test_executor_records_a_persisted_hold_without_stopping_the_run(self):
        from services.ai_rate_policy import MODELS
        from services.news_custom_analysis import RECIPE
        from services.news_direct_analysis import DirectAnalysis
        job = jobs.enqueue(self.conn, self.run, payload=dict(title='PRIVATE title',
            link='https://example.invalid/private', site_name='PRIVATE site', content='PRIVATE source'),
            model=MODELS[0], recipe=RECIPE)

        async def hold(job_id, _client, **_kwargs):
            token = jobs.claim(self.conn, job_id, now=0)
            jobs.defer(self.conn, job_id, token, retry_after=0,
                       reason='ai_input_size_review_required', failed=True)
            return 'failed'

        with patch.object(executor, 'process_job', side_effect=hold), \
             patch.object(executor.ai_coordinator, 'try_start_job', new_callable=AsyncMock, return_value=True), \
             patch.object(executor.ai_coordinator, 'finish_job'), \
             patch('services.policy_sync.try_acquire_maintenance_lock', new_callable=AsyncMock, return_value=True), \
             patch('services.policy_sync.release_maintenance_lock'), \
             self.assertLogs(executor.logger, level='INFO') as captured:
            summary = await executor.execute_registered_run(self.run, client=object(),
                scorer=DirectAnalysis(), analysis_model=MODELS[0], threshold=None, call_db=self.db)
        self.assertEqual(summary['completion_status'], 'complete_with_review')
        self.assertFalse(summary['work_remaining'])
        [record] = records(captured)
        self.assertEqual(record['job_id'], job)
        self.assertEqual(record['recipe'], RECIPE)
        self.assertEqual(record['reason'], 'ai_input_size_review_required')
        self.assertNotIn('PRIVATE', str(captured.output))

    async def test_logging_error_cannot_change_successful_outcome(self):
        with patch.object(workflows.logger, 'info', side_effect=OSError('log unavailable')):
            result = await self.run_workflow(
                collector=AsyncMock(return_value={'state': 'finished', 'analysis_required': False}))
        self.assertEqual(result['state'], 'complete')
