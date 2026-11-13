"""No-network tests for trusted Bokjiro result storage and change detection."""
from dataclasses import replace
import sqlite3
import unittest

from services.news_profile import NewsProfile
from services import news_profile_store as profiles
from services import news_analysis_store as jobs
from services import news_official_results as official
from services.bokjiro_collector import normalize_official_service


class TestNewsOfficialResults(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.execute('PRAGMA foreign_keys=ON')
        profiles.ensure_schema(self.conn)
        jobs.ensure_schema(self.conn)
        official.ensure_schema(self.conn)
        self.profile = NewsProfile(
            'p', 1, '노년 주거', '전국', sources=('bokjiro',), site_ids=(),
            life_cycles=('노년',), interest_topics=('주거',),
            bokjiro_mode='official_tags_v1')
        profiles.create_profile(self.conn, self.profile)
        self.row = {
            'WLFARE_INFO_ID': 'svc-1', 'WLFARE_INFO_NM': '노년 주거 지원',
            'WLFARE_INFO_OUTL_CN': '주거비를 지원합니다.', 'BIZ_CHR_INST_NM': '보건복지부',
            'ADDR': '', 'RETURN_STR': 'BKJR_LFTM_CYC_CD:노년;INTRS_THEMA_CD:주거',
        }

    def tearDown(self):
        self.conn.close()

    def make_run(self):
        return profiles.prepare_run(self.conn, 'p', expected_revision=1, sites=[])

    def test_new_unchanged_updated_and_same_run_idempotency(self):
        item = normalize_official_service(self.row)
        first = self.make_run()
        self.assertEqual(official.register_official_results(self.conn, first, [item]), [{
            'source_key': 'bokjiro:svc-1', 'change_state': 'new', 'created': True}])
        self.assertFalse(official.register_official_results(self.conn, first, [item])[0]['created'])
        second = self.make_run()
        self.assertEqual(official.register_official_results(self.conn, second, [item])[0]['change_state'],
                         'unchanged')
        third = self.make_run()
        changed = normalize_official_service(self.row | {'WLFARE_INFO_OUTL_CN': '지원 내용 변경'})
        self.assertEqual(official.register_official_results(self.conn, third, [changed])[0]['change_state'],
                         'updated')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_official_results').fetchone()[0], 3)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_official_source_state').fetchone()[0], 1)

    def test_unknown_region_and_bounded_age_are_review_not_match(self):
        bounded = replace(self.profile, revision=2, region='도봉구', min_age_months=780)
        profiles.save_revision(self.conn, bounded, expected_revision=1)
        run_id = profiles.prepare_run(self.conn, 'p', expected_revision=2, sites=[])
        official.register_official_results(self.conn, run_id,
                                           [normalize_official_service(self.row)])
        saved = self.conn.execute('SELECT result_json FROM news_official_results').fetchone()[0]
        import json
        result = json.loads(saved)
        self.assertEqual(result['target_status'], 'review')
        self.assertEqual(result['checks']['region'], 'review')
        self.assertEqual(result['checks']['age'], 'review')
        self.assertFalse(result['ai_called'])

    def test_separate_household_choice_survives_official_broad_query(self):
        import json
        selected = replace(self.profile, revision=2, characteristics=('다문화',))
        profiles.save_revision(self.conn, selected, expected_revision=1)
        self.assertEqual(selected.require_bokjiro_support()['characteristics'], ('다문화·탈북민',))
        item = normalize_official_service(self.row)
        item['household_situations'] = ['다문화·탈북민']
        run = profiles.prepare_run(self.conn, 'p', expected_revision=2, sites=[])
        official.register_official_results(self.conn, run, [item])
        result = json.loads(self.conn.execute('SELECT result_json FROM news_official_results').fetchone()[0])
        self.assertEqual(result['target_status'], 'review')
        self.assertEqual(result['checks']['characteristics'], 'review')
        self.assertIn('세부 가구상황', result['input_note'])
        self.assertFalse(result['ai_called'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_jobs').fetchone()[0], 0)

    def test_custom_only_household_cannot_become_unbounded_official_query(self):
        selected = replace(self.profile, life_cycles=(), interest_topics=(),
                           characteristics=('일반', '1인 가구'))
        with self.assertRaises(ValueError):
            selected.require_bokjiro_support()

    def test_results_are_visible_without_becoming_ai_jobs(self):
        run_id = self.make_run()
        official.register_official_results(self.conn, run_id,
                                           [normalize_official_service(self.row)])
        result = jobs.list_profile_results(self.conn, 'p', run_id=run_id)['items'][0]
        self.assertEqual(result['source_type'], 'bokjiro_service')
        self.assertEqual(result['change_state'], 'new')
        self.assertEqual(result['target_status'], 'match')
        self.assertEqual(result['details']['support_detail'], '주거비를 지원합니다.')
        self.assertIsNone(result['job_id'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_analysis_jobs').fetchone()[0], 0)

    def test_mismatch_tampering_and_duplicate_batch_roll_back(self):
        run_id = self.make_run()
        mismatch = normalize_official_service(self.row | {
            'RETURN_STR': 'BKJR_LFTM_CYC_CD:청년;INTRS_THEMA_CD:교육'})
        valid = normalize_official_service(self.row)
        invalid = dict(valid, link='https://example.invalid/tampered')
        for batch in ([mismatch], [invalid], [valid, valid]):
            with self.subTest(batch=batch), self.assertRaises(ValueError):
                official.register_official_results(self.conn, run_id, batch)
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news_official_results').fetchone()[0], 0)

    def test_site_only_or_legacy_run_cannot_use_official_boundary(self):
        site_profile = NewsProfile('site', 1, '사이트', '전국', site_ids=('1',))
        profiles.create_profile(self.conn, site_profile)
        run_id = profiles.prepare_run(self.conn, 'site', expected_revision=1,
            sites=[{'id': 1, 'name': '시험', 'url': 'https://example.invalid'}])
        with self.assertRaises(ValueError):
            official.register_official_results(self.conn, run_id,
                                               [normalize_official_service(self.row)])


if __name__ == '__main__':
    unittest.main()
