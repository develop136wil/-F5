"""Offline profile contract and historical collection decision baselines."""
from dataclasses import FrozenInstanceError, replace
import unittest

from services.news_profile import NewsProfile, capture_legacy_profile


class TestNewsProfile(unittest.TestCase):
    def legacy(self, **overrides):
        args = dict(region='도봉', max_age_months=108, keywords=['영유아', '돌봄'],
                    excluded_keywords=['채용'], site_ids=['site-1'],
                    ai_prompt='기존 지침\n그대로', semantic_text='기존 의미 기준 그대로',
                    relevance_enabled=True)
        return capture_legacy_profile(**(args | overrides))

    def test_legacy_preserves_effective_values_without_enabling_bokjiro(self):
        profile = self.legacy()
        self.assertEqual(profile.keywords, ('영유아', '돌봄'))
        self.assertEqual(profile.ai_prompt, '기존 지침\n그대로')
        self.assertEqual(profile.semantic_text, '기존 의미 기준 그대로')
        self.assertEqual(profile.sources, ('sites',))
        self.assertEqual(profile.general_mode, 'legacy_infant_v1')
        self.assertEqual(profile.bokjiro_mode, 'legacy_infant_v1')

    def test_input_and_returned_snapshot_cannot_change_running_profile(self):
        keywords = ['영유아']
        profile = self.legacy(keywords=keywords)
        digest = profile.fingerprint
        keywords.append('노년')
        snapshot = profile.snapshot()
        snapshot['keywords'].append('청소년')
        self.assertEqual(profile.keywords, ('영유아',))
        self.assertEqual(profile.fingerprint, digest)
        with self.assertRaises(FrozenInstanceError):
            profile.region = '서울'

    def test_fingerprint_is_stable_and_changes_with_analysis_settings(self):
        profile = self.legacy()
        self.assertEqual(profile.fingerprint, self.legacy().fingerprint)
        for changes in ({'ai_prompt': '다른 지침'}, {'revision': 2},
                        {'semantic_text': '다른 기준'}, {'keywords': ('다른 단어',)},
                        {'interest_topics': ('주거',)}):
            with self.subTest(changes=changes):
                self.assertNotEqual(profile.fingerprint, replace(profile, **changes).fingerprint)

    def test_invalid_values_are_rejected(self):
        profile = self.legacy()
        for changes in ({'max_age_months': True}, {'min_age_months': -1},
                        {'min_age_months': 120, 'max_age_months': 108},
                        {'max_age_months': 1801}, {'keywords': '노년'},
                        {'site_ids': [1]}, {'sources': ()}, {'sources': ('unknown',)},
                        {'sources': ('sites', 'sites')}, {'revision': False},
                        {'region': ' '}, {'relevance_enabled': 'false'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(profile, **changes)

    def test_custom_unbounded_age_and_senior_range(self):
        custom = NewsProfile('custom', 1, '한부모', '서울', characteristics=['한부모'])
        self.assertIsNone(custom.min_age_months)
        self.assertIsNone(custom.max_age_months)
        senior = replace(custom, min_age_months=780, life_cycles=['노년'])
        self.assertEqual(senior.min_age_months, 780)
        self.assertEqual(senior.life_cycles, ('노년',))

    def test_custom_bokjiro_conditions_cannot_silently_use_legacy(self):
        custom = NewsProfile('senior', 1, '노년', '서울', sources=['bokjiro'])
        with self.assertRaisesRegex(ValueError, '아직 지원'):
            custom.require_bokjiro_support()
        with self.assertRaises(ValueError):
            replace(custom, bokjiro_mode='legacy_infant_v1')
        self.legacy(include_bokjiro=True).require_bokjiro_support()

    def test_custom_bokjiro_official_mode_keeps_only_official_filters(self):
        profile = NewsProfile(
            'senior', 1, '노년', '전국', sources=['sites', 'bokjiro'],
            life_cycles=['노인', '기관 자체 생애주기'],
            characteristics=['한부모', '독거'],
            interest_topics=['주거', '기관 자체 분야'],
            bokjiro_mode='official_tags_v1',
        )
        self.assertEqual(profile.require_bokjiro_support(), {
            'mode': 'official_tags_v1',
            'life_cycles': ('노년',),
            'characteristics': ('한부모·조손',),
            'interest_topics': ('주거',),
        })

    def test_custom_bokjiro_official_mode_requires_source_and_official_filter(self):
        with self.assertRaisesRegex(ValueError, 'requires the Bokjiro source'):
            NewsProfile('p', 1, '시험', '전국', bokjiro_mode='official_tags_v1')
        profile = NewsProfile('p', 1, '시험', '전국', sources=['bokjiro'],
                              characteristics=['기관 자체 분류'],
                              bokjiro_mode='official_tags_v1')
        with self.assertRaisesRegex(ValueError, '하나 이상'):
            profile.require_bokjiro_support()

    def test_legacy_out_of_range_is_not_silently_clamped(self):
        for maximum in (241, -1, '108', True):
            with self.subTest(maximum=maximum), self.assertRaises(ValueError):
                self.legacy(max_age_months=maximum)


class TestLegacyCollectionBaseline(unittest.TestCase):
    """Characterization, not endorsements of every historical decision.

    Deliberate changes to these rules require separate product/quality review.
    """
    def test_bokjiro_infant_decision_matrix(self):
        from services.bokjiro_collector import _is_infant_focused
        cases = [
            ('영유아', '보육료 지원', True),
            ('영유아,아동,임신·출산', '가족 지원', True),
            ('노년', '노인 지원', False),
            ('청소년', '청소년 지원', False),
            ('영유아,청소년,청년', '가족 지원', True),
            ('영유아,청소년,청년,노년', '전 연령 지원', False),
            ('영유아', '어르신 지원', False),
            ('영유아', '대학생 학자금 지원', False),
            ('', '영유아 보육료 지원', False),
        ]
        for cycles, title, expected in cases:
            with self.subTest(cycles=cycles, title=title):
                self.assertEqual(_is_infant_focused(cycles, title), expected)

    def test_bokjiro_region_matrix(self):
        from services.bokjiro_collector import _is_target_region
        for region, expected in [('', True), ('전국', True), ('서울 도봉구', True),
                                 ('서울 강북구', True), ('서울 노원구', True),
                                 ('부산 해운대구', False), ('경기 수원시', False)]:
            with self.subTest(region=region):
                self.assertEqual(_is_target_region(region), expected)

    def test_general_age_matrix(self):
        from services.relevance_filter import age_ok
        cases = [({}, True), ({'start_age_months': 108}, True),
                 ({'start_age_months': 109}, False),
                 ({'target': '영유아'}, True),
                 ({'life_cycle': ['노년']}, False),
                 ({'life_cycle': ['청소년']}, False),
                 ({'life_cycle': ['전생애']}, True),
                 ({'end_age_months': 900}, True)]
        for item, expected in cases:
            with self.subTest(item=item):
                self.assertEqual(age_ok(item, 108), expected)

    def test_official_bokjiro_sample_requires_response_tag_match(self):
        from services.bokjiro_collector import _official_tags, _matches_official_tags, _build_body
        import json
        value = ('BKJR_LFTM_CYC_CD:노년,중장년;'
                 'BKJR_GAD_SIT_CD:한부모·조손,저소득;'
                 'INTRS_THEMA_CD:생활지원,보호·돌봄')
        tags = _official_tags(value)
        self.assertEqual(tags['life_cycles'], ['노년', '중장년'])
        self.assertEqual(tags['household_situations'], ['저소득', '한부모·조손'])
        self.assertTrue(_matches_official_tags(value, life_cycles=['노년']))
        self.assertTrue(_matches_official_tags(value, characteristics=['한부모']))
        self.assertTrue(_matches_official_tags(value, interest_topics=['생활지원']))
        self.assertTrue(_matches_official_tags(value, life_cycles=['노년'],
                                               characteristics=['저소득'],
                                               interest_topics=['보호·돌봄']))
        self.assertFalse(_matches_official_tags(value, life_cycles=['노년'],
                                                interest_topics=['주거']))
        self.assertFalse(_matches_official_tags(value, life_cycles=['영유아']))
        body = json.loads(_build_body(2, life_cycles=['노년'],
                                     characteristics=['한부모·조손'],
                                     interest_topics=['생활지원']))
        self.assertEqual(body['dmSearchParam']['period'], '노년')
        self.assertEqual(body['dmSearchParam']['daesang'], '한부모·조손')
        self.assertEqual(body['dmSearchParam']['subject'], '생활지원')

    def test_official_bokjiro_sample_rejects_unknown_or_empty_taxonomy(self):
        from services.bokjiro_collector import _matches_official_tags
        for values in ({}, {'life_cycles': ['사용자분류']}, {'characteristics': ['독거']},
                       {'interest_topics': ['기관자체분류']}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                _matches_official_tags('', **values)

    def test_official_service_normalization_preserves_evidence_without_claiming_eligibility(self):
        from services.bokjiro_collector import normalize_official_service
        row = {
            'WLFARE_INFO_ID': 'svc_123-ABC', 'WLFARE_INFO_NM': ' 노년 주거 지원 ',
            'WLFARE_INFO_OUTL_CN': ' 지원 개요 ', 'BIZ_CHR_INST_NM': '서울특별시 도봉구',
            'ADDR': '서울특별시 도봉구',
            'RETURN_STR': 'BKJR_LFTM_CYC_CD:노년;INTRS_THEMA_CD:주거',
        }
        item = normalize_official_service(row)
        self.assertEqual(item['source_key'], 'bokjiro:svc_123-ABC')
        self.assertEqual(item['provider_region'], '서울 도봉구')
        self.assertEqual(item['eligibility_region'], '')
        self.assertFalse(item['age_range_known'])
        self.assertEqual(item['life_cycles'], ['노년'])
        self.assertEqual(item['interest_topics'], ['주거'])
        self.assertTrue(item['link'].endswith('svc_123-ABC'))
        self.assertIsNone(normalize_official_service(row | {'WLFARE_INFO_ID': '../unsafe'}))
        self.assertIsNone(normalize_official_service(row | {'WLFARE_INFO_NM': ' '}))


if __name__ == '__main__':
    unittest.main()
