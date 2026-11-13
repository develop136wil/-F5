"""Offline positive and adversarial contracts for labeled scalar display."""
import copy
import unittest

from services.news_analysis_safety import assess
from services.news_field_presentation import verified_labeled_display
from services.news_profile import NewsProfile


class LabeledDisplayTests(unittest.TestCase):
    def test_exact_labeled_audience_keeps_full_verified_context(self):
        for quote in ('참가대상\n: 성인', '참가대상: 성인', '참가대상:\n성인',
                      '참여 대상\n성인', '신청 자격：성인', '대상\r\n: 성인'):
            with self.subTest(quote=quote):
                self.assertEqual(verified_labeled_display('eligibility_detail', '성인', quote,
                    '안내\n' + quote + '\n현장 대면'), quote)

    def test_complete_contact_numbers_and_institution_are_preserved(self):
        for quote, value in (
                ('문의:\n02-724-0292, 0239\n(한양도성연구소)', '02-724-0292, 0239'),
                ('문의처: 1522-3690', '1522-3690'),
                ('연락처\n051-241-7859', '051-241-7859'),
                ('전화번호: 02-724-0292 / 02-724-0239\n(공공 지원센터)', '02-724-0292 / 02-724-0239')):
            with self.subTest(quote=quote):
                self.assertEqual(verified_labeled_display('contact', value, quote, quote), quote)

    def test_no_partial_payload_or_disguised_label_is_accepted(self):
        cases = ('참가대상: 성인 제외', '참가대상: 성인만 참가, 미성년자 제외',
                 '참가대상\n성인\n아님', '참가대상: 성인\n무료 대상: 아동',
                 '제외 대상: 성인', '제외 참가대상: 성인', '참가대상 아님: 성인',
                 '참가대상: 성인 및 아동', '참가대상: 만 19세 이상',
                 '기존 참가대상: 성인')
        for quote in cases:
            with self.subTest(quote=quote):
                self.assertIsNone(verified_labeled_display('eligibility_detail', '성인', quote, quote))

    def test_adjacent_negative_context_cannot_be_hidden_by_short_quote(self):
        for source in ('제외 대상:\n참가대상: 성인', '참가대상: 성인\n아님',
                       '참가대상: 성인 제외', '참가대상: 성인\n신청 불가'):
            with self.subTest(source=source):
                self.assertIsNone(verified_labeled_display('eligibility_detail', '성인',
                    '참가대상: 성인', source))

    def test_wrong_expanded_reordered_or_incomplete_telephone_is_held(self):
        quote = '문의:\n02-724-0292, 0239\n(한양도성연구소)'
        for value in ('02-724-0293, 0239', '02-724-0292', '02-724-0292, 02-724-0239',
                      '0239, 02-724-0292', '02-724-0292, 0239 내선 2'):
            with self.subTest(value=value):
                self.assertIsNone(verified_labeled_display('contact', value, quote, quote))

    def test_extra_contact_institution_or_conditions_remain_held(self):
        for quote in ('문의:\n02-724-0292\n신청 불가',
                      '문의:\n02-724-0292\n02-724-0239',
                      '문의: A기관 02-724-0292 / B기관 02-724-0239',
                      '문의:\n02-724-0292\n(연구소)\n평일만 가능',
                      '문의:\n02-724-0292\n(문의 아님)',
                      '문의:\n02-724-0292\n(내선 2)'):
            with self.subTest(quote=quote):
                self.assertIsNone(verified_labeled_display('contact', '02-724-0292', quote, quote))

    def test_unsupported_fields_or_quotes_get_no_equivalence(self):
        quote = '참가대상: 성인'
        self.assertIsNone(verified_labeled_display('support_detail', '성인', quote, quote))
        self.assertIsNone(verified_labeled_display('eligibility_detail', '성인', quote, '참가대상: 아동'))
        self.assertIsNone(verified_labeled_display('eligibility_detail', None, quote, quote))
        self.assertIsNone(verified_labeled_display('eligibility_detail', '', quote, quote))


class LabeledAssessmentTests(unittest.TestCase):
    profile = NewsProfile('labeled-offline', 1, '표시 검증', '전국')

    def analyze(self, quote, value, field='eligibility_detail', **changes):
        source = '개인이 신청합니다.\n' + quote
        data = dict(is_relevant=True, category='복지', target='개인', residence=[], life_cycle=[],
            target_characteristics=[], interest_topics=[], start_age_months=None, end_age_months=None,
            life_cycle_evidence=[], residence_evidence=[], evidence_snippet='',
            applicant_type='individual', applicant_evidence='개인이 신청합니다.', applicant_detail='개인',
            beneficiary_detail='개인', support_detail='', eligibility_detail='', contact='', deadline='',
            fact_evidence={field: quote})
        data[field] = value
        data.update(changes)
        before = copy.deepcopy(data)
        decision = assess(self.profile, data, source)
        self.assertEqual(data, before)
        return decision, source

    def test_normalized_fields_are_idempotent_and_do_not_infer_age(self):
        for field, quote, value in (
                ('eligibility_detail', '참가대상\n: 성인', '성인'),
                ('contact', '문의:\n02-724-0292, 0239\n(한양도성연구소)', '02-724-0292, 0239')):
            with self.subTest(field=field):
                result, source = self.analyze(quote, value, field)
                self.assertEqual(result['analysis'][field], quote)
                self.assertNotIn(field, result['analysis']['unverified_facts'])
                self.assertIsNone(result['analysis']['start_age_months'])
                self.assertIsNone(result['analysis']['end_age_months'])
                repeated = assess(self.profile, result['analysis'], source)
                self.assertEqual(repeated, result)

    def test_prior_withheld_values_are_diagnostics_only(self):
        quote = '참가대상\n: 성인'
        result, _ = self.analyze(quote, '', unverified_facts={'eligibility_detail': '성인'})
        self.assertEqual(result['analysis']['eligibility_detail'], '')
        self.assertEqual(result['target_status'], 'review')
        self.assertEqual(result['analysis']['unverified_facts']['eligibility_detail'], '성인')

    def test_real_restriction_still_prevents_approval(self):
        quote = '참가대상: 성인\n중복 지원 불가'
        result, _ = self.analyze(quote, '성인')
        self.assertFalse(result['accepted'])
        self.assertEqual(result['analysis']['eligibility_detail'], '')
        self.assertIn('omitted_conditions', result['analysis']['unverified_facts'])


if __name__ == '__main__':
    unittest.main()
