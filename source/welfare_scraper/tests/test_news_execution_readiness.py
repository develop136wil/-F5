import unittest
from services.news_execution_readiness import readiness


class ReadinessTests(unittest.TestCase):
    def test_collection_readiness_has_no_ai_dependency(self):
        from services.news_execution_readiness import collection_readiness
        for stopping in (False, True):
            for bad in (False, True):
                enabled, code, reason = collection_readiness(
                    shutting_down=stopping, selection_reason='수집 사이트 확인' if bad else '')
                self.assertEqual(enabled, not stopping and not bad)
                self.assertEqual(bool(code), not enabled)
                self.assertEqual(bool(reason), not enabled)

    def test_direct_mode_still_requires_key_settings_and_selection(self):
        from itertools import product
        for key, settings, stopping, bad in product((False, True), repeat=4):
            result = readiness(ai_required=True, key_ready=key, threshold_ready=False,
                settings_ready=settings, direct_analysis=True, shutting_down=stopping,
                selection_reason='조건 확인' if bad else '')
            self.assertEqual(result[0], key and settings and not stopping and not bad)
    def test_all_combinations_preserve_execution_gate(self):
        from itertools import product
        for ai, key, threshold, settings, stopping, bad_selection in product((False, True), repeat=6):
            enabled, code, reason = readiness(ai_required=ai,key_ready=key,
                threshold_ready=threshold,settings_ready=settings,
                shutting_down=stopping,selection_reason='조건 확인' if bad_selection else '')
            self.assertEqual(enabled, not stopping and not bad_selection and
                             (not ai or (key and threshold and settings)))
            self.assertEqual(bool(code), not enabled)
            self.assertEqual(bool(reason), not enabled)

    def test_quality_problem_does_not_request_another_key(self):
        result = readiness(ai_required=True,key_ready=True,threshold_ready=False,settings_ready=False)
        self.assertEqual(result[1], 'QUALITY_VALIDATION_REQUIRED')
        self.assertIn('다시 입력할 필요는 없습니다', result[2])

    def test_official_only_needs_no_ai_calibration(self):
        self.assertEqual(readiness(ai_required=False,key_ready=False,
            threshold_ready=False,settings_ready=True), (True, '', ''))
