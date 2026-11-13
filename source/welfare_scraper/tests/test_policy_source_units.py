"""Offline unit transport contracts; hand-written selections are not AI scores."""
import copy
import hashlib
import json
import unittest
from unittest.mock import AsyncMock, patch

import ai_classifier
from services.policy_evidence import POLICY_PROSE_FIELDS, diagnose_policy_evidence, validate_policy_evidence
from services.policy_source_plan import PolicySourcePlanError, build_policy_extraction_prompt, hydrate_policy_source_ranges
from services.policy_source_units import source_units, render_unit_selection, SELECTION_KEY

SOURCE = ('지원내용\n교통비 월 3만원\n문의처\n복지정책과\n'
          '신청방법\n온라인 또는 주민센터 방문 신청\n지원 제외 대상\n주택 소유자\n'
          '제출서류\n임대차계약서와 본인 명의 통장 사본')


def plan(**fields):
    return {'_source_units': {key: fields.get(key, []) for key in POLICY_PROSE_FIELDS},
            'category': '', 'target_characteristics': [], 'residence': [],
            'life_cycle': [], 'start_age_months': None, 'end_age_months': None, 'contact': ''}


def complete_plan(source=SOURCE):
    fields = {key: [] for key in POLICY_PROSE_FIELDS}
    for unit in source_units(source):
        if unit['required']:
            field = 'eligibility_detail' if set(unit['kinds']) & {'eligibility', 'exclusion', 'continuation'} else 'support_detail'
            fields[field].append(unit['id'])
    return plan(**fields)


