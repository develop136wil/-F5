"""Frozen real-score counterexample; never calls Google or tunes production."""
import unittest
from services.news_quality_evaluation import evaluate


class LiveScoreRegression(unittest.TestCase):
    def test_body_target_clue_rescues_generic_child_title_not_senior_notice(self):
        from services.news_profile import NewsProfile
        from services.news_analysis_context import NewsAnalysisContext
        context=NewsAnalysisContext(NewsProfile('child',1,'아동','도봉구',life_cycles=('아동',)))
        # Paraphrased source evidence, not a current eligibility determination.
        self.assertEqual(context.route_candidate('모내기 체험 참여가족 모집',
            '모집대상: 도봉구에 거주하는 6세 이상 아동 동반 가족')[0], 'matched')
        self.assertEqual(context.route_candidate('어르신 일자리 참여자 모집',
            '모집대상: 65세 이상 어르신')[0], 'semantic_candidate')

    def test_child_title_inversion_cannot_be_fixed_by_threshold(self):
        # Source-reviewed public Dobong announcements, measured 2026-09-07.
        positive = .8043212525159443  # reqstId=454, families with children 6+
        negative = .8145936286469951  # reqstId=324, older-adult job applicants
        self.assertGreater(negative, positive)
        rows=[dict(profile_id='child',candidate_id='454',label='relevant',score=positive,scoring_version='live-v1'),
              dict(profile_id='child',candidate_id='324',label='irrelevant',score=negative,scoring_version='live-v1')]
        for threshold in (-1, positive, (positive+negative)/2, negative, 1):
            result=evaluate(rows,threshold=threshold,scoring_version='live-v1')
            self.assertGreater(result['false_positive']+result['false_negative'],0)
            self.assertFalse(result['production_approved'])
