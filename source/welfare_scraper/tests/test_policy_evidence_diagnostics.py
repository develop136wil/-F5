"""Offline hold explanations: same approval guard, immutable public responses."""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

import ai_classifier
from services.fact_presentation import presentation_clauses
from services.policy_evidence import (
    POLICY_PROSE_FIELDS, POLICY_EVIDENCE_VERSION, POLICY_DIAGNOSTIC_VERSION,
    diagnose_policy_evidence, public_policy_diagnostics,
    quote_matches_source, structured_policy_issues, validate_policy_evidence,
)
from services import policy_sync


def extraction(value, quote=None, **facts):
    return {'support_detail': value,
            '_source_evidence': {'support_detail': value if quote is None else quote}, **facts}


def codes(diagnostic):
    return {item['code'] for item in diagnostic['issues']}


def saved_policy():
    root = Path(__file__).resolve().parents[2] / 'artifacts' / 'ai-fresh-eval-20260910'
    ledger = (root / 'fresh-live-01' / 'ledger.jsonl').read_bytes()
    source = (root / 'fixtures' / 'policy-busan-saving.txt').read_bytes()
    assert hashlib.sha256(ledger).hexdigest() == '52e762bbfaea7d828aaf660904394941f6568aa294ca2c92867f6e7b7eb4a2e8'
    assert hashlib.sha256(source).hexdigest() == '6933628ac88edec28167f99b2d299fda25a06a1da8b90496bff90bb14338fba2'
    response = next(item for item in map(json.loads, ledger.decode('utf-8').splitlines())
                    if item.get('event') == 'response' and item.get('case_id') == 'policy-busan-saving')
    return source.decode('utf-8'), json.loads(response['raw']['text'])


