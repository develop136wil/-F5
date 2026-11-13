"""Request-only target filtering reuses, never weakens, the evidence gate."""
import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

import ai_classifier
from services.policy_evidence import structured_policy_issues
from services.policy_source_plan import (
    PolicySourcePlanError, _source_screened_options, build_policy_extraction_request,
)


class TargetOptions(unittest.TestCase):
    def shortlist(self, source, labels):
        return _source_screened_options(source, {'target_characteristics': labels})['target_characteristics']

    def test_income_numbers_do_not_establish_low_income_tag(self):
        for source in ('지원 대상: 중위소득 120% 이하 청년',
                       '지원 대상: 중위소득 50% 이하 가구'):
            with self.subTest(source=source):
                self.assertEqual(self.shortlist(source, ['일반', '저소득']), ['일반'])

    def test_explicit_positive_target_and_aliases_preserve_exact_labels(self):
        for source, label in (
            ('지원 대상: 저소득 가구', '저소득'),
            ('지원 대상: 기초생활수급자 가구', '저소득'),
            ('지원 대상: 차상위 가구', '저소득'),
            ('지원 대상: 국가유공자', '국가유공자'),
            ('지원 대상: 북한이탈주민 가구', '탈북민'),
            ('지원 대상: 한부모 가구', '한부모·조손'),
            ('지원 대상: 장애인', '장애/발달지연'),
            ('지원 대상: 자립준비청년', '자립준비청년'),
        ):
            with self.subTest(label=label):
                self.assertEqual(self.shortlist(source, [label, '근거없는맞춤대상']), [label])

    def test_excluded_proxy_provider_or_negated_mentions_not_offered(self):
        for source, label in (
            ('지원 대상에서 저소득 가구 제외', '저소득'),
            ('지원 제외 대상\n저소득 가구', '저소득'),
            ('지원 대상: 비저소득 가구', '저소득'),
            ('저소득지원센터에서 청년에게 지원합니다', '저소득'),
            ('문의처: 장애인복지관', '장애인'),
            ('지원 대상: 장애인 보호자의 아동', '장애인'),
            ('신청자: 장애인 부모\n지원 대상: 아동', '장애인'),
        ):
            with self.subTest(source=source):
                self.assertEqual(self.shortlist(source, [label]), [])

    def test_taxonomy_is_unchanged_and_no_general_label_is_invented(self):
        options = {'category': ['교육'], 'target_characteristics': ['저소득', '국가유공자'],
                   'residence': ['부산광역시'], 'life_cycle': ['청년']}
        before = copy.deepcopy(options)
        result = _source_screened_options('지원 대상: 부산시 거주 청년', options)
        self.assertEqual(result['target_characteristics'], [])
        self.assertEqual(result['residence'], ['부산광역시'])
        self.assertEqual(result['life_cycle'], ['청년'])
        self.assertEqual(options, before)

    def test_empty_candidates_are_enforced_by_request_schema(self):
        source = '지원 대상: 중위소득 120% 이하 청년'
        prompt, schema = build_policy_extraction_request(source, '저소득 정책',
            'https://example.invalid', '저소득지원센터', {'target_characteristics': ['저소득']})
        offered = json.loads(prompt.split('[Allowed Notion options]\n')[1].split('\n[Required')[0])
        self.assertEqual(offered['target_characteristics'], [])
        self.assertEqual(schema['properties']['target_characteristics'],
            {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 0})
        self.assertIn(source, prompt)

    def test_shortlist_and_final_guard_agree_on_custom_labels(self):
        source = '지원 대상: 국가유공자 및 저소득 가구'
        labels = ['일반', '저소득', '국가유공자', '장애인', '미등록맞춤대상']
        expected = [label for label in labels
            if not structured_policy_issues({'target_characteristics': [label]}, source)]
        self.assertEqual(self.shortlist(source, labels), expected)

    def test_target_count_bound_and_invalid_entries(self):
        with self.assertRaises(PolicySourcePlanError):
            self.shortlist('지원 대상: 저소득 가구', ['저소득'] * 101)
        self.assertEqual(self.shortlist('지원 대상: 저소득 가구', [None, '', 1, {}, 'x' * 81, '저소득']), ['저소득'])


class TargetResponseGuard(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_response_not_repaired_or_retried(self):
        source = '지원 대상: 중위소득 120% 이하 청년'
        raw = {'target_characteristics': ['저소득']}
        before = copy.deepcopy(raw)
        review = {}
        with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value=raw)) as call:
            result = await ai_classifier.extract_policy_fields_async(object(), '정책',
                'https://example.invalid', content=source, review=review)
        call.assert_awaited_once()
        self.assertIsNone(result)
        self.assertEqual(raw, before)
        schema = call.await_args.kwargs['response_json_schema']
        self.assertNotIn('저소득', schema['properties']['target_characteristics']['items'].get('enum', []))
