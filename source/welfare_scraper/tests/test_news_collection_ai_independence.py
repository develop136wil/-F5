"""Offline source collection must not require AI admission or mutate old queues."""
import asyncio
import json
import os
import sqlite3
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import database
from services import news_profiles_api as api, news_profile_store as profiles
from services import news_analysis_store as jobs
from services import news_profile_workflow as workflows
from services.news_profile import NewsProfile
from services.news_candidate_intake import register_candidates
from tests import test_news_jobs_api as fixture


class TestCollectionAdmission(unittest.TestCase):
    setUp = fixture.TestNewsJobsApi.setUp
    tearDown = fixture.TestNewsJobsApi.tearDown

    def add_site(self):
        with database.get_connection() as conn:
            conn.execute("INSERT INTO sites(id,name,url,selectors) VALUES (1,'시험','https://example.invalid','{}')")
            conn.commit()

    def waiting(self, *, settings, stage='collecting', summary=None, run_id=None):
        with database.get_connection() as conn:
            run_id = run_id or profiles.prepare_run(conn, 'p', expected_revision=1,
                sites=[dict(id=1, name='시험', url='https://example.invalid')])
            workflow_id = workflows.create_workflow(conn, run_id, settings=settings)
            workflows._transition(conn, workflow_id, 'running', stage)
            workflows._transition(conn, workflow_id, 'waiting', stage, summary=summary)
        return workflow_id, run_id

    def test_source_start_and_options_do_not_need_ai_key_or_budget(self):
        self.add_site()
        for client, usage, expected in (
            (None, {}, 'authentication_required'),
            (object(), {'models':{'*':{'daily_exhausted':True}}}, 'daily_quota_exhausted'),
            (object(), {'state_warning':'unavailable'}, 'ai_usage_state_unavailable'),
        ):
            with self.subTest(expected=expected), patch.dict(os.environ, {}, clear=False), \
                 patch('ai_classifier.get_client', return_value=client), \
                 patch('services.ai_coordinator.status', return_value=usage), \
                 patch.object(api, '_launch_workflow') as launch:
                os.environ.pop('NEWS_PROFILE_SEMANTIC_THRESHOLD', None)
                options = self.client.get('/api/news/profiles/p/workflow-options').json()
                self.assertTrue(options['enabled'])
                self.assertFalse(options['analysis_ready'])
                self.assertEqual(options['analysis_reason_code'], expected)
                response = self.client.post('/api/news/profiles/p/workflows', json={'expected_revision':1})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(launch.call_args.kwargs['analysis_wait_reason'], expected)
                self.assertNotIn('defer_analysis', launch.call_args.kwargs)
                with database.get_connection() as conn:
                    workflows.cancel_if_ready(conn, response.json()['workflow_id'])

    def test_legacy_daily_exhaustion_does_not_block_new_analysis(self):
        self.add_site()
        usage = {'local_budget_exhausted': True, 'local_budget_remaining': 0,
                 'daily_request_limit': 20, 'models': {}}
        with patch.dict(os.environ, {}, clear=False), \
             patch('ai_classifier.get_client', return_value=object()), \
             patch('services.ai_coordinator.status', return_value=usage), \
             patch.object(api, '_launch_workflow') as launch:
            os.environ.pop('NEWS_PROFILE_SEMANTIC_THRESHOLD', None)
            options = self.client.get('/api/news/profiles/p/workflow-options').json()
            self.assertTrue(options['enabled'])
            self.assertTrue(options['analysis_ready'])
            self.assertFalse(options['analysis_reason_code'])
            response = self.client.post('/api/news/profiles/p/workflows', json={'expected_revision': 1})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIsNone(launch.call_args.kwargs.get('analysis_wait_reason'))

    def test_malformed_usage_status_defers_analysis_without_blocking_sources(self):
        self.add_site()
        for usage in (None, {'models': None}, {'models': {'*': 'unknown'}}):
            with self.subTest(usage=usage), patch('ai_classifier.get_client', return_value=object()), \
                 patch('services.ai_coordinator.status', return_value=usage), \
                 patch.object(api, '_launch_workflow') as launch:
                response = self.client.post('/api/news/profiles/p/workflows', json={'expected_revision': 1})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(launch.call_args.kwargs['analysis_wait_reason'], 'ai_usage_state_unavailable')
                with database.get_connection() as conn:
                    workflows.cancel_if_ready(conn, response.json()['workflow_id'])

    def test_broken_client_initialization_does_not_block_source_collection(self):
        self.add_site()
        with patch('ai_classifier.get_client', side_effect=ValueError('synthetic invalid key')), \
             patch.object(api, '_launch_workflow') as launch:
            response = self.client.post('/api/news/profiles/p/workflows', json={'expected_revision':1})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(launch.call_args.kwargs['analysis_wait_reason'], 'authentication_required')

    def test_invalid_analysis_settings_defer_jobs_instead_of_blocking_collection(self):
        self.add_site()
        with patch.dict(os.environ, {'NEWS_PROFILE_SEMANTIC_THRESHOLD':'nan'}), \
             patch('ai_classifier.get_client', return_value=None), patch.object(api, '_launch_workflow') as launch:
            response = self.client.post('/api/news/profiles/p/workflows', json={'expected_revision':1, 'headless':False})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(launch.call_args.kwargs['defer_analysis'])
            self.assertIsNone(launch.call_args.kwargs['client'])
            with database.get_connection() as conn:
                saved = workflows.get_workflow_settings(conn, response.json()['workflow_id'])
                self.assertEqual(saved, api._pending_workflow_settings(headless=False))
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM news_analysis_jobs WHERE run_id=?',
                    (response.json()['run_id'],)).fetchone()[0], 0)

    def test_invalid_embedding_model_is_unconfigured_before_admission(self):
        for value in ('', ' ', ' model'):
            with self.subTest(value=value), patch.dict(os.environ, {'NEWS_PROFILE_SEMANTIC_THRESHOLD':'.8','EMBED_MODEL':value}):
                self.assertIsNone(api._runtime_workflow_settings())

    def test_profile_revision_race_cannot_mix_old_analysis_mode_with_new_snapshot(self):
        self.add_site()
        original = api._owned_execute
        async def raced_read(action):
            result = await original(action)
            if isinstance(result, NewsProfile):
                with database.get_connection() as conn:
                    profiles.save_revision(conn, replace(result,revision=2,keywords=('바뀐 조건',)),expected_revision=1)
            return result
        with patch.object(api,'_owned_execute',side_effect=raced_read), \
             patch('ai_classifier.get_client',return_value=None), patch.object(api,'_launch_workflow') as launch:
            response = self.client.post('/api/news/profiles/p/workflows',json={'expected_revision':2})
            self.assertEqual(response.status_code,409,response.text)
            launch.assert_not_called()
        with database.get_connection() as conn:
            self.assertEqual(profiles.get_profile(conn,'p').revision,2)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM news_profile_workflows').fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM news_run_snapshots').fetchone()[0],1)

    def test_collection_resume_keeps_original_snapshot_model_and_jobs_without_ai(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('NEWS_PROFILE_SEMANTIC_THRESHOLD', None)
            saved = api._runtime_workflow_settings()
        workflow_id, run_id = self.waiting(settings=saved, run_id=self.run)
        with database.get_connection() as conn:
            before = profiles.get_run_snapshot(conn, run_id)
            before_job = tuple(conn.execute('SELECT * FROM news_analysis_jobs WHERE job_id=?', (self.job,)).fetchone())
        for client, usage, env, expected in (
            (None, {}, {'CLASSIFY_MODEL':saved['analysis_model']}, 'authentication_required'),
            (object(), {'models':{saved['analysis_model']:{'daily_exhausted':True}}}, {'CLASSIFY_MODEL':saved['analysis_model']}, 'daily_quota_exhausted'),
            (object(), {}, {'CLASSIFY_MODEL':'different-model'}, 'analysis_settings_changed'),
            (object(), {}, {'NEWS_PROFILE_SEMANTIC_THRESHOLD':'invalid'}, 'analysis_settings_required'),
        ):
            with self.subTest(expected=expected), patch.dict(os.environ, env), \
                 patch('ai_classifier.get_client', return_value=client), \
                 patch('services.ai_coordinator.status', return_value=usage), \
                 patch.object(api, '_launch_workflow') as launch:
                response = self.client.post(f'/api/news/workflows/{workflow_id}/resume')
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(launch.call_args.kwargs['analysis_model'], saved['analysis_model'])
                self.assertEqual(launch.call_args.kwargs['analysis_wait_reason'], expected)
                with database.get_connection() as conn:
                    self.assertEqual(workflows.get_workflow_settings(conn, workflow_id), saved)
                    self.assertEqual(profiles.get_run_snapshot(conn, run_id), before)
                    self.assertEqual(tuple(conn.execute('SELECT * FROM news_analysis_jobs WHERE job_id=?',
                        (self.job,)).fetchone()), before_job)

    def test_collection_resume_does_not_reset_failed_analysis_jobs(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('NEWS_PROFILE_SEMANTIC_THRESHOLD', None)
            saved = api._runtime_workflow_settings()
        workflow_id, _ = self.waiting(settings=saved, run_id=self.run)
        with database.get_connection() as conn:
            token = jobs.claim(conn, self.job, now=0)
            jobs.defer(conn, self.job, token, retry_after=0, reason='daily_quota_exhausted', failed=True)
        with patch('ai_classifier.get_client', return_value=None), patch.object(api, '_launch_workflow'):
            self.assertEqual(self.client.post(f'/api/news/workflows/{workflow_id}/resume').status_code, 200)
        self.assertEqual(self.client.get(self.url).json()['state'], 'failed')
        with database.get_connection() as conn:
            conn.execute("UPDATE news_analysis_jobs SET reason='ai_input_review_required' WHERE job_id=?",(self.job,))
            conn.commit()
        with patch('ai_classifier.get_client', return_value=None), patch.object(api, '_launch_workflow'):
            self.assertEqual(self.client.post(f'/api/news/workflows/{workflow_id}/resume').status_code, 200)
        self.assertEqual(self.client.get(self.url).json()['reason'], 'ai_input_review_required')

    def test_pending_first_settings_require_explicit_analysis_resume_and_ready_ai(self):
        saved = api._pending_workflow_settings()
        workflow_id, run_id = self.waiting(settings=saved, stage='analyzing', summary={'collection':{'state':'finished'}})
        with database.get_connection() as conn:
            register_candidates(conn, run_id, 1, [dict(title='안내', link='https://example.invalid/a', content='본문')],
                model=saved['analysis_model'], defer_analysis=True)
            before = profiles.get_run_snapshot(conn, run_id)
        with patch.dict(os.environ, {}, clear=False), patch('ai_classifier.get_client', return_value=object()), \
             patch('services.ai_coordinator.status', return_value={}), patch.object(api, '_launch_workflow') as launch:
            os.environ.pop('NEWS_PROFILE_SEMANTIC_THRESHOLD', None)
            with self.assertRaises(Exception) as caught:
                asyncio.run(api._resume_workflow(workflow_id, ownership=object(), allow_initial_setup=False))
            self.assertEqual(caught.exception.status_code, 409)
            launch.assert_not_called()
            with patch('services.ai_coordinator.status', return_value={'models':{'*':{'daily_exhausted':True}}}):
                self.assertEqual(self.client.post(f'/api/news/workflows/{workflow_id}/resume').status_code, 409)
            with database.get_connection() as conn:
                self.assertEqual(workflows.get_workflow_settings(conn, workflow_id), saved)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM news_analysis_jobs WHERE run_id=?', (run_id,)).fetchone()[0], 0)
            response = self.client.post(f'/api/news/workflows/{workflow_id}/resume')
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn('defer_analysis', launch.call_args.kwargs)
            with database.get_connection() as conn:
                self.assertEqual(workflows.get_workflow_settings(conn, workflow_id), api._runtime_workflow_settings())
                self.assertEqual(profiles.get_run_snapshot(conn, run_id), before)
                self.assertEqual(conn.execute('SELECT route FROM news_run_candidates WHERE run_id=?', (run_id,)).fetchone()[0], 'semantic_candidate')
            with patch.dict(os.environ, {'CLASSIFY_MODEL':'other-model'}):
                self.assertEqual(self.client.post(f'/api/news/workflows/{workflow_id}/resume').status_code, 409)

    def test_explicit_new_source_run_preserves_ai_backlog_and_blocks_double_click(self):
        self.add_site()
        workflow_id, run_id = self.waiting(settings=api._pending_workflow_settings(), stage='analyzing',
            summary={'collection':{'state':'finished'}, 'analysis':{'remaining':1}}, run_id=self.run)
        with database.get_connection() as conn:
            before = workflows.get_workflow(conn, workflow_id)
            snapshot = profiles.get_run_snapshot(conn, run_id)
            job = tuple(conn.execute('SELECT * FROM news_analysis_jobs WHERE job_id=?', (self.job,)).fetchone())
        with patch('ai_classifier.get_client', return_value=None), patch.object(api, '_launch_workflow') as launch:
            options = self.client.get('/api/news/profiles/p/workflow-options').json()
            self.assertTrue(options['can_start_new_collection'])
            self.assertEqual(self.client.post('/api/news/profiles/p/workflows', json={'expected_revision':1}).status_code, 409)
            for value in ('true', 1, None):
                self.assertEqual(self.client.post('/api/news/profiles/p/workflows', json={
                    'expected_revision':1, 'new_collection':value}).status_code, 422)
            body = {'expected_revision':1, 'new_collection':True}
            response = self.client.post('/api/news/profiles/p/workflows', json=body)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotEqual(response.json()['run_id'], run_id)
            self.assertEqual(self.client.post('/api/news/profiles/p/workflows', json=body).status_code, 409)
            self.assertFalse(self.client.get('/api/news/profiles/p/workflow-options').json()['can_start_new_collection'])
            launch.assert_called_once()
        with database.get_connection() as conn:
            self.assertEqual(workflows.get_workflow(conn, workflow_id), before)
            self.assertEqual(profiles.get_run_snapshot(conn, run_id), snapshot)
            self.assertEqual(tuple(conn.execute('SELECT * FROM news_analysis_jobs WHERE job_id=?', (self.job,)).fetchone()), job)

    def test_only_finished_source_waits_allow_new_collection(self):
        self.add_site()
        workflow_id, _ = self.waiting(settings=api._pending_workflow_settings())
        for state, stage, summary, allowed in (
            ('waiting','analyzing',{'collection':{'state':'finished'}},True),
            ('waiting','analyzing',{'collection':{'state':'waiting'}},False),
            ('waiting','collecting',{'collection':{'state':'finished'}},False),
            ('waiting','prepared',{},False), ('ready','prepared',{},False),
            ('running','analyzing',{'collection':{'state':'finished'}},False),
            ('waiting','analyzing',{},False),
        ):
            with self.subTest(state=state, stage=stage, summary=summary), database.get_connection() as conn:
                conn.execute('UPDATE news_profile_workflows SET state=?,stage=?,summary_json=? WHERE workflow_id=?',
                    (state,stage,json.dumps(summary),workflow_id))
                conn.commit()
                self.assertEqual(api._collection_backlog(conn, 'p', 1), (True,allowed))
        with database.get_connection() as conn:
            conn.execute("UPDATE news_profile_workflows SET state='waiting',stage='analyzing',summary_json=?", (json.dumps({'collection':{'state':'finished'}}),))
            profiles.save_revision(conn, replace(profiles.get_profile(conn, 'p'), revision=2), expected_revision=1)
            new_run = profiles.prepare_run(conn, 'p', expected_revision=2, sites=[dict(id=1,name='시험',url='https://example.invalid')])
            workflows.create_workflow(conn, new_run)
            self.assertEqual(api._collection_backlog(conn, 'p', 1), (True,False))


class TestCollectionAnalysisWait(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        profiles.ensure_schema(self.conn)
        jobs.ensure_schema(self.conn)
        profiles.create_profile(self.conn, NewsProfile('p',1,'조건','전국', site_ids=('1',), keywords=('지원',), excluded_keywords=('제외',)))
        self.run = profiles.prepare_run(self.conn, 'p', expected_revision=1, sites=[dict(id=1,name='시험',url='https://example.invalid')])
        self.saved = api._pending_workflow_settings()
        self.workflow = workflows.create_workflow(self.conn, self.run, settings=self.saved)

    def tearDown(self):
        self.conn.close()

    async def db(self, action, *args, **kwargs):
        return action(self.conn, *args, **kwargs)

    def candidates(self, *, defer=True):
        return register_candidates(self.conn, self.run, 1, [
            dict(title='지원 안내', link='https://example.invalid/1', content='원문 지원'),
            dict(title='행사 안내', link='https://example.invalid/2', content='원문 행사'),
            dict(title='제외 안내', link='https://example.invalid/3', content='제외 원문')],
            model=self.saved['analysis_model'], defer_analysis=defer)

    async def test_source_finishes_then_waits_once_without_executor_or_ai(self):
        async def collect(run_id, **kwargs):
            self.assertTrue(kwargs['defer_analysis'])
            self.candidates()
            return {'state':'finished', 'registered':3}
        collector, executor = AsyncMock(side_effect=collect), AsyncMock()
        result = await workflows.run_workflow(self.workflow, client=None, scorer=None,
            analysis_model=self.saved['analysis_model'], threshold=None, call_db=self.db,
            collector=collector, executor=executor, analysis_wait_reason='analysis_settings_required', defer_analysis=True)
        self.assertEqual((result['state'],result['stage'],result['error_code']), ('waiting','analyzing','analysis_settings_required'))
        self.assertEqual(result['summary']['analysis']['remaining'],2)
        self.assertFalse(result['summary']['analysis']['ai_called'])
        self.assertTrue(result['can_resume'])
        collector.assert_awaited_once(); executor.assert_not_awaited()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_jobs').fetchone()[0],0)
        self.assertEqual([r[0] for r in self.conn.execute('SELECT route FROM news_run_candidates ORDER BY rowid')],
            ['analysis_unconfigured','analysis_unconfigured','excluded'])
        self.assertFalse(any(row['created'] for row in self.candidates()))
        visible = jobs.list_profile_results(self.conn,'p')['items']
        self.assertEqual(sum(item['state']=='analysis_unconfigured' for item in visible),2)
        self.assertNotIn('원문 지원',json.dumps(visible,ensure_ascii=False))

    async def test_empty_or_excluded_only_source_finishes_even_without_ai(self):
        register_candidates(self.conn, self.run, 1, [dict(title='제외 안내',link='https://example.invalid/3',content='본문')],
            model=self.saved['analysis_model'], defer_analysis=True)
        executor = AsyncMock()
        result = await workflows.run_workflow(self.workflow, client=None, scorer=None, analysis_model='not-configured',
            threshold=None, call_db=self.db, collector=AsyncMock(return_value={'state':'finished'}),
            executor=executor, analysis_wait_reason='authentication_required', defer_analysis=True)
        self.assertEqual(result['state'],'complete')
        self.assertEqual(result['summary']['analysis']['remaining'],0)
        executor.assert_not_awaited()

    async def test_completed_sources_are_reused_when_collection_resumes_without_ai(self):
        first = {'state':'waiting','stop_reason':'site_collection_failed','registered':1}
        async def collect(run_id, **kwargs):
            self.candidates()
            return first if collector.await_count == 1 else {'state':'finished','registered':3}
        collector, executor = AsyncMock(side_effect=collect), AsyncMock()
        kwargs = dict(client=None, scorer=None, analysis_model='not-configured', threshold=None,
            call_db=self.db, collector=collector, executor=executor,
            analysis_wait_reason='local_budget_exhausted', defer_analysis=True)
        result = await workflows.run_workflow(self.workflow, **kwargs)
        self.assertEqual((result['state'],result['stage']),('waiting','collecting'))
        result = await workflows.run_workflow(self.workflow, **kwargs)
        self.assertEqual((result['state'],result['stage']),('waiting','analyzing'))
        self.assertEqual(collector.call_args.kwargs['previous_collection'], first)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_run_candidates').fetchone()[0],3)
        executor.assert_not_awaited()

    def prepare_initial(self):
        self.candidates()
        workflows._transition(self.conn, self.workflow, 'running','analyzing')
        workflows._transition(self.conn, self.workflow, 'waiting','analyzing', summary={'collection':{'state':'finished'}})
        return {'mode':'direct-body-bounded-v1','analysis_model':'first-model','headless':True}

    def test_first_setup_binds_once_preserving_original_payload_and_profile(self):
        settings = self.prepare_initial()
        before = profiles.get_run_snapshot(self.conn, self.run)
        payloads = [r[0] for r in self.conn.execute('SELECT payload_json FROM news_run_candidates ORDER BY rowid')]
        workflows.initialize_analysis_settings(self.conn, self.workflow, expected=self.saved, settings=settings)
        self.assertEqual(profiles.get_run_snapshot(self.conn, self.run), before)
        self.assertEqual([r[0] for r in self.conn.execute('SELECT payload_json FROM news_run_candidates ORDER BY rowid')],payloads)
        self.assertEqual([tuple(r) for r in self.conn.execute('SELECT model,attempts,state FROM news_analysis_jobs')], [('first-model',0,'queued')])
        self.assertEqual([r[0] for r in self.conn.execute('SELECT route FROM news_run_candidates ORDER BY rowid')], ['matched','semantic_candidate','excluded'])
        with self.assertRaises(ValueError):
            workflows.initialize_analysis_settings(self.conn, self.workflow, expected=self.saved, settings={**settings,'analysis_model':'second-model'})
        self.assertEqual(workflows.get_workflow_settings(self.conn, self.workflow),settings)

    def test_first_setup_rejects_any_existing_job_even_with_zero_attempts(self):
        settings = self.prepare_initial()
        jobs.enqueue(self.conn, self.run, payload=dict(title='지원',link='https://example.invalid/x',content='원문',site_name='시험'),model='old-model',recipe='old-recipe')
        with self.assertRaises(ValueError):
            workflows.initialize_analysis_settings(self.conn, self.workflow, expected=self.saved, settings=settings)
        self.assertEqual(workflows.get_workflow_settings(self.conn,self.workflow), self.saved)

    def test_first_setup_rejects_execution_and_semantic_decision_records(self):
        settings = self.prepare_initial()
        for table in ('news_execution_settings','news_semantic_decisions'):
            with self.subTest(table=table):
                self.conn.execute('CREATE TABLE '+table+'(run_id TEXT)')
                self.conn.execute('INSERT INTO '+table+' VALUES (?)',(self.run,))
                with self.assertRaises(ValueError):
                    workflows.initialize_analysis_settings(self.conn,self.workflow,expected=self.saved,settings=settings)
                self.assertEqual(workflows.get_workflow_settings(self.conn,self.workflow),self.saved)
                self.conn.execute('DROP TABLE '+table)

    def test_first_setup_rolls_back_entire_batch_on_partial_save_failure(self):
        settings = self.prepare_initial()
        self.conn.execute("CREATE TRIGGER fail_first_setup BEFORE UPDATE OF settings_json ON news_profile_workflows BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            workflows.initialize_analysis_settings(self.conn,self.workflow,expected=self.saved,settings=settings)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_jobs').fetchone()[0],0)
        self.assertEqual([r[0] for r in self.conn.execute('SELECT route FROM news_run_candidates ORDER BY rowid')], ['analysis_unconfigured','analysis_unconfigured','excluded'])
        self.assertEqual(workflows.get_workflow_settings(self.conn,self.workflow),self.saved)

    def test_deferred_flag_never_accepts_truthy_strings(self):
        for value in ('true',1,None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                register_candidates(self.conn,self.run,1,[dict(title='지원',link='https://example.invalid',content='원문')],model='m',defer_analysis=value)
