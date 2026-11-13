"""Site-only news scope; tests use isolated DBs and never call remote services."""
from dataclasses import replace
import unittest
from unittest.mock import patch

import database
from services import news_profiles_api as api, news_profile_store as profiles
from services import news_profile_workflow as workflows
from services.news_auto_sources import upgrade_saved_sources, SOURCE_SCOPE_CHANGED_REASON
from tests import test_news_jobs_api as fixture


class TestRegisteredSourcesApi(unittest.TestCase):
    setUp = fixture.TestNewsJobsApi.setUp
    tearDown = fixture.TestNewsJobsApi.tearDown

    def old_run(self, state='waiting'):
        with database.get_connection() as conn:
            conn.execute("INSERT INTO sites(id,name,url,selectors) VALUES (1,'기관','https://example.invalid','{}')")
            current=profiles.get_profile(conn,'p')
            old=replace(current,revision=2,life_cycles=('영유아',),sources=('sites','bokjiro'),
                        bokjiro_mode='official_tags_v1')
            profiles.save_revision(conn,old,expected_revision=1)
            run=profiles.prepare_run(conn,'p',expected_revision=2,
                sites=[dict(id=1,name='기관',url='https://example.invalid')])
            workflow=workflows.create_workflow(conn,run,settings=api._pending_workflow_settings())
            if state == 'waiting':
                workflows._transition(conn,workflow,'running','collecting')
                workflows._transition(conn,workflow,'waiting','collecting',summary={'preserved':'old result'})
            snapshot=conn.execute('SELECT * FROM news_run_snapshots WHERE run_id=?',(run,)).fetchone()
            before=tuple(conn.execute('SELECT * FROM news_profile_workflows WHERE workflow_id=?',(workflow,)).fetchone())
            upgrade_saved_sources(conn)
            conn.commit()
        return run,workflow,tuple(snapshot),before

    def test_api_save_ignores_old_client_source_expansion(self):
        response=self.client.post('/api/news/profiles',json=dict(name='영유아',region='전국',
            life_cycles=['영유아'],sources=['sites','bokjiro'],site_ids=['1']))
        self.assertEqual(response.status_code,200,response.text)
        current=self.client.get('/api/news/profiles/'+response.json()['profile_id']).json()['profile']
        self.assertEqual(current['sources'],['sites'])
        self.assertEqual(current['bokjiro_mode'],'unsupported')
        self.assertEqual(current['site_ids'],['1'])

    def test_missing_sites_never_falls_back_to_bokjiro(self):
        response=self.client.post('/api/news/profiles',json=dict(name='영유아',region='전국',
            life_cycles=['영유아'],sources=['bokjiro']))
        profile_id=response.json()['profile_id']
        with patch.object(api,'_analysis_client',return_value=None):
            options=self.client.get(f'/api/news/profiles/{profile_id}/workflow-options').json()
        self.assertFalse(options['enabled'])
        self.assertEqual(options['selection']['sources'],['sites'])
        self.assertIn('수집 사이트',options['reason'])

    def test_old_run_resume_refused_without_changing_history_or_launching(self):
        run,workflow,snapshot,before=self.old_run()
        with patch.object(api,'_launch_workflow') as launch:
            response=self.client.post(f'/api/news/workflows/{workflow}/resume')
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(response.json()['detail']['error'],SOURCE_SCOPE_CHANGED_REASON)
        launch.assert_not_called()
        with database.get_connection() as conn:
            self.assertEqual(tuple(conn.execute('SELECT * FROM news_run_snapshots WHERE run_id=?',(run,)).fetchone()),snapshot)
            self.assertEqual(tuple(conn.execute('SELECT * FROM news_profile_workflows WHERE workflow_id=?',(workflow,)).fetchone()),before)
            self.assertEqual(profiles.get_profile(conn,'p').sources,('sites',))
            self.assertEqual(profiles.get_profile(conn,'p',2).sources,('sites','bokjiro'))

    def test_old_waiting_source_does_not_block_new_site_run(self):
        _run,workflow,_snapshot,_before=self.old_run()
        with patch.object(api,'_analysis_client',return_value=None), patch.object(api,'_launch_workflow') as launch:
            response=self.client.post('/api/news/profiles/p/workflows',json={'expected_revision':3})
        self.assertEqual(response.status_code,200,response.text)
        launch.assert_called_once()
        with database.get_connection() as conn:
            current=profiles.get_run_snapshot(conn,response.json()['run_id'])
            self.assertEqual(current['profile']['sources'],['sites'])
            self.assertEqual([str(site['id']) for site in current['sites']],['1'])
            self.assertEqual(workflows.get_workflow(conn,workflow)['state'],'waiting')

    def test_old_ready_source_is_not_a_backlog_for_sites_only_condition(self):
        self.old_run(state='ready')
        with database.get_connection() as conn:
            self.assertEqual(api._collection_backlog(conn,'p',3),(False,False))

    def test_unmigrated_source_cannot_be_prepared_or_started(self):
        with database.get_connection() as conn:
            current=profiles.get_profile(conn,'p')
            profiles.save_revision(conn,replace(current,revision=2,life_cycles=('영유아',),
                sources=('sites','bokjiro'),bokjiro_mode='official_tags_v1'),expected_revision=1)
        with patch.object(api,'_analysis_client',return_value=None), patch.object(api,'_launch_workflow') as launch:
            for action in ('prepare','workflows'):
                response=self.client.post(f'/api/news/profiles/p/{action}',json={'expected_revision':2})
                self.assertEqual(response.status_code,409,response.text)
            launch.assert_not_called()
