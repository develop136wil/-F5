import sqlite3
import unittest
from dataclasses import replace

from services.news_profile import NewsProfile
from services import news_profile_store as profiles, news_analysis_store as jobs
from services.news_candidate_intake import register_candidates, ensure_schema
from services.news_custom_analysis import RECIPE


class TestCandidateIntake(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        profiles.ensure_schema(self.conn)
        jobs.ensure_schema(self.conn)
        ensure_schema(self.conn)
        self.profile = NewsProfile('p', 1, '노년', '전국', keywords=('지원',),
                                  excluded_keywords=('채용',), site_ids=('1',))
        profiles.create_profile(self.conn, self.profile)
        self.run = profiles.prepare_run(self.conn, 'p', expected_revision=1,
            sites=[dict(id=1, name='고정 출처', url='https://example.invalid')])
        self.candidate = dict(title='노년 지원', link='https://example.invalid/post', content='본문')

    def tearDown(self):
        self.conn.close()

    def register(self, candidates, site='1', model='fixed-model'):
        return register_candidates(self.conn, self.run, site, candidates, model=model)

    def test_routes_preserve_semantic_candidates_without_ai(self):
        result = self.register([self.candidate, self.candidate | {'title': '지원 채용'}, self.candidate | {'title': '돌봄 소식'}])
        self.assertEqual([r['route'] for r in result], ['matched', 'excluded', 'semantic_candidate'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_run_candidates').fetchone()[0], 3)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_jobs').fetchone()[0], 1)
        job = jobs.get_job(self.conn, result[0]['job_id'])
        self.assertEqual((job['state'], job['model'], job['recipe']), ('queued', 'fixed-model', RECIPE))
        self.assertIn('고정 출처', job['input_json'])

    def test_duplicate_batch_does_not_queue_again_or_change_model(self):
        first = self.register([self.candidate])[0]
        second = self.register([self.candidate], model='different-model')[0]
        self.assertFalse(second['created'])
        self.assertEqual(first['job_id'], second['job_id'])
        self.assertEqual(jobs.get_job(self.conn, first['job_id'])['model'], 'fixed-model')

    def test_category_only_profile_queues_once_and_general_stays_unqueued(self):
        profile = replace(self.profile, profile_id='category', keywords=(),
                          characteristics=('일반', '장애인'))
        profiles.create_profile(self.conn, profile)
        run = profiles.prepare_run(self.conn, 'category', expected_revision=1,
            sites=[dict(id=1, name='고정 출처', url='https://example.invalid')])
        candidate = self.candidate | {'title':'장애인 이동 서비스 안내'}
        def register(values):
            return register_candidates(self.conn, run, '1', values, model='fixed-model')
        first = register([candidate, candidate])
        self.assertEqual([r['created'] for r in first], [True, False])
        self.assertEqual(first[0]['route'], 'matched')
        self.assertEqual(first[0]['job_id'], first[1]['job_id'])
        general = register([candidate | {'title':'일반 안내'}])[0]
        self.assertEqual(general['route'], 'semantic_candidate')
        self.assertIsNone(general['job_id'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_jobs').fetchone()[0], 1)

    def test_oversized_batch_fails_before_any_work_is_created(self):
        with self.assertRaises(ValueError):
            self.register([self.candidate] * 101)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_jobs').fetchone()[0], 0)

    def test_explicit_body_groups_queue_only_for_analysis_once(self):
        profile = replace(self.profile, profile_id='body', keywords=(),
                          life_cycles=('아동',), characteristics=('장애인',))
        profiles.create_profile(self.conn, profile)
        run = profiles.prepare_run(self.conn, 'body', expected_revision=1,
            sites=[dict(id=1, name='시험', url='https://example.invalid')])
        base = self.candidate | {'title':'참여자 모집', 'content':'모집대상: 장애인 아동'}
        values = [base, base,
                  base | {'content':'모집대상: 아동'},
                  base | {'content':'모집대상: 장애인 아동 제외'},
                  base | {'content':'메뉴 장애인 아동'},
                  base | {'title':'채용', 'content':'모집대상: 장애인 아동'},
                  base | {'content':'모집대상: 노년'}]
        result = register_candidates(self.conn, run, '1', values, model='fixed-model')
        self.assertEqual([r['route'] for r in result],
            ['matched','matched','semantic_candidate','semantic_candidate','semantic_candidate','excluded','semantic_candidate'])
        self.assertFalse(result[1]['created'])
        self.assertEqual(result[0]['job_id'], result[1]['job_id'])
        job = jobs.get_job(self.conn, result[0]['job_id'])
        self.assertEqual(job['state'], 'queued')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_jobs').fetchone()[0],1)

    def test_invalid_second_candidate_rolls_back_entire_batch(self):
        with self.assertRaises(ValueError):
            self.register([self.candidate, self.candidate | {'content': None}])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_run_candidates').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_jobs').fetchone()[0], 0)

    def test_changed_profile_does_not_change_original_run(self):
        profiles.save_revision(self.conn, replace(self.profile, revision=2, keywords=('의료',)), expected_revision=1)
        self.assertEqual(self.register([self.candidate])[0]['route'], 'matched')

    def test_unselected_site_and_unsafe_link_rejected(self):
        with self.assertRaises(ValueError):
            self.register([self.candidate], site='2')
        for link in ('file:///private', 'https://user:pass@example.invalid'):
            with self.assertRaises(ValueError):
                self.register([self.candidate | {'link': link}])

    def test_local_and_private_literal_addresses_are_rejected(self):
        for link in ('http://localhost/post', 'http://service.local/post',
                     'http://127.0.0.1/post', 'http://10.0.0.2/post', 'http://[::1]/post'):
            with self.subTest(link=link), self.assertRaises(ValueError):
                self.register([self.candidate | {'link': link}])

    def test_changed_content_registers_new_analysis(self):
        first = self.register([self.candidate])[0]
        second = self.register([self.candidate | {'content': '변경된 내용'}])[0]
        self.assertNotEqual(first['job_id'], second['job_id'])
