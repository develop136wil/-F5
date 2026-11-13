from dataclasses import replace
import sqlite3
import unittest

from services.news_profile import NewsProfile
from services import news_profile_store as profiles
from services import news_analysis_store as jobs


class TestNewsAnalysisStore(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.execute('PRAGMA foreign_keys=ON')
        profiles.ensure_schema(self.conn)
        jobs.ensure_schema(self.conn)
        self.profile = NewsProfile('p', 1, '시험', '서울', site_ids=('1',))
        profiles.create_profile(self.conn, self.profile)
        self.run = self.make_run('p')
        self.payload = dict(title='지원', link='https://example.invalid/1', site_name='시험', content='본문')

    def tearDown(self):
        self.conn.close()

    def make_run(self, profile_id, revision=1):
        return profiles.prepare_run(self.conn, profile_id, expected_revision=revision,
                                    sites=[dict(id=1, name='시험', url='https://example.invalid')])

    def test_source_completeness_is_derived_separately_from_target_status(self):
        from services.page_coverage import make
        payload = {'content':'확보한 본문'}
        cases = [({}, {}, 'empty'), ({'content':'  '}, {'source_input':{'status':'complete'}}, 'empty'),
                 (payload, {}, 'unknown'), (payload, {'source_input':{'status':'complete'}}, 'complete'),
                 (payload, {'source_input':{'status':'partial'}}, 'partial'),
                 (payload, {'source_page':make('attachment_review')}, 'partial'),
                 (payload | {'source_page':make('fetch_failed')}, {}, 'partial'),
                 (payload | {'source_page':make('fetch_failed')}, {'source_page':make()}, 'partial'),
                 (payload, {'source_attachments':{'status':'review_required'}}, 'partial')]
        for source, analysis, expected in cases:
            with self.subTest(expected=expected, source=source, analysis=analysis):
                self.assertEqual(jobs._source_review(source, analysis)['status'], expected)

    def enqueue(self, run=None, **changes):
        return jobs.enqueue(self.conn, run or self.run,
                            **(dict(payload=self.payload, model='model-a', recipe='v1') | changes))

    def test_duplicate_enqueue_and_exclusive_claim(self):
        job = self.enqueue()
        self.assertEqual(job, self.enqueue())
        self.assertIsNotNone(jobs.claim(self.conn, job, now=0))
        self.assertIsNone(jobs.claim(self.conn, job, now=0))
        self.assertEqual(jobs.get_job(self.conn, job)['attempts'], 1)

    def test_ineligible_claim_never_reads_or_rewrites_cache(self):
        from unittest.mock import patch
        job = self.enqueue()
        token = jobs.claim(self.conn, job, now=0)
        jobs.complete(self.conn, job, token, {'accepted':True})
        before = jobs.get_job(self.conn, job)
        with patch('services.news_analysis_context.NewsAnalysisContext.from_run',
                   side_effect=AssertionError('Completed job must not be re-evaluated')):
            self.assertIsNone(jobs.claim(self.conn, job, now=0))
        self.assertEqual(jobs.get_job(self.conn, job), before)

    def test_inconsistent_accepted_cache_preserves_review_hold(self):
        import json
        from services.news_custom_analysis import RECIPE
        first = self.enqueue(recipe=RECIPE)
        token = jobs.claim(self.conn, first, now=0)
        jobs.complete(self.conn, first, token, {
            'analysis':{'is_relevant':True,'residence':['전국']},
            'accepted':True, 'review_required':True, 'target_status':'review'})
        second = self.enqueue(self.make_run('p'), recipe=RECIPE)
        self.assertIsNone(jobs.claim(self.conn, second, now=0))
        result = json.loads(jobs.get_job(self.conn, second)['result_json'])
        self.assertFalse(result['accepted'])
        self.assertTrue(result['review_required'])
        self.assertEqual(jobs.get_job(self.conn, second)['attempts'], 0)

    def test_custom_cache_rechecks_target_without_another_attempt(self):
        import json
        from services.news_custom_analysis import RECIPE
        profile = replace(self.profile, profile_id='specific', region='전국',
                          characteristics=('일반', '장애인'))
        profiles.create_profile(self.conn, profile)
        first = self.enqueue(self.make_run('specific'), recipe=RECIPE)
        token = jobs.claim(self.conn, first, now=0)
        old = {'analysis':{'is_relevant':True, 'target_characteristics':['일반']},
               'accepted':True, 'target_status':'match'}
        jobs.complete(self.conn, first, token, old)
        second = self.enqueue(self.make_run('specific'), recipe=RECIPE)
        self.assertIsNone(jobs.claim(self.conn, second, now=0))
        job = jobs.get_job(self.conn, second)
        self.assertEqual(job['attempts'], 0)
        result = json.loads(job['result_json'])
        self.assertFalse(result['accepted'])
        self.assertEqual(result['target_status'], 'review')
        # Reads now apply the same local evidence guard to historical matches;
        # the stored result itself remains unchanged for inspection/recovery.
        self.assertEqual(json.loads(jobs.get_job(self.conn, first)['result_json'])['target_status'], 'review')
        self.assertEqual(json.loads(self.conn.execute(
            'SELECT result_json FROM news_analysis_jobs WHERE job_id=?', (first,)).fetchone()[0]), old)

    def test_cache_reused_only_for_identical_inputs(self):
        first = self.enqueue()
        token = jobs.claim(self.conn, first, now=0)
        jobs.complete(self.conn, first, token, {'is_relevant': True})
        other_run = self.make_run('p')
        second = self.enqueue(other_run)
        self.assertIsNone(jobs.claim(self.conn, second, now=0))
        self.assertEqual(jobs.get_job(self.conn, second)['state'], 'complete')
        for changes in (dict(model='model-b'), dict(recipe='v2'),
                        dict(payload=self.payload | {'content': '변경'}),
                        dict(payload=self.payload | {'title': '변경'})):
            with self.subTest(changes=changes):
                job = self.enqueue(other_run, **changes)
                self.assertIsNotNone(jobs.claim(self.conn, job, now=0))

    def test_cache_hit_does_not_count_as_an_ai_attempt(self):
        first = self.enqueue()
        jobs.complete(self.conn, first, jobs.claim(self.conn, first, now=0), {'is_relevant': True})
        second = self.enqueue(self.make_run('p'))
        self.assertIsNone(jobs.claim(self.conn, second, now=0))
        stored = jobs.get_job(self.conn, second)
        self.assertEqual((stored['state'], stored['attempts']), ('complete', 0))

    def test_corrupt_cache_is_discarded_and_job_is_claimed(self):
        job = self.enqueue()
        row = self.conn.execute('SELECT cache_key FROM news_analysis_jobs WHERE job_id=?', (job,)).fetchone()
        self.conn.execute('INSERT INTO news_analysis_cache(cache_key,result_json) VALUES (?,?)', (row[0], '{broken'))
        token = jobs.claim(self.conn, job, now=0)
        self.assertIsNotNone(token)
        self.assertEqual(jobs.get_job(self.conn, job)['state'], 'running')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_cache').fetchone()[0], 0)

    def test_profile_and_revision_are_separate_cache_domains(self):
        first = self.enqueue()
        jobs.complete(self.conn, first, jobs.claim(self.conn, first, now=0), {'accepted': True})
        profiles.create_profile(self.conn, replace(self.profile, profile_id='other'))
        other = self.enqueue(self.make_run('other'))
        self.assertIsNotNone(jobs.claim(self.conn, other, now=0))
        profiles.save_revision(self.conn, replace(self.profile, revision=2), expected_revision=1)
        revised = self.enqueue(self.make_run('p', 2))
        self.assertIsNotNone(jobs.claim(self.conn, revised, now=0))

    def test_quota_wait_keeps_original_inputs(self):
        job = self.enqueue()
        original = jobs.get_job(self.conn, job)
        token = jobs.claim(self.conn, job, now=0)
        jobs.defer(self.conn, job, token, retry_after=60, reason='quota')
        self.assertIsNone(jobs.claim(self.conn, job, now=59))
        next_token = jobs.claim(self.conn, job, now=60)
        self.assertIsNotNone(next_token)
        self.assertNotEqual(token, next_token)
        with self.assertRaises(ValueError):
            jobs.complete(self.conn, job, token, {'stale': True})
        current = jobs.get_job(self.conn, job)
        self.assertEqual(current['input_json'], original['input_json'])
        self.assertEqual(current['run_id'], original['run_id'])

    def test_result_list_exposes_only_fixed_manual_review_reason_codes(self):
        import json
        allowed = ('ai_input_review_required', 'ai_input_size_review_required',
                   'ai_response_review_required')
        expected = {}
        for index, reason in enumerate((*allowed, 'daily_quota_exhausted', 'private provider error')):
            job = self.enqueue(payload=self.payload | {'link': f'https://example.invalid/{index}',
                                                       'content': 'private original source body'})
            token = jobs.claim(self.conn, job, now=0)
            jobs.defer(self.conn, job, token, retry_after=0, reason=reason, failed=True)
            expected[job] = reason if reason in allowed else ''
        items = jobs.list_profile_results(self.conn, 'p')['items']
        self.assertEqual({item['job_id']: item['review_reason'] for item in items}, expected)
        encoded = json.dumps(items)
        for private in ('private provider error', 'private original source body', 'input_json', 'claim_token'):
            self.assertNotIn(private, encoded)
        job = next(iter(expected))
        self.conn.execute("UPDATE news_analysis_jobs SET state='waiting' WHERE job_id=?", (job,))
        item = next(item for item in jobs.list_profile_results(self.conn, 'p')['items'] if item['job_id'] == job)
        self.assertEqual(item['review_reason'], '')

    def test_failure_is_not_cached_or_automatically_retried(self):
        job = self.enqueue()
        token = jobs.claim(self.conn, job, now=0)
        jobs.defer(self.conn, job, token, retry_after=0, reason='invalid_output', failed=True)
        self.assertIsNone(jobs.claim(self.conn, job, now=100))
        other = self.enqueue(self.make_run('p'))
        self.assertIsNotNone(jobs.claim(self.conn, other, now=0))

    def test_recovery_invalidates_old_claim(self):
        job = self.enqueue()
        token = jobs.claim(self.conn, job, now=0)
        self.assertEqual(jobs.recover_interrupted_run(self.conn, self.run), 1)
        with self.assertRaises(ValueError):
            jobs.complete(self.conn, job, token, {'old': True})
        self.assertIsNotNone(jobs.claim(self.conn, job, now=0))

    def test_completion_cache_and_job_are_atomic(self):
        job = self.enqueue()
        token = jobs.claim(self.conn, job, now=0)
        self.conn.execute('''CREATE TRIGGER fail_complete BEFORE UPDATE ON news_analysis_jobs
            WHEN NEW.state='complete' BEGIN SELECT RAISE(ABORT,'failure'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            jobs.complete(self.conn, job, token, {'result': True})
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_cache').fetchone()[0], 0)
        self.assertEqual(jobs.get_job(self.conn, job)['state'], 'running')

    def test_schema_and_transactions_preserve_old_data(self):
        self.conn.execute('CREATE TABLE welfare_items (title TEXT)')
        self.conn.execute("INSERT INTO welfare_items VALUES ('既存')")
        self.conn.commit()
        jobs.ensure_schema(self.conn)
        self.conn.execute('BEGIN')
        job = self.enqueue()
        self.conn.rollback()
        with self.assertRaises(KeyError):
            jobs.get_job(self.conn, job)
        self.assertEqual(self.conn.execute('SELECT title FROM welfare_items').fetchone()[0], '既存')

    def test_queued_cancel_and_manual_resume_preserve_inputs(self):
        job = self.enqueue()
        before = jobs.get_job(self.conn, job)
        self.assertTrue(jobs.cancel_job(self.conn, job))
        self.assertIsNone(jobs.claim(self.conn, job, now=0))
        jobs.resume_job(self.conn, job, expected_attempts=0)
        with self.assertRaises(ValueError):
            jobs.resume_job(self.conn, job, expected_attempts=0)
        after = jobs.get_job(self.conn, job)
        self.assertEqual(before['input_json'], after['input_json'])
        self.assertEqual(before['run_id'], after['run_id'])
        self.assertIsNotNone(jobs.claim(self.conn, job, now=0))

    def test_running_cancel_prevents_early_resume_and_result_cache(self):
        job = self.enqueue()
        token = jobs.claim(self.conn, job, now=0)
        jobs.cancel_job(self.conn, job)
        with self.assertRaises(ValueError):
            jobs.resume_job(self.conn, job, expected_attempts=1)
        self.assertFalse(jobs.complete(self.conn, job, token, {'answer': True}))
        self.assertEqual(jobs.get_job(self.conn, job)['reason'], 'cancelled')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_cache').fetchone()[0], 0)
        jobs.resume_job(self.conn, job, expected_attempts=1)
        self.assertEqual(jobs.get_job(self.conn, job)['retry_start_attempt'], 1)
        self.assertEqual(jobs.get_job(self.conn, job)['attempts'], 1)

    def test_completed_job_not_cancelled_or_resumed(self):
        job = self.enqueue()
        jobs.complete(self.conn, job, jobs.claim(self.conn, job, now=0), {'answer': True})
        self.assertFalse(jobs.cancel_job(self.conn, job))
        with self.assertRaises(ValueError):
            jobs.resume_job(self.conn, job, expected_attempts=1)

    def test_cancel_survives_error_and_crash_recovery(self):
        for recover in (True, False):
            job = self.enqueue(payload=self.payload | {'title': str(recover)})
            token = jobs.claim(self.conn, job, now=0)
            jobs.cancel_job(self.conn, job)
            if recover:
                jobs.recover_interrupted_run(self.conn, self.run)
            else:
                jobs.defer(self.conn, job, token, retry_after=60, reason='network')
            self.assertEqual(jobs.get_job(self.conn, job)['state'], 'failed')
            self.assertEqual(jobs.get_job(self.conn, job)['reason'], 'cancelled')

    def test_status_omits_sensitive_payload_and_supports_paging(self):
        self.enqueue()
        rows = jobs.list_job_statuses(self.conn, self.run, limit=1)
        self.assertEqual(len(rows), 1)
        self.assertFalse({'input_json', 'claim_token', 'result_json'} & set(rows[0]))
        self.assertEqual(jobs.list_job_statuses(self.conn, self.run, offset=1), [])
        with self.assertRaises(ValueError):
            jobs.list_job_statuses(self.conn, self.run, limit=1000)

    def test_resume_keeps_failure_audit_and_rejects_invalid_input(self):
        job = self.enqueue()
        token = jobs.claim(self.conn, job, now=0)
        jobs.defer(self.conn, job, token, retry_after=0, reason='invalid_analysis', failed=True)
        with self.assertRaises(ValueError):
            jobs.resume_job(self.conn, job, expected_attempts=1)
        # Cannot turn an invalid input into a retryable cancellation.
        self.assertFalse(jobs.cancel_job(self.conn, job))
        self.assertEqual(jobs.get_job(self.conn, job)['reason'], 'invalid_analysis')


if __name__ == '__main__':
    unittest.main()
