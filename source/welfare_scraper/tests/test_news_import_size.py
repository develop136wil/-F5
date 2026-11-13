"""Offline manual-import size boundaries; no provider or operating DB access.

The service-reader cases are already-reviewed synthetic records. Integration
cases use real saved-result revalidation with v13's canonical prose contract.
They do not claim that a live model generated these long fields.
"""
import copy
import json
import sqlite3
import unittest

from services import news_analysis_store as jobs, news_custom_analysis as custom
from services import news_profile_store as profiles, news_result_import as imports
from services.news_profile import NewsProfile


FIELDS = ('category', 'target', 'support_detail', 'eligibility_detail', 'contact', 'deadline')


def facts():
    return dict(is_relevant=True, category='복지', target='개인', residence=[],
        life_cycle=[], target_characteristics=[], interest_topics=[],
        start_age_months=None, end_age_months=None, evidence_snippet='',
        support_detail='', eligibility_detail='', contact='', deadline='',
        applicant_type='individual', applicant_evidence='개인이 신청합니다.',
        applicant_detail='개인', beneficiary_detail='개인',
        life_cycle_evidence=[], residence_evidence=[],
        fact_evidence=dict.fromkeys(('support_detail', 'eligibility_detail', 'contact',
                                    'deadline', 'is_relevant'), ''))


