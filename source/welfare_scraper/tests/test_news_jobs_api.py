import os
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

from fastapi.testclient import TestClient
import api_server
import database
from services import news_profile_store as profiles, news_analysis_store as jobs
from services.news_profile import NewsProfile


class TestNewsJobsApi(unittest.TestCase):
    def setUp(self):
        database.close_all_connections()
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(database, 'DB_FILE', os.path.join(self.temp.name, 'test.db'))
        self.db_patch.start()
        database.init_db()
        self.token_patch = patch.dict(os.environ, {'WELFARE_DESKTOP_TOKEN': 'test-session'})
        self.token_patch.start()
        self.client = TestClient(api_server.app)  # no lifespan or schedules
        self.client.headers['X-Welfare-Session'] = 'test-session'
        with database.get_connection() as conn:
            profiles.create_profile(conn, NewsProfile('p', 1, '시험', '서울', site_ids=('1',), semantic_text='복지 지원 소식'))
            self.run = profiles.prepare_run(conn, 'p', expected_revision=1,
                sites=[dict(id=1, name='시험', url='https://example.invalid')])
            self.job = jobs.enqueue(conn, self.run,
                payload=dict(title='제목', link='https://example.invalid', site_name='시험', content='private-text'),
                model='test-model', recipe='test-recipe')
        self.url = f'/api/news/jobs/{self.job}'

    def tearDown(self):
        from services import news_profiles_api
        for task in list(news_profiles_api._WORKFLOW_TASKS.values()):
            task.cancel()
        news_profiles_api._WORKFLOW_TASKS.clear()
        self.client.close()
        database.close_all_connections()
        self.db_patch.stop()
        self.token_patch.stop()
        self.temp.cleanup()

    def test_auth_protects_read_and_write(self):
        for path, method in [(self.url, self.client.get), (self.url+'/cancel', self.client.post)]:
            response = method(path, headers={'X-Welfare-Session': 'wrong'})
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get(self.url).json()['state'], 'queued')

    def test_profile_results_keep_review_and_hide_private_fields(self):
        with database.get_connection() as conn:
            token = jobs.claim(conn, self.job, now=0)
            jobs.complete(conn, self.job, token, {'analysis': {'secret': 'private-analysis'},
                'target_status': 'review', 'accepted': False, 'review_required': True})
        response = self.client.get('/api/news/profiles/p/results')
        self.assertEqual(response.status_code, 200)
        item = response.json()['items'][0]
        self.assertEqual(item['target_status'], 'review')
        self.assertEqual(item['revision'], 1)
        self.assertEqual(item['title'], '제목')
        for private in ('private-text', 'private-analysis', 'claim_token', 'input_json'):
            self.assertNotIn(private, response.text)
        self.assertEqual(self.client.get('/api/news/profiles/p/results',
            headers={'X-Welfare-Session': 'wrong'}).status_code, 403)

    def test_profile_result_pagination_scope_and_missing_profile(self):
        with database.get_connection() as conn:
            jobs.enqueue(conn, self.run, payload=dict(title='두번째', link='https://example.invalid/2',
                site_name='시험', content='secret'), model='m', recipe='r')
            profiles.create_profile(conn, NewsProfile('empty', 1, '다른 기준', '전국'))
        page = self.client.get('/api/news/profiles/p/results?limit=1').json()
        self.assertTrue(page['has_more'])
        self.assertEqual(page['items'][0]['title'], '두번째')
        second = self.client.get('/api/news/profiles/p/results?limit=1&offset=1').json()
        self.assertFalse(second['has_more'])
        self.assertEqual(second['items'][0]['job_id'], self.job)
        self.assertIsNone(second['items'][0]['target_status'])
        self.assertEqual(self.client.get('/api/news/profiles/empty/results').json()['items'], [])
        self.assertEqual(self.client.get('/api/news/profiles/missing/results').status_code, 404)
        self.assertEqual(self.client.get('/api/news/profiles/p/results?limit=101').status_code, 422)

    def test_candidate_records_visible_once_and_without_source_body(self):
        from services.news_candidate_intake import register_candidates
        from dataclasses import replace
        with database.get_connection() as conn:
            profile = profiles.get_profile(conn, 'p')
            profiles.save_revision(conn, replace(profile, revision=2, keywords=('지원',), excluded_keywords=('채용',)), expected_revision=1)
            run = profiles.prepare_run(conn, 'p', expected_revision=2,
                sites=[dict(id=1, name='시험', url='https://example.invalid')])
            register_candidates(conn, run, '1', [dict(title=t, link='https://example.invalid', content='private-candidate')
                for t in ('지원 소식', '돌봄 소식', '채용 소식')], model='fixed')
        response = self.client.get('/api/news/profiles/p/results')
        self.assertEqual(response.status_code, 200)
        items = response.json()['items']
        self.assertEqual(len(items), 4)  # original job + three new candidates
        self.assertEqual(len({i['record_id'] for i in items}), 4)
        self.assertEqual(sum(i['title'] == '지원 소식' for i in items), 1)
        self.assertEqual({i['state'] for i in items}, {'queued', 'semantic_candidate', 'excluded'})
        self.assertNotIn('private-candidate', response.text)
        seen = []
        for offset in range(4):
            page = self.client.get(f'/api/news/profiles/p/results?limit=1&offset={offset}').json()
            seen.append(page['items'][0]['record_id'])
        self.assertEqual(seen, [i['record_id'] for i in items])

    def test_metadata_does_not_leak_input_or_token(self):
        response = self.client.get(f'/api/news/runs/{self.run}/jobs')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('private-text', response.text)
        self.assertNotIn('claim_token', response.text)
        self.assertNotIn('input_json', response.text)

    def test_cancel_resume_is_explicit_and_does_not_launch(self):
        self.assertEqual(self.client.post(self.url+'/cancel').json()['job']['state'], 'failed')
        response = self.client.post(self.url+'/resume', json={'expected_attempts': 0})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['scheduled'])
        self.assertEqual(response.json()['job']['state'], 'queued')
        self.assertEqual(self.client.post(self.url+'/resume', json={'expected_attempts': 0}).status_code, 409)

    def test_running_cannot_resume_early(self):
        with database.get_connection() as conn:
            jobs.claim(conn, self.job, now=0)
        self.client.post(self.url+'/cancel')
        self.assertEqual(self.client.post(self.url+'/resume', json={'expected_attempts': 1}).status_code, 409)

    def test_missing_and_invalid_input(self):
        self.assertEqual(self.client.get('/api/news/jobs/missing').status_code, 404)
        self.assertEqual(self.client.get('/api/news/runs/missing/jobs').status_code, 404)
        self.assertEqual(self.client.get(f'/api/news/runs/{self.run}/jobs?limit=501').status_code, 422)
        for value in (True, -1, '0'):
            self.assertEqual(self.client.post(self.url+'/resume', json={'expected_attempts': value}).status_code, 422)

    def test_restore_admission_rejects_database_access(self):
        with patch.object(database, 'run_db', side_effect=database.DatabaseMaintenanceError()):
            self.assertEqual(self.client.post(self.url+'/cancel').status_code, 503)
            self.assertEqual(self.client.get(self.url).status_code, 503)

    def test_profile_create_edit_conflict_clone_archive(self):
        settings = {'name': '새 대상', 'region': '서울', 'site_ids': ['1']}
        created = self.client.post('/api/news/profiles', json=settings)
        self.assertEqual(created.status_code, 200)
        pid = created.json()['profile_id']
        self.assertTrue(created.json()['execution_enabled'])
        url = f'/api/news/profiles/{pid}'
        edit = {'expected_revision': 1, 'settings': settings | {'name': '변경'}}
        self.assertEqual(self.client.put(url, json=edit).json()['revision'], 2)
        self.assertEqual(self.client.put(url, json=edit).status_code, 409)
        clone = self.client.post(url+'/clone', json={'name': '복제'})
        self.assertEqual(clone.status_code, 200)
        self.assertNotEqual(clone.json()['profile_id'], pid)
        self.assertEqual(clone.json()['revision'], 1)
        self.assertEqual(self.client.post(url+'/archive').status_code, 200)
        archived = self.client.get(url).json()
        self.assertFalse(archived['execution_enabled'])
        listed = self.client.get('/api/news/profiles').json()['items']
        self.assertNotIn(pid, [p['profile_id'] for p in listed])

    def test_profile_api_normalizes_old_source_choice_and_requires_sites(self):
        from services import news_profiles_api
        settings = {'name': '노년 복지로', 'region': '전국',
                    'life_cycles': ['노년'], 'sources': ['bokjiro']}
        created = self.client.post('/api/news/profiles', json=settings)
        self.assertEqual(created.status_code, 200)
        pid = created.json()['profile_id']
        loaded = self.client.get(f'/api/news/profiles/{pid}').json()['profile']
        self.assertEqual(loaded['bokjiro_mode'], 'unsupported')
        self.assertEqual(loaded['sources'], ['sites'])
        options = self.client.get(f'/api/news/profiles/{pid}/workflow-options').json()
        self.assertFalse(options['enabled'])
        self.assertTrue(options['ai_required'])
        self.assertEqual(options['selection']['sources'], ['sites'])
        with patch('ai_classifier.get_client', return_value=None), \
             patch.object(news_profiles_api, '_launch_workflow') as launch:
            started = self.client.post(f'/api/news/profiles/{pid}/workflows',
                                       json={'expected_revision': 1})
        self.assertEqual(started.status_code, 409)
        launch.assert_not_called()

    def test_profile_api_does_not_require_official_classification_for_sites(self):
        for extra in ({}, {'characteristics': ['기관 자체 분류']}):
            response = self.client.post('/api/news/profiles', json={
                'name': '범위 없는 복지로', 'region': '전국',
                'sources': ['bokjiro'], **extra})
            self.assertEqual(response.status_code, 200)
            profile=self.client.get('/api/news/profiles/'+response.json()['profile_id']).json()['profile']
            self.assertEqual(profile['sources'], ['sites'])
            self.assertEqual(profile['bokjiro_mode'], 'unsupported')

    def test_saved_official_only_workflow_cannot_resume_collection(self):
        from services import news_profiles_api
        from services.news_profile_workflow import _transition, create_workflow
        with database.get_connection() as conn:
            profiles.create_profile(conn,NewsProfile('old-official',1,'과거 수집','전국',
                sources=('bokjiro',),life_cycles=('노년',),bokjiro_mode='official_tags_v1'))
            run=profiles.prepare_run(conn,'old-official',expected_revision=1,sites=[])
            workflow=create_workflow(conn,run,settings=news_profiles_api._offline_workflow_settings())
            _transition(conn, workflow, 'running', 'collecting')
            _transition(conn, workflow, 'waiting', 'collecting', error_code='busy')
            conn.commit()
        with patch('ai_classifier.get_client') as get_client, \
             patch.object(news_profiles_api, '_launch_workflow') as launch:
            response = self.client.post(f"/api/news/workflows/{workflow}/resume")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail']['error'],'news_source_scope_changed')
        get_client.assert_not_called()
        launch.assert_not_called()

    def test_profile_taxonomy_uses_current_bokjiro_labels_and_is_authenticated(self):
        response = self.client.get('/api/news/profile-taxonomy')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['life_cycles'],
            ['임신·출산', '영유아', '아동', '청소년', '청년', '중장년', '노년'])
        self.assertIn('한부모·조손', data['household_situations'])
        self.assertIn('장애인', data['household_situations'])
        self.assertEqual(len(data['interest_topics']), 15)
        self.assertIn('보호·돌봄', data['interest_topics'])
        self.assertIn('서로 다른 줄', data['combination_rule'])
        self.assertEqual({p['id'] for p in data['presets']},
                         {'senior', 'single_parent', 'disabled'})
        self.assertEqual(self.client.get('/api/news/profile-taxonomy',
            headers={'X-Welfare-Session': 'wrong'}).status_code, 403)

    def test_retired_bokjiro_sample_is_gone_and_still_authenticated(self):
        settings = {'name': '노년 시험', 'region': '전국', 'life_cycles': ['노년']}
        with patch('services.bokjiro_collector.collect_official_sample',
                   new=AsyncMock()) as collect:
            response = self.client.post('/api/news/bokjiro-sample', json={
                'settings': settings, 'max_pages': 2, 'limit': 10})
        self.assertEqual(response.status_code, 410)
        self.assertIn('등록한 기관 사이트에서만', response.json()['detail'])
        self.assertNotIn('자동으로 함께', response.json()['detail'])
        collect.assert_not_awaited()
        self.assertEqual(self.client.post('/api/news/bokjiro-sample', json={
            'settings': settings}, headers={'X-Welfare-Session': 'wrong'}).status_code, 403)

    def test_retired_bokjiro_sample_rejects_unknown_target_without_network(self):
        settings = {'name': '독거 시험', 'region': '전국', 'characteristics': ['독거']}
        with patch('services.bokjiro_collector.collect_official_sample',
                   new=AsyncMock()) as collect:
            response = self.client.post('/api/news/bokjiro-sample', json={'settings': settings})
        self.assertEqual(response.status_code, 410)
        collect.assert_not_awaited()

    def test_retired_bokjiro_sample_cannot_be_reenabled_by_official_filter(self):
        settings = {'name': '혼합 분류', 'region': '전국',
                    'life_cycles': ['노년', '기관 자체 생애주기'],
                    'characteristics': ['독거'], 'interest_topics': ['주거', '기관 자체 분야']}
        with patch('services.bokjiro_collector.collect_official_sample',
                   new=AsyncMock()) as collect:
            response = self.client.post('/api/news/bokjiro-sample', json={'settings': settings})
        self.assertEqual(response.status_code, 410)
        collect.assert_not_awaited()

    def test_retired_bokjiro_sample_does_not_enter_provider_or_active_jobs(self):
        from services import app_lifecycle
        settings = {'name': '노년 시험', 'region': '전국', 'life_cycles': ['노년']}
        before = app_lifecycle.active_profile_jobs
        with patch('services.bokjiro_collector.collect_official_sample',
                   new=AsyncMock(side_effect=AssertionError('provider must not run'))) as collect:
            response = self.client.post('/api/news/bokjiro-sample', json={'settings': settings})
        self.assertEqual(response.status_code, 410)
        collect.assert_not_awaited()
        self.assertEqual(app_lifecycle.active_profile_jobs, before)

    def test_retired_news_bokjiro_routes_do_not_create_jobs_or_require_a_body(self):
        with database.get_connection() as conn:
            tables=('news_run_snapshots','news_analysis_jobs','news_profile_workflows')
            before={table:conn.execute('SELECT * FROM '+table).fetchall() for table in tables}
        for route in ('/api/news/bokjiro-sample','/api/bokjiro/collect-infant'):
            response=self.client.post(route)
            self.assertEqual(response.status_code,410,response.text)
            self.assertIn('등록한 기관 사이트에서만',response.json()['detail'])
        with database.get_connection() as conn:
            after={table:conn.execute('SELECT * FROM '+table).fetchall() for table in tables}
        self.assertEqual(after,before)

    def test_ai_instructions_roundtrip_clear_and_conflict_preserve_saved_version(self):
        settings = {'name': '기준 검증', 'region': '전국',
                    'interest_topics': ['보육', '보호·돌봄'],
                    'ai_prompt': '영유아 요청', 'semantic_text': '영유아 돌봄'}
        created = self.client.post('/api/news/profiles', json=settings)
        self.assertEqual(created.status_code, 200)
        url = f"/api/news/profiles/{created.json()['profile_id']}"
        updated = settings | {'ai_prompt': '노년층 지원을 확인', 'semantic_text': '노년층 생활과 돌봄'}
        self.assertEqual(self.client.put(url, json={'expected_revision': 1, 'settings': updated}).status_code, 200)
        loaded = self.client.get(url).json()['profile']
        self.assertEqual(loaded['ai_prompt'], updated['ai_prompt'])
        self.assertEqual(loaded['semantic_text'], updated['semantic_text'])
        self.assertEqual(loaded['interest_topics'], ['보육', '보호·돌봄'])
        self.assertEqual(self.client.put(url, json={'expected_revision': 1, 'settings': settings}).status_code, 409)
        self.assertEqual(self.client.get(url).json()['profile'], loaded)
        cleared = updated | {'ai_prompt': '', 'semantic_text': ''}
        self.assertEqual(self.client.put(url, json={'expected_revision': 2, 'settings': cleared}).status_code, 200)
        loaded = self.client.get(url).json()['profile']
        self.assertEqual((loaded['ai_prompt'], loaded['semantic_text']), ('', ''))

    def test_target_preview_does_not_save_or_call_ai(self):
        request = {'settings': {'name': '한부모', 'region': '전국', 'characteristics': ['한부모']},
                   'facts': {'target_characteristics': ['한부모']}}
        before = self.client.get('/api/news/profiles').json()
        response = self.client.post('/api/news/target-preview', json=request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'match')
        self.assertFalse(response.json()['saved'])
        self.assertFalse(response.json()['ai_called'])
        self.assertEqual(self.client.get('/api/news/profiles').json(), before)
        request['facts'] = {}
        self.assertEqual(self.client.post('/api/news/target-preview', json=request).json()['status'], 'review')

    def test_sample_trial_preserves_profile_and_routes_without_ai(self):
        created = self.client.post('/api/news/profiles', json={'name': '시험', 'region': '전국',
            'keywords': ['지원'], 'excluded_keywords': ['채용'], 'characteristics': ['한부모']}).json()
        url = f"/api/news/profiles/{created['profile_id']}/sample-trial"
        body = {'expected_revision': 1, 'candidates': [
            {'title': '한부모 지원', 'facts': {'target_characteristics': ['한부모']}},
            {'title': '지원 채용', 'facts': {}}, {'title': '다른 소식', 'facts': {}}]}
        response = self.client.post(url, json=body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r['status'] for r in response.json()['results']], ['match', 'excluded', 'review'])
        self.assertFalse(response.json()['network_collection'])
        self.assertFalse(response.json()['ai_called'])
        self.assertEqual(self.client.post(url, json=body | {'expected_revision': 2}).status_code, 409)
        self.assertEqual(self.client.post(url, json=body | {'candidates': body['candidates'] * 7}).status_code, 422)

    def test_legacy_capture_preserves_settings_and_is_idempotent(self):
        import content_filter
        with patch.dict(os.environ, {'CUSTOM_AI_PROMPT': '원래 지침', 'TARGET_REGION': '도봉',
                                    'TARGET_AGE_MAX_MONTHS': '108'}), \
             patch.object(content_filter, '_keyword_cache', ['맞춤 단어']), \
             patch.object(content_filter, '_excluded_keyword_cache', []):
            first = self.client.post('/api/news/profiles/legacy/capture')
            self.assertEqual(first.status_code, 200)
            self.assertTrue(first.json()['protected'])
            self.assertFalse(first.json()['execution_enabled'])
            original = self.client.get('/api/news/profiles/legacy-infant').json()['profile']
            self.assertEqual(original['keywords'], ['맞춤 단어'])
            self.assertEqual(original['excluded_keywords'], [])
            self.assertEqual(original['ai_prompt'], '원래 지침')
        with patch.dict(os.environ, {'CUSTOM_AI_PROMPT': '변경된 지침'}):
            self.assertEqual(self.client.post('/api/news/profiles/legacy/capture').status_code, 200)
        self.assertEqual(self.client.get('/api/news/profiles/legacy-infant').json()['profile'], original)
        self.assertEqual(self.client.post('/api/news/profiles/legacy-infant/archive').status_code, 409)

    def test_workflow_start_status_cancel_is_authenticated_and_nonblocking(self):
        from services import news_profiles_api
        import asyncio
        entered = asyncio.Event()
        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        client = object()
        with database.get_connection() as conn:
            conn.execute("INSERT INTO sites(id,name,url,selectors) VALUES (1,'시험','https://example.invalid','{}')")
            conn.commit()
        with patch.dict(os.environ, {'NEWS_PROFILE_SEMANTIC_THRESHOLD': '0.8'}), \
             patch('ai_classifier.get_client', return_value=client), \
             patch.object(news_profiles_api, '_run_workflow', side_effect=blocked):
            response = self.client.post('/api/news/profiles/p/workflows', json={
                'expected_revision': 1, 'headless': True})
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data['experimental'])
            status = self.client.get('/api/news/workflows/' + data['workflow_id'])
            self.assertEqual(status.status_code, 200)
            self.assertIsInstance(status.json()['active'], bool)
            cancelled = self.client.post('/api/news/workflows/' + data['workflow_id'] + '/cancel')
            self.assertIsInstance(cancelled.json()['changed'], bool)
        self.assertEqual(self.client.get('/api/news/workflows/missing').status_code, 404)
        self.assertEqual(self.client.post('/api/news/profiles/p/workflows', json={
            'expected_revision': 1},
            headers={'X-Welfare-Session':'wrong'}).status_code, 403)

    def test_workflow_start_validates_key_revision_threshold_and_sites(self):
        with patch.dict(os.environ, {'NEWS_PROFILE_SEMANTIC_THRESHOLD': '0.8'}), patch('ai_classifier.get_client', return_value=None):
            self.assertEqual(self.client.post('/api/news/profiles/p/workflows', json={
                'expected_revision':1}).status_code, 409)
        with patch.dict(os.environ, {'NEWS_PROFILE_SEMANTIC_THRESHOLD': '0.8'}), patch('ai_classifier.get_client', return_value=object()):
            self.assertEqual(self.client.post('/api/news/profiles/p/workflows', json={
                'expected_revision':2}).status_code, 409)
            self.assertEqual(self.client.post('/api/news/profiles/p/workflows', json={
                'expected_revision':1, 'unknown':1}).status_code, 422)

    def test_workflow_options_offer_bounded_analysis_without_claiming_calibration(self):
        with database.get_connection() as conn:
            conn.execute("INSERT INTO sites(id,name,url,selectors) VALUES (1,'시험','https://example.invalid','{}')")
            conn.commit()
        with patch('ai_classifier.get_client', return_value=object()), patch.dict(os.environ, {}, clear=False):
            os.environ.pop('NEWS_PROFILE_SEMANTIC_THRESHOLD', None)
            options = self.client.get('/api/news/profiles/p/workflow-options').json()
            self.assertTrue(options['enabled'])
            self.assertTrue(options['key_ready'])
            self.assertFalse(options['semantic_calibrated'])
            self.assertEqual(options['reason_code'], '')
            self.assertEqual(options['analysis_mode'], 'direct-body-bounded-v1')
            self.assertIsNone(options['batch_limit'])
            self.assertFalse(options['scheduled'])
        with patch('ai_classifier.get_client', return_value=object()), \
             patch.dict(os.environ, {'NEWS_PROFILE_SEMANTIC_THRESHOLD':'0.8'}):
            self.assertTrue(self.client.get('/api/news/profiles/p/workflow-options').json()['enabled'])
        self.assertEqual(self.client.get('/api/news/profiles/p/workflows').json()['items'], [])
        self.assertEqual(self.client.get('/api/news/profiles/missing/workflows').status_code, 404)
        self.assertEqual(self.client.get('/api/news/profiles/p/workflows?limit=101').status_code, 422)

    def test_workflow_selection_blocks_missing_and_archived_sites_without_starting(self):
        from services import news_profiles_api
        with patch('ai_classifier.get_client', return_value=object()), patch.dict(os.environ, {'NEWS_PROFILE_SEMANTIC_THRESHOLD':'0.8'}), patch.object(news_profiles_api, '_launch_workflow') as launch:
            options = self.client.get('/api/news/profiles/p/workflow-options').json()
            self.assertFalse(options['enabled'])
            self.assertIn('삭제', options['reason'])
            self.assertEqual(self.client.post('/api/news/profiles/p/workflows', json={'expected_revision':1}).status_code, 409)
            with database.get_connection() as conn:
                conn.execute("INSERT INTO sites(id,name,url,selectors) VALUES (1,'시험','https://example.invalid','{}')")
                conn.commit()
            options = self.client.get('/api/news/profiles/p/workflow-options').json()
            self.assertTrue(options['enabled'])
            self.assertEqual(options['selection']['sites'], [{'id':'1','name':'시험'}])
            self.assertNotIn('ai_prompt', options['selection'])
            with database.get_connection() as conn:
                conn.execute("UPDATE sites SET selectors='[]' WHERE id=1")
                conn.commit()
            self.assertFalse(self.client.get('/api/news/profiles/p/workflow-options').json()['enabled'])
            self.client.post('/api/news/profiles/p/archive')
            self.assertFalse(self.client.get('/api/news/profiles/p/workflow-options').json()['enabled'])
            self.assertEqual(self.client.post('/api/news/profiles/p/workflows', json={'expected_revision':1}).status_code, 409)
            launch.assert_not_called()

    def test_direct_workflow_starts_without_threshold_and_pins_mode(self):
        from services import news_profiles_api as api
        from services.news_profile_workflow import get_workflow_settings, _transition
        from services.news_direct_analysis import DirectAnalysis
        with database.get_connection() as conn:
            conn.execute("INSERT INTO sites(id,name,url,selectors) VALUES (1,'시험','https://example.invalid','{}')")
            conn.commit()
        with patch.dict(os.environ, {}, clear=False), patch('ai_classifier.get_client', return_value=object()), \
             patch.object(api, '_launch_workflow') as launch:
            os.environ.pop('NEWS_PROFILE_SEMANTIC_THRESHOLD', None)
            response = self.client.post('/api/news/profiles/p/workflows', json={'expected_revision':1})
            self.assertEqual(response.status_code, 200)
            workflow_id = response.json()['workflow_id']
            self.assertIsInstance(launch.call_args.kwargs['scorer'], DirectAnalysis)
            self.assertIsNone(launch.call_args.kwargs['threshold'])
            with database.get_connection() as conn:
                settings = get_workflow_settings(conn, workflow_id)
                self.assertEqual(settings['mode'], 'direct-body-bounded-v1')
                self.assertNotIn('semantic_threshold', settings)
                _transition(conn, workflow_id, 'running', 'analyzing')
                _transition(conn, workflow_id, 'waiting', 'analyzing')
            self.assertEqual(self.client.post(f'/api/news/workflows/{workflow_id}/resume').status_code, 200)
            with patch.dict(os.environ, {'NEWS_PROFILE_SEMANTIC_THRESHOLD':'0.8'}):
                self.assertEqual(self.client.post(f'/api/news/workflows/{workflow_id}/resume').status_code, 409)

    def test_invalid_explicit_threshold_does_not_silently_enable_direct_mode(self):
        from services import news_profiles_api as api
        for value in ('', 'nan', 'bad', '2'):
            with patch.dict(os.environ, {'NEWS_PROFILE_SEMANTIC_THRESHOLD':value}):
                self.assertIsNone(api._runtime_workflow_settings())

    def test_old_revision_waiting_does_not_block_current_collection(self):
        from dataclasses import replace
        from services import news_profiles_api as api
        from services.news_profile_workflow import create_workflow, _transition
        with database.get_connection() as conn:
            conn.execute("INSERT INTO sites(id,name,url,selectors) VALUES (1,'시험','https://example.invalid','{}')")
            old = create_workflow(conn, self.run)
            _transition(conn, old, 'running', 'analyzing')
            _transition(conn, old, 'waiting', 'analyzing')
            profiles.save_revision(conn, replace(profiles.get_profile(conn, 'p'),revision=2,name='변경한 조건'), expected_revision=1)
            conn.commit()
        with patch('ai_classifier.get_client',return_value=object()), patch.object(api, '_launch_workflow'):
            response = self.client.post('/api/news/profiles/p/workflows', json={'expected_revision':2})
            self.assertEqual(response.status_code, 200, response.text)
        history = self.client.get('/api/news/profiles/p/workflows').json()['items']
        self.assertEqual({item['revision'] for item in history}, {1,2})
        self.assertEqual(next(item for item in history if item['revision']==1)['state'], 'waiting')

    def test_cancelled_start_drains_commit_and_cancels_orphan_ready(self):
        import asyncio
        from services import news_profiles_api as api
        with database.get_connection() as conn:
            conn.execute("INSERT INTO sites(id,name,url,selectors) VALUES (1,'시험','https://example.invalid','{}')")
            conn.commit()
        original = api._execute
        async def scenario():
            committed, release = asyncio.Event(), asyncio.Event()
            async def delayed(fn):
                result = await original(fn)
                if fn.__name__ == 'create':
                    committed.set()
                    await release.wait()
                return result
            with patch.object(api, '_execute', side_effect=delayed), patch('ai_classifier.get_client',return_value=object()), \
                 patch.object(api, '_launch_workflow') as launch:
                task = asyncio.create_task(api.start_profile_workflow('p', api.StartWorkflowRequest(expected_revision=1)))
                await asyncio.wait_for(committed.wait(), 5)
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError): await task
                launch.assert_not_called()
        asyncio.run(scenario())
        history = self.client.get('/api/news/profiles/p/workflows').json()['items']
        self.assertEqual(len(history),1)
        self.assertEqual(history[0]['state'],'cancelled')

    def test_legacy_quota_failure_resume_checks_quota_before_resetting_jobs(self):
        from services import news_profiles_api as api
        from services.news_profile_workflow import create_workflow, _transition
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('NEWS_PROFILE_SEMANTIC_THRESHOLD', None)
            settings = api._runtime_workflow_settings()
            with database.get_connection() as conn:
                workflow = create_workflow(conn, self.run, settings=settings)
                token = jobs.claim(conn, self.job, now=0)
                jobs.defer(conn, self.job, token, retry_after=0, reason='daily_quota_exhausted', failed=True)
                _transition(conn, workflow, 'running', 'analyzing')
                _transition(conn, workflow, 'failed', 'failed', error_code='analysis_failed', summary={'collection':{'state':'finished'}})
            with patch('ai_classifier.get_client', return_value=object()), patch.object(api, '_launch_workflow') as launch:
                with patch('services.ai_coordinator.status', return_value={'models':{settings['analysis_model']:{'daily_exhausted':True}}}):
                    self.assertEqual(self.client.post(f'/api/news/workflows/{workflow}/resume').status_code,409)
                    self.assertEqual(self.client.get(self.url).json()['state'],'failed')
                    launch.assert_not_called()
                with patch('services.ai_coordinator.status', return_value={'models':{}}):
                    self.assertEqual(self.client.post(f'/api/news/workflows/{workflow}/resume').status_code,200)
                    self.assertEqual(self.client.get(self.url).json()['state'],'queued')
                    launch.assert_called_once()

    def test_workflow_description_only_blocks_semantic_analysis_not_collection(self):
        with database.get_connection() as conn:
            conn.execute("INSERT INTO sites(id,name,url,selectors) VALUES (1,'시험','https://example.invalid','{}')")
            conn.commit()
        with patch('ai_classifier.get_client', return_value=object()), patch.dict(os.environ, {'NEWS_PROFILE_SEMANTIC_THRESHOLD':'0.8'}):
            created = self.client.post('/api/news/profiles', json={'name':'설명 없는 조건','region':'전국','site_ids':['1']}).json()
            response = self.client.get(f"/api/news/profiles/{created['profile_id']}/workflow-options")
            self.assertTrue(response.json()['enabled'])
            self.assertFalse(response.json()['analysis_ready'])
            self.assertEqual(response.json()['analysis_reason_code'], 'analysis_settings_required')

    def test_duplicate_start_rejected_for_ready_running_and_waiting(self):
        from services import news_profiles_api
        with database.get_connection() as conn:
            conn.execute("INSERT INTO sites(id,name,url,selectors) VALUES (1,'시험','https://example.invalid','{}')")
            conn.commit()
        with patch('ai_classifier.get_client', return_value=object()), patch.dict(os.environ, {'NEWS_PROFILE_SEMANTIC_THRESHOLD':'0.8'}), patch.object(news_profiles_api, '_launch_workflow') as launch:
            response = self.client.post('/api/news/profiles/p/workflows', json={'expected_revision':1})
            self.assertEqual(response.status_code, 200)
            for state in ('ready','running','waiting'):
                with database.get_connection() as conn:
                    conn.execute('UPDATE news_profile_workflows SET state=?', (state,))
                    conn.commit()
                self.assertEqual(self.client.post('/api/news/profiles/p/workflows', json={'expected_revision':1}).status_code, 409)
            launch.assert_called_once()
            with database.get_connection() as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM news_profile_workflows').fetchone()[0], 1)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM news_run_snapshots').fetchone()[0], 2)

    def test_workflow_shutdown_refuses_new_run(self):
        from services import app_lifecycle, news_profiles_api
        with patch.object(app_lifecycle, 'shutdown_requested', True), patch.object(news_profiles_api, '_launch_workflow') as launch:
            self.assertEqual(self.client.post('/api/news/profiles/p/workflows', json={'expected_revision':1}).status_code, 503)  # lifecycle middleware rejects before routing
            launch.assert_not_called()

    def test_result_run_scope_and_display_allowlist(self):
        with database.get_connection() as conn:
            token = jobs.claim(conn, self.job, now=0)
            jobs.complete(conn, self.job, token, {'target_status':'review', 'analysis':{
                'support_detail':'지원 내용', 'contact':'공식 문의처', 'secret':'do-not-show', 'source_body':'private body'}})
            second = profiles.prepare_run(conn, 'p', expected_revision=1, sites=[dict(id=1,name='시험',url='https://example.invalid')])
            jobs.enqueue(conn, second, payload=dict(title='다른 실행',link='javascript:alert(1)',content='private',site_name='시험'), model='other', recipe='other')
            profiles.create_profile(conn, NewsProfile('other',1,'다른 조건','전국'))
        response = self.client.get(f'/api/news/profiles/p/results?run_id={self.run}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['items']), 1)
        item = response.json()['items'][0]
        self.assertEqual(item['run_id'], self.run)
        self.assertEqual(item['link'], 'https://example.invalid')
        self.assertEqual(item['details'], {'support_detail':'지원 내용','contact':'공식 문의처'})
        self.assertNotIn('do-not-show', response.text)
        self.assertNotIn('private body', response.text)
        self.assertEqual(self.client.get(f'/api/news/profiles/other/results?run_id={self.run}').status_code, 404)
        self.assertEqual(self.client.get('/api/news/profiles/p/results?run_id=missing').status_code, 404)
        unsafe = self.client.get(f'/api/news/profiles/p/results?run_id={second}').json()['items'][0]
        self.assertEqual(unsafe['link'], '')

    def test_waiting_analysis_resume_requires_original_settings(self):
        from services import news_profiles_api
        from services.news_profile_workflow import create_workflow, _transition
        settings = {'analysis_model':'gemini-3.5-flash-lite', 'embedding_model':'gemini-embedding-001',
                    'embedding_dimensions':768, 'semantic_threshold':.8, 'headless':True}
        with database.get_connection() as conn:
            workflow = create_workflow(conn, self.run, settings=settings)
            _transition(conn, workflow, 'running', 'collecting')
            _transition(conn, workflow, 'waiting', 'collecting', error_code='busy')
        with patch.dict(os.environ, {'NEWS_PROFILE_SEMANTIC_THRESHOLD':'0.8'}), \
             patch('ai_classifier.get_client', return_value=object()), \
             patch.object(news_profiles_api, '_launch_workflow') as launch:
            response = self.client.post(f'/api/news/workflows/{workflow}/resume')
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()['scheduled'])
            launch.assert_called_once()
        with patch.dict(os.environ, {'NEWS_PROFILE_SEMANTIC_THRESHOLD':'0.7'}), \
             patch('ai_classifier.get_client', return_value=object()), \
             patch.object(news_profiles_api, '_launch_workflow') as launch:
            self.assertEqual(self.client.post(f'/api/news/workflows/{workflow}/resume').status_code, 200)
            self.assertEqual(launch.call_args.kwargs['threshold'], .8)
            self.assertEqual(launch.call_args.kwargs['analysis_wait_reason'], 'analysis_settings_changed')
            with database.get_connection() as conn:
                _transition(conn, workflow, 'running', 'analyzing')
                _transition(conn, workflow, 'waiting', 'analyzing', summary={'collection':{'state':'finished'}})
            self.assertEqual(self.client.post(f'/api/news/workflows/{workflow}/resume').status_code, 409)
        self.assertEqual(self.client.post(f'/api/news/workflows/{workflow}/cancel').status_code, 200)

    def test_profile_requests_reject_mode_injection_and_bad_types(self):
        for values in ({'general_mode': 'legacy_infant_v1'}, {'min_age_months': True},
                       {'unknown': 1}, {'site_ids': [1]}):
            result = self.client.post('/api/news/profiles', json={'name': '시험', 'region': '서울'} | values)
            self.assertEqual(result.status_code, 422)
        result = self.client.post('/api/news/profiles', json={
            'name': '시험', 'region': '서울', 'min_age_months': 120, 'max_age_months': 12})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.client.post('/api/news/profiles', json={'name': '시험', 'region': '서울'},
                         headers={'X-Welfare-Session': 'wrong'}).status_code, 403)

    def test_prepare_reads_and_freezes_actual_site_without_launching(self):
        with database.get_connection() as conn:
            conn.execute("INSERT INTO sites(id,name,url,selectors) VALUES (1,'실제 사이트','https://example.invalid','{}')")
            conn.commit()
        response = self.client.post('/api/news/profiles/p/prepare', json={'expected_revision': 1})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['scheduled'])
        with database.get_connection() as conn:
            snapshot = profiles.get_run_snapshot(conn, response.json()['run_id'])
            self.assertEqual(snapshot['sites'][0]['name'], '실제 사이트')
        self.assertEqual(self.client.post('/api/news/profiles/p/prepare',
            json={'expected_revision': 1, 'sites': []}).status_code, 422)

    def test_prepare_missing_site_never_falls_back_to_official_bokjiro(self):
        self.assertEqual(self.client.post('/api/news/profiles/p/prepare',
            json={'expected_revision': 1}).status_code, 409)
        created = self.client.post('/api/news/profiles', json={'name': '노년', 'region': '서울',
                                                             'sources': ['bokjiro'],
                                                             'life_cycles': ['노년']}).json()
        prepared = self.client.post(f"/api/news/profiles/{created['profile_id']}/prepare",
                                    json={'expected_revision': 1})
        self.assertEqual(prepared.status_code, 409)
        self.assertIn('수집 사이트',prepared.json()['detail'])
