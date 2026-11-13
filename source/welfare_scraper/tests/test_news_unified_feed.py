"""Disposable-DB regressions for the single-list, explicit-final-action flow."""
import json
import unittest
from unittest.mock import patch

import database
from services import news_unified_feed as feed, news_analysis_store as jobs
from services import news_profile_store as profiles
from services.news_profile import NewsProfile
from tests import test_news_result_import as import_fixture


class TestUnifiedNewsFeed(unittest.TestCase):
    setUp = import_fixture.TestNewsResultImport.setUp
    tearDown = import_fixture.TestNewsResultImport.tearDown
    new_job = import_fixture.TestNewsResultImport.new_job

    def listing(self, view='ready', **params):
        response = self.client.get('/api/news/feed', params={'profile_id':'p', 'view':view, **params})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def prepare(self, rows, action='html', confirmed=True, profile='p'):
        return self.client.post('/api/news/feed/prepare', json={
            'profile_id':profile, 'action':action, 'confirmed':confirmed,
            'items':[{'id':row['id'], 'fingerprint':row['fingerprint']} for row in rows]})

    def test_get_is_read_only_without_ai_or_raw_input(self):
        with database.get_connection() as conn:
            before = conn.total_changes
            items_before = conn.execute('SELECT count(*) FROM welfare_items').fetchone()[0]
            result = feed.read_feed(conn, 'p')
            self.assertEqual(conn.total_changes, before)
            self.assertEqual(conn.execute('SELECT count(*) FROM welfare_items').fetchone()[0], items_before)
        self.assertEqual(result['total'], 1)
        row = result['items'][0]
        self.assertTrue(row['selectable'])
        self.assertTrue(row['review_required'])
        self.assertEqual(row['id'], 'job:'+self.job)
        self.assertEqual(row['target'], '노년')
        # The historical fixture lacks full prose grounding: missing facts stay
        # blank rather than treating source text as approved AI analysis.
        self.assertEqual(row['support_detail'], '')
        self.assertNotIn('private', json.dumps(result))
        self.assertFalse(result['ai_called'])

    def test_final_action_prepares_only_selected_then_uses_existing_item(self):
        second = self.new_job(payload=self.payload | {'link':'https://example.invalid/second'})
        row = next(row for row in self.listing()['items'] if row['job_id'] == self.job)
        response = self.prepare([row])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()['notion_uploaded'])
        item_id = response.json()['ids'][0]
        with database.get_connection() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM welfare_items').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT job_id FROM news_result_imports').fetchone()[0], self.job)
        rows = self.listing()['items']
        self.assertEqual(len(rows), 2)
        stored = next(row for row in rows if row['item_id'] == item_id)
        self.assertEqual(self.prepare([stored]).json()['ids'], [item_id])
        self.assertTrue(any(row['job_id'] == second for row in rows))

    def test_confirmation_fingerprint_scope_and_auth_are_enforced(self):
        row = self.listing()['items'][0]
        self.assertEqual(self.prepare([row], confirmed=False).status_code, 409)
        self.assertEqual(self.prepare([row], confirmed='true').status_code, 422)
        self.assertEqual(self.prepare([row | {'fingerprint':'0'*64}]).status_code, 409)
        self.assertEqual(self.prepare([row], profile='missing').status_code, 404)
        self.assertEqual(self.client.get('/api/news/feed', headers={'X-Welfare-Session':'bad'}).status_code, 403)
        with database.get_connection() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM welfare_items').fetchone()[0], 0)

    def test_pending_failure_and_nonrelevant_are_not_selectable(self):
        with database.get_connection() as conn:
            conn.execute("UPDATE news_analysis_jobs SET state='failed',reason='ai_input_review_required' WHERE job_id=?", (self.job,))
            conn.commit()
        self.assertEqual(self.listing()['total'], 0)
        row = self.listing('review')['items'][0]
        self.assertFalse(row['selectable'])
        self.assertIn('개인정보', row['input_note'])
        self.assertEqual(self.prepare([row]).status_code, 409)
        with database.get_connection() as conn:
            conn.execute("UPDATE news_analysis_jobs SET state='complete',result_json=? WHERE job_id=?",
                (jobs._encode({'analysis':self.facts | {'is_relevant':False}, 'target_status':'review'}), self.job))
            conn.commit()
        self.assertEqual(self.listing()['total'], 0)
        self.assertFalse(self.listing('review')['items'][0]['selectable'])

    def test_saved_invalid_result_never_becomes_a_usable_fact(self):
        with database.get_connection() as conn:
            conn.execute('UPDATE news_analysis_jobs SET result_json=? WHERE job_id=?',
                (jobs._encode({'analysis':{'target':'FAKE-UNVERIFIED'}, 'target_status':'match'}),self.job))
            conn.commit()
        data = self.listing('review')
        self.assertEqual(data['total'], 1)
        self.assertNotIn('FAKE-UNVERIFIED', json.dumps(data))
        self.assertEqual(self.prepare(data['items']).status_code, 409)

    def test_operational_jobs_are_processing_not_human_review_or_selectable_news(self):
        for state, reason in (('queued', None), ('running', None), ('waiting', 'rate_limited'),
                              ('failed', 'daily_quota_exhausted'), ('failed', 'local_budget_exhausted'),
                              ('failed', 'authentication_required'), ('failed', 'cancelled'),
                              ('failed', 'retry_limit_reached'), ('failed', 'unknown_failure')):
            with self.subTest(state=state, reason=reason):
                with database.get_connection() as conn:
                    conn.execute('UPDATE news_analysis_jobs SET state=?,reason=? WHERE job_id=?',
                                 (state, reason, self.job))
                    conn.commit()
                self.assertEqual(self.listing()['total'], 0)
                self.assertEqual(self.listing('review')['total'], 0)
                data = self.listing('processing')
                self.assertEqual(data['counts'], {'ready': 0, 'review': 0, 'processing': 1, 'records': 0})
                row = data['items'][0]
                self.assertFalse(row['selectable'])
                self.assertFalse(row['review_required'])
                self.assertFalse(row['input_review_required'])
                self.assertTrue(row['processing_required'])
                self.assertEqual(row['job_id'], self.job)
                self.assertTrue(row['run_id'])
                self.assertTrue(row['input_note'])
                self.assertNotIn('private', json.dumps(data))
                self.assertEqual(self.prepare([row]).status_code, 409)

    def test_explicit_safety_holds_remain_review_and_do_not_become_processing(self):
        for reason in ('ai_input_review_required', 'ai_input_size_review_required', 'ai_response_review_required'):
            with self.subTest(reason=reason):
                with database.get_connection() as conn:
                    conn.execute("UPDATE news_analysis_jobs SET state='failed',reason=? WHERE job_id=?", (reason, self.job))
                    conn.commit()
                row = self.listing('review')['items'][0]
                self.assertEqual(row['review_reason'], reason)
                self.assertFalse(row['selectable'])
                self.assertEqual(self.listing('processing')['total'], 0)

    def test_unanalyzed_candidates_and_low_semantic_scores_are_distinct(self):
        with database.get_connection() as conn:
            run = jobs.get_job(conn, self.job)['run_id']
            for route in ('semantic_candidate', 'analysis_unconfigured', 'semantic_review'):
                payload = self.payload | {'link': 'https://example.invalid/' + route}
                conn.execute('INSERT INTO news_run_candidates VALUES (?,?,?,?,?,NULL)',
                             (run, route, '1', jobs._encode(payload), route))
            conn.commit()
        data = self.listing('processing')
        self.assertEqual(data['total'], 2)
        self.assertEqual({row['state'] for row in data['items']}, {'semantic_candidate', 'analysis_unconfigured'})
        review = self.listing('review')['items']
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0]['review_reason'], 'semantic_review')
        self.assertEqual(data['counts'], {'ready': 1, 'review': 1, 'processing': 2, 'records': 0})

    def test_processing_read_preserves_original_job_and_workflow_resume_identity(self):
        with database.get_connection() as conn:
            run = jobs.get_job(conn, self.job)['run_id']
            conn.execute("UPDATE news_analysis_jobs SET state='failed',reason='local_budget_exhausted' WHERE job_id=?", (self.job,))
            conn.execute('INSERT INTO news_profile_workflows(workflow_id,run_id,state,stage) VALUES(?,?,?,?)',
                         ('original-workflow', run, 'waiting', 'analyzing'))
            conn.commit()
            before = list(conn.execute('SELECT input_json,result_json,state,reason FROM news_analysis_jobs'))
            changes = conn.total_changes
            with patch.object(jobs, 'get_job', side_effect=AssertionError('No result reanalysis for waiting jobs')):
                data = feed.read_feed(conn, 'p', view='processing')
            self.assertEqual(conn.total_changes, changes)
            self.assertEqual(list(conn.execute('SELECT input_json,result_json,state,reason FROM news_analysis_jobs')), before)
        row = data['items'][0]
        self.assertEqual((row['workflow_id'], row['workflow_state'], row['run_id']),
                         ('original-workflow', 'waiting', run))
        self.assertNotIn('can_resume', row, 'Only the existing workflow status route decides resumption')

    def test_legacy_unsupported_analysis_is_history_not_a_reviewable_news_result(self):
        with database.get_connection() as conn:
            conn.execute("UPDATE news_analysis_jobs SET recipe='legacy-unsupported' WHERE job_id=?", (self.job,))
            conn.commit()
        self.assertEqual(self.listing()['total'], 0)
        self.assertEqual(self.listing('review')['total'], 0)
        row = self.listing('processing')['items'][0]
        self.assertEqual(row['review_reason'], 'unsupported_analysis')
        self.assertFalse(row['selectable'])

    def test_processing_pagination_and_latest_url_dedup_keep_counts_exact(self):
        for index in range(12):
            self.new_job(state='queued', payload=self.payload | {'link': f'https://example.invalid/waiting/{index}'})
        self.new_job(state='queued')
        first = self.listing('processing')
        self.assertEqual(first['total'], 13)
        self.assertEqual(first['counts']['ready'], 0)
        self.assertEqual(len(first['items']), 10)
        self.assertTrue(first['has_more'])
        second = self.listing('processing', offset=10)
        self.assertEqual(len(second['items']), 3)
        self.assertFalse(second['has_more'])
        self.assertFalse({row['id'] for row in first['items']} & {row['id'] for row in second['items']})

    def test_resolved_processing_record_returns_to_ready_only_after_existing_validation(self):
        with database.get_connection() as conn:
            conn.execute("UPDATE news_analysis_jobs SET state='waiting',reason='rate_limited' WHERE job_id=?", (self.job,))
            conn.commit()
        old_row = self.listing('processing')['items'][0]
        with database.get_connection() as conn:
            conn.execute("UPDATE news_analysis_jobs SET state='complete',reason=NULL WHERE job_id=?", (self.job,))
            conn.commit()
        self.assertEqual(self.listing('processing')['total'], 0)
        ready = self.listing()['items'][0]
        self.assertTrue(ready['selectable'])
        self.assertTrue(ready['review_required'], 'Validated target-review candidates remain visible news')
        self.assertEqual(self.prepare([old_row]).status_code, 409)

    def test_empty_feed_counts_include_processing_without_materializing_data(self):
        with database.get_connection() as conn:
            profiles.create_profile(conn, NewsProfile('empty', 1, '빈 조건', '전국', site_ids=('1',)))
        response = self.client.get('/api/news/feed', params={'profile_id': 'empty', 'view': 'processing'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['counts'], {'ready': 0, 'review': 0, 'processing': 0, 'records': 0})

    def test_newer_same_url_supersedes_old_even_when_latest_is_excluded(self):
        run = None
        with database.get_connection() as conn:
            run = profiles.prepare_run(conn, 'p', expected_revision=1,
                sites=[dict(id=1, name='시험 기관', url='https://example.invalid')])
            payload = self.payload | {'published_date':'2020-01-01', 'selection_reason':'outside_publication_period'}
            conn.execute('INSERT INTO news_run_candidates VALUES (?,?,?,?,?,NULL)',
                (run, 'new-excluded', '1', jobs._encode(payload), 'excluded'))
            conn.commit()
        self.assertEqual(self.listing()['total'], 0)
        records = self.listing('records')['items']
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['published_date'], '2020-01-01')
        self.assertIn('게시 기간', records[0]['input_note'])
        self.assertFalse(records[0]['selectable'])

    def test_same_run_exclusion_order_uses_candidates_not_unrelated_job_rowids(self):
        with database.get_connection() as conn:
            run = jobs.get_job(conn, self.job)['run_id']
            # Jobs can be queued later (semantic/deferred analysis); their
            # table rowids are not the chronological order of collected posts.
            conn.execute('UPDATE news_analysis_jobs SET rowid=9000 WHERE job_id=?', (self.job,))
            conn.execute('INSERT INTO news_run_candidates VALUES (?,?,?,?,?,?)',
                (run, 'earlier', '1', jobs._encode(self.payload), 'matched', self.job))
            conn.execute('INSERT INTO news_run_candidates VALUES (?,?,?,?,?,NULL)',
                (run, 'later-exclusion', '1', jobs._encode(self.payload | {'selection_reason':'not_article'}), 'excluded'))
            conn.commit()
        self.assertEqual(self.listing()['total'], 0)
        records = self.listing('records')['items']
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['review_reason'], 'not_article')
        self.assertEqual(self.prepare(records).status_code, 409)

    def test_linked_selection_exclusion_overrides_completed_relevant_analysis(self):
        with database.get_connection() as conn:
            run = jobs.get_job(conn, self.job)['run_id']
            conn.execute('INSERT INTO news_run_candidates VALUES (?,?,?,?,?,?)',
                (run, 'linked-filter', '1', jobs._encode(self.payload | {'selection_reason':'not_article'}), 'matched', self.job))
            conn.commit()
        self.assertEqual(self.listing()['total'], 0)
        records = self.listing('records')['items']
        self.assertEqual(records[0]['review_reason'], 'not_article')
        self.assertFalse(records[0]['selectable'])
        self.assertEqual(self.prepare(records).status_code, 409)

    def test_imported_deleted_dismissed_and_uploaded_urls_never_reappear(self):
        row = self.listing()['items'][0]
        item_id = self.prepare([row]).json()['ids'][0]
        for mutation in (
            ('UPDATE welfare_items SET notion_dismissed=1 WHERE id=?', (item_id,)),
            ("UPDATE welfare_items SET notion_dismissed=0,notion_page_id='existing' WHERE id=?", (item_id,)),
            ('DELETE FROM welfare_items WHERE id=?', (item_id,)),
        ):
            with database.get_connection() as conn:
                conn.execute(*mutation)
                conn.commit()
            self.new_job()
            for view in ('ready', 'review'):
                self.assertEqual(self.listing(view)['total'], 0)
        self.assertEqual(self.prepare([row]).status_code, 409)

    def test_existing_non_candidate_and_url_fragment_duplicate_are_not_promoted(self):
        with database.get_connection() as conn:
            conn.execute('INSERT INTO welfare_items(title,link) VALUES (?,?)',
                ('기존 정책', self.payload['link']+'#detail'))
            conn.commit()
        self.assertEqual(self.listing()['total'], 0)
        self.assertEqual(self.listing('review')['total'], 0)

    def test_legacy_candidate_remains_default_after_existing_import_receipt(self):
        from services import news_result_import as imports
        with database.get_connection() as conn:
            conn.execute('INSERT INTO welfare_items(title,link,deadline,ai_data) VALUES (?,?,?,?)',
                ('기존 행사', self.payload['link'], '2099-01-01', '{"item_type":"event"}'))
            preview = imports.preview(conn, 'p', self.job)
            imports.import_result(conn, 'p', self.job, fingerprint=preview['fingerprint'], confirmed=True)
            conn.commit()
        response = self.client.get('/api/news/feed')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['total'], 1)

    def test_expired_job_moves_to_records_without_changing_saved_source(self):
        with database.get_connection() as conn:
            original = jobs.get_job(conn, self.job)
            payload = json.loads(original['input_json'])
            payload['content'] += '\n신청 마감 2000-01-01'
            conn.execute('UPDATE news_analysis_jobs SET input_json=?,result_json=? WHERE job_id=?',
                (jobs._encode(payload), jobs._encode({'analysis':self.facts | {'deadline':'2000-01-01'},
                 'target_status':'review'}), self.job))
            conn.commit()
        self.assertEqual(self.listing()['total'], 0)
        rows = self.listing('records')['items']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], '종료')
        self.assertEqual(self.prepare(rows).status_code, 409)

    def test_other_profile_cannot_materialize_or_see_imported_candidate(self):
        row = self.listing()['items'][0]
        self.prepare([row])
        with database.get_connection() as conn:
            profiles.create_profile(conn, NewsProfile('other', 1, '별도 조건', '전국', site_ids=('1',)))
        self.assertEqual(self.prepare([row], profile='other').status_code, 409)
        response = self.client.get('/api/news/feed', params={'profile_id':'default-news'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['total'], 0)

    def test_stale_batch_is_atomic_and_does_not_materialize_first_row(self):
        self.new_job(payload=self.payload | {'link':'https://example.invalid/second'})
        rows = self.listing()['items']
        rows[1]['fingerprint'] = '0'*64
        self.assertEqual(self.prepare(rows).status_code, 409)
        with database.get_connection() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM welfare_items').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT count(*) FROM news_result_imports').fetchone()[0], 0)

    def test_materialization_failure_rolls_back_every_selected_item(self):
        self.new_job(payload=self.payload | {'link':'https://example.invalid/second'})
        rows = self.listing()['items']
        with database.get_connection() as conn:
            conn.execute("""CREATE TRIGGER fail_second BEFORE INSERT ON news_result_imports
                WHEN (SELECT count(*) FROM news_result_imports)>0
                BEGIN SELECT RAISE(ABORT,'do-not-expose-this'); END""")
            conn.commit()
        response = self.prepare(rows)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('do-not-expose-this', response.text)
        with database.get_connection() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM welfare_items').fetchone()[0], 0)

    def test_uncertain_upload_cannot_be_silently_retried(self):
        item_id = self.prepare(self.listing()['items']).json()['ids'][0]
        with database.get_connection() as conn:
            conn.execute('CREATE TABLE notion_upload_attempts(link TEXT PRIMARY KEY,page_id TEXT)')
            conn.execute('INSERT INTO notion_upload_attempts VALUES (?,NULL)', (self.payload['link'],))
            conn.commit()
        row = self.listing()['items'][0]
        self.assertTrue(row['upload_uncertain'])
        self.assertEqual(self.prepare([row], action='upload').status_code, 409)
        self.assertEqual(self.prepare([row], action='retry').json()['ids'], [item_id])
        with database.get_connection() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM notion_upload_attempts').fetchone()[0], 1)

    def test_pagination_is_ten_and_no_duplicate_runs(self):
        for index in range(12):
            self.new_job(payload=self.payload | {'link':f'https://example.invalid/article/{index}'})
        self.new_job()
        first = self.listing()
        self.assertEqual(first['total'], 13)
        self.assertEqual(len(first['items']), 10)
        self.assertTrue(first['has_more'])
        second = self.listing(offset=10)
        self.assertEqual(len(second['items']), 3)
        self.assertFalse(second['has_more'])

    def test_publication_day_is_not_confused_with_deadline_or_fetch_date(self):
        with database.get_connection() as conn:
            run = jobs.get_job(conn, self.job)['run_id']
            payload = self.payload | {'published_date':'2026-09-16', 'publication_status':'known'}
            conn.execute('INSERT INTO news_run_candidates VALUES (?,?,?,?,?,?)',
                (run, 'dated', '1', jobs._encode(payload), 'matched', self.job))
            conn.commit()
        row = self.listing()['items'][0]
        self.assertEqual(row['published_date'], '2026-09-16')
        self.assertEqual(row['start'], '')
        self.assertTrue(row['selectable'])
        item_id = self.prepare([row]).json()['ids'][0]
        stored = next(item for item in self.listing()['items'] if item['item_id'] == item_id)
        self.assertEqual(stored['published_date'], '2026-09-16')


if __name__ == '__main__':
    unittest.main()