class UnitTransport(unittest.TestCase):
    def test_repeated_headers_are_one_unit_not_heading_only_choices(self):
        source = '신청안내\n신청기간:\n신청기간: 2026년 9월\n제출서류\n구분\n제출서류\n신분증'
        units = source_units(source)
        self.assertEqual([(u['start_line'], u['end_line']) for u in units], [(1, 3), (4, 7)])
        result = hydrate_policy_source_ranges(plan(support_detail=[1, 2]), source)
        self.assertEqual(result['support_detail'], source)
        self.assertTrue(validate_policy_evidence(result, source))

    def test_units_partition_source_preserving_headings_children(self):
        units = source_units(SOURCE)
        self.assertEqual([(u['start_line'], u['end_line']) for u in units], [(1, 2), (3, 4), (5, 6), (7, 8), (9, 10)])
        self.assertEqual([u['id'] for u in units if u['required']], [1, 3, 4, 5])
        lines = SOURCE.splitlines(keepends=True)
        self.assertEqual(''.join(''.join(lines[u['start_line'] - 1:u['end_line']]) for u in units), SOURCE)

    def test_disjoint_units_pass_existing_fact_guards_and_keep_all_requirements(self):
        raw = complete_plan()
        before = copy.deepcopy(raw)
        result = hydrate_policy_source_ranges(raw, SOURCE)
        self.assertEqual(before, raw)
        self.assertNotIn('복지정책과', result['support_detail'])
        for text in ('교통비 월 3만원', '온라인 또는 주민센터 방문 신청', '임대차계약서와 본인 명의 통장 사본'):
            self.assertIn(text, result['support_detail'])
        self.assertEqual(result['eligibility_detail'], '지원 제외 대상\n주택 소유자\n')
        self.assertTrue(validate_policy_evidence(result, SOURCE))

    def test_json_round_trip_retains_independently_verifiable_provenance(self):
        result = hydrate_policy_source_ranges(complete_plan(), SOURCE)
        self.assertTrue(validate_policy_evidence(json.loads(json.dumps(result)), SOURCE))
        self.assertEqual(result[SELECTION_KEY]['source_sha256'], hashlib.sha256(SOURCE.encode()).hexdigest())

    def test_normalize_and_store_preserve_literal_source_markdown(self):
        from services import policy_sync
        source = '### 지원내용\n**교통비 월 3만원**\n신청방법\n방문 신청'
        result = hydrate_policy_source_ranges(plan(support_detail=[1, 2]), source)
        with patch.object(policy_sync, '_get_db_schema', return_value={}):
            policy_sync._normalize_extracted_fields(result)
            legacy = {'support_detail': '### 지원내용\n**교통비 월 3만원**'}
            policy_sync._normalize_extracted_fields(legacy)
        self.assertEqual(result['support_detail'], source)
        self.assertTrue(validate_policy_evidence(json.loads(json.dumps(result)), source))
        self.assertNotIn('###', legacy['support_detail'])
        self.assertNotIn('**', legacy['support_detail'])

    def test_adjacent_units_retain_exact_original_line_endings(self):
        for source in ('지원내용\r\n교통비 월 3만원\r\n신청방법\r\n방문 신청\r\n',
                       '지원내용\r교통비 월 3만원\r신청방법\r방문 신청'):
            with self.subTest(source=repr(source)):
                ids = [unit['id'] for unit in source_units(source)]
                result = hydrate_policy_source_ranges(plan(support_detail=ids), source)
                self.assertEqual(result['support_detail'], source)
                self.assertTrue(validate_policy_evidence(result, source))

    def test_every_required_unit_omission_is_rejected(self):
        raw = complete_plan()
        for field, ids in raw['_source_units'].items():
            for number in ids:
                changed = copy.deepcopy(raw)
                changed['_source_units'][field].remove(number)
                with self.subTest(number=number), self.assertRaisesRegex(PolicySourcePlanError, 'source_sections_incomplete'):
                    hydrate_policy_source_ranges(changed, SOURCE)

    def test_invalid_ids_shapes_order_duplicates_and_bool_rejected(self):
        for value in ([0], [999], [True], [1.0], ['1'], [[1, 2]], [3, 1], [1, 1], None, {}, '1', (1,)):
            with self.subTest(value=value), self.assertRaises(PolicySourcePlanError):
                hydrate_policy_source_ranges(plan(support_detail=value), SOURCE)
        for fields in ({}, [], None, {**plan()['_source_units'], 'invented': []}):
            with self.subTest(fields=fields), self.assertRaises(PolicySourcePlanError):
                hydrate_policy_source_ranges({'_source_units': fields}, SOURCE)

    def test_provider_mixed_prose_ranges_and_internal_provenance_rejected(self):
        for key in ('support_detail', '_source_ranges', '_source_evidence', SELECTION_KEY, 'complete'):
            raw = complete_plan()
            raw[key] = 'untrusted'
            with self.subTest(key=key), self.assertRaises(PolicySourcePlanError):
                hydrate_policy_source_ranges(raw, SOURCE)

    def test_missing_provenance_cannot_launder_disjoint_quotes(self):
        result = hydrate_policy_source_ranges(complete_plan(), SOURCE)
        result.pop(SELECTION_KEY)
        self.assertFalse(validate_policy_evidence(result, SOURCE))

    def test_tampering_in_display_evidence_provenance_or_source_is_rejected(self):
        original = hydrate_policy_source_ranges(complete_plan(), SOURCE)
        for kind in ('display', 'evidence', 'both', 'hash', 'selection', 'extra', 'version', 'source'):
            result, source = copy.deepcopy(original), SOURCE
            if kind in ('display', 'both'):
                result['support_detail'] = result['support_detail'].replace('3만원', '30만원')
            if kind in ('evidence', 'both'):
                result['_source_evidence']['support_detail'] = result['support_detail'] + '추가 지급'
            if kind == 'hash':
                result[SELECTION_KEY]['source_sha256'] = '0' * 64
            if kind == 'selection':
                result[SELECTION_KEY]['fields']['support_detail'] = [1]
            if kind == 'extra':
                result[SELECTION_KEY]['approved'] = True
            if kind == 'version':
                result[SELECTION_KEY]['version'] = True
            if kind == 'source':
                source += '\n추가 제외 대상'
            with self.subTest(kind=kind):
                self.assertFalse(validate_policy_evidence(result, source))

    def test_negative_subject_and_table_parent_are_indivisible(self):
        for source in ('대학생\n아님\n직장인 신청 가능',
                       '지원 제외 대상\n연령\n만 20세 이하\n소득기준\n100만원 이하'):
            units = source_units(source)
            self.assertEqual(len(units), 1)
            result = hydrate_policy_source_ranges(plan(eligibility_detail=[1]), source)
            self.assertEqual(result['eligibility_detail'], source)
            self.assertTrue(validate_policy_evidence(result, source))

    def test_unknown_context_is_not_automatically_assigned(self):
        source = '기관 소개\n여기는 복지 안내\n지원내용\n교통비 월 3만원'
        result = hydrate_policy_source_ranges(plan(support_detail=[2]), source)
        self.assertNotIn('기관 소개', str(result))
        self.assertEqual(result['support_detail'], '지원내용\n교통비 월 3만원')

    def test_structured_hallucinations_remain_rejected(self):
        raw = complete_plan()
        raw.update(residence=['전국'], end_age_months=24)
        result = hydrate_policy_source_ranges(raw, SOURCE)
        self.assertFalse(validate_policy_evidence(result, SOURCE))
        self.assertIn('structured_fact_unsupported', [i['code'] for i in diagnose_policy_evidence(result, SOURCE)['issues']])

    def test_prompt_contains_one_source_copy_and_whole_unit_catalog(self):
        prompt = build_policy_extraction_prompt(SOURCE, '정책', 'https://example.invalid', '기관', {})
        self.assertEqual(prompt.count('교통비 월 3만원'), 1)
        self.assertIn('_source_units must contain exactly', prompt)
        self.assertNotIn('[Source rows JSON', prompt)
        catalog = prompt.split('[Source units JSON: id and complete original text]\n')[1].split('\n[Allowed')[0]
        lines = SOURCE.splitlines(keepends=True)
        self.assertEqual(json.loads(catalog), [
            {'id': u['id'], 'text': ''.join(lines[u['start_line'] - 1:u['end_line']])}
            for u in source_units(SOURCE)])

    def test_old_incomplete_range_response_still_rejected(self):
        raw = {'_source_ranges': {key: [] for key in POLICY_PROSE_FIELDS}}
        raw['_source_ranges']['support_detail'] = [1, 2]
        with self.assertRaises(PolicySourcePlanError):
            hydrate_policy_source_ranges(raw, SOURCE)


class UnitIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_fragmented_source_or_large_metadata_holds_before_call(self):
        from services.policy_source_plan import (
            MAX_SOURCE_LINES, MAX_POLICY_SOURCE_CHARS, MAX_COUNTED_POLICY_PROMPT_CHARS)
        for source, title in (('안내\n' * (MAX_SOURCE_LINES + 1), '정책'),
                              ('가' * (MAX_POLICY_SOURCE_CHARS + 1), '정책'),
                              (SOURCE, '가' * (MAX_COUNTED_POLICY_PROMPT_CHARS + 1))):
            review = {}
            with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock()) as call:
                result = await ai_classifier.extract_policy_fields_async(object(), title, 'https://example.invalid',
                    content=source, review=review)
            self.assertIsNone(result)
            call.assert_not_awaited()
            self.assertEqual(review['code'], 'source_too_long')

    async def test_single_call_hydrates_units_and_runs_final_guards(self):
        with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value=complete_plan())) as call:
            result = await ai_classifier.extract_policy_fields_async(object(), '정책', 'https://example.invalid', content=SOURCE)
        self.assertEqual(call.await_count, 1)
        self.assertTrue(validate_policy_evidence(result, SOURCE))
        self.assertIn('_source_units', call.await_args.args[1])

    async def test_missing_unit_holds_without_retry(self):
        review = {}
        with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value=plan(support_detail=[1]))) as call:
            result = await ai_classifier.extract_policy_fields_async(object(), '정책', 'https://example.invalid', content=SOURCE, review=review)
        self.assertIsNone(result)
        self.assertEqual(call.await_count, 1)
        self.assertEqual(review['code'], 'source_conditions_incomplete')

    async def test_null_response_is_distinct_from_missing_source_or_quotes(self):
        review = {}
        with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value=None)) as call:
            result = await ai_classifier.extract_policy_fields_async(object(), '정책', 'https://example.invalid', content=SOURCE, review=review)
        self.assertIsNone(result)
        self.assertEqual(call.await_count, 1)
        self.assertEqual(review['code'], 'model_abstained')
        self.assertNotIn('충돌', review['note'])
        self.assertIn('기존 내용은 유지', review['note'])
