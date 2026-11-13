"""Offline checks for the live OCR evaluator; never dispatch an API request."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

with patch.object(sys, 'path', [str(Path(__file__).resolve().parents[1] / 'scripts'), *sys.path]):
    import evaluate_ocr_bounded as runner


class TestOcrQualityChecks(unittest.TestCase):
    def result(self, case):
        return dict(is_relevant=True, support_detail=' '.join(case['facts']),
            start_age_months=case['ages'][0], end_age_months=case['ages'][1],
            deadline=case['deadline'], residence=case['residence'],
            target_income='', contact='', related_urls=[])

    def test_valid_fixture_and_each_invented_unknown(self):
        for case in runner.CASES:
            base = self.result(case)
            self.assertTrue(all(runner.check_result(case, base).values()))
            for key, value in [('target_income', '소득 무관'), ('contact', '02-000-0000'),
                               ('related_urls', ['https://example.invalid/not-in-source'])]:
                with self.subTest(case=case['id'], field=key):
                    self.assertFalse(runner.check_result(case, dict(base, **{key: value}))[key])

    def test_missing_region_does_not_pass_as_nationwide(self):
        case = runner.CASES[1]
        base = self.result(case)
        self.assertFalse(runner.check_result(case, dict(base, residence=['전국']))['residence'])

    def test_omitted_field_is_not_equivalent_to_known_empty(self):
        case = runner.CASES[0]
        base = self.result(case)
        del base['target_income']
        self.assertFalse(runner.check_result(case, base)['target_income'])
