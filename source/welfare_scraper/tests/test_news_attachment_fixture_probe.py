"""Audit-only legacy fixture compatibility, supported model and fake SDK only."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from services import ai_rate_policy, ai_verified_input, news_analysis_store as jobs
from tests import test_local_attachment_privacy as fixture


class AttachmentFixtureCompatibilityProbe(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.LocalCoveragePipeline.setUp
    tearDown = fixture.LocalCoveragePipeline.tearDown
    db = fixture.LocalCoveragePipeline.db
    _assert_review_pipeline = fixture.LocalCoveragePipeline._assert_review_pipeline

    async def check_with_supported_model(self, kind, reasons, *, current_response=True):
        import ai_classifier
        original_collect = fixture.collect_registered_sites
        original_process = fixture.process_job
        count = AsyncMock(return_value=SimpleNamespace(total_tokens=1000))
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(count_tokens=count)))
        ai_verified_input._reset_for_tests()
        self.addCleanup(ai_verified_input._reset_for_tests)
        async def collect(*args, **kwargs):
            kwargs['analysis_model'] = ai_rate_policy.MODELS[0]
            return await original_collect(*args, **kwargs)
        async def process(job_id, _old_client, **kwargs):
            if current_response:
                raw = deepcopy(ai_classifier._call_gemini_json.return_value)
                raw['_support_source_units'] = [1]
                raw.pop('support_detail', None)
                raw['fact_evidence'].pop('support_detail', None)
                ai_classifier._call_gemini_json.return_value = raw
            else:
                raw = deepcopy(ai_classifier._call_gemini_json.return_value)
                raw.pop('_support_source_units', None)
                raw['support_detail'] = '교육비 지원'
                raw['fact_evidence']['support_detail'] = '교육비 지원'
                ai_classifier._call_gemini_json.return_value = raw
            self.response_had_selection = '_support_source_units' in ai_classifier._call_gemini_json.return_value
            result = await original_process(job_id, client, **kwargs)
            self.last_reason = jobs.get_job(self.conn, job_id)['reason']
            return result
        with patch.object(fixture, 'collect_registered_sites', new=collect), \
             patch.object(fixture, 'process_job', new=process), \
             patch.object(ai_verified_input, '_take_count_slot', new=AsyncMock(return_value=True)):
            await self._assert_review_pipeline(kind, reasons)

    async def test_pdf_fixture_passes_real_capacity_with_supported_model_and_fake_sdk(self):
        await self.check_with_supported_model('pdf', ['extraction_timeout'])

    async def test_ocr_fixture_has_second_legacy_prose_schema_mismatch(self):
        with self.assertRaisesRegex(AssertionError, "'failed' != 'complete'"):
            await self.check_with_supported_model('png', ['ocr_verification_required', 'ocr_low_confidence'], current_response=False)
        self.assertFalse(self.response_had_selection)
        self.assertEqual(self.last_reason, 'invalid_analysis')

    async def test_ocr_review_expectations_pass_when_fake_reply_uses_current_source_selection(self):
        await self.check_with_supported_model('png', ['ocr_verification_required', 'ocr_low_confidence'],
                                             current_response=True)
