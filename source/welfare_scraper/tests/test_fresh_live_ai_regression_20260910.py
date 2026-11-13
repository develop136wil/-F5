"""Offline replay of four frozen real responses, not a new model-quality trial.

Only pure production validators run. The pre-response gold, full source files,
execution ledger, and raw JSON responses stay immutable; tests must not repair
those fixtures or turn an overall review hold into a successful AI extraction.
"""
import copy
import hashlib
import json
from contextlib import ExitStack
from pathlib import Path
import unittest
from unittest.mock import patch

from services.news_analysis_safety import assess
from services.news_profile import NewsProfile
from services.policy_evidence import quote_matches_source, validate_policy_evidence


ROOT = Path(__file__).resolve().parents[2]
CAMPAIGN = ROOT / 'artifacts/ai-fresh-eval-20260910'
FIXTURES = CAMPAIGN / 'fixtures'
LEDGER = CAMPAIGN / 'fresh-live-01/ledger.jsonl'
CASES = FIXTURES / 'cases.json'
LEDGER_SHA256 = '52e762bbfaea7d828aaf660904394941f6568aa294ca2c92867f6e7b7eb4a2e8'
CASES_SHA256 = '1fc3a1d169e3ab7bbed65e6415782318399a9f4975bb37df59e2be0105d72f38'
POLICY_IDS = {'policy-busan-saving', 'policy-busan-moving'}
NEWS_IDS = {'news-seoul-wall', 'news-busan-jobfair'}
CASE_IDS = POLICY_IDS | NEWS_IDS


def load_frozen_cases():
    """Freeze stored bytes and original AI input; never rewrite either form."""
    hashes = {LEDGER: LEDGER_SHA256, CASES: CASES_SHA256}
    for path, digest in hashes.items():
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise AssertionError('Immutable live fixture changed: ' + path.name)
    bundle = json.loads(CASES.read_bytes().decode('utf-8'))
    if not bundle['selected_before_model_response']:
        raise AssertionError('Expected pre-response gold')
    if bundle['max_total_model_attempts'] != 4 or bundle['retries_allowed']:
        raise AssertionError('Original four-request no-retry campaign changed')
    rows = bundle['cases']
    if len(rows) != 4 or {row['id'] for row in rows} != CASE_IDS:
        raise AssertionError('Expected exactly four original public documents')
    events = [json.loads(line) for line in LEDGER.read_bytes().decode('utf-8').splitlines()]
    plans = [row for row in events if row['event'] == 'plan']
    if len(plans) != 1 or plans[0]['cases'] != rows or plans[0]['cases_sha256'] != CASES_SHA256:
        raise AssertionError('Execution plan does not match frozen gold and inputs')
    sources = {}
    for row in rows:
        case_id = row['id']
        if row['source_file'] != case_id + '.txt' or row['raw_source_file'] != case_id + '.html':
            raise AssertionError('Unexpected fixture path')
        path = FIXTURES / row['source_file']
        html_path = FIXTURES / row['raw_source_file']
        source_bytes = path.read_bytes()
        # This campaign saved LF text bytes matching the exact source hash;
        # unlike the earlier campaign, universal-newline conversion is not used.
        source = source_bytes.decode('utf-8')
        if (hashlib.sha256(source_bytes).hexdigest() != row['source_sha256']
                or len(source) != row['char_count']
                or hashlib.sha256(html_path.read_bytes()).hexdigest() != row['raw_sha256']):
            raise AssertionError('Original source changed: ' + case_id)
        sources[case_id] = source
        hashes[path] = row['source_sha256']
        hashes[html_path] = row['raw_sha256']
    responses = [row for row in events if row['event'] == 'response']
    attempts = [row for row in events if row['event'] == 'attempt_reserved']
    if (len(responses) != 4 or len(attempts) != 4
            or [row['attempt'] for row in attempts] != [1, 2, 3, 4]
            or [row['case_id'] for row in responses] != [row['id'] for row in rows]
            or [row['case_id'] for row in attempts] != [row['case_id'] for row in responses]):
        raise AssertionError('Expected exactly four once-only real responses')
    for row in responses:
        if (row['raw']['candidate_count'] != 1
                or row['raw']['candidates'][0]['finish_reason'] != 'STOP'
                or row['raw']['prompt_block_reason'] is not None):
            raise AssertionError('Original transport completion changed')
    raw = {row['case_id']: json.loads(row['raw']['text']) for row in responses}
    gold = {row['id']: row['gold'] for row in rows}
    return events, raw, sources, gold, hashes


def replay_news(raw, sources, case_id):
    profile = NewsProfile('fresh-public-document-eval', 1, '공개문서 평가', '전국')
    return assess(profile, copy.deepcopy(raw[case_id]), sources[case_id])


class FreshFrozenLiveResponseRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.events, cls.raw, cls.sources, cls.gold, cls.hashes = load_frozen_cases()
        cls.original = copy.deepcopy(cls.raw)

    def setUp(self):
        self.guards = ExitStack()
        self.addCleanup(self.guards.close)
        self.blocked_calls = []
        for target in ('socket.socket.connect', 'socket.socket.connect_ex',
                       'socket.create_connection', 'socket.getaddrinfo', 'sqlite3.connect'):
            self.blocked_calls.append(self.guards.enter_context(patch(
                target, side_effect=AssertionError('Offline replay attempted network or database access'))))

    def tearDown(self):
        for blocked in self.blocked_calls:
            blocked.assert_not_called()
        self.assertEqual(self.raw, self.original)
        for path, digest in self.hashes.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_seoul_correct_recruitment_deadline_survives_unrelated_review_holds(self):
        self.assertEqual(self.raw['news-seoul-wall']['deadline'], '2026-09-03 16:00')
        evidence = self.raw['news-seoul-wall']['fact_evidence']['deadline']
        self.assertEqual(evidence, '모집기간\n: 2026. 8. 24.(월) 10시 ~ 9. 3.(목) 16시')
        decision = replay_news(self.raw, self.sources, 'news-seoul-wall')
        self.assertEqual(decision['analysis']['deadline'], '2026-09-03 16:00')
        self.assertFalse(decision['accepted'])
        self.assertEqual(decision['target_status'], 'review')

    def test_seoul_labelled_eligibility_preserves_original_context_not_inferred_age(self):
        raw = self.raw['news-seoul-wall']
        self.assertEqual(raw['eligibility_detail'], '성인')
        self.assertEqual(raw['fact_evidence']['eligibility_detail'], '참가대상\n: 성인')
        decision = replay_news(self.raw, self.sources, 'news-seoul-wall')
        analysis = decision['analysis']
        # A narrow field-label normalization restores the FULL verified quote,
        # not a model-repaired scalar or a guessed numeric adult age threshold.
        self.assertEqual(analysis['eligibility_detail'], raw['fact_evidence']['eligibility_detail'])
        self.assertIsNone(analysis['start_age_months'])
        self.assertIsNone(analysis['end_age_months'])
        self.assertNotIn('eligibility_detail', analysis.get('unverified_facts', {}))
        self.assertFalse(decision['accepted'])

    def test_seoul_contact_retains_exact_numbers_and_the_quoted_institution(self):
        raw = self.raw['news-seoul-wall']
        self.assertEqual(raw['contact'], '02-724-0292, 0239')
        quote = '문의:\n02-724-0292, 0239\n(한양도성연구소)'
        self.assertEqual(raw['fact_evidence']['contact'], quote)
        self.assertIn(quote, self.sources['news-seoul-wall'])
        analysis = replay_news(self.raw, self.sources, 'news-seoul-wall')['analysis']
        # Never drop the institution, swap numbers, or invent a full second
        # telephone number from the source's abbreviated extension.
        self.assertEqual(analysis['contact'], quote)
        self.assertNotIn('contact', analysis.get('unverified_facts', {}))

    def test_seoul_missing_application_channel_is_not_repaired_by_other_field_recovery(self):
        decision = replay_news(self.raw, self.sources, 'news-seoul-wall')
        analysis = decision['analysis']
        self.assertFalse(decision['accepted'])
        self.assertEqual(decision['target_status'], 'review')
        missing = analysis['unverified_facts'].get('omitted_conditions', [])
        self.assertTrue(any('서울시 공공서비스예약' in row and '선착순 마감' in row for row in missing))
        self.assertNotIn('서울시 공공서비스예약', analysis['support_detail'])

    def test_busan_false_residence_and_typo_are_not_approved_as_source_facts(self):
        raw = self.raw['news-busan-jobfair']
        self.assertEqual(raw['residence'], ['부산광역시'])
        self.assertIn('청년권 우수기업', raw['support_detail'])
        self.assertNotIn('청년권 우수기업', self.sources['news-busan-jobfair'])
        self.assertFalse(quote_matches_source(raw['fact_evidence']['is_relevant'],
                                             self.sources['news-busan-jobfair']))
        decision = replay_news(self.raw, self.sources, 'news-busan-jobfair')
        analysis = decision['analysis']
        self.assertEqual(analysis['residence'], [])
        self.assertEqual(analysis['support_detail'], '')
        self.assertEqual(analysis['unverified_facts']['support_detail'], raw['support_detail'])
        self.assertIn('is_relevant', analysis['unverified_facts'])
        self.assertFalse(decision['accepted'])
        self.assertEqual(decision['target_status'], 'review')
        # The AI left the date blank. Validator repair must not invent a new
        # AI deadline (especially a made-up 00:00 time) from this replay.
        self.assertEqual(raw['deadline'], '')
        self.assertEqual(analysis['deadline'], '')

    def test_policies_with_actual_omissions_remain_blocked_without_editing_raw_output(self):
        omissions = {
            'policy-busan-saving': ('online_channel', 'visit_channel', 'self_support_extra_payment'),
            'policy-busan-moving': ('exclude_owner_parent_home', 'prior_support_exception', 'no_simple_receipt'),
        }
        for case_id, fact_ids in omissions.items():
            with self.subTest(case_id=case_id):
                raw = self.raw[case_id]
                prose = '\n'.join(raw.get(key, '') for key in
                                  ('support_detail', 'eligibility_detail', 'age_detail', 'target_income'))
                facts = {row['id']: row for row in self.gold[case_id]['facts']}
                for fact_id in fact_ids:
                    quote = facts[fact_id]['quote']
                    self.assertIn(quote, self.sources[case_id])
                    self.assertNotIn(quote, prose)
                self.assertFalse(validate_policy_evidence(copy.deepcopy(raw), self.sources[case_id]))

    def test_four_review_holds_are_not_relabelled_as_four_successful_ai_analyses(self):
        policy = {case_id: validate_policy_evidence(copy.deepcopy(self.raw[case_id]), self.sources[case_id])
                  for case_id in POLICY_IDS}
        news = {case_id: replay_news(self.raw, self.sources, case_id) for case_id in NEWS_IDS}
        self.assertEqual(policy, dict.fromkeys(POLICY_IDS, False))
        self.assertEqual({case_id: row['target_status'] for case_id, row in news.items()},
                         dict.fromkeys(NEWS_IDS, 'review'))
        self.assertEqual(sum(policy.values()) + sum(row['accepted'] for row in news.values()), 0)


if __name__ == '__main__':
    unittest.main()
