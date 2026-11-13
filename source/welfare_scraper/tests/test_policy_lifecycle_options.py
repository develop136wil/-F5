"""Pre-request option screening; no model quality or inference claims."""
import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

import ai_classifier
from services.policy_source_plan import _source_screened_options, build_policy_extraction_prompt, PolicySourcePlanError
from services.policy_source_units import source_units

LABELS = ['임신/출산', '영아', '유아', '학령기', '청년', '노년', '전생애', '대학생']


class LifecycleOptions(unittest.TestCase):
    def screened(self, source):
        return _source_screened_options(source, {'life_cycle': LABELS})['life_cycle']

    def test_birth_registration_dates_and_duration_do_not_supply_stage(self):
        for source in ('지원 대상: 2023년 이후 출생신고된 아동.',
                       '지원내용: 아동 이용권. 사용기간: 출생일부터 3년.',
                       '지원 대상: 정상적으로 주민등록번호를 부여받은 아동.'):
            with self.subTest(source=source):
                self.assertEqual(self.screened(source), [])

    def test_explicit_recipients_and_custom_institution_labels_survive(self):
        for source, expected in (('지원 대상: 임산부.', ['임신/출산']),
                                 ('지원 대상: 영아.', ['영아']),
                                 ('지원 대상: 청년.', ['청년']),
                                 ('지원 대상: 대학생.', ['대학생']),
                                 ('지원 대상: 전연령 주민.', ['전생애'])):
            with self.subTest(source=source):
                self.assertEqual(self.screened(source), expected)

    def test_proxy_provider_and_excluded_people_do_not_supply_stage(self):
        for source in ('지원 대상: 아동. 신청자: 임산부 보호자.',
                       '임산부 지원센터 안내. 지원 대상: 일반 주민.',
                       '임산부는 지원 대상에서 제외됩니다. 일반 주민에게 지원.'):
            with self.subTest(source=source):
                self.assertEqual(self.screened(source), [])

    def test_exact_recipient_age_keeps_overlapping_stages(self):
        for lower, upper, expected in ((0, 35, ['영아']), (35, 36, ['영아', '유아']),
                                       (83, 84, ['유아', '학령기']), (0, 107, ['영아', '유아', '학령기'])):
            with self.subTest(lower=lower, upper=upper):
                self.assertEqual(self.screened(f'지원 대상: 생후 {lower}~{upper}개월 아동.'), expected)
        self.assertEqual(self.screened('지원 대상: 만 65세 이상 주민.'), ['노년'])

    def test_disjoint_or_proxy_ages_are_not_filled_in(self):
        for source in ('지원 대상: 0~24개월 또는 84~107개월 아동.',
                       '아동의 보호자는 만 65세 이상이어야 신청 가능.'):
            with self.subTest(source=source):
                self.assertEqual(self.screened(source), [])

    def test_input_options_are_never_mutated_or_extended(self):
        options = {'life_cycle': ['청년', '임신/출산'], 'residence': ['전국'], 'category': ['주거']}
        before = copy.deepcopy(options)
        result = _source_screened_options('지원 대상: 청년.', options)
        self.assertEqual(options, before)
        self.assertEqual(result, {**options, 'life_cycle': ['청년'], 'residence': []})
        self.assertEqual(_source_screened_options('지원 대상: 영아.', {'life_cycle': []}), {'life_cycle': []})

    def test_no_lifecycle_schema_is_added_when_absent(self):
        self.assertEqual(_source_screened_options('지원 대상: 청년.', {'category': ['주거']}), {'category': ['주거']})

    def test_unbounded_option_count_holds_instead_of_truncating(self):
        with self.assertRaises(PolicySourcePlanError):
            _source_screened_options('지원 대상: 청년.', {'life_cycle': ['청년'] * 101})

    def test_prompt_displays_empty_candidates_without_losing_source(self):
        source = '지원 대상: 출생신고된 아동.\r\n지원내용: 교육 이용권.\r\n'
        prompt = build_policy_extraction_prompt(source, '임신/출산', 'https://example.invalid', '영아센터', {'life_cycle': LABELS})
        options = json.loads(prompt.split('[Allowed Notion options]\n')[1].split('\n[Required')[0])
        units = json.loads(prompt.split('[Source units JSON: id and complete original text]\n')[1].split('\n[Allowed')[0])
        self.assertEqual(options['life_cycle'], [])
        self.assertEqual(''.join(u['text'] for u in units), source)
        self.assertIn('If that list is empty, return life_cycle: [].', prompt)
        self.assertIn('출생·출생신고 조건만으로 임신/출산·영아를 선택하지 마세요.', prompt)


class ResponseSafety(unittest.IsolatedAsyncioTestCase):
    async def test_model_ignoring_empty_candidates_is_still_rejected_not_repaired(self):
        source = '지원 대상: 출생신고된 아동.\n지원내용: 교육 이용권.'
        raw = {'_source_units': {'support_detail': [u['id'] for u in source_units(source)],
               'eligibility_detail': [], 'age_detail': [], 'target_income': []},
               'category': '', 'target_characteristics': [], 'residence': [],
               'life_cycle': ['임신/출산', '영아'], 'start_age_months': None,
               'end_age_months': None, 'contact': ''}
        original = copy.deepcopy(raw)
        review = {}
        with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value=raw)) as call:
            result = await ai_classifier.extract_policy_fields_async(object(), '정책', 'https://example.invalid',
                content=source, review=review)
        self.assertIsNone(result)
        self.assertEqual(review['code'], 'structured_evidence_missing')
        self.assertEqual(review['fields'], ['life_cycle'])
        self.assertEqual(raw, original)
        self.assertEqual(call.await_count, 1)
