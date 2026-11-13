"""Pure offline replay of five immutable 2026-09-10 real model responses.

This tests changed validators, not fresh model quality. There are no API calls,
quota accesses, operational data reads, or fixture writes. Original source,
gold, ledger and raw responses are never normalized or otherwise rewritten.
"""
import copy
import hashlib
import json
from pathlib import Path
from contextlib import ExitStack
import unittest
from unittest.mock import patch

from services.policy_evidence import (
    quote_matches_source,
    structured_policy_issues,
    validate_policy_evidence,
)
from services.news_analysis_safety import assess
from services.news_profile import NewsProfile


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / 'artifacts/public-document-eval-20260909'
LEDGER = ROOT / 'artifacts/ai-live-validation-20260910/generation-live-01/ledger.jsonl'
LEDGER_SHA256 = '7ce0d34fd7bf827e409a7bfe9fffb936e85dc0f6d5268e1cd303fab7a125a6f2'
GOLD_SHA256 = 'e7794d83bf0c0025e75a5de43711199bb1e43debba52755af8856385b228005d'
# Snapshot files were saved with Windows CRLF; the recorded original AI input
# used Path.read_text's universal-newline decoding. Freeze both byte storage and
# the exact original prompt text instead of mistaking the two hashes for one.
SOURCE_BYTES_SHA256 = {
    'policy-child': '92aaf028afa8df7700d2aca634377c70a32297f3a25b5280fe042e8fa6e7562d',
    'policy-first': '3b1b5818c780931fb90ff5c8de50e4c5ff9d529d99b5b10bf7c3aa99d9eb43ab',
    'policy-rent': '704767490f23afd78872d8cc4f3f8be99164ab43386eb3618d7898b551845d27',
    'news-provider': '7334f0828e40a5e251394d04145d18222eae2e6f230137bc2494e47ff49ae387',
    'news-market': 'b5f776658a2e362f74034a41cd25f90d6cccdcaed9cd073682f70987fc0af896',
    'news-safety': 'cc5053c0e85b5e972f505abab9c1c21832715a26a0418ecafb021b77247b66dd',
}
POLICY_CASES = {'policy-child', 'policy-first', 'policy-rent'}
NEWS_CASES = {'news-provider', 'news-market'}


def load_frozen_cases():
    """Fail before replay if any immutable fixture has changed."""
    raw_ledger = LEDGER.read_bytes()
    if hashlib.sha256(raw_ledger).hexdigest() != LEDGER_SHA256:
        raise AssertionError('Live ledger changed; never update its expected hash to make a test pass')
    events = [json.loads(line) for line in raw_ledger.decode('utf-8').splitlines()]
    plans = [row for row in events if row['event'] == 'plan']
    if len(plans) != 1:
        raise AssertionError('Expected exactly one immutable execution plan')
    plan = plans[0]
    gold_raw = (FIXTURES / 'gold.json').read_bytes()
    if (hashlib.sha256(gold_raw).hexdigest() != GOLD_SHA256
            or plan['gold_sha256'] != GOLD_SHA256):
        raise AssertionError('Gold changed')
    gold = {row['id']: row for row in json.loads(gold_raw)['cases']}
    sources, hashes = {}, {LEDGER: LEDGER_SHA256, FIXTURES / 'gold.json': GOLD_SHA256}
    for case in plan['cases']:
        case_id = case['id']
        if case_id not in POLICY_CASES | NEWS_CASES | {'news-safety'}:
            raise AssertionError('Unexpected public fixture identifier')
        path = FIXTURES / 'sources' / (case_id + '.txt')
        source_bytes = path.read_bytes()
        source = path.read_text(encoding='utf-8')
        if (hashlib.sha256(source_bytes).hexdigest() != SOURCE_BYTES_SHA256[case_id]
                or hashlib.sha256(source.encode('utf-8')).hexdigest() != case['source_sha256']
                or len(source) != case['source_chars'] or case['gold'] != gold[case_id]):
            raise AssertionError('Public source or planned gold changed: ' + case_id)
        sources[case_id] = source
        hashes[path] = SOURCE_BYTES_SHA256[case_id]
    responses = [row for row in events if row['event'] == 'response']
    raw = {row['case_id']: json.loads(row['raw']['text']) for row in responses}
    if len(responses) != 5 or set(raw) != POLICY_CASES | NEWS_CASES or len(sources) != 6:
        raise AssertionError('Expected five real responses and one separate zero-call safety fixture')
    return events, raw, sources, hashes