class NewsImportSize(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.execute('PRAGMA foreign_keys=ON')
        profiles.ensure_schema(self.conn)
        jobs.ensure_schema(self.conn)
        self.conn.execute('''CREATE TABLE welfare_items (
            id INTEGER PRIMARY KEY, title TEXT, link TEXT UNIQUE, site_name TEXT,
            category TEXT, target TEXT, residence TEXT, life_cycle TEXT,
            target_characteristics TEXT, start_age_months INTEGER, end_age_months INTEGER,
            support_detail TEXT, eligibility_detail TEXT, contact TEXT, deadline TEXT, ai_data TEXT)''')
        imports.ensure_schema(self.conn)
        self.conn.execute("INSERT INTO welfare_items(title,link,support_detail) VALUES ('합성 사용자 기록','https://example.invalid/user','기존 내용')")
        self.profile = NewsProfile('p', 1, '합성 크기 검사', '전국', site_ids=('1',))
        profiles.create_profile(self.conn, self.profile)
        self.payload = dict(title='합성 안내', site_name='합성 기관',
            link='https://example.invalid/size', content='개인이 신청합니다.')

    def tearDown(self):
        self.conn.close()

    def snapshot(self):
        return {name: self.conn.execute(f'SELECT * FROM {name} ORDER BY rowid').fetchall()
            for name in ('welfare_items', 'news_result_imports', 'news_analysis_jobs', 'news_analysis_cache')}

    def reviewed_job(self, value):
        return dict(state='complete', recipe=custom.SOURCE_SPAN_RECIPE,
            input_json=jobs._encode(self.payload),
            result_json=jobs._encode(dict(analysis=value, target_status='match', accepted=True)))

    def read_eligibility(self, value):
        return imports.eligibility(self.conn, 'p', 'synthetic-reviewed',
            reviewed_job=self.reviewed_job(value), run_snapshot={'profile':self.profile.snapshot()})

    def test_source_prose_allows_30000_but_scalar_fields_keep_16000_limit(self):
        before = self.snapshot()
        for field in FIELDS:
            for size in (16000, 16001, 30000):
                with self.subTest(field=field, size=size):
                    value = facts() | {field:'가' * size}
                    original = copy.deepcopy(value)
                    custom.validate_facts(value)
                    result = self.read_eligibility(value)
                    allowed = field in ('support_detail', 'eligibility_detail') or size == 16000
                    self.assertEqual(result, {'allowed':allowed,
                        'reason':'confirmation_required' if allowed else 'import_text_too_long'})
                    self.assertEqual(value, original)
                    self.assertEqual(self.snapshot(), before)

    def test_invalid_canonical_types_and_over_30000_keep_invalid_saved_contract(self):
        before = self.snapshot()
        for field in FIELDS:
            for malformed in (False, 1, [], {}, None):
                # The existing optional prose contract rejects an explicit None;
                # category/target are required nonempty strings too.
                with self.subTest(field=field, malformed=malformed):
                    self.assertEqual(self.read_eligibility(facts() | {field:malformed}),
                        {'allowed':False, 'reason':'invalid_saved_result'})
        for field in ('support_detail', 'eligibility_detail', 'contact', 'deadline'):
            with self.subTest(field=field, size=30001):
                self.assertEqual(self.read_eligibility(facts() | {field:'가' * 30001}),
                    {'allowed':False, 'reason':'invalid_saved_result'})
        self.assertEqual(self.snapshot(), before)

    def seed(self, size):
        # Exact contiguous source and evidence prevent the safety layer from
        # correctly withholding fabricated text before this size check is reached.
        tail = '\n개인이 신청합니다.'
        source = '지원 안내\n' + '가' * (size - len('지원 안내\n') - len(tail)) + tail
        self.assertEqual(len(source), size)
        value = facts() | {'support_detail':source,
            'fact_evidence':facts()['fact_evidence'] | {'support_detail':source}}
        run = profiles.prepare_run(self.conn, 'p', expected_revision=1,
            sites=[dict(id=1, name='합성 기관', url='https://example.invalid')])
        job = jobs.enqueue(self.conn, run, payload=self.payload | {'content':source},
            model='offline-size', recipe=custom.SOURCE_SPAN_RECIPE)
        token = jobs.claim(self.conn, job, now=0)
        self.assertIsNotNone(token)
        jobs.complete(self.conn, job, token, dict(analysis=value, accepted=True,
            target_status='match', review_required=False,
            import_eligibility={'allowed':True, 'reason':'confirmation_required'}))
        self.conn.commit()
        return job, source

    def test_real_revalidation_list_preview_and_forced_confirm_do_not_write_invalid_oversize(self):
        for size in (30001, 32000):
            with self.subTest(size=size):
                job, source = self.seed(size)
                before = self.snapshot()
                result = json.loads(jobs.get_job(self.conn, job)['result_json'])
                self.assertEqual(result['analysis']['support_detail'], source)
                self.assertEqual(imports.eligibility(self.conn, 'p', job),
                    {'allowed':False, 'reason':'invalid_saved_result'})
                listed = next(row for row in jobs.list_profile_results(self.conn, 'p')['items'] if row['job_id'] == job)
                self.assertEqual(listed['import_eligibility'],
                    {'allowed':False, 'reason':'invalid_saved_result'})
                for action in (
                    lambda: imports.preview(self.conn, 'p', job),
                    lambda: imports.import_result(self.conn, 'p', job, fingerprint='0'*64, confirmed=True),
                ):
                    with self.assertRaisesRegex(ValueError, 'Invalid custom text fact'):
                        action()
                    self.assertEqual(self.snapshot(), before)
                with self.assertRaisesRegex(ValueError, 'Explicit confirmation'):
                    imports.import_result(self.conn, 'p', job, fingerprint='0'*64, confirmed=False)
                self.assertEqual(self.snapshot(), before)

    def test_boundary_import_preserves_tail_and_requires_current_fingerprint(self):
        job, source = self.seed(30000)
        preview = imports.preview(self.conn, 'p', job)
        self.assertEqual(preview['status'], 'ready')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'Preview changed'):
            imports.import_result(self.conn, 'p', job, fingerprint='0'*64, confirmed=True)
        self.assertEqual(self.snapshot(), before)
        result = imports.import_result(self.conn, 'p', job,
            fingerprint=preview['fingerprint'], confirmed=True)
        self.assertEqual(result['status'], 'created')
        row = self.conn.execute('SELECT support_detail,ai_data FROM welfare_items WHERE id=?', (result['item_id'],)).fetchone()
        self.assertEqual(row[0], source)
        self.assertEqual(json.loads(row[1])['support_detail'], source)
        after = self.snapshot()
        self.assertEqual(after['news_analysis_jobs'], before['news_analysis_jobs'])
        self.assertEqual(after['news_analysis_cache'], before['news_analysis_cache'])
        self.assertEqual(after['welfare_items'][0], before['welfare_items'][0])
        self.assertEqual(len(after['news_result_imports']), 1)

    def test_source_prose_keeps_outer_whitespace_while_scalar_fields_are_trimmed(self):
        source = '  원문 들여쓰기\n마지막 조건  \n'
        value = facts() | {'support_detail':source, 'eligibility_detail':source, 'contact':'  문의  '}
        _payload, imported, _fingerprint, _revision = imports._read(
            self.conn, 'p', 'synthetic-reviewed', reviewed_job=self.reviewed_job(value),
            run_snapshot={'profile':self.profile.snapshot()})
        self.assertEqual(imported['support_detail'], source)
        self.assertEqual(imported['eligibility_detail'], source)
        self.assertEqual(imported['contact'], '문의')

    def test_failed_local_recheck_blocks_list_preview_and_confirmed_import_from_cache_too(self):
        job, _source = self.seed(100)
        old_preview = imports.preview(self.conn, 'p', job)
        saved = self.conn.execute('SELECT result_json,cache_key FROM news_analysis_jobs WHERE job_id=?', (job,)).fetchone()
        tampered = json.loads(saved[0])
        # A missing saved source field raises during current grounding. That
        # path intentionally retains the original analysis, which must never be
        # treated as approved fields merely because a person confirms an import.
        tampered['analysis'].update(support_detail='원문에 없는 허위 지원 문장')
        tampered.update(accepted=True, target_status='match', review_required=False,
            import_eligibility={'allowed':True, 'reason':'confirmation_required'})
        original_payload = json.loads(self.conn.execute('SELECT input_json FROM news_analysis_jobs WHERE job_id=?', (job,)).fetchone()[0])
        broken_payload = {key:value for key, value in original_payload.items() if key != 'content'}
        self.conn.execute('UPDATE news_analysis_jobs SET input_json=?,result_json=? WHERE job_id=?',
            (jobs._encode(broken_payload), jobs._encode(tampered), job))
        self.conn.execute('UPDATE news_analysis_cache SET result_json=? WHERE cache_key=?', (jobs._encode(tampered), saved[1]))
        self.conn.commit()
        run = profiles.prepare_run(self.conn, 'p', expected_revision=1,
            sites=[dict(id=1, name='합성 기관', url='https://example.invalid')])
        cached_job = jobs.enqueue(self.conn, run, payload=original_payload,
            model='offline-size', recipe=custom.SOURCE_SPAN_RECIPE)
        self.conn.execute('UPDATE news_analysis_jobs SET input_json=? WHERE job_id=?',
            (jobs._encode(broken_payload), cached_job))
        self.conn.commit()
        self.assertIsNone(jobs.claim(self.conn, cached_job, now=0))
        self.assertEqual(jobs.get_job(self.conn, cached_job)['attempts'], 0)
        before = self.snapshot()
        for candidate in (job, cached_job):
            with self.subTest(candidate='original' if candidate == job else 'cache'):
                reviewed = json.loads(jobs.get_job(self.conn, candidate)['result_json'])
                self.assertEqual(reviewed['filter_reason'], 'invalid_saved_analysis')
                self.assertFalse(reviewed['accepted'])
                self.assertEqual(imports.eligibility(self.conn, 'p', candidate),
                    {'allowed':False, 'reason':'invalid_saved_result'})
                listing = next(row for row in jobs.list_profile_results(self.conn, 'p')['items'] if row['job_id'] == candidate)
                self.assertEqual(listing['import_eligibility'],
                    {'allowed':False, 'reason':'invalid_saved_result'})
                self.assertEqual(listing['details'], {})
                self.assertNotIn('허위 지원', json.dumps(listing, ensure_ascii=False))
                for action in (
                    lambda: imports.preview(self.conn, 'p', candidate),
                    lambda: imports.import_result(self.conn, 'p', candidate,
                        fingerprint=old_preview['fingerprint'], confirmed=True),
                ):
                    with self.assertRaises(imports.ImportUnavailable) as caught:
                        action()
                    self.assertEqual(caught.exception.reason, 'invalid_saved_result')
                    self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_result_imports').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM welfare_items').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