class PolicyDiagnostics(unittest.TestCase):
    def assert_hold(self, value, source, expected):
        before = copy.deepcopy(value)
        diagnostic = diagnose_policy_evidence(value, source)
        self.assertFalse(diagnostic['valid'])
        self.assertFalse(validate_policy_evidence(value, source))
        self.assertIn(expected, codes(diagnostic))
        self.assertEqual(value, before)
        return diagnostic

    def test_valid_complete_and_exact_presentation_equivalences_still_pass(self):
        for source, value in (
            ('교육비 10만원 지원. 방문 신청 필요. 중복 지원 불가.',
             '교육비 10만원 지원. 방문 신청 필요. 중복 지원 불가.'),
            ('지원금 1만원 지급', '지원금 10000원 지급'),
            ('주차 1시간 면제', '주차 60분 면제'),
            ('일반주차장: 1시간 면제 후 50% 할인. 환승주차장: 3시간 면제 후 80% 할인.',
             '환승주차장: 3시간 면제 후 80% 감면. 일반주차장: 1시간 면제 후 50% 감면.'),
        ):
            with self.subTest(source=source):
                result = extraction(value, source)
                self.assertTrue(validate_policy_evidence(result, source))
                diagnostic = diagnose_policy_evidence(result, source)
                self.assertTrue(diagnostic['valid'])
                self.assertEqual(diagnostic['issues'], [])
                self.assertEqual(diagnostic['condition_candidates'], [])

    def test_quote_missing_outside_and_boundary_are_separate(self):
        source = '성인\n아동 아님'
        cases = (
            ({'support_detail': source}, 'source_quote_missing'),
            (extraction(source, '전국민 대상 안내'), 'source_quote_not_found'),
            (extraction('성인 아동\n아님', '성인 아동\n아님'), 'source_quote_boundary_changed'),
            (extraction(source, '성인'), 'source_quote_insufficient'),
        )
        for result, expected in cases:
            with self.subTest(expected=expected):
                diagnostic = self.assert_hold(result, source, expected)
                self.assertEqual(diagnostic['fields'], ['support_detail'])

    def test_presentation_number_unit_and_label_issues_remain_holds(self):
        cases = (
            ('교육비 10만원 지원. 방문 신청 필요.', '교육비 10만원 지원.', 'presentation_not_preserved'),
            ('문서 번호 123 안내', '문서 번호 124 안내', 'number_not_supported'),
            ('지원금 50원 지급', '지원금 50% 지급', 'quantity_unit_changed'),
            ('청년: 1.5만원 지원\n노년: 2.5만원 지원',
             '청년: 2.5만원 지원\n노년: 1.5만원 지원', 'label_quantity_changed'),
            ('대상자 A에게 10만원 지원. 대상자 B는 지원금 지급 금지.',
             '대상자 B에게 10만원 지원. 대상자 A는 지원금 지급 금지.', 'presentation_not_preserved'),
        )
        for source, value, expected in cases:
            with self.subTest(expected=expected):
                diagnostic = self.assert_hold(extraction(value, source), source, expected)
                self.assertEqual(diagnostic['fields'], ['support_detail'])
                self.assertEqual(diagnostic['condition_candidates'], [])

    def test_true_short_quote_cannot_omit_conditions_or_choose_conflicting_income(self):
        short = '교육비 10만원 지원.'
        for omitted in (
            '다른 교육비와 중복 지원 불가.', '방문 신청 필요.',
            '복지로 온라인 신청 또는 행정복지센터 방문 신청.', '온라인 신청만 가능합니다.',
            '가입 시 소득 50% 이하. 유지 시 소득 100% 이하.',
            '추가지원금은 전월에 12일 이상 참여한 가입자에게만 20만원 지원.',
            '현금 지급 금지.',
        ):
            with self.subTest(omitted=omitted):
                source = short + '\n' + omitted
                diagnostic = self.assert_hold(extraction(short), source, 'source_condition_not_preserved')
                self.assertEqual(diagnostic['fields'], list(POLICY_PROSE_FIELDS))
                self.assertGreater(diagnostic['condition_candidate_count'], 0)
                self.assertNotIn(omitted, json.dumps(diagnostic, ensure_ascii=False))
                self.assertTrue(validate_policy_evidence(extraction(source), source))

    def test_unsupported_structured_facts_keep_original_priority(self):
        source = '교육비 10만원 지원.'
        result = extraction(source, residence=['전국'])
        diagnostic = self.assert_hold(result, source, 'structured_fact_unsupported')
        self.assertEqual(diagnostic['fields'], ['residence'])
        self.assertEqual(diagnostic['condition_candidates'], [])

    def test_invalid_objects_and_empty_prose_fail_closed_with_safe_diagnostics(self):
        for result in (None, [], {}, {'support_detail': None}, {'support_detail': []},
                       {'support_detail': 'None'}, {'support_detail': '교육비 지원', '_source_evidence': []}):
            with self.subTest(result=result):
                diagnostic = diagnose_policy_evidence(result, '교육비 지원')
                self.assertFalse(diagnostic['valid'])
                self.assertFalse(validate_policy_evidence(result, '교육비 지원'))
                json.dumps(diagnostic, allow_nan=False)

    def test_condition_diagnostics_are_bounded_and_not_claimed_semantic_omission_counts(self):
        source = '교육비 지원.\n' + '\n'.join(f'검토번호 {i}: 방문 신청 필요.' for i in range(30))
        diagnostic = self.assert_hold(extraction('교육비 지원.'), source, 'source_condition_not_preserved')
        self.assertEqual(diagnostic['condition_candidate_count'], 30)
        self.assertEqual(len(diagnostic['condition_candidates']), 20)
        self.assertTrue(diagnostic['condition_candidates_truncated'])
        self.assertNotIn('검토번호', json.dumps(diagnostic, ensure_ascii=False))

    def test_saved_response_quotes_are_valid_but_actual_conditions_stay_on_hold(self):
        source, result = saved_policy()
        self.assertEqual(structured_policy_issues(result, source), [])
        for quote in result['_source_evidence'].values():
            self.assertTrue(quote_matches_source(quote, source))
        current = self.assert_hold(result, source, 'source_income_criteria_review')
        self.assertIn('source_calendar_age_review', codes(current))
        # Exercise the older coverage gate independently of the new earlier
        # source-review gate; do not drop its original omission assertions.
        with patch('services.policy_source_review.source_criteria_issues', return_value=[]):
            diagnostic = self.assert_hold(result, source, 'source_condition_not_preserved')
        self.assertEqual(codes(diagnostic), {'source_condition_not_preserved'})
        clauses = list(presentation_clauses(source))
        candidates = [clauses[item['clause_index']] for item in diagnostic['condition_candidates']]
        self.assertTrue(any('신청기간' in text for text in candidates))
        self.assertTrue(any('온라인 신청' in text for text in candidates))
        self.assertTrue(any('방문 신청' in text for text in candidates))
        self.assertTrue(any('12일 이상' in text for text in candidates))
        self.assertTrue(any('100% 이하' in text for text in candidates))


