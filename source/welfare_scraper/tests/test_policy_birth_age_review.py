"""Calendar cohort boundaries are not a legal eligibility determination."""
from datetime import date
import copy
import json
import unittest
from unittest.mock import AsyncMock, patch
import ai_classifier
from services.policy_source_review import source_criteria_issues, source_criteria_note
from services.policy_evidence import diagnose_policy_evidence, public_policy_diagnostics


class BirthAgeReview(unittest.TestCase):
    def birth_issue(self, source):
        return any(item['code'] == 'source_birth_date_age_review' for item in source_criteria_issues(source))

    def test_explicit_age_and_birth_criteria_need_review(self):
        for source in (
            '연령: 18~39세 청년 *출생연도 기준 : 1986.1.1.~2008.12.31.',
            '대상: 만18세 이상 39세 이하\n출생년도: 1986년~2008년',
            '출생일: 1986.01.01~2008.12.31\n대상: 18~39세',
            '대상: 18~39세 (1986.1.1.~2008.12.31. 출생)',
            '대상: 18~39살, 생년월일 기준 1986-01-01~2008-12-31',
            '만20세 이상 (2000년 이후 출생)',
        ):
            with self.subTest(source=source):
                self.assertTrue(self.birth_issue(source))

    def test_normal_duration_forms_and_unrelated_years_not_held(self):
        for source in (
            '지원 대상: 만18~39세 청년',
            '2026년 사업. 지원 대상: 만18~39세 청년',
            '지원 대상: 만18~39세\n제출서류: 생년월일 작성 필요',
            '지원 대상: 2022년 이후 출생아. 사용기간: 출생일부터 2년',
            '지원 대상: 1986.1.1.~2008.12.31. 출생자',
            '지원 대상: 만18~39세\n문의처\n기관 연혁: 2000년 출생 통계 연구',
            '지원 대상: 만18~39세\n출생일 기준 안내',
        ):
            with self.subTest(source=source):
                self.assertFalse(self.birth_issue(source))

    def test_fixed_birth_cohort_and_rolling_month_range_disagree_at_boundaries(self):
        # The source application opening day is a test anchor only. No product
        # code infers that it is the actual legal age-reference date.
        anchor = date(2026, 8, 18)
        for birthday, expected_months, in_cohort, in_months in (
            (date(1985, 12, 31), 487, False, False),
            (date(1986, 1, 1), 487, True, False),
            (date(2008, 12, 31), 211, True, False),
            (date(2009, 1, 1), 211, False, False),
            (date(2000, 8, 18), 312, True, True),
        ):
            with self.subTest(birthday=birthday):
                months = ((anchor.year - birthday.year) * 12 + anchor.month - birthday.month
                          - (anchor.day < birthday.day))
                self.assertEqual(months, expected_months)
                self.assertEqual(date(1986, 1, 1) <= birthday <= date(2008, 12, 31), in_cohort)
                self.assertEqual(216 <= months <= 479, in_months)

    def test_saved_response_diagnostic_no_source_echo_or_mutation(self):
        source = 'PRIVATE-MARKER 연령: 18~39세 (출생연도: 1986~2008년)'
        value = {'age_detail': source, 'start_age_months': 216, 'end_age_months': 479}
        original = copy.deepcopy(value)
        result = diagnose_policy_evidence(value, source)
        self.assertFalse(result['valid'])
        self.assertEqual(result['fields'], ['age_detail'])
        self.assertEqual(value, original)
        public = public_policy_diagnostics(result)
        self.assertEqual(public['issues'], [{'code': 'source_birth_date_age_review', 'field': 'age_detail'}])
        self.assertNotIn('PRIVATE-MARKER', json.dumps(public))
        self.assertIn('기준일', source_criteria_note(public['issues']))


class BirthAgePreflight(unittest.IsolatedAsyncioTestCase):
    async def test_birth_cohort_holds_before_ai_without_inventing_months(self):
        source = '지원 내용: 이사비 지원\n연령: 18~39세 (출생연도: 1986~2008년)'
        review = {}
        with patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock()) as call:
            result = await ai_classifier.extract_policy_fields_async(object(), '정책',
                'https://example.invalid', content=source, review=review)
        call.assert_not_awaited()
        self.assertIsNone(result)
        self.assertEqual(review['code'], 'source_criteria_review_required')
        self.assertEqual(review['fields'], ['age_detail'])

