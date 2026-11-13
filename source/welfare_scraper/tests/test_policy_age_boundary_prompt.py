"""Pure request/grounding regression, not proof of fresh model compliance.

No ai_classifier/policy_sync, SDK, configuration, database, or network import.
The public frozen response is read unchanged and must retain its original hold.
The request-only tests below do not prove branch-age safety; the separate
test_policy_age_branch_guard suite covers rejection of conflicting bounds.
Preserve all conditions and keep ambiguous numeric ages unknown.
"""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from services.policy_evidence import structured_policy_issues
from services.policy_source_plan import (
    POLICY_REQUEST_FORMAT, MAX_POLICY_PROMPT_CHARS, PolicySourcePlanError,
    build_policy_extraction_request, hydrate_policy_source_ranges,
)
from services.policy_source_units import source_units


ROOT = Path(__file__).resolve().parents[2] / 'artifacts/release-approval-independent-20260914'
SOURCE = ROOT / 'source-text-01/policy-jeonju-hearing.txt'
RESPONSE = ROOT / 'fresh-live-01/policy-jeonju-hearing.sdk-response.json'
SOURCE_SHA = '14a026dbaa082621d47570d2347922f4032d8a2474567db9a2e7d0c96bf8969c'
RESPONSE_SHA = '524846e7e8a318af3eda9fbc8aed648785f5bba71f6d4e1df58a67a8e6a7ba69'


def request(source):
    return build_policy_extraction_request(source, '합성 정책',
        'https://example.invalid/policy', '합성 기관', {})


def frozen():
    source, response = SOURCE.read_bytes(), RESPONSE.read_bytes()
    if hashlib.sha256(source).hexdigest() != SOURCE_SHA or hashlib.sha256(response).hexdigest() != RESPONSE_SHA:
        raise AssertionError('Frozen public inputs changed')
    raw = json.loads(json.loads(response)['candidates'][0]['content']['parts'][0]['text'])
    return source.decode('utf-8'), raw


