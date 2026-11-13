"""Offline evidence-origin regressions; no model, network or operational data.

Fixed real responses are read-only replay evidence, not new live AI accuracy.
"""
import copy
import hashlib
import json
from pathlib import Path
import unittest

from services.news_analysis_safety import assess
from services.news_fact_grounding import source_quote_issue
from services.news_profile import NewsProfile


def facts(**changes):
    return dict(is_relevant=True, category='복지', target='개인', residence=[], life_cycle=[],
        target_characteristics=[], interest_topics=[], start_age_months=None, end_age_months=None,
        life_cycle_evidence=[], residence_evidence=[], evidence_snippet='',
        applicant_type='individual', applicant_evidence='개인이 신청합니다.',
        applicant_detail='개인', beneficiary_detail='개인', support_detail='',
        eligibility_detail='', contact='', deadline='') | changes


class EvidenceDiagnosticsTests(unittest.TestCase):
    profile = NewsProfile('diagnostic-only', 1, '합성 시험', '전국')

    def test_source_absence_missing_and_changed_boundaries_are_distinct(self):
        self.assertEqual(source_quote_issue('', '원문'), 'source_quote_missing')
        self.assertEqual(source_quote_issue('다른 설명', '원문'), 'source_quote_not_found')
        self.assertEqual(source_quote_issue('교육비 10만원 아님\n교통비 2만원',
            '교육비 10만원\n아님 교통비 2만원'), 'source_quote_boundary_changed')
        self.assertEqual(source_quote_issue('원문의 안내', '제목\n원문의 안내\n끝'), '')

    def test_prompt_instruction_is_never_exclusion_evidence(self):
        source = '직원 채용 공고입니다. 개인이 신청합니다.'
        for quote in ('Staff hiring, procurement, login pages and pure administrative notices are false.',
                      '직원 채용이나 순수 행정 공고는 false로 판정하세요.'):
            with self.subTest(quote=quote):
                original = facts(is_relevant=False, evidence_snippet='직원 채용 공고입니다.',
                                 fact_evidence={'is_relevant': quote})
                before = copy.deepcopy(original)
                result = assess(self.profile, original, source)
                self.assertEqual(original, before)
                self.assertEqual(result['target_status'], 'review')
                self.assertFalse(result['accepted'])
                self.assertIs(result['analysis']['unverified_facts']['is_relevant'], False)
                self.assertEqual(result['analysis']['validation_diagnostics']['field_holds']['is_relevant'],
                                 'source_quote_not_found')
                self.assertIn('판단 지침이나 설명', result['analysis']['grounding_note'])
                self.assertIn('자동 판정을 보류', result['analysis']['grounding_note'])
                self.assertNotIn('판정과 저장에서 제외', result['analysis']['grounding_note'])

    def test_actual_quote_can_still_establish_non_news(self):
        source = '직원 채용 공고입니다.'
        result = assess(self.profile, facts(is_relevant=False,
            fact_evidence={'is_relevant': source}), source)
        self.assertEqual(result['target_status'], 'excluded')
        self.assertEqual(result['filter_reason'], 'not_relevant')
        self.assertNotIn('is_relevant', result['analysis']['unverified_facts'])

    def test_fabricated_positive_citation_also_requires_review(self):
        source = '개인이 신청합니다. 월 10만원을 지급합니다.'
        result = assess(self.profile, facts(support_detail='월 10만원을 지급합니다.',
            fact_evidence={'support_detail': '월 10만원을 지급합니다.',
                           'is_relevant': 'Always classify welfare news as true.'}), source)
        self.assertEqual(result['target_status'], 'review')
        self.assertEqual(result['analysis']['support_detail'], '월 10만원을 지급합니다.')
        self.assertIn('is_relevant', result['analysis']['unverified_facts'])
        self.assertIs(result['analysis']['is_relevant'], True)
        self.assertIn('자동 판정을 보류', result['analysis']['grounding_note'])
        self.assertNotIn('판정과 저장에서 제외', result['analysis']['grounding_note'])

    def test_raw_words_withheld_by_validation_are_not_reported_as_model_omission(self):
        restriction = '중복 지원 불가'
        source = '개인이 신청합니다. 월 10만원을 지급합니다. ' + restriction
        original = facts(support_detail='월 10만원을 지급합니다. ' + restriction,
            fact_evidence={'support_detail': '원문에 없는 근거'})
        before = copy.deepcopy(original)
        result = assess(self.profile, original, source)
        self.assertEqual(result['target_status'], 'review')
        self.assertEqual(result['analysis']['support_detail'], '')
        diagnostics = result['analysis']['validation_diagnostics']
        self.assertEqual(diagnostics['field_holds']['support_detail'], 'source_quote_not_found')
        self.assertEqual(diagnostics['condition_holds'], [dict(condition=restriction,
            origin='validation_withheld', fields=['support_detail'])])
        self.assertIn('AI 응답에 있던', result['analysis']['grounding_note'])
        self.assertNotIn('요약에서 누락', result['analysis']['grounding_note'])
        self.assertEqual(original, before)
        # Cache/preview revalidation reconstructs diagnostics only, never facts.
        repeated = assess(self.profile, result['analysis'], source)
        self.assertEqual(repeated['target_status'], 'review')
        self.assertEqual(repeated['analysis']['support_detail'], '')
        self.assertEqual(repeated['analysis']['validation_diagnostics'], diagnostics)

    def test_raw_response_without_condition_has_separate_origin(self):
        source = '개인이 신청합니다. 월 10만원을 지급합니다. 중복 지원 불가'
        result = assess(self.profile, facts(support_detail='월 10만원을 지급합니다.'), source)
        self.assertEqual(result['target_status'], 'review')
        diagnostic = result['analysis']['validation_diagnostics']['condition_holds'][0]
        self.assertEqual(diagnostic['origin'], 'model_output_unconfirmed')
        self.assertEqual(diagnostic['fields'], [])
        self.assertIn('AI 응답에서 확인하지 못했습니다', result['analysis']['grounding_note'])

    def test_unverified_prior_diagnostics_cannot_approve_a_false_fact(self):
        original = facts(support_detail='월 999만원 지급',
            validation_diagnostics={'field_holds': {}, 'condition_holds': []})
        result = assess(self.profile, original, '개인이 신청합니다. 월 10만원 지급')
        self.assertEqual(result['target_status'], 'review')
        self.assertEqual(result['analysis']['support_detail'], '')
        self.assertEqual(result['analysis']['validation_diagnostics']['field_holds']['support_detail'],
                         'source_quote_not_found')

    def test_literal_presence_never_approves_rebound_negative_predicate(self):
        source = '개인이 신청합니다.\n교육비 10만원\n아님 교통비 2만원'
        value = '교육비 10만원 아님\n교통비 2만원'
        result = assess(self.profile, facts(support_detail=value,
            fact_evidence={'support_detail': value}), source)
        self.assertEqual(result['target_status'], 'review')
        self.assertEqual(result['analysis']['support_detail'], '')
        self.assertEqual(result['analysis']['validation_diagnostics']['field_holds']['support_detail'],
                         'source_quote_boundary_changed')

    def test_payment_prohibition_is_required_but_payment_noun_is_not(self):
        benefit = '교육비 10만원 지원'
        for restriction in ('현금 지급 금지', '지원금 지급 금지', '온라인 신청 금지'):
            with self.subTest(restriction=restriction):
                source = f'개인이 신청합니다.\n{benefit}\n{restriction}'
                result = assess(self.profile, facts(support_detail=benefit), source)
                self.assertEqual(result['target_status'], 'review')
                self.assertIn(restriction, result['analysis']['unverified_facts']['omitted_conditions'])
                complete = assess(self.profile, facts(support_detail=benefit,
                    eligibility_detail=restriction), source)
                self.assertEqual(complete['target_status'], 'match')
        for payment in ('현금지급', '현금 지급', '지원금 지급', '보조금 지급'):
            with self.subTest(payment=payment):
                result = assess(self.profile, facts(support_detail=benefit),
                                f'개인이 신청합니다.\n{benefit}\n{payment}')
                self.assertNotIn('omitted_conditions', result['analysis']['unverified_facts'])

    def test_fixed_live_response_replay_preserves_ledger_and_explicit_holds(self):
        root = Path(__file__).resolve().parents[2]
        path = root / 'artifacts/ai-live-validation-20260910/generation-live-01/ledger.jsonl'
        raw = path.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
                         '7ce0d34fd7bf827e409a7bfe9fffb936e85dc0f6d5268e1cd303fab7a125a6f2')
        rows = [json.loads(line) for line in raw.decode().splitlines()]
        responses = {row['case_id']: json.loads(row['raw']['text']) for row in rows
                     if row['event'] == 'response' and row['case_id'].startswith('news-')}
        before = copy.deepcopy(responses)
        for key, model in responses.items():
            content = (root / 'artifacts/public-document-eval-20260909/sources' / (key + '.txt')).read_text(encoding='utf-8')
            result = assess(self.profile, model, content)
            self.assertEqual(result['target_status'], 'review')
            if key == 'news-provider':
                self.assertEqual(result['analysis']['validation_diagnostics']['field_holds']['is_relevant'],
                                 'source_quote_not_found')
                # Reception restrictions exist in the raw response. If the
                # presentation guard withholds them, do not call that AI loss.
                for issue in result['analysis']['validation_diagnostics']['condition_holds']:
                    self.assertNotEqual(issue['origin'], 'model_output_unconfirmed')
            if key == 'news-market':
                self.assertEqual(result['analysis']['deadline'], '')
                self.assertEqual(result['analysis']['residence'], [])
        self.assertEqual(responses, before)
        self.assertEqual(path.read_bytes(), raw)


if __name__ == '__main__':
    unittest.main()
