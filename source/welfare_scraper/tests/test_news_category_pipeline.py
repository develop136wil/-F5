"""Synthetic wiring checks, not measured AI accuracy or collection recall.

No provider is called. These fixtures deliberately supply correct model facts
to show that every offered target can reach analysis and the final rule gate.
"""
import json
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

from services import news_profile_store as profiles, news_analysis_store as jobs
from services.news_profile import NewsProfile
from services.news_profile_taxonomy import LIFE_CYCLES, HOUSEHOLD_SITUATIONS, INTEREST_TOPICS
from services.news_candidate_intake import register_candidates
from services.news_direct_analysis import queue_candidates
from services.news_analysis_worker import process_job
from services.news_target_rules import evaluate_target
from services import page_coverage
from services.news_analysis_safety import assess


def tearDownModule():
    import database
    database.close_all_connections()


class TestNewsCategoryPipeline(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        profiles.ensure_schema(self.conn)
        jobs.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    async def call_db(self, fn, *args, **kwargs):
        return fn(self.conn, *args, **kwargs)

    async def test_every_offered_target_reaches_intake_analysis_and_rules(self):
        # Mirrors conditionCategories.js: seven life stages and eight household
        # choices. Extra official labels are also supported in the advanced form.
        households = tuple(dict.fromkeys((*HOUSEHOLD_SITUATIONS,
            '1인 가구', '다문화', '탈북민', '보훈', '여성')))
        for group, tags in (('life_cycles', LIFE_CYCLES), ('characteristics', households)):
            for tag in tags:
                for explicit_heading in (False, True):
                    with self.subTest(group=group, tag=tag, heading=explicit_heading):
                        profile_id = f'{group}-{tag}-{explicit_heading}'
                        profile = NewsProfile(profile_id, 1, tag, '전국', site_ids=('1',),
                                              **{group: (tag,)})
                        profiles.create_profile(self.conn, profile)
                        run = profiles.prepare_run(self.conn, profile_id, expected_revision=1,
                            sites=[dict(id=1, name='시험 기관', url='https://example.invalid')])
                        content = (f'지원 대상: {tag}' if explicit_heading else f'{tag} 대상입니다.')
                        content += '\n개인이 신청할 수 있습니다.'
                        candidate = dict(title='이용 안내', link='https://example.invalid/notice',
                                         content=content, source_page=page_coverage.make())
                        receipt = register_candidates(self.conn, run, '1', [candidate], model='fake-model')[0]
                        if explicit_heading:
                            self.assertEqual(receipt['route'], 'matched')
                        else:
                            self.assertEqual(receipt['route'], 'semantic_candidate')
                            self.assertEqual(queue_candidates(self.conn, run, 'fake-model', 50), 1)
                        job = self.conn.execute('SELECT job_id FROM news_run_candidates WHERE run_id=?',
                                                (run,)).fetchone()[0]
                        facts = dict(is_relevant=True, category='복지', target=tag,
                            residence=[], life_cycle=[tag] if group == 'life_cycles' else [],
                            target_characteristics=[tag] if group == 'characteristics' else [],
                            interest_topics=[], start_age_months=None, end_age_months=None,
                            evidence_snippet=content, applicant_type='individual',
                            applicant_evidence='개인이 신청할 수 있습니다.',
                            applicant_detail='개인', beneficiary_detail=tag,
                            residence_evidence=[], life_cycle_evidence=(
                                [dict(value=tag, quote=f'{tag}', basis='explicit')]
                                if group == 'life_cycles' else []),
                            _support_source_units=[1], eligibility_detail='', contact='', deadline='',
                            fact_evidence={key: '' for key in
                                ('eligibility_detail', 'contact', 'deadline', 'is_relevant')})
                        with patch('ai_classifier._call_gemini_json', new_callable=AsyncMock,
                                   return_value=facts) as ai:
                            self.assertEqual(await process_job(job, object(), call_db=self.call_db), 'complete')
                            criteria = ai.call_args.args[1].split('\nCRITERIA: ')[1].split('\n')[0]
                            self.assertEqual(json.loads(criteria)[group], [tag])
                        result = json.loads(jobs.get_job(self.conn, job)['result_json'])
                        self.assertTrue(result['accepted'])
                        self.assertEqual(result['target_status'], 'match')
                        self.assertEqual(result['analysis']['support_detail'], content)
                        # This synthetic full-page marker is part of the v15
                        # fixture, not a relaxation for real sources. Removing
                        # it still holds even these otherwise matching facts.
                        unknown_page = {key: value for key, value in candidate.items()
                                        if key != 'source_page'}
                        withheld = assess(profile, result['analysis'], content,
                            require_source_selection=True, whole_document_rules=True,
                            source_payload=unknown_page)
                        self.assertFalse(withheld['accepted'])
                        self.assertTrue(withheld['review_required'])
                        self.assertEqual(withheld['target_status'], 'review')
                        self.assertIn('원문 확보 범위', withheld['analysis']['grounding_note'])
                        # The initial/default condition uses the same persisted
                        # intake and analysis contract, not the old infant filter.
                        from dataclasses import replace
                        from services import news_default_condition as defaults
                        previous = defaults.read(self.conn)
                        revision = previous.revision + 1 if previous else 1
                        default = replace(profile, profile_id=defaults.PROFILE_ID, revision=revision)
                        defaults.save(self.conn, default, revision - 1)
                        default_run = profiles.prepare_run(self.conn, defaults.PROFILE_ID,
                            expected_revision=revision,
                            sites=[dict(id=1, name='시험 기관', url='https://example.invalid')])
                        default_receipt = register_candidates(self.conn, default_run, '1',
                            [candidate], model='fake-model')[0]
                        self.assertEqual(default_receipt['route'], receipt['route'])
                        if not explicit_heading:
                            self.assertEqual(queue_candidates(self.conn, default_run, 'fake-model', 50), 1)
                        default_job = self.conn.execute(
                            'SELECT job_id FROM news_run_candidates WHERE run_id=?', (default_run,)).fetchone()[0]
                        with patch('ai_classifier._call_gemini_json', new_callable=AsyncMock,
                                   return_value=facts) as ai:
                            self.assertEqual(await process_job(default_job, object(), call_db=self.call_db), 'complete')
                            self.assertEqual(ai.call_args.args[1].split('\nCRITERIA: ')[1].split('\n')[0], criteria)
                        default_result = json.loads(jobs.get_job(self.conn, default_job)['result_json'])
                        self.assertEqual(default_result['target_status'], result['target_status'])

    def test_every_official_tag_maps_to_provider_and_matching_response(self):
        from services.bokjiro_collector import _build_body, _matches_official_tags
        groups = (('life_cycles', LIFE_CYCLES, 'period', 'BKJR_LFTM_CYC_CD'),
                  ('characteristics', HOUSEHOLD_SITUATIONS, 'daesang', 'BKJR_GAD_SIT_CD'),
                  ('interest_topics', INTEREST_TOPICS, 'subject', 'INTRS_THEMA_CD'))
        for group, tags, request_key, response_key in groups:
            for tag in tags:
                with self.subTest(group=group, tag=tag):
                    profile = NewsProfile('official', 1, tag, '전국', sources=('bokjiro',),
                                          bokjiro_mode='official_tags_v1', **{group: (tag,)})
                    filters = profile.require_bokjiro_support()
                    filters.pop('mode')
                    body = json.loads(_build_body(1, **filters))['dmSearchParam']
                    self.assertEqual(body[request_key], tag)
                    self.assertTrue(_matches_official_tags(f'{response_key}:{tag}', **filters))
                    self.assertFalse(_matches_official_tags('', **filters))

    def test_general_is_unrestricted_but_not_a_bokjiro_query_tag(self):
        general = NewsProfile('common', 1, '일반', '전국', characteristics=('일반',))
        self.assertEqual(evaluate_target(general, {}).status, 'match')
        from dataclasses import replace
        with self.assertRaises(ValueError):
            replace(general, sources=('bokjiro',), bokjiro_mode='official_tags_v1').require_bokjiro_support()
        self.assertNotIn('기타', LIFE_CYCLES)
        self.assertNotIn('일반', HOUSEHOLD_SITUATIONS)

    def test_month_boundaries_are_inclusive_without_lifecycle_age_guessing(self):
        profile = NewsProfile('months', 1, '2~6개월', '전국', min_age_months=2, max_age_months=6)
        for age, expected in ((1, 'excluded'), (2, 'match'), (6, 'match'), (7, 'excluded')):
            with self.subTest(age=age):
                self.assertEqual(evaluate_target(profile, {'start_age_months': age,
                    'end_age_months': age}).status, expected)
        self.assertEqual(evaluate_target(profile, {}).status, 'review')
        # Correct numeric overlap does not manufacture an absent life-stage tag.
        from dataclasses import replace
        self.assertEqual(evaluate_target(replace(profile, life_cycles=('영유아',)),
            {'start_age_months': 2, 'end_age_months': 6}).status, 'review')