class PolicyAgeBoundaryPrompt(unittest.TestCase):
    def test_request_version_and_explicit_boundaries(self):
        prompt, _ = request('지원대상\n19세~39세 청년\n지원내용\n교통비 지원')
        self.assertEqual(POLICY_REQUEST_FORMAT, 'policy-source-units-v12-reference-reminders')
        for rule in (
            'N세 미만 -> end_age_months = N*12-1',
            '18세 미만 -> 215, NOT 216',
            'N세 이하 -> end_age_months = (N+1)*12-1',
            '18세 이하 -> 227',
            'N세 이상 -> start_age_months = N*12',
            'N세 초과 -> start_age_months = (N+1)*12',
            'N개월 미만/이하 -> end_age_months = N-1/N',
            'N개월 이상/초과 -> start_age_months = N/N+1',
            '19세~39세 -> 228..479 months',
            'with or without the literal 만', 'NEVER an invented 0',
            'arithmetic rules, NOT source evidence',
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, prompt)

    def test_frozen_invalid_age_values_still_rejected_without_repair(self):
        source, raw = frozen()
        before = copy.deepcopy(raw)
        candidate = hydrate_policy_source_ranges(raw, source)
        self.assertEqual((candidate['start_age_months'], candidate['end_age_months']), (0, 216))
        self.assertEqual(set(structured_policy_issues(candidate, source)),
                         {'start_age_months', 'end_age_months'})
        self.assertEqual(raw, before)
        self.assertIn('18세 미만', candidate['age_detail'])
        self.assertIn('5세 미만 영유아', candidate['age_detail'])
        self.assertIn('장애 미등록자도 가능', candidate['age_detail'])
        frozen()  # Revalidate the unchanged source/response file hashes.

    def test_ordinary_explicit_range_stays_supported(self):
        source = '지원대상: 19세~39세 청년\n지원내용\n교통비 지원'
        self.assertEqual(structured_policy_issues(
            {'start_age_months': 228, 'end_age_months': 479}, source), [])
        self.assertIn('end_age_months', structured_policy_issues(
            {'start_age_months': 228, 'end_age_months': 480}, source))

    def test_existing_exact_year_and_month_boundaries_remain_unchanged(self):
        cases = (
            ('18세 미만', 'end_age_months', 215, 216),
            ('만 18세 미만', 'end_age_months', 215, 216),
            ('18세 이하', 'end_age_months', 227, 215),
            ('19세 이상', 'start_age_months', 228, 229),
            ('19세 초과', 'start_age_months', 240, 228),
            ('생후 18개월 미만', 'end_age_months', 17, 18),
            ('생후 18개월 이하', 'end_age_months', 18, 17),
            ('생후 19개월 이상', 'start_age_months', 19, 20),
            ('생후 19개월 초과', 'start_age_months', 20, 19),
        )
        for clause, field, good, bad in cases:
            source = '지원대상: ' + clause + ' 주민\n지원내용\n교통비 지원'
            with self.subTest(clause=clause):
                self.assertEqual(structured_policy_issues({field: good}, source), [])
                self.assertIn(field, structured_policy_issues({field: bad}, source))

    def test_unstated_lower_is_null_not_zero(self):
        for clause in ('18세 미만', '만 18세 미만', '18세 이하'):
            source = '지원대상: ' + clause + ' 주민\n지원내용\n교통비 지원'
            with self.subTest(clause=clause):
                self.assertEqual(structured_policy_issues({'start_age_months': None}, source), [])
                self.assertIn('start_age_months', structured_policy_issues({'start_age_months': 0}, source))

    def test_branch_exception_instructions_do_not_force_or_repair_scalars(self):
        source, raw = frozen()
        original = copy.deepcopy(raw)
        prompt, schema = request(source)
        for key in ('start_age_months', 'end_age_months'):
            self.assertEqual(schema['properties'][key],
                {'type': ['integer', 'null'], 'minimum': 0, 'maximum': 1800})
        self.assertIn('Multiple age conditions and exceptions must remain COMPLETE in age_detail', prompt)
        self.assertIn('keep BOTH numeric age fields null and preserve ALL age conditions', prompt)
        self.assertIn("do not make 5세 미만 the whole programme's eligibility ceiling", prompt)
        self.assertNotIn('SOURCE-VERIFIED REQUEST CONSTRAINT (not a model prediction):', prompt)
        units = json.loads(prompt.split('[Source units JSON: id and complete original text]\n')[1].split('\n[Allowed')[0])
        self.assertEqual(''.join(row['text'] for row in units), source)
        self.assertEqual(raw, original)

    def test_synthetic_uncertain_scalars_preserve_every_age_condition(self):
        source = ('지원대상\n18세 미만 등록 청각장애인\n'
                  '5세 미만은 의사 소견이 있으면 장애 미등록자도 가능\n'
                  '지원내용\n수술비 100만원 이내 지원')
        all_units = [unit['id'] for unit in source_units(source)]
        raw = {'_source_units': {field: list(all_units) for field in
            ('support_detail', 'eligibility_detail', 'age_detail', 'target_income')},
            'start_age_months': None, 'end_age_months': None}
        before = copy.deepcopy(raw)
        candidate = hydrate_policy_source_ranges(raw, source)
        self.assertEqual(candidate['age_detail'], source)
        self.assertIsNone(candidate['start_age_months'])
        self.assertIsNone(candidate['end_age_months'])
        self.assertEqual(structured_policy_issues(candidate, source), [])
        self.assertEqual(raw, before)

    def test_prompt_plus_schema_cap_is_still_enforced_without_truncation(self):
        source, _ = frozen()
        prompt, schema = request(source)
        total = len(prompt) + len(json.dumps(schema, ensure_ascii=False, separators=(',', ':')))
        self.assertLessEqual(total, MAX_POLICY_PROMPT_CHARS)
        with patch('services.policy_source_plan.MAX_POLICY_PROMPT_CHARS', total - 1):
            with self.assertRaises(PolicySourcePlanError) as caught:
                request(source)
        self.assertEqual(caught.exception.code, 'source_structure_too_large')


if __name__ == '__main__':
    unittest.main()
