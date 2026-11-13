"""Offline contract tests; synthetic model selections are NOT live AI scores."""
import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

from services import news_custom_analysis as custom
from services import news_source_selection as selection
from services.news_profile import NewsProfile
from services.news_analysis_safety import assess


def response(ids=None):
    schema = selection.response_schema(custom.response_schema(complete_grounding=True))
    output = {key: '' for key in schema['required']}
    output.update(is_relevant=True, category='행사', target='미상', applicant_type='unknown',
        applicant_detail='미상', beneficiary_detail='미상', start_age_months=None,
        end_age_months=None, residence=[], life_cycle=[], target_characteristics=[],
        interest_topics=[], life_cycle_evidence=[], residence_evidence=[],
        _support_source_units=[1] if ids is None else ids,
        fact_evidence={key: '' for key in ('eligibility_detail', 'contact', 'deadline', 'is_relevant')})
    return output


class WholeSourceSelection(unittest.TestCase):
    def test_small_complete_source_and_transport_boundaries(self):
        for text, expected in (
            ('가' * 1200, True), ('가' * 1201, False), ('', False), (' \r\n', False),
            ('가\n' * 80, True), ('가\n' * 81, False),
            ('본문\x00제외', False), ('본문\u202e제외', False),
            ('본문\n[... 일부 내용 생략 ...]\n제외', False),
            ('본문\n--- [첨부파일: 조건]\n조건', False),
        ):
            with self.subTest(text=text[:25], size=len(text)):
                self.assertEqual(bool(selection.eligible_source(text)), expected)
        self.assertFalse(selection.eligible_source(None))
        self.assertFalse(selection.eligible_payload({'content':'본문', 'source_page':{'reasons':['fetch_failed']}}))
        self.assertTrue(selection.eligible_payload({'content':'본문', 'source_page':{'reasons':[]}}))

    def test_original_line_endings_and_tail_preserved_without_mutating_response(self):
        source = '행사 안내\r\n신청: 2026.7.14. 18;00까지\r\n중복 신청 불가\r\n'
        raw = response()
        frozen = copy.deepcopy(raw)
        result = selection.materialize(raw, source)
        self.assertEqual(raw, frozen)
        self.assertNotIn('_support_source_units', result)
        self.assertEqual(result['support_detail'], source)
        self.assertEqual(result['fact_evidence']['support_detail'], source)
        self.assertEqual(selection.selection_issue(json.loads(json.dumps(result)), source, required=True), '')

    def test_bad_or_mixed_contracts_fail_closed(self):
        for ids in ([True], [1.0], ['1'], [0], [2], [1,1], '1', {}, None):
            raw = response()
            raw['_support_source_units'] = ids
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                selection.materialize(raw, '신청 안내')
        for key, value in (('support_detail','자의적 요약'), (selection.META_KEY, {}),
                           ('source_input', {'status':'complete'}), ('accepted', True)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                selection.materialize(dict(response(), **{key:value}), '신청 안내')
        raw = response()
        raw['fact_evidence']['support_detail'] = '신청 안내'
        with self.assertRaises(ValueError):
            selection.materialize(raw, '신청 안내')
        raw = response()
        del raw['deadline']
        with self.assertRaises(ValueError):
            selection.materialize(raw, '신청 안내')

    def test_empty_selection_and_missing_metadata_remain_unconfirmed(self):
        source = '개인 신청 안내'
        raw = selection.materialize(response([]), source)
        self.assertEqual(raw['support_detail'], '')
        self.assertEqual(selection.selection_issue(raw, source), 'source_selection_unconfirmed')
        profile = NewsProfile('test', 1, '시험', '전국', general_mode='custom_v1')
        for value in (raw, {'support_detail': source}):
            output = assess(profile, value, source, require_source_selection=True)
            self.assertEqual(output['target_status'], 'review')
            self.assertFalse(output['accepted'])
            self.assertEqual(output['analysis']['support_detail'], '')
        self.assertEqual(selection.selection_issue({}, source), '')  # historical contract

    def test_persisted_selection_cannot_restore_or_authorize_changed_text(self):
        source = '지원 대상\n개인 신청 가능\n단, 중복 신청 불가'
        original = selection.materialize(response(), source)
        changes = []
        value = copy.deepcopy(original); value['support_detail'] = ''
        changes.append(value)
        value = copy.deepcopy(original); value['support_detail'] = '개인 신청 가능'
        value['fact_evidence']['support_detail'] = '개인 신청 가능'
        changes.append(value)
        value = copy.deepcopy(original); value[selection.META_KEY]['source_sha256'] = '0' * 64
        changes.append(value)
        value = copy.deepcopy(original); value[selection.META_KEY]['version'] = True
        changes.append(value)
        for value in changes:
            self.assertEqual(selection.selection_issue(value, source), 'source_selection_changed')
        self.assertEqual(selection.selection_issue(original, source + '\n조건 추가'), 'source_selection_changed')

    def test_schema_has_no_model_authored_support_and_is_sdk_compatible(self):
        base = custom.response_schema(complete_grounding=True)
        snapshot = copy.deepcopy(base)
        schema = selection.response_schema(base)
        self.assertEqual(base, snapshot)
        self.assertNotIn('support_detail', schema['required'])
        self.assertNotIn('support_detail', schema['properties']['fact_evidence']['required'])
        self.assertEqual(schema['properties']['_support_source_units']['items']['enum'], [1])
        from google.genai.types import GenerateContentConfig
        self.assertEqual(GenerateContentConfig(response_json_schema=schema).response_json_schema, schema)


class WholeSourceRequest(unittest.IsolatedAsyncioTestCase):
    async def capture(self, source, *, page=None, raw=None, enabled=True):
        profile = NewsProfile('test', 1, '시험', '전국', general_mode='custom_v1')
        payload = dict(title='제목은 근거 아님', site_name='기관', link='https://example.invalid/', content=source)
        if page is not None:
            payload['source_page'] = page
        with patch('ai_classifier._call_gemini_json', new=AsyncMock(return_value=raw or response())) as call:
            output = await custom.classify(object(), profile, payload, 'mock-only',
                source_unit_rules=enabled, request_attempts=1)
        self.assertEqual(call.await_count, 1)
        self.assertEqual(call.call_args.kwargs['request_attempts'], 1)
        return output, call.call_args

    async def test_v14_materializes_after_one_request_and_keeps_other_facts(self):
        source = '신청 안내\n개인이 온라인 신청\n접수 2026.7.14. 18;00까지\n중복 신청 불가'
        raw = response()
        raw['deadline'] = '2026-07-14 18:00'
        output, args = await self.capture(source, raw=raw)
        self.assertEqual(output['support_detail'], source)
        self.assertIn(selection.PROMPT, args.args[1])
        self.assertEqual(json.loads(args.args[1].split('\nSOURCE: ')[1])['content'], source)
        self.assertEqual(output['source_input']['status'], 'complete')
        profile = NewsProfile('test', 1, '시험', '전국', general_mode='custom_v1')
        result = assess(profile, output, source, require_source_selection=True)
        self.assertEqual(result['analysis']['support_detail'], source)
        self.assertEqual(result['analysis']['deadline'], '')
        self.assertEqual(result['target_status'], 'review')

    async def test_ineligible_inputs_use_prose_contract_without_second_request(self):
        for source, page in (('가' * 1201, None), ('가' * 6001, None),
                             ('짧은 본문', {'reasons':['attachment_review']}),
                             ('본문\n--- [첨부파일: 문서]\n예외', None), ('', None)):
            legacy = response()
            del legacy['_support_source_units']
            legacy['support_detail'] = ''
            legacy['fact_evidence']['support_detail'] = ''
            with self.subTest(size=len(source), page=page):
                output, args = await self.capture(source, page=page, raw=legacy)
                self.assertNotIn(selection.META_KEY, output)
                self.assertNotIn(selection.PROMPT, args.args[1])
                self.assertIn('support_detail', args.kwargs['response_json_schema']['required'])
                if page:
                    self.assertEqual(output['source_page']['status'], 'review_required')
                if len(source) > 6000:
                    self.assertEqual(output['source_input']['status'], 'partial')

    async def test_invalid_response_does_not_trigger_fallback_call(self):
        profile = NewsProfile('test', 1, '시험', '전국', general_mode='custom_v1')
        payload = dict(title='제목', site_name='기관', link='https://example.invalid/', content='원문')
        with patch('ai_classifier._call_gemini_json', new=AsyncMock(return_value={})) as call:
            with self.assertRaises(ValueError):
                await custom.classify(object(), profile, payload, 'mock-only',
                    source_unit_rules=True, request_attempts=1)
        self.assertEqual(call.await_count, 1)

    def test_old_queue_capability_thresholds_unchanged(self):
        self.assertTrue(custom.recipe_options(custom.RECIPE)['source_unit_rules'])
        self.assertTrue(custom.recipe_options(custom.BOUNDED_SOURCE_RECIPE)['source_unit_rules'])
        self.assertTrue(custom.recipe_options(custom.SHORT_SOURCE_RECIPE)['source_unit_rules'])
        self.assertTrue(custom.recipe_options(custom.RECIPE)['whole_document_rules'])
        self.assertTrue(custom.recipe_options(custom.BOUNDED_SOURCE_RECIPE)['whole_document_rules'])
        self.assertFalse(custom.recipe_options(custom.SHORT_SOURCE_RECIPE)['whole_document_rules'])
        for recipe in custom.SUPPORTED_RECIPES[custom.SUPPORTED_RECIPES.index(custom.SOURCE_SPAN_RECIPE):]:
            self.assertFalse(custom.recipe_options(recipe)['source_unit_rules'])
        self.assertTrue(custom.recipe_options(custom.SOURCE_SPAN_RECIPE)['source_span_rules'])
        self.assertFalse(custom.recipe_options(custom.EXPLICIT_FACTS_RECIPE)['source_span_rules'])
