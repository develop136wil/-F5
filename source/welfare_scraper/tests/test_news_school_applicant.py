"""Offline school/individual application regressions, no model calls."""
import copy
import hashlib
import json
from pathlib import Path
import unittest

from services.news_applicant_context import school_group_application_context
from services.news_analysis_safety import applicant_status


def result(role, quote):
    return {'applicant_type': role, 'applicant_evidence': quote}


class SchoolApplicantContext(unittest.TestCase):
    def test_teacher_authentication_for_one_class_is_review_not_personal_match(self):
        quote = '교육 접수는 담임선생님 한 분의 본인 인증을 통해 1개 학급만 신청할 수 있습니다.'
        for role in ('individual', 'proxy', 'organization'):
            with self.subTest(role=role):
                facts = result(role, quote)
                before = copy.deepcopy(facts)
                self.assertTrue(school_group_application_context(quote))
                self.assertEqual(applicant_status(facts, quote), 'review')
                self.assertEqual(facts, before)

    def test_cropped_personal_quote_does_not_remove_full_source_group_route(self):
        quote = '본인 인증으로 신청합니다.'
        source = quote + '\n학급단체만 신청가능합니다.'
        self.assertEqual(applicant_status(result('proxy', quote), source), 'review')

    def test_explicit_class_and_school_unit_routes_are_review_only(self):
        for group in ('학급단체만 신청가능합니다.', '학교 단체로 신청합니다.',
                      '학교 단위로 접수합니다.', '학급별 접수', '학교만 신청가능',
                      '학교가 신청합니다.', '한 개 학급만 신청할 수 있습니다.',
                      '신청주체: 학교단체'):
            with self.subTest(group=group):
                source = group + '\n본인 인증으로 신청합니다.'
                self.assertTrue(school_group_application_context(source))
                self.assertEqual(applicant_status(result('proxy', '본인 인증으로 신청합니다.'), source), 'review')

    def test_parent_proxy_and_child_individual_applications_stay_match(self):
        for role, quote in (
                ('proxy', '보호자가 자녀를 대신하여 신청합니다.'),
                ('proxy', '부모가 아동의 교육비를 대신 신청합니다.'),
                ('individual', '아동 개인이 신청합니다.'),
                ('individual', '개인이 본인 인증 후 신청합니다.'),
                ('proxy', '보호자가 자녀의 학교에 신청합니다.')):
            with self.subTest(quote=quote):
                self.assertFalse(school_group_application_context(quote))
                self.assertEqual(applicant_status(result(role, quote), quote), 'match')

    def test_teachers_own_benefits_are_not_class_group_applications(self):
        for quote in ('교사가 본인의 연수비를 신청합니다.',
                      '담임선생님이 본인의 교통비를 신청합니다.',
                      '한 개 학급만 담당하는 교사가 본인의 지원금을 신청합니다.'):
            with self.subTest(quote=quote):
                self.assertFalse(school_group_application_context(quote))
                self.assertEqual(applicant_status(result('individual', quote), quote), 'match')

    def test_school_as_personal_choice_object_is_not_an_institution_applicant(self):
        for quote in ('학생 개인은 통학할 한 개의 학교만 신청합니다.',
                      '개인이 희망하는 학교만 신청합니다.'):
            with self.subTest(quote=quote):
                self.assertFalse(school_group_application_context(quote))
                self.assertEqual(applicant_status(result('individual', quote), quote), 'match')
        self.assertTrue(school_group_application_context('학교만 신청가능합니다.'))

    def test_school_venue_topic_and_recipient_words_do_not_establish_applicant(self):
        quote = '개인이 본인 인증 후 신청합니다.'
        for other in ('교육 장소: 초등학교 강당', '교육 주제: 학교와 학급 생활',
                      '교육대상: 초등학교 3~6학년', '담임교사와 학부모를 위한 행사',
                      '학교에서 열리는 행사입니다.', '교육인원: 한 학급'):
            with self.subTest(other=other):
                source = other + '\n' + quote
                self.assertFalse(school_group_application_context(source))
                self.assertEqual(applicant_status(result('individual', quote), source), 'match')

    def test_group_and_personal_routes_remain_review_even_with_personal_quote(self):
        quote = '개인이 본인 인증으로 신청합니다.'
        source = '학교단체만 신청가능합니다.\n별도 개인접수\n' + quote
        for role in ('individual', 'proxy', 'mixed'):
            with self.subTest(role=role):
                self.assertEqual(applicant_status(result(role, quote), source), 'review')

    def test_negative_group_route_does_not_override_separate_personal_route(self):
        quote = '개인이 신청합니다.'
        for group in ('학교단체는 신청할 수 없습니다.', '학급단체 신청불가',
                      '학교단체만 신청가능한 것이 아닙니다.',
                      '학급단체 신청\n\n미접수', '신청 제외 대상:\n학급단체 신청',
                      '학급단체 신청과 달리 개인 접수가 가능합니다.'):
            with self.subTest(group=group):
                source = group + '\n안내\n' + quote
                self.assertFalse(school_group_application_context(source))
                self.assertEqual(applicant_status(result('individual', quote), source), 'match')

    def test_comparison_document_and_prior_examples_are_not_current_group_route(self):
        quote = '개인이 신청합니다.'
        for other in ('지난해에는 학교단체만 신청가능했습니다.',
                      '예시: 학급단체만 신청가능', '참고자료\n학급단체 신청',
                      '제출서류\n학급단체 신청서', '교육주제\n학교단체 신청',
                      '학교단체 신청과 비교하여 개인 접수를 설명합니다.'):
            with self.subTest(other=other):
                source = other + '\n안내\n' + quote
                self.assertFalse(school_group_application_context(source))
                self.assertEqual(applicant_status(result('individual', quote), source), 'match')

    def test_existing_non_school_organization_and_unknown_contracts_unchanged(self):
        for role, quote, expected in (
                ('organization', '식품 판매 업소 영업자 모집', 'excluded'),
                ('organization', '프로그램 운영기관을 모집합니다.', 'excluded'),
                ('mixed', '개인과 기관 각각 신청', 'review'),
                ('unknown', '', 'review'),
                ('invented', '개인이 신청합니다.', 'review')):
            with self.subTest(role=role, quote=quote):
                self.assertEqual(applicant_status(result(role, quote), quote), expected)

    def test_absent_and_negated_applicant_evidence_cannot_match(self):
        source = '학급단체만 신청가능합니다. 개인은 신청할 수 없습니다.'
        for quote in ('원문에 없는 본인 신청 문장', '개인은 신청할 수 없습니다.', ''):
            with self.subTest(quote=quote):
                self.assertEqual(applicant_status(result('individual', quote), source), 'review')

    def test_helper_is_bounded_and_does_not_interpret_unsafe_input(self):
        for source in (None, '', '학급단체만 신청가능합니다.' + ' ' * 6000,
                       '학급단체만 신청가능합니다.\x00'):
            with self.subTest(source=repr(source)[:80]):
                self.assertFalse(school_group_application_context(source))

    def test_frozen_memorial_response_now_reviews_applicant_without_mutation(self):
        root = Path(__file__).resolve().parents[2] / 'artifacts'
        source_bytes = (root / 'ai-holdout-prep-20260913/sources/news-memorial-school.txt').read_bytes()
        ledger_bytes = (root / 'ai-retest-20260914/fresh-live-01/ledger.jsonl').read_bytes()
        self.assertEqual(hashlib.sha256(source_bytes).hexdigest(),
                         'fabf744aa848e01ab473c4eb341c85367fb1dd89219f929bb4fd397d95e1506e')
        self.assertEqual(hashlib.sha256(ledger_bytes).hexdigest(),
                         'ea955497e7521b89a092350b91b3094a477a3e336f34a88491af9e5a5e68809b')
        rows = [json.loads(line) for line in ledger_bytes.decode('utf-8').splitlines()]
        raw = json.loads(next(row for row in rows if row.get('event') == 'response'
                             and row.get('case_id') == 'news-memorial-school')['raw']['text'])
        old = next(row for row in rows if row.get('event') == 'case_result'
                   and row.get('case_id') == 'news-memorial-school')
        before = copy.deepcopy(raw)
        self.assertEqual(old['decision']['target_checks']['applicant'], 'match')
        self.assertEqual(old['decision']['target_status'], 'review')
        self.assertEqual(applicant_status(raw, source_bytes.decode('utf-8')), 'review')
        self.assertEqual(raw, before)


if __name__ == '__main__':
    unittest.main()
