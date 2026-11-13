import sqlite3
import unittest
from dataclasses import replace
from unittest.mock import ANY, AsyncMock, patch
from fastapi import HTTPException
from services import news_default_condition as defaults
from services import news_profile_store as store
from services.news_profile import NewsProfile
from services.news_auto_sources import automatic_sources


class DefaultConditionTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:')
        self.conn.execute('CREATE TABLE sites(id INTEGER PRIMARY KEY,name TEXT,url TEXT,selectors TEXT)')

    def tearDown(self):
        self.conn.close()

    def profile(self, revision=1):
        return NewsProfile(profile_id=defaults.PROFILE_ID, revision=revision, name='기본', region='전국',
            life_cycles=('노년',), min_age_months=780, max_age_months=None,
            semantic_text='노년 복지 소식', site_ids=())

    def test_read_does_not_create_or_migrate(self):
        self.assertIsNone(defaults.read(self.conn))
        self.assertIsNone(self.conn.execute("SELECT name FROM sqlite_master WHERE name='news_profiles'").fetchone())

    def test_save_versions_and_conflicts(self):
        defaults.save(self.conn,self.profile(),0)
        updated=replace(self.profile(2),region='서울특별시')
        defaults.save(self.conn,updated,1)
        self.assertEqual(defaults.read(self.conn),automatic_sources(updated))
        self.assertEqual(store.get_profile(self.conn,defaults.PROFILE_ID,1).region,'전국')
        with self.assertRaises(store.ProfileConflict):defaults.save(self.conn,updated,1)
        self.assertEqual(defaults.read(self.conn).revision,2)

    def test_site_sync_is_idempotent_and_preserves_old_revision(self):
        defaults.save(self.conn,self.profile(),0)
        self.conn.execute("INSERT INTO sites VALUES(7,'기관','https://example.org','{}')")
        self.assertEqual(defaults.synchronize_sites(self.conn).site_ids,('7',))
        self.assertEqual(defaults.synchronize_sites(self.conn).revision,2)
        self.assertEqual(store.get_profile(self.conn,defaults.PROFILE_ID,1).site_ids,())
        self.conn.execute('DELETE FROM sites')
        self.assertEqual(defaults.synchronize_sites(self.conn).site_ids,())

    def test_targets_do_not_add_a_source_when_no_sites_registered(self):
        saved=defaults.save(self.conn,self.profile(),0)
        self.assertEqual(saved.sources,('sites',))
        self.assertEqual(saved.bokjiro_mode,'unsupported')

    def test_old_bokjiro_only_condition_is_normalized_to_registered_sites(self):
        profile=replace(self.profile(), sources=('bokjiro',), bokjiro_mode='official_tags_v1', region='부산광역시')
        defaults.save(self.conn,profile,0)
        self.conn.execute("INSERT INTO sites VALUES(7,'기관','https://example.org','{}')")
        self.assertEqual(defaults.synchronize_sites(self.conn).sources,('sites',))
        self.assertEqual(profile.require_bokjiro_support()['life_cycles'],('노년',))

    def test_site_sync_does_not_restore_combined_sources(self):
        profile=replace(self.profile(), sources=('sites','bokjiro'), bokjiro_mode='official_tags_v1')
        defaults.save(self.conn,profile,0)
        self.conn.execute("INSERT INTO sites VALUES(7,'기관','https://example.org','{}')")
        updated=defaults.synchronize_sites(self.conn)
        self.assertEqual(updated.sources,('sites',))
        self.assertEqual(updated.site_ids,('7',))

    def test_legacy_bokjiro_mode_rejected_for_default(self):
        with self.assertRaises(ValueError):
            defaults.save(self.conn,replace(self.profile(),sources=('bokjiro',),bokjiro_mode='legacy_infant_v1'),0)

    def test_unknown_target_skips_bokjiro_without_blocking_save(self):
        saved=defaults.save(self.conn,replace(self.profile(),life_cycles=('자체 대상',)),0)
        self.assertEqual(saved.sources,('sites',))
        self.assertEqual(saved.bokjiro_mode,'unsupported')

    def test_detail_settings_survive_save_and_site_synchronization(self):
        from services.news_analysis_context import NewsAnalysisContext
        profile = replace(self.profile(), keywords=('돌봄',), excluded_keywords=('채용',),
                          ai_prompt='신청 방법을 쉽게 설명해주세요.')
        defaults.save(self.conn, profile, 0)
        self.conn.execute("INSERT INTO sites VALUES(7,'기관','https://example.invalid','{}')")
        synchronized = defaults.synchronize_sites(self.conn)
        self.assertEqual(synchronized.keywords, profile.keywords)
        self.assertEqual(synchronized.excluded_keywords, profile.excluded_keywords)
        self.assertEqual(synchronized.ai_prompt, profile.ai_prompt)
        self.assertEqual(synchronized.min_age_months, profile.min_age_months)
        context = NewsAnalysisContext(defaults.read(self.conn))
        self.assertEqual(context.route_title('돌봄 지원 안내'), ('matched', ('돌봄',)))
        self.assertEqual(context.route_title('돌봄 담당자 채용'), ('excluded', ()))


class DefaultSourceApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_derives_sources_regardless_of_old_source_choice(self):
        from services import news_profiles_api as api
        conn=sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.execute('CREATE TABLE sites(id INTEGER PRIMARY KEY)')
        conn.execute('INSERT INTO sites VALUES(7)')
        async def execute(fn): return fn(conn)
        for revision,sources in enumerate([['bokjiro'],['sites','bokjiro'],['sites']]):
            request=api.DefaultConditionRequest(expected_revision=revision,settings=api.ProfileSettings(
                name='수집',region='부산광역시',life_cycles=['노년'],sources=sources))
            with patch.object(api,'_execute',side_effect=execute):
                result=await api.save_default_condition(request)
            self.assertEqual(result['profile']['sources'],['sites'])
            self.assertEqual(result['profile']['site_ids'],['7'])
            self.assertEqual(result['profile']['bokjiro_mode'],'unsupported')

    async def test_retired_infant_route_cannot_collect(self):
        import api_server
        from fastapi import BackgroundTasks
        tasks=BackgroundTasks()
        with self.assertRaises(HTTPException) as error:
            await api_server.bokjiro_collect_infant(tasks)
        self.assertEqual(error.exception.status_code,410)
        self.assertIn('등록한 기관 사이트에서만',error.exception.detail)
        self.assertNotIn('자동으로 함께',error.exception.detail)
        self.assertEqual(tasks.tasks,[])



class DefaultExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # These tests isolate dispatch, not the OS admission/recovery contract.
        # The latter uses real temporary DB/locks in test_news_workflow_ownership.
        from types import SimpleNamespace
        from contextlib import nullcontext
        from services import news_profiles_api as api
        self.ownership = SimpleNamespace(transferred=False, close=lambda: None, pin=nullcontext)
        gate = patch.object(api, '_acquire_workflow_ownership',
                            AsyncMock(return_value=self.ownership))
        gate.start()
        self.addCleanup(gate.stop)

    async def test_all_entrypoints_use_default_dispatch(self):
        import api_server
        from services import news_profiles_api as api
        from fastapi import BackgroundTasks
        from services import app_lifecycle
        with patch.object(api,'start_default_condition',AsyncMock(return_value={'workflow_id':'fixture'})) as dispatch, \
             patch.object(app_lifecycle,'shutdown_requested',False):
            result=await api_server.start_scrape(api_server.ScrapeRequest(headless=False),BackgroundTasks())
            self.assertEqual(result['workflow_id'],'fixture')
            dispatch.assert_awaited_with(headless=False)
            self.assertTrue(await api_server._scheduled_scrape_job())
            self.assertTrue(dispatch.await_args.kwargs['wait'])
            await api_server._run_once_scrape()
            dispatch.assert_awaited_with(headless=True,wait=True)

    async def test_schedule_refuses_unready_default(self):
        import api_server
        from services import news_profiles_api as api
        from services import app_lifecycle
        with patch.object(api,'start_default_condition',AsyncMock(side_effect=HTTPException(409,'품질 검증 필요'))), \
             patch.object(app_lifecycle,'shutdown_requested',False), \
             patch.object(api_server,'run_scraper_task',AsyncMock()) as legacy:
            self.assertFalse(await api_server._scheduled_scrape_job())
            await api_server._run_once_scrape()
            legacy.assert_not_awaited()

    async def test_only_absent_default_with_proven_history_allows_legacy(self):
        from services import news_profiles_api as api
        with patch.object(api,'_execute',AsyncMock(side_effect=[None, True])), patch.object(api,'_start_profile_workflow',AsyncMock()) as start:
            self.assertIsNone(await api.start_default_condition())
            start.assert_not_awaited()

    async def test_absent_default_without_proven_history_never_allows_legacy(self):
        from services import news_profiles_api as api
        with patch.object(api,'_execute',AsyncMock(side_effect=[None, False])), patch.object(api,'_start_profile_workflow',AsyncMock()) as start:
            with self.assertRaises(HTTPException) as caught:
                await api.start_default_condition()
            self.assertEqual(caught.exception.status_code, 409)
            start.assert_not_awaited()

    async def test_unready_default_never_falls_back_or_starts(self):
        from services import news_profiles_api as api
        p=NewsProfile(profile_id=defaults.PROFILE_ID,revision=1,name='기본',region='전국')
        with patch.object(api,'_execute',AsyncMock(return_value=p)), \
             patch.object(api,'workflow_options',AsyncMock(return_value={'enabled':False,'reason':'품질 검증 필요'})), \
             patch.object(api,'_start_profile_workflow',AsyncMock()) as start:
            with self.assertRaises(HTTPException):await api.start_default_condition(wait=True)
            start.assert_not_awaited()

    async def test_ready_uses_exact_revision(self):
        from services import news_profiles_api as api
        p=NewsProfile(profile_id=defaults.PROFILE_ID,revision=4,name='기본',region='전국')
        with patch.object(api,'_execute',AsyncMock(return_value=p)), \
             patch.object(api,'workflow_options',AsyncMock(return_value={'enabled':True})), \
             patch.object(api,'_start_profile_workflow',AsyncMock(return_value={'workflow_id':'test'})) as start:
            await api.start_default_condition(headless=False)
            self.assertEqual(start.await_args.args[1].expected_revision,4)
            self.assertFalse(start.await_args.args[1].headless)

    async def test_schedule_resumes_waiting_work_without_new_run(self):
        from services import news_profiles_api as api
        p=NewsProfile(profile_id=defaults.PROFILE_ID,revision=4,name='기본',region='전국')
        with patch.object(api,'_execute',AsyncMock(side_effect=[p,'waiting-id',{'state':'complete'}])), \
             patch.object(api,'workflow_options',AsyncMock(return_value={'enabled':True})), \
             patch.object(api,'_resume_workflow',AsyncMock(return_value={'workflow_id':'waiting-id'})) as resume, \
             patch.object(api,'_start_profile_workflow',AsyncMock()) as start:
            result = await api.start_default_condition(wait=True)
            self.assertEqual(result['state'], 'complete')
            resume.assert_awaited_once_with('waiting-id', ownership=ANY, allow_initial_setup=False);start.assert_not_awaited()
