"""Preserved public sources, real admission, fake external generation only.

Run via scripts/run_isolated_tests.py. No live fetch, Google, Notion or model
quality claim. Incomplete captured pages must remain review after admission.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from google.genai import types
from services import ai_coordinator as quota, news_custom_analysis as custom
from services import news_analysis_store as jobs, news_default_condition as defaults
from services import news_profile_store as profiles, news_source_selection as selection
from services import page_coverage, policy_source_plan as policy
from services.news_custom_request_plan import compact_response_schema
from services.news_analysis_worker import process_job
from services.news_profile import NewsProfile
from tests import test_news_custom_input_budget as fixtures


CORPUS = Path(__file__).resolve().parents[2] / 'artifacts' / 'ai-holdout-prep-20260913'


def public_case(case_id):
    manifest = json.loads((CORPUS / 'sources-manifest.json').read_text(encoding='utf-8'))
    case = next(row for row in manifest['cases'] if row['id'] == case_id)
    raw = (CORPUS / case['source_file']).read_bytes()
    if hashlib.sha256(raw).hexdigest() != case['source_sha256']:
        raise AssertionError('Frozen public source changed')
    return case, raw.decode('utf-8')


def expanded(schema):
    definitions = schema.get('$defs', {})
    def visit(value):
        if isinstance(value, dict):
            if '$ref' in value:
                return visit(definitions[value['$ref'].split('/')[-1]])
            return {key: visit(child) for key, child in value.items() if key != '$defs'}
        if isinstance(value, list):
            return [visit(child) for child in value]
        return value
    return visit(schema)


class RequestReferenceContracts(unittest.TestCase):
    def test_news_references_are_exactly_the_same_schema_without_mutating_inline(self):
        for whole in (False, True):
            inline = custom.response_schema(grounding=True, complete_grounding=True)
            if whole:
                inline = selection.response_schema(inline)
            original = copy.deepcopy(inline)
            factored = compact_response_schema(inline)
            self.assertEqual(inline, original)
            self.assertEqual(expanded(factored), inline)
            self.assertIn('$defs', factored)
            self.assertLess(quota.estimate_generation_input_tokens('', factored),
                            quota.estimate_generation_input_tokens('', inline))

    def test_v17_identity_and_capabilities_are_unchanged(self):
        self.assertEqual(custom.BUDGET_RECIPE, 'news-custom-v17-complete-request-budget-page6000')
        self.assertTrue(custom.recipe_options(custom.BUDGET_RECIPE)['budgeted_request'])
        self.assertFalse(custom.recipe_options(custom.BUDGET_RECIPE)['compact_schema_rules'])
        self.assertTrue(custom.recipe_options(custom.RECIPE)['compact_schema_rules'])
        for recipe in custom.SUPPORTED_RECIPES[custom.SUPPORTED_RECIPES.index(custom.BUDGET_RECIPE):]:
            self.assertFalse(custom.recipe_options(recipe)['compact_schema_rules'])

    def test_policy_reminders_keep_every_required_source_unit_and_target_or_group(self):
        from services.policy_field_requirements import policy_field_requirements
        from services.policy_source_units import source_units
        from services.policy_target_completeness import target_evidence_candidates
        _, source = public_case('policy-goesan-diapers')
        catalog = source_units(source)
        allowed = {'target_characteristics': ['한부모', '다자녀', '장애/발달지연']}
        requirements = policy_field_requirements(source, catalog)
        targets = target_evidence_candidates(source, allowed)
        frozen = copy.deepcopy((requirements, targets, catalog))
        fields, reminders = policy._request_reminder_references(requirements, targets, catalog)
        self.assertEqual((requirements, targets, catalog), frozen)
        for field in ('age_detail', 'target_income'):
            self.assertEqual(fields[field], sorted({row['unit_id'] for row in requirements[field]}))
        self.assertEqual(fields['contact'], requirements['contact'])
        self.assertEqual(len(reminders), len(targets))
        lines = source.splitlines(keepends=True)
        texts = {unit['id']: ''.join(lines[unit['start_line'] - 1:unit['end_line']]) for unit in catalog}
        for original, compact in zip(targets, reminders):
            self.assertEqual(compact['labels'], original['labels'])
            self.assertIn(original['quote'], texts[compact['unit_id']])


class PublicRequestAdmission(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.CurrentCustomInputBudget.setUp
    call_db = fixtures.CurrentCustomInputBudget.call_db

    def enqueue_public(self, case, source, *, recipe=None):
        if defaults.read(self.conn) is None:
            defaults.save(self.conn, NewsProfile(defaults.PROFILE_ID, 1, '공개 원문', '전국',
                site_ids=('1',)), expected_revision=0)
        profile = defaults.read(self.conn)
        run = profiles.prepare_run(self.conn, profile.profile_id, expected_revision=profile.revision,
            sites=[dict(id=1, name='공개기관', url='https://example.invalid')])
        return jobs.enqueue(self.conn, run, payload=dict(title=case['title'], link=case['url'],
            site_name=urlsplit(case['url']).hostname, content=source,
            source_page=page_coverage.normalize(case.get('source_page'))),
            model=self.model, recipe=recipe or custom.SHARED_SCHEMA_RECIPE)

    def sdk_reply(self, value):
        self.provider.return_value = types.GenerateContentResponse(candidates=[types.Candidate(
            content=types.Content(parts=[types.Part(text=json.dumps(value, ensure_ascii=False))]),
            finish_reason='STOP')])

    def source_preservation_reply(self):
        # Deliberately unknown interpretation: proves transport/holds, not AI accuracy.
        result = {key: '' for key in custom.response_schema(complete_grounding=True)['required']}
        result.pop('support_detail')
        result.update(is_relevant=True, category='기타', target='미상', applicant_type='unknown',
            applicant_detail='미상', beneficiary_detail='미상', start_age_months=None,
            end_age_months=None, residence=[], life_cycle=[], target_characteristics=[],
            interest_topics=[], life_cycle_evidence=[], residence_evidence=[],
            _support_source_units=[1], fact_evidence={key: '' for key in
                ('eligibility_detail', 'contact', 'deadline', 'is_relevant')})
        return result

    def assert_sdk_size(self, label, source):
        self.provider.assert_awaited_once()
        actual = self.provider.await_args.kwargs
        size = quota.estimate_generation_input_tokens(actual['contents'], actual['config'].response_json_schema)
        self.assertLessEqual(size, 6000)
        self.assertEqual(quota.input_token_budget(self.model), 6000)
        self.assertEqual(quota.status()['recorded_requests_today'], 1)
        print(f'PUBLIC_REQUEST_FIT case={label} chars={len(source)} '
              f'source_bytes={len(source.encode("utf-8"))} request_units={size} budget=6000 SDK_mock=1')
        return actual

    async def test_public_news_701_reaches_sdk_full_source_and_incomplete_attachment_stays_review(self):
        case, source = public_case('news-science-terrarium')
        self.assertEqual(len(source), 701)
        self.sdk_reply(self.source_preservation_reply())
        job = self.enqueue_public(case, source)
        self.assertEqual(await process_job(job, self.client, call_db=self.call_db), 'complete',
                         jobs.get_job(self.conn, job))
        actual = self.assert_sdk_size(case['id'], source)
        transmitted = json.loads(actual['contents'].split('\nSOURCE: ')[1])
        self.assertEqual(transmitted['content'], source)
        output = json.loads(jobs.get_job(self.conn, job)['result_json'])
        self.assertEqual(output['analysis']['support_detail'], source)
        self.assertEqual(output['analysis']['fact_evidence']['support_detail'], source)
        self.assertFalse(output['accepted'])
        self.assertEqual(output['target_status'], 'review')
        self.assertEqual(output['analysis']['source_page']['status'], 'review_required')
        self.assertIn('attachment_review', output['analysis']['source_page']['reasons'])

    async def test_pinned_v17_stays_held_and_cannot_share_new_schema_cache(self):
        case, source = public_case('news-science-terrarium')
        old = self.enqueue_public(case, source, recipe=custom.BUDGET_RECIPE)
        new = self.enqueue_public(case, source)
        self.assertNotEqual(jobs.get_job(self.conn, old)['cache_key'], jobs.get_job(self.conn, new)['cache_key'])
        self.assertEqual(await process_job(old, self.client, call_db=self.call_db), 'failed')
        self.assertEqual(jobs.get_job(self.conn, old)['reason'], 'ai_input_size_review_required')
        self.provider.assert_not_awaited()

    async def test_public_news_3501_holds_without_truncation_or_extra_calls(self):
        case, source = public_case('news-memorial-school')
        self.assertEqual(len(source), 3501)
        job = self.enqueue_public(case, source)
        self.assertEqual(await process_job(job, self.client, call_db=self.call_db), 'failed')
        saved = jobs.get_job(self.conn, job)
        self.assertEqual(saved['reason'], 'ai_input_size_review_required')
        self.assertEqual(json.loads(saved['input_json'])['content'], source)
        self.provider.assert_not_awaited()
        self.assertEqual(quota.status()['recorded_requests_today'], 0)
        self.assertEqual(await process_job(job, self.client, call_db=self.call_db), 'not_claimed')

    async def policy_admission(self, draft):
        import ai_classifier
        case, source = public_case('policy-goesan-diapers')
        self.assertEqual(len(source), 835)
        self.report_policy_request(case, source)
        self.sdk_reply(None)  # Valid abstention after real guarded provider admission.
        review = {}
        with patch.dict(os.environ, {'CLASSIFY_MODEL': self.model}):
            result = await ai_classifier.extract_policy_fields_async(self.client, case['title'], case['url'],
                site_name=urlsplit(case['url']).hostname, content=source, review=review, draft_preview=draft)
        actual = self.assert_sdk_size(case['id'] + ('-draft' if draft else '-check'), source)
        self.assertIsNone(result)
        self.assertEqual(review['code'], 'model_abstained')
        units = json.loads(actual['contents'].split('Units (complete original text): ')[1].split('\nAllowed options:')[0])
        self.assertEqual(''.join(unit['text'] for unit in units), source)
        self.assertEqual(public_case(case['id'])[1], source)

    async def test_public_policy_835_draft_reaches_sdk_without_omitting_conditions(self):
        await self.policy_admission(True)

    async def test_public_policy_835_regular_check_reaches_sdk_without_omitting_conditions(self):
        await self.policy_admission(False)

    def report_policy_request(self, case, source):
        import ai_classifier
        allowed = dict(category=list(ai_classifier.CATEGORIES),
            target_characteristics=['일반', '장애/발달지연', '저소득', '임산부', '보호자', '다문화', '다자녀', '한부모'],
            residence=['전국', '서울시', '도봉구'],
            life_cycle=['임신/출산', '영아', '유아', '학령기', '청소년', '청년', '중장년', '노년', '전생애'])
        prompt, schema = policy.build_policy_extraction_request(source, case['title'], case['url'],
            urlsplit(case['url']).hostname, allowed, compact=True)
        print('PUBLIC_POLICY_PLAN chars={} prompt_bytes={} schema_bytes={} request_units={} budget=6000'.format(
            len(source), len(prompt.encode('utf-8')),
            len(json.dumps(schema, ensure_ascii=False, separators=(',', ':')).encode('utf-8')),
            quota.estimate_generation_input_tokens(prompt, schema)))

    async def test_public_policy_835_grounded_response_survives_actual_field_validation(self):
        import ai_classifier
        from services.policy_field_requirements import policy_field_requirements
        from services.policy_source_units import source_units, verified_unit_fields
        case, source = public_case('policy-goesan-diapers')
        catalog = source_units(source)
        requirements = policy_field_requirements(source, catalog)
        # An explicit fixture response, not simulated model accuracy: complete
        # whole-source prose plus all dedicated age/income units, no paraphrase.
        raw = dict(_source_units=dict(support_detail=[row['id'] for row in catalog],
            eligibility_detail=[row['id'] for row in catalog],
            age_detail=sorted({row['unit_id'] for row in requirements['age_detail']}),
            target_income=sorted({row['unit_id'] for row in requirements['target_income']})),
            category='', target_characteristics=['저소득', '한부모', '다자녀', '장애/발달지연'],
            residence=[], life_cycle=[], contact='', start_age_months=None, end_age_months=None)
        constraint = policy._source_age_constraint(source)
        if constraint:
            raw.update({key: constraint[key] for key in ('start_age_months', 'end_age_months')})
        self.sdk_reply(raw)
        original = copy.deepcopy(raw)
        review = {}
        with patch.dict(os.environ, {'CLASSIFY_MODEL': self.model}):
            result = await ai_classifier.extract_policy_fields_async(self.client, case['title'], case['url'],
                site_name=urlsplit(case['url']).hostname, content=source, review=review, draft_preview=True)
        self.assert_sdk_size(case['id'] + '-validated-fields', source)
        self.assertIsNotNone(result, review)
        self.assertEqual(raw, original)
        self.assertEqual(result['support_detail'], source)
        self.assertEqual(result['eligibility_detail'], source)
        self.assertEqual(result['_source_evidence']['support_detail'], source)
        self.assertEqual(verified_unit_fields(result, source),
            {'support_detail', 'eligibility_detail', 'age_detail', 'target_income'})
        for condition in ('80%', '100%'):
            self.assertIn(condition, result['target_income'])
        self.assertIn('소급 지원 불가', result['eligibility_detail'])
        self.assertIn('www.gov.kr', result['support_detail'])
        self.assertIn('장애/발달지연', result['target_characteristics'])
