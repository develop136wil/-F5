"""OCR schema budget regression; run only through the isolated audit harness.

Synthetic bytes, mocked admission and SDK response. No network, operating
configuration, shared quota ledger or document download is used.
"""
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from google.genai import types
from services import ai_coordinator, ai_rate_policy, ocr_service
from services import ai_verified_input
from tests.test_ocr_evidence import candidate as ocr_candidate


IMAGE = b'\x89PNG\r\n\x1a\n' + b'synthetic-image-content' * 60


def completed_response():
    return types.GenerateContentResponse(candidates=[types.Candidate(
        finish_reason='STOP', content=types.Content(parts=[types.Part(
            text=json.dumps(ocr_candidate('교육비 지원'), ensure_ascii=False))]))])


class TestOcrSchemaBudget(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ai_verified_input._reset_for_tests()
        self.addCleanup(ai_verified_input._reset_for_tests)

    async def test_admission_includes_compact_unicode_schema_and_transmits_same_object(self):
        schema = ocr_service.response_schema()
        schema['description'] = '원문에 명시한 조건만 보존'
        response = completed_response()
        client = Mock()
        client.aio.models.generate_content = AsyncMock(return_value=response)
        client.aio.models.count_tokens = AsyncMock(return_value=types.CountTokensResponse(total_tokens=1000))
        original_config = types.GenerateContentConfig
        with patch.object(ocr_service, 'response_schema', return_value=schema) as factory, \
             patch.object(ai_coordinator, 'estimate_input_tokens', wraps=ai_rate_policy.estimate_input_tokens) as estimate, \
             patch.object(ai_coordinator, 'acquire_model_slot', new=AsyncMock()) as acquire, \
             patch.object(ai_coordinator, 'register_api_error') as errors, \
             patch.object(ocr_service.types, 'GenerateContentConfig', wraps=original_config) as config_factory:
            result = await ocr_service._analyze_document_bytes(client, IMAGE,
                mime_type='image/png', file_name='synthetic.png', privacy_reviewed=True)

        self.assertTrue(result['is_relevant'])
        self.assertEqual(result['support_detail'], '교육비 지원')
        self.assertTrue(result['needs_source_review'])
        factory.assert_called_once_with()
        estimate.assert_called_once()
        acquire.assert_awaited_once()
        client.aio.models.generate_content.assert_awaited_once()
        errors.assert_not_called()
        sent = client.aio.models.generate_content.call_args.kwargs
        prompt = sent['contents'][0]
        self.assertIn('ALL three date fields must be exact calendar dates in YYYY-MM-DD form or null.', prompt)
        self.assertIn('If only a year or month is visible, return null; never invent a first/last day.', prompt)
        for key in ocr_service._DATE_FIELDS:
            self.assertIn('YYYY-MM-DD or null', schema['properties'][key]['description'])
        schema_text = json.dumps(schema, ensure_ascii=False, separators=(',', ':'))
        self.assertIn('원문에 명시한 조건만 보존', schema_text)
        estimate.assert_called_once_with([prompt, schema_text], media_bytes=len(IMAGE))
        acquire.assert_awaited_once_with(sent['model'], input_tokens=
            1000 + 256 + 100 + 128 + len(schema_text.encode('utf-8')))
        self.assertEqual(client.aio.models.count_tokens.await_args.kwargs['contents'], sent['contents'])
        config_factory.assert_called_once()
        self.assertIs(config_factory.call_args.kwargs['response_json_schema'], schema)
        self.assertEqual(sent['config'].response_json_schema, schema)
        self.assertEqual(sent['config'].max_output_tokens, 8192)
        self.assertEqual(sent['config'].candidate_count, 1)
        self.assertEqual(sent['contents'][1].inline_data.data, IMAGE)
        self.assertEqual(sent['contents'][1].inline_data.mime_type, 'image/png')

    async def test_schema_crossing_counted_prompt_budget_stops_before_generation(self):
        client = Mock()
        client.aio.models.generate_content = AsyncMock(return_value=completed_response())
        client.aio.models.count_tokens = AsyncMock(return_value=types.CountTokensResponse(total_tokens=1000))
        with patch.object(ai_coordinator, 'estimate_input_tokens', wraps=ai_rate_policy.estimate_input_tokens) as estimate, \
             patch.object(ai_coordinator, 'input_token_capacity', return_value=1500), \
             patch.object(ai_coordinator, 'acquire_model_slot', new=AsyncMock()) as acquire, \
             patch.object(ai_coordinator, 'register_api_error') as errors:
            result = await ocr_service._analyze_document_bytes(client, IMAGE,
                mime_type='image/png', file_name='synthetic.png', privacy_reviewed=True)

        estimate.assert_called_once()
        client.aio.models.count_tokens.assert_awaited_once()
        acquire.assert_not_awaited()
        client.aio.models.generate_content.assert_not_called()
        self.assertFalse(result['is_relevant'])
        self.assertEqual(result['error_code'], 'AI_DOCUMENT_INPUT_TOO_LARGE')
        errors.assert_not_called()

    async def test_unreviewed_image_stays_before_schema_estimation_and_sdk(self):
        client = Mock()
        client.aio.models.generate_content = AsyncMock()
        with patch.object(ocr_service, 'response_schema') as factory, \
             patch.object(ai_coordinator, 'estimate_input_tokens') as estimate, \
             patch.object(ai_coordinator, 'acquire_model_slot', new=AsyncMock()) as acquire:
            result = await ocr_service._analyze_document_bytes(client, IMAGE,
                mime_type='image/png', file_name='synthetic.png', privacy_reviewed=False)
        self.assertEqual(result['error_code'], 'AI_DOCUMENT_REVIEW_REQUIRED')
        factory.assert_not_called()
        estimate.assert_not_called()
        acquire.assert_not_awaited()
        client.aio.models.generate_content.assert_not_called()
