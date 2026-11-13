"""Offline counterexamples: branch ages must not become one programme limit."""
import copy
import os
import unittest
from unittest.mock import AsyncMock, patch
from services.policy_evidence import structured_policy_issues


class PolicyAgeBranchGuard(unittest.TestCase):
    def test_nested_upper_exception_cannot_become_overall_ceiling(self):
        lines = ['18세 미만 등록 청각장애인', '5세 미만은 의사 소견이 있으면 장애 미등록자도 가능']
        for clauses in (lines, list(reversed(lines))):
            for separator in ('\n', '. ', ', '):
                source = '지원대상\n' + separator.join(clauses) + '\n지원내용\n수술비 지원'
                for end in (59, 215):
                    with self.subTest(separator=separator, clauses=clauses, end=end):
                        facts = {'start_age_months': None, 'end_age_months': end, 'age_detail': source}
                        original = copy.deepcopy(facts)
                        self.assertIn('end_age_months', structured_policy_issues(facts, source))
                        self.assertEqual(facts, original)

    def test_different_lower_criteria_also_require_review(self):
        source = '지원대상\n65세 이상 주민\n장애인은 18세 이상부터 신청 가능\n지원내용\n교통비 지원'
        for start in (216, 780):
            self.assertIn('start_age_months', structured_policy_issues({'start_age_months':start}, source))

    def test_repeated_equivalent_bounds_do_not_trigger_ambiguity(self):
        for source, facts in (
            ('지원대상\n18세 미만 아동\n216개월 미만 아동\n지원내용\n교통비 지원', {'end_age_months':215}),
            ('지원대상: 9세 미만(0~107개월) 아동', {'start_age_months':0,'end_age_months':107}),
            ('지원대상: 19세~39세 청년', {'start_age_months':228,'end_age_months':479}),
        ):
            self.assertEqual(structured_policy_issues(facts, source), [])

    def test_guardian_staff_and_exclusion_ages_do_not_create_new_ambiguity(self):
        source = '지원대상\n65세 이상 노인\n제공인력\n18세 이상 요양보호사\n지원내용\n돌봄 지원'
        self.assertEqual(structured_policy_issues({'start_age_months':780}, source), [])

    def test_null_age_does_not_erase_or_repair_conditions(self):
        source = '지원대상\n18세 미만 등록 청각장애인\n5세 미만은 미등록자도 가능'
        facts = {'start_age_months':None,'end_age_months':None,'age_detail':source}
        original=copy.deepcopy(facts)
        self.assertEqual(structured_policy_issues(facts, source), [])
        self.assertEqual(facts, original)

    def test_frozen_response_keeps_its_original_hold(self):
        from tests.test_policy_age_boundary_prompt import frozen
        from services.policy_source_plan import hydrate_policy_source_ranges
        source, raw = frozen()
        candidate = hydrate_policy_source_ranges(raw, source)
        self.assertEqual(set(structured_policy_issues(candidate,source)), {'start_age_months','end_age_months'})
        frozen()


class PolicyBranchPipeline(unittest.IsolatedAsyncioTestCase):
    async def test_one_fake_response_is_held_without_repair_or_second_request(self):
        import ai_classifier
        from tests.test_policy_age_boundary_prompt import frozen
        source, original = frozen()
        for ceiling in (59, 215):
            raw = copy.deepcopy(original)
            raw['start_age_months'] = None
            raw['end_age_months'] = ceiling
            before = copy.deepcopy(raw)
            review = {}
            with patch.dict(os.environ, {'AI_CONTENT_MAX_CHARS':'6000'}), \
                 patch.object(ai_classifier, '_call_gemini_json', new=AsyncMock(return_value=raw)) as call:
                result = await ai_classifier.extract_policy_fields_async(object(), '합성 연령 예외 정책',
                    'https://example.invalid/age-branch', content=source, review=review)
            self.assertIsNone(result)
            self.assertEqual(review['code'], 'structured_evidence_missing')
            self.assertIn('end_age_months', review['fields'])
            self.assertEqual(call.await_count, 1)
            self.assertEqual(raw, before)
        frozen()
