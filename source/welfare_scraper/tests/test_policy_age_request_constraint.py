"""Source-owned request constraints, not model accuracy or response repair.

Run only in the isolated audit runner. SDK calls below are synthetic doubles.
Frozen actual responses are inspected unchanged and must remain on hold.
"""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

import ai_classifier
from services.policy_evidence import structured_policy_issues
from services.policy_source_plan import (
    _source_age_constraint, build_policy_extraction_request,
    hydrate_policy_source_ranges, policy_response_matches_schema,
)
from services.policy_source_units import source_units


SOURCE = '지원대상\n만 2세 미만의 영아\n지원내용\n기저귀 월 9만원 지원\n신청방법\n주민센터 방문 신청'


def response(source, start=None, end=23):
    return dict(_source_units=dict(support_detail=[unit['id'] for unit in source_units(source)],
        eligibility_detail=[], age_detail=[1], target_income=[]), category='',
        target_characteristics=[], residence=[], life_cycle=[],
        start_age_months=start, end_age_months=end, contact='')


class SourceAgeRequestConstraint(unittest.TestCase):
    def test_only_explicit_exclusive_upper_is_calculated_without_lower_invention(self):
        for years in (1, 2, 3, 18, 65, 150):
            with self.subTest(years=years):
                source = f'지원대상\n만 {years}세 미만의 주민\n지원내용\n교통비 지원'
                before = copy.deepcopy(source)
                value = _source_age_constraint(source)
                self.assertEqual(value, dict(basis='explicit_exclusive_years_upper_bound',
                    source_years=years, start_age_months=None, end_age_months=years * 12 - 1))
                self.assertEqual(source, before)

    def test_repeated_same_recipient_upper_preserves_other_non_age_durations(self):
        source = ('지원대상\n만 2세 미만의 영아를 둔 한부모가족 수급 가구\n'
                  '만 2세 미만의 영아를 둔 다자녀 또는 장애인 가구\n'
                  '부부 모두 6개월 이상 거주\n지원내용\n최대 24개월 지원\n'
                  '질환으로 인한 약물 복용 3개월 이상이며 의사 판단 필요')
        self.assertEqual(_source_age_constraint(source)['end_age_months'], 23)
        self.assertIsNone(_source_age_constraint(source)['start_age_months'])

    def test_ambiguous_other_roles_ranges_and_non_age_numbers_do_not_get_a_constraint(self):
        cases = (
            '', None, '영아 지원', '지원기간\n24개월 지원',
            '대상자\n임신기간 4개월 이상의 태아를 유산한 여성',
            '지원대상\n만 2세 이하 영아',
            '지원대상\n만 2세 미만의 부모가 자녀 대신 신청',
            '제공인력\n만 2세 미만\n지원내용\n영아 돌봄 지원',
            '지원 제외 대상\n만 2세 미만의 영아\n지원내용\n돌봄 지원',
            '지원대상\n만 2세 미만 영아는 제외\n지원내용\n돌봄 지원',
            '지원대상\n만 2세 미만 영아\n또는 만 5세 미만 아동',
            '지원대상\n만 2세 미만 영아\n만 1세 미만 영아',
            '지원대상\n만 0세 이상 만 2세 미만 영아',
            '지원대상\n만 0~2세 미만 영아',
            '지원대상\n만 2세 미만 영아\n제공인력\n만 18세 이상',
            '지원대상\n만 2세 미만 영아\n지원 제외 대상\n만 1세 미만 영아',
            '지원대상\n만 2세 미만 영아\n2025년 이후 출생자',
            '지원대상\n신청 월에 만 2세가 되는 아동 포함',
            '지원대상\n만 2세 미만 영아\n가구소득 중위소득 50% 이하\n가입기준 중위소득 100% 이하',
            '지원대상\n만 0세 미만 영아', '지원대상\n만 151세 미만 주민',
            '지원대상\n만 2세 미만 영아 또는 연령 무관 장애아\n지원내용\n교육 지원',
            '지원대상\n만 2세 미만 영아\n또는 나이 제한 없음 장애아\n지원내용\n교육 지원',
            '지원대상\n만 2세 미만 영아\n장애아의 나이\n제한 없음\n지원내용\n교육 지원',
            '지원대상\n만 2세 미만 영아 또는 모든 연령의 장애아\n지원내용\n교육 지원',
            '지원대상\n만 2세 미만 영아\n장애아는 연령 제한이 없습니다\n지원내용\n교육 지원',
            '지원대상\n만 2세 미만 영아\n장애아는 나이와 관계없이 지원\n지원내용\n교육 지원',
            '지원대상\n만 2세 미만 영아\n연령 기준의 예외: 장애아\n지원내용\n교육 지원',
            '지원대상\n만 2세 미만 영아\n장애아는 연령 기준 완화\n지원내용\n교육 지원',
            '지원대상\n만 2세 미만 영아 우선 지원\n지원내용\n교육 지원',
            '우선 지원 대상\n만 2세 미만 영아\n지원내용\n교육 지원',
            '지원대상\n만 2세 미만 영아\n※ 장애아는 예외\n지원내용\n교육 지원',
        )
        for source in cases:
            with self.subTest(source=source):
                self.assertIsNone(_source_age_constraint(source))

    def test_request_schema_restricts_age_without_mutating_source_or_response(self):
        source_before = copy.deepcopy(SOURCE)
        allowed = {'life_cycle': ['영아'], 'category': ['복지']}
        before = copy.deepcopy(allowed)
        prompt, schema = build_policy_extraction_request(SOURCE, '정책', 'https://example.invalid', '기관', allowed)
        self.assertEqual(schema['properties']['start_age_months'], {'type': 'null'})
        self.assertEqual(schema['properties']['end_age_months'], {'type': 'integer', 'enum': [23]})
        self.assertIn('SOURCE-VERIFIED REQUEST CONSTRAINT (not a model prediction)', prompt)
        self.assertIn('Return these exact age values or abstain with the ENTIRE response JSON null', prompt)
        example = json.loads(prompt.split('Return ONLY null or this JSON shape.')[1].split('\n', 3)[3])
        self.assertIsNone(example['start_age_months'])
        self.assertEqual(example['end_age_months'], 23)
        self.assertEqual(allowed, before)
        self.assertEqual(SOURCE, source_before)
        self.assertTrue(policy_response_matches_schema(response(SOURCE), schema))
        self.assertTrue(policy_response_matches_schema(None, schema))
        for start, end in ((0, 24), (0, 23), (None, 24), (None, None),
                           (True, 23), (None, True), ('0', 23), (None, '23'), (24, 23)):
            raw = response(SOURCE, start, end)
            original = copy.deepcopy(raw)
            with self.subTest(start=start, end=end):
                self.assertFalse(policy_response_matches_schema(raw, schema))
                self.assertEqual(raw, original)

    def test_nonmatching_source_keeps_existing_schema_and_evidence_guards(self):
        source = '지원대상\n만 2세 이하 영아\n지원내용\n기저귀 지원'
        _, schema = build_policy_extraction_request(source, '정책', 'https://example.invalid', '기관', {})
        self.assertEqual(schema['properties']['end_age_months'],
                         {'type': ['integer', 'null'], 'minimum': 0, 'maximum': 1800})
        self.assertIn('end_age_months', structured_policy_issues({'end_age_months': 24}, source))


class SourceAgeExtractionIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_synthetic_compliant_response_passes_once_without_posthoc_repair(self):
        raw = response(SOURCE)
        before = copy.deepcopy(raw)
        review = {}
        with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value=raw)) as call:
            result = await ai_classifier.extract_policy_fields_async(object(), '정책',
                'https://example.invalid', content=SOURCE, review=review)
        call.assert_awaited_once()
        self.assertIsNotNone(result)
        self.assertIsNone(result['start_age_months'])
        self.assertEqual(result['end_age_months'], 23)
        self.assertEqual(raw, before)
        self.assertEqual(review, {})

    async def test_old_or_invalid_model_values_remain_a_hold_with_no_retry(self):
        for start, end in ((0, 24), (0, 23), (None, 24), (None, None), (True, 23), ('0', 23)):
            raw = response(SOURCE, start, end)
            original = copy.deepcopy(raw)
            review = {}
            with self.subTest(start=start, end=end), patch.object(ai_classifier, '_call_gemini_json',
                    new=AsyncMock(return_value=raw)) as call:
                result = await ai_classifier.extract_policy_fields_async(object(), '정책',
                    'https://example.invalid', content=SOURCE, review=review)
            self.assertIsNone(result)
            call.assert_awaited_once()
            self.assertEqual(raw, original)
            self.assertIn(review['code'], ('structured_evidence_missing', 'source_schema_invalid'))

    def test_frozen_goesan_real_response_is_not_corrected_or_promoted(self):
        root = Path(__file__).resolve().parents[2] / 'artifacts'
        source_path = root / 'ai-holdout-prep-20260913/sources/policy-goesan-diapers.txt'
        ledger_path = root / 'ai-holdout-live-20260913/fresh-live-01/ledger.jsonl'
        source_bytes, ledger_bytes = source_path.read_bytes(), ledger_path.read_bytes()
        self.assertEqual(hashlib.sha256(source_bytes).hexdigest(),
            '03473b6a889c4185d9f7d92f49514eb4e36006203a6c8530884212cfe47ebb48')
        self.assertEqual(hashlib.sha256(ledger_bytes).hexdigest(),
            '91e4f35f18f6d29c5cdab391e6e4cc53d485f2ccdae4eb3ce8ebc725e6e1b9a8')
        rows = [json.loads(line) for line in ledger_bytes.decode('utf-8').splitlines()]
        raw = json.loads(next(row for row in rows if row['event'] == 'response'
            and row['case_id'] == 'policy-goesan-diapers')['raw']['text'])
        original = copy.deepcopy(raw)
        source = source_bytes.decode('utf-8')
        constraint = _source_age_constraint(source)
        self.assertEqual(constraint['end_age_months'], 23)
        self.assertIsNone(constraint['start_age_months'])
        candidate = hydrate_policy_source_ranges(raw, source)
        self.assertEqual(candidate['start_age_months'], 0)
        self.assertEqual(candidate['end_age_months'], 24)
        self.assertEqual(set(structured_policy_issues(candidate, source)),
                         {'start_age_months', 'end_age_months'})
        self.assertEqual(raw, original)
        self.assertEqual(source_path.read_bytes(), source_bytes)
        self.assertEqual(ledger_path.read_bytes(), ledger_bytes)
