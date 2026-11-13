"""Offline age-field completeness, not a live quality or eligibility claim."""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from google.genai import types
import ai_classifier
from services import policy_source_plan as plan
from services.policy_field_requirements import policy_field_requirements, missing_policy_fields
from services.policy_source_units import source_units


SOURCE = '지원대상\n만 2세 미만 영아\n지원내용\n기저귀 월 9만원 지원\n신청방법\n주민센터 방문 신청'
OPTIONS = {'category': ['복지'], 'target_characteristics': ['일반'],
           'life_cycle': ['영아'], 'residence': ['전국']}


def response(*, age_ids=None, start=None, end=23):
    return dict(_source_units=dict(support_detail=[u['id'] for u in source_units(SOURCE)],
        eligibility_detail=[], age_detail=[] if age_ids is None else age_ids, target_income=[]),
        category='', target_characteristics=[], residence=[], life_cycle=[],
        start_age_months=start, end_age_months=end, contact='')


class RecipientAgeField(unittest.TestCase):
    def test_explicit_recipient_years_months_and_bounded_ranges_require_whole_units(self):
        for statement in ('지원대상\n만 2세 미만 영아', '지원대상\n만 65세 이상 노인',
                '지원대상\n생후 6~23개월 영아', '지원대상\n만 18~24세 청년',
                '나이\n만 65세 이상', '신청자격\n만 65세 이상 노인'):
            for newline in ('\n', '\r\n'):
                source = statement.replace('\n', newline) + newline + '지원내용' + newline + '교통비 지원'
                with self.subTest(statement=statement, newline=repr(newline)):
                    requirements = policy_field_requirements(source)
                    ages = requirements['age_detail']
                    self.assertTrue(ages)
                    for item in ages:
                        self.assertIn(item['quote'], source)
                    ids = sorted({item['unit_id'] for item in ages})
                    raw = dict(_source_units=dict(support_detail=[u['id'] for u in source_units(source)],
                        eligibility_detail=[], age_detail=[], target_income=[]), contact='')
                    candidate = plan.hydrate_policy_source_ranges(raw, source)
                    self.assertIn('age_detail', missing_policy_fields(candidate, requirements))
                    raw['_source_units']['age_detail'] = ids
                    candidate = plan.hydrate_policy_source_ranges(raw, source)
                    original = copy.deepcopy(candidate)
                    self.assertNotIn('age_detail', missing_policy_fields(candidate, requirements))
                    self.assertEqual(candidate, original)

    def test_periods_other_roles_exclusions_documents_filters_and_unknown_ages_are_not_required(self):
        cases = (
            '', '지원대상\n영아', '지원대상\n아동', '지원기간\n24개월 지원',
            '지원대상\n임신기간 4개월 이상 태아를 유산한 여성',
            '지원대상\n지역 거주기간 6개월 이상인 주민',
            '지원대상\n사용기간 24개월 이하', '지급기간\n24개월 이하',
            '지원대상\n신청자는 만 18세 이상', '보호자 연령\n만 18세 이상',
            '신청자 연령\n만 18세 이상', '대리인 연령\n만 18세 이상',
            '지원대상\n만 2세 미만 자녀와 만 18세 이상 보호자',
            '제공인력\n연령\n만 18세 이상', '신청서류\n나이\n만 18세 이상',
            '법적근거\n연령\n만 18세 이상', '검색필터\n나이\n만 2세 미만',
            '지원제외대상\n만 2세 미만 영아', '지원대상\n만 2세 미만 영아 제외',
            '지원대상\n만 2세 미만 영아\n지원 대상 아님',
            '지원대상\n만 2세 미만 영아 또는 만 8세 이상 아동',
            '지원대상\n만 2세 미만 영아 또는 연령 무관 장애아',
            '지원대상\n만 2세 미만 영아\n장애아는 나이 제한 없음',
            '우선 지원대상\n만 2세 미만 영아',
            '지원대상\n만 2세 미만 영아\n연령 기준 예외: 장애아',
            '지원대상\n2025년 이후 출생아\n만 2세 미만 영아',
            '지원대상\n신청 월에 만 2세가 되는 영아',
        )
        for source in cases:
            with self.subTest(source=source):
                self.assertEqual(policy_field_requirements(source)['age_detail'], [])

    def test_recipient_age_does_not_require_nearby_guardian_or_payment_duration_units(self):
        source = ('지원대상\n만 2세 미만 영아\n보호자 연령: 만 18세 이상\n'
                  '지원내용\n기저귀 지원\n지원기간\n최대 24개월')
        ages = policy_field_requirements(source)['age_detail']
        self.assertTrue(ages)
        self.assertTrue(all(item['quote'] == '만 2세 미만 영아' for item in ages))

    def test_nested_role_target_headings_cannot_turn_guardian_or_applicant_age_into_recipient_age(self):
        for role in ('보호자', '신청자', '신청인', '부모'):
            for heading in ('대상', '지원대상', '신청자격'):
                source = f'지원대상\n아동\n{role}\n{heading}\n만 65세 이상\n지원내용\n교육비 지원'
                with self.subTest(role=role, heading=heading):
                    self.assertEqual(policy_field_requirements(source)['age_detail'], [])

    def test_only_independent_content_boundary_can_restart_recipient_age_after_role_table(self):
        for role in ('보호자', '신청자', '부모'):
            for boundary in ('지원내용', '사업개요'):
                source = (f'{role}\n지원대상\n만 18세 이상\n{boundary}\n교육비 지원\n'
                          '지원대상\n만 65세 이상 노인')
                with self.subTest(role=role, boundary=boundary):
                    ages = policy_field_requirements(source)['age_detail']
                    self.assertTrue(ages)
                    self.assertTrue(all(item['quote'] == '만 65세 이상 노인' for item in ages))
        # A wrapped guardian age does not erase the earlier clearly stated
        # infant condition, and it must never become the requested age quote.
        source = '지원대상\n만 2세 미만 영아\n보호자 연령\n만 18세 이상\n지원내용\n교육비 지원'
        ages = policy_field_requirements(source)['age_detail']
        self.assertTrue(ages)
        self.assertTrue(all(item['quote'] == '만 2세 미만 영아' for item in ages))

    def test_request_and_sdk_schema_stay_bounded_without_truncation_or_hidden_age_repair(self):
        prompt, schema = plan.build_policy_extraction_request(SOURCE, '정책', 'https://example.invalid', '', OPTIONS)
        self.assertIn('Every age_detail unit_id', prompt)
        self.assertIn('Correct numeric age bounds', prompt)
        self.assertEqual(schema['properties']['start_age_months'], {'type': 'null'})
        self.assertEqual(schema['properties']['end_age_months'], {'type': 'integer', 'enum': [23]})
        config = types.GenerateContentConfig(response_mime_type='application/json', response_json_schema=schema)
        self.assertEqual(json.loads(config.model_dump_json())['response_json_schema'], schema)
        total = len(prompt) + len(json.dumps(schema, ensure_ascii=False, separators=(',', ':')))
        self.assertLessEqual(total, plan.MAX_POLICY_PROMPT_CHARS)
        with patch.object(plan, 'MAX_POLICY_PROMPT_CHARS', total):
            self.assertEqual(plan.build_policy_extraction_request(SOURCE, '정책',
                'https://example.invalid', '', OPTIONS), (prompt, schema))
        with patch.object(plan, 'MAX_POLICY_PROMPT_CHARS', total - 1):
            with self.assertRaises(plan.PolicySourcePlanError) as caught:
                plan.build_policy_extraction_request(SOURCE, '정책', 'https://example.invalid', '', OPTIONS)
        self.assertEqual(caught.exception.code, 'source_structure_too_large')

    def test_frozen_accepted_goesan_response_is_detected_not_rewritten_or_relabelled(self):
        root = Path(__file__).resolve().parents[2] / 'artifacts'
        ledger_bytes = (root / 'ai-retest-20260914/fresh-live-01/ledger.jsonl').read_bytes()
        source_bytes = (root / 'ai-holdout-prep-20260913/sources/policy-goesan-diapers.txt').read_bytes()
        self.assertEqual(hashlib.sha256(ledger_bytes).hexdigest(),
            'ea955497e7521b89a092350b91b3094a477a3e336f34a88491af9e5a5e68809b')
        self.assertEqual(hashlib.sha256(source_bytes).hexdigest(),
            '03473b6a889c4185d9f7d92f49514eb4e36006203a6c8530884212cfe47ebb48')
        rows = [json.loads(line) for line in ledger_bytes.decode('utf-8').splitlines()]
        original_event = next(row for row in rows if row.get('event') == 'case_result'
            and row.get('case_id') == 'policy-goesan-diapers')
        self.assertEqual(original_event['outcome'], 'accepted')
        raw = json.loads(next(row for row in rows if row.get('event') == 'response'
            and row.get('case_id') == 'policy-goesan-diapers')['raw']['text'])
        before = copy.deepcopy(raw)
        self.assertEqual(raw['_source_units']['age_detail'], [])
        self.assertEqual((raw['start_age_months'], raw['end_age_months']), (None, 23))
        source = source_bytes.decode('utf-8')
        candidate = plan.hydrate_policy_source_ranges(raw, source)
        required = policy_field_requirements(source)
        self.assertEqual({item['unit_id'] for item in required['age_detail']}, {1})
        self.assertEqual(missing_policy_fields(candidate, required), ['age_detail'])
        self.assertEqual(candidate['age_detail'], '')
        self.assertEqual(raw, before)
        self.assertEqual(original_event['outcome'], 'accepted')


