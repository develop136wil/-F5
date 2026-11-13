import unittest
from services.news_quality_evaluation import evaluate


class QualityEvaluationTests(unittest.TestCase):
    def row(self, i, label, score):
        return dict(profile_id='p', candidate_id=str(i), label=label, score=score, scoring_version='v1')

    def test_metrics_include_misses_unknown_and_failed_scores(self):
        rows=[self.row(1,'relevant',.8),self.row(2,'irrelevant',.9),
              self.row(3,'relevant',.6),self.row(4,'irrelevant',.1),
              self.row(5,'unknown',.9),self.row(6,'relevant',None)]
        result=evaluate(rows,threshold=.8,scoring_version='v1')
        self.assertEqual(result['precision'],.5)
        self.assertEqual(result['recall_scored'],.5)
        self.assertEqual(result['recall_including_unscored'],1/3)
        self.assertEqual(result['unknown_promoted'],1)
        self.assertEqual(result['promoted'],3)
        self.assertFalse(result['production_approved'])

    def test_no_predictions_is_not_perfect_precision(self):
        result=evaluate([self.row(1,'irrelevant',0)],threshold=.8,scoring_version='v1')
        self.assertIsNone(result['precision'])
        self.assertIsNone(result['recall_scored'])

    def test_invalid_or_mixed_evidence_rejected(self):
        row=self.row(1,'relevant',.9)
        for rows in ([],[row,row],[{**row,'scoring_version':'other'}],
                     [{**row,'score':float('nan')}],[{**row,'score':True}],
                     [{**row,'label':'guessed'}]):
            with self.assertRaises(ValueError):
                evaluate(rows,threshold=.8,scoring_version='v1')
        for value in (True, float('inf'), -2):
            with self.assertRaises(ValueError):
                evaluate([row],threshold=value,scoring_version='v1')

    def test_input_not_modified(self):
        import copy
        rows=[self.row(1,'unknown',None)];before=copy.deepcopy(rows)
        result=evaluate(rows,threshold=.5,scoring_version='v1')
        self.assertEqual(result['unknown'],1)
        self.assertEqual(result['unscored'],1)
        self.assertEqual(rows,before)
