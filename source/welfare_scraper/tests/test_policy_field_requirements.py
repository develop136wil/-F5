"""Literal new-request requirements; not a model-quality evaluation."""
import copy
import hashlib
import json
from pathlib import Path
import unittest

from services.policy_field_requirements import policy_field_requirements, missing_policy_fields
from services.policy_source_plan import build_policy_extraction_request, hydrate_policy_source_ranges
from services.policy_source_units import source_units


class PolicyFieldRequirements(unittest.TestCase):
    def test_complete_single_enquiry_is_exact_source_not_footer_or_application(self):
        for newline in ('\n', '\r\n'):
            for heading in ('문의처', '문의처: 각 동 행정복지센터'):
                with self.subTest(newline=repr(newline), heading=heading):
                    rows = ['지원내용', '출산비용 120만원 지원', '신청방법',
                            '관할 동 행정복지센터 방문 신청', heading]
                    if ':' not in heading:
                        rows.append('각 동 행정복지센터')
                    rows += ['장애인복지과 481-2562',
                             '본 저작물은 공공누리 제1유형 조건에 따라 이용할 수 있습니다.']
                    source = newline.join(rows)
                    original = copy.deepcopy(source)
                    requirement = policy_field_requirements(source)['contact']
                    self.assertEqual(requirement['text'], newline.join(
                        ['각 동 행정복지센터', '장애인복지과 481-2562']))
                    self.assertIn(requirement['text'], source)
                    self.assertEqual(source, original)
                    self.assertNotIn('공공누리', requirement['text'])
                    self.assertNotIn('신청', requirement['text'])

    def test_ambiguous_role_footer_and_conditions_do_not_become_contact_requirements(self):
        cases = (
            '지원내용\n돌봄 지원\n신청장소\n각 동 행정복지센터',
            '지원내용\n돌봄 지원\n담당기관\n장애인복지과 481-2562',
            '지원내용\n돌봄 지원\n본 저작물은 공공누리\n문의처\n장애인복지과 481-2562',
            '지원내용\n돌봄 지원\n자료관리담당자\n문의처\n장애인복지과 481-2562',
            '지원내용\n돌봄 지원\n담당부서\n문의처\n장애인복지과 481-2562',
            '지원내용\n돌봄 지원\n제공기관\n문의처\n장애인복지과 481-2562',
            '지원내용\n돌봄 지원\n문의처\n장애인복지과 481-2562\n추가 문의는 별도 기관',
            '지원내용\n돌봄 지원\n문의처\n장애인복지과 481-2562\n문의\n교육과 123-4567',
            '지원내용\n돌봄 지원\n문의처\n장애인복지과 481-2562, 서울 거주자만',
            '지원내용\n돌봄 지원\n문의처\n장애인복지과 481-2562\n만 2세 미만',
            '지원내용\n돌봄 지원\n문의처\n장애인복지과 481-2562\n방문 신청만 가능',
            '지원내용\n돌봄 지원\n문의처\n장애인복지과 481-2562\n소급 지원 불가',
            '지원내용\n돌봄 지원\n문의처\n홍길동 010-1234-5678',
        )
        for source in cases:
            with self.subTest(source=source):
                self.assertIsNone(policy_field_requirements(source)['contact'])

    def test_explicit_income_requires_its_whole_unit_in_income_field(self):
        cases = ('소득\n소득무관', '소득: 소득 무관', '소득\n무관',
                 '소득기준\n소득 제한 없음', '가구소득\n기준중위소득 100% 이하',
                 '가구소득: 월평균 소득 300만원 이하')
        for clause in cases:
            source = f'지원대상\n여성장애인\n{clause}\n지원내용\n출산비용 120만원 지원'
            with self.subTest(clause=clause):
                requirements = policy_field_requirements(source)
                self.assertTrue(requirements['target_income'])
                unit_ids = sorted({item['unit_id'] for item in requirements['target_income']})
                selection = dict(support_detail=[unit['id'] for unit in source_units(source)],
                    eligibility_detail=[], age_detail=[], target_income=[])
                raw = dict(_source_units=selection, contact='')
                missing = hydrate_policy_source_ranges(raw, source)
                self.assertEqual(missing_policy_fields(missing, requirements), ['target_income'])
                selection['target_income'] = unit_ids
                complete = hydrate_policy_source_ranges(raw, source)
                original = copy.deepcopy(complete)
                self.assertEqual(missing_policy_fields(complete, requirements), [])
                self.assertEqual(complete, original)

    def test_leading_income_profile_is_required_in_its_field_even_when_union_unit_is_optional(self):
        for newline in ('\n', '\r\n'):
            for profile in (('소득', '소득무관'), ('소득', '무관')):
                with self.subTest(newline=repr(newline), profile=profile):
                    source = newline.join([*profile, '지원대상', '등록 여성장애인에게 지원',
                                           '지원내용', '출산비용 120만원 지원'])
                    catalog = source_units(source)
                    self.assertFalse(catalog[0]['required'])
                    requirements = policy_field_requirements(source, catalog)
                    self.assertEqual({i['unit_id'] for i in requirements['target_income']}, {1})
                    for item in requirements['target_income']:
                        self.assertIn(item['quote'], source)
                    raw = dict(_source_units=dict(support_detail=[u['id'] for u in catalog],
                        eligibility_detail=[], age_detail=[], target_income=[]), contact='')
                    candidate = hydrate_policy_source_ranges(raw, source)
                    self.assertEqual(missing_policy_fields(candidate, requirements), ['target_income'])
                    raw['_source_units']['target_income'] = [1]
                    candidate = hydrate_policy_source_ranges(raw, source)
                    self.assertEqual(missing_policy_fields(candidate, requirements), [])
                    self.assertIn(newline.join(profile), candidate['target_income'])

    def test_unstated_excluded_document_legal_staff_and_filter_income_are_not_inferred(self):
        cases = ('지원대상\n여성장애인', '지원대상\n소득은 별도 확인',
            '지원제외대상\n소득무관', '지원대상\n소득무관 대상 아님',
            '신청서류\n소득기준\n소득무관', '법적근거\n소득\n소득무관',
            '관련법령\n가구소득\n중위소득 100% 이하',
            '제공인력\n소득기준\n소득무관', '검색필터\n소득\n소득무관',
            '검색조건\n소득: 소득무관', '지원대상\n소득무관 예시',
            '신청서류\n소득 증명서(중위소득 100% 이하)',
            '지원대상\n가구소득은 기준중위소득 100% 이하가 아님')
        for clause in cases:
            source = f'{clause}\n지원내용\n출산비용 120만원 지원'
            with self.subTest(clause=clause):
                self.assertEqual(policy_field_requirements(source)['target_income'], [])

    def test_new_request_explains_per_field_coverage_without_changing_source(self):
        source = ('소득\n소득무관\n지원대상\n여성장애인\n지원내용\n출산비용 120만원 지원\n'
                  '문의처\n각 동 행정복지센터\n장애인복지과 481-2562')
        before = copy.deepcopy(source)
        prompt, _ = build_policy_extraction_request(source, '정책', 'https://example.invalid', '기관', {})
        self.assertIn('[Field-specific source requirements JSON]', prompt)
        self.assertIn('even when already selected in eligibility_detail', prompt)
        self.assertIn('ENTIRE text in contact', prompt)
        self.assertEqual(source, before)

    def test_frozen_ansan_partial_response_stays_unchanged_and_missing_fields_are_explicit(self):
        root = Path(__file__).resolve().parents[2] / 'artifacts'
        source_bytes = (root / 'ai-holdout-prep-20260913/sources/policy-ansan-birth.txt').read_bytes()
        ledger_bytes = (root / 'ai-holdout-live-20260913/fresh-live-01/ledger.jsonl').read_bytes()
        self.assertEqual(hashlib.sha256(source_bytes).hexdigest(),
            '6bbc1bf61cec526c6167e28423555048219ab25f22c7f5c923aaaddee3068c5a')
        self.assertEqual(hashlib.sha256(ledger_bytes).hexdigest(),
            '91e4f35f18f6d29c5cdab391e6e4cc53d485f2ccdae4eb3ce8ebc725e6e1b9a8')
        rows = [json.loads(line) for line in ledger_bytes.decode('utf-8').splitlines()]
        raw = json.loads(next(row for row in rows if row['event'] == 'response'
            and row['case_id'] == 'policy-ansan-birth')['raw']['text'])
        original = copy.deepcopy(raw)
        source = source_bytes.decode('utf-8')
        requirements = policy_field_requirements(source)
        self.assertEqual(requirements['contact']['text'], '각 동 행정복지센터\n장애인복지과 481-2562')
        self.assertEqual({item['unit_id'] for item in requirements['target_income']}, {1})
        result = hydrate_policy_source_ranges(raw, source)
        self.assertEqual(missing_policy_fields(result, requirements), ['contact', 'target_income'])
        self.assertEqual(result['target_income'], '')
        self.assertEqual(result['contact'], '장애인복지과 481-2562')
        self.assertEqual(raw, original)