class AgeFieldNewCallWorkflow(unittest.IsolatedAsyncioTestCase):
    async def test_correct_scalars_without_dedicated_age_source_are_held_once(self):
        raw, review = response(), {}
        before = copy.deepcopy(raw)
        with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value=raw)) as call:
            result = await ai_classifier.extract_policy_fields_async(object(), '정책',
                'https://example.invalid', content=SOURCE, review=review)
        call.assert_awaited_once()
        self.assertIsNone(result)
        self.assertEqual(review['code'], 'source_field_incomplete')
        self.assertEqual(review['fields'], ['age_detail'])
        self.assertIn('나이 상세 조건', review['note'])
        self.assertEqual(raw, before)

    async def test_complete_synthetic_age_unit_passes_but_wrong_scalar_still_holds(self):
        for start, end in ((None, 23), (0, 24), (None, 24), (None, None)):
            raw, review = response(age_ids=[1], start=start, end=end), {}
            before = copy.deepcopy(raw)
            with self.subTest(start=start, end=end), patch.object(ai_classifier, '_call_gemini_json',
                    new=AsyncMock(return_value=raw)) as call:
                result = await ai_classifier.extract_policy_fields_async(object(), '정책',
                    'https://example.invalid', content=SOURCE, review=review)
            call.assert_awaited_once()
            self.assertEqual(raw, before)
            if (start, end) == (None, 23):
                self.assertIsNotNone(result)
                self.assertIn('만 2세 미만 영아', result['age_detail'])
            else:
                self.assertIsNone(result)
                self.assertIn(review['code'], ('structured_evidence_missing', 'source_schema_invalid'))

    async def test_legacy_response_is_not_silently_reclassified_by_new_unit_requirement(self):
        legacy = plan.hydrate_policy_source_ranges(response(), SOURCE)
        legacy.pop('_source_unit_selection')
        before, review = copy.deepcopy(legacy), {}
        with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value=legacy)) as call:
            result = await ai_classifier.extract_policy_fields_async(object(), '정책',
                'https://example.invalid', content=SOURCE, review=review)
        call.assert_awaited_once()
        self.assertIsNotNone(result)
        self.assertEqual(result['age_detail'], '')
        self.assertEqual(legacy, before)
        self.assertEqual(review, {})

    async def test_request_budget_overflow_holds_before_any_model_request(self):
        review = {}
        with patch.object(plan, 'MAX_COUNTED_POLICY_PROMPT_CHARS', 1), patch.object(ai_classifier,
                '_call_gemini_json', new=AsyncMock()) as call:
            result = await ai_classifier.extract_policy_fields_async(object(), '정책',
                'https://example.invalid', content=SOURCE, review=review)
        self.assertIsNone(result)
        self.assertEqual(review['code'], 'source_too_long')
        call.assert_not_awaited()
