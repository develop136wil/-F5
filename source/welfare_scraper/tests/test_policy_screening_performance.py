"""Offline request-local screening parity and deterministic work-count checks.

Run only through the isolated test runner. No external provider is involved;
printed timings are diagnostics, not a flaky wall-clock pass/fail threshold.
"""
import copy
import hashlib
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from services import policy_evidence as evidence, policy_source_plan as plan
from services.news_analysis_safety import _age_evidence


OPTIONS = dict(category=['복지'], residence=['전국', '서울시', '도봉구'],
    target_characteristics=['일반', '장애/발달지연', '저소득', '임산부', '보호자', '다문화', '다자녀', '한부모'],
    life_cycle=['임신/출산', '영아', '유아', '학령기', '청소년', '청년', '중장년', '노년', '전생애'])
CORPUS = Path(__file__).resolve().parents[2] / 'artifacts' / 'ai-holdout-prep-20260913'


def captured_policies():
    manifest = json.loads((CORPUS / 'sources-manifest.json').read_text(encoding='utf-8'))
    for case in manifest['cases']:
        if case['kind'] != 'policy':
            continue
        raw = (CORPUS / case['source_file']).read_bytes()
        if hashlib.sha256(raw).hexdigest() != case['source_sha256']:
            raise AssertionError('Public corpus bytes changed')
        yield case['id'], raw.decode('utf-8')


def independently_screened(source, allowed):
    """Pre-optimization option algorithm: re-derive source evidence per option."""
    result = dict(allowed)
    for field in ('residence', 'target_characteristics'):
        candidates = result.get(field)
        if isinstance(candidates, list):
            if len(candidates) > 100:
                raise plan.PolicySourcePlanError('source_structure_too_large')
            result[field] = [label for label in candidates
                if field not in evidence.structured_policy_issues({field: [label]}, source)]
    labels = result.get('life_cycle')
    if not isinstance(labels, list):
        return result
    if len(labels) > 100:
        raise plan.PolicySourcePlanError('source_structure_too_large')
    bounds, intervals = _age_evidence(source)
    age_context = {}
    if len(intervals) == 1:
        for key, value in zip(('start_age_months', 'end_age_months'), intervals[0]):
            if (key, value) in bounds:
                age_context[key] = value
    result['life_cycle'] = [label for label in labels if 'life_cycle' not in
        evidence.structured_policy_issues(dict(age_context, life_cycle=[label]), source)]
    return result


class RequestLocalPolicyScreening(unittest.TestCase):
    def test_public_sources_and_negative_age_roles_keep_identical_options(self):
        cases = [*captured_policies(),
            ('excluded-role', '지원제외대상\n서울시 장애인 가구\n지원내용\n교통비 3만원 지원'),
            ('provider-role', '제공인력\n만 65세 이상 보호자\n지원내용\n아동 돌봄'),
            ('disjoint-age', '지원대상\n만 5세 이하 또는 만 65세 이상\n지원내용\n교통비 지원'),
            ('geographic-exception', '지원대상\n전국민\n서울시 거주자는 제외\n지원내용\n교통비 지원')]
        for label, source in cases:
            with self.subTest(case=label):
                before = copy.deepcopy(OPTIONS)
                self.assertEqual(plan._source_screened_options(source, OPTIONS),
                                 independently_screened(source, OPTIONS))
                self.assertEqual(OPTIONS, before)

    def test_default_twenty_options_prepare_source_once_not_twenty_times(self):
        source = dict(captured_policies())['policy-goesan-diapers']
        prepare = evidence._structured_policy_source
        started = time.perf_counter()
        with patch.object(evidence, '_structured_policy_source', wraps=prepare) as calls:
            expected = independently_screened(source, OPTIONS)
            old_calls = calls.call_count
        old_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        with patch.object(evidence, '_structured_policy_source', wraps=prepare) as calls:
            actual = plan._source_screened_options(source, OPTIONS)
            new_calls = calls.call_count
        new_ms = (time.perf_counter() - started) * 1000
        self.assertEqual(actual, expected)
        self.assertEqual(old_calls, 20)
        self.assertEqual(new_calls, 1)
        print(f'POLICY_SCREEN_WORK chars={len(source)} old_preparations={old_calls} '
              f'new_preparations={new_calls} old_ms={old_ms:.3f} new_ms={new_ms:.3f}')

    def test_different_requests_and_final_validation_prepare_fresh_evidence(self):
        positive = '지원대상\n서울시 거주 장애인 가구\n지원내용\n교통비 지원'
        negative = '지원제외대상\n서울시 거주 장애인 가구\n지원내용\n교통비 지원'
        prepare = evidence._structured_policy_source
        with patch.object(evidence, '_structured_policy_source', wraps=prepare) as calls:
            first = plan._source_screened_options(positive, OPTIONS)
            second = plan._source_screened_options(negative, OPTIONS)
            self.assertEqual(calls.call_count, 2)
            # A fresh final guard cannot inherit the earlier source's shortlist.
            result = evidence.structured_policy_issues({'target_characteristics': ['장애/발달지연']}, negative)
            self.assertEqual(calls.call_count, 3)
        self.assertIn('장애/발달지연', first['target_characteristics'])
        self.assertNotIn('장애/발달지연', second['target_characteristics'])
        self.assertIn('target_characteristics', result)

    def test_empty_or_oversized_options_do_not_prepare_unneeded_evidence(self):
        with patch.object(evidence, '_structured_policy_source', side_effect=AssertionError('unneeded source work')):
            self.assertEqual(plan._source_screened_options('지원내용\n교통비 지원', {}), {})
            with self.assertRaises(plan.PolicySourcePlanError) as caught:
                plan._source_screened_options('지원내용\n교통비 지원', {'residence': ['전국'] * 101})
            self.assertEqual(caught.exception.code, 'source_structure_too_large')
