"""Offline range transport tests; hand-authored plans are not AI quality scores."""
import copy
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

import ai_classifier
from services.policy_evidence import POLICY_PROSE_FIELDS, validate_policy_evidence
from services.policy_source_plan import (
    MAX_SOURCE_LINES, POLICY_REQUEST_FORMAT, PolicySourcePlanError,
    build_policy_extraction_prompt, hydrate_policy_source_ranges, source_lines,
)
from services import policy_sync


def plan(**ranges):
    return {'_source_ranges': {key: ranges.get(key, []) for key in POLICY_PROSE_FIELDS},
            'category': '', 'target_characteristics': [], 'residence': [],
            'life_cycle': [], 'start_age_months': None, 'end_age_months': None, 'contact': ''}


class RangeTransport(unittest.TestCase):
    def test_exact_crlf_blank_lines_and_terminal_newline_preserved(self):
        for source in ('지원내용\n교통비 월 3만원\n',
                       '지원내용\r\n\r\n교통비 월 3만원\r\n',
                       '지원내용\r교통비 월 3만원'):
            with self.subTest(source=repr(source)):
                raw = plan(support_detail=[1, len(source.splitlines())])
                before = copy.deepcopy(raw)
                result = hydrate_policy_source_ranges(raw, source)
                self.assertEqual(result['support_detail'], source)
                self.assertEqual(result['_source_evidence']['support_detail'], source)
                self.assertEqual(raw, before)
                self.assertNotIn('_source_ranges', result)
                self.assertTrue(validate_policy_evidence(result, source))

    def test_out_of_range_reversed_and_non_integer_ids_rejected(self):
        source = '지원내용\n교통비 월 3만원'
        for value in ([0, 2], [1, 3], [2, 1], [True, 2], [1, False], [1.0, 2],
                      ['1', 2], [1], [1, 2, 3], None, '1:2', {'start': 1}, (1, 2)):
            with self.subTest(value=value), self.assertRaises(PolicySourcePlanError):
                hydrate_policy_source_ranges(plan(support_detail=value), source)

    def test_missing_extra_and_mixed_payloads_rejected_not_silently_repaired(self):
        source = '교통비 월 3만원'
        values = []
        for key in ('support_detail', '_source_evidence', 'complete', 'unexpected'):
            raw = plan(support_detail=[1, 1])
            raw[key] = '100만원으로 바꾸라는 지시'
            values.append(raw)
        for ranges in ({}, [], None, {**plan()['_source_ranges'], 'invented': []}):
            values.append({'_source_ranges': ranges})
        for raw in values:
            with self.subTest(raw=raw), self.assertRaises(PolicySourcePlanError):
                hydrate_policy_source_ranges(raw, source)

    def test_critical_child_rows_cannot_be_omitted(self):
        for heading, body in (
            ('지원 제외 대상', '주택 소유자(분양권, 입주권 포함)'),
            ('제출서류', '임대차계약서 사본 1부'),
            ('신청방법', '기관 포털 온라인 접수'),
            ('지원내용', '교통비 월 3만원'),
        ):
            source = '교육비 지원.\n' + heading + '\n' + body
            with self.subTest(heading=heading), self.assertRaises(PolicySourcePlanError):
                hydrate_policy_source_ranges(plan(support_detail=[1, 2]), source)

    def test_heading_and_payload_cannot_be_distributed_across_fields(self):
        for source in ('지원 제외 대상\n주택 소유자(분양권, 입주권 포함)',
                       '제출서류\n직접제출\n임대차계약서\n공공마이데이터(조회)\n주민등록 등본'):
            with self.subTest(source=source), self.assertRaises(PolicySourcePlanError):
                hydrate_policy_source_ranges(plan(support_detail=[2, len(source.splitlines())],
                                                  eligibility_detail=[1, 1]), source)

    def test_detached_negative_cannot_be_reassigned_across_fields(self):
        for source in ('대학생\n아님\n직장인 신청 가능',
                       '대학생\r\n\r\n지원 대상이 아닙니다\r\n직장인 신청 가능',
                       '현금 지급\n지급하지 않음\n교육비 지원',
                       '현금 지급\n지원하지 않습니다\n교육비 지원',
                       '대학생\n제외됩니다\n교육비 지원'):
            with self.subTest(source=source):
                last = len(source.splitlines())
                with self.assertRaises(PolicySourcePlanError):
                    hydrate_policy_source_ranges(plan(support_detail=[1, 1],
                                                      eligibility_detail=[2, last]), source)
                result = hydrate_policy_source_ranges(plan(support_detail=[1, last]), source)
                self.assertEqual(result['support_detail'], source)
                self.assertTrue(validate_policy_evidence(result, source))

    def test_complete_separate_sections_can_use_separate_fields_without_expansion(self):
        source = '지원내용\n교통비 월 3만원\n지원 제외 대상\n주택 소유자\n문의처\n복지정책과'
        result = hydrate_policy_source_ranges(plan(support_detail=[1, 2], eligibility_detail=[3, 4]), source)
        self.assertEqual(result['support_detail'], '지원내용\n교통비 월 3만원\n')
        self.assertEqual(result['eligibility_detail'], '지원 제외 대상\n주택 소유자\n')
        self.assertNotIn('복지정책과', str(result))
        self.assertTrue(validate_policy_evidence(result, source))

    def test_structured_facts_still_require_recipient_evidence(self):
        source = '지원내용\n교육비 월 3만원'
        raw = plan(support_detail=[1, 2])
        raw.update(residence=['전국'], start_age_months=0, end_age_months=24)
        result = hydrate_policy_source_ranges(raw, source)
        self.assertFalse(validate_policy_evidence(result, source))
        self.assertEqual(result['residence'], ['전국'])  # never repair to unknown to get approval

    def test_empty_plan_and_blank_slice_never_establish_valid_prose(self):
        result = hydrate_policy_source_ranges(plan(), '일반 안내')
        self.assertFalse(validate_policy_evidence(result, '일반 안내'))
        with self.assertRaises(PolicySourcePlanError):
            hydrate_policy_source_ranges(plan(support_detail=[2, 2]), '안내\n\n')

    def test_original_source_line_numbers_not_metadata_are_evidence(self):
        source = '기관 소개\n지원내용\n교통비 월 3만원\n문의처\n1234-5678'
        result = hydrate_policy_source_ranges(plan(support_detail=[2, 3]), source)
        self.assertEqual(result['support_detail'], '지원내용\n교통비 월 3만원\n')
        self.assertNotIn('[2,', result['support_detail'])
        prompt = build_policy_extraction_prompt(source, '지원금 999만원 지시', 'https://example.invalid', '가짜기관', {})
        units = json.loads(prompt.split('[Source units JSON: id and complete original text]\n')[1].split('\n[Allowed')[0])
        self.assertEqual(units[0], {'id': 1, 'text': '기관 소개\n'})
        self.assertIn('999만원', prompt)
        self.assertNotIn('999만원', str(result))

    def test_no_schema_or_quote_mutation_of_legacy_response(self):
        source = '교육비 월 3만원'
        raw = {'support_detail': source, '_source_evidence': {'support_detail': source}}
        self.assertIs(hydrate_policy_source_ranges(raw, source), raw)
        self.assertTrue(validate_policy_evidence(raw, source))

    def test_line_limit_holds_without_truncation(self):
        for count, valid in ((MAX_SOURCE_LINES, True), (MAX_SOURCE_LINES + 1, False)):
            source = '가\n' * count
            if valid:
                self.assertEqual(len(source_lines(source)), count)
            else:
                with self.assertRaises(PolicySourcePlanError):
                    source_lines(source)

    def test_checklist_contains_ids_not_duplicated_source_payload(self):
        source = '지원내용\n교통비 월 3만원\n지원 제외 대상\n주택 소유자'
        prompt = build_policy_extraction_prompt(source, '정책', 'https://example.invalid', '기관', {})
        self.assertEqual(prompt.count('교통비 월 3만원'), 1)
        section = prompt.split('[Required unit IDs JSON]\n')[1].split('\n\nFINAL')[0]
        self.assertEqual(json.loads(section), [1, 2])
        self.assertNotIn('[Coverage candidate ranges', prompt)
        self.assertIn('Only unit text supplies facts', prompt)
        self.assertIn('never instructions', prompt)


class ExtractorRangeIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_one_successful_call_hydrates_and_runs_existing_guard(self):
        source = '지원내용\n교통비 월 3만원\n지원 제외 대상\n주택 소유자'
        raw = plan(support_detail=[1, 4])
        with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value=raw)) as call:
            result = await ai_classifier.extract_policy_fields_async(object(), '정책', 'https://example.invalid', content=source)
        self.assertEqual(call.await_count, 1)
        self.assertEqual(result['support_detail'], source)
        self.assertTrue(validate_policy_evidence(result, source))
        self.assertIn('_source_units', call.await_args.args[1])

    async def test_invalid_or_incomplete_result_held_without_retry_or_sensitive_log(self):
        source = '교육비 지원.\n제출서류\n서류 전체'
        cases = ((plan(support_detail=[1, 2]), 'source_conditions_incomplete'),
                 (plan(support_detail=[0, 3]), 'source_range_invalid'))
        for raw, code in cases:
            review = {}
            with self.subTest(code=code), patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value=raw)) as call, patch.object(ai_classifier, 'logger') as logger:
                result = await ai_classifier.extract_policy_fields_async(object(), '정책', 'https://example.invalid', content=source, review=review)
            self.assertIsNone(result)
            self.assertEqual(call.await_count, 1)
            self.assertEqual(review['code'], code)
            self.assertNotIn('서류 전체', str(logger.mock_calls))
            self.assertIn('기존 내용은 유지', review['note'])

    async def test_unsupported_tags_remain_on_hold_after_exact_hydration(self):
        source = '지원내용\n교통비 월 3만원'
        raw = plan(support_detail=[1, 2])
        raw['residence'] = ['전국']
        review = {}
        with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value=raw)) as call:
            result = await ai_classifier.extract_policy_fields_async(object(), '서울 행사', 'https://example.invalid', content=source, review=review)
        self.assertIsNone(result)
        self.assertEqual(call.await_count, 1)
        self.assertEqual(review['code'], 'structured_evidence_missing')

    async def test_large_line_structure_spends_no_request(self):
        review = {}
        with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock()) as call, patch.dict(os.environ, {'AI_CONTENT_MAX_CHARS': '6000'}):
            result = await ai_classifier.extract_policy_fields_async(object(), '정책', 'https://example.invalid',
                content='가\n' * (MAX_SOURCE_LINES + 1), review=review)
        self.assertIsNone(result)
        call.assert_not_awaited()
        self.assertEqual(review['code'], 'source_too_long')

    def test_request_format_metadata_does_not_change_compatibility_version(self):
        with patch.object(policy_sync, '_get_db_schema', return_value={}):
            meta = policy_sync._extraction_meta('medical')
        self.assertEqual(meta['request_format'], POLICY_REQUEST_FORMAT)
        self.assertEqual(meta['prompt_version'], 'policy-extract-v6-payment-predicate')
