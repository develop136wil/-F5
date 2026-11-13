"""Date-only review keeps transcription; fixed-response replay is not fresh AI quality."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch

from google.genai import types
from services import ai_coordinator, ocr_evidence, ocr_service
from tests.test_ocr_evidence import candidate


FROZEN_SHA = '932176e17e3703821201ea167c44fe588701af58e30b1fa4a39be7b565ae440e'
FROZEN_PATH = (Path(__file__).resolve().parents[2] / 'artifacts/ai-release-fixes-20260914'
               / 'fresh-live-01/ocr-gijang-home-library.sdk-response.json')


def frozen_response():
    data = FROZEN_PATH.read_bytes()
    if hashlib.sha256(data).hexdigest() != FROZEN_SHA:
        raise AssertionError('Frozen public OCR response changed')
    response = json.loads(data)
    text = response['candidates'][0]['content']['parts'][0]['text']
    return json.loads(text), types.GenerateContentResponse(candidates=[types.Candidate(
        finish_reason='STOP', content=types.Content(parts=[types.Part(text=text)]))])


class TestOcrDateReview(unittest.TestCase):
    def test_non_iso_or_impossible_dates_hold_only_that_field_without_normalization(self):
        for key in ocr_evidence.DATE_FIELDS:
            for date_value in ('2026.8.25.(화)', '2026년 9월', '2026년 12월',
                               '2026-02-30', '2026-13-01', '20260825', '2026-8-25',
                               '2026-08-25T10:00', '2026-W35-2', ' 2026-08-25 ', '0000-01-01'):
                with self.subTest(field=key, date=date_value):
                    source = '교육비 지원\n기간: ' + date_value
                    raw = candidate(source, **{key: date_value})
                    raw['field_evidence'][key] = [source.splitlines()[1]]
                    original = deepcopy(raw)
                    result = ocr_service.validate_ocr_result(raw)
                    self.assertNotIn('error_code', result)
                    self.assertIsNone(result[key])
                    self.assertEqual(result['_ocr_review']['unverified_fields'], {key: date_value})
                    self.assertIn('ocr_date_format_unverified', result['_ocr_review']['reasons'])
                    self.assertEqual(result['support_detail'], source)
                    self.assertEqual(result['_ocr_review']['source_text'], source)
                    self.assertTrue(result['needs_source_review'])
                    self.assertEqual(raw, original)

    def test_one_bad_field_does_not_clear_independently_grounded_iso_dates(self):
        source = ('접수기간: 2026-08-04 ~ 2026-08-25\n'
                  '이용기간: 2026-09-01 ~ 2026-12-31')
        raw = candidate(source, deadline='2026-08-25', event_date='2026년 9월',
                        event_end_date='2026-12-31')
        raw['field_evidence']['deadline'] = [source.splitlines()[0]]
        for key in ('event_date', 'event_end_date'):
            raw['field_evidence'][key] = [source.splitlines()[1]]
        result = ocr_service.validate_ocr_result(raw)
        self.assertEqual(result['deadline'], '2026-08-25')
        self.assertEqual(result['event_end_date'], '2026-12-31')
        self.assertIsNone(result['event_date'])
        self.assertEqual(result['_ocr_review']['unverified_fields'], {'event_date': '2026년 9월'})
        self.assertEqual(result['support_detail'], source)

    def test_null_month_only_period_stays_unknown_without_filling_any_day(self):
        source = '대출기간: 2026년 9월 ~ 12월'
        for missing in (None, ''):
            with self.subTest(missing=missing):
                raw = candidate(source, **{key: missing for key in ocr_evidence.DATE_FIELDS})
                result = ocr_service.validate_ocr_result(raw)
                for key in ocr_evidence.DATE_FIELDS:
                    self.assertEqual(result[key], missing)
                self.assertEqual(result['_ocr_review']['unverified_fields'], {})
                self.assertEqual(result['support_detail'], source)
                self.assertTrue(result['needs_source_review'])

    def test_wrong_date_types_or_oversized_values_keep_strict_shape_rejection(self):
        for key in ocr_evidence.DATE_FIELDS:
            for invalid in (True, False, 0, 20260825, [], {}, 'x' * 129):
                with self.subTest(field=key, value=invalid), self.assertRaises(ValueError):
                    ocr_service.validate_ocr_result(candidate(**{key: invalid}))

    def test_invalid_envelope_remains_whole_hold_even_when_only_date_is_malformed(self):
        for change in ({'source_text': ''}, {'source_text': None}, {'source_complete': 'true'},
                       {'field_evidence': {}}, {'field_evidence': []}):
            with self.subTest(change=change):
                raw = candidate(deadline='2026-02-30', **change)
                result = ocr_service.validate_ocr_result(raw)
                self.assertEqual(result['error_code'], 'OCR_SOURCE_REVIEW_REQUIRED')
                self.assertFalse(result['is_relevant'])
                self.assertTrue(result['needs_source_review'])
                self.assertNotIn('support_detail', result)

    def test_frozen_new_response_keeps_source_but_not_dates_phone_expansion_or_guessed_tags(self):
        raw, _ = frozen_response()
        original = deepcopy(raw)
        result = ocr_service.validate_ocr_result(raw)
        self.assertNotIn('error_code', result)
        self.assertTrue(result['needs_source_review'])
        self.assertEqual(result['support_detail'], raw['source_text'])
        self.assertEqual(result['_ocr_review']['source_text'], raw['source_text'])
        self.assertIs(result['_ocr_review']['source_complete'], True)  # Model self-report only.
        self.assertIs(result['_ocr_review']['transcription_independently_verified'], False)
        for key in ocr_evidence.DATE_FIELDS:
            self.assertIsNone(result[key])
            self.assertEqual(result['_ocr_review']['unverified_fields'][key], raw[key])
        self.assertEqual(result['contact'], '')
        self.assertEqual(result['_ocr_review']['unverified_fields']['contact'], '051-709-5322')
        self.assertEqual(result['life_cycle'], [])
        self.assertEqual(result['target_characteristics'], [])
        self.assertEqual(result['residence'], ['기장군'])
        self.assertNotIn('무료', result['support_detail'])
        # The subsequent title-only guard permits whitespace presentation of
        # the complete consecutive title quotes; date/phone guards stay strict.
        self.assertEqual(result['title'], raw['title'])
        self.assertNotIn('title', result['_ocr_review']['unverified_fields'])
        self.assertEqual(raw, original)
        self.assertEqual(hashlib.sha256(FROZEN_PATH.read_bytes()).hexdigest(), FROZEN_SHA)


class TestOcrDateReviewSdkBoundary(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from services import ai_verified_input
        ai_verified_input._reset_for_tests()
        self.addCleanup(ai_verified_input._reset_for_tests)

    async def test_frozen_response_returns_review_preview_after_one_mock_sdk_call(self):
        raw, response = frozen_response()
        client = Mock()
        client.aio.models.generate_content = AsyncMock(return_value=response)
        client.aio.models.count_tokens = AsyncMock(return_value=types.CountTokensResponse(total_tokens=1000))
        with patch.object(ai_coordinator, 'acquire_model_slot', new=AsyncMock()) as admission, \
             patch.object(ai_coordinator, 'register_api_error') as errors:
            result = await ocr_service._analyze_document_bytes(client, b'%PDF-1.4 synthetic',
                mime_type='application/pdf', privacy_reviewed=True)
        admission.assert_awaited_once()
        client.aio.models.generate_content.assert_awaited_once()
        errors.assert_not_called()
        self.assertNotIn('error_code', result)
        self.assertEqual(result['support_detail'], raw['source_text'])
        self.assertTrue(result['needs_source_review'])
        for key in ocr_evidence.DATE_FIELDS:
            self.assertIsNone(result[key])
            self.assertEqual(result['_ocr_review']['unverified_fields'][key], raw[key])
        config = client.aio.models.generate_content.call_args.kwargs['config']
        self.assertEqual(config.max_output_tokens, 8192)
        self.assertEqual(config.candidate_count, 1)

    async def test_wrong_date_type_is_still_nonretrying_whole_review_hold(self):
        raw = candidate(deadline=['2026-08-25'])
        response = types.GenerateContentResponse(candidates=[types.Candidate(
            finish_reason='STOP', content=types.Content(parts=[types.Part(text=json.dumps(raw))]))])
        client = Mock()
        client.aio.models.generate_content = AsyncMock(return_value=response)
        client.aio.models.count_tokens = AsyncMock(return_value=types.CountTokensResponse(total_tokens=1000))
        with patch.object(ai_coordinator, 'acquire_model_slot', new=AsyncMock()), \
             patch.object(ai_coordinator, 'register_api_error') as errors:
            result = await ocr_service._analyze_document_bytes(client, b'%PDF-1.4 synthetic',
                mime_type='application/pdf', privacy_reviewed=True)
        client.aio.models.generate_content.assert_awaited_once()
        errors.assert_not_called()
        self.assertEqual(result['error_code'], 'OCR_SOURCE_REVIEW_REQUIRED')
        self.assertFalse(result['is_relevant'])
        self.assertTrue(result['needs_source_review'])
