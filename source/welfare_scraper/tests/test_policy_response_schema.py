"""Offline schema contracts; SDK serialization is not provider acceptance."""
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from google.genai import types
import ai_classifier
from services import ai_coordinator
from services import policy_source_plan as plan
from services.policy_source_units import source_units

SOURCE = '지원내용\n교통비 3만원\n신청방법\n온라인 신청\n제출서류\n신분증'
OPTIONS = {'category': ['복지'], 'target_characteristics': ['일반'],
           'residence': ['전국'], 'life_cycle': ['영아']}


def request():
    return plan.build_policy_extraction_request(SOURCE, '시험 정책',
        'https://example.invalid', '기관', OPTIONS)


def valid_response():
    return {'_source_units': {'support_detail': [u['id'] for u in source_units(SOURCE)],
        'eligibility_detail': [], 'age_detail': [], 'target_income': []},
        'category': '복지', 'target_characteristics': [], 'residence': [],
        'life_cycle': [], 'start_age_months': None, 'end_age_months': None, 'contact': ''}


class SchemaContract(unittest.TestCase):
    def test_schema_uses_same_catalog_and_screened_options_as_prompt(self):
        before = copy.deepcopy(OPTIONS)
        prompt, schema = request()
        options = json.loads(prompt.split('[Allowed Notion options]\n')[1].split('\n[Required')[0])
        props = schema['properties']
        self.assertEqual(props['category']['enum'], ['', *options['category']])
        for field in ('target_characteristics', 'residence', 'life_cycle'):
            self.assertEqual(props[field]['items'].get('enum', []), options[field])
            self.assertEqual(props[field]['maxItems'], len(options[field]))
        for spec in props['_source_units']['properties'].values():
            self.assertEqual(spec['items'], {'type': 'integer', 'minimum': 1,
                'maximum': len(source_units(SOURCE))})
        self.assertEqual(OPTIONS, before)

    def test_valid_and_abstention_round_trip_through_installed_sdk(self):
        _, schema = request()
        config = types.GenerateContentConfig(response_mime_type='application/json',
            response_json_schema=schema)
        self.assertEqual(json.loads(config.model_dump_json())['response_json_schema'], schema)
        self.assertTrue(plan.policy_response_matches_schema(valid_response(), schema))
        self.assertTrue(plan.policy_response_matches_schema(None, schema))

    def test_empty_candidates_are_empty_arrays_without_invalid_empty_enum(self):
        _, schema = request()
        field = schema['properties']['life_cycle']
        self.assertEqual(field, {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 0})
        invalid = valid_response()
        invalid['life_cycle'] = ['영아']
        self.assertFalse(plan.policy_response_matches_schema(invalid, schema))

    def test_bad_types_bounds_unknown_options_and_extra_missing_keys_are_rejected(self):
        _, schema = request()
        mutations = [('_source_units', {'support_detail': ['교통비 3만원'],
            'eligibility_detail': [], 'age_detail': [], 'target_income': []}),
            ('category', '출산/보육'), ('category', None), ('contact', 123),
            ('residence', ['없는 지역']), ('target_characteristics', ['없는 대상']),
            ('start_age_months', True), ('start_age_months', 1.5),
            ('start_age_months', -1), ('end_age_months', 1801), ('extra', '')]
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                raw = valid_response()
                raw[key] = value
                self.assertFalse(plan.policy_response_matches_schema(raw, schema))
        for key in valid_response():
            with self.subTest(missing=key):
                raw = valid_response()
                del raw[key]
                self.assertFalse(plan.policy_response_matches_schema(raw, schema))
        for value in (True, '1', 0, len(source_units(SOURCE)) + 1):
            with self.subTest(id=value):
                raw = valid_response()
                raw['_source_units']['support_detail'] = [value]
                self.assertFalse(plan.policy_response_matches_schema(raw, schema))

    def test_combined_request_size_holds_without_truncation(self):
        prompt, schema = request()
        total = len(prompt) + len(json.dumps(schema, ensure_ascii=False, separators=(',', ':')))
        with patch.object(plan, 'MAX_POLICY_PROMPT_CHARS', total):
            self.assertEqual(request(), (prompt, schema))
        with patch.object(plan, 'MAX_POLICY_PROMPT_CHARS', total - 1):
            with self.assertRaises(plan.PolicySourcePlanError) as caught:
                request()
        self.assertEqual(caught.exception.code, 'source_structure_too_large')


class SchemaCallPath(unittest.IsolatedAsyncioTestCase):
    async def extract(self, raw):
        review = {}
        before = copy.deepcopy(raw)
        with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value=raw)) as call:
            result = await ai_classifier.extract_policy_fields_async(object(), '시험 정책',
                'https://example.invalid', content=SOURCE, taxonomy=OPTIONS, review=review)
        self.assertEqual(raw, before)
        call.assert_awaited_once()
        self.assertIsNotNone(call.await_args.kwargs['response_json_schema'])
        return result, review

    async def test_complete_grounded_selection_is_accepted(self):
        result, review = await self.extract(valid_response())
        self.assertIsNotNone(result)
        self.assertEqual(result['support_detail'], SOURCE)

    async def test_unknown_category_is_held_not_silently_replaced(self):
        raw = valid_response()
        raw['category'] = '출산/보육'
        result, review = await self.extract(raw)
        self.assertIsNone(result)
        self.assertEqual(review['code'], 'source_schema_invalid')

    async def test_schema_does_not_replace_coverage_order_or_evidence_guards(self):
        for ids, expected in (([], 'source_conditions_incomplete'),
                ([1, 1], 'source_range_invalid'), ([2, 1], 'source_range_invalid')):
            with self.subTest(ids=ids):
                raw = valid_response()
                raw['_source_units']['support_detail'] = ids
                result, review = await self.extract(raw)
                self.assertIsNone(result)
                self.assertEqual(review['code'], expected)

    async def test_null_remains_abstention(self):
        result, review = await self.extract(None)
        self.assertIsNone(result)
        self.assertEqual(review['code'], 'model_abstained')

    async def test_schema_is_sent_and_counted_in_admission_without_extra_requests(self):
        prompt, schema = request()
        for supplied in (schema, None):
            with self.subTest(schema=supplied is not None):
                response = types.GenerateContentResponse(candidates=[types.Candidate(
                    content=types.Content(parts=[types.Part(text='null')]), finish_reason='STOP')])
                generate = AsyncMock(return_value=response)
                client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
                with patch.object(ai_coordinator, 'acquire_model_slot', new=AsyncMock()) as admit:
                    self.assertIsNone(await ai_classifier._call_gemini_json(client, prompt,
                        '시험 정책', response_json_schema=supplied))
                generate.assert_awaited_once()
                admit.assert_awaited_once()
                quota_input = prompt if supplied is None else [prompt,
                    json.dumps(supplied, ensure_ascii=False, separators=(',', ':'))]
                self.assertEqual(admit.await_args.kwargs['input_tokens'],
                    ai_coordinator.estimate_input_tokens(quota_input))
                if supplied is not None:
                    self.assertGreater(admit.await_args.kwargs['input_tokens'],
                        ai_coordinator.estimate_input_tokens(prompt))
                self.assertEqual(generate.await_args.kwargs['config'].response_json_schema, supplied)
