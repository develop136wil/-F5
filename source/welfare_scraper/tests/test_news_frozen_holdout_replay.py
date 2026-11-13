"""Frozen v14 replies stay unchanged; synthetic v15 copying is NOT live quality.

Run only in the guarded audit runner. No live client, request, network, operating
configuration or DB is needed. Never add new purpose citations or v15 selections
to the actual model replies. The third test is a separate hand-authored response.
"""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from services import news_custom_analysis as custom, news_source_selection as selection
from services.news_analysis_safety import assess
from services.news_analysis_worker import validate_result
from services.news_profile import NewsProfile


ARTIFACTS = Path(__file__).resolve().parents[2] / 'artifacts'
PREP = ARTIFACTS / 'ai-holdout-prep-20260913'
PINS = {
    PREP / 'cases.json': '6c49c8886b6b140cc9a5936279a7af821918585a20c4f6f4967c909c122d226e',
    PREP / 'sources/news-memorial-school.txt': 'fabf744aa848e01ab473c4eb341c85367fb1dd89219f929bb4fd397d95e1506e',
    PREP / 'sources/news-library-login.txt': 'e1680094ae72bcc6c8dd0e3166bd5fc25d4d73aed50d14a1213a3258c8bcfaf9',
    ARTIFACTS / 'ai-holdout-live-20260913/fresh-live-01/ledger.jsonl':
        '91e4f35f18f6d29c5cdab391e6e4cc53d485f2ccdae4eb3ce8ebc725e6e1b9a8',
}
FLAGS = ('legacy_input', 'evidence_rules', 'applicant_rules', 'structured_output',
         'grounding_rules', 'complete_grounding_rules', 'explicit_facts_rules',
         'source_span_rules', 'source_unit_rules', 'whole_document_rules')


class FrozenNewsHoldoutReplay(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.frozen = {}
        for path, digest in PINS.items():
            raw = path.read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
            self.frozen[path] = raw
        self.addCleanup(self.assert_frozen_unchanged)
        self.cases = {row['id']: row for row in json.loads(self.frozen[PREP / 'cases.json'])['cases']}
        ledger_path = next(path for path in PINS if path.name == 'ledger.jsonl')
        self.ledger = [json.loads(line) for line in self.frozen[ledger_path].decode('utf-8').splitlines()]
        self.profile = NewsProfile('offline-holdout-preflight', 1, '공개문서 사전 평가', '전국')

    def assert_frozen_unchanged(self):
        for path, raw in self.frozen.items():
            self.assertEqual(path.read_bytes(), raw)

    def fixture(self, case_id):
        case = self.cases[case_id]
        source = self.frozen[PREP / case['source_file']].decode('utf-8')
        self.assertEqual(hashlib.sha256(source.encode('utf-8')).hexdigest(), case['source_sha256'])
        responses = [row for row in self.ledger if row['event'] == 'response' and row.get('case_id') == case_id]
        self.assertEqual(len(responses), 1)
        raw = json.loads(responses[0]['raw']['text'])
        payload = dict(title=case['title'], link=case['url'], site_name='공개기관',
                       content=source, source_page=copy.deepcopy(case['source_page']))
        return source, raw, payload

    async def replay_v14(self, case_id):
        source, raw, payload = self.fixture(case_id)
        original, original_payload = copy.deepcopy(raw), copy.deepcopy(payload)
        options = custom.recipe_options(custom.SHORT_SOURCE_RECIPE)
        self.assertFalse(options['whole_document_rules'])
        # This is the original captured JSON, not a repaired model response.
        with patch('ai_classifier._call_gemini_json', new=AsyncMock(return_value=copy.deepcopy(raw))) as fake:
            output = await custom.classify(object(), self.profile, payload, 'offline-frozen-replay',
                request_attempts=1, **{key: options[key] for key in FLAGS})
        fake.assert_awaited_once()
        validate_result(output)
        custom.validate_facts(output, require_interest_topics=options['require_interest_topics'])
        custom.applicant_status(output, source)
        result = assess(self.profile, output, source, require_source_selection=(
            options['source_unit_rules'] and selection.eligible_payload(payload)))
        self.assertEqual(raw, original)
        self.assertEqual(payload, original_payload)
        self.assertEqual(result['target_status'], 'review')
        self.assertFalse(result['accepted'])
        self.assertTrue(result['review_required'])
        self.assertEqual(result['analysis']['support_detail'], '')
        self.assertIsNone(result['analysis']['start_age_months'])
        self.assertIsNone(result['analysis']['end_age_months'])
        return source, raw, result

    async def test_frozen_memorial_v14_prose_and_attachment_holds_remain(self):
        source, raw, result = await self.replay_v14('news-memorial-school')
        self.assertGreater(len(source), 1200)
        self.assertNotIn('_support_source_units', raw)
        self.assertTrue(raw['support_detail'])
        self.assertNotIn(selection.META_KEY, result['analysis'])
        self.assertEqual(result['analysis']['source_page']['status'], 'review_required')
        self.assertIn('attachment_review', result['analysis']['source_page']['reasons'])
        self.assertIn('support_detail', result['analysis']['unverified_facts'])
        self.assertTrue(result['analysis']['unverified_facts'].get('omitted_conditions'))

    async def test_frozen_library_v14_negative_has_no_new_citation_or_selection(self):
        _, raw, result = await self.replay_v14('news-library-login')
        self.assertIs(raw['is_relevant'], False)
        self.assertEqual(raw['_support_source_units'], [])
        self.assertFalse(raw['fact_evidence']['is_relevant'])
        self.assertEqual(result['analysis']['fact_evidence']['is_relevant'], '')
        self.assertEqual(result['analysis'][selection.META_KEY]['version'], 1)
        self.assertEqual(result['analysis'][selection.META_KEY]['unit_ids'], [])
        self.assertNotEqual(result['filter_reason'], 'not_relevant')

    def test_synthetic_v15_memorial_whole_copy_is_preservation_not_model_quality(self):
        source, actual_raw, captured = self.fixture('news-memorial-school')
        original = copy.deepcopy(actual_raw)
        schema = selection.response_schema(custom.response_schema(complete_grounding=True))
        synthetic = {key: '' for key in schema['required']}
        synthetic.update(is_relevant=True, category='', target='미상', applicant_type='unknown',
            applicant_detail='미상', beneficiary_detail='미상', start_age_months=None,
            end_age_months=None, residence=[], life_cycle=[], target_characteristics=[],
            interest_topics=[], life_cycle_evidence=[], residence_evidence=[],
            _support_source_units=[1], fact_evidence={key: '' for key in
                ('eligibility_detail', 'contact', 'deadline', 'is_relevant')})
        saved_synthetic = copy.deepcopy(synthetic)
        materialized = selection.materialize(synthetic, source, extended=True)
        result = assess(self.profile, materialized, source, whole_document_rules=True,
                        source_payload=captured)
        self.assertEqual(synthetic, saved_synthetic)
        self.assertEqual(actual_raw, original)
        self.assertEqual(result['analysis']['support_detail'], source)
        self.assertEqual(result['analysis']['fact_evidence']['support_detail'], source)
        self.assertEqual(result['analysis'][selection.META_KEY]['version'], 2)
        self.assertEqual(result['target_status'], 'review')
        self.assertFalse(result['accepted'])
        self.assertEqual(result['analysis']['source_page']['status'], 'review_required')
        self.assertIn('attachment_review', result['analysis']['source_page']['reasons'])