class PolicyReviewBoundary(unittest.IsolatedAsyncioTestCase):
    async def test_diagnostic_change_does_not_expire_v6_pending_or_request_new_ai(self):
        self.assertEqual(POLICY_EVIDENCE_VERSION, 'policy-extract-v6-payment-predicate')
        self.assertEqual(policy_sync._EXTRACTION_PROMPT_VERSION, 'policy-extract-v6-payment-predicate')
        self.assertEqual(POLICY_DIAGNOSTIC_VERSION, 1)
        source = '교육비 월 5만원 지원.'
        url = 'https://example.invalid/policy'
        pending = {'fields': [{'field': '지원 내용', 'old_display': '기존', 'new_display': source, 'snapshot_current': True}],
                   'proposal_schema_version': 2,
                   'extraction_meta': {'prompt_version': 'policy-extract-v6-payment-predicate'}}
        row = dict(id=1, page_id='fake-page', title='교육비', db_key='medical',
            notion_fields=json.dumps({'support_detail': '교육비 월 3만원 지원.'}),
            pending_diff=json.dumps(pending), change_status='detected', source_urls=json.dumps([url]),
            source_hashes=json.dumps({url: hashlib.sha256(source.encode()).hexdigest()}),
            source_texts=json.dumps({url: source}), last_extracted_fields=json.dumps(extraction(source)))
        with patch.object(policy_sync, '_active_configured_keys', return_value=['medical']), \
             patch.object(policy_sync, '_get_db_schema', return_value={}), \
             patch.object(policy_sync, '_db_query_strict', new=AsyncMock(return_value=[row])), \
             patch.object(policy_sync, '_db_write_strict', new=AsyncMock()) as write, \
             patch.object(policy_sync, '_fetch_source', new=AsyncMock(return_value=(source, source, False))), \
             patch.object(ai_classifier, 'get_client', return_value=object()), \
             patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock()) as call, \
             patch.object(policy_sync, '_diff_fields_with_embeddings', new=AsyncMock()) as diff:
            actual = await policy_sync.check_policies(['medical'], scraper=object())
        call.assert_not_awaited()
        diff.assert_not_awaited()
        self.assertEqual(actual['changed'], 0)
        sql, _ = write.await_args.args
        self.assertNotIn('pending_diff=', sql)
        self.assertNotIn('notion_fields=', sql)
        self.assertNotIn('last_extracted_fields=', sql)

    async def test_saved_actual_result_returns_accurate_hold_without_retry_or_source_logging(self):
        source, result = saved_policy()
        review = {}
        with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value=result)) as call, \
                patch.object(ai_classifier, 'logger') as logger:
            actual = await ai_classifier.extract_policy_fields_async(object(), '정책',
                'https://example.invalid', content=source, review=review)
        self.assertIsNone(actual)
        call.assert_not_awaited()
        self.assertEqual(review['code'], 'source_criteria_review_required')
        self.assertEqual(set(review['fields']), {'age_detail', 'target_income'})
        self.assertIn('소득 상한', review['note'])
        self.assertIn('기존 내용은 유지', review['note'])
        self.assertNotIn('diagnostics', review)  # No model response was requested.
        self.assertNotIn('1522-3690', str(logger.mock_calls))
        self.assertNotIn('청년내일저축계좌', json.dumps(review, ensure_ascii=False))
        self.assertNotIn('support_detail', review['note'])

    async def test_field_failures_and_structured_contract_preserve_fields(self):
        source = '지원금 50원 지급'
        cases = (
            ({'support_detail': source}, 'source_evidence_missing', ['support_detail']),
            (extraction(source, '교육비 별도 안내'), 'source_evidence_mismatch', ['support_detail']),
            (extraction('지원금 50% 지급', source), 'source_fact_changed', ['support_detail']),
            (extraction(source, residence=['전국']), 'structured_evidence_missing', ['residence']),
        )
        for result, expected, fields in cases:
            with self.subTest(expected=expected), patch.object(ai_classifier, '_call_gemini_json',
                    new=AsyncMock(return_value=result)) as call:
                review = {}
                actual = await ai_classifier.extract_policy_fields_async(object(), '정책',
                    'https://example.invalid', content=source, review=review)
                self.assertIsNone(actual)
                self.assertEqual(call.await_count, 1)
                self.assertEqual(review['code'], expected)
                self.assertEqual(review['fields'], fields)
                self.assertIn('기존 내용은 유지', review['note'])


