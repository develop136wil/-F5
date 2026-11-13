from dataclasses import replace
import unittest
from services.news_profile import NewsProfile
from services.news_target_rules import evaluate_target


class TestTargetRules(unittest.TestCase):
    def test_specific_household_does_not_match_sibling_or_combined_evidence(self):
        base = NewsProfile('p', 1, '가구', '전국', characteristics=('다문화',))
        for value in ('탈북민', '다문화·탈북민'):
            self.assertEqual(evaluate_target(base, {'target_characteristics':[value]}).status, 'review')
        self.assertEqual(evaluate_target(base, {'target_characteristics':['다문화']}).status, 'match')
        combined = replace(base, characteristics=('다문화·탈북민',))
        self.assertEqual(evaluate_target(combined, {'target_characteristics':['탈북민']}).status, 'match')

    def setUp(self):
        self.profile = NewsProfile('p', 1, '노년', '전국', min_age_months=780, life_cycles=['노년'])

    def test_senior_overlap_and_exclusion(self):
        self.assertEqual(evaluate_target(self.profile, {'start_age_months': 780, 'life_cycle': ['노년']}).status, 'match')
        self.assertEqual(evaluate_target(self.profile, {'end_age_months': 240}).status, 'excluded')

    def test_unknown_age_is_review_not_assumed_zero(self):
        self.assertEqual(evaluate_target(self.profile, {'life_cycle': ['노년']}).status, 'review')
        self.assertEqual(evaluate_target(self.profile, {'end_age_months': 900, 'life_cycle': ['노년']}).status, 'match')

    def test_or_within_field_and_between_fields(self):
        profile = replace(self.profile, min_age_months=None, life_cycles=('청소년', '노년'), characteristics=('한부모', '저소득'))
        self.assertEqual(evaluate_target(profile, {'life_cycle': ['청소년'], 'target_characteristics': ['한부모']}).status, 'match')
        self.assertEqual(evaluate_target(profile, {'life_cycle': ['청소년']}).status, 'review')
        self.assertEqual(evaluate_target(profile, {'life_cycle': ['영아'], 'target_characteristics': ['한부모']}).status, 'excluded')

    def test_custom_tags_not_silently_rejected(self):
        profile = replace(self.profile, min_age_months=None, life_cycles=('사용자분류',))
        self.assertEqual(evaluate_target(profile, {'life_cycle': ['노년']}).status, 'review')
        self.assertEqual(evaluate_target(profile, {'life_cycle': ['사용자분류']}).status, 'match')

    def test_bokjiro_labels_and_common_aliases_are_equivalent(self):
        infant = replace(self.profile, min_age_months=None, life_cycles=('영유아',))
        self.assertEqual(evaluate_target(infant, {'life_cycle': ['영아']}).status, 'match')
        single_parent = replace(self.profile, min_age_months=None, life_cycles=(),
                                characteristics=('한부모·조손',))
        self.assertEqual(evaluate_target(single_parent,
            {'target_characteristics': ['한부모가족']}).status, 'match')
        disabled = replace(single_parent, characteristics=('장애인',))
        self.assertEqual(evaluate_target(disabled,
            {'target_characteristics': ['등록장애인']}).status, 'match')

    def test_interest_topics_are_a_real_final_condition(self):
        profile = replace(self.profile, min_age_months=None, life_cycles=(),
                          interest_topics=('주거', '생활지원'))
        self.assertEqual(evaluate_target(profile, {'interest_topics': ['주거']}).status, 'match')
        self.assertEqual(evaluate_target(profile, {'interest_topics': ['교육']}).status, 'excluded')
        self.assertEqual(evaluate_target(profile, {}).status, 'review')

    def test_custom_interest_topic_is_conservative(self):
        profile = replace(self.profile, min_age_months=None, life_cycles=(),
                          interest_topics=('기관 자체 분야',))
        self.assertEqual(evaluate_target(profile,
            {'interest_topics': ['기관 자체 분야']}).status, 'match')
        self.assertEqual(evaluate_target(profile,
            {'interest_topics': ['교육']}).status, 'review')

    def test_region_exact_or_nationwide_otherwise_review(self):
        profile = replace(self.profile, region='도봉구', min_age_months=None, life_cycles=())
        for residence in ('도봉구', '전국'):
            self.assertEqual(evaluate_target(profile, {'residence': residence}).status, 'match')
        for residence in ('서울', '서울 도봉구', '부산', ''):
            self.assertEqual(evaluate_target(profile, {'residence': residence}).status, 'review')

    def test_invalid_age_and_disabled_filter(self):
        for item in ({'start_age_months': True}, {'start_age_months': -1},
                     {'start_age_months': 900, 'end_age_months': 800}):
            self.assertEqual(evaluate_target(self.profile, item).status, 'review')
        self.assertEqual(evaluate_target(replace(self.profile, relevance_enabled=False), {}).status, 'review')

    def test_legacy_rejected_and_input_unchanged(self):
        item = {'life_cycle': ['노년']}
        evaluate_target(self.profile, item)
        self.assertEqual(item, {'life_cycle': ['노년']})
        with self.assertRaises(ValueError):
            evaluate_target(replace(self.profile, general_mode='legacy_infant_v1'), item)

    def test_requested_all_life_cycles_does_not_require_matching_tag(self):
        profile = replace(self.profile, min_age_months=None, life_cycles=('전생애',))
        for item in ({}, {'life_cycle': ['청소년']}, {'life_cycle': ['노년']}):
            self.assertEqual(evaluate_target(profile, item).status, 'match')

    def test_general_is_common_not_a_specific_household_match(self):
        base = replace(self.profile, min_age_months=None, life_cycles=(),
                       characteristics=('일반',))
        for item in ({}, {'target_characteristics':['일반']},
                     {'target_characteristics':['장애인']}):
            self.assertEqual(evaluate_target(base, item).status, 'match')
        specific = replace(base, characteristics=('일반', '장애인'))
        for item in ({}, {'target_characteristics':['일반']},
                     {'target_characteristics':['한부모']}):
            self.assertEqual(evaluate_target(specific, item).status, 'review')
        self.assertEqual(evaluate_target(specific,
            {'target_characteristics':['일반', '등록장애인']}).status, 'match')
        self.assertEqual(specific.characteristics, ('일반', '장애인'))

    def test_general_never_bypasses_age_region_or_lifecycle(self):
        base = replace(self.profile, characteristics=('일반',))
        self.assertEqual(evaluate_target(base,
            {'life_cycle':['노년'], 'end_age_months':240}).status, 'excluded')
        self.assertEqual(evaluate_target(base,
            {'life_cycle':['노년']}).status, 'review')
        self.assertEqual(evaluate_target(replace(base, min_age_months=None),
            {'life_cycle':['아동']}).status, 'excluded')
        self.assertEqual(evaluate_target(replace(base, region='도봉구',
            min_age_months=None), {'life_cycle':['노년']}).status, 'review')
