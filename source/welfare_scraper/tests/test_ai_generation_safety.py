"""Offline response-integrity checks with real SDK response/config structures.

Run through scripts/run_isolated_tests.py. API admission, SDK calls, DB reads
and config writes are test doubles; no provider or operational data is touched.
"""
from types import SimpleNamespace
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from google.genai import types

import ai_classifier
from services import ai_coordinator, ocr_service, policy_sync
from tests.test_ocr_evidence import candidate as ocr_candidate
from services.ai_generation_safety import (
    AiResponseReviewRequired, STRUCTURED_OUTPUT_TOKENS, STYLE_GUIDE_OUTPUT_TOKENS,
    complete_response_text,
)


def response(text='{"category":"복지"}', finish='STOP', **candidate_fields):
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(parts=[types.Part(text=text)]),
        finish_reason=finish, **candidate_fields,
    )])


class TestResponseIntegrity(unittest.TestCase):
    def test_sdk_stop_returns_text_and_does_not_modify_response(self):
        value = response('  {"ok":true}  ')
        before = value.model_dump()
        self.assertEqual(complete_response_text(value), '{"ok":true}')
        self.assertEqual(value.model_dump(), before)

    def test_text_only_replay_compatibility_does_not_invent_completion_metadata(self):
        value = SimpleNamespace(text='{"ok":true}')
        self.assertEqual(complete_response_text(value), value.text)
        self.assertFalse(hasattr(value, 'candidates'))

    def test_parseable_json_at_output_limit_is_never_accepted(self):
        with self.assertRaises(AiResponseReviewRequired) as caught:
            complete_response_text(response('{"is_relevant":true}', 'MAX_TOKENS'))
        self.assertEqual(caught.exception.reason, 'output_limit')
        self.assertEqual(caught.exception.code, 'ai_response_review_required')

    def test_sdk_missing_empty_and_ambiguous_candidates_are_held(self):
        for candidates in (None, [], [types.Candidate()], [types.Candidate(), types.Candidate()]):
            with self.subTest(candidates=candidates), self.assertRaises(AiResponseReviewRequired):
                complete_response_text(types.GenerateContentResponse(candidates=candidates))

    def test_unspecified_and_unknown_finish_are_not_success(self):
        for finish in (None, '', 'FINISH_REASON_UNSPECIFIED', 'OTHER', 'UNEXPECTED_TOOL_CALL', 123):
            with self.subTest(finish=finish), self.assertRaises(AiResponseReviewRequired) as caught:
                complete_response_text(SimpleNamespace(text='{}', candidates=[
                    SimpleNamespace(finish_reason=finish)]))
            self.assertEqual(caught.exception.reason, 'incomplete')

    def test_provider_safety_reasons_override_nonempty_text(self):
        for finish in ('SAFETY', 'RECITATION', 'BLOCKLIST', 'PROHIBITED_CONTENT', 'SPII'):
            with self.subTest(finish=finish), self.assertRaises(AiResponseReviewRequired) as caught:
                complete_response_text(response('private-provider-payload', finish))
            self.assertEqual(caught.exception.reason, 'blocked')
            self.assertNotIn('private-provider-payload', str(caught.exception))

    def test_prompt_block_is_checked_before_accessing_text(self):
        class Blocked:
            prompt_feedback = SimpleNamespace(block_reason='SAFETY')

            @property
            def text(self):
                raise AssertionError('Blocked response text must not be inspected')

        with self.assertRaises(AiResponseReviewRequired) as caught:
            complete_response_text(Blocked())
        self.assertEqual(caught.exception.reason, 'blocked')

    def test_unspecified_prompt_feedback_is_not_itself_a_block(self):
        value = response()
        value.prompt_feedback = types.GenerateContentResponsePromptFeedback(
            block_reason='BLOCKED_REASON_UNSPECIFIED')
        self.assertEqual(complete_response_text(value), '{"category":"복지"}')

    def test_blocked_safety_rating_overrides_stop(self):
        value = response(safety_ratings=[types.SafetyRating(blocked=True)])
        with self.assertRaises(AiResponseReviewRequired) as caught:
            complete_response_text(value)
        self.assertEqual(caught.exception.reason, 'blocked')

    def test_empty_or_nontext_adapter_output_is_held(self):
        for text in (None, '', ' \n ', 123, {}):
            with self.subTest(text=text), self.assertRaises(AiResponseReviewRequired) as caught:
                complete_response_text(SimpleNamespace(text=text))
            self.assertEqual(caught.exception.reason, 'empty')

    def test_empty_sdk_stop_is_held(self):
        with self.assertRaises(AiResponseReviewRequired) as caught:
            complete_response_text(response(''))
        self.assertEqual(caught.exception.reason, 'empty')

    def test_error_never_echoes_untrusted_reason(self):
        error = AiResponseReviewRequired('source/private-key\n429')
        self.assertEqual(error.reason, 'incomplete')
        self.assertNotIn('private-key', str(error))
        self.assertFalse(ai_coordinator.analyze_api_error(error)['rate_limited'])


