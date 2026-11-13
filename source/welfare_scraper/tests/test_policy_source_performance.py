"""Bounded offline policy preparation diagnostic; no provider or app profile."""
import cProfile
import io
import pstats
import unittest
from unittest.mock import patch

from services.policy_source_plan import build_policy_extraction_request
from tests.test_policy_input_parity_audit import source_of_length


class PolicySourcePerformance(unittest.TestCase):
    def test_profile_whole_source_planning(self):
        profiler = cProfile.Profile()
        source = source_of_length(6000)
        profiler.runcall(build_policy_extraction_request, source, '교통비 지원',
            'https://example.invalid/policy', '', {'category': ['복지'],
            'residence': ['전국'], 'target_characteristics': ['일반'], 'life_cycle': ['전생애']},
            input_token_budget=6000, defer_token_budget_check=True)
        output = io.StringIO()
        pstats.Stats(profiler, stream=output).sort_stats('cumtime').print_stats(18)
        print(output.getvalue())

    def test_prepared_context_preserves_positive_and_negative_boundaries(self):
        from services.news_fact_grounding import affirmative_quote_checker, scoped_affirmative_quote
        cases = [
            ('도봉구 거주자', '도봉구 거주자 대상, 중복 신청 불가.', '도봉구 거주자'),
            ('도봉구 거주자', '제외 대상:\n도봉구 거주자', None),
            ('도봉구 거주자', '도봉구 거주자는\n신청할 수 없습니다.', None),
            ('노년', '제외 대상: 청년, 노년', None),
            ('지원 대상:\r\n도봉구 거주자', '지원 대상:\r\n도봉구 거주자', '지원 대상:\r\n도봉구 거주자'),
            ('청년 대상', '청년 대상\n중복 수급자 제외', '청년 대상'),
            ('없는 인용', '청년 대상', None),
        ]
        for quote, source, expected in cases:
            with self.subTest(source=source):
                checker = affirmative_quote_checker(source)
                self.assertEqual(scoped_affirmative_quote(quote, source), expected)
                self.assertEqual(checker(quote), expected)
                self.assertEqual(checker(quote), expected)

    def test_memo_is_local_to_exact_source_and_repeated_quote_is_checked_once(self):
        from services import news_fact_grounding as grounding
        original = grounding._scoped_affirmative_quote
        with patch.object(grounding, '_scoped_affirmative_quote', wraps=original) as check:
            positive = grounding.affirmative_quote_checker('청년 대상')
            negative = grounding.affirmative_quote_checker('제외 대상: 청년 대상')
            for _ in range(200):
                self.assertEqual(positive('청년 대상'), '청년 대상')
                self.assertIsNone(negative('청년 대상'))
            self.assertEqual(check.call_count, 2)
