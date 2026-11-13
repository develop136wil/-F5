"""Offline import, candidate and export integration against a disposable app DB."""
import asyncio
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import api_server
import database
import history_manager
from services import news_profile_store as profiles, news_analysis_store as jobs
from services import news_result_import as imports, news_custom_analysis, page_coverage
from services.news_profile import NewsProfile


class TestNewsResultImport(unittest.TestCase):
    def setUp(self):
        database.close_all_connections()
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(database, 'DB_FILE', os.path.join(self.temp.name, 'test.db'))
        self.db_patch.start()
        database.init_db()
        self.token_patch = patch.dict(os.environ, {'WELFARE_DESKTOP_TOKEN': 'test-session'})
        self.token_patch.start()
        self.client = TestClient(api_server.app)
        self.client.headers['X-Welfare-Session'] = 'test-session'
        self.facts = dict(is_relevant=True, category='생활지원', target='노년', residence=['서울'],
            life_cycle=['노년'], target_characteristics=[], interest_topics=[],
            start_age_months=780, end_age_months=None,
            evidence_snippet='만 65세', support_detail='돌봄 지원', eligibility_detail='원문 자격 확인',
            applicant_type='individual', applicant_evidence='개인이 신청합니다.',
            applicant_detail='개인', beneficiary_detail='노년',
            life_cycle_evidence=[dict(value='노년', quote='노년 대상', basis='explicit')],
            residence_evidence=[dict(value='서울', quote='서울 거주자', basis='explicit')],
            source_page=page_coverage.make('attachment_review'), secret='private-provider-data')
        self.payload = dict(title='어르신 돌봄 지원', site_name='시험 기관',
            link='https://example.invalid/notice', content='서울 거주자, 노년 대상. 만 65세 이상 신청. 돌봄 지원. 원문 자격 확인. 개인이 신청합니다. private-source-body')
        with database.get_connection() as conn:
            profiles.create_profile(conn, NewsProfile('p', 1, '노년', '전국', site_ids=('1',)))
        self.job = self.new_job()
        self.url = f'/api/news/profiles/p/jobs/{self.job}'

    def tearDown(self):
        self.client.close()
        database.close_all_connections()
        self.db_patch.stop()
        self.token_patch.stop()
        self.temp.cleanup()

    def new_job(self, *, state='complete', status='review', facts=None, payload=None):
        with database.get_connection() as conn:
            run = profiles.prepare_run(conn, 'p', expected_revision=1,
                sites=[dict(id=1, name='시험 기관', url='https://example.invalid')])
            # Historical v14 saved facts carry coverage in analysis; v15 has separate workflow tests.
            job = jobs.enqueue(conn, run, payload=payload or self.payload,
                               model='offline', recipe=news_custom_analysis.SHORT_SOURCE_RECIPE)
            if state == 'complete':
                token = jobs.claim(conn, job, now=0)
                # Each test run can reuse the immutable successful cache.
                if token:
                    jobs.complete(conn, job, token, {'analysis': facts or self.facts, 'target_status': status})
            return job

    def preview(self):
        response = self.client.get(self.url+'/import-preview')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def commit(self, fingerprint=None):
        return self.client.post(self.url+'/import', json={
            'confirmed': True, 'fingerprint': fingerprint or self.preview()['fingerprint']})

    def test_preview_read_only_safe_and_confirmation_required(self):
        preview = self.preview()
        self.assertEqual(preview['status'], 'ready')
        self.assertIn('첨부파일', preview['note'])
        self.assertIn('대상 조건', preview['note'])
        self.assertNotIn('private', json.dumps(preview))
        self.assertEqual(database.execute_query('SELECT COUNT(*) AS n FROM welfare_items', fetch_one=True)['n'], 0)
        self.assertEqual(self.client.post(self.url+'/import', json={
            'confirmed': False, 'fingerprint': preview['fingerprint']}).status_code, 409)
        for confirmed in ('true', 1):
            self.assertEqual(self.client.post(self.url+'/import', json={
                'confirmed': confirmed, 'fingerprint': preview['fingerprint']}).status_code, 422)
        self.assertEqual(self.client.post(self.url+'/import', json={
            'confirmed': True, 'fingerprint': preview['fingerprint'], 'title': 'forged'}).status_code, 422)

    def test_result_list_derives_eligibility_for_old_saved_result_without_rewriting_it(self):
        with database.get_connection() as conn:
            before = conn.execute('SELECT result_json,input_json FROM news_analysis_jobs WHERE job_id=?', (self.job,)).fetchone()
        with patch.object(jobs, 'get_job', wraps=jobs.get_job) as read_job:
            response = self.client.get('/api/news/profiles/p/results')
        self.assertEqual(response.status_code, 200, response.text)
        item = next(row for row in response.json()['items'] if row['job_id'] == self.job)
        self.assertEqual(item['import_eligibility'], {'allowed':True,'reason':'confirmation_required'})
        self.assertEqual(item['source_review'], {'status':'partial','reason':'source_incomplete'})
        self.assertEqual(read_job.call_count, 1)  # no second grounding pass for eligibility
        with database.get_connection() as conn:
            self.assertEqual(conn.execute('SELECT result_json,input_json FROM news_analysis_jobs WHERE job_id=?', (self.job,)).fetchone(), before)
        self.assertEqual(self.preview()['status'], 'ready')

    def test_reviewed_non_news_is_not_offered_for_import_even_with_forged_saved_permission(self):
        with database.get_connection() as conn:
            result = {'analysis':self.facts | {'is_relevant':False}, 'target_status':'review',
                      'import_eligibility':{'allowed':True,'reason':'confirmation_required'}}
            conn.execute('UPDATE news_analysis_jobs SET result_json=? WHERE job_id=?', (jobs._encode(result), self.job))
            conn.commit()
        response = self.client.get('/api/news/profiles/p/results')
        self.assertEqual(response.status_code, 200, response.text)
        item = next(row for row in response.json()['items'] if row['job_id'] == self.job)
        self.assertEqual(item['target_status'], 'review')
        self.assertEqual(item['import_eligibility'], {'allowed':False,'reason':'relevance_unconfirmed'})
        self.assertEqual(self.client.get(self.url+'/import-preview').status_code, 409)
        self.assertEqual(self.commit('0'*64).status_code, 409)
        self.assertEqual(database.execute_query('SELECT COUNT(*) AS n FROM welfare_items', fetch_one=True)['n'], 0)

    def test_eligibility_is_not_authority_after_saved_result_changes(self):
        self.assertTrue(self.client.get('/api/news/profiles/p/results').json()['items'][0]['import_eligibility']['allowed'])
        fingerprint = self.preview()['fingerprint']
        with database.get_connection() as conn:
            conn.execute('UPDATE news_analysis_jobs SET result_json=? WHERE job_id=?',
                (jobs._encode({'analysis':self.facts | {'is_relevant':False},'target_status':'review'}),self.job))
            conn.commit()
        self.assertEqual(self.commit(fingerprint).status_code, 409)

    def test_malformed_saved_analysis_has_safe_unavailable_reason(self):
        with database.get_connection() as conn:
            conn.execute('UPDATE news_analysis_jobs SET result_json=? WHERE job_id=?',
                (jobs._encode({'analysis':{},'target_status':'review'}),self.job))
            conn.commit()
        response = self.client.get('/api/news/profiles/p/results')
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()['items'][0]
        self.assertEqual(item['import_eligibility'], {'allowed':False,'reason':'invalid_saved_result'})
        self.assertNotIn('fingerprint', item['import_eligibility'])

    def test_list_never_uses_saved_review_projection_as_source_evidence_or_import_fields(self):
        forged = {'version':1,'items':[{'text':'RAW_PROVIDER_RESPONSE_DO_NOT_SHOW'}]}
        with database.get_connection() as conn:
            conn.execute('UPDATE news_analysis_jobs SET result_json=? WHERE job_id=?',
                (jobs._encode({'analysis':self.facts | {'review_evidence':forged},
                               'target_status':'review','review_evidence':forged}),self.job))
            conn.commit()
        response = self.client.get('/api/news/profiles/p/results')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn('RAW_PROVIDER_RESPONSE_DO_NOT_SHOW', response.text)
        self.assertNotIn('review_evidence', response.json()['items'][0]['details'])
        result = self.commit().json()
        row = database.execute_query('SELECT ai_data FROM welfare_items WHERE id=?', (result['item_id'],), fetch_one=True)
        self.assertNotIn('review_evidence', json.loads(row['ai_data']))

    def test_fifty_results_revalidate_once_and_share_snapshot_reads_per_run(self):
        import time
        with database.get_connection() as conn:
            initial_run = jobs.get_job(conn, self.job)['run_id']
            second_run = profiles.prepare_run(conn, 'p', expected_revision=1,
                sites=[dict(id=1,name='시험 기관',url='https://example.invalid')])
            for index in range(49):
                run = initial_run if index < 24 else second_run
                job = jobs.enqueue(conn, run, payload=self.payload | {'title':f'공개자료 {index}'},
                                   model='offline',recipe=news_custom_analysis.SHORT_SOURCE_RECIPE)
                token = jobs.claim(conn,job,now=0)
                jobs.complete(conn,job,token,{'analysis':self.facts,'target_status':'review'})
            conn.commit()
            before = conn.execute('SELECT job_id,result_json FROM news_analysis_jobs ORDER BY job_id').fetchall()
            with patch.object(jobs,'get_job',wraps=jobs.get_job) as read_job, \
                    patch.object(jobs,'get_run_snapshot',wraps=jobs.get_run_snapshot) as read_snapshot:
                started = time.perf_counter()
                result = jobs.list_profile_results(conn,'p',limit=50)
                elapsed = time.perf_counter()-started
            self.assertEqual(len(result['items']),50)
            self.assertEqual(read_job.call_count,50)
            self.assertEqual(read_snapshot.call_count,2)
            self.assertTrue(all(item['import_eligibility']['allowed'] for item in result['items']))
            self.assertEqual(conn.execute('SELECT job_id,result_json FROM news_analysis_jobs ORDER BY job_id').fetchall(),before)
            print(f'NEWS_RESULT_PROJECTION_FIXTURE: rows=50 runs=2 revalidations=50 snapshot_reads=2 seconds={elapsed:.4f}; synthetic local fixture, not collection throughput')

    def test_import_preserves_facts_warnings_no_fabricated_dates_and_no_duplicate(self):
        result = self.commit()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['status'], 'created')
        self.assertFalse(result.json()['notion_uploaded'])
        row = database.execute_query('SELECT * FROM welfare_items', fetch_one=True)
        ai = json.loads(row['ai_data'])
        self.assertEqual(row['start_age_months'], 780)
        self.assertEqual(row['life_cycle'], '노년')
        self.assertIsNone(row['event_date'])
        self.assertIsNone(row['deadline'])
        self.assertNotIn('item_type', ai)
        self.assertNotIn('secret', ai)
        self.assertTrue(ai['news_import_review'])
        self.assertEqual(history_manager.count_ai_backlog(), 0)
        self.assertEqual(self.commit().json()['status'], 'already_imported')
        self.assertEqual(database.execute_query('SELECT COUNT(*) AS n FROM welfare_items', fetch_one=True)['n'], 1)
        self.assertEqual(database.execute_query('SELECT COUNT(*) AS n FROM news_result_imports', fetch_one=True)['n'], 1)

    def test_existing_archived_dismissed_uploaded_record_is_never_overwritten_or_reactivated(self):
        with database.get_connection() as conn:
            conn.execute('''INSERT INTO welfare_items(title,link,is_archived,notion_dismissed,notion_page_id)
                            VALUES (?,?,1,1,'already-uploaded')''', ('기존 제목', self.payload['link']))
            conn.commit()
        before = database.execute_query('SELECT * FROM welfare_items', fetch_one=True)
        self.assertEqual(self.preview()['status'], 'existing')
        self.assertEqual(self.commit().json()['status'], 'existing')
        self.assertEqual(database.execute_query('SELECT * FROM welfare_items', fetch_one=True), before)
        self.assertEqual(self.client.get('/api/notion/candidates').json()['count'], 0)

    def test_stale_fingerprint_and_wrong_profile_auth_rejected(self):
        self.assertEqual(self.commit('0'*64).status_code, 409)
        other = self.url.replace('/profiles/p/', '/profiles/other/')
        self.assertEqual(self.client.get(other+'/import-preview').status_code, 404)
        for suffix, method, kwargs in (
                ('/import-preview', self.client.get, {}),
                ('/import', self.client.post, {'json': {'confirmed': True, 'fingerprint':'0'*64}})):
            self.assertEqual(method(self.url+suffix, headers={'X-Welfare-Session':'wrong'}, **kwargs).status_code, 403)

    def test_pending_excluded_bad_schema_unsafe_link_and_ended_not_importable(self):
        with database.get_connection() as conn:
            original = jobs.get_job(conn, self.job)
            for state, result, payload in (
                ('queued', original['result_json'], original['input_json']),
                ('complete', jobs._encode({'analysis': {}, 'target_status': 'match'}), original['input_json']),
                ('complete', original['result_json'], jobs._encode(self.payload | {'link':'https://user:pass@example.invalid'})),
            ):
                conn.execute('UPDATE news_analysis_jobs SET state=?,result_json=?,input_json=? WHERE job_id=?',
                             (state,result,payload,self.job))
                conn.commit()
                self.assertEqual(self.client.get(self.url+'/import-preview').status_code, 409)
            # An old model-only exclusion without supporting evidence is now
            # a review candidate; it cannot remain silently excluded forever.
            conn.execute('UPDATE news_analysis_jobs SET state=?,result_json=?,input_json=? WHERE job_id=?',
                ('complete', jobs._encode({'analysis':self.facts, 'target_status':'excluded'}), original['input_json'], self.job))
            conn.commit()
            self.assertEqual(self.client.get(self.url+'/import-preview').status_code, 200)
            ended_payload = json.loads(original['input_json'])
            ended_payload['content'] += '\n신청 마감 2000-01-01'
            conn.execute('UPDATE news_analysis_jobs SET state=?,result_json=?,input_json=? WHERE job_id=?',
                ('complete', jobs._encode({'analysis':self.facts | {'deadline':'2000-01-01'}, 'target_status':'review'}),
                 jobs._encode(ended_payload),self.job))
            conn.commit()
        self.assertEqual(self.preview()['status'], 'ended')
        self.assertEqual(self.commit().status_code, 409)

    def test_audit_failure_rolls_back_item_and_search_index(self):
        with database.get_connection() as conn:
            conn.execute("""CREATE TRIGGER fail_import BEFORE INSERT ON news_result_imports
                            BEGIN SELECT RAISE(ABORT,'private SQL detail'); END""")
            conn.commit()
        response = self.commit()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('private SQL detail', response.text)
        self.assertEqual(database.execute_query('SELECT COUNT(*) AS n FROM welfare_items', fetch_one=True)['n'], 0)
        from services import policy_sync
        self.assertFalse(policy_sync._MAINTENANCE_LOCK.locked())

    def test_deleted_and_dismissed_import_stays_out_on_retry(self):
        self.commit()
        with database.get_connection() as conn:
            conn.execute('UPDATE welfare_items SET notion_dismissed=1')
            conn.commit()
        self.assertEqual(self.commit().json()['status'], 'already_imported')
        self.assertEqual(self.client.get('/api/notion/candidates').json()['count'], 0)
        with database.get_connection() as conn:
            conn.execute('DELETE FROM welfare_items')
            conn.commit()
        self.assertEqual(self.commit().json()['status'], 'already_imported')
        self.assertEqual(database.execute_query('SELECT COUNT(*) AS n FROM welfare_items', fetch_one=True)['n'], 0)

    def test_original_result_change_invalidates_preview(self):
        fingerprint = self.preview()['fingerprint']
        with database.get_connection() as conn:
            conn.execute('UPDATE news_analysis_jobs SET result_json=? WHERE job_id=?',
                (jobs._encode({'analysis':self.facts | {'target':'변경된 대상'}, 'target_status':'review'}), self.job))
            conn.commit()
        self.assertEqual(self.commit(fingerprint).status_code, 409)

    def test_dateless_curated_news_appears_but_forged_ai_marker_does_not(self):
        self.commit()
        with database.get_connection() as conn:
            conn.execute('INSERT INTO welfare_items(title,link,ai_data) VALUES (?,?,?)',
                ('자동 정책', 'https://example.invalid/not-event', '{"manual_news":true,"news_import_review":true}'))
            conn.commit()
        data = self.client.get('/api/notion/candidates').json()
        self.assertEqual(data['count'], 1)
        self.assertTrue(data['items'][0]['manual_news'])
        self.assertEqual(data['items'][0]['status'], '기간 확인 필요')
        self.assertTrue(data['items'][0]['input_review_required'])
        self.assertIn('첨부파일', data['items'][0]['input_note'])

    def test_html_and_copy_keep_review_warning_without_network(self):
        result = self.commit().json()
        with patch('api_server._poster_for_news_item', new=AsyncMock(return_value=('', True))):
            response = self.client.post('/api/news/gallery-html', json={'ids':[result['item_id']]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('대상 조건의 근거가 부족', response.text)
        self.assertIn('첨부파일', response.text)
        from services.news_gallery import _copy_text
        row = database.execute_query('SELECT * FROM welfare_items', fetch_one=True)
        self.assertIn('대상 조건', _copy_text(row))
        self.assertIn('일정 확인 필요', _copy_text(row))

    def test_notion_payload_keeps_review_even_with_poster_and_unknown_period_is_not_active(self):
        self.commit()
        from services.notion_service import build_payload
        row = database.execute_query('SELECT * FROM welfare_items', fetch_one=True)
        payload = build_payload(row, 'https://example.invalid/poster.jpg', 'fake-db')
        self.assertNotIn('선택', payload['properties'])
        self.assertNotIn('기간', payload['properties'])
        self.assertIn('첨부파일', json.dumps(payload,ensure_ascii=False))
        self.assertIn('대상 조건', json.dumps(payload,ensure_ascii=False))

    def test_oversize_gallery_explains_no_partial_export_and_releases_locks(self):
        result = self.commit().json()
        before = database.execute_query('SELECT * FROM welfare_items', fetch_one=True)
        from services import news_gallery, policy_sync
        with patch('api_server._poster_for_news_item', new=AsyncMock(return_value=('', True))), \
                patch.object(news_gallery, 'MAX_GALLERY_BYTES', 1), \
                patch.object(news_gallery, 'create_temp_file') as create:
            response = self.client.post('/api/news/gallery-html', json={'ids':[result['item_id']]})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn('나누어 선택', response.json()['detail'])
        create.assert_not_called()
        self.assertEqual(database.execute_query('SELECT * FROM welfare_items', fetch_one=True), before)
        self.assertFalse(api_server._news_media_lock.locked())
        self.assertFalse(policy_sync._MAINTENANCE_LOCK.locked())

    def test_legacy_rescrape_does_not_replace_curated_target_result(self):
        self.commit()
        before = database.execute_query('SELECT * FROM welfare_items', fetch_one=True)
        result = history_manager.save_data([self.payload | {'target':'영유아', 'title':'바뀐 제목',
                                                           'ai_data':'{"is_relevant":false}'}])
        self.assertEqual(result['saved'], 0)
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(database.execute_query('SELECT * FROM welfare_items', fetch_one=True), before)

    def test_restore_gate_and_database_failure(self):
        fingerprint = self.preview()['fingerprint']
        from services import policy_sync
        with patch.object(policy_sync, 'acquire_db_mutation_lock', new=AsyncMock(return_value=False)):
            self.assertEqual(self.commit(fingerprint).status_code, 409)
        with patch.object(database, 'run_db', side_effect=database.DatabaseMaintenanceError()):
            self.assertEqual(self.commit(fingerprint).status_code, 503)
        self.assertFalse(policy_sync._MAINTENANCE_LOCK.locked())

    def test_caller_transaction_can_roll_back_item_receipt_and_fts(self):
        fingerprint = self.preview()['fingerprint']
        with database.get_connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            imports.import_result(conn,'p',self.job,fingerprint=fingerprint,confirmed=True)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM welfare_items_fts').fetchone()[0], 1)
            conn.rollback()
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM welfare_items').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM welfare_items_fts').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM news_result_imports').fetchone()[0], 0)

    def test_another_run_same_source_uses_existing_record(self):
        self.commit()
        other_job = self.new_job()
        with database.get_connection() as conn:
            preview = imports.preview(conn,'p',other_job)
            self.assertEqual(preview['status'], 'existing')
            result = imports.import_result(conn,'p',other_job,fingerprint=preview['fingerprint'],confirmed=True)
            self.assertEqual(result['status'], 'existing')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM welfare_items').fetchone()[0], 1)

    def test_two_concurrent_imports_have_one_item_and_receipt(self):
        from concurrent.futures import ThreadPoolExecutor
        fingerprint = self.preview()['fingerprint']
        start = threading.Barrier(2)
        def write():
            start.wait(timeout=5)
            with database.get_connection() as conn:
                return imports.import_result(conn,'p',self.job,fingerprint=fingerprint,confirmed=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _:write(), range(2)))
        self.assertEqual({r['status'] for r in results}, {'created','already_imported'})
        self.assertEqual(database.execute_query('SELECT COUNT(*) AS n FROM welfare_items', fetch_one=True)['n'], 1)


class TestImportCancellation(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_drains_write_before_releasing_maintenance(self):
        from services import news_jobs_api, policy_sync
        entered, release = threading.Event(), threading.Event()
        async def execute(action):
            def work():
                entered.set()
                release.wait(5)
                return {'status':'created','item_id':1}
            return await asyncio.to_thread(work)
        try:
            with patch.object(news_jobs_api, '_execute', side_effect=execute):
                task = asyncio.create_task(news_jobs_api.import_result('p','j',
                    news_jobs_api.ImportRequest(fingerprint='0'*64,confirmed=True)))
                await asyncio.to_thread(entered.wait, 5)
                task.cancel()
                await asyncio.sleep(0)
                self.assertTrue(policy_sync._MAINTENANCE_LOCK.locked())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertFalse(policy_sync._MAINTENANCE_LOCK.locked())
        finally:
            release.set()