class PublicPolicyStatus(unittest.TestCase):
    def test_status_summary_distinguishes_incomplete_conditions_and_presentation(self):
        for code, label in (
            ('model_abstained', 'AI 추출 미확정'),
            ('source_conditions_incomplete', '신청·제한 조건 확인 필요'),
            ('source_evidence_mismatch', '표현·근거 대조 필요'),
            ('source_presentation_mismatch', '표현·근거 대조 필요'),
            ('source_fact_changed', '표현·근거 대조 필요'),
            ('source_evidence_boundary_changed', '표현·근거 대조 필요'),
            ('source_evidence_missing', '원문 근거 확인 필요'),
            ('source_too_long', '긴 원문 확인 필요'),
            ('ai_input_review_required', '민감정보 전송 전 확인 필요'),
        ):
            with self.subTest(code=code):
                summary = policy_sync._extraction_review_summary(
                    {'review_count': 1, 'review_notes': [{'code': code}]})
                self.assertIn(label, summary)
                self.assertIn('기존 내용 유지', summary)
                if code != 'source_evidence_missing':
                    self.assertNotIn('원문 근거 확인 필요', summary)

    def test_status_passes_only_bounded_known_code_and_numeric_indexes(self):
        secret = 'never-include-source-or-model-text'
        raw = {
            'valid': True, 'text': secret,
            'issues': [{'code': 'source_condition_not_preserved', 'text': secret},
                       {'code': ['malformed'], 'field': secret}, {'code': secret},
                       {'code': 'presentation_not_preserved', 'field': 'support_detail', 'quote': secret}],
            'condition_candidates': [
                {'clause_index': 3, 'kind': 'application', 'quote': secret},
                {'clause_index': True, 'kind': 'application'},
                {'clause_index': -1, 'kind': 'application'},
                {'clause_index': 100001, 'kind': 'application'},
                {'clause_index': 5, 'kind': secret},
            ],
            'condition_candidate_count': 4,
        }
        safe = public_policy_diagnostics(raw)
        self.assertEqual(safe['condition_candidates'], [{'clause_index': 3, 'kind': 'application'}])
        self.assertEqual(len(safe['issues']), 2)
        self.assertNotIn(secret, json.dumps(safe))
        self.assertNotIn('valid', safe)
        review = {'code': 'source_conditions_incomplete', 'note': '원문 조건 대조 필요',
                  'fields': ['support_detail'], 'diagnostics': raw}
        status = {}
        policy_sync._record_extraction_review(status, '정책', review)
        self.assertEqual(status['review_count'], 1)
        entry = status['review_notes'][0]
        self.assertEqual(entry['code'], review['code'])
        self.assertEqual(entry['note'], review['note'])
        self.assertEqual(entry['fields'], review['fields'])
        self.assertEqual(entry['diagnostics'], safe)
        self.assertNotIn(secret, json.dumps(status))

    def test_public_diagnostics_reject_malformed_values_and_status_remains_bounded(self):
        for value in (None, 'bad', [], {'issues': 'bad', 'condition_candidates': 'bad'},
                      {'issues': [None, {}], 'condition_candidates': [None, {}], 'condition_candidate_count': True}):
            with self.subTest(value=value):
                safe = public_policy_diagnostics(value)
                self.assertLessEqual(len(safe.get('issues', [])), 20)
                self.assertLessEqual(len(safe.get('condition_candidates', [])), 20)
                json.dumps(safe, allow_nan=False)
        source = '교육비 지원.\n' + '방문 신청 필요.\n' * 40
        review = {'code': 'source_conditions_incomplete', 'note': '원문 조건 대조 필요',
                  'fields': [], 'diagnostics': diagnose_policy_evidence(extraction('교육비 지원.'), source)}
        status = {}
        for _ in range(30):
            policy_sync._record_extraction_review(status, '정책', review)
        self.assertEqual(status['review_count'], 30)
        self.assertEqual(len(status['review_notes']), 20)
        self.assertEqual(len(status['review_notes'][0]['diagnostics']['condition_candidates']), 20)


if __name__ == '__main__':
    unittest.main()
