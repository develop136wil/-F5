import asyncio
import json
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

from services import app_lifecycle, news_profile_store as profiles, news_analysis_store as jobs
from services.news_profile import NewsProfile
from services.news_candidate_intake import register_candidates
from services.news_profile_workflow import (
    create_workflow, get_workflow, list_workflows, _transition,
    recover_interrupted_workflows, run_workflow)
from services.news_workflow_progress import ProgressReporter, analysis_progress, save_progress
from services.news_site_collector import collect_registered_sites


class TestWorkflowProgress(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        app_lifecycle.shutdown_requested = False
        self.conn = sqlite3.connect(':memory:')
        profiles.ensure_schema(self.conn)
        jobs.ensure_schema(self.conn)
        profiles.create_profile(self.conn, NewsProfile('p', 1, '시험', '전국',
            site_ids=('1', '2'), keywords=('지원',), semantic_text='돌봄 소식'))
        self.run = profiles.prepare_run(self.conn, 'p', expected_revision=1,
            sites=[dict(id=1, name='첫 기관', url='https://one.example.invalid'),
                   dict(id=2, name='둘째 기관', url='https://two.example.invalid')])
        self.workflow = create_workflow(self.conn, self.run)

    def tearDown(self):
        self.conn.close()
        app_lifecycle.shutdown_requested = False

    async def db(self, fn, *args, **kwargs):
        return fn(self.conn, *args, **kwargs)

    def register(self, count, *, title='지원'):
        return register_candidates(self.conn, self.run, '1', [dict(title=f'{title} {i}',
            link=f'https://one.example.invalid/{i}', content='지원 안내 본문')
            for i in range(count)], model='m')

    def reporter(self, clock=lambda: 10):
        current = get_workflow(self.conn, self.workflow)
        return ProgressReporter(self.workflow, self.run, current['progress'], self.db, clock=clock)

    async def test_initial_progress_freezes_site_total_without_document_guess(self):
        result = get_workflow(self.conn, self.workflow)
        self.assertEqual(result['progress']['sites_total'], 2)
        self.assertEqual(result['progress']['sites_processed'], 0)
        self.assertIsNone(result['progress']['site_documents_total'])
        self.assertNotIn('analysis_total', result['progress'])
        self.assertEqual(list_workflows(self.conn, 'p')[0]['progress'], result['progress'])

    async def test_throttle_and_boundary_flush_keep_summary_and_safe_fields(self):
        _transition(self.conn, self.workflow, 'running', 'collecting', summary={'kept': 1})
        reporter = self.reporter()
        await reporter({'site_index': 1, 'site_name': ' first\nsite ' + 'x' * 150,
                        'raw_content': 'do not persist', 'documents_processed': 1})
        await reporter({'documents_processed': 2, 'sites_total': -1})
        saved = get_workflow(self.conn, self.workflow)
        self.assertEqual(saved['progress']['documents_processed'], 1)
        self.assertEqual(saved['progress']['sites_total'], 2)
        self.assertNotIn('raw_content', saved['progress'])
        self.assertLessEqual(len(saved['progress']['site_name']), 120)
        await reporter(force=True)
        self.assertEqual(get_workflow(self.conn, self.workflow)['progress']['documents_processed'], 2)
        self.assertEqual(get_workflow(self.conn, self.workflow)['summary'], {'kept': 1})

    async def test_analysis_is_cumulative_across_fifty_item_batches_and_read_only(self):
        rows = self.register(53)
        _transition(self.conn, self.workflow, 'running', 'analyzing',
                    summary={'analysis': {'jobs_processed': 1}})
        reporter = self.reporter()
        await reporter({'phase': 'analysis'}, force=True)
        self.conn.executemany("UPDATE news_analysis_jobs SET state='complete' WHERE job_id=?",
                             [(row['job_id'],) for row in rows[:51]])
        self.conn.execute("UPDATE news_analysis_jobs SET state='failed',reason='ai_input_review_required' WHERE job_id=?",
                          (rows[51]['job_id'],))
        self.conn.execute("UPDATE news_analysis_jobs SET state='failed',reason='daily_quota_exhausted' WHERE job_id=?",
                          (rows[52]['job_id'],))
        changed = self.conn.total_changes
        result = get_workflow(self.conn, self.workflow)
        progress = result['progress']
        self.assertEqual(progress['analysis_total'], 53)
        self.assertEqual(progress['analysis_processed'], 52)
        self.assertEqual(progress['analysis_complete'], 51)
        self.assertEqual(progress['analysis_review_required'], 1)
        self.assertEqual(progress['analysis_remaining'], 1)
        self.assertEqual(progress['analysis_failed'], 1)
        self.assertEqual(self.conn.total_changes, changed)
        self.assertEqual(result['summary']['analysis']['jobs_processed'], 1)

    async def test_semantic_queue_to_jobs_does_not_double_total(self):
        rows = self.register(3, title='새 공고')
        self.assertTrue(all(row['job_id'] is None for row in rows))
        first = analysis_progress(self.conn, self.run)
        self.assertEqual((first['analysis_total'], first['analysis_processed']), (3, 0))
        job_id = jobs.enqueue(self.conn, self.run, payload=dict(title='새 공고 0',
            link='https://one.example.invalid/0', content='지원 안내 본문', site_name='첫 기관'),
            model='m', recipe='test')
        self.conn.execute("UPDATE news_run_candidates SET route='semantic_matched',job_id=? WHERE candidate_key=?",
                          (job_id, rows[0]['candidate_key']))
        self.conn.execute("UPDATE news_analysis_jobs SET state='complete' WHERE job_id=?", (job_id,))
        self.conn.execute("UPDATE news_run_candidates SET route='semantic_review' WHERE candidate_key=?",
                          (rows[1]['candidate_key'],))
        current = analysis_progress(self.conn, self.run)
        self.assertEqual((current['analysis_total'], current['analysis_processed'],
                          current['analysis_remaining']), (3, 2, 1))
        self.assertEqual(current['analysis_review_required'], 1)

    async def test_executor_reports_each_item_without_resetting_at_batch_boundary(self):
        from services.news_run_executor import execute_registered_run
        from services.news_semantic_scorer import SemanticScorer, EmbeddingRecipe
        self.register(53)
        seen = []
        async def progress(_changes, **kwargs):
            seen.append(analysis_progress(self.conn, self.run))
        async def finish(job_id, _client, **kwargs):
            self.conn.execute("UPDATE news_analysis_jobs SET state='complete' WHERE job_id=?", (job_id,))
            return 'complete'
        scorer = SemanticScorer(object(), EmbeddingRecipe('embedding', dimensions=2))
        with patch('services.news_run_executor.ai_coordinator.try_start_job', new=AsyncMock(return_value=True)), \
             patch('services.news_run_executor.ai_coordinator.finish_job'), \
             patch('services.policy_sync.try_acquire_maintenance_lock', new=AsyncMock(return_value=True)), \
             patch('services.policy_sync.release_maintenance_lock'), \
             patch('services.news_run_executor.process_job', side_effect=finish):
            for _ in range(2):
                await execute_registered_run(self.run, client=object(), scorer=scorer,
                    analysis_model='m', threshold=.8, call_db=self.db, progress=progress, limit=50)
        self.assertTrue(all(value['analysis_total'] == 53 for value in seen))
        processed = [value['analysis_processed'] for value in seen]
        self.assertEqual(processed, sorted(processed))
        self.assertEqual(set(processed), set(range(54)))

    async def test_excluded_candidates_are_not_analysis_work(self):
        rows = self.register(3)
        self.conn.execute("UPDATE news_run_candidates SET route='excluded' WHERE candidate_key=?",
                          (rows[0]['candidate_key'],))
        self.assertEqual(analysis_progress(self.conn, self.run)['analysis_total'], 2)
        # A legacy job with no intake receipt still contributes one work item.
        jobs.enqueue(self.conn, self.run, payload=dict(title='old support',
            link='https://one.example.invalid/old', content='본문', site_name='첫 기관'),
            model='m', recipe='legacy')
        self.assertEqual(analysis_progress(self.conn, self.run)['analysis_total'], 3)

    async def test_waiting_and_recovery_preserve_counters_without_completing_failed_jobs(self):
        self.register(1)
        _transition(self.conn, self.workflow, 'running', 'analyzing')
        await self.reporter()({'phase': 'analysis'}, force=True)
        recover_interrupted_workflows(self.conn)
        result = get_workflow(self.conn, self.workflow)
        self.assertEqual((result['state'], result['error_code']), ('waiting', 'interrupted'))
        self.assertEqual(result['progress']['analysis_remaining'], 1)
        self.assertEqual(result['progress']['analysis_processed'], 0)
        before = dict(result['progress'])
        save_progress(self.conn, self.workflow, {'schema_version': 1, 'sites_total': 999})
        self.assertEqual(get_workflow(self.conn, self.workflow)['progress'], before)

    async def test_completed_zero_remaining_snapshot_is_reused_for_status_and_history(self):
        self.register(2)
        self.conn.execute("UPDATE news_analysis_jobs SET state='complete' WHERE run_id=?", (self.run,))
        _transition(self.conn, self.workflow, 'running', 'analyzing')
        await self.reporter()({'phase': 'analysis'}, force=True)
        _transition(self.conn, self.workflow, 'complete', 'finished')
        before = self.conn.total_changes
        with patch('services.news_profile_workflow.analysis_progress',
                   side_effect=AssertionError('Completed history must reuse the final snapshot')):
            current = get_workflow(self.conn, self.workflow)
            history = list_workflows(self.conn, 'p')
        self.assertEqual(current['progress']['analysis_total'], 2)
        self.assertEqual(current['progress']['analysis_processed'], 2)
        self.assertEqual(history[0]['progress'], current['progress'])
        self.assertEqual(self.conn.total_changes, before)

    async def test_completed_stale_or_missing_analysis_counters_fall_back_to_receipts(self):
        self.register(1)
        self.conn.execute("UPDATE news_analysis_jobs SET state='complete' WHERE run_id=?", (self.run,))
        _transition(self.conn, self.workflow, 'running', 'analyzing')
        _transition(self.conn, self.workflow, 'complete', 'finished')
        for counters in ({}, {'analysis_remaining': 1, 'analysis_running': 0},
                         {'analysis_remaining': 0},
                         {'analysis_remaining': 0, 'analysis_running': 1},
                         {'analysis_remaining': False, 'analysis_running': 0}):
            with self.subTest(counters=counters):
                self.conn.execute('UPDATE news_profile_workflows SET progress_json=? WHERE workflow_id=?',
                    (json.dumps(dict(schema_version=1, phase='analysis', **counters)), self.workflow))
                with patch('services.news_profile_workflow.analysis_progress', wraps=analysis_progress) as read:
                    current = get_workflow(self.conn, self.workflow)
                read.assert_called_once_with(self.conn, self.run)
                self.assertEqual(current['progress']['analysis_remaining'], 0)
                self.assertEqual(current['progress']['analysis_processed'], 1)

    async def test_cancelled_or_waiting_zero_snapshot_is_still_rechecked(self):
        self.register(1)
        _transition(self.conn, self.workflow, 'running', 'analyzing')
        self.conn.execute('UPDATE news_profile_workflows SET progress_json=? WHERE workflow_id=?',
            (json.dumps(dict(schema_version=1, phase='analysis', analysis_remaining=0,
                             analysis_running=0)), self.workflow))
        for state in ('waiting', 'cancelled'):
            self.conn.execute('UPDATE news_profile_workflows SET state=? WHERE workflow_id=?',
                              (state, self.workflow))
            with patch('services.news_profile_workflow.analysis_progress', wraps=analysis_progress) as read:
                current = get_workflow(self.conn, self.workflow)
            read.assert_called_once_with(self.conn, self.run)
            self.assertEqual(current['progress']['analysis_remaining'], 1)

    async def test_progress_storage_failure_cannot_abort_work(self):
        _transition(self.conn, self.workflow, 'running', 'collecting')
        with patch('services.news_workflow_progress.save_progress', side_effect=sqlite3.OperationalError('private')):
            await self.reporter()({'site_index': 1}, force=True)
        self.assertEqual(get_workflow(self.conn, self.workflow)['state'], 'running')

    async def test_collector_reports_current_site_body_and_issue_counts_without_ai(self):
        snapshots = []
        async def progress(value, **kwargs):
            snapshots.append(dict(value))
        engine = type('Engine', (), {})()
        engine.start, engine.close = AsyncMock(), AsyncMock()
        engine.scrape_site = AsyncMock(side_effect=[{'state': 'finished', 'items': [
            dict(title='지원 1', link='https://one.example.invalid/a'),
            dict(title='지원 2', link='https://one.example.invalid/b')]}, RuntimeError('private-site-failure')])
        async def fetch(_session, _url):
            self.assertEqual(snapshots[-1]['phase'], 'content')
            self.assertEqual(snapshots[-1]['site_documents_total'], 2)
            await asyncio.sleep(0)
            return {'text': '읽을 수 있는 기관 공고입니다.' * 10}
        with patch('services.scraper_service._acquire_scrape_lock', return_value=object()), \
             patch('services.scraper_service._release_scrape_lock'), \
             patch('ai_classifier.get_client', side_effect=AssertionError('no AI needed')):
            result = await collect_registered_sites(self.run, analysis_model='m', call_db=self.db,
                engine_factory=lambda **kw: engine, content_fetcher=fetch, progress=progress)
        self.assertEqual(result['state'], 'finished')
        self.assertEqual(snapshots[0]['site_documents_total'], None)
        end = snapshots[-1]
        self.assertEqual((end['sites_total'], end['sites_processed'], end['sites_succeeded'],
                          end['sites_review_required']), (2, 2, 1, 1))
        self.assertEqual((end['documents_processed'], end['saved_candidates']), (2, 2))
        self.assertEqual(end['site_name'], '둘째 기관')
        self.assertNotIn('private-site-failure', json.dumps(snapshots))
        engine.close.assert_awaited_once()

    async def test_workflow_passes_reporter_and_preserves_terminal_counts(self):
        async def collector(*args, progress, **kwargs):
            await progress({'phase': 'content', 'sites_total': 2, 'sites_processed': 1,
                            'site_name': '첫 기관', 'documents_processed': 1,
                            'site_documents_total': 1, 'site_documents_processed': 1}, force=True)
            self.register(1)
            return {'state': 'finished', 'registered': 1}
        async def executor(*args, progress, **kwargs):
            self.conn.execute("UPDATE news_analysis_jobs SET state='complete' WHERE run_id=?", (self.run,))
            await progress({'phase': 'analysis'})
            return {'state': 'pass_finished', 'jobs_processed': 1}
        result = await run_workflow(self.workflow, client=object(), scorer=object(), analysis_model='m',
                                    threshold=.8, call_db=self.db, collector=collector, executor=executor)
        self.assertEqual(result['state'], 'complete')
        self.assertEqual((result['progress']['analysis_processed'], result['progress']['analysis_total']), (1, 1))
        before = result['progress']
        save_progress(self.conn, self.workflow, {'schema_version': 1})
        self.assertEqual(get_workflow(self.conn, self.workflow)['progress'], before)

    async def test_cancelled_workflow_keeps_latest_collection_receipt(self):
        entered = asyncio.Event()
        async def collector(*args, progress, **kwargs):
            await progress({'phase': 'content', 'documents_processed': 7}, force=True)
            entered.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(run_workflow(self.workflow, client=None, scorer=None,
            analysis_model='m', threshold=None, call_db=self.db, collector=collector))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        result = get_workflow(self.conn, self.workflow)
        self.assertEqual((result['state'], result['stage']), ('cancelled', 'cancelled'))
        self.assertEqual(result['progress']['documents_processed'], 7)
