"""Independent blank-profile audit; disposable DB and fake external boundaries only.

Assertions enforce current first-use and legacy compatibility boundaries.
Run through scripts/run_isolated_tests.py, not a live profile.
"""
import asyncio
from contextlib import nullcontext
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from google.genai import types
import api_server
import database
import utils
from services import ai_coordinator as quota, ai_rate_policy as rate, ai_verified_input as verified
from services import news_profiles_api as api, news_default_condition as defaults
from services import news_profile_store as profiles, news_profile_workflow as workflows
from services import news_analysis_store as jobs, news_unified_feed as feed, page_coverage
from services.news_candidate_intake import register_candidates
from services.news_direct_analysis import queue_candidates, DirectAnalysis
from services.news_analysis_worker import process_job
from tests import test_news_custom_input_budget as response_fixture


class BlankFirstUseProbe(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        database.close_all_connections()
        self.temp = tempfile.TemporaryDirectory(prefix='blank-news-audit-')
        self.root = Path(self.temp.name)
        self.patches = [
            patch.object(database, 'DB_FILE', str(self.root / 'empty.db')),
            patch.object(utils, 'get_config_path', return_value=str(self.root / 'config.json')),
            patch.object(quota, '_state_path', return_value=str(self.root / 'quota.json')),
            patch.dict(os.environ, {'WELFARE_DESKTOP_TOKEN':'blank-test', 'GOOGLE_API_KEY':'',
                'NOTION_TOKEN':'', 'NOTION_DATABASE_ID':'', 'AI_DAILY_REQUEST_LIMIT':'20',
                'CLASSIFY_MODEL':rate.MODELS[0]}),
            patch.object(api_server.app_lifecycle, 'shutdown_requested', False),
        ]
        for item in self.patches:
            item.start()
        os.environ.pop('NEWS_PROFILE_SEMANTIC_THRESHOLD', None)
        database.init_db()
        quota._reset_for_tests()
        verified._reset_for_tests()
        self.client = TestClient(api_server.app)
        self.client.headers['X-Welfare-Session'] = 'blank-test'

    def tearDown(self):
        self.client.close()
        database.close_all_connections()
        quota._reset_for_tests()
        verified._reset_for_tests()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    async def db(self, fn, *args, **kwargs):
        with database.get_connection() as conn:
            return fn(conn, *args, **kwargs)

    def save_condition(self):
        response = self.client.put('/api/news/default-condition', json={
            'expected_revision':0, 'settings':{'name':'첫 조건', 'region':'전국'}})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['profile']

    def add_site(self):
        utils.save_sites([dict(name='합성센터', url='https://example.invalid/board',
                              selectors={}, registration_status='needs_check')])

    async def setup_run(self):
        self.save_condition()
        self.add_site()
        def create(conn):
            profile = defaults.read(conn)
            run = profiles.prepare_run(conn, profile.profile_id, expected_revision=profile.revision,
                                       sites=utils.load_sites())
            workflow = workflows.create_workflow(conn, run, settings=api._runtime_workflow_settings())
            return run, workflow
        return await self.db(create)

    def test_actual_init_keeps_sites_keywords_profiles_and_feed_empty(self):
        with database.get_connection() as conn:
            for table in ('sites', 'keywords', 'excluded_keywords', 'welfare_items', 'news_profiles'):
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0], 0, table)
            self.assertFalse(defaults.legacy_collection_available(conn))
        result = self.client.get('/api/news/default-condition').json()
        self.assertIsNone(result['profile'])
        self.assertFalse(result['legacy_collection_available'])
        self.assertEqual(result['initial_settings'], {})
        listing = self.client.get('/api/news/feed').json()
        self.assertEqual(listing['counts'], dict(ready=0, review=0, processing=0, records=0))
        self.assertFalse(listing['configured'])
        self.assertEqual(quota.status()['quota_policy']['state'], 'unconfirmed')
        self.assertEqual(quota.input_token_capacity(rate.MODELS[0]), 6000)

    def test_zero_sites_condition_then_actual_first_registration_enables_collection_without_key(self):
        first = self.save_condition()
        self.assertEqual((first['site_ids'], first['keywords'], first['excluded_keywords']), ([], [], []))
        with patch.object(api, '_analysis_client', return_value=None):
            before = self.client.get('/api/news/profiles/default-news/workflow-options').json()
            self.assertFalse(before['enabled'])
            self.add_site()
            after = self.client.get('/api/news/profiles/default-news/workflow-options').json()
        self.assertTrue(after['enabled'])
        self.assertFalse(after['analysis_ready'])
        self.assertEqual(after['analysis_reason_code'], 'authentication_required')
        self.assertEqual(after['selection']['revision'], 2)
        self.assertEqual(len(after['selection']['sites']), 1)
        with database.get_connection() as conn:
            self.assertEqual(profiles.get_profile(conn, 'default-news', 1).site_ids, ())
            self.assertEqual(defaults.read(conn).site_ids, ('1',))

    async def test_no_key_first_candidate_is_preserved_as_processing_not_false_empty_completion(self):
        run, workflow = await self.setup_run()
        async def collect(run_id, **kwargs):
            await self.db(register_candidates, run_id, 1, [dict(title='첫 사업 안내',
                link='https://example.invalid/post/1', content=response_fixture.SHORT)], model=rate.MODELS[0])
            return dict(state='finished', registered=1)
        executor = AsyncMock()
        result = await workflows.run_workflow(workflow, client=None, scorer=DirectAnalysis(),
            analysis_model=rate.MODELS[0], threshold=None, call_db=self.db, collector=collect,
            executor=executor, analysis_wait_reason='authentication_required')
        self.assertEqual((result['state'], result['can_resume']), ('waiting', True))
        self.assertEqual(result['summary']['analysis']['remaining'], 1)
        executor.assert_not_awaited()
        listing = await self.db(feed.read_feed, 'default-news', view='processing')
        self.assertEqual(listing['counts']['ready'], 0)
        self.assertEqual(listing['total'], 1)
        self.assertFalse(listing['items'][0]['selectable'])

    async def test_true_zero_candidates_finishes_without_key_or_wait_loop(self):
        run, workflow = await self.setup_run()
        executor = AsyncMock()
        result = await workflows.run_workflow(workflow, client=None, scorer=DirectAnalysis(),
            analysis_model=rate.MODELS[0], threshold=None, call_db=self.db,
            collector=AsyncMock(return_value=dict(state='finished', registered=0)),
            executor=executor, analysis_wait_reason='authentication_required')
        self.assertEqual((result['state'], result['can_resume']), ('complete', False))
        executor.assert_not_awaited()

    async def test_first_current_recipe_success_is_selectable_exportable_and_missing_notion_is_local_rejection(self):
        run, _ = await self.setup_run()
        await self.db(register_candidates, run, 1, [dict(title='청소년 교육비 지원',
            link='https://example.invalid/post/1', content=response_fixture.SHORT,
            source_page=page_coverage.make())], model=rate.MODELS[0])
        self.assertEqual(await self.db(queue_candidates, run, rate.MODELS[0], 10), 1)
        job = await self.db(lambda c: c.execute('SELECT job_id FROM news_analysis_jobs').fetchone()[0])
        raw = response_fixture.CurrentCustomInputBudget.response(self)
        provider = AsyncMock(return_value=types.GenerateContentResponse(candidates=[types.Candidate(
            content=types.Content(parts=[types.Part(text=json.dumps(raw, ensure_ascii=False))]), finish_reason='STOP')]))
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=provider,
            count_tokens=AsyncMock(return_value=SimpleNamespace(total_tokens=1000)))))
        with patch.object(verified, '_take_count_slot', new=AsyncMock(return_value=True)):
            self.assertEqual(await process_job(job, client, call_db=self.db, clock=lambda:0), 'complete')
        row = (await self.db(feed.read_feed, 'default-news'))['items'][0]
        self.assertTrue(row['selectable'])
        self.assertNotEqual(row['support_detail'], response_fixture.SHORT)
        self.assertEqual(row['presentation_kind'], 'source_excerpt')
        self.assertIn('센터 방문 접수', row['support_detail'])
        self.assertIn('개인이 신청합니다.', row['support_detail'])
        prepare = self.client.post('/api/news/feed/prepare', json={'action':'html', 'confirmed':True,
            'items':[dict(id=row['id'], fingerprint=row['fingerprint'])]})
        self.assertEqual(prepare.status_code, 200, prepare.text)
        from services import notion_service
        with patch.object(api_server, '_poster_for_news_item', new=AsyncMock(return_value=('', False))), \
             patch.object(notion_service, 'create_event_page', new=AsyncMock()) as send:
            html = self.client.post('/api/news/gallery-html', json={'ids':prepare.json()['ids']})
            self.assertEqual(html.status_code, 200, html.text)
            self.assertIn('센터 방문 접수', html.text)
            upload = self.client.post('/api/notion/upload', json={'ids':prepare.json()['ids']})
            self.assertEqual(upload.status_code, 400)
            self.assertIn('Notion 토큰/DB', upload.json()['detail'])
            send.assert_not_awaited()
        self.assertEqual(provider.await_count, 1)

    async def test_blank_profile_manual_and_scheduled_start_cannot_select_legacy_path(self):
        self.add_site()  # Registration is not evidence of prior legacy usage.
        with database.get_connection() as conn:
            self.assertIsNone(defaults.read(conn))
            self.assertFalse(defaults.legacy_collection_available(conn))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM sites').fetchone()[0], 1)
        owner = SimpleNamespace(transferred=False, close=lambda:None, pin=nullcontext)
        from services import policy_sync
        from fastapi import BackgroundTasks
        for scheduled in (False, True):
            with self.subTest(scheduled=scheduled), \
                 patch.object(api, '_acquire_workflow_ownership', new=AsyncMock(return_value=owner)), \
                 patch.object(quota, 'try_start_job', new=AsyncMock(return_value=True)), \
                 patch.object(policy_sync, 'maintenance_busy_message', return_value=''), \
                 patch.dict(api_server.SCRAPER_STATUS, {'is_running':False}), \
                 patch.object(api_server, 'run_scraper_task', new=AsyncMock()) as legacy:
                if scheduled:
                    self.assertFalse(await api_server._scheduled_scrape_job(allow_defer=False))
                    self.assertEqual(await api_server._run_once_scrape(), api_server.RUN_ONCE_NOT_READY)
                    legacy.assert_not_awaited()
                else:
                    background = BackgroundTasks()
                    from fastapi import HTTPException
                    with self.assertRaises(HTTPException) as caught:
                        await api_server.start_scrape(api_server.ScrapeRequest(), background)
                    self.assertEqual(caught.exception.status_code, 409)
                    self.assertIn('수집 조건을 먼저 저장', caught.exception.detail)
                    self.assertEqual(len(background.tasks), 0)
                    legacy.assert_not_awaited()

    async def test_absent_default_with_real_legacy_history_remains_compatible(self):
        self.add_site()
        with database.get_connection() as conn:
            conn.execute("INSERT INTO scrape_runs(site_id,site_name) VALUES (1,'이전 사용 기관')")
            conn.commit()
            self.assertTrue(defaults.legacy_collection_available(conn))
        owner = SimpleNamespace(transferred=False, close=lambda:None, pin=nullcontext)
        with patch.object(api, '_acquire_workflow_ownership', new=AsyncMock(return_value=owner)):
            self.assertIsNone(await api.start_default_condition())