class TestGenerationCallSites(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from services import ai_verified_input
        ai_verified_input._reset_for_tests()
        self.addCleanup(ai_verified_input._reset_for_tests)
        self.generate = AsyncMock(return_value=response())
        self.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
            generate_content=self.generate,
            count_tokens=AsyncMock(return_value=SimpleNamespace(total_tokens=1000)))))
        self.admit = patch.object(ai_coordinator, 'acquire_model_slot', new_callable=AsyncMock).start()
        self.register = patch.object(ai_coordinator, 'register_api_error',
            side_effect=lambda _model, error: ai_coordinator.analyze_api_error(error)).start()
        self.addCleanup(patch.stopall)

    def assert_cap(self, cap):
        config = self.generate.await_args.kwargs['config']
        self.assertIsInstance(config, types.GenerateContentConfig)
        self.assertEqual(config.max_output_tokens, cap)
        self.assertEqual(config.candidate_count, 1)

    async def test_classifier_success_uses_structured_cap_and_preserves_json_fences(self):
        self.generate.return_value = response('```JSON\n{"category":"복지"}\n```')
        actual = await ai_classifier._call_gemini_json(self.client, '공개 복지사업 안내', '합성 문서')
        self.assertEqual(actual, {'category': '복지'})
        self.generate.assert_awaited_once()
        self.assert_cap(STRUCTURED_OUTPUT_TOKENS)

    async def test_incomplete_classifier_is_not_retried_even_with_three_attempts(self):
        self.generate.return_value = response('{"category":"복지"}', 'MAX_TOKENS')
        with self.assertRaises(AiResponseReviewRequired):
            await ai_classifier._call_gemini_json(self.client, '공개 복지사업 안내', '합성 문서', request_attempts=3)
        self.generate.assert_awaited_once()
        self.admit.assert_awaited_once()
        self.assert_cap(STRUCTURED_OUTPUT_TOKENS)

    async def test_policy_extraction_reports_hold_without_returning_partial_fields(self):
        self.generate.return_value = response('{"support_detail":"교육비 지원"}', 'MAX_TOKENS')
        review = {}
        with patch.dict('os.environ', {'AI_CONTENT_MAX_CHARS': '6000'}):
            actual = await ai_classifier.extract_policy_fields_async(self.client, '합성 정책',
                'https://example.invalid/policy', content='교육비 지원', review=review)
        self.assertIsNone(actual)
        self.assertEqual(review['code'], 'ai_response_review_required')
        self.assertIn('기존 내용은 유지', review['note'])
        self.generate.assert_awaited_once()

    async def test_ocr_success_uses_structured_cap(self):
        self.generate.return_value = response(
            json.dumps(ocr_candidate('교육비 지원'), ensure_ascii=False))
        actual = await ocr_service._analyze_document_bytes(self.client,
            b'%PDF-1.4 synthetic-public-document', privacy_reviewed=True)
        self.assertTrue(actual['is_relevant'])
        self.assertEqual(actual['support_detail'], '교육비 지원')
        self.assertTrue(actual['needs_source_review'])
        self.assert_cap(STRUCTURED_OUTPUT_TOKENS)

    async def test_ocr_valid_but_truncated_json_is_a_review_not_extraction(self):
        self.generate.return_value = response(
            '{"is_relevant":true,"title":"합성 정책","support_detail":"교육비 지원"}', 'MAX_TOKENS')
        actual = await ocr_service._analyze_document_bytes(self.client,
            b'%PDF-1.4 synthetic-public-document', privacy_reviewed=True)
        self.assertFalse(actual['is_relevant'])
        self.assertTrue(actual['needs_source_review'])
        self.assertEqual(actual['error_code'], 'OCR_SOURCE_REVIEW_REQUIRED')
        self.assertEqual(actual['response_review_code'], 'ai_response_review_required')
        self.assertTrue(actual['_ocr_review']['required'])
        self.assertIs(actual['_ocr_review']['source_complete'], False)
        self.assertNotIn('support_detail', actual)
        self.generate.assert_awaited_once()

    async def style_guide(self, provider_response):
        self.generate.return_value = provider_response
        save = Mock()
        with patch.object(ai_classifier, 'get_client', return_value=self.client), \
             patch.object(policy_sync.database, 'run_db', new=AsyncMock(return_value=(
                 {'총건수': 1}, [{'support_detail': '교육비 지원'}]))), \
             patch.object(policy_sync.utils, 'set_env_value', save):
            result = await policy_sync.generate_style_guide(['synthetic-db'])
        return result, save

    async def test_style_success_has_small_cap_and_saves_once(self):
        result, save = await self.style_guide(response('- 지원금은 지원 내용에 표기'))
        self.assertTrue(result['ok'])
        self.assertEqual(result['guide'], '- 지원금은 지원 내용에 표기')
        save.assert_called_once_with('POLICY_STYLE_GUIDE', result['guide'])
        self.assert_cap(STYLE_GUIDE_OUTPUT_TOKENS)

    async def test_style_partial_response_never_overwrites_saved_guide(self):
        result, save = await self.style_guide(response('- 지원금은', 'MAX_TOKENS'))
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'ai_response_review_required')
        save.assert_not_called()
        self.generate.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
