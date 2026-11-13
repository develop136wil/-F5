import os
import sys
import unittest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.evaluate_search_quality import evaluate_case, summarize


class TestSearchQualityEvaluator(unittest.TestCase):
    def test_relevant_term_and_category_produce_hit(self):
        case = {
            'id': 'elderly', 'query': '노인 돌봄',
            'required_any': ['노인', '어르신'], 'expected_categories': ['복지'],
        }
        result = evaluate_case(case, [
            {'title': '아동 돌봄', 'category': '복지'},
            {'title': '어르신 맞춤 돌봄', 'category': '복지', 'match_sources': ['semantic']},
        ])
        self.assertTrue(result['hit_at_5'])
        self.assertEqual(result['first_relevant_rank'], 2)
        self.assertEqual(result['reciprocal_rank'], 0.5)

    def test_wrong_category_is_not_automatic_hit(self):
        case = {
            'id': 'youth', 'query': '청소년 교육비',
            'required_any': ['청소년'], 'expected_categories': ['교육/보육'],
        }
        result = evaluate_case(case, [
            {'title': '청소년 행사', 'category': '기타'},
        ])
        self.assertFalse(result['hit_at_5'])

    def test_summary_lists_failed_cases(self):
        summary = summarize([
            {'id': 'a', 'hit_at_5': True, 'reciprocal_rank': 1.0},
            {'id': 'b', 'hit_at_5': False, 'reciprocal_rank': 0.0},
        ])
        self.assertEqual(summary['hit_at_5_rate'], 0.5)
        self.assertEqual(summary['mean_reciprocal_rank'], 0.5)
        self.assertEqual(summary['failed_case_ids'], ['b'])


if __name__ == '__main__':
    unittest.main()
