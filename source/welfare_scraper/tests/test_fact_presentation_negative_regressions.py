"""Independent offline challenges for cash wording and bound factual clauses.

These are synthetic safety regressions, not measurements of model accuracy.
No model, network, credentials, operational database, or mutable fixture is used.
"""
import unittest

from services.policy_evidence import (
    preserves_quoted_facts,
    quote_matches_source,
    validate_policy_evidence,
)


class NegativePresentationRegressions(unittest.TestCase):
    def test_cash_compounds_do_not_turn_safe_clause_reordering_into_a_ban(self):
        for phrase in ('현금지급', '현금 지급', '지원금 지급', '장려금 지급',
                       '급여 현금지급', '보조금지급'):
            with self.subTest(phrase=phrase):
                quote = '지급일: 매월 25일\n지급방식: ' + phrase + ' (본인 명의 계좌)'
                value = '지급방식: ' + phrase + ' (본인 명의 계좌)\n지급일: 매월 25일'
                self.assertTrue(preserves_quoted_facts(value, quote))
                self.assertTrue(validate_policy_evidence(
                    {'support_detail': value, '_source_evidence': {'support_detail': quote}},
                    quote))

    def test_cash_wording_does_not_mask_a_separate_real_negative(self):
        for predicate in ('금지', '지급 금지', '신청 금지', '신청 불가',
                          '지급하지 않음', '지급되지 않음', '지원받을 수 없음'):
            with self.subTest(predicate=predicate):
                quote = '교육비 현금지급\n' + predicate + '\n교통비 현금지급'
                value = '교육비 현금지급\n교통비 현금지급\n' + predicate
                self.assertFalse(preserves_quoted_facts(value, quote))
                self.assertFalse(validate_policy_evidence(
                    {'support_detail': value, '_source_evidence': {'support_detail': quote}},
                    quote))

    def test_same_characters_cannot_rebind_negative_to_a_different_subject(self):
        cases = (
            ('아동 대상 아님\n청년 대상', '아동 대상\n아님 청년 대상'),
            ('아동 지원 제외\n청년 지원', '아동 지원\n제외 청년 지원'),
            ('방문 신청 불가\n온라인 신청', '방문 신청\n불가 온라인 신청'),
            ('교육비 100만원\n아님 교통비 10만원',
             '교육비 100만원 아님\n교통비 10만원'),
            ('현금 지급 금지\n포인트 지급', '현금 지급\n금지 포인트 지급'),
        )
        for source, rewritten in cases:
            with self.subTest(source=source):
                self.assertEqual(''.join(source.split()), ''.join(rewritten.split()))
                self.assertFalse(preserves_quoted_facts(rewritten, source))
                self.assertFalse(quote_matches_source(rewritten, source))
                # The model cannot evade the source check by making its prose
                # and its fabricated quote agree with each other.
                self.assertFalse(validate_policy_evidence({
                    'support_detail': rewritten,
                    '_source_evidence': {'support_detail': rewritten},
                }, source))

    def test_attached_eligibility_and_benefit_values_cannot_swap(self):
        cases = (
            ('첫째 200만원\n둘째 이상 300만원', '첫째 300만원\n둘째 이상 200만원'),
            ('청년 10만원\n노년 20만원', '청년 20만원\n노년 10만원'),
            ('지원대상: 도봉구 거주자\n제외대상: 비거주자',
             '지원대상: 비거주자\n제외대상: 도봉구 거주자'),
            ('보호자: 신청 가능\n아동: 신청 불가',
             '보호자: 신청 불가\n아동: 신청 가능'),
            ('방문 신청 가능\n이메일 신청 불가', '방문 신청 불가\n이메일 신청 가능'),
            ('신청기간: 9월 10일\n행사기간: 9월 20일',
             '신청기간: 9월 20일\n행사기간: 9월 10일'),
        )
        for source, rewritten in cases:
            with self.subTest(source=source):
                self.assertFalse(preserves_quoted_facts(rewritten, source))
                self.assertFalse(validate_policy_evidence({
                    'support_detail': rewritten,
                    '_source_evidence': {'support_detail': source},
                }, source))

    def test_omitted_restrictions_are_not_approved_by_a_short_correct_quote(self):
        for restriction in ('현금 지급 금지', '다른 지원금과 중복 지원 불가',
                            '본인 신청만 가능', '방문 신청 필요'):
            with self.subTest(restriction=restriction):
                benefit = '교육비 10만원 지원'
                source = benefit + '\n' + restriction
                self.assertFalse(validate_policy_evidence({
                    'support_detail': benefit,
                    '_source_evidence': {'support_detail': benefit},
                }, source))


if __name__ == '__main__':
    unittest.main()
