import asyncio
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch
from services import app_lifecycle, news_profile_store as profiles, news_analysis_store as jobs
from services.news_profile import NewsProfile
from services.news_profile_workflow import (
    collect_profile_sources, create_workflow, get_workflow, run_workflow)


class TestProfileWorkflow(unittest.IsolatedAsyncioTestCase):
    async def test_unusable_source_warning_survives_completion_without_retry_loop(self):
        site = {'state': 'finished', 'sites': 1, 'registered': 0, 'saved_total': 0,
                'content_failed': 0, 'skipped_candidates': 2, 'vision_review_sites': 1}
        site_collector = AsyncMock(return_value=site)
        summary = await collect_profile_sources(self.run, analysis_model='m', call_db=self.db,
            site_collector=site_collector, bokjiro_collector=AsyncMock())
        self.assertEqual(summary['skipped_candidates'], 2)
        self.assertEqual(summary['sources']['sites']['skipped_candidates'], 2)
        self.assertEqual(summary['vision_review_sites'], 1)
        collector = AsyncMock(return_value=summary)
        executor = AsyncMock(return_value={'state': 'pass_finished', 'jobs_processed': 0})
        result = await run_workflow(self.workflow, client=object(), scorer=object(), analysis_model='m',
            threshold=.8, call_db=self.db, collector=collector, executor=executor)
        self.assertEqual(result['state'], 'complete')
        self.assertFalse(result['can_resume'])
        self.assertEqual(result['summary']['collection']['skipped_candidates'], 2)
        self.assertEqual(result['summary']['collection']['vision_review_sites'], 1)
        collector.assert_awaited_once(); executor.assert_awaited_once()
        reused = await collect_profile_sources(self.run, analysis_model='m', call_db=self.db,
            site_collector=site_collector, previous_collection=summary, bokjiro_collector=AsyncMock())
        self.assertEqual(reused['skipped_candidates'], 2)
        self.assertEqual(reused['vision_review_sites'], 1)
        site_collector.assert_awaited_once()

    async def test_site_save_failure_waits_before_other_sources_or_ai(self):
        site = {'state':'waiting','stop_reason':'site_save_failed',
                'sites':0,'registered':20,'content_failed':0,'saved_total':20}
        summary = await collect_profile_sources(self.run, analysis_model='m', call_db=self.db,
            site_collector=AsyncMock(return_value=site), bokjiro_collector=AsyncMock())
        self.assertEqual(summary['sources']['sites'], site)
        self.assertEqual(summary['state'], 'waiting')
        executor = AsyncMock()
        result = await run_workflow(self.workflow, client=None, scorer=None, analysis_model='m',
            threshold=None, call_db=self.db, collector=AsyncMock(return_value=summary), executor=executor)
        self.assertEqual((result['state'], result['stage'], result['error_code']),
                         ('waiting', 'collecting', 'site_save_failed'))
        self.assertTrue(result['can_resume'])
        executor.assert_not_awaited()

    async def test_site_review_does_not_block_analysis_or_create_resume_loop(self):
        issue = dict(site_id='1', site_name='시험', issue_code='site_partial')
        site = dict(state='finished', sites=1, registered=3, content_failed=0,
                    site_review_required=1, site_failed=0, site_partial=1,
                    site_issues=[issue], vision_review_sites=1)
        site_collector = AsyncMock(return_value=site)
        summary = await collect_profile_sources(self.run, analysis_model='m', call_db=self.db,
            site_collector=site_collector, bokjiro_collector=AsyncMock())
        executor = AsyncMock(return_value={'state': 'pass_finished', 'jobs_processed': 3})
        result = await run_workflow(self.workflow, client=object(), scorer=object(), analysis_model='m',
            threshold=.8, call_db=self.db, collector=AsyncMock(return_value=summary), executor=executor)
        self.assertEqual(result['state'], 'complete')
        self.assertFalse(result['can_resume'])
        self.assertEqual(result['summary']['collection']['site_issues'], [issue])
        self.assertEqual(result['summary']['collection']['site_review_required'], 1)
        self.assertEqual(result['summary']['collection']['vision_review_sites'], 1)
        executor.assert_awaited_once()
        reused = await collect_profile_sources(self.run, analysis_model='m', call_db=self.db,
            site_collector=site_collector, previous_collection=summary, bokjiro_collector=AsyncMock())
        self.assertEqual(reused['site_issues'], [issue])
        site_collector.assert_awaited_once()

    async def test_collection_page_windows_continue_before_analysis(self):
        collector = AsyncMock(side_effect=[
            {'state':'more_pages','pages_processed':5,'registered':45},
            {'state':'finished','registered':54,'analysis_required':False}])
        executor = AsyncMock()
        result = await run_workflow(self.workflow, client=None, scorer=None, analysis_model='not-used',
            threshold=0, call_db=self.db, collector=collector, executor=executor)
        self.assertEqual((result['state'], result['summary']['collection']['registered']), ('complete', 54))
        self.assertEqual(collector.await_count, 2)
        self.assertEqual(collector.call_args_list[1].kwargs['previous_collection']['registered'], 45)
        executor.assert_not_awaited()

    async def test_failed_collection_page_stays_collecting_and_can_resume(self):
        failed = {'state':'waiting','stop_reason':'bokjiro_page_failed','registered':9,'pages_processed':1}
        executor = AsyncMock()
        result = await run_workflow(self.workflow, client=None, scorer=None, analysis_model='not-used',
            threshold=0, call_db=self.db, collector=AsyncMock(return_value=failed), executor=executor)
        self.assertEqual((result['state'], result['stage'], result['error_code']),
                         ('waiting', 'collecting', 'bokjiro_page_failed'))
        self.assertTrue(result['can_resume'])
        executor.assert_not_awaited()
        collector = AsyncMock(return_value={'state':'finished','registered':18,'analysis_required':False})
        result = await run_workflow(self.workflow, client=None, scorer=None, analysis_model='not-used',
            threshold=0, call_db=self.db, collector=collector, executor=executor)
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(collector.call_args.kwargs['previous_collection'], failed)

    async def test_no_progress_cannot_spin_collection_forever(self):
        collector = AsyncMock(return_value={'state':'more_pages','pages_processed':0})
        executor = AsyncMock()
        result = await run_workflow(self.workflow, client=None, scorer=None, analysis_model='not-used',
            threshold=0, call_db=self.db, collector=collector, executor=executor)
        self.assertEqual(result['state'], 'waiting')
        collector.assert_awaited_once()
        executor.assert_not_awaited()

    async def test_internal_batches_continue_without_user_restart(self):
        collector = AsyncMock(return_value={'state':'finished','registered':53})
        executor = AsyncMock(side_effect=[
            {'state':'pass_finished','jobs_processed':50,'remaining':3,'work_remaining':True,'stop_reason':'batch_limit'},
            {'state':'pass_finished','jobs_processed':3,'remaining':0}])
        result = await run_workflow(self.workflow, client=object(), scorer=object(), analysis_model='m',
            threshold=.8, call_db=self.db, collector=collector, executor=executor)
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(executor.await_count, 2)
        collector.assert_awaited_once()

    async def test_quota_stop_stays_resumable_without_automatic_retry(self):
        executor = AsyncMock(return_value={'state':'pass_finished','jobs_processed':1,
            'work_remaining':True,'stop_reason':'daily_quota_exhausted','recovery_required':True})
        result = await run_workflow(self.workflow, client=object(), scorer=object(), analysis_model='m',
            threshold=.8, call_db=self.db, collector=AsyncMock(return_value={'state':'finished'}), executor=executor)
        self.assertEqual(result['state'], 'waiting')
        self.assertTrue(result['can_resume'])
        executor.assert_awaited_once()

    async def test_interrupted_terminal_job_is_not_resumable_or_complete(self):
        from services.news_profile_workflow import _transition, recover_interrupted_workflows, prepare_resume
        job = jobs.enqueue(self.conn, self.run, payload={'title':'지원','link':'https://example.invalid','content':'본문','site_name':'시험'},
                           model='m', recipe='test')
        token = jobs.claim(self.conn, job, now=0)
        jobs.defer(self.conn, job, token, retry_after=0, reason='invalid_analysis', failed=True)
        _transition(self.conn,self.workflow,'running','analyzing')
        recover_interrupted_workflows(self.conn)
        self.assertFalse(get_workflow(self.conn,self.workflow)['can_resume'])
        with self.assertRaises(ValueError): prepare_resume(self.conn,self.workflow)

    async def test_failed_legacy_quota_workflow_can_recover_but_invalid_data_cannot(self):
        from services.news_profile_workflow import _transition, prepare_resume
        job = jobs.enqueue(self.conn, self.run, payload={'title':'지원','link':'https://example.invalid','content':'본문','site_name':'시험'},
                           model='m', recipe='test')
        token = jobs.claim(self.conn, job, now=0)
        jobs.defer(self.conn, job, token, retry_after=0, reason='daily_quota_exhausted', failed=True)
        _transition(self.conn, self.workflow, 'running', 'analyzing')
        _transition(self.conn, self.workflow, 'failed', 'failed', error_code='analysis_failed',
                    summary={'collection':{'state':'finished'}})
        self.assertTrue(get_workflow(self.conn, self.workflow)['can_resume'])
        prepare_resume(self.conn, self.workflow)
        self.assertEqual(jobs.get_job(self.conn, job)['state'], 'queued')
        self.assertEqual(get_workflow(self.conn, self.workflow)['stage'], 'analyzing')
        token = jobs.claim(self.conn, job, now=0)
        jobs.defer(self.conn, job, token, retry_after=0, reason='invalid_analysis', failed=True)
        _transition(self.conn, self.workflow, 'running', 'analyzing')
        _transition(self.conn, self.workflow, 'failed', 'failed', error_code='analysis_failed')
        self.assertFalse(get_workflow(self.conn, self.workflow)['can_resume'])
        with self.assertRaises(ValueError): prepare_resume(self.conn, self.workflow)
    async def test_direct_workflow_real_worker_preserves_review_and_finishes(self):
        from unittest.mock import patch
        from pathlib import Path
        import tempfile
        from services import ai_coordinator, ai_rate_policy
        from services.news_direct_analysis import DirectAnalysis
        from services.news_candidate_intake import register_candidates
        model = ai_rate_policy.MODELS[0]
        async def collect(run_id, **kwargs):
            register_candidates(self.conn, run_id, '1', [dict(title='프로그램 안내',
                link='https://example.invalid/new', content='신청자: 개인. 상세 자격 확인 필요')], model=model)
            return {'state':'finished','registered':1}
        facts = dict(is_relevant=True, category='복지', target='일반', residence=[], life_cycle=[],
            target_characteristics=[], interest_topics=[], start_age_months=None, end_age_months=None,
            evidence_snippet='', applicant_type='individual', applicant_evidence='신청자: 개인',
            applicant_detail='개인', beneficiary_detail='', life_cycle_evidence=[], residence_evidence=[],
            _support_source_units=[1], eligibility_detail='', contact='', deadline='',
            fact_evidence={key: '' for key in ('eligibility_detail', 'contact', 'deadline', 'is_relevant')})
        # Current recipes retain full-request sizing under the explicit 6000-unit
        # test allowance before this workflow-only generation stub.
        with tempfile.TemporaryDirectory(prefix='news-workflow-budget-') as temporary, \
             patch.object(ai_coordinator, '_state_path', return_value=str(Path(temporary) / 'quota.json')), \
             patch('ai_classifier._call_gemini_json', new_callable=AsyncMock, return_value=facts), \
             patch('services.news_run_executor.ai_coordinator.try_start_job', new_callable=AsyncMock, return_value=True), \
             patch('services.news_run_executor.ai_coordinator.finish_job'), \
             patch('services.policy_sync.try_acquire_maintenance_lock', new_callable=AsyncMock, return_value=True), \
             patch('services.policy_sync.release_maintenance_lock'):
            result = await run_workflow(self.workflow, client=object(), scorer=DirectAnalysis(),
                analysis_model=model, threshold=None, call_db=self.db, collector=collect)
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(result['summary']['analysis']['jobs_processed'], 1)
        items = jobs.list_profile_results(self.conn, 'p')['items']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['state'], 'complete')
        self.assertEqual(items[0]['details']['applicant_detail'], '개인')
        self.assertEqual(app_lifecycle.active_profile_jobs, 0)
    async def test_launch_rechecks_duplicate_resume_and_keeps_new_task_owner(self):
        from unittest.mock import patch
        from fastapi import HTTPException
        from services import news_profiles_api as api
        from services import news_workflow_ownership
        import tempfile
        from pathlib import Path
        async def blocked(*args, **kwargs):
            await asyncio.Event().wait()
        with tempfile.TemporaryDirectory() as temporary, patch.object(news_workflow_ownership, '_lock_path', return_value=str(Path(temporary) / 'workflow.lock')), patch.object(api, '_run_workflow', side_effect=blocked):
            api._launch_workflow('launch-test')
            owner = api._WORKFLOW_TASKS['launch-test']
            try:
                with self.assertRaises(HTTPException) as caught:
                    api._launch_workflow('launch-test')
                self.assertEqual(caught.exception.status_code, 409)
                self.assertIs(api._WORKFLOW_TASKS['launch-test'], owner)
                old = asyncio.create_task(asyncio.sleep(0))
                await old
                api._consume_workflow_task('launch-test', old)
                self.assertIs(api._WORKFLOW_TASKS['launch-test'], owner)
            finally:
                owner.cancel()
                try:
                    await owner
                except asyncio.CancelledError:
                    pass
                api._WORKFLOW_TASKS.pop('launch-test', None)

    def setUp(self):
        budget = patch('services.ai_coordinator.input_token_budget', return_value=6000)
        budget.start()
        self.addCleanup(budget.stop)
        capacity = patch('services.ai_coordinator.input_token_capacity', return_value=6000)
        capacity.start()
        self.addCleanup(capacity.stop)
        app_lifecycle.active_profile_jobs = 0
        self.conn = sqlite3.connect(':memory:')
        profiles.ensure_schema(self.conn); jobs.ensure_schema(self.conn)
        profiles.create_profile(self.conn, NewsProfile('p', 1, '시험', '전국', site_ids=('1',)))
        self.run = profiles.prepare_run(self.conn, 'p', expected_revision=1,
            sites=[dict(id=1,name='시험',url='https://example.invalid')])
        self.workflow = create_workflow(self.conn, self.run)

    def tearDown(self): self.conn.close(); app_lifecycle.active_profile_jobs = 0
    async def db(self, fn, *args, **kwargs): return fn(self.conn, *args, **kwargs)

    async def test_success_tracks_stages_and_releases_lifecycle(self):
        seen = []
        async def collector(*args, **kwargs):
            seen.append((get_workflow(self.conn, self.workflow)['stage'], app_lifecycle.active_profile_jobs))
            return {'state':'finished','sites':1,'registered':2,'content_failed':0}
        executor = AsyncMock(return_value={'state':'pass_finished','semantic_processed':1,'jobs_processed':2})
        result = await run_workflow(self.workflow, client=object(), scorer=object(), analysis_model='m',
            threshold=.8, call_db=self.db, collector=collector, executor=executor)
        self.assertEqual((result['state'], result['stage']), ('complete','finished'))
        self.assertEqual(seen, [('collecting',1)])
        self.assertEqual(result['summary']['analysis']['jobs_processed'], 2)
        self.assertEqual(app_lifecycle.active_profile_jobs, 0)

    async def test_busy_collection_waits_without_executor(self):
        executor = AsyncMock()
        result = await run_workflow(self.workflow, client=object(), scorer=object(), analysis_model='m',
            threshold=.8, call_db=self.db, collector=AsyncMock(return_value={'state':'busy'}), executor=executor)
        self.assertEqual((result['state'], result['error_code']), ('waiting','busy'))
        executor.assert_not_awaited()

    async def test_collecting_resume_reuses_finished_source(self):
        first = {
            'state': 'busy', 'analysis_required': True,
            'sources': {
                'sites': {'state': 'finished', 'registered': 2, 'sites': 1},
                'bokjiro': {'state': 'busy'},
            },
        }
        seen_previous = []

        async def collector(*args, **kwargs):
            seen_previous.append(kwargs.get('previous_collection'))
            if len(seen_previous) == 1:
                return first
            self.assertEqual(
                get_workflow(self.conn, self.workflow)['summary']['collection'], first)
            return {'state': 'finished', 'registered': 3, 'analysis_required': True,
                    'sources': {
                        'sites': {'state': 'finished', 'registered': 2, 'sites': 1},
                        'bokjiro': {'state': 'finished', 'registered': 1},
                    }}

        executor = AsyncMock(return_value={'state': 'pass_finished'})
        waiting = await run_workflow(self.workflow, client=object(), scorer=object(),
            analysis_model='m', threshold=.8, call_db=self.db, collector=collector,
            executor=executor)
        self.assertEqual(waiting['state'], 'waiting')
        result = await run_workflow(self.workflow, client=object(), scorer=object(),
            analysis_model='m', threshold=.8, call_db=self.db, collector=collector,
            executor=executor)
        self.assertEqual(result['state'], 'complete')
        self.assertIsNone(seen_previous[0])
        self.assertEqual(seen_previous[1], first)

    async def test_composite_collector_skips_source_already_finished(self):
        snapshot = {'profile': {'sources': ['sites']}}

        async def snapshot_db(fn, *args, **kwargs):
            return snapshot

        site_collector = AsyncMock()
        bokjiro_collector = AsyncMock(return_value={
            'state': 'finished', 'registered': 1, 'new': 1})
        previous = {'sources': {
            'sites': {'state': 'finished', 'registered': 2, 'sites': 1,
                      'attachments_reused': 1},
            'bokjiro': {'state': 'busy'},
        }}
        result = await collect_profile_sources('run', analysis_model='m',
            call_db=snapshot_db, previous_collection=previous,
            site_collector=site_collector, bokjiro_collector=bokjiro_collector)
        site_collector.assert_not_awaited()
        bokjiro_collector.assert_not_awaited()
        self.assertEqual(result['state'], 'finished')
        self.assertEqual(result['registered'], 2)
        self.assertEqual(result['sources']['sites']['attachments_reused'], 1)

    async def test_retired_bokjiro_scope_cannot_collect_any_source(self):
        from services.news_auto_sources import SOURCE_SCOPE_CHANGED_MESSAGE
        site_collector, bokjiro_collector = AsyncMock(), AsyncMock()
        async def snapshot_db(*_args, **_kwargs):
            return {'profile': {'sources': ['sites', 'bokjiro']}}
        with self.assertRaisesRegex(ValueError, SOURCE_SCOPE_CHANGED_MESSAGE):
            await collect_profile_sources('old-run', analysis_model='m', call_db=snapshot_db,
                site_collector=site_collector, bokjiro_collector=bokjiro_collector)
        site_collector.assert_not_awaited()
        bokjiro_collector.assert_not_awaited()

    async def test_retired_snapshot_blocks_resume_and_execution_without_rewriting_history(self):
        from services.news_profile_workflow import _transition, prepare_resume
        from services.news_auto_sources import SOURCE_SCOPE_CHANGED_REASON, SOURCE_SCOPE_CHANGED_MESSAGE
        profiles.create_profile(self.conn, NewsProfile('old-scope', 1, '이전 조건', '전국',
            site_ids=('1',), sources=('sites', 'bokjiro'), life_cycles=('노년',),
            bokjiro_mode='official_tags_v1'))
        self.run = profiles.prepare_run(self.conn, 'old-scope', expected_revision=1,
            sites=[dict(id=1, name='시험', url='https://example.invalid')])
        self.workflow = create_workflow(self.conn, self.run)
        _transition(self.conn, self.workflow, 'running', 'collecting')
        _transition(self.conn, self.workflow, 'waiting', 'collecting',
                    summary={'collection': {'state': 'waiting', 'registered': 2}},
                    error_code='bokjiro_page_failed')
        before = get_workflow(self.conn, self.workflow)
        self.assertFalse(before['can_resume'])
        self.assertEqual(before['resume_block_reason'], SOURCE_SCOPE_CHANGED_REASON)
        self.assertEqual(before['resume_block_message'], SOURCE_SCOPE_CHANGED_MESSAGE)
        with self.assertRaises(ValueError):
            prepare_resume(self.conn, self.workflow)
        collector, executor = AsyncMock(), AsyncMock()
        with self.assertRaisesRegex(ValueError, SOURCE_SCOPE_CHANGED_MESSAGE):
            await run_workflow(self.workflow, client=None, scorer=None, analysis_model='m',
                threshold=0, call_db=self.db, collector=collector, executor=executor)
        collector.assert_not_awaited(); executor.assert_not_awaited()
        self.assertEqual(get_workflow(self.conn, self.workflow), before)
        self.assertEqual(app_lifecycle.active_profile_jobs, 0)

    async def test_failure_saves_only_safe_code(self):
        with self.assertRaisesRegex(RuntimeError, 'private-url'):
            await run_workflow(self.workflow, client=object(), scorer=object(), analysis_model='m', threshold=.8,
                call_db=self.db, collector=AsyncMock(side_effect=RuntimeError('private-url')), executor=AsyncMock())
        stored = get_workflow(self.conn, self.workflow)
        self.assertEqual(stored['error_code'], 'workflow_failed')
        self.assertNotIn('private-url', json_text(stored))
        self.assertEqual(app_lifecycle.active_profile_jobs, 0)

    async def test_cancel_is_persisted_and_lifecycle_released(self):
        entered = asyncio.Event()
        async def blocked(*args, **kwargs): entered.set(); await asyncio.Event().wait()
        task = asyncio.create_task(run_workflow(self.workflow, client=object(), scorer=object(), analysis_model='m',
            threshold=.8, call_db=self.db, collector=blocked, executor=AsyncMock()))
        await entered.wait(); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(get_workflow(self.conn, self.workflow)['state'], 'cancelled')
        self.assertEqual(app_lifecycle.active_profile_jobs, 0)

    async def test_cannot_run_completed_workflow_twice(self):
        common = dict(client=object(), scorer=object(), analysis_model='m', threshold=.8, call_db=self.db,
            collector=AsyncMock(return_value={'state':'finished'}), executor=AsyncMock(return_value={'state':'pass_finished'}))
        await run_workflow(self.workflow, **common)
        with self.assertRaises(ValueError): await run_workflow(self.workflow, **common)

    async def test_ready_workflow_can_be_cancelled_before_task_starts(self):
        from services.news_profile_workflow import cancel_if_ready
        self.assertTrue(cancel_if_ready(self.conn, self.workflow))
        self.assertFalse(cancel_if_ready(self.conn, self.workflow))
        self.assertEqual(get_workflow(self.conn, self.workflow)['state'], 'cancelled')

    async def test_waiting_workflow_can_be_explicitly_ended(self):
        from services.news_profile_workflow import _transition, cancel_if_ready
        _transition(self.conn, self.workflow, 'running', 'collecting')
        _transition(self.conn, self.workflow, 'waiting', 'collecting', error_code='busy')
        self.assertTrue(cancel_if_ready(self.conn, self.workflow))
        self.assertEqual(get_workflow(self.conn, self.workflow)['state'], 'cancelled')

    async def test_startup_recovery_requeues_claim_and_preserves_stage(self):
        from services.news_profile_workflow import _transition, recover_interrupted_workflows
        job = jobs.enqueue(self.conn, self.run, payload={'title':'지원','link':'https://example.invalid/1',
                           'site_name':'시험','content':'본문'},
                           model='m', recipe='v1')
        jobs.claim(self.conn, job, now=0)
        _transition(self.conn, self.workflow, 'running', 'analyzing', summary={'collection': {'state':'finished'}})
        self.assertEqual(recover_interrupted_workflows(self.conn), 1)
        stored = get_workflow(self.conn, self.workflow)
        self.assertEqual((stored['state'], stored['stage'], stored['error_code']),
                         ('waiting', 'analyzing', 'interrupted'))
        self.assertEqual(jobs.get_job(self.conn, job)['state'], 'waiting')

    async def test_waiting_analysis_resume_skips_collection(self):
        from services.news_profile_workflow import _transition
        _transition(self.conn, self.workflow, 'running', 'collecting')
        _transition(self.conn, self.workflow, 'running', 'analyzing', summary={'collection': {'state':'finished','registered':3}})
        _transition(self.conn, self.workflow, 'waiting', 'analyzing', summary={'collection': {'state':'finished','registered':3}}, error_code='scheduled_wait')
        collector = AsyncMock()
        result = await run_workflow(self.workflow, client=object(), scorer=object(), analysis_model='m', threshold=.8,
            call_db=self.db, collector=collector, executor=AsyncMock(return_value={'state':'pass_finished'}))
        collector.assert_not_awaited()
        self.assertEqual(result['state'], 'complete')

    async def test_terminal_analysis_failure_fails_workflow(self):
        result = await run_workflow(self.workflow, client=object(), scorer=object(), analysis_model='m',
            threshold=.8, call_db=self.db, collector=AsyncMock(return_value={'state':'finished'}),
            executor=AsyncMock(return_value={'state':'failed','stop_reason':'analysis_failed'}))
        self.assertEqual((result['state'], result['stage'], result['error_code']),
                         ('failed', 'failed', 'analysis_failed'))

    async def test_manual_item_holds_finish_workflow_with_review_summary(self):
        summary = dict(state='pass_finished', jobs_processed=3, remaining=0,
                       work_remaining=False, review_required=True, review_required_count=2,
                       review_reasons={'ai_input_review_required': 2},
                       completion_status='complete_with_review')
        result = await run_workflow(self.workflow, client=object(), scorer=object(), analysis_model='m',
            threshold=.8, call_db=self.db, collector=AsyncMock(return_value={'state':'finished'}),
            executor=AsyncMock(return_value=summary))
        self.assertEqual((result['state'], result['stage']), ('complete', 'finished'))
        self.assertFalse(result['can_resume'])
        self.assertIsNone(result['error_code'])
        self.assertEqual(result['summary']['analysis'], summary)

    async def test_manual_holds_do_not_block_quota_resume_or_count_as_pending(self):
        from services.news_profile_workflow import _transition, prepare_resume, _pending_analysis_count
        held, quota = [jobs.enqueue(self.conn, self.run, payload={'title': f'지원 {index}',
            'link':f'https://example.invalid/{index}', 'content':'본문', 'site_name':'시험'},
            model='m', recipe='test') for index in range(2)]
        for job_id, reason in ((held, 'ai_input_review_required'), (quota, 'daily_quota_exhausted')):
            token = jobs.claim(self.conn, job_id, now=0)
            jobs.defer(self.conn, job_id, token, retry_after=0, reason=reason, failed=True)
        _transition(self.conn, self.workflow, 'running', 'analyzing')
        _transition(self.conn, self.workflow, 'waiting', 'analyzing', error_code='daily_quota_exhausted')
        self.assertTrue(get_workflow(self.conn, self.workflow)['can_resume'])
        self.assertEqual(_pending_analysis_count(self.conn, self.run), 1)
        prepare_resume(self.conn, self.workflow)
        self.assertEqual(jobs.get_job(self.conn, held)['state'], 'failed')
        self.assertEqual(jobs.get_job(self.conn, quota)['state'], 'queued')

    async def test_legacy_manual_hold_failure_can_resume_without_retrying_held_input(self):
        from services.news_profile_workflow import _transition, prepare_resume, _pending_analysis_count
        held = jobs.enqueue(self.conn, self.run, payload={'title':'지원',
            'link':'https://example.invalid/held', 'content':'본문', 'site_name':'시험'},
            model='m', recipe='test')
        token = jobs.claim(self.conn, held, now=0)
        jobs.defer(self.conn, held, token, retry_after=0, reason='ai_input_review_required', failed=True)
        _transition(self.conn, self.workflow, 'running', 'analyzing')
        _transition(self.conn, self.workflow, 'failed', 'failed', error_code='ai_input_review_required',
                    summary={'collection': {'state':'finished'}})
        self.assertTrue(get_workflow(self.conn, self.workflow)['can_resume'])
        prepare_resume(self.conn, self.workflow)
        self.assertEqual(get_workflow(self.conn, self.workflow)['state'], 'waiting')
        self.assertEqual(jobs.get_job(self.conn, held)['state'], 'failed')
        self.assertEqual(_pending_analysis_count(self.conn, self.run), 0)
        executor = AsyncMock()
        result = await run_workflow(self.workflow, client=None, scorer=None, analysis_model='m',
            threshold=.8, call_db=self.db, collector=AsyncMock(), executor=executor,
            analysis_wait_reason='authentication_required')
        executor.assert_not_awaited()
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(result['summary']['analysis']['completion_status'], 'complete_with_review')
        self.assertEqual(result['summary']['analysis']['review_required_count'], 1)

    async def test_official_only_collection_completes_without_executor(self):
        executor = AsyncMock()
        result = await run_workflow(self.workflow, client=None, scorer=None, analysis_model='m',
            threshold=.8, call_db=self.db,
            collector=AsyncMock(return_value={'state':'finished', 'registered':2,
                                               'analysis_required':False, 'ai_called':False}),
            executor=executor)
        executor.assert_not_awaited()
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(result['summary']['analysis']['state'], 'not_required')
        self.assertFalse(result['summary']['analysis']['ai_called'])


def json_text(value):
    import json
    return json.dumps(value, ensure_ascii=False)