def replay(raw, sources):
    """Pass independent copies to production pure functions; retain raw evidence."""
    policy = {case_id: validate_policy_evidence(copy.deepcopy(raw[case_id]), sources[case_id])
              for case_id in sorted(POLICY_CASES)}
    profile = NewsProfile('public-document-eval', 1, '공개문서 평가', '전국')
    news = {case_id: assess(profile, copy.deepcopy(raw[case_id]), sources[case_id])
            for case_id in sorted(NEWS_CASES)}
    return policy, news


class FrozenLiveResponseRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.events, cls.raw, cls.sources, cls.hashes = load_frozen_cases()
        cls.original = copy.deepcopy(cls.raw)

    def setUp(self):
        self.guards = ExitStack()
        self.addCleanup(self.guards.close)
        self.blocked_calls = []
        for target in ('socket.socket.connect', 'socket.socket.connect_ex',
                       'socket.create_connection', 'socket.getaddrinfo', 'sqlite3.connect'):
            self.blocked_calls.append(self.guards.enter_context(patch(
                target, side_effect=AssertionError('Pure replay attempted network or database access'))))

    def tearDown(self):
        for blocked in self.blocked_calls:
            blocked.assert_not_called()
        self.assertEqual(self.raw, self.original)
        for path, digest in self.hashes.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_actual_child_cash_response_is_preserved_without_editing_model_output(self):
        child = copy.deepcopy(self.raw['policy-child'])
        self.assertIn('현금', child['support_detail'])
        self.assertIn('\n', child['support_detail'])
        self.assertNotIn('\\n', child['support_detail'])
        self.assertEqual((child['start_age_months'], child['end_age_months']), (0, 107))
        self.assertEqual(structured_policy_issues(child, self.sources['policy-child']), [])
        self.assertTrue(validate_policy_evidence(child, self.sources['policy-child']))
        self.assertEqual(child, self.raw['policy-child'])

    def test_first_benefit_and_rent_keep_unsupported_structured_facts_on_hold(self):
        first = copy.deepcopy(self.raw['policy-first'])
        self.assertIsNone(first['start_age_months'])
        self.assertIsNone(first['end_age_months'])
        self.assertIn('life_cycle', structured_policy_issues(first, self.sources['policy-first']))
        self.assertFalse(validate_policy_evidence(first, self.sources['policy-first']))
        rent = copy.deepcopy(self.raw['policy-rent'])
        self.assertIn('residence', structured_policy_issues(rent, self.sources['policy-rent']))
        self.assertIn('target_characteristics', structured_policy_issues(rent, self.sources['policy-rent']))
        self.assertFalse(validate_policy_evidence(rent, self.sources['policy-rent']))

    def test_market_invented_deadline_and_residence_never_return_to_approved_fields(self):
        raw = self.raw['news-market']
        self.assertEqual(raw['deadline'], '2026-09-15 00:00')
        self.assertTrue(raw['residence'])
        _, news = replay(self.raw, self.sources)
        decision = news['news-market']
        self.assertEqual(decision['target_status'], 'review')
        self.assertFalse(decision['accepted'])
        self.assertEqual(decision['analysis']['deadline'], '')
        self.assertEqual(decision['analysis']['residence'], [])

    def test_provider_instruction_cannot_be_used_as_an_original_source_quote(self):
        raw = self.raw['news-provider']
        instruction = raw['fact_evidence']['is_relevant']
        self.assertIn('Staff hiring', instruction)
        self.assertNotIn(instruction, self.sources['news-provider'])
        self.assertFalse(quote_matches_source(instruction, self.sources['news-provider']))
        _, news = replay(self.raw, self.sources)
        decision = news['news-provider']
        self.assertEqual(decision['target_status'], 'review')
        self.assertFalse(decision['accepted'])
        # Its correct deadline should not be discarded just because another
        # independent field contains a fabricated citation.
        self.assertEqual(decision['analysis']['deadline'], '2025-12-24 18:00')

    def test_summary_is_one_validator_recovery_and_four_holds_not_new_model_accuracy(self):
        policy, news = replay(self.raw, self.sources)
        self.assertEqual(policy, {'policy-child': True, 'policy-first': False, 'policy-rent': False})
        self.assertEqual({key: row['target_status'] for key, row in news.items()},
                         {'news-market': 'review', 'news-provider': 'review'})
        self.assertEqual(sum(policy.values()) + sum(row['accepted'] for row in news.values()), 1)

    def test_original_privacy_hold_stays_separate_from_the_five_real_responses(self):
        self.assertNotIn('news-safety', self.raw)
        records = [row for row in self.events
                   if row['event'] == 'case_result' and row['case_id'] == 'news-safety']
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['attempts'], 0)
        self.assertEqual(records[0]['outcome'], 'preflight_hold')
        self.assertEqual(records[0]['reason_codes'], ['email_address'])


if __name__ == '__main__':
    unittest.main()
